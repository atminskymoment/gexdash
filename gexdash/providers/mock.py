"""Synthetic chain provider.

Exists so the dashboard runs, and the test suite passes, with no credentials
and no market hours. The generated chain is *shaped* like a real index chain
-- volatility skew, OI clustered on round strikes, a couple of deliberate
walls, volume concentrated near the money -- but the numbers are invented.

Everything is seeded, so the dashboard renders identically on every run. That
matters when you are eyeballing a layout change and need to know the data did
not move under you.

The banner in the UI says MOCK in orange for a reason: nothing here is a
market observation.
"""

from __future__ import annotations

import zlib
from datetime import datetime, timedelta, timezone

import numpy as np

from ..bars import PriceBars, build_bars
from ..breadth import BreadthSnapshot, build_snapshot
from ..chain import OptionChain, build_chain
from ..sessions import previous_session
from ..instruments import ES, SPX, Instrument

# Anchors chosen to match the reference dashboard screenshots so the layout can
# be compared side by side (ES 7774.32, NQ 29811.55). The cash/ETF levels are
# set at their usual ratios to those so cross-instrument comparisons look sane.
DEFAULT_SPOT = {
    "SPX": 7774.32,
    "SPY": 776.15,
    "ES": 7791.50,
    "NDX": 29811.55,
    "QQQ": 725.90,
    "NQ": 29855.25,
    "USO": 74.60,
}

# Per-instrument ATM vol, so QQQ/NDX are not rendered with the S&P's vol level.
DEFAULT_ATM_VOL = {
    "SPX": 0.115, "SPY": 0.118, "ES": 0.115,
    "NDX": 0.155, "QQQ": 0.158, "NQ": 0.155,
    # Crude is roughly twice as volatile as the equity indices.
    "USO": 0.34,
}


class MockProvider:
    """Seeded synthetic chains, spot, price history and breadth; no credentials.

    ``seed`` fixes the shape of the chain and the bar series, so they come
    out the same on every run. ``spot`` replaces every instrument's anchor
    price (otherwise :data:`DEFAULT_SPOT`): the chain and bars use it
    exactly, while :meth:`get_spot` walks around it.
    """

    name = "mock"

    def __init__(self, seed: int = 7, spot: float | None = None) -> None:
        self.seed = seed
        self._spot_override = spot
        self._drift = 0.0
        # One long-lived generator. Re-seeding per call from a value derived
        # from the drift was the original approach and it was a latent crash:
        # once drift went negative the derived seed did too, and
        # default_rng rejects a negative seed. A persistent generator is both
        # correct and more genuinely reproducible.
        self._rng = np.random.default_rng(seed)
        self._breadth_drift: dict[str, float] = {}

    # ------------------------------------------------------------------

    @staticmethod
    def _symbol_hash(instrument: Instrument) -> int:
        """A stable per-symbol number for typed (generic) symbols; 0 for the
        registered ones, whose synthetic series stay exactly as they were."""
        return zlib.crc32(instrument.key.encode()) if instrument.generic else 0

    def _anchor(self, instrument: Instrument) -> float:
        """The price a synthetic series is built around. A typed symbol gets
        one from its name, log-uniform between $10 and $2,000, so two tickers
        never draw the same chart."""
        if self._spot_override:
            return self._spot_override
        if instrument.key in DEFAULT_SPOT:
            return DEFAULT_SPOT[instrument.key]
        h = self._symbol_hash(instrument)
        return round(10 ** (1 + 2.3 * (h % 10_000) / 10_000), 2) if h else 100.0

    @staticmethod
    def _atm_vol(instrument: Instrument) -> float:
        """ATM vol: per registered instrument, or a single stock's ~28%."""
        return DEFAULT_ATM_VOL.get(instrument.key, 0.28 if instrument.generic else 0.115)

    def get_spot(self, instrument: Instrument) -> float:
        """The instrument's anchor price plus a random walk, rounded to cents.

        One drift is shared by every instrument and moves on each call, so
        consecutive ticks look live. It draws from the provider's single
        generator, so the path depends on the seed and on every draw before it.
        """
        base = self._anchor(instrument)
        # Proportional random walk, capped at +/-0.35%. An absolute step would
        # be 3% on a $776 ETF and invisible on a 29,800 index.
        self._drift = float(np.clip(self._drift + self._rng.normal(0, 4.5e-5), -0.0035, 0.0035))
        return round(base * (1.0 + self._drift), 2)

    def get_breadth(self, exchange: str) -> BreadthSnapshot:
        """Synthesize exchange breadth.

        Issue counts are scaled to each exchange's rough listing count so the
        magnitudes read plausibly (NYSE ~3,000 lines, Nasdaq ~4,300). The net
        is derived from the counts, so the cross-check in build_snapshot
        passes -- a mock that failed its own integrity check would train you
        to ignore the warning.
        """
        rng = np.random.default_rng(self.seed + (2_000 if exchange == "NYSE" else 3_000))
        total = 3000 if exchange == "NYSE" else 4300

        # Drift the tilt across calls, or every sample is identical and the
        # session A/D line renders dead flat -- which would read as a bug in
        # the sparkline rather than a property of the mock.
        self._breadth_drift[exchange] = float(np.clip(
            self._breadth_drift.get(exchange, 0.0) + self._rng.normal(0, 0.006), -0.09, 0.09))
        tilt = float(np.clip(rng.uniform(0.46, 0.56) + self._breadth_drift[exchange], 0.2, 0.8))
        advancing = float(int(total * tilt))
        declining = float(int(total * (1 - tilt) * 0.95))
        up_vol = float(int(rng.uniform(0.6, 2.4) * 1e9 * tilt))
        down_vol = float(int(rng.uniform(0.6, 2.4) * 1e9 * (1 - tilt)))

        return build_snapshot(
            exchange,
            {
                "advancing": advancing,
                "declining": declining,
                "net": advancing - declining,
                "up_volume": up_vol,
                "down_volume": down_vol,
                "tick": float(int(rng.normal(0, 450))),
                "trin": round(float(rng.uniform(0.55, 1.6)), 2),
            },
        )

    def get_price_history(
        self, instrument: Instrument, *, interval_minutes: int = 1,
        lookback_hours: float = 7.0, plan=None
    ) -> PriceBars:
        """Synthesize an intraday OHLCV series.

        A seeded GBM walk with intraday volume shaped like a real session (a
        heavy open, a midday lull, a close ramp) so the candle panel and the
        VWAP line have something plausible to render. Invented, like the rest
        of this provider -- the UI banner says so.
        """
        # A plan carries a range and an interval; translate both into a bar
        # count and a bar size so the synthetic series spans what was asked
        # for rather than a fixed session.
        if plan is not None:
            from ..history import INTERVALS, RANGES
            iv = INTERVALS[plan.served_interval_key or plan.interval_key]
            if plan.start is not None and plan.end is not None:
                # An explicit window is what the feed honours, so the mock
                # does too -- otherwise a caller that over-fetches for
                # indicator warm-up would get the display range back and
                # never exercise the warm-up at all.
                days = max((plan.end - plan.start).days, 1)
            else:
                rng_def = RANGES.get(plan.range_key)
                days = rng_def.days if rng_def and rng_def.days is not None else 365
            if iv.intraday:
                interval_minutes = iv.minutes
                n = max(int(days * 390 / iv.minutes), 30)
            else:
                per_year = {"daily": 252, "weekly": 52, "monthly": 12}[iv.kind]
                interval_minutes = {"daily": 1440, "weekly": 7 * 1440, "monthly": 30 * 1440}[iv.kind]
                n = max(int(days / 365 * per_year), 12)
            n = min(n, 4000)          # keep the payload sane on 10Y/1m asks
        else:
            n = max(int(lookback_hours * 60 / max(interval_minutes, 1)), 30)

        base = self._anchor(instrument)
        rng = np.random.default_rng(self.seed + 1_000 + self._symbol_hash(instrument))

        # Per-bar vol scaled off the instrument's ATM level, annualised ->
        # per-interval, so NDX candles are visibly wider than SPX's.
        annual = self._atm_vol(instrument)
        bars_per_year = 252 * 390 / max(interval_minutes, 1)
        per_bar = annual / np.sqrt(max(bars_per_year, 1.0))

        steps = rng.normal(0.0, per_bar, n)
        # Mild mean reversion so a long series does not wander off the strike grid.
        path = np.cumsum(steps) - 0.02 * np.cumsum(np.cumsum(steps)) / max(n, 1)
        closes = base * np.exp(path - path[-1])  # end the series at ~base

        opens = np.concatenate([[closes[0]], closes[:-1]])
        wick = np.abs(rng.normal(0.0, per_bar * 0.7, n)) * closes
        highs = np.maximum(opens, closes) + wick
        lows = np.minimum(opens, closes) - wick

        # U-shaped session volume: busy open and close, quiet midday.
        t = np.linspace(0, 1, n)
        shape = 0.4 + 1.6 * (1 - np.sin(np.pi * t)) ** 2 + 0.25
        volume = shape * rng.uniform(0.6, 1.4, n) * 90_000

        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        if plan is not None and plan.frequency_type == "minute" and not plan.extended_hours:
            # Regular hours only, as the feed serves them with
            # needExtendedHoursData=false: weekday 9:30-16:00 ET bars.
            times = _rth_times(now, n, interval_minutes)
        else:
            times = [now - timedelta(minutes=interval_minutes * (n - 1 - i)) for i in range(n)]

        return build_bars(
            instrument,
            [
                {
                    "time": times[i],
                    "open": float(opens[i]),
                    "high": float(highs[i]),
                    "low": float(lows[i]),
                    "close": float(closes[i]),
                    "volume": float(int(volume[i])),
                }
                for i in range(n)
            ],
            interval_minutes=interval_minutes,
            meta={"synthetic": True, "seed": self.seed},
        )

    def get_chain(self, instrument: Instrument, *, max_dte: float | None = None) -> OptionChain:
        """A synthetic chain around the instrument's anchor price.

        ``display_strikes`` strikes each side of the money, on expiries from
        the next 20:00 UTC settlement out to 60 days, cut at ``max_dte`` (at
        least one is always kept). Re-seeded on every call, so the random
        draws repeat (IV and OI still shift as time to expiry runs down), and
        its spot is the fixed anchor rather than the walk from :meth:`get_spot`. A call wall one strike above the
        money and a put wall one below are planted on the front expiry.
        """
        rng = np.random.default_rng(self.seed + self._symbol_hash(instrument))
        spot = self._anchor(instrument)
        now = datetime.now(timezone.utc)

        step = instrument.strike_step
        if step <= 0:
            # A typed symbol has no spec step: about 0.5% of spot, on the
            # 1-2.5-5 ladder listed strikes use.
            raw = spot * 0.005
            mag = 10 ** np.floor(np.log10(raw))
            step = float(next(m * mag for m in (1, 2.5, 5, 10) if m * mag >= raw))
        atm = round(spot / step) * step
        # Width is per-instrument: the reference dashboard shows 61 strikes for
        # ES and 101 for NQ, because strike density differs enormously.
        half = instrument.display_strikes
        strikes = np.arange(atm - half * step, atm + (half + 1) * step, step)
        strikes = strikes[strikes > 0]          # a grid around a cheap stock must not reach zero

        expiries = self._expiries(now, max_dte)
        rows: list[dict] = []

        for exp, dte in expiries:
            t = max(dte, 0.5) / 365.0
            # Base ATM vol: term structure rising with maturity, plus the
            # elevated 0DTE level that short-dated index options carry.
            base = self._atm_vol(instrument)
            atm_vol = base + 0.055 * np.sqrt(t * 12) + (0.035 if dte < 1.5 else 0.0)

            for k in strikes:
                moneyness = np.log(k / spot)
                # Equity index skew: puts bid, calls offered.
                skew = -0.55 * moneyness / max(np.sqrt(t), 0.05)
                smile = 2.2 * moneyness**2 / max(t, 0.002)
                iv = float(np.clip(atm_vol + skew * 0.09 + smile * 0.004, 0.055, 1.6))

                dist = abs(k - spot) / spot
                # OI decays away from the money and clusters on round strikes.
                decay = np.exp(-((dist / 0.022) ** 2))
                round_bonus = 2.4 if k % (step * 10) == 0 else (1.35 if k % (step * 4) == 0 else 1.0)
                near_term = 2.8 if dte < 2 else (1.5 if dte < 9 else 1.0)

                for right in ("C", "P"):
                    side = 1.25 if (right == "P" and k < spot) else (1.15 if (right == "C" and k > spot) else 0.7)
                    oi = decay * round_bonus * near_term * side * 9000 * rng.uniform(0.35, 1.65)
                    vol = oi * rng.uniform(0.05, 0.55) * (2.2 if dte < 2 else 0.7)
                    rows.append(
                        {
                            "strike": float(k),
                            "expiry": exp,
                            "right": right,
                            "iv": iv,
                            "open_interest": float(int(oi)),
                            "volume": float(int(vol)),
                        }
                    )

        # Two deliberate walls so level detection has something to find, sized
        # to dominate their expiry the way a real pinned strike does.
        self._plant_wall(rows, strike=atm + step, right="C", expiry=expiries[0][0], oi=145_000)
        self._plant_wall(rows, strike=atm - step, right="P", expiry=expiries[0][0], oi=118_000)

        chain = build_chain(
            instrument,
            spot,
            rows,
            asof=now,
            # OI is a prior-session OCC figure by definition -- never "now".
            oi_asof=previous_session(),
            meta={"synthetic": True, "seed": self.seed},
        )
        return chain

    # ------------------------------------------------------------------

    @staticmethod
    def _expiries(now: datetime, max_dte: float | None) -> list[tuple[datetime, float]]:
        """0DTE, the next few dailies, then weeklies out to the cap.

        The near expiry is anchored to the next settlement *in the future*.
        Anchoring it to today's 16:00 ET unconditionally means that whenever
        the generator runs after the close, the "0DTE" contracts are already
        settled -- which produces singular greeks rather than a useful chain.
        """
        first = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if first <= now:
            first = first + timedelta(days=1)

        offsets = [0, 1, 2, 3, 7, 14, 21, 30, 45, 60]
        out = []
        for d in offsets:
            exp = first + timedelta(days=d)
            dte = (exp - now).total_seconds() / 86400.0
            if max_dte is not None and dte > max_dte:
                continue
            out.append((exp, dte))
        return out or [(first, (first - now).total_seconds() / 86400.0)]

    @staticmethod
    def _plant_wall(rows: list[dict], *, strike: float, right: str, expiry: datetime, oi: float) -> None:
        """Set one contract's OI (and its volume to 42% of that) in place.
        Does nothing if no row matches."""
        for row in rows:
            if row["strike"] == strike and row["right"] == right and row["expiry"] == expiry:
                row["open_interest"] = oi
                row["volume"] = oi * 0.42
                return


def default_provider() -> MockProvider:
    """A :class:`MockProvider` with the default seed."""
    return MockProvider()


__all__ = ["MockProvider", "default_provider", "DEFAULT_SPOT", "SPX", "ES"]


def _rth_times(now: datetime, n: int, minutes: int) -> list[datetime]:
    """The last ``n`` bar-open times of US regular sessions up to ``now``,
    oldest first: weekdays, 9:30 to 16:00 America/New_York."""
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    out: list[datetime] = []
    day = now.astimezone(et).date()
    while len(out) < n:
        if day.weekday() < 5:
            open_ = datetime(day.year, day.month, day.day, 9, 30, tzinfo=et)
            slots = [open_ + timedelta(minutes=minutes * k) for k in range(390 // minutes + (390 % minutes > 0))]
            out.extend(t.astimezone(timezone.utc) for t in reversed(slots) if t <= now)
        day -= timedelta(days=1)
    return list(reversed(out[:n]))
