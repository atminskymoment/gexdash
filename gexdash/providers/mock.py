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
}

# Per-instrument ATM vol, so QQQ/NDX are not rendered with the S&P's vol level.
DEFAULT_ATM_VOL = {
    "SPX": 0.115, "SPY": 0.118, "ES": 0.115,
    "NDX": 0.155, "QQQ": 0.158, "NQ": 0.155,
}


class MockProvider:
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

    def get_spot(self, instrument: Instrument) -> float:
        base = self._spot_override or DEFAULT_SPOT.get(instrument.key, 100.0)
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
        self, instrument: Instrument, *, interval_minutes: int = 1, lookback_hours: float = 7.0
    ) -> PriceBars:
        """Synthesize an intraday OHLCV series.

        A seeded GBM walk with intraday volume shaped like a real session (a
        heavy open, a midday lull, a close ramp) so the candle panel and the
        VWAP line have something plausible to render. Invented, like the rest
        of this provider -- the UI banner says so.
        """
        base = self._spot_override or DEFAULT_SPOT.get(instrument.key, 100.0)
        n = max(int(lookback_hours * 60 / max(interval_minutes, 1)), 30)
        rng = np.random.default_rng(self.seed + 1_000)

        # Per-bar vol scaled off the instrument's ATM level, annualised ->
        # per-interval, so NDX candles are visibly wider than SPX's.
        annual = DEFAULT_ATM_VOL.get(instrument.key, 0.115)
        per_bar = annual / np.sqrt(252 * 390 / max(interval_minutes, 1))

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
        rng = np.random.default_rng(self.seed)
        spot = self._spot_override or DEFAULT_SPOT.get(instrument.key, 100.0)
        now = datetime.now(timezone.utc)

        step = instrument.strike_step
        atm = round(spot / step) * step
        # Width is per-instrument: the reference dashboard shows 61 strikes for
        # ES and 101 for NQ, because strike density differs enormously.
        half = instrument.display_strikes
        strikes = np.arange(atm - half * step, atm + (half + 1) * step, step)

        expiries = self._expiries(now, max_dte)
        rows: list[dict] = []

        for exp, dte in expiries:
            t = max(dte, 0.5) / 365.0
            # Base ATM vol: term structure rising with maturity, plus the
            # elevated 0DTE level that short-dated index options carry.
            base = DEFAULT_ATM_VOL.get(instrument.key, 0.115)
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
        for row in rows:
            if row["strike"] == strike and row["right"] == right and row["expiry"] == expiry:
                row["open_interest"] = oi
                row["volume"] = oi * 0.42
                return


def default_provider() -> MockProvider:
    return MockProvider()


__all__ = ["MockProvider", "default_provider", "DEFAULT_SPOT", "SPX", "ES"]
