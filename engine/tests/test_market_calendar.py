"""The calendar must reproduce the early closes the TAPE actually shows.

2026-09-07 (Labor Day) closed at 13:00 ET. Every sleeve flattened on a hardcoded
15:59, so nothing fired and a live position rode through a closed market into the
next session. These are the three early closes present in claude_bars_live across
1,150 RTH sessions -- the only ground truth available -- and the computed
calendar has to agree with all three.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd

from engine.core.market_calendar import (EARLY_CLOSE_MIN, RTH_CLOSE_MIN,
                                         close_min, flat_min, holidays,
                                         is_holiday, lockout_min, past_flat)

# day -> last RTH bar observed, from claude_bars_live (see the module docstring)
OBSERVED_EARLY = {"2026-06-19": 13 * 60, "2026-07-03": 13 * 60,
                  "2026-09-07": 12 * 60 + 59}


def test_matches_every_early_close_in_the_tape():
    for day in OBSERVED_EARLY:
        assert is_holiday(day), f"{day} closed early in the tape but is not a holiday"
        assert close_min(day) == EARLY_CLOSE_MIN


def test_ordinary_sessions_are_untouched():
    """The days either side of Labor Day, and a plain midweek session."""
    for day in ("2026-09-04", "2026-09-08", "2026-09-09", "2026-06-18",
                "2026-07-02", "2026-08-12"):
        assert not is_holiday(day)
        assert close_min(day) == RTH_CLOSE_MIN
        assert flat_min(day) == 15 * 60 + 59        # the old constant, preserved


def test_the_flatten_and_lockout_move_with_the_close():
    assert flat_min("2026-09-07") == 12 * 60 + 59
    assert lockout_min("2026-09-07") == 12 * 60 + 45
    assert flat_min("2026-09-08") == 15 * 60 + 59
    assert lockout_min("2026-09-08") == 15 * 60 + 45


def test_past_flat_is_true_after_the_early_close_and_not_before():
    def ts(s):
        return int(pd.Timestamp(s, tz="America/New_York").value)

    # Labor Day: flat from 12:59, and 13:30 is well past it
    assert not past_flat(ts("2026-09-07 12:58"))
    assert past_flat(ts("2026-09-07 12:59"))
    assert past_flat(ts("2026-09-07 13:30"))
    # the very next session is normal again
    assert not past_flat(ts("2026-09-08 12:59"))
    assert not past_flat(ts("2026-09-08 15:58"))
    assert past_flat(ts("2026-09-08 15:59"))


def test_holidays_are_computed_not_typed_so_they_do_not_expire():
    """A hardcoded list of dates silently stops working. Spot-check the rules on
    years either side of the data, including leap and DST-shifted ones."""
    assert "2026-09-07" in holidays(2026)          # Labor Day, 1st Monday Sept
    assert "2027-09-06" in holidays(2027)
    assert "2030-09-02" in holidays(2030)
    assert "2026-11-26" in holidays(2026)          # Thanksgiving, 4th Thursday
    assert "2026-11-27" in holidays(2026)          # and the Friday after
    assert "2026-05-25" in holidays(2026)          # Memorial Day, last Monday May
    assert "2027-05-31" in holidays(2027)
    assert "2026-01-19" in holidays(2026)          # MLK, 3rd Monday January
    assert "2026-04-03" in holidays(2026)          # Good Friday
    assert "2027-03-26" in holidays(2027)
    for y in range(2024, 2035):                     # the rules never go empty
        assert len(holidays(y)) >= 9


def test_a_weekend_holiday_is_observed_on_a_weekday():
    """July 4th 2026 is a SATURDAY and the tape shows Friday the 3rd closing
    early -- the observed date is the one that trades short."""
    assert dt.date(2026, 7, 4).weekday() == 5
    assert "2026-07-03" in holidays(2026)
    assert "2026-07-04" not in holidays(2026)
    # and every holiday returned is a weekday: a Saturday entry would be dead
    for y in (2025, 2026, 2027, 2028):
        for d in holidays(y):
            assert dt.date.fromisoformat(d).weekday() < 5, d


# ── the regression itself ───────────────────────────────────────────────────
def _bars(day: str, first: int, last: int, px: float = 7715.0):
    """One 1m bar per minute from `first` to `last` ET on `day`."""
    from engine.core.events import Bar
    out = []
    for m in range(first, last + 1):
        ts = int(pd.Timestamp(f"{day} {m // 60:02d}:{m % 60:02d}",
                              tz="America/New_York").value)
        out.append(Bar(ts, "1m", px, px + 1, px - 1, px, 100, "ES"))
    return out


def test_labor_day_2026_a_live_sleeve_flattens_at_the_early_close():
    """THE REGRESSION. On 2026-09-07 ES closed at 13:00 ET. `onfade` entered a
    live long at 09:40 and its end-of-day flat was hardcoded to 15:59, so it
    never fired: the position rode through the close, through the 18:00 Globex
    reopen and into the next session. The tape confirms the halt -- last bar
    12:59, next bar 18:01.

    The sleeve must now be flat by 12:59 on that date, and unchanged on the
    ordinary session either side of it."""
    from engine.core.events import PositionUpdate
    from engine.strategies.overnight_fade import OvernightFadeStrategy

    def run(day, prev_day, close_min_et):
        s = OvernightFadeStrategy("ES", min_on_bars=10)
        pos = 0
        orders = []
        # a RISING overnight (7700 -> 7715) so the sleeve fades it SHORT at the
        # open. A flat night gives on_move == 0 and the sleeve correctly stands
        # down, which is not what this test is about.
        night = []
        for i, m in enumerate(range(0, 9 * 60 + 30)):
            night += _bars(day, m, m, 7700.0 + 15.0 * i / (9 * 60 + 29))
        for b in night + _bars(day, 9 * 60 + 30, close_min_et):
            for o in s.on_bar(b):
                orders.append((o.tag, b.ts))
                pos += o.side * o.qty
                s.on_position(PositionUpdate(b.ts, "ES", pos, 0.0))
        return orders, pos

    # HOLIDAY: flat by 12:59, even though the bars stop at 13:00
    orders, pos = run("2026-09-07", "2026-09-04", 13 * 60)
    assert any(t == "entry-onfade" for t, _ in orders), "should still take the trade"
    assert any(t == "onfade-moc" for t, _ in orders), \
        "the early close must trigger the end-of-day flat"
    assert pos == 0, "a live position must not survive a holiday close"
    flat_ts = next(ts for t, ts in orders if t == "onfade-moc")
    et = pd.Timestamp(flat_ts, tz="UTC").tz_convert("America/New_York")
    assert et.hour * 60 + et.minute == 12 * 60 + 59

    # ORDINARY session: unchanged, still 15:59
    orders, pos = run("2026-09-08", "2026-09-07", 16 * 60)
    flat_ts = next(ts for t, ts in orders if t == "onfade-moc")
    et = pd.Timestamp(flat_ts, tz="UTC").tz_convert("America/New_York")
    assert et.hour * 60 + et.minute == 15 * 60 + 59
    assert pos == 0


def test_the_risk_supervisor_backstop_also_moves_to_the_early_close():
    """Belt and braces: even if a sleeve forgets, the supervisor flattens. Its
    configured 15:58 must not out-vote a 13:00 close -- whichever is EARLIER
    wins, because firing late is the failure mode that costs money."""
    from engine.core.risk import RiskSupervisor

    holiday = int(pd.Timestamp("2026-09-07 12:00", tz="America/New_York").value)
    ordinary = int(pd.Timestamp("2026-09-08 12:00", tz="America/New_York").value)
    assert RiskSupervisor._flatten_min(holiday, (15, 58)) == 12 * 60 + 59
    assert RiskSupervisor._flatten_min(ordinary, (15, 58)) == 15 * 60 + 58
    assert RiskSupervisor._lockout_min(holiday, (15, 45)) == 12 * 60 + 45
    assert RiskSupervisor._lockout_min(ordinary, (15, 45)) == 15 * 60 + 45
