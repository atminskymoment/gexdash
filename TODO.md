# To do

Known bugs, most visible to traders first. Found during the code review of
23 Nov 2026; none of them is fixed yet.

- [ ] **The Columns view shows "$0" exposure when the chain has no open
  interest.** The Terminal correctly shows "n/a", because "$0" reads as a
  fact about the market.
  Where: `renderBias` in `gexdash/static/app.js` ignores `d.oi_unavailable`.

- [ ] **"% over session" in the bias panel is measured over the whole loaded
  range**, such as 1Y, not over the session. Relatedly, the panel isn't
  redrawn when bars arrive, so with the stream paused, Momentum and VWAP can
  say "needs price history" after bars have loaded.
  Where: `renderOrderFlow` and `loadBars` in `gexdash/static/app.js`.

- [ ] **The Terminal chart's zoom resets every 30 seconds while streaming.**
  The Chart view keeps its zoom when it refreshes the same series.
  Where: `loadBars` in `gexdash/static/app.js` sets `state.zoom = null` on
  every call, the timed refresh included.

- [ ] **Network errors come back as a generic server error instead of
  "provider failed".** Timeouts and refused connections are never wrapped as
  `ProviderError`, so:
  - `/api/spot` and `/api/exposure` return 500 instead of 502;
  - the live-spot fallback in `_build_payload` doesn't kick in;
  - `/api/history`, `/api/technicals` and `/api/breadth` return 500 instead
    of `{"available": false}` with a reason.

  A cached chain still covers it. `docs/api.md` notes the current behaviour.
  Where: `_get` and `SchwabAuth.refresh` in `gexdash/providers/schwab.py`.

- [ ] **`crosscheck` crashes when the provider gives only one of delta or
  gamma.** `CrossCheckReport.__str__` raises a TypeError in either case.
  Where: `gexdash/crosscheck.py`.

- [ ] **Market internals can be cached under the wrong exchange.**
  `?symbol=SPX&exchange=NASDAQ` shows the NYSE table and caches it under
  NASDAQ for 60 seconds.
  Where: `/api/breadth` in `gexdash/server.py` takes the breadth symbols from
  the instrument's family, not from the exchange queried.

- [x] **The optional `autologin` command needs a package `pyproject.toml`
  doesn't list.** `oauth_loopback.py` imports `cryptography`. The `/connect`
  page and `python -m gexdash.providers.schwab login` don't need it.
  Fixed: it is the `autologin` extra (`pip install -e ".[autologin]"`), part
  of `dev` and installed in CI. It had failed CI on a clean install, and
  without it `autologin` now stops before opening the browser and says what
  to install.

- [ ] **In winter, synthetic 0DTE options expire an hour early.**
  `SETTLEMENT_HOUR_UTC = 20.0` in `gexdash/chain.py` is 16:00 ET only under
  daylight saving; from November to March it is 15:00 ET. It applies only to
  bare-date expiries, such as the mock provider's. Live data isn't affected,
  because Schwab sends full timestamps.

## Lower priority

### Dashboard

- [ ] **Bars and breadth don't discard out-of-date responses.** When the
  symbol changes quickly, an older response that arrives last overwrites the
  newer one. (The startup case, a request sent before the symbol list
  loaded, is fixed.)
  Where: `loadBars` and `loadBreadth` in `gexdash/static/app.js`; a request
  counter or `AbortController` would do.

- [ ] **Server error messages are lost on the Terminal's chart and internals
  panels.** `loadBars` and `loadBreadth` don't check `res.ok`, so for a 400
  the body carries `detail` but no `reason`, and the panel shows no reason.
  Where: `gexdash/static/app.js`; `load()` already does this right.

- [ ] **The price-chart title starts as "· 1-minute" whatever the interval.**
  It is corrected only once bars are available.
  Where: `renderTerminal` in `gexdash/static/app.js`.

- [ ] **The Terminal's time-axis labels fall every N bars, not on round
  times** (09:30, 10:09, 10:48 …). The Chart view's axis uses round clock
  times; the Terminal could share that logic.
  Where: `renderPriceChart` in `gexdash/static/app.js`.

- [ ] **Session dividers on the Chart view's intraday axis sit a fraction of a
  bar out.** The axis works out bar width as `innerW / n`, ignoring the
  chart's right-hand padding, so clock-label spacing uses the wrong width
  too.
  Where: `techTimeAxis` in `gexdash/static/technicals.js`.

- [ ] **No recovery on daily bars when a remembered range is rejected.**
  `loadTechnicals` falls back to 1D / 1Y only when the interval isn't 1D, so
  on daily bars the chart just shows "unavailable".
  Where: `loadTechnicals` in `gexdash/static/technicals.js`.

- [ ] **A fallback to the chain's own spot isn't visible.** When the live spot
  fetch fails, `_build_payload` uses the chain's spot, and its comment says
  the UI flags the staleness, but no payload field marks it and the page
  doesn't detect it.
  Where: `_build_payload` in `gexdash/server.py`, and `gexdash/static/app.js`.

### API and server

- [ ] **Parameter validation gaps.**
  - An unknown `flip_method` is silently run as `spot_solve`
    (`gexdash/exposure.py`).
  - An unknown `dealer_sign` returns 422, not 400 like a bad
    `expiry_window` (`_config` in `gexdash/server.py`).
  - On `/api/stream`, a bad `dealer_sign` sends an error frame on every tick
    instead of rejecting the request.

- [ ] **`/api/refresh` gives no stale flag when it served a fallback
  snapshot.**
  Where: `api_refresh` in `gexdash/server.py`.

- [x] **`/connect` is served without the no-cache header** that
  `_RevalidatingStatic` calls a correctness requirement for the static
  files, so a browser can show a stale connect page after an update.
  Fixed: it is served `no-cache, must-revalidate`, like the dashboard.

### Schwab login

- [ ] **The login failure hints point to a check that doesn't exist.** They
  say credentials are fine if `status` showed "Basic auth accepted", but
  `status` never tests Basic auth.
  Where: the hint in `gexdash/server.py` (token exchange) and the two
  messages in the `login` and `autologin` commands in
  `gexdash/providers/schwab.py`.

- [ ] **A login from the command line isn't picked up by a running server.**
  The server keeps its in-memory token until a restart or a logout.
  Where: `SchwabAuth._load` in `gexdash/providers/schwab.py`, which doesn't
  re-read the token file.

- [ ] **The `autologin` result page puts error text into HTML unescaped.**
  `error_description` from the redirect and the token-exchange error are
  inserted as they come. Low impact: it listens only on 127.0.0.1.
  Where: the request handler in `gexdash/oauth_loopback.py`.

- [ ] **`autologin` reuses its self-signed certificate after it expires.**
  The cached certificate is used whenever the files exist, whatever its
  dates.
  Where: `ensure_self_signed_cert` in `gexdash/oauth_loopback.py`.
