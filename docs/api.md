# HTTP API

Base URL: `http://127.0.0.1:8420`. Interactive OpenAPI docs are served at
[`/api/docs`](http://127.0.0.1:8420/api/docs).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The dashboard |
| `GET` | `/api/exposure` | Full exposure surface, levels, regime, diagnostics |
| `GET` | `/api/spot` | Current underlying price |
| `GET` | `/api/stream` | SSE — the surface re-priced on each spot tick |
| `POST` | `/api/refresh` | Force a chain pull, bypassing the cache |
| `GET` | `/api/meta` | Instruments, families, valid parameter values, session |
| `GET` | `/api/docs` | OpenAPI / Swagger UI |

## Shared query parameters

Accepted by `/api/exposure` and `/api/stream`.

| Parameter | Type | Default | Values |
|---|---|---|---|
| `symbol` | string | `SPX` | `SPX` `SPY` `ES` `NDX` `QQQ` `NQ` |
| `expiry_window` | string | `30dte` | `0dte` `7dte` `30dte` `60dte` `all` |
| `dealer_sign` | string | `long_calls_short_puts` | `long_calls_short_puts` `short_calls_long_puts` `all_long` |
| `flip_method` | string | `spot_solve` | `spot_solve` `cumulative` |
| `rate` | float | `0.042` | Continuously-compounded risk-free rate |
| `div` | float | *unset* | Dividend yield. **Omit** to use the instrument's own default — passing a value applies it to every instrument, which is wrong for a mixed watchlist |
| `interval` | float | `2.0` | `/api/stream` only, seconds, 0.25–60 |
| `max_events` | int | `0` | `/api/stream` only; stop after N events, 0 = unbounded |

Every one of these changes the numbers, so the response echoes the active set
back in `diagnostics` and the UI renders it in the provenance panel.

## `GET /api/exposure`

```bash
curl "http://127.0.0.1:8420/api/exposure?symbol=SPX&expiry_window=30dte"
```

### Response

```jsonc
{
  "spot": 7773.53,
  "instrument": "SPX",
  "contracts": 854,

  // Per-strike arrays, all aligned with `strikes`.
  "strikes":      [7625, 7630, ...],
  "gex":          [...],   // $ delta per +1% spot
  "dex":          [...],   // $ delta
  "vanx":         [...],   // $ delta per +1 vol point
  "cex":          [...],   // $ delta per day
  "vex":          [...],   // $ P&L per +1 vol point
  "thex":         [...],   // $ P&L per day
  "call_gex":     [...],   // call-only split, for the walls
  "put_gex":      [...],
  "call_oi":      [...], "put_oi":     [...],
  "call_volume":  [...], "put_volume": [...],

  "totals": {
    "gex": -31830000000.0, "dex": 4.15e12, "vanx": ..., "cex": ...,
    "vex": ..., "thex": ...,
    "call_oi": 5938315, "put_oi": 6229020,
    "call_volume": ..., "put_volume": ...,
    "pc_oi_ratio": 1.049, "pc_volume_ratio": 0.984
  },

  "levels": {
    // strike may be null when the level does not exist in range —
    // a flip only exists if total GEX actually crosses zero.
    "call_wall":   {"name": "Call Wall", "strike": 7780, "value": 9.68e10,
                    "distance": 6.47, "distance_pct": 0.083},
    "put_wall":    {...}, "max_pos_gex": {...}, "max_neg_gex": {...},
    "gamma_flip":  {...}, "max_pain": {...}, "volume_poc": {...}
  },

  // Our own regime rule, not a reproduction of any vendor's label.
  "regime": {
    "label": "AMPLIFIED",
    "gloss": "Dealers hedge with the move — trends extend",
    "gamma_sign": "short", "position": "below flip", "entrenched": true,
    "derivation": "gexdash rule: sign(GEX) x (spot vs gamma flip)"
  },

  "diagnostics": {
    "expiry_window": "30dte", "expiry_window_days": 30,
    "contracts_in_window": 854, "contracts_in_chain": 1098,
    "expiries_in_window": 7, "min_dte": 0.86, "max_dte": 21.9,
    "expired_excluded": 0, "zero_dte_expired": false,
    "zero_dte_gex_share_pct": 31.9,
    "dealer_sign": "long_calls_short_puts", "flip_method": "spot_solve",
    "pricing_model": "black_scholes", "multiplier": 100.0,
    "exercise_style": "european",
    "greeks_approximation": null,      // set for SPY/QQQ
    "risk_free_rate": 0.042,
    "dividend_yield": 0.013, "dividend_yield_source": "instrument default",
    "oi_asof": "2026-08-08",           // OCC prior-session date
    "max_pain_expiry": "2026-08-10T20:00:00",
    "front_expiry": "...", "front_expiry_gex_share_pct": 22.1
  },

  "session": {"phase": "closed", "is_open": false, "exchange_time": "20:51:15",
              "seconds_to_next": 25680, "next_transition": "pre-market"},

  "instrument_meta": {"key": "SPX", "label": "S&P 500 Index", "multiplier": 100.0,
                      "model": "index", "exercise_style": "european",
                      "display_strikes": 30, "strike_step": 5.0, "note": ""},

  // Metrics the reference dashboard shows that tier A cannot produce, with
  // the reason. Rendered as explicit placeholders, never as numbers.
  "unavailable": {"oi_flow": "needs classified trade prints (tier B)", ...},

  "provider": "mock", "synthetic": true,
  "from_cache": true, "stale": false, "cache_age_hours": 1.54,
  "server_time": "2026-08-10T00:51:15+00:00"
}
```

### Status codes

| Code | Meaning |
|---|---|
| `200` | OK |
| `400` | Invalid `expiry_window` |
| `404` | Unknown `symbol` |
| `422` | No live contracts in the window (e.g. `0dte` after settlement) |
| `502` | Provider failed *and* no cached chain was available to fall back on |

## `GET /api/stream`

Server-sent events. Each event is one complete `/api/exposure` payload — the
cached chain re-priced against a fresh spot. Errors arrive as
`{"error": "..."}` rather than closing the stream.

```bash
curl -N "http://127.0.0.1:8420/api/stream?symbol=SPX&interval=2"
curl -N "http://127.0.0.1:8420/api/stream?symbol=SPX&max_events=3"   # bounded
```

The generator polls `request.is_disconnected()`, so a closed tab stops the
loop rather than leaving the server re-pricing forever.

## `POST /api/refresh`

Bypasses the daily cache. Rarely needed — open interest only changes once a
day — but useful after a provider outage.

```jsonc
{"symbol": "SPX", "contracts": 1098, "spot": 7774.32,
 "oi_asof": "2026-08-08", "fetched_at": "2026-08-09T23:10:04+00:00"}
```

## Caching and staleness

`GET /api/exposure` never refetches the chain if a snapshot under 12 hours old
exists — open interest is an OCC once-daily figure, so re-pulling buys nothing.
Spot is **not** cached; it is the live half.

If the provider is unreachable and the cached chain is past its window, the
last good snapshot is served with `"stale": true` rather than a 502. A
few-hours-old OI surface is still usable; a failed request is not. The UI
flags it in the header strip and provenance panel.

## Browser URL parameters

The dashboard itself accepts `?symbol=` and `?theme=` so a specific view is
linkable:

```
http://127.0.0.1:8420/?symbol=QQQ&theme=dark
```

`theme` resolves as: URL parameter → stored preference → OS
`prefers-color-scheme`.
