"""Tests for the provider-greek cross-check.

The point of the cross-check is to catch a wrong rate or dividend assumption
before it propagates silently into VANX and CEX, which no other test can
verify. So these tests must prove the check has *teeth* -- that it fails when
the inputs are wrong, not merely that it passes when they are right.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from gexdash import instruments
from gexdash.chain import build_chain
from gexdash.crosscheck import cross_check, fit_rate
from gexdash.greeks import CALL, PUT, black_scholes

NOW = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
TRUE_R = 0.0415
TRUE_Q = 0.0121


def _chain_with_provider_greeks(r=TRUE_R, q=TRUE_Q, spot=7774.32):
    """A chain whose 'provider' greeks were generated at known r and q."""
    strikes = np.arange(7600, 7960, 20.0)
    rows = []
    exp = NOW + timedelta(days=21)
    for k in strikes:
        for right, w in (("C", CALL), ("P", PUT)):
            iv = 0.13 + 0.5 * abs(np.log(k / spot))
            t = np.array([21.0 / 365.0])
            g = black_scholes(
                np.array([spot]), np.array([k]), t, np.array([iv]), np.array([w]), r=r, q=q
            )
            rows.append(
                {
                    "strike": float(k),
                    "expiry": exp,
                    "right": right,
                    "iv": float(iv),
                    "open_interest": 1000.0,
                    "volume": 100.0,
                    "delta": float(g.delta[0]),
                    "gamma": float(g.gamma[0]),
                }
            )
    return build_chain(instruments.SPX, spot, rows, asof=NOW)


def test_matching_assumptions_pass():
    report = cross_check(_chain_with_provider_greeks(), r=TRUE_R, q=TRUE_Q)
    assert report.compared > 0
    assert report.delta_mae < 1e-9
    assert report.gamma_mae < 1e-12
    assert report.ok


def test_a_wrong_rate_is_caught():
    """The whole point: a bad rate must fail, not slide through."""
    chain = _chain_with_provider_greeks()
    bad = cross_check(chain, r=TRUE_R + 0.05, q=TRUE_Q)
    assert bad.delta_mae > 0.01
    assert not bad.ok


def test_a_wrong_dividend_is_caught():
    chain = _chain_with_provider_greeks()
    bad = cross_check(chain, r=TRUE_R, q=TRUE_Q + 0.05)
    assert bad.delta_mae > 0.01
    assert not bad.ok


def test_fit_rate_recovers_the_true_rate():
    chain = _chain_with_provider_greeks()
    assert fit_rate(chain, q=TRUE_Q) == pytest.approx(TRUE_R, abs=2e-3)


def test_chain_without_provider_greeks_reports_nothing_to_compare():
    chain = build_chain(
        instruments.SPX,
        100.0,
        [{"strike": 100, "expiry": NOW + timedelta(days=7), "right": "C",
          "iv": 0.2, "open_interest": 10, "volume": 1}],
        asof=NOW,
    )
    report = cross_check(chain)
    assert report.compared == 0
    assert not report.ok
    assert "no provider greeks" in str(report)


def test_expired_and_ivless_contracts_are_skipped():
    chain = _chain_with_provider_greeks()
    n_before = cross_check(chain).compared

    chain.iv[0] = 0.0                      # no usable IV
    chain.expiry[1] = np.datetime64(NOW.replace(tzinfo=None), "s")  # expired
    assert cross_check(chain).compared == n_before - 2


def test_report_renders_a_verdict():
    text = str(cross_check(_chain_with_provider_greeks(), r=TRUE_R, q=TRUE_Q))
    assert "PASS" in text and "delta" in text and "gamma" in text


# ----------------------------------------------------------------------
# Dividend resolution
# ----------------------------------------------------------------------


def test_dividend_defaults_to_the_instruments_own_yield():
    """The tool built to catch a wrong dividend must not supply one.

    ExposureConfig resolves q per instrument. A hardcoded default here would
    mean cross-checking QQQ (0.5%) against 1.3% and reporting a mismatch the
    engine never actually has.
    """
    from gexdash.providers.mock import MockProvider

    for key in ("SPX", "QQQ", "NDX"):
        inst = instruments.get(key)
        chain = MockProvider().get_chain(inst, max_dte=30)
        report = cross_check(chain)
        assert report.dividend == inst.default_dividend_yield, (
            f"{key} cross-checked against {report.dividend}, "
            f"not its own {inst.default_dividend_yield}"
        )


def test_explicit_dividend_still_overrides():
    chain = _chain_with_provider_greeks()
    assert cross_check(chain, q=0.031).dividend == 0.031
