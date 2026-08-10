"""Finite-difference validation of the analytic greeks.

Sign errors in vanna and charm are the dangerous failure mode for this
project: nothing downstream cross-checks them, and a flipped sign produces a
VANX/CEX profile that looks entirely plausible while pointing the wrong way.
So every analytic partial is pinned against a numerical derivative of the
*price* function here.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from gexdash.greeks import (
    CALL,
    PUT,
    black76_params,
    black_scholes,
    implied_vol,
    norm_cdf,
)

# A deliberately awkward grid: deep ITM/OTM, short and long dated, low and
# high vol, nonzero rate and dividend. Degenerate corners are covered
# separately in test_clamps_degenerate_inputs.
S = np.array([100.0, 100.0, 100.0, 100.0, 7774.32, 7774.32, 50.0, 50.0])
K = np.array([80.0, 100.0, 120.0, 100.0, 7780.0, 7600.0, 55.0, 45.0])
T = np.array([0.5, 0.5, 0.5, 0.02, 0.0192, 0.25, 1.5, 1.5])
IV = np.array([0.35, 0.20, 0.28, 0.65, 0.13, 0.22, 0.45, 0.45])
R = 0.042
Q = 0.013


def _price(s, k, t, sigma, right, r=R, q=Q):
    return black_scholes(s, k, t, sigma, right, r=r, q=q).price


def _delta(s, k, t, sigma, right, r=R, q=Q):
    return black_scholes(s, k, t, sigma, right, r=r, q=q).delta


@pytest.mark.parametrize("right", [CALL, PUT])
def test_delta_matches_finite_difference(right):
    w = np.full(S.shape, right)
    h = 1e-4 * S
    num = (_price(S + h, K, T, IV, w) - _price(S - h, K, T, IV, w)) / (2 * h)
    ana = black_scholes(S, K, T, IV, w, r=R, q=Q).delta
    np.testing.assert_allclose(ana, num, rtol=1e-5, atol=1e-7)


@pytest.mark.parametrize("right", [CALL, PUT])
def test_gamma_matches_ddelta_ds(right):
    """gamma as the first derivative of delta.

    Deliberately *not* a second difference of price: with h large enough to
    avoid catastrophic cancellation, truncation error swamps the check on
    short-dated near-ATM strikes (a 7-day 7780 call needs h ~ 8 index points
    at h = 1e-3*S, over which gamma is far from constant). delta is itself
    pinned to dV/dS above, so this composes into a valid chain.
    """
    w = np.full(S.shape, right)
    h = 1e-5 * S  # central-difference truncation goes as h^2
    num = (_delta(S + h, K, T, IV, w) - _delta(S - h, K, T, IV, w)) / (2 * h)
    ana = black_scholes(S, K, T, IV, w, r=R, q=Q).gamma
    np.testing.assert_allclose(ana, num, rtol=1e-7, atol=1e-10)


@pytest.mark.parametrize("right", [CALL, PUT])
def test_gamma_matches_price_second_difference_loosely(right):
    """Independent confirmation straight off the price surface.

    Looser tolerance by necessity -- see the note above on conditioning.
    """
    w = np.full(S.shape, right)
    h = 1e-4 * S
    num = (_price(S + h, K, T, IV, w) - 2 * _price(S, K, T, IV, w) + _price(S - h, K, T, IV, w)) / (h * h)
    ana = black_scholes(S, K, T, IV, w, r=R, q=Q).gamma
    np.testing.assert_allclose(ana, num, rtol=1e-3, atol=1e-8)


@pytest.mark.parametrize("right", [CALL, PUT])
def test_vega_matches_finite_difference(right):
    w = np.full(S.shape, right)
    h = 1e-5
    num = (_price(S, K, T, IV + h, w) - _price(S, K, T, IV - h, w)) / (2 * h)
    ana = black_scholes(S, K, T, IV, w, r=R, q=Q).vega
    np.testing.assert_allclose(ana, num, rtol=1e-5, atol=1e-6)


@pytest.mark.parametrize("right", [CALL, PUT])
def test_theta_is_dv_dt_forward(right):
    # theta = dV/dt with t running forward, i.e. -dV/dT.
    w = np.full(S.shape, right)
    h = 1e-6
    num = -(_price(S, K, T + h, IV, w) - _price(S, K, T - h, IV, w)) / (2 * h)
    ana = black_scholes(S, K, T, IV, w, r=R, q=Q).theta
    np.testing.assert_allclose(ana, num, rtol=1e-4, atol=1e-5)


@pytest.mark.parametrize("right", [CALL, PUT])
def test_vanna_is_ddelta_dsigma(right):
    w = np.full(S.shape, right)
    h = 1e-6
    num = (_delta(S, K, T, IV + h, w) - _delta(S, K, T, IV - h, w)) / (2 * h)
    ana = black_scholes(S, K, T, IV, w, r=R, q=Q).vanna
    np.testing.assert_allclose(ana, num, rtol=1e-4, atol=1e-6)


@pytest.mark.parametrize("right", [CALL, PUT])
def test_charm_is_ddelta_dt_forward(right):
    # charm = d(delta)/dt with t running forward, i.e. -d(delta)/dT.
    w = np.full(S.shape, right)
    h = 1e-6
    num = -(_delta(S, K, T + h, IV, w) - _delta(S, K, T - h, IV, w)) / (2 * h)
    ana = black_scholes(S, K, T, IV, w, r=R, q=Q).charm
    np.testing.assert_allclose(ana, num, rtol=1e-4, atol=1e-5)


def test_vanna_and_gamma_are_right_independent():
    call = black_scholes(S, K, T, IV, np.full(S.shape, CALL), r=R, q=Q)
    put = black_scholes(S, K, T, IV, np.full(S.shape, PUT), r=R, q=Q)
    np.testing.assert_allclose(call.gamma, put.gamma, rtol=1e-12)
    np.testing.assert_allclose(call.vanna, put.vanna, rtol=1e-12)
    np.testing.assert_allclose(call.vega, put.vega, rtol=1e-12)


def test_put_call_parity():
    call = _price(S, K, T, IV, np.full(S.shape, CALL))
    put = _price(S, K, T, IV, np.full(S.shape, PUT))
    lhs = call - put
    rhs = S * np.exp(-Q * T) - K * np.exp(-R * T)
    np.testing.assert_allclose(lhs, rhs, rtol=1e-10, atol=1e-9)


def test_delta_bounds():
    disc_q = np.exp(-Q * T)
    call_d = _delta(S, K, T, IV, np.full(S.shape, CALL))
    put_d = _delta(S, K, T, IV, np.full(S.shape, PUT))
    assert np.all(call_d > 0) and np.all(call_d < disc_q + 1e-12)
    assert np.all(put_d < 0) and np.all(put_d > -disc_q - 1e-12)
    # put delta = call delta - e^{-qT}
    np.testing.assert_allclose(call_d - put_d, disc_q, rtol=1e-12)


def test_atm_call_charm_is_negative():
    # With r = q = 0 an ATM call has delta > 0.5 that decays toward 0.5,
    # so charm (d delta / dt forward) must be negative.
    s = np.array([100.0])
    g = black_scholes(s, s, np.array([0.5]), np.array([0.3]), np.array([CALL]), r=0.0, q=0.0)
    assert g.delta[0] > 0.5
    assert g.charm[0] < 0


def test_black76_equals_black_scholes_with_q_equals_r():
    """Black-76 identity: the futures path is not a separate implementation."""
    f = np.array([7774.32, 5000.0, 120.0])
    k = np.array([7780.0, 4900.0, 130.0])
    t = np.array([0.0192, 0.35, 1.0])
    iv = np.array([0.13, 0.19, 0.44])
    w = np.array([CALL, PUT, CALL])
    r = 0.042

    got = black_scholes(f, k, t, iv, w, **black76_params(r))

    # Reference Black-76, written out independently.
    sqrt_t = np.sqrt(t)
    d1 = (np.log(f / k) + 0.5 * iv * iv * t) / (iv * sqrt_t)
    d2 = d1 - iv * sqrt_t
    disc = np.exp(-r * t)
    expect_price = w * disc * (f * norm_cdf(w * d1) - k * norm_cdf(w * d2))
    expect_delta = disc * (norm_cdf(d1) - np.where(w > 0, 0.0, 1.0))
    expect_gamma = disc * np.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi) / (f * iv * sqrt_t)

    np.testing.assert_allclose(got.price, expect_price, rtol=1e-12)
    np.testing.assert_allclose(got.delta, expect_delta, rtol=1e-12)
    np.testing.assert_allclose(got.gamma, expect_gamma, rtol=1e-12)


def test_implied_vol_round_trips():
    w = np.array([CALL, PUT, CALL, PUT])
    s = np.array([7774.32, 7774.32, 100.0, 100.0])
    k = np.array([7800.0, 7700.0, 105.0, 95.0])
    t = np.array([0.02, 0.02, 0.5, 0.5])
    iv = np.array([0.14, 0.16, 0.30, 0.28])

    px = black_scholes(s, k, t, iv, w, r=R, q=Q).price
    back = implied_vol(px, s, k, t, w, r=R, q=Q)
    np.testing.assert_allclose(back, iv, rtol=1e-4)


def test_clamps_degenerate_inputs():
    """Expired / zero-vol contracts must yield finite numbers, not nan."""
    s = np.array([100.0, 100.0, 100.0])
    k = np.array([100.0, 90.0, 110.0])
    t = np.array([0.0, -1.0, 0.5])       # expired and negative time
    iv = np.array([0.2, 0.2, 0.0])       # and zero vol
    w = np.array([CALL, PUT, CALL])

    g = black_scholes(s, k, t, iv, w, r=R, q=Q)
    for name, arr in g.as_dict().items():
        assert np.all(np.isfinite(arr)), f"{name} produced non-finite values: {arr}"


def test_recompute_is_fast_enough_for_a_spot_stream():
    """A full chain must re-price well inside a tick budget."""
    import time

    n = 2000
    rng = np.random.default_rng(0)
    s = np.full(n, 7774.32)
    k = rng.uniform(7000, 8500, n)
    t = rng.uniform(0.002, 1.0, n)
    iv = rng.uniform(0.08, 0.9, n)
    w = rng.choice([CALL, PUT], n).astype(float)

    black_scholes(s, k, t, iv, w, r=R, q=Q)  # warm up
    start = time.perf_counter()
    for _ in range(20):
        black_scholes(s, k, t, iv, w, r=R, q=Q)
    per_call = (time.perf_counter() - start) / 20

    assert per_call < 0.010, f"{per_call*1000:.2f} ms per {n}-contract recompute"
