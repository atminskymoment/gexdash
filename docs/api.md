# HTTP API

Base URL: `http://127.0.0.1:8420`. Interactive OpenAPI docs are served at
[`/api/docs`](http://127.0.0.1:8420/api/docs).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | The dashboard |
| `GET` | `/api/exposure` | Full exposure surface, levels, regime, diagnostics |
| `GET` | `/api/spot` | Current underlying price |
| `GET` | `/api/stream` | SSE — the surface re-priced on each spot tick |
| `GET` | `/api/history` | Intraday OHLCV bars + VWAP (optional capability) |
| `GET` | `/api/breadth` | Exchange market internals + session A/D line (optional) |
| `GET` | `/api/technicals` | Chart view, 1m to weekly: averages, bands, Supertrend, profile, VAMS, signals (optional) |
| `POST` | `/api/refresh` | Force a chain pull, bypassing the cache |
| `GET` | `/api/meta` | Instruments, families, valid parameter values, session |
| `GET` | `/api/symbol` | What a typed ticker resolves to: registered or generic specs, and whether exposure is available |
| `GET` | `/api/docs` | OpenAPI / Swagger UI |

## Shared query parameters

Accepted by `/api/exposure` and `/api/stream`.

| Parameter | Type | Default | Values |
|---|---|---|---|
| `symbol` | string | `SPX` | any symbol: the seven registered ones carry verified specs, aliases such as `/ES` or `$SPX` resolve to them, and anything else well-formed gets generic specs ([`/api/symbol`](#get-apisymbol)) |
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

  // Plain-language reading of the numbers above, built server-side so the
  // wording is testable. `null` when `oi_unavailable` — a surface of zeros
  // has nothing to interpret. See methodology §10.
  "interpretation": {
    "headline": "SUPPRESSED",
    "headline_gloss": "Dealers dampen moves — mean reversion toward walls",
    "readings": [
      {"key": "gex", "label": "Net GEX", "value": 8.23e9, "tone": "pos",
       "meaning": "Dealers are long gamma, so they hedge against the move …",
       "caveat": null},
      // `caveat` is set where a reading is an artefact of a chosen
      // assumption rather than a measurement — DEX under an assumed
      // dealer sign convention, for instance.
      {"key": "dex", "…": "…", "caveat": "This is dominated by the dealer sign …"}
    ],
    "synthesis": ["Taken together: damped, but conditionally. …"],
    "caveats": ["Open interest is the OCC snapshot for 2026-08-07 — …"],
    "derivation": "Derived from the exposure numbers on this page by a fixed rule set …"
  },

  "provider": "mock", "synthetic": true,
  "from_cache": true, "stale": false, "cache_age_hours": 1.54,

  // Set when the chain on screen is not a clean current fetch. `stale` says
  // the data is not fresh; `degraded_reason` says why, in words meant for
  // display. `oi_unavailable` means the exposure numbers could not be
  // computed at all and must be rendered as unavailable, never as zero —
  // every measure is proportional to OI, so they would all read $0.
  "degraded_reason": null,
  "oi_unavailable": false,

  "server_time": "2026-08-10T00:51:15+00:00"
}
```

### Degraded responses

A chain can arrive intact but carry no open interest — a failed fetch that
looks like a successful one. Because every exposure measure is proportional to
OI, the whole surface collapses to `0`, which renders as a confident statement
about the market rather than as missing data.

When that is detected (see [methodology §9](methodology.md#9-detecting-a-chain-that-is-not-there)):

| Situation | Response |
|---|---|
| A previous snapshot with real OI exists | That snapshot, `stale: true`, `degraded_reason` set. The empty chain is never written to the cache. |
| No usable snapshot exists | The empty chain, `oi_unavailable: true`, `interpretation: null`. Clients must render the affected metrics as unavailable. |

The provider is not re-asked immediately: retries back off `30s → 60s → 120s
→ 240s → 480s`, resetting on the first good chain. `POST /api/refresh` bypasses
the back-off.

### Status codes

| Code | Meaning |
|---|---|
| `200` | OK |
| `400` | Invalid `expiry_window` |
| `404` | Malformed `symbol` (not letters first, then letters, digits or dots, with `$` or `/` allowed in front) |
| `422` | A typed future such as `/CL`: exposure needs its multiplier, which is unknown. Also from `/api/stream`, before it opens, and `/api/refresh` |
| `422` | No live contracts in the window (e.g. `0dte` after settlement), or an unknown `dealer_sign` |
| `502` | Provider failed *and* no cached chain was available to fall back on |

A network failure on the way to the provider (a timeout, a refused connection)
currently surfaces as `500` rather than `502`. A cached chain still covers it.

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

## `GET /api/history`

Intraday candles for the terminal view's price panel. A **separate** endpoint
from `/api/exposure` on purpose: bars move on a different cadence, and
doubling the SSE payload every 2 seconds to redraw candles that shifted by one
bar would be waste. The client refreshes them on a 30-second timer.

```bash
curl "http://127.0.0.1:8420/api/history?symbol=SPX&range=3M&interval=1D"
```

| Parameter | Default | Range |
|---|---|---|
| `symbol` | `SPX` | any instrument |
| `range` | `1D` | `1D` `5D` `10D` `1M` `3M` `6M` `YTD` `1Y` `2Y` `5Y` `10Y` |
| `interval` | `1m` | `1m` `5m` `10m` `15m` `30m` `1h` `2h` `4h` `1D` `1W` `1Mo` |
| `indicators` | `true` | include RSI + MA(20), MACD and ADX/DI |
| `adjust` | `true` | ES/NQ: adjust for contract changes, as on the Chart view |
| `astro` | `false` | add the lunar-solar [`astro` block](#the-astro-block), indexed like these bars |
| `interval_minutes` | *unset* | **deprecated** legacy form, still honoured |
| `lookback_hours` | *unset* | **deprecated** legacy form, still honoured |

Not every pair is servable, and `/api/meta` carries the matrix so the UI can
grey out the rest rather than let you pick one and silently receive something
else. Where a request cannot be served exactly it is adjusted and the
adjustment is reported in `plan.notes` — never applied quietly.

```jsonc
{"available": true, "symbol": "SPX", "interval_minutes": 1,
 "time": ["2026-08-09T19:10:00", ...],
 "open": [...], "high": [...], "low": [...], "close": [...], "volume": [...],
 "vwap": [...],                       // running session VWAP, volume-weighted
 "session_low": 7722.4, "session_high": 7787.6, "synthetic": true,

 "indicators": {
   "rsi": [...], "rsi_ma": [...],     // nulls through the warm-up, never zeros
   "macd": [...], "macd_signal": [...], "macd_histogram": [...],
   "adx": [...], "di_plus": [...], "di_minus": [...],
   "params": {"rsi_length": 14, "rsi_ma_length": 20, "adx_length": 14,
              "macd": [12, 26, 9],
              "smoothing": "Wilder for RSI/ADX, conventional EMA for MACD"}
 },

 // How the request was actually served.
 "plan": {"range": "3M", "interval_requested": "1D", "interval_served": "1D",
          "aggregated_from_minutes": null,
          "window": {"start": "2026-05-12T20:00:00+00:00",
                     "end":   "2026-08-10T20:00:00+00:00"},
          "provider_request": {"periodType": "year", "period": 1,
                               "frequencyType": "daily", "frequency": 1,
                               "startDate": 1778702400000,
                               "endDate":   1786492800000},
          "notes": []}}
```

Requests are bounded by `startDate`/`endDate`, so **the range asked for is the
range served** — there is no period bucket left to round up to, and nothing is
over-fetched and trimmed. Short intraday ranges (`1D`, `5D`, `10D`) count
*sessions*, so their window covers the last N + 2 weekdays up to now,
starting at 18:00 ET the evening before the first of them, and the server
keeps the last N sessions it finds (`plan.sessions`). A `1D` chart before Monday's open is therefore
Friday's session, and one during Monday's session is Monday's so far. The
`period` form would have ended at the previous session's close.

`interval_served` differs from `interval_requested` when the bar size gives
way, always with the reason in `notes`. Two independent limits cause that:

| Limit | Cause | Example |
|---|---|---|
| **Reach** | how far back the feed serves that bar size — ~48 days for 1-minute, ~260 for 5-minute and coarser | `1Y` + `30m` → daily |
| **Plot budget** | more candles than are worth drawing (15,000) | `1M` + `1m` → `5m` |

Coarsening steps up the intraday ladder before falling to daily, so asking for
1-minute bars over a month yields 5-minute bars rather than losing intraday
entirely. `/api/meta` carries the same two limits (`intraday_reach_days`,
`max_plotted_bars`) and the resulting per-range interval lists, so the selector
offers exactly what will be served rather than accepting a choice and quietly
changing it.

Sub-minute intervals are refused outright with a `400` rather than rounded up
to a minute.

**This capability is optional**, and unavailability is a `200`, not an error:

```jsonc
{"available": false, "symbol": "ES",
 "reason": "/ES price history ends at 71.40, outside the plausible band 1,000-50,000 ..."}
```

Futures roots (`/ES`, `/NQ`) are requested rather than refused up front. A
series whose last price falls outside the instrument's plausible band is
refused as above (bare `ES` on Schwab is Eversource Energy), and one that passes
carries `meta.unverified`, because Schwab does not document these
continuous-contract aliases. They are front-month series. Both chart
endpoints adjust them for contract changes by default, as thinkorswim's chart
option does: prices before each quarterly roll are shifted so the last close
meets the first open after it. `adjust=false` returns the raw series, where
each roll is a step of about 1%. Both answer with `adjusted` and `rolls`
(`index`/`i`, `date`, `gap`, `verified`); see
[data sources](data-sources.md).

The dashboard is fully usable without bars: the exposure surface does not need
them, so the UI renders the reason where the chart would go rather than an
empty plot that reads as a loading state. `404` is still returned for an
unknown `symbol`.

## `GET /api/technicals`

The Chart view's data: intraday, daily or weekly bars with every overlay,
level and signal already computed.

```bash
curl "http://127.0.0.1:8420/api/technicals?symbol=QQQ&range=1Y&interval=1D"
```

| Parameter | Default | Values |
|---|---|---|
| `symbol` | `QQQ` | any instrument; the view offers `QQQ` `SPY` `USO` `ES` `NQ` |
| `range` | `1Y` | per interval, from `caps`: `1D` `5D` `10D` (sessions), `1M` `3M` `6M` `YTD` `1Y` `2Y` `5Y` |
| `interval` | `1D` | `1m` `5m` `15m` `30m` `1h` `4h` `1D` `1W` |
| `session` | `auto` | intraday only: `rth` (the ETF default) or `eth`. ES and NQ are always full-session |
| `st_period` | `10` | Supertrend ATR period |
| `st_mult` | `3.0` | Supertrend ATR multiplier |
| `adjust` | `true` | ES/NQ: adjust for contract changes (thinkorswim's option). Ignored for ETFs |
| `astro` | `false` | add the lunar-solar [`astro` block](#the-astro-block); its rules join `rules` and its notes `notes` |

Each interval offers its own ranges (`caps.intervals[].ranges`): 1-minute
offers 1D and 5D, 4-hour offers 1M to 6M, daily offers 3M to 5Y. A range the
interval does not offer is replaced by its `default_range`, with a note.
Unknown keys are a `400`.

**Warm-up.** Daily and weekly ask for everything the provider serves (10 years
daily, 20 years weekly). Intraday asks for the interval's longest range plus
about 1,000 bars, within the feed's reach (11 days for 1-minute, enough for 5D
plus the warm-up; about 250 days otherwise). Indicators are computed over all
of it, then trimmed to `range`. SMMA 200 is Wilder smoothing, whose seed still
weighs 13% after 400 bars, so computed over only the visible window its left
edge would be wrong. Where warm-up is still short (4-hour bars), `notes` says
so. The fetch is cached server-side per symbol, native bar size and session:
60 s for daily and weekly, 25 s intraday. Every range of an interval shares
one fetch, and 30m/1h/4h share their 30-minute bars.

**Sessions.** Intraday windows always end now. Without an `endDate` the feed
ends at the previous session's close. Hourly and 4-hour bars are built from
30-minute bars anchored to the session open: 9:30 ET for regular hours, 4:00
for extended hours, 18:00 for futures. 1D/5D/10D count sessions by their
exchange date (a futures session opening Sunday 18:00 is Monday's). The
3M/52W levels are always measured on daily bars.

### Response

```jsonc
{
  "available": true, "symbol": "QQQ", "label": "Invesco QQQ Trust",
  "range": "1Y", "interval": "1D", "synthetic": false, "provider": "schwab",
  "session": null,                         // intraday: "rth" | "eth" | "futures"
  "session_breaks": [], "session_labels": [],  // intraday: first bar of each session, its date
  "time": ["2025-11-24T06:00:00", ...], "open": [...], "high": [...],
  "low": [...], "close": [...], "volume": [...],
  "prev_close": 588.1,                     // close before the first bar shown
  "series": {                              // aligned with the bars; null = no value yet
    "smma20": [...], "smma50": [...], "smma100": [...], "smma200": [...],
    "ema9": [...], "bb_basis": [...],
    "bb20_2_upper": [...], "bb20_2_lower": [...],
    "bb20_3_upper": [...], "bb20_3_lower": [...],
    "atr": [...], "rsi": [...],
    "macd": [...], "macd_signal": [...], "macd_histogram": [...],
    "vams_upper": [...], "vams_lower": [...],
    "st_up": [...], "st_dn": [...]         // Supertrend: the active band only, null otherwise
  },
  "supertrend": {"trend": "down", "line": 685.83,
                 "signals": [{"i": 40, "type": "ST_BUY", "price": 605.97}, ...]},
  //   priced at the band, as the script draws them
  "vams": {
    "check": [1, 0, -1, ...],              // per bar: bullish / no call / bearish
    "state": [1, 1, 0, ...],               // latched, decays to 0 (neutral)
    "current": {"state": "neutral", "check": "neutral",   // the chart labels it "no call"
                "to_bullish": 533.57, "to_bearish": 515.09}
  },
  "signals": [{"i": 212, "type": "BUY", "price": 523.4}, ...],
  //   type: BUY ADD TRIM SELL BREAKOUT BREAKDOWN; i indexes the arrays above
  "ranges": [{"start": 180, "end": 200, "high": 585.3, "low": 541.0,
              "outcome": "breakout"}],
  "current_range": {"start": 345, "end": 364, "high": 585.3, "low": 404.4,
                    "is_base": false},
  "position": "flat",
  "profile": {"edges": [...49], "up": [...48], "down": [...48], "poc": 217.75},
  "levels": [{"key": "high_3m_52w", "label": "3M/52W high", "price": 649.42,
              "partial": false}, ...],
  "warmup": {"bars_before_window": 2151, "bars_total": 2516, "notes": []},
  "notes": [],                              // anything the reader must know
  "params": {...},                          // every threshold, see TechParams
  "rules": [{"key": "buy", "name": "BUY", "text": "From flat: ..."}, ...],
  "plan": {...},
  "caps": {...}                             // intervals with their ranges; sessions
}
```

**`rules` are gexdash's own.** The chart this view is modelled on draws VAMS,
BUY/ADD/TRIM/SELL and breakout labels from a private script. These are
stand-in definitions, written out in `rules` and shown under the chart. They
are not a reconstruction of that script. Change the thresholds in
`technicals.TechParams`.

Unavailable history is a `200` with `available: false` and a `reason`, as for
`/api/history`.

### The `astro` block

`astro=true` on `/api/history` or `/api/technicals` adds the lunar-solar cycle
(`cycles.py`; the model is in [methodology §11](methodology.md#11-the-lunar-solar-cycle)).
It is fitted to the symbol's **daily** history whatever the chart's bar size,
using the Chart view's cached 10-year fetch, and evaluated at the chart's bar
times. ES and NQ are fitted on the raw front month with each roll day's return
left out. Without the parameter the payloads are unchanged.

```jsonc
"astro": {
  "available": true,
  "kind": "daily",                         // minute | daily | weekly | monthly
  "series": {                              // one entry per bar, like "time"
    "lsc": [...], "d1": [...], "d2": [...],   // the three lines, each scaled to ±1
    "illum": [...],                        // share of the disc lit, 0-1
    "elong": [...],                        // phase: Moon minus Sun longitude, 0 = new, 180 = full
    "dist": [...], "dec": [...], "sun": [...]  // Moon km, Moon declination °, Sun longitude °
  },
  "future": {                              // slots after the last bar; empty intraday
    "time": ["2026-11-30T00:00:00", ...],  // weekdays (daily), weeks, months
    "lsc": [...], "d1": [...], "d2": [...], "illum": [...], "elong": [...],
    "dist": [...], "dec": [...], "sun": [...],
    "path": [7712.3, ...]                  // projected price: last close × exp(ΔLSC)
  },
  "turns": [{"i": 17, "type": "high", "price": 7712.3, "date": "2026-12-22"}],
  //   i indexes future[]; a reversal of 5% of the path's range confirms each
  "events": [{"type": "full_moon", "label": "Full moon", "time": "2026-12-24T01:28:09Z",
              "slot": 271}],
  //   slot: bars first, then future; the first slot closing at or after the event.
  //   types: new_moon first_quarter full_moon last_quarter perigee apogee (with
  //   distance_km) march_equinox june_solstice september_equinox december_solstice
  "scale": {"lsc": 0.0123, "d1": 0.0009, "d2": 0.0004},  // the divisors: log price, per day, per day²
  "model": {
    "days": 2515, "start": "2016-11-28", "end": "2026-11-27", "r2": 0.0041,
    "cycles": [{"key": "phase", "name": "Lunar phase", "period_days": 29.531,
                "amplitude_bp": 2.1, "peak_days": 5.4, "zero": "new moon"}, ...],
    //   amplitude: basis points a day; peak: days after "zero" that the cycle's return is highest
    "holdout": {"days": 629, "hit_rate": 0.512, "no_skill": 0.503, "corr": 0.011,
                "noise": 0.080, "up_share": 0.54},
    "swing_pct": 1.8                       // the projection's range, % of the last close
  },
  "params": {...},                         // cycles.AstroParams
  "notes": ["Lunar-solar fit: 2,515 daily returns ..."],
  "rules": [{"key": "astro_model", "name": "Lunar-solar cycle (LSC)", "text": "..."}, ...]
}
```

Fewer than 250 daily returns, or daily history that will not load, gives
`{"available": false, "reason": ...}` in place of the block; the rest of the
payload is unaffected. Intraday charts get the lines and events at their bar
times and no `future` slots, with a note saying so. Exchange holidays are not
modelled in future dates.

## `GET /api/symbol`

What a typed symbol resolves to, before anything is fetched for it. Every
endpoint taking `symbol` resolves it the same way.

```bash
curl "http://127.0.0.1:8420/api/symbol?symbol=/CL"
```

```jsonc
{"symbol": "/CL", "label": "/CL (future; generic specs)", "registered": false, "generic": true,
 "model": "future", "multiplier": null, "exercise_style": "american", "future": true,
 "quarterly_rolls": false,
 "exposure": {"available": false, "reason": "exposure needs the contract multiplier, ..."}}
```

| Typed | Resolves to |
|---|---|
| `SPX`, `spx`, `$SPX`; `ES`, `/ES` | the registered, verified spec (`registered: true`) |
| `AAPL`, `BRK.B` | equity or ETF: ×100, Black-Scholes, American, dividend 0 unless `div` is passed |
| `$RUT` | cash index: ×100, Black-Scholes, European |
| `/CL` | future: bars only, no roll adjustment (`quarterly_rolls` is true only for ES and NQ), exposure refused |

The exposure payload's `instrument_meta` carries `generic`, and `strike_step`
is `null` for a typed symbol, whose strikes come from its chain. Whether the
provider has the symbol at all is the data endpoints' answer: a ticker Schwab
does not know comes back as their usual unavailable reason. A malformed symbol
is a `404` with what a symbol may look like. The `/api/technicals` payload says
what it drew with `future`, `quarterly_rolls` and `generic`.

## `GET /api/breadth`

Exchange advance/decline internals for the instrument's family — S&P 500
instruments get **NYSE**, Nasdaq-100 instruments get **NASDAQ**, keyed off
`instruments.FAMILIES`.

```bash
curl "http://127.0.0.1:8420/api/breadth?symbol=SPX"
curl "http://127.0.0.1:8420/api/breadth?symbol=SPX&exchange=NASDAQ"   # override
```

| Parameter | Default | Notes |
|---|---|---|
| `symbol` | `SPX` | its family selects the exchange |
| `exchange` | *derived* | `NYSE` or `NASDAQ`; 400 otherwise |
| `history` | `240` | session A/D samples to return, 0–1000 |

```jsonc
{"available": true, "exchange": "NYSE",
 "advancing": 1607, "declining": 1322, "total_issues": 2929,
 "net": 285,               // derived from the counts shown
 "net_reported": 285,      // the exchange's own $ADD, used as a cross-check
 "ad_ratio": 1.22, "label": "NEUTRAL",
 "up_volume": ..., "down_volume": ..., "volume_ratio": 1.71,
 "tick": 152, "trin": 0.86,
 "unresolved": {},                    // {metric: symbol} for tickers that failed
 "reconciliation_warning": null,      // set when counts disagree with $ADD
 "scope": "NYSE exchange-wide issues, not index constituents",
 "history": [{"t": "...", "advancing": ..., "declining": ..., "net": 285}],
 "cached": false, "cached_seconds": 0.0}
```

**Read `scope` before reading the numbers.** These are exchange-wide counts —
`$ADVN`/`$DECN` cover ~3,000 NYSE issues including ETFs and preferreds, not
the 503 S&P 500 members. Pairing NYSE breadth with an SPX surface is the
standard trader convention and still a proxy.

**Caching is server-side**, 60s, shared by every client — a browser timer
would not protect the rate limit, since two open tabs double it. Swapping the
provider invalidates the cache.

**`reconciliation_warning`** is set when `advancing - declining` disagrees with
the exchange's own net symbol, which means a ticker in `BREADTH_SYMBOLS` is
mapped to the wrong series. A wrong mapping otherwise produces entirely
plausible numbers.

**Unavailability is a `200`:**

```jsonc
{"available": false, "exchange": "NASDAQ",
 "reason": "no breadth symbols defined for 'NASDAQ'"}
```

## Browser URL parameters

Every screen is linkable:

```
http://127.0.0.1:8420/?symbol=QQQ&view=terminal&theme=dark&tz=Europe/London
http://127.0.0.1:8420/?view=chart&chart=ES&chart_range=2Y&chart_interval=1D&chart_adjust=off
```

| Parameter | Values | Resolution order |
|---|---|---|
| `symbol` | any symbol, as `/api/symbol` reads it | URL → `SPX` |
| `view` | `terminal` `columns` `chart` | URL → stored preference → `terminal` (a URL value is also stored) |
| `theme` | `light` `dark` | URL → stored preference → OS `prefers-color-scheme` |
| `tz` | `exchange`, `local`, or any IANA zone | URL → stored preference → `exchange` (New York) |
| `chart` | any symbol; `QQQ` `SPY` `USO` `ES` `NQ` have tabs | URL → stored preference → `QQQ` |
| `chart_range` | a range the bar size offers, e.g. `5D` `1Y` | URL → stored preference → `1Y` |
| `chart_interval` | `1m` `5m` `15m` `30m` `1h` `4h` `1D` `1W` | URL → stored preference → `1D` |
| `chart_session` | `rth` `eth` (ETFs) | URL → stored preference → `rth` |
| `chart_adjust` | `on` `off` (ES, NQ) | URL → stored preference → `on` |
| `chart_astro` | `on` `off`: the Chart view's Moon/Sun layer | URL → stored preference → `off` |

Apart from `view`, a value in the URL applies to that page and is not stored;
choosing in the page stores it. `tz` is display only: nothing sent to the
server depends on it.
