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
| `american.py` | CRR binomial, used **only** to bound the European-approximation error, and the two per-model disclosure notes. Not in the hot path. |
| `chain.py` | `OptionChain`: parallel numpy arrays, not a list of objects. `with_spot()` shares arrays rather than copying. |
| `exposure.py` | The engine: per-strike exposure, walls, flip, max pain, concentration, regime. All modelling choices live in `ExposureConfig`. |
| `instruments.py` | Per-instrument specs — multiplier, model, exercise style, strike step, display width, dividend default. Exercise style is overridable via `GEXDASH_EXERCISE_OVERRIDES`. |
| `sessions.py` | US exchange session dates and market phase. OI is published on that clock, which is neither UTC nor local. |
| `store.py` | SQLite chain cache + `CachedProvider`. Not a scheduler. |
| `crosscheck.py` | Diffs our greeks against the provider's; `--fit-rate` solves for the implied rate. |
| `providers/` | `base.py` protocol, `mock.py` synthetic, `schwab.py` OAuth + chains. |
| `server.py` | FastAPI + SSE. |
| `static/` | Dashboard. No build step, no CDN, no chart library. |

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

**Degrade, don't fail.** If the provider is unreachable and the cached chain
is stale, the last good snapshot is served with `stale: true`. A few-hours-old
OI surface is usable; a 502 is not.

## Frontend

No framework, no build step, no CDN — one HTML file, one CSS file, one JS
file, and hand-rolled SVG charts.

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

Five bugs found during development, each invisible to the obvious test:

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
