"""Exposure engine tests against hand-built chains with known answers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from gexdash import exposure, instruments
from gexdash.chain import build_chain
from gexdash.exposure import ExposureConfig

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)


def _rows(spec, expiry_days=7):
    """spec: list of (strike, right, oi, iv, volume)."""
    exp = NOW + timedelta(days=expiry_days)
    return [
        {
            "strike": k,
            "expiry": exp,
            "right": right,
            "iv": iv,
            "open_interest": oi,
            "volume": vol,
        }
        for k, right, oi, iv, vol in spec
    ]


def _chain(spec, spot=100.0, expiry_days=7, inst=None):
    return build_chain(
        inst or instruments.SPX,
        spot,
        _rows(spec, expiry_days),
        asof=NOW,
    )


# ----------------------------------------------------------------------
# Walls and concentration
# ----------------------------------------------------------------------


def test_call_and_put_walls_land_on_the_biggest_oi_strikes():
    chain = _chain(
        [
            (100, "C", 100, 0.20, 10),
            (105, "C", 9000, 0.20, 10),   # dominant call strike
            (110, "C", 200, 0.20, 10),
            (95, "P", 8000, 0.20, 10),    # dominant put strike
            (90, "P", 150, 0.20, 10),
        ]
    )
    res = exposure.compute(chain, ExposureConfig(expiry_window="30dte"))
    assert res.levels["call_wall"].strike == 105.0
    assert res.levels["put_wall"].strike == 95.0


def test_walls_are_immune_to_the_dealer_sign_convention():
    """Flipping the convention must not move which strike is the wall."""
    spec = [
        (100, "C", 100, 0.20, 1),
        (105, "C", 9000, 0.20, 1),
        (95, "P", 8000, 0.20, 1),
    ]
    a = exposure.compute(_chain(spec), ExposureConfig(dealer_sign="long_calls_short_puts"))
    b = exposure.compute(_chain(spec), ExposureConfig(dealer_sign="short_calls_long_puts"))
    assert a.levels["call_wall"].strike == b.levels["call_wall"].strike
    assert a.levels["put_wall"].strike == b.levels["put_wall"].strike
    # ...but the signed totals must flip.
    assert a.totals["gex"] == pytest.approx(-b.totals["gex"])


# ----------------------------------------------------------------------
# Max pain
# ----------------------------------------------------------------------


def test_max_pain_known_case():
    # 20 calls at 100, 10 puts at 110.
    #   settle 100 -> calls 0    + puts 10*10=100 -> 100  <- min
    #   settle 105 -> calls 5*20 + puts 5*10      -> 150
    #   settle 110 -> calls 10*20 + puts 0        -> 200
    chain = _chain(
        [
            (100, "C", 20, 0.2, 0),
            (105, "C", 0, 0.2, 0),
            (110, "P", 10, 0.2, 0),
        ]
    )
    strike, _ = exposure.max_pain(chain)
    assert strike == 100.0


def test_max_pain_is_per_expiry_and_reports_which():
    near = _rows([(100, "C", 50, 0.2, 0), (110, "P", 5, 0.2, 0)], expiry_days=2)
    far = _rows([(200, "C", 5, 0.2, 0), (300, "P", 5000, 0.2, 0)], expiry_days=40)
    chain = build_chain(instruments.SPX, 100.0, near + far, asof=NOW)

    strike, expiry = exposure.max_pain(chain)
    # Must use the near expiry, and must not be dragged by the far 300 puts.
    assert strike == 100.0
    assert expiry == chain.expiries()[0]


# ----------------------------------------------------------------------
# Gamma flip
# ----------------------------------------------------------------------


def test_spot_solve_flip_actually_zeroes_total_gex():
    """The defining property: GEX(flip) == 0."""
    chain = _chain(
        [
            (95, "P", 5000, 0.22, 0),
            (100, "P", 3000, 0.20, 0),
            (100, "C", 1000, 0.20, 0),
            (105, "C", 4000, 0.19, 0),
            (110, "C", 2000, 0.19, 0),
        ],
        spot=100.0,
    )
    cfg = ExposureConfig(expiry_window="30dte", flip_method="spot_solve")
    res = exposure.compute(chain, cfg)
    flip = res.levels["gamma_flip"].strike
    assert flip is not None

    total_at_flip = exposure.total_gex_at_spot(chain, cfg, flip)
    scale = abs(exposure.total_gex_at_spot(chain, cfg, chain.spot))
    assert abs(total_at_flip) < scale * 1e-3, f"GEX at flip {flip} was {total_at_flip}"


def test_two_flip_methods_are_both_available_and_labelled():
    spec = [
        (95, "P", 5000, 0.22, 0),
        (105, "C", 4000, 0.19, 0),
    ]
    a = exposure.compute(_chain(spec), ExposureConfig(flip_method="spot_solve"))
    b = exposure.compute(_chain(spec), ExposureConfig(flip_method="cumulative"))
    assert a.diagnostics["flip_method"] == "spot_solve"
    assert b.diagnostics["flip_method"] == "cumulative"


def test_flip_returns_none_when_gamma_never_crosses_zero():
    # All calls, dealers long calls -> GEX positive everywhere.
    chain = _chain([(100, "C", 1000, 0.2, 0), (105, "C", 1000, 0.2, 0)])
    res = exposure.compute(chain, ExposureConfig(flip_method="spot_solve"))
    assert res.levels["gamma_flip"].strike is None


# ----------------------------------------------------------------------
# Expiry window
# ----------------------------------------------------------------------


def test_expiry_window_changes_the_total():
    near = _rows([(100, "C", 1000, 0.2, 0)], expiry_days=3)
    far = _rows([(100, "C", 50000, 0.2, 0)], expiry_days=45)
    chain = build_chain(instruments.SPX, 100.0, near + far, asof=NOW)

    tight = exposure.compute(chain, ExposureConfig(expiry_window="7dte"))
    wide = exposure.compute(chain, ExposureConfig(expiry_window="60dte"))

    assert tight.diagnostics["contracts_in_window"] == 1
    assert wide.diagnostics["contracts_in_window"] == 2
    assert wide.totals["gex"] > tight.totals["gex"]
    # The window in force must be reported, not implied.
    assert tight.diagnostics["expiry_window"] == "7dte"
    assert tight.diagnostics["expiry_window_days"] == 7.0


def test_empty_window_raises_rather_than_returning_zeros():
    chain = _chain([(100, "C", 1000, 0.2, 0)], expiry_days=45)
    with pytest.raises(ValueError, match="no live contracts within expiry window"):
        exposure.compute(chain, ExposureConfig(expiry_window="0dte"))


# ----------------------------------------------------------------------
# Expired contracts
# ----------------------------------------------------------------------


def test_settled_contracts_are_excluded_not_clamped():
    """Regression: a settled strike must not dominate the aggregates.

    The analytic greeks are singular at T=0 (gamma ~ 1/sqrt(T),
    charm ~ 1/T). greeks.py clamps to one minute so nothing goes nan, but a
    clamped-not-dropped contract is finite *and enormous*: on a real chain a
    single settled strike contributed twice the entire CEX total.
    """
    live = _rows([(100, "C", 1000, 0.20, 5)], expiry_days=7)
    settled = _rows([(100, "C", 1000, 0.20, 5)], expiry_days=0)
    settled[0]["expiry"] = NOW  # exactly at valuation time
    chain = build_chain(instruments.SPX, 100.0, live + settled, asof=NOW)

    res = exposure.compute(chain, ExposureConfig(expiry_window="30dte"))
    assert res.diagnostics["expired_excluded"] == 1
    assert res.diagnostics["contracts_in_window"] == 1

    live_only = exposure.compute(_chain([(100, "C", 1000, 0.20, 5)]), ExposureConfig())
    assert res.totals["cex"] == pytest.approx(live_only.totals["cex"])
    assert res.totals["gex"] == pytest.approx(live_only.totals["gex"])


def test_zero_dte_expired_state_is_reported():
    """The reference dashboard's '0DTE EXPIRED' state."""
    live = _rows([(100, "C", 1000, 0.20, 5)], expiry_days=7)
    settled = _rows([(100, "P", 1000, 0.20, 5)], expiry_days=0)
    settled[0]["expiry"] = NOW
    chain = build_chain(instruments.SPX, 100.0, live + settled, asof=NOW)

    res = exposure.compute(chain, ExposureConfig(expiry_window="30dte"))
    assert res.diagnostics["zero_dte_expired"] is True


def test_min_dte_floor_suppresses_the_final_hour_spike():
    two_hours = 2.0 / 24.0
    rows = _rows([(100, "C", 1000, 0.20, 5)], expiry_days=7)
    rows += _rows([(100, "P", 1000, 0.20, 5)], expiry_days=0)
    rows[-1]["expiry"] = NOW + timedelta(hours=1)
    chain = build_chain(instruments.SPX, 100.0, rows, asof=NOW)

    keep = exposure.compute(chain, ExposureConfig(min_dte_days=0.0))
    drop = exposure.compute(chain, ExposureConfig(min_dte_days=two_hours))
    assert keep.diagnostics["contracts_in_window"] == 2
    assert drop.diagnostics["contracts_in_window"] == 1
    assert abs(keep.totals["cex"]) > abs(drop.totals["cex"])


def test_all_long_convention_gives_netted_dollar_delta():
    """With w = +1 everywhere, put deltas net against call deltas.

    This is the conventional "open-interest weighted net delta" reading of
    DEX, and it is the escape hatch from the synthetic-long artifact that
    long_calls_short_puts produces (see the module docstring).
    """
    spec = [(100, "C", 1000, 0.20, 0), (100, "P", 1000, 0.20, 0)]
    netted = exposure.compute(_chain(spec), ExposureConfig(dealer_sign="all_long"))
    dealer = exposure.compute(_chain(spec), ExposureConfig(dealer_sign="long_calls_short_puts"))
    assert abs(netted.totals["dex"]) < abs(dealer.totals["dex"])


def test_unknown_expiry_window_raises():
    with pytest.raises(KeyError, match="unknown expiry_window"):
        ExposureConfig(expiry_window="nonsense").max_dte()


# ----------------------------------------------------------------------
# Instrument specifics
# ----------------------------------------------------------------------


def test_multiplier_scales_exposure_linearly():
    """ES is x50, SPX is x100 -- isolated from the model difference.

    Setting q = r makes Black-Scholes and Black-76 coincide exactly (see
    ``greeks.black76_params``), so any residual difference here is the
    multiplier and nothing else.
    """
    spec = [(7780, "C", 1000, 0.15, 0), (7700, "P", 1000, 0.16, 0)]
    same_model = ExposureConfig(risk_free_rate=0.042, dividend_yield=0.042)

    spx = exposure.compute(_chain(spec, spot=7774.32, inst=instruments.SPX), same_model)
    es = exposure.compute(_chain(spec, spot=7774.32, inst=instruments.ES), same_model)

    assert spx.diagnostics["multiplier"] == 100.0
    assert es.diagnostics["multiplier"] == 50.0
    assert spx.totals["gex"] / es.totals["gex"] == pytest.approx(2.0, rel=1e-12)
    for metric in ("dex", "vanx", "cex", "vex", "thex"):
        assert spx.totals[metric] / es.totals[metric] == pytest.approx(2.0, rel=1e-12)


def test_index_and_futures_models_genuinely_differ_under_real_params():
    """With a real dividend yield the two models must NOT agree.

    Guards against someone "simplifying" the futures path into the index one:
    ES carries no dividend drift, SPX does, and that changes gamma.
    """
    spec = [(7780, "C", 1000, 0.15, 0)]
    cfg = ExposureConfig(risk_free_rate=0.042, dividend_yield=0.013)
    spx = exposure.compute(_chain(spec, spot=7774.32, inst=instruments.SPX), cfg)
    es = exposure.compute(_chain(spec, spot=7774.32, inst=instruments.ES), cfg)

    per_contract_ratio = (spx.totals["gex"] / 100.0) / (es.totals["gex"] / 50.0)
    assert per_contract_ratio != pytest.approx(1.0, rel=1e-6)


def test_es_uses_black76_and_spx_uses_black_scholes():
    spec = [(7780, "C", 1000, 0.15, 0)]
    spx = exposure.compute(_chain(spec, spot=7774.32, inst=instruments.SPX))
    es = exposure.compute(_chain(spec, spot=7774.32, inst=instruments.ES))
    assert spx.diagnostics["pricing_model"] == "black_scholes"
    assert es.diagnostics["pricing_model"] == "black76"


# ----------------------------------------------------------------------
# Aggregation integrity
# ----------------------------------------------------------------------


def test_per_strike_gex_sums_to_the_total():
    chain = _chain(
        [
            (95, "P", 500, 0.22, 3),
            (100, "C", 900, 0.20, 7),
            (100, "P", 400, 0.20, 2),
            (105, "C", 700, 0.19, 5),
        ]
    )
    res = exposure.compute(chain)
    assert res.gex.sum() == pytest.approx(res.totals["gex"])
    assert (res.call_gex + res.put_gex).sum() == pytest.approx(res.totals["gex"])


def test_volume_poc_and_pc_ratios():
    chain = _chain(
        [
            (100, "C", 10, 0.2, 100),
            (105, "C", 10, 0.2, 900),   # highest total volume
            (95, "P", 10, 0.2, 300),
        ]
    )
    res = exposure.compute(chain)
    assert res.levels["volume_poc"].strike == 105.0
    assert res.totals["pc_volume_ratio"] == pytest.approx(300 / 1000)
    assert res.totals["pc_oi_ratio"] == pytest.approx(10 / 20)


def test_distances_are_signed_from_spot():
    chain = _chain([(110, "C", 1000, 0.2, 0), (90, "P", 1000, 0.2, 0)], spot=100.0)
    res = exposure.compute(chain)
    assert res.levels["call_wall"].distance == pytest.approx(10.0)
    assert res.levels["put_wall"].distance == pytest.approx(-10.0)
    assert res.levels["call_wall"].distance_pct == pytest.approx(10.0)


def test_result_is_json_serialisable():
    import json

    chain = _chain([(100, "C", 10, 0.2, 1), (95, "P", 10, 0.2, 1)])
    payload = exposure.compute(chain).to_dict()
    assert json.loads(json.dumps(payload))["instrument"] == "SPX"
