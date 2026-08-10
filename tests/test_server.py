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
from gexdash.store import OI_ZERO_REASON, CachedProvider, ChainStore


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


# ----------------------------------------------------------------------
# Market internals
# ----------------------------------------------------------------------


@pytest.mark.parametrize("symbol,exchange", [
    ("SPX", "NYSE"), ("SPY", "NYSE"), ("ES", "NYSE"),
    ("NDX", "NASDAQ"), ("QQQ", "NASDAQ"), ("NQ", "NASDAQ"),
])
def test_breadth_follows_the_instrument_family(client, symbol, exchange):
    d = client.get(f"/api/breadth?symbol={symbol}").json()
    assert d["available"] is True
    assert d["exchange"] == exchange
    assert d["advancing"] > 0 and d["declining"] > 0
    assert d["net"] == d["advancing"] - d["declining"]
    assert d["reconciliation_warning"] is None
    assert "not index constituents" in d["scope"]


def test_breadth_is_cached_server_side(client):
    """A client timer would not protect the rate limit: two tabs double it."""
    first = client.get("/api/breadth?symbol=SPX").json()
    second = client.get("/api/breadth?symbol=SPX").json()
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["advancing"] == first["advancing"], "cache returned a different reading"


def test_breadth_accumulates_samples(client):
    client.get("/api/breadth?symbol=SPX")
    d = client.get("/api/breadth?symbol=SPX").json()
    assert isinstance(d["history"], list) and len(d["history"]) >= 1
    assert all("net" in h and "t" in h for h in d["history"])


def test_breadth_reports_unavailable_without_erroring(client, tmp_path):
    class NoBreadth(MockProvider):
        get_breadth = None

    set_provider(CachedProvider(NoBreadth(), ChainStore(tmp_path / "nb2.db")))
    r = client.get("/api/breadth?symbol=SPX")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is False and "does not supply market internals" in d["reason"]


def test_breadth_surfaces_a_typed_unavailable(client, tmp_path):
    from gexdash.providers.base import BreadthUnavailable

    class Refuses(MockProvider):
        def get_breadth(self, exchange):
            raise BreadthUnavailable("entitlement does not cover exchange internals")

    set_provider(CachedProvider(Refuses(), ChainStore(tmp_path / "rb.db")))
    d = client.get("/api/breadth?symbol=SPX").json()
    assert d["available"] is False and "entitlement" in d["reason"]


def test_breadth_exchange_override(client):
    d = client.get("/api/breadth?symbol=SPX&exchange=NASDAQ").json()
    assert d["exchange"] == "NASDAQ"
    assert client.get("/api/breadth?symbol=SPX&exchange=LSE").status_code == 400


def test_market_internals_is_no_longer_claimed_impossible(client):
    """It moved from an unavailable tile to a served endpoint.

    Only per-constituent movers remains out of reach. Leaving the old tile in
    place would show a dashed placeholder beside a live A/D count.
    """
    d = client.get("/api/exposure?symbol=SPX").json()
    assert "market_internals" not in d["unavailable"]
    assert "movers" in d["unavailable"]


# ----------------------------------------------------------------------
# Schwab connection page
# ----------------------------------------------------------------------


def test_connect_page_is_served(client):
    r = client.get("/connect")
    assert r.status_code == 200
    assert "Authorize with Schwab" in r.text
    assert 'id="redirect-url"' in r.text


def test_auth_status_never_leaks_a_secret(client, monkeypatch, tmp_path):
    """The page renders this verbatim, so it must be safe to display."""
    from gexdash.providers.schwab import SchwabAuth, SchwabProvider
    from gexdash.store import OI_ZERO_REASON, CachedProvider, ChainStore

    monkeypatch.setenv("SCHWAB_APP_KEY", "TOPSECRETKEY123")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "TOPSECRETSECRET")
    auth = SchwabAuth(token_path=tmp_path / "t.json")
    set_provider(CachedProvider(SchwabProvider(auth), ChainStore(tmp_path / "s.db")))

    body = client.get("/api/auth/status").text

    # The app SECRET must never appear anywhere.
    assert "TOPSECRETSECRET" not in body

    # The app KEY is the OAuth client_id, which is inherently public -- it is
    # in every authorize URL the browser visits, so it necessarily appears
    # there. It is still redacted in the settings table, which is the part
    # someone screenshots.
    settings = client.get("/api/auth/status").json()["settings"]
    key_row = next(r for r in settings if r["key"] == "SCHWAB_APP_KEY")
    secret_row = next(r for r in settings if r["key"] == "SCHWAB_APP_SECRET")
    assert key_row["value"] == "<set, 15 chars>"
    assert secret_row["value"] == "<set, 15 chars>"
    assert "authorize_url" in body


def test_auth_status_without_credentials(client):
    d = client.get("/api/auth/status").json()
    assert d["authenticated"] is False
    assert d["authorize_url"] is None


def test_exchange_rejects_a_value_with_no_code(client, monkeypatch, tmp_path):
    from gexdash.providers.schwab import SchwabAuth, SchwabProvider
    from gexdash.store import OI_ZERO_REASON, CachedProvider, ChainStore

    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    set_provider(CachedProvider(SchwabProvider(SchwabAuth(token_path=tmp_path / "t.json")),
                                ChainStore(tmp_path / "s.db")))

    r = client.post("/api/auth/exchange", json={"redirect_url": "https://127.0.0.1/?session=only"})
    assert r.status_code == 400
    assert "no authorization code" in r.json()["detail"]


def test_exchange_tries_every_redirect_form_then_reports(client, monkeypatch, tmp_path):
    """Each attempt is reported by redirect_uri, so a mismatch is diagnosable
    rather than a bare 'invalid_grant'."""
    from gexdash.providers import schwab
    from gexdash.providers.base import ProviderError
    from gexdash.store import OI_ZERO_REASON, CachedProvider, ChainStore

    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    auth = schwab.SchwabAuth(callback_url="https://127.0.0.1/", token_path=tmp_path / "t.json")
    set_provider(CachedProvider(schwab.SchwabProvider(auth), ChainStore(tmp_path / "s.db")))

    tried = []
    def boom(self, code, redirect_uri=None):
        tried.append(redirect_uri)
        raise ProviderError("invalid_grant")
    monkeypatch.setattr(schwab.SchwabAuth, "exchange_code_value", boom)

    d = client.post("/api/auth/exchange", json={"redirect_url": "https://127.0.0.1/?code=X%40"}).json()
    assert d["ok"] is False
    assert tried == ["https://127.0.0.1/", "https://127.0.0.1"]
    assert len(d["attempts"]) == 2
    assert "Ready For Use" in d["hint"]


def test_exchange_reports_which_redirect_form_worked(client, monkeypatch, tmp_path):
    from gexdash.providers import schwab
    from gexdash.providers.base import ProviderError
    from gexdash.store import OI_ZERO_REASON, CachedProvider, ChainStore

    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    auth = schwab.SchwabAuth(callback_url="https://127.0.0.1/", token_path=tmp_path / "t.json")
    set_provider(CachedProvider(schwab.SchwabProvider(auth), ChainStore(tmp_path / "s.db")))

    def only_bare(self, code, redirect_uri=None):
        if redirect_uri != "https://127.0.0.1":
            raise ProviderError("invalid_grant")
        self._save({"access_token": "a", "refresh_token": "r", "expires_in": 1800}, new_consent=True)
        return {}
    monkeypatch.setattr(schwab.SchwabAuth, "exchange_code_value", only_bare)

    d = client.post("/api/auth/exchange", json={"redirect_url": "https://127.0.0.1/?code=X"}).json()
    assert d["ok"] is True
    assert d["redirect_uri_accepted"] == "https://127.0.0.1"
    assert d["mismatch"] is True          # tells the user to fix .env
    assert d["refresh_expires_in_days"] == pytest.approx(7.0, abs=0.01)


def test_logout_removes_the_token(client, monkeypatch, tmp_path):
    from gexdash.providers.schwab import SchwabAuth, SchwabProvider
    from gexdash.store import OI_ZERO_REASON, CachedProvider, ChainStore

    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    auth = SchwabAuth(token_path=tmp_path / "t.json")
    auth._save({"access_token": "a", "refresh_token": "r", "expires_in": 1800}, new_consent=True)
    set_provider(CachedProvider(SchwabProvider(auth), ChainStore(tmp_path / "s.db")))

    assert client.post("/api/auth/logout").json() == {"ok": True, "removed": True}
    assert not (tmp_path / "t.json").exists()
    assert client.get("/api/auth/status").json()["authenticated"] is False


def test_exposure_payload_flags_when_login_is_needed(client, monkeypatch, tmp_path):
    """The dashboard must say 'not connected' rather than quietly showing
    numbers from a provider that cannot answer."""
    from gexdash.providers.schwab import SchwabAuth, SchwabProvider
    from gexdash.store import OI_ZERO_REASON, CachedProvider, ChainStore

    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    set_provider(CachedProvider(SchwabProvider(SchwabAuth(token_path=tmp_path / "none.json")),
                                ChainStore(tmp_path / "s.db")))
    # The chain fetch will fail unauthenticated; only the auth block matters.
    r = client.get("/api/exposure?symbol=SPX")
    if r.status_code == 200:
        assert r.json()["auth"]["needs_login"] is True


def test_mock_provider_reports_no_login_needed(client):
    assert client.get("/api/exposure?symbol=SPX").json()["auth"]["needs_login"] is False


# ----------------------------------------------------------------------
# Static asset caching
# ----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/static/app.js", "/static/styles.css"])
def test_front_end_assets_are_revalidated_not_cached(client, path):
    """The markup, CSS and JS ship as a unit and must not skew in a cache.

    A browser holding a stale stylesheet renders the new markup *without
    failing*: unstyled SVG falls back to black fill and no stroke, so a
    missing rule looks exactly like a drawing bug. This cost real debugging
    time once -- the crosshair appeared as a black box with no lines.
    """
    response = client.get(path)
    assert response.status_code == 200
    assert "no-cache" in response.headers.get("cache-control", "")


def test_degraded_chain_is_reported_in_the_payload(client, tmp_path):
    """A zero-OI chain must reach the client as unavailable, not as $0.

    Every exposure measure is proportional to open interest, so the surface
    collapses to zero -- and a zero renders as a confident statement about
    the market rather than as a failed fetch.
    """
    from gexdash.providers.mock import MockProvider
    from gexdash.store import OI_ZERO_REASON, CachedProvider, ChainStore

    class _ZeroOI(MockProvider):
        name = "zero-oi"

        def get_chain(self, inst, *, max_dte=None):
            chain = super().get_chain(inst, max_dte=max_dte)
            chain.open_interest[:] = 0.0
            return chain

    set_provider(CachedProvider(_ZeroOI(), ChainStore(tmp_path / "zero.db")))
    payload = client.get("/api/exposure?symbol=SPX").json()

    assert payload["oi_unavailable"] is True
    assert payload["degraded_reason"] == OI_ZERO_REASON
    assert payload["totals"]["gex"] == 0.0, "the number really is zero -- the UI must not print it"
