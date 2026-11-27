/* gexdash technical chart: the "Chart" view.
 *
 * An intraday, daily or weekly chart for QQQ, SPY, USO, ES and NQ. It draws SMMA
 * 20/50/100/200, EMA 9, Bollinger 20/2 and 20/3, volume with a volume
 * profile, RSI 14, MACD 12/26/9 and two VAMS ribbons, plus range boxes,
 * 3M/52W levels, BUY / ADD / TRIM / SELL / Breakout / Breakdown markers and a
 * Supertrend port. Which bar sizes, ranges and sessions exist comes from the
 * server (`caps` in each payload, and /api/meta), not from a list kept here.
 *
 * Every number is computed server-side (/api/technicals, technicals.py) and
 * this file only draws. The signal rules are gexdash's own: they are written
 * out in the payload and shown under the chart, and they are not the private
 * script the reference chart was drawn with.
 *
 * Load order: this file loads BEFORE app.js and only defines things. app.js
 * owns startup and calls initTechnicals() / loadTechnicals() /
 * renderTechnicals(). Every top-level name carries a tech/TECH_ prefix,
 * because classic scripts share one global scope and a duplicate `const`
 * would stop app.js loading at all.
 *
 * One SVG holds every pane, so the crosshair is a single vertical line rather
 * than a sync across separate SVGs. Its state lives on `tech`, not on
 * `state.chart`/`state.xhair`. The terminal chart's hideCrosshair() clears
 * every [data-xhair] on the page, so this chart marks its own with
 * [data-txhair] instead.
 *
 * Sections, in order: formatting; controls (initTechnicals and the buttons);
 * fetch (loadTechnicals and its timer); render (renderTechnicals, the axis
 * tags, the date and time axes); legend and pane titles; crosshair, pan and
 * zoom.
 *
 * The payload, per bar: `time`, `open`, `high`, `low`, `close`, `volume`;
 * `series` (smma20/50/100/200, ema9, bb20_2_upper/lower, bb20_3_upper/lower,
 * rsi, macd, macd_signal, macd_histogram, st_up/st_dn, among others); and
 * `vams.state` / `vams.check` (+1 / 0 / -1). Missing values are null. Not per
 * bar: `signals` and `supertrend.signals` ({i, type, price}), `ranges` and
 * `current_range` (start/end/high/low), `levels`, `vams.current`, `profile`
 * (edges, up, down, poc), `session_breaks` / `session_labels`, `rolls`,
 * `rules`, `notes` and `caps`. Every index is into the displayed window, not
 * the longer history the indicators were computed over.
 *
 * Moon/Sun (the `astro` layer, off by default, fetched with astro=true): the
 * lunar-solar block is drawn by astro.js. Its future dates are extra slots
 * after the last bar, so `total` slots include them while `hb`, in the render,
 * is where the candles stop; techSlotTime() gives any slot's stamp.
 *
 * Borrowed from app.js, which loads after this file, so only at call time:
 * `$`, `esc` and `state`. tz.js and astro.js load first; their helpers are
 * used directly.
 */

/** The quick-pick tabs. Any other symbol is typed into the box beside them. */
const TECH_SYMBOLS = ["QQQ", "SPY", "USO", "ES", "NQ"];
/** Futures are drawn over their full session; the regular/extended toggle is
 *  for ETFs. ES and NQ are also the futures whose quarterly rolls can be
 *  adjusted; a typed future (/CL) is full-session but never roll-adjusted. */
const TECH_FULL_SESSION = new Set(["ES", "NQ"]);
/** What a typed symbol may look like, as instruments.SYMBOL_PATTERN; the
 *  server has the last word. */
const TECH_SYMBOL_RE = /^[$/]?[A-Z][A-Z0-9.]{0,11}$/;

/** A future: ES, NQ, anything typed with a leading "/", or whatever the
 *  latest payload for this symbol says. */
const techIsFuture = (sym) => TECH_FULL_SESSION.has(sym) || String(sym).startsWith("/") ||
  (tech.data?.symbol === sym && !!tech.data.future);
/** A future on the quarterly equity-index calendar, so Adjust rolls applies. */
const techRollsAdjustable = (sym) => TECH_FULL_SESSION.has(sym) ||
  (tech.data?.symbol === sym && !!tech.data.quarterly_rolls);
/** Empty space past the last candle, in pixels. Sized for the named level
 *  boxes ("VAMS→Bearish") that sit just left of the axis: a pad counted in bar
 *  slots shrinks to ~30px on a year of dailies, and the boxes then cover the
 *  newest candle -- the one read most. */
const TECH_RIGHT_PAD_PX = 112;
/** The timer ticks at the intraday cadence; daily and weekly charts reload on
 *  every other tick. The server's caps carry the same two numbers. */
const TECH_TICK_MS = 30_000;
const TECH_DAILY_RELOAD_MS = 55_000;

const tech = {
  symbol: "QQQ",
  range: "1Y",
  interval: "1D",
  session: "rth",          // ETFs only: "rth" | "eth"
  adjust: true,            // futures only: adjust for contract changes
  caps: null,              // from the server: intervals, their ranges, sessions
  loadedAt: 0,
  data: null,
  // Visible window as fractional indices into the bar arrays. null = fit all.
  zoom: null,
  // Last pointer position, so the crosshair survives a re-render.
  xhair: null,
  // Pan state. Lives here, not in a listener closure: every pan step
  // re-renders the SVG, and the replacement must carry on the same drag.
  drag: null,
  geom: null,
  timer: null,
  seq: 0,
  layers: {
    smma: true, ema: true, bb2: true, bb3: true,
    ranges: true, levels: true, profile: true, signals: true, supertrend: true,
    // The lunar-solar layer (astro.js) costs a larger payload, so it is
    // fetched only while on, and starts off.
    astro: false,
  },
};

const TECH_LAYERS = [
  ["smma", "SMMA"], ["ema", "EMA 9"], ["bb2", "BB 20/2"], ["bb3", "BB 20/3"],
  ["ranges", "Ranges"], ["levels", "Levels"], ["profile", "Profile"], ["signals", "Signals"],
  ["supertrend", "Supertrend"], ["astro", "Moon/Sun"],
];

/* Colours are role tokens defined in styles.css for light and dark. The five
 * average hues were run through the palette validator against both surfaces. */
const TECH_OVERLAYS = [
  { key: "smma20", label: "SMMA 20", colour: "var(--ma-20)", layer: "smma" },
  { key: "smma50", label: "SMMA 50", colour: "var(--ma-50)", layer: "smma" },
  { key: "smma100", label: "SMMA 100", colour: "var(--ma-100)", layer: "smma" },
  { key: "smma200", label: "SMMA 200", colour: "var(--ma-200)", layer: "smma" },
  { key: "ema9", label: "EMA 9", colour: "var(--ema-9)", layer: "ema", dash: "6 4" },
  { key: "bb20_2_upper", label: "BB 20/2 up", colour: "var(--bb-up)", layer: "bb2" },
  { key: "bb20_2_lower", label: "BB 20/2 low", colour: "var(--bb-low)", layer: "bb2" },
  { key: "bb20_3_upper", label: "BB 20/3 up", colour: "var(--bb-up)", layer: "bb3", dash: "2 3" },
  { key: "bb20_3_lower", label: "BB 20/3 low", colour: "var(--bb-low)", layer: "bb3", dash: "2 3" },
];

const TECH_SIGNALS = {
  BUY: { side: "below", colour: "var(--sig-buy)", label: "BUY", block: true, rule: "buy" },
  ADD: { side: "below", colour: "var(--sig-buy)", label: "ADD", block: true, rule: "add" },
  TRIM: { side: "above", colour: "var(--sig-sell)", label: "TRIM", block: true, rule: "trim" },
  SELL: { side: "above", colour: "var(--sig-sell)", label: "SELL", block: true, rule: "sell" },
  BREAKOUT: { side: "below", colour: "var(--sig-breakout)", label: "Breakout", block: false, rule: "breakout" },
  BREAKDOWN: { side: "above", colour: "var(--sig-breakdown)", label: "Breakdown", block: false, rule: "breakout" },
};

/** Pane heights. The ribbons get a title row plus a 16px band. */
const TECH_PANES = [
  { key: "price", h: 470 },
  { key: "volume", h: 84 },
  { key: "rsi", h: 104 },
  { key: "macd", h: 112 },
  // Only while the Moon/Sun layer is on and the server could compute it.
  { key: "astro", h: 124 },
  { key: "vams", h: 30 },
  { key: "vamscheck", h: 30 },
];
const TECH_GAP = 8;
const TECH_AXIS_H = 20;
/** Room for a pane's title row above its plot. */
const TECH_TITLE_H = 14;

const TECH_DATE_FMT = new Intl.DateTimeFormat("en-US", {
  weekday: "short", day: "2-digit", month: "short", year: "numeric", timeZone: "UTC",
});
const TECH_MONTH_FMT = new Intl.DateTimeFormat("en-US", { month: "short", timeZone: "UTC" });
// Intraday times are shown in the header's chosen zone (tz.js); daily and
// weekly bars keep their session date, formatted in UTC above.
const TECH_WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

/* ------------------------------------------------------------ formatting */

/** A finite number. The payload sends null where a value does not exist yet
 *  (an indicator still warming up). */
const techOk = (v) => v !== null && v !== undefined && Number.isFinite(v);

/** A price with thousands separators and two decimals; "—" when missing. */
function techPx(v) {
  if (!techOk(v)) return "—";
  return v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Volume abbreviated to K / M / B; "—" when missing. */
function techVol(v) {
  if (!techOk(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return String(Math.round(v));
}

/** To `d` decimals with "+" or a true minus sign (U+2212), for MACD values
 *  and changes; zero is unsigned, "—" when missing. */
const techSigned = (v, d = 2) => (techOk(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}` : "—");

/** Bar timestamps arrive as "YYYY-MM-DDTHH:MM:SS" (UTC). Daily bars are dated
 *  by their session, so only the date part is meaningful. */
const techDate = (stamp) => new Date(`${String(stamp).slice(0, 10)}T00:00:00Z`);
/** The instant itself, for intraday bars. */
const techInstant = tzInstant;
/** Intraday interval keys end in m or h ("5m", "1h"); range keys like "1M" never reach here. */
const techIsIntraday = (interval) => /^\d+[mh]$/.test(String(interval || ""));
/** A bar's session date for daily/weekly bars; for intraday ones, its date
 *  and time in the chosen zone, with the zone's tag. */
function techStamp(stamp, interval = tech.data?.interval) {
  if (!techIsIntraday(interval)) return TECH_DATE_FMT.format(techDate(stamp));
  const t = techInstant(stamp);
  return `${tzStamp(t)} ${tzTag(t)}`;
}

/** A round tick step giving roughly `target` intervals across `span`. */
function techStep(span, target) {
  const raw = span / Math.max(target, 1);
  const mag = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * mag >= raw) return m * mag;
  return 10 * mag;
}

/** Polylines for a series, broken wherever a value is missing: an indicator
 *  that does not exist yet must not be drawn as a line along the axis. */
function techLines(values, lo, hi, x, y, attrs) {
  const out = [];
  let run = [];
  const flush = () => {
    if (run.length > 1) out.push(`<polyline ${attrs} points="${run.join(" ")}"/>`);
    run = [];
  };
  for (let i = lo; i < hi; i++) {
    const v = values?.[i];
    if (techOk(v)) run.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    else flush();
  }
  flush();
  return out.join("");
}

/* ------------------------------------------------------------- controls */

/** Called once by app.js at startup, before the first setView(). Restores
 *  the chart's choices -- URL (?chart=, chart_range, chart_interval,
 *  chart_session, chart_adjust) first, then localStorage, then defaults --
 *  builds the symbol and layer buttons and binds every control. Fetches
 *  nothing: setView() does that when the Chart view is mounted. */
function initTechnicals() {
  const q = new URLSearchParams(location.search);
  // Interval keys are case-sensitive: "1m" is a minute, "1M" a month range.
  // Range and interval are checked by the server, which answers with what it
  // actually served; only their shape is checked here.
  const read = (key, fallback, ok) => {
    let v = q.get(key);
    if (!v) { try { v = localStorage.getItem(`gexdash-tech-${key}`); } catch { v = null; } }
    return v && ok(v) ? v : fallback;
  };
  tech.symbol = read("chart", "QQQ", (v) => TECH_SYMBOL_RE.test(v.trim().toUpperCase())).trim().toUpperCase();
  tech.range = read("chart_range", "1Y", (v) => /^(\d+[DMY]|YTD)$/i.test(v)).toUpperCase();
  tech.interval = read("chart_interval", "1D", (v) => /^(\d+[mh]|1D|1W)$/.test(v));
  tech.session = read("chart_session", "rth", (v) => v === "rth" || v === "eth");
  tech.adjust = read("chart_adjust", "on", (v) => v === "on" || v === "off") === "on";
  tech.layers.astro = read("chart_astro", "off", (v) => v === "on" || v === "off") === "on";

  $("tech-symbols").innerHTML = TECH_SYMBOLS
    .map((s) => `<button role="tab" data-sym="${s}">${s}</button>`).join("");
  $("tech-layers").innerHTML = TECH_LAYERS
    .map(([k, label]) => `<button data-layer="${k}">${label}</button>`).join("");

  $("tech-symbols").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      if (tech.symbol === b.dataset.sym) return;
      tech.symbol = b.dataset.sym;
      techRemember("chart", tech.symbol);
      tech.data = null;
      tech.zoom = null;
      renderTechnicals();
      loadTechnicals();
    }));
  // Any symbol: Enter (or leaving the box) loads it; Escape restores the one on screen.
  $("tech-symbol-input").addEventListener("change", (e) => {
    const sym = e.target.value.trim().toUpperCase();
    if (!sym) return techSyncControls();
    if (!TECH_SYMBOL_RE.test(sym)) {
      e.target.value = TECH_SYMBOLS.includes(tech.symbol) ? "" : tech.symbol;
      $("tech-note").textContent = `${sym} is not a symbol gexdash can look up: letters first, then letters, ` +
        "digits or dots, with $ for a cash index or / for a future (AAPL, BRK.B, $RUT, /CL).";
      return;
    }
    if (sym === tech.symbol) return;
    tech.symbol = sym;
    techRemember("chart", tech.symbol);
    tech.data = null;
    tech.zoom = null;
    renderTechnicals();
    loadTechnicals();
  });
  $("tech-symbol-input").addEventListener("keydown", (e) => {
    if (e.key === "Escape") { techSyncControls(); e.target.blur(); }
  });
  $("tech-range").addEventListener("change", (e) => {
    tech.range = e.target.value;
    techRemember("chart_range", tech.range);
    loadTechnicals();
  });
  $("tech-interval").addEventListener("change", (e) => {
    tech.interval = e.target.value;
    // Each bar size offers its own ranges; keep the current one if it is
    // among them, otherwise take the bar size's default.
    const iv = techIntervalCaps(tech.interval);
    if (iv && !iv.ranges.includes(tech.range)) tech.range = iv.default_range;
    techRemember("chart_interval", tech.interval);
    techRemember("chart_range", tech.range);
    tech.zoom = null;
    loadTechnicals();
  });
  $("tech-adjust").addEventListener("click", () => {
    tech.adjust = !tech.adjust;
    techRemember("chart_adjust", tech.adjust ? "on" : "off");
    loadTechnicals();
  });
  $("tech-session").addEventListener("click", () => {
    tech.session = tech.session === "eth" ? "rth" : "eth";
    techRemember("chart_session", tech.session);
    tech.zoom = null;
    loadTechnicals();
  });
  $("tech-layers").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      const key = b.dataset.layer;
      tech.layers[key] = !tech.layers[key];
      if (key === "astro") {
        techRemember("chart_astro", tech.layers.astro ? "on" : "off");
        // The block comes only with astro=true; fetch it the first time.
        if (tech.layers.astro && tech.data?.available && !tech.data.astro) return loadTechnicals();
      }
      renderTechnicals();
    }));
  $("tech-zoom-in").addEventListener("click", () => techZoomBy(1 / 1.5));
  $("tech-zoom-out").addEventListener("click", () => techZoomBy(1.5));
  $("tech-zoom-reset").addEventListener("click", () => { tech.zoom = null; renderTechnicals(); });
  techSyncControls();
  // Drawing tools: listeners on the stable host, so they outlive each render's SVG.
  drawInit("tech", $("tech-chart"));
  drawToolbar($("draw-bar-tech"), $("tech-chart"), $("draw-frame-tech"));
}

/** Persist one chart choice as gexdash-tech-<key>; skipped where storage is
 *  blocked. */
function techRemember(key, value) {
  try { localStorage.setItem(`gexdash-tech-${key}`, value); } catch { /* private mode */ }
}

/** Bring the buttons and selects in line with `tech` and the server's caps.
 *  Called on every render, because a payload can bring new caps or a range
 *  the server substituted. The session toggle shows only on intraday bars
 *  and is locked to "Full session" for futures; the adjust toggle shows only
 *  for futures. */
function techSyncControls() {
  $("tech-symbols").querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-selected", String(b.dataset.sym === tech.symbol)));
  $("tech-layers").querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-pressed", String(!!tech.layers[b.dataset.layer])));
  const caps = techCaps();
  const iv = techIntervalCaps(tech.interval);
  // Until the server has answered once, show just the current choices.
  const intervals = caps ? caps.intervals : [{ key: tech.interval, label: tech.interval }];
  const ranges = iv ? iv.ranges : [tech.range];
  const opt = (value, label, on) => `<option value="${value}" ${on ? "selected" : ""}>${esc(label)}</option>`;
  // Intraday sizes first, as a platform lists them, in one group each.
  const groups = [["Intraday", intervals.filter((i) => i.intraday)], ["", intervals.filter((i) => !i.intraday)]];
  $("tech-interval").innerHTML = groups.map(([name, list]) => {
    const body = list.map((i) => opt(i.key, i.label, i.key === tech.interval)).join("");
    return name && list.length ? `<optgroup label="${name}">${body}</optgroup>` : body;
  }).join("");
  $("tech-range").innerHTML = ranges.map((r) => opt(r, r, r === tech.range)).join("");

  // A typed symbol shows in the box; a tab's symbol leaves it empty.
  const box = $("tech-symbol-input");
  if (document.activeElement !== box) box.value = TECH_SYMBOLS.includes(tech.symbol) ? "" : tech.symbol;
  box.classList.toggle("active", !TECH_SYMBOLS.includes(tech.symbol));

  const intraday = techIsIntraday(tech.interval);
  const full = techIsFuture(tech.symbol);
  const btn = $("tech-session");
  btn.hidden = !intraday;
  btn.disabled = full;
  btn.setAttribute("aria-pressed", String(full || tech.session === "eth"));
  btn.textContent = full ? "Full session" : "Ext hours";
  const adj = $("tech-adjust");
  adj.hidden = !techRollsAdjustable(tech.symbol);
  adj.setAttribute("aria-pressed", String(tech.adjust));
}

/** What the server offers (bar sizes, their ranges, sessions): from the
 *  latest /api/technicals payload, else /api/meta's `technicals`, else null
 *  before either has answered. */
function techCaps() {
  return tech.caps || state.meta?.technicals || null;
}

/** The caps entry for one bar size ({key, label, intraday, ranges,
 *  default_range}), or null. */
function techIntervalCaps(key) {
  return techCaps()?.intervals.find((i) => i.key === key) || null;
}

/* ----------------------------------------------------------------- fetch */

/**
 * Fetch /api/technicals for the current symbol, range and bar size. Each call
 * takes a sequence number, and a response that is no longer the latest is
 * dropped. A failure of any kind is stored as {available: false, reason}.
 * The server's answer wins: on intraday or weekly bars, an interval or range
 * it does not know falls back to 1D / 1Y, and a range it substituted replaces
 * ours. A refresh of the same series keeps the zoom. Draws only if the Chart
 * view is mounted.
 */
async function loadTechnicals() {
  const seq = ++tech.seq;
  const q = new URLSearchParams({ symbol: tech.symbol, range: tech.range, interval: tech.interval });
  // Futures have no session choice; sending the ETF preference would only
  // earn a note saying it was not applied.
  if (techIsIntraday(tech.interval) && !techIsFuture(tech.symbol)) q.set("session", tech.session);
  if (techRollsAdjustable(tech.symbol) && !tech.adjust) q.set("adjust", "false");
  if (tech.layers.astro) q.set("astro", "true");
  $("tech-chart").classList.add("stale");
  let body;
  try {
    const res = await fetch(`/api/technicals?${q}`);
    body = await res.json();
    if (!res.ok) body = { available: false, reason: body.detail || `HTTP ${res.status}` };
  } catch (err) {
    body = { available: false, reason: err.message };
  }
  // A slower, older request must not overwrite the symbol just clicked.
  if (seq !== tech.seq) return;
  $("tech-chart").classList.remove("stale");
  tech.loadedAt = Date.now();

  // A remembered interval the server no longer knows: fall back once.
  if (!body.available && /unknown (interval|range)/.test(body.reason || "") && tech.interval !== "1D") {
    tech.interval = "1D"; tech.range = "1Y";
    techRemember("chart_interval", "1D"); techRemember("chart_range", "1Y");
    return loadTechnicals();
  }
  if (body.caps) tech.caps = body.caps;
  // The server may have replaced a range the bar size does not offer; show
  // what was actually served.
  if (body.available) {
    if (body.range) tech.range = body.range;
    if (body.interval) tech.interval = body.interval;
    // An alias resolves to its registered key ("/ES" is ES); show that one.
    if (body.symbol && body.symbol !== tech.symbol) {
      tech.symbol = body.symbol;
      techRemember("chart", tech.symbol);
    }
  }

  // A background refresh of the same series keeps the reader's zoom; a new
  // series starts fitted.
  const prev = tech.data;
  const same = prev && prev.available && body.available && prev.symbol === body.symbol &&
    prev.range === body.range && prev.interval === body.interval && prev.session === body.session &&
    prev.adjusted === body.adjusted;
  tech.data = { ...body, symbol: body.symbol || tech.symbol };
  if (!same) tech.zoom = null;
  if (state.view === "chart") renderTechnicals();
}

/** The Chart view's refresh timer, started by setView() while it is mounted.
 *  Ticks every 30s and skips while the tab is hidden; daily and weekly
 *  charts reload only once 55s have passed since the last load. */
function startTechTimer() {
  stopTechTimer();
  tech.timer = setInterval(() => {
    if (state.view !== "chart" || document.hidden) return;
    if (!techIsIntraday(tech.interval) && Date.now() - tech.loadedAt < TECH_DAILY_RELOAD_MS) return;
    loadTechnicals();
  }, TECH_TICK_MS);
}

/** Stop the refresh timer; setView() calls it when another view is mounted. */
function stopTechTimer() {
  clearInterval(tech.timer);
  tech.timer = null;
}

/* ---------------------------------------------------------------- render */

/** The visible [lo, hi) slot indices from tech.zoom, over `n` slots of which
 *  the first `bars` are bars (the rest are the lunar-solar layer's future
 *  dates): at least 10 slots, at least 10 of them bars where the series has
 *  them, and every slot when zoom is null. */
function techWindow(n, bars = n) {
  if (!tech.zoom) return [0, n];
  const span = Math.max(tech.zoom[1] - tech.zoom[0], 10);
  let lo = Math.max(0, Math.round(tech.zoom[0]));
  lo = Math.min(lo, Math.max(0, bars - 10));
  const hi = Math.min(n, Math.round(lo + span));
  if (hi - lo < 10) lo = Math.max(0, hi - 10);
  return [lo, hi];
}

/** The stamp of slot i: a bar's time, or past the last bar the lunar-solar
 *  layer's future date. */
function techSlotTime(d, i) {
  return i < d.time.length ? d.time[i] : d.astro?.future?.time?.[i - d.time.length];
}

/** The lunar-solar block when its layer is on and it was computed, else null. */
const techAstro = (d) => (tech.layers.astro && astroOk(d?.astro) ? d.astro : null);

/** The payload's signal rules (RULES in technicals.py) in the list under
 *  the chart, worded as the server words them. */
function techRules(d) {
  const rules = d?.rules || [];
  $("tech-rules").innerHTML = rules
    .map((r) => `<dt>${esc(r.name)}</dt><dd>${esc(r.text)}</dd>`).join("");
}

/**
 * Draw the whole Chart view from tech.data: the legend, then one SVG holding
 * the price, volume, RSI, MACD and two VAMS panes, the date axis and the
 * crosshair. Shows a loading or unavailable message instead when there is
 * no usable payload. Rebuilt from scratch on every change -- data, zoom,
 * each pan step, a layer toggle, theme, zone or resize -- and the geometry
 * the crosshair and zoom need is kept on tech.geom. Width comes from
 * #tech-chart, so the Chart view must be mounted: hidden, it falls back to
 * 900px.
 */
function renderTechnicals() {
  const host = $("tech-chart");
  if (!host) return;
  techSyncControls();
  const d = tech.data;

  if (!d) {
    host.innerHTML = `<div class="nodata">Loading ${esc(tech.symbol)}…</div>`;
    $("tech-legend").innerHTML = "";
    $("tech-note").textContent = "";
    tech.geom = null;
    return;
  }
  if (!d.available) {
    const login = /token|consent|login|authenticat/i.test(d.reason || "");
    // One inner block: .nodata is a flex row, which would set each line side by side.
    host.innerHTML = `<div class="nodata"><div>${esc(tech.symbol)} price history unavailable<br>
      <span class="small">${esc(d.reason || "no reason given")}</span>
      ${login ? `<br><span class="small"><a href="/connect">Reconnect Schwab →</a></span>` : ""}</div></div>`;
    $("tech-legend").innerHTML = "";
    $("tech-note").textContent = "";
    techRules(d);
    tech.geom = null;
    return;
  }

  const total = d.close.length;
  // With the Moon/Sun layer on, future dates follow the bars as extra slots.
  // Bars stop at hb; slots from hb to hi are dates, not candles.
  const astro = techAstro(d);
  const slots = total + (astro ? astroFutureCount(astro, total) : 0);
  const [lo, hi] = techWindow(slots, total);
  const n = hi - lo;
  const hb = Math.min(hi, total);

  const W = Math.max(host.clientWidth || 900, 420);
  const padL = 8, padR = 78;
  const innerW = W - padL - padR;
  const right = padL + innerW;

  let top = 6;
  // The legend sits over the top of the price pane. Measure it with the latest
  // bar's values and add that height to the pane as a strip the plot never
  // enters, so no candle or marker is drawn under it.
  const legendEl = $("tech-legend");
  legendEl.innerHTML = techLegendRows(d, hb - 1);
  const legendBottom = legendEl.offsetHeight ? legendEl.offsetTop + legendEl.offsetHeight : 0;
  const inset = Math.max(0, Math.ceil(legendBottom + 6 - top));
  const panes = {};
  TECH_PANES.filter((p) => p.key !== "astro" || astro).forEach((p) => {
    const h = p.key === "price" ? p.h + inset : p.h;
    panes[p.key] = { ...p, h, top };
    top += h + TECH_GAP;
  });
  const axisTop = top - TECH_GAP + 2;
  const H = axisTop + TECH_AXIS_H;

  const bw = (innerW - Math.min(TECH_RIGHT_PAD_PX, innerW * 0.25)) / n;
  // Full-series index -> x. Everything (signals, boxes) is indexed this way.
  const x = (i) => padL + (i - lo + 0.5) * bw;
  const body = Math.max(1, Math.min(bw * 0.64, 11));

  const parts = [];
  const tagsOut = [];

  /* ---------------- price pane ---------------- */
  const pp = panes.price;
  const pTop = pp.top + inset, pBot = pp.top + pp.h;   // plot area, below the legend
  const overlays = TECH_OVERLAYS.filter((o) => tech.layers[o.layer]);

  let yLo = Infinity, yHi = -Infinity;
  for (let i = lo; i < hb; i++) { yLo = Math.min(yLo, d.low[i]); yHi = Math.max(yHi, d.high[i]); }
  // The averages and bands share the price axis legitimately -- they are
  // prices -- so the scale includes them, as any chart platform's does.
  overlays.forEach((o) => {
    const s = d.series[o.key] || [];
    for (let i = lo; i < hi; i++) if (techOk(s[i])) { yLo = Math.min(yLo, s[i]); yHi = Math.max(yHi, s[i]); }
  });
  // The Supertrend band sits up to 3 ATR beyond the bars; without this the
  // upper band of a downtrend runs off the top of the pane.
  const stOn = tech.layers.supertrend && d.series.st_up;
  if (stOn) ["st_up", "st_dn"].forEach((k) => {
    const s = d.series[k];
    for (let i = lo; i < hi; i++) if (techOk(s[i])) { yLo = Math.min(yLo, s[i]); yHi = Math.max(yHi, s[i]); }
  });
  // The projected path is a price, so it shares the axis like the averages.
  const pathExt = astro ? astroPathExtent(astro, lo, hi, total) : null;
  if (pathExt) { yLo = Math.min(yLo, pathExt[0]); yHi = Math.max(yHi, pathExt[1]); }

  const levels = [];
  if (tech.layers.levels) {
    d.levels.filter((l) => l.key !== "poc").forEach((l) =>
      levels.push({ label: l.label, price: l.price, colour: "var(--text-muted)", style: "dotted", named: true }));
    const vc = d.vams.current;
    if (techOk(vc.to_bullish)) levels.push({ label: "VAMS→Bullish", price: vc.to_bullish, colour: "var(--bb-up)", style: "dashed", named: true });
    if (techOk(vc.to_bearish)) levels.push({ label: "VAMS→Bearish", price: vc.to_bearish, colour: "var(--bb-low)", style: "dashed", named: true });
  }
  if (tech.layers.profile && d.profile) {
    levels.push({ label: "PoC", price: d.profile.poc, colour: "var(--text-secondary)", style: "solid", named: true });
  }

  // Absorb nearby levels into the scale; pin distant ones to the edge with an
  // arrow rather than dropping them, as the terminal chart does.
  // Intraday, the 3M/52W levels are daily-bar extremes, usually far away: a
  // looser rule would squash a session's candles into a sliver to reach them.
  const span0 = Math.max(yHi - yLo, 1e-9);
  const reach = techIsIntraday(d.interval) ? 0.15 : 0.5;
  const capLo = yLo - span0 * reach, capHi = yHi + span0 * reach;
  levels.forEach((l) => {
    if (l.price >= capLo && l.price <= capHi) { yLo = Math.min(yLo, l.price); yHi = Math.max(yHi, l.price); }
    else l.off = l.price > capHi ? "up" : "down";
  });
  // Headroom for the signal markers above highs and below lows.
  const headroom = (yHi - yLo) * 0.09 || 1;
  yLo -= headroom; yHi += headroom;
  const py = (v) => pBot - ((v - yLo) / (yHi - yLo)) * (pBot - pTop);

  const step = techStep(yHi - yLo, 8);
  const priceTicks = [];
  // Ticks by integer multiple, not by accumulating `step`: repeated addition
  // drifts, and a tick that should be 0 comes out as -1e-15 and prints "-0.00".
  for (let k = Math.ceil(yLo / step); k * step <= yHi; k++) priceTicks.push(Math.abs(k) < 1e-9 ? 0 : k * step);
  priceTicks.forEach((v) =>
    parts.push(`<line class="grid-line" x1="${padL}" x2="${right}" y1="${py(v).toFixed(1)}" y2="${py(v).toFixed(1)}"/>`));

  // Volume profile, behind everything else on the pane.
  if (tech.layers.profile && d.profile) {
    const pr = d.profile;
    const tot = pr.up.map((u, k) => u + pr.down[k]);
    const mx = Math.max(...tot) || 1;
    const maxW = innerW * 0.2;
    // Anchored at the last bar, not the axis, when future dates fill the right.
    const edge = hi > total ? x(total - 1) + bw / 2 : right;
    for (let k = 0; k < tot.length; k++) {
      const yTop = py(pr.edges[k + 1]), yBot = py(pr.edges[k]);
      const h = Math.max(yBot - yTop - 1, 0.6);
      const wu = (pr.up[k] / mx) * maxW, wd = (pr.down[k] / mx) * maxW;
      if (wu > 0.2) parts.push(`<rect class="vp-up" x="${(edge - wu).toFixed(1)}" y="${(yTop + 0.5).toFixed(1)}" width="${wu.toFixed(1)}" height="${h.toFixed(1)}"/>`);
      if (wd > 0.2) parts.push(`<rect class="vp-dn" x="${(edge - wu - wd).toFixed(1)}" y="${(yTop + 0.5).toFixed(1)}" width="${wd.toFixed(1)}" height="${h.toFixed(1)}"/>`);
    }
  }

  // Range boxes: each base that resolved into a breakout or breakdown, and
  // the current 20-bar range.
  if (tech.layers.ranges) {
    const boxes = [...(d.ranges || [])];
    if (d.current_range) boxes.push({ ...d.current_range, outcome: "current" });
    boxes.forEach((b) => {
      if (b.end < lo || b.start >= hi) return;
      const x0 = Math.max(x(Math.max(b.start, lo)) - bw / 2, padL);
      const x1 = Math.min(x(Math.min(b.end, hi - 1)) + bw / 2, right);
      const y0 = py(b.high), y1 = py(b.low);
      parts.push(`<rect class="range-box ${b.outcome}" x="${x0.toFixed(1)}" y="${y0.toFixed(1)}" width="${Math.max(x1 - x0, 1).toFixed(1)}" height="${Math.max(y1 - y0, 1).toFixed(1)}"><title>${
        b.outcome === "current" ? "Current" : b.outcome === "breakout" ? "Base → breakout" : "Base → breakdown"
      } range ${techPx(b.low)} – ${techPx(b.high)}</title></rect>`);
    });
  }

  // Level lines across the plot.
  levels.filter((l) => !l.off).forEach((l) => {
    const ly = py(l.price).toFixed(1);
    parts.push(`<line class="lvl-line ${l.style}" stroke="${l.colour}" x1="${padL}" x2="${right}" y1="${ly}" y2="${ly}"/>`);
  });

  // Averages and bands.
  overlays.forEach((o) => {
    parts.push(techLines(d.series[o.key], lo, hi, x, py,
      `class="ma-line" stroke="${o.colour}"${o.dash ? ` stroke-dasharray="${o.dash}"` : ""}`));
  });

  // Supertrend, as the script draws it: the active band (2px), a light fill
  // between it and ohlc4, both broken where the trend flips.
  if (stOn) {
    [["st_up", "var(--st-up)"], ["st_dn", "var(--st-dn)"]].forEach(([k, colour]) => {
      const band = d.series[k];
      let run = [];
      const flush = () => {
        if (run.length > 1) {
          const mid = run.map((i) => `${x(i).toFixed(1)},${py((d.open[i] + d.high[i] + d.low[i] + d.close[i]) / 4).toFixed(1)}`);
          const edge = run.slice().reverse().map((i) => `${x(i).toFixed(1)},${py(band[i]).toFixed(1)}`);
          parts.push(`<polygon class="st-fill" fill="${colour}" points="${mid.concat(edge).join(" ")}"/>`);
        }
        run = [];
      };
      for (let i = lo; i < hi; i++) { if (techOk(band[i])) run.push(i); else flush(); }
      flush();
      parts.push(techLines(band, lo, hi, x, py, `class="st-line" stroke="${colour}"`));
    });
  }

  // Candles.
  for (let i = lo; i < hb; i++) {
    const cls = d.close[i] >= d.open[i] ? "candle-up" : "candle-dn";
    const cx = x(i).toFixed(1);
    parts.push(`<line class="wick ${cls}" x1="${cx}" x2="${cx}" y1="${py(d.high[i]).toFixed(1)}" y2="${py(d.low[i]).toFixed(1)}"/>`);
    const yo = py(d.open[i]), yc = py(d.close[i]);
    parts.push(`<rect class="${cls}" x="${(x(i) - body / 2).toFixed(1)}" y="${Math.min(yo, yc).toFixed(1)}" width="${body.toFixed(1)}" height="${Math.max(Math.abs(yc - yo), 0.8).toFixed(1)}"/>`);
  }

  // Lunar-solar projection: dotted from the last close, highs and lows labelled.
  if (astro) {
    parts.push(astroNowMarkup(total, lo, hi, x, bw, pTop, pBot));
    parts.push(...astroProjectionMarkup(astro, {
      lo, hi, total, x, y: py, lastClose: d.close[total - 1], top: pTop, bottom: pBot,
      // Clear of the named level boxes that sit just left of the axis.
      left: padL, right: right - 96,
    }));
  }

  // Signal markers: an arrow plus a label. Laid out in bar order; one that
  // would overlap a marker already placed -- on the same bar or a neighbour,
  // either side -- moves up to two slots further out from its bar. Where even
  // that is crowded (long ranges, a bar or two per pixel) it drops the label
  // and tries again as a bare arrow; the label stays in the hover tooltip.
  const ruleText = Object.fromEntries((d.rules || []).map((r) => [r.key, r.text]));
  const placed = [];
  const hits = (b) => placed.some((p) => p.x0 < b.x1 && b.x0 < p.x1 && p.y0 < b.y1 && b.y0 < p.y1);

  // Supertrend Buy / Sell: fixed at the band price, as the script's
  // location.absolute labels are, so they are placed first and every other
  // marker steps around them. The dot on the band is always drawn; the label
  // box only where it does not cover another one (a 1-minute week flips
  // hundreds of times), and the tooltip carries it either way.
  if (stOn) {
    (d.supertrend?.signals || []).filter((s) => s.i >= lo && s.i < hi).forEach((s) => {
      const buy = s.type === "ST_BUY";
      const cx = x(s.i), cy = py(s.price);
      const dir = buy ? 1 : -1;
      const label = buy ? "Buy" : "Sell";
      const w = buy ? 26 : 28, boxTop = buy ? cy + 7 : cy - 21;
      const box = { x0: cx - w / 2, x1: cx + w / 2, y0: Math.min(cy, boxTop), y1: Math.max(cy, boxTop + 14) };
      const room = !hits(box) && box.y0 >= pTop && box.y1 <= pBot;
      placed.push(room ? box : { x0: cx - 3, x1: cx + 3, y0: cy - 3, y1: cy + 3 });
      const fill = buy ? "var(--st-up-box)" : "var(--st-dn-box)";
      const date = techStamp(d.time[s.i]);
      parts.push(`<g class="st-sig"><title>${esc(`Supertrend ${label} · ${date} · band ${techPx(s.price)}\n${ruleText.supertrend || ""}`)}</title>` +
        `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="2.6" fill="${buy ? "var(--st-up)" : "var(--st-dn)"}"/>` +
        (room
          ? `<path d="M${cx.toFixed(1)},${(cy + 3 * dir).toFixed(1)} l4,${4 * dir} h-8 z" fill="${fill}"/>` +
            `<rect x="${(cx - w / 2).toFixed(1)}" y="${boxTop.toFixed(1)}" width="${w}" height="14" rx="2.5" fill="${fill}"/>` +
            `<text x="${cx.toFixed(1)}" y="${(boxTop + 10.2).toFixed(1)}" text-anchor="middle">${label}</text>`
          : "") + `</g>`);
    });
  }

  if (tech.layers.signals) {
    const SLOTS = 3;
    (d.signals || []).filter((s) => s.i >= lo && s.i < hi).sort((a, b) => a.i - b.i).forEach((s) => {
      const spec = TECH_SIGNALS[s.type];
      if (!spec) return;
      const below = spec.side === "below";
      const cx = x(s.i);
      const base = below ? py(d.low[s.i]) + 4 : py(d.high[s.i]) - 4;
      const boxAt = (tip, halfW, h) =>
        ({ x0: cx - halfW, x1: cx + halfW, y0: below ? tip : tip - h, y1: below ? tip + h : tip });
      // halfW/h: the footprint; arrow alone is 12 x 13px, a 10px bold label adds 12px.
      // A slot past the plot's edge counts as taken: above, it would climb
      // into the legend strip; below, into the volume pane.
      const fit = (halfW, h) => {
        for (let k = 0; k < SLOTS; k++) {
          const tip = below ? base + k * (h + 1) : base - k * (h + 1);
          const box = boxAt(tip, halfW, h);
          if (box.y0 >= pTop && box.y1 <= pBot && !hits(box)) return { tip, box };
        }
        return null;
      };
      const labelled = fit(Math.max(6, spec.label.length * 3.2 + 3), 25);
      const spot = labelled || fit(6, 14) || { tip: base, box: boxAt(base, 6, 14) };
      const tip = spot.tip;
      placed.push(spot.box);
      const dir = below ? 1 : -1;
      const shape = spec.block
        // Block arrow: 12px head, 5px shaft.
        ? `M${cx},${tip} l6,${7 * dir} h-3.5 v${6 * dir} h-5 v${-6 * dir} h-3.5 z`
        // Thin arrow for breakouts/breakdowns: they mark an event, not an action.
        : `M${cx},${tip} l4,${6 * dir} h-2.5 v${6 * dir} h-3 v${-6 * dir} h-2.5 z`;
      const ty = below ? tip + 22 : tip - 15;
      const date = techStamp(d.time[s.i]);
      // A marker moved out to clear a neighbour keeps a leader back to its bar.
      const leader = tip === base ? ""
        : `<line class="sig-leader" stroke="${spec.colour}" x1="${cx}" x2="${cx}" y1="${base.toFixed(1)}" y2="${tip.toFixed(1)}"/>`;
      parts.push(`<g class="sig"><title>${esc(`${spec.label} · ${date} · close ${techPx(s.price)}\n${ruleText[spec.rule] || ""}`)}</title>` +
        leader + `<path d="${shape}" fill="${spec.colour}"/>` +
        (labelled ? `<text x="${cx}" y="${ty}" text-anchor="middle">${spec.label}</text>` : "") + `</g>`);
    });
  }

  /* ---- right-axis tags: laid out first, so axis labels can avoid them ---- */
  const last = hb - 1;
  const prevClose = last > 0 ? d.close[last - 1] : d.prev_close;
  const tags = [{
    price: d.close[last], solid: true,
    colour: techOk(prevClose) && d.close[last] < prevClose ? "var(--neg)" : "var(--pos)",
  }];
  overlays.forEach((o) => {
    const v = d.series[o.key]?.[last];
    if (techOk(v)) tags.push({ price: v, colour: o.colour });
  });
  levels.filter((l) => !l.off).forEach((l) => tags.push({ price: l.price, colour: l.colour, name: l.label }));
  if (stOn) {
    const up = d.series.st_up[last], dn = d.series.st_dn[last];
    if (techOk(up)) tags.push({ price: up, colour: "var(--st-up)", name: "ST▲" });
    else if (techOk(dn)) tags.push({ price: dn, colour: "var(--st-dn)", name: "ST▼" });
  }
  if (tech.layers.ranges && d.current_range) {
    // The current range often shares an extreme with the 3M/52W levels;
    // one price gets one tag, and the named one says more.
    const taken = new Set(tags.filter((t) => t.name).map((t) => techPx(t.price)));
    [d.current_range.high, d.current_range.low].forEach((p) => {
      if (!taken.has(techPx(p))) tags.push({ price: p, colour: "var(--text-muted)" });
    });
  }
  techPlaceTags(tags, pTop, pBot, py);

  const occupied = tags.map((t) => t.ty);
  priceTicks.forEach((v) => {
    const gy = py(v);
    if (!occupied.some((o) => Math.abs(o - gy) < 12)) {
      parts.push(`<text class="axis-label" x="${right + 6}" y="${(gy + 3).toFixed(1)}">${techPx(v)}</text>`);
    }
  });
  tagsOut.push(...techTagMarkup(tags, right, padR));

  // Off-scale levels pinned to the edge, never silently dropped.
  let upSlot = 0, downSlot = 0;
  levels.filter((l) => l.off).forEach((l) => {
    const up = l.off === "up";
    const ty = up ? pTop + 10 + upSlot++ * 11 : pBot - 4 - downSlot++ * 11;
    parts.push(`<text class="level-edge" fill="${l.colour}" x="${right - 4}" y="${ty}" text-anchor="end">${up ? "▲" : "▼"} ${esc(l.label)} ${techPx(l.price)}</text>`);
  });

  /* ---------------- volume pane ---------------- */
  const vp = panes.volume;
  let vmax = 0;
  for (let i = lo; i < hi; i++) vmax = Math.max(vmax, d.volume[i] || 0);
  const vTop = vp.top + TECH_TITLE_H, vH = vp.h - TECH_TITLE_H;
  const vy = (v) => vTop + vH - (v / (vmax || 1)) * vH;
  parts.push(`<line class="zero-line" x1="${padL}" x2="${right}" y1="${vTop + vH}" y2="${vTop + vH}"/>`);
  const vbody = Math.max(1, Math.min(bw * 0.72, 12));
  for (let i = lo; i < hi; i++) {
    const v = d.volume[i] || 0;
    if (v <= 0) continue;
    parts.push(`<rect class="${d.close[i] >= d.open[i] ? "vol-up" : "vol-dn"}" x="${(x(i) - vbody / 2).toFixed(1)}" y="${vy(v).toFixed(1)}" width="${vbody.toFixed(1)}" height="${(vTop + vH - vy(v)).toFixed(1)}"/>`);
  }
  if (vmax > 0) {
    parts.push(`<text class="axis-label" x="${right + 6}" y="${vTop + 7}">${techVol(vmax)}</text>`);
    parts.push(`<text class="axis-label" x="${right + 6}" y="${vTop + vH}">0</text>`);
  }

  /* ---------------- RSI pane ---------------- */
  const rp = panes.rsi;
  const rTop = rp.top + TECH_TITLE_H, rH = rp.h - TECH_TITLE_H;
  const ry = (v) => rTop + rH - (v / 100) * rH;
  parts.push(`<rect class="ind-band" x="${padL}" y="${ry(70)}" width="${innerW}" height="${ry(30) - ry(70)}"/>`);
  [[70, "dotted"], [50, "dashed"], [30, "dotted"]].forEach(([g, style]) => {
    parts.push(`<line class="lvl-line ${style}" stroke="var(--text-muted)" x1="${padL}" x2="${right}" y1="${ry(g)}" y2="${ry(g)}"/>`);
  });
  parts.push(techLines(d.series.rsi, lo, hi, x, ry, `class="ind-line" stroke="var(--series-violet)"`));
  const rsiTags = [];
  if (techOk(d.series.rsi?.[last])) rsiTags.push({ price: d.series.rsi[last], colour: "var(--series-violet)", fmt: (v) => v.toFixed(2) });
  [70, 30].forEach((g) => {
    if (!rsiTags.some((t) => Math.abs(ry(t.price) - ry(g)) < 12)) {
      parts.push(`<text class="axis-label" x="${right + 6}" y="${ry(g) + 3}">${g}</text>`);
    }
  });
  techPlaceTags(rsiTags, rTop, rTop + rH, ry);
  tagsOut.push(...techTagMarkup(rsiTags, right, padR));

  /* ---------------- MACD pane ---------------- */
  const mp = panes.macd;
  const mTop = mp.top + TECH_TITLE_H, mH = mp.h - TECH_TITLE_H;
  let mAbs = 0;
  ["macd", "macd_signal", "macd_histogram"].forEach((k) => {
    const s = d.series[k] || [];
    for (let i = lo; i < hi; i++) if (techOk(s[i])) mAbs = Math.max(mAbs, Math.abs(s[i]));
  });
  mAbs = (mAbs || 1) * 1.08;
  const my = (v) => mTop + mH / 2 - (v / mAbs) * (mH / 2);
  parts.push(`<line class="zero-line" x1="${padL}" x2="${right}" y1="${my(0)}" y2="${my(0)}"/>`);
  const hist = d.series.macd_histogram || [];
  const hbody = Math.max(1, Math.min(bw * 0.72, 12));
  for (let i = lo; i < hi; i++) {
    const v = hist[i];
    if (!techOk(v)) continue;
    const p = hist[i - 1];
    // Four tones: a histogram bar is strong while it is still extending away
    // from zero and weak once it has started to shrink back.
    const growing = techOk(p) ? Math.abs(v) >= Math.abs(p) : true;
    const cls = `${v >= 0 ? "h-pos" : "h-neg"}${growing ? "" : " weak"}`;
    const y0 = my(0), yv = my(v);
    parts.push(`<rect class="${cls}" x="${(x(i) - hbody / 2).toFixed(1)}" y="${Math.min(y0, yv).toFixed(1)}" width="${hbody.toFixed(1)}" height="${Math.max(Math.abs(yv - y0), 0.6).toFixed(1)}"/>`);
  }
  parts.push(techLines(d.series.macd, lo, hi, x, my, `class="ind-line" stroke="var(--series-call)"`));
  parts.push(techLines(d.series.macd_signal, lo, hi, x, my, `class="ind-line" stroke="var(--series-put)"`));
  const macdTags = [];
  [["macd", "var(--series-call)"], ["macd_signal", "var(--series-put)"]].forEach(([k, c]) => {
    const v = d.series[k]?.[last];
    if (techOk(v)) macdTags.push({ price: v, colour: c, fmt: (q) => techSigned(q) });
  });
  parts.push(`<text class="axis-label" x="${right + 6}" y="${my(0) + 3}">0.00</text>`);
  techPlaceTags(macdTags, mTop, mTop + mH, my);
  tagsOut.push(...techTagMarkup(macdTags, right, padR));

  /* ---------------- VAMS ribbons ---------------- */
  [["vams", d.vams.state], ["vamscheck", d.vams.check]].forEach(([key, values]) => {
    const p = panes[key];
    const bTop = p.top + TECH_TITLE_H, bH = p.h - TECH_TITLE_H;
    for (let i = lo; i < hi; i++) {
      const v = values[i];
      if (!techOk(v)) continue;
      const cls = v > 0 ? "vams-bull" : v < 0 ? "vams-bear" : "vams-neutral";
      parts.push(`<rect class="${cls}" x="${(x(i) - bw / 2).toFixed(2)}" y="${bTop}" width="${(bw + 0.25).toFixed(2)}" height="${bH}"/>`);
    }
  });

  /* ---------------- lunar-solar pane ---------------- */
  let astroGeom = null;
  if (astro) {
    const ap = panes.astro;
    const aTop = ap.top + TECH_TITLE_H, aBot = ap.top + ap.h;
    const pane = astroPaneMarkup(astro, { lo, hi, total, x, bw, left: padL, right, top: aTop, bottom: aBot });
    parts.push(...pane.parts);
    // A named tag per line at the last bar, as the reference draws them.
    const astroTags = ASTRO_LINES
      .map((l) => ({ price: astroAt(astro, l.key, last, total), colour: l.colour, name: l.label, fmt: (v) => astroSigned(v) }))
      .filter((t) => t.price !== null);
    techPlaceTags(astroTags, pane.plotTop, aBot, pane.y);
    [[1, "+1"], [0, "0"], [-1, "−1"]].forEach(([v, label]) => {
      if (astroTags.some((t) => Math.abs(t.ty - pane.y(v)) < 12)) return;
      parts.push(`<text class="axis-label" x="${right + 6}" y="${(pane.y(v) + 3).toFixed(1)}">${label}</text>`);
    });
    tagsOut.push(...techTagMarkup(astroTags, right, padR));
    // The crosshair reads the plot under the icon strip, where y maps ±1.1.
    astroGeom = { top: pane.plotTop, bottom: aBot, lo: -1.1, hi: 1.1, fmt: (v) => astroSigned(v) };
  }

  /* ---------------- pane titles (updated on hover) ---------------- */
  ["volume", ...(astro ? ["astro"] : []), "rsi", "macd", "vams", "vamscheck"].forEach((k) =>
    parts.push(`<text class="pane-title" data-ptitle="${k}" x="${padL + 2}" y="${panes[k].top + 10}"></text>`));

  /* ---------------- date axis ---------------- */
  parts.push(...techDateAxis(d, lo, hi, x, axisTop, innerW, n, pTop));
  // Contract changes (futures): where an adjustment was made, or where the
  // unadjusted series steps.
  (d.rolls || []).filter((r) => r.i >= lo && r.i < hi).forEach((r) => {
    const rx = (x(r.i) - bw / 2).toFixed(1);
    const what = d.adjusted
      ? `earlier prices shifted ${techSigned(r.gap)}`
      : `the series steps ${techSigned(r.gap)} here`;
    parts.push(`<g class="roll-mark"><title>${esc(`Contract change ${r.date}: ${what}${r.verified ? "" : " (roll date not verifiable before Dec 2022)"}`)}</title>` +
      `<line x1="${rx}" x2="${rx}" y1="${(pBot - 10).toFixed(1)}" y2="${pBot.toFixed(1)}"/>` +
      `<text x="${rx}" y="${(pBot - 12).toFixed(1)}" text-anchor="middle">roll</text></g>`);
  });

  /* ---------------- hit area + crosshair ---------------- */
  parts.push(`<rect class="hit" x="${padL}" y="${pp.top}" width="${innerW}" height="${axisTop - pp.top}"/>`);
  parts.push(...tagsOut);
  parts.push(`<g class="txhair" data-txhair style="display:none">
    <line data-tv y1="${pp.top}" y2="${axisTop}"/>
    <line data-th x1="${padL}" x2="${right}"/>
    <g data-tval><rect x="${right + 2}" width="${padR - 4}" height="14" rx="2"/><text x="${right + 6}"></text></g>
    <g data-tdate><rect height="15" rx="2"/><text text-anchor="middle"></text></g>
  </g>`);

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${esc(d.symbol)} technical chart">${parts.join("")}</svg>`;
  const svg = host.querySelector("svg");

  // `total` is every slot, future dates included, because pan and zoom
  // range over them; `bars` is where the candles stop.
  tech.geom = {
    W, H, padL, padR, innerW, right, bw, lo, hi, n, total: slots, bars: total, axisTop,
    panes: {
      price: { top: pTop, bottom: pBot, lo: yLo, hi: yHi, fmt: techPx },
      volume: { top: vTop, bottom: vTop + vH, lo: 0, hi: vmax || 1, fmt: techVol },
      rsi: { top: rTop, bottom: rTop + rH, lo: 0, hi: 100, fmt: (v) => v.toFixed(2) },
      macd: { top: mTop, bottom: mTop + mH, lo: -mAbs, hi: mAbs, fmt: (v) => techSigned(v) },
      ...(astroGeom ? { astro: astroGeom } : {}),
    },
  };
  techAttach(svg);
  // Drawings, in time and price, on the slots this render laid out.
  drawAttach("tech", {
    host, svg, symbol: d.symbol, plot: { l: padL, r: right, t: pTop, b: pBot },
    x0: padL, lo, bw, yLo, yHi, stamps: d.time,
    kind: d.interval === "1D" ? "daily" : d.interval === "1W" ? "weekly" : "minute",
    step: techIsIntraday(d.interval) ? techIntervalMinutes(d.interval) * 60 : 0,
    bars: { open: d.open, high: d.high, low: d.low, close: d.close, volume: d.volume },
    fmtPrice: techPx, fmtTime: (stamp) => techStamp(stamp, d.interval),
  });
  techNotes(d);
  techRules(d);
  techLegend(null);
  if (tech.xhair) techDrawCrosshair(svg, tech.xhair.x, tech.xhair.y);
}

/** Lay tags out top-down without overlap, then pull any that ran off the
 *  bottom back up. Each tag keeps its true price; `ty` is where it sits. */
function techPlaceTags(tags, top, bottom, y) {
  const TAG_H = 14;
  tags.forEach((t) => { t.y = Math.min(Math.max(y(t.price), top + TAG_H / 2), bottom - TAG_H / 2); });
  // The last-price tag is pinned; everything else moves around it.
  tags.sort((a, b) => a.y - b.y || (a.solid ? -1 : 1));
  let prev = -Infinity;
  tags.forEach((t) => { t.ty = Math.max(t.y, prev + TAG_H); prev = t.ty; });
  let next = bottom - TAG_H / 2 + TAG_H;
  for (let k = tags.length - 1; k >= 0; k--) {
    tags[k].ty = Math.min(tags[k].ty, next - TAG_H);
    next = tags[k].ty;
  }
}

/** SVG for right-axis value tags already placed by techPlaceTags(): a
 *  coloured box with the value, a name box to its left for named levels,
 *  and a short leader when the tag had to move off its price. */
function techTagMarkup(tags, right, padR) {
  const out = [];
  tags.forEach((t) => {
    const fmt = t.fmt || techPx;
    out.push(`<g class="ttag${t.solid ? " solid" : ""}" style="--c:${t.colour}">` +
      `<rect x="${right + 2}" y="${(t.ty - 7).toFixed(1)}" width="${padR - 4}" height="14" rx="2"/>` +
      `<text x="${right + 6}" y="${(t.ty + 3.5).toFixed(1)}">${fmt(t.price)}</text></g>`);
    if (t.name) {
      const w = t.name.length * 5.6 + 10;
      out.push(`<g class="tname"><rect x="${(right - w - 2).toFixed(1)}" y="${(t.ty - 7).toFixed(1)}" width="${w.toFixed(1)}" height="14" rx="2"/>` +
        `<text x="${right - 6}" y="${(t.ty + 3.5).toFixed(1)}" text-anchor="end">${esc(t.name)}</text></g>`);
    }
    // A tag displaced from its price gets a short leader back to it.
    if (Math.abs(t.ty - t.y) > 2) {
      out.push(`<line class="tag-leader" stroke="${t.colour}" x1="${right - 3}" x2="${right + 2}" y1="${t.y.toFixed(1)}" y2="${t.ty.toFixed(1)}"/>`);
    }
  });
  return out;
}

/** Month labels at month boundaries, thinned to fit; January shows the year. */
function techDateAxis(d, lo, hi, x, axisTop, innerW, n, plotTop) {
  if (techIsIntraday(d.interval)) return techTimeAxis(d, lo, hi, x, axisTop, innerW, n, plotTop);
  const out = [];
  const marks = [];
  let prevKey = null;
  for (let i = lo; i < hi; i++) {
    const dt = techDate(techSlotTime(d, i));
    const key = dt.getUTCFullYear() * 12 + dt.getUTCMonth();
    if (prevKey !== null && key !== prevKey) marks.push({ i, dt });
    prevKey = key;
  }
  // Under ~two months on screen, label weeks instead of months.
  if (marks.length < 2) {
    const every = Math.max(1, Math.round(n / Math.max(innerW / 90, 2)));
    for (let i = lo; i < hi; i += every) {
      const dt = techDate(techSlotTime(d, i));
      out.push(`<text class="axis-label date" x="${x(i).toFixed(1)}" y="${axisTop + 13}" text-anchor="middle">${dt.getUTCDate()} ${TECH_MONTH_FMT.format(dt)}</text>`);
    }
    return out;
  }
  const perMonth = innerW / Math.max(marks.length, 1);
  const k = [1, 2, 3, 6, 12].find((s) => s * perMonth >= 44) || 12;
  marks.forEach(({ i, dt }) => {
    const m = dt.getUTCMonth();
    if (m % k !== 0) return;
    const label = m === 0 ? String(dt.getUTCFullYear()) : TECH_MONTH_FMT.format(dt);
    out.push(`<line class="grid-line" x1="${x(i).toFixed(1)}" x2="${x(i).toFixed(1)}" y1="${axisTop - 3}" y2="${axisTop}"/>`);
    out.push(`<text class="axis-label date${m === 0 ? " year" : ""}" x="${x(i).toFixed(1)}" y="${axisTop + 13}" text-anchor="middle">${label}</text>`);
  });
  return out;
}

/** Minutes after midnight in the chosen zone for every bar, computed once per
 *  payload and zone: switching zones redraws without refetching. */
function techClockMinutes(d) {
  if (d._clockZone !== tz.zone) {
    d._clockMin = d.time.map((t) => tzMinutes(techInstant(t)));
    d._clockZone = tz.zone;
  }
  return d._clockMin;
}

/** Intraday axis: each session's date at its first bar, with a faint line up
 *  through the chart (bars are spaced by index, so a session break is
 *  otherwise invisible), and clock times between them where there is room. */
function techTimeAxis(d, lo, hi, x, axisTop, innerW, n, plotTop) {
  const out = [];
  const breaks = (d.session_breaks || []).map((i, k) => ({ i, date: d.session_labels?.[k] }))
    .filter((b) => b.i >= lo && b.i < hi);
  const bw = innerW / Math.max(n, 1);
  const many = breaks.length > 12;
  const dayLabel = (iso) => {
    const dt = new Date(`${iso}T00:00:00Z`);
    return many ? `${TECH_MONTH_FMT.format(dt)} ${dt.getUTCDate()}` : `${TECH_WEEKDAYS[dt.getUTCDay()]} ${dt.getUTCDate()}`;
  };
  // Thin the date labels to one per ~56px.
  let lastX = -Infinity;
  const labelled = [];
  breaks.forEach((b) => {
    const bx = x(b.i);
    const edge = (bx - bw / 2).toFixed(1);   // between the last bar of one session and the first of the next
    // Dividers help across a few sessions; across dozens they are a fence.
    if (b.i > lo && breaks.length <= 15) out.push(`<line class="session-line" x1="${edge}" x2="${edge}" y1="${plotTop}" y2="${axisTop}"/>`);
    if (bx - lastX < 56 || !b.date) return;
    lastX = bx;
    labelled.push(bx);
    out.push(`<text class="axis-label date year" x="${bx.toFixed(1)}" y="${axisTop + 13}" text-anchor="middle">${dayLabel(b.date)}</text>`);
  });
  // Clock times: the smallest round step that leaves ~64px between labels.
  const minutesPerBar = techIntervalMinutes(d.interval);
  const step = [15, 30, 60, 120, 240, 480].find((s) => (s / minutesPerBar) * bw >= 64);
  if (!step) return out;
  const clock = techClockMinutes(d);
  const starts = new Set(breaks.map((b) => b.i));
  for (let i = Math.max(lo, 1); i < hi; i++) {
    if (starts.has(i) || Math.floor(clock[i] / step) === Math.floor(clock[i - 1] / step)) continue;
    const bx = x(i);
    if (labelled.some((lx) => Math.abs(lx - bx) < 40)) continue;
    labelled.push(bx);
    const hm = `${String(Math.floor(clock[i] / 60)).padStart(2, "0")}:${String(clock[i] % 60).padStart(2, "0")}`;
    out.push(`<text class="axis-label date" x="${bx.toFixed(1)}" y="${axisTop + 13}" text-anchor="middle">${hm}</text>`);
  }
  return out;
}

/** Minutes per bar for an intraday key ("5m" → 5, "4h" → 240). Only
 *  intraday keys reach it; 1440 is a fallback, not a daily setting. */
function techIntervalMinutes(key) {
  const m = /^(\d+)([mh])$/.exec(String(key));
  return m ? Number(m[1]) * (m[2] === "h" ? 60 : 1) : 1440;
}

/** The note line under the chart: a synthetic-data warning, the server's
 *  notes (warm-up, substitutions, futures adjustment), the profile's scope
 *  and, when zoomed, how to pan and reset. */
function techNotes(d) {
  const bits = [];
  if (d.synthetic) bits.push("SYNTHETIC — NOT MARKET DATA.");
  bits.push(...(d.notes || []));
  bits.push("Volume profile and PoC cover the selected range.");
  if (tech.zoom) bits.push("Zoomed: drag to pan, ⤢ to fit.");
  $("tech-note").textContent = bits.join("  ");
}

/* ------------------------------------------------------- legend / titles */

/** Legend and pane titles for one bar; null means the most recent. */
function techLegend(index) {
  const d = tech.data, g = tech.geom;
  if (!d || !d.available || !g) return;
  // At rest the legend shows the last bar in view, never a future date.
  const i = index == null ? Math.min(g.hi, g.bars) - 1 : index;
  $("tech-legend").innerHTML = techLegendRows(d, i);
  techPaneTitles(d, i, index == null);
}

/** Legend markup for slot i: a bar, or past the last bar a lunar-solar
 *  future date. Separate so the render can measure it first. */
function techLegendRows(d, i) {
  const astro = techAstro(d);
  const total = d.close.length;
  const item = (colour, name, value) =>
    `<span class="tl-item">${colour ? `<span class="r-dot" style="background:${colour}"></span>` : ""}${name} <b>${value}</b></span>`;
  if (i >= total) {
    // A future date has no candle: its date, the projected price where the
    // bar size has one, and the Moon.
    const s = astroSlotInfo(astro, i, total);
    return `<div class="tl-row tl-head"><span class="tl-sym">${esc(d.symbol)}</span>` +
      `<span class="muted">projected · lunar-solar cycle</span>` +
      `<span class="tl-when">${techStamp(techSlotTime(d, i), d.interval)}</span>` +
      (s?.path !== null && s?.path !== undefined ? item(null, "Path", techPx(s.path)) : "") + `</div>` +
      (astro ? techAstroLegendRow(astro, i, total, item, false) : "");
  }
  const prev = i > 0 ? d.close[i - 1] : d.prev_close;
  const chg = techOk(prev) && prev ? ((d.close[i] - prev) / prev) * 100 : null;
  const dir = techOk(prev) && d.close[i] < prev ? "dn" : "up";
  const ivCaps = techIntervalCaps(d.interval);
  const sessionName = { rth: "Regular hours", eth: "Extended hours", futures: "Full session" }[d.session];
  const interval = [ivCaps?.label || d.interval, sessionName].filter(Boolean).join(" · ");

  const rows = [
    `<div class="tl-row tl-head"><span class="tl-sym">${esc(d.symbol)}</span>` +
      `<span class="muted">${esc(d.label || "")} · ${interval}</span>` +
      `<span class="tl-when">${techStamp(d.time[i], d.interval)}</span>` +
      `${item(null, "O", techPx(d.open[i]))}${item(null, "H", techPx(d.high[i]))}` +
      `${item(null, "L", techPx(d.low[i]))}${item(null, "C", techPx(d.close[i]))}` +
      `<span class="tl-chg ${dir}">${chg === null ? "" : `${techSigned(d.close[i] - prev)} (${techSigned(chg)}%)`}</span></div>`,
  ];
  const ov = TECH_OVERLAYS.filter((o) => tech.layers[o.layer])
    .map((o) => item(o.colour, o.label, techPx(d.series[o.key]?.[i])));
  if (tech.layers.supertrend && d.series.st_up) {
    const up = d.series.st_up[i], dn = d.series.st_dn[i];
    const p = d.params || {};
    const name = `Supertrend ${p.st_period ?? 10}/${p.st_mult ?? 3}`;
    if (techOk(up)) ov.push(item("var(--st-up)", name, `▲ ${techPx(up)}`));
    else if (techOk(dn)) ov.push(item("var(--st-dn)", name, `▼ ${techPx(dn)}`));
  }
  if (tech.layers.ranges && d.current_range) {
    ov.push(item(null, "Range high", techPx(d.current_range.high)));
    ov.push(item(null, "Range low", techPx(d.current_range.low)));
  }
  if (ov.length) rows.push(`<div class="tl-row">${ov.join("")}</div>`);
  if (astro) rows.push(techAstroLegendRow(astro, i, total, item, i === total - 1));
  return rows.join("");
}

/** The legend's Moon row for slot i: phase and share lit, distance and
 *  declination, and on the latest bar the next new or full moon. */
function techAstroLegendRow(a, i, total, item, latest) {
  const s = astroSlotInfo(a, i, total);
  if (!s || s.phase === null) return "";
  const bits = [item(null, s.phase, `${Math.round((s.illum ?? 0) * 100)}% lit`)];
  if (s.dist !== null) bits.push(item(null, "Moon", `${Math.round(s.dist).toLocaleString("en-US")} km`));
  if (s.dec !== null) bits.push(item(null, "dec", `${astroSigned(s.dec, 1)}°`));
  const next = latest ? astroNextPhase(a, i) : null;
  if (next) bits.push(item(null, `Next ${next.label.toLowerCase()}`, astroWhen(next)));
  return `<div class="tl-row">${bits.join("")}</div>`;
}

/** Titles of the lower panes for bar i; `latest` adds the current position. */
function techPaneTitles(d, i, latest) {
  const s = d.series;
  const vamsName = (v, check) => (!techOk(v) ? "n/a" : v > 0 ? "Bullish" : v < 0 ? "Bearish" : check ? "no call" : "Neutral");
  const val = (v, colour) => `<tspan class="v"${colour ? ` fill="${colour}"` : ""}>${v}</tspan>`;
  const dot = (colour) => `<tspan fill="${colour}">●</tspan>`;
  const titles = {
    // Zero means "not reported" here (Schwab sends 0 for the in-progress /ES
    // weekly bar), not a session in which nothing traded.
    volume: `Volume ${val(d.volume[i] > 0 ? techVol(d.volume[i]) : "—")}`,
    rsi: `RSI 14 ${dot("var(--series-violet)")}${val(techOk(s.rsi?.[i]) ? s.rsi[i].toFixed(2) : "—")}`,
    macd: `MACD 12/26/9 ${dot("var(--series-call)")}${val(techSigned(s.macd?.[i]))} ` +
      `${dot("var(--series-put)")}${val(techSigned(s.macd_signal?.[i]))} ${val(techSigned(s.macd_histogram?.[i]))}`,
    vams: `VAMS ${val(vamsName(d.vams.state[i]))}` +
      (latest ? `  <tspan class="muted-t">· position ${esc(d.position)}</tspan>` : ""),
    vamscheck: `VAMS check ${val(vamsName(d.vams.check[i], true))}`,
    astro: techAstro(d) ? astroTitleSpans(d.astro, i, d.close.length) : "",
  };
  document.querySelectorAll("#tech-chart [data-ptitle]").forEach((t) => {
    t.innerHTML = titles[t.dataset.ptitle] || "";
  });
}

/* ------------------------------------------------------------ crosshair */

/** Pointer and wheel handlers for one chart SVG, attached on every render
 *  as each render replaces it. A drag past 3px pans, hover draws the
 *  crosshair, and the wheel zooms over the price pane only. Pan state is on
 *  tech.drag, not in a closure, so a drag carries on across the re-render. */
function techAttach(svg) {
  svg.addEventListener("pointerdown", (ev) => {
    const g = tech.geom;
    if (!g) return;
    tech.drag = { x: ev.clientX, lo: g.lo, hi: g.hi, moved: false };
  });
  svg.addEventListener("pointermove", (ev) => {
    tech.xhair = { x: ev.clientX, y: ev.clientY };
    const g = tech.geom;
    if (tech.drag && g) {
      const dx = ev.clientX - tech.drag.x;
      if (Math.abs(dx) > 3) tech.drag.moved = true;
      if (tech.drag.moved) {
        const r = svg.getBoundingClientRect();
        const span = tech.drag.hi - tech.drag.lo;
        const barsPerPx = span / (g.innerW * (r.width / g.W));
        const nlo = Math.min(Math.max(tech.drag.lo - dx * barsPerPx, 0), g.total - span);
        tech.zoom = span >= g.total ? null : [nlo, nlo + span];
        renderTechnicals();
        return;
      }
    }
    techDrawCrosshair(svg, ev.clientX, ev.clientY);
  });
  const stop = () => { tech.drag = null; };
  svg.addEventListener("pointerup", stop);
  svg.addEventListener("pointercancel", stop);
  svg.addEventListener("pointerleave", () => {
    // Re-rendering replaces this SVG under the cursor and Chrome fires a leave
    // on the detached node. That is not the pointer leaving; honouring it
    // would wipe the crosshair the new render just restored.
    if (!svg.isConnected) return;
    stop();
    tech.xhair = null;
    techHideCrosshair();
  });
  svg.addEventListener("wheel", (ev) => {
    const g = tech.geom;
    if (!g) return;
    const r = svg.getBoundingClientRect();
    // Zoom only over the price pane. The chart is ~900px tall; capturing the
    // wheel across every pane would leave the page nearly unscrollable.
    const pyv = ((ev.clientY - r.top) / r.height) * g.H;
    if (pyv < g.panes.price.top || pyv > g.panes.price.bottom) return;
    ev.preventDefault();
    const px = ((ev.clientX - r.left) / r.width) * g.W;
    const anchor = Math.min(Math.max(g.lo + (px - g.padL) / g.bw, g.lo), g.hi);
    const factor = ev.deltaY > 0 ? 1.15 : 1 / 1.15;
    const span = Math.min(Math.max((g.hi - g.lo) * factor, 10), g.total);
    const frac = (anchor - g.lo) / Math.max(g.hi - g.lo, 1);
    const nlo = Math.min(Math.max(anchor - frac * span, 0), g.total - span);
    tech.zoom = span >= g.total ? null : [nlo, nlo + span];
    renderTechnicals();
  }, { passive: false });
}

/** The zoom buttons: scale the visible span about its centre (factor < 1
 *  zooms in), never below 10 bars. */
function techZoomBy(factor) {
  const g = tech.geom;
  if (!g) return;
  const mid = (g.lo + g.hi) / 2;
  const span = Math.min(Math.max((g.hi - g.lo) * factor, 10), g.total);
  const nlo = Math.min(Math.max(mid - span / 2, 0), g.total - span);
  tech.zoom = span >= g.total ? null : [nlo, nlo + span];
  renderTechnicals();
}

/** Hide this chart's crosshair only ([data-txhair] in #tech-chart) and
 *  return the legend and pane titles to the latest bar. */
function techHideCrosshair() {
  document.querySelectorAll("#tech-chart [data-txhair]").forEach((g) => (g.style.display = "none"));
  techLegend(null);
}

/** Put the crosshair at a client point, snapped to the bar under it, and
 *  show that bar in the legend and pane titles. The horizontal line and
 *  value tag appear only over a pane with a value scale (price, volume, RSI,
 *  MACD; not the VAMS ribbons). Left of the first bar, past the last, or
 *  over the date axis, it hides instead. */
function techDrawCrosshair(svg, clientX, clientY) {
  const g = tech.geom;
  const d = tech.data;
  if (!g || !d) return;
  const r = svg.getBoundingClientRect();
  const px = ((clientX - r.left) / r.width) * g.W;
  const pyv = ((clientY - r.top) / r.height) * g.H;
  const slot = Math.floor((px - g.padL) / g.bw);
  const i = g.lo + slot;
  if (slot < 0 || i >= g.hi || pyv > g.axisTop) return techHideCrosshair();

  const cx = g.padL + (slot + 0.5) * g.bw;
  const xh = svg.querySelector("[data-txhair]");
  xh.style.display = "";
  const v = xh.querySelector("[data-tv]");
  v.setAttribute("x1", cx); v.setAttribute("x2", cx);

  // Horizontal line and value tag only inside a pane with a value scale.
  const pane = Object.values(g.panes).find((p) => pyv >= p.top && pyv <= p.bottom);
  const h = xh.querySelector("[data-th]");
  const tv = xh.querySelector("[data-tval]");
  if (pane) {
    const value = pane.hi - ((pyv - pane.top) / (pane.bottom - pane.top)) * (pane.hi - pane.lo);
    h.style.display = ""; tv.style.display = "";
    h.setAttribute("y1", pyv); h.setAttribute("y2", pyv);
    tv.querySelector("rect").setAttribute("y", pyv - 7);
    const t = tv.querySelector("text");
    t.setAttribute("y", pyv + 3.5);
    t.textContent = pane.fmt(value);
  } else {
    h.style.display = "none"; tv.style.display = "none";
  }

  const label = techStamp(techSlotTime(d, i), d.interval);
  const tw = label.length * 5.6 + 10;
  const lx = Math.min(Math.max(cx, g.padL + tw / 2), g.right - tw / 2);
  const dg = xh.querySelector("[data-tdate]");
  dg.querySelector("rect").setAttribute("x", lx - tw / 2);
  dg.querySelector("rect").setAttribute("y", g.axisTop + 2);
  dg.querySelector("rect").setAttribute("width", tw);
  const dt = dg.querySelector("text");
  dt.setAttribute("x", lx);
  dt.setAttribute("y", g.axisTop + 13);
  dt.textContent = label;

  techLegend(i);
}
