# Architecture

## The split

```
                    once a day                    every tick
                         │                             │
   provider.get_chain() ─┴─► ChainStore (SQLite)       │
                                   │                   │
                                   ▼                   ▼
                            OptionChain ◄──── provider.get_spot()
                          (numpy arrays)      chain.with_spot(s)
                                   │
                                   ▼
                          exposure.compute()
                    greeks → per-strike → levels → regime
                                   │
                                   ▼
                       /api/exposure   /api/stream (SSE)
                                   │
                                   ▼
                          static/app.js  (hand-rolled SVG)
```

Open interest is an OCC once-daily figure, so the chain is fetched once and
spot streams. Every tick re-prices the cached chain — that is what makes the
whole surface live without a live OI feed, which does not exist at any price.

## Modules

| Module | Responsibility |
|---|---|
| `greeks.py` | Vectorized Black-Scholes. Black-76 via the exact `q = r` identity, so there is one code path. No scipy — `math.erf` vectorized is 0.14 ms for 2000 contracts. |
| `breadth.py` | Exchange market internals from the `$ADVN`/`$DECN` symbol family, plus the counts-vs-net cross-check. |
| `bars.py` | Intraday OHLCV + VWAP. Optional: a provider that cannot serve bars raises `PriceHistoryUnavailable` and the UI states the reason. |
| `history.py` | Chart range/interval vocabulary and the mapping onto the provider's period types, plus aggregation and the over-fetch trim. Owns every compromise between what was asked for and what can be served, and records each one in `plan.notes`. |
| `indicators.py` | RSI + MA(20), MACD, ADX/DI. Server-side so they can be tested — Wilder smoothing where Wilder specified it, conventional EMAs for MACD. |
| `interpretation.py` | The written reading of the exposure surface. Server-side for the same reason: prose assembled in the browser is prose nobody checks, and an inverted comparison still reads fluently. |
| `american.py` | CRR binomial, used **only** to bound the European-approximation error, and the two per-model disclosure notes. Not in the hot path. |
| `chain.py` | `OptionChain`: parallel numpy arrays, not a list of objects. `with_spot()` shares arrays rather than copying. |
| `exposure.py` | The engine: per-strike exposure, walls, flip, max pain, concentration, regime. All modelling choices live in `ExposureConfig`. |
| `instruments.py` | Per-instrument specs — multiplier, model, exercise style, strike step, display width, dividend default. Exercise style is overridable via `GEXDASH_EXERCISE_OVERRIDES`. |
| `sessions.py` | US exchange session dates and market phase. OI is published on that clock, which is neither UTC nor local. |
| `store.py` | SQLite chain cache + `CachedProvider`, and the breadth sample table that accumulates the session A/D line. Also owns degradation policy: detecting a chain with no open interest, refusing to cache it, falling back to the last usable snapshot, and backing off before retrying. Not a scheduler. |
| `crosscheck.py` | Diffs our greeks against the provider's; `--fit-rate` solves for the implied rate. |
| `providers/` | `base.py` protocol, `mock.py` synthetic, `schwab.py` OAuth + chains. |
| `server.py` | FastAPI + SSE. |
| `static/` | Both dashboards. No build step, no CDN, no chart library. |

## Design decisions

**Arrays, not objects.** A ~2000-contract chain re-prices in under a
millisecond because greeks are computed array-wise. A per-contract Python loop
could not keep up with a spot stream, and `spot_solve` alone does ~140 full
chain re-pricings per request.

**One Black-Scholes core.** Black-76 is exactly Black-Scholes with `q = r` and
`S = F`. Implementing it separately would double the surface area for a sign
error. A test asserts the identity against an independently written Black-76.

**Config, not constants.** Expiry window, dealer sign, flip method, rate and
dividend all change the numbers materially. Each is a parameter, echoed back
in `diagnostics`, and rendered in the UI's provenance panel.

**Lazy provider construction.** `ChainStore()` creates a database under
`GEXDASH_HOME`, so building it at import time would mean importing the module
for a test writes to the real `~/.gexdash`. `get_provider()` / `set_provider()`
instead.

**Optional capabilities are separate protocols.** `ChainProvider` stays two
methods; `PriceHistoryProvider` and `BreadthProvider` are their own protocols
with their own typed `…Unavailable` errors. Each endpoint returns `200` with a
reason rather than an error status, because nothing on the exposure surface
depends on either. The pattern is now used three times and is mechanical.

**Degrade, don't fail.** If the provider is unreachable and the cached chain
is stale, the last good snapshot is served with `stale: true`. A few-hours-old
OI surface is usable; a 502 is not.

## Frontend

No framework, no build step, no CDN — one HTML file, one CSS file, one JS
file, and hand-rolled SVG charts.

### Two views over one dataset

**Terminal** (default) is chart-centric: KPI strip, candlesticks with GEX
levels drawn as price-axis annotations, a tabbed horizontal exposure panel,
and an order-flow/bias column. **Columns** is panel-centric: market bias,
spot & levels, zone analysis, signal chart.

Both consume the same `/api/exposure` payload and **share their chart
primitives** — `axisTicks`, `niceCeil`, the rounded-end bar paths, the
metric definitions, the diverging colours. The horizontal exposure panel is
a coordinate swap of the vertical small multiples, not a second renderer, so
they cannot drift apart.

Three things the switch has to get right, each of which broke first:

- A hidden container reports `clientWidth === 0`, so charts laid out while
  hidden get degenerate geometry. Switching **re-renders** rather than
  unhiding.
- `display: grid` on `.columns`/`.terminal` outranks the UA stylesheet's
  `[hidden] { display: none }`, so both views rendered at once until an
  explicit `!important` rule was added.
- The SSE handler dispatches to whichever view is mounted; rendering
  unconditionally would write into detached nodes.

Levels drawn on the price axis are legitimate — they are prices. An exposure
series on that axis would be the dual-axis anti-pattern, and is deliberately
absent.

Two deliberate departures from the commercial dashboard the layout is
modelled on:

- **No dual axes.** The reference stacks two measures with different scales on
  one plot ("VOL / GEX", "GEX / VANX"). Two y-scales align arbitrarily, which
  invents a correlation the data does not contain. Each measure here is its own
  small multiple over a shared strike axis — the same visual comparison, done
  honestly.
- **Calls/puts are blue/orange, not green/red.** Green-vs-red is the worst pair
  under colour-vision deficiency, and this is the one distinction on the page
  that must never be ambiguous.

Both palettes were validated against the surface they render on:

| | calls/puts | pos/neg | surface |
|---|---|---|---|
| light | CVD ΔE 24.7 | ΔE 21.6 | `#fcfcfb` |
| dark | CVD ΔE 26.8 | ΔE 19.2 | `#1a1a19` |

All six checks pass in both modes (target ≥ 8). Light is the default; dark is
opt-in via the toggle, `?theme=dark`, or the OS setting.

## Testing

```bash
python -m pytest tests/ -q
```

| File | Covers |
|---|---|
| `test_greeks.py` | Every analytic partial pinned to a finite difference; Black-76 identity; put-call parity; degenerate clamping; kernel latency |
| `test_american.py` | Binomial converges to BS for a non-dividend call; premium never negative; equity and futures error bounds; equity worst case is always an ITM put, futures premium is near-symmetric |
| `test_exposure.py` | Levels on hand-built chains with known answers; flip zeroes total GEX; expiry windows; sign conventions; expired-contract exclusion |
| `test_instruments.py` | Multipliers and exercise styles vs contract specs; multiplier scaling isolated from model; exercise style surfaced; per-instrument dividends; override parsing and its failure modes |
| `test_providers.py` | Mock chain shape; Schwab response parsing without network; store round-trips; cache behaviour |
| `test_crosscheck.py` | Passes on matching assumptions, **fails on a wrong rate** — proving the check has teeth |
| `test_server.py` | Full API including SSE, stale fallback, error mapping, request latency |

### Regression tests worth knowing about

Bugs found during development, each invisible to the obvious test:

1. **Settled contracts were clamped, not dropped.** Greeks are singular at
   T=0; clamping left a finite but enormous value, and one expired strike
   contributed 2× the entire CEX total.
2. **The mock's spot walk crashed once it drifted below its start**, because
   it re-seeded from a value that went negative. Only reproducible after
   enough calls, so it passed every short test and surfaced as a 422.
3. **The SSE generator never checked for disconnect**, leaving the server
   re-pricing a closed tab's chain forever.
4. **OI dates mixed UTC and local time.** Caught by a test that passed all
   afternoon and failed at 00:01 UTC — precisely the window where the two
   disagree. OI is published on a US *exchange session* clock, which is
   neither.
5. **The `div` query parameter defaulted to a literal**, so every request
   reported "override" and silently applied the S&P's dividend yield to QQQ.
6. **Weekly bars ignored the requested range.** Every weekly request was sent
   as a *year* period, so "3M / Weekly" drew a full year — 357 days of data
   under a label saying 90. Nothing announced it: the axis relabels and a year
   of weekly candles looks like an ordinary chart. There is now a blanket test
   that no offered combination requests materially more history than its range.
7. **A chain with no open interest was cached over a good one.** Every
   exposure measure is proportional to OI, so the surface read `$0` — a
   confident number, not a gap. See
   [methodology §9](methodology.md#9-detecting-a-chain-that-is-not-there).
   Two follow-ons, both caught by tests rather than by inspection: `force=True`
   skipped the cache read entirely, so the refetch button *discarded* a good
   snapshot when the refetch came back empty; and `load()` returns the newest
   snapshot day, so an already-written empty row shadowed the usable one
   behind it.
8. **The interpretation text called a gamma flip below spot "above" it.**
   `(level - spot)/spot` is negative when the level sits beneath, and the
   inverted sentence is perfectly grammatical — it just describes the opposite
   market. This is the argument for building prose server-side where it can be
   tested; every directional claim now has one.
9. **A stale stylesheet rendered as a drawing bug.** A cached `styles.css`
   predating the crosshair rules left its tag at SVG's black default and its
   lines with no stroke at all, which reads as a rendering fault rather than a
   cache one. Front-end assets are now served `no-cache`: they change as a
   unit, and a partial update fails silently.
10. **The crosshair vanished on every refresh.** Re-rendering replaces the SVG
   *under the cursor*, so Chrome fires `pointerleave` on the detached node —
   and `hideCrosshair` is document-wide, so that stale event wiped the
   crosshair the new render had just restored.
