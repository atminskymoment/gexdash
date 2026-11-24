"""The display time zone (static/tz.js).

Every clock time on screen goes through tz.js, so its conversions are tested
here by running the file under Node. Skipped where Node is not installed; the
page itself needs nothing but a browser."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gexdash.providers.mock import MockProvider
from gexdash.server import app, set_provider

TZ_JS = Path(__file__).resolve().parents[1] / "gexdash" / "static" / "tz.js"
NODE = shutil.which("node")


def run_js(checks: str) -> None:
    if not NODE:
        pytest.skip("node is not installed")
    script = TZ_JS.read_text() + """
const eq = (got, want, what) => {
  if (JSON.stringify(got) !== JSON.stringify(want))
    throw new Error(`${what}: got ${JSON.stringify(got)}, want ${JSON.stringify(want)}`);
};
""" + checks + '\nconsole.log("ok");\n'
    r = subprocess.run([NODE, "-"], input=script, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stderr or r.stdout


def test_exchange_time_is_the_default_and_new_york_by_name_is_the_same_entry():
    run_js("""
eq(tz.zone, "America/New_York", "default zone");
const open = new Date("2026-11-23T14:30:00Z");
eq([tzHM(open), tzTag(open), tzIsExchange()], ["09:30", "ET", true], "exchange");
tzSet("America/New_York");
eq(tz.choice, "exchange", "New York by name");
""")


def test_bars_read_in_the_chosen_zone_including_half_hours_and_the_next_day():
    run_js("""
const open = tzInstant("2026-11-23T14:30:00");          // 09:30 ET, as the API sends it
tzSet("Asia/Kolkata");
eq([tzHM(open), tzMinutes(open), tzTag(open), tzIsExchange()], ["20:00", 1200, "IST", false], "India");
tzSet("Asia/Tokyo");
const late = tzInstant("2026-11-23T15:30:00");          // 10:30 ET Monday
eq([tzYMD(late), tzHM(late), tzStamp(late)], ["2026-11-24", "00:30", "Tue, Nov 24, 00:30"], "Tokyo");
tzSet("America/Los_Angeles");
eq([tzHM(open), tzTag(open)], ["06:30", "PT"], "Los Angeles");
""")


def test_tags_follow_daylight_saving_where_the_name_changes():
    run_js("""
const winter = new Date("2026-11-23T12:00:00Z"), summer = new Date("2026-07-01T12:00:00Z");
eq([tzTag(winter, "Europe/London"), tzTag(summer, "Europe/London")], ["GMT", "BST"], "London");
eq([tzTag(winter, "Europe/Berlin"), tzTag(summer, "Europe/Berlin")], ["CET", "CEST"], "Frankfurt");
eq([tzTag(winter, "Australia/Sydney"), tzTag(summer, "Australia/Sydney")], ["AEDT", "AEST"], "Sydney");
eq([tzTag(winter, "America/New_York"), tzTag(summer, "America/New_York")], ["ET", "ET"], "ET is generic");
eq(tzTag(winter, "America/Sao_Paulo"), "GMT-3", "a zone outside the list");
""")


def test_the_spring_forward_hour_is_skipped_not_repeated():
    run_js("""
// 2026-03-08: New York goes from 01:59 EST to 03:00 EDT.
const before = new Date("2026-03-08T06:59:00Z"), after = new Date("2026-03-08T07:00:00Z");
eq([tzHM(before), tzHM(after)], ["01:59", "03:00"], "New York DST");
tzSet("UTC");
eq([tzHM(before), tzHM(after), tzTag(after)], ["06:59", "07:00", "UTC"], "UTC does not move");
""")


def test_unknown_zones_fall_back_and_aliases_keep_their_picker_entry():
    run_js("""
eq(tzSet("Not/AZone"), "America/New_York", "fallback");
eq(tz.choice, "exchange", "fallback choice");
tzSet("Asia/Calcutta");                                  // ICU's older name for Kolkata
eq([tz.choice, tzTag()], ["Asia/Kolkata", "IST"], "alias");
tzSet("asia/tokyo");
eq(tz.choice, "Asia/Tokyo", "case");
tzSet("local");
eq([tz.choice, tz.zone], ["local", tzLocal()], "local follows the machine");
""")


def test_the_picker_lists_exchange_and_local_first_and_any_linked_zone():
    run_js("""
const values = () => tzOptions().map((o) => o.value);
eq(values().slice(0, 2), ["exchange", "local"], "order");
eq(values().includes("America/New_York"), false, "New York appears once, as exchange");
eq(values().includes("America/Sao_Paulo"), false, "not offered by default");
tzSet("America/Sao_Paulo");                              // e.g. from ?tz=
eq(values().filter((v) => v === "America/Sao_Paulo").length, 1, "linked zone listed once");
eq(tzOptions().find((o) => o.value === "Asia/Kolkata").text, "India (IST)", "label");
""")


def test_changing_zone_rebuilds_formatters_and_tells_listeners():
    run_js("""
const t = new Date("2026-11-23T14:30:00Z");
let heard = null;
tzOnChange((z) => { heard = z.zone; });
eq(tzHM(t), "09:30", "before");
tzSet("Europe/London");
eq([tzHM(t), heard], ["14:30", "Europe/London"], "after");
""")


def test_index_ships_the_zone_picker_and_loads_tz_js_first():
    set_provider(MockProvider())
    with TestClient(app) as c:
        html = c.get("/").text
        assert c.get("/static/tz.js").status_code == 200
    assert 'id="f-tz"' in html and 'id="clock-tz"' in html
    assert html.index("/static/tz.js") < html.index("/static/technicals.js") < html.index("/static/app.js")
