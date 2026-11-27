/* gexdash drawing tools, on both price charts.
 *
 * Cursors (cross, dot, arrow, eraser); trend lines, rays, channels and
 * pitchforks; Fibonacci and Gann tools; harmonic patterns, Elliott waves and
 * cycles; positions, forecasts, anchored VWAP, a fixed-range volume profile
 * and range measures; brushes, arrows and shapes. Plus Magnet, Lock all, Hide
 * all and Remove. The Terminal chart and the Chart view share every drawing
 * of a symbol.
 *
 * Coordinates. A drawing is stored as points in time and price ({t: Unix
 * seconds, p: price}), never in pixels, so it survives zoom, pan, refresh,
 * a change of bar size and a change of view. Each chart hands this file an
 * adapter after every render (drawAttach): its plot box, its slot-to-x map
 * and price-to-y map, and its bar stamps. A slot is a bar index; past the
 * last bar, slots continue on the same rule the server's lunar-solar layer
 * uses (cycles.future_stamps): weekdays for daily bars, a week for weekly,
 * a month for monthly, the bar size intraday. So a drawing in the future
 * sits on the same date with the Moon/Sun layer on or off.
 *
 * Interaction is handled on the chart's stable host element in the capture
 * phase, before the chart's own pan and crosshair listeners, and hit-testing
 * is done here on the geometry each tool returns. The chart code only calls
 * drawAttach() after rendering and drawInit()/drawToolbar() once.
 *
 * Load order: after tz.js, before technicals.js and app.js. Nothing here
 * touches the DOM or storage until drawInit() runs, so the pure functions
 * load under Node for the tests (tests/test_draw.py). Every top-level name
 * carries a draw/DRAW_ prefix: classic scripts share one global scope.
 */

/* ===================================================================== */
/* Pure geometry and measures: tested under Node                         */
/* ===================================================================== */

/** Unix seconds for a bar stamp ("YYYY-MM-DDTHH:MM:SS", UTC). */
const drawSeconds = (stamp) => Date.parse(`${String(stamp).slice(0, 19)}Z`) / 1000;

/** 00:00 UTC on a date, as Unix seconds. */
const drawMidnight = (y, m, d) => Date.UTC(y, m, d) / 1000;

/** The k-th future slot's time after a last bar at `last` seconds, by the
 *  server's rule (cycles.future_stamps): the k-th weekday after its date for
 *  daily bars (holidays not modelled), k weeks on for weekly, k months on
 *  (day clamped to the month) for monthly, k bar sizes on intraday. Dated
 *  slots fall at 00:00 UTC, as the server stamps them. */
function drawFutureTime(last, kind, k, stepSec) {
  if (kind === "minute") return last + k * stepSec;
  const d = new Date(last * 1000);
  const y = d.getUTCFullYear(), m = d.getUTCMonth(), day = d.getUTCDate();
  if (kind === "weekly") return drawMidnight(y, m, day + 7 * k);
  if (kind === "monthly") {
    const len = new Date(Date.UTC(y, m + k + 1, 0)).getUTCDate();
    return drawMidnight(y, m + k, Math.min(day, len));
  }
  let t = drawMidnight(y, m, day), left = k;
  while (left > 0) {
    t += 86400;
    const wd = new Date(t * 1000).getUTCDay();
    if (wd !== 0 && wd !== 6) left -= 1;
  }
  return t;
}

/** The k-th slot before a first bar at `first` seconds, on the same rule
 *  as the future run backwards: weekdays for daily bars, weeks, months (day
 *  clamped), bar sizes intraday. Keeps the bar's time of day, so a drawing
 *  anchored on a bar that a shorter range no longer loads lands where that
 *  bar was. Holidays are not modelled, and intraday ignores the session. */
function drawPastTime(first, kind, k, stepSec) {
  if (kind === "minute") return first - k * stepSec;
  if (kind === "weekly") return first - 7 * k * 86400;
  const d = new Date(first * 1000);
  const tod = first - drawMidnight(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  if (kind === "monthly") {
    const y = d.getUTCFullYear(), m = d.getUTCMonth() - k;
    const len = new Date(Date.UTC(y, m + 1, 0)).getUTCDate();
    return drawMidnight(y, m, Math.min(d.getUTCDate(), len)) + tod;
  }
  let t = first, left = k;
  while (left > 0) {
    t -= 86400;
    const wd = new Date(t * 1000).getUTCDay();
    if (wd !== 0 && wd !== 6) left -= 1;
  }
  return t;
}

/** Slots <-> times for one chart's bars. Bars are slots 0..n-1 at their own
 *  stamps, linear between neighbours; past the last bar the slots follow
 *  drawFutureTime(), before the first drawPastTime(). Both are generated on
 *  demand and kept. */
function drawTimeline(stamps, kind, stepSec) {
  const T = stamps.map(drawSeconds);
  const n = T.length;
  const gaps = [];
  for (let i = Math.max(1, n - 200); i < n; i++) gaps.push(T[i] - T[i - 1]);
  gaps.sort((a, b) => a - b);
  const back = stepSec || gaps[Math.floor(gaps.length / 2)] || 86400;
  const past = [];
  const pastAt = (k) => {
    if (kind === "minute") return T[0] - k * back;
    while (past.length < k) {
      if (kind === "daily" && past.length) {
        let t = past[past.length - 1] - 86400;    // the weekday before the last one made
        while ([0, 6].includes(new Date(t * 1000).getUTCDay())) t -= 86400;
        past.push(t);
      } else {
        past.push(drawPastTime(T[0], kind, past.length + 1, back));
      }
    }
    return past[k - 1];
  };
  const fut = [];
  const futAt = (k) => {
    if (kind === "minute") return T[n - 1] + k * back;
    while (fut.length < k) {
      if (kind === "daily" && fut.length) {
        let t = fut[fut.length - 1] + 86400;      // the next weekday after the last one made
        while ([0, 6].includes(new Date(t * 1000).getUTCDay())) t += 86400;
        fut.push(t);
      } else {
        fut.push(drawFutureTime(T[n - 1], kind, fut.length + 1, back));
      }
    }
    return fut[k - 1];
  };
  return {
    n, T, kind, step: back,
    /** The time at a (fractional) slot. */
    timeOf(s) {
      if (!n) return 0;
      if (s < 0) {
        const k = -s, k0 = Math.floor(k), f = k - k0;
        const a = k0 === 0 ? T[0] : pastAt(k0);
        return f ? a + (pastAt(k0 + 1) - a) * f : a;
      }
      if (s <= n - 1) {
        const i = Math.floor(s), f = s - i;
        return i + 1 < n ? T[i] + (T[i + 1] - T[i]) * f : T[i];
      }
      const k = s - (n - 1), k0 = Math.floor(k), f = k - k0;
      const a = k0 === 0 ? T[n - 1] : futAt(k0);
      return f ? a + (futAt(k0 + 1) - a) * f : a;
    },
    /** The (fractional) slot at a time. */
    slotOf(t) {
      if (!n) return 0;
      if (t < T[0]) {
        if (kind === "minute") return (t - T[0]) / back;
        let prev = T[0];
        for (let k = 1; k <= 20000; k++) {
          const b = pastAt(k);
          if (t >= b) return -(k - 1) - (prev - t) / (prev - b);
          prev = b;
        }
        return -20000;
      }
      if (t <= T[n - 1]) {
        let lo = 0, hi = n - 1;
        while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (T[mid] <= t) lo = mid; else hi = mid; }
        return lo + (t - T[lo]) / ((T[hi] - T[lo]) || 1);
      }
      if (kind === "minute") return n - 1 + (t - T[n - 1]) / back;
      let prev = T[n - 1];
      for (let k = 1; k <= 20000; k++) {
        const b = futAt(k);
        if (t < b) return n - 1 + (k - 1) + (t - prev) / (b - prev);
        prev = b;
      }
      return n - 1 + 20000;
    },
  };
}

/** Fibonacci retracement: the price of each level for a move from p1 (the
 *  swing's start) to p2 (its end). 0 is the end of the move, 1 its start:
 *  price = p2 + (p1 - p2) * level, so 0.618 is a 61.8% pullback from p2. */
const DRAW_FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.618, 2.618];
function drawFibLevels(p1, p2, levels = DRAW_FIB_LEVELS) {
  return levels.map((level) => ({ level, price: p2 + (p1 - p2) * level }));
}

/** Trend-based Fibonacci extension: the move p1 -> p2 projected from p3:
 *  price = p3 + (p2 - p1) * level. */
const DRAW_FIBEXT_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.272, 1.618, 2, 2.618];
function drawFibExtension(p1, p2, p3, levels = DRAW_FIBEXT_LEVELS) {
  return levels.map((level) => ({ level, price: p3 + (p2 - p1) * level }));
}

/** Fibonacci time zones: the slots s1 + (s2 - s1) * F for the Fibonacci
 *  numbers 0, 1, 2, 3, 5, 8 ... */
const DRAW_FIB_NUMBERS = [0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233];
function drawFibTimeZones(s1, s2) {
  return DRAW_FIB_NUMBERS.map((f) => ({ f, slot: s1 + (s2 - s1) * f }));
}

/** A pitchfork from three points {x, y} (any linear space: slots and prices,
 *  or pixels). Returns the median line's origin and direction and the two
 *  parallel tines' starts. The median line runs from the origin through the
 *  midpoint of points 2 and 3. The origin is point 1 (Andrews); halfway
 *  between points 1 and 2 in price only, at point 1's time ("schiff"); or
 *  halfway in both time and price ("modified"). */
function drawPitchfork(a, b, c, variant = "andrews") {
  const mid = { x: (b.x + c.x) / 2, y: (b.y + c.y) / 2 };
  const origin = variant === "schiff" ? { x: a.x, y: (a.y + b.y) / 2 }
    : variant === "modified" ? { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 } : { x: a.x, y: a.y };
  return { origin, mid, dir: { x: mid.x - origin.x, y: mid.y - origin.y }, upper: b, lower: c };
}

/** Gann fan angles from p1 with p2 setting the 1x1: each ray's slope is the
 *  1x1 slope times price units / time units ("1x2" = one unit of price per
 *  two of time, half the 1x1 slope; "2x1" twice it). */
const DRAW_GANN_RATIOS = [[1, 8], [1, 4], [1, 3], [1, 2], [1, 1], [2, 1], [3, 1], [4, 1], [8, 1]];
function drawGannRays(p1, p2) {
  const run = p2.x - p1.x || 1e-9;
  const slope = (p2.y - p1.y) / run;
  return DRAW_GANN_RATIOS.map(([p, t]) => ({ label: `${p}x${t}`, slope: slope * (p / t) }));
}

/** Least-squares line of close on bar index over bars [i1, i2], with the
 *  population standard deviation of its residuals. null for fewer than two
 *  bars. The regression trend draws the line and +/-2 sigma around it. */
function drawRegression(close, i1, i2) {
  const a0 = Math.max(0, Math.ceil(Math.min(i1, i2))), a1 = Math.min(close.length - 1, Math.floor(Math.max(i1, i2)));
  const xs = [], ys = [];
  for (let i = a0; i <= a1; i++) if (Number.isFinite(close[i])) { xs.push(i); ys.push(close[i]); }
  const n = xs.length;
  if (n < 2) return null;
  const mx = xs.reduce((s, v) => s + v, 0) / n, my = ys.reduce((s, v) => s + v, 0) / n;
  let sxy = 0, sxx = 0;
  for (let k = 0; k < n; k++) { sxy += (xs[k] - mx) * (ys[k] - my); sxx += (xs[k] - mx) ** 2; }
  const slope = sxx ? sxy / sxx : 0, intercept = my - slope * mx;
  let ss = 0;
  for (let k = 0; k < n; k++) ss += (ys[k] - (intercept + slope * xs[k])) ** 2;
  return { slope, intercept, sd: Math.sqrt(ss / n), from: a0, to: a1, n };
}

/** VWAP anchored at bar `i0`: the running sum of typical price x volume
 *  over the running volume, from that bar to the last. A bar with no volume
 *  yet falls back to its typical price. [{i, v}]. */
function drawAnchoredVwap(bars, i0) {
  const out = [];
  let pv = 0, vol = 0;
  for (let i = Math.max(0, Math.ceil(i0 - 0.5)); i < bars.close.length; i++) {
    const tp = (bars.high[i] + bars.low[i] + bars.close[i]) / 3;
    const v = Math.max(bars.volume?.[i] || 0, 0);
    if (!Number.isFinite(tp)) continue;
    pv += tp * v; vol += v;
    out.push({ i, v: vol > 0 ? pv / vol : tp });
  }
  return out;
}

/** Volume by price over bars [i1, i2], in `rows` bins from the lowest low to
 *  the highest high. Each bar's volume is spread over the bins its range
 *  overlaps, in proportion to the overlap, as the server's volume profile
 *  does; a zero-range bar puts it all in one bin. Returns the bin edges,
 *  volumes, the point of control (the fullest bin's middle) and the value
 *  area: the 70% of volume around it, grown a bin at a time toward the
 *  fuller neighbour. null when there is no volume. */
function drawVolumeProfile(bars, i1, i2, rows = 24) {
  const a0 = Math.max(0, Math.ceil(Math.min(i1, i2))), a1 = Math.min(bars.close.length - 1, Math.floor(Math.max(i1, i2)));
  let lo = Infinity, hi = -Infinity, total = 0;
  for (let i = a0; i <= a1; i++) {
    lo = Math.min(lo, bars.low[i]); hi = Math.max(hi, bars.high[i]); total += Math.max(bars.volume?.[i] || 0, 0);
  }
  if (!(hi > lo) || !(total > 0)) return null;
  const edges = Array.from({ length: rows + 1 }, (_, k) => lo + ((hi - lo) * k) / rows);
  const vol = new Array(rows).fill(0);
  for (let i = a0; i <= a1; i++) {
    const v = Math.max(bars.volume?.[i] || 0, 0), h = bars.high[i], l = bars.low[i];
    if (!v) continue;
    if (h <= l) { vol[Math.min(rows - 1, Math.max(0, Math.floor(((h - lo) / (hi - lo)) * rows)))] += v; continue; }
    for (let k = 0; k < rows; k++) {
      const overlap = Math.min(h, edges[k + 1]) - Math.max(l, edges[k]);
      if (overlap > 0) vol[k] += (v * overlap) / (h - l);
    }
  }
  let poc = 0;
  vol.forEach((v, k) => { if (v > vol[poc]) poc = k; });
  let a = poc, b = poc, inside = vol[poc];
  while (inside < 0.7 * total && (a > 0 || b < rows - 1)) {
    const down = a > 0 ? vol[a - 1] : -1, up = b < rows - 1 ? vol[b + 1] : -1;
    if (up >= down) { b += 1; inside += vol[b]; } else { a -= 1; inside += vol[a]; }
  }
  return { edges, vol, total, poc, pocPrice: (edges[poc] + edges[poc + 1]) / 2, vaLow: edges[a], vaHigh: edges[b + 1], from: a0, to: a1 };
}

/** A long or short position from an entry, target and stop price: the
 *  reward and risk in price and percent, and their ratio (reward / risk;
 *  null without risk). */
function drawPosition(side, entry, target, stop) {
  const sign = side === "short" ? -1 : 1;
  const reward = sign * (target - entry), risk = sign * (entry - stop);
  return {
    reward, risk,
    rewardPct: (reward / entry) * 100, riskPct: (risk / entry) * 100,
    ratio: risk > 0 ? reward / risk : null,
  };
}

/** Distance from point (px, py) to segment (x1, y1)-(x2, y2). */
function drawSegDist(px, py, x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1;
  const len = dx * dx + dy * dy;
  const f = len ? Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / len)) : 0;
  return Math.hypot(px - (x1 + f * dx), py - (y1 + f * dy));
}

/** Point in polygon, by ray casting. */
function drawInPoly(px, py, pts) {
  let inside = false;
  for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
    const a = pts[i], b = pts[j];
    if ((a.y > py) !== (b.y > py) && px < ((b.x - a.x) * (py - a.y)) / (b.y - a.y || 1e-12) + a.x) inside = !inside;
  }
  return inside;
}

/** Douglas-Peucker: the indices of a polyline worth keeping within `eps`
 *  pixels, first and last always. Thins a brush stroke before it is stored. */
function drawThin(pts, eps = 1) {
  if (pts.length < 3) return pts.map((_, i) => i);
  const keep = new Set([0, pts.length - 1]);
  const stack = [[0, pts.length - 1]];
  while (stack.length) {
    const [a, b] = stack.pop();
    let worst = -1, at = -1;
    for (let i = a + 1; i < b; i++) {
      const d = drawSegDist(pts[i].x, pts[i].y, pts[a].x, pts[a].y, pts[b].x, pts[b].y);
      if (d > worst) { worst = d; at = i; }
    }
    if (worst > eps) { keep.add(at); stack.push([a, at], [at, b]); }
  }
  return [...keep].sort((p, q) => p - q);
}

/** Point a + (b - a) * f. */
const drawLerp = (a, b, f) => ({ x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f });

/** Far along the line a -> b: the point `len` pixels past b, or before a
 *  with `back`. The plot's clip path trims what lies beyond its edge. */
function drawFar(a, b, len, back = false) {
  const dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1;
  return back ? { x: a.x - (dx / d) * len, y: a.y - (dy / d) * len } : { x: b.x + (dx / d) * len, y: b.y + (dy / d) * len };
}

/** Where lines a1-a2 and b1-b2 cross, or null when parallel. */
function drawIntersect(a1, a2, b1, b2) {
  const d = (a1.x - a2.x) * (b1.y - b2.y) - (a1.y - a2.y) * (b1.x - b2.x);
  if (Math.abs(d) < 1e-9) return null;
  const t = ((a1.x - b1.x) * (b1.y - b2.y) - (a1.y - b1.y) * (b1.x - b2.x)) / d;
  return { x: a1.x + t * (a2.x - a1.x), y: a1.y + t * (a2.y - a1.y) };
}

/** Points along a quadratic Bezier, for drawing a curve's hit line. */
function drawBezier(p0, c, p1, steps = 24) {
  return Array.from({ length: steps + 1 }, (_, k) => {
    const t = k / steps, u = 1 - t;
    return { x: u * u * p0.x + 2 * u * t * c.x + t * t * p1.x, y: u * u * p0.y + 2 * u * t * c.y + t * t * p1.y };
  });
}

/* ===================================================================== */
/* Rendering helpers                                                     */
/* ===================================================================== */

/** Collects one drawing's SVG and the geometry it can be clicked on:
 *  segments (within 6px) and filled polygons (inside). */
function drawOut() {
  const parts = [], segs = [], fills = [];
  const f1 = (v) => (Number.isFinite(v) ? v.toFixed(1) : "0");
  const pts = (list) => list.map((p) => `${f1(p.x)},${f1(p.y)}`).join(" ");
  const o = {
    parts, segs, fills,
    line(a, b, cls = "dw-line", style = "") {
      segs.push([a.x, a.y, b.x, b.y]);
      parts.push(`<line class="${cls}" x1="${f1(a.x)}" y1="${f1(a.y)}" x2="${f1(b.x)}" y2="${f1(b.y)}"${style ? ` style="${style}"` : ""}/>`);
    },
    poly(list, cls = "dw-line", closed = false, style = "") {
      for (let i = 1; i < list.length; i++) segs.push([list[i - 1].x, list[i - 1].y, list[i].x, list[i].y]);
      if (closed && list.length > 2) segs.push([list[list.length - 1].x, list[list.length - 1].y, list[0].x, list[0].y]);
      parts.push(`<${closed ? "polygon" : "polyline"} class="${cls}" points="${pts(list)}"${style ? ` style="${style}"` : ""}/>`);
    },
    area(list, cls = "dw-fill", style = "") {
      fills.push(list);
      parts.push(`<polygon class="${cls}" points="${pts(list)}"${style ? ` style="${style}"` : ""}/>`);
    },
    text(x, y, s, cls = "dw-text", anchor = "start") {
      parts.push(`<text class="${cls}" x="${f1(x)}" y="${f1(y)}" text-anchor="${anchor}">${drawEsc(s)}</text>`);
    },
    /** A label on a filled tag, for prices and measures. */
    tag(x, y, s, cls = "dw-tag", anchor = "middle") {
      const w = String(s).length * 5.7 + 10;
      const x0 = anchor === "middle" ? x - w / 2 : anchor === "end" ? x - w : x;
      parts.push(`<g class="${cls}"><rect x="${f1(x0)}" y="${f1(y - 9)}" width="${f1(w)}" height="14" rx="3"/>` +
        `<text x="${f1(x0 + w / 2)}" y="${f1(y + 1.5)}" text-anchor="middle">${drawEsc(s)}</text></g>`);
    },
    circle(cx, cy, r, cls = "dw-line") {
      const ring = Array.from({ length: 33 }, (_, k) => ({ x: cx + r * Math.cos((k / 32) * 2 * Math.PI), y: cy + r * Math.sin((k / 32) * 2 * Math.PI) }));
      for (let i = 1; i < ring.length; i++) segs.push([ring[i - 1].x, ring[i - 1].y, ring[i].x, ring[i].y]);
      parts.push(`<circle class="${cls}" cx="${f1(cx)}" cy="${f1(cy)}" r="${f1(Math.max(r, 0.5))}"/>`);
    },
    ellipse(cx, cy, rx, ry, cls = "dw-line") {
      const ring = Array.from({ length: 33 }, (_, k) => ({ x: cx + rx * Math.cos((k / 32) * 2 * Math.PI), y: cy + ry * Math.sin((k / 32) * 2 * Math.PI) }));
      for (let i = 1; i < ring.length; i++) segs.push([ring[i - 1].x, ring[i - 1].y, ring[i].x, ring[i].y]);
      parts.push(`<ellipse class="${cls}" cx="${f1(cx)}" cy="${f1(cy)}" rx="${f1(Math.max(rx, 0.5))}" ry="${f1(Math.max(ry, 0.5))}"/>`);
    },
    /** An arrowhead at b, pointing from a. */
    head(a, b, size = 9, cls = "dw-head") {
      const ang = Math.atan2(b.y - a.y, b.x - a.x);
      const l = { x: b.x - size * Math.cos(ang - 0.42), y: b.y - size * Math.sin(ang - 0.42) };
      const r = { x: b.x - size * Math.cos(ang + 0.42), y: b.y - size * Math.sin(ang + 0.42) };
      parts.push(`<polygon class="${cls}" points="${pts([b, l, r])}"/>`);
    },
    raw(svg) { parts.push(svg); },
  };
  return o;
}

/** Text for SVG, escaped here so this file does not depend on app.js's esc. */
const drawEsc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

/** Signed with a true minus sign. */
const drawSigned = (v, d = 2) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}`;

/** A span of seconds as "3d 4h", "2h 15m" or "45m". */
function drawSpan(sec) {
  const s = Math.abs(sec);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.round((s % 3600) / 60);
  if (d >= 7) return `${Math.floor(d / 7)}w ${d % 7}d`;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}

/** Colours for multi-level tools (Fibonacci, Gann), from the page's tokens
 *  so both themes hold. The level's label carries its identity, not the hue. */
const DRAW_LEVEL_COLOURS = ["var(--text-secondary)", "var(--series-put)", "var(--ma-50)", "var(--series-call)",
  "var(--series-violet)", "var(--series-gold)", "var(--neg)", "var(--ma-200)", "var(--text-muted)"];
const drawLevelColour = (k) => DRAW_LEVEL_COLOURS[k % DRAW_LEVEL_COLOURS.length];

/** A price label for a level: "0.618 (7,712.30)". */
const drawLevelText = (c, level, price) => `${+level.toFixed(3)} (${c.A.fmtPrice(price)})`;

/* ===================================================================== */
/* The tools                                                             */
/* ===================================================================== */

/* Each tool: a name, a group, how many points it takes (`n`; "free" for a
 * brush stroke, "path" for click-until-double-click), a one-line definition
 * shown as its tooltip, and render(c, o). `c.P` holds the points in pixels
 * (while drawing, the pointer is the last one), `c.D` in data ({t, p, s}),
 * `c.box` the plot box, `c.A` the chart adapter. A renderer draws whatever
 * its points so far allow. */

/** Renderers that only need lines between the points so far. */
const drawZig = (c, o, cls = "dw-line") => { if (c.P.length > 1) o.poly(c.P, cls); };

/** A wave or pattern's point labels, above a peak and below a trough. */
function drawPointLabels(c, o, names, skipFirst = false) {
  c.P.forEach((p, i) => {
    const name = names[i - (skipFirst ? 1 : 0)];
    if (!name || (skipFirst && i === 0)) return;
    const prev = c.P[i - 1] || c.P[i + 1], next = c.P[i + 1] || c.P[i - 1];
    const peak = !prev || !next ? p.y <= (prev || next || p).y : p.y <= prev.y && p.y <= next.y;
    o.text(p.x, peak ? p.y - 7 : p.y + 15, name, "dw-text dw-bold", "middle");
  });
}

/** The price ratio |a-b| / |c-d| of two legs, as "0.618". */
const drawRatio = (a, b, c2, d) => {
  const den = Math.abs(c2.p - d.p);
  return den ? (Math.abs(a.p - b.p) / den).toFixed(3) : "—";
};

function drawPitchforkTool(variant) {
  return (c, o) => {
    if (c.P.length < 3) return drawZig(c, o, "dw-line dw-dash");
    const f = drawPitchfork(c.P[0], c.P[1], c.P[2], variant);
    const far = (from) => ({ x: from.x + f.dir.x * 40, y: from.y + f.dir.y * 40 });
    const up = far(f.upper), dn = far(f.lower);
    o.area([f.upper, up, dn, f.lower], "dw-fill");
    o.line(f.origin, far(f.origin), "dw-line");
    o.line(f.upper, up, "dw-line");
    o.line(f.lower, dn, "dw-line");
    o.line(f.upper, f.lower, "dw-line dw-dash");
    if (variant !== "andrews") o.line(c.P[0], f.origin, "dw-line dw-dash");
  };
}

/** Elliott wave labels: the origin is unlabelled, the waves carry these. */
const drawWave = (names) => (c, o) => { drawZig(c, o); drawPointLabels(c, o, names, true); };

const DRAW_TOOLS = {
  /* ---- cursors ---- */
  cross: { name: "Cross", group: "cursor", cursor: true, tip: "Crosshair cursor: lines through the pointer to both axes" },
  dot: { name: "Dot", group: "cursor", cursor: true, tip: "A dot for the pointer, with the crosshair" },
  arrow: { name: "Arrow", group: "cursor", cursor: true, tip: "The plain arrow pointer, crosshair lines hidden" },
  eraser: { name: "Eraser", group: "cursor", cursor: true, tip: "Click a drawing to remove it" },

  /* ---- lines, channels, pitchforks ---- */
  trend: { name: "Trend line", group: "lines", n: 2, tip: "A segment between two points",
    render: (c, o) => { if (c.P.length > 1) o.line(c.P[0], c.P[1]); } },
  ray: { name: "Ray", group: "lines", n: 2, tip: "From the first point through the second, to the edge",
    render: (c, o) => { if (c.P.length > 1) o.line(c.P[0], drawFar(c.P[0], c.P[1], 4000)); } },
  extended: { name: "Extended line", group: "lines", n: 2, tip: "Through both points, to both edges",
    render: (c, o) => { if (c.P.length > 1) o.line(drawFar(c.P[0], c.P[1], 4000, true), drawFar(c.P[0], c.P[1], 4000)); } },
  info: { name: "Info line", group: "lines", n: 2, tip: "A segment labelled with its price change, percent, bars and angle",
    render: (c, o) => {
      if (c.P.length < 2) return;
      o.line(c.P[0], c.P[1]);
      const dp = c.D[1].p - c.D[0].p, bars = Math.round(c.D[1].s - c.D[0].s);
      const ang = (Math.atan2(c.P[0].y - c.P[1].y, c.P[1].x - c.P[0].x) * 180) / Math.PI;
      o.tag(c.P[1].x, c.P[1].y - 12, `${drawSigned(dp)} (${drawSigned((dp / c.D[0].p) * 100)}%) · ${bars} bars · ${ang.toFixed(0)}°`);
    } },
  hline: { name: "Horizontal line", group: "lines", n: 1, tip: "A price level across the chart",
    render: (c, o) => {
      o.line({ x: c.box.l, y: c.P[0].y }, { x: c.box.r, y: c.P[0].y });
      o.tag(c.box.r - 4, c.P[0].y - 9, c.A.fmtPrice(c.D[0].p), "dw-tag", "end");
    } },
  hray: { name: "Horizontal ray", group: "lines", n: 1, tip: "A price level from the point to the right",
    render: (c, o) => {
      o.line(c.P[0], { x: c.box.r, y: c.P[0].y });
      o.tag(c.box.r - 4, c.P[0].y - 9, c.A.fmtPrice(c.D[0].p), "dw-tag", "end");
    } },
  vline: { name: "Vertical line", group: "lines", n: 1, tip: "A moment in time, top to bottom",
    render: (c, o) => {
      o.line({ x: c.P[0].x, y: c.box.t }, { x: c.P[0].x, y: c.box.b });
      o.tag(c.P[0].x, c.box.b - 8, c.A.fmtTime(c.D[0].t));
    } },
  crossline: { name: "Cross line", group: "lines", n: 1, tip: "A horizontal and a vertical line through one point",
    render: (c, o) => {
      o.line({ x: c.box.l, y: c.P[0].y }, { x: c.box.r, y: c.P[0].y });
      o.line({ x: c.P[0].x, y: c.box.t }, { x: c.P[0].x, y: c.box.b });
      o.tag(c.box.r - 4, c.P[0].y - 9, c.A.fmtPrice(c.D[0].p), "dw-tag", "end");
    } },
  channel: { name: "Parallel channel", group: "lines", n: 3, tip: "A base line, and a parallel one through the third point, with the midline",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const [a, b] = c.P;
      o.line(a, b);
      if (c.P.length < 3) return;
      const along = (b.x - a.x) ? (c.P[2].x - a.x) / (b.x - a.x) : 0;
      const dy = c.P[2].y - (a.y + (b.y - a.y) * along);
      const a2 = { x: a.x, y: a.y + dy }, b2 = { x: b.x, y: b.y + dy };
      o.area([a, b, b2, a2]);
      o.line(a2, b2);
      o.line({ x: a.x, y: a.y + dy / 2 }, { x: b.x, y: b.y + dy / 2 }, "dw-line dw-dash");
    } },
  regression: { name: "Regression trend", group: "lines", n: 2, tip: "Least-squares line of the closes between the points, ±2 standard deviations of the residuals",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const r = drawRegression(c.A.bars.close, c.D[0].s, c.D[1].s);
      if (!r) return o.line(c.P[0], c.P[1], "dw-line dw-dash");
      const pt = (i, k) => ({ x: c.A.x(i), y: c.A.y(r.intercept + r.slope * i + k * r.sd) });
      o.area([pt(r.from, 2), pt(r.to, 2), pt(r.to, -2), pt(r.from, -2)]);
      o.line(pt(r.from, 0), pt(r.to, 0));
      o.line(pt(r.from, 2), pt(r.to, 2), "dw-line dw-dash");
      o.line(pt(r.from, -2), pt(r.to, -2), "dw-line dw-dash");
      o.tag(pt(r.to, 2).x, pt(r.to, 2).y - 10, `${r.n} bars · σ ${c.A.fmtPrice(r.sd)}`, "dw-tag", "end");
    } },
  pitchfork: { name: "Pitchfork", group: "lines", n: 3, tip: "Andrews: the median line runs from point 1 through the midpoint of points 2 and 3; the tines are parallel to it",
    render: drawPitchforkTool("andrews") },
  schiff: { name: "Schiff pitchfork", group: "lines", n: 3, tip: "The median line starts halfway between points 1 and 2 in price, at point 1's time",
    render: drawPitchforkTool("schiff") },
  modschiff: { name: "Modified Schiff pitchfork", group: "lines", n: 3, tip: "The median line starts halfway between points 1 and 2 in both time and price",
    render: drawPitchforkTool("modified") },

  /* ---- Fibonacci and Gann ---- */
  fib: { name: "Fib retracement", group: "fib", n: 2, tip: "Levels between a swing's start (1) and end (0): 0.236, 0.382, 0.5, 0.618, 0.786, and 1.618, 2.618 beyond",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const x0 = Math.min(c.P[0].x, c.P[1].x), x1 = Math.max(c.P[0].x, c.P[1].x);
      const lv = drawFibLevels(c.D[0].p, c.D[1].p);
      lv.forEach((l, k) => {
        const y = c.A.y(l.price);
        if (k) {
          const yPrev = c.A.y(lv[k - 1].price);
          o.raw(`<rect class="dw-band" x="${x0.toFixed(1)}" y="${Math.min(y, yPrev).toFixed(1)}" width="${(x1 - x0).toFixed(1)}" height="${Math.abs(y - yPrev).toFixed(1)}" style="fill:${drawLevelColour(k)}"/>`);
        }
        o.line({ x: x0, y }, { x: x1, y }, "dw-line dw-level", `stroke:${drawLevelColour(k)}`);
        o.text(x0 + 3, y - 2.5, drawLevelText(c, l.level, l.price), "dw-text dw-small");
      });
      o.line(c.P[0], c.P[1], "dw-line dw-dash");
    } },
  fibext: { name: "Trend-based fib extension", group: "fib", n: 3, tip: "The move from point 1 to point 2, projected from point 3 at 0.618, 1, 1.272, 1.618, 2, 2.618 and more",
    render: (c, o) => {
      drawZig(c, o, "dw-line dw-dash");
      if (c.P.length < 3) return;
      const x0 = c.P[2].x, x1 = c.P[2].x + Math.max(Math.abs(c.P[1].x - c.P[0].x), 40);
      drawFibExtension(c.D[0].p, c.D[1].p, c.D[2].p).forEach((l, k) => {
        const y = c.A.y(l.price);
        o.line({ x: x0, y }, { x: x1, y }, "dw-line dw-level", `stroke:${drawLevelColour(k)}`);
        o.text(x1 + 4, y + 3.5, drawLevelText(c, l.level, l.price), "dw-text dw-small");
      });
    } },
  fibchannel: { name: "Fib channel", group: "fib", n: 3, tip: "A base line, with parallels at 0.382, 0.618, 1, 1.618 and 2.618 of the width set by point 3",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const [a, b] = c.P;
      o.line(a, drawFar(a, b, 4000));
      if (c.P.length < 3) return;
      const along = (b.x - a.x) ? (c.P[2].x - a.x) / (b.x - a.x) : 0;
      const dy = c.P[2].y - (a.y + (b.y - a.y) * along);
      [0.382, 0.618, 1, 1.618, 2.618].forEach((l, k) => {
        const a2 = { x: a.x, y: a.y + dy * l }, b2 = { x: b.x, y: b.y + dy * l };
        o.line(a2, drawFar(a2, b2, 4000), "dw-line dw-level", `stroke:${drawLevelColour(k + 1)}`);
        o.text(a2.x - 4, a2.y + 3.5, String(l), "dw-text dw-small", "end");
      });
    } },
  fibtime: { name: "Fib time zone", group: "fib", n: 2, tip: "Verticals at 0, 1, 2, 3, 5, 8, 13 … times the spacing between the points",
    render: (c, o) => {
      if (c.P.length < 2) return;
      drawFibTimeZones(c.D[0].s, c.D[1].s).forEach(({ f, slot }, k) => {
        const x = c.A.x(slot);
        if (x < c.box.l - 1 || x > c.box.r + 1) return;
        o.line({ x, y: c.box.t }, { x, y: c.box.b }, "dw-line dw-level", `stroke:${drawLevelColour(k)}`);
        o.text(x + 3, c.box.t + 11, String(f), "dw-text dw-small");
      });
    } },
  fibfan: { name: "Fib speed resistance fan", group: "fib", n: 2, tip: "Rays from point 1 through 0.25, 0.382, 0.5, 0.618 and 0.75 of the box to point 2, in price and in time",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const [a, b] = c.P;
      o.raw(`<rect class="dw-box" x="${Math.min(a.x, b.x).toFixed(1)}" y="${Math.min(a.y, b.y).toFixed(1)}" width="${Math.abs(b.x - a.x).toFixed(1)}" height="${Math.abs(b.y - a.y).toFixed(1)}"/>`);
      [0.25, 0.382, 0.5, 0.618, 0.75, 1].forEach((r, k) => {
        const p = { x: b.x, y: a.y + (b.y - a.y) * r };
        o.line(a, drawFar(a, p, 3000), "dw-line dw-level", `stroke:${drawLevelColour(k)}`);
        o.text(b.x + 4, p.y + 3.5, String(r), "dw-text dw-small");
        if (r < 1) {
          const q = { x: a.x + (b.x - a.x) * r, y: b.y };
          o.line(a, drawFar(a, q, 3000), "dw-line dw-level dw-dash", `stroke:${drawLevelColour(k)}`);
        }
      });
    } },
  fibcircles: { name: "Fib circles", group: "fib", n: 2, tip: "Circles about point 1 at 0.382, 0.5, 0.618, 1, 1.618 and 2.618 of the distance to point 2 (on screen)",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const R = Math.hypot(c.P[1].x - c.P[0].x, c.P[1].y - c.P[0].y);
      [0.382, 0.5, 0.618, 1, 1.618, 2.618].forEach((l, k) => {
        o.raw(`<circle class="dw-line dw-level" cx="${c.P[0].x.toFixed(1)}" cy="${c.P[0].y.toFixed(1)}" r="${(R * l).toFixed(1)}" style="stroke:${drawLevelColour(k)}"/>`);
        o.text(c.P[0].x + R * l + 3, c.P[0].y - 3, String(l), "dw-text dw-small");
      });
      o.circle(c.P[0].x, c.P[0].y, R, "dw-hit");
      o.line(c.P[0], c.P[1], "dw-line dw-dash");
    } },
  gannbox: { name: "Gann box", group: "fib", n: 2, tip: "A box with price and time levels at 0.25, 0.382, 0.5, 0.618 and 0.75",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const [a, b] = c.P;
      o.area([a, { x: b.x, y: a.y }, b, { x: a.x, y: b.y }], "dw-fill");
      o.poly([a, { x: b.x, y: a.y }, b, { x: a.x, y: b.y }], "dw-line", true);
      // A level's label shows only where it clears the last one drawn.
      let lastX = -Infinity, lastY = -Infinity;
      [0.25, 0.382, 0.5, 0.618, 0.75].forEach((r, k) => {
        const y = a.y + (b.y - a.y) * r, x = a.x + (b.x - a.x) * r;
        o.line({ x: a.x, y }, { x: b.x, y }, "dw-line dw-level", `stroke:${drawLevelColour(k + 1)}`);
        o.line({ x, y: a.y }, { x, y: b.y }, "dw-line dw-level", `stroke:${drawLevelColour(k + 1)}`);
        if (Math.abs(y - lastY) >= 11) { o.text(Math.min(a.x, b.x) + 3, y - 2.5, String(r), "dw-text dw-small"); lastY = y; }
        if (Math.abs(x - lastX) >= 26) { o.text(x + 2, Math.max(a.y, b.y) - 3, String(r), "dw-text dw-small"); lastX = x; }
      });
    } },
  gannsquare: { name: "Gann square", group: "fib", n: 2, tip: "A box divided in thirds and quarters, with both diagonals and the 1x2 and 2x1 lines from the first corner",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const [a, b] = c.P;
      const corners = [a, { x: b.x, y: a.y }, b, { x: a.x, y: b.y }];
      o.poly(corners, "dw-line", true);
      [1 / 4, 1 / 3, 1 / 2, 2 / 3, 3 / 4].forEach((r) => {
        const cls = r === 1 / 2 ? "dw-line" : "dw-line dw-dash";
        o.line({ x: a.x, y: a.y + (b.y - a.y) * r }, { x: b.x, y: a.y + (b.y - a.y) * r }, cls);
        o.line({ x: a.x + (b.x - a.x) * r, y: a.y }, { x: a.x + (b.x - a.x) * r, y: b.y }, cls);
      });
      o.line(a, b, "dw-line dw-level", `stroke:${drawLevelColour(1)}`);
      o.line(corners[1], corners[3], "dw-line dw-level", `stroke:${drawLevelColour(1)}`);
      o.line(a, { x: b.x, y: a.y + (b.y - a.y) / 2 }, "dw-line dw-level", `stroke:${drawLevelColour(2)}`);
      o.line(a, { x: a.x + (b.x - a.x) / 2, y: b.y }, "dw-line dw-level", `stroke:${drawLevelColour(2)}`);
    } },
  gannfan: { name: "Gann fan", group: "fib", n: 2, tip: "Rays from point 1 at 1x8 … 1x1 … 8x1, with point 2 setting the 1x1 (one unit of price per unit of time)",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const a = c.P[0], run = c.P[1].x - a.x || 1e-9;
      // Labels a fixed distance out along each ray, so they fan apart
      // instead of piling up at the origin.
      const reach = Math.max(90, 1.25 * Math.hypot(c.P[1].x - a.x, c.P[1].y - a.y));
      drawGannRays(c.P[0], c.P[1]).forEach((g, k) => {
        const end = { x: a.x + run * 3, y: a.y + g.slope * run * 3 };
        o.line(a, drawFar(a, end, 3000), "dw-line dw-level", `stroke:${drawLevelColour(k)}`);
        const len = Math.hypot(run, g.slope * run) || 1;
        o.text(a.x + (run / len) * reach, a.y + ((g.slope * run) / len) * reach - 3, g.label, "dw-text dw-small");
      });
    } },

  /* ---- patterns, Elliott waves, cycles ---- */
  xabcd: { name: "XABCD pattern", group: "patterns", n: 5, tip: "Five points X-A-B-C-D with the harmonic ratios AB/XA, BC/AB, CD/BC and AD/XA",
    render: (c, o) => {
      const [X, A, B, C, D] = c.P;
      if (c.P.length >= 3) o.area([X, A, B]);
      if (c.P.length >= 5) o.area([B, C, D]);
      drawZig(c, o);
      const d = c.D;
      const mid = (p, q, label) => { o.line(p, q, "dw-line dw-dash"); o.tag((p.x + q.x) / 2, (p.y + q.y) / 2, label); };
      if (c.P.length >= 3) mid(X, B, drawRatio(d[1], d[2], d[0], d[1]));
      if (c.P.length >= 4) mid(A, C, drawRatio(d[2], d[3], d[1], d[2]));
      if (c.P.length >= 5) { mid(B, D, drawRatio(d[3], d[4], d[2], d[3])); mid(X, D, drawRatio(d[1], d[4], d[0], d[1])); }
      drawPointLabels(c, o, ["X", "A", "B", "C", "D"]);
    } },
  abcd: { name: "ABCD pattern", group: "patterns", n: 4, tip: "A-B-C-D with the ratios BC/AB and CD/BC",
    render: (c, o) => {
      drawZig(c, o);
      const d = c.D, P = c.P;
      if (P.length >= 3) { o.line(P[0], P[2], "dw-line dw-dash"); o.tag((P[0].x + P[2].x) / 2, (P[0].y + P[2].y) / 2, drawRatio(d[1], d[2], d[0], d[1])); }
      if (P.length >= 4) { o.line(P[1], P[3], "dw-line dw-dash"); o.tag((P[1].x + P[3].x) / 2, (P[1].y + P[3].y) / 2, drawRatio(d[2], d[3], d[1], d[2])); }
      drawPointLabels(c, o, ["A", "B", "C", "D"]);
    } },
  trianglepat: { name: "Triangle pattern", group: "patterns", n: 4, tip: "Four swings A-B-C-D; the lines A-C and B-D are drawn on to where they meet",
    render: (c, o) => {
      drawZig(c, o);
      if (c.P.length >= 4) {
        const [A, B, C, D] = c.P;
        const x = drawIntersect(A, C, B, D);
        const meet = x && x.x > Math.max(C.x, D.x) && x.x - D.x < 4000 ? x : null;
        o.line(A, meet || drawFar(A, C, 300), "dw-line dw-dash");
        o.line(B, meet || drawFar(B, D, 300), "dw-line dw-dash");
        if (meet) o.area([A, meet, B]);
      }
      drawPointLabels(c, o, ["A", "B", "C", "D"]);
    } },
  headshoulders: { name: "Head and shoulders", group: "patterns", n: 7, tip: "Seven points: start, left shoulder, neckline, head, neckline, right shoulder, end; the neckline is drawn through both neckline points",
    render: (c, o) => {
      drawZig(c, o);
      if (c.P.length >= 5) {
        const n1 = c.P[2], n2 = c.P[4];
        o.line(drawFar(n1, n2, 3000, true), drawFar(n1, n2, 3000), "dw-line dw-dash");
        o.area([n1, c.P[3], n2]);
      }
      const names = ["", "Left shoulder", "", "Head", "", "Right shoulder", ""];
      c.P.forEach((p, i) => { if (names[i]) o.text(p.x, p.y + (c.P[2] && p.y > c.P[2].y ? 15 : -7), names[i], "dw-text dw-bold", "middle"); });
    } },
  elliott_impulse: { name: "Elliott impulse wave (1-2-3-4-5)", group: "patterns", n: 6, tip: "An origin and five waves, labelled (1) to (5)",
    render: drawWave(["(1)", "(2)", "(3)", "(4)", "(5)"]) },
  elliott_correction: { name: "Elliott correction wave (A-B-C)", group: "patterns", n: 4, tip: "An origin and three waves, labelled (A) (B) (C)",
    render: drawWave(["(A)", "(B)", "(C)"]) },
  elliott_triangle: { name: "Elliott triangle wave (A-B-C-D-E)", group: "patterns", n: 6, tip: "An origin and five waves, labelled (A) to (E)",
    render: drawWave(["(A)", "(B)", "(C)", "(D)", "(E)"]) },
  elliott_double: { name: "Elliott double combo (W-X-Y)", group: "patterns", n: 4, tip: "An origin and three waves, labelled (W) (X) (Y)",
    render: drawWave(["(W)", "(X)", "(Y)"]) },
  elliott_triple: { name: "Elliott triple combo (W-X-Y-X-Z)", group: "patterns", n: 6, tip: "An origin and five waves, labelled (W) (X) (Y) (X) (Z)",
    render: drawWave(["(W)", "(X)", "(Y)", "(X)", "(Z)"]) },
  cyclic: { name: "Cyclic lines", group: "patterns", n: 2, tip: "Verticals repeating at the spacing between the two points, from the first one on",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const step = c.P[1].x - c.P[0].x;
      if (Math.abs(step) < 3) return o.line(c.P[0], c.P[1]);
      for (let k = 0, x = c.P[0].x; k < 300 && (step > 0 ? x <= c.box.r : x >= c.box.l); k++, x += step) {
        o.line({ x, y: c.box.t }, { x, y: c.box.b }, k < 2 ? "dw-line" : "dw-line dw-dash");
      }
    } },
  timecycles: { name: "Time cycles", group: "patterns", n: 2, tip: "Arcs repeating at the spacing between the two points, as high as the second point",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const [a, b] = c.P, w = b.x - a.x, h = b.y - a.y;
      if (Math.abs(w) < 3) return o.line(a, b);
      const ry = Math.abs(h) || Math.abs(w) / 2, sweep = h <= 0 ? 1 : 0;
      for (let k = 0, x = a.x; k < 200 && (w > 0 ? x <= c.box.r : x >= c.box.l); k++, x += w) {
        o.raw(`<path class="dw-line" d="M${x.toFixed(1)},${a.y.toFixed(1)} A${(Math.abs(w) / 2).toFixed(1)},${ry.toFixed(1)} 0 0 ${w > 0 ? sweep : 1 - sweep} ${(x + w).toFixed(1)},${a.y.toFixed(1)}"/>`);
        const arc = Array.from({ length: 13 }, (_, j) => ({ x: x + (w * j) / 12, y: a.y + (h <= 0 ? -1 : 1) * ry * Math.sin((Math.PI * j) / 12) }));
        for (let j = 1; j < arc.length; j++) o.segs.push([arc[j - 1].x, arc[j - 1].y, arc[j].x, arc[j].y]);
      }
    } },
  sine: { name: "Sine line", group: "patterns", n: 2, tip: "A sine wave with point 1 one extreme and point 2 the next opposite one: half a cycle apart",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const [a, b] = c.P, half = b.x - a.x;
      if (Math.abs(half) < 3) return o.line(a, b);
      const amp = (b.y - a.y) / 2, mid = (a.y + b.y) / 2, pts = [];
      for (let x = c.box.l; x <= c.box.r; x += 3) pts.push({ x, y: mid - amp * Math.cos((Math.PI * (x - a.x)) / half) });
      o.poly(pts);
    } },

  /* ---- forecasting, volume-based, measures ---- */
  long: { name: "Long position", group: "measure", n: 3, tip: "Entry, then the target (and the box's end in time), then the stop: reward, risk and their ratio",
    render: (c, o) => drawPositionTool(c, o, "long") },
  short: { name: "Short position", group: "measure", n: 3, tip: "Entry, then the target (and the box's end in time), then the stop: reward, risk and their ratio",
    render: (c, o) => drawPositionTool(c, o, "short") },
  forecast: { name: "Forecast", group: "measure", n: 2, tip: "An arrow from now to a forecast price and date, with the change and the time it allows",
    render: (c, o) => {
      if (c.P.length < 2) return;
      o.line(c.P[0], c.P[1], "dw-line dw-dash");
      o.head(c.P[0], c.P[1]);
      o.circle(c.P[0].x, c.P[0].y, 3.5, "dw-dot");
      const dp = c.D[1].p - c.D[0].p;
      o.tag(c.P[1].x, c.P[1].y + (dp >= 0 ? -14 : 20),
        `${c.A.fmtPrice(c.D[1].p)} (${drawSigned((dp / c.D[0].p) * 100)}%) · ${c.A.fmtTime(c.D[1].t)} · ${drawSpan(c.D[1].t - c.D[0].t)}`);
    } },
  avwap: { name: "Anchored VWAP", group: "measure", n: 1, tip: "Volume-weighted average of the typical price (H+L+C)/3 from the anchor bar to the last bar",
    render: (c, o) => {
      o.circle(c.P[0].x, c.P[0].y, 3.5, "dw-dot");
      const v = drawAnchoredVwap(c.A.bars, c.D[0].s);
      if (v.length < 1) return;
      const pts = v.map((q) => ({ x: c.A.x(q.i), y: c.A.y(q.v) }));
      if (pts.length > 1) o.poly(pts, "dw-line dw-vwap");
      const last = v[v.length - 1];
      o.tag(pts[pts.length - 1].x, pts[pts.length - 1].y - 11, `AVWAP ${c.A.fmtPrice(last.v)}`, "dw-tag", "end");
    } },
  vprofile: { name: "Fixed range volume profile", group: "measure", n: 2, tip: "Volume by price over the bars between the points: 24 rows, the point of control and the 70% value area",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const vp = drawVolumeProfile(c.A.bars, c.D[0].s, c.D[1].s);
      const x0 = Math.min(c.P[0].x, c.P[1].x), x1 = Math.max(c.P[0].x, c.P[1].x);
      if (!vp) return o.line(c.P[0], c.P[1], "dw-line dw-dash");
      const max = Math.max(...vp.vol) || 1, width = Math.max((x1 - x0) * 0.7, 30);
      const yTop = c.A.y(vp.edges[vp.edges.length - 1]), yBot = c.A.y(vp.edges[0]);
      o.area([{ x: x0, y: yTop }, { x: x1, y: yTop }, { x: x1, y: yBot }, { x: x0, y: yBot }], "dw-box");
      vp.vol.forEach((v, k) => {
        const yt = c.A.y(vp.edges[k + 1]), yb = c.A.y(vp.edges[k]);
        const inVA = vp.edges[k] >= vp.vaLow - 1e-9 && vp.edges[k + 1] <= vp.vaHigh + 1e-9;
        o.raw(`<rect class="dw-vp${inVA ? " va" : ""}" x="${x0.toFixed(1)}" y="${(Math.min(yt, yb) + 0.5).toFixed(1)}" width="${((v / max) * width).toFixed(1)}" height="${Math.max(Math.abs(yb - yt) - 1, 0.6).toFixed(1)}"/>`);
      });
      const yp = c.A.y(vp.pocPrice);
      o.line({ x: x0, y: yp }, { x: x1, y: yp }, "dw-line dw-poc");
      o.tag(x1, yp - 10, `POC ${c.A.fmtPrice(vp.pocPrice)}`, "dw-tag", "end");
    } },
  pricerange: { name: "Price range", group: "measure", n: 2, tip: "The price change between two points, in price and percent",
    render: (c, o) => drawRangeTool(c, o, true, false) },
  daterange: { name: "Date range", group: "measure", n: 2, tip: "The bars and time between two points",
    render: (c, o) => drawRangeTool(c, o, false, true) },
  dprange: { name: "Date and price range", group: "measure", n: 2, tip: "Both: price change, percent, bars and time",
    render: (c, o) => drawRangeTool(c, o, true, true) },

  /* ---- brushes, arrows, shapes ---- */
  brush: { name: "Brush", group: "shapes", n: "free", tip: "Draw freehand while the button is held",
    render: (c, o) => { if (c.P.length > 1) o.poly(c.P, "dw-line dw-brush"); } },
  highlighter: { name: "Highlighter", group: "shapes", n: "free", tip: "A broad translucent stroke, freehand",
    render: (c, o) => { if (c.P.length > 1) o.poly(c.P, "dw-highlight"); } },
  arrowline: { name: "Arrow", group: "shapes", n: 2, tip: "A line with an arrowhead at the second point",
    render: (c, o) => { if (c.P.length > 1) { o.line(c.P[0], c.P[1]); o.head(c.P[0], c.P[1]); } } },
  arrowup: { name: "Arrow mark up", group: "shapes", n: 1, tip: "An up arrow with its tip at the point",
    render: (c, o) => drawMarker(c, o, 1) },
  arrowdown: { name: "Arrow mark down", group: "shapes", n: 1, tip: "A down arrow with its tip at the point",
    render: (c, o) => drawMarker(c, o, -1) },
  rect: { name: "Rectangle", group: "shapes", n: 2, tip: "A box between two corners",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const [a, b] = c.P, q = [a, { x: b.x, y: a.y }, b, { x: a.x, y: b.y }];
      o.area(q); o.poly(q, "dw-line", true);
    } },
  circle: { name: "Circle", group: "shapes", n: 2, tip: "A circle about the first point through the second (on screen)",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const r = Math.hypot(c.P[1].x - c.P[0].x, c.P[1].y - c.P[0].y);
      o.raw(`<circle class="dw-fill" cx="${c.P[0].x.toFixed(1)}" cy="${c.P[0].y.toFixed(1)}" r="${r.toFixed(1)}"/>`);
      o.circle(c.P[0].x, c.P[0].y, r);
    } },
  ellipse: { name: "Ellipse", group: "shapes", n: 2, tip: "An ellipse inside the box between two corners",
    render: (c, o) => {
      if (c.P.length < 2) return;
      const [a, b] = c.P, cx = (a.x + b.x) / 2, cy = (a.y + b.y) / 2, rx = Math.abs(b.x - a.x) / 2, ry = Math.abs(b.y - a.y) / 2;
      o.raw(`<ellipse class="dw-fill" cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" rx="${rx.toFixed(1)}" ry="${ry.toFixed(1)}"/>`);
      o.ellipse(cx, cy, rx, ry);
    } },
  triangle: { name: "Triangle", group: "shapes", n: 3, tip: "Three corners",
    render: (c, o) => { if (c.P.length >= 3) o.area(c.P); o.poly(c.P, "dw-line", c.P.length >= 3); } },
  path: { name: "Path", group: "shapes", n: "path", tip: "Click each point; double-click or press Enter to finish",
    render: (c, o) => { if (c.P.length > 1) { o.poly(c.P); o.head(c.P[c.P.length - 2], c.P[c.P.length - 1]); } } },
  curve: { name: "Curve", group: "shapes", n: 3, tip: "From the first point to the second, bent toward the third",
    render: (c, o) => {
      if (c.P.length < 3) return drawZig(c, o, "dw-line dw-dash");
      const pts = drawBezier(c.P[0], c.P[2], c.P[1]);
      for (let i = 1; i < pts.length; i++) o.segs.push([pts[i - 1].x, pts[i - 1].y, pts[i].x, pts[i].y]);
      o.raw(`<path class="dw-line" d="M${c.P[0].x.toFixed(1)},${c.P[0].y.toFixed(1)} Q${c.P[2].x.toFixed(1)},${c.P[2].y.toFixed(1)} ${c.P[1].x.toFixed(1)},${c.P[1].y.toFixed(1)}"/>`);
    } },
};

/** Long and short positions: the target box (reward) and the stop box
 *  (risk) from the entry to the second point's time. */
function drawPositionTool(c, o, side) {
  const E = c.P[0];
  if (c.P.length < 2) return;
  const xEnd = c.P[1].x === E.x ? E.x + 60 : c.P[1].x;
  const yT = c.P[1].y;
  const target = c.D[1].p;
  const hasStop = c.P.length >= 3;
  const stop = hasStop ? c.D[2].p : c.D[0].p - (target - c.D[0].p) / 2;
  const yS = c.A.y(stop);
  const box = (y, cls) => {
    const q = [{ x: E.x, y: E.y }, { x: xEnd, y: E.y }, { x: xEnd, y }, { x: E.x, y }];
    o.area(q, cls); o.poly(q, `${cls}-edge`, true);
  };
  box(yT, "dw-target");
  box(yS, "dw-stop");
  o.line(E, { x: xEnd, y: E.y }, "dw-line dw-entry");
  const st = drawPosition(side, c.D[0].p, target, stop);
  const xm = (E.x + xEnd) / 2;
  const upFirst = yT < yS;
  o.tag(xm, yT + (upFirst ? -10 : 18), `Target ${c.A.fmtPrice(target)} (${drawSigned(st.rewardPct)}%)`, "dw-tag dw-tag-pos");
  o.tag(xm, yS + (upFirst ? 18 : -10), `Stop ${c.A.fmtPrice(stop)} (${drawSigned(-st.riskPct)}%)${hasStop ? "" : " · click to set"}`, "dw-tag dw-tag-neg");
  o.tag(xm, E.y + (upFirst ? 14 : -8), `${side === "long" ? "Long" : "Short"} ${c.A.fmtPrice(c.D[0].p)} · R/R ${st.ratio === null ? "—" : st.ratio.toFixed(2)}`);
}

/** Price, date, or date-and-price range: a box with its arrows and label. */
function drawRangeTool(c, o, price, date) {
  if (c.P.length < 2) return;
  const [a, b] = c.P, q = [a, { x: b.x, y: a.y }, b, { x: a.x, y: b.y }];
  o.area(q, "dw-range");
  const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
  const bits = [];
  if (price) {
    o.line({ x: mx, y: a.y }, { x: mx, y: b.y }, "dw-line");
    o.head({ x: mx, y: a.y }, { x: mx, y: b.y }, 7);
    const dp = c.D[1].p - c.D[0].p;
    bits.push(`${drawSigned(dp)} (${drawSigned((dp / c.D[0].p) * 100)}%)`);
  }
  if (date) {
    o.line({ x: a.x, y: my }, { x: b.x, y: my }, "dw-line");
    o.head({ x: a.x, y: my }, { x: b.x, y: my }, 7);
    bits.push(`${Math.round(c.D[1].s - c.D[0].s)} bars · ${drawSpan(c.D[1].t - c.D[0].t)}`);
  }
  o.tag(mx, Math.max(a.y, b.y) + 16, bits.join(" · "));
}

/** An arrow marker with its tip at the point: dir 1 up (drawn below), -1 down. */
function drawMarker(c, o, dir) {
  const { x, y } = c.P[0];
  const s = dir;
  const pts = [{ x, y }, { x: x + 8, y: y + 9 * s }, { x: x + 3.5, y: y + 9 * s }, { x: x + 3.5, y: y + 20 * s },
    { x: x - 3.5, y: y + 20 * s }, { x: x - 3.5, y: y + 9 * s }, { x: x - 8, y: y + 9 * s }];
  o.area(pts, dir > 0 ? "dw-mark-up" : "dw-mark-dn");
}

/** The toolbar's groups, in order, with the tools each offers. */
const DRAW_GROUPS = [
  { key: "cursor", label: "Cursors" },
  { key: "lines", label: "Lines, channels and pitchforks" },
  { key: "fib", label: "Fibonacci and Gann" },
  { key: "patterns", label: "Patterns, Elliott waves and cycles" },
  { key: "measure", label: "Forecasting, volume-based and measures" },
  { key: "shapes", label: "Brushes, arrows and shapes" },
];

/** Inline icons, 18x18, drawn in currentColor so both themes hold. */
const DRAW_ICONS = {
  cross: '<path d="M9 2v14M2 9h14"/>',
  dot: '<circle cx="9" cy="9" r="2.6" fill="currentColor"/>',
  arrow: '<path d="M4 2l10 7-4.2 1L12 15l-1.8.8-2.2-5L5 13z" fill="currentColor" stroke="none"/>',
  eraser: '<path d="M3 12l6-7 6 5-5 6H6z"/><path d="M6.5 8.8l5 4.2"/>',
  lines: '<path d="M3 15L15 3"/><circle cx="3" cy="15" r="1.6"/><circle cx="15" cy="3" r="1.6"/>',
  fib: '<path d="M2 3h14M2 7h10M2 10.5h14M2 15h8"/>',
  patterns: '<path d="M1.5 13l3.5-7 3 5 3-8 2.5 6 3-3"/>',
  measure: '<path d="M2 13L13 2l3 3L5 16z"/><path d="M5.5 9.5l1.5 1.5M8 7l1.5 1.5M10.5 4.5L12 6"/>',
  shapes: '<path d="M3 15c3-1 4-4 6-6l3-3 2 2-3 3c-2 2-5 3-8 4z"/><path d="M12 4l2-2 2 2-2 2"/>',
  magnet: '<path d="M4 3v6a5 5 0 0010 0V3"/><path d="M4 6h3M11 6h3"/>',
  lock: '<rect x="3.5" y="8" width="11" height="8" rx="1.5"/><path d="M6 8V5.5a3 3 0 016 0V8"/>',
  unlock: '<rect x="3.5" y="8" width="11" height="8" rx="1.5"/><path d="M6 8V5.5a3 3 0 015.8-1"/>',
  eye: '<path d="M1.5 9S4.5 3.5 9 3.5 16.5 9 16.5 9 13.5 14.5 9 14.5 1.5 9 1.5 9z"/><circle cx="9" cy="9" r="2.3"/>',
  eyeoff: '<path d="M1.5 9S4.5 3.5 9 3.5 16.5 9 16.5 9 13.5 14.5 9 14.5 1.5 9 1.5 9z"/><path d="M3 15L15 3"/>',
  trash: '<path d="M3 5h12M7 5V3h4v2M5 5l1 11h6l1-11"/>',
};
const drawIcon = (k) => `<svg viewBox="0 0 18 18" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">${DRAW_ICONS[k] || ""}</svg>`;

/* ===================================================================== */
/* State, storage, rendering and interaction (browser only)              */
/* ===================================================================== */

const DRAW_STORE_KEY = "gexdash-drawings";
const DRAW_PREFS_KEY = "gexdash-draw-prefs";

const draw = {
  tool: "cross",           // the active tool; cursor tools are cross, dot, arrow, eraser
  cursor: "cross",         // the cursor to return to after a drawing is placed
  lock: false, hide: false, magnet: false,
  store: {},               // symbol -> [{id, tool, pts: [{t, p}]}]
  charts: {},              // chart key -> {A, layer, hits}
  active: null,            // the drawing being placed
  drag: null,              // a drawing or one of its points being moved
  selected: null,          // {symbol, id}
  armed: 0,                // Remove all asks twice; the time of the first click
  ready: false,
};

/** Read drawings and preferences. Storage can be blocked or hold garbage;
 *  either way the result is "nothing saved", never a crash. */
function drawLoad() {
  try {
    const raw = JSON.parse(localStorage.getItem(DRAW_STORE_KEY) || "{}");
    if (raw && typeof raw === "object") {
      for (const [sym, list] of Object.entries(raw)) {
        if (!Array.isArray(list)) continue;
        draw.store[sym] = list.filter((d) => d && DRAW_TOOLS[d.tool] && Array.isArray(d.pts) &&
          d.pts.every((p) => Number.isFinite(p?.t) && Number.isFinite(p?.p)));
      }
    }
  } catch { draw.store = {}; }
  try {
    const prefs = JSON.parse(localStorage.getItem(DRAW_PREFS_KEY) || "{}");
    draw.lock = !!prefs.lock; draw.hide = !!prefs.hide; draw.magnet = !!prefs.magnet;
    if (DRAW_TOOLS[prefs.cursor]?.cursor && prefs.cursor !== "eraser") draw.cursor = draw.tool = prefs.cursor;
  } catch { /* defaults */ }
}

/** Persist drawings and preferences; a blocked store is skipped. */
function drawSave() {
  try {
    const compact = {};
    for (const [sym, list] of Object.entries(draw.store)) if (list.length) compact[sym] = list;
    localStorage.setItem(DRAW_STORE_KEY, JSON.stringify(compact));
    localStorage.setItem(DRAW_PREFS_KEY, JSON.stringify({ lock: draw.lock, hide: draw.hide, magnet: draw.magnet, cursor: draw.cursor }));
  } catch { /* not remembered */ }
}

/** The drawings of a symbol (a live array). */
const drawList = (symbol) => (draw.store[symbol] ||= []);

/**
 * Called by a chart after every render with its geometry:
 *   host, svg, symbol, plot {l, r, t, b}, x0, lo, bw (x = x0 + (slot - lo + 0.5) * bw),
 *   yLo, yHi (the price scale over plot.t..plot.b), stamps (bar times),
 *   kind (minute | daily | weekly | monthly), step (bar seconds intraday),
 *   bars {open, high, low, close, volume}, fmtPrice(p), fmtTime(stamp or seconds).
 * Builds the adapter and draws the layer into the new SVG.
 */
function drawAttach(key, g) {
  const prev = draw.charts[key];
  // A drawing half-placed on another symbol does not carry over.
  if (draw.active?.chart === key && draw.active.symbol !== g.symbol) draw.active = null;
  const timeline = prev && prev.stamps === g.stamps && prev.kind === g.kind ? prev.timeline : drawTimeline(g.stamps, g.kind, g.step);
  const span = (g.plot.b - g.plot.t) || 1, range = (g.yHi - g.yLo) || 1;
  const A = {
    key, symbol: g.symbol, plot: g.plot, bars: g.bars, timeline, svg: g.svg, host: g.host,
    x: (s) => g.x0 + (s - g.lo + 0.5) * g.bw,
    slotOfX: (x) => (x - g.x0) / g.bw + g.lo - 0.5,
    y: (p) => g.plot.b - ((p - g.yLo) / range) * span,
    priceOfY: (y) => g.yLo + ((g.plot.b - y) / span) * range,
    fmtPrice: g.fmtPrice,
    fmtTime: (t) => g.fmtTime(typeof t === "number" ? new Date(t * 1000).toISOString().slice(0, 19) : t),
  };
  draw.charts[key] = { A, stamps: g.stamps, kind: g.kind, timeline, layer: null, hits: [] };
  drawRender(key);
}

/** A data point {t, p, s} from an SVG point: the slot snapped to a bar (or
 *  left free for a brush), the price free or, with Magnet, the nearest of
 *  that bar's open, high, low and close. */
function drawPointAt(A, x, y, free = false) {
  let s = A.slotOfX(x);
  if (!free) s = Math.round(s);
  let p = A.priceOfY(y);
  if (draw.magnet && !free) {
    const i = Math.round(s), b = A.bars;
    const ohlc = i >= 0 && i < b.close.length ? [b.open[i], b.high[i], b.low[i], b.close[i]].filter(Number.isFinite) : [];
    if (ohlc.length) p = ohlc.reduce((best, v) => (Math.abs(v - p) < Math.abs(best - p) ? v : best));
  }
  return { t: A.timeline.timeOf(s), p, s };
}

/** Render one drawing into an output builder; returns it with its pixel points. */
function drawOne(A, d, extra = null) {
  const D = d.pts.map((q) => ({ t: q.t, p: q.p, s: A.timeline.slotOf(q.t) }));
  if (extra) D.push(extra);
  const P = D.map((q) => ({ x: A.x(q.s), y: A.y(q.p) }));
  const o = drawOut();
  const tool = DRAW_TOOLS[d.tool];
  if (P.length === 1 && tool.n !== 1) o.circle(P[0].x, P[0].y, 3, "dw-dot");
  else tool.render?.({ P, D, box: A.plot, A }, o);
  return { o, P };
}

/** Redraw the drawing layer of one chart (or every chart). Builds a fresh
 *  <g> just under the chart's crosshair, clipped to the price plot, and
 *  records each drawing's clickable geometry. */
function drawRender(key) {
  if (!key) return Object.keys(draw.charts).forEach(drawRender);
  const ch = draw.charts[key];
  if (!ch) return;
  const { A } = ch;
  ch.hits = [];
  const svg = A.svg;
  if (!svg || !svg.isConnected) return;
  svg.querySelector(":scope > g.draw-layer")?.remove();
  svg.querySelector(`#dclip-${key}`)?.remove();
  if (draw.hide) return;
  const NS = "http://www.w3.org/2000/svg";
  const clip = document.createElementNS(NS, "clipPath");
  clip.id = `dclip-${key}`;
  clip.innerHTML = `<rect x="${A.plot.l}" y="${A.plot.t}" width="${A.plot.r - A.plot.l}" height="${A.plot.b - A.plot.t}"/>`;
  const layer = document.createElementNS(NS, "g");
  layer.setAttribute("class", `draw-layer${draw.lock ? " locked" : ""}`);
  layer.setAttribute("clip-path", `url(#dclip-${key})`);
  const out = [];
  for (const d of drawList(A.symbol)) {
    const { o, P } = drawOne(A, d);
    ch.hits.push({ id: d.id, segs: o.segs, fills: o.fills, P });
    const sel = draw.selected?.id === d.id && draw.selected.symbol === A.symbol;
    out.push(`<g class="drw${sel ? " sel" : ""}" data-draw="${d.id}">${o.parts.join("")}</g>`);
    if (sel && !draw.lock) out.push(P.length <= 12 ? P.map((p) => `<circle class="dw-handle" cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="4.5"/>`).join("") : "");
  }
  const act = draw.active;
  if (act && act.chart === key) {
    const { o } = drawOne(A, act, act.hover && !DRAW_TOOLS[act.tool].free ? act.hover : null);
    out.push(`<g class="drw preview">${o.parts.join("")}</g>`);
  }
  layer.innerHTML = out.join("");
  const before = svg.querySelector(":scope > [data-txhair], :scope > [data-xhair]");
  svg.insertBefore(clip, svg.firstChild);
  svg.insertBefore(layer, before || null);
  ch.layer = layer;
}

/** The SVG point under a pointer event, in the chart's own coordinates. */
function drawSvgPoint(A, ev) {
  const r = A.svg.getBoundingClientRect(), vb = A.svg.viewBox.baseVal;
  return { x: ((ev.clientX - r.left) / r.width) * vb.width, y: ((ev.clientY - r.top) / r.height) * vb.height };
}

const drawInside = (A, pt) => pt.x >= A.plot.l && pt.x <= A.plot.r && pt.y >= A.plot.t && pt.y <= A.plot.b;

/** The topmost drawing under a point, and which of its points if one is
 *  within reach: {id, handle (index, or -1 for the body)}. */
function drawHitTest(key, pt) {
  const ch = draw.charts[key];
  if (!ch) return null;
  for (let k = ch.hits.length - 1; k >= 0; k--) {
    const h = ch.hits[k];
    const hi = h.P.length <= 12 ? h.P.findIndex((p) => Math.hypot(p.x - pt.x, p.y - pt.y) <= 7) : -1;
    if (hi >= 0) return { id: h.id, handle: hi };
  }
  // A line wins over the inside of another drawing's shading: a trend line
  // drawn across a channel must still be clickable.
  for (let k = ch.hits.length - 1; k >= 0; k--) {
    const h = ch.hits[k];
    if (h.segs.some((s) => drawSegDist(pt.x, pt.y, ...s) <= 6)) return { id: h.id, handle: -1 };
  }
  for (let k = ch.hits.length - 1; k >= 0; k--) {
    const h = ch.hits[k];
    if (h.fills.some((f) => drawInPoly(pt.x, pt.y, f))) return { id: h.id, handle: -1 };
  }
  return null;
}

/** Finish the drawing being placed: store it, select it, go back to the cursor. */
function drawFinish() {
  const act = draw.active;
  if (!act) return;
  draw.active = null;
  const tool = DRAW_TOOLS[act.tool];
  let pts = act.pts;
  if (tool.n === "free") {
    // Thin the stroke on screen before storing it: a brush stroke is hundreds of pointer moves.
    const A = draw.charts[act.chart].A;
    const px = pts.map((q) => ({ x: A.x(A.timeline.slotOf(q.t)), y: A.y(q.p) }));
    pts = drawThin(px, 0.8).map((i) => pts[i]);
  }
  if (pts.length >= (typeof tool.n === "number" ? tool.n : 2)) {
    const d = { id: `d${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`, tool: act.tool, pts: pts.map(({ t, p }) => ({ t, p })) };
    drawList(act.symbol).push(d);
    draw.selected = { symbol: act.symbol, id: d.id };
    drawSave();
  }
  drawSetTool(draw.cursor);
}

/** Delete one drawing by id from a symbol. */
function drawDelete(symbol, id) {
  const list = drawList(symbol);
  const i = list.findIndex((d) => d.id === id);
  if (i >= 0) list.splice(i, 1);
  if (draw.selected?.id === id) draw.selected = null;
  drawSave();
}

/** The pointer handlers for one chart host, attached once, in the capture
 *  phase so a drawing click never also starts the chart's pan. */
function drawBoot() {
  if (draw.ready) return;
  draw.ready = true;
  drawLoad();
  document.addEventListener("keydown", drawKey);
  document.addEventListener("pointerdown", (ev) => {
    if (!ev.target.closest?.(".draw-menu, .draw-group")) drawCloseMenus();
  });
}

function drawInit(key, host) {
  drawBoot();
  host.addEventListener("pointerdown", (ev) => {
    const ch = draw.charts[key];
    if (!ch || ev.button !== 0) return;
    const A = ch.A, pt = drawSvgPoint(A, ev);
    const tool = DRAW_TOOLS[draw.tool];
    if (!tool.cursor) {
      if (!drawInside(A, pt) && !draw.active) return;
      ev.stopPropagation(); ev.preventDefault();
      host.setPointerCapture?.(ev.pointerId);
      if (draw.hide) { draw.hide = false; drawSyncBars(); }
      const point = drawPointAt(A, pt.x, pt.y, tool.n === "free");
      if (!draw.active || draw.active.chart !== key) {
        draw.selected = null;
        draw.active = { chart: key, symbol: A.symbol, tool: draw.tool, pts: [point], hover: point, down: pt };
        if (tool.n === 1) return drawFinish();
      } else {
        draw.active.pts.push(point);
        draw.active.down = pt;
        if (typeof tool.n === "number" && draw.active.pts.length >= tool.n) return drawFinish();
      }
      return drawRender(key);
    }
    if (draw.lock || draw.hide) return;
    const hit = drawHitTest(key, pt);
    if (draw.tool === "eraser") {
      if (!hit) return;
      ev.stopPropagation(); ev.preventDefault();
      drawDelete(A.symbol, hit.id);
      return drawRender();
    }
    if (!hit) {
      if (draw.selected) { draw.selected = null; drawRender(); drawSyncBars(); }
      return;                              // the chart pans as usual
    }
    ev.stopPropagation(); ev.preventDefault();
    host.setPointerCapture?.(ev.pointerId);
    const d = drawList(A.symbol).find((q) => q.id === hit.id);
    draw.selected = { symbol: A.symbol, id: hit.id };
    draw.drag = { chart: key, id: hit.id, handle: hit.handle, start: drawPointAt(A, pt.x, pt.y, true),
      orig: d.pts.map((q) => ({ ...q, s: A.timeline.slotOf(q.t) })) };
    drawRender(); drawSyncBars();
  }, true);

  host.addEventListener("pointermove", (ev) => {
    const ch = draw.charts[key];
    if (!ch) return;
    const A = ch.A, pt = drawSvgPoint(A, ev);
    const act = draw.active;
    if (act && act.chart === key) {
      const tool = DRAW_TOOLS[act.tool];
      if (tool.n === "free") {
        if (!(ev.buttons & 1)) return;
        const last = act.pts[act.pts.length - 1];
        const lx = A.x(A.timeline.slotOf(last.t)), ly = A.y(last.p);
        if (Math.hypot(pt.x - lx, pt.y - ly) >= 2) act.pts.push(drawPointAt(A, pt.x, pt.y, true));
      } else {
        act.hover = drawPointAt(A, pt.x, pt.y);
      }
      drawRender(key);
      return;                              // the crosshair still follows
    }
    const drag = draw.drag;
    if (drag && drag.chart === key) {
      ev.stopPropagation();
      const d = drawList(A.symbol).find((q) => q.id === drag.id);
      if (!d) { draw.drag = null; return; }
      if (drag.handle >= 0) {
        const q = drawPointAt(A, pt.x, pt.y);
        d.pts[drag.handle] = { t: q.t, p: q.p };
      } else {
        const now = drawPointAt(A, pt.x, pt.y, true);
        const ds = Math.round(now.s - drag.start.s), dp = now.p - drag.start.p;
        d.pts = drag.orig.map((q) => ({ t: A.timeline.timeOf(q.s + ds), p: q.p + dp }));
      }
      drawRender();
    }
  }, true);

  host.addEventListener("pointerup", (ev) => {
    const ch = draw.charts[key];
    if (!ch) return;
    const act = draw.active;
    if (act && act.chart === key) {
      const tool = DRAW_TOOLS[act.tool];
      ev.stopPropagation();
      if (tool.n === "free") return drawFinish();
      // Press, drag and release places the next point where it was let go.
      const pt = drawSvgPoint(ch.A, ev);
      if (typeof tool.n === "number" && act.down && Math.hypot(pt.x - act.down.x, pt.y - act.down.y) > 6) {
        act.pts.push(drawPointAt(ch.A, pt.x, pt.y));
        act.down = null;
        if (act.pts.length >= tool.n) return drawFinish();
        drawRender(key);
      }
      return;
    }
    if (draw.drag && draw.drag.chart === key) {
      ev.stopPropagation();
      draw.drag = null;
      drawSave();
    }
  }, true);

  host.addEventListener("dblclick", (ev) => {
    if (draw.active?.chart === key && DRAW_TOOLS[draw.active.tool].n === "path") {
      ev.stopPropagation();
      // The double-click's two presses each added the same point.
      const pts = draw.active.pts;
      while (pts.length > 1 && Math.abs(pts[pts.length - 1].s - pts[pts.length - 2].s) < 0.5 &&
        Math.abs(pts[pts.length - 1].p - pts[pts.length - 2].p) < 1e-9) pts.pop();
      drawFinish();
    }
  }, true);
}

/** Escape cancels a drawing or clears the selection; Enter finishes a path;
 *  Delete or Backspace removes the selected drawing. Never while typing. */
function drawKey(ev) {
  const t = ev.target;
  if (t && (t.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(t.tagName))) return;
  if (ev.key === "Escape") {
    if (draw.active) { draw.active = null; drawSetTool(draw.cursor); }
    else if (draw.selected) { draw.selected = null; drawRender(); drawSyncBars(); }
    drawCloseMenus();
  } else if (ev.key === "Enter" && draw.active && DRAW_TOOLS[draw.active.tool].n === "path") {
    drawFinish();
  } else if ((ev.key === "Delete" || ev.key === "Backspace") && draw.selected && !draw.lock) {
    ev.preventDefault();
    drawDelete(draw.selected.symbol, draw.selected.id);
    drawRender(); drawSyncBars();
  }
}

/** Choose a tool. A cursor tool is remembered as the one to return to. */
function drawSetTool(key) {
  if (!DRAW_TOOLS[key]) return;
  if (draw.active && draw.active.tool !== key) draw.active = null;
  draw.tool = key;
  if (DRAW_TOOLS[key].cursor && key !== "eraser") draw.cursor = key;
  if (!DRAW_TOOLS[key].cursor) draw.selected = null;
  drawSave();
  drawSyncBars();
  drawRender();
}

/** The pointer on a chart's frame (the toolbar's wrapper, so the Terminal's
 *  indicator panes follow too): which cursor shows, and in arrow mode that
 *  the crosshair lines are hidden. styles.css reads data-draw-cursor. */
function drawSyncFrame(frame) {
  const t = DRAW_TOOLS[draw.tool];
  frame.dataset.drawCursor = t.cursor ? draw.tool : "draw";
}

/* ------------------------------------------------------------- toolbar */

const DRAW_BARS = [];

/** Build the vertical toolbar into `el` for the chart drawn in `host`;
 *  `frame` wraps both and carries the cursor. Both charts' toolbars share one
 *  state, and every button says what it does in its tooltip. */
function drawToolbar(el, host, frame) {
  drawBoot();
  DRAW_BARS.push({ el, host, frame });
  const groupBtn = (g) => `<div class="draw-group" data-group="${g.key}">
      <button class="draw-btn" data-open="${g.key}" title="${drawEsc(g.label)}" aria-haspopup="true">${drawIcon(g.key === "cursor" ? "cross" : g.key)}</button>
      <div class="draw-menu" role="menu" hidden>
        <div class="draw-menu-head">${drawEsc(g.label)}</div>
        ${Object.entries(DRAW_TOOLS).filter(([, t]) => t.group === g.key).map(([k, t]) =>
          `<button role="menuitemradio" data-tool="${k}" title="${drawEsc(t.tip)}">${t.cursor ? drawIcon(k) : ""}<span>${drawEsc(t.name)}</span></button>`).join("")}
      </div></div>`;
  el.innerHTML = DRAW_GROUPS.map(groupBtn).join("") + `<div class="draw-sep"></div>
    <button class="draw-btn" data-act="magnet" title="Magnet: snap points to the nearest open, high, low or close">${drawIcon("magnet")}</button>
    <button class="draw-btn" data-act="lock" title="Lock all drawings: they can no longer be selected, moved or erased">${drawIcon("lock")}</button>
    <button class="draw-btn" data-act="hide" title="Hide all drawings">${drawIcon("eye")}</button>
    <button class="draw-btn" data-act="remove" title="Remove the selected drawing; with none selected, remove all of this symbol's (click twice)">${drawIcon("trash")}</button>`;
  el.addEventListener("click", (ev) => {
    const b = ev.target.closest("button");
    if (!b) return;
    if (b.dataset.open) {
      const menu = b.parentElement.querySelector(".draw-menu");
      const was = !menu.hidden;
      drawCloseMenus();
      menu.hidden = was;
      return;
    }
    if (b.dataset.tool) { drawCloseMenus(); return drawSetTool(b.dataset.tool); }
    const act = b.dataset.act;
    if (act === "magnet") draw.magnet = !draw.magnet;
    if (act === "lock") { draw.lock = !draw.lock; if (draw.lock) draw.selected = null; }
    if (act === "hide") { draw.hide = !draw.hide; if (draw.hide) { draw.selected = null; draw.active = null; } }
    if (act === "remove") drawRemove(host);
    drawSave(); drawSyncBars(); drawRender();
  });
  drawSyncBars();
}

/** The Remove button: the selected drawing, or after a second click within
 *  four seconds, every drawing of the symbol on this chart. No browser
 *  confirm(): it blocks the page. */
function drawRemove(host) {
  if (draw.lock) return;
  if (draw.selected) { drawDelete(draw.selected.symbol, draw.selected.id); return; }
  const ch = Object.values(draw.charts).find((c) => c.A.host === host);
  if (!ch) return;
  if (Date.now() - draw.armed < 4000) {
    draw.store[ch.A.symbol] = [];
    draw.armed = 0;
  } else {
    draw.armed = drawList(ch.A.symbol).length ? Date.now() : 0;
    setTimeout(drawSyncBars, 4100);
  }
}

function drawCloseMenus() {
  document.querySelectorAll(".draw-menu").forEach((m) => (m.hidden = true));
}

/** Bring every toolbar in line with the state: pressed buttons, the group
 *  icon showing its chosen tool, counts and the Remove button's prompt. */
function drawSyncBars() {
  for (const { el, host, frame } of DRAW_BARS) {
    drawSyncFrame(frame);
    el.querySelectorAll(".draw-group").forEach((g) => {
      const key = g.dataset.group;
      const on = DRAW_TOOLS[draw.tool].group === key;
      // The cursor group shows the cursor in use; the others their group's icon.
      const icon = key !== "cursor" ? key : DRAW_TOOLS[draw.tool].cursor ? draw.tool : draw.cursor;
      const btn = g.querySelector("[data-open]");
      btn.classList.toggle("on", on && key !== "cursor" || key === "cursor" && draw.tool === "eraser");
      btn.innerHTML = drawIcon(icon);
      g.querySelectorAll("[data-tool]").forEach((t) => t.setAttribute("aria-checked", String(t.dataset.tool === draw.tool)));
    });
    const set = (act, on, icon, title) => {
      const b = el.querySelector(`[data-act="${act}"]`);
      b.classList.toggle("on", on);
      if (icon) b.innerHTML = drawIcon(icon);
      if (title) b.title = title;
    };
    set("magnet", draw.magnet);
    set("lock", draw.lock, draw.lock ? "lock" : "unlock", draw.lock ? "Unlock all drawings" : "Lock all drawings: they can no longer be selected, moved or erased");
    set("hide", draw.hide, draw.hide ? "eyeoff" : "eye", draw.hide ? "Show all drawings" : "Hide all drawings");
    const ch = Object.values(draw.charts).find((c) => c.A.host === host);
    const count = ch ? drawList(ch.A.symbol).length : 0;
    const armed = Date.now() - draw.armed < 4000;
    const rm = el.querySelector('[data-act="remove"]');
    rm.classList.toggle("armed", armed && !draw.selected);
    rm.disabled = draw.lock || (!draw.selected && !count);
    rm.title = draw.selected ? "Remove the selected drawing (or press Delete)"
      : armed ? `Click again to remove all ${count} drawing${count === 1 ? "" : "s"} of this symbol`
      : `Remove all ${count} drawing${count === 1 ? "" : "s"} of this symbol (click twice)`;
  }
}
