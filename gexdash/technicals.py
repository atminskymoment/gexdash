"""The technical chart: moving averages, bands, volume profile, ranges, the
VAMS state, position signals and Supertrend, over intraday, daily or weekly
bars.

Two kinds of thing live here, and the difference matters:

**Standard indicators** -- SMMA 20/50/100/200, EMA 9, Bollinger 20/2 and
20/3, RSI 14, MACD 12/26/9, ATR 14, volume profile, 3-month and 52-week
extremes. These have published definitions and are computed exactly, with
the smoothing each one specifies (see ``indicators.py``).

**gexdash rules** -- the VAMS state, range boxes with breakouts and
breakdowns, and BUY / ADD / TRIM / SELL. The chart these are modelled on
draws labels of those names from a private script whose rules are not
published. Rather than invent numbers that look like the reference's, this
module defines its own rules, writes every one of them down in
:data:`RULES`, and ships that text with the payload so the UI can show
exactly what produced each marker. Change the thresholds in
:class:`TechParams`; do not mistake the output for the reference script.

**Warm-up.** SMMA(200) is Wilder smoothing seeded with a 200-bar mean, and
the seed's weight decays as ``(1 - 1/200)^k`` -- still 13% after 400 bars.
A chart that computed it over only the visible year would draw a line that
is simply wrong at the left edge and slowly converges. So the feed is asked
for everything it serves at the bar size (10 years daily, 20 years weekly),
every series is computed over that, and only then trimmed to the window the
user asked for. The 52-week extreme needs the same: a one-year window cannot
know the high from eleven months before it starts.

**Intraday** cannot reach that far -- 1-minute bars about 48 days, coarser
ones about 260 -- so each bar size fetches the longest
range it offers plus about :data:`WARMUP_BARS` bars, and the payload says so
whenever SMMA 200 still had too little history at the left edge. The 3M/52W
levels on an intraday chart come from daily bars, not from bars-per-year of
minute data. Sessions follow TradingView's defaults: US ETFs in regular hours
(extended hours on request), CME index futures over the full electronic
session; hourly and 4-hour bars are anchored to the session open, and the
1D/5D/10D ranges count sessions, not 24-hour spans.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from .bars import PriceBars, build_bars
from .history import Plan
from .sessions import session_dates as _session_dates
from .indicators import _clean, atr, bollinger, ema, macd, rsi, smma, supertrend

__all__ = [
    "TechParams", "TechInterval", "TECH_RANGES", "TECH_INTERVALS", "RULES", "SESSIONS",
    "plan_for", "compute", "capabilities", "session_kind", "display_start_for",
    "aggregate_session", "session_dates", "vams_state", "range_events", "position_signals",
    "volume_profile", "extreme_levels",
]

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class TechParams:
    """Every threshold the chart uses. Echoed in the payload, never hidden."""

    smma_lengths: tuple[int, ...] = (20, 50, 100, 200)
    ema_length: int = 9
    bb_length: int = 20
    bb_mults: tuple[float, ...] = (2.0, 3.0)
    rsi_length: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    atr_length: int = 14

    # VAMS -- volatility-adjusted moving-average state.
    vams_basis: int = 20
    vams_atr_mult: float = 0.5
    vams_decay_bars: int = 5

    # Ranges.
    range_length: int = 20
    base_atr_mult: float = 5.0
    breakout_cooldown: int = 10

    # Position signals.
    trend_length: int = 200
    pullback_length: int = 50
    trim_rsi: float = 70.0
    trim_spacing: int = 10
    add_spacing: int = 10

    profile_bins: int = 48

    # Supertrend -- the pasted TradingView script's inputs and defaults.
    st_period: int = 10
    st_mult: float = 3.0
    st_wilder: bool = True        # "Change ATR Calculation Method ?" = true


#: Display ranges: ("sessions", n) counts trading sessions -- a 1D intraday
#: chart on Monday morning is Friday's session, not the last 24 hours --
#: ("days", n) is calendar days, ("ytd", None) since 1 January.
TECH_RANGES: dict[str, tuple[str, int | None, str]] = {
    "1D": ("sessions", 1, "1 day"),
    "5D": ("sessions", 5, "5 days"),
    "10D": ("sessions", 10, "10 days"),
    "1M": ("days", 30, "1 month"),
    "3M": ("days", 90, "3 months"),
    "6M": ("days", 180, "6 months"),
    "YTD": ("ytd", None, "Year to date"),
    "1Y": ("days", 365, "1 year"),
    "2Y": ("days", 730, "2 years"),
    "5Y": ("days", 1825, "5 years"),
}


@dataclass(frozen=True)
class TechInterval:
    """A bar size the Chart view offers, with the ranges it allows.

    Separate from ``history.Interval``, which serves the terminal price
    panel: each size here also fixes how far back it fetches, so that
    indicators have warm-up history before the displayed range.
    """

    key: str
    label: str
    kind: str                      # "minute" | "daily" | "weekly"
    #: Intraday bar size, and the native feed size it is built from (1/5/15/30).
    minutes: int | None
    native: int | None
    #: Daily/weekly: bars in a year, the 52-week window.
    bars_per_year: int | None
    #: Calendar days fetched at most. The feed reaches ~260 days at 5m and up
    #: (history.py); 1-minute is held to 11 days, which covers 5D plus the
    #: warm-up -- a longer window would only cost transfer time.
    reach_days: int
    #: Display ranges offered. Bounded by reach and by drawing cost: every
    #: pair stays at or under about 7,000 bars even over a 23-hour futures session.
    ranges: tuple[str, ...]
    default_range: str

    @property
    def intraday(self) -> bool:
        """True for the minute-based sizes, 1m to 4h."""
        return self.kind == "minute"


TECH_INTERVALS: dict[str, TechInterval] = {i.key: i for i in [
    TechInterval("1m", "1 min", "minute", 1, 1, None, 11, ("1D", "5D"), "1D"),
    TechInterval("5m", "5 min", "minute", 5, 5, None, 250, ("1D", "5D", "10D", "1M"), "5D"),
    TechInterval("15m", "15 min", "minute", 15, 15, None, 250, ("1D", "5D", "10D", "1M", "3M"), "10D"),
    TechInterval("30m", "30 min", "minute", 30, 30, None, 250, ("5D", "10D", "1M", "3M", "6M"), "1M"),
    TechInterval("1h", "1 hour", "minute", 60, 30, None, 250, ("5D", "10D", "1M", "3M", "6M"), "3M"),
    TechInterval("4h", "4 hours", "minute", 240, 30, None, 250, ("1M", "3M", "6M"), "6M"),
    TechInterval("1D", "Daily", "daily", None, None, 252, 3650,
                 ("3M", "6M", "YTD", "1Y", "2Y", "5Y"), "1Y"),
    TechInterval("1W", "Weekly", "weekly", None, None, 52, 7300,
                 ("6M", "YTD", "1Y", "2Y", "5Y"), "5Y"),
]}

#: Intraday sessions. ``anchor`` is where hourly and 4-hour bars start, in ET
#: minutes after midnight; ``date_shift`` moves a bar into the session it
#: belongs to (a futures session opening Sunday 18:00 is Monday's);
#: ``minutes`` is a typical session length, used only to size the warm-up.
SESSIONS: dict[str, dict] = {
    "rth": {"label": "Regular hours", "anchor": 9 * 60 + 30, "date_shift": 0, "minutes": 390},
    "eth": {"label": "Extended hours", "anchor": 4 * 60, "date_shift": 4, "minutes": 960},
    "futures": {"label": "Full session", "anchor": 18 * 60, "date_shift": 6, "minutes": 1380},
}

#: Bars of history wanted before an intraday window: SMMA 200's seed weighs
#: (1 - 1/200)^k, under 1% after ~900 bars.
WARMUP_BARS = 1000

#: Seconds an intraday fetch is reused for; daily and weekly use the server's
#: longer TTL. The client refreshes intraday charts every 30 seconds.
INTRADAY_TTL_SECONDS = 25.0

#: The written definition of every gexdash rule, sent with each payload.
RULES: list[dict[str, str]] = [
    {"key": "vams_check", "name": "VAMS check",
     "text": "Each bar's close against SMMA 20 ± 0.5 × ATR 14. Above the upper trigger is a "
             "bullish call, below the lower trigger a bearish call, between them no call."},
    {"key": "vams", "name": "VAMS state",
     "text": "Latches the last bullish or bearish call and holds it through no-call bars; "
             "after 5 consecutive no-call bars it decays to neutral. The →Bullish and "
             "→Bearish levels are the current triggers."},
    {"key": "range", "name": "Range",
     "text": "The high and low of the prior 20 bars. It counts as a base when that span is "
             "at most 5 × ATR 14 — a consolidation, not just any 20 bars."},
    {"key": "breakout", "name": "Breakout / Breakdown",
     "text": "A close above a base's high (breakout) or below its low (breakdown). The base "
             "is drawn as a dotted box. 10-bar cooldown per direction."},
    {"key": "buy", "name": "BUY",
     "text": "From flat: VAMS state bullish with the close above both SMMA 50 and SMMA 200."},
    {"key": "add", "name": "ADD",
     "text": "While long: the bar's low tests SMMA 50 and the close holds above it, at least "
             "10 bars after the last BUY or ADD."},
    {"key": "trim", "name": "TRIM",
     "text": "While long: RSI 14 crosses back down through 70 — momentum rolling over from "
             "overbought. At most one TRIM per 10 bars."},
    {"key": "sell", "name": "SELL",
     "text": "While long: VAMS state bearish and the close below SMMA 50."},
    {"key": "supertrend", "name": "Supertrend",
     "text": "Not a gexdash rule: a line-for-line port of the TradingView \"Supertrend\" script "
             "(v4). Bands at hl2 ∓ multiplier × ATR; the lower band only rises and the upper only "
             "falls while the previous close holds them. The trend flips when the close crosses the "
             "previous bar's opposite band; Buy and Sell mark the flip, drawn at the band. "
             "Inputs are listed under params (st_period, st_mult, st_wilder)."},
    {"key": "earnings", "name": "E (earnings)",
     "text": "Not drawn. ETFs and index futures do not report earnings, and substituting "
             "ex-dividend or roll dates would put a different event under the same mark."},
]


# ---------------------------------------------------------------- planning


def session_kind(interval_key: str, *, is_future: bool, requested: str = "auto") -> tuple[str | None, list[str]]:
    """The session an intraday chart is drawn over, and any note about it.

    Daily and weekly bars have no session choice. ETFs default to regular
    hours, which is what TradingView shows unless extended hours are switched
    on; index futures are always drawn over the full session.
    """
    if not TECH_INTERVALS[interval_key].intraday:
        return None, []
    if is_future:
        note = ["Futures are drawn over the full electronic session; regular hours "
                "apply to the ETFs."] if requested == "rth" else []
        return "futures", note
    return ("eth" if requested == "eth" else "rth"), []


def _range_calendar_days(range_key: str, now: datetime) -> int:
    """Calendar days a display range spans, session-counted ranges padded
    for a weekend and a holiday."""
    mode, n, _ = TECH_RANGES[range_key]
    if mode == "sessions":
        # Sessions to calendar days, with room for a weekend and a holiday.
        return math.ceil(n * 7 / 5) + 4
    if mode == "ytd":
        return max((now.date() - date(now.year, 1, 1)).days, 1)
    return n


def _intraday_fetch_days(iv: TechInterval, session: str, now: datetime) -> int:
    """One fixed window per (bar size, session): the longest range offered
    plus the warm-up, within the feed's reach. Fixed so one fetch serves
    every range of that bar size from the cache."""
    longest = max(_range_calendar_days(r, now) for r in iv.ranges)
    per_session = SESSIONS[session]["minutes"]
    warm_sessions = math.ceil(WARMUP_BARS * iv.minutes / per_session)
    warm_days = math.ceil(warm_sessions * 7 / 5) + 3
    return min(iv.reach_days, longest + warm_days)


def plan_for(
    range_key: str, interval_key: str, *, now: datetime | None = None, session: str | None = None,
) -> tuple[Plan, datetime | None]:
    """The provider request and the first timestamp to display.

    The request reaches as far back as the bar size needs for warm-up (all of
    the feed's history for daily and weekly); the display window is applied
    after the indicators are computed. ``display_start`` is None for the
    session-counted ranges (1D/5D/10D): those are resolved from the bars, by
    :func:`display_start_for`.
    """
    if range_key not in TECH_RANGES:
        raise ValueError(f"unknown range {range_key!r}; known: {list(TECH_RANGES)}")
    if interval_key not in TECH_INTERVALS:
        raise ValueError(f"unknown interval {interval_key!r}; known: {list(TECH_INTERVALS)}")
    iv = TECH_INTERVALS[interval_key]
    if range_key not in iv.ranges:
        raise ValueError(f"{range_key} is not offered at {iv.label}; offered: {list(iv.ranges)}")

    now = now or datetime.now(timezone.utc)
    mode, n, _ = TECH_RANGES[range_key]
    if mode == "ytd":
        display_start: datetime | None = datetime(now.year, 1, 1, tzinfo=timezone.utc)
    elif mode == "days":
        display_start = now - timedelta(days=n)
    else:
        display_start = None

    if iv.intraday:
        session = session or "rth"
        days = _intraday_fetch_days(iv, session, now)
        plan = Plan(
            range_key=range_key, interval_key=interval_key,
            period_type="day", period=10, frequency_type="minute", frequency=iv.native,
            # Always an explicit window ending now. The period form without
            # dates ends at the previous session's close, so it never
            # contains today.
            start=now - timedelta(days=days), end=now,
            served_interval_key=interval_key, extended_hours=session != "rth",
        )
        return plan, display_start

    plan = Plan(
        range_key=range_key, interval_key=interval_key,
        period_type="year", period=1, frequency_type=iv.kind, frequency=1,
        # A few days inside the measured reach, so a request on the boundary
        # does not come back quietly truncated.
        start=now - timedelta(days=iv.reach_days - 5), end=now,
        served_interval_key=interval_key,
    )
    return plan, display_start


def _et(times: np.ndarray) -> list[datetime]:
    """Bar times (naive UTC ``datetime64``) as timezone-aware ET datetimes."""
    epoch = times.astype("datetime64[s]").astype("int64")
    return [datetime.fromtimestamp(int(t), tz=timezone.utc).astimezone(ET) for t in epoch]


def session_dates(times: np.ndarray, session: str) -> list[date]:
    """The trading session each bar belongs to, in exchange time."""
    return _session_dates(times, SESSIONS[session]["date_shift"])


def display_start_for(
    range_key: str, bars: PriceBars, *, session: str | None, fallback: datetime | None,
) -> datetime:
    """First displayed timestamp. Session-counted ranges take the opening
    bar of the Nth most recent session present in the bars."""
    mode, n, _ = TECH_RANGES[range_key]
    if mode != "sessions" or session is None or len(bars) == 0:
        return fallback or datetime(1970, 1, 1, tzinfo=timezone.utc)
    dates = session_dates(bars.time, session)
    keep = sorted(set(dates))[-n:]
    first = next(i for i, d in enumerate(dates) if d >= keep[0])
    return datetime.fromtimestamp(
        int(bars.time[first].astype("datetime64[s]").astype("int64")), tz=timezone.utc)


def aggregate_session(bars: PriceBars, minutes: int, session: str) -> PriceBars:
    """Coarser bars anchored to the session open, as TradingView builds them:
    regular-hours hourly bars at 9:30, 10:30, ... 15:30 (the last one half
    an hour), futures 4-hour bars at 18:00, 22:00, 02:00 ... ET.

    :func:`history.aggregate` buckets on UTC epoch boundaries, which gives a
    9:30-10:00 stub and then 10:00, 11:00 ...; indicators computed on those
    bars differ from a platform's, Supertrend's flips included. Buckets are
    wall-clock in ET, so they survive daylight-saving changes.
    """
    n = len(bars)
    if n == 0 or minutes <= bars.interval_minutes:
        return bars
    anchor = timedelta(minutes=SESSIONS[session]["anchor"])
    keys = np.empty(n, dtype=np.int64)
    for k, t in enumerate(_et(bars.time)):
        shifted = t - anchor                      # wall-clock arithmetic in ET
        keys[k] = shifted.toordinal() * 10_000 + (shifted.hour * 60 + shifted.minute) // minutes
    starts = np.flatnonzero(np.r_[True, keys[1:] != keys[:-1]])
    ends = np.r_[starts[1:] - 1, n - 1]
    rows = [{
        "time": bars.time[a], "open": float(bars.open[a]), "close": float(bars.close[b]),
        "high": float(hi), "low": float(lo), "volume": float(vol),
    } for a, b, hi, lo, vol in zip(
        starts, ends,
        np.maximum.reduceat(bars.high, starts), np.minimum.reduceat(bars.low, starts),
        np.add.reduceat(bars.volume, starts),
    )]
    return build_bars(bars.instrument, rows, interval_minutes=minutes,
                      meta={**(bars.meta or {}), "aggregated_from_minutes": bars.interval_minutes,
                            "anchored_to": session})


def capabilities() -> dict:
    """What the Chart view may offer: intervals with their ranges, sessions."""
    return {
        "symbols": ["QQQ", "SPY", "USO", "ES", "NQ"],
        "ranges": {k: label for k, (_, _, label) in TECH_RANGES.items()},
        "intervals": [
            {"key": iv.key, "label": iv.label, "intraday": iv.intraday,
             "ranges": list(iv.ranges), "default_range": iv.default_range}
            for iv in TECH_INTERVALS.values()
        ],
        "sessions": {k: v["label"] for k, v in SESSIONS.items()},
        "full_session_only": ["ES", "NQ"],
        "refresh_seconds": {"intraday": 30, "daily": 60},
    }


# ------------------------------------------------------------------- rules


def vams_state(
    close: np.ndarray, basis: np.ndarray, atr_values: np.ndarray, *, mult: float, decay: int
) -> dict[str, np.ndarray]:
    """VAMS check and latched state. See :data:`RULES`.

    Returns ``check`` and ``state`` as float arrays of +1 / -1 / 0, nan where
    the basis or ATR is still warming up -- a ribbon must not claim a state
    on a bar where its inputs do not exist yet.
    """
    close = np.asarray(close, dtype=float)
    upper = basis + mult * atr_values
    lower = basis - mult * atr_values
    valid = np.isfinite(upper) & np.isfinite(lower)

    check = np.full(len(close), np.nan)
    check[valid] = 0.0
    check[valid & (close > upper)] = 1.0
    check[valid & (close < lower)] = -1.0

    out = np.full(len(close), np.nan)
    current, quiet = 0.0, 0
    for i in np.flatnonzero(valid):
        if check[i] != 0:
            current, quiet = check[i], 0
        else:
            quiet += 1
            if quiet >= decay:
                current = 0.0
        out[i] = current
    return {"check": check, "state": out, "upper": upper, "lower": lower}


def range_events(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, atr_values: np.ndarray, *,
    length: int, base_mult: float, cooldown: int,
) -> dict:
    """Prior-``length``-bar ranges, which of them are bases, and the bars that
    closed out of a base. See :data:`RULES`."""
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    close = np.asarray(close, dtype=float)
    n = len(close)

    prior_high = np.full(n, np.nan)
    prior_low = np.full(n, np.nan)
    for i in range(length, n):
        prior_high[i] = high[i - length : i].max()
        prior_low[i] = low[i - length : i].min()

    events: list[dict] = []
    last = {"breakout": -(10**9), "breakdown": -(10**9)}
    for i in range(length + 1, n):
        a = atr_values[i - 1]
        if not np.isfinite(a) or not np.isfinite(prior_high[i]):
            continue
        # The base is judged on the bar *before* the break, by the ATR that
        # was known then -- the breakout bar's own range must not decide
        # whether what it broke out of was tight.
        if prior_high[i] - prior_low[i] > base_mult * a:
            continue
        kind = ("breakout" if close[i] > prior_high[i]
                else "breakdown" if close[i] < prior_low[i] else None)
        if kind is None or i - last[kind] < cooldown:
            continue
        last[kind] = i
        events.append({
            "i": i, "type": kind, "price": float(close[i]),
            "box": {"start": i - length, "end": i,
                    "high": float(prior_high[i]), "low": float(prior_low[i])},
        })

    current = None
    if n >= length:
        hi, lo = float(high[-length:].max()), float(low[-length:].min())
        a = atr_values[-1]
        current = {
            "start": n - length, "end": n - 1, "high": hi, "low": lo,
            "is_base": bool(np.isfinite(a) and hi - lo <= base_mult * a),
        }
    return {"events": events, "current": current}


def position_signals(
    close: np.ndarray, low: np.ndarray, state: np.ndarray,
    trend_ma: np.ndarray, pullback_ma: np.ndarray, rsi_values: np.ndarray, *,
    trim_rsi: float, add_spacing: int, trim_spacing: int = 10,
) -> dict:
    """BUY / ADD / TRIM / SELL from one long-or-flat position. See :data:`RULES`.

    A single position is what makes the four labels coherent: there is no
    SELL without a prior BUY, and no ADD or TRIM while flat. Evaluated on
    each bar's close, exits first -- a bar that both breaks down and tests
    the 50 is a SELL, not an ADD.
    """
    n = len(close)
    out: list[dict] = []
    long, last_entry, last_trim = False, -(10**9), -(10**9)
    for i in range(1, n):
        if not (np.isfinite(trend_ma[i]) and np.isfinite(pullback_ma[i]) and np.isfinite(state[i])):
            continue
        if long:
            if state[i] < 0 and close[i] < pullback_ma[i]:
                out.append({"i": i, "type": "SELL", "price": float(close[i])})
                long = False
                continue
            r0, r1 = rsi_values[i - 1], rsi_values[i]
            if (np.isfinite(r0) and np.isfinite(r1) and r0 >= trim_rsi > r1
                    and i - last_trim >= trim_spacing):
                out.append({"i": i, "type": "TRIM", "price": float(close[i])})
                last_trim = i
            elif low[i] <= pullback_ma[i] < close[i] and i - last_entry >= add_spacing:
                out.append({"i": i, "type": "ADD", "price": float(close[i])})
                last_entry = i
        elif state[i] > 0 and close[i] > pullback_ma[i] and close[i] > trend_ma[i]:
            out.append({"i": i, "type": "BUY", "price": float(close[i])})
            long, last_entry = True, i
    return {"signals": out, "position": "long" if long else "flat"}


def volume_profile(
    open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
    volume: np.ndarray, *, bins: int,
) -> dict | None:
    """Volume by price over the bars given, split by bar direction.

    Each bar's volume is spread across the price bins its high-low range
    overlaps, in proportion to the overlap -- assigning it all to the close
    would pile a wide bar's volume onto one price it may barely have traded
    at. Up-bar volume and down-bar volume are kept apart so the profile can
    show which side dominated at each price.
    """
    high = np.asarray(high, dtype=float)
    low = np.asarray(low, dtype=float)
    vol = np.maximum(np.asarray(volume, dtype=float), 0.0)
    if len(high) == 0 or not np.isfinite(vol).any() or vol.sum() <= 0:
        return None

    lo, hi = float(low.min()), float(high.max())
    if hi <= lo:
        return None
    edges = np.linspace(lo, hi, bins + 1)

    span = high - low
    overlap = np.clip(
        np.minimum(high[:, None], edges[None, 1:]) - np.maximum(low[:, None], edges[None, :-1]),
        0.0, None,
    )
    weight = np.where(span[:, None] > 0, overlap / np.where(span > 0, span, 1.0)[:, None], 0.0)
    # A zero-range bar traded at one price: all of it goes in that price's bin.
    flat = span <= 0
    if flat.any():
        idx = np.clip(np.searchsorted(edges, high[flat], side="right") - 1, 0, bins - 1)
        weight[np.flatnonzero(flat), idx] = 1.0

    up = np.asarray(close, dtype=float) >= np.asarray(open_, dtype=float)
    up_vol = (weight * (vol * up)[:, None]).sum(axis=0)
    down_vol = (weight * (vol * ~up)[:, None]).sum(axis=0)
    k = int(np.argmax(up_vol + down_vol))
    return {
        "edges": [round(float(e), 6) for e in edges],
        "up": [round(float(v), 2) for v in up_vol],
        "down": [round(float(v), 2) for v in down_vol],
        "poc": round(float((edges[k] + edges[k + 1]) / 2), 6),
    }


def extreme_levels(high: np.ndarray, low: np.ndarray, *, bars_per_year: int) -> list[dict]:
    """3-month and 52-week high/low, merged where they coincide.

    Computed over the full fetched series, not the display window: a 3M
    chart still has a 52-week high, and it is usually off the left edge.
    """
    n = len(high)
    out: list[dict] = []
    if n == 0:
        return out
    k3, k52 = max(bars_per_year // 4, 1), bars_per_year
    partial = n < k52
    pairs = [
        ("high", float(np.max(high[-k3:])), float(np.max(high[-k52:]))),
        ("low", float(np.min(low[-k3:])), float(np.min(low[-k52:]))),
    ]
    for side, three, year in pairs:
        if three == year:
            out.append({"key": f"{side}_3m_52w", "label": f"3M/52W {side}", "price": year,
                        "partial": partial})
        else:
            out.append({"key": f"{side}_3m", "label": f"3M {side}", "price": three, "partial": False})
            out.append({"key": f"{side}_52w", "label": f"52W {side}", "price": year,
                        "partial": partial})
    return out


# ----------------------------------------------------------------- compute


def compute(
    bars: PriceBars, *, display_start: datetime, interval: str = "1D",
    params: TechParams | None = None, session: str | None = None,
    daily: PriceBars | None = None,
) -> dict:
    """Every series, level and signal, computed over the full history and
    trimmed to the bars at or after ``display_start``.

    Intraday charts pass their ``session`` (for the session breaks on the time
    axis) and ``daily`` bars, from which the 3M/52W levels are taken.
    """
    p = params or TechParams()
    iv = TECH_INTERVALS[interval]
    bars_per_year = iv.bars_per_year
    o, h, l, c, v = bars.open, bars.high, bars.low, bars.close, bars.volume
    n = len(c)

    start_ts = np.datetime64(display_start.astimezone(timezone.utc).replace(tzinfo=None), "s")
    first = int(np.searchsorted(bars.time, start_ts, side="left"))
    first = min(first, max(n - 1, 0))
    window = slice(first, n)

    series: dict[str, np.ndarray] = {}
    for length in p.smma_lengths:
        series[f"smma{length}"] = smma(c, length)
    series[f"ema{p.ema_length}"] = ema(c, p.ema_length)
    # ema() seeds with the first value, so it exists from bar 0 -- but it is
    # not meaningful until it has seen a period's worth of closes.
    series[f"ema{p.ema_length}"][: p.ema_length - 1] = np.nan
    for mult in p.bb_mults:
        b = bollinger(c, p.bb_length, mult)
        tag = f"bb{p.bb_length}_{mult:g}"
        series[f"{tag}_upper"], series[f"{tag}_lower"] = b["upper"], b["lower"]
        series["bb_basis"] = b["basis"]
    atr_values = atr(h, l, c, p.atr_length)
    series["atr"] = atr_values
    rsi_values = rsi(c, p.rsi_length)
    series["rsi"] = rsi_values
    m = macd(c, p.macd_fast, p.macd_slow, p.macd_signal)
    # MACD's EMAs are also seeded at bar 0; blank them until the slow EMA and
    # then the signal line have had their periods.
    for key in ("macd", "signal", "histogram"):
        m[key][: p.macd_slow - 1] = np.nan
    m["signal"][: p.macd_slow + p.macd_signal - 2] = np.nan
    m["histogram"][: p.macd_slow + p.macd_signal - 2] = np.nan
    series["macd"], series["macd_signal"], series["macd_histogram"] = m["macd"], m["signal"], m["histogram"]

    basis = series.get(f"smma{p.vams_basis}")
    if basis is None:
        basis = smma(c, p.vams_basis)
    vams = vams_state(c, basis, atr_values, mult=p.vams_atr_mult, decay=p.vams_decay_bars)
    series["vams_upper"], series["vams_lower"] = vams["upper"], vams["lower"]

    ranges = range_events(h, l, c, atr_values, length=p.range_length,
                          base_mult=p.base_atr_mult, cooldown=p.breakout_cooldown)
    trend = series.get(f"smma{p.trend_length}")
    if trend is None:
        trend = smma(c, p.trend_length)
    pull = series.get(f"smma{p.pullback_length}")
    if pull is None:
        pull = smma(c, p.pullback_length)
    pos = position_signals(c, l, vams["state"], trend, pull, rsi_values,
                           trim_rsi=p.trim_rsi, add_spacing=p.add_spacing,
                           trim_spacing=p.trim_spacing)

    st = supertrend(h, l, c, period=p.st_period, mult=p.st_mult, wilder_atr=p.st_wilder)
    # Drawn as the script draws them: the lower band while the trend is up,
    # the upper band while it is down, broken where the trend flips.
    series["st_up"] = np.where(st["trend"] == 1, st["up"], np.nan)
    series["st_dn"] = np.where(st["trend"] == -1, st["dn"], np.nan)

    # Re-index everything that refers to a bar into the display window.
    def shift(i: int) -> int:
        """Index in the full series to index in the display window."""
        return i - first

    signals = [
        {**s, "i": shift(s["i"])} for s in pos["signals"] if s["i"] >= first
    ] + [
        {"i": shift(e["i"]), "type": e["type"].upper(), "price": e["price"]}
        for e in ranges["events"] if e["i"] >= first
    ]
    signals.sort(key=lambda s: s["i"])

    boxes = []
    for e in ranges["events"]:
        if e["i"] < first:
            continue
        b = e["box"]
        boxes.append({"start": max(shift(b["start"]), 0), "end": shift(b["end"]),
                      "high": b["high"], "low": b["low"], "outcome": e["type"]})
    current_range = None
    if ranges["current"]:
        cr = ranges["current"]
        current_range = {**cr, "start": max(shift(cr["start"]), 0), "end": shift(cr["end"])}

    st_signals = [
        {"i": shift(int(i)), "type": "ST_BUY", "price": float(st["up"][i])}
        for i in np.flatnonzero(st["buy"]) if i >= first
    ] + [
        {"i": shift(int(i)), "type": "ST_SELL", "price": float(st["dn"][i])}
        for i in np.flatnonzero(st["sell"]) if i >= first
    ]
    st_signals.sort(key=lambda sg: sg["i"])
    st_last = int(st["trend"][-1])
    st_line = st["up"][-1] if st_last == 1 else st["dn"][-1]

    profile = volume_profile(o[window], h[window], l[window], c[window], v[window], bins=p.profile_bins)

    notes: list[str] = []
    if not iv.intraday:
        levels = extreme_levels(h, l, bars_per_year=bars_per_year)
    elif daily is not None and len(daily):
        # Price levels hold on any bar size; they are measured on daily bars.
        levels = extreme_levels(daily.high, daily.low, bars_per_year=TECH_INTERVALS["1D"].bars_per_year)
    else:
        levels = []
        notes.append("3M/52W levels need daily history, which did not load.")
    if profile:
        levels.append({"key": "poc", "label": "PoC", "price": profile["poc"], "partial": False})

    last_state, last_check = vams["state"][-1], vams["check"][-1]
    names = {1.0: "bullish", -1.0: "bearish", 0.0: "neutral"}

    # Warm-up: say how much history preceded the window, and flag any series
    # that still had not converged by its left edge.
    longest = max(p.smma_lengths)
    if first < 3 * longest:
        notes.append(
            f"only {first} bars precede the window; SMMA {longest} wants ~{3 * longest} to "
            "converge, so its left edge may differ from a chart with a longer history."
        )
    if bars_per_year and n < bars_per_year:
        notes.append(f"{n} bars of history: the 52-week levels cover less than a year.")

    # Where each session starts inside the window, for the intraday time axis.
    session_breaks: list[int] = []
    session_labels: list[str] = []
    if iv.intraday and session:
        dates = session_dates(bars.time, session)
        for i in range(first, n):
            if i == 0 or dates[i] != dates[i - 1]:
                session_breaks.append(i - first)
                session_labels.append(dates[i].isoformat())   # the session's date, not the bar's

    trimmed = {k: _clean(arr[window]) for k, arr in series.items()}
    return {
        "interval": interval,
        "session": session,
        "session_breaks": session_breaks,
        "session_labels": session_labels,
        "time": [str(t) for t in bars.time[window]],
        "open": o[window].tolist(),
        "high": h[window].tolist(),
        "low": l[window].tolist(),
        "close": c[window].tolist(),
        "volume": v[window].tolist(),
        # Previous close for the first displayed bar, so its change is real
        # rather than measured against its own open.
        "prev_close": float(c[first - 1]) if first > 0 else None,
        "series": trimmed,
        "vams": {
            "check": _clean(vams["check"][window]),
            "state": _clean(vams["state"][window]),
            "current": {
                "state": names.get(float(last_state), "n/a") if np.isfinite(last_state) else "n/a",
                "check": (names.get(float(last_check), "n/a") if np.isfinite(last_check) else "n/a"),
                "to_bullish": _clean(vams["upper"][-1:])[0],
                "to_bearish": _clean(vams["lower"][-1:])[0],
            },
        },
        "ranges": boxes,
        "current_range": current_range,
        "signals": signals,
        "supertrend": {
            "signals": st_signals,
            "trend": "up" if st_last == 1 else "down",
            "line": _clean(np.array([st_line]))[0],
        },
        "position": pos["position"],
        "profile": profile,
        "levels": levels,
        "warmup": {"bars_before_window": first, "bars_total": n, "notes": notes},
        "params": asdict(p),
        "rules": RULES,
    }
