"""Back-adjustment for futures contract changes, as thinkorswim defines it:
"adjust all price data prior to the active contract change so that the last
close before the contract change is at the same level as the first open
after it." The roll is the first session on or after the Monday of expiry
week, which is where every measurable step in Schwab's /ES and /NQ has been
since December 2022."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from fastapi.testclient import TestClient

from gexdash import instruments
from gexdash.bars import build_bars
from gexdash.futures import VERIFIED_FROM, adjust_note, back_adjust, roll_dates, third_friday
from gexdash.providers.mock import MockProvider
from gexdash.server import app, set_provider
from gexdash.store import CachedProvider, ChainStore

ET = ZoneInfo("America/New_York")


def test_roll_dates_are_the_monday_of_expiry_week():
    assert third_friday(2026, 9) == date(2026, 9, 18)
    # Every date below carried the measured quarterly step on /ES and /NQ.
    assert roll_dates(date(2024, 1, 1), date(2026, 11, 23)) == [
        date(2024, 3, 11), date(2024, 6, 17), date(2024, 9, 16), date(2024, 12, 16),
        date(2025, 3, 17), date(2025, 6, 16), date(2025, 9, 15), date(2025, 12, 15),
        date(2026, 3, 16), date(2026, 6, 15), date(2026, 9, 14),
    ]


def _daily(days_and_prices, inst="ES"):
    rows = [{"time": datetime(d.year, d.month, d.day, 6, tzinfo=timezone.utc),
             "open": o, "high": max(o, c) + 1, "low": min(o, c) - 1, "close": c, "volume": 100.0}
            for d, o, c in days_and_prices]
    return build_bars(instruments.get(inst), rows, interval_minutes=1440)


def test_additive_adjustment_closes_each_gap_and_leaves_the_present_alone():
    # Two rolls: 2026-06-15 (gap +60) and 2026-09-14 (gap +70).
    bars = _daily([
        (date(2026, 6, 11), 7000, 7010), (date(2026, 6, 12), 7010, 7020),
        (date(2026, 6, 15), 7080, 7090), (date(2026, 9, 11), 7100, 7110),
        (date(2026, 9, 14), 7180, 7185), (date(2026, 9, 15), 7185, 7190),
    ])
    adj, rolls = back_adjust(bars)
    assert [(r["index"], r["date"], r["gap"]) for r in rolls] == [
        (2, "2026-06-15", 60.0), (4, "2026-09-14", 70.0)]
    np.testing.assert_allclose(adj.close, [7140, 7150, 7160, 7180, 7185, 7190])
    # The definition: the last close before each change meets the first open after it.
    for r in rolls:
        assert adj.close[r["index"] - 1] == pytest.approx(adj.open[r["index"]])
    assert adj.close[-1] == bars.close[-1] and adj.open[-2] == bars.open[-2]
    np.testing.assert_array_equal(adj.volume, bars.volume)
    # A copy: the cached series must not move.
    assert bars.close[0] == 7010 and adj is not bars


def test_ratio_adjustment_is_available_as_the_one_switch():
    bars = _daily([(date(2026, 9, 11), 7000, 7000), (date(2026, 9, 14), 7070, 7080)])
    adj, rolls = back_adjust(bars, method="ratio")
    assert rolls[0]["gap"] == pytest.approx(7070 / 7000)
    assert adj.close[0] == pytest.approx(7070) and adj.close[1] == 7080


def test_intraday_roll_is_the_sunday_evening_open():
    """Friday 16:55 is the old contract's last bar; Sunday 18:00 opens Monday's
    session, the first of the new one."""
    stamps = [(datetime(2026, 9, 11, 16, 50, tzinfo=ET), 7000.0), (datetime(2026, 9, 11, 16, 55, tzinfo=ET), 7002.0),
              (datetime(2026, 9, 13, 18, 0, tzinfo=ET), 7075.0), (datetime(2026, 9, 14, 9, 30, tzinfo=ET), 7080.0)]
    rows = [{"time": t.astimezone(timezone.utc), "open": p, "high": p + 1, "low": p - 1,
             "close": p, "volume": 1.0} for t, p in stamps]
    adj, rolls = back_adjust(build_bars(instruments.get("ES"), rows, interval_minutes=5))
    assert [r["index"] for r in rolls] == [2] and rolls[0]["gap"] == pytest.approx(73.0)
    assert adj.close[1] == pytest.approx(7075.0)


@pytest.mark.parametrize("weekday_offset", [0, -1])        # weeks stamped Monday, or Sunday
def test_weekly_bars_roll_on_the_expiry_week(weekday_offset):
    mondays = [date(2026, 8, 31) + timedelta(weeks=k) for k in range(4)]   # roll week: Sep 14
    prices = [(7000, 7010), (7010, 7020), (7090, 7100), (7100, 7110)]
    bars = _daily([(d + timedelta(days=weekday_offset), o, c) for d, (o, c) in zip(mondays, prices)])
    adj, rolls = back_adjust(bars)
    assert [r["index"] for r in rolls] == [2] and rolls[0]["gap"] == 70.0
    np.testing.assert_allclose(adj.close[:2], [7080, 7090])


def test_a_series_that_does_not_straddle_a_roll_is_returned_as_is():
    bars = _daily([(date(2026, 10, 1), 7000, 7010), (date(2026, 10, 2), 7010, 7020)])
    adj, rolls = back_adjust(bars)
    assert rolls == [] and adj is bars


def test_rolls_before_december_2022_are_marked_unverified():
    bars = _daily([(date(2022, 9, 9), 4000, 4010), (date(2022, 9, 12), 4030, 4040),
                   (date(2022, 12, 9), 3900, 3910), (date(2022, 12, 12), 3940, 3950)])
    _, rolls = back_adjust(bars)
    assert [r["verified"] for r in rolls] == [False, True] and VERIFIED_FROM == date(2022, 12, 12)
    assert "1 of the 2 rolls shown are before December 2022" in adjust_note("/ES", adjusted=True, rolls=rolls)
    assert "not adjusted" in adjust_note("/ES", adjusted=False, rolls=[])


# ----------------------------------------------------------------------
# Endpoints
# ----------------------------------------------------------------------


class _Stepped(MockProvider):
    """Mock futures bars with a +80 step at every roll Monday, like the feed's."""

    calls = 0

    def get_price_history(self, inst, **kw):
        type(self).calls += 1
        bars = super().get_price_history(inst, **kw)
        if not inst.is_future:
            return bars
        days = [date.fromisoformat(str(t)[:10]) for t in bars.time]
        rolls = roll_dates(days[0], days[-1])
        step = np.array([80.0 * sum(1 for r in rolls if r <= d) for d in days])
        step -= step[-1]                      # the present is the present
        return build_bars(inst, [
            {"time": t, "open": o + s, "high": h + s, "low": lo + s, "close": c + s, "volume": v}
            for t, o, h, lo, c, v, s in zip(bars.time, bars.open, bars.high, bars.low,
                                             bars.close, bars.volume, step)
        ], interval_minutes=bars.interval_minutes, meta=bars.meta)


@pytest.fixture
def client(tmp_path):
    set_provider(CachedProvider(_Stepped(), ChainStore(tmp_path / "f.db")))
    with TestClient(app) as c:
        yield c


def test_technicals_adjusts_futures_by_default_and_says_so(client):
    d = client.get("/api/technicals?symbol=ES&range=2Y&interval=1D").json()
    assert d["available"] and d["adjusted"] is True and d["rolls"]
    assert any("Adjusted for contract changes" in n for n in d["notes"])
    for r in d["rolls"]:
        i = r["i"]
        assert d["close"][i - 1] == pytest.approx(d["open"][i])
    raw = client.get("/api/technicals?symbol=ES&range=2Y&interval=1D&adjust=false").json()
    assert raw["adjusted"] is False
    assert [r["date"] for r in raw["rolls"]] == [r["date"] for r in d["rolls"]]   # still marked
    assert any("not adjusted" in n for n in raw["notes"])
    assert raw["close"][-1] == d["close"][-1]           # the latest price never moves
    assert raw["close"][0] != d["close"][0]


def test_repeated_requests_do_not_compound_the_adjustment(client):
    """The fetch is cached; adjusting the cached series in place would add
    the gaps again on every request."""
    before = _Stepped.calls
    a = client.get("/api/technicals?symbol=NQ&range=1Y&interval=1D").json()
    b = client.get("/api/technicals?symbol=NQ&range=1Y&interval=1D").json()
    assert _Stepped.calls == before + 1, "the second request must be served from the cache"
    assert a["adjusted"] and a["close"] == b["close"]


def test_equities_are_never_adjusted(client):
    d = client.get("/api/technicals?symbol=QQQ&range=1Y&interval=1D").json()
    assert d["adjusted"] is False and d["rolls"] == []
    assert not any("contract changes" in n for n in d["notes"])


def test_history_endpoint_adjusts_the_terminal_chart_too(client):
    d = client.get("/api/history?symbol=ES&range=1Y&interval=1D&indicators=false").json()
    assert d["available"] and d["adjusted"] is True and d["rolls"]
    assert any("Adjusted for contract changes" in n for n in d["plan"]["notes"])
    for r in d["rolls"]:
        assert d["close"][r["index"] - 1] == pytest.approx(d["open"][r["index"]])
