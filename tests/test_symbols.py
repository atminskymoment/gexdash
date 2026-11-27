"""Typed symbols: any well-formed ticker resolves, to stated generic specs.

A registered symbol, or one of its provider aliases, must return the verified
spec: getting that wrong would price ES at x100. A typed symbol must carry
exactly the defaults the docs state, and a typed future, whose multiplier is
unknown, must be refused exposure everywhere rather than priced at a guess.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gexdash import instruments
from gexdash.instruments import resolve
from gexdash.providers.mock import MockProvider
from gexdash.server import app, set_provider
from gexdash.store import CachedProvider, ChainStore


@pytest.fixture
def client(tmp_path):
    set_provider(CachedProvider(MockProvider(), ChainStore(tmp_path / "sym.db")))
    with TestClient(app) as c:
        yield c


# ----------------------------------------------------------------------
# resolve()
# ----------------------------------------------------------------------


@pytest.mark.parametrize("typed, key", [
    ("SPX", "SPX"), ("spx", "SPX"), (" qqq ", "QQQ"), ("$SPX", "SPX"), ("$NDX", "NDX"),
    ("/ES", "ES"), ("/nq", "NQ"),
])
def test_registered_symbols_and_their_aliases_return_the_verified_spec(typed, key):
    assert resolve(typed) is instruments.REGISTRY[key]


def test_an_equity_gets_the_stated_generic_specs():
    a = resolve("aapl")
    assert (a.key, a.multiplier, a.model, a.european, a.default_dividend_yield) == ("AAPL", 100.0, "index", False, 0.0)
    assert a.generic and not a.quarterly_rolls and a.exposure_unavailable is None
    assert "generic specs" in a.label
    assert resolve("AAPL") is a                         # one object per symbol
    assert resolve("BRK.B").key == "BRK.B"


def test_a_cash_index_is_european_and_a_future_is_refused_exposure():
    rut = resolve("$rut")
    assert rut.key == "$RUT" and rut.european and rut.multiplier == 100.0 and rut.exposure_unavailable is None
    cl = resolve("/CL")
    assert cl.is_future and cl.generic and not cl.quarterly_rolls
    assert "multiplier" in cl.exposure_unavailable


def test_only_es_and_nq_roll_on_the_quarterly_calendar():
    assert {k for k, i in instruments.REGISTRY.items() if i.quarterly_rolls} == {"ES", "NQ"}


@pytest.mark.parametrize("bad", ["", "NOPE!", "1ABC", "A B", "$$X", "/", "TOOLONGSYMBOL1", "AAPL;DROP"])
def test_malformed_symbols_are_refused_and_get_stays_strict(bad):
    with pytest.raises(KeyError):
        resolve(bad)
    with pytest.raises(KeyError, match="unknown instrument"):
        instruments.get("AAPL")


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


def test_symbol_endpoint_says_what_a_ticker_resolves_to(client):
    es = client.get("/api/symbol", params={"symbol": "/ES"}).json()
    assert es["symbol"] == "ES" and es["registered"] and es["quarterly_rolls"] and es["multiplier"] == 50.0
    aapl = client.get("/api/symbol", params={"symbol": "aapl"}).json()
    assert aapl["generic"] and aapl["exposure"] == {"available": True, "reason": None}
    cl = client.get("/api/symbol", params={"symbol": "/CL"}).json()
    assert cl["exposure"]["available"] is False and cl["multiplier"] is None
    bad = client.get("/api/symbol", params={"symbol": "NOPE!"})
    assert bad.status_code == 404 and "AAPL, BRK.B, $RUT, /CL" in bad.json()["detail"]


def test_a_typed_equity_gets_a_full_exposure_surface(client):
    d = client.get("/api/exposure", params={"symbol": "AAPL"}).json()
    assert d["instrument"] == "AAPL"
    meta = d["instrument_meta"]
    assert meta["generic"] is True and meta["strike_step"] is None and meta["multiplier"] == 100.0
    assert d["strikes"] and min(d["strikes"]) > 0


def test_a_typed_symbol_is_served_from_the_chain_cache_too(client):
    # The first request fetches and caches the chain; later ones rebuild it
    # from the store by key, which once only knew the registered symbols.
    first = client.get("/api/exposure", params={"symbol": "AAPL"})
    again = client.get("/api/exposure", params={"symbol": "AAPL"})
    assert first.status_code == again.status_code == 200
    assert again.json()["instrument"] == "AAPL" and again.json()["instrument_meta"]["generic"] is True
    import json
    with client.stream("GET", "/api/stream", params={"symbol": "AAPL", "max_events": 3, "interval": 0.25}) as r:
        frames = [json.loads(line[5:]) for line in r.iter_lines() if line.startswith("data:")]
    assert len(frames) == 3 and not any("error" in f for f in frames)


def test_a_cheap_typed_symbols_strike_grid_never_reaches_zero(client):
    # Find a symbol the mock anchors under $15, where a fixed step would go negative.
    mock = MockProvider()
    cheap = next(s for s in (f"Z{i}" for i in range(500)) if mock._anchor(resolve(s)) < 15)
    d = client.get("/api/exposure", params={"symbol": cheap}).json()
    assert min(d["strikes"]) > 0


def test_typed_symbols_draw_different_synthetic_series(client):
    a = client.get("/api/history", params={"symbol": "AAPL", "range": "1Y", "interval": "1D"}).json()
    b = client.get("/api/history", params={"symbol": "MSFT", "range": "1Y", "interval": "1D"}).json()
    assert a["available"] and b["available"]
    assert a["close"][-1] != b["close"][-1]
    assert a["close"][:5] != b["close"][:5]


def test_a_typed_future_is_refused_exposure_before_any_stream_opens(client):
    for path in ("/api/exposure", "/api/stream"):
        r = client.get(path, params={"symbol": "/CL", "max_events": 1})
        assert r.status_code == 422 and "multiplier" in r.json()["detail"]
    assert client.post("/api/refresh", params={"symbol": "/CL"}).status_code == 422


def test_a_typed_future_serves_bars_unadjusted_and_says_so(client):
    h = client.get("/api/history", params={"symbol": "/CL", "range": "1Y", "interval": "1D"}).json()
    assert h["available"] and h["adjusted"] is False and h["rolls"] == []
    assert any("not adjusted" in n for n in h["plan"]["notes"])
    t = client.get("/api/technicals", params={"symbol": "/CL", "range": "1Y", "interval": "1D",
                                              "astro": "true"}).json()
    assert t["available"] and t["future"] is True and t["quarterly_rolls"] is False
    assert t["adjusted"] is False and t["rolls"] == []
    assert any("not adjusted" in n for n in t["notes"])
    # No quarterly roll days are dropped from a calendar /CL does not follow.
    spy = client.get("/api/technicals", params={"symbol": "SPY", "range": "1Y", "interval": "1D",
                                                "astro": "true"}).json()
    assert t["astro"]["model"]["days"] == spy["astro"]["model"]["days"]


def test_registered_futures_still_adjust_by_default(client):
    t = client.get("/api/technicals", params={"symbol": "/ES", "range": "2Y", "interval": "1D"}).json()
    assert t["symbol"] == "ES" and t["adjusted"] is True and t["quarterly_rolls"] is True


def test_typed_symbols_take_their_internals_from_the_nyse(client):
    b = client.get("/api/breadth", params={"symbol": "AAPL"}).json()
    assert b.get("exchange", "NYSE") == "NYSE"
