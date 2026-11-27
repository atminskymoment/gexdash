/* gexdash lunar-solar layer: the Sun and the Moon on both price charts.
 *
 * Draws the `astro` block that /api/technicals (the Chart view) and
 * /api/history (the Terminal chart) add when asked with astro=true, computed
 * by cycles.py on the server: the lunar-solar cycle (LSC) and its 1st and 2nd
 * derivatives in a pane of their own, solid over the bars and dotted past the
 * last one; the projected price path with its highs and lows labelled; and
 * new, quarter and full moons, perigees, apogees, equinoxes and solstices
 * along the top of the pane. Every number comes from the server; this file
 * only lays it out, so the two charts cannot draw it differently.
 *
 * Slots. A chart's bars are slots 0 .. total-1 and the block's future entries
 * follow as total, total+1, ... Both charts map a slot to x the same way
 * whether it holds a bar or a future date. Future slots are never appended to
 * the OHLC arrays: every other layer reads those as real bars.
 *
 * The payload: `series` (per bar) and `future` (per future slot, plus `time`
 * and `path`) each hold `lsc`, `d1`, `d2` (scaled to ±1), `illum` (0-1),
 * `elong` (phase, degrees), `dist` (km), `dec` (degrees); `turns` ({i, type,
 * price, date}, i into `future`); `events` ({type, label, time, slot});
 * `scale`, `model`, `notes`, `rules`. See cycles.py.
 *
 * Load order: after tz.js, before technicals.js and app.js. Borrows `esc` from
 * app.js at call time only. Every top-level name carries an astro/ASTRO_
 * prefix, because classic scripts share one global scope.
 */

/** The pane's three lines. Colour tokens are in styles.css, validated as one
 *  set against both surfaces; the weights differ too (2 / 1.5 / 1px). */
const ASTRO_LINES = [
  { key: "lsc", label: "LSC", colour: "var(--astro-lsc)" },
  { key: "d1", label: "1st deriv", colour: "var(--astro-d1)" },
  { key: "d2", label: "2nd deriv", colour: "var(--astro-d2)" },
];

/** Which events get an icon, in which row, and which win a crowded slot. */
const ASTRO_EVENTS = {
  new_moon: { row: 0, rank: 0 },
  full_moon: { row: 0, rank: 0 },
  first_quarter: { row: 0, rank: 2 },
  last_quarter: { row: 0, rank: 2 },
  march_equinox: { row: 1, rank: 1, glyph: "☉" },
  june_solstice: { row: 1, rank: 1, glyph: "☉" },
  september_equinox: { row: 1, rank: 1, glyph: "☉" },
  december_solstice: { row: 1, rank: 1, glyph: "☉" },
  perigee: { row: 1, rank: 3, glyph: "P" },
  apogee: { row: 1, rank: 3, glyph: "A" },
};

/** Height of the icon strip at the top of the pane, above the plot. */
const ASTRO_STRIP_H = 24;

const ASTRO_PHASES = ["New moon", "Waxing crescent", "First quarter", "Waxing gibbous",
  "Full moon", "Waning gibbous", "Last quarter", "Waning crescent"];

const ASTRO_DAY_FMT = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const ASTRO_MONTH_FMT = new Intl.DateTimeFormat("en-US", { month: "short", year: "numeric", timeZone: "UTC" });

/* ------------------------------------------------------------- reading */

/** True when the block exists and the server could compute it. */
const astroOk = (a) => !!(a && a.available);

/** A finite number from the payload, or null. */
const astroNum = (v) => (v === null || v === undefined || !Number.isFinite(v) ? null : v);

/** One per-slot field at slot i: a bar's below `total`, a future slot's at
 *  or above it. null where there is none. */
function astroAt(a, key, i, total) {
  if (!astroOk(a) || i < 0) return null;
  return astroNum(i < total ? a.series[key]?.[i] : a.future[key]?.[i - total]);
}

/** Future slots to draw after `bars` bars: about 40% more, at least a lunar
 *  month on daily bars, never more than the server sent. */
function astroFutureCount(a, bars) {
  const avail = astroOk(a) ? a.future.time.length : 0;
  if (!avail) return 0;
  const floor = { daily: 22, weekly: 8, monthly: 3 }[a.kind] || 0;
  return Math.min(avail, Math.max(floor, Math.ceil(bars * 0.4)));
}

/** The future slot's date stamp ("YYYY-MM-DDT00:00:00"), for axes and legends. */
const astroFutureTime = (a, k) => a.future.time[k];

/** The phase's common name: eight 45° sectors, as astro.phase_name. */
const astroPhaseName = (elong) => ASTRO_PHASES[Math.floor((((elong + 22.5) % 360) + 360) % 360 / 45)];

/** "Dec 22" (or "Dec 2026" on monthly bars) for a session-date stamp. */
function astroShortDate(stamp, kind) {
  const d = new Date(`${String(stamp).slice(0, 10)}T00:00:00Z`);
  return (kind === "monthly" ? ASTRO_MONTH_FMT : ASTRO_DAY_FMT).format(d);
}

/** An event's instant in the chosen zone, with the zone's tag (tz.js). */
function astroWhen(e) {
  const t = new Date(e.time);
  return `${tzStamp(t)} ${tzTag(t)}`;
}

/** Signed to `d` places with a true minus sign; "—" when missing. */
const astroSigned = (v, d = 2) =>
  (astroNum(v) === null ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}`);

/** Everything known about slot i, for a legend or readout: the Moon's phase,
 *  lit share, distance and declination, the three lines, and on a future slot
 *  the projected price. */
function astroSlotInfo(a, i, total) {
  if (!astroOk(a)) return null;
  const elong = astroAt(a, "elong", i, total);
  return {
    future: i >= total,
    phase: elong === null ? null : astroPhaseName(elong),
    illum: astroAt(a, "illum", i, total),
    dist: astroAt(a, "dist", i, total),
    dec: astroAt(a, "dec", i, total),
    lsc: astroAt(a, "lsc", i, total),
    d1: astroAt(a, "d1", i, total),
    d2: astroAt(a, "d2", i, total),
    path: i >= total ? astroNum(a.future.path?.[i - total]) : null,
  };
}

/** The next new or full moon after slot i, or null. */
function astroNextPhase(a, i) {
  return (a.events || []).find((e) => e.slot > i && (e.type === "new_moon" || e.type === "full_moon")) || null;
}

/** SVG tspans for a pane title at slot i: each line's value beside a dot of
 *  its colour, then the Moon. Values are in text tokens; the dot carries
 *  the identity. `valueClass` is the chart's class for a value tspan. */
function astroTitleSpans(a, i, total, valueClass = "v") {
  const s = astroSlotInfo(a, i, total);
  if (!s) return "";
  const val = (v) => `<tspan class="${valueClass}">${v}</tspan>`;
  const lines = astroLineSpans(a, i, total, valueClass);
  const moon = s.phase === null ? "" :
    `  ·  ${s.phase} ${val(`${Math.round((s.illum ?? 0) * 100)}%`)}` +
    `  ${val(s.dist === null ? "—" : `${Math.round(s.dist).toLocaleString("en-US")} km`)}` +
    `  dec ${val(astroSigned(s.dec, 1))}°`;
  return `Lunar–solar cycle  ${lines}${moon}`;
}

/** Just the three lines' values at slot i, each beside its colour's dot. */
function astroLineSpans(a, i, total, valueClass = "v") {
  const s = astroSlotInfo(a, i, total);
  if (!s) return "";
  return ASTRO_LINES.map((l) =>
    `<tspan fill="${l.colour}">●</tspan> ${l.label} <tspan class="${valueClass}">${astroSigned(s[l.key])}</tspan>`).join("  ");
}

/* ------------------------------------------------------------- drawing */

/** A polyline through slots [from, to) of one line, broken at gaps. */
function astroPolyline(a, key, from, to, total, x, y, cls) {
  const out = [];
  let run = [];
  const flush = () => {
    if (run.length > 1) out.push(`<polyline class="${cls}" points="${run.join(" ")}"/>`);
    run = [];
  };
  for (let i = from; i < to; i++) {
    const v = astroAt(a, key, i, total);
    if (v === null) flush();
    else run.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  }
  flush();
  return out.join("");
}

/** A small Moon icon: the dark disc, and the lit half or whole on top.
 *  Fixed colours, not series tokens: a new moon reads dark and a full one
 *  bright in either theme. */
function astroMoonIcon(type, cx, cy, r = 4.2) {
  // The ring is a text token, so the dark disc of a new moon still shows on the dark surface.
  const disc = (fill) => `<circle cx="${cx.toFixed(1)}" cy="${cy}" r="${r}" fill="${fill}" stroke="var(--text-secondary)" stroke-width="1"/>`;
  const dark = "#3b3b39", lit = "#f1e4ae";
  if (type === "new_moon") return disc(dark);
  if (type === "full_moon") return disc(lit);
  // First quarter: right half lit (as seen from the northern hemisphere); last quarter: left.
  const sweep = type === "first_quarter" ? 1 : 0;
  return disc(dark) +
    `<path d="M${cx.toFixed(1)},${(cy - r).toFixed(1)} A${r},${r} 0 0 ${sweep} ${cx.toFixed(1)},${(cy + r).toFixed(1)} Z" fill="${lit}"/>`;
}

/**
 * The lunar-solar pane, as SVG strings: ±1 guides and zero line, the three
 * lines (solid over bars, dotted past the last), a "now" divider, and the
 * event icons in a strip along the top. `g` gives the slots shown
 * ([lo, hi)), the bar count `total`, the slot-to-x map `x`, the slot width
 * `bw`, and the pane box (left, right, top, bottom). Returns {parts, y,
 * plotTop}: `y` maps -1..+1 to pixels, for the crosshair.
 */
function astroPaneMarkup(a, g) {
  const { lo, hi, total, x, bw, left, right, top, bottom } = g;
  const parts = [];
  const plotTop = top + ASTRO_STRIP_H, plotBot = bottom;
  const y = (v) => plotTop + ((1.1 - v) / 2.2) * (plotBot - plotTop);
  [1, -1].forEach((v) =>
    parts.push(`<line class="astro-guide" x1="${left}" x2="${right}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}"/>`));
  parts.push(`<line class="zero-line" x1="${left}" x2="${right}" y1="${y(0).toFixed(1)}" y2="${y(0).toFixed(1)}"/>`);

  // Faint guides at new and full moons, while there are few enough to read.
  const shown = (a.events || []).filter((e) => e.slot >= lo && e.slot < hi);
  const majors = shown.filter((e) => e.type === "new_moon" || e.type === "full_moon");
  if (majors.length <= 16) {
    majors.forEach((e) => {
      const ex = x(e.slot).toFixed(1);
      parts.push(`<g class="astro-ev"><line x1="${ex}" x2="${ex}" y1="${plotTop}" y2="${plotBot}"/></g>`);
    });
  }

  const now = total - 1;
  if (hi > total && lo <= now) {
    const nx = (x(now) + bw / 2).toFixed(1);
    parts.push(`<line class="astro-now" x1="${nx}" x2="${nx}" y1="${top}" y2="${bottom}"/>`);
  }
  // Drawn back to front, LSC last so it sits on top.
  [...ASTRO_LINES].reverse().forEach((l) => {
    parts.push(astroPolyline(a, l.key, lo, Math.min(hi, total), total, x, y, `astro-line ${l.key}`));
    if (hi > total) {
      parts.push(astroPolyline(a, l.key, Math.max(lo, now), hi, total, x, y, `astro-line ${l.key} future`));
    }
  });

  // Icons: the most important first, each kept only where it clears the
  // ones already placed in its row.
  const taken = [[], []];
  shown.slice().sort((p, q) => (ASTRO_EVENTS[p.type]?.rank ?? 9) - (ASTRO_EVENTS[q.type]?.rank ?? 9))
    .forEach((e) => {
      const spec = ASTRO_EVENTS[e.type];
      if (!spec) return;
      const ex = x(e.slot);
      if (taken[spec.row].some((t) => Math.abs(t - ex) < 11)) return;
      taken[spec.row].push(ex);
      const cy = top + 7 + spec.row * 11;
      const tip = `${e.label} · ${astroWhen(e)}${e.distance_km ? ` · ${e.distance_km.toLocaleString("en-US")} km` : ""}`;
      const icon = spec.glyph
        ? `<text x="${ex.toFixed(1)}" y="${cy + 3.5}" text-anchor="middle">${spec.glyph}</text>`
        : astroMoonIcon(e.type, ex, cy);
      parts.push(`<g class="astro-ev${spec.rank >= 2 ? " minor" : ""}"><title>${esc(tip)}</title>${icon}</g>`);
    });
  return { parts, y, plotTop };
}

/** Lowest and highest projected price over the future slots in [lo, hi),
 *  for the price scale; null when none is shown. */
function astroPathExtent(a, lo, hi, total) {
  if (!astroOk(a) || !a.future.path?.length || hi <= total) return null;
  let mn = Infinity, mx = -Infinity;
  for (let i = Math.max(lo, total); i < hi; i++) {
    const v = astroNum(a.future.path[i - total]);
    if (v !== null) { mn = Math.min(mn, v); mx = Math.max(mx, v); }
  }
  return mn <= mx ? [mn, mx] : null;
}

/**
 * The projected price path, as SVG strings: dotted from the last close
 * through the future slots in view, with a dot and a "High Dec 22" label at
 * each turn. Labels that would overlap one already placed keep the dot
 * and lose the text; the tooltip carries it either way. `g` gives the slots
 * ([lo, hi)), `total`, the maps `x` and `y` (price), the last close, the
 * pane's top and bottom, and its left and right edges, inside which every
 * label is kept.
 */
function astroProjectionMarkup(a, g) {
  const { lo, hi, total, x, y, lastClose, top, bottom } = g;
  const left = g.left ?? -Infinity, right = g.right ?? Infinity;
  if (!astroOk(a) || !a.future.path?.length || hi <= total) return [];
  const parts = [];
  const pts = [];
  if (lo <= total - 1 && Number.isFinite(lastClose)) pts.push(`${x(total - 1).toFixed(1)},${y(lastClose).toFixed(1)}`);
  for (let i = Math.max(lo, total); i < hi; i++) {
    const v = astroNum(a.future.path[i - total]);
    if (v !== null) pts.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  }
  if (pts.length > 1) parts.push(`<polyline class="astro-path" points="${pts.join(" ")}"/>`);

  const boxes = [];
  (a.turns || []).forEach((t) => {
    const slot = total + t.i;
    if (slot < lo || slot >= hi) return;
    const cx = x(slot), cy = y(t.price);
    const high = t.type === "high";
    const text = `${high ? "High" : "Low"} ${astroShortDate(t.date, a.kind)}`;
    const w = text.length * 5.4 + 4;
    const ty = Math.min(Math.max(high ? cy - 8 : cy + 15, top + 10), bottom - 3);
    // The last turns sit near the axis; keep their text off the axis tags.
    const lx = Math.min(Math.max(cx, left + w / 2), right - w / 2);
    const box = { x0: lx - w / 2, x1: lx + w / 2, y0: ty - 10, y1: ty + 2 };
    const clear = !boxes.some((b) => b.x0 < box.x1 && box.x0 < b.x1 && b.y0 < box.y1 && box.y0 < b.y1);
    if (clear) boxes.push(box);
    const tip = `Projected ${high ? "high" : "low"} · ${t.date} · ${t.price.toLocaleString("en-US", { maximumFractionDigits: 2 })}\n` +
      "Lunar-solar cycle at its fitted amplitude: not a forecast with demonstrated skill (see the holdout note).";
    parts.push(`<g class="astro-turn"><title>${esc(tip)}</title>` +
      `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="2.6"/>` +
      (clear ? `<text x="${lx.toFixed(1)}" y="${ty.toFixed(1)}" text-anchor="middle">${text}</text>` : "") + `</g>`);
  });
  return parts;
}

/** The dashed "now" divider between the last bar and the first future slot,
 *  for a price pane; empty when no future slot is in view. */
function astroNowMarkup(total, lo, hi, x, bw, top, bottom) {
  if (hi <= total || lo > total - 1) return "";
  const nx = (x(total - 1) + bw / 2).toFixed(1);
  return `<line class="astro-now" x1="${nx}" x2="${nx}" y1="${top}" y2="${bottom}"/>`;
}
