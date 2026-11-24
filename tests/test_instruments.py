"""Instrument specs.

A wrong multiplier is the archetypal silent failure here: every number stays
smooth and plausible, just scaled wrong. So each one is pinned against the
published contract spec, and the scaling behaviour is verified rather than
assumed.
"""

from __future__ import annotations

import numpy as np
import pytest

from gexdash import exposure, instruments
from gexdash.exposure import ExposureConfig
from gexdash.providers.mock import DEFAULT_SPOT, MockProvider

ALL = ["SPX", "SPY", "ES", "NDX", "QQQ", "NQ", "USO"]


# ----------------------------------------------------------------------
# Contract specs
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "key,multiplier",
    [
        ("SPX", 100.0),   # Cboe S&P 500 index option
        ("SPY", 100.0),   # 100 shares
        ("ES", 50.0),     # CME E-mini S&P 500: $50 x index
        ("NDX", 100.0),   # Nasdaq-100 index option: $100
        ("QQQ", 100.0),   # 100 shares
        ("NQ", 20.0),     # CME E-mini Nasdaq-100: $20 x index
        ("USO", 100.0),   # 100 shares
    ],
)
def test_multipliers_match_contract_specs(key, multiplier):
    assert instruments.get(key).multiplier == multiplier


@pytest.mark.parametrize(
    "key,is_future",
    [("SPX", False), ("SPY", False), ("ES", True), ("NDX", False), ("QQQ", False), ("NQ", True),
     ("USO", False)],
)
def test_pricing_model_follows_the_instrument_type(key, is_future):
    inst = instruments.get(key)
    assert inst.is_future is is_future
    assert inst.model == ("future" if is_future else "index")


@pytest.mark.parametrize(
    "key,european",
    [
        # Cash-settled index options: European, verified against Nasdaq/Cboe.
        ("SPX", True), ("NDX", True),
        # CME options on E-mini futures: the quarterly and serial contracts
        # are American-style (weeklies and EOM are European).
        ("ES", False), ("NQ", False),
        # American, physically settled on a dividend-paying ETF.
        ("SPY", False), ("QQQ", False), ("USO", False),
    ],
)
def test_exercise_style(key, european):
    assert instruments.get(key).european is european


def test_ndx_default_stays_european():
    """Spec-conformance anchor.

    NDX is European cash-settled per Nasdaq and Cboe, which market "no early
    assignment risk" as a feature. It is easy to "fix" this to American for
    consistency with the CME futures instruments -- they are a different
    product with a different rule. If this fails, check the spec before
    changing the test.
    """
    assert instruments.get("NDX").european is True
    assert instruments.get("SPX").european is True


def test_etfs_carry_their_own_dividend_yield():
    """Applying the S&P's yield to QQQ is simply wrong."""
    assert instruments.get("QQQ").default_dividend_yield < instruments.get("SPY").default_dividend_yield
    assert instruments.get("NDX").default_dividend_yield < instruments.get("SPX").default_dividend_yield


def test_registry_and_families_agree():
    assert set(instruments.REGISTRY) == set(ALL)
    listed = [k for fam in instruments.FAMILIES.values() for k in fam]
    assert sorted(listed) == sorted(ALL), "every instrument must appear in exactly one family"


def test_unknown_instrument_raises_with_a_useful_message():
    with pytest.raises(KeyError, match="unknown instrument"):
        instruments.get("TSLA")


# ----------------------------------------------------------------------
# Scaling
# ----------------------------------------------------------------------


def test_multiplier_scales_exposure_linearly_for_every_instrument():
    """Same synthetic book, exposure must scale exactly with the multiplier.

    q = r makes Black-Scholes and Black-76 coincide, isolating the multiplier
    from the model difference.
    """
    same_model = ExposureConfig(risk_free_rate=0.042, dividend_yield=0.042)
    spot = 1000.0
    # Deliberately asymmetric. With equal call and put OI at every strike the
    # dealer-sign convention makes net GEX cancel to exactly zero, and the
    # ratio under test becomes 0/0 -- correct behaviour, useless fixture.
    rows_for = lambda: [
        {"strike": k, "expiry": "2026-09-18T20:00:00", "right": r, "iv": 0.2,
         "open_interest": oi, "volume": 10}
        for k in (950.0, 1000.0, 1050.0)
        for r, oi in (("C", 1500), ("P", 900))
    ]

    from datetime import datetime, timezone
    from gexdash.chain import build_chain

    now = datetime(2026, 8, 20, 14, 0, tzinfo=timezone.utc)
    results = {}
    for key in ALL:
        inst = instruments.get(key)
        chain = build_chain(inst, spot, rows_for(), asof=now)
        results[key] = exposure.compute(chain, same_model).totals["gex"]

    # NQ is x20, ES x50, everything else x100.
    assert results["SPX"] / results["NQ"] == pytest.approx(100 / 20, rel=1e-12)
    assert results["SPX"] / results["ES"] == pytest.approx(100 / 50, rel=1e-12)
    for key in ("SPY", "NDX", "QQQ"):
        assert results[key] == pytest.approx(results["SPX"], rel=1e-12)


# ----------------------------------------------------------------------
# End to end
# ----------------------------------------------------------------------


@pytest.mark.parametrize("key", ALL)
def test_every_instrument_computes_end_to_end(key):
    inst = instruments.get(key)
    chain = MockProvider().get_chain(inst, max_dte=60)
    res = exposure.compute(chain, ExposureConfig(expiry_window="30dte"))

    assert res.diagnostics["contracts_in_window"] > 0
    assert np.isfinite(res.totals["gex"])
    assert res.levels["call_wall"].strike is not None
    assert res.levels["max_pain"].strike is not None
    # Strike grid must bracket spot.
    assert res.strikes.min() < chain.spot < res.strikes.max()


@pytest.mark.parametrize("key", ALL)
def test_exercise_style_is_surfaced_not_silently_ignored(key):
    """The `european` flag must reach the output, or it is decorative."""
    inst = instruments.get(key)
    chain = MockProvider().get_chain(inst, max_dte=30)
    diag = exposure.compute(chain, ExposureConfig(expiry_window="30dte")).diagnostics

    assert diag["exercise_style"] == inst.exercise_style
    if inst.european:
        assert diag["greeks_approximation"] is None
    else:
        assert "American" in diag["greeks_approximation"]


@pytest.mark.parametrize("key", ALL)
def test_instrument_dividend_default_is_used_when_unset(key):
    inst = instruments.get(key)
    chain = MockProvider().get_chain(inst, max_dte=30)

    auto = exposure.compute(chain, ExposureConfig()).diagnostics
    assert auto["dividend_yield"] == inst.default_dividend_yield
    assert auto["dividend_yield_source"] == "instrument default"

    override = exposure.compute(chain, ExposureConfig(dividend_yield=0.031)).diagnostics
    assert override["dividend_yield"] == 0.031
    assert override["dividend_yield_source"] == "override"


@pytest.mark.parametrize("key", ALL)
def test_display_width_and_strike_step_produce_a_sane_grid(key):
    """A ~$1 strike step on a $776 ETF and a 25-point step on a 29,800 index
    must both yield a readable number of strikes across a similar span."""
    inst = instruments.get(key)
    chain = MockProvider().get_chain(inst, max_dte=7)
    strikes = np.unique(chain.strike)

    assert len(strikes) == 2 * inst.display_strikes + 1
    span_pct = (strikes.max() - strikes.min()) / DEFAULT_SPOT[key] * 100
    assert 3.0 < span_pct < 30.0, f"{key} grid spans {span_pct:.1f}% of spot"


@pytest.mark.parametrize("key", ALL)
def test_mock_spot_walk_is_proportional_to_price(key):
    """An absolute walk would be 3% on SPY and invisible on NDX."""
    p = MockProvider()
    inst = instruments.get(key)
    spots = [p.get_spot(inst) for _ in range(400)]
    base = DEFAULT_SPOT[key]
    moves = [abs(s - base) / base for s in spots]
    assert max(moves) < 0.005, f"{key} walked {max(moves):.2%} from base"
    assert len(set(spots)) > 10, f"{key} spot barely moved"


# ----------------------------------------------------------------------
# Exercise-style overrides
# ----------------------------------------------------------------------


def _registry():
    return {inst.key: inst for inst in instruments.all_instruments()}


def test_override_flips_a_style():
    out = instruments.apply_exercise_overrides(_registry(), "NDX=american,NQ=european")
    assert out["NDX"].european is False
    assert out["NQ"].european is True
    # Untouched instruments keep their defaults.
    assert out["ES"].european is False
    assert out["SPX"].european is True


def test_override_updates_the_derived_note():
    """The UI warning is derived, so an override must carry it.

    A stored note would leave the header strip claiming European on an
    instrument the override just made American.
    """
    out = instruments.apply_exercise_overrides(_registry(), "NDX=american")
    assert "American exercise" in out["NDX"].note
    cleared = instruments.apply_exercise_overrides(_registry(), "NQ=european")
    assert cleared["NQ"].note == ""


def test_no_override_leaves_everything_alone():
    for raw in (None, "", "   "):
        out = instruments.apply_exercise_overrides(_registry(), raw)
        assert all(out[k].european is instruments.get(k).european for k in ALL)


@pytest.mark.parametrize(
    "raw,match",
    [
        ("NDX=bermudan", "unknown exercise style"),
        ("TSLA=american", "unknown instrument"),
        ("NDXamerican", "expected 'SYMBOL=style'"),
        ("NDX=american,QQQ=", "unknown exercise style"),
    ],
)
def test_malformed_overrides_raise(raw, match):
    """An override that silently does nothing is worse than a crash --
    you would go on believing it applied."""
    with pytest.raises(ValueError, match=match):
        instruments.apply_exercise_overrides(_registry(), raw)


def test_overrides_are_case_and_space_insensitive():
    out = instruments.apply_exercise_overrides(_registry(), " ndx = American , nq=EUROPEAN ")
    assert out["NDX"].european is False
    assert out["NQ"].european is True
