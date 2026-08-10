/* gexdash client.
 *
 * Charts are hand-rolled SVG: no chart library, no CDN, no build step.
 *
 * Two structural choices worth naming, both departures from the commercial
 * dashboard this layout is modelled on:
 *
 *  1. NO DUAL AXES. The reference stacks two measures with different scales
 *     on one plot ("VOL / GEX", "GEX / VANX"). The alignment of two y-scales
 *     is arbitrary, so it invents a correlation the data does not contain.
 *     Here each measure is its own small multiple over a shared strike axis,
 *     which supports the same visual comparison honestly.
 *
 *  2. Calls/puts are BLUE/ORANGE, not green/red. Green-vs-red is the worst
 *     pair under colour-vision deficiency, and this is the one distinction on
 *     the page that must never be ambiguous.
 */

const $ = (id) => document.getElementById(id);

const state = { data: null, meta: null, stream: null, showTable: false };

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

/* Show decimals only when the value has them. Listed strikes are whole
   numbers and rendering them as "7625.00" is pure noise, but spot and the
   solved flip are genuinely fractional and must keep their precision. */
function priceFmt(v) {
  if (v === null || v === undefined) return "—";
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

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
      const opts = keys
        .map((k) => {
          const i = meta.instruments.find((x) => x.key === k);
          return `<option value="${k}">${k} — ${i.label}</option>`;
        })
        .join("");
      return `<optgroup label="${family}">${opts}</optgroup>`;
    })
    .join("");
  // ?symbol= makes a given ticker linkable and bookmarkable.
  const wanted = (new URLSearchParams(location.search).get("symbol") || "SPX").toUpperCase();
  sel.value = meta.instruments.some((i) => i.key === wanted) ? wanted : "SPX";
}

async function load() {
  $("main").classList.add("stale");
  try {
    const res = await fetch(`/api/exposure?${params()}`);
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    render(body);
    notice(null);
  } catch (err) {
    notice(err.message, true);
  } finally {
    $("main").classList.remove("stale");
  }
}

/* Errors reuse a single node rather than prepending. Under SSE this fires on
 * every failed tick, and EventSource retries on a timer — prepending would
 * grow an unbounded stack of identical divs within a minute. */
function notice(msg, isError) {
  const box = $("notices");
  if (!msg) {
    box.querySelectorAll(".err").forEach((n) => n.remove());
    return;
  }
  const cls = isError ? "err" : "info";
  let el = box.querySelector(`.notice.${cls}`);
  if (!el) {
    el = document.createElement("div");
    el.className = `notice ${cls}`;
    box.prepend(el);
  }
  const n = isError ? Number(el.dataset.count || 0) + 1 : 0;
  el.dataset.count = n;
  el.innerHTML =
    `<strong>${isError ? "Error" : "Note"}:</strong> ${msg}` +
    (n > 1 ? ` <span class="muted">(×${n})</span>` : "");
}

/* --------------------------------------------------------------- render */

function render(d) {
  state.data = d;
  renderHeader(d);
  renderStrip(d);
  renderBias(d);
  renderSpot(d);
  renderRadar(d);
  renderCharts(d);
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
  const h = Math.floor(secs / 3600), m = Math.floor((secs % 3600) / 60);
  $("clock-next").textContent = s.next_transition ? `→ ${s.next_transition} in ${h}h ${m}m` : "";

  $("badge-provider").textContent = `provider: ${d.provider}`;
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
    ? `<span style="color:var(--serious)">⚠ ${im.note}</span>`
    : `${im.exercise_style} · ×${im.multiplier}`;
  $("strip-oi").innerHTML =
    `OI as of <b>${d.diagnostics.oi_asof || "unknown"}</b> — OCC publishes once daily; ` +
    `re-priced against live spot` + (d.stale ? ' · <span style="color:var(--critical)">STALE CHAIN</span>' : "");
}

const EXPOSURE_TILES = [
  ["gex", "GEX", "$ delta per +1% spot"],
  ["dex", "DEX", "$ delta"],
  ["cex", "CEX", "$ delta per day"],
  ["vanx", "VANX", "$ delta per +1 vol pt"],
  ["vex", "VEX", "$ per +1 vol pt"],
  ["thex", "THEX", "$ per day"],
];

function renderBias(d) {
  const r = d.regime || {};
  const box = $("regime");
  box.className = `regime ${(r.label || "").toLowerCase()}`;
  $("regime-label").textContent = r.label || "—";
  $("regime-gloss").textContent = r.gloss || "";
  // The reference dashboard's equivalent labels are proprietary and
  // undocumented. This one is ours, and says so on screen rather than only
  // in a code comment.
  $("regime-src").textContent = r.derivation
    ? `${r.derivation} · ${r.gamma_sign} gamma, ${r.position}`
    : "";

  $("exposure-tiles").innerHTML = EXPOSURE_TILES.map(([key, label, unit]) => {
    const v = d.totals[key];
    const cls = v > 0 ? "pos" : v < 0 ? "neg" : "";
    return `<div class="tile" title="${unit}"><div class="k">${label}</div><div class="v ${cls}">${money(v)}</div></div>`;
  }).join("");

  const z = d.diagnostics.zero_dte_gex_share_pct;
  $("t-zerodte").textContent = d.diagnostics.zero_dte_expired
    ? "0DTE expired"
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
  $(hostId).innerHTML = keys
    .filter((k) => d.unavailable?.[k])
    .map(
      (k) => `<div class="tile unavailable"><div class="v">—</div><div class="body">
        <div class="k">${k.replace(/_/g, " ")}</div>
        <div class="why">${d.unavailable[k]}</div></div></div>`
    )
    .join("");
}

const GEX_LEVEL_ROWS = [
  ["max_pos_gex", "Max +GEX", "var(--pos)"],
  ["call_wall", "Call wall", "var(--series-call)"],
  ["gamma_flip", "Flip", "var(--warning)"],
  ["put_wall", "Put wall", "var(--series-put)"],
  ["max_neg_gex", "Max −GEX", "var(--neg)"],
];

function renderSpot(d) {
  $("spot").textContent = priceFmt(d.spot);
  const im = d.instrument_meta || {};
  $("spot-meta").innerHTML =
    `${im.label || d.instrument}<br>${d.diagnostics.expiries_in_window} expiries · ` +
    `${d.diagnostics.expiry_window}<br>${d.diagnostics.pricing_model}`;

  $("gexlevels-body").innerHTML = GEX_LEVEL_ROWS.map(([key, name, colour]) => {
    const lv = d.levels[key];
    if (!lv || lv.strike === null) {
      return `<tr><td><span class="lv"><span class="swatch" style="background:${colour}"></span>${name}</span></td>
              <td class="num muted" colspan="3">none in range</td></tr>`;
    }
    const cls = lv.distance > 0 ? "pos" : lv.distance < 0 ? "neg" : "";
    return `<tr>
      <td><span class="lv"><span class="swatch" style="background:${colour}"></span>${name}</span></td>
      <td class="num muted">${lv.value === null ? "" : money(lv.value)}</td>
      <td class="num">${priceFmt(lv.strike)}</td>
      <td class="num ${cls}">${signed(lv.distance)}</td>
    </tr>`;
  }).join("");

  $("t-maxpain").textContent = priceFmt(d.levels.max_pain?.strike);
  $("maxpain-exp").textContent = d.diagnostics.max_pain_expiry
    ? `· ${String(d.diagnostics.max_pain_expiry).slice(0, 10)}`
    : "";

  const cw = d.levels.call_wall, pw = d.levels.put_wall;
  $("t-resist").textContent = signed(cw?.distance);
  $("t-resist-sub").textContent = cw ? `call wall ${priceFmt(cw.strike)} · ${money(cw.value)}` : "";
  $("t-support").textContent = signed(pw?.distance);
  $("t-support-sub").textContent = pw ? `put wall ${priceFmt(pw.strike)} · ${money(pw.value)}` : "";
  $("t-poc").textContent = priceFmt(d.levels.volume_poc?.strike);
}

/* --------------------------------------------------- active level radar */

const RADAR_LEVELS = [
  ["max_pos_gex", "Max +GEX"],
  ["call_wall", "Call wall"],
  ["gamma_flip", "Flip"],
  ["volume_poc", "Volume POC"],
  ["max_pain", "Max pain"],
  ["put_wall", "Put wall"],
  ["max_neg_gex", "Max −GEX"],
];

/* Levels sorted by price with spot in its sorted position, so the panel reads
 * as a price ladder: what is above, what is below, and how far. Every input
 * is tier A. */
function renderRadar(d) {
  const rows = RADAR_LEVELS.filter(([k]) => d.levels[k]?.strike !== null && d.levels[k] !== undefined).map(
    ([k, name]) => ({ name, price: d.levels[k].strike, dist: d.levels[k].distance, spot: false })
  );
  rows.push({ name: "Spot", price: d.spot, dist: 0, spot: true });
  rows.sort((a, b) => b.price - a.price);

  $("radar").innerHTML = rows
    .map((r) => {
      if (r.spot) {
        return `<div class="radar-row is-spot">
          <span class="nm">Spot</span><span class="px">${priceFmt(r.price)}</span></div>`;
      }
      const side = r.price > d.spot ? "above" : "below";
      const cls = r.dist > 0 ? "pos" : "neg";
      return `<div class="radar-row ${side}">
        <span class="nm">${r.name}</span>
        <span class="px">${priceFmt(r.price)}</span>
        <span class="dl ${cls}">${signed(r.dist)}</span></div>`;
    })
    .join("");

  renderUnavailable("unavailable-zone", d, ["zone_state", "speed_engine", "intraday_oi_change", "king_maker"]);
}

/* --------------------------------------------------------------- charts */

const AXIS_BAND = 20;
const PLOT_H = 86;

function barPath(x, y, w, h, up) {
  const r = Math.min(3, w / 2, Math.abs(h));
  if (h <= 0.5) return `M${x},${y}h${w}`;
  return up
    ? `M${x},${y + h} v${-(h - r)} a${r},${r} 0 0 1 ${r},${-r} h${w - 2 * r} a${r},${r} 0 0 1 ${r},${r} v${h - r} z`
    : `M${x},${y} v${h - r} a${r},${r} 0 0 0 ${r},${r} h${w - 2 * r} a${r},${r} 0 0 0 ${r},${-r} v${-(h - r)} z`;
}

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

/* Strikes shown in the plots. Never a silent cap — the count is displayed. */
function visibleIndices(d, pct = 0.05) {
  const lo = d.spot * (1 - pct), hi = d.spot * (1 + pct);
  const idx = d.strikes.map((k, i) => [k, i]).filter(([k]) => k >= lo && k <= hi).map(([, i]) => i);
  return idx.length >= 5 ? idx : d.strikes.map((_, i) => i);
}

const CHART_SPECS = [
  { key: "gex", title: "Gamma exposure (GEX)", fmt: money, kind: "diverging" },
  { key: "dex", title: "Delta exposure (DEX)", fmt: money, kind: "diverging" },
  { key: "vanx", title: "Vanna exposure (VANX)", fmt: money, kind: "diverging" },
  { key: "volume", title: "Contract volume", fmt: count, kind: "grouped" },
];

function renderCharts(d) {
  const host = $("charts");
  // Rebuilding the SVGs drops the hover band with them. Under SSE that would
  // leave a tooltip pinned open showing values from a tick ago.
  clearHover();
  host.innerHTML = "";

  const idx = visibleIndices(d);
  const strikes = idx.map((i) => d.strikes[i]);
  const width = Math.max(host.clientWidth || 420, 300);
  // The chart column is narrow; a fixed 62px gutter would eat a fifth of it.
  const padL = Math.max(40, Math.min(62, width * 0.13));
  const padR = 8;
  const innerW = width - padL - padR;
  const band = innerW / strikes.length;
  const barW = Math.max(1.2, band - 2); // 2px surface gap between bars
  const xOf = (i) => padL + i * band + (band - barW) / 2;

  $("chart-legend").innerHTML = `<span class="item muted">${
    strikes.length < d.strikes.length
      ? `${strikes.length} of ${d.strikes.length} strikes (±5%) — full range in table view`
      : `all ${strikes.length} strikes`
  }</span>`;

  CHART_SPECS.forEach((spec, si) => {
    const isLast = si === CHART_SPECS.length - 1;
    const h = PLOT_H + (isLast ? AXIS_BAND : 0);
    const isSigned = spec.kind !== "grouped";

    let series, maxAbs;
    if (isSigned) {
      series = idx.map((i) => d[spec.key][i]);
      maxAbs = Math.max(...series.map(Math.abs), 1);
    } else {
      const c = idx.map((i) => d.call_volume[i]), p = idx.map((i) => d.put_volume[i]);
      series = { c, p };
      maxAbs = Math.max(...c, ...p, 1);
    }

    const yTop = 6, yBot = PLOT_H - 6;
    const zeroY = isSigned ? (yTop + yBot) / 2 : yBot;
    const { bound, ticks } = axisTicks(maxAbs, isSigned);
    const scale = (isSigned ? yBot - zeroY : yBot - yTop) / bound;

    const parts = [];
    ticks.forEach((t) => {
      const y = zeroY - t * scale;
      if (y < yTop - 1 || y > yBot + 1) return;
      parts.push(`<line class="${Math.abs(t) < 1e-9 ? "zero-line" : "grid-line"}" x1="${padL}" x2="${width - padR}" y1="${y}" y2="${y}"/>`);
      parts.push(`<text class="axis-label" x="${padL - 6}" y="${y + 3}" text-anchor="end">${spec.fmt(t)}</text>`);
    });

    if (isSigned) {
      series.forEach((v, i) => {
        const hgt = Math.abs(v) * scale, up = v >= 0;
        parts.push(`<path class="bar" fill="${up ? "var(--pos)" : "var(--neg)"}" d="${barPath(xOf(i), up ? zeroY - hgt : zeroY, barW, hgt, up)}"/>`);
      });
    } else {
      const half = barW / 2;
      series.c.forEach((v, i) => {
        const hgt = v * scale;
        parts.push(`<path class="bar" fill="var(--series-call)" d="${barPath(xOf(i), zeroY - hgt, Math.max(half - 0.5, 0.8), hgt, true)}"/>`);
      });
      series.p.forEach((v, i) => {
        const hgt = v * scale;
        parts.push(`<path class="bar" fill="var(--series-put)" d="${barPath(xOf(i) + half + 0.5, zeroY - hgt, Math.max(half - 0.5, 0.8), hgt, true)}"/>`);
      });
    }

    // Level markers — solid hairlines, labelled on the first chart only.
    const lo = strikes[0], hi = strikes[strikes.length - 1];
    const placed = [
      [d.spot, "var(--text-primary)", "SPOT"],
      [d.levels.gamma_flip?.strike, "var(--warning)", "FLIP"],
      [d.levels.call_wall?.strike, "var(--series-call)", "CW"],
      [d.levels.put_wall?.strike, "var(--series-put)", "PW"],
    ]
      .filter(([v]) => v !== null && v !== undefined && v >= lo && v <= hi)
      .map(([value, colour, label]) => ({
        colour, label,
        x: padL + ((value - lo) / Math.max(hi - lo, 1e-9)) * (innerW - band) + band / 2,
      }))
      .sort((a, b) => a.x - b.x);

    // Stack onto a new row when the previous label's rendered extent would be
    // overlapped — comparing line positions alone is not enough.
    let lastEnd = -Infinity, row = 0;
    placed.forEach((m) => {
      parts.push(`<line class="marker-line" stroke="${m.colour}" x1="${m.x}" x2="${m.x}" y1="${yTop}" y2="${yBot}"/>`);
      if (si !== 0) return;
      const atEdge = m.x > padL + innerW - 32;
      const w = m.label.length * 6 + 5;
      const startX = atEdge ? m.x - w : m.x;
      row = startX < lastEnd ? row + 1 : 0;
      lastEnd = startX + w;
      parts.push(
        `<text class="marker-label" fill="${m.colour}" x="${m.x + (atEdge ? -3 : 3)}" y="${yTop + 8 + row * 9}" text-anchor="${atEdge ? "end" : "start"}">${m.label}</text>`
      );
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

    const legend = isSigned
      ? `<span class="item"><span class="swatch" style="background:var(--pos)"></span>+</span>
         <span class="item"><span class="swatch" style="background:var(--neg)"></span>−</span>`
      : `<span class="item"><span class="swatch" style="background:var(--series-call)"></span>calls</span>
         <span class="item"><span class="swatch" style="background:var(--series-put)"></span>puts</span>`;

    const block = document.createElement("div");
    block.className = "chart-block";
    block.innerHTML = `
      <div class="chart-head"><span class="chart-title">${spec.title}</span><span class="legend">${legend}</span></div>
      <svg viewBox="0 0 ${width} ${h}" height="${h}" role="img" aria-label="${spec.title} by strike">${parts.join("")}</svg>`;

    const svg = block.querySelector("svg");
    svg.addEventListener("pointermove", (ev) => onHover(ev, svg, width, strikes, idx, padL, band));
    svg.addEventListener("pointerleave", clearHover);
    host.appendChild(block);
  });
}

function onHover(ev, svg, width, strikes, idx, left, band) {
  const rect = svg.getBoundingClientRect();
  const xInSvg = ((ev.clientX - rect.left) / rect.width) * width;
  const i = Math.floor((xInSvg - left) / band);
  if (i < 0 || i >= strikes.length) return clearHover();

  document.querySelectorAll("[data-hoverband]").forEach((b) => {
    b.setAttribute("x", left + i * band);
    b.setAttribute("width", band);
  });

  const d = state.data, j = idx[i];
  const tt = $("tooltip");
  tt.innerHTML =
    `<div class="tt-strike">${priceFmt(strikes[i])}</div>` +
    [
      ["GEX", money(d.gex[j])], ["DEX", money(d.dex[j])], ["VANX", money(d.vanx[j])],
      ["Call OI", count(d.call_oi[j])], ["Put OI", count(d.put_oi[j])],
      ["Call vol", count(d.call_volume[j])], ["Put vol", count(d.put_volume[j])],
    ].map(([k, v]) => `<div class="tt-row"><span>${k}</span><span>${v}</span></div>`).join("");
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
     <td>${count(d.call_volume[i])}</td><td>${count(d.put_volume[i])}</td></tr>`
  );
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
  ];
  if (g.greeks_approximation) items.push(["Approximation", g.greeks_approximation]);
  $("diag").innerHTML = items.map(([k, v]) => `<div><span>${k}:</span> ${v}</div>`).join("");
}

/* --------------------------------------------------------------- events */

["f-symbol", "f-window", "f-sign", "f-flip"].forEach((id) =>
  $(id).addEventListener("change", () => (state.stream ? startStream() : load()))
);

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
  } catch (err) {
    notice(err.message, true);
  } finally {
    $("btn-refresh").disabled = false;
  }
});

function startStream() {
  stopStream();
  const es = new EventSource(`/api/stream?${params()}&interval=2`);
  es.onmessage = (ev) => {
    const body = JSON.parse(ev.data);
    if (body.error) return notice(body.error, true);
    render(body);
  };
  es.onerror = () => notice("stream disconnected", true);
  state.stream = es;
  $("btn-live").textContent = "⏸ pause";
  $("badge-live").textContent = "streaming";
  $("badge-live").className = "badge live";
}

function stopStream() {
  if (state.stream) state.stream.close();
  state.stream = null;
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
  if (state.data) renderCharts(state.data);
});

/* Theme resolution order: ?theme= in the URL, then the stored preference,
 * then the OS setting via prefers-color-scheme (no attribute set at all).
 * The URL form makes a specific mode linkable and screenshot-testable. */
const urlTheme = new URLSearchParams(location.search).get("theme");
const savedTheme = urlTheme || localStorage.getItem("gexdash-theme");
if (savedTheme === "dark" || savedTheme === "light") {
  document.documentElement.dataset.theme = savedTheme;
}

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => state.data && renderCharts(state.data), 150);
});

loadMeta().then(load).catch((e) => notice(e.message, true));
