"""The lunar-solar cycle: the model does what RULES says it does.

As with the signal rules in test_technicals.py, the model is gexdash's own,
so these tests build series where the right answer is known by construction
-- a planted lunar effect the fit must recover, a path whose turns are
obvious -- and check the plumbing: daily fit whatever the bar size, no
lookahead, units that do not change with bar size, honest holdout numbers.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from gexdash import astro, cycles
from gexdash.cycles import (
    CYCLES, RULES, AstroParams, close_time, eval_times, features, fit, future_stamps, holdout,
    turning_points,
)
from gexdash.providers.mock import MockProvider
from gexdash.server import app, set_provider
from gexdash.store import CachedProvider, ChainStore


def _weekdays(start: date, end: date) -> list[date]:
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _series(planted_bp: float = 0.0, peak_deg: float = 40.0, noise: float = 0.01, seed: int = 1,
            start: date = date(2016, 11, 25), end: date = date(2026, 11, 25)):
    """Daily stamps and closes whose returns carry ``planted_bp`` basis points
    a day times cos(elongation - peak_deg), plus noise."""
    dates = _weekdays(start, end)
    t = np.array([close_time(d) for d in dates])
    elong = astro.positions(t)["elongation"]
    rng = np.random.default_rng(seed)
    r = planted_bp * 1e-4 * np.cos(np.radians(elong - peak_deg)) + rng.normal(0.0003, noise, len(t))
    r[0] = 0.0
    close = 100 * np.exp(np.cumsum(r))
    stamps = [f"{d.isoformat()}T05:00:00" for d in dates]
    return stamps, close


# ----------------------------------------------------------------------
# The fit
# ----------------------------------------------------------------------


def test_features_are_cos_and_sin_of_each_cycle_angle():
    pos = {"elongation": np.array([0.0, 90.0]), "mean_anomaly": np.array([180.0, 0.0]),
           "moon_lon": np.array([0.0, 0.0]), "sun_lon": np.array([270.0, 0.0])}
    X = features(pos)
    assert X.shape == (2, 2 * len(CYCLES))
    np.testing.assert_allclose(X[0, :2], [1, 0], atol=1e-12)      # phase at new moon
    np.testing.assert_allclose(X[1, :2], [0, 1], atol=1e-12)      # first quarter
    np.testing.assert_allclose(X[1, 2:4], [-1, 0], atol=1e-12)    # the tide term doubles the angle
    np.testing.assert_allclose(X[0, 4:6], [-1, 0], atol=1e-12)    # distance at apogee


def test_the_fit_recovers_a_planted_lunar_phase_effect():
    # 30 bp/day, peaking 40 degrees after new moon: 3.3 days.
    stamps, close = _series(planted_bp=30, peak_deg=40, noise=0.005)
    out = cycles.compute(stamps, close, bar_stamps=stamps[-252:], kind="daily", last_close=close[-1])
    phase = next(c for c in out["model"]["cycles"] if c["key"] == "phase")
    assert phase["amplitude_bp"] == pytest.approx(30, rel=0.2)
    assert phase["peak_days"] == pytest.approx(40 / 360 * astro.SYNODIC_DAYS, abs=1.0)
    # The cycles nobody planted stay small beside it.
    for c in out["model"]["cycles"]:
        if c["key"] != "phase":
            assert c["amplitude_bp"] < 8
    # And the holdout sees it: real skill shows up out of sample.
    h = out["model"]["holdout"]
    assert h["corr"] > h["noise"] and h["hit_rate"] > h["no_skill"]


def test_the_cyclical_part_carries_no_drift_over_the_fit():
    stamps, close = _series(seed=4)
    t = np.array([close_time(date.fromisoformat(s[:10])) for s in stamps])
    X = features(astro.positions(t))
    r = np.log(close[1:] / close[:-1])
    m = fit(r, X[1:], ridge=0.02)
    assert ((X[1:] - m["mean"]) @ m["coef"]).sum() == pytest.approx(0, abs=1e-12)
    assert float(m["intercept"]) == pytest.approx(r.mean())


def test_ridge_shrinks_toward_zero_and_zero_ridge_is_least_squares():
    stamps, close = _series(planted_bp=30, noise=0.005, seed=2)
    t = np.array([close_time(date.fromisoformat(s[:10])) for s in stamps[1:]])
    X, r = features(astro.positions(t)), np.log(close[1:] / close[:-1])
    ols = np.linalg.lstsq(np.c_[np.ones(len(r)), X], r, rcond=None)[0][1:]
    np.testing.assert_allclose(fit(r, X, ridge=0.0)["coef"], ols, atol=1e-10)
    assert np.linalg.norm(fit(r, X, ridge=0.5)["coef"]) < np.linalg.norm(ols)


def test_holdout_scores_against_the_no_skill_mix_not_fifty_percent():
    # Pure noise in a rising market: calls that lean "up" score above 50% by
    # luck. no_skill is what such calls score with no information.
    stamps, close = _series(seed=7)
    t = np.array([close_time(date.fromisoformat(s[:10])) for s in stamps[1:]])
    X, r = features(astro.positions(t)), np.log(close[1:] / close[:-1])
    h = holdout(r, X, share=0.25, ridge=0.02)
    assert h["days"] == len(r) - round(len(r) * 0.75)
    assert h["noise"] == pytest.approx(2 / np.sqrt(h["days"]), abs=1e-4)
    assert abs(h["corr"]) < 2 * h["noise"]
    assert abs(h["hit_rate"] - h["no_skill"]) < 0.06
    # The formula itself.
    cut = round(len(r) * 0.75)
    m = fit(r[:cut], X[:cut], ridge=0.02)
    calls = np.sign((X[cut:] - m["mean"]) @ m["coef"])
    u, a = np.mean(calls > 0), np.mean(r[cut:] > 0)
    assert h["no_skill"] == pytest.approx(u * a + (1 - u) * (1 - a), abs=1e-4)


def test_the_fit_never_sees_the_future_or_the_chart():
    stamps, close = _series(seed=3)
    a = cycles.compute(stamps, close, bar_stamps=stamps[-252:], kind="daily", last_close=close[-1])
    b = cycles.compute(stamps, close, bar_stamps=stamps[-60:], kind="daily", last_close=close[-1],
                       params=AstroParams(daily_horizon=20))
    c = cycles.compute(stamps, close, bar_stamps=stamps[-30:], kind="weekly", last_close=close[-1])
    assert a["model"]["cycles"] == b["model"]["cycles"] == c["model"]["cycles"]
    assert a["model"]["holdout"] == b["model"]["holdout"]


def test_roll_bars_are_left_out_of_the_fit():
    stamps, close = _series(seed=5)
    a = cycles.compute(stamps, close, bar_stamps=stamps[-50:], kind="daily", last_close=close[-1])
    b = cycles.compute(stamps, close, bar_stamps=stamps[-50:], kind="daily", last_close=close[-1],
                       skip=frozenset({100, 200, 300}))
    assert b["model"]["days"] == a["model"]["days"] - 3


def test_too_little_history_is_a_reason_not_a_fit():
    stamps, close = _series(start=date(2026, 6, 1))
    out = cycles.compute(stamps, close, bar_stamps=stamps[-20:], kind="daily", last_close=close[-1])
    assert out["available"] is False and "at least 250" in out["reason"]


# ----------------------------------------------------------------------
# Times and slots
# ----------------------------------------------------------------------


def test_daily_bars_are_read_at_the_cash_close_through_daylight_saving():
    summer, winter = eval_times(["2026-07-15T05:00:00", "2026-01-15T05:00:00"], "daily")
    assert datetime.fromtimestamp(summer, tz=timezone.utc).hour == 20     # 16:00 EDT
    assert datetime.fromtimestamp(winter, tz=timezone.utc).hour == 21     # 16:00 EST


def test_weekly_and_monthly_bars_are_read_at_their_last_session():
    sunday, monday = eval_times(["2026-11-22T00:00:00", "2026-11-23T00:00:00"], "weekly")
    assert sunday == monday == close_time(date(2026, 11, 27))             # that week's Friday
    (may,) = eval_times(["2026-05-01T00:00:00"], "monthly")
    assert may == close_time(date(2026, 5, 29))                           # the 31st is a Sunday


def test_intraday_bars_are_read_at_their_close():
    (t,) = eval_times(np.array(["2026-11-25T14:30:00"], dtype="datetime64[s]"), "minute", 5)
    assert t == datetime(2026, 11, 25, 14, 35, tzinfo=timezone.utc).timestamp()


def test_future_dates_are_weekdays_weeks_or_months_and_none_intraday():
    days = future_stamps(date(2026, 11, 27), "daily", 6)                  # a Friday
    assert days[0] == date(2026, 11, 30) and all(d.weekday() < 5 for d in days)
    assert future_stamps(date(2026, 11, 23), "weekly", 2) == [date(2026, 11, 30), date(2026, 12, 7)]
    assert future_stamps(date(2027, 1, 31), "monthly", 2) == [date(2027, 2, 28), date(2027, 3, 31)]
    assert future_stamps(date(2026, 11, 27), "minute", 5) == []


# ----------------------------------------------------------------------
# Turns and the projection
# ----------------------------------------------------------------------


def test_turning_points_confirm_each_extreme_by_the_threshold():
    path = np.array([0, 1, 2, 1.5, 0.4, 0.8, 1.9, 1.85, 1.95, 1.0])
    assert turning_points(path, threshold=0.5) == [(2, "high"), (4, "low"), (8, "high")]


def test_the_anchor_is_never_a_turn_and_an_unconfirmed_end_is_left_out():
    # Falls from the anchor, bottoms at 3, then rises to the end unconfirmed.
    assert turning_points(np.array([5, 4, 3, 2, 3, 4, 6]), threshold=1) == [(3, "low")]
    # Wiggles smaller than the threshold are not turns.
    assert turning_points(np.array([1, 1.1, 1.0, 1.1, 1.0]), threshold=0.5) == []
    # A flat path, whose range and so threshold are zero, has none at all.
    assert turning_points(np.full(10, 100.0), threshold=0.0) == []


def test_projection_starts_at_the_last_close_and_turns_where_d1_changes_sign():
    stamps, close = _series(planted_bp=30, noise=0.005, seed=6)
    out = cycles.compute(stamps, close, bar_stamps=stamps[-252:], kind="daily", last_close=close[-1])
    fut = out["future"]
    assert len(fut["time"]) == len(fut["path"]) == len(fut["lsc"]) == 126
    # The path is the last close moved by the change in LSC.
    lsc = np.array(fut["lsc"]) * out["scale"]["lsc"]
    anchor = out["series"]["lsc"][-1] * out["scale"]["lsc"]
    np.testing.assert_allclose(fut["path"], close[-1] * np.exp(lsc - anchor), rtol=1e-4)
    # Highs where the 1st derivative crosses down, lows where it crosses up,
    # give or take the bar either side.
    d1 = np.r_[out["series"]["d1"][-1], fut["d1"]]
    assert out["turns"]
    for t in out["turns"]:
        k = t["i"] + 1
        window = d1[max(k - 1, 0): k + 2]
        assert (window.max() >= 0 >= window.min()), t
        assert (d1[k - 1] > d1[min(k + 1, len(d1) - 1)]) == (t["type"] == "high")


def test_lines_are_scaled_to_one_and_the_second_derivative_has_no_weekend_steps():
    stamps, close = _series(planted_bp=30, noise=0.005, seed=8)
    out = cycles.compute(stamps, close, bar_stamps=stamps[-500:], kind="daily", last_close=close[-1])
    for key in ("lsc", "d1", "d2"):
        assert max(abs(v) for v in out["series"][key] + out["future"][key]) <= 1.0001
    # Differencing sessions would triple every Monday's step; read in
    # continuous time, Monday's d2 is no different from any other day's.
    d2 = np.array(out["series"]["d2"])
    jumps = np.abs(np.diff(d2))
    monday = np.array([date.fromisoformat(s[:10]).weekday() == 0 for s in stamps[-499:]])
    assert jumps[~monday].mean() > 0
    assert jumps[monday].mean() < 4 * jumps[~monday].mean()    # a weekend spans 3 days, not 9


def test_intraday_gets_lines_and_no_projection():
    stamps, close = _series(seed=9)
    bars = np.arange(np.datetime64("2026-11-25T14:30:00"), np.datetime64("2026-11-25T21:00:00"),
                     np.timedelta64(5, "m"))
    out = cycles.compute(stamps, close, bar_stamps=bars, kind="minute", minutes=5, last_close=close[-1])
    assert out["available"] and len(out["series"]["lsc"]) == len(bars)
    assert out["future"]["time"] == [] and out["future"]["path"] == [] and out["turns"] == []
    assert any("intraday" in n for n in out["notes"])


def test_each_event_sits_at_the_first_slot_closing_at_or_after_it():
    stamps, close = _series(seed=10)
    out = cycles.compute(stamps, close, bar_stamps=stamps[-120:], kind="daily", last_close=close[-1])
    slots = np.r_[eval_times(stamps[-120:], "daily"),
                  [close_time(date.fromisoformat(t[:10])) for t in out["future"]["time"]]]
    assert out["events"]
    for e in out["events"]:
        ts = datetime.fromisoformat(e["time"].replace("Z", "+00:00")).timestamp()
        assert slots[e["slot"]] >= ts
        assert e["slot"] == 0 or slots[e["slot"] - 1] < ts


def test_rules_and_notes_say_what_the_model_is_and_how_it_did():
    stamps, close = _series(seed=11)
    out = cycles.compute(stamps, close, bar_stamps=stamps[-252:], kind="daily", last_close=close[-1])
    keys = {r["key"] for r in RULES}
    assert {"astro_model", "astro_derivatives", "astro_projection", "astro_holdout", "astro_events"} <= keys
    assert "not the reference chart's PPM2" in RULES[0]["text"]
    note = out["notes"][0]
    assert "held-out" in note and "no skill" in note and "holidays are not modelled" in note


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    set_provider(CachedProvider(MockProvider(), ChainStore(tmp_path / "a.db")))
    with TestClient(app) as c:
        yield c


def test_technicals_leaves_the_payload_alone_unless_asked(client):
    d = client.get("/api/technicals?symbol=SPY&range=1Y&interval=1D").json()
    assert "astro" not in d
    assert all(not r["key"].startswith("astro") for r in d["rules"])


@pytest.mark.parametrize("symbol, interval, rng, future", [
    ("SPY", "1D", "1Y", 126), ("ES", "1W", "5Y", 52), ("QQQ", "5m", "5D", 0),
])
def test_technicals_adds_the_block_indexed_like_the_bars(client, symbol, interval, rng, future):
    d = client.get(f"/api/technicals?symbol={symbol}&range={rng}&interval={interval}&astro=true").json()
    a = d["astro"]
    assert a["available"] is True
    n = len(d["time"])
    for key in ("lsc", "d1", "d2", "illum", "elong", "dist", "dec"):
        assert len(a["series"][key]) == n
        assert len(a["future"][key]) == future
    assert len(a["future"]["time"]) == future
    assert any(r["key"] == "astro_model" for r in d["rules"])
    assert any(n.startswith("Lunar-solar fit") for n in d["notes"])
    if interval == "1W":
        t = [date.fromisoformat(s[:10]) for s in a["future"]["time"]]
        assert all((b - a_).days == 7 for a_, b in zip(t, t[1:]))


def test_history_adds_the_block_for_the_terminal(client):
    d = client.get("/api/history?symbol=SPX&range=1Y&interval=1D&astro=true").json()
    a = d["astro"]
    assert a["available"] and len(a["series"]["lsc"]) == len(d["time"])
    assert a["future"]["path"] and a["kind"] == "daily"
    intraday = client.get("/api/history?symbol=SPX&range=1D&interval=1m&astro=true").json()["astro"]
    assert intraday["kind"] == "minute" and intraday["future"]["time"] == []
    assert "astro" not in client.get("/api/history?symbol=SPX&range=1Y&interval=1D").json()
