# How to use GEXDASH

GEXDASH estimates where options dealers have to hedge (gamma, delta, vanna and
charm exposure by strike, the walls and the gamma flip) and charts trend and
signals beside it. This guide walks through installing it, connecting live
data, and reading every screen.

![An 18-second preview of GEXDASH, cut from the narrated tour](media/gexdash-tour.gif)

**Watch the narrated tour:** [gexdash-tour.mp4](media/gexdash-tour.mp4)
(3:04, with [subtitles](media/gexdash-tour.srt)). The video and the preview
above use live market data; the screenshots below use the built-in synthetic
data, so their prices are invented.

> **Not trading advice.** GEXDASH is an analysis tool. Every exposure figure
> rests on an assumption about who holds each side of the trade, which nobody
> publishes. See [the dealer sign convention](methodology.md#4-the-dealer-sign-convention).

## Contents

1. [Install and start](#1-install-and-start)
2. [Connect Schwab for live data](#2-connect-schwab-for-live-data)
3. [The sidebar and top bar](#3-the-sidebar-and-top-bar)
4. [Terminal view](#4-terminal-view)
5. [Columns view](#5-columns-view)
6. [Chart view](#6-chart-view)
7. [Drawing on the charts](#7-drawing-on-the-charts)
8. [Time zone](#8-time-zone)
9. [Light and dark](#9-light-and-dark)
10. [Links you can share](#10-links-you-can-share)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. Install and start

You need Python 3.11 or newer. Nothing else: no Node, no database server, no
build step.

```bash
git clone https://github.com/atminskymoment/gexdash.git
cd gexdash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
python -m gexdash.server           # or just: gexdash
```

Open <http://127.0.0.1:8420>. It starts on **synthetic data**, with no account
and at any hour, so you can explore everything first. While it is synthetic,
the top bar carries a **Synthetic data** badge and every chart says so too.

To use a different address or port, set `GEXDASH_HOST` or `GEXDASH_PORT`
(see [`.env.example`](../.env.example)).

## 2. Connect Schwab for live data

Schwab's API is free with a brokerage account and carries the same data as
thinkorswim.

1. Register an app at [developer.schwab.com](https://developer.schwab.com)
   (Market Data Production). Set its callback URL to
   `https://127.0.0.1:8182`. It must be `https`.
2. Copy `.env.example` to `.env` and fill in your keys:

   ```bash
   SCHWAB_APP_KEY=...
   SCHWAB_APP_SECRET=...
   SCHWAB_CALLBACK_URL=https://127.0.0.1:8182
   GEXDASH_PROVIDER=schwab
   ```

   `.env` is git-ignored. Never commit it.
3. Restart the server and open <http://127.0.0.1:8420/connect>.

![The connect page](images/connect-schwab.png)

4. **Authorize with Schwab** opens Schwab in a new tab. After you approve,
   the browser tries to load the callback and *fails to connect*. That is
   expected: the address bar still holds the code.
5. Copy that whole address, paste it into step 2 on the page, and press
   **Connect**. Do it straight away; the code is short-lived.

The access token refreshes itself. The **refresh token lasts 7 days**, and
Schwab offers no way around re-consenting. Two days before it expires, the
dashboard shows a *Reconnect* notice; follow it and repeat steps 4 and 5.
From a terminal, `python -m gexdash.providers.schwab login` does the same
([`autologin`](data-sources.md#schwab--free-and-the-recommended-start) skips
the paste).

**What works on Schwab:** exposure for SPX, SPY, NDX, QQQ and USO, and charts
for all five chart symbols. Schwab's option-chain API refuses the ES and NQ
futures, so their *exposure* is unavailable; their *charts* work. Details are
in [data sources](data-sources.md#schwab--free-and-the-recommended-start).

## 3. The sidebar and top bar

The page follows the [AffectUI](https://www.affectui.com/components) design
system: a sidebar for where you are, a slim top bar for what you are looking
at, and, on the Terminal and Columns views, one toolbar row for how the
exposure is computed.

**Sidebar**

| Item | What it does |
|---|---|
| **Terminal · Columns · Chart** | Switches the view. Your choice is remembered. |
| **Connect Schwab** | The [connection page](#2-connect-schwab-for-live-data). |
| **API docs · How to use** | The interactive API reference, and this guide. |
| **Collapse** | Shrinks the sidebar to an icon rail, with each item's name as a tooltip. Remembered; narrow windows start on the rail. |

**Top bar**

| Part | What it does |
|---|---|
| **Search** | The symbol of the view on screen: the Terminal and Columns symbol, or the Chart view's own. Press `/` from anywhere to jump to it. Suggestions list the registered symbols (SPX, SPY, ES, NDX, QQQ, NQ, USO) and ones you typed before; or type any symbol and press Enter: `AAPL`, `BRK.B`, `$RUT` for a cash index, `/CL` for a future. Escape puts back the one on screen. A typed symbol is priced on stated generic specs, shown in the strip; a typed future has charts but no exposure, because its multiplier is unknown. |
| **Clock and zone tag** | The time in the zone you pick (New York by default). The tag is the [zone picker](#8-time-zone). |
| **Phase** | Pre-market, open, after-hours or closed, and the time to the next change, on the exchange's clock. |
| **Status badge** | **Synthetic data** on the mock provider, otherwise the feed's name. |
| **◐** | Light or dark. |

**Toolbar** (Terminal and Columns)

| Part | What it does |
|---|---|
| **Expiry · Dealers · Flip** | The three settings that change every number, described under the [Terminal view](#4-terminal-view). |
| **Paused / Streaming** | Whether the surface is live-streaming. |
| **Stream** | Re-prices the surface on every spot tick, every 2 seconds; **Pause** stops it. |
| **Refetch** | Pulls a fresh option chain now. |
| **Table** | Opens the per-strike table. |

## 4. Terminal view

One instrument through the session: exposure across the top, candles with
the dealer levels in the middle, exposure by strike beside them.

![Terminal view, numbered](images/terminal-annotated.png)

1. **Views**, in the sidebar: Terminal, Columns and Chart.
2. **Clock and time-zone picker**, in the top bar.
3. **Search**, the symbol on screen. Type any ticker; `/` focuses it.
4. **Expiry, Dealers and Flip**, the toolbar's settings. These change every number on the page:
   - *Expiry window* (0DTE, ≤ 7, ≤ 30, ≤ 60 DTE, or all listed): which
     expiries count. It moves the result more than anything else; the
     default is ≤ 30 DTE.
   - *Dealer assumption*: who holds which side. The default, *long calls /
     short puts*, is the common convention and an assumption. *Raw netted*
     applies no sign flip.
   - *Flip method*: *Spot solve* finds the spot price where total gamma
     exposure crosses zero, re-pricing every contract; *Cumulative by
     strike* finds where the running sum by strike crosses zero.
5. **Stream, Refetch, Table**, at the right of the toolbar. **Stream** re-prices
   the surface every 2 seconds. **Refetch** pulls a fresh option chain now.
   **Table** opens the per-strike table.
6. **Exposure tiles.** Net GEX (dollar delta per +1% move in spot), net DEX,
   vanna, charm, the gamma flip, and put/call open interest.
7. **Range and interval** of the candles, from 1 day to 10 years and 1-minute
   to monthly. Combinations that can't be served are greyed out.
8. **Layers.** *Levels* draws the dealer levels; *Fit levels* stretches the
   price scale to include every level, even far ones; *VWAP*, *Crosshair* and
   *Readout* toggle what they say. RSI, MACD and ADX/DI toggle the panes
   underneath, and **Moon/Sun** adds the lunar-solar layer
   ([below](#moon-and-sun)): its pane, the projected path on daily, weekly
   and monthly bars, and the Moon in the readout.
9. **Dealer levels** on the price chart: call wall, put wall, gamma flip, max
   pain, the largest positive and negative gamma strikes, and the volume
   point of control. A level outside the visible range is pinned to the edge
   with an arrow rather than dropped.
10. **Readout.** The bar under the cursor (or the latest bar): open, high,
    low, close, RSI and its 20-bar average.
11. **Exposure by strike.** Gamma, delta, vanna, charm, volume and open
    interest, one tab each, with the flip and spot marked.
12. **Order flow and bias.** Session volume split by calls and puts, and bias
    signals: gamma regime, momentum, price against VWAP, volume flow,
    positioning, and whether price sits between the walls. Market internals
    (advancing and declining issues, TICK, TRIN, a session A/D line) sit
    below.

Hover the price chart for the crosshair and readout. Scroll over it to zoom,
drag to pan, and use **−**, **+** and **⤢** (fit) above it.

![Crosshair and readout over the price chart](images/terminal-price-panel.png)

Below the chart, a **written reading** puts the current exposure and levels
into words, for example "Dealers hedge with the move — trends extend". It is
produced by a fixed rule set, and it is withheld when the chain carries no
open interest to read.

Tiles that need data this tool doesn't have (classified trade prints,
time-and-sales, per-constituent quotes) are shown greyed with the reason,
never as a plausible-looking number.

## 5. Columns view

The same exposure data as the Terminal, laid out to read the whole surface at
once: market bias, spot and levels, the active level radar (every level in
price order with spot in position), and exposure by strike as small multiples.

![Columns view](images/columns.png)

The **Model and data provenance** panel at the bottom records every
assumption behind the numbers: expiry window, dealer assumption, flip method,
pricing model, rate and dividend, and when open interest was published.

## 6. Chart view

A full-width technical chart. Its five tabs pick **QQQ, SPY, USO, ES and NQ**,
and on this view the top bar's search sets its symbol to anything else. It
keeps its own symbol, separate from the Terminal's, and doesn't use the
option chain.

![Chart view, numbered](images/chart-annotated.png)

1. **Symbols.**
2. **Range and bar size.** Each bar size offers the ranges it can reach:

   | Bar size | Ranges |
   |---|---|
   | 1 min | 1D, 5D |
   | 5 min | 1D, 5D, 10D, 1M |
   | 15 min | 1D, 5D, 10D, 1M, 3M |
   | 30 min, 1 hour | 5D, 10D, 1M, 3M, 6M |
   | 4 hours | 1M, 3M, 6M |
   | Daily | 3M, 6M, YTD, 1Y, 2Y, 5Y |
   | Weekly | 6M, YTD, 1Y, 2Y, 5Y |

   1D, 5D and 10D count trading sessions, so 1D before Monday's open is
   Friday's session.
3. **Layers.** SMMA 20/50/100/200, EMA 9, Bollinger 20/2 and 20/3, ranges,
   levels, volume profile, signals, Supertrend and Moon/Sun each toggle on and
   off.
4. **Zoom.** **−**, **+** and **⤢** (fit). The mouse wheel zooms over the
   price pane; drag to pan.
5. **Legend.** Open, high, low, close and change for the bar under the
   cursor, and the value of every visible line.
6. **Signals.** BUY, ADD, TRIM and SELL follow one position at a time.
7. **Bases and breakouts.** A dotted box marks a consolidation base; a close
   above it is a Breakout, a close below it a Breakdown.
8. **Supertrend**, ported line for line from TradingView's script (ATR 10,
   multiplier 3). Its own Buy and Sell mark each flip.
9. **3-month and 52-week** highs and lows.
10. **Volume profile** over the range shown, with its point of control (PoC).
11. **Price tags** for the last price and every line, colour-matched.
12. **Lower panes**, continued below.

Every signal follows a written rule, listed under the chart in *How the
signals are defined*, so any mark can be checked by hand:

![The signal rules](images/signal-rules.png)

The same rules, as a table, are in the [README](../README.md#the-chart-view).

### Lower panes

Volume, RSI 14 with its 30/70 band, MACD 12/26/9, and two VAMS ribbons:
the VAMS check bar by bar, and the latched VAMS state. Blue is bullish, red
bearish, grey no call.

![Volume, RSI, MACD and the VAMS ribbons](images/chart-panes.png)

### Intraday

Intraday follows TradingView's default sessions: ETFs in regular hours
(**Ext hours** adds pre- and post-market), ES and NQ over the full electronic
session. A faint line marks each session break. Hourly and 4-hour bars start
at the session open (9:30, 10:30 … ET for ETFs; 18:00, 22:00 … ET for
futures). Intraday charts refresh every 30 seconds.

![QQQ, five days of 15-minute bars](images/chart-intraday.png)

### Moon and Sun

**Moon/Sun**, on the Chart view's layer buttons and beside RSI, MACD and ADX/DI
on the Terminal, adds the lunar-solar layer. It is off until you switch it on,
and then remembered.

- The chart runs on past the last bar into **future dates**. A dashed line
  marks today.
- A **dotted path** from the last close projects price, labelled "High Dec 22",
  "Low Dec 31" and so on at its turns. This needs daily, weekly or monthly
  bars; intraday charts get the pane alone.
- A **Lunar–solar cycle pane** draws LSC (orange) and its 1st (blue) and 2nd
  (green) derivatives, solid over the bars and dotted into the future. Each is
  scaled to ±1. Icons along its top mark new ●, quarter ◐ ◑ and full ○ moons,
  ☉ equinoxes and solstices, and P/A perigee and apogee. Hover one for its
  time.
- The **legend** (or the Terminal's readout) adds the Moon's phase, share lit,
  distance and declination, and the next new or full moon. Hover a future date
  to read its projected price.

The model is gexdash's own, listed with the other rules under the chart. Read
the note above the chart before the path. It reports how the model did on the
last quarter of the history, which it was not fitted to, against calls with no
skill. If the correlation there sits inside its noise band, the path is a
drawing of the fit, not a forecast.

### Futures and contract rolls

ES and NQ are **adjusted for contract changes** by default, as thinkorswim's
option of the same name does: before each quarterly roll (the Monday of
expiry week), prices are shifted so the last close meets the first open after
it. Without that, every roll prints a step that looks like a breakout.
**Adjust rolls** switches it off, and a small `roll` mark sits along the foot
of the price pane at each change.

![ES, two years of daily bars, with roll marks](images/chart-futures-rolls.png)

## 7. Drawing on the charts

Both price charts, the Terminal's and the Chart view's, have a drawing
toolbar down their left side. A drawing belongs to its symbol, not to the
chart: draw on QQQ in the Chart view and it is on QQQ in the Terminal too, at
any bar size. Drawings are kept in this browser. A point outside the bars a
chart has loaded (a drawing from a 1Y chart viewed on 3M, or one past the
last bar) is placed by the calendar: weekdays for daily bars with holidays not
modelled, and on intraday bars by the bar size, without a session calendar.
There it is close, not exact.

| Group | Tools |
|---|---|
| Cursors | Cross, Dot, Arrow (crosshair lines hidden), Eraser |
| Lines, channels, pitchforks | Trend line, Ray, Extended line, Info line, Horizontal line and ray, Vertical line, Cross line, Parallel channel, Regression trend (±2σ), Pitchfork, Schiff and Modified Schiff pitchforks |
| Fibonacci and Gann | Fib retracement, Trend-based fib extension, Fib channel, Fib time zone, Fib speed resistance fan, Fib circles, Gann box, Gann square, Gann fan |
| Patterns, Elliott waves, cycles | XABCD and ABCD (with their ratios), Triangle pattern, Head and shoulders, Elliott impulse (1-5), correction (A-B-C), triangle (A-E), double (W-X-Y) and triple (W-X-Y-X-Z) combos, Cyclic lines, Time cycles, Sine line |
| Forecasting, volume, measures | Long and Short position (reward, risk, R/R), Forecast, Anchored VWAP, Fixed range volume profile (POC, 70% value area), Price range, Date range, Date and price range |
| Brushes, arrows, shapes | Brush, Highlighter, Arrow, Arrow marks up and down, Rectangle, Circle, Ellipse, Triangle, Path, Curve |
| Then | Magnet (snap to the bar's open, high, low or close), **Lock all**, **Hide all**, Remove (the selected drawing, or all of the symbol's with a second click) |

**Placing.** Open a group, pick a tool, then click each point. For two-point
tools you can also press, drag and release. A Path finishes on a double-click
or Enter. Afterwards the pointer goes back to the cursor you last used.
Points snap to bars, and with **Magnet** on, price snaps to the nearest open,
high, low or close of that bar. You can draw past the last bar: future dates
follow the Moon/Sun layer's rule (weekdays for daily bars, weeks, months).

**Editing.** With a cursor tool, click a drawing to select it: its points get
handles. Drag a handle to move that point, drag the drawing to move all of
it, press Delete to remove it, or use the Eraser. Escape cancels a drawing in
progress or clears the selection. **Lock all** stops drawings being selected,
moved or erased; new ones can still be drawn. **Hide all** hides them; picking
a drawing tool shows them again. The bin removes the selected drawing, or,
with none selected, every drawing of the symbol once clicked twice.

**Cursors.** Cross is the usual crosshair; Dot shows a dot for the pointer;
Arrow is the plain pointer with the crosshair lines hidden (the legend and
readout still follow it).

**Definitions that differ between platforms**, as gexdash draws them (each
tool's tooltip says the same):

- *Fib retracement* runs from the swing's start, level 1, to its end, level 0:
  0.618 is a 61.8% pullback from where the move ended.
- *Trend-based fib extension* projects the move from point 1 to point 2 from
  point 3.
- *Schiff pitchfork* starts its median halfway between points 1 and 2 in
  price, at point 1's time; *Modified Schiff* halfway in both time and price.
- *Gann fan*: point 2 sets the 1x1; "1x2" is one unit of price per two of time.
- *Regression trend*: least squares of the closes between the points, with
  ±2 standard deviations of the residuals.
- *Fixed range volume profile*: 24 rows, each bar's volume spread over the
  prices it traded through, the fullest row as the POC and the 70% around it
  as the value area.
- *Fib circles* and *Circle* are round on screen, so they change shape against
  the prices when you zoom.

## 8. Time zone

Click the zone tag beside the clock (it reads **ET** by default). Every time
on screen follows your choice: the clock, intraday axis times, crosshair tags,
the readout, the legend and signal tooltips.

![The time-zone picker](images/time-zone-picker.png)

- **New York** is the exchange's own time and the default.
- **Local** follows your computer, including when you travel.
- The rest are the main market centres. Any other zone works from a link:
  `?tz=America/Sao_Paulo`.

Sessions stay the exchange's: hourly bars still start at 9:30 New York time,
and daily bars keep their dates in every zone. A session divider names its
trading day, so in Tokyo a bar from Monday's US session reads Tuesday morning
under a "Mon" divider. That is correct.

## 9. Light and dark

**◐** in the top bar switches theme; `?theme=dark` links it. Both palettes
were checked for colour-vision deficiency, and calls and puts are blue and
orange, never green and red.

![Dark theme](images/chart-dark.png)

## 10. Links you can share

Every screen can be linked. A link's parameters apply to that page; only
`view` is also remembered as your choice.

| Parameter | Values | Example |
|---|---|---|
| `view` | `terminal` `columns` `chart` | `?view=columns` |
| `symbol` | any symbol for Terminal and Columns | `?symbol=QQQ`, `?symbol=AAPL` |
| `theme` | `light` `dark` | `?theme=dark` |
| `tz` | `exchange`, `local`, or any IANA zone | `?tz=Europe/London` |
| `chart` | any symbol for the Chart view | `?view=chart&chart=USO`, `&chart=/CL` |
| `chart_range`, `chart_interval` | as in the table above | `&chart_range=2Y&chart_interval=1W` |
| `chart_session` | `rth` (regular hours) or `eth` (extended), ETFs only | `&chart_session=eth` |
| `chart_adjust` | `on` or `off`, ES and NQ only | `&chart_adjust=off` |
| `chart_astro` | `on` or `off`: the Chart view's Moon/Sun layer | `&chart_astro=on` |

The HTTP API behind the dashboard is documented in [api.md](api.md), and
interactive OpenAPI docs are served at `/api/docs` while it runs.

## 11. Troubleshooting

| What you see | What it means |
|---|---|
| `SYNTHETIC — NOT MARKET DATA` | You're on the mock provider. Follow [section 2](#2-connect-schwab-for-live-data). |
| "Schwab credentials are configured but not connected" | Open `/connect` and authorize. |
| "Refresh token expires in N days" | Reconnect at `/connect` before it lapses; Schwab requires it every 7 days. |
| ES or NQ exposure unavailable on Schwab | Expected: Schwab's chain API refuses futures options. Use the Chart view for ES and NQ. |
| `STALE CHAIN` or a degraded-feed notice | The latest fetch failed, or came back without open interest, so the last usable chain is shown with its age and the reason. Retries back off on their own; *↻ refetch* retries now. |
| A greyed interval in the Terminal | That bar size can't cover that range; pick a coarser one. |
| "Price history unavailable" | The provider can't serve bars for that symbol or range; the reason is shown in place. |
| DEX looks enormous | A consequence of the default dealer assumption, explained in [methodology §4](methodology.md#why-dex-looks-enormous). |
| `Address already in use` | Another program holds port 8420: `GEXDASH_PORT=8430 python -m gexdash.server`. |
