"""Provider and store tests."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

from gexdash import exposure, instruments
from gexdash.exposure import ExposureConfig
from gexdash.providers.mock import MockProvider
from gexdash.providers.schwab import SchwabProvider, _clean
from gexdash.sessions import exchange_today, previous_session
from gexdash.store import OI_ZERO_REASON, CachedProvider, ChainStore


# ----------------------------------------------------------------------
# Mock provider
# ----------------------------------------------------------------------


def test_mock_spot_walk_survives_a_long_run():
    """Regression: the spot walk must not crash once it drifts negative.

    The original implementation re-seeded a generator per call with a value
    derived from the accumulated drift. As soon as the walk went below its
    starting point that seed was negative, and numpy rejects a negative seed
    with "expected non-negative integer" -- a crash that only appeared after
    the price happened to wander down, so it passed every short test.
    """
    p = MockProvider()
    spots = [p.get_spot(instruments.SPX) for _ in range(3000)]
    assert all(np.isfinite(s) for s in spots)
    assert min(spots) < spots[0], "walk never went down; the regression is untested"


def test_mock_chain_is_shaped_like_an_index_chain():
    chain = MockProvider().get_chain(instruments.SPX, max_dte=60)
    assert len(chain) > 500
    assert chain.oi_asof is not None and chain.oi_asof < exchange_today()
    assert chain.meta["synthetic"] is True

    # Every listed contract must be live -- a provider that emits already
    # settled contracts produces singular greeks downstream.
    assert np.all(chain.dte > 0), "mock emitted expired contracts"

    # Equity index skew: downside puts should carry more vol than upside calls.
    front = chain.expiry == chain.expiries()[0]
    otm_put = front & (chain.right < 0) & (chain.strike < chain.spot * 0.985)
    otm_call = front & (chain.right > 0) & (chain.strike > chain.spot * 1.015)
    assert chain.iv[otm_put].mean() > chain.iv[otm_call].mean()


def test_mock_chain_respects_max_dte():
    chain = MockProvider().get_chain(instruments.SPX, max_dte=7)
    assert chain.dte.max() <= 7.0


def test_mock_chain_computes_end_to_end_for_both_instruments():
    for inst in (instruments.SPX, instruments.ES):
        chain = MockProvider().get_chain(inst, max_dte=60)
        res = exposure.compute(chain, ExposureConfig(expiry_window="30dte"))
        assert res.diagnostics["contracts_in_window"] > 0
        assert res.levels["call_wall"].strike is not None
        assert np.isfinite(res.totals["gex"])
        # CEX is the metric that explodes if expired contracts leak through.
        assert abs(res.totals["cex"]) < abs(res.totals["gex"]) * 1e3


# ----------------------------------------------------------------------
# Schwab response parsing (no network)
# ----------------------------------------------------------------------


def test_clean_maps_schwab_sentinels_to_none():
    assert _clean(-999.0) is None
    assert _clean(float("nan")) is None
    assert _clean(None) is None
    assert _clean("abc") is None
    assert _clean(0.42) == 0.42
    assert _clean(0.0, 5.0) == 0.0  # a real zero is not a sentinel


def test_schwab_chain_parsing():
    """Parse a response shaped like Schwab's, without touching the network."""
    payload = {
        "2026-08-10:0": {
            "7780.0": [
                {
                    "putCall": "CALL",
                    "strikePrice": 7780.0,
                    "expirationDate": "2026-08-10T20:00:00.000+00:00",
                    "volatility": 13.5,          # percent, must become 0.135
                    "openInterest": 1234,
                    "totalVolume": 567,
                    "delta": 0.51,
                    "gamma": 0.0031,
                }
            ],
            "7785.0": [
                {
                    "putCall": "CALL",
                    "strikePrice": 7785.0,
                    "expirationDate": "2026-08-10T20:00:00.000+00:00",
                    "volatility": -999.0,        # sentinel -> iv 0.0
                    "openInterest": -999.0,      # sentinel -> 0.0
                    "totalVolume": 10,
                    "delta": -999.0,
                    "gamma": -999.0,
                }
            ],
        }
    }
    rows = list(SchwabProvider._iter_contracts(payload, "C"))
    assert len(rows) == 2

    first = rows[0]
    assert first["strike"] == 7780.0
    assert first["iv"] == pytest.approx(0.135), "percent volatility must be scaled to a decimal"
    assert first["open_interest"] == 1234
    assert first["delta"] == 0.51

    second = rows[1]
    assert second["iv"] == 0.0
    assert second["open_interest"] == 0.0
    assert second["delta"] is None


def test_schwab_oi_date_is_a_prior_session():
    assert SchwabProvider._infer_oi_date() < exchange_today()


# ----------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------


def test_store_round_trips_a_chain(tmp_path):
    store = ChainStore(tmp_path / "t.db")
    original = MockProvider().get_chain(instruments.SPX, max_dte=30)
    store.save(original, provider="mock")

    loaded = store.load(instruments.SPX)
    assert loaded is not None
    assert len(loaded) == len(original)
    np.testing.assert_allclose(loaded.strike, original.strike)
    np.testing.assert_allclose(loaded.iv, original.iv)
    np.testing.assert_allclose(loaded.open_interest, original.open_interest)
    assert loaded.oi_asof == original.oi_asof
    assert loaded.instrument.key == "SPX"

    # And the reloaded chain must still compute.
    res = exposure.compute(loaded, ExposureConfig(expiry_window="30dte"))
    assert np.isfinite(res.totals["gex"])


def test_store_survives_concurrent_use_from_worker_threads(tmp_path):
    """The server shares one store across its worker threads, and a fresh
    install's first page load asks for exposure, bars and breadth at once.
    Without the store's lock this crashed the process inside SQLite (exit 139
    or 138, every run), so it runs in a subprocess: a regression fails this
    test rather than killing the suite."""
    script = f"""
import threading
from gexdash import instruments
from gexdash.providers.mock import MockProvider
from gexdash.store import ChainStore

store = ChainStore({str(tmp_path / "fresh.db")!r})
chain = MockProvider().get_chain(instruments.SPX, max_dte=7)

def work(k):
    for j in range(25):
        store.save_breadth("NYSE", k, j, k - j)
        store.save(chain, provider="mock")
        assert store.load(instruments.SPX) is not None
        store.breadth_history("NYSE")

threads = [threading.Thread(target=work, args=(k,)) for k in range(8)]
for t in threads: t.start()
for t in threads: t.join()
print("ok")
"""
    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
                       cwd=root, env={**os.environ, "PYTHONPATH": str(root)})
    assert r.returncode == 0 and r.stdout.strip() == "ok", (r.returncode, r.stderr[-2000:])


def test_cached_provider_serves_from_cache_but_streams_spot(tmp_path):
    class Counting(MockProvider):
        chain_calls = 0
        spot_calls = 0

        def get_chain(self, inst, *, max_dte=None):
            Counting.chain_calls += 1
            return super().get_chain(inst, max_dte=max_dte)

        def get_spot(self, inst):
            Counting.spot_calls += 1
            return super().get_spot(inst)

    provider = CachedProvider(Counting(), ChainStore(tmp_path / "c.db"))
    for _ in range(5):
        provider.get_chain(instruments.SPX, max_dte=30)
        provider.get_spot(instruments.SPX)

    assert Counting.chain_calls == 1, "chain should be fetched once -- OI is a daily figure"
    assert Counting.spot_calls == 5, "spot is the live half and must not be cached"


def test_cached_provider_force_bypasses_cache(tmp_path):
    store = ChainStore(tmp_path / "f.db")
    provider = CachedProvider(MockProvider(), store)
    provider.get_chain(instruments.SPX, max_dte=30)
    chain = provider.get_chain(instruments.SPX, max_dte=30, force=True)
    assert chain.meta.get("from_cache") is False


# ----------------------------------------------------------------------
# Live-feed quirks, all found against a real Schwab entitlement
# ----------------------------------------------------------------------


def test_long_max_value_sentinel_is_filtered():
    """$UVOL returns 92233720368.54776 live -- Long.MAX_VALUE / 1e8.

    Taken at face value that is 92 billion shares of advancing volume, and
    every ratio derived from it is nonsense.
    """
    assert _clean(92233720368.54776) is None
    assert _clean(9223372036854775807.0) is None
    assert _clean(1642.0) == 1642.0
    assert _clean(9.2e10) == 9.2e10, "a merely large number is not the sentinel"


def test_wrong_security_is_refused_by_the_plausibility_band():
    """Schwab's bare `ES` is Eversource Energy at ~$71.

    It returns a valid option chain. Priced as an E-mini at x$50 with
    Black-76 it would yield a smooth, wholly fictitious exposure surface, so
    the band exists to make that loud instead of silent.
    """
    from gexdash.providers.base import ProviderError
    from gexdash.providers.schwab import SchwabProvider

    with pytest.raises(ProviderError, match="outside the plausible band"):
        SchwabProvider._assert_plausible(instruments.get("ES"), 71.17, "chain")

    # A real E-mini print passes.
    SchwabProvider._assert_plausible(instruments.get("ES"), 7791.5, "chain")


@pytest.mark.parametrize("key,spot", [
    ("SPX", 7753.11), ("SPY", 773.0), ("NDX", 29621.8), ("QQQ", 721.05),
])
def test_real_observed_prices_pass_the_band(key, spot):
    """Bands are decades-wide tripwires, not forecasts -- live values must pass."""
    from gexdash.providers.schwab import SchwabProvider
    SchwabProvider._assert_plausible(instruments.get(key), spot, "chain")


def test_chain_responses_merge_without_losing_strikes():
    """The date-bisection fix for Schwab's TooBigBody 502.

    A full SPX chain over 60 days exceeds the gateway's response limit, so it
    is fetched in date slices and merged. strikeCount would have been easier
    and would have silently truncated the wings -- where put walls live.
    """
    from gexdash.providers.schwab import SchwabProvider

    left = {"underlyingPrice": 7753.11,
            "callExpDateMap": {"2026-08-14:4": {"7700.0": [{"a": 1}]}},
            "putExpDateMap": {"2026-08-14:4": {"7700.0": [{"b": 1}]}}}
    right = {"underlyingPrice": None,
             "callExpDateMap": {"2026-09-18:39": {"7800.0": [{"c": 1}]},
                                "2026-08-14:4": {"7750.0": [{"d": 1}]}},
             "putExpDateMap": {}}

    merged = SchwabProvider._merge_chain_responses(left, right)
    assert set(merged["callExpDateMap"]) == {"2026-08-14:4", "2026-09-18:39"}
    # A boundary landing mid-expiry must union the strikes, not replace them.
    assert set(merged["callExpDateMap"]["2026-08-14:4"]) == {"7700.0", "7750.0"}
    assert merged["underlyingPrice"] == 7753.11, "must keep the slice that had a price"


# ----------------------------------------------------------------------
# Zero open interest — a failed fetch that looks like a successful one
# ----------------------------------------------------------------------


class _ZeroOIProvider(MockProvider):
    """A provider whose chain arrives with every openInterest at zero.

    Observed live: Schwab served 2,338 SPX contracts with openInterest 0 on
    every one, volume populated, no error. Every exposure measure is
    proportional to OI, so the surface collapses to $0 -- which renders as a
    confident zero rather than as missing data.
    """

    name = "zero-oi"

    def get_chain(self, inst, *, max_dte=None):
        chain = super().get_chain(inst, max_dte=max_dte)
        chain.open_interest[:] = 0.0
        return chain


def test_zero_oi_chain_never_overwrites_a_good_snapshot(tmp_path):
    store = ChainStore(tmp_path / "z.db")
    good = CachedProvider(MockProvider(), store)
    baseline = good.get_chain(instruments.SPX, max_dte=30)
    assert baseline.open_interest.sum() > 0

    degraded = CachedProvider(_ZeroOIProvider(), store)
    chain = degraded.get_chain(instruments.SPX, max_dte=30, force=True)

    assert chain.open_interest.sum() > 0, "must keep serving the last good chain"
    assert chain.meta["stale"] is True
    assert chain.meta["degraded_reason"] == OI_ZERO_REASON

    # And the zeroed chain must not have displaced the good one on disk.
    assert store.load(instruments.SPX).open_interest.sum() > 0


def test_zero_oi_chain_is_flagged_when_there_is_nothing_to_fall_back_to(tmp_path):
    provider = CachedProvider(_ZeroOIProvider(), ChainStore(tmp_path / "z2.db"))
    chain = provider.get_chain(instruments.SPX, max_dte=30)

    assert chain.meta["oi_unavailable"] is True
    assert chain.meta["stale"] is True
    assert chain.meta["degraded_reason"] == OI_ZERO_REASON


def test_a_zero_oi_chain_is_never_cached(tmp_path):
    """Otherwise the next request serves it from disk as if it were good."""
    store = ChainStore(tmp_path / "z3.db")
    CachedProvider(_ZeroOIProvider(), store).get_chain(instruments.SPX, max_dte=30)
    assert store.load(instruments.SPX) is None


def test_a_thin_but_real_chain_is_not_mistaken_for_a_failed_fetch(tmp_path):
    """A thin chain is one where few contracts are *listed*, not one where
    thousands are listed and two are held.

    Measured healthy coverage on a live entitlement was 34-73% of contracts;
    the degraded chain was 0.01%. This keeps a 10% chain valid.
    """
    class _Thin(MockProvider):
        def get_chain(self, inst, *, max_dte=None):
            chain = super().get_chain(inst, max_dte=max_dte)
            chain.open_interest[:] = 0.0
            chain.open_interest[::10] = 1.0          # 10% coverage
            return chain

    chain = CachedProvider(_Thin(), ChainStore(tmp_path / "z4.db")).get_chain(instruments.SPX, max_dte=30)
    assert chain.meta.get("oi_unavailable") is not True
    assert chain.meta["stale"] is False


def test_a_handful_of_positions_in_a_huge_chain_is_a_failed_fetch(tmp_path):
    """The case actually observed: 2 of 16,862 SPX contracts carried OI, 896
    contracts in total, and none at all inside the analysed expiry window."""
    class _TwoPositions(MockProvider):
        def get_chain(self, inst, *, max_dte=None):
            chain = super().get_chain(inst, max_dte=max_dte)
            chain.open_interest[:] = 0.0
            chain.open_interest[0] = 512.0
            chain.open_interest[1] = 384.0
            return chain

    chain = CachedProvider(_TwoPositions(), ChainStore(tmp_path / "z5.db")).get_chain(instruments.SPX, max_dte=30)
    assert chain.meta["oi_unavailable"] is True


def test_a_poisoned_snapshot_does_not_shadow_the_good_one_behind_it(tmp_path):
    """The zero-OI check postdates the rows already on disk.

    load() returns the newest snapshot_day, so a zeroed row written before
    the check existed would be served as good until it aged out -- and would
    also hide the previous day's usable chain.
    """
    from datetime import date, timedelta

    store = ChainStore(tmp_path / "p.db")
    good = MockProvider().get_chain(instruments.SPX, max_dte=30)
    store.save(good, provider="mock", day=date(2026, 8, 7))

    zeroed = MockProvider().get_chain(instruments.SPX, max_dte=30)
    zeroed.open_interest[:] = 0.0
    store.save(zeroed, provider="mock", day=date(2026, 8, 10))

    assert store.load(instruments.SPX).open_interest.sum() == 0, "newest row is the bad one"
    assert store.load_usable(instruments.SPX).open_interest.sum() > 0

    # And the provider serves the good one rather than the zeroed newest.
    chain = CachedProvider(_ZeroOIProvider(), store).get_chain(instruments.SPX, max_dte=30)
    assert chain.open_interest.sum() > 0
    assert chain.meta["stale"] is True


def test_a_degraded_chain_is_not_refetched_on_every_request(tmp_path):
    """It is never written to the store, so without a back-off each request
    would re-pull it. For SPX that is four paginated calls and ~17k
    contracts, repeated on every dashboard poll.
    """
    class _Counting(_ZeroOIProvider):
        calls = 0

        def get_chain(self, inst, *, max_dte=None):
            _Counting.calls += 1
            return super().get_chain(inst, max_dte=max_dte)

    provider = CachedProvider(_Counting(), ChainStore(tmp_path / "b.db"))
    for _ in range(5):
        chain = provider.get_chain(instruments.SPX, max_dte=30)
        assert chain.meta["oi_unavailable"] is True

    assert _Counting.calls == 1, "the refusal must be remembered, not re-discovered"

    # The refetch button must still reach the provider.
    provider.get_chain(instruments.SPX, max_dte=30, force=True)
    assert _Counting.calls == 2


def test_the_backoff_clears_once_a_good_chain_arrives(tmp_path):
    store = ChainStore(tmp_path / "r.db")
    provider = CachedProvider(_ZeroOIProvider(), store)
    provider.get_chain(instruments.SPX, max_dte=30)
    assert provider._degraded

    provider.provider = MockProvider()
    chain = provider.get_chain(instruments.SPX, max_dte=30, force=True)
    assert chain.meta["stale"] is False
    assert not provider._degraded, "a recovered feed must not stay backed off"
    assert store.load(instruments.SPX) is not None, "and the good chain is cached again"


def test_the_retry_delay_doubles_while_the_feed_stays_bad(tmp_path):
    """30s first -- the common case is a transient drop around the session
    boundary, and a long first wait leaves the dashboard reading "n/a" well
    after the data came back. Doubling stops a genuinely broken feed from
    being re-pulled every 30s all night.
    """
    p = CachedProvider(_ZeroOIProvider(), ChainStore(tmp_path / "e.db"))
    assert [p._backoff_seconds(n) for n in range(1, 7)] == [30, 60, 120, 240, 480, 480]


def test_consecutive_failures_lengthen_the_wait_and_success_resets_it(tmp_path):
    store = ChainStore(tmp_path / "e2.db")
    p = CachedProvider(_ZeroOIProvider(), store)

    for expected in (1, 2, 3):
        p.get_chain(instruments.SPX, max_dte=30, force=True)
        assert p._degraded[instruments.SPX.key][2] == expected

    p.provider = MockProvider()
    p.get_chain(instruments.SPX, max_dte=30, force=True)
    assert instruments.SPX.key not in p._degraded

    # A later failure starts the ladder again rather than resuming at 240s.
    p.provider = _ZeroOIProvider()
    p.get_chain(instruments.SPX, max_dte=30, force=True)
    assert p._degraded[instruments.SPX.key][2] == 1
