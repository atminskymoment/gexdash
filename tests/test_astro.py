"""The ephemeris against published answers.

Positions are pinned to Meeus's worked examples (*Astronomical Algorithms*,
2nd ed.), which state their inputs in dynamical time, so the tests convert
with the module's own Delta T. Event times are pinned to almanac values: the
new moon of the 8 April 2024 total eclipse, the 2024 equinox and solstices.
A wider sweep against PyEphem (2016-2030, 1,201 events) was run while
writing this; its worst differences are quoted in ``astro.py`` and set the
tolerances here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from gexdash import astro
from gexdash.astro import DELTA_T_SECONDS, _centuries, _moon, _sun


def _ut_from_jde(jde: float) -> float:
    """Unix seconds (UT) for a Julian Ephemeris Day, by the module's Delta T."""
    return (jde - 2440587.5) * 86400.0 - DELTA_T_SECONDS


def _utc(*args) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


# ----------------------------------------------------------------------
# Positions: Meeus's worked examples
# ----------------------------------------------------------------------


def test_moon_matches_meeus_example_47a():
    # 1992 April 12, 0h TD.
    T = _centuries(np.array([_ut_from_jde(2448724.5)]))
    assert T[0] == pytest.approx(-0.077221081451, abs=1e-11)
    m = _moon(T)
    assert m["lon"][0] == pytest.approx(133.162655, abs=5e-6)
    assert m["lat"][0] == pytest.approx(-3.229126, abs=5e-6)
    assert m["dist_km"][0] == pytest.approx(368409.7, abs=0.1)


def test_sun_matches_meeus_example_25a():
    # 1992 October 13, 0h TD: the low-accuracy method's own answers.
    s = _sun(_centuries(np.array([_ut_from_jde(2448908.5)])))
    assert s["lon"][0] == pytest.approx(199.90895, abs=5e-5)
    assert s["dist_au"][0] == pytest.approx(0.99766, abs=1e-5)
    p = astro.positions(np.array([_ut_from_jde(2448908.5)]))
    assert p["sun_dec"][0] == pytest.approx(-7.78507, abs=1e-4)


def test_illuminated_fraction_matches_meeus_example_48a():
    p = astro.positions(np.array([_ut_from_jde(2448724.5)]))
    assert p["illumination"][0] == pytest.approx(0.6786, abs=1e-4)
    # Example 47.a's declination, which uses full nutation; ours has its main term.
    assert p["moon_dec"][0] == pytest.approx(13.768368, abs=5e-4)


def test_positions_accept_datetime64_aware_datetimes_and_seconds_alike():
    t = _utc(2026, 11, 27, 21)
    a = astro.positions(np.array([np.datetime64("2026-11-27T21:00:00")]))
    b = astro.positions(np.array([t], dtype=object))
    c = astro.positions(np.array([t.timestamp()]))
    for key in a:
        assert a[key][0] == pytest.approx(b[key][0]) == pytest.approx(c[key][0])


def test_elongation_and_illumination_agree_at_new_and_full_moon():
    # The eclipse new moon, then the full moon two weeks on.
    new = astro.positions(np.array([_utc(2024, 4, 8, 18, 21).timestamp()]))
    full = astro.positions(np.array([_utc(2024, 4, 23, 23, 49).timestamp()]))
    assert min(new["elongation"][0], 360 - new["elongation"][0]) < 0.1
    assert new["illumination"][0] < 0.001
    assert full["elongation"][0] == pytest.approx(180, abs=0.1)
    assert full["illumination"][0] > 0.99


# ----------------------------------------------------------------------
# Events: almanac times
# ----------------------------------------------------------------------


def _find(events, kind, near: datetime) -> datetime:
    """The event of a kind closest to ``near``."""
    return min((e["time"] for e in events if e["type"] == kind), key=lambda t: abs(t - near))


@pytest.mark.parametrize("kind, when, tol_s", [
    ("new_moon", _utc(2024, 4, 8, 18, 20, 49), 120),        # total solar eclipse
    ("full_moon", _utc(2024, 3, 25, 7, 0), 120),            # penumbral lunar eclipse
    ("new_moon", _utc(1977, 2, 18, 3, 36, 54), 120),        # Meeus example 49.a, 03:37:42 TD
    ("march_equinox", _utc(2024, 3, 20, 3, 6), 900),
    ("june_solstice", _utc(2024, 6, 20, 20, 51), 900),
    ("september_equinox", _utc(2024, 9, 22, 12, 44), 900),
    ("december_solstice", _utc(2024, 12, 21, 9, 20), 900),
])
def test_event_times_match_the_almanac(kind, when, tol_s):
    ev = astro.events(when - timedelta(days=20), when + timedelta(days=20))
    assert abs((_find(ev, kind, when) - when).total_seconds()) < tol_s


def test_a_year_of_events_is_in_order_and_complete():
    ev = astro.events(_utc(2025, 1, 1), _utc(2026, 1, 1))
    phases = [e for e in ev if e["type"] in ("new_moon", "first_quarter", "full_moon", "last_quarter")]
    order = ["new_moon", "first_quarter", "full_moon", "last_quarter"]
    # Each phase follows the one before it, about a quarter of a month later.
    for a, b in zip(phases, phases[1:]):
        assert order.index(b["type"]) == (order.index(a["type"]) + 1) % 4
        assert 5.5 < (b["time"] - a["time"]).total_seconds() / 86400 < 9.0
    assert 12 <= sum(e["type"] == "new_moon" for e in ev) <= 13
    # Perigee and apogee alternate, at plausible distances.
    ends = [e for e in ev if e["type"] in ("perigee", "apogee")]
    assert all(a["type"] != b["type"] for a, b in zip(ends, ends[1:]))
    for e in ends:
        lo, hi = (356_000, 371_000) if e["type"] == "perigee" else (404_000, 407_000)
        assert lo <= e["distance_km"] <= hi
    # One of each solar event, in calendar order.
    solar = [e["type"] for e in ev if e["type"].endswith(("equinox", "solstice"))]
    assert solar == ["march_equinox", "june_solstice", "september_equinox", "december_solstice"]
    # Within the bounds asked for, oldest first, with aware UTC times.
    times = [e["time"] for e in ev]
    assert times == sorted(times) and all(t.tzinfo is not None for t in times)
    assert times[0] >= _utc(2025, 1, 1) and times[-1] <= _utc(2026, 1, 2)


def test_events_are_copies_a_caller_cannot_corrupt_the_cache_through():
    a = astro.events(_utc(2025, 1, 1), _utc(2025, 2, 1))
    a[0]["type"] = "tampered"
    assert astro.events(_utc(2025, 1, 1), _utc(2025, 2, 1))[0]["type"] != "tampered"


@pytest.mark.parametrize("elong, name", [
    (0, "New moon"), (359, "New moon"), (22, "New moon"), (23, "Waxing crescent"),
    (90, "First quarter"), (135, "Waxing gibbous"), (180, "Full moon"),
    (225, "Waning gibbous"), (270, "Last quarter"), (315, "Waning crescent"),
])
def test_phase_names_are_eight_sectors_centred_on_the_phases(elong, name):
    assert astro.phase_name(elong) == name
