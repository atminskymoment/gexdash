"""RSI, MACD and ADX/DI.

Validated against properties and an independently written reference, not by
eye. A wrong indicator tracks the right one closely enough to look fine and
diverges exactly when it matters.
"""

from __future__ import annotations

import numpy as np
import pytest

from gexdash.indicators import adx, compute_all, ema, macd, rsi, wilder


def _trend(n=120, slope=0.4, noise=0.0, seed=0):
    rng = np.random.default_rng(seed)
    close = 100 + slope * np.arange(n) + rng.normal(0, noise, n)
    high = close + 0.5 + abs(rng.normal(0, 0.2, n))
    low = close - 0.5 - abs(rng.normal(0, 0.2, n))
    return high, low, close


# ----------------------------------------------------------------------
# Smoothing — the distinction everything else rests on
# ----------------------------------------------------------------------


def test_wilder_is_not_an_ema_of_the_same_length():
    """Wilder uses alpha = 1/n; a conventional EMA uses 2/(n+1).

    Substituting one for the other is the classic way RSI and ADX come out
    subtly and persistently wrong.
    """
    x = np.linspace(1, 100, 100)
    w, e = wilder(x, 14), ema(x, 14)
    assert not np.allclose(w[-1], e[-1])
    # Wilder(n) responds like an EMA of roughly 2n-1.
    assert np.isclose(w[-1], ema(x, 27)[-1], rtol=0.02)


def test_wilder_seeds_with_the_first_n_mean():
    x = np.arange(1.0, 21.0)
    w = wilder(x, 5)
    assert np.isnan(w[3])
    assert w[4] == pytest.approx(np.mean(x[:5]))
    assert w[5] == pytest.approx(w[4] + (x[5] - w[4]) / 5)


def test_wilder_matches_an_independent_recurrence():
    rng = np.random.default_rng(1)
    x = rng.normal(10, 2, 60)
    got = wilder(x, 14)

    want = np.full(60, np.nan)
    acc = x[:14].mean()
    want[13] = acc
    for i in range(14, 60):
        acc = acc + (x[i] - acc) / 14
        want[i] = acc
    np.testing.assert_allclose(got, want, equal_nan=True)


def test_short_input_returns_all_nan_not_an_exception():
    assert np.all(np.isnan(wilder(np.array([1.0, 2.0]), 14)))
    assert np.all(np.isnan(rsi(np.array([1.0, 2.0]), 14)))


# ----------------------------------------------------------------------
# RSI
# ----------------------------------------------------------------------


def test_rsi_is_bounded_and_aligned():
    _, _, close = _trend(200, slope=0.1, noise=1.5, seed=3)
    r = rsi(close, 14)
    assert len(r) == len(close), "must stay aligned with the bars"
    finite = r[np.isfinite(r)]
    assert finite.min() >= 0 and finite.max() <= 100


def test_rsi_saturates_on_a_one_way_market():
    up = np.arange(100, 200, dtype=float)
    assert rsi(up, 14)[-1] == pytest.approx(100.0)
    assert rsi(up[::-1].copy(), 14)[-1] == pytest.approx(0.0)


def test_rsi_of_a_flat_series_is_not_nan():
    """No gains and no losses. RS is 0/0; the convention is 100, not nan."""
    r = rsi(np.full(60, 50.0), 14)
    assert np.isfinite(r[-1])


def test_rsi_matches_an_independent_wilder_implementation():
    rng = np.random.default_rng(7)
    close = 100 + np.cumsum(rng.normal(0, 1, 200))
    got = rsi(close, 14)

    d = np.diff(close)
    g, l = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    ag, al = g[:14].mean(), l[:14].mean()
    want = np.full(len(close), np.nan)
    want[14] = 100 - 100 / (1 + ag / al) if al else 100.0
    for i in range(14, len(d)):
        ag = ag + (g[i] - ag) / 14
        al = al + (l[i] - al) / 14
        want[i + 1] = 100 - 100 / (1 + ag / al) if al else 100.0
    np.testing.assert_allclose(got[15:], want[15:], rtol=1e-9)


# ----------------------------------------------------------------------
# MACD
# ----------------------------------------------------------------------


def test_macd_is_the_ema_difference_and_histogram_is_consistent():
    _, _, close = _trend(150, noise=1.0, seed=5)
    m = macd(close, 12, 26, 9)
    np.testing.assert_allclose(m["macd"], ema(close, 12) - ema(close, 26), rtol=1e-12)
    np.testing.assert_allclose(m["histogram"], m["macd"] - m["signal"], rtol=1e-12, equal_nan=True)


def test_macd_of_a_constant_series_is_zero():
    m = macd(np.full(120, 42.0))
    assert m["macd"][-1] == pytest.approx(0.0)
    assert m["histogram"][-1] == pytest.approx(0.0)


def test_macd_is_positive_in_an_uptrend():
    _, _, close = _trend(150, slope=0.5)
    assert macd(close)["macd"][-1] > 0


# ----------------------------------------------------------------------
# ADX / DI
# ----------------------------------------------------------------------


def test_di_plus_leads_in_an_uptrend_and_di_minus_in_a_downtrend():
    high, low, close = _trend(150, slope=0.5, noise=0.3, seed=11)
    up = adx(high, low, close, 14)
    assert up["di_plus"][-1] > up["di_minus"][-1]

    down = adx(high[::-1].copy(), low[::-1].copy(), close[::-1].copy(), 14)
    assert down["di_minus"][-1] > down["di_plus"][-1]


def test_adx_is_higher_in_a_trend_than_in_chop():
    trend = adx(*_trend(200, slope=0.6, noise=0.2, seed=2), 14)["adx"]

    rng = np.random.default_rng(4)
    close = 100 + rng.normal(0, 1, 200)          # no drift
    high, low = close + 0.5, close - 0.5
    chop = adx(high, low, close, 14)["adx"]

    assert np.nanmean(trend[-40:]) > np.nanmean(chop[-40:])


def test_adx_and_di_are_bounded():
    high, low, close = _trend(200, slope=0.3, noise=1.2, seed=9)
    a = adx(high, low, close, 14)
    for key in ("adx", "di_plus", "di_minus"):
        finite = a[key][np.isfinite(a[key])]
        assert finite.min() >= 0 and finite.max() <= 100, key


def test_directional_movement_is_exclusive():
    """Only the larger move counts, and an inside bar counts for neither.

    Awarding both inflates ADX, and the result still looks like an ADX.
    """
    # Inside bars throughout: no directional movement at all, so DI collapses.
    n = 80
    high = np.full(n, 101.0); low = np.full(n, 99.0); close = np.full(n, 100.0)
    high[0], low[0] = 110.0, 90.0                # one wide bar, then all inside
    a = adx(high, low, close, 14)
    assert a["di_plus"][-1] == pytest.approx(0.0, abs=1e-9)
    assert a["di_minus"][-1] == pytest.approx(0.0, abs=1e-9)


def test_all_series_are_aligned_with_the_bars():
    high, low, close = _trend(120)
    out = compute_all(high, low, close)
    for key in ("rsi", "macd", "macd_signal", "macd_histogram", "adx", "di_plus", "di_minus"):
        assert len(out[key]) == len(close), f"{key} is not aligned"


def test_compute_all_is_json_safe():
    import json
    high, low, close = _trend(60)
    payload = compute_all(high, low, close)
    round_tripped = json.loads(json.dumps(payload))
    # nan must arrive as null so the client can break the line, not as NaN
    # which is invalid JSON and silently accepted by some parsers.
    assert any(v is None for v in round_tripped["adx"])
    assert "Wilder" in round_tripped["params"]["smoothing"]


# ----------------------------------------------------------------------
# RSI moving average
# ----------------------------------------------------------------------


def test_sma_is_nan_until_the_window_fills():
    from gexdash.indicators import sma
    x = np.arange(1.0, 11.0)
    out = sma(x, 5)
    assert np.all(np.isnan(out[:4]))
    assert out[4] == pytest.approx(np.mean(x[:5]))
    assert out[-1] == pytest.approx(np.mean(x[-5:]))


def test_sma_does_not_propagate_the_indicator_warmup_nans():
    """RSI is undefined for its own warm-up.

    A plain rolling mean would turn every window touching those nans into
    nan for good; the MA should simply not exist yet and then start cleanly.
    """
    from gexdash.indicators import sma
    x = np.concatenate([np.full(14, np.nan), np.full(20, 50.0)])
    out = sma(x, 20)
    assert np.all(np.isnan(out[:33]))
    assert out[33] == pytest.approx(50.0)


def test_rsi_ma_is_a_20_period_mean_of_rsi():
    from gexdash.indicators import rsi as rsi_fn, sma
    rng = np.random.default_rng(21)
    close = 100 + np.cumsum(rng.normal(0, 1, 200))

    out = compute_all(close + 0.5, close - 0.5, close)
    want = sma(rsi_fn(close, 14), 20)

    got = [v for v in out["rsi_ma"]]
    assert len(got) == len(close)
    for i in (150, 199):
        assert got[i] == pytest.approx(want[i], abs=1e-6)
    assert out["params"]["rsi_ma_length"] == 20


def test_rsi_ma_is_smoother_than_rsi():
    """The point of the line: it should lag and steady the raw series."""
    rng = np.random.default_rng(5)
    close = 100 + np.cumsum(rng.normal(0, 1.2, 300))
    out = compute_all(close + 0.5, close - 0.5, close)

    raw = np.array([v for v in out["rsi"][60:]], dtype=float)
    ma = np.array([v if v is not None else np.nan for v in out["rsi_ma"][60:]], dtype=float)
    assert np.nanstd(np.diff(ma)) < np.nanstd(np.diff(raw))


def test_rsi_ma_stays_within_rsi_bounds():
    high, low, close = _trend(240, slope=0.2, noise=1.4, seed=13)
    vals = [v for v in compute_all(high, low, close)["rsi_ma"] if v is not None]
    assert min(vals) >= 0 and max(vals) <= 100
