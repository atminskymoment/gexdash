"""Exchange session dates.

Open interest is published by the OCC on a **US market** schedule. Deriving
"yesterday" from either UTC or the user's local clock is wrong, and wrong in a
way that only shows up during part of the day:

* UTC rolls over at 19:00/20:00 US Eastern, so an evening run would date the
  snapshot to tomorrow's session.
* Local time is whatever the machine happens to be set to -- a user in Tokyo
  would be a full day out for most of their working hours.

Both were live in this codebase until a test failed at 00:01 UTC / 17:01
Pacific, which is precisely the window where the two disagree.

Everything about *when a session happened* goes through here.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

EXCHANGE_TZ = ZoneInfo("America/New_York")

__all__ = ["EXCHANGE_TZ", "exchange_today", "previous_session", "market_phase"]


def exchange_today() -> date:
    """Today's date at the exchange, regardless of the machine's timezone."""
    return datetime.now(EXCHANGE_TZ).date()


#: US equity session boundaries, in ET. Index options run to 16:15 and futures
#: nearly around the clock; this uses the equity schedule, which is what the
#: phase banner is for. Holidays are not modelled -- see previous_session.
_PHASES = [
    ("closed", 0.00, 4.00),
    ("pre-market", 4.00, 9.50),
    ("open", 9.50, 16.00),
    ("after-hours", 16.00, 20.00),
    ("closed", 20.00, 24.00),
]


def market_phase(now: datetime | None = None) -> dict:
    """Current session phase and time to the next transition.

    Derived purely from the clock -- no market data involved -- so this is
    honest to report even though everything around it is a daily snapshot.
    """
    now = now.astimezone(EXCHANGE_TZ) if now else datetime.now(EXCHANGE_TZ)
    hours = now.hour + now.minute / 60 + now.second / 3600
    weekend = now.weekday() >= 5

    phase, ends_at = "closed", 24.0
    for name, start, end in _PHASES:
        if start <= hours < end:
            phase, ends_at = name, end
            break

    if weekend:
        phase = "closed"
        # Next transition is Monday's pre-market open.
        days_to_monday = 7 - now.weekday()
        seconds = (days_to_monday * 24 - hours + 4.0) * 3600
    else:
        seconds = (ends_at - hours) * 3600

    return {
        "phase": phase,
        "is_open": phase == "open" and not weekend,
        "exchange_time": now.strftime("%H:%M:%S"),
        "exchange_date": now.date().isoformat(),
        "seconds_to_next": int(max(seconds, 0)),
        "next_transition": _next_name(phase, weekend),
    }


def _next_name(phase: str, weekend: bool) -> str:
    if weekend:
        return "pre-market"
    return {
        "closed": "pre-market",
        "pre-market": "open",
        "open": "after-hours",
        "after-hours": "closed",
    }[phase]


def previous_session(day: date | None = None) -> date:
    """The prior trading day, skipping weekends.

    Does not know about market holidays -- on the day after one it will point
    a day early. For dating an OI snapshot that is cosmetic (the OI itself is
    whatever the provider returned); if it ever needs to be exact, swap in an
    exchange calendar here and nothing else changes.
    """
    day = day or exchange_today()
    back = {0: 3, 6: 2}.get(day.weekday(), 1)  # Mon -> Fri, Sun -> Fri
    return day - timedelta(days=back)
