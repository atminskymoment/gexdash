# Methodology

Every number on the dashboard, and every assumption behind it.

## 1. The constraint that shapes everything

**Open interest updates once per day.** The OCC computes OI after the close
and publishes it before the next open. There is no live OI feed — not from any
vendor at any price. Every "live GEX" number on every dashboard, commercial
ones included, is last night's OI re-priced against current spot.

This is why the architecture splits the way it does: the **chain** is fetched
once a day, **spot** streams, and every tick re-prices the cached chain.
Gamma and delta are functions of spot, so the surface moves continuously even
with frozen OI. What it cannot produce is anything derived from *flow* — those
tiles are rendered as explicit placeholders with the reason.

## 2. Greeks

One Black-Scholes core serves every instrument:

$$d_1 = \frac{\ln(S/K) + (r - q + \tfrac{1}{2}\sigma^2)T}{\sigma\sqrt{T}},
\qquad d_2 = d_1 - \sigma\sqrt{T}$$

| Greek | Formula | Notes |
|---|---|---|
| delta | $e^{-qT}N(d_1)$ (call) | put = call − $e^{-qT}$ |
| gamma | $e^{-qT}\phi(d_1) / (S\sigma\sqrt{T})$ | right-independent |
| vega | $Se^{-qT}\phi(d_1)\sqrt{T}$ | right-independent |
| theta | $\partial V/\partial t$, t forward | negative for long premium |
| **vanna** | $-e^{-qT}\phi(d_1)d_2/\sigma$ | $\partial\Delta/\partial\sigma$; right-independent |
| **charm** | $wqe^{-qT}N(wd_1) - e^{-qT}\phi(d_1)\frac{2(r-q)T - d_2\sigma\sqrt{T}}{2T\sigma\sqrt{T}}$ | $\partial\Delta/\partial t$, t forward |

Vanna and charm are **not published by any retail provider**, so they are
computed here — which means they inherit the rate and dividend assumptions
and nothing downstream can catch an error in them. Two defences:

- **Finite differences.** Every analytic partial is pinned against a numerical
  derivative of the price function (`tests/test_greeks.py`). This is what
  catches sign errors, the failure mode that produces a plausible-looking
  profile pointing the wrong way.
- **Provider cross-check.** Schwab publishes delta and gamma. All six greeks
  come from the same call with the same `r` and `q`, so agreement on the two
  that are published is evidence the inputs are right. See
  [`crosscheck.py`](../gexdash/crosscheck.py).

### Futures: Black-76 without a second code path

Options on futures are Black-76, which is **exactly** Black-Scholes with
$q = r$ and $S = F$ — the $F = Se^{(r-q)T}$ substitution collapses the drift
term. So there is one implementation, and a test asserts the identity against
an independently written Black-76 (`test_black76_equals_black_scholes_with_q_equals_r`).

The underlying passed for ES/NQ is the **futures price**, not the index. They
differ by carry, and the basis drifts through the session.

### Numerical guards

The analytic greeks are singular at $T = 0$: gamma goes as $1/\sqrt{T}$ and
charm as $1/T$. Two separate mechanisms:

- `greeks.py` clamps $T$ to one minute and $\sigma$ to $10^{-6}$ so nothing
  returns `nan`.
- `exposure.py` **excludes** contracts at or below `min_dte_days` entirely.

The second matters more than it sounds. Clamping alone leaves a *finite but
enormous* value: on a real chain, one settled strike contributed twice the
entire CEX total. Clamped-not-dropped is worse than `nan`, because `nan`
announces itself.

## 3. Exposure definitions

Raw greeks come out in per-1.0 units. The conversions to quoted units are all
in one place so the choices are visible:

| Metric | Definition | Reads as |
|---|---|---|
| GEX | $\Gamma \cdot OI \cdot m \cdot S^2 \cdot 0.01$ | \$ delta gained per +1% in spot |
| DEX | $\Delta \cdot OI \cdot m \cdot S$ | \$ delta |
| VANX | $\text{vanna} \cdot OI \cdot m \cdot S / 100$ | \$ delta gained per +1 vol point |
| CEX | $\text{charm} \cdot OI \cdot m \cdot S / 365$ | \$ delta gained per day elapsed |
| VEX | $\text{vega} \cdot OI \cdot m / 100$ | \$ P&L per +1 vol point |
| THEX | $\theta \cdot OI \cdot m / 365$ | \$ P&L per day elapsed |

$m$ is the contract multiplier: 100 for SPX/NDX/SPY/QQQ, \$50 for ES,
\$20 for NQ.

## 4. The dealer sign convention

**Nobody publishes dealer inventory.** The near-universal convention —
dealers long calls, short puts — is an *assumption* about who is on the other
side of customer flow. It moves the gamma flip more than any data-quality
issue, so it is a parameter, and the active choice is reported.

| Option | Weight | Use |
|---|---|---|
| `long_calls_short_puts` | calls +1, puts −1 | The standard convention. Default. |
| `short_calls_long_puts` | calls −1, puts +1 | Inverts every sign. |
| `all_long` | +1 everywhere | Raw netted exposure, no positioning assumption. |

### Why DEX looks enormous

The convention is designed for **gamma**, which is positive for calls and puts
alike and therefore needs an external sign to express positioning. Applied
uniformly it also implies dealers are long every call and short every put — a
synthetic long against the entire chain — so aggregate DEX comes out very
large and positive *by construction*.

That is the assumption being honest about itself, not a bug. For the
conventional "open-interest weighted net delta", where put deltas net against
call deltas, use `all_long`. On a typical SPX chain that is the difference
between +\$4.2T and −\$45B.

## 5. Levels

**Call wall / put wall** — the strike with the largest single-strike gamma
concentration on each side, measured on *absolute* exposure so the sign
convention cannot change which strike is selected.

**Max ±GEX** — the strikes with the largest positive and negative *net* GEX.
These differ from the walls, which are call-only and put-only.

**Gamma flip** — two defensible definitions that do **not** agree:

- `spot_solve` *(default)* — root-find over spot for the price at which total
  GEX would be zero, re-pricing every contract's gamma at each candidate.
  This is what people mean by "below the flip, dealers are short gamma".
  Assumes sticky-strike: each contract's IV is held fixed as spot moves. Real
  surfaces reposition in a selloff, so the true flip moves relative to this.
- `cumulative` — the strike where the running sum of per-strike GEX crosses
  zero. Cheaper, and it falls straight out of a groupby, but it answers a
  different question: it ignores that gamma itself moves with spot.

`spot_solve` costs ~140 chain re-pricings per request (~33 ms); `cumulative`
is ~3 ms.

**Max pain** — the strike minimising total intrinsic payout to holders,
computed for a **single expiry**. Pooling across expiries produces a number
with no interpretation, so the expiry used is reported.

**Volume POC** — highest total option volume by strike. This is cumulative
session volume from the chain snapshot, *not* a classified trade tape.

## 6. Expiry window

"Total GEX" is meaningless until you say across which expiries, and this
choice moves the result more than anything else. It is an explicit parameter
(`0dte`, `7dte`, `30dte`, `60dte`, `all`), rendered on screen, never a
constant buried in the engine.

**GEX concentration** is the share of gross |GEX| sitting in contracts
expiring today — high means the surface is dominated by contracts that will
not exist tomorrow.

## 7. American exercise

Four of the six instruments are American, for two different reasons:

- **SPY, QQQ** — American options on a dividend-paying ETF, physically settled.
- **ES, NQ** — CME options on E-mini futures. The **quarterly and serial**
  contracts are American-style; the weeklies and EOM are European. The family
  is labelled by its quarterlies, which carry the bulk of the open interest
  that drives an exposure surface. Override with
  `GEXDASH_EXERCISE_OVERRIDES="NQ=european"` if your chain is mostly weeklies.

**SPX and NDX are European**, cash-settled index options. Nasdaq and Cboe
market NDX specifically on having no early-assignment risk. It is tempting to
"correct" NDX to American for consistency with the CME futures instruments —
they are a different product with a different rule, and
`test_ndx_default_stays_european` exists to catch that.

The exposure engine uses European greeks for everything. A binomial per
contract per spot tick is not viable when `spot_solve` alone does ~140 chain
re-pricings. But "it's an approximation" is only honest if you know how large
the error is, so [`american.py`](../gexdash/american.py) implements a
Cox-Ross-Rubinstein binomial used solely to **measure** it.

### The two cases differ structurally, not just in magnitude

This is why there are two disclosure notes rather than one:

**Equity (SPY, QQQ), q < r.** Early-exercise value concentrates in **ITM
puts** — holding a deep ITM put forgoes interest on the strike you could
collect now. Measured on a SPY-like grid (spot 670, σ 17%, r 4.2%, q 1.2%):

| Strike band | 7 DTE | 30 DTE | 60 DTE |
|---|---|---|---|
| ±5% of spot | 0.014 | 0.018 | 0.022 |
| ±10% of spot | 0.017 | 0.038 | 0.050 |

The worst case is an ITM put at **every** maturity, asserted as a structural
test rather than left as a claim.

**Futures (ES, NQ), q = r.** Black-76 sets the drift term to `q = r`, so the
asymmetry disappears: ITM calls and ITM puts carry comparable premium, and the
worst case is usually an **ITM call**. Measured on an ES-like grid (spot 7790,
σ 12%), ITM call premium 0.545 against ITM put 0.523 — near-symmetric. The
error is also roughly **4× smaller** than the equity case:

| Strike band | 7 DTE | 30 DTE | 60 DTE |
|---|---|---|---|
| ±5% of spot | 0.002 | 0.003 | 0.004 |
| ±10% of spot | 0.002 | 0.008 | 0.012 |

Emitting the equity wording for ES/NQ would state something measurably false,
which is why `american.approximation_note()` keys on `instrument.is_future`.

**Consequence worth knowing:** put-wall detection looks at ITM puts, so the
equity approximation is weakest exactly where that level is found. For the
futures instruments there is no such alignment, and the error is small enough
that it moves no level.

The UI flags American instruments in the header strip and reports the relevant
note in the provenance panel.

## 8. The regime label

`SUPPRESSED` / `AMPLIFIED` / `TRANSITIONAL` is **our own rule**, not a
reproduction of any commercial dashboard's proprietary label, and the UI says
so on screen. It is derived entirely from tier-A quantities, so unlike vendor
labels it is reproducible and auditable:

| GEX | Spot vs flip | Label |
|---|---|---|
| > 0 (long gamma) | above flip | `SUPPRESSED` — dealers dampen moves, mean reversion toward walls |
| < 0 (short gamma) | below flip | `AMPLIFIED` — dealers hedge with the move, trends extend |
| mixed | — | `TRANSITIONAL` — near a regime boundary |

None of this is a prediction. It restates the exposure numbers in words and
inherits every assumption behind them, above all the dealer sign convention,
which decides the sign of GEX in the first place.

## 9. Detecting a chain that is not there

A chain can arrive complete, well-formed and carrying no open interest. Every
exposure measure in §3 is proportional to OI, so the whole surface collapses
to exactly zero — and a dashboard of `$0` tiles reads as a confident statement
about the market rather than as a failed fetch.

Observed live, not hypothesised: Schwab served an SPX chain of 16,862
contracts with open interest on **2** of them, 896 contracts in total, none
inside the ≤30 DTE window, with volume populated and no error anywhere.

### The test is coverage, not total

"Any open interest at all" passes that chain — those 2 contracts satisfy it.
An absolute floor would have to be calibrated per instrument, since SPY
carries millions and a quiet single name does not. What separates the cases is
the **share of listed contracts carrying any OI**, sampled across one live
entitlement:

| Chain | Contracts with OI |
|---|---|
| SPY | 72.7% |
| QQQ | 68.3% |
| NDX | 34.3% |
| SPX *(degraded)* | **0.01%** |

The floor is **1%** — roughly 34× below the thinnest healthy reading, and far
above the degraded one. A genuinely thin chain is one where few contracts are
*listed* and most are held; that passes comfortably. Thousands listed with two
held is a feed declining to answer.

### What happens then

The empty chain is **never written to the cache** — otherwise it displaces the
good snapshot behind it and is served all day as if it were fine. Instead the
last snapshot that carried a real OI surface is served, marked stale with the
reason. If no usable snapshot exists, the response sets `oi_unavailable` and
the client renders the metrics as unavailable rather than as zero.

Retries back off `30s → 60s → 120s → 240s → 480s`, resetting on the first good
chain. Short first because the common case is transient — the feed drops OI
around the session boundary and returns within a minute or two — and doubling
after that because one SPX attempt is four paginated requests and ~17k
contracts. An explicit refetch bypasses the back-off.

## 10. The written reading

The dashboard states, in words, what the current numbers imply. That is more
dangerous than any chart: a confident sentence is believed in a way a plot is
not, and an inverted comparison still reads fluently. The rules are therefore
in `gexdash/interpretation.py` — server-side, so they are testable — and each
directional claim has a test.

Three rules the text holds to:

1. **Restate, never predict.** "Dealers sell into strength" describes a
   hedging mechanic implied by positive gamma. It does not say the market
   will fall.
2. **Assumptions travel with the claim.** DEX is large by construction under
   an assumed dealer sign convention (§4), so that caveat renders beside DEX
   rather than in a footnote — and disappears under `all_long`.
3. **Nothing comparative in absolute currency.** "$8B of gamma" means nothing
   without knowing the instrument. Distances are percentages of spot, so they
   carry unchanged between SPY and NDX.

Proximity to a level is banded at **0.35%** of spot ("at") and **2%**
("near"). These are labelling choices, not measurements — 0.35% is roughly a
quiet index day's range — so the exact distance always prints beside the label
and the reader can disagree with the word while keeping the number.

With no open interest the reading is withheld entirely. Interpreting a surface
of zeros would produce a fluent description of a market that was never
measured.

## 11. The lunar-solar cycle

The Moon/Sun layer on both price charts (`cycles.py`, `astro.py`) is a model
of a kind this project otherwise avoids: one that projects price. It is held
to the same rule as the signal layer. Every step is written down and shipped
with the payload, and its out-of-sample record is printed where the projection
is drawn.

**The sky.** Positions come from Meeus's low-precision theory: the Sun from
chapter 25, and the Moon from chapter 47 with all 60 longitude/distance and 60
latitude terms. The worked examples are pinned in the tests. Against PyEphem
over 2016-2030 the largest differences were 94 seconds for lunar phases, 12
minutes for equinoxes and solstices, and 15 minutes and 10 km for perigee and
apogee. On bars of a day or longer all of that is invisible. Delta T is fixed
at 69 s.

**The fit.** Daily log returns are regressed on the cosine and sine of five
angles, each read at the session's 16:00 ET close:

| Cycle | Angle | Period | Zero at |
|---|---|---|---|
| Lunar phase | elongation | 29.53 d | new moon |
| Spring-neap tide | 2 × elongation | 14.77 d | new or full moon |
| Lunar distance | the Moon's mean anomaly | 27.55 d | perigee |
| Lunar declination | the Moon's longitude | 27.32 d | crossing the equator northward |
| Solar year | the Sun's longitude | 365.24 d | March equinox |

These are the actual angles, so the Moon's uneven motion is in the inputs;
they are not sine waves of fixed period. Features are centred on the fit
sample, so the cyclical part sums to zero over it and cannot absorb the drift.
The fit is ridge regression with a penalty of 0.02 per observation. Features
have variance ½, so that shrinks an isolated cycle by about 4%. Its real job
is the two lunar months 27.32 and 27.55 days long, which beat over 8.85 years
and would otherwise trade off with large opposite coefficients.

Futures are fitted on the raw front-month closes, with the return across each
contract change left out. Every other day's return is then exact, which a
back-adjusted series would not give: additive adjustment shifts each older
segment by a constant, and that rescales every earlier return.

**Always daily.** Weekly bars would put the 14.77-day term at the sampling
limit, and intraday history spans too few lunar months to fit. One fit is made
on daily bars and evaluated at whatever times a chart shows.

**The lines.** LSC is the fitted cyclical return summed over trading days and
centred. The 1st derivative is that return per trading day. The 2nd is its
rate of change, a central difference over ±6 hours of continuous time:
differencing consecutive sessions would triple every Monday's step, which
spans a weekend. Each line is divided by its own largest absolute value over
the fitted span and horizon, so the three share one ±1 pane. Their zero
crossings carry the information: LSC turns where the 1st derivative crosses
zero. Their relative heights do not, and `scale` gives the divisors.

**The projection** is the last close times exp(ΔLSC), at the fitted amplitude,
and the note states its range as a percentage of the last close. It is not
rescaled to look like a target. A turn is labelled once the path
has come back from it by 5% of its range (a zigzag), so a wiggle on a slope is
not a turn.

**In-sample over the bars.** The solid lines over past bars come from the
fit over the whole history, which has seen those returns. Any match between
past LSC turns and past price turns is therefore partly fitted, and is no
evidence the model works.

**The holdout** is the part to read first, and the only out-of-sample evidence. The model is fitted to the first
75% of the history and scored on the last 25%. The direction hit rate is not
compared with 50%: a model that mostly says "up" scores above half in a rising
market with no information at all. The comparison is with calls made blind to
the day in the same proportion, `u·a + (1 − u)(1 − a)` for up-call share `u`
and up-day share `a`. The correlation is compared with ±2/√n, inside which
zero lands 95% of the time. A correlation inside that band means the cycles
explained nothing the model had not seen, and the projection is then a
drawing of the fit, not a forecast.

Published work on lunar effects reports small return differences around new
and full moons (Dichev & Janes 2003; Yuan, Zheng & Zhu 2006). The model leaves
their sign and size to the data rather than building them in.

## 12. What this cannot do

| Tier | Metrics | Blocker |
|---|---|---|
| **B** | OI flow, volume bias, intraday OI change | Needs per-trade OPRA prints classified buy/sell against the NBBO, plus an opening-vs-closing heuristic |
| **C** | Speed engine, momentum, trading range | Needs underlying time-and-sales |
| **D** | Kinetics, dealer regime, zone state, king maker | Proprietary vendor labels — no API returns them; define your own thresholds |

These are sent by the API in an `unavailable` map with the reason and drawn as
dashed placeholders. An empty box reads as zero, and a plausible number would
be worse: in six weeks nobody remembers which tiles were real.
