"""FastAPI app serving the tier-A dashboard.

Endpoints
---------
``GET /``                     the dashboard
``GET /api/exposure``         full exposure surface + levels + diagnostics
``GET /api/spot``             current underlying price
``GET /api/stream``           SSE: spot ticks with the surface recomputed
``POST /api/refresh``         force a fresh chain pull, bypassing the cache

The split matters and mirrors the tier-A architecture: the *chain* is pulled
once a day because open interest is an OCC once-daily figure, while *spot*
streams. Every spot tick re-prices the cached chain -- that is what makes the
whole surface live without a live OI feed, which does not exist at any price.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import exposure, instruments
from .exposure import EXPIRY_WINDOWS, ExposureConfig
from .providers import build_provider
from .sessions import market_phase
from .breadth import FAMILY_EXCHANGE, symbols_for_family
from .providers.base import (
    BreadthProvider,
    BreadthUnavailable,
    PriceHistoryProvider,
    PriceHistoryUnavailable,
    ProviderError,
)
from .store import CachedProvider, ChainStore

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="gexdash", version="0.1.0", docs_url="/api/docs")

_lock = asyncio.Lock()
_provider: CachedProvider | None = None


def get_provider() -> CachedProvider:
    """Lazily construct the provider.

    Deliberately not built at import time: ``ChainStore()`` opens (and
    creates) a database under ``GEXDASH_HOME``, so constructing it on import
    means merely importing this module for a test writes to the real
    ``~/.gexdash``.
    """
    global _provider
    if _provider is None:
        _provider = CachedProvider(build_provider(), ChainStore())
    return _provider


def set_provider(provider: CachedProvider) -> None:
    """Override the provider (tests, or an alternate data source).

    Invalidates the breadth cache: it is keyed by exchange, not by provider,
    so a swap would otherwise keep serving the previous provider's readings
    until the TTL expired.
    """
    global _provider
    _provider = provider
    _breadth_cache.clear()


def _config(
    expiry_window: str,
    dealer_sign: str,
    flip_method: str,
    rate: float,
    div: float,
) -> ExposureConfig:
    if expiry_window not in EXPIRY_WINDOWS:
        raise HTTPException(400, f"expiry_window must be one of {sorted(EXPIRY_WINDOWS)}")
    return ExposureConfig(
        expiry_window=expiry_window,
        dealer_sign=dealer_sign,  # type: ignore[arg-type]
        flip_method=flip_method,  # type: ignore[arg-type]
        risk_free_rate=rate,
        dividend_yield=div,
    )


def _resolve(symbol: str):
    try:
        return instruments.get(symbol)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from None


def _build_payload(inst, cfg: ExposureConfig, *, live_spot: bool = True) -> dict:
    provider = get_provider()
    chain = provider.get_chain(inst, max_dte=60)

    if live_spot:
        try:
            chain = chain.with_spot(provider.get_spot(inst))
        except ProviderError:
            # Fall back to the chain's own underlying print rather than
            # failing the request; the UI flags the staleness.
            pass

    result = exposure.compute(chain, cfg)
    payload = result.to_dict()
    payload["provider"] = provider.name
    payload["synthetic"] = bool(chain.meta.get("synthetic"))
    payload["from_cache"] = bool(chain.meta.get("from_cache"))
    payload["stale"] = bool(chain.meta.get("stale"))
    payload["cache_age_hours"] = chain.meta.get("cache_age_hours")
    payload["server_time"] = datetime.now(timezone.utc).isoformat()
    # Tiles the reference dashboard shows that tier A cannot compute. Sent
    # explicitly so the UI renders them as unavailable rather than blank --
    # an empty box reads as "zero", and a plausible number would be worse.
    payload["unavailable"] = UNAVAILABLE_TILES
    payload["session"] = market_phase()
    payload["instrument_meta"] = {
        "key": inst.key,
        "label": inst.label,
        "multiplier": inst.multiplier,
        "model": inst.model,
        "exercise_style": inst.exercise_style,
        "display_strikes": inst.display_strikes,
        "strike_step": inst.strike_step,
        "note": inst.note,
    }
    return payload


#: Metrics from the reference dashboard that a nightly OI snapshot cannot
#: produce, with the reason. Rendered as explicit placeholders.
UNAVAILABLE_TILES = {
    "oi_flow": "needs classified trade prints (tier B)",
    "volume_bias": "needs classified trade prints (tier B)",
    "intraday_oi_change": "OCC publishes OI once daily - no live feed exists",
    "kinetics": "proprietary label - define your own thresholds (tier D)",
    "dealer_regime": "proprietary label - define your own thresholds (tier D)",
    "zone_state": "proprietary label - define your own thresholds (tier D)",
    "speed_engine": "needs underlying time-and-sales (tier C)",
    "king_maker": "proprietary label (tier D)",
    # Top gainers/losers still needs per-constituent quotes for every index
    # member. Exchange breadth (advancing/declining/TICK/TRIN) does NOT --
    # it comes from the exchange internals symbols and is served by
    # /api/breadth. The two were one tile until the breadth symbols made half
    # of it reachable.
    "movers": "needs per-constituent equity quotes for every index member - a separate feed",
}


def _breadth_source():
    """The underlying provider, unwrapped from CachedProvider."""
    provider = get_provider()
    return getattr(provider, "provider", provider)


#: Server-side breadth cache: {exchange: (fetched_at, payload)}.
#:
#: A client-side timer would not protect the rate limit -- two open tabs
#: double the request rate, and the timer lives in the browser. Breadth moves
#: over minutes, so every client is served from one cached reading.
_BREADTH_TTL_SECONDS = 60.0
_breadth_cache: dict[str, tuple[float, dict]] = {}


@app.get("/api/exposure")
async def api_exposure(
    symbol: str = Query("SPX"),
    expiry_window: str = Query("30dte"),
    dealer_sign: str = Query("long_calls_short_puts"),
    flip_method: str = Query("spot_solve"),
    rate: float = Query(0.042),
    div: float | None = Query(None, description="dividend yield; omit to use the instrument default"),
):
    inst = _resolve(symbol)
    cfg = _config(expiry_window, dealer_sign, flip_method, rate, div)
    async with _lock:
        try:
            return await asyncio.to_thread(_build_payload, inst, cfg)
        except ProviderError as exc:
            raise HTTPException(502, f"provider error: {exc}") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None


@app.get("/api/spot")
async def api_spot(symbol: str = Query("SPX")):
    inst = _resolve(symbol)
    try:
        spot = await asyncio.to_thread(get_provider().get_spot, inst)
    except ProviderError as exc:
        raise HTTPException(502, f"provider error: {exc}") from None
    return {"symbol": inst.key, "spot": spot, "time": datetime.now(timezone.utc).isoformat()}


@app.post("/api/refresh")
async def api_refresh(symbol: str = Query("SPX")):
    inst = _resolve(symbol)
    async with _lock:
        try:
            chain = await asyncio.to_thread(get_provider().get_chain, inst, max_dte=60, force=True)
        except ProviderError as exc:
            raise HTTPException(502, f"provider error: {exc}") from None
    return {
        "symbol": inst.key,
        "contracts": len(chain),
        "spot": chain.spot,
        "oi_asof": chain.oi_asof.isoformat() if chain.oi_asof else None,
        "fetched_at": chain.asof.isoformat(),
    }


@app.get("/api/stream")
async def api_stream(
    request: Request,
    symbol: str = Query("SPX"),
    expiry_window: str = Query("30dte"),
    dealer_sign: str = Query("long_calls_short_puts"),
    flip_method: str = Query("spot_solve"),
    rate: float = Query(0.042),
    div: float | None = Query(None, description="dividend yield; omit to use the instrument default"),
    interval: float = Query(2.0, ge=0.25, le=60.0),
    max_events: int = Query(0, ge=0, description="stop after N events; 0 = unbounded"),
):
    """Server-sent events: re-price the cached chain on each spot tick."""
    inst = _resolve(symbol)
    cfg = _config(expiry_window, dealer_sign, flip_method, rate, div)

    async def gen():
        sent = 0
        while True:
            # Without this the generator outlives its client: a closed tab
            # leaves the loop re-pricing the chain forever, taking _lock on
            # every tick.
            if await request.is_disconnected():
                break
            try:
                async with _lock:
                    payload = await asyncio.to_thread(_build_payload, inst, cfg)
                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as exc:  # keep the stream alive across blips
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"

            sent += 1
            if max_events and sent >= max_events:
                break
            await asyncio.sleep(interval)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/history")
async def api_history(
    symbol: str = Query("SPX"),
    interval_minutes: int = Query(1, ge=1, le=60),
    lookback_hours: float = Query(7.0, gt=0, le=48),
):
    """Intraday OHLCV bars for the terminal view's price panel.

    Deliberately a separate endpoint from /api/exposure rather than folded
    into it: bars change on a different cadence than the exposure surface,
    and doubling the SSE payload every 2 seconds to redraw candles that moved
    by one bar would be waste.

    Returns 200 with ``{"available": false, "reason": ...}`` rather than an
    error status when the provider cannot serve history -- the dashboard is
    fully usable without it, and the UI renders the reason in place of the
    chart.
    """
    inst = _resolve(symbol)
    provider = get_provider()
    source = getattr(provider, "provider", provider)  # unwrap CachedProvider

    if not isinstance(source, PriceHistoryProvider):
        return {
            "available": False,
            "symbol": inst.key,
            "reason": f"the {provider.name} provider does not supply price history",
        }

    def _fetch():
        return source.get_price_history(
            inst, interval_minutes=interval_minutes, lookback_hours=lookback_hours
        )

    try:
        bars = await asyncio.to_thread(_fetch)
    except PriceHistoryUnavailable as exc:
        return {"available": False, "symbol": inst.key, "reason": str(exc)}
    except ProviderError as exc:
        return {"available": False, "symbol": inst.key, "reason": f"provider error: {exc}"}

    payload = bars.to_dict()
    payload["available"] = True
    payload["symbol"] = inst.key
    payload["synthetic"] = bool((bars.meta or {}).get("synthetic"))
    return payload


@app.get("/api/breadth")
async def api_breadth(
    symbol: str = Query("SPX", description="instrument; its family selects the exchange"),
    exchange: str | None = Query(None, description="override: NYSE or NASDAQ"),
    history: int = Query(240, ge=0, le=1000),
):
    """Exchange market internals for the instrument's family.

    Served from a server-side 60s cache shared by every client. Returns 200
    with ``{"available": false, "reason": ...}`` when the provider cannot
    serve breadth -- nothing else on the dashboard depends on it.
    """
    inst = _resolve(symbol)
    if exchange is None:
        family = next((f for f, keys in instruments.FAMILIES.items() if inst.key in keys), None)
        exchange = FAMILY_EXCHANGE.get(family, "NYSE")
    exchange = exchange.upper()
    if exchange not in FAMILY_EXCHANGE.values():
        raise HTTPException(400, f"exchange must be one of {sorted(set(FAMILY_EXCHANGE.values()))}")

    source = _breadth_source()
    if not isinstance(source, BreadthProvider):
        return {"available": False, "exchange": exchange,
                "reason": f"the {get_provider().name} provider does not supply market internals"}

    cached = _breadth_cache.get(exchange)
    now = time.monotonic()
    if cached and now - cached[0] < _BREADTH_TTL_SECONDS:
        payload = dict(cached[1])
        # An explicit flag, not an inferred one: two calls inside the same
        # tenth of a second both round to 0.0s, so a duration cannot signal
        # a cache hit.
        payload["cached"] = True
        payload["cached_seconds"] = round(now - cached[0], 1)
    else:
        try:
            snap = await asyncio.to_thread(source.get_breadth, exchange)
        except BreadthUnavailable as exc:
            return {"available": False, "exchange": exchange, "reason": str(exc)}
        except ProviderError as exc:
            return {"available": False, "exchange": exchange, "reason": f"provider error: {exc}"}

        payload = snap.to_dict()
        payload["available"] = True
        payload["cached"] = False
        payload["cached_seconds"] = 0.0
        payload["symbols"] = symbols_for_family(
            next((f for f, keys in instruments.FAMILIES.items() if inst.key in keys), "S&P 500")
        )[1]
        _breadth_cache[exchange] = (now, payload)

        # Accumulate the session A/D line: point-in-time quotes give the
        # counts, the line has to be built as the session runs.
        try:
            get_provider().store.save_breadth(exchange, snap.advancing, snap.declining, snap.net)
        except Exception:  # a sample failing to persist must not fail the read
            pass

    if history:
        try:
            payload["history"] = get_provider().store.breadth_history(exchange, limit=history)
        except Exception:
            payload["history"] = []
    return payload


@app.get("/api/meta")
async def api_meta():
    return {
        "instruments": [
            {
                "key": i.key,
                "label": i.label,
                "multiplier": i.multiplier,
                "model": i.model,
                "exercise_style": i.exercise_style,
                "note": i.note,
            }
            for i in instruments.REGISTRY.values()
        ],
        "families": instruments.FAMILIES,
        "session": market_phase(),
        "expiry_windows": sorted(EXPIRY_WINDOWS),
        "dealer_signs": ["long_calls_short_puts", "short_calls_long_puts", "all_long"],
        "flip_methods": ["spot_solve", "cumulative"],
        "provider": get_provider().name,
        "views": ["columns", "terminal"],
        "exchanges": FAMILY_EXCHANGE,
    }


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("GEXDASH_HOST", "127.0.0.1"),
        port=int(os.environ.get("GEXDASH_PORT", "8420")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
