"""API tests, including the SSE path.

This layer had zero coverage and it is where the one real production bug
landed: a crash in the spot walk surfaced as a 422 only after the price
happened to drift negative, which no single request would have caught. So the
happy-path tests here deliberately loop.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from gexdash import instruments
from gexdash.providers.base import ProviderError
from gexdash.providers.mock import MockProvider
from gexdash.server import app, set_provider
from gexdash.store import CachedProvider, ChainStore


@pytest.fixture(autouse=True)
def isolated_provider(tmp_path):
    """Never touch the real ~/.gexdash from a test."""
    set_provider(CachedProvider(MockProvider(), ChainStore(tmp_path / "test.db")))
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


# ----------------------------------------------------------------------
# Repeated requests — exercises the spot walk
# ----------------------------------------------------------------------


def test_repeated_exposure_requests_all_succeed(client):
    """Regression for the negative-seed crash, at the API boundary."""
    spots = []
    for _ in range(30):
        r = client.get("/api/exposure?symbol=SPX&expiry_window=30dte")
        assert r.status_code == 200, r.text
        spots.append(r.json()["spot"])
    assert len(set(spots)) > 1, "spot never moved; the walk is not being exercised"


def test_exposure_payload_shape(client):
    d = client.get("/api/exposure?symbol=SPX").json()
    for key in ("spot", "strikes", "gex", "dex", "vanx", "cex", "vex", "thex",
                "totals", "levels", "diagnostics", "unavailable", "synthetic", "stale"):
        assert key in d, f"missing {key}"
    n = len(d["strikes"])
    for arr in ("gex", "dex", "vanx", "call_oi", "put_volume"):
        assert len(d[arr]) == n
    assert d["synthetic"] is True
    assert len(d["unavailable"]) >= 6, "unavailable tiles must be declared, not omitted"


@pytest.mark.parametrize("symbol", ["SPX", "SPY", "ES", "NDX", "QQQ", "NQ"])
@pytest.mark.parametrize("window", ["0dte", "7dte", "30dte", "all"])
def test_all_instrument_and_window_combinations(client, symbol, window):
    r = client.get(f"/api/exposure?symbol={symbol}&expiry_window={window}")
    assert r.status_code == 200, r.text
    assert r.json()["diagnostics"]["expiry_window"] == window


@pytest.mark.parametrize("sign", ["long_calls_short_puts", "short_calls_long_puts", "all_long"])
@pytest.mark.parametrize("flip", ["spot_solve", "cumulative"])
def test_all_model_options(client, sign, flip):
    r = client.get(f"/api/exposure?dealer_sign={sign}&flip_method={flip}")
    assert r.status_code == 200, r.text


# ----------------------------------------------------------------------
# Errors
# ----------------------------------------------------------------------


def test_unknown_symbol_404(client):
    assert client.get("/api/exposure?symbol=NOPE").status_code == 404


def test_bad_expiry_window_400(client):
    r = client.get("/api/exposure?expiry_window=nonsense")
    assert r.status_code == 400
    assert "expiry_window" in r.json()["detail"]


def test_provider_error_becomes_502(client, tmp_path):
    class Broken(MockProvider):
        def get_chain(self, inst, *, max_dte=None):
            raise ProviderError("upstream down")

    set_provider(CachedProvider(Broken(), ChainStore(tmp_path / "b.db")))
    r = client.get("/api/exposure?symbol=SPX")
    assert r.status_code == 502
    assert "upstream down" in r.json()["detail"]


def test_outage_falls_back_to_the_stale_snapshot(client, tmp_path):
    """The store's documented degradation path, end to end."""
    store = ChainStore(tmp_path / "s.db")
    good = CachedProvider(MockProvider(), store)
    set_provider(good)
    assert client.get("/api/exposure?symbol=SPX").status_code == 200

    class Broken(MockProvider):
        def get_chain(self, inst, *, max_dte=None):
            raise ProviderError("upstream down")

    # Same store, dead upstream, and a zero-hour freshness window so the
    # cached chain is always considered too old to serve directly.
    set_provider(CachedProvider(Broken(), store, max_age_hours=0))
    r = client.get("/api/exposure?symbol=SPX")
    assert r.status_code == 200, "should serve stale rather than fail"
    assert r.json()["stale"] is True


# ----------------------------------------------------------------------
# refresh / spot / meta
# ----------------------------------------------------------------------


def test_refresh(client):
    r = client.post("/api/refresh?symbol=SPX")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["contracts"] > 0
    assert body["oi_asof"] is not None


def test_spot_endpoint(client):
    r = client.get("/api/spot?symbol=SPX")
    assert r.status_code == 200
    assert r.json()["spot"] > 0


def test_meta_endpoint(client):
    d = client.get("/api/meta").json()
    assert {i["key"] for i in d["instruments"]} == {"SPX", "SPY", "ES", "NDX", "QQQ", "NQ"}
    assert "spot_solve" in d["flip_methods"]


def test_index_serves_the_dashboard(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "gexdash" in r.text


# ----------------------------------------------------------------------
# SSE
# ----------------------------------------------------------------------


def test_stream_emits_successive_events_with_moving_spot(client):
    events = []
    with client.stream("GET", "/api/stream?symbol=SPX&interval=0.25&max_events=3") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
            while "\n\n" in buf:
                raw, buf = buf.split("\n\n", 1)
                if raw.startswith("data: "):
                    events.append(json.loads(raw[6:]))
            if len(events) >= 3:
                break

    assert len(events) >= 3
    for e in events:
        assert "error" not in e, e
        assert e["spot"] > 0
        assert len(e["gex"]) == len(e["strikes"])
    assert len({e["spot"] for e in events}) > 1, "spot did not move across ticks"


# ----------------------------------------------------------------------
# Latency
# ----------------------------------------------------------------------


def test_full_request_latency_is_stream_viable(client):
    """Measures the endpoint, not the greeks kernel.

    spot_solve does ~121 grid evaluations plus bisection -- on the order of
    140 full chain re-pricings per request -- so the kernel benchmark in
    test_greeks.py says nothing about what the stream can sustain.
    """
    client.get("/api/exposure?symbol=SPX")  # warm the chain cache

    start = time.perf_counter()
    for _ in range(5):
        assert client.get("/api/exposure?symbol=SPX&flip_method=spot_solve").status_code == 200
    per = (time.perf_counter() - start) / 5

    assert per < 2.0, f"{per*1000:.0f} ms per request is too slow for a 2s stream interval"
    print(f"\n  spot_solve request: {per*1000:.0f} ms")


def test_cumulative_flip_is_cheaper_than_spot_solve(client):
    client.get("/api/exposure?symbol=SPX")

    def timed(method):
        start = time.perf_counter()
        for _ in range(3):
            client.get(f"/api/exposure?symbol=SPX&flip_method={method}")
        return (time.perf_counter() - start) / 3

    cum, solve = timed("cumulative"), timed("spot_solve")
    print(f"\n  cumulative {cum*1000:.0f} ms vs spot_solve {solve*1000:.0f} ms")
    assert cum < solve


@pytest.mark.parametrize("symbol", ["SPX", "SPY", "ES", "NDX", "QQQ", "NQ"])
@pytest.mark.parametrize("sign", ["long_calls_short_puts", "short_calls_long_puts", "all_long"])
def test_every_instrument_under_every_dealer_convention(client, symbol, sign):
    """symbol x sign was the untested combination.

    `all_long` makes GEX positive at every strike, so no zero crossing exists
    and the flip comes back null -- which sends the regime classifier down its
    `flip is None` branch. Defensible, but it needs to not error on any
    instrument.
    """
    r = client.get(f"/api/exposure?symbol={symbol}&dealer_sign={sign}&flip_method=spot_solve")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["regime"]["label"] in {"SUPPRESSED", "AMPLIFIED", "TRANSITIONAL"}
    # A null flip must be reported as null, never silently as 0.
    flip = d["levels"]["gamma_flip"]["strike"]
    assert flip is None or flip > 0


# ----------------------------------------------------------------------
# Price history endpoint (terminal view)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("symbol", ["SPX", "SPY", "ES", "NDX", "QQQ", "NQ"])
def test_history_endpoint(client, symbol):
    r = client.get(f"/api/history?symbol={symbol}")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["available"] is True
    n = len(d["close"])
    for k in ("open", "high", "low", "volume", "vwap", "time"):
        assert len(d[k]) == n
    assert d["synthetic"] is True


def test_history_reports_unavailable_without_erroring(client, tmp_path):
    """A provider with no bars must degrade, not 502.

    The exposure surface does not need price history, so a missing feed
    renders a reason where the chart goes rather than failing the request.
    """
    class NoBars(MockProvider):
        get_price_history = None   # drops out of the PriceHistoryProvider check

    set_provider(CachedProvider(NoBars(), ChainStore(tmp_path / "nb.db")))
    r = client.get("/api/history?symbol=SPX")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is False
    assert "does not supply price history" in d["reason"]


def test_history_surfaces_a_typed_unavailable(client, tmp_path):
    from gexdash.providers.base import PriceHistoryUnavailable

    class Refuses(MockProvider):
        def get_price_history(self, inst, **kw):
            raise PriceHistoryUnavailable("futures roots are unverified")

    set_provider(CachedProvider(Refuses(), ChainStore(tmp_path / "rf.db")))
    d = client.get("/api/history?symbol=ES").json()
    assert d["available"] is False and "unverified" in d["reason"]


def test_history_unknown_symbol_404(client):
    assert client.get("/api/history?symbol=NOPE").status_code == 404


def test_meta_lists_both_views(client):
    assert set(client.get("/api/meta").json()["views"]) == {"columns", "terminal"}


def test_index_serves_both_views(client):
    """Both view containers must exist in the document the server ships."""
    html = client.get("/").text
    assert 'id="view-terminal"' in html and 'id="view-columns"' in html
    assert 'id="view-terminal-btn"' in html and 'id="view-columns-btn"' in html
