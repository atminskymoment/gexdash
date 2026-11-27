"""The drawing tools' geometry and measures (static/draw.js), under Node.

Every number a drawing shows -- a Fibonacci level, a pitchfork's median, a
Gann angle, a regression channel, an anchored VWAP, a volume profile, a
position's reward and risk -- is computed in draw.js, so it is pinned here
against values worked by hand, as the server's indicators are in Python. So
is the slot <-> time mapping every drawing is stored through, including the
rule for dates past the last bar, which must match the server's
(cycles.future_stamps) so a drawing in the future sits on the same date with
the Moon/Sun layer on or off. Skipped where Node is not installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

DRAW_JS = Path(__file__).resolve().parents[1] / "gexdash" / "static" / "draw.js"
NODE = shutil.which("node")


def run_js(checks: str) -> None:
    if not NODE:
        pytest.skip("node is not installed")
    script = DRAW_JS.read_text() + """
const eq = (got, want, what) => {
  if (JSON.stringify(got) !== JSON.stringify(want))
    throw new Error(`${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
};
const near = (got, want, what, tol = 1e-9) => {
  if (!(Math.abs(got - want) <= tol)) throw new Error(`${what}: got ${got}, want ${want}`);
};
const day = (t) => new Date(t * 1000).toISOString().slice(0, 10);
""" + checks + '\nconsole.log("ok");\n'
    r = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr or r.stdout


def test_the_file_loads_without_a_browser():
    run_js("eq(typeof drawTimeline, 'function', 'loaded'); eq(draw.ready, false, 'nothing started');")


# ----------------------------------------------------------------------
# Time and slots
# ----------------------------------------------------------------------


def test_future_dates_follow_the_servers_rule():
    run_js("""
const fri = drawSeconds("2026-11-27T05:00:00");              // a Friday's daily bar
eq([1, 2, 5, 6].map((k) => day(drawFutureTime(fri, "daily", k))),
   ["2026-11-30", "2026-12-01", "2026-12-04", "2026-12-07"], "weekdays, weekends skipped");
eq(new Date(drawFutureTime(fri, "daily", 1) * 1000).toISOString().slice(11, 19), "00:00:00", "dated at midnight UTC");
eq(day(drawFutureTime(drawSeconds("2026-11-23T00:00:00"), "weekly", 2)), "2026-12-07", "two weeks on");
const jan31 = drawSeconds("2027-01-31T00:00:00");
eq([1, 2].map((k) => day(drawFutureTime(jan31, "monthly", k))), ["2027-02-28", "2027-03-31"], "months, day clamped");
near(drawFutureTime(1000, "minute", 3, 300), 1900, "intraday: bar size steps");
""")


def test_a_drawing_keeps_its_bar_spacing_when_a_shorter_range_loads():
    # A trend line drawn on a year of daily bars, with one point nine months
    # back, then viewed on three months: the point is now before the loaded
    # bars, and must sit as many weekdays back as it did.
    run_js("""
const days = [];
for (let t = Date.UTC(2026, 0, 5); t <= Date.UTC(2026, 11, 31); t += 86400000) {
  const d = new Date(t); if (d.getUTCDay() !== 0 && d.getUTCDay() !== 6) days.push(d.toISOString().slice(0, 10) + "T05:00:00");
}
const year = drawTimeline(days, "daily", 0), quarter = drawTimeline(days.slice(-63), "daily", 0);
const a = drawSeconds(days[40]), b = drawSeconds(days[days.length - 5]);
near(quarter.slotOf(b) - quarter.slotOf(a), year.slotOf(b) - year.slotOf(a), "weekdays between the points", 1e-9);
near(quarter.timeOf(quarter.slotOf(a)), a, "and back to the same time", 1e-6);
// Weekly and monthly run back the same way.
const wk = drawTimeline(["2026-11-02T00:00:00", "2026-11-09T00:00:00"], "weekly", 0);
eq(day(wk.timeOf(-3)), "2026-10-12", "three weeks back");
const mo = drawTimeline(["2026-03-31T00:00:00", "2026-04-30T00:00:00"], "monthly", 0);
eq(day(mo.timeOf(-1)), "2026-02-28", "a month back, day clamped");
""")


def test_slots_and_times_invert_inside_past_and_before_the_bars():
    run_js("""
const stamps = ["2026-11-23T05:00:00", "2026-11-24T05:00:00", "2026-11-25T05:00:00",
                "2026-11-26T05:00:00", "2026-11-27T05:00:00"];
const tl = drawTimeline(stamps, "daily", 0);
eq(tl.n, 5, "bars");
near(tl.timeOf(2), drawSeconds(stamps[2]), "a bar's own time");
near(tl.slotOf(drawSeconds(stamps[3])), 3, "and back");
near(tl.timeOf(1.5), (drawSeconds(stamps[1]) + drawSeconds(stamps[2])) / 2, "between bars, linear");
eq(day(tl.timeOf(5)), "2026-11-30", "first slot past the last bar: the next weekday");
eq(day(tl.timeOf(9)), "2026-12-04", "fifth: that Friday");
eq(new Date(tl.timeOf(-1) * 1000).toISOString(), "2026-11-20T05:00:00.000Z", "before the first bar: the weekday before, same time of day");
eq(day(tl.timeOf(-2)), "2026-11-19", "and the one before that");
for (const s of [-3, -0.5, 0, 0.25, 2.7, 4, 4.4, 5, 7.5, 30, 130.25]) near(tl.slotOf(tl.timeOf(s)), s, `round trip ${s}`, 1e-7);
// Intraday: the bar size, in closed form however far out.
const it = drawTimeline(["2026-11-27T14:30:00", "2026-11-27T14:35:00"], "minute", 300);
near(it.slotOf(it.timeOf(5000)), 5000, "intraday far future");
""")


# ----------------------------------------------------------------------
# Measures
# ----------------------------------------------------------------------


def test_fibonacci_levels_run_from_the_swings_end_back_to_its_start():
    run_js("""
const lv = Object.fromEntries(drawFibLevels(100, 200).map((l) => [l.level, l.price]));
near(lv[0], 200, "0 is the end of the move");
near(lv[1], 100, "1 is its start");
near(lv[0.618], 138.2, "61.8% pullback", 1e-9);
near(lv[1.618], 38.2, "beyond the start", 1e-9);
const ext = Object.fromEntries(drawFibExtension(100, 200, 150).map((l) => [l.level, l.price]));
near(ext[1], 250, "the move again from point 3");
near(ext[1.618], 311.8, "1.618 of it", 1e-9);
eq(drawFibTimeZones(10, 12).slice(0, 7).map((z) => z.slot), [10, 12, 14, 16, 20, 26, 36], "time zones");
""")


def test_pitchfork_origins_and_median_line():
    run_js("""
const a = { x: 0, y: 0 }, b = { x: 10, y: 10 }, c = { x: 10, y: -10 };
const f = drawPitchfork(a, b, c);
eq([f.origin, f.mid, f.dir], [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 0 }], "Andrews: point 1 to the 2-3 midpoint");
eq(drawPitchfork(a, b, c, "schiff").origin, { x: 0, y: 5 }, "Schiff: halfway in price, point 1's time");
eq(drawPitchfork(a, b, c, "modified").origin, { x: 5, y: 5 }, "Modified Schiff: halfway in both");
eq(drawPitchfork(a, b, c, "modified").dir, { x: 5, y: -5 }, "its median runs to the same midpoint");
""")


def test_gann_angles_scale_the_one_by_one():
    run_js("""
const g = Object.fromEntries(drawGannRays({ x: 0, y: 0 }, { x: 10, y: 20 }).map((r) => [r.label, r.slope]));
eq([g["1x1"], g["1x2"], g["2x1"], g["8x1"], g["1x8"]], [2, 1, 4, 16, 0.25], "1x2 is half the 1x1, 2x1 twice it");
""")


def test_regression_line_and_its_deviation():
    run_js("""
const exact = Array.from({ length: 30 }, (_, i) => 2 * i + 3);
const r = drawRegression(exact, 5, 20);
near(r.slope, 2, "slope"); near(r.intercept, 3, "intercept"); near(r.sd, 0, "no residual");
eq([r.from, r.to, r.n], [5, 20, 16], "the bars between the points, inclusive");
const zig = Array.from({ length: 40 }, (_, i) => 10 + (i % 2 ? 1 : -1));
near(drawRegression(zig, 0, 39).sd, 1, "population sigma of +/-1 residuals", 0.01);
eq(drawRegression(exact, 3, 3), null, "one bar is no line");
near(drawRegression(exact, 20, 5).slope, 2, "point order does not matter");
""")


def test_anchored_vwap_from_the_anchor_bar_on():
    run_js("""
const bars = { high: [11, 12, 13, 14], low: [9, 10, 11, 12], close: [10, 11, 12, 13], volume: [100, 100, 300, 0] };
const v = drawAnchoredVwap(bars, 1);
eq(v.map((q) => q.i), [1, 2, 3], "starts at the anchor");
near(v[0].v, 11, "first bar: its typical price");
near(v[1].v, (11 * 100 + 12 * 300) / 400, "volume-weighted");
near(v[2].v, v[1].v, "a bar with no volume leaves it where it was");
""")


def test_volume_profile_conserves_volume_and_finds_its_value_area():
    run_js("""
const n = 60;
const bars = { open: [], high: [], low: [], close: [], volume: [] };
for (let i = 0; i < n; i++) {
  const mid = 100 + 10 * Math.sin(i / 6);
  bars.open.push(mid); bars.close.push(mid + 0.5); bars.high.push(mid + 2); bars.low.push(mid - 2);
  bars.volume.push(1000 + (i % 7) * 150);
}
bars.high[10] = bars.low[10] = bars.close[10];               // one zero-range bar
const vp = drawVolumeProfile(bars, 0, n - 1, 24);
const sum = vp.vol.reduce((s, v) => s + v, 0);
near(sum, vp.total, "every share lands in a bin", 1e-6);
near(vp.total, bars.volume.reduce((s, v) => s + v, 0), "over the bars asked for", 1e-6);
eq(vp.vol.indexOf(Math.max(...vp.vol)), vp.poc, "the point of control is the fullest bin");
const inVA = vp.vol.reduce((s, v, k) => s + (vp.edges[k] >= vp.vaLow - 1e-9 && vp.edges[k + 1] <= vp.vaHigh + 1e-9 ? v : 0), 0);
if (!(inVA >= 0.7 * sum)) throw new Error(`value area holds ${inVA / sum}`);
if (!(vp.vaLow <= vp.pocPrice && vp.pocPrice <= vp.vaHigh)) throw new Error("value area contains the POC");
eq(drawVolumeProfile({ ...bars, volume: bars.volume.map(() => 0) }, 0, n - 1), null, "no volume, no profile");
""")


def test_position_reward_risk_and_ratio():
    run_js("""
const long = drawPosition("long", 100, 110, 95);
eq([long.reward, long.risk, long.ratio], [10, 5, 2], "long");
near(long.rewardPct, 10, "reward percent");
const short = drawPosition("short", 100, 90, 105);
eq([short.reward, short.risk, short.ratio], [10, 5, 2], "short mirrors it");
eq(drawPosition("long", 100, 110, 100).ratio, null, "no risk, no ratio");
""")


def test_hit_testing_and_stroke_thinning():
    run_js("""
near(drawSegDist(5, 3, 0, 0, 10, 0), 3, "perpendicular");
near(drawSegDist(13, 4, 0, 0, 10, 0), 5, "past the end, to the end");
const sq = [{ x: 0, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }];
eq([drawInPoly(5, 5, sq), drawInPoly(15, 5, sq)], [true, false], "inside, outside");
const line = Array.from({ length: 50 }, (_, i) => ({ x: i, y: 2 * i }));
eq(drawThin(line, 0.5), [0, 49], "a straight stroke keeps its ends");
const bent = [...line, { x: 50, y: 0 }];
eq(drawThin(bent, 0.5), [0, 49, 50], "and its corners");
near(drawIntersect({ x: 0, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }, { x: 10, y: 0 }).x, 5, "crossing");
eq(drawIntersect({ x: 0, y: 0 }, { x: 1, y: 1 }, { x: 0, y: 1 }, { x: 1, y: 2 }), null, "parallel");
""")


# ----------------------------------------------------------------------
# Every tool renders, and storage survives garbage
# ----------------------------------------------------------------------


def test_every_tool_renders_clickable_geometry_from_any_point_count():
    run_js("""
const stamps = Array.from({ length: 80 }, (_, i) => new Date(Date.UTC(2026, 0, 1 + i)).toISOString().slice(0, 19));
const close = stamps.map((_, i) => 100 + 10 * Math.sin(i / 5));
const bars = { open: close, high: close.map((c) => c + 1), low: close.map((c) => c - 1), close, volume: close.map(() => 1000) };
const tl = drawTimeline(stamps, "daily", 0);
const A = { plot: { l: 0, r: 800, t: 0, b: 400 }, bars, timeline: tl, x: (s) => s * 8 + 4, y: (p) => 400 - (p - 80) * 10,
  fmtPrice: (p) => p.toFixed(2), fmtTime: (t) => String(t) };
let tools = 0;
for (const [key, t] of Object.entries(DRAW_TOOLS)) {
  if (t.cursor) continue;
  tools += 1;
  const full = typeof t.n === "number" ? t.n : 6;
  for (let k = 1; k <= full; k++) {                    // every stage of placing it
    const pts = Array.from({ length: k }, (_, j) => ({ t: tl.timeOf(10 + 9 * j), p: close[10 + 9 * j] + (j % 2 ? 4 : -4) }));
    const { o } = drawOne(A, { id: key, tool: key, pts });
    if (k === full && !(o.parts.length && o.segs.length + o.fills.length))
      throw new Error(`${key}: nothing drawn or nothing to click`);
  }
}
eq(tools, 53, "every drawing tool");
eq(DRAW_GROUPS.map((g) => g.key), ["cursor", "lines", "fib", "patterns", "measure", "shapes"], "groups");
for (const t of Object.values(DRAW_TOOLS)) if (!DRAW_GROUPS.some((g) => g.key === t.group) || !t.tip) throw new Error(`${t.name}: group or tip`);
""")


def test_storage_survives_garbage_and_drops_malformed_drawings():
    run_js("""
const mem = { "gexdash-drawings": "{not json" };
globalThis.localStorage = { getItem: (k) => mem[k] ?? null, setItem: (k, v) => { mem[k] = v; } };
drawLoad();
eq(draw.store, {}, "corrupt JSON is nothing saved");
mem["gexdash-drawings"] = JSON.stringify({ SPY: [
  { id: "a", tool: "trend", pts: [{ t: 1, p: 2 }, { t: 3, p: 4 }] },
  { id: "b", tool: "no-such-tool", pts: [] },
  { id: "c", tool: "fib", pts: [{ t: "x", p: 1 }] },
], QQQ: "nope" });
mem["gexdash-draw-prefs"] = JSON.stringify({ lock: true, hide: false, magnet: true, cursor: "dot" });
drawLoad();
eq(draw.store.SPY.map((d) => d.id), ["a"], "only well-formed drawings of known tools");
eq(draw.store.QQQ, undefined, "a non-list is ignored");
eq([draw.lock, draw.magnet, draw.tool, draw.cursor], [true, true, "dot", "dot"], "preferences");
drawSave();
eq(JSON.parse(mem["gexdash-drawings"]).SPY.length, 1, "saved back");
globalThis.localStorage = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
drawLoad(); drawSave();                                   // blocked storage: no throw
""")
