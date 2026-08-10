# gexdash

Dealer exposure dashboard for **SPX, SPY, ES, NDX, QQQ and NQ**.

Computes the exposure surface a once-daily open-interest snapshot can honestly
support — GEX, DEX, VANX, CEX, VEX and THEX by strike, plus call/put walls, the
gamma flip, max pain and volume POC — and re-prices all of it against live spot
on every tick.

```bash
pip install -e .
python -m gexdash.server          # http://127.0.0.1:8420
```

Runs immediately with a synthetic provider — no credentials, no market hours.
The banner says `SYNTHETIC — NOT MARKET DATA` when it is not real.

---

## The constraint everything is built around

**Open interest updates once per day.** The OCC computes OI after the close
and publishes it before the next open. There is no live OI feed — not from any
vendor at any price. Every "live GEX" number on every dashboard, commercial
ones included, is last night's OI re-priced against current spot.

That is good news and bad news:

- **Good:** most of a GEX dashboard still works, because gamma and delta are
  functions of spot. A frozen OI snapshot re-prices continuously as price moves.
- **Bad:** everything derived from *flow* is an estimate you would have to build.

So the chain is fetched **once a day** and spot **streams**. That split is the
whole architecture. Metrics needing a live trade feed are rendered as explicit
placeholders with the reason — never as a plausible-looking number.

| Tile | Status |
|---|---|
| GEX / DEX / VANX / CEX / VEX / THEX, walls, flip, max pain, volume POC, level radar | computed |
| OI flow, volume bias, intraday OI change | needs classified trade prints (tier B) |
| Speed engine, momentum | needs underlying time-and-sales (tier C) |
| Kinetics, dealer regime, zone state, king maker | proprietary labels — define your own (tier D) |

---

## Instruments

| Key | Instrument | Multiplier | Model | Exercise |
|---|---|---|---|---|
| `SPX` | S&P 500 Index | ×100 | Black-Scholes | European |
| `SPY` | SPDR S&P 500 ETF | ×100 | Black-Scholes | **American** ¹ |
| `ES` | E-mini S&P 500 future | ×$50 | Black-76 | **American** ² ³ |
| `NDX` | Nasdaq-100 Index | ×100 | Black-Scholes | European |
| `QQQ` | Invesco QQQ Trust | ×100 | Black-Scholes | **American** ¹ |
| `NQ` | E-mini Nasdaq-100 future | ×$20 | Black-76 | **American** ² ³ |

All American instruments keep European greeks and **disclose** it; the error is
**measured, not assumed**, and it differs structurally between the two cases:

| | worst delta error, ±5% | at ±10% / 60 DTE | worst case sits in |
|---|---|---|---|
| ¹ ETFs (q < r) | 0.018 | 0.050 | always an **ITM put** |
| ² Futures (q = r) | 0.004 | 0.012 | usually an **ITM call**; premium is near-symmetric |

See [methodology §7](docs/methodology.md#7-american-exercise).

² CME quarterly and serial options on E-mini futures are American-style; the
weeklies and EOM are European. The family is labelled by its quarterlies.
Override per instrument without editing code:
`GEXDASH_EXERCISE_OVERRIDES="NQ=european"`.

³ **ES and NQ are unverified against Schwab.** The maths is implemented and
tested, but only against the mock provider — `/ES` and `/NQ` are
continuous-contract aliases and Schwab's chains endpoint documents equity and
index options. See [data sources](docs/data-sources.md).

---

## Documentation

| Document | Contents |
|---|---|
| [docs/api.md](docs/api.md) | HTTP endpoints, parameters, response schema, status codes |
| [docs/methodology.md](docs/methodology.md) | Every formula and assumption: greeks, exposure units, sign conventions, flip methods, American approximation |
| [docs/data-sources.md](docs/data-sources.md) | Providers, Schwab setup, OPRA fees, instrument reference |
| [docs/architecture.md](docs/architecture.md) | Module map, design decisions, test strategy |

Interactive OpenAPI docs are served at `/api/docs` when running.

---

## Modelling choices that move the numbers

All exposed as parameters, all reported in the UI's provenance panel. None are
hidden constants.

**Expiry window.** "Total GEX" is meaningless until you say across which
expiries; this moves the result more than anything else. Default `≤ 30 DTE`.

**Dealer sign convention.** Nobody publishes dealer inventory. The
near-universal convention — dealers long calls, short puts — is an
*assumption*, and it moves the gamma flip more than any data-quality issue.

> One consequence worth knowing: that convention is designed for *gamma*,
> which is positive for calls and puts alike and so needs an external sign.
> Applied uniformly it also implies dealers are long every call and short every
> put — a synthetic long against the whole chain — so aggregate DEX comes out
> very large and positive by construction. For the conventional netted dollar
> delta, use `all_long`. On a typical SPX chain that is +$4.2T vs −$45B.

**Gamma flip method.** Two defensible definitions that do not agree:
`spot_solve` (default) root-finds over spot for total GEX = 0, re-pricing every
contract's gamma; `cumulative` finds where the running sum by strike crosses
zero. The first is what people mean by "below the flip, dealers are short
gamma"; the second is cheaper but ignores that gamma moves with spot.

**Rate and dividend yield.** These feed vanna and charm, which no provider
publishes and nothing downstream can check. Each instrument carries its own
dividend default — applying the S&P's 1.3% to QQQ would be wrong.

---

## Verifying the greeks

Vanna and charm are self-computed and unverifiable downstream: a wrong rate
produces a smooth, plausible, wrong VANX. Two defences:

**Finite differences.** Every analytic partial is pinned against a numerical
derivative of the price function — that is what catches sign errors.

**Provider cross-check.** Schwab publishes delta and gamma, and all six greeks
come from the same call with the same `r` and `q`:

```bash
python -m gexdash.crosscheck --symbol SPX --provider schwab --fit-rate
```

A delta MAE above ~0.01 means the rate or dividend assumption is off.
`--fit-rate` solves for the rate that best reproduces the provider's deltas —
if that lands far from what you configured, what you configured is wrong.

---

## Charting notes

Two deliberate departures from the commercial dashboard this layout is
modelled on:

- **No dual axes.** The reference stacks two measures with different scales on
  one plot. Two y-scales align arbitrarily, which invents a correlation the
  data does not contain. Each measure here is its own small multiple over a
  shared strike axis.
- **Calls/puts are blue/orange, not green/red.** Green-vs-red is the worst pair
  under colour-vision deficiency, and this is the one distinction that must
  never be ambiguous.

Both palettes were validated against the surface they render on — all six
checks pass in light and dark (worst adjacent CVD ΔE 21.6–26.8, target ≥ 8).
Light is the default; dark is opt-in via the toggle, `?theme=dark`, or the OS
setting.

---

## Layout

```
gexdash/
  greeks.py       vectorized Black-Scholes; Black-76 via the q = r identity
  american.py     CRR binomial — bounds the SPY/QQQ approximation error
  exposure.py     exposure surface, walls, flip, max pain, regime
  chain.py        option chain as parallel numpy arrays
  instruments.py  per-instrument specs
  sessions.py     US exchange session dates and market phase
  store.py        SQLite chain cache + CachedProvider
  crosscheck.py   diff our greeks against the provider's
  server.py       FastAPI + SSE
  providers/      mock (synthetic) and schwab
  static/         dashboard — no build step, no CDN, no chart library
docs/             api · methodology · data-sources · architecture
tests/            greeks · american · exposure · instruments · providers · crosscheck · server
```

A ~2000-contract chain re-prices in well under a millisecond. A full
`/api/exposure` request measures **~33 ms** with `spot_solve` (~140 chain
re-pricings to root-find the flip) and **~3 ms** with `cumulative` —
comfortably inside the 2 s stream interval. `scipy` is not a dependency.

---

## Tests

```bash
python -m pytest tests/ -q      # 182 tests, ~6s
```

Covering finite-difference validation of all six greeks, the Black-76
identity, the American error bound, exposure levels on hand-built chains with
known answers, contract multipliers against published specs, Schwab response
parsing without network, store round-trips, cross-check sensitivity to a wrong
rate, and the full API surface including SSE and the stale-cache fallback.

Five of them are regressions for bugs found during the build, each invisible
to the obvious test — see
[architecture.md](docs/architecture.md#regression-tests-worth-knowing-about).

---

## Fees, briefly

OPRA subscriber fees are separate from any vendor subscription:
**$1.25/user/month** non-professional, **$31.50** professional,
**$1,500/month** the moment data is redistributed to anyone else. Personal use
rounds to zero; a product does not. Keep the data layer behind the provider
interface and do not architect around free redistribution.

---

## Licence

MIT — see [LICENSE](LICENSE).

**Not investment advice.** This is an analysis tool built on a stated set of
assumptions, several of which — above all the dealer sign convention — are
guesses about unobservable positioning.
