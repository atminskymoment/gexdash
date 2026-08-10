/* gexdash client.
 *
 * Two layouts over one dataset:
 *   terminal  — chart-centric: KPI strip, candles with level annotations,
 *               tabbed exposure-by-strike, order flow & bias.
 *   columns   — panel-centric: market bias / spot+levels / zone / signal chart.
 *
 * Charts are hand-rolled SVG: no chart library, no CDN, no build step. Both
 * views share the primitives in the CHART PRIMITIVES section below — the
 * horizontal exposure panel is a coordinate swap of the vertical one, not a
 * second renderer, so they cannot drift apart.
 *
 * Two structural rules, both departures from the commercial dashboards these
 * layouts are modelled on:
 *
 *  1. NO DUAL AXES. Two y-scales on one plot align arbitrarily and invent a
 *     correlation the data does not contain. Each measure gets its own plot.
 *     Level *annotations* on the price axis are fine — they are prices. An
 *     exposure curve on that axis would not be.
 *  2. Calls/puts are BLUE/ORANGE, not green/red — the worst pair under
 *     colour-vision deficiency, and the one distinction that must never be
 *     ambiguous. Candle bodies use the diverging pos/neg pair, which is the
 *     same blue/red used for signed exposure.
 */

const $ = (id) => document.getElementById(id);

const state = {
  data: null,
  bars: null,
  breadth: null,
  meta: null,
  stream: null,
  showTable: false,
  view: "terminal",
  expTab: "gex",
  range: "1D",
  interval: "1m",
  // Visible window as fractional indices into the bar array. null = fit all.
  zoom: null,
  indicators: { rsi: true, macd: true, adx: true },
  // Last pointer position over the price chart, so the crosshair survives a
  // re-render. null when the pointer is away.
  xhair: null,
  // Bar the readout is showing; null means the most recent.
  readoutIndex: null,
};

/* ----------------------------------------------------------- formatting */

function money(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v), sign = v < 0 ? "-" : "";
  if (a >= 1e12) return `${sign}$${(a / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(1)}K`;
  return `${sign}$${a.toFixed(0)}`;
}

function count(v) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const a = Math.abs(v);
  if (a >= 1e6) return `${(v / 1e6).toFixed(2)}M`;
  if (a >= 1e3) return `${(v / 1e3).toFixed(1)}K`;
  return v.toFixed(0);
}

const px = (v, d = 2) => (v === null || v === undefined ? "—" : v.toFixed(d));
const signed = (v, d = 2) => (v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(d)}`);

/* Decimals only when the value has them. Listed strikes are whole numbers and
   "7625.00" is noise; spot and the solved flip are genuinely fractional. */
function priceFmt(v) {
  if (v === null || v === undefined) return "—";
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

/* Issue counts are small integers a reader compares directly (332 vs 170).
   The K/M abbreviation used for dollar magnitudes destroys that. */
const issues = (v) =>
  v === null || v === undefined ? "—" : Math.round(v).toLocaleString("en-US");

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

/* ═══════════════════════ CHART PRIMITIVES (shared) ═══════════════════════ */

/* A fine ladder on purpose. A coarse 1/2/5/10 ladder rounds a 5.7-unit
 * maximum up to 10, so the tallest bar fills barely half its plot and the
 * chart reads as flat. */
const NICE_STEPS = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];

function niceCeil(v) {
  if (!(v > 0)) return 1;
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  return (NICE_STEPS.find((s) => v / exp <= s + 1e-9) ?? 10) * exp;
}

function axisTicks(maxAbs, isSigned) {
  const m = niceCeil(maxAbs);
  return isSigned ? { bound: m, ticks: [-m, 0, m] } : { bound: m, ticks: [0, m / 2, m] };
}

/** Vertical bar: rounded outer end, square to the baseline. */
function barPathV(x, y, w, h, up) {
  const r = Math.min(3, w / 2, Math.abs(h));
  if (h <= 0.5) return `M${x},${y}h${w}`;
  return up
    ? `M${x},${y + h} v${-(h - r)} a${r},${r} 0 0 1 ${r},${-r} h${w - 2 * r} a${r},${r} 0 0 1 ${r},${r} v${h - r} z`
    : `M${x},${y} v${h - r} a${r},${r} 0 0 0 ${r},${r} h${w - 2 * r} a${r},${r} 0 0 0 ${r},${-r} v${-(h - r)} z`;
}

/** Horizontal bar: the same shape rotated. `len` grows right when `right`. */
function barPathH(x0, y, len, thick, right) {
  const r = Math.min(3, thick / 2, Math.abs(len));
  if (len <= 0.5) return `M${x0},${y}v${thick}`;
  return right
    ? `M${x0},${y} h${len - r} a${r},${r} 0 0 1 ${r},${r} v${thick - 2 * r} a${r},${r} 0 0 1 ${-r},${r} h${-(len - r)} z`
    : `M${x0},${y} h${-(len - r)} a${r},${r} 0 0 0 ${-r},${r} v${thick - 2 * r} a${r},${r} 0 0 0 ${r},${r} h${len - r} z`;
}

/* Strikes shown in the plots. Never a silent cap — the count is displayed. */
function visibleIndices(d, pct = 0.05) {
  const lo = d.spot * (1 - pct), hi = d.spot * (1 + pct);
  const idx = d.strikes.map((k, i) => [k, i]).filter(([k]) => k >= lo && k <= hi).map(([, i]) => i);
  return idx.length >= 5 ? idx : d.strikes.map((_, i) => i);
}

/** Metric definitions, shared by both views' exposure charts. */
const METRICS = {
  gex: { label: "Gamma", title: "Gamma exposure (GEX)", fmt: money, kind: "diverging", key: "gex" },
  dex: { label: "Delta", title: "Delta exposure (DEX)", fmt: money, kind: "diverging", key: "dex" },
  vanx: { label: "Vanna", title: "Vanna exposure (VANX)", fmt: money, kind: "diverging", key: "vanx" },
  cex: { label: "Charm", title: "Charm exposure (CEX)", fmt: money, kind: "diverging", key: "cex" },
  volume: { label: "Volume", title: "Contract volume", fmt: count, kind: "grouped", a: "call_volume", b: "put_volume" },
  oi: { label: "OI", title: "Open interest", fmt: count, kind: "grouped", a: "call_oi", b: "put_oi" },
};

function seriesFor(d, spec, idx) {
  if (spec.kind === "grouped") {
    const a = idx.map((i) => d[spec.a][i]), b = idx.map((i) => d[spec.b][i]);
    return { grouped: true, a, b, maxAbs: Math.max(...a, ...b, 1) };
  }
  const v = idx.map((i) => d[spec.key][i]);
  return { grouped: false, v, maxAbs: Math.max(...v.map(Math.abs), 1) };
}

const legendFor = (spec) =>
  spec.kind === "grouped"
    ? `<span class="item"><span class="swatch" style="background:var(--series-call)"></span>calls</span>
       <span class="item"><span class="swatch" style="background:var(--series-put)"></span>puts</span>`
    : `<span class="item"><span class="swatch" style="background:var(--pos)"></span>+</span>
       <span class="item"><span class="swatch" style="background:var(--neg)"></span>−</span>`;

/* ---------------------------------------------------------------- fetch */

function params() {
  return new URLSearchParams({
    symbol: $("f-symbol").value || "SPX",
    expiry_window: $("f-window").value,
    dealer_sign: $("f-sign").value,
    flip_method: $("f-flip").value,
  });
}

async function loadMeta() {
  const meta = await (await fetch("/api/meta")).json();
  state.meta = meta;
  const sel = $("f-symbol");
  sel.innerHTML = Object.entries(meta.families)
    .map(([family, keys]) => {
      const opts = keys.map((k) => {
        const i = meta.instruments.find((x) => x.key === k);
        return `<option value="${k}">${k} — ${esc(i.label)}</option>`;
      }).join("");
      return `<optgroup label="${esc(family)}">${opts}</optgroup>`;
    }).join("");
  const wanted = (new URLSearchParams(location.search).get("symbol") || "SPX").toUpperCase();
  sel.value = meta.instruments.some((i) => i.key === wanted) ? wanted : "SPX";
}

async function load() {
  const host = activeMain();
  host.classList.add("stale");
  try {
    const res = await fetch(`/api/exposure?${params()}`);
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    render(body);
    notice(null);
  } catch (err) {
    notice(err.message, true);
  } finally {
    host.classList.remove("stale");
  }
}

async function loadBreadth() {
  try {
    const res = await fetch(`/api/breadth?symbol=${$("f-symbol").value}`);
    state.breadth = await res.json();
  } catch (err) {
    state.breadth = { available: false, reason: err.message };
  }
  if (state.view === "terminal") renderInternals();
}

async function loadBars() {
  const q = new URLSearchParams({
    symbol: $("f-symbol").value, range: state.range, interval: state.interval, indicators: "true",
  });
  try {
    const res = await fetch(`/api/history?${q}`);
    state.bars = await res.json();
    state.zoom = null;                       // a new series starts fitted
  } catch (err) {
    state.bars = { available: false, reason: err.message };
  }
  if (state.view === "terminal") { renderChartControls(); renderPriceChart(); }
}

/* Errors reuse a single node. Under SSE this fires on every failed tick and
 * EventSource retries on a timer — prepending would grow an unbounded stack. */
function notice(msg, isError, allowHtml) {
  const box = $("notices");
  if (!msg) { box.querySelectorAll(".err").forEach((n) => n.remove()); return; }
  const cls = isError ? "err" : "info";
  let el = box.querySelector(`.notice.${cls}`);
  if (!el) { el = document.createElement("div"); el.className = `notice ${cls}`; box.prepend(el); }
  const n = isError ? Number(el.dataset.count || 0) + 1 : 0;
  el.dataset.count = n;
  // Only trusted, locally-composed strings may pass allowHtml — provider
  // and error text is always escaped.
  const shown = allowHtml ? msg : esc(msg);
  el.innerHTML = `<strong>${isError ? "Error" : "Note"}:</strong> ${shown}` +
    (n > 1 ? ` <span class="muted">(×${n})</span>` : "");
}

/* --------------------------------------------------------------- render */

const activeMain = () => (state.view === "terminal" ? $("view-terminal") : $("view-columns"));

function render(d) {
  state.data = d;
  renderHeader(d);
  renderStrip(d);
  // Only the mounted view is rendered: the other one's containers report
  // clientWidth 0 while hidden, which would produce degenerate geometry.
  if (state.view === "terminal") renderTerminal(d);
  else renderColumns(d);
  renderTableView(d);
  renderDiagnostics(d);
}

function renderHeader(d) {
  const s = d.session || {};
  $("clock-time").textContent = s.exchange_time || "—";
  const phase = $("clock-phase");
  phase.textContent = s.phase || "—";
  phase.className = `phase ${s.phase || ""}`;
  const secs = s.seconds_to_next || 0;
  $("clock-next").textContent = s.next_transition
    ? `→ ${s.next_transition} in ${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m` : "";
  $("badge-provider").textContent = `provider: ${d.provider}`;

  // Connection state, if the provider has an auth story at all.
  const auth = d.auth || {};
  if (auth.needs_login) {
    notice('Schwab credentials are configured but not connected. ' +
           '<a href="/connect">Connect to Schwab →</a>', false, true);
  } else if (auth.refresh_expires_in_days !== null && auth.refresh_expires_in_days !== undefined
             && auth.refresh_expires_in_days < 2) {
    notice(`Schwab refresh token expires in ${auth.refresh_expires_in_days.toFixed(1)} days. ` +
           '<a href="/connect">Reconnect →</a>', false, true);
  }
  const syn = $("badge-synthetic");
  syn.hidden = !d.synthetic;
  syn.textContent = "synthetic — not market data";
}

function renderStrip(d) {
  const lo = Math.min(...d.strikes), hi = Math.max(...d.strikes);
  const im = d.instrument_meta || {};
  $("strip-range").textContent = `${priceFmt(lo)} – ${priceFmt(hi)}`;
  $("strip-strikes").textContent = `${d.strikes.length} strikes (±${im.display_strikes} ATM, ${im.strike_step} step)`;
  $("strip-contracts").textContent = d.contracts;
  $("strip-exercise").innerHTML = im.note
    ? `<span style="color:var(--serious)">⚠ ${esc(im.note)}</span>`
    : `${esc(im.exercise_style || "")} · ×${im.multiplier}`;
  $("strip-oi").innerHTML =
    `OI as of <b>${d.diagnostics.oi_asof || "unknown"}</b> — OCC publishes once daily; re-priced against live spot` +
    (d.stale ? ' · <span style="color:var(--critical)">STALE CHAIN</span>' : "");
}

/* ═══════════════════════════ TERMINAL VIEW ═══════════════════════════ */

const KPIS = [
  ["gex", "Net GEX", "$ delta per +1% spot"],
  ["dex", "Net DEX", "δ exposure"],
  ["vanx", "Net vanna", "dδ/dσ"],
  ["cex", "Net charm", "dδ/dt"],
];

function renderTerminal(d) {
  // KPI strip
  const flip = d.levels.gamma_flip?.strike;
  const tiles = KPIS.map(([k, label, sub]) => {
    const v = d.totals[k];
    return `<div class="kpi"><div class="k">${label}</div>
      <div class="v ${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${money(v)}</div>
      <div class="sub">${sub}</div></div>`;
  });
  tiles.push(`<div class="kpi"><div class="k">Gamma flip</div>
    <div class="v">${flip === null || flip === undefined ? "—" : priceFmt(flip)}</div>
    <div class="sub">zero-γ strike${flip === null ? " — none in range" : ""}</div></div>`);
  tiles.push(`<div class="kpi"><div class="k">Open interest</div>
    <div class="v ${d.totals.pc_oi_ratio > 1 ? "neg" : "pos"}">P/C ${px(d.totals.pc_oi_ratio, 2)}</div>
    <div class="sub">${count(d.totals.call_oi)}c · ${count(d.totals.put_oi)}p</div></div>`);
  $("kpi-row").innerHTML = tiles.join("");

  $("price-title").textContent = `${d.instrument} · 1-minute`;

  renderExpTabs();
  renderExposureH(d);
  renderOrderFlow(d);
  renderChartControls();
  renderPriceChart();
  renderInternals();
}

/* ------------------------- candlestick price panel ---------------------- */

/** Range / interval / indicator controls, driven by the server's matrix so
 *  impossible pairs are disabled rather than silently substituted. */
function renderChartControls() {
  const cap = state.meta?.chart;
  if (!cap) return;

  const valid = new Set((cap.ranges.find((r) => r.key === state.range) || {}).intervals || []);

  $("sel-range").innerHTML = cap.ranges
    .map((r) => `<option value="${r.key}" ${r.key === state.range ? "selected" : ""}>${r.key} — ${esc(r.label)}</option>`)
    .join("");
  // Invalid intervals stay listed but disabled, so the constraint is visible
  // rather than the option simply vanishing.
  $("sel-interval").innerHTML = cap.intervals
    .map((i) => `<option value="${i.key}" ${i.key === state.interval ? "selected" : ""} ${valid.has(i.key) ? "" : "disabled"}>${i.key} — ${esc(i.label)}${valid.has(i.key) ? "" : " (n/a)"}</option>`)
    .join("");

  $("ind-seg").querySelectorAll("button").forEach((b) =>
    b.setAttribute("aria-selected", String(!!state.indicators[b.dataset.ind])));

  const notes = state.bars?.plan?.notes || [];
  $("plan-note").textContent = notes.join("  ");
}

/** Visible bar window, clamped. null zoom means fit everything. */
function visibleWindow(n) {
  if (!state.zoom) return [0, n];
  const span = Math.max(state.zoom[1] - state.zoom[0], 5);
  let lo = Math.max(0, Math.round(state.zoom[0]));
  let hi = Math.min(n, Math.round(lo + span));
  if (hi - lo < 5) { lo = Math.max(0, hi - 5); }
  return [lo, hi];
}

function renderPriceChart() {
  const host = $("price-chart");
  const d = state.data, bars = state.bars;
  if (!d) return;

  if (!bars || !bars.available) {
    host.innerHTML = `<div class="nodata">Price history unavailable${
      bars?.reason ? `<br><span class="small">${esc(bars.reason)}</span>` : ""
    }<br><span class="small">Every other panel is unaffected — the exposure surface does not need bars.</span></div>`;
    $("price-ohlc").textContent = "";
    $("chart-readout").hidden = true;
    state.chart = null;
    $("indicator-panes").innerHTML = "";
    return;
  }

  const total = bars.close.length;
  const [lo, hi] = visibleWindow(total);
  const n = hi - lo;

  const W = Math.max(host.clientWidth || 700, 360);
  const H = 442;                      // +30% on the previous 340
  const padL = 8, padR = 66, padT = 8, padB = 18;
  const innerW = W - padL - padR, innerH = H - padT - padB;

  const showLevels = $("chk-levels").checked;
  const fitLevels = $("chk-fit").checked;
  const showVwap = $("chk-vwap").checked;

  // Level annotations are prices, so they share the price axis legitimately.
  const levels = !showLevels ? [] : [
    ["call_wall", "Call Wall", "var(--series-call)", false],
    ["put_wall", "Put Wall", "var(--series-put)", false],
    ["gamma_flip", "Gamma Flip", "var(--warning)", true],
    ["max_pain", "Max Pain", "var(--text-muted)", false],
    ["max_pos_gex", "Max +GEX", "var(--pos)", false],
    ["max_neg_gex", "Max −GEX", "var(--neg)", false],
    ["volume_poc", "Vol POC", "var(--text-secondary)", false],
  ].map(([k, label, colour, dashed]) => ({ label, colour, dashed, price: d.levels[k]?.strike }))
   .filter((l) => l.price !== null && l.price !== undefined);

  const slice = (a) => a.slice(lo, hi);
  const vHigh = slice(bars.high), vLow = slice(bars.low);
  const vOpen = slice(bars.open), vClose = slice(bars.close);
  const vTime = slice(bars.time), vVwap = bars.vwap ? slice(bars.vwap) : null;

  let yLo = Math.min(...vLow), yHi = Math.max(...vHigh);
  const span = Math.max(yHi - yLo, 1e-6);

  if (fitLevels) {
    // Explicit request: show every level even if the candles compress.
    levels.forEach((l) => { yLo = Math.min(yLo, l.price); yHi = Math.max(yHi, l.price); });
  } else {
    // Otherwise absorb only nearby levels; distant ones get pinned to the
    // edge below rather than dropped, so a wall is always indicated.
    const capLo = yLo - span * 0.6, capHi = yHi + span * 0.6;
    levels.forEach((l) => {
      if (l.price >= capLo && l.price <= capHi) { yLo = Math.min(yLo, l.price); yHi = Math.max(yHi, l.price); }
      else l.offscale = l.price > capHi ? "up" : "down";
    });
  }
  const pad = (yHi - yLo) * 0.06 || 1;
  yLo -= pad; yHi += pad;

  const y = (p) => padT + innerH - ((p - yLo) / (yHi - yLo)) * innerH;
  // Reserve empty slots past the last candle. The current bar is the one you
  // look at most, and pinning it to the axis leaves nowhere for its tag and
  // no room to read where price is heading.
  const bw = innerW / (n + RIGHT_PAD_BARS);
  const body = Math.max(1, Math.min(bw * 0.62, 9));
  const x = (i) => padL + i * bw + bw / 2;

  const parts = [];

  // VWAP is a level like any other for labelling purposes, so it has to join
  // the list *before* the tags are laid out -- adding it later meant its tag
  // was placed against no one and sat on whatever was already there.
  if (showVwap && vVwap) {
    levels.push({ label: "VWAP", colour: "var(--series-violet)", price: vVwap[n - 1] });
  }

  // Lay the right-hand tags out first, then suppress axis labels against
  // where the tags actually ended up. Using each level's natural price
  // instead lets a displaced tag land straight on an axis label.
  const TAG_H = 11;           // line height of a level tag
  const LAST_TAG_H = 14;      // height of the last-price tag box
  const TEXT_ASCENT = 9;      // baseline to cap height, at the tag font size
  const lastY = y(vClose[n - 1]);
  let sweepY = -Infinity;
  const tags = levels.filter((l) => !l.offscale)
    .map((l) => ({ ...l, y: y(l.price) }))
    .sort((a, b) => a.y - b.y)
    .map((t) => {
      let ty = Math.max(t.y - 3, sweepY + TAG_H);
      // The last-price tag is pinned to the price and drawn separately, so it
      // takes no part in this sweep -- step over its band rather than letting
      // a tag land on top of it and make both unreadable. Clearance is the
      // tag box's half-height plus the text ascent, not TAG_H: the box is
      // 14px tall, so spacing by line height alone still clips it.
      const boxTop = lastY - LAST_TAG_H / 2, boxBottom = lastY + LAST_TAG_H / 2;
      if (ty > boxTop - 2 && ty < boxBottom + TEXT_ASCENT) ty = boxBottom + TEXT_ASCENT;
      sweepY = ty;
      return { ...t, ty };
    });

  const occupied = tags.map((t) => t.ty - 3);
  occupied.push(lastY);
  for (let g = 0; g <= 5; g++) {
    const p = yLo + ((yHi - yLo) * g) / 5, gy = y(p);
    parts.push(`<line class="grid-line" x1="${padL}" x2="${padL + innerW}" y1="${gy}" y2="${gy}"/>`);
    if (!occupied.some((o) => Math.abs(o - gy) < 13)) {
      parts.push(`<text class="axis-label" x="${padL + innerW + 5}" y="${gy + 3}">${priceFmt(Math.round(p * 100) / 100)}</text>`);
    }
  }

  for (let i = 0; i < n; i++) {
    const up = vClose[i] >= vOpen[i];
    const cls = up ? "candle-up" : "candle-dn";
    const cx = x(i);
    parts.push(`<line class="wick ${cls}" x1="${cx}" x2="${cx}" y1="${y(vHigh[i])}" y2="${y(vLow[i])}"/>`);
    const yo = y(vOpen[i]), yc = y(vClose[i]);
    parts.push(`<rect class="${cls}" x="${cx - body / 2}" y="${Math.min(yo, yc)}" width="${body}" height="${Math.max(Math.abs(yc - yo), 0.8)}"/>`);
  }

  if (showVwap && vVwap) {
    parts.push(`<polyline class="vwap-line" points="${vVwap.map((v, i) => `${x(i)},${y(v)}`).join(" ")}"/>`);
  }

  // In-range levels: the line at the true price, the tag at its laid-out slot.
  tags.forEach((t) => {
    if (t.label !== "VWAP") {
      parts.push(`<line class="level-line${t.dashed ? " flip" : ""}" stroke="${t.colour}" x1="${padL}" x2="${padL + innerW}" y1="${t.y}" y2="${t.y}"/>`);
    }
    parts.push(`<text class="level-tag" fill="${t.colour}" x="${padL + innerW + 5}" y="${t.ty}">${t.label} ${priceFmt(t.price)}</text>`);
  });

  // Off-scale levels pinned to the edge with a direction arrow. "Indicate
  // the walls" has to hold even when a wall is far from the last price.
  let upSlot = 0, downSlot = 0;
  levels.filter((l) => l.offscale).forEach((l) => {
    const up = l.offscale === "up";
    const ty = up ? padT + 9 + upSlot++ * 10 : padT + innerH - 3 - downSlot++ * 10;
    parts.push(`<text class="level-edge" fill="${l.colour}" x="${padL + 4}" y="${ty}">${up ? "▲" : "▼"} ${l.label} ${priceFmt(l.price)}</text>`);
  });

  parts.push(`<g class="last-tag"><rect x="${padL + innerW + 1}" y="${lastY - 7}" width="60" height="14" rx="3"/>
    <text x="${padL + innerW + 5}" y="${lastY + 3.5}">${vClose[n - 1].toFixed(2)}</text></g>`);

  const every = Math.max(1, Math.floor(n / Math.max(4, Math.floor(W / 100))));
  const daily = ["1D", "1W", "1Mo"].includes(state.bars?.plan?.interval_served);
  for (let i = 0; i < n; i += every) {
    const t = String(vTime[i]);
    parts.push(`<text class="axis-label" x="${x(i)}" y="${H - 4}" text-anchor="middle">${daily ? t.slice(0, 10) : t.slice(11, 16)}</text>`);
  }

  parts.push(`<rect class="hit" x="${padL}" y="${padT}" width="${innerW}" height="${innerH}"/>`);

  parts.push(crosshairMarkup(padL, padT, innerW, innerH));

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Price with exposure levels">${parts.join("")}</svg>`;
  const svg = host.querySelector("svg");
  attachZoom(svg, total, padL, innerW);

  // Geometry the crosshair needs, captured once per render rather than
  // recomputed (or re-rendered) on every pointer move.
  state.chart = {
    W, H, padL, padT, padR, innerW, innerH, n, lo, hi,
    yLo, yHi, bw, daily,
    time: vTime, open: vOpen, high: vHigh, low: vLow, close: vClose,
  };
  attachCrosshair(svg);

  $("price-title").textContent =
    `${d.instrument} · ${state.bars?.plan?.interval_served || state.interval} · ${state.range}`;
  // The header keeps the context that does not track the cursor; the per-bar
  // numbers belong to the readout.
  $("price-ohlc").textContent =
    `${n} of ${total} bars` + (bars.synthetic ? "   · synthetic" : "");

  renderIndicatorPanes(lo, hi, W, padL, padR, innerW, bw);
  renderReadout(null);

  // Every refresh rebuilds this SVG from scratch, which would drop the
  // crosshair until the pointer happened to move again -- so while streaming
  // it would blink out every few seconds under a stationary cursor. Redraw it
  // at the remembered position. Must follow renderIndicatorPanes, which
  // rebuilds the pane overlays this reaches into.
  if (state.xhair) drawCrosshair(svg, state.xhair.x, state.xhair.y);
}

/* ------------------------------- crosshair ------------------------------ */

function crosshairMarkup(padL, padT, innerW, innerH) {
  return `<g class="xhair" data-xhair style="display:none">
    <line data-xv y1="${padT}" y2="${padT + innerH}"/>
    <line data-xh x1="${padL}" x2="${padL + innerW}"/>
    <g data-xprice><rect width="60" height="14" rx="3"/><text x="0" y="0"></text></g>
    <g data-xtime><rect width="52" height="13" rx="3"/><text x="0" y="0"></text></g>
  </g>`;
}

/** Index of the bar under a client X, or null when outside the plot. */
function barAtClientX(svg, clientX) {
  const c = state.chart;
  if (!c) return null;
  const r = svg.getBoundingClientRect();
  const px = ((clientX - r.left) / r.width) * c.W;
  const i = Math.floor((px - c.padL) / c.bw);
  return i >= 0 && i < c.n ? i : null;
}

function attachCrosshair(svg) {
  svg.addEventListener("pointermove", (ev) => {
    // Remembered so a re-render can restore it -- see drawCrosshair's caller.
    state.xhair = { x: ev.clientX, y: ev.clientY };
    drawCrosshair(svg, ev.clientX, ev.clientY);
  });
  svg.addEventListener("pointerleave", () => {
    // A re-render replaces this SVG while it is under the cursor, and Chrome
    // fires a leave on the now-detached node. That is not the pointer going
    // away, and honouring it would wipe the crosshair the new render just
    // restored -- hideCrosshair is document-wide, so the stale node's event
    // clears its replacement.
    if (!svg.isConnected) return;
    state.xhair = null;
    hideCrosshair();
  });
}

/** Position the crosshair at a client point. Safe to call outside an event. */
function drawCrosshair(svg, clientX, clientY) {
  if (!$("chk-cross").checked) return hideCrosshair();
  const c = state.chart;
  const i = barAtClientX(svg, clientX);
  if (i === null) return hideCrosshair();

  const r = svg.getBoundingClientRect();
  const py = ((clientY - r.top) / r.height) * c.H;
  const cy = Math.min(Math.max(py, c.padT), c.padT + c.innerH);
  const cx = c.padL + i * c.bw + c.bw / 2;
  const price = c.yHi - ((cy - c.padT) / c.innerH) * (c.yHi - c.yLo);

  const g = svg.querySelector("[data-xhair]");
  g.style.display = "";
  g.querySelector("[data-xv]").setAttribute("x1", cx);
  g.querySelector("[data-xv]").setAttribute("x2", cx);
  g.querySelector("[data-xh]").setAttribute("y1", cy);
  g.querySelector("[data-xh]").setAttribute("y2", cy);

  const pg = g.querySelector("[data-xprice]");
  pg.querySelector("rect").setAttribute("x", c.padL + c.innerW + 1);
  pg.querySelector("rect").setAttribute("y", cy - 7);
  const pt = pg.querySelector("text");
  pt.setAttribute("x", c.padL + c.innerW + 5);
  pt.setAttribute("y", cy + 3.5);
  pt.textContent = price.toFixed(2);

  const tg = g.querySelector("[data-xtime]");
  const stamp = String(c.time[i]);
  const label = c.daily ? stamp.slice(0, 10) : stamp.slice(11, 16);
  const tw = label.length * 6 + 8;
  tg.querySelector("rect").setAttribute("x", Math.min(Math.max(cx - tw / 2, c.padL), c.padL + c.innerW - tw));
  tg.querySelector("rect").setAttribute("y", c.padT + c.innerH + 2);
  tg.querySelector("rect").setAttribute("width", tw);
  const tt = tg.querySelector("text");
  tt.setAttribute("x", Math.min(Math.max(cx, c.padL + tw / 2), c.padL + c.innerW - tw / 2));
  tt.setAttribute("y", c.padT + c.innerH + 11.5);
  tt.setAttribute("text-anchor", "middle");
  tt.textContent = label;

  syncIndicatorCrosshair(cx);

  // Read out the hovered bar, not just the last one.
  renderReadout(i);
}

function hideCrosshair() {
  document.querySelectorAll("[data-xhair]").forEach((g) => (g.style.display = "none"));
  renderReadout(null);
}

/**
 * Bottom-left readout for one bar: null means the most recent.
 *
 * The OHLC lives here rather than in the header so the numbers sit beside the
 * candle they describe, and so the same values are not shown in two places at
 * once -- which reads as two different quotes when only one of them tracks the
 * cursor.
 */
function renderReadout(index) {
  const box = $("chart-readout");
  const c = state.chart;
  if (!box) return;
  // Remembered so toggling the checkbox restores whatever bar was showing.
  state.readoutIndex = index;
  if (!c || !c.n || !$("chk-readout").checked) { box.hidden = true; return; }
  box.hidden = false;

  const i = index == null ? c.n - 1 : index;
  const stamp = String(c.time[i]);
  const when = c.daily ? stamp.slice(0, 10) : `${stamp.slice(0, 10)} ${stamp.slice(11, 16)}`;
  const dir = c.close[i] >= c.open[i] ? "up" : "dn";

  // Indicators are indexed over the whole series; the chart shows a window.
  const ind = state.bars?.indicators || {};
  const at = (key) => {
    const v = ind[key]?.[c.lo + i];
    return v === null || v === undefined || !isFinite(v) ? null : v;
  };
  const rsi = at("rsi"), rsiMa = at("rsi_ma");

  const cell = (k, v, cls = "") =>
    `<span><span class="r-k">${k}</span> <span class="r-v ${cls}">${v}</span></span>`;
  const swatch = (colour) => `<span class="r-dot" style="background:${colour}"></span>`;

  const rows = [
    `<div class="r-row"><span class="r-when">${when}</span></div>`,
    `<div class="r-row">${cell("O", c.open[i].toFixed(2))}${cell("H", c.high[i].toFixed(2))}` +
      `${cell("L", c.low[i].toFixed(2))}${cell("C", c.close[i].toFixed(2), dir)}</div>`,
  ];
  if (rsi !== null) {
    rows.push(
      `<div class="r-row">` +
      `<span>${swatch("var(--series-violet)")}${cell("RSI", rsi.toFixed(1))}</span>` +
      (rsiMa === null ? "" : `<span>${swatch("var(--series-gold)")}${cell("MA20", rsiMa.toFixed(1))}</span>`) +
      `</div>`
    );
  }
  box.innerHTML = rows.join("");
}

/** Same x on every indicator pane, so the panes read as one chart. */
function syncIndicatorCrosshair(cx) {
  document.querySelectorAll("#indicator-panes [data-xhair]").forEach((g) => {
    g.style.display = "";
    g.querySelector("[data-xv]").setAttribute("x1", cx);
    g.querySelector("[data-xv]").setAttribute("x2", cx);
  });
}

/* ------------------------------ zoom / pan ------------------------------ */

function attachZoom(svg, total, padL, innerW) {
  const toIndex = (clientX) => {
    const r = svg.getBoundingClientRect();
    const vb = svg.viewBox.baseVal.width;
    const px = ((clientX - r.left) / r.width) * vb;
    const [lo, hi] = visibleWindow(total);
    return lo + ((px - padL) / innerW) * (hi - lo);
  };

  svg.addEventListener("wheel", (ev) => {
    ev.preventDefault();
    const [lo, hi] = visibleWindow(total);
    const anchor = Math.min(Math.max(toIndex(ev.clientX), lo), hi);
    const factor = ev.deltaY > 0 ? 1.18 : 1 / 1.18;      // out : in
    const newSpan = Math.min(Math.max((hi - lo) * factor, 5), total);
    // Keep whatever sits under the cursor stationary.
    const frac = (anchor - lo) / Math.max(hi - lo, 1);
    let nlo = anchor - frac * newSpan;
    nlo = Math.min(Math.max(nlo, 0), total - newSpan);
    state.zoom = newSpan >= total ? null : [nlo, nlo + newSpan];
    renderPriceChart();
  }, { passive: false });

  let dragging = null;
  svg.addEventListener("pointerdown", (ev) => {
    const [lo, hi] = visibleWindow(total);
    dragging = { x: ev.clientX, lo, hi };
    svg.classList.add("panning");
    svg.setPointerCapture(ev.pointerId);
  });
  svg.addEventListener("pointermove", (ev) => {
    if (!dragging) return;
    const r = svg.getBoundingClientRect();
    const span = dragging.hi - dragging.lo;
    const barsPerPx = span / (innerW * (r.width / svg.viewBox.baseVal.width));
    const shift = -(ev.clientX - dragging.x) * barsPerPx;
    let nlo = Math.min(Math.max(dragging.lo + shift, 0), total - span);
    state.zoom = span >= total ? null : [nlo, nlo + span];
    renderPriceChart();
  });
  const stop = () => { dragging = null; svg.classList.remove("panning"); };
  svg.addEventListener("pointerup", stop);
  svg.addEventListener("pointercancel", stop);
  svg.addEventListener("pointerleave", stop);
}

function zoomBy(factor) {
  const total = state.bars?.close?.length || 0;
  if (!total) return;
  const [lo, hi] = visibleWindow(total);
  const mid = (lo + hi) / 2;
  const span = Math.min(Math.max((hi - lo) * factor, 5), total);
  let nlo = Math.min(Math.max(mid - span / 2, 0), total - span);
  state.zoom = span >= total ? null : [nlo, nlo + span];
  renderPriceChart();
}

/* --------------------------- indicator panes ---------------------------- */

// Heights are +30% on the originals (62/68/62).
/** Empty bar slots kept to the right of the last candle, price and panes alike. */
const RIGHT_PAD_BARS = 10;

const IND_SPECS = [
  { key: "rsi", title: "RSI (14) + MA(20)", height: 101,
    series: [["rsi", "var(--series-violet)"], ["rsi_ma", "var(--series-gold)"]],
    domain: [0, 100], guides: [30, 70] },
  { key: "macd", title: "MACD (12,26,9)", height: 110,
    series: [["macd", "var(--series-call)"], ["macd_signal", "var(--series-put)"]],
    histogram: "macd_histogram", zero: true },
  { key: "adx", title: "ADX / DI (14)", height: 101,
    series: [["adx", "var(--text-primary)"], ["di_plus", "var(--pos)"], ["di_minus", "var(--neg)"]],
    domain: [0, 100], guides: [25] },
];

function renderIndicatorPanes(lo, hi, W, padL, padR, innerW, bw) {
  const host = $("indicator-panes");
  const ind = state.bars?.indicators;
  if (!ind) { host.innerHTML = ""; return; }

  const n = hi - lo;
  // Slot width comes from the price chart rather than being recomputed here:
  // the two must agree exactly or the shared crosshair drifts between panes.
  const x = (i) => padL + i * bw + bw / 2;
  const blocks = [];

  IND_SPECS.filter((spec) => state.indicators[spec.key]).forEach((spec) => {
    const H = spec.height, top = 12, bot = 4;
    const inner = H - top - bot;
    const cols = spec.series.map(([k]) => (ind[k] || []).slice(lo, hi));
    const hist = spec.histogram ? (ind[spec.histogram] || []).slice(lo, hi) : null;

    let vals = cols.flat().filter((v) => v !== null && isFinite(v));
    if (hist) vals = vals.concat(hist.filter((v) => v !== null && isFinite(v)));
    if (!vals.length) return;

    let [dlo, dhi] = spec.domain || [Math.min(...vals), Math.max(...vals)];
    if (!spec.domain) {
      const m = Math.max(Math.abs(dlo), Math.abs(dhi)) || 1;
      [dlo, dhi] = spec.zero ? [-m, m] : [dlo, dhi];
    }
    const y = (v) => top + inner - ((v - dlo) / Math.max(dhi - dlo, 1e-9)) * inner;

    const parts = [];
    (spec.guides || []).forEach((g) =>
      parts.push(`<line class="grid-line" x1="${padL}" x2="${padL + innerW}" y1="${y(g)}" y2="${y(g)}"/>`));
    if (spec.zero) parts.push(`<line class="zero-line" x1="${padL}" x2="${padL + innerW}" y1="${y(0)}" y2="${y(0)}"/>`);

    if (hist) {
      const barW = Math.max(1, bw - 1);
      hist.forEach((v, i) => {
        if (v === null) return;
        const y0 = y(0), yv = y(v);
        parts.push(`<rect class="bar" fill="${v >= 0 ? "var(--pos)" : "var(--neg)"}" x="${x(i) - barW / 2}"
          y="${Math.min(y0, yv)}" width="${barW}" height="${Math.max(Math.abs(yv - y0), 0.6)}"/>`);
      });
    }

    spec.series.forEach(([key, colour], si) => {
      // Break the polyline wherever the indicator has no value yet, rather
      // than drawing a straight line across the warm-up period.
      const pts = cols[si].map((v, i) => (v === null || !isFinite(v)) ? null : `${x(i)},${y(v)}`);
      let run = [];
      pts.forEach((pt) => {
        if (pt) run.push(pt);
        else if (run.length) { parts.push(`<polyline class="ind-line" stroke="${colour}" points="${run.join(" ")}"/>`); run = []; }
      });
      if (run.length) parts.push(`<polyline class="ind-line" stroke="${colour}" points="${run.join(" ")}"/>`);
    });

    const last = spec.series
      .map(([k, c], i) => {
        const v = [...cols[i]].reverse().find((x) => x !== null && isFinite(x));
        const name = k === "rsi_ma" ? "MA20" : k.replace("macd_", "").replace("di_", "DI");
        // The gold MA sits below 3:1 on the light surface, so this label is
        // the documented relief -- it is not optional decoration.
        return v === undefined ? null : `<tspan fill="${c}">${name} ${v.toFixed(1)}</tspan>`;
      })
      .filter(Boolean).join('<tspan fill="var(--text-muted)">  ·  </tspan>');

    parts.push(`<text class="ind-title" x="${padL}" y="9">${spec.title}</text>`);
    parts.push(`<text class="axis-label" x="${padL + innerW + 5}" y="9">${dhi.toFixed(0)}</text>`);
    parts.push(`<text class="axis-label" x="${padL + innerW + 5}" y="${H - 2}">${dlo.toFixed(0)}</text>`);
    // Right-aligned: a fixed left offset collides with the longer titles
    // ("MACD (12,26,9)") at narrow widths.
    parts.push(`<text class="ind-title" x="${padL + innerW - 4}" y="9" text-anchor="end">${last}</text>`);

    parts.push(`<g class="xhair" data-xhair style="display:none"><line data-xv y1="0" y2="${H}"/></g>`);
    blocks.push(`<div class="ind-pane"><svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${spec.title}">${parts.join("")}</svg></div>`);
  });

  host.innerHTML = blocks.join("");
}

/* ------------------- horizontal tabbed exposure by strike --------------- */

function renderExpTabs() {
  $("exp-tabs").innerHTML = Object.entries(METRICS)
    .map(([k, m]) => `<button data-tab="${k}" aria-selected="${state.expTab === k}">${m.label}</button>`)
    .join("");
  $("exp-tabs").querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      state.expTab = b.dataset.tab;
      renderExpTabs();
      renderExposureH(state.data);
    })
  );
}

/** The vertical small multiples rotated: same arrays, same axisTicks, same
 *  colours, same rounded-end bars — only the coordinate mapping differs. */
function renderExposureH(d) {
  if (!d) return;
  const host = $("exp-h-chart");
  const spec = METRICS[state.expTab];
  const idx = visibleIndices(d, 0.06);
  const strikes = idx.map((i) => d.strikes[i]);
  const s = seriesFor(d, spec, idx);

  const W = Math.max(host.clientWidth || 300, 220);
  const H = Math.max(strikes.length * 9, 300);
  const padL = 44, padR = 10, padT = 16, padB = 20;
  const innerW = W - padL - padR, innerH = H - padT - padB;

  const isSigned = !s.grouped;
  const { bound, ticks } = axisTicks(s.maxAbs, isSigned);
  const zeroX = isSigned ? padL + innerW / 2 : padL;
  const scale = (isSigned ? innerW / 2 : innerW) / bound;

  const band = innerH / strikes.length;
  const thick = Math.max(1.5, band - 2);            // 2px surface gap
  const yOf = (i) => padT + (strikes.length - 1 - i) * band + (band - thick) / 2;

  const parts = [];

  ticks.forEach((t) => {
    const tx = zeroX + t * scale;
    parts.push(`<line class="${Math.abs(t) < 1e-9 ? "zero-line" : "grid-line"}" x1="${tx}" x2="${tx}" y1="${padT}" y2="${padT + innerH}"/>`);
    parts.push(`<text class="axis-label" x="${tx}" y="${padT - 5}" text-anchor="middle">${spec.fmt(t)}</text>`);
  });

  if (s.grouped) {
    const half = thick / 2;
    s.a.forEach((v, i) => parts.push(`<path class="bar" fill="var(--series-call)" d="${barPathH(zeroX, yOf(i), v * scale, Math.max(half - 0.5, 0.8), true)}"/>`));
    s.b.forEach((v, i) => parts.push(`<path class="bar" fill="var(--series-put)" d="${barPathH(zeroX, yOf(i) + half + 0.5, v * scale, Math.max(half - 0.5, 0.8), true)}"/>`));
  } else {
    s.v.forEach((v, i) => {
      const len = Math.abs(v) * scale, right = v >= 0;
      parts.push(`<path class="bar" fill="${right ? "var(--pos)" : "var(--neg)"}" d="${barPathH(zeroX, yOf(i), len, thick, right)}"/>`);
    });
  }

  // strike labels, thinned to fit
  const step = Math.max(1, Math.ceil(strikes.length / Math.floor(innerH / 22)));
  strikes.forEach((k, i) => {
    if (i % step) return;
    parts.push(`<text class="axis-label" x="${padL - 6}" y="${yOf(i) + thick / 2 + 3}" text-anchor="end">${priceFmt(k)}</text>`);
  });

  // spot and flip as horizontal reference lines across the strike axis
  const lo = strikes[0], hi = strikes[strikes.length - 1];
  const yAt = (p) => {
    const frac = (p - lo) / Math.max(hi - lo, 1e-9);
    return padT + innerH - frac * (innerH - band) - band / 2;
  };
  [[d.spot, "var(--text-primary)", `Spot ${priceFmt(d.spot)}`, false],
   [d.levels.gamma_flip?.strike, "var(--warning)", "Flip", true]].forEach(([p, colour, label, dashed]) => {
    if (p === null || p === undefined || p < lo || p > hi) return;
    parts.push(`<line class="level-line${dashed ? " flip" : ""}" stroke="${colour}" x1="${padL}" x2="${W - padR}" y1="${yAt(p)}" y2="${yAt(p)}"/>`);
    parts.push(`<text class="level-tag" fill="${colour}" x="${W - padR}" y="${yAt(p) - 3}" text-anchor="end">${label}</text>`);
  });

  host.innerHTML =
    `<div class="chart-head"><span class="chart-title">${spec.title}</span><span class="legend">${legendFor(spec)}</span></div>` +
    `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${spec.title} by strike">${parts.join("")}</svg>` +
    `<div class="muted small">${strikes.length} of ${d.strikes.length} strikes${strikes.length < d.strikes.length ? " (±6%) — full range in table view" : ""}</div>`;
}

/* --------------------------- order flow & bias -------------------------- */

function renderOrderFlow(d) {
  const cv = d.totals.call_volume || 0, pv = d.totals.put_volume || 0;
  $("sv-fill").style.width = `${(cv / Math.max(cv + pv, 1)) * 100}%`;
  $("sv-calls").textContent = count(cv);
  $("sv-puts").textContent = count(pv);
  $("sv-note").textContent =
    `Vol P/C ${px(d.totals.pc_volume_ratio, 2)} — cumulative session volume from the chain snapshot, not a classified tape.`;

  const rows = [];
  const push = (dir, name, val) =>
    rows.push(`<div class="bias-row"><span class="ind ${dir}">${dir === "up" ? "▲" : dir === "dn" ? "▼" : "—"}</span>
      <span class="nm">${name}</span><span class="val">${val}</span></div>`);

  const r = d.regime || {};
  push(r.gamma_sign === "long" ? "up" : "dn", "Gamma regime",
       `${r.label ? r.label.toLowerCase() : "—"} · ${r.position || ""}`);

  const b = state.bars;
  if (b && b.available && b.close.length > 1) {
    const first = b.close[0], last = b.close[b.close.length - 1];
    const chg = ((last - first) / first) * 100;
    push(chg > 0.02 ? "up" : chg < -0.02 ? "dn" : "flat", "Momentum", `${signed(chg, 2)}% over session`);
    const vw = b.vwap[b.vwap.length - 1];
    push(last > vw ? "up" : "dn", "Price vs VWAP", `${signed(last - vw, 2)} vs ${priceFmt(Math.round(vw * 100) / 100)}`);
  } else {
    push("flat", "Momentum", "needs price history");
    push("flat", "Price vs VWAP", "needs price history");
  }

  const vr = d.totals.pc_volume_ratio;
  push(vr < 0.95 ? "up" : vr > 1.05 ? "dn" : "flat", "Volume flow",
       vr < 0.95 ? "call-skewed" : vr > 1.05 ? "put-skewed" : "balanced");

  const or = d.totals.pc_oi_ratio;
  push(or < 0.95 ? "up" : or > 1.05 ? "dn" : "flat", "Positioning", `OI P/C ${px(or, 2)} · prior close`);

  const cw = d.levels.call_wall, pw = d.levels.put_wall;
  if (cw?.strike && pw?.strike) {
    const mid = (cw.strike + pw.strike) / 2;
    push(d.spot > mid ? "up" : "dn", "Within walls",
         `${priceFmt(pw.strike)} – ${priceFmt(cw.strike)}`);
  }

  $("bias-rows").innerHTML = rows.join("");
  renderUnavailable("unavailable-terminal", d, ["movers", "oi_flow", "speed_engine"]);
}

/* --------------------------- market internals -------------------------- */

/** Advance/decline from the exchange internals symbols.
 *
 *  Exchange-wide, not index-constituent: $ADVN/$DECN count every NYSE issue
 *  (~3,000 lines including ETFs and preferreds), not the 503 S&P members. The
 *  heading names the exchange for that reason.
 */
function renderInternals() {
  const b = state.breadth;
  const badge = $("internals-label");
  const host = $("internals-body");

  if (!b || !b.available) {
    badge.textContent = "unavailable";
    badge.className = "badge";
    host.innerHTML = `<div class="nodata" style="min-height:90px">Market internals unavailable${
      b?.reason ? `<br><span class="small">${esc(b.reason)}</span>` : ""}</div>`;
    return;
  }

  badge.textContent = `${b.exchange} · ${b.label}`;
  badge.className = `badge ${b.label === "POSITIVE" ? "live" : b.label === "NEGATIVE" ? "warn" : ""}`;

  const adv = b.advancing ?? 0, dec = b.declining ?? 0;
  const share = (adv / Math.max(adv + dec, 1)) * 100;

  const parts = [`
    <div class="ad-row">
      <div><div class="ad-num adv">${issues(adv)}</div><div class="ad-cap">advancing</div></div>
      <div class="ad-mid"><div class="r">${b.ad_ratio ? b.ad_ratio.toFixed(2) : "—"}</div><div class="ad-cap">A/D ratio</div></div>
      <div style="text-align:right"><div class="ad-num dec">${issues(dec)}</div><div class="ad-cap">declining</div></div>
    </div>
    <div class="adbar" style="margin-top:6px"><div class="adbar-fill" style="width:${share}%"></div></div>
    <div class="pcbar-legend"><span>Net <b>${b.net > 0 ? "+" : ""}${issues(b.net)}</b></span>
      <span class="muted">${issues(b.total_issues)} issues</span></div>`];

  // Session A/D line, accumulated server-side. Point-in-time quotes give the
  // counts; the line has to be sampled as the session runs.
  const hist = (b.history || []).filter((h) => h.net !== null);
  if (hist.length > 2) {
    const W = 240, H = 46;
    const nets = hist.map((h) => h.net);
    const lo = Math.min(...nets, 0), hi = Math.max(...nets, 0);
    const rng = Math.max(hi - lo, 1);
    const x = (i) => (i / (hist.length - 1)) * W;
    const y = (v) => H - 3 - ((v - lo) / rng) * (H - 6);
    const pts = nets.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    parts.push(`<div class="ad-cap" style="margin-top:8px">Session A/D line · ${hist.length} samples</div>
      <svg class="spark" viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Session advance-decline line">
        <line class="spark-zero" x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}"/>
        <polyline class="spark-line${nets[nets.length - 1] < 0 ? " neg" : ""}" points="${pts}"/>
      </svg>`);
  } else {
    parts.push(`<div class="ad-cap" style="margin-top:8px">Session A/D line</div>
      <div class="scope-note">Accumulating — ${hist.length} sample${hist.length === 1 ? "" : "s"} so far.
      The line is built from readings taken through the session, not fetched.</div>`);
  }

  const extra = [];
  if (b.volume_ratio) extra.push(["Up/down volume", b.volume_ratio.toFixed(2)]);
  if (b.tick !== null && b.tick !== undefined) extra.push(["TICK", Math.round(b.tick)]);
  if (b.trin !== null && b.trin !== undefined) extra.push(["TRIN", b.trin.toFixed(2)]);
  if (extra.length) {
    parts.push(`<div class="internals-grid" style="margin-top:8px">` +
      extra.map(([k, v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("") +
      `</div>`);
  }

  if (b.reconciliation_warning) {
    parts.push(`<div class="warn-note" style="margin-top:8px">${esc(b.reconciliation_warning)}</div>`);
  }
  if (b.unresolved && Object.keys(b.unresolved).length) {
    parts.push(`<div class="scope-note" style="margin-top:6px">Unresolved symbols: ${
      Object.entries(b.unresolved).map(([k, v]) => `${k} (${v})`).join(", ")}</div>`);
  }
  parts.push(`<div class="scope-note" style="margin-top:6px">${esc(b.scope)}.${
    b.cached_seconds ? ` Cached ${b.cached_seconds}s.` : ""}</div>`);

  host.innerHTML = parts.join("");
}

/* ═══════════════════════════ COLUMNS VIEW ════════════════════════════ */

const EXPOSURE_TILES = [
  ["gex", "GEX", "$ delta per +1% spot"], ["dex", "DEX", "$ delta"],
  ["cex", "CEX", "$ delta per day"], ["vanx", "VANX", "$ delta per +1 vol pt"],
  ["vex", "VEX", "$ per +1 vol pt"], ["thex", "THEX", "$ per day"],
];

function renderColumns(d) {
  renderBias(d);
  renderSpot(d);
  renderRadar(d);
  renderChartsV(d);
}

function renderBias(d) {
  const r = d.regime || {};
  $("regime").className = `regime ${(r.label || "").toLowerCase()}`;
  $("regime-label").textContent = r.label || "—";
  $("regime-gloss").textContent = r.gloss || "";
  // The reference dashboards' equivalent labels are proprietary and
  // undocumented. This one is ours, and says so on screen.
  $("regime-src").textContent = r.derivation ? `${r.derivation} · ${r.gamma_sign} gamma, ${r.position}` : "";

  $("exposure-tiles").innerHTML = EXPOSURE_TILES.map(([key, label, unit]) => {
    const v = d.totals[key];
    return `<div class="tile" title="${unit}"><div class="k">${label}</div>
      <div class="v ${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${money(v)}</div></div>`;
  }).join("");

  const z = d.diagnostics.zero_dte_gex_share_pct;
  $("t-zerodte").textContent = d.diagnostics.zero_dte_expired ? "0DTE expired"
    : z === null || z === undefined ? "—" : `${z.toFixed(1)}%`;
  $("t-pcoi").textContent = px(d.totals.pc_oi_ratio, 3);
  $("t-pcvol").textContent = px(d.totals.pc_volume_ratio, 3);

  const c = d.totals.call_oi || 0, p = d.totals.put_oi || 0;
  $("pcbar-fill").style.width = `${(c / Math.max(c + p, 1)) * 100}%`;
  $("pc-calls").textContent = count(c);
  $("pc-puts").textContent = count(p);

  renderUnavailable("unavailable-bias", d, ["oi_flow", "volume_bias", "kinetics", "dealer_regime"]);
}

function renderUnavailable(hostId, d, keys) {
  const host = $(hostId);
  if (!host) return;
  host.className = "tiles wide";
  host.innerHTML = keys.filter((k) => d.unavailable?.[k]).map((k) =>
    `<div class="tile unavailable"><div class="v">—</div><div class="body">
      <div class="k">${k.replace(/_/g, " ")}</div>
      <div class="why">${esc(d.unavailable[k])}</div></div></div>`).join("");
}

const GEX_LEVEL_ROWS = [
  ["max_pos_gex", "Max +GEX", "var(--pos)"], ["call_wall", "Call wall", "var(--series-call)"],
  ["gamma_flip", "Flip", "var(--warning)"], ["put_wall", "Put wall", "var(--series-put)"],
  ["max_neg_gex", "Max −GEX", "var(--neg)"],
];

function renderSpot(d) {
  $("spot").textContent = priceFmt(d.spot);
  const im = d.instrument_meta || {};
  $("spot-meta").innerHTML =
    `${esc(im.label || d.instrument)}<br>${d.diagnostics.expiries_in_window} expiries · ${d.diagnostics.expiry_window}<br>${d.diagnostics.pricing_model}`;

  $("gexlevels-body").innerHTML = GEX_LEVEL_ROWS.map(([key, name, colour]) => {
    const lv = d.levels[key];
    if (!lv || lv.strike === null) {
      return `<tr><td><span class="lv"><span class="swatch" style="background:${colour}"></span>${name}</span></td>
              <td class="num muted" colspan="3">none in range</td></tr>`;
    }
    const cls = lv.distance > 0 ? "pos" : lv.distance < 0 ? "neg" : "";
    return `<tr><td><span class="lv"><span class="swatch" style="background:${colour}"></span>${name}</span></td>
      <td class="num muted">${lv.value === null ? "" : money(lv.value)}</td>
      <td class="num">${priceFmt(lv.strike)}</td>
      <td class="num ${cls}">${signed(lv.distance)}</td></tr>`;
  }).join("");

  $("t-maxpain").textContent = priceFmt(d.levels.max_pain?.strike);
  $("maxpain-exp").textContent = d.diagnostics.max_pain_expiry ? `· ${String(d.diagnostics.max_pain_expiry).slice(0, 10)}` : "";

  const cw = d.levels.call_wall, pw = d.levels.put_wall;
  $("t-resist").textContent = signed(cw?.distance);
  $("t-resist-sub").textContent = cw ? `call wall ${priceFmt(cw.strike)} · ${money(cw.value)}` : "";
  $("t-support").textContent = signed(pw?.distance);
  $("t-support-sub").textContent = pw ? `put wall ${priceFmt(pw.strike)} · ${money(pw.value)}` : "";
  $("t-poc").textContent = priceFmt(d.levels.volume_poc?.strike);
}

const RADAR_LEVELS = [
  ["max_pos_gex", "Max +GEX"], ["call_wall", "Call wall"], ["gamma_flip", "Flip"],
  ["volume_poc", "Volume POC"], ["max_pain", "Max pain"], ["put_wall", "Put wall"],
  ["max_neg_gex", "Max −GEX"],
];

function renderRadar(d) {
  const rows = RADAR_LEVELS
    .filter(([k]) => d.levels[k] && d.levels[k].strike !== null)
    .map(([k, name]) => ({ name, price: d.levels[k].strike, dist: d.levels[k].distance, spot: false }));
  rows.push({ name: "Spot", price: d.spot, dist: 0, spot: true });
  rows.sort((a, b) => b.price - a.price);

  $("radar").innerHTML = rows.map((r) => {
    if (r.spot) return `<div class="radar-row is-spot"><span class="nm">Spot</span><span class="px">${priceFmt(r.price)}</span></div>`;
    return `<div class="radar-row ${r.price > d.spot ? "above" : "below"}">
      <span class="nm">${r.name}</span><span class="px">${priceFmt(r.price)}</span>
      <span class="dl ${r.dist > 0 ? "pos" : "neg"}">${signed(r.dist)}</span></div>`;
  }).join("");

  renderUnavailable("unavailable-zone", d, ["zone_state", "speed_engine", "intraday_oi_change", "king_maker"]);
}

/* Vertical small multiples — the columns view's signal chart. */
const V_SPECS = ["gex", "dex", "vanx", "volume"];
const AXIS_BAND = 20, PLOT_H = 86;

function renderChartsV(d) {
  const host = $("charts");
  clearHover();
  host.innerHTML = "";

  const idx = visibleIndices(d);
  const strikes = idx.map((i) => d.strikes[i]);
  const width = Math.max(host.clientWidth || 420, 300);
  const padL = Math.max(40, Math.min(62, width * 0.13)), padR = 8;
  const innerW = width - padL - padR;
  const band = innerW / strikes.length;
  const barW = Math.max(1.2, band - 2);
  const xOf = (i) => padL + i * band + (band - barW) / 2;

  $("chart-legend").innerHTML = `<span class="item muted">${
    strikes.length < d.strikes.length
      ? `${strikes.length} of ${d.strikes.length} strikes (±5%) — full range in table view`
      : `all ${strikes.length} strikes`}</span>`;

  V_SPECS.forEach((key, si) => {
    const spec = METRICS[key];
    const isLast = si === V_SPECS.length - 1;
    const h = PLOT_H + (isLast ? AXIS_BAND : 0);
    const s = seriesFor(d, spec, idx);
    const isSigned = !s.grouped;

    const yTop = 6, yBot = PLOT_H - 6;
    const zeroY = isSigned ? (yTop + yBot) / 2 : yBot;
    const { bound, ticks } = axisTicks(s.maxAbs, isSigned);
    const scale = (isSigned ? yBot - zeroY : yBot - yTop) / bound;

    const parts = [];
    ticks.forEach((t) => {
      const yy = zeroY - t * scale;
      if (yy < yTop - 1 || yy > yBot + 1) return;
      parts.push(`<line class="${Math.abs(t) < 1e-9 ? "zero-line" : "grid-line"}" x1="${padL}" x2="${width - padR}" y1="${yy}" y2="${yy}"/>`);
      parts.push(`<text class="axis-label" x="${padL - 6}" y="${yy + 3}" text-anchor="end">${spec.fmt(t)}</text>`);
    });

    if (isSigned) {
      s.v.forEach((v, i) => {
        const hgt = Math.abs(v) * scale, up = v >= 0;
        parts.push(`<path class="bar" fill="${up ? "var(--pos)" : "var(--neg)"}" d="${barPathV(xOf(i), up ? zeroY - hgt : zeroY, barW, hgt, up)}"/>`);
      });
    } else {
      const half = barW / 2;
      s.a.forEach((v, i) => parts.push(`<path class="bar" fill="var(--series-call)" d="${barPathV(xOf(i), zeroY - v * scale, Math.max(half - 0.5, 0.8), v * scale, true)}"/>`));
      s.b.forEach((v, i) => parts.push(`<path class="bar" fill="var(--series-put)" d="${barPathV(xOf(i) + half + 0.5, zeroY - v * scale, Math.max(half - 0.5, 0.8), v * scale, true)}"/>`));
    }

    const lo = strikes[0], hi = strikes[strikes.length - 1];
    const placed = [
      [d.spot, "var(--text-primary)", "SPOT"],
      [d.levels.gamma_flip?.strike, "var(--warning)", "FLIP"],
      [d.levels.call_wall?.strike, "var(--series-call)", "CW"],
      [d.levels.put_wall?.strike, "var(--series-put)", "PW"],
    ].filter(([v]) => v !== null && v !== undefined && v >= lo && v <= hi)
     .map(([value, colour, label]) => ({ colour, label, x: padL + ((value - lo) / Math.max(hi - lo, 1e-9)) * (innerW - band) + band / 2 }))
     .sort((a, b) => a.x - b.x);

    let lastEnd = -Infinity, row = 0;
    placed.forEach((m) => {
      parts.push(`<line class="marker-line" stroke="${m.colour}" x1="${m.x}" x2="${m.x}" y1="${yTop}" y2="${yBot}"/>`);
      if (si !== 0) return;
      const atEdge = m.x > padL + innerW - 32, w = m.label.length * 6 + 5;
      const startX = atEdge ? m.x - w : m.x;
      row = startX < lastEnd ? row + 1 : 0;
      lastEnd = startX + w;
      parts.push(`<text class="marker-label" fill="${m.colour}" x="${m.x + (atEdge ? -3 : 3)}" y="${yTop + 8 + row * 9}" text-anchor="${atEdge ? "end" : "start"}">${m.label}</text>`);
    });

    if (isLast) {
      const every = Math.max(1, Math.ceil(strikes.length / Math.max(4, Math.floor(width / 78))));
      strikes.forEach((k, i) => {
        if (i % every) return;
        parts.push(`<text class="axis-label" x="${xOf(i) + barW / 2}" y="${PLOT_H + 12}" text-anchor="middle">${priceFmt(k)}</text>`);
      });
    }

    parts.push(`<rect class="hit" x="${padL}" y="${yTop}" width="${innerW}" height="${yBot - yTop}"/>`);
    parts.push(`<rect class="hover-band" x="0" y="${yTop}" width="0" height="${yBot - yTop}" data-hoverband/>`);

    const block = document.createElement("div");
    block.className = "chart-block";
    block.innerHTML = `<div class="chart-head"><span class="chart-title">${spec.title}</span>
      <span class="legend">${legendFor(spec)}</span></div>
      <svg viewBox="0 0 ${width} ${h}" height="${h}" role="img" aria-label="${spec.title} by strike">${parts.join("")}</svg>`;

    const svg = block.querySelector("svg");
    svg.addEventListener("pointermove", (ev) => onHover(ev, svg, width, strikes, idx, padL, band));
    svg.addEventListener("pointerleave", clearHover);
    host.appendChild(block);
  });
}

function onHover(ev, svg, width, strikes, idx, left, band) {
  const rect = svg.getBoundingClientRect();
  const i = Math.floor((((ev.clientX - rect.left) / rect.width) * width - left) / band);
  if (i < 0 || i >= strikes.length) return clearHover();
  document.querySelectorAll("[data-hoverband]").forEach((b) => {
    b.setAttribute("x", left + i * band);
    b.setAttribute("width", band);
  });
  const d = state.data, j = idx[i], tt = $("tooltip");
  tt.innerHTML = `<div class="tt-strike">${priceFmt(strikes[i])}</div>` +
    [["GEX", money(d.gex[j])], ["DEX", money(d.dex[j])], ["VANX", money(d.vanx[j])],
     ["Call OI", count(d.call_oi[j])], ["Put OI", count(d.put_oi[j])],
     ["Call vol", count(d.call_volume[j])], ["Put vol", count(d.put_volume[j])]]
      .map(([k, v]) => `<div class="tt-row"><span>${k}</span><span>${v}</span></div>`).join("");
  tt.classList.add("on");
  tt.style.left = `${Math.min(ev.clientX + 14, window.innerWidth - 200)}px`;
  tt.style.top = `${Math.min(ev.clientY + 14, window.innerHeight - 190)}px`;
}

function clearHover() {
  $("tooltip").classList.remove("on");
  document.querySelectorAll("[data-hoverband]").forEach((b) => b.setAttribute("width", 0));
}

/* ----------------------------------------------------------- table view */

function renderTableView(d) {
  $("tableview-card").hidden = !state.showTable;
  if (!state.showTable) return;
  const head = ["Strike", "GEX", "DEX", "VANX", "CEX", "Call OI", "Put OI", "Call vol", "Put vol"];
  const rows = d.strikes.map((k, i) =>
    `<tr><td>${priceFmt(k)}</td><td>${money(d.gex[i])}</td><td>${money(d.dex[i])}</td><td>${money(d.vanx[i])}</td>
     <td>${money(d.cex[i])}</td><td>${count(d.call_oi[i])}</td><td>${count(d.put_oi[i])}</td>
     <td>${count(d.call_volume[i])}</td><td>${count(d.put_volume[i])}</td></tr>`);
  $("tableview").innerHTML =
    `<table><thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

/* ---------------------------------------------------------- diagnostics */

function renderDiagnostics(d) {
  const g = d.diagnostics;
  const items = [
    ["Instrument", `${d.instrument} · ${d.instrument_meta?.label ?? ""}`],
    ["Expiry window", `${g.expiry_window} (${g.expiry_window_days ?? "no cap"} days)`],
    ["Contracts", `${g.contracts_in_window} of ${g.contracts_in_chain} · ${g.expiries_in_window} expiries`],
    ["DTE range", `${g.min_dte.toFixed(2)} – ${g.max_dte.toFixed(1)}`],
    ["Expired dropped", g.expired_excluded],
    ["Dealer assumption", g.dealer_sign],
    ["Flip method", g.flip_method],
    ["Pricing model", `${g.pricing_model} · ×${g.multiplier}`],
    ["Exercise", g.exercise_style],
    ["Rate / dividend", `${(g.risk_free_rate * 100).toFixed(2)}% / ${(g.dividend_yield * 100).toFixed(2)}% (${g.dividend_yield_source})`],
    ["OI as of", g.oi_asof || "unknown"],
    ["Max pain expiry", g.max_pain_expiry ? String(g.max_pain_expiry).slice(0, 10) : "—"],
    ["Chain fetched", d.from_cache ? `cache (${d.cache_age_hours}h old)${d.stale ? " — STALE" : ""}` : "live"],
    ["Price history", state.bars?.available ? `${state.bars.close.length} × ${state.bars.interval_minutes}m` : "unavailable"],
    ["Market internals", state.breadth?.available
      ? `${state.breadth.exchange} exchange-wide (not index constituents)` : "unavailable"],
  ];
  if (g.greeks_approximation) items.push(["Approximation", g.greeks_approximation]);
  $("diag").innerHTML = items.map(([k, v]) => `<div><span>${k}:</span> ${esc(v)}</div>`).join("");
}

/* --------------------------------------------------------- view switch */

function setView(view, { reload = true } = {}) {
  state.view = view === "columns" ? "columns" : "terminal";
  localStorage.setItem("gexdash-view", state.view);

  $("view-terminal").hidden = state.view !== "terminal";
  $("view-columns").hidden = state.view !== "columns";
  ["terminal", "columns"].forEach((v) =>
    $(`view-${v}-btn`).setAttribute("aria-selected", String(state.view === v)));

  // A hidden container reports clientWidth 0, so every chart laid out while
  // the view was hidden has degenerate geometry. Switching must re-render,
  // not merely unhide.
  if (reload && state.data) render(state.data);
  if (state.view === "terminal") {
    if (!state.bars) loadBars(); else renderPriceChart();
    if (!state.breadth) loadBreadth(); else renderInternals();
  }
}

/* --------------------------------------------------------------- events */

["f-window", "f-sign", "f-flip"].forEach((id) =>
  $(id).addEventListener("change", () => (state.stream ? startStream() : load())));

$("f-symbol").addEventListener("change", () => {
  state.bars = null;                     // bars are per-instrument
  state.breadth = null;                  // and the exchange can change with it
  if (state.stream) startStream(); else load();
  if (state.view === "terminal") { loadBars(); loadBreadth(); }
});

["view-terminal-btn", "view-columns-btn"].forEach((id) =>
  $(id).addEventListener("click", () => setView($(id).dataset.view)));

["chk-levels", "chk-vwap", "chk-fit"].forEach((id) =>
  $(id).addEventListener("change", renderPriceChart));
$("chk-cross").addEventListener("change", () => { if (!$("chk-cross").checked) hideCrosshair(); });
// Redrawn in place rather than by re-rendering the chart: the readout is an
// overlay, and a full render would drop the hover the user is reading from.
$("chk-readout").addEventListener("change", () => renderReadout(state.readoutIndex));

$("sel-range").addEventListener("change", (e) => {
  state.range = e.target.value;
  const cap = state.meta?.chart;
  const now = new Set((cap.ranges.find((r) => r.key === state.range) || {}).intervals || []);
  // Coarsen rather than let the server clamp behind the user's back -- but
  // only to the finest size the new range allows. Taking the last entry
  // picked the coarsest, so stepping 1D/1m up to 3M landed on Weekly when
  // Daily is the nearer equivalent of what was on screen.
  if (!now.has(state.interval)) state.interval = [...now][0] || "1D";
  loadBars();
});
$("sel-interval").addEventListener("change", (e) => { state.interval = e.target.value; loadBars(); });
$("ind-seg").querySelectorAll("button").forEach((b) =>
  b.addEventListener("click", () => {
    state.indicators[b.dataset.ind] = !state.indicators[b.dataset.ind];
    renderChartControls(); renderPriceChart();
  }));

$("zoom-in").addEventListener("click", () => zoomBy(1 / 1.5));
$("zoom-out").addEventListener("click", () => zoomBy(1.5));
$("zoom-reset").addEventListener("click", () => { state.zoom = null; renderPriceChart(); });

$("btn-table").addEventListener("click", () => {
  state.showTable = !state.showTable;
  $("btn-table").textContent = state.showTable ? "▤ hide" : "▤ table";
  if (state.data) renderTableView(state.data);
});

$("btn-refresh").addEventListener("click", async () => {
  $("btn-refresh").disabled = true;
  try {
    const res = await fetch(`/api/refresh?symbol=${$("f-symbol").value}`, { method: "POST" });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    notice(`Chain refetched: ${body.contracts} contracts, OI as of ${body.oi_asof}.`);
    await load();
  } catch (err) { notice(err.message, true); }
  finally { $("btn-refresh").disabled = false; }
});

function startStream() {
  stopStream();
  const es = new EventSource(`/api/stream?${params()}&interval=2`);
  es.onmessage = (ev) => {
    const body = JSON.parse(ev.data);
    if (body.error) return notice(body.error, true);
    render(body);   // dispatches to whichever view is mounted
  };
  es.onerror = () => notice("stream disconnected", true);
  state.stream = es;
  $("btn-live").textContent = "⏸ pause";
  $("badge-live").textContent = "streaming";
  $("badge-live").className = "badge live";
  // Bars move far more slowly than the exposure surface; refreshing them on
  // every 2s tick would be waste.
  clearInterval(state.barTimer);
  state.barTimer = setInterval(() => {
    if (state.view !== "terminal") return;
    loadBars();
    loadBreadth();   // server-side 60s TTL means this is cheap regardless
  }, 30_000);
}

function stopStream() {
  if (state.stream) state.stream.close();
  state.stream = null;
  clearInterval(state.barTimer);
  $("btn-live").textContent = "▶ start";
  $("badge-live").textContent = "paused";
  $("badge-live").className = "badge";
}

$("btn-live").addEventListener("click", () => (state.stream ? stopStream() : startStream()));

$("btn-theme").addEventListener("click", () => {
  const root = document.documentElement;
  const isDark = root.dataset.theme
    ? root.dataset.theme === "dark"
    : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = isDark ? "light" : "dark";
  localStorage.setItem("gexdash-theme", root.dataset.theme);
  if (state.data) render(state.data);
});

/* Theme and view both resolve: URL parameter → stored preference → default.
 * The URL form makes a specific screen linkable and screenshot-testable. */
const urlParams = new URLSearchParams(location.search);
const savedTheme = urlParams.get("theme") || localStorage.getItem("gexdash-theme");
if (savedTheme === "dark" || savedTheme === "light") document.documentElement.dataset.theme = savedTheme;

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => state.data && render(state.data), 150);
});

setView(urlParams.get("view") || localStorage.getItem("gexdash-view") || "terminal", { reload: false });
loadMeta().then(() => Promise.all([load(), loadBars(), loadBreadth()])).catch((e) => notice(e.message, true));
