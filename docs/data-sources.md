# Data sources, entitlements and fees

## What tier A actually needs

Two things, and they have completely different refresh characteristics:

1. **One option chain per day**, carrying strike, expiry, right, IV, open
   interest and volume. Open interest is an OCC once-daily figure, so pulling
   it more often buys nothing.
2. **A live spot price.**

The provider interface is exactly those two methods, which is what makes
swapping vendors a single class:

```python
class ChainProvider(Protocol):
    def get_chain(self, instrument, *, max_dte=None) -> OptionChain: ...
    def get_spot(self, instrument) -> float: ...
```

### Optional: price history

The terminal view's candle panel needs intraday bars, which is a third data
source. It is deliberately a **separate, optional** protocol rather than a
third mandatory method:

```python
class PriceHistoryProvider(Protocol):
    def get_price_history(self, instrument, *, interval_minutes=1,
                          lookback_hours=7.0) -> PriceBars: ...
```

A provider that cannot serve bars raises `PriceHistoryUnavailable`, the
endpoint returns `200` with a reason, and the UI renders that reason where the
chart would be. Nothing else on the dashboard depends on it — the exposure
surface never needed price history. Schwab implements it for the index and ETF
symbols via `/marketdata/v1/pricehistory`. The futures roots `/ES` and `/NQ`
are requested too, and the response decides what happens. A feed error or an
empty series is reported with the feed's reason. A series whose last price is
outside the instrument's `plausible_spot` band is refused, because a root that
resolves to another security (bare `ES` is Eversource Energy) would otherwise
chart perfectly plausibly. A series that passes is served with
`meta.unverified` set, and the UI shows that note.

Checked against the live endpoint on 2026-11-23. `/ES` and `/NQ` return about
ten years of daily bars and twenty of weekly, with no missing sessions. The
series is the **front month, not back-adjusted**. It rolls on the Monday of
expiry week, and each roll leaves an upward step equal to the calendar spread:
0.5–1.5% of price in 2023–2026. Unadjusted, that skews the Chart view's rules.
In five years of daily bars, about 230 signals across ES and NQ, four
breakouts fell on a roll day with no SPY/QQQ breakout within three days
(ES 2024-03-12, 2025-09-15; NQ 2023-09-11, 2026-06-15). USO's history is
split-adjusted: its 1-for-8 reverse split in April 2020 leaves no step.

**Adjusting for contract changes** (`futures.py`, on by default for ES and NQ
on both charts; `adjust=false` turns it off). This follows thinkorswim's chart
option of that name. Its manual defines it as adjusting "all price data prior
to the active contract change so that the last close before the contract
change is at the same level as the first open after it". The gap is
therefore Friday's close to the Sunday-evening open, weekend move included,
and it is removed additively, cumulatively from the newest roll back. The
manual's wording also fits a ratio adjustment, and `back_adjust(method="ratio")`
is one switch away.

Where the rolls are was established against SPX and NDX, which have neither a
roll nor a dividend (SPY and QQQ go ex-dividend in expiry week, which spoils
them as a reference):

- **December 2022 to September 2026:** every quarter, on both contracts, the
  step falls on the Monday of expiry week (0.56–1.5%, well above day-to-day
  noise of about 0.3%) and on no other day near expiry.
- **Before mid-2022:** rates were near zero, and so was the step; no day
  stands out. The same rule is applied there because it is the only evidence,
  and the chart counts how many of the rolls it shows are from that period.
- **Expired contracts can't settle it:** `/pricehistory` serves the live
  contracts (`/ESZ26`, `/ESH27`) but returns empty for expired ones (`/ESU26`,
  `/ESH22`).

With the adjustment on, the roll-day steps against the index fall from about
+1% to a mean of +0.08%, the latest price is unchanged, and three of the four
roll-day breakouts above disappear. The one left (ES 2025-09-15) sits on a
roll that adjusted cleanly. Values to compare with thinkorswim's own chart,
ES daily close:

| Date | Unadjusted | Additive (default) | Ratio |
|---|---|---|---|
| 2025-11-24 | 6,725.00 | 6,942.50 | 6,931.59 |
| 2023-11-27 | 4,561.50 | 5,221.00 | 5,088.59 |
| 2021-11-26 (roll dates unverified) | 4,579.00 | 5,419.75 | 5,332.57 |

#### What `/pricehistory` actually accepts

Established by probing the live endpoint, not from documentation. The
combinations matter because the wrong one is not an error — it returns a
perfectly good chart covering the wrong span.

**Bound requests with `startDate`/`endDate`, not `periodType`/`period`.**
This is the single most important thing to know about the endpoint. The
`period` form quantises to fixed buckets — `periodType=day` maxes out at 10 —
so reading the limits off it produces "minute bars stop at 10 days", which is
a property of the *parameter*, not of the feed. With explicit dates the same
endpoint serves 30-minute bars over eight months.

Once dates are supplied, `periodType` is only validated against
`frequencyType` and no longer bounds the window: `year` is accepted alongside
daily, weekly and monthly alike. Only `month` + `monthly` is rejected outright.

| Bar size | Accepted `periodType` | Reach with explicit dates |
|---|---|---|
| 1 minute | `day` | **~48 days** |
| 5 / 10 / 15 / 30 minute | `day` | **~260 days** |
| daily | `month`, `year`, `ytd` | 10 years |
| weekly | `month`, `year`, `ytd` | 20 years |
| monthly | `year` **only** | 20 years |

Measured against SPX, SPY, NDX and QQQ by requesting a 400-day window and
reading back what arrived. For that window all four symbols returned exactly
12,870 one-minute candles (33 sessions × 390 RTH minutes). That looked like a
per-request candle cap, but it is not one: a 30-day window later returned
29,112 candles ending at the current minute. The coarser sizes all stopped on
the same calendar date with counts from 13,746 down to 2,293, so theirs is a
date horizon.

Two further consequences the code carries:

- **`frequency` 60 and 240 are rejected.** Hourly, 2-hour and 4-hour bars are
  aggregated locally from 30-minute bars, the largest native size.
- **Candle volume depends on the extended-hours flag.** The terminal's
  intraday requests set `needExtendedHoursData=true`, which roughly triples the
  count: a 10-day 1-minute request returns 2,730 candles RTH-only and 12,760
  with extended hours. Any estimate of response size has to account for it.
  With the flag off, QQQ 5-minute bars run 09:30 to 15:55 ET (checked live on
  2026-11-23); the Chart view uses that for ETFs, TradingView's default
  session. With it on, QQQ starts at 07:00 ET.
- **Without `endDate` the window ends at the previous session's close.** The
  period form (`periodType=day&period=1`, no dates) on Monday 2026-11-23 at
  13:40 ET returned Friday's session and nothing from Monday. Both charts now
  send an explicit window ending now. For 1D/5D/10D the window covers N + 2
  weekdays, and the server keeps the last N sessions present in the bars, so
  before Monday's open 1D is still Friday. (Sending `endDate` with the period
  form counts business days correctly for ETFs, but for `/ES` a 2-day period
  returned only the current session, so it is not used.)

There are **no sub-minute bars at any period**. One-second candles need a tick
or streaming feed; the request is refused with that reason rather than
silently rounded up to a minute.

## Schwab — free, and the recommended start

Free with a brokerage account, and the same data behind thinkorswim. It is a
brokerage API with rate limits rather than a market-data firehose, so
chain-wide polling lands in seconds — fine here, because OI only moves daily.

```bash
# 1. Register an app at https://developer.schwab.com (Market Data Production)
# 2. Set the callback URL to https://127.0.0.1:8182  (must be https)
export SCHWAB_APP_KEY=...
export SCHWAB_APP_SECRET=...
export SCHWAB_CALLBACK_URL=https://127.0.0.1:8182

python -m gexdash.providers.schwab login    # paste the redirect URL
export GEXDASH_PROVIDER=schwab
```

Or skip the paste: `autologin` listens on the callback port and exchanges the
code the moment Schwab redirects. Its local HTTPS listener needs a
self-signed certificate, which needs the optional `cryptography` package:

```bash
pip install -e ".[autologin]"
python -m gexdash.providers.schwab autologin
```

The access token lasts 30 minutes and refreshes automatically. **The refresh
token lasts 7 days**, after which `login` must be re-run — Schwab provides no
way around that re-consent. If credentials are missing the app falls back to
the mock provider with a warning rather than refusing to start.

Schwab quirks handled in the client:

- `volatility` is returned in **percent** (13.5 = 13.5%), scaled to a decimal.
- `-999.0` is the sentinel for "unavailable" across numeric fields, including
  greeks and open interest. Mapped to `None`, not treated as a value.
- The `openInterest` field is the OCC prior-session figure by construction.

### ⚠ Futures (ES, NQ) exposure does not work on Schwab

**SPX, SPY, NDX, QQQ and USO work end to end. ES and NQ do not yet, and you should
assume they need work.** The exposure maths for futures — Black-76, ×$50 / ×$20,
futures price as the underlying — is implemented and tested, but only against
the mock provider. Two reasons to expect the Schwab leg to fail:

- `/ES` and `/NQ` are continuous-contract *aliases*, not tradeable contracts.
  A dated root (`/ESU26`) is probably required.
- Schwab's `/marketdata/v1/chains` documents equity and index options;
  futures-options coverage there is undocumented.

Requesting a futures chain prints a warning saying so. Verify with
`SchwabProvider().get_chain(instruments.ES)` once authenticated. If it does
not resolve, route the futures legs to a CME-carrying provider.

**Status on 2026-11-23:** `/chains` answers `400 Check Param Values` for both
`/ES` and `/NQ`, so the futures exposure legs do not work on Schwab. Their
*price history* does (see *Optional: price history* above).

### Optional: market internals

Advance/decline comes from the **exchange breadth symbols** that thinkorswim
and the Schwab API expose as ordinary quotables, so the whole panel costs one
batched `/quotes` call rather than 500 per-constituent quotes:

| Metric | NYSE | Nasdaq |
|---|---|---|
| advancing | `$ADVN` | `$ADVQ` |
| declining | `$DECN` | `$DECQ` |
| net (cross-check) | `$ADD` | `$ADQDC` |
| up / down volume | `$UVOL` / `$DVOL` | `$UVOLQ` / `$DVOLQ` |
| TICK / TRIN | `$TICK` / `$TRIN` | `$TICKQ` / `$TRINQ` |

Two things to know:

- **These are exchange-wide, not index-constituent.** `$ADVN`/`$DECN` count
  every NYSE issue — roughly 3,000 lines including ETFs, preferreds and
  closed-end funds — not the 503 S&P 500 members. Pairing NYSE breadth with an
  SPX exposure surface is the standard convention and still a proxy; many S&P
  members are Nasdaq-listed. The UI names the exchange, not the index.
- **Every metric resolves independently.** A ticker your entitlement does not
  carry degrades that one field and is reported by name, rather than failing
  the panel. Correct it in `BREADTH_SYMBOLS` in `gexdash/breadth.py`.

The counts are fetched separately from the net symbol and reconciled against
it. A mismatch means a symbol is mapped to the wrong series — which would
otherwise produce entirely plausible numbers, this project's recurring failure
mode.

### Open interest can arrive empty

Not a hypothetical. Schwab has served a complete, well-formed SPX chain of
16,862 contracts with open interest on **2** of them — 896 in total, none
inside the ≤30 DTE window — with volume populated and no error anywhere.

Every exposure measure is proportional to OI, so this renders as a dashboard
of `$0` tiles: a confident claim about the market rather than a failed fetch.
It appears to happen around the session boundary and clears on its own.

`CachedProvider` detects it by OI *coverage*, keeps serving the last usable
snapshot, and never writes the empty chain to the cache. See
[methodology §9](methodology.md#9-detecting-a-chain-that-is-not-there) for the
threshold and the measurements behind it.

**Licensing note:** the breadth symbols are exchange *equity* data (CTA/UTP),
a different tape from OPRA. The fee section below is OPRA-specific; live
equity quotes carry their own separate licensing with the same
personal-versus-redistribution cliff.

## Other providers

Prices as of research in August 2026 — **re-check before committing**, these
move, and the Polygon→Massive rebrand in particular.

| Provider | Price | Notes |
|---|---|---|
| **Schwab Trader API** | free w/ account | Chains + greeks + WebSocket streaming. Rate-limited. |
| **ThetaData** | $40 / $80 / $160 per month | Consolidated OPRA, daily OI, 1st–3rd order greeks. Best price-to-capability for this use case. |
| **Databento** | OPRA Standard $199/mo, metered | Institutional tick fidelity, MBO. Also carries CME Globex, so it can feed both the index and futures legs from one vendor. |
| **Massive** (ex-Polygon.io, renamed Oct 2025) | Options $29 / $99 / $199 | Billed per asset class. |
| **Cboe All Access / DataShop** | varies | Authoritative, direct from the exchange where SPX trades. Real-time SPX bid/ask index values need a Cboe Global Indices licence from ~$1k/month. |
| **CME DataMine / dxFeed** | varies | For ES/NQ. No OPRA fees — CME has its own entitlements. |

## OPRA fees — the 1000× cliff

These are exchange subscriber fees, **separate** from whatever you pay a
vendor:

| Usage | Cost |
|---|---|
| Non-professional (personal) | **$1.25** / user / month |
| Professional (business use, even solo, no redistribution) | **$31.50** / user / month |
| Redistributing to any other user | **$1,500** / month ($650 query-only) + per-end-user fees |
| Non-display / algorithmic consumption | **~$2,000** / month per category, and categories stack |
| Delayed 15+ minutes | free |

For personal use this rounds to zero — your cost is the vendor subscription.
**The moment a second person sees the screen it is ~1000×.**

Build personal, but keep the data layer behind the provider interface and do
not architect around free redistribution. Note also that cached chains under
`GEXDASH_HOME` are licensed market data on a real account; `.gitignore`
excludes `*.db` for that reason as well as size.

## The mock provider

Runs with no credentials and no market hours, so the dashboard and the full
test suite work offline. It generates a chain *shaped* like a real one —
volatility skew, OI clustered on round strikes, deliberate walls, volume
concentrated near the money, per-instrument vol levels — but **the numbers are
invented**. The UI shows `SYNTHETIC — NOT MARKET DATA` whenever it is active.

Everything is seeded, so the dashboard renders identically on every run. That
matters when eyeballing a layout change: you need to know the data did not
move under you.

## Instrument reference

| Key | Instrument | Multiplier | Model | Exercise | Settlement |
|---|---|---|---|---|---|
| `SPX` | S&P 500 Index | ×100 | Black-Scholes | European | cash |
| `SPY` | SPDR S&P 500 ETF | ×100 | Black-Scholes | **American** | physical |
| `ES` | E-mini S&P 500 future | ×$50 | Black-76 | **American** ¹ | futures |
| `NDX` | Nasdaq-100 Index | ×100 | Black-Scholes | European | cash |
| `QQQ` | Invesco QQQ Trust | ×100 | Black-Scholes | **American** | physical |
| `NQ` | E-mini Nasdaq-100 future | ×$20 | Black-76 | **American** ¹ | futures |

¹ CME quarterly and serial options on E-mini futures are American-style; the
weekly and end-of-month contracts are European. The family is labelled by its
quarterlies, which carry the bulk of the open interest driving an exposure
surface.

Specs verified against the exchanges rather than from memory — a wrong
multiplier or exercise style is a silent error, the output stays smooth and
plausible:

- **CME** — E-mini Nasdaq-100 is $20 × index; quarterly options on E-mini
  futures are American-style, weeklies and EOM European.
- **Nasdaq / Cboe** — NDX is $100, European, cash-settled, and explicitly
  marketed on having *no early-assignment risk*. Do not "correct" NDX to
  American for consistency with the CME futures instruments: different
  product, different rule.

### Overriding exercise style

Any instrument can be flipped without editing code:

```bash
export GEXDASH_EXERCISE_OVERRIDES="NDX=american,NQ=european"
```

Useful when your chain is dominated by an expiry type whose rule differs from
the family default — CME weeklies being European is the realistic case. A
malformed entry, an unknown instrument, or an unrecognised style raises at
import rather than being skipped: an override that silently does nothing is
worse than a crash, because you would go on believing it applied.

Each instrument carries its own default dividend yield. Applying the S&P's
1.3% to QQQ (~0.5%) would be simply wrong, and the provenance panel reports
which yield was used and whether it came from the instrument or an override.
