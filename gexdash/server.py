"""FastAPI app serving the tier-A dashboard.

Endpoints
---------
``GET /``                     the dashboard (Terminal, Columns and Chart views)
``GET /connect``              the Schwab connect page
``GET /api/meta``             instruments, families, chart capabilities
``GET /api/symbol``           what a typed ticker resolves to (any symbol, generic specs if unregistered)
``GET /api/exposure``         full exposure surface + levels + diagnostics
``GET /api/spot``             current underlying price
``GET /api/stream``           SSE: spot ticks with the surface recomputed
``POST /api/refresh``         force a fresh chain pull, bypassing the cache
``GET /api/history``          the Terminal's candles + RSI/MACD/ADX, 1-minute to monthly
``GET /api/breadth``          exchange internals: advancing/declining, TICK, TRIN, A/D line
``GET /api/technicals``       the Chart view, 1-minute to weekly: averages, bands, profile,
                              signals, Supertrend, futures roll adjustment

``astro=true`` on either chart endpoint adds the lunar-solar cycle (``cycles.py``).
``GET /api/auth/status``      Schwab token state; ``POST /api/auth/exchange`` and
                              ``POST /api/auth/logout`` complete or drop it

``docs/api.md`` documents each one; ``/api/docs`` serves the OpenAPI view.

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

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import cycles, exposure, futures, instruments, interpretation, technicals
from .exposure import EXPIRY_WINDOWS, ExposureConfig
from .providers import build_provider
from .sessions import market_phase, trim_sessions
from .breadth import FAMILY_EXCHANGE, symbols_for_family
from .history import capability_matrix, resolve as resolve_plan
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
    _tech_cache.clear()


def _config(
    expiry_window: str,
    dealer_sign: str,
    flip_method: str,
    rate: float,
    div: float,
) -> ExposureConfig:
    """The exposure config for a request.

    Only ``expiry_window`` is validated here (400). An unknown ``dealer_sign``
    fails later, in ``exposure.compute``; an unknown ``flip_method`` is
    treated as ``spot_solve``.
    """
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
    """The instrument for ``symbol``, or a 404 for a malformed one.

    Any well-formed symbol resolves (``instruments.resolve``): a registered
    one to its verified spec, anything else to stated generic specs. Whether
    the provider actually has it is the provider's answer, given where the
    data would have been.
    """
    try:
        return instruments.resolve(symbol)
    except KeyError as exc:
        raise HTTPException(404, exc.args[0] if exc.args else str(exc)) from None


def _require_exposure(inst) -> None:
    """422 for an instrument whose exposure cannot be computed (a typed
    future: its multiplier is unknown). Checked before any work, and before
    ``/api/stream`` opens, so a refused symbol is one error, not one a tick."""
    if inst.exposure_unavailable:
        raise HTTPException(422, inst.exposure_unavailable)


def _build_payload(inst, cfg: ExposureConfig, *, live_spot: bool = True) -> dict:
    """The ``/api/exposure`` payload, shared with ``/api/stream``.

    Reads the cached chain (expiries up to 60 days out), re-prices it at live
    spot unless ``live_spot`` is False -- keeping the chain's own spot if the
    quote raises ProviderError -- and adds provenance, cache and auth state,
    instrument metadata and the written reading. Blocking; run it in a thread.
    """
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
    # Why the data is degraded, when it is. Distinct from `stale`: a chain can
    # be stale because a refresh failed, or because the refresh succeeded and
    # returned a chain with no open interest in it.
    payload["degraded_reason"] = chain.meta.get("degraded_reason")
    payload["oi_unavailable"] = bool(chain.meta.get("oi_unavailable"))
    payload["server_time"] = datetime.now(timezone.utc).isoformat()
    # Tiles the reference dashboard shows that tier A cannot compute. Sent
    # explicitly so the UI renders them as unavailable rather than blank --
    # an empty box reads as "zero", and a plausible number would be worse.
    payload["unavailable"] = UNAVAILABLE_TILES
    payload["session"] = market_phase()

    # So the dashboard can say "not connected" and link to /connect rather
    # than just showing mock data that looks real.
    source = getattr(provider, "provider", provider)
    auth = getattr(source, "auth", None)
    payload["auth"] = (
        {"provider": provider.name, "needs_login": False}
        if auth is None
        else {
            "provider": provider.name,
            "configured": auth.configured,
            "needs_login": auth.configured and not auth.status().get("authenticated"),
            "refresh_expires_in_days": auth.status().get("refresh_expires_in_days"),
        }
    )
    payload["instrument_meta"] = {
        "key": inst.key,
        "label": inst.label,
        "multiplier": inst.multiplier,
        "model": inst.model,
        "exercise_style": inst.exercise_style,
        "display_strikes": inst.display_strikes,
        # A typed symbol's strikes come from its chain; there is no spec step.
        "strike_step": None if inst.generic else inst.strike_step,
        "note": inst.note,
        "generic": inst.generic,
    }
    # Built last: it reads the assembled payload, so what is interpreted is
    # exactly what the client is sent.
    payload["interpretation"] = interpretation.build(payload)
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
    """Full exposure surface for one instrument, with levels, regime and a written reading.

    The cached chain snapshot (refetched once it is 12 hours old; expiries up to 60 days out) is
    re-priced at a fresh spot quote, or at the chain's own underlying price if the provider returns an
    error for the quote. `expiry_window` picks which of those expiries count, so `all` means all within
    60 days; `dealer_sign` is the assumed dealer positioning; `flip_method` is how the gamma flip is
    located; `rate` and `div` are pricing inputs (omit `div` to use the instrument's own dividend
    yield). The active choices are echoed in `diagnostics`.

    Per-strike arrays are aligned with `strikes`. Units: GEX $ delta per +1% spot, DEX $ delta, VANX
    $ delta per vol point, CEX $ delta per day, VEX $ P&L per vol point, THEX $ P&L per day.
    `stale` and `degraded_reason` say when the chain is not a clean current fetch; with
    `oi_unavailable` the exposure figures must be shown as unavailable, not as zero.

    Errors: 400 unknown `expiry_window`; 404 unknown `symbol`; 422 no live contracts in the window
    (or an unknown `dealer_sign`); 502 provider failure with no cached chain to fall back on.
    """
    inst = _resolve(symbol)
    _require_exposure(inst)
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
    """Current underlying price, straight from the provider (spot is never cached).

    Returns `symbol`, `spot` and `time` (server time, UTC). 404 unknown `symbol`; 502 provider error.
    """
    inst = _resolve(symbol)
    try:
        spot = await asyncio.to_thread(get_provider().get_spot, inst)
    except ProviderError as exc:
        raise HTTPException(502, f"provider error: {exc}") from None
    return {"symbol": inst.key, "spot": spot, "time": datetime.now(timezone.utc).isoformat()}


@app.post("/api/refresh")
async def api_refresh(symbol: str = Query("SPX")):
    """Force a chain pull, bypassing the 12-hour cache and the empty-chain retry back-off.

    Rarely needed, since open interest changes once a day. If the pull fails or comes back without
    open interest, the last good snapshot stays in service and is what this summarises: `fetched_at`
    says which chain is now being served. 404 unknown `symbol`; 502 provider failure with no snapshot
    to fall back on.
    """
    inst = _resolve(symbol)
    _require_exposure(inst)
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
    _require_exposure(inst)
    cfg = _config(expiry_window, dealer_sign, flip_method, rate, div)

    async def gen():
        """One ``data:`` frame per tick until the client goes or ``max_events``
        is reached. A failed tick is sent as ``{"error": ...}`` and counted."""
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
    range: str = Query("1D", description="1D 5D 10D 1M 3M 6M YTD 1Y 2Y 5Y 10Y"),
    interval: str = Query("1m", description="1m 5m 10m 15m 30m 1h 2h 4h 1D 1W 1Mo"),
    indicators: bool = Query(True),
    adjust: bool = Query(True, description="futures: adjust for contract changes, as thinkorswim does"),
    astro: bool = Query(False, description="add the lunar-solar cycle, its derivatives, projection and events"),
    # Legacy form, kept so existing callers keep working.
    interval_minutes: int | None = Query(None, deprecated=True),
    lookback_hours: float | None = Query(None, deprecated=True),
):
    """OHLCV bars, optionally with RSI / MACD / ADX.

    Separate from /api/exposure on purpose: bars change on a different
    cadence, and re-sending them on every SSE tick would be waste.

    Returns 200 with ``{"available": false, "reason": ...}`` when the provider
    cannot serve history -- the rest of the dashboard is unaffected.

    ``astro=true`` adds the lunar-solar ``astro`` block, as on
    ``/api/technicals``, indexed like these bars.
    """
    inst = _resolve(symbol)
    provider = get_provider()
    source = getattr(provider, "provider", provider)

    if not isinstance(source, PriceHistoryProvider):
        return {"available": False, "symbol": inst.key,
                "reason": f"the {provider.name} provider does not supply price history"}

    legacy = interval_minutes is not None or lookback_hours is not None
    plan = None
    if not legacy:
        try:
            plan = resolve_plan(range, interval)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from None

    def _fetch():
        if plan is not None:
            return source.get_price_history(inst, plan=plan)
        return source.get_price_history(
            inst, interval_minutes=interval_minutes or 1, lookback_hours=lookback_hours or 7.0
        )

    try:
        bars = await asyncio.to_thread(_fetch)
    except PriceHistoryUnavailable as exc:
        return {"available": False, "symbol": inst.key, "reason": str(exc),
                "plan": plan.to_dict() if plan else None}
    except ProviderError as exc:
        return {"available": False, "symbol": inst.key, "reason": f"provider error: {exc}"}

    # Session-counted ranges: the window over-fetched, keep the last N
    # sessions -- before VWAP, the session range and the indicators see them.
    if plan is not None and plan.sessions:
        bars = trim_sessions(bars, plan.sessions, 6 if inst.is_future else 4)
    rolls: list[dict] = []
    # Only the quarterly equity-index calendar is known (ES, NQ); a typed
    # future such as /CL is served as the feed sends it.
    adjusted = bool(inst.quarterly_rolls and adjust)
    if adjusted:
        bars, rolls = futures.back_adjust(bars)     # a copy; the feed's bars are untouched

    payload = bars.to_dict()
    payload["available"] = True
    payload["symbol"] = inst.key
    payload["synthetic"] = bool((bars.meta or {}).get("synthetic"))
    payload["adjusted"] = adjusted
    payload["rolls"] = rolls
    if plan is not None:
        payload["plan"] = plan.to_dict()
        # The terminal chart shows plan notes, not meta: a futures series
        # served unverified must say so where it is drawn.
        lead = []
        if payload["meta"].get("unverified"):
            lead.append(payload["meta"]["unverified"])
        if inst.quarterly_rolls:
            lead.append(futures.adjust_note(inst.quote_symbol, adjusted=adjusted, rolls=rolls))
        elif inst.is_future:
            lead.append(_UNROLLED_NOTE.format(symbol=inst.quote_symbol))
        payload["plan"]["notes"] = [*lead, *payload["plan"]["notes"]]

    if indicators and len(bars) > 1:
        from .indicators import compute_all
        payload["indicators"] = await asyncio.to_thread(
            compute_all, bars.high, bars.low, bars.close
        )
    if astro and len(bars):
        from .history import INTERVALS
        served = INTERVALS.get(plan.served_interval_key or plan.interval_key) if plan is not None else None
        kind = served.kind if served else "minute"
        minutes = served.minutes if served else bars.interval_minutes
        payload["astro"] = await _astro_block(inst, source, bar_stamps=bars.time, kind=kind,
                                              minutes=minutes, last_close=float(bars.close[-1]))
    return payload


#: Server-side bar cache for the technical chart, keyed by what was actually
#: fetched: (symbol, frequency type, native frequency, extended hours, days).
#: Each bar size fetches one fixed window, so every range of it is cut from
#: the same request -- 1Y -> 2Y -> 3M on one symbol is one provider call --
#: and 30m, 1h and 4h over the same session share their 30-minute fetch.
_TECH_TTL_SECONDS = 60.0
_tech_cache: dict[tuple, tuple[float, object]] = {}

#: Said wherever a typed future is drawn: its contract changes are steps.
_UNROLLED_NOTE = ("{symbol}: contract changes are not adjusted. gexdash knows the roll calendar "
                  "only for ES and NQ, so each roll may show as a step.")


class _NoBars(Exception):
    """The provider could not serve bars; carries the reason for the payload."""


async def _tech_bars(inst, source, plan) -> tuple[object, float]:
    """Bars for a plan, from the cache when fresh. Returns (bars, age)."""
    days = round((plan.end - plan.start).total_seconds() / 86400) if plan.start else 0
    key = (inst.key, plan.frequency_type, plan.frequency, plan.extended_hours, days)
    ttl = technicals.INTRADAY_TTL_SECONDS if plan.frequency_type == "minute" else _TECH_TTL_SECONDS
    cached = _tech_cache.get(key)
    now = time.monotonic()
    if cached and now - cached[0] < ttl:
        return cached[1], now - cached[0]
    try:
        bars = await asyncio.to_thread(source.get_price_history, inst, plan=plan)
    except PriceHistoryUnavailable as exc:
        raise _NoBars(str(exc)) from None
    except ProviderError as exc:
        raise _NoBars(f"provider error: {exc}") from None
    _tech_cache[key] = (now, bars)
    return bars, 0.0


async def _astro_block(inst, source, *, bar_stamps, kind: str, minutes: int | None,
                       last_close: float) -> dict:
    """The lunar-solar cycle for one chart (``cycles.py``), or a reason.

    The fit is always on the symbol's daily history, the Chart view's
    10-year fetch, shared through ``_tech_bars`` with the daily chart and the
    intraday 3M/52W levels. Futures are fitted on the raw front-month closes
    with each roll bar's return left out, whatever the chart's own adjust
    toggle says: a contract change is not a return, and every other day's
    return is exact. Back-adjusting first would not help. The additive
    adjustment shifts each older segment by a constant, which rescales every
    earlier return.
    """
    daily_plan, _ = technicals.plan_for("1Y", "1D")
    try:
        daily, _ = await _tech_bars(inst, source, daily_plan)
    except _NoBars as exc:
        return {"available": False, "reason": f"daily history did not load: {exc}"}
    skip: set[int] = set()
    if inst.quarterly_rolls:
        _, rolls = futures.back_adjust(daily)       # positions only; the closes stay raw
        skip = {r["index"] for r in rolls}
    return await asyncio.to_thread(
        cycles.compute, daily.time, daily.close, bar_stamps=bar_stamps, kind=kind,
        minutes=minutes, last_close=last_close, skip=frozenset(skip),
    )


@app.get("/api/technicals")
async def api_technicals(
    symbol: str = Query("QQQ"),
    range: str = Query("1Y", description=" ".join(technicals.TECH_RANGES)),
    interval: str = Query("1D", description=" ".join(technicals.TECH_INTERVALS)),
    session: str = Query("auto", pattern="^(auto|rth|eth)$",
                         description="intraday only: rth (ETF default), eth; futures are always full-session"),
    st_period: int = Query(10, ge=1, le=200, description="Supertrend ATR period"),
    st_mult: float = Query(3.0, gt=0, le=20, description="Supertrend ATR multiplier"),
    adjust: bool = Query(True, description="futures: adjust for contract changes, as thinkorswim does"),
    astro: bool = Query(False, description="add the lunar-solar cycle, its derivatives, projection and events"),
):
    """The technical chart: SMMA 20/50/100/200, EMA 9, Bollinger 20/2 and
    20/3, RSI, MACD, volume and volume profile, 3M/52W levels, ranges, the
    gexdash VAMS / BUY-ADD-TRIM-SELL rules and Supertrend, on intraday,
    daily or weekly bars.

    Indicators are computed over more history than is shown -- the feed's
    full history for daily and weekly, the longest range plus ~1,000 bars
    intraday -- and trimmed to ``range`` afterwards, so long averages are
    converged at the left edge (and the payload says where they are not). A
    range the bar size does not offer is replaced by its default, with a
    note. Returns 200 with ``{"available": false, "reason": ...}`` when the
    provider cannot serve bars.

    ``astro=true`` adds an ``astro`` block (``cycles.py``): the lunar-solar
    cycle and its two derivatives per bar, future slots past the last bar
    with the projected path and its highs and lows, and the Moon and Sun
    events in the window. Its rules join ``rules`` and its notes ``notes``.
    """
    inst = _resolve(symbol)
    iv = technicals.TECH_INTERVALS.get(interval)
    if iv is None:
        raise HTTPException(400, f"unknown interval {interval!r}; known: {list(technicals.TECH_INTERVALS)}")
    if range not in technicals.TECH_RANGES:
        raise HTTPException(400, f"unknown range {range!r}; known: {list(technicals.TECH_RANGES)}")
    notes: list[str] = []
    if range not in iv.ranges:
        notes.append(f"{range} is not offered on {iv.label} bars; showing {iv.default_range}.")
        range = iv.default_range
    kind, session_notes = technicals.session_kind(interval, is_future=inst.is_future, requested=session)
    notes += session_notes
    plan, display_start = technicals.plan_for(range, interval, session=kind)

    provider = get_provider()
    source = getattr(provider, "provider", provider)
    if not isinstance(source, PriceHistoryProvider):
        return {"available": False, "symbol": inst.key,
                "reason": f"the {provider.name} provider does not supply price history"}

    try:
        bars, age = await _tech_bars(inst, source, plan)
    except _NoBars as exc:
        return {"available": False, "symbol": inst.key, "reason": str(exc)}

    daily = None
    if iv.intraday:
        if iv.minutes != iv.native:
            bars = await asyncio.to_thread(technicals.aggregate_session, bars, iv.minutes, kind)
        # The 3M/52W levels are daily-bar levels whatever the chart's bar size.
        daily_plan, _ = technicals.plan_for("1Y", "1D")
        try:
            daily, _ = await _tech_bars(inst, source, daily_plan)
        except _NoBars:
            daily = None

    if len(bars) < 2:
        return {"available": False, "symbol": inst.key,
                "reason": f"only {len(bars)} bar(s) returned for {inst.key}"}

    # Contract changes: adjusted copies, never the cached series itself. The
    # daily bars behind the intraday 3M/52W levels get the same treatment.
    rolls: list[dict] = []
    adjusted = bool(inst.quarterly_rolls and adjust)
    if adjusted:
        bars, rolls = futures.back_adjust(bars)
        if daily is not None:
            daily, _ = futures.back_adjust(daily)
    elif inst.quarterly_rolls:
        _, rolls = futures.back_adjust(bars)    # positions only: where the series steps

    display_start = technicals.display_start_for(range, bars, session=kind, fallback=display_start)
    params = technicals.TechParams(st_period=st_period, st_mult=st_mult)
    payload = await asyncio.to_thread(
        technicals.compute, bars, display_start=display_start, interval=interval,
        params=params, session=kind, daily=daily,
    )
    meta = bars.meta or {}
    notes += payload["warmup"]["notes"]
    first = payload["warmup"]["bars_before_window"]
    shown = [r for r in rolls if r["index"] >= first]
    if inst.quarterly_rolls:
        # About the rolls on screen; the warm-up's older ones move the long
        # averages' left edge only slightly.
        notes.insert(0, futures.adjust_note(inst.quote_symbol, adjusted=adjusted, rolls=shown))
    elif inst.is_future:
        notes.insert(0, _UNROLLED_NOTE.format(symbol=inst.quote_symbol))
    if inst.generic:
        notes.insert(0, f"{inst.label}: not one of gexdash's registered instruments.")
    if meta.get("unverified"):
        notes.insert(0, meta["unverified"])
    if astro:
        block = await _astro_block(inst, source, bar_stamps=bars.time[first:], kind=iv.kind,
                                   minutes=iv.minutes, last_close=payload["close"][-1])
        payload["astro"] = block
        if block.get("available"):
            notes += block["notes"]
            payload["rules"] = [*payload["rules"], *block["rules"]]
        else:
            notes.append(f"Lunar-solar cycle unavailable: {block.get('reason')}")
    payload.update({
        "available": True,
        "symbol": inst.key,
        "label": inst.label,
        "future": inst.is_future,
        "quarterly_rolls": inst.quarterly_rolls,
        "generic": inst.generic,
        "range": range,
        "adjusted": adjusted,
        # Contract changes inside the window, indexed like the bars.
        "rolls": [{**r, "i": r["index"] - first} for r in shown],
        "synthetic": bool(meta.get("synthetic")),
        "provider": provider.name,
        "cached_seconds": round(age, 1),
        "notes": notes,
        "plan": plan.to_dict(),
        "caps": technicals.capabilities(),
    })
    return payload


# ---------------------------------------------------------------------
# Schwab connection
#
# The CLI login works but forces a terminal round-trip, and the
# authorization code is short-lived and single-use -- every second spent
# switching windows is a second it ages. This does the exchange from the
# browser tab that already has the redirect URL in it.
#
# Deliberately not requiring the loopback listener: that needs port 443
# (root) or a re-registered callback. This works with whatever callback the
# Schwab app already has.
# ---------------------------------------------------------------------


def _schwab_auth():
    """The live provider's auth object when possible, else a fresh one.

    Reusing the cached provider's instance means a successful exchange
    updates its in-memory token too, so the very next request is authorised
    without waiting for a re-read.
    """
    from .providers.schwab import SchwabAuth

    source = getattr(get_provider(), "provider", None)
    auth = getattr(source, "auth", None)
    return auth if auth is not None else SchwabAuth()


@app.get("/api/auth/status")
async def api_auth_status():
    """Credential and token state. Never returns secret material."""
    from .config import settings_report

    auth = _schwab_auth()
    status = auth.status()
    status["provider"] = get_provider().name
    status["authorize_url"] = auth.authorize_url() if auth.configured else None
    status["redirect_candidates"] = auth.redirect_candidates() if auth.configured else []
    status["settings"] = [
        {"key": k, "value": v, "source": src} for k, v, src in settings_report()
    ]
    return status


@app.post("/api/auth/exchange")
async def api_auth_exchange(payload: dict = Body(...)):
    """Exchange a pasted redirect URL (or bare code) for tokens."""
    from .providers.schwab import _extract_code

    auth = _schwab_auth()
    if not auth.configured:
        raise HTTPException(400, "SCHWAB_APP_KEY / SCHWAB_APP_SECRET are not set")

    code = _extract_code(str(payload.get("redirect_url", "")))
    if not code:
        raise HTTPException(400, "no authorization code found in that value")

    # Schwab's authorize endpoint tolerates a trailing-slash difference that
    # its token endpoint rejects, so try each form against the one code.
    errors = []
    for candidate in auth.redirect_candidates():
        try:
            await asyncio.to_thread(auth.exchange_code_value, code, redirect_uri=candidate)
        except ProviderError as exc:
            errors.append({"redirect_uri": candidate, "error": str(exc)[:300]})
            continue

        _breadth_cache.clear()
        status = auth.status()
        return {
            "ok": True,
            "redirect_uri_accepted": candidate,
            "configured_redirect_uri": auth.callback_url,
            "mismatch": candidate != auth.callback_url,
            "access_expires_in_minutes": status["access_expires_in_minutes"],
            "refresh_expires_in_days": status["refresh_expires_in_days"],
        }

    return {
        "ok": False,
        "attempts": errors,
        "hint": (
            "Credentials are not the issue if Basic auth was accepted. With expiry and "
            "both redirect_uri forms ruled out, the remaining cause is app state: the app "
            "must be 'Ready For Use' on developer.schwab.com. 'Approved - Pending' "
            "authorizes fine but cannot exchange codes."
        ),
    }


@app.post("/api/auth/logout")
async def api_auth_logout():
    """Delete the stored token. Requires a fresh browser consent afterwards."""
    auth = _schwab_auth()
    existed = auth.token_path.exists()
    if existed:
        auth.token_path.unlink()
    auth._token = None
    _breadth_cache.clear()
    return {"ok": True, "removed": existed}


@app.get("/connect")
async def connect_page():
    """Browser page for connecting a Schwab account: open the consent link, then paste the redirect URL."""
    return FileResponse(STATIC_DIR / "connect.html")


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


@app.get("/api/symbol")
async def api_symbol(symbol: str = Query(..., description="anything a user may type: AAPL, BRK.B, $RUT, /CL, SPX")):
    """What a typed symbol resolves to, before anything is fetched for it.

    A registered symbol or alias (``/ES``, ``$SPX``) returns its verified spec; any other
    well-formed symbol returns the generic specs it would be priced with (``generic: true``).
    ``exposure`` says whether the Terminal and Columns views can compute exposure for it, and
    why not. Whether the provider has the symbol is answered by the data endpoints. 404 for a
    malformed symbol, with what a symbol may look like.
    """
    inst = _resolve(symbol)
    return {
        "symbol": inst.key,
        "label": inst.label,
        "registered": not inst.generic,
        "generic": inst.generic,
        "model": inst.model,
        "multiplier": None if inst.exposure_unavailable else inst.multiplier,
        "exercise_style": inst.exercise_style,
        "future": inst.is_future,
        "quarterly_rolls": inst.quarterly_rolls,
        "exposure": {"available": inst.exposure_unavailable is None,
                     "reason": inst.exposure_unavailable},
    }


@app.get("/api/meta")
async def api_meta():
    """What the dashboard needs to build its controls: instruments, families and valid parameter values.

    Also the current session phase, the provider name, the bar-size/range matrices for the two chart
    endpoints (`chart` for `/api/history`, `technicals` for `/api/technicals`) and the exchange each
    instrument family takes its breadth from.
    """
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
        "views": ["columns", "terminal", "chart"],
        "chart": capability_matrix(),
        "technicals": technicals.capabilities(),
        "exchanges": FAMILY_EXCHANGE,
    }


@app.get("/")
async def index():
    """The dashboard. `?symbol=`, `?view=` and `?theme=` are read by the page itself."""
    return FileResponse(
        STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache, must-revalidate"}
    )


class _RevalidatingStatic(StaticFiles):
    """Serve the dashboard assets with ``no-cache``.

    Not a performance concession — a correctness one. The markup, CSS and JS
    here change as a unit, and a browser holding one stale file renders the
    other two wrong *without failing*: unstyled SVG falls back to black fill
    and no stroke, so a missing rule looks exactly like a drawing bug and
    gets debugged as one. ``no-cache`` still allows a conditional request, so
    the cost is a 304 on a localhost single-user app.
    """

    def file_response(self, *args, **kwargs):
        """The base response, with ``Cache-Control: no-cache, must-revalidate``."""
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


if STATIC_DIR.exists():
    app.mount("/static", _RevalidatingStatic(directory=STATIC_DIR), name="static")


def main() -> None:
    """The ``gexdash`` command: serve the app with uvicorn on ``GEXDASH_HOST``:``GEXDASH_PORT``,
    127.0.0.1:8420 by default."""
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("GEXDASH_HOST", "127.0.0.1"),
        port=int(os.environ.get("GEXDASH_PORT", "8420")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
