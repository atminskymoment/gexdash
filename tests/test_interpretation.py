"""The written reading of the exposure surface.

Prose is the easiest thing in the app to get wrong without noticing: a
sentence with an inverted comparison still reads fluently, and a reader who
trusts it has no way to tell. So the directional claims are asserted here
against constructed inputs where the right answer is known.
"""

from __future__ import annotations

import pytest

from gexdash import interpretation
from gexdash.interpretation import PROXIMITY_AT, PROXIMITY_NEAR, build


def _payload(**over) -> dict:
    base = {
        "spot": 100.0,
        "totals": {"gex": 1e9, "dex": 5e9, "vanx": 1e8, "cex": -1e8,
                   "pc_oi_ratio": 1.5, "pc_volume_ratio": 1.1},
        "levels": {
            "gamma_flip": {"strike": 95.0},
            "call_wall": {"strike": 105.0},
            "put_wall": {"strike": 95.0},
            "max_pain": {"strike": 99.0},
        },
        "regime": {"label": "SUPPRESSED", "gloss": "Dealers dampen moves"},
        "diagnostics": {"dealer_sign": "long_calls_short_puts",
                        "expiry_window": "30dte", "oi_asof": "2026-08-07"},
        "instrument_meta": {"note": ""},
        "stale": False,
        "oi_unavailable": False,
    }
    base.update(over)
    return base


def _reading(result, key):
    return next(r for r in result["readings"] if r["key"] == key)


# ----------------------------------------------------------------------
# The comparison that reads fine either way round
# ----------------------------------------------------------------------


def test_a_flip_below_spot_is_described_as_below():
    """Regression: this said "above" for a level beneath spot.

    `(level - spot)/spot` is negative when the level is below, and the
    inverted sentence is perfectly grammatical -- it just describes the
    opposite market.
    """
    out = build(_payload(spot=100.0, levels={"gamma_flip": {"strike": 98.0}}))
    text = _reading(out, "gamma_flip")["meaning"]
    assert "below spot" in text
    assert "above spot" not in text


def test_a_flip_above_spot_is_described_as_above():
    out = build(_payload(spot=100.0, levels={"gamma_flip": {"strike": 102.0}}))
    assert "above spot" in _reading(out, "gamma_flip")["meaning"]


def test_max_pain_side_matches_the_arithmetic():
    below = build(_payload(spot=100.0, levels={"max_pain": {"strike": 97.0}}))
    assert "below spot" in _reading(below, "max_pain")["meaning"]
    above = build(_payload(spot=100.0, levels={"max_pain": {"strike": 103.0}}))
    assert "above spot" in _reading(above, "max_pain")["meaning"]


# ----------------------------------------------------------------------
# Gamma sign drives the whole reading
# ----------------------------------------------------------------------


def test_positive_gex_says_dealers_fade_moves_and_negative_says_they_chase():
    pos = _reading(build(_payload(totals={"gex": 1e9})), "gex")
    assert pos["tone"] == "pos"
    assert "against the move" in pos["meaning"]

    neg = _reading(build(_payload(totals={"gex": -1e9})), "gex")
    assert neg["tone"] == "neg"
    assert "with the move" in neg["meaning"]


def test_short_gamma_far_from_the_flip_reads_as_unstable():
    out = build(_payload(
        spot=100.0, totals={"gex": -1e9},
        levels={"gamma_flip": {"strike": 120.0}},
    ))
    joined = " ".join(out["synthesis"])
    assert "unstable" in joined
    assert "damped" not in joined


def test_long_gamma_far_from_the_flip_reads_as_damped():
    out = build(_payload(
        spot=100.0, totals={"gex": 1e9},
        levels={"gamma_flip": {"strike": 80.0}},
    ))
    joined = " ".join(out["synthesis"])
    assert "damped" in joined
    assert "conditionally" not in joined, "far from the flip is not the fragile case"


def test_long_gamma_close_to_the_flip_is_flagged_as_conditional():
    out = build(_payload(
        spot=100.0, totals={"gex": 1e9},
        levels={"gamma_flip": {"strike": 99.5}},
    ))
    assert "conditionally" in " ".join(out["synthesis"])


# ----------------------------------------------------------------------
# Proximity banding
# ----------------------------------------------------------------------


@pytest.mark.parametrize("strike,expected", [
    (100.2, "at"),      # 0.20% -- inside PROXIMITY_AT
    (101.0, "near"),    # 1.00%
    (110.0, "far"),     # 10.0%
])
def test_proximity_bands(strike, expected):
    assert interpretation._proximity((strike - 100.0)) == expected


def test_the_bands_are_ordered():
    assert 0 < PROXIMITY_AT < PROXIMITY_NEAR


def test_sitting_on_the_flip_is_called_out_as_the_unstable_case():
    out = build(_payload(spot=100.0, levels={"gamma_flip": {"strike": 100.1}}))
    assert "least stable" in _reading(out, "gamma_flip")["meaning"]


# ----------------------------------------------------------------------
# Assumptions travel with the claims
# ----------------------------------------------------------------------


def test_dex_carries_the_dealer_sign_caveat_beside_it():
    """DEX is huge by construction under an assumed sign convention.

    Reporting the level without that attached invites reading an artefact as
    a measurement.
    """
    out = build(_payload())
    assert "sign convention" in _reading(out, "dex")["caveat"]


def test_raw_netted_delta_gets_no_sign_convention_caveat():
    out = build(_payload(diagnostics={"dealer_sign": "all_long", "oi_asof": "2026-08-07"}))
    assert _reading(out, "dex")["caveat"] is None
    assert not any("dropdown" in s for s in out["synthesis"])


def test_the_once_daily_oi_constraint_is_always_stated():
    caveats = " ".join(build(_payload())["caveats"])
    assert "once daily" in caveats
    assert "2026-08-07" in caveats


def test_an_american_exercise_note_reaches_the_caveats():
    out = build(_payload(diagnostics={
        "dealer_sign": "all_long", "oi_asof": "2026-08-07",
        "greeks_approximation": "American exercise priced with European greeks.",
    }))
    assert any("American exercise" in c for c in out["caveats"])


def test_a_stale_chain_is_disclosed_in_the_reading_itself():
    out = build(_payload(stale=True))
    assert any("stale" in c for c in out["caveats"])


# ----------------------------------------------------------------------
# Refusals
# ----------------------------------------------------------------------


def test_no_reading_is_offered_when_there_is_no_open_interest():
    """With no OI every total is zero, and the honest output is silence.

    Interpreting zeros would produce a fluent description of a market that
    was never measured.
    """
    assert build(_payload(oi_unavailable=True)) is None


def test_missing_levels_do_not_raise():
    out = build(_payload(levels={}))
    assert out is not None
    assert "no nearby boundary" in _reading(out, "gamma_flip")["meaning"]


def test_a_flat_surface_is_described_as_flat_rather_than_directional():
    out = build(_payload(totals={"gex": 0.0, "dex": 0.0, "vanx": 0.0, "cex": 0.0}))
    assert _reading(out, "gex")["tone"] == "flat"
    assert "nets to zero" in _reading(out, "dex")["meaning"]
    # No charm sentence when there is no charm to describe.
    assert not any("Charm adds" in s for s in out["synthesis"])
