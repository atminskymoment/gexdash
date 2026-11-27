"""Where the Sun and Moon are, and when the lunar and solar events fall.

A low-precision analytical ephemeris from Jean Meeus, *Astronomical
Algorithms* (2nd ed., 1998): the Sun from chapter 25 and the Moon from
chapter 47, with its full periodic-term tables 47.A and 47.B. No data files,
no network, nothing beyond numpy, and it evaluates as well for next year as
for last year, which is the point: the cycles can be drawn into the future
because the sky can be computed there.

How good it is, measured rather than assumed. Meeus's worked examples are
pinned in ``tests/test_astro.py``; against PyEphem over 2016-2030 (1,201
events) the largest differences were:

* new, quarter and full moons: 94 seconds. The Moon's longitude is good to
  about 10 arcseconds;
* equinoxes and solstices: 12 minutes. The Sun's longitude is good to about
  0.01 degrees, and it moves a degree a day;
* perigee and apogee: 15 minutes and 10 km. The distance curve is flat at
  its turns, so a small error in distance is a large one in time.

All three are shown on bars of a day or longer, where minutes are invisible.

Times are UTC. The series are written in dynamical time (TT); the
difference, Delta T, is taken as :data:`DELTA_T_SECONDS`, which is under a
minute out anywhere from 1975 to the 2030s.

Phase is *elongation*: the Moon's apparent ecliptic longitude minus the
Sun's, 0 to 360 degrees. 0 is new, 90 first quarter, 180 full, 270 last
quarter. That is the astronomical definition the almanacs publish times
for, not "percent illuminated", which reaches 0 and 100 only approximately.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache

import numpy as np

__all__ = [
    "DELTA_T_SECONDS", "SYNODIC_DAYS", "ANOMALISTIC_DAYS", "TROPICAL_MONTH_DAYS",
    "TROPICAL_YEAR_DAYS", "EVENT_LABELS", "julian_day", "positions", "events", "phase_name",
]

#: TT minus UT, in seconds. It has sat near 69 s since 2017.
DELTA_T_SECONDS = 69.0

#: Mean cycle lengths in days (Meeus, chapters 49 and 50; the tropical year
#: and month from the same mean elements).
SYNODIC_DAYS = 29.530588861
ANOMALISTIC_DAYS = 27.554549886
TROPICAL_MONTH_DAYS = 27.321582241
TROPICAL_YEAR_DAYS = 365.242189

_J2000 = 2451545.0
_UNIX_EPOCH_JD = 2440587.5
_RAD = np.pi / 180.0
#: Mean Earth-Sun distance in km, for the illuminated fraction.
_AU_KM = 149_597_870.7

#: Display names for every event :func:`events` can return.
EVENT_LABELS = {
    "new_moon": "New moon",
    "first_quarter": "First quarter",
    "full_moon": "Full moon",
    "last_quarter": "Last quarter",
    "perigee": "Perigee",
    "apogee": "Apogee",
    "march_equinox": "March equinox",
    "june_solstice": "June solstice",
    "september_equinox": "September equinox",
    "december_solstice": "December solstice",
}

# --------------------------------------------------------------- Meeus 47.A
# Multiples of D, M, M', F; then the coefficient of sin for longitude (1e-6
# degrees) and of cos for distance (1e-3 km).
_LR = np.array([
    (0, 0, 1, 0, 6288774, -20905355), (2, 0, -1, 0, 1274027, -3699111),
    (2, 0, 0, 0, 658314, -2955968), (0, 0, 2, 0, 213618, -569925),
    (0, 1, 0, 0, -185116, 48888), (0, 0, 0, 2, -114332, -3149),
    (2, 0, -2, 0, 58793, 246158), (2, -1, -1, 0, 57066, -152138),
    (2, 0, 1, 0, 53322, -170733), (2, -1, 0, 0, 45758, -204586),
    (0, 1, -1, 0, -40923, -129620), (1, 0, 0, 0, -34720, 108743),
    (0, 1, 1, 0, -30383, 104755), (2, 0, 0, -2, 15327, 10321),
    (0, 0, 1, 2, -12528, 0), (0, 0, 1, -2, 10980, 79661),
    (4, 0, -1, 0, 10675, -34782), (0, 0, 3, 0, 10034, -23210),
    (4, 0, -2, 0, 8548, -21636), (2, 1, -1, 0, -7888, 24208),
    (2, 1, 0, 0, -6766, 30824), (1, 0, -1, 0, -5163, -8379),
    (1, 1, 0, 0, 4987, -16675), (2, -1, 1, 0, 4036, -12831),
    (2, 0, 2, 0, 3994, -10445), (4, 0, 0, 0, 3861, -11650),
    (2, 0, -3, 0, 3665, 14403), (0, 1, -2, 0, -2689, -7003),
    (2, 0, -1, 2, -2602, 0), (2, -1, -2, 0, 2390, 10056),
    (1, 0, 1, 0, -2348, 6322), (2, -2, 0, 0, 2236, -9884),
    (0, 1, 2, 0, -2120, 5751), (0, 2, 0, 0, -2069, 0),
    (2, -2, -1, 0, 2048, -4950), (2, 0, 1, -2, -1773, 4130),
    (2, 0, 0, 2, -1595, 0), (4, -1, -1, 0, 1215, -3958),
    (0, 0, 2, 2, -1110, 0), (3, 0, -1, 0, -892, 3258),
    (2, 1, 1, 0, -810, 2616), (4, -1, -2, 0, 759, -1897),
    (0, 2, -1, 0, -713, -2117), (2, 2, -1, 0, -700, 2354),
    (2, 1, -2, 0, 691, 0), (2, -1, 0, -2, 596, 0),
    (4, 0, 1, 0, 549, -1423), (0, 0, 4, 0, 537, -1117),
    (4, -1, 0, 0, 520, -1571), (1, 0, -2, 0, -487, -1739),
    (2, 1, 0, -2, -399, 0), (0, 0, 2, -2, -381, -4421),
    (1, 1, 1, 0, 351, 0), (3, 0, -2, 0, -340, 0),
    (4, 0, -3, 0, 330, 0), (2, -1, 2, 0, 327, 0),
    (0, 2, 1, 0, -323, 1165), (1, 1, -1, 0, 299, 0),
    (2, 0, 3, 0, 294, 0), (2, 0, -1, -2, 0, 8752),
], dtype=float)

# --------------------------------------------------------------- Meeus 47.B
# Multiples of D, M, M', F; the coefficient of sin for latitude (1e-6 degrees).
_B = np.array([
    (0, 0, 0, 1, 5128122), (0, 0, 1, 1, 280602), (0, 0, 1, -1, 277693),
    (2, 0, 0, -1, 173237), (2, 0, -1, 1, 55413), (2, 0, -1, -1, 46271),
    (2, 0, 0, 1, 32573), (0, 0, 2, 1, 17198), (2, 0, 1, -1, 9266),
    (0, 0, 2, -1, 8822), (2, -1, 0, -1, 8216), (2, 0, -2, -1, 4324),
    (2, 0, 1, 1, 4200), (2, 1, 0, -1, -3359), (2, -1, -1, 1, 2463),
    (2, -1, 0, 1, 2211), (2, -1, -1, -1, 2065), (0, 1, -1, -1, -1870),
    (4, 0, -1, -1, 1828), (0, 1, 0, 1, -1794), (0, 0, 0, 3, -1749),
    (0, 1, -1, 1, -1565), (1, 0, 0, 1, -1491), (0, 1, 1, 1, -1475),
    (0, 1, 1, -1, -1410), (0, 1, 0, -1, -1344), (1, 0, 0, -1, -1335),
    (0, 0, 3, 1, 1107), (4, 0, 0, -1, 1021), (4, 0, -1, 1, 833),
    (0, 0, 1, -3, 777), (4, 0, -2, 1, 671), (2, 0, 0, -3, 607),
    (2, 0, 2, -1, 596), (2, -1, 1, -1, 491), (2, 0, -2, 1, -451),
    (0, 0, 3, -1, 439), (2, 0, 2, 1, 422), (2, 0, -3, -1, 421),
    (2, 1, -1, 1, -366), (2, 1, 0, 1, -351), (4, 0, 0, 1, 331),
    (2, -1, 1, 1, 315), (2, -2, 0, -1, 302), (0, 0, 1, 3, -283),
    (2, 1, 1, -1, -229), (1, 1, 0, -1, 223), (1, 1, 0, 1, 223),
    (0, 1, -2, -1, -220), (2, 1, -1, -1, -220), (1, 0, 1, 1, -185),
    (2, -1, -2, -1, 181), (0, 1, 2, 1, -177), (4, 0, -2, -1, 176),
    (4, -1, -1, -1, 166), (1, 0, 1, -1, -164), (4, 0, 1, -1, 132),
    (1, 0, -1, -1, -119), (4, -1, 0, -1, 115), (2, -2, 0, 1, 107),
], dtype=float)


# ------------------------------------------------------------------ time


def _seconds(times) -> np.ndarray:
    """UTC instants as float Unix seconds. Accepts ``datetime64`` (naive
    UTC), aware ``datetime`` objects, or numbers already in seconds."""
    arr = np.asarray(times)
    if np.issubdtype(arr.dtype, np.datetime64):
        return arr.astype("datetime64[ms]").astype("int64") / 1000.0
    if arr.dtype == object:
        return np.array([t.timestamp() for t in arr.ravel()], dtype=float).reshape(arr.shape)
    return arr.astype(float)


def julian_day(times) -> np.ndarray:
    """Julian Day (UT) of each instant."""
    return _seconds(times) / 86400.0 + _UNIX_EPOCH_JD


def _centuries(seconds: np.ndarray) -> np.ndarray:
    """Julian centuries of dynamical time since J2000.0."""
    jde = (seconds + DELTA_T_SECONDS) / 86400.0 + _UNIX_EPOCH_JD
    return (jde - _J2000) / 36525.0


# ------------------------------------------------------------ positions


def _sun(T: np.ndarray) -> dict[str, np.ndarray]:
    """The Sun's apparent longitude (degrees) and distance (AU), Meeus 25."""
    L0 = 280.46646 + 36000.76983 * T + 0.0003032 * T**2
    M = 357.52911 + 35999.05029 * T - 0.0001537 * T**2
    e = 0.016708634 - 0.000042037 * T - 0.0000001267 * T**2
    Mr = M * _RAD
    C = ((1.914602 - 0.004817 * T - 0.000014 * T**2) * np.sin(Mr)
         + (0.019993 - 0.000101 * T) * np.sin(2 * Mr) + 0.000289 * np.sin(3 * Mr))
    true_lon = L0 + C
    nu = (M + C) * _RAD
    R = 1.000001018 * (1 - e**2) / (1 + e * np.cos(nu))
    omega = (125.04 - 1934.136 * T) * _RAD
    # Aberration (-0.00569) and the main term of nutation (-0.00478 sin omega).
    lon = true_lon - 0.00569 - 0.00478 * np.sin(omega)
    return {"lon": np.mod(lon, 360.0), "dist_au": R, "omega": omega}


def _moon(T: np.ndarray) -> dict[str, np.ndarray]:
    """The Moon's geocentric longitude and latitude (degrees), distance (km)
    and mean anomaly M' (degrees), Meeus 47. Longitude here is geometric;
    :func:`positions` adds nutation."""
    Lp = 218.3164477 + 481267.88123421 * T - 0.0015786 * T**2 + T**3 / 538841 - T**4 / 65194000
    D = 297.8501921 + 445267.1114034 * T - 0.0018819 * T**2 + T**3 / 545868 - T**4 / 113065000
    M = 357.5291092 + 35999.0502909 * T - 0.0001536 * T**2 + T**3 / 24490000
    Mp = 134.9633964 + 477198.8675055 * T + 0.0087414 * T**2 + T**3 / 69699 - T**4 / 14712000
    F = 93.2720950 + 483202.0175233 * T - 0.0036539 * T**2 - T**3 / 3526000 + T**4 / 863310000
    A1 = (119.75 + 131.849 * T) * _RAD
    A2 = (53.09 + 479264.290 * T) * _RAD
    A3 = (313.45 + 481266.484 * T) * _RAD
    E = 1 - 0.002516 * T - 0.0000074 * T**2

    args = np.stack([np.mod(D, 360), np.mod(M, 360), np.mod(Mp, 360), np.mod(F, 360)]) * _RAD  # (4, n)

    # Terms in M are scaled by E for each power of M: the Earth's orbital
    # eccentricity is shrinking.
    def series(table: np.ndarray, coef: np.ndarray, fn) -> np.ndarray:
        angle = table[:, :4] @ args                                   # (terms, n)
        scale = np.where(np.abs(table[:, 1:2]) == 1, E, np.where(np.abs(table[:, 1:2]) == 2, E**2, 1.0))
        return (coef[:, None] * scale * fn(angle)).sum(axis=0)

    sl = series(_LR, _LR[:, 4], np.sin)
    sr = series(_LR, _LR[:, 5], np.cos)
    sb = series(_B, _B[:, 4], np.sin)

    Lr, Fr, Mpr = Lp * _RAD, F * _RAD, Mp * _RAD
    sl = sl + 3958 * np.sin(A1) + 1962 * np.sin(Lr - Fr) + 318 * np.sin(A2)
    sb = (sb - 2235 * np.sin(Lr) + 382 * np.sin(A3) + 175 * np.sin(A1 - Fr)
          + 175 * np.sin(A1 + Fr) + 127 * np.sin(Lr - Mpr) - 115 * np.sin(Lr + Mpr))
    return {
        "lon": np.mod(Lp + sl / 1e6, 360.0),
        "lat": sb / 1e6,
        "dist_km": 385000.56 + sr / 1000.0,
        "mean_anomaly": np.mod(Mp, 360.0),
    }


def positions(times) -> dict[str, np.ndarray]:
    """Sun and Moon at each instant.

    Returns arrays, all in degrees unless named otherwise:

    ``sun_lon``      the Sun's apparent ecliptic longitude: 0 at the March
                     equinox, 90 the June solstice, 180, 270
    ``sun_dec``      the Sun's declination
    ``moon_lon``     the Moon's apparent ecliptic longitude
    ``moon_lat``     its ecliptic latitude
    ``moon_dec``     its declination: the monthly north-south swing
    ``moon_dist_km`` its distance from the Earth's centre
    ``mean_anomaly`` the Moon's mean anomaly M': 0 near perigee
    ``elongation``   phase, Moon minus Sun longitude: 0 new, 180 full
    ``illumination`` the fraction of the disc lit, 0 to 1
    """
    s = _seconds(times)
    T = _centuries(s)
    sun = _sun(T)
    moon = _moon(T)
    omega = sun["omega"]
    # Nutation in longitude, main term only (Meeus 22: the rest is < 1.5").
    moon_lon = np.mod(moon["lon"] - 0.004778 * np.sin(omega), 360.0)
    eps = (23.4392911 - 0.0130042 * T - 1.64e-7 * T**2 + 5.04e-7 * T**3 + 0.00256 * np.cos(omega)) * _RAD

    lam, beta = moon_lon * _RAD, moon["lat"] * _RAD
    moon_dec = np.arcsin(np.sin(beta) * np.cos(eps) + np.cos(beta) * np.sin(eps) * np.sin(lam))
    sun_dec = np.arcsin(np.sin(eps) * np.sin(sun["lon"] * _RAD))

    elong = np.mod(moon_lon - sun["lon"], 360.0)
    # Illuminated fraction (Meeus 48.2-48.3): the phase angle seen from the
    # Moon, from the geocentric elongation psi and the two distances.
    cos_psi = np.cos(beta) * np.cos((moon_lon - sun["lon"]) * _RAD)
    psi = np.arccos(np.clip(cos_psi, -1, 1))
    R = sun["dist_au"] * _AU_KM
    i = np.arctan2(R * np.sin(psi), moon["dist_km"] - R * np.cos(psi))
    illum = (1 + np.cos(i)) / 2

    return {
        "sun_lon": sun["lon"],
        "sun_dec": sun_dec / _RAD,
        "moon_lon": moon_lon,
        "moon_lat": moon["lat"],
        "moon_dec": moon_dec / _RAD,
        "moon_dist_km": moon["dist_km"],
        "mean_anomaly": moon["mean_anomaly"],
        "elongation": elong,
        "illumination": illum,
    }


_PHASE_NAMES = ["New moon", "Waxing crescent", "First quarter", "Waxing gibbous",
                "Full moon", "Waning gibbous", "Last quarter", "Waning crescent"]


def phase_name(elongation: float) -> str:
    """The phase's common name: eight 45-degree sectors centred on new,
    first quarter, full and last quarter and the four between them."""
    return _PHASE_NAMES[int(((float(elongation) + 22.5) % 360.0) // 45.0)]


# --------------------------------------------------------------- events


#: Sampling step for the event search, in seconds. Six hours moves the
#: elongation about 3 degrees; linear interpolation over that is good to well
#: under a minute before the refinement step.
_STEP = 6 * 3600.0


def _crossings(t: np.ndarray, angle: np.ndarray, rate_deg_per_day: float, key: str,
               names: list[str]) -> list[tuple[float, str]]:
    """Times at which a monotonically increasing angle crosses each multiple
    of 90 degrees, refined against the ephemeris. ``names[k % 4]`` is the
    event at ``k * 90``."""
    unwrapped = np.unwrap(angle * _RAD) / _RAD
    first, last = np.ceil(unwrapped[0] / 90.0), np.floor(unwrapped[-1] / 90.0)
    out = []
    for k in np.arange(first, last + 1):
        target = k * 90.0
        j = int(np.searchsorted(unwrapped, target))
        if j == 0 or j >= len(t):
            continue
        a0, a1 = unwrapped[j - 1], unwrapped[j]
        tc = t[j - 1] + (target - a0) / (a1 - a0) * (t[j] - t[j - 1])
        # Two Newton steps on the true angle, using the mean rate as the slope.
        for _ in range(2):
            now = positions(np.array([tc]))[key][0]
            err = (now - target + 180.0) % 360.0 - 180.0
            tc -= err / rate_deg_per_day * 86400.0
        out.append((float(tc), names[int(k) % 4]))
    return out


def _extremes(t: np.ndarray, dist: np.ndarray) -> list[tuple[float, str, float]]:
    """Perigees and apogees: turns in the sampled distance, each refined on a
    five-minute grid across the two samples either side."""
    d = np.diff(dist)
    out = []
    for j in np.flatnonzero(np.sign(d[:-1]) != np.sign(d[1:])) + 1:
        kind = "perigee" if d[j - 1] < 0 else "apogee"
        fine = np.arange(t[j] - 2 * _STEP, t[j] + 2 * _STEP + 1, 300.0)
        fd = positions(fine)["moon_dist_km"]
        k = int(np.argmin(fd) if kind == "perigee" else np.argmax(fd))
        out.append((float(fine[k]), kind, float(fd[k])))
    return out


@lru_cache(maxsize=64)
def _events_cached(start_s: int, end_s: int) -> tuple[dict, ...]:
    """:func:`events` over whole days, cached: every chart reload asks again."""
    pad = 2 * 86400
    t = np.arange(start_s - pad, end_s + pad + _STEP, _STEP, dtype=float)
    pos = positions(t)
    found: list[tuple[float, str, float | None]] = []
    found += [(tc, name, None) for tc, name in _crossings(
        t, pos["elongation"], 360.0 / SYNODIC_DAYS, "elongation",
        ["new_moon", "first_quarter", "full_moon", "last_quarter"])]
    found += [(tc, name, None) for tc, name in _crossings(
        t, pos["sun_lon"], 360.0 / TROPICAL_YEAR_DAYS, "sun_lon",
        ["march_equinox", "june_solstice", "september_equinox", "december_solstice"])]
    found += _extremes(t, pos["moon_dist_km"])
    rows = []
    for tc, kind, dist in sorted(found):
        if not start_s <= tc <= end_s:
            continue
        row = {"type": kind, "label": EVENT_LABELS[kind],
               "time": datetime.fromtimestamp(round(tc), tz=timezone.utc)}
        if dist is not None:
            row["distance_km"] = round(dist)
        rows.append(row)
    return tuple(rows)


def events(start: datetime, end: datetime) -> list[dict]:
    """Lunar phases, perigees and apogees, equinoxes and solstices between
    two instants, oldest first.

    Each is ``{"type", "label", "time"}`` with ``time`` an aware UTC
    datetime; perigee and apogee also carry ``distance_km``. The search is
    cached by whole UTC day, so the bounds are widened to midnight.
    """
    s = int(start.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    e = int((end.astimezone(timezone.utc) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp())
    return [dict(r) for r in _events_cached(s, e)]
