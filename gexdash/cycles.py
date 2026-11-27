"""The lunar-solar cycle: gexdash's own model of Sun and Moon cycles in a
symbol's returns, drawn with its first and second derivatives and projected
past the last bar as a dotted price path with its highs and lows labelled.

The charts this was modelled on draw a "PPM2" line, its 1st and 2nd
derivatives, and a projection marked "High Dec 22", "Low Dec 31" and so on.
That script is private and its rules are not published. As with the
signal rules in ``technicals.py``, this module does not imitate its output.
It defines its own model, writes every step of it down in :data:`RULES`,
ships that text with the payload, and reports how the model did on days it
was not fitted to. Do not mistake the output for the reference script, or
the projection for a forecast that has shown skill: the holdout figures say
whether it has.

**Model.** Daily log returns are regressed on the cosine and sine of five
astronomical angles (:data:`CYCLES`): the lunar phase, its second harmonic
(spring and neap tides), the Moon's anomaly (distance), its longitude
(declination) and the Sun's longitude (the year). The angles come from the
ephemeris in ``astro.py``, evaluated at each session's 16:00 ET close, so
they are the sky's actual positions, not fixed-period sine waves. The fit is
ridge regression on centred features, so that two near-equal lunar months
(27.32 and 27.55 days) cannot trade off against each other with huge
opposite coefficients.

**Always daily.** The fit uses daily bars whatever the chart's bar size: on
weekly bars the 14.8-day tide term would sit at the sampling limit, and on
intraday bars there are too few lunar months. The fitted model is a function
of time, so it is then evaluated at whatever times the chart shows. The
cycle component is accumulated over trading days, so a weekly bar carries
five days of it, not one.

**Lines.** ``lsc`` is the cycle component of log price: the fitted cyclical
returns accumulated over trading days, centred. ``d1`` is its first
derivative, the fitted cyclical return per trading day; ``d2`` is the second,
``d1``'s rate of change per day. Both are evaluated at each bar's own time.
Each is divided by its own largest absolute
value over the whole fitted span and horizon, so all three run from -1 to +1
on one pane, and :func:`compute` returns the divisors under ``scale``.
Highs and lows of ``lsc`` fall where ``d1`` crosses zero.

**Projection.** On daily, weekly and monthly bars, the path is the last close
times ``exp`` of the change in the cycle component from the last bar. That
is the fitted amplitude, which is usually a fraction of a percent; it is
not rescaled to look like a price target. Intraday bars get the lines and
no projection, since that would need the next sessions' bar times.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from . import astro

__all__ = [
    "Cycle", "CYCLES", "AstroParams", "RULES", "features", "fit", "holdout", "close_time",
    "eval_times", "future_stamps", "turning_points", "compute",
]

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class Cycle:
    """One astronomical cycle in the model: which ephemeris angle, at what
    multiple, and what an angle of zero means."""

    key: str
    name: str
    period_days: float
    angle: str            # a key of astro.positions()
    multiple: int
    zero: str


CYCLES: tuple[Cycle, ...] = (
    Cycle("phase", "Lunar phase", astro.SYNODIC_DAYS, "elongation", 1, "new moon"),
    Cycle("tide", "Spring-neap tide", astro.SYNODIC_DAYS / 2, "elongation", 2, "new or full moon"),
    Cycle("distance", "Lunar distance", astro.ANOMALISTIC_DAYS, "mean_anomaly", 1, "perigee"),
    Cycle("declination", "Lunar declination", astro.TROPICAL_MONTH_DAYS, "moon_lon", 1,
          "the Moon crossing the equator northward"),
    Cycle("solar", "Solar year", astro.TROPICAL_YEAR_DAYS, "sun_lon", 1, "the March equinox"),
)


@dataclass(frozen=True)
class AstroParams:
    """Every choice the model makes. Echoed in the payload."""

    #: Ridge penalty per observation. Features have variance 1/2, so 0.02
    #: shrinks an isolated cycle by about 4%; its real job is the pair of
    #: near-equal lunar months.
    ridge: float = 0.02
    #: The last share of the daily history held out to test the model.
    holdout: float = 0.25
    #: Fewer daily returns than this and there is no fit.
    min_days: int = 250
    #: Future slots, per bar size.
    daily_horizon: int = 126
    weekly_horizon: int = 52
    monthly_horizon: int = 12
    #: A projected high or low must be followed by a reversal of at least
    #: this share of the projection's full range to count.
    turn_fraction: float = 0.05
    #: Daily bars are read at the cash close, in ET.
    close_hour_et: int = 16


#: The written definition of every step, sent with each payload.
RULES: list[dict[str, str]] = [
    {"key": "astro_model", "name": "Lunar-solar cycle (LSC)",
     "text": "gexdash's own model, not the reference chart's PPM2 script. Up to 10 years of daily "
             "log returns are regressed (ridge 0.02, centred features) on the cosine and sine of five "
             "angles computed from a Meeus ephemeris at each 16:00 ET close: lunar phase (29.53 d), "
             "its second harmonic for spring and neap tides (14.77 d), lunar distance (27.55 d), "
             "lunar declination (27.32 d) and the Sun's longitude (the year). LSC is the fitted "
             "cyclical return accumulated over trading days: the part of the price the cycles "
             "explain. The fit is always on daily bars and is evaluated at each bar's time. Futures "
             "are fitted on the raw front month with each contract change's return left out. Over the "
             "bars the lines are in-sample: the fit has seen those returns, so any match between past "
             "LSC turns and past price turns is partly fitted. The holdout is the only out-of-sample "
             "evidence."},
    {"key": "astro_derivatives", "name": "1st and 2nd derivative",
     "text": "The 1st derivative is the fitted cyclical return per trading day, the slope of LSC; "
             "the 2nd is its rate of change per day. Both are read at each bar's own time. Each line is divided by its own largest swing over "
             "the fitted span and horizon, so all three run from -1 to +1 on one pane. LSC peaks "
             "where the 1st derivative crosses down through zero and bottoms where it crosses up."},
    {"key": "astro_projection", "name": "Projection, highs and lows",
     "text": "Past the last bar the lines are drawn dotted: they are computed from where the Sun and "
             "Moon will be, not observed. On daily, weekly and monthly bars the projected price is the "
             "last close moved by the change in LSC, at its fitted amplitude. It is not rescaled. A "
             "high or low is labelled where the path reverses by at least 5% of its range. Future "
             "dates are weekdays; exchange holidays are not modelled. Intraday bars get no projection."},
    {"key": "astro_holdout", "name": "Holdout test",
     "text": "Before the full fit, the model is fitted to the first 75% of the daily history and "
             "tested on the last 25%. The note reports how often the sign of the fitted cyclical return "
             "matched the day's return, beside what calls with no skill would score given the same mix of "
             "up and down calls in a market that rose on the days it did (not 50%: a model that mostly says "
             "up scores above half in a rising market by luck). It also gives their correlation, with the "
             "band (±2/√n) inside which a correlation is indistinguishable from none."},
    {"key": "astro_events", "name": "Moon and Sun events",
     "text": "New, first-quarter, full and last-quarter moons, perigee and apogee, equinoxes and "
             "solstices, from the same ephemeris (within two minutes of PyEphem for phases, fifteen "
             "for the rest). Each is drawn at the first bar that closes at or after it."},
]


# --------------------------------------------------------------- features


def features(pos: dict[str, np.ndarray]) -> np.ndarray:
    """``(n, 2 * len(CYCLES))``: cos and sin of each cycle's angle."""
    cols = []
    for c in CYCLES:
        a = np.asarray(pos[c.angle], dtype=float) * c.multiple * math.pi / 180.0
        cols += [np.cos(a), np.sin(a)]
    return np.column_stack(cols)


def fit(returns: np.ndarray, X: np.ndarray, *, ridge: float) -> dict[str, np.ndarray]:
    """Ridge regression of returns on centred features.

    Returns ``coef``, ``mean`` (the feature means the fit was centred on) and
    ``intercept`` (the mean return). Prediction of the cyclical part is
    ``(X - mean) @ coef``: by construction it sums to zero over the fit, so
    the cycle component cannot quietly carry the drift.
    """
    returns = np.asarray(returns, dtype=float)
    mean = X.mean(axis=0)
    Xc = X - mean
    n, k = Xc.shape
    coef = np.linalg.solve(Xc.T @ Xc + ridge * n * np.eye(k), Xc.T @ (returns - returns.mean()))
    return {"coef": coef, "mean": mean, "intercept": np.array(returns.mean())}


def holdout(returns: np.ndarray, X: np.ndarray, *, share: float, ridge: float) -> dict | None:
    """Fit on the first ``1 - share`` of the returns, test on the rest.

    ``hit_rate`` is how often the sign of the fitted cyclical return matched
    the sign of the day's return, over days that moved. 50% is the wrong
    yardstick for it: a model that calls "up" on most days scores above 50%
    in a rising market with no skill at all. ``no_skill`` is what calls made
    independently of the day, with the same share of ups, would score:
    ``u * a + (1 - u) * (1 - a)``, for ``u`` the share of up calls and ``a``
    the share of up days. ``corr`` is the correlation of the fitted and
    actual returns and ``noise`` is 2/sqrt(n), the band a correlation of zero
    lands inside 95% of the time. None when either part would be too short.
    """
    n = len(returns)
    cut = int(round(n * (1 - share)))
    if cut < 100 or n - cut < 40:
        return None
    m = fit(returns[:cut], X[:cut], ridge=ridge)
    pred = (X[cut:] - m["mean"]) @ m["coef"]
    actual = returns[cut:]
    moved = actual != 0
    if not moved.any():
        return None
    calls, days = np.sign(pred[moved]), np.sign(actual[moved])
    hit = float(np.mean(calls == days))
    u, a = float(np.mean(calls > 0)), float(np.mean(days > 0))
    corr = float(np.corrcoef(pred, actual)[0, 1]) if np.std(pred) > 0 and np.std(actual) > 0 else float("nan")
    return {
        "days": int(n - cut),
        "hit_rate": round(hit, 4),
        "no_skill": round(u * a + (1 - u) * (1 - a), 4),
        "corr": round(corr, 4),
        "noise": round(2 / math.sqrt(n - cut), 4),
        "up_share": round(a, 4),
    }


# ------------------------------------------------------------------ times


def close_time(d: date, hour: int = 16) -> float:
    """``hour``:00 ET on a date, as Unix seconds."""
    return datetime(d.year, d.month, d.day, hour, tzinfo=ET).timestamp()


def _date_of(stamp) -> date:
    """The date part of a bar stamp (``datetime64`` or ISO string)."""
    return date.fromisoformat(str(stamp)[:10])


def _period_end(d: date, kind: str) -> date:
    """The last session of the period a bar stamped ``d`` covers: the day
    itself, that week's Friday (weekly bars are stamped Sunday or Monday), or
    the month's last weekday."""
    if kind == "weekly":
        return d + timedelta(days=(4 - d.weekday()) % 7)
    if kind == "monthly":
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = nxt - timedelta(days=1)
        while end.weekday() >= 5:
            end -= timedelta(days=1)
        return end
    return d


def eval_times(stamps, kind: str, minutes: int | None = None, hour: int = 16) -> np.ndarray:
    """The instant each bar is read at, Unix seconds: an intraday bar at its
    close (open + bar size), a daily bar at its session's close, a weekly or
    monthly bar at the close of the period's last session."""
    if kind == "minute":
        opens = np.asarray(stamps).astype("datetime64[s]").astype("int64").astype(float)
        return opens + 60.0 * (minutes or 1)
    return np.array([close_time(_period_end(_date_of(s), kind), hour) for s in stamps], dtype=float)


def _add_months(d: date, k: int) -> date:
    """The same day ``k`` months on, clamped to the month's length."""
    y, m = divmod(d.month - 1 + k, 12)
    first = date(d.year + y, m + 1, 1)
    last = ((first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)).day
    return first.replace(day=min(d.day, last))


def future_stamps(last: date, kind: str, count: int) -> list[date]:
    """Bar dates after ``last``: weekdays for daily bars (holidays are not
    modelled), the same weekday a week apart for weekly, a month apart for
    monthly. Empty for intraday."""
    out: list[date] = []
    if kind == "daily":
        d = last
        while len(out) < count:
            d += timedelta(days=1)
            if d.weekday() < 5:
                out.append(d)
    elif kind == "weekly":
        out = [last + timedelta(days=7 * k) for k in range(1, count + 1)]
    elif kind == "monthly":
        out = [_add_months(last, k) for k in range(1, count + 1)]
    return out


def _weekdays_after(last: date, until: date) -> list[date]:
    """Every weekday after ``last`` up to and including ``until``."""
    out, d = [], last
    while d < until:
        d += timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


# ------------------------------------------------------------------ turns


def turning_points(path: np.ndarray, *, threshold: float) -> list[tuple[int, str]]:
    """Highs and lows of a path, zigzag-style: an extreme counts once the
    path has come back from it by ``threshold``. ``path[0]`` is the anchor
    (the last close); it can set the first direction but is never itself
    labelled, and an extreme still unconfirmed at the end is left out.
    Returns ``(index into path, "high" | "low")``.
    """
    n = len(path)
    out: list[tuple[int, str]] = []
    # A flat path (a zero fit) has no turns; a zero threshold would make
    # every slot one.
    if n < 3 or not threshold > 0:
        return out
    direction = 0              # +1 rising, -1 falling, 0 not yet known
    hi = lo = 0
    for k in range(1, n):
        v = path[k]
        if direction >= 0 and v >= path[hi]:
            hi = k
        if direction <= 0 and v <= path[lo]:
            lo = k
        if direction >= 0 and path[hi] - v >= threshold and (direction > 0 or hi > lo):
            if hi > 0:
                out.append((hi, "high"))
            direction, lo = -1, k
        elif direction <= 0 and v - path[lo] >= threshold and (direction < 0 or lo > hi):
            if lo > 0:
                out.append((lo, "low"))
            direction, hi = 1, k
    return out


# ---------------------------------------------------------------- compute


def _round(values, digits: int) -> list:
    """JSON-safe rounding: nan becomes null, as the client expects."""
    return [None if v is None or not np.isfinite(v) else round(float(v), digits) for v in values]


def _iso(ts: float) -> str:
    """Unix seconds as an ISO UTC string with a Z."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute(
    daily_stamps, daily_close: np.ndarray, *, bar_stamps, kind: str, minutes: int | None = None,
    last_close: float | None = None, skip: frozenset[int] | set[int] = frozenset(),
    params: AstroParams | None = None,
) -> dict:
    """The lunar-solar cycle for one chart.

    ``daily_stamps`` / ``daily_close`` are the symbol's daily history (for
    futures, the raw front month, with the roll bars' indices in ``skip`` so
    a contract change is not read as a return). ``bar_stamps`` are the chart's
    bars, ``kind`` their size (``minute``, ``daily``, ``weekly``,
    ``monthly``), ``minutes`` the intraday bar length, and ``last_close`` the
    price the projection starts from (the chart's last close).

    Returns ``{"available": False, "reason"}`` when there is not enough
    history, else the per-bar ``series``, the ``future`` slots, ``turns``,
    ``events``, ``scale``, ``model`` and ``notes``. See :data:`RULES`.
    """
    p = params or AstroParams()
    closes = np.asarray(daily_close, dtype=float)
    dates = [_date_of(s) for s in daily_stamps]
    if len(bar_stamps) == 0:
        return {"available": False, "reason": "no bars to draw the cycle against"}

    # ---- daily returns, read at each session's close
    hist_t = np.array([close_time(d, p.close_hour_et) for d in dates], dtype=float)
    ok = np.zeros(len(closes), dtype=bool)
    ok[1:] = (closes[1:] > 0) & (closes[:-1] > 0) & np.isfinite(closes[1:]) & np.isfinite(closes[:-1])
    for k in skip:
        if 0 <= k < len(ok):
            ok[k] = False
    idx = np.flatnonzero(ok)
    if len(idx) < p.min_days:
        return {"available": False,
                "reason": f"{len(idx)} daily returns; the lunar-solar fit needs at least {p.min_days}"}
    returns = np.log(closes[idx] / closes[idx - 1])
    X_fit = features(astro.positions(hist_t[idx]))

    test = holdout(returns, X_fit, share=p.holdout, ridge=p.ridge)
    model = fit(returns, X_fit, ridge=p.ridge)
    resid = returns - model["intercept"] - (X_fit - model["mean"]) @ model["coef"]
    r2 = 1 - float(np.var(resid) / np.var(returns)) if np.var(returns) > 0 else float("nan")

    # ---- chart slots: the bars, then the future
    bar_t = eval_times(bar_stamps, kind, minutes, p.close_hour_et)
    last_bar = _date_of(bar_stamps[-1])
    horizon = {"daily": p.daily_horizon, "weekly": p.weekly_horizon, "monthly": p.monthly_horizon}.get(kind, 0)
    fut_dates = future_stamps(last_bar, kind, horizon)
    fut_t = np.array([close_time(_period_end(d, kind), p.close_hour_et) for d in fut_dates], dtype=float)

    # ---- the daily grid the cycle component accumulates over: every
    # fitted session, then weekdays far enough to cover every slot.
    reach = max(float(bar_t[-1]), float(fut_t[-1]) if len(fut_t) else 0.0, float(hist_t[-1]))
    until = datetime.fromtimestamp(reach, tz=ET).date() + timedelta(days=7)
    grid_dates = dates + _weekdays_after(dates[-1], until)
    grid_t = np.array([close_time(d, p.close_hour_et) for d in grid_dates], dtype=float)
    def rate(t: np.ndarray) -> np.ndarray:
        """The fitted cyclical return per trading day at any instants."""
        return (features(astro.positions(t)) - model["mean"]) @ model["coef"]

    def accel(t: np.ndarray) -> np.ndarray:
        """Its rate of change per day: a central difference over +-6 hours of
        continuous time. Differencing consecutive sessions instead would
        triple every Monday's step, which spans the weekend."""
        h = 6 * 3600.0
        return (rate(t + h) - rate(t - h)) / (2 * h / 86400.0)

    f = rate(grid_t)
    f[0] = 0.0                       # the first close has no return before it
    level = np.cumsum(f)
    level -= level[: len(dates)].mean()
    scale = {k: float(np.max(np.abs(v))) or 1.0
             for k, v in (("lsc", level), ("d1", f), ("d2", accel(grid_t)))}

    def sample(t: np.ndarray) -> dict[str, np.ndarray]:
        """The three lines (unscaled) at arbitrary instants; nan before the
        grid. LSC is interpolated between session closes; the derivatives
        are evaluated at the instants themselves."""
        before = t < grid_t[0] - 86400
        out = {"lsc": np.interp(t, grid_t, level), "d1": rate(t), "d2": accel(t)}
        for v in out.values():
            v[before] = np.nan
        return out

    def slots(t: np.ndarray) -> dict[str, list]:
        """Per-slot payload: the scaled lines and the Sun and Moon."""
        if len(t) == 0:
            return {k: [] for k in ("lsc", "d1", "d2", "illum", "elong", "dist", "dec", "sun")}
        lines = sample(t)
        pos = astro.positions(t)
        return {
            **{k: _round(v / scale[k], 4) for k, v in lines.items()},
            "illum": _round(pos["illumination"], 3),
            "elong": _round(pos["elongation"], 1),
            "dist": _round(pos["moon_dist_km"], 0),
            "dec": _round(pos["moon_dec"], 2),
            "sun": _round(pos["sun_lon"], 1),
        }

    series = slots(bar_t)
    future = {"time": [f"{d.isoformat()}T00:00:00" for d in fut_dates], **slots(fut_t)}

    # ---- projection and its highs and lows
    notes: list[str] = []
    turns: list[dict] = []
    path: list = []
    swing = None
    anchor_level = sample(bar_t[-1:])["lsc"][0]
    if len(fut_t) and last_close and np.isfinite(anchor_level):
        lv = sample(fut_t)["lsc"]
        prices = float(last_close) * np.exp(lv - anchor_level)
        path = _round(prices, 4)
        full = np.r_[float(last_close), prices]
        span = float(full.max() - full.min())
        swing = span / float(last_close)
        for k, side in turning_points(full, threshold=p.turn_fraction * span):
            turns.append({"i": k - 1, "type": side, "price": round(float(full[k]), 4),
                          "date": fut_dates[k - 1].isoformat()})
    elif kind == "minute":
        notes.append("Lunar-solar cycle: intraday bars show the daily fit sampled at each bar; "
                     "the projection needs daily or longer bars.")
    future["path"] = path

    # ---- events, each at the first slot that closes at or after it
    all_t = np.r_[bar_t, fut_t]
    period = {"minute": 60.0 * (minutes or 1), "daily": 86400.0, "weekly": 7 * 86400.0,
              "monthly": 31 * 86400.0}.get(kind, 86400.0)
    start = datetime.fromtimestamp(all_t[0] - period, tz=timezone.utc)
    end = datetime.fromtimestamp(all_t[-1], tz=timezone.utc)
    events = []
    for e in astro.events(start, end):
        ts = e["time"].timestamp()
        slot = int(np.searchsorted(all_t, ts, side="left"))
        if slot >= len(all_t) or ts < all_t[0] - period:
            continue
        events.append({**e, "time": _iso(ts), "slot": slot})

    # ---- the model, in words a reader can check
    cycles = []
    for j, c in enumerate(CYCLES):
        b, s = model["coef"][2 * j], model["coef"][2 * j + 1]
        peak = math.degrees(math.atan2(s, b)) % 360.0          # angle of the cycle's highest return
        cycles.append({
            "key": c.key, "name": c.name, "period_days": round(c.period_days, 3),
            "amplitude_bp": round(math.hypot(b, s) * 1e4, 3),
            "peak_days": round(peak / 360.0 * c.period_days, 2), "zero": c.zero,
        })
    first_day, last_day = dates[int(idx[0])], dates[int(idx[-1])]
    summary = (f"Lunar-solar fit: {len(returns):,} daily returns, {first_day:%b %Y} to {last_day:%b %Y}.")
    if test:
        summary += (f" On the held-out last {test['days']:,} days the cycles called the day's direction "
                    f"{test['hit_rate'] * 100:.1f}% of the time, against {test['no_skill'] * 100:.1f}% for "
                    f"calls with no skill and the same mix of ups and downs; correlation "
                    f"{test['corr']:+.3f}, where ±{test['noise']:.3f} is indistinguishable from none.")
    if swing is not None:
        summary += f" Projection at the fitted amplitude: a {swing * 100:.2f}% range."
        if kind == "daily":
            summary += " Future dates are weekdays; holidays are not modelled."
    notes.insert(0, summary)

    return {
        "available": True,
        "kind": kind,
        "series": series,
        "future": future,
        "turns": turns,
        "events": events,
        "scale": {k: round(v, 8) for k, v in scale.items()},
        "model": {
            "days": int(len(returns)), "start": first_day.isoformat(), "end": last_day.isoformat(),
            "r2": round(r2, 5), "cycles": cycles, "holdout": test,
            "swing_pct": None if swing is None else round(swing * 100, 3),
        },
        "params": asdict(p),
        "notes": notes,
        "rules": RULES,
    }
