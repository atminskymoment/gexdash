"""Continuous futures: contract changes and back-adjustment.

Schwab's ``/ES`` and ``/NQ`` history is the front contract, unadjusted: each
quarter the series switches to the next contract and jumps by the calendar
spread. thinkorswim's chart option **Adjust for contract changes** removes
that jump, and its manual defines exactly how: *"adjust all price data prior
to the active contract change so that the last close before the contract
change is at the same level as the first open after it."* That is computable
from the continuous series alone, given where the changes are.

**Where.** The Monday of expiry week (the third Friday of March, June,
September and December, less four days) -- the first session on or after it.
Measured against SPX and NDX, which have no roll and no dividend: from
December 2022 to September 2026 every one of those Mondays carries the whole
quarterly step on both contracts (0.56-1.5%, well above the day-to-day noise),
and no other day near expiry does. Before mid-2022 interest rates were near
zero and so was the step, and no day stands out; the same rule is applied
there because it is the only evidence, and the payload says the older rolls
are unverified.

**How.** Additive by default: every bar before a change is shifted by that
change's gap, ``first open after - last close before``, cumulatively from the
newest change back. ``method="ratio"`` scales instead. The manual's wording
("at the same level") fits either; additive is the conventional back-adjusted
series. Volume is untouched.

The adjusted bars are always new arrays. Callers cache the raw series, and
adjusting a cached object in place would add the gaps again on every hit.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import numpy as np

from .sessions import session_dates

__all__ = ["ROLL_MONTHS", "VERIFIED_FROM", "third_friday", "roll_dates", "back_adjust", "adjust_note"]

ROLL_MONTHS = (3, 6, 9, 12)

#: First roll verified against the index (see module docstring).
VERIFIED_FROM = date(2022, 12, 12)

#: A CME futures session opening Sunday 18:00 ET is Monday's.
_FUTURES_SHIFT_HOURS = 6


def third_friday(year: int, month: int) -> date:
    """The calendar third Friday of ``month``, the quarterly expiry that
    :func:`roll_dates` counts back from. Not holiday-adjusted: only the Monday
    of that week is used, and a holiday on the Friday does not move it."""
    d = date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


def roll_dates(start: date, end: date) -> list[date]:
    """The Monday of each quarterly expiry week between ``start`` and ``end``."""
    out = []
    for y in range(start.year, end.year + 1):
        for m in ROLL_MONTHS:
            r = third_friday(y, m) - timedelta(days=4)
            if start <= r <= end:
                out.append(r)
    return out


def _bar_keys(bars) -> tuple[list, callable]:
    """A sortable per-bar key, and the same key for a roll date.

    Intraday bars belong to their exchange session (Sunday evening is
    Monday's). Daily bars are dated by their session already. Weekly bars are
    matched by ISO week: a week stamped on Sunday or on Monday is the same
    week, and an on-or-after date test would put the roll a week late.
    """
    n = len(bars)
    if bars.interval_minutes < 1440:
        return session_dates(bars.time, _FUTURES_SHIFT_HOURS), (lambda r: r)
    days = [date.fromisoformat(str(t)[:10]) for t in bars.time]
    spacing = np.median(np.diff(bars.time.astype("datetime64[D]").astype("int64"))) if n > 2 else 1
    if spacing >= 5:
        week = lambda d: d.isocalendar()[:2]        # (ISO year, ISO week)
        return [week(d + timedelta(days=1)) for d in days], week
    return days, (lambda r: r)


def back_adjust(bars, *, method: str = "additive"):
    """``(adjusted bars, rolls)``. Each roll is ``{"index", "date", "gap"}``:
    the first bar of the new contract, the roll Monday, and the raw jump
    (``open[index] - close[index - 1]``; for ``ratio``, their ratio)."""
    n = len(bars)
    if n < 2:
        return bars, []
    keys, key_of = _bar_keys(bars)
    first_day = date.fromisoformat(str(bars.time[0])[:10]) - timedelta(days=7)
    last_day = date.fromisoformat(str(bars.time[-1])[:10]) + timedelta(days=7)
    o, h, l, c = (np.array(a, dtype=float) for a in (bars.open, bars.high, bars.low, bars.close))

    rolls = []
    for r in roll_dates(first_day, last_day):
        rk = key_of(r)
        k = next((i for i, kk in enumerate(keys) if kk >= rk), None)
        if k is None or k == 0:
            continue                       # the series does not straddle this change
        gap = (o[k] / c[k - 1]) if method == "ratio" else (o[k] - c[k - 1])
        rolls.append({"index": k, "date": r.isoformat(), "gap": float(gap),
                      "verified": r >= VERIFIED_FROM})

    if not rolls:
        return bars, []
    # Cumulative from the newest change back: bar j moves by every gap after it.
    if method == "ratio":
        factor = np.ones(n)
        for rl in rolls:
            factor[: rl["index"]] *= rl["gap"]
        o, h, l, c = o * factor, h * factor, l * factor, c * factor
    else:
        shift = np.zeros(n)
        for rl in rolls:
            shift[: rl["index"]] += rl["gap"]
        o, h, l, c = o + shift, h + shift, l + shift, c + shift
    meta = {**(bars.meta or {}), "adjusted": method}
    return replace(bars, open=o, high=h, low=l, close=c, meta=meta), rolls


def adjust_note(symbol: str, *, adjusted: bool, rolls: list[dict]) -> str:
    """The one line the chart shows about contract changes."""
    if not adjusted:
        return (f"{symbol} is not adjusted for contract changes: each quarterly roll "
                "shows as a price step.")
    old = [r for r in rolls if not r["verified"]]
    tail = (f" {len(old)} of the {len(rolls)} rolls shown are before December 2022, where the "
            "roll date could not be checked against the index." if old else "")
    return ("Adjusted for contract changes, as thinkorswim's option of that name: before each "
            "quarterly roll (the Monday of expiry week) prices are shifted so the last close "
            "meets the first open after it." + tail)
