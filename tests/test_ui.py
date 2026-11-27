"""The app shell: the stylesheet's themes and the ticker search (static/ui.js).

Two things here fail quietly when they go wrong. A component rule with a
hard-coded colour, or a dark override keyed on [data-theme="dark"] alone,
looks fine with the toggle pressed and breaks for anyone whose dark mode
comes from the operating system: that path sets tokens only. And the search's
suggestion order decides what Enter picks, so a loose match (an index whose
name contains the letters typed) silently picks the wrong symbol.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "gexdash" / "static"
CSS = (STATIC / "styles.css").read_text()
NODE = shutil.which("node")


def _blocks():
    light = CSS[CSS.index(":root {"):CSS.index("/* Dark steps")]
    media = CSS[CSS.index("@media (prefers-color-scheme: dark) {"):CSS.index(':root[data-theme="dark"] {')]
    explicit = CSS[CSS.index(':root[data-theme="dark"] {'):CSS.index("* { box-sizing")]
    return light, media, explicit


def _names(block: str) -> set[str]:
    return set(re.findall(r"(--[a-z0-9-]+):", block))


def test_both_dark_themes_set_the_same_tokens():
    _, media, explicit = _blocks()
    assert _names(media) == _names(explicit)


def test_no_component_rule_depends_on_the_theme_toggle():
    # OS dark mode only swaps tokens, so a rule keyed on [data-theme="dark"]
    # would be skipped for it.
    rest = CSS[CSS.index("* { box-sizing"):]
    assert re.findall(r"[^{}]*data-theme[^{}]*\{", rest) == []


def test_every_themed_shell_token_has_a_dark_value():
    light, media, _ = _blocks()
    shell = {"--surface-1", "--page", "--text-primary", "--text-muted", "--border", "--edge", "--tray",
             "--glow", "--pill", "--btn-bg", "--btn-line", "--focus", "--tip-bg", "--menu-ink",
             "--placeholder", "--switch-off", "--switch-on-thumb", "--kbd-edge"}
    assert shell <= _names(light)
    assert shell <= _names(media)


def run_js(checks: str) -> None:
    if not NODE:
        pytest.skip("node is not installed")
    script = """
const mem = {};
globalThis.localStorage = { getItem: (k) => mem[k] ?? null, setItem: (k, v) => { mem[k] = String(v); } };
""" + (STATIC / "ui.js").read_text() + """
const eq = (got, want, what) => {
  if (JSON.stringify(got) !== JSON.stringify(want))
    throw new Error(`${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
};
const REG = [
  { symbol: "SPX", label: "S&P 500 Index" }, { symbol: "SPY", label: "SPDR S&P 500 ETF Trust" },
  { symbol: "ES", label: "E-mini S&P 500 Future" }, { symbol: "NDX", label: "Nasdaq-100 Index" },
  { symbol: "QQQ", label: "Invesco QQQ Trust" }, { symbol: "NQ", label: "E-mini Nasdaq-100 Future" },
  { symbol: "USO", label: "United States Oil Fund" },
];
const keys = (q) => uiSuggest(q, REG).map((r) => r.symbol);
""" + checks + '\nconsole.log("ok");\n'
    r = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr or r.stdout


def test_search_puts_the_best_match_first_so_enter_picks_it():
    run_js("""
eq(keys("nd"), ["NDX", "ND"], "symbol prefix first, then the literal look-up; Index and Fund do not match");
eq(keys("aapl"), ["AAPL"], "an unregistered ticker is only the look-up");
eq(keys("ndx"), ["NDX"], "an exact registered symbol has no look-up row");
eq(keys("oil"), ["USO", "OIL"], "a word of a name matches after symbols");
eq(keys(""), REG.map((r) => r.symbol), "empty lists every registered symbol");
""")


def test_recent_symbols_are_suggested_once_and_newest_first():
    run_js("""
uiRemember("AAPL"); uiRemember("MSFT"); uiRemember("AAPL"); uiRemember("SPX");
eq(uiRecent(), ["SPX", "AAPL", "MSFT"], "newest first, no repeats");
eq(keys("").filter((s) => s === "SPX").length, 1, "a recent registered symbol appears once");
eq(keys("").slice(-2), ["AAPL", "MSFT"], "recent typed ones after the registered");
eq(keys("ms"), ["MSFT", "MS"], "a recent symbol matches by prefix");
for (let i = 0; i < 12; i++) uiRemember(`T${i}`);
eq(uiRecent().length, 8, "kept to eight");
mem["gexdash-recent-symbols"] = "{not json";
eq(uiRecent(), [], "garbage in storage is nothing remembered");
""")


def test_typing_is_detected_for_fields_and_editable_text():
    run_js("""
eq([uiTyping({ tagName: "INPUT" }), uiTyping({ tagName: "SELECT" }), uiTyping({ tagName: "DIV", isContentEditable: true }),
    uiTyping({ tagName: "BUTTON" }), uiTyping(null)], [true, true, true, false, false], "fields, not buttons");
""")
