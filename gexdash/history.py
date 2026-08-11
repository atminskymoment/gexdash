"""Chart range and bar-interval vocabulary, and the aggregation behind it.

The provider does not offer arbitrary combinations, so this module owns the
translation and — more importantly — makes every compromise visible. A chart
that quietly serves daily bars when you asked for 5-minute ones is worse than
one that refuses, because the axis still looks plausible.

What the Schwab feed actually supports, established by probing it live rather
than from documentation:

===============  =========================================================
minute bars      frequency 1, 5, 10, 15, 30 only. 60 and 240 are rejected.
1-minute         ~48 days back -- a hard cap of 12,870 candles, which is
                 33 sessions x 390 RTH minutes, identical on every symbol
5m and coarser   ~260 days back, a date horizon rather than a candle cap
daily            10 years        weekly/monthly  20 years
one-second       **not offered at all** -- the floor is one minute
===============  =========================================================

**Requests are bounded by startDate/endDate, not by periodType/period.** That
distinction is the whole reason for the reach figures above. The ``period``
form quantises to fixed buckets -- ``periodType=day`` maxes out at 10 -- so
reading the limits off it gives "minute bars stop at 10 days", which is a
property of the parameter, not of the feed. With explicit dates the same
endpoint serves 30-minute bars over six months. Once dates are supplied
``periodType`` is only a validation token: ``year`` is accepted alongside
daily, weekly and monthly alike, and only ``month`` + ``monthly`` is rejected.

Bounding by date also means every range is served exactly, so nothing has to
be over-fetched and trimmed afterwards.

Hourly, 2-hour and 4-hour bars are still built here by aggregating 30-minute
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

#: How far back each native minute size reaches, in calendar days.
#:
#: Measured against SPX, SPY, NDX and QQQ on one live entitlement, requesting
#: a 400-day window and reading back what arrived:
#:
#:     1m   12,870 candles on every symbol -> 48 days
#:     5m   260 days      10m  260 days     15m  260 days     30m  260 days
#:
#: 1-minute is limited by a *candle count* (12,870 = 33 sessions x 390 RTH
#: minutes, identical to the contract across all four symbols), so its reach
#: in days drifts with how many sessions fall in the window. The coarser sizes
#: all stopped on the same calendar date with wildly different candle counts
#: (13,746 vs 2,293), which makes theirs a date horizon.
#:
#: Held a little short of the measurements so a range sitting on the boundary
#: does not quietly come back truncated.
INTRADAY_REACH_DAYS = {1: 45, 5: 250, 10: 250, 15: 250, 30: 250}

#: The furthest any intraday size reaches. Used to decide whether an intraday
#: request is plausible at all before consulting the per-size table.
MAX_INTRADAY_DAYS = max(INTRADAY_REACH_DAYS.values())


def intraday_reach_days(native_minutes: int) -> int:
    """Reach of the native bar size an interval is served from."""
    return INTRADAY_REACH_DAYS.get(native_minutes, min(INTRADAY_REACH_DAYS.values()))


#: Minutes of quotable time in a calendar day, averaged across weekends.
#: Only used to *estimate* candle counts, so it is deliberately rough --
#: intraday requests include extended hours, which is about 16 hours a
#: session. Measured against the feed: a 28-day 1-minute request returned
#: 27,256 candles, i.e. ~973 per calendar day.
_MINUTES_PER_CALENDAR_DAY = 960

#: Most candles worth putting on screen.
#:
#: Not a feed limit -- the feed will serve far more. It is a rendering limit,
#: and it was measured rather than guessed. Draw times for the price panel,
#: with the SVG node count that drives them:
#:
#:        707 bars    1,455 nodes   0.40 s
#:      2,932 bars    5,910 nodes   0.72 s
#:     12,760 bars   25,565 nodes   1.02 s
#:     26,645 bars   54,558 nodes   2.15 s
#:
#: The cost is roughly linear and only the last row is unacceptable -- and it
#: is paid again on every 30-second bar refresh. The threshold sits above
#: 12,760 deliberately: that is what a 10-day 1-minute chart already produced
#: before this limit existed, and quietly coarsening a view that worked would
#: be a regression dressed up as a safeguard. What it does stop is the newly
#: reachable end of the range -- a month of 1-minute bars, 26,645 candles, ~45
#: of them per pixel on a 600px chart.
#:
#: Exceeding it coarsens the bar size rather than truncating the range, and
#: says so -- the range is the more deliberate choice, same as for reach.
MAX_PLOTTED_BARS = 15_000


def estimated_bars(days: int, interval_minutes: int | None) -> int:
    """Roughly how many candles a (range, interval) pair would draw."""
    if not interval_minutes:
        return 0
    return int(days * _MINUTES_PER_CALENDAR_DAY / interval_minutes)

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
    #: Explicit window. When set, these bound the request and ``period`` is
    #: ignored by the feed -- which is how a range is served exactly rather
    #: than rounded up to the next period bucket and trimmed afterwards.
    start: datetime | None = None
    end: datetime | None = None
    #: Aggregate the fetched bars up to this many minutes, if set.
    aggregate_minutes: int | None = None
    #: Non-fatal adjustments, surfaced to the user.
    notes: list[str] = field(default_factory=list)
    served_interval_key: str = ""

    def request_params(self) -> dict:
        """Query parameters for /pricehistory, dates included where set."""
        params = {
            "periodType": self.period_type,
            "period": self.period,
            "frequencyType": self.frequency_type,
            "frequency": self.frequency,
        }
        if self.start is not None and self.end is not None:
            # Epoch milliseconds. Sent alongside periodType, which the feed
            # still validates against frequencyType even though it no longer
            # bounds the window.
            params["startDate"] = int(self.start.timestamp() * 1000)
            params["endDate"] = int(self.end.timestamp() * 1000)
        return params

    def to_dict(self) -> dict:
        return {
            "range": self.range_key,
            "interval_requested": self.interval_key,
            "interval_served": self.served_interval_key or self.interval_key,
            "aggregated_from_minutes": self.frequency if self.aggregate_minutes else None,
            "window": None if self.start is None else {
                "start": self.start.isoformat(), "end": self.end.isoformat(),
            },
            "provider_request": self.request_params(),
            "notes": self.notes,
        }


def valid_intervals(range_key: str, *, now: datetime | None = None) -> list[str]:
    """Intervals that can actually be served over a range.

    Used to grey out impossible combinations in the UI rather than letting
    the user pick one and silently receive something else. Takes the same
    clock as :func:`resolve` because YTD is a moving range -- treating it as
    a flat 365 days here would grey out sizes that resolve would serve
    perfectly well in January.
    """
    rng = RANGES[range_key]
    now = now or datetime.now(timezone.utc)
    days = rng.days if rng.days is not None else _ytd_days(now)
    out = []
    for key, iv in sorted(INTERVALS.items(), key=lambda kv: kv[1].order):
        # Per bar size, not one blanket intraday limit: 30-minute bars reach
        # eight months where 1-minute bars reach about six weeks.
        if iv.intraday and not _intraday_servable(iv, days):
            continue
        if iv.kind == "weekly" and days < 60:
            continue
        if iv.kind == "monthly" and days < 365:
            continue
        # A bar bigger than the whole window yields one or two candles.
        if iv.intraday and iv.minutes and days == 1 and iv.minutes > 240:
            continue
        # Same rule, applied to daily bars: one or two candles is not a chart.
        # A week bar over one day was already excluded for this reason.
        if iv.kind == "daily" and days < 3:
            continue
        out.append(key)
    return out


def _period_for(days: int, allowed: tuple[int, ...]) -> int:
    for value in allowed:
        if value >= days:
            return value
    return allowed[-1]


#: Longest intraday range still expressed with `periodType=day` + `period`.
#: The day period tops out at 10, and inside that it is preferable to an
#: explicit window because it counts sessions rather than clock time.
_MAX_SESSION_PERIOD_DAYS = 10


def _ytd_days(now: datetime) -> int:
    """Calendar days since 1 January. YTD is a moving range, not 365 days."""
    return max((now.date() - date(now.year, 1, 1)).days, 1)


def _window(days: int, now: datetime) -> tuple[datetime, datetime]:
    from datetime import timedelta
    return now - timedelta(days=days), now


def _native_for(minutes: int) -> int:
    """Largest native bar size an interval can be built from."""
    if minutes in NATIVE_MINUTES:
        return minutes
    return max(n for n in NATIVE_MINUTES if minutes % n == 0)


def _intraday_servable(iv: Interval, days: int) -> bool:
    """Can this bar size cover this range, and be worth drawing afterwards?

    Two independent limits. Reach is the feed's; the bar count is ours. Both
    are applied here so the selector offers exactly what will be served --
    an option that silently becomes something coarser is the thing this
    module exists to prevent.
    """
    if days > intraday_reach_days(_native_for(iv.minutes)):
        return False
    return estimated_bars(days, iv.minutes) <= MAX_PLOTTED_BARS


def resolve(range_key: str, interval_key: str, *, now: datetime | None = None) -> Plan:
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
    now = now or datetime.now(timezone.utc)
    days = rng.days if rng.days is not None else _ytd_days(now)

    # Intraday beyond what this bar size reaches: keep the range the user
    # asked for -- it is the more deliberate choice -- and coarsen the bar
    # size, saying so. The limit is per size, so 30-minute bars survive a
    # six-month range where 1-minute bars do not.
    if iv.intraday and not _intraday_servable(iv, days):
        # Step up through the intraday ladder to the first size that works,
        # and fall through to daily only when none does. Jumping straight to
        # daily would throw away six months of 30-minute bars because
        # 1-minute ones were asked for.
        coarser = next(
            (c for c in sorted(INTERVALS.values(), key=lambda i: i.order)
             if c.intraday and c.minutes > iv.minutes and _intraday_servable(c, days)),
            None,
        )
        reach = intraday_reach_days(_native_for(iv.minutes))
        if coarser is not None:
            why = (f"reach about {reach} days back" if days > reach
                   else f"would draw about {estimated_bars(days, iv.minutes):,} candles")
            notes.append(
                f"{iv.key} bars {why} over {rng.label}; serving {coarser.key} instead."
            )
            iv = coarser
        else:
            notes.append(
                f"{iv.key} bars reach about {reach} days back; "
                f"serving daily bars over {rng.label} instead."
            )
            iv = INTERVALS["1D"]

    if iv.intraday:
        native = _native_for(iv.minutes)
        aggregate = iv.minutes if iv.minutes not in NATIVE_MINUTES else None
        if aggregate:
            notes.append(
                f"{iv.key} bars aggregated from {native}-minute bars — "
                "the feed's largest native size is 30m."
            )

        # Short intraday ranges keep the period form. `periodType=day` counts
        # *sessions*, so it still returns the last real session when the
        # market has not opened yet -- an explicit window ending now would
        # start after Friday's close and come back empty on a Monday morning.
        if days <= _MAX_SESSION_PERIOD_DAYS:
            return Plan(
                range_key=range_key, interval_key=interval_key,
                period_type="day", period=_period_for(days, (1, 2, 3, 4, 5, 10)),
                frequency_type="minute", frequency=native,
                aggregate_minutes=aggregate, notes=notes, served_interval_key=iv.key,
            )

        # Longer intraday: only an explicit window can express this at all,
        # since the day period tops out at 10.
        start_at, end_at = _window(days, now)
        return Plan(
            range_key=range_key, interval_key=interval_key,
            period_type="day", period=10,
            frequency_type="minute", frequency=native,
            start=start_at, end=end_at,
            aggregate_minutes=aggregate, notes=notes, served_interval_key=iv.key,
        )

    # Daily, weekly and monthly are all bounded by the window. Once dates are
    # supplied `periodType` is only validated against `frequencyType`, and
    # `year` is accepted by all three -- so there is one mapping rather than a
    # per-range ladder of period buckets, and no combination can round the
    # range up to the next bucket.
    start_at, end_at = _window(days, now)
    return Plan(
        range_key=range_key, interval_key=interval_key,
        period_type="year", period=1,
        frequency_type=iv.kind, frequency=1,
        start=start_at, end=end_at,
        notes=notes, served_interval_key=iv.key,
    )


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


def capability_matrix(*, now: datetime | None = None) -> dict:
    """What the UI needs to offer only valid combinations."""
    now = now or datetime.now(timezone.utc)
    return {
        "ranges": [
            {"key": r.key, "label": r.label, "days": r.days,
             "intervals": valid_intervals(r.key, now=now)}
            for r in sorted(RANGES.values(), key=lambda r: r.order)
        ],
        "intervals": [
            {"key": i.key, "label": i.label, "kind": i.kind, "minutes": i.minutes}
            for i in sorted(INTERVALS.values(), key=lambda i: i.order)
        ],
        "unsupported": UNSUPPORTED_INTERVALS,
        "max_intraday_days": MAX_INTRADAY_DAYS,
        "intraday_reach_days": INTRADAY_REACH_DAYS,
        "max_plotted_bars": MAX_PLOTTED_BARS,
        "native_minute_sizes": list(NATIVE_MINUTES),
    }
