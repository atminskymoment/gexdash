"""Price history: the optional provider capability behind the terminal view."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from gexdash import instruments
from gexdash.bars import build_bars
from gexdash.providers.base import PriceHistoryProvider, PriceHistoryUnavailable
from gexdash.providers.mock import MockProvider
from gexdash.providers.schwab import SchwabProvider

ALL = ["SPX", "SPY", "ES", "NDX", "QQQ", "NQ"]


def _bars(rows, inst=None):
    return build_bars(inst or instruments.SPX, rows)


# ----------------------------------------------------------------------
# VWAP
# ----------------------------------------------------------------------


def test_vwap_is_volume_weighted_not_a_mean():
    """A big print at one price must drag VWAP toward it."""
    t = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
    rows = [
        {"time": t, "open": 100, "high": 100, "low": 100, "close": 100, "volume": 1},
        {"time": t + timedelta(minutes=1), "open": 200, "high": 200, "low": 200, "close": 200, "volume": 999},
    ]
    v = _bars(rows).vwap()
    assert v[0] == pytest.approx(100)
    assert v[-1] > 199, "VWAP ignored the volume weighting"


def test_vwap_survives_zero_volume():
    """Pre-market bars can print with no volume; that must not divide by zero."""
    t = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
    rows = [{"time": t + timedelta(minutes=i), "open": 100, "high": 101,
             "low": 99, "close": 100, "volume": 0} for i in range(5)]
    v = _bars(rows).vwap()
    assert np.all(np.isfinite(v))
    assert v[0] == pytest.approx(100)


def test_bars_are_sorted_oldest_first():
    t = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
    rows = [{"time": t + timedelta(minutes=i), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
            for i in (3, 0, 2, 1)]
    b = _bars(rows)
    assert list(b.time) == sorted(b.time)


def test_mismatched_column_lengths_raise():
    from gexdash.bars import PriceBars
    with pytest.raises(ValueError, match="expected"):
        PriceBars(
            instrument=instruments.SPX, interval_minutes=1,
            time=np.array([1, 2], dtype="datetime64[s]"),
            open=np.array([1.0]), high=np.array([1.0]),
            low=np.array([1.0]), close=np.array([1.0]), volume=np.array([1.0]),
        )


def test_epoch_millis_are_parsed():
    """Schwab returns candle timestamps as epoch milliseconds."""
    b = _bars([{"time": 1786000000000, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}])
    assert str(b.time[0]).startswith("2026-")


# ----------------------------------------------------------------------
# Mock provider
# ----------------------------------------------------------------------


def test_mock_declares_the_capability():
    assert isinstance(MockProvider(), PriceHistoryProvider)


@pytest.mark.parametrize("key", ALL)
def test_mock_bars_are_coherent(key):
    inst = instruments.get(key)
    b = MockProvider().get_price_history(inst, interval_minutes=1, lookback_hours=3)

    assert len(b) > 100
    assert np.all(b.high >= b.low)
    assert np.all(b.high >= np.maximum(b.open, b.close) - 1e-9)
    assert np.all(b.low <= np.minimum(b.open, b.close) + 1e-9)
    assert np.all(b.volume >= 0)
    assert np.all(np.isfinite(b.vwap()))
    assert list(b.time) == sorted(b.time)

    # The walk must stay near the instrument's own level, not wander to a
    # value where the strike grid would not bracket it.
    from gexdash.providers.mock import DEFAULT_SPOT
    assert abs(b.last - DEFAULT_SPOT[key]) / DEFAULT_SPOT[key] < 0.05


def test_mock_bars_are_reproducible():
    a = MockProvider().get_price_history(instruments.SPX, lookback_hours=2)
    b = MockProvider().get_price_history(instruments.SPX, lookback_hours=2)
    np.testing.assert_allclose(a.close, b.close)


def test_higher_vol_instruments_get_wider_candles():
    """NDX is generated at a higher ATM vol than SPX; the bars must show it."""
    spx = MockProvider().get_price_history(instruments.SPX, lookback_hours=4)
    ndx = MockProvider().get_price_history(instruments.NDX, lookback_hours=4)
    rel = lambda b: np.mean((b.high - b.low) / b.close)
    assert rel(ndx) > rel(spx)


def test_interval_and_lookback_are_respected():
    b = MockProvider().get_price_history(instruments.SPX, interval_minutes=5, lookback_hours=2)
    assert b.interval_minutes == 5
    assert 20 <= len(b) <= 30
    gaps = np.diff(b.time).astype("timedelta64[m]").astype(int)
    assert set(gaps.tolist()) == {5}


# ----------------------------------------------------------------------
# Schwab: the unavailable contract
# ----------------------------------------------------------------------


def _schwab_with_candles(closes):
    """A SchwabProvider whose /pricehistory returns the given closes."""
    p = SchwabProvider.__new__(SchwabProvider)
    candles = [
        {"datetime": 1_700_000_000_000 + i * 86_400_000, "open": c, "high": c + 1,
         "low": c - 1, "close": c, "volume": 1000}
        for i, c in enumerate(closes)
    ]
    p._get = lambda path, params: {"candles": candles, "empty": False}
    return p


def test_schwab_refuses_futures_history_that_resolves_to_another_security():
    """Bare ``ES`` on Schwab is Eversource Energy at ~$71. Priced as an E-mini
    it would chart perfectly plausibly, so it must be refused -- with the
    *typed* error, so the server degrades instead of 502ing."""
    p = _schwab_with_candles([70.1, 70.9, 71.4])
    with pytest.raises(PriceHistoryUnavailable, match="plausible band"):
        p.get_price_history(instruments.get("ES"))


def test_schwab_serves_plausible_futures_history_marked_unverified():
    """A futures root that passes the tripwire is served, but says what it is."""
    p = _schwab_with_candles([7790.0, 7801.5, 7795.25])
    bars = p.get_price_history(instruments.get("ES"))
    assert bars.last == 7795.25
    assert "unverified" in bars.meta and "/ES" in bars.meta["unverified"]


def test_schwab_equity_history_carries_no_unverified_note():
    p = _schwab_with_candles([720.0, 722.0, 725.5])
    assert "unverified" not in p.get_price_history(instruments.get("QQQ")).meta


def test_schwab_asks_for_regular_hours_only_when_the_plan_says_so():
    """The Chart view draws ETFs in regular hours, TradingView's default; the
    terminal chart keeps extended hours. One flag on the plan decides."""
    from gexdash.technicals import plan_for

    seen = []
    p = _schwab_with_candles([720.0, 722.0, 725.5])
    inner = p._get
    p._get = lambda path, params: (seen.append(params), inner(path, params))[1]
    rth, _ = plan_for("5D", "5m", session="rth")
    eth, _ = plan_for("5D", "5m", session="eth")
    p.get_price_history(instruments.get("QQQ"), plan=rth)
    p.get_price_history(instruments.get("QQQ"), plan=eth)
    assert [q["needExtendedHoursData"] for q in seen] == ["false", "true"]
    assert all("startDate" in q and "endDate" in q for q in seen)
