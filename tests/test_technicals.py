"""The technical chart: standard indicators exactly, gexdash rules as written.

The indicator tests pin definitions against hand computation. The rule tests
build series where the right answer is obvious by construction, because the
rules are gexdash's own and the only thing worth testing is that the code
does what RULES says it does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from gexdash import instruments
from gexdash.bars import build_bars
from gexdash.indicators import atr, bollinger, ema, sma, smma, supertrend, wilder
from gexdash.providers.mock import MockProvider
from gexdash.server import app, set_provider
from gexdash.store import CachedProvider, ChainStore
from gexdash.technicals import (
    ET, RULES, TECH_INTERVALS, TechParams, aggregate_session, capabilities, compute,
    display_start_for, extreme_levels, plan_for, position_signals, range_events,
    session_dates, session_kind, vams_state, volume_profile,
)

NAN = np.nan


# ----------------------------------------------------------------------
# Standard indicators
# ----------------------------------------------------------------------


def test_smma_is_wilder_smoothing_not_sma_or_ema():
    x = np.array([10, 11, 13, 12, 15, 14, 18, 17, 16, 20], dtype=float)
    s = smma(x, 4)
    np.testing.assert_allclose(s, wilder(x, 4), equal_nan=True)

    # Seed is the first-n mean, then (prev * (n-1) + x) / n.
    assert np.isnan(s[:3]).all()
    assert s[3] == pytest.approx(np.mean(x[:4]))
    assert s[4] == pytest.approx((s[3] * 3 + x[4]) / 4)

    # And it is neither of the two averages it is most often confused with.
    assert not np.allclose(s[4:], sma(x, 4)[4:])
    assert not np.allclose(s[4:], ema(x, 4)[4:])


def test_bollinger_uses_population_standard_deviation():
    rng = np.random.default_rng(3)
    x = 100 + rng.normal(0, 2, 40)
    b = bollinger(x, 20, 2.0)
    window = x[-20:]
    assert b["basis"][-1] == pytest.approx(window.mean())
    assert b["upper"][-1] - b["basis"][-1] == pytest.approx(2 * window.std(ddof=0))
    # The sample form is measurably wider -- this is the trap being pinned.
    assert b["upper"][-1] - b["basis"][-1] < 2 * window.std(ddof=1)
    assert np.isnan(b["upper"][:19]).all()


def test_atr_true_range_uses_the_previous_close():
    high = np.array([11.0, 12.0, 20.0, 13.0])
    low = np.array([9.0, 10.0, 18.0, 11.0])
    close = np.array([10.0, 11.0, 19.0, 12.0])
    # True ranges: 2 (first bar: high-low), 2, |20-11| = 9, |11-19| = 8.
    a = atr(high, low, close, 2)
    assert np.isnan(a[0])
    assert a[1] == pytest.approx(2.0)                 # seed: mean(2, 2)
    assert a[2] == pytest.approx(2.0 + (9 - 2.0) / 2)
    assert a[3] == pytest.approx(a[2] + (8 - a[2]) / 2)


# ----------------------------------------------------------------------
# VAMS
# ----------------------------------------------------------------------


# ----------------------------------------------------------------------
# Supertrend: a port of the pasted TradingView v4 script
# ----------------------------------------------------------------------


def test_supertrend_matches_a_hand_worked_pine_evaluation():
    """period 1 makes ATR equal to each bar's true range, so every step can be
    checked by hand. Bars 1 and 3 are the porting traps: the close is beyond
    *this* bar's ratcheted band but not the *previous* bar's, which is the one
    Pine compares against -- so the trend must not flip there."""
    h = np.array([10.5, 12.0, 10.2, 9.7, 10.4])
    l = np.array([9.5, 10.0, 9.0, 8.5, 9.6])
    c = np.array([10.0, 10.2, 9.2, 9.65, 10.3])
    st = supertrend(h, l, c, period=1, mult=0.25)

    np.testing.assert_allclose(st["up"], [9.75, 10.5, 9.3, 8.8, 9.8])
    np.testing.assert_allclose(st["dn"], [10.25, 10.25, 9.9, 9.4, 10.2])
    np.testing.assert_array_equal(st["trend"], [1, 1, -1, -1, 1])
    assert st["sell"].tolist() == [False, False, True, False, False]
    assert st["buy"].tolist() == [False, False, False, False, True]
    # Bar 1: close 10.2 is under this bar's band (10.5) but above up1 (9.75).
    assert c[1] < st["up"][1] and st["trend"][1] == 1
    # Bar 3: close 9.65 is above this bar's band (9.4) but below dn1 (9.9).
    assert c[3] > st["dn"][3] and st["trend"][3] == -1


def test_supertrend_holds_the_initial_up_trend_through_atr_warm_up():
    h = np.array([11, 12, 13, 12, 11, 10], dtype=float)
    l = h - 2
    c = h - 1
    st = supertrend(h, l, c, period=3, mult=3.0, wilder_atr=False)
    # sma(tr, 3) with tr na on bar 0: no ATR until bar 3.
    assert np.isnan(st["atr"][:3]).all() and np.isfinite(st["atr"][3])
    assert np.isnan(st["up"][:3]).all()
    assert (st["trend"][:3] == 1).all() and not st["buy"].any()


def test_supertrend_invariants_on_a_long_walk():
    rng = np.random.default_rng(7)
    c = 100 * np.exp(np.cumsum(rng.normal(0, 0.015, 3000)))
    h = c * (1 + np.abs(rng.normal(0, 0.006, 3000)))
    l = c * (1 - np.abs(rng.normal(0, 0.006, 3000)))
    st = supertrend(h, l, c)
    up, dn = st["up"], st["dn"]
    for i in range(1, len(c)):
        if np.isfinite(up[i - 1]) and c[i - 1] > up[i - 1]:
            assert up[i] >= up[i - 1]          # the lower band only ratchets up
        if np.isfinite(dn[i - 1]) and c[i - 1] < dn[i - 1]:
            assert dn[i] <= dn[i - 1]          # the upper band only ratchets down
    flips = [("buy" if st["buy"][i] else "sell") for i in range(len(c)) if st["buy"][i] or st["sell"][i]]
    assert len(flips) > 10
    assert flips[0] == "sell"                   # the trend starts up
    assert all(a != b for a, b in zip(flips, flips[1:]))


def test_compute_draws_one_supertrend_band_per_bar_and_prices_flips_on_it():
    bars, start = _daily_bars(1500)
    d = compute(bars, display_start=start + timedelta(days=1000), interval="1D")
    up, dn = d["series"]["st_up"], d["series"]["st_dn"]
    assert all((u is None) != (v is None) for u, v in zip(up, dn))
    sigs = d["supertrend"]["signals"]
    assert sigs and {s["type"] for s in sigs} <= {"ST_BUY", "ST_SELL"}
    for s in sigs:
        band = up[s["i"]] if s["type"] == "ST_BUY" else dn[s["i"]]
        assert s["price"] == pytest.approx(band)
    assert d["supertrend"]["trend"] in ("up", "down")
    assert d["params"]["st_period"] == 10 and d["params"]["st_mult"] == 3.0


def test_vams_check_and_latched_state_with_decay():
    close = np.array([100, 102, 100, 100, 100, 100, 100, 98, 100], dtype=float)
    basis = np.full(9, 100.0)
    atr_values = np.full(9, 2.0)                 # triggers at 101 / 99
    v = vams_state(close, basis, atr_values, mult=0.5, decay=5)

    assert v["check"].tolist() == [0, 1, 0, 0, 0, 0, 0, -1, 0]
    # Bullish latches through no-call bars (indices 2-5), decays to neutral on
    # the 5th consecutive one (index 6), and a bearish call flips it at once.
    assert v["state"].tolist() == [0, 1, 1, 1, 1, 1, 0, -1, -1]
    assert v["upper"][0] == 101 and v["lower"][0] == 99


def test_vams_claims_nothing_during_warm_up():
    close = np.array([100.0, 105.0, 95.0])
    basis = np.array([NAN, 100.0, 100.0])
    v = vams_state(close, basis, np.full(3, 2.0), mult=0.5, decay=5)
    assert np.isnan(v["check"][0]) and np.isnan(v["state"][0])
    assert v["check"][1] == 1 and v["state"][2] == -1


# ----------------------------------------------------------------------
# Ranges
# ----------------------------------------------------------------------


def _flat_then(after_close: list[float], *, n_flat: int = 30, width: float = 2.0):
    high = np.r_[np.full(n_flat, 100 + width / 2), np.array(after_close) + 0.5]
    low = np.r_[np.full(n_flat, 100 - width / 2), np.array(after_close) - 0.5]
    close = np.r_[np.full(n_flat, 100.0), np.array(after_close)]
    return high, low, close


def test_breakout_from_a_tight_base_records_the_box():
    high, low, close = _flat_then([103.0, 104.0])
    r = range_events(high, low, close, np.full(len(close), 1.0),
                     length=20, base_mult=5.0, cooldown=10)
    assert [e["type"] for e in r["events"]] == ["breakout"]   # cooldown eats bar 31
    e = r["events"][0]
    assert e["i"] == 30
    assert e["box"] == {"start": 10, "end": 30, "high": 101.0, "low": 99.0}


def test_breakdown_is_symmetric():
    high, low, close = _flat_then([97.0])
    r = range_events(high, low, close, np.full(len(close), 1.0),
                     length=20, base_mult=5.0, cooldown=10)
    assert [e["type"] for e in r["events"]] == ["breakdown"]


def test_a_wide_range_is_not_a_base():
    # 20-point span against an ATR of 1: a trend, not a consolidation.
    high, low, close = _flat_then([115.0], width=20.0)
    r = range_events(high, low, close, np.full(len(close), 1.0),
                     length=20, base_mult=5.0, cooldown=10)
    assert r["events"] == []
    assert r["current"]["is_base"] is False


# ----------------------------------------------------------------------
# Position signals
# ----------------------------------------------------------------------


def _pos(close, low, state, rsi_values, *, trend=50.0, pull=90.0, add_spacing=10):
    n = len(close)
    return position_signals(
        np.asarray(close, float), np.asarray(low, float), np.asarray(state, float),
        np.full(n, trend), np.full(n, pull), np.asarray(rsi_values, float),
        trim_rsi=70.0, add_spacing=add_spacing,
    )


def test_buy_then_trim_then_sell_from_one_position():
    close = [100, 100, 101, 102, 95, 85]
    low = [99, 99, 100, 101, 94, 84]
    state = [0, 1, 1, 1, -1, -1]
    rsi_values = [50, 60, 75, 65, 40, 30]
    out = _pos(close, low, state, rsi_values)
    assert [(s["i"], s["type"]) for s in out["signals"]] == [(1, "BUY"), (3, "TRIM"), (5, "SELL")]
    # Bearish at bar 4 but still above SMMA 50 (90): not a SELL yet.
    assert out["position"] == "flat"


def test_no_sell_trim_or_add_while_flat():
    close = [100, 100, 85, 100]
    low = [99, 89, 84, 89]
    state = [-1, -1, -1, -1]                 # never bullish -> never bought
    rsi_values = [80, 65, 40, 75]
    out = _pos(close, low, state, rsi_values)
    assert out["signals"] == [] and out["position"] == "flat"


def test_buy_needs_the_long_term_trend():
    out = _pos([100, 100], [99, 99], [0, 1], [50, 55], trend=120.0)
    assert out["signals"] == []


def test_add_on_a_held_test_of_the_50_respects_spacing():
    close = [100, 100, 100, 100, 100]
    low = [99, 99, 89, 89, 89]           # tests SMMA 50 (90) on bars 2-4
    state = [0, 1, 1, 1, 1]
    out = _pos(close, low, state, [50] * 5, add_spacing=2)
    assert [(s["i"], s["type"]) for s in out["signals"]] == [(1, "BUY"), (3, "ADD")]


# ----------------------------------------------------------------------
# Profile and levels
# ----------------------------------------------------------------------


def test_volume_profile_conserves_volume_and_finds_the_poc():
    o = np.array([10.0, 12.0, 11.0, 11.0])
    c = np.array([12.0, 10.0, 11.5, 11.0])
    h = np.array([12.0, 12.0, 11.6, 11.0])
    l = np.array([10.0, 10.0, 11.4, 11.0])         # last bar has zero range
    v = np.array([100.0, 200.0, 5000.0, 50.0])
    p = volume_profile(o, h, l, c, v, bins=10)
    assert sum(p["up"]) + sum(p["down"]) == pytest.approx(v.sum())
    assert sum(p["down"]) == pytest.approx(200.0)   # the one down bar
    assert 11.4 <= p["poc"] <= 11.6                 # where the heavy bar traded


def test_extreme_levels_merge_when_three_month_and_year_agree():
    bpy = 252
    # The last 70 bars cover the 63-bar 3-month window entirely.
    high = np.r_[np.full(182, 100.0), np.full(70, 120.0)]   # year high is recent
    low = np.r_[np.full(182, 50.0), np.full(70, 80.0)]      # year low is old
    by_key = {lv["key"]: lv for lv in extreme_levels(high, low, bars_per_year=bpy)}
    assert by_key["high_3m_52w"]["price"] == 120.0
    assert by_key["low_3m"]["price"] == 80.0
    assert by_key["low_52w"]["price"] == 50.0


# ----------------------------------------------------------------------
# compute(): warm-up and trimming
# ----------------------------------------------------------------------


def _daily_bars(n: int, *, seed: int = 1):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0004, 0.012, n)))
    start = datetime(2015, 1, 1, tzinfo=timezone.utc)
    rows = [{
        "time": start + timedelta(days=i), "open": close[i - 1] if i else close[0],
        "high": close[i] * 1.006, "low": close[i] * 0.994, "close": close[i],
        "volume": 1e6 + i,
    } for i in range(n)]
    return build_bars(instruments.get("QQQ"), rows, interval_minutes=1440), start


def test_compute_trims_to_the_window_after_warming_up():
    bars, start = _daily_bars(1500)
    display_start = start + timedelta(days=1200)
    d = compute(bars, display_start=display_start, interval="1D")

    assert len(d["close"]) == 300 and d["warmup"]["bars_before_window"] == 1200
    for key in ("smma200", "smma100", "ema9", "bb20_3_upper", "rsi", "macd_signal"):
        assert len(d["series"][key]) == 300
        assert None not in d["series"][key], f"{key} has warm-up gaps inside the window"
    assert None not in d["vams"]["state"]
    assert d["prev_close"] == pytest.approx(bars.close[1199])
    assert all(0 <= s["i"] < 300 for s in d["signals"])
    assert all(0 <= b["start"] <= b["end"] < 300 for b in d["ranges"])
    assert d["warmup"]["notes"] == []


def test_compute_without_history_leaves_warm_up_null_and_says_so():
    bars, start = _daily_bars(260)
    d = compute(bars, display_start=start, interval="1D")
    assert d["series"]["smma200"][198] is None and d["series"]["smma200"][199] is not None
    assert d["series"]["ema9"][7] is None and d["series"]["ema9"][8] is not None
    assert d["series"]["macd_signal"][32] is None and d["series"]["macd_signal"][33] is not None
    assert any("SMMA 200" in note for note in d["warmup"]["notes"])


def test_every_rule_in_the_payload_is_written_down():
    keys = {r["key"] for r in RULES}
    assert {"vams", "vams_check", "range", "breakout", "buy", "add", "trim", "sell", "earnings"} <= keys
    assert all(r["text"] for r in RULES)


# ----------------------------------------------------------------------
# Planning
# ----------------------------------------------------------------------


def test_plan_reaches_the_full_history_but_displays_the_range():
    now = datetime(2026, 11, 23, 15, tzinfo=timezone.utc)
    plan, display_start = plan_for("1Y", "1D", now=now)
    assert (now - plan.start).days >= 3600          # ~10 years of daily bars
    assert display_start == now - timedelta(days=365)
    assert plan.frequency_type == "daily"

    _, ytd = plan_for("YTD", "1W", now=now)
    assert ytd == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_plan_rejects_unknown_and_unoffered_pairs():
    with pytest.raises(ValueError, match="not offered at 5 min"):
        plan_for("1Y", "5m")
    with pytest.raises(ValueError, match="unknown range"):
        plan_for("7Y", "1D")
    with pytest.raises(ValueError, match="unknown interval"):
        plan_for("1D", "2m")


def test_weekly_and_daily_have_their_own_year_length():
    assert TECH_INTERVALS["1D"].bars_per_year == 252 and TECH_INTERVALS["1W"].bars_per_year == 52
    assert all(iv.bars_per_year is None for iv in TECH_INTERVALS.values() if iv.intraday)


# ----------------------------------------------------------------------
# Intraday: planning, sessions, anchoring
# ----------------------------------------------------------------------


NOW = datetime(2026, 11, 23, 18, 40, tzinfo=timezone.utc)   # Monday 13:40 ET


def test_intraday_plan_is_an_explicit_window_ending_now():
    """The period form without dates ends at the previous session's close,
    so on a Monday afternoon it would show Friday. Always a window to now."""
    plan, start = plan_for("5D", "5m", now=NOW, session="rth")
    assert plan.end == NOW and plan.start is not None
    assert plan.frequency_type == "minute" and plan.frequency == 5
    assert plan.extended_hours is False and start is None      # 5D counts sessions
    assert plan_for("5D", "5m", now=NOW, session="eth")[0].extended_hours is True


def test_intraday_fetch_window_is_fixed_per_bar_size_so_ranges_share_it():
    a, _ = plan_for("1D", "5m", now=NOW, session="rth")
    b, _ = plan_for("1M", "5m", now=NOW, session="rth")
    assert a.start == b.start
    # Longest range plus ~1,000 bars of warm-up: 1M + ~13 sessions of 78 bars.
    assert 45 <= (NOW - a.start).days <= 60


def test_one_minute_stays_inside_the_candle_cap_and_hourly_is_built_from_30m():
    one, _ = plan_for("5D", "1m", now=NOW, session="futures")
    assert (NOW - one.start).days <= 11          # ~8 sessions x 1,380 < 12,870
    hourly, _ = plan_for("3M", "1h", now=NOW, session="rth")
    four, _ = plan_for("6M", "4h", now=NOW, session="rth")
    assert hourly.frequency == four.frequency == 30
    assert hourly.start == four.start            # one 30m fetch serves both


def test_session_kind_defaults():
    assert session_kind("1D", is_future=False) == (None, [])
    assert session_kind("5m", is_future=False) == ("rth", [])
    assert session_kind("5m", is_future=False, requested="eth") == ("eth", [])
    kind, notes = session_kind("5m", is_future=True, requested="rth")
    assert kind == "futures" and notes and "full electronic session" in notes[0]


def _intraday(times_et: list[datetime], *, minutes: int, closes=None, inst="QQQ"):
    closes = closes or [100.0 + k for k in range(len(times_et))]
    rows = [{"time": t.astimezone(timezone.utc), "open": c - 0.5, "high": c + 1.0,
             "low": c - 1.0, "close": c, "volume": 10.0 + k}
            for k, (t, c) in enumerate(zip(times_et, closes))]
    return build_bars(instruments.get(inst), rows, interval_minutes=minutes)


def _rth_day(d, minutes):
    open_ = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
    return [open_ + timedelta(minutes=minutes * k) for k in range(390 // minutes)]


def test_hourly_bars_are_anchored_to_the_regular_session_open():
    day = datetime(2026, 11, 20).date()
    bars = _intraday(_rth_day(day, 30), minutes=30)          # 13 half-hour bars
    hourly = aggregate_session(bars, 60, "rth")
    starts = [datetime.fromtimestamp(int(t.astype("int64")), tz=timezone.utc).astimezone(ET).strftime("%H:%M")
              for t in hourly.time.astype("datetime64[s]")]
    assert starts == ["09:30", "10:30", "11:30", "12:30", "13:30", "14:30", "15:30"]
    # OHLCV of the first hour = bars 0 and 1; the last hour is one 30m bar.
    assert hourly.open[0] == bars.open[0] and hourly.close[0] == bars.close[1]
    assert hourly.high[0] == max(bars.high[:2]) and hourly.volume[0] == bars.volume[0] + bars.volume[1]
    assert hourly.close[-1] == bars.close[-1] and hourly.volume[-1] == bars.volume[-1]

    four = aggregate_session(bars, 240, "rth")
    assert len(four) == 2 and four.close[0] == bars.close[7]   # 9:30-13:30 is 8 bars


def test_anchoring_survives_the_daylight_saving_change():
    """1 Nov 2026 ends DST: the session still opens at 9:30 ET on either side."""
    bars = _intraday(_rth_day(datetime(2026, 10, 30).date(), 30) +
                     _rth_day(datetime(2026, 11, 2).date(), 30), minutes=30)
    hourly = aggregate_session(bars, 60, "rth")
    et = [datetime.fromtimestamp(int(t.astype("int64")), tz=timezone.utc).astimezone(ET)
          for t in hourly.time.astype("datetime64[s]")]
    assert len(hourly) == 14 and all(t.minute == 30 for t in et)


def test_futures_four_hour_bars_start_at_the_evening_open():
    start = datetime(2026, 11, 22, 18, 0, tzinfo=ET)          # Sunday 18:00
    bars = _intraday([start + timedelta(minutes=30 * k) for k in range(46)], minutes=30, inst="ES")
    four = aggregate_session(bars, 240, "futures")
    hours = [datetime.fromtimestamp(int(t.astype("int64")), tz=timezone.utc).astimezone(ET).hour
             for t in four.time.astype("datetime64[s]")]
    assert hours == [18, 22, 2, 6, 10, 14]
    assert set(session_dates(bars.time, "futures")) == {datetime(2026, 11, 23).date()}


def test_session_ranges_count_sessions_not_hours():
    days = [datetime(2026, 11, d).date() for d in (17, 18, 19, 20, 23)]   # Tue..Fri, Mon
    bars = _intraday([t for d in days for t in _rth_day(d, 30)], minutes=30)
    one = display_start_for("1D", bars, session="rth", fallback=None)
    assert one.astimezone(ET) == datetime(2026, 11, 23, 9, 30, tzinfo=ET)
    five = display_start_for("5D", bars, session="rth", fallback=None)
    assert five.astimezone(ET) == datetime(2026, 11, 17, 9, 30, tzinfo=ET)
    # Calendar ranges keep their fallback.
    fb = datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert display_start_for("1M", bars, session="rth", fallback=fb) == fb


def test_compute_on_intraday_marks_sessions_and_takes_levels_from_daily():
    days = [datetime(2026, 11, d).date() for d in (17, 18, 19, 20, 23)]
    bars = _intraday([t for d in days for t in _rth_day(d, 30)], minutes=30)
    daily, _ = _daily_bars(400)
    start = display_start_for("5D", bars, session="rth", fallback=None)
    d = compute(bars, display_start=start, interval="30m", session="rth", daily=daily)
    assert d["session"] == "rth" and d["session_breaks"] == [0, 13, 26, 39, 52]
    assert {lv["key"] for lv in d["levels"]} >= {"poc"}
    assert any(lv["price"] == pytest.approx(float(np.max(daily.high[-252:]))) for lv in d["levels"])
    # Without daily bars the levels are omitted, and the payload says why.
    d2 = compute(bars, display_start=start, interval="30m", session="rth", daily=None)
    assert [lv["key"] for lv in d2["levels"]] == ["poc"]
    assert any("daily history" in note for note in d2["warmup"]["notes"])


def test_capabilities_offer_every_interval_with_its_ranges():
    caps = capabilities()
    keys = [iv["key"] for iv in caps["intervals"]]
    assert keys == ["1m", "5m", "15m", "30m", "1h", "4h", "1D", "1W"]
    for iv in caps["intervals"]:
        assert iv["default_range"] in iv["ranges"]
        assert set(iv["ranges"]) <= set(caps["ranges"])


# ----------------------------------------------------------------------
# Endpoint
# ----------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    set_provider(CachedProvider(MockProvider(), ChainStore(tmp_path / "t.db")))
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("symbol", ["QQQ", "SPY", "USO", "ES", "NQ"])
def test_technicals_endpoint_serves_every_requested_symbol(client, symbol):
    d = client.get(f"/api/technicals?symbol={symbol}&range=1Y&interval=1D").json()
    assert d["available"] is True and d["symbol"] == symbol
    n = len(d["close"])
    assert n > 200
    assert all(len(v) == n for v in d["series"].values())
    assert len(d["vams"]["state"]) == n and len(d["vams"]["check"]) == n
    assert None not in d["series"]["smma200"]        # the over-fetch worked
    assert d["synthetic"] is True
    assert d["rules"] == RULES


def test_technicals_weekly(client):
    d = client.get("/api/technicals?symbol=QQQ&range=5Y&interval=1W").json()
    assert d["available"] and d["interval"] == "1W"
    assert 200 < len(d["close"]) < 300


def test_technicals_bad_requests(client):
    assert client.get("/api/technicals?symbol=NOPE!").status_code == 404
    assert client.get("/api/technicals?symbol=QQQ&interval=2m").status_code == 400
    assert client.get("/api/technicals?symbol=QQQ&range=9Y").status_code == 400
    assert client.get("/api/technicals?symbol=QQQ&session=overnight").status_code == 422


def test_an_unoffered_range_is_replaced_by_the_default_and_says_so(client):
    d = client.get("/api/technicals?symbol=QQQ&range=1Y&interval=1m").json()
    assert d["available"] and d["range"] == "1D" and d["interval"] == "1m"
    assert any("not offered" in note for note in d["notes"])


def _et_times(d):
    return [datetime.fromisoformat(t).replace(tzinfo=timezone.utc).astimezone(ET) for t in d["time"]]


def test_technicals_intraday_etf_is_regular_hours_by_default(client):
    d = client.get("/api/technicals?symbol=QQQ&range=5D&interval=5m").json()
    assert d["available"] and d["session"] == "rth"
    times = _et_times(d)
    assert all((9, 30) <= (t.hour, t.minute) < (16, 0) and t.weekday() < 5 for t in times)
    assert len({t.date() for t in times}) == 5 and len(d["session_breaks"]) == 5
    assert d["supertrend"]["trend"] in ("up", "down")
    assert any(lv["key"].endswith("52w") for lv in d["levels"])     # from daily bars
    assert len(d["series"]["smma200"]) == len(d["close"])


def test_technicals_hourly_bars_start_on_the_half_hour(client):
    d = client.get("/api/technicals?symbol=SPY&range=1M&interval=1h").json()
    assert d["available"] and {t.minute for t in _et_times(d)} == {30}


def test_technicals_futures_are_full_session_even_when_rth_is_asked(client):
    d = client.get("/api/technicals?symbol=ES&range=1D&interval=15m&session=rth").json()
    assert d["available"] and d["session"] == "futures"
    assert any("full electronic session" in note for note in d["notes"])


def test_technicals_30m_1h_4h_share_one_fetch(client, tmp_path):
    calls = []

    class Counting(MockProvider):
        def get_price_history(self, inst, *, plan=None, **kw):
            calls.append((plan.frequency_type, plan.frequency))
            return super().get_price_history(inst, plan=plan, **kw)

    set_provider(CachedProvider(Counting(), ChainStore(tmp_path / "s.db")))
    for iv, rng in (("30m", "3M"), ("1h", "3M"), ("4h", "6M")):
        assert client.get(f"/api/technicals?symbol=QQQ&range={rng}&interval={iv}").json()["available"]
    assert calls.count(("minute", 30)) == 1 and calls.count(("daily", 1)) == 1


def test_technicals_degrades_without_history(client, tmp_path):
    class NoBars(MockProvider):
        get_price_history = None

    set_provider(CachedProvider(NoBars(), ChainStore(tmp_path / "nb.db")))
    d = client.get("/api/technicals?symbol=QQQ").json()
    assert d["available"] is False and "does not supply price history" in d["reason"]


def test_technicals_shares_one_fetch_across_ranges(client, tmp_path):
    calls = []

    class Counting(MockProvider):
        def get_price_history(self, inst, **kw):
            calls.append(inst.key)
            return super().get_price_history(inst, **kw)

    set_provider(CachedProvider(Counting(), ChainStore(tmp_path / "c.db")))
    for rng in ("3M", "1Y", "2Y"):
        assert client.get(f"/api/technicals?symbol=SPY&range={rng}").json()["available"]
    assert calls == ["SPY"]


def test_technicals_passes_the_unverified_futures_note_through(client, tmp_path):
    class Unverified(MockProvider):
        def get_price_history(self, inst, **kw):
            bars = super().get_price_history(inst, **kw)
            bars.meta["unverified"] = "/ES history is unverified"
            return bars

    set_provider(CachedProvider(Unverified(), ChainStore(tmp_path / "u.db")))
    d = client.get("/api/technicals?symbol=ES").json()
    assert d["notes"][0] == "/ES history is unverified"


def test_history_endpoint_shows_the_unverified_note_where_the_terminal_reads_it(client, tmp_path):
    class Unverified(MockProvider):
        def get_price_history(self, inst, **kw):
            bars = super().get_price_history(inst, **kw)
            bars.meta["unverified"] = "/NQ history is unverified"
            return bars

    set_provider(CachedProvider(Unverified(), ChainStore(tmp_path / "u2.db")))
    d = client.get("/api/history?symbol=NQ&range=3M&interval=1D").json()
    assert d["plan"]["notes"][0] == "/NQ history is unverified"


def test_params_are_echoed(client):
    d = client.get("/api/technicals?symbol=QQQ").json()
    assert d["params"]["vams_atr_mult"] == TechParams().vams_atr_mult
    assert d["params"]["smma_lengths"] == [20, 50, 100, 200]


def test_index_ships_the_chart_view_and_loads_its_script_first(client):
    html = client.get("/").text
    assert 'id="view-chart"' in html and 'id="view-chart-btn"' in html
    assert html.index("/static/technicals.js") < html.index("/static/app.js")
