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
    MAX_INTRADAY_DAYS,
    NATIVE_MINUTES,
    RANGES,
    UNSUPPORTED_INTERVALS,
    aggregate,
    apply_max_bars,
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
    assert any(str(MAX_INTRADAY_DAYS) in n for n in plan.notes)


def test_ten_days_is_the_intraday_boundary():
    assert resolve("10D", "1m").frequency_type == "minute"
    assert resolve("1M", "1m").frequency_type == "daily"


@pytest.mark.parametrize("rng,ptype,period", [
    ("1M", "month", 1), ("6M", "month", 6), ("YTD", "ytd", 1),
    ("1Y", "year", 1), ("2Y", "year", 2), ("10Y", "year", 10),
])
def test_daily_period_selection(rng, ptype, period):
    plan = resolve(rng, "1D")
    assert (plan.period_type, plan.period) == (ptype, period)


def test_weekly_and_monthly_use_year_periods():
    assert resolve("10Y", "1W").frequency_type == "weekly"
    assert resolve("10Y", "1W").period == 10
    assert resolve("10Y", "1Mo").frequency_type == "monthly"


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


def _implied_days(plan):
    """Roughly how far back the provider request reaches."""
    return {"day": 1, "month": 30, "year": 365, "ytd": 365}[plan.period_type] * plan.period


@pytest.mark.parametrize("range_key,period_type,period", [
    ("3M", "month", 3), ("6M", "month", 6), ("YTD", "ytd", 1),
    ("1Y", "year", 1), ("2Y", "year", 2),
])
def test_weekly_bars_honour_the_requested_range(range_key, period_type, period):
    """Weekly used to be sent as a year period regardless of the range.

    "3M / Weekly" therefore drew a full year. Nothing announced it: the axis
    relabels and a year of weekly candles looks like a perfectly ordinary
    chart. Verified live that the feed accepts month and ytd periods with
    weekly frequency; only *monthly* is year-only.
    """
    plan = resolve(range_key, "1W")
    assert (plan.period_type, plan.period) == (period_type, period)
    assert plan.frequency_type == "weekly"


def test_no_offered_combination_reaches_far_past_its_range():
    """A blanket guard against the whole class, not just the weekly case."""
    for range_key in RANGES:
        days = RANGES[range_key].days
        if days is None or days < 30:
            continue          # sub-month ranges over-fetch by necessity, see below
        for interval in valid_intervals(range_key):
            plan = resolve(range_key, interval)
            if plan.max_bars:
                continue      # trimmed after fetching instead
            assert _implied_days(plan) <= days * 1.5, (
                f"{range_key}/{interval} requests ~{_implied_days(plan)}d "
                f"for a {days}d range"
            )


def test_short_daily_ranges_are_capped_because_no_shorter_period_exists():
    """periodType=day serves minute frequencies only, verified live.

    So a 5-day daily chart has to fetch a month and cut back.
    """
    assert resolve("5D", "1D").max_bars == 5
    assert resolve("10D", "1D").max_bars == 10
    assert resolve("1Y", "1D").max_bars is None, "no cap needed once the period fits"


def test_a_daily_bar_over_one_day_is_not_offered():
    """One candle is not a chart -- the same reason a week bar over one day
    is already excluded."""
    assert "1D" not in valid_intervals("1D")
    assert valid_intervals("1D"), "the range must still offer intraday sizes"
    assert "1D" in valid_intervals("1M")


def test_apply_max_bars_keeps_the_most_recent_and_says_so():
    src = _bars(40, minutes=1440)
    plan = resolve("5D", "1D")
    out = apply_max_bars(src, plan)

    assert len(out) == 5
    assert out.close[-1] == src.close[-1], "must keep the newest, not the oldest"
    assert out.open[0] == src.open[-5]
    assert any("dropped" in n for n in plan.notes), "a silent trim is the same bug"


def test_apply_max_bars_is_a_noop_when_the_series_already_fits():
    src = _bars(3, minutes=1440)
    plan = resolve("5D", "1D")
    assert apply_max_bars(src, plan) is src
    assert plan.notes == []


def test_apply_max_bars_counts_bars_rather_than_filtering_on_a_cutoff():
    """A wall-clock cutoff blanks the chart when the last session predates it.

    Ask for 1D before Monday's open and the newest real bar is Friday's.
    Counting keeps whatever exists; a cutoff would return nothing.
    """
    stale = _bars(30, minutes=1440, start=datetime(2020, 1, 6, 14, 30, tzinfo=timezone.utc))
    out = apply_max_bars(stale, resolve("5D", "1D"))
    assert len(out) == 5, "old bars are still bars"
