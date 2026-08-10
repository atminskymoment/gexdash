"""Market internals from the exchange breadth symbols."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gexdash import instruments
from gexdash.breadth import (
    BREADTH_SYMBOLS,
    FAMILY_EXCHANGE,
    build_snapshot,
    symbols_for_family,
)
from gexdash.providers.base import BreadthProvider, BreadthUnavailable
from gexdash.providers.mock import MockProvider
from gexdash.providers.schwab import SchwabProvider
from gexdash.store import ChainStore


# ----------------------------------------------------------------------
# Symbol map
# ----------------------------------------------------------------------


def test_both_exchanges_define_the_core_metrics():
    for exchange, table in BREADTH_SYMBOLS.items():
        for metric in ("advancing", "declining", "net"):
            assert metric in table, f"{exchange} missing {metric}"
            assert table[metric].startswith("$"), f"{exchange}.{metric} is not an index symbol"


def test_family_mapping_covers_every_instrument_family():
    """Breadth keys off instruments.FAMILIES rather than a second list."""
    assert set(FAMILY_EXCHANGE) == set(instruments.FAMILIES)
    for family in instruments.FAMILIES:
        exchange, table = symbols_for_family(family)
        assert exchange in BREADTH_SYMBOLS
        assert table is BREADTH_SYMBOLS[exchange]


def test_sp500_family_maps_to_nyse_and_nasdaq_family_to_nasdaq():
    assert symbols_for_family("S&P 500")[0] == "NYSE"
    assert symbols_for_family("Nasdaq-100")[0] == "NASDAQ"
    assert BREADTH_SYMBOLS["NYSE"]["advancing"] == "$ADVN"
    assert BREADTH_SYMBOLS["NASDAQ"]["advancing"] == "$ADVQ"


# ----------------------------------------------------------------------
# Derived quantities
# ----------------------------------------------------------------------


def test_derived_values():
    s = build_snapshot("NYSE", {"advancing": 1800.0, "declining": 1200.0, "net": 600.0})
    assert s.total_issues == 3000
    assert s.net == 600
    assert s.ad_ratio == pytest.approx(1.5)
    assert s.label == "POSITIVE"
    assert s.reconciliation_warning is None


def test_label_thresholds():
    assert build_snapshot("NYSE", {"advancing": 900.0, "declining": 2100.0}).label == "NEGATIVE"
    assert build_snapshot("NYSE", {"advancing": 1500.0, "declining": 1500.0}).label == "NEUTRAL"
    assert build_snapshot("NYSE", {}).label == "UNKNOWN"


def test_net_prefers_the_counts_it_displays():
    """The panel shows both counts, so the net must be what they imply.

    If the derived net and the exchange's own figure disagree, showing the
    exchange's would contradict the two numbers right beside it.
    """
    s = build_snapshot("NYSE", {"advancing": 1800.0, "declining": 1200.0, "net": 599.0})
    assert s.net == 600
    assert s.net_reported == 599


def test_net_falls_back_to_the_reported_figure():
    s = build_snapshot("NYSE", {"net": 412.0})
    assert s.net == 412


def test_zero_declining_does_not_divide_by_zero():
    s = build_snapshot("NYSE", {"advancing": 100.0, "declining": 0.0})
    assert s.ad_ratio is None
    assert s.label == "UNKNOWN"


# ----------------------------------------------------------------------
# The cross-check — must have teeth
# ----------------------------------------------------------------------


def test_mismatched_net_raises_a_reconciliation_warning():
    """A wrong symbol mapping produces entirely plausible numbers.

    That is this project's recurring failure mode, so the counts are checked
    against the exchange's own net rather than trusted.
    """
    s = build_snapshot("NYSE", {"advancing": 1800.0, "declining": 1200.0, "net": 150.0})
    assert s.reconciliation_warning is not None
    assert "BREADTH_SYMBOLS" in s.reconciliation_warning
    assert "1,800" not in s.reconciliation_warning  # reports the derived net, not the input
    assert "600" in s.reconciliation_warning and "150" in s.reconciliation_warning


def test_small_discrepancies_are_tolerated():
    s = build_snapshot("NYSE", {"advancing": 1800.0, "declining": 1200.0, "net": 601.0})
    assert s.reconciliation_warning is None


def test_no_warning_when_the_net_symbol_is_missing():
    s = build_snapshot("NYSE", {"advancing": 1800.0, "declining": 1200.0}, unresolved={"net": "$ADD"})
    assert s.reconciliation_warning is None
    assert s.unresolved == {"net": "$ADD"}


# ----------------------------------------------------------------------
# Providers
# ----------------------------------------------------------------------


def test_mock_declares_the_capability():
    assert isinstance(MockProvider(), BreadthProvider)


@pytest.mark.parametrize("exchange", ["NYSE", "NASDAQ"])
def test_mock_breadth_is_coherent(exchange):
    b = MockProvider().get_breadth(exchange)
    assert b.exchange == exchange
    assert b.advancing > 0 and b.declining > 0
    assert b.total_issues == b.advancing + b.declining
    assert b.net == b.advancing - b.declining
    assert b.reconciliation_warning is None, "the mock must pass its own integrity check"
    assert b.unresolved == {}


def test_mock_breadth_drifts_across_calls():
    """A constant reading would render the session A/D line dead flat."""
    p = MockProvider()
    nets = [p.get_breadth("NYSE").net for _ in range(10)]
    assert len(set(nets)) > 5, "breadth is not varying; the sparkline would be a straight line"


def test_scope_string_says_exchange_wide():
    """Nobody should read these counts as index membership."""
    d = MockProvider().get_breadth("NYSE").to_dict()
    assert "not index constituents" in d["scope"]


def test_schwab_rejects_an_unknown_exchange():
    p = SchwabProvider.__new__(SchwabProvider)
    with pytest.raises(BreadthUnavailable, match="no breadth symbols"):
        p.get_breadth("LSE")


# ----------------------------------------------------------------------
# Sample store — the session A/D line
# ----------------------------------------------------------------------


def test_samples_accumulate_oldest_first(tmp_path):
    store = ChainStore(tmp_path / "b.db")
    base = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
    for i in range(5):
        store.save_breadth("NYSE", 1000 + i * 10, 900, 100 + i * 10, base + timedelta(minutes=i))

    hist = store.breadth_history("NYSE")
    assert len(hist) == 5
    assert [h["net"] for h in hist] == [100, 110, 120, 130, 140]


def test_samples_bucket_to_the_minute(tmp_path):
    """At a 60s TTL, re-fetches inside one bucket must overwrite, not pile up."""
    store = ChainStore(tmp_path / "b.db")
    base = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
    for sec in (0, 15, 44, 59):
        store.save_breadth("NYSE", 1000, 900, 100 + sec, base + timedelta(seconds=sec))
    hist = store.breadth_history("NYSE")
    assert len(hist) == 1
    assert hist[0]["net"] == 159   # last write in the bucket wins


def test_exchanges_are_stored_separately(tmp_path):
    store = ChainStore(tmp_path / "b.db")
    now = datetime(2026, 8, 10, 14, 0, tzinfo=timezone.utc)
    store.save_breadth("NYSE", 1000, 900, 100, now)
    store.save_breadth("NASDAQ", 2000, 2100, -100, now)
    assert store.breadth_history("NYSE")[0]["net"] == 100
    assert store.breadth_history("NASDAQ")[0]["net"] == -100
