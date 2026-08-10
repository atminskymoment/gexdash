"""Bound the European-approximation error for the American instruments."""

from __future__ import annotations

import numpy as np
import pytest

from gexdash.american import binomial_american, early_exercise_premium, max_greek_error
from gexdash.greeks import CALL, PUT, black_scholes

R = 0.042
Q = 0.012


def _euro(s, k, t, sigma, right, r=R, q=Q):
    return black_scholes(
        np.array([s]), np.array([k]), np.array([t]), np.array([sigma]), np.array([right]), r=r, q=q
    )


# ----------------------------------------------------------------------
# The binomial itself must be right before it can bound anything
# ----------------------------------------------------------------------


def test_binomial_converges_to_black_scholes_for_a_non_dividend_call():
    """With q = 0 an American call is never exercised early, so it must equal
    the European price. This is the standard correctness check on the tree."""
    got = binomial_american(100.0, 100.0, 0.5, 0.25, CALL, r=0.04, q=0.0, steps=800)
    want = _euro(100.0, 100.0, 0.5, 0.25, CALL, r=0.04, q=0.0)
    assert got["price"] == pytest.approx(float(want.price[0]), rel=2e-3)
    assert got["delta"] == pytest.approx(float(want.delta[0]), rel=5e-3)
    assert got["gamma"] == pytest.approx(float(want.gamma[0]), rel=2e-2)


def test_american_is_never_worth_less_than_european():
    """Early exercise is an extra right; it cannot have negative value."""
    for k in (600.0, 660.0, 670.0, 700.0, 760.0):
        for right in (CALL, PUT):
            prem = early_exercise_premium(670.0, k, 0.25, 0.18, right, R, Q, steps=300)
            assert prem > -0.02, f"negative premium {prem:.4f} at K={k} right={right}"


def test_american_is_never_worth_less_than_intrinsic():
    deep_itm_put = binomial_american(500.0, 700.0, 0.5, 0.2, PUT, R, Q, steps=300)
    assert deep_itm_put["price"] >= 200.0 - 1e-6


def test_deep_itm_puts_carry_the_largest_premium():
    """Documents *where* the approximation is weakest, not just that it is."""
    spot, t, sigma = 670.0, 0.25, 0.18
    deep_itm = early_exercise_premium(spot, 800.0, t, sigma, PUT, R, Q, steps=300)
    atm = early_exercise_premium(spot, 670.0, t, sigma, PUT, R, Q, steps=300)
    otm = early_exercise_premium(spot, 560.0, t, sigma, PUT, R, Q, steps=300)
    assert deep_itm > atm > otm
    assert otm < 0.02


# ----------------------------------------------------------------------
# The bound quoted in the README
# ----------------------------------------------------------------------


def test_greek_error_within_five_percent_of_spot():
    """The headline bound, for the band holding most SPY/QQQ open interest.

    Measured, not guessed. These are the numbers the README and the UI
    provenance panel quote; if this fails, both need updating -- which is the
    point of pinning it.
    """
    spot = 670.0
    strikes = np.arange(636.0, 704.0, 4.0)
    worst = max_greek_error(spot, strikes, t=30 / 365, sigma=0.17, r=R, q=Q, steps=300)

    print(
        f"\n  SPY-like 30 DTE +/-5%: max delta err {worst['delta']:.4f} at {worst['delta_at']}, "
        f"gamma {worst['gamma']:.6f} at {worst['gamma_at']}"
    )
    assert worst["delta"] < 0.02
    assert worst["gamma"] < 0.001


def test_worst_case_is_always_an_itm_put():
    """The structural claim behind the approximation being acceptable.

    If the worst error ever showed up in a call or an OTM put, the reasoning
    in american.py's docstring -- and the decision to accept the European
    greeks -- would need revisiting.
    """
    spot = 670.0
    for dte in (7, 30, 60):
        worst = max_greek_error(
            spot, np.arange(600.0, 745.0, 5.0), t=dte / 365, sigma=0.17, r=R, q=Q, steps=300
        )
        strike, right = worst["delta_at"]
        assert right == "P", f"{dte} DTE worst delta error was a {right}, not a put"
        assert strike > spot, f"{dte} DTE worst error at K={strike} is not ITM"


def test_error_grows_with_maturity_and_moneyness():
    """Both directions matter: widening the expiry window or the strike
    display range makes the approximation worse, and by how much is worth
    having pinned."""
    spot = 670.0
    near = np.arange(636.0, 704.0, 4.0)   # +/-5%
    wide = np.arange(600.0, 745.0, 5.0)   # +/-10%

    short_near = max_greek_error(spot, near, t=7 / 365, sigma=0.17, r=R, q=Q, steps=300)
    long_near = max_greek_error(spot, near, t=60 / 365, sigma=0.17, r=R, q=Q, steps=300)
    long_wide = max_greek_error(spot, wide, t=60 / 365, sigma=0.17, r=R, q=Q, steps=300)

    assert long_near["delta"] > short_near["delta"], "should worsen with maturity"
    assert long_wide["delta"] > long_near["delta"], "should worsen with moneyness"
    # The documented worst case anywhere we plot: +/-10%, 60 DTE.
    assert long_wide["delta"] < 0.06
    print(f"\n  worst plotted case (+/-10%, 60 DTE): delta err {long_wide['delta']:.4f}")


# ----------------------------------------------------------------------
# Futures options (Black-76, q = r) — structurally different from equities
# ----------------------------------------------------------------------

FUT_R = 0.042


def test_futures_premium_is_not_put_concentrated():
    """The structural claim behind having a separate futures note.

    On a dividend-paying equity (q < r) early-exercise value concentrates in
    ITM puts. On futures options the drift term is q = r, so ITM calls and
    ITM puts carry comparable premium. Emitting the equity wording for ES/NQ
    would state something measurably false.
    """
    spot, t, sig = 7790.0, 30 / 365, 0.12
    itm_call = early_exercise_premium(spot, 7400.0, t, sig, CALL, FUT_R, FUT_R, steps=250)
    itm_put = early_exercise_premium(spot, 8180.0, t, sig, PUT, FUT_R, FUT_R, steps=250)

    assert itm_call > 0.1 and itm_put > 0.1, "both sides must carry real premium"
    ratio = itm_call / itm_put
    assert 0.5 < ratio < 2.0, f"expected near-symmetric premium, got call/put = {ratio:.2f}"


@pytest.mark.parametrize("name,spot,sig,step", [("ES-like", 7790.0, 0.12, 40), ("NQ-like", 29850.0, 0.16, 150)])
def test_futures_greek_error_within_five_percent(name, spot, sig, step):
    """The bound quoted in american._FUTURES_NOTE and the docs."""
    strikes = np.arange(spot * 0.95, spot * 1.05, step)
    worst = max_greek_error(spot, strikes, t=30 / 365, sigma=sig, r=FUT_R, q=FUT_R, steps=250)
    print(f"\n  {name} 30d +/-5%: delta {worst['delta']:.4f} at {worst['delta_at']}")
    assert worst["delta"] < 0.004


def test_futures_error_is_smaller_than_the_equity_case():
    """Documents why the two notes quote different numbers."""
    equity = max_greek_error(670.0, np.arange(636.0, 704.0, 4.0), t=30 / 365, sigma=0.17, r=R, q=Q, steps=250)
    futures = max_greek_error(7790.0, np.arange(7400.0, 8180.0, 40.0), t=30 / 365, sigma=0.12, r=FUT_R, q=FUT_R, steps=250)
    assert futures["delta"] < equity["delta"]


def test_approximation_note_is_per_model():
    from gexdash import instruments
    from gexdash.american import approximation_note

    assert approximation_note(instruments.get("SPX")) is None
    assert approximation_note(instruments.get("NDX")) is None

    equity = approximation_note(instruments.get("SPY"))
    futures = approximation_note(instruments.get("NQ"))
    assert "ITM puts" in equity and "q = r" not in equity
    assert "near-symmetric" in futures and "q = r" in futures
    assert equity != futures
