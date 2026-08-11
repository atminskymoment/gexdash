"""Chart range/interval resolution and bar aggregation.

The provider's capability matrix was established by probing it live, not from
documentation: minute bars come in 1/5/10/15/30 only and reach back 10 days;
60 and 240 are rejected outright; daily runs to 10 years and weekly/monthly
to 20; and there are no sub-minute bars at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from gexdash import instruments
from gexdash.bars import build_bars
from gexdash.history import (
    INTERVALS,
    MAX_PLOTTED_BARS,
    estimated_bars,
    INTRADAY_REACH_DAYS,
    MAX_INTRADAY_DAYS,
    NATIVE_MINUTES,
    RANGES,
    UNSUPPORTED_INTERVALS,
    aggregate,
    capability_matrix,
    resolve,
    valid_intervals,
)


# ----------------------------------------------------------------------
# Resolution
# ----------------------------------------------------------------------


@pytest.mark.parametrize("interval,freq", [("1m", 1), ("5m", 5), ("15m", 15), ("30m", 30)])
def test_native_minute_sizes_pass_straight_through(interval, freq):
    plan = resolve("1D", interval)
    assert plan.frequency_type == "minute" and plan.frequency == freq
    assert plan.aggregate_minutes is None
    assert plan.notes == []


@pytest.mark.parametrize("interval,minutes,native", [("1h", 60, 30), ("2h", 120, 30), ("4h", 240, 30)])
def test_hourly_sizes_are_aggregated_from_a_native_size(interval, minutes, native):
    """The feed rejects frequency 60 and 240, so these are built locally."""
    plan = resolve("1D", interval)
    assert plan.frequency in NATIVE_MINUTES
    assert plan.frequency == native
    assert plan.aggregate_minutes == minutes
    assert any("aggregated" in n for n in plan.notes)


def test_intraday_beyond_the_minute_window_coarsens_and_says_so():
    """A year of 5-minute bars does not exist anywhere.

    The range is the more deliberate choice, so it is preserved and the bar
    size gives way -- but never silently.
    """
    plan = resolve("1Y", "5m")
    assert plan.frequency_type == "daily"
    assert plan.served_interval_key == "1D"
    assert plan.interval_key == "5m", "the original request is still reported"
    assert any("reach about" in n for n in plan.notes)


def test_the_intraday_limit_is_per_bar_size_not_one_blanket_rule():
    """Measured: 1-minute is capped at 12,870 candles (~48 days) while 5m and
    coarser reach ~260 days. Treating them alike -- as a single 10-day rule
    once did -- refuses 30-minute bars over three months that the feed serves
    perfectly well.
    """
    assert "1m" in valid_intervals("10D"), "1-minute reaches well past 10 days"
    assert "1m" not in valid_intervals("3M"), "but not three months"
    assert resolve("3M", "1m").served_interval_key == "10m", (
        "coarsened within intraday, not dropped to daily"
    )
    assert resolve("1M", "30m").frequency_type == "minute"
    assert resolve("3M", "30m").frequency_type == "minute"
    assert resolve("6M", "30m").frequency_type == "minute"
    assert resolve("1Y", "30m").frequency_type == "daily", "a year is past even 30m"


def test_reach_is_ordered_so_a_coarser_bar_never_reaches_less_far():
    reaches = [INTRADAY_REACH_DAYS[n] for n in sorted(INTRADAY_REACH_DAYS)]
    assert reaches == sorted(reaches)


@pytest.mark.parametrize("rng,days", [
    ("1M", 30), ("6M", 180), ("1Y", 365), ("2Y", 730), ("10Y", 3650),
])
def test_non_intraday_requests_carry_an_exact_window(rng, days):
    """The window is what bounds the request, so the range is served exactly.

    The period ladder this replaced rounded up to the next bucket, which is
    how "3M / Weekly" became a full year of candles.
    """
    now = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)
    plan = resolve(rng, "1D", now=now)
    assert plan.start is not None and plan.end == now
    assert (plan.end - plan.start).days == days


def test_ytd_is_measured_from_january_not_as_a_fixed_365_days():
    now = datetime(2026, 3, 1, 20, 0, tzinfo=timezone.utc)
    plan = resolve("YTD", "1D", now=now)
    assert (plan.end - plan.start).days == 59
    assert plan.start.year == 2026 and plan.start.month == 1


def test_one_period_type_serves_daily_weekly_and_monthly():
    """Verified live: once dates are supplied periodType is only validated
    against frequencyType, and `year` is accepted by all three."""
    for interval, kind in [("1D", "daily"), ("1W", "weekly"), ("1Mo", "monthly")]:
        plan = resolve("10Y", interval)
        assert plan.frequency_type == kind
        assert plan.period_type == "year", "month + monthly is rejected by the feed"
        assert plan.start is not None


def test_sub_minute_intervals_raise_with_the_reason():
    """Asked for constantly; impossible without a tick feed. Say so rather
    than rounding up to a minute and pretending."""
    for key in UNSUPPORTED_INTERVALS:
        with pytest.raises(ValueError, match="tick/streaming feed"):
            resolve("1D", key)


def test_unknown_keys_raise():
    with pytest.raises(ValueError, match="unknown range"):
        resolve("3Y", "1D")
    with pytest.raises(ValueError, match="unknown interval"):
        resolve("1D", "7m")


# ----------------------------------------------------------------------
# What the UI is allowed to offer
# ----------------------------------------------------------------------


def test_valid_intervals_exclude_the_impossible():
    day = valid_intervals("1D")
    assert "1m" in day and "4h" in day
    assert "1W" not in day and "1Mo" not in day, "a week bar over one day is meaningless"

    year = valid_intervals("1Y")
    assert year == ["1D", "1W", "1Mo"]
    assert "1m" not in year


def test_every_offered_interval_actually_resolves():
    """The matrix drives the UI, so anything it offers must be servable
    without triggering a clamp -- otherwise the greying-out is pointless."""
    for range_key in RANGES:
        for interval in valid_intervals(range_key):
            plan = resolve(range_key, interval)
            assert plan.served_interval_key == interval, (
                f"{range_key}/{interval} was offered but got clamped to "
                f"{plan.served_interval_key}"
            )


def test_capability_matrix_is_json_safe_and_complete():
    import json
    cap = json.loads(json.dumps(capability_matrix()))
    assert {r["key"] for r in cap["ranges"]} == set(RANGES)
    assert cap["max_intraday_days"] == MAX_INTRADAY_DAYS
    assert "1s" in cap["unsupported"]
    assert all(r["intervals"] for r in cap["ranges"]), "every range must offer something"


# ----------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------


def _bars(n, minutes=30, start=None):
    start = start or datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc)
    rows = [{
        "time": start + timedelta(minutes=minutes * i),
        "open": 100.0 + i, "high": 100.5 + i, "low": 99.5 + i,
        "close": 100.2 + i, "volume": 1000.0,
    } for i in range(n)]
    return build_bars(instruments.SPX, rows, interval_minutes=minutes)


def test_aggregation_preserves_ohlcv_semantics():
    """Aligned start, so hourly buckets pair cleanly."""
    src = _bars(4, minutes=30, start=datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc))
    out = aggregate(src, 60)

    assert out.interval_minutes == 60
    assert len(out) == 2
    assert out.open[0] == src.open[0]
    assert out.close[0] == src.close[1]
    assert out.high[0] == max(src.high[0], src.high[1])
    assert out.low[0] == min(src.low[0], src.low[1])
    assert out.volume[0] == src.volume[0] + src.volume[1]


def test_session_open_lands_in_a_partial_bucket():
    """The US open is 13:30 UTC, i.e. mid-hour.

    Wall-clock bucketing therefore puts the 13:30 bar alone in the 13:00
    hour. That is the intended behaviour and the reason for not pairing bars
    by count: candles stay on the hour instead of drifting off it.
    """
    src = _bars(4, minutes=30, start=datetime(2026, 8, 10, 13, 30, tzinfo=timezone.utc))
    out = aggregate(src, 60)

    assert [str(t)[11:16] for t in out.time] == ["13:00", "14:00", "15:00"]
    assert list(out.volume) == [1000.0, 2000.0, 1000.0]


def test_aggregation_buckets_on_wall_clock_not_bar_count():
    """A missing bar must not shift every later candle.

    Counting in groups of two drifts permanently after any gap; bucketing on
    the clock self-corrects.
    """
    start = datetime(2026, 8, 10, 13, 0, tzinfo=timezone.utc)
    times = [0, 30, 60, 150]            # 14:00-14:30 is missing
    rows = [{
        "time": start + timedelta(minutes=m),
        "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1.0,
    } for m in times]
    src = build_bars(instruments.SPX, rows, interval_minutes=30)

    out = aggregate(src, 60)
    hours = [str(t)[11:16] for t in out.time]
    assert hours == ["13:00", "14:00", "15:00"]
    # The gap leaves its hour with one bar, not a borrowed neighbour.
    assert list(out.volume) == [2.0, 1.0, 1.0]


def test_aggregation_is_a_noop_when_not_coarser():
    src = _bars(6, minutes=30)
    assert aggregate(src, 30) is src
    assert aggregate(src, 5) is src


def test_aggregation_records_its_provenance():
    out = aggregate(_bars(8, minutes=30), 120)
    assert out.meta["aggregated_from_minutes"] == 30


def test_vwap_still_works_after_aggregation():
    out = aggregate(_bars(8, minutes=30), 120)
    assert np.all(np.isfinite(out.vwap()))


# ----------------------------------------------------------------------
# The requested range must actually be the range served
# ----------------------------------------------------------------------


@pytest.mark.parametrize("range_key,days", [("3M", 90), ("6M", 180), ("1Y", 365), ("2Y", 730)])
def test_weekly_bars_honour_the_requested_range(range_key, days):
    """Weekly used to be sent as a year period regardless of the range.

    "3M / Weekly" therefore drew a full year. Nothing announced it: the axis
    relabels and a year of weekly candles looks like a perfectly ordinary
    chart. An explicit window removes the whole class of bug -- there is no
    bucket left to round up to.
    """
    now = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)
    plan = resolve(range_key, "1W", now=now)
    assert plan.frequency_type == "weekly"
    assert (plan.end - plan.start).days == days


def test_no_offered_combination_reaches_past_its_range():
    """The blanket guard against the whole class, not just the weekly case."""
    now = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)
    for range_key in RANGES:
        days = RANGES[range_key].days
        for interval in valid_intervals(range_key):
            plan = resolve(range_key, interval, now=now)
            if plan.start is None:
                # Session-counted intraday: bounded by `period`, which is
                # never more than the 10-day ceiling.
                assert plan.period_type == "day" and plan.period <= 10
                continue
            span = (plan.end - plan.start).days
            assert days is None or span == days, (
                f"{range_key}/{interval} requests {span}d for a {days}d range"
            )


def test_short_intraday_ranges_still_count_sessions_rather_than_clock_time():
    """`periodType=day` returns the last real *session*, so a 1D chart is not
    empty before Monday's open. An explicit window ending now would start
    after Friday's close and come back with nothing.
    """
    for range_key in ("1D", "5D", "10D"):
        plan = resolve(range_key, "5m")
        assert plan.start is None, "short intraday must not use a clock window"
        assert plan.period_type == "day"


def test_long_intraday_uses_a_window_because_the_day_period_tops_out_at_ten():
    now = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)
    plan = resolve("6M", "30m", now=now)
    assert plan.frequency_type == "minute" and plan.frequency == 30
    assert (plan.end - plan.start).days == 180


def test_the_request_carries_epoch_millisecond_dates():
    now = datetime(2026, 8, 10, 20, 0, tzinfo=timezone.utc)
    params = resolve("3M", "1D", now=now).request_params()
    assert params["endDate"] == int(now.timestamp() * 1000)
    assert params["startDate"] < params["endDate"]
    # periodType still travels: the feed validates it against frequencyType
    # even though it no longer bounds the window.
    assert params["periodType"] == "year" and params["frequencyType"] == "daily"


def test_a_daily_bar_over_one_day_is_not_offered():
    """One candle is not a chart -- the same reason a week bar over one day
    is already excluded."""
    assert "1D" not in valid_intervals("1D")
    assert valid_intervals("1D"), "the range must still offer intraday sizes"
    assert "1D" in valid_intervals("1M")


# ----------------------------------------------------------------------
# The bar-count guard — a rendering limit, not a feed one
# ----------------------------------------------------------------------


def test_a_month_of_one_minute_bars_is_coarsened_rather_than_drawn():
    """The feed serves it; the browser should not be asked to draw it.

    Measured: 27,256 candles took 2.1s and 54,558 SVG nodes, re-run on every
    30-second refresh, to show ~45 candles per pixel.
    """
    plan = resolve("1M", "1m")
    assert plan.served_interval_key != "1m"
    assert plan.frequency_type == "minute", "still intraday — only the size gave way"
    assert any("candles" in n for n in plan.notes)


def test_coarsening_steps_up_the_ladder_rather_than_collapsing_to_daily():
    """Falling straight to daily would throw away perfectly good intraday
    bars because a finer size than the budget allows was asked for."""
    plan = resolve("1M", "1m")
    assert plan.served_interval_key == "5m"
    assert plan.frequency_type == "minute"

    # And six months of 30-minute bars is inside the budget, so it is served
    # as asked rather than coarsened at all.
    assert resolve("6M", "30m").served_interval_key == "30m"


def test_the_guard_falls_through_to_daily_when_no_intraday_size_reaches():
    plan = resolve("1Y", "30m")
    assert plan.served_interval_key == "1D"
    assert any("reach about" in n for n in plan.notes)


def test_every_offered_intraday_combination_stays_under_the_plot_budget():
    """The selector must offer only what will actually be served, so a
    coarsening note should never appear for something it offered."""
    for range_key in RANGES:
        days = RANGES[range_key].days or 222
        for interval in valid_intervals(range_key):
            iv = INTERVALS[interval]
            if not iv.intraday:
                continue
            assert estimated_bars(days, iv.minutes) <= MAX_PLOTTED_BARS, (
                f"{range_key}/{interval} is offered but would draw "
                f"{estimated_bars(days, iv.minutes):,} candles"
            )


def test_the_new_reach_actually_widens_what_is_offered():
    """Regression in the other direction: the old blanket 10-day rule refused
    30-minute bars over three months that the feed serves fine."""
    assert "30m" in valid_intervals("3M")
    assert "1h" in valid_intervals("6M")
    assert "1m" not in valid_intervals("3M"), "still past 1-minute's reach"
