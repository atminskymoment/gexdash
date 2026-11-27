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
| `history.py` | Chart range/interval vocabulary and the mapping onto the provider's period types, plus aggregation and the two limits that decide which intervals a range can offer. Owns every compromise between what was asked for and what can be served, and records each one in `plan.notes`. |
| `indicators.py` | RSI + MA(20), MACD, ADX/DI, SMMA, Bollinger, ATR. Server-side so they can be tested — Wilder smoothing where Wilder specified it, conventional EMAs for MACD, population stdev for Bollinger. |
| `technicals.py` | The Chart view. Fetches the history the bar size needs (all of it for daily/weekly, the longest range plus ~1,000 bars intraday), computes every overlay over it, then trims to the requested range, so long averages are converged at the left edge. Owns intraday sessions: regular/extended/futures hours, session-anchored hourly and 4-hour bars, session-counted ranges. Also holds the gexdash signal rules (VAMS, ranges, BUY/ADD/TRIM/SELL), written out in `RULES` and shipped with each payload, and the Supertrend port. |
| `astro.py` | Sun and Moon positions from Meeus (chapters 25 and 47, the full periodic-term tables), vectorized over numpy arrays, and the event search: phases and equinoxes by root-finding the elongation and the Sun's longitude, perigee and apogee by refining the distance's turns. No data files and no dependency, so it runs into the future as easily as the past. |
| `cycles.py` | The lunar-solar cycle: a ridge fit of daily returns on cos/sin of five ephemeris angles, the cycle component accumulated over trading days, its two derivatives read in continuous time, the projected path and its zigzag turns, the holdout test against a no-skill baseline, and each event's chart slot. Always fitted on daily bars, then evaluated at the chart's bar times, so one fit serves every bar size. Rules in `RULES`, like the technicals'. |
| `interpretation.py` | The written reading of the exposure surface. Server-side for the same reason: prose assembled in the browser is prose nobody checks, and an inverted comparison still reads fluently. |
| `american.py` | CRR binomial, used **only** to bound the European-approximation error, and the two per-model disclosure notes. Not in the hot path. |
| `chain.py` | `OptionChain`: parallel numpy arrays, not a list of objects. `with_spot()` shares arrays rather than copying. |
| `exposure.py` | The engine: per-strike exposure, walls, flip, max pain, concentration, regime. All modelling choices live in `ExposureConfig`. |
| `instruments.py` | Per-instrument specs — multiplier, model, exercise style, strike step, display width, dividend default. Exercise style is overridable via `GEXDASH_EXERCISE_OVERRIDES`. `resolve()` takes anything a user types: a registered key or provider alias returns the verified spec, any other well-formed symbol stated generic specs (`generic`, and `exposure_unavailable` for a typed future); `get()` stays strict. `quarterly_rolls` marks the two futures whose roll calendar is known. |
| `sessions.py` | US exchange session dates and market phase. OI is published on that clock, which is neither UTC nor local. Also which session each bar belongs to (a futures session opening Sunday 18:00 is Monday's), and trimming a series to its last N sessions. |
| `futures.py` | Continuous-futures roll dates (the Monday of expiry week) and back-adjustment for contract changes as thinkorswim defines it. Returns adjusted copies, so cached raw series are never altered. |
| `store.py` | SQLite chain cache + `CachedProvider`, and the breadth sample table that accumulates the session A/D line. Also owns degradation policy: detecting a chain with no open interest, refusing to cache it, falling back to the last usable snapshot, and backing off before retrying. Not a scheduler. |
| `crosscheck.py` | Diffs our greeks against the provider's; `--fit-rate` solves for the implied rate. |
| `providers/` | `base.py` protocol, `mock.py` synthetic, `schwab.py` OAuth + chains. |
| `server.py` | FastAPI + SSE. |
| `static/` | Both dashboards. No build step, no CDN, no chart library. `tz.js` is the display time zone every clock time goes through; display only, so sessions, anchors and roll dates stay in exchange time. |

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

No framework, no build step, no CDN — one HTML file, one CSS file, five JS
files, and hand-rolled SVG charts. `tz.js` (the display zone), `astro.js` (the
Moon/Sun layer), `draw.js` (the drawing tools) and `technicals.js` (the Chart
view) load before `app.js` and only define functions; `app.js` owns startup.
Every top-level name in them is prefixed `tz`, `astro`/`ASTRO_`, `draw`/`DRAW_`
or `tech`/`TECH_`, because classic scripts share one global scope and a
duplicate `const` stops the second file loading.

### Drawing tools on both price charts

`draw.js` stores each drawing as points in time and price per symbol
(localStorage), never in pixels. Each chart calls `drawAttach()` after every
render with its plot box, slot-to-x and price-to-y maps and bar stamps; the
module turns them into an adapter and draws its layer into the new SVG, just
under the crosshair and clipped to the price plot. Slots past the last bar
follow the server's future-date rule (`cycles.future_stamps`), so a drawing
there matches the Moon/Sun layer's dates whether or not that layer is on;
slots before the first bar follow the same rule backwards, so a drawing from
a longer range keeps its spacing on a shorter one.
Pointer handling sits on the chart's stable host element in the capture
phase, so a drawing click never also starts the chart's pan, and the
listeners outlive each render's SVG. Hit-testing is done in code on the
segments and fills each tool returns, with lines winning over another
drawing's shading. The toolbar sits beside a wrapper holding the Terminal's
price chart *and* its indicator panes, because the panes reuse the price
chart's width. Nothing in the file touches the DOM or storage until
`drawInit()`, so its geometry runs under Node in the tests.

### Typed symbols

The Terminal's ticker is a type-in box with the registered symbols as
suggestions; `/api/symbol` checks a typed one before anything is fetched. A
symbol with no exposure payload (a typed future, or a stock whose chain does
not load) clears the exposure panels with the reason rather than leaving the
previous symbol's numbers under the new name, and its candles still draw:
`renderPriceChart` needs only bars, and draws levels only from a payload for
the same symbol. The Chart view's five tabs are quick picks beside a symbol
box; its session and roll controls follow the payload's `future` and
`quarterly_rolls` rather than a fixed list.

### The Moon/Sun layer on both price charts

`astro.js` draws the lunar-solar block for the Terminal chart and the Chart
view alike: the pane, the projected path with its labelled turns, the event
icons. Each chart passes its own slot-to-x map. Future dates are extra
*slots* after the last bar, never rows appended to the OHLC arrays, because
every other layer reads those as real bars. Loops over candles stop at the last
bar, while the x-domain, pan, zoom and crosshair run on to the last future
slot. Hovering a future date shows its date, projected price and the Moon,
with OHLC left blank.

### Three views

The Chart view is independent of the exposure payload. It has its own symbol
tabs, its own crosshair state and its own `[data-txhair]` markers, because the
terminal chart's `hideCrosshair()` clears every `[data-xhair]` on the page.
`render()` branches on all three views explicitly: a bare `else` would draw the
hidden Columns view on every stream tick.

### Terminal and Columns: two views over one dataset

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
| `test_providers.py` | Mock chain shape; Schwab response parsing without network; store round-trips; the store under concurrent use from worker threads (in a subprocess, since the failure was a crash); cache behaviour |
| `test_crosscheck.py` | Passes on matching assumptions, **fails on a wrong rate** — proving the check has teeth |
| `test_server.py` | Full API including SSE, stale fallback, error mapping, request latency |
| `test_futures.py` | Roll dates are the Monday of expiry week; additive and ratio adjustment close each gap exactly and never move the latest price; intraday rolls at the Sunday-evening open; weekly bars matched by ISO week (stamped Sunday or Monday); the cached series is never adjusted in place; both endpoints adjust ES/NQ by default and never an ETF |
| `test_tz.py` | The display zone, run under Node (skipped without it): exchange time by default, half-hour offsets, crossing midnight, daylight-saving tags (BST, CEST, AEDT) and New York's skipped hour, unknown zones falling back, ICU's alias for Kolkata keeping India's entry; the page loads `tz.js` first |
| `test_astro.py` | The Moon against Meeus example 47.a (longitude to 5e-6°, distance to 0.1 km), the Sun against 25.a, illumination against 48.a; new and full moons (the 2024 eclipses, Meeus 49.a) within 2 minutes, the 2024 equinoxes and solstices within 15; a year of events in order, with perigee and apogee alternating at plausible distances |
| `test_cycles.py` | The fit recovers a planted lunar effect's amplitude and timing and its holdout sees it; pure noise stays inside the noise band and is scored against the no-skill mix, not 50%; no lookahead (the fit is identical whatever the chart or horizon); roll days left out; cash-close reading across daylight saving, weekly and monthly period ends; future dates; zigzag turns; the path is the last close moved by ΔLSC and turns where d1 changes sign; no weekend steps in d2; each event at the first slot closing after it; both endpoints, and unchanged payloads without `astro` |
| `test_symbols.py` | Registered symbols and their aliases (`/ES`, `$SPX`) return the verified spec object; a typed equity, cash index and future get exactly the stated generic specs; malformed symbols are refused while `get()` stays strict; a typed equity gets a full surface whose strikes never reach zero; typed tickers draw different synthetic series; a typed future is refused exposure (422) before any stream opens and serves bars unadjusted, with a note; ES and NQ still adjust |
| `test_draw.py` | Under Node: future dates on the server's rule; slot <-> time round trips inside, past and before the bars; Fibonacci retracement and extension prices and time zones; Andrews, Schiff and Modified Schiff origins; Gann slopes; regression ±2σ; anchored VWAP; the fixed-range profile conserves volume and its value area holds 70%; position reward, risk and ratio; hit-testing and stroke thinning; every tool renders clickable geometry at every stage of placing it; storage survives corrupt JSON, blocked storage and malformed drawings |
| `test_technicals.py` | SMMA is Wilder, Bollinger is population stdev, ATR uses the previous close; VAMS latching and decay; bases, breakouts and cooldown; one-position BUY/ADD/TRIM/SELL; profile conserves volume; warm-up leaves no gaps in the window; Supertrend against a hand-worked Pine evaluation, including its previous-band comparisons; hourly/4-hour anchoring (across a DST change), session-counted ranges, regular-hours default for ETFs, full session for futures; the endpoint for all five symbols, one fetch shared across ranges and across 30m/1h/4h |

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
   of weekly candles looks like an ordinary chart. Now that requests carry
   explicit dates there is no bucket left to round up to, and a blanket test
   asserts no offered combination reaches past its range.
7. **The documented "minute bars stop at 10 days" limit was a property of the
   parameter, not the feed.** It was measured off `periodType=day`, whose
   period maxes out at 10. Bounding with `startDate`/`endDate` instead, the
   same endpoint serves 30-minute bars over eight months. This is the failure
   mode of probing an API through one calling convention and recording the
   result as a fact about the service.
8. **A chain with no open interest was cached over a good one.** Every
   exposure measure is proportional to OI, so the surface read `$0` — a
   confident number, not a gap. See
   [methodology §9](methodology.md#9-detecting-a-chain-that-is-not-there).
   Two follow-ons, both caught by tests rather than by inspection: `force=True`
   skipped the cache read entirely, so the refetch button *discarded* a good
   snapshot when the refetch came back empty; and `load()` returns the newest
   snapshot day, so an already-written empty row shadowed the usable one
   behind it.
9. **The interpretation text called a gamma flip below spot "above" it.**
   `(level - spot)/spot` is negative when the level sits beneath, and the
   inverted sentence is perfectly grammatical — it just describes the opposite
   market. This is the argument for building prose server-side where it can be
   tested; every directional claim now has one.
10. **A stale stylesheet rendered as a drawing bug.** A cached `styles.css`
   predating the crosshair rules left its tag at SVG's black default and its
   lines with no stroke at all, which reads as a rendering fault rather than a
   cache one. Front-end assets are now served `no-cache`: they change as a
   unit, and a partial update fails silently.
11. **The crosshair vanished on every refresh.** Re-rendering replaces the SVG
   *under the cursor*, so Chrome fires `pointerleave` on the detached node —
   and `hideCrosshair` is document-wide, so that stale event wiped the
   crosshair the new render had just restored.
12. **A fresh install crashed on its first page load.** The chain store shared
    one SQLite connection across the server's worker threads with no lock;
    `check_same_thread=False` only switches off Python's check. With
    `sqlite3.threadsafety == 1`, two concurrent commits segfaulted inside
    SQLite. An existing cache hid it, because writes were then rare. The first
    page load writes on several threads at once: exposure, bars and breadth
    together. Every use of the connection now holds a lock, and the test
    hammers a fresh store from eight threads in a subprocess. Without the lock
    it crashed on every run.
13. **Dragging the Terminal chart panned it once.** The drag state lived in the
    SVG's event-handler closure, and each pan step re-renders a new SVG, so the
    state died with the first step. It now lives on `state.pan`, and the leave
    Chrome fires on the detached SVG (see 11) is ignored, as the Chart view
    has always done with `tech.drag`.
