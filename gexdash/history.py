"""Chart range and bar-interval vocabulary, and the aggregation behind it.

The provider does not offer arbitrary combinations, so this module owns the
translation and — more importantly — makes every compromise visible. A chart
that quietly serves daily bars when you asked for 5-minute ones is worse than
one that refuses, because the axis still looks plausible.

What the Schwab feed actually supports, established by probing it live rather
than from documentation:

===============  =========================================================
minute bars      frequency 1, 5, 10, 15, 30 only. 60 and 240 are rejected.
                 Available for at most **10 days** back.
daily            up to 10 years
weekly/monthly   up to 20 years
one-second       **not offered at all** -- the floor is one minute
===============  =========================================================

So hourly, 2-hour and 4-hour bars are built here by aggregating 30-minute
bars, and anything below a minute is impossible without a tick feed. Both
facts are reported rather than worked around silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import numpy as np

from .bars import PriceBars, build_bars

__all__ = [
    "RANGES", "INTERVALS", "Plan", "resolve", "aggregate", "valid_intervals",
    "UNSUPPORTED_INTERVALS", "capability_matrix",
]


@dataclass(frozen=True)
class Range:
    key: str
    label: str
    days: int | None          # None = year-to-date
    order: int


@dataclass(frozen=True)
class Interval:
    key: str
    label: str
    #: Bar size in minutes; None for daily/weekly/monthly.
    minutes: int | None
    #: "minute" | "daily" | "weekly" | "monthly"
    kind: str
    order: int

    @property
    def intraday(self) -> bool:
        return self.kind == "minute"


RANGES: dict[str, Range] = {r.key: r for r in [
    Range("1D", "1 day", 1, 0),
    Range("5D", "5 days", 5, 1),
    Range("10D", "10 days", 10, 2),
    Range("1M", "1 month", 30, 3),
    Range("3M", "3 months", 90, 4),
    Range("6M", "6 months", 180, 5),
    Range("YTD", "Year to date", None, 6),
    Range("1Y", "1 year", 365, 7),
    Range("2Y", "2 years", 730, 8),
    Range("5Y", "5 years", 1825, 9),
    Range("10Y", "10 years", 3650, 10),
]}

INTERVALS: dict[str, Interval] = {i.key: i for i in [
    Interval("1m", "1 minute", 1, "minute", 0),
    Interval("5m", "5 minutes", 5, "minute", 1),
    Interval("10m", "10 minutes", 10, "minute", 2),
    Interval("15m", "15 minutes", 15, "minute", 3),
    Interval("30m", "30 minutes", 30, "minute", 4),
    Interval("1h", "1 hour", 60, "minute", 5),
    Interval("2h", "2 hours", 120, "minute", 6),
    Interval("4h", "4 hours", 240, "minute", 7),
    Interval("1D", "Daily", None, "daily", 8),
    Interval("1W", "Weekly", None, "weekly", 9),
    Interval("1Mo", "Monthly", None, "monthly", 10),
]}

#: Bar sizes the provider serves natively. Everything else intraday is
#: aggregated from the largest native size that divides it.
NATIVE_MINUTES = (1, 5, 10, 15, 30)

#: Furthest back minute bars go, per the live probe.
MAX_INTRADAY_DAYS = 10

#: Asked for often, impossible here. Stated rather than silently rounded up.
UNSUPPORTED_INTERVALS = {
    "1s": "one-second bars need a tick/streaming feed; this provider's floor is 1 minute",
    "5s": "sub-minute bars need a tick/streaming feed; this provider's floor is 1 minute",
    "15s": "sub-minute bars need a tick/streaming feed; this provider's floor is 1 minute",
    "30s": "sub-minute bars need a tick/streaming feed; this provider's floor is 1 minute",
}


@dataclass
class Plan:
    """How a (range, interval) request will actually be served."""

    range_key: str
    interval_key: str
    #: Provider request
    period_type: str
    period: int
    frequency_type: str
    frequency: int
    #: Aggregate the fetched bars up to this many minutes, if set.
    aggregate_minutes: int | None = None
    #: Non-fatal adjustments, surfaced to the user.
    notes: list[str] = field(default_factory=list)
    served_interval_key: str = ""

    def to_dict(self) -> dict:
        return {
            "range": self.range_key,
            "interval_requested": self.interval_key,
            "interval_served": self.served_interval_key or self.interval_key,
            "aggregated_from_minutes": self.frequency if self.aggregate_minutes else None,
            "provider_request": {
                "periodType": self.period_type, "period": self.period,
                "frequencyType": self.frequency_type, "frequency": self.frequency,
            },
            "notes": self.notes,
        }


def valid_intervals(range_key: str) -> list[str]:
    """Intervals that can actually be served over a range.

    Used to grey out impossible combinations in the UI rather than letting
    the user pick one and silently receive something else.
    """
    rng = RANGES[range_key]
    days = rng.days if rng.days is not None else 365
    out = []
    for key, iv in sorted(INTERVALS.items(), key=lambda kv: kv[1].order):
        if iv.intraday and days > MAX_INTRADAY_DAYS:
            continue
        if iv.kind == "weekly" and days < 60:
            continue
        if iv.kind == "monthly" and days < 365:
            continue
        # A bar bigger than the whole window yields one or two candles.
        if iv.intraday and iv.minutes and days == 1 and iv.minutes > 240:
            continue
        out.append(key)
    return out


def _period_for(days: int, allowed: tuple[int, ...]) -> int:
    for value in allowed:
        if value >= days:
            return value
    return allowed[-1]


def resolve(range_key: str, interval_key: str) -> Plan:
    """Translate a (range, interval) pair into a provider request.

    Impossible combinations are coarsened rather than rejected -- a year of
    1-minute bars is not available anywhere -- but the substitution is always
    recorded in ``notes`` and echoed to the client.
    """
    if interval_key in UNSUPPORTED_INTERVALS:
        raise ValueError(UNSUPPORTED_INTERVALS[interval_key])
    if range_key not in RANGES:
        raise ValueError(f"unknown range {range_key!r}; known: {sorted(RANGES)}")
    if interval_key not in INTERVALS:
        raise ValueError(f"unknown interval {interval_key!r}; known: {sorted(INTERVALS)}")

    rng, iv = RANGES[range_key], INTERVALS[interval_key]
    notes: list[str] = []
    days = rng.days if rng.days is not None else 365

    # Intraday beyond the provider's minute-history window: keep the range
    # the user asked for -- it is the more deliberate choice -- and coarsen
    # the bar size, saying so.
    if iv.intraday and days > MAX_INTRADAY_DAYS:
        notes.append(
            f"{iv.label} bars are only available for the last {MAX_INTRADAY_DAYS} days; "
            f"serving daily bars over {rng.label} instead."
        )
        iv = INTERVALS["1D"]

    if iv.intraday:
        native = max(n for n in NATIVE_MINUTES if iv.minutes % n == 0) if iv.minutes not in NATIVE_MINUTES else iv.minutes
        aggregate = iv.minutes if iv.minutes not in NATIVE_MINUTES else None
        if aggregate:
            notes.append(f"{iv.label} bars aggregated from {native}-minute bars — the feed's largest native size is 30m.")
        return Plan(
            range_key=range_key, interval_key=interval_key,
            period_type="day", period=_period_for(days, (1, 2, 3, 4, 5, 10)),
            frequency_type="minute", frequency=native,
            aggregate_minutes=aggregate, notes=notes, served_interval_key=iv.key,
        )

    if iv.kind == "daily":
        if rng.days is None:
            return Plan(range_key, interval_key, "ytd", 1, "daily", 1,
                        notes=notes, served_interval_key=iv.key)
        if days <= 180:
            return Plan(range_key, interval_key, "month", _period_for(days // 30 or 1, (1, 2, 3, 6)),
                        "daily", 1, notes=notes, served_interval_key=iv.key)
        return Plan(range_key, interval_key, "year", _period_for(days // 365 or 1, (1, 2, 3, 5, 10, 15, 20)),
                    "daily", 1, notes=notes, served_interval_key=iv.key)

    years = _period_for(max(days // 365, 1), (1, 2, 3, 5, 10, 15, 20))
    return Plan(range_key, interval_key, "year", years, iv.kind, 1,
                notes=notes, served_interval_key=iv.key)


def aggregate(bars: PriceBars, minutes: int) -> PriceBars:
    """Resample bars up to a coarser interval.

    Buckets on wall-clock boundaries rather than by counting bars, so an
    hourly candle starts on the hour even when the session opens mid-bucket
    or a bar is missing. Counting in groups of two would drift after any gap.
    """
    if len(bars) == 0 or minutes <= bars.interval_minutes:
        return bars

    epoch = bars.time.astype("datetime64[s]").astype("int64")
    bucket = (epoch // (minutes * 60)) * (minutes * 60)

    rows = []
    for edge in np.unique(bucket):
        mask = bucket == edge
        rows.append({
            "time": np.datetime64(int(edge), "s"),
            "open": float(bars.open[mask][0]),
            "high": float(bars.high[mask].max()),
            "low": float(bars.low[mask].min()),
            "close": float(bars.close[mask][-1]),
            "volume": float(bars.volume[mask].sum()),
        })

    return build_bars(
        bars.instrument, rows, interval_minutes=minutes,
        meta={**(bars.meta or {}), "aggregated_from_minutes": bars.interval_minutes},
    )


def capability_matrix() -> dict:
    """What the UI needs to offer only valid combinations."""
    return {
        "ranges": [
            {"key": r.key, "label": r.label, "days": r.days,
             "intervals": valid_intervals(r.key)}
            for r in sorted(RANGES.values(), key=lambda r: r.order)
        ],
        "intervals": [
            {"key": i.key, "label": i.label, "kind": i.kind, "minutes": i.minutes}
            for i in sorted(INTERVALS.values(), key=lambda i: i.order)
        ],
        "unsupported": UNSUPPORTED_INTERVALS,
        "max_intraday_days": MAX_INTRADAY_DAYS,
        "native_minute_sizes": list(NATIVE_MINUTES),
    }
