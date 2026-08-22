"""THE REPRODUCTION: sleeves holding for the last sliver of a spent day.

2026-08-21. At the minute ES printed its high, the session had already produced
86% of the median range of the prior ten sessions; NQ had produced 106% -- more
than a full normal day. Nine long sleeves were at maximum profit in that minute.
Every one held through it, five were still holding four hours later at the 15:59
flat, and the book gave back $23,232 of $18,565 in peak profit (capture -25%).

None of them could have known, because nothing in the engine computed how much of
a normal day had already happened. This is that number.

These tests pin what has to be true for it to be usable: the reference must come
only from COMPLETED prior sessions, the overnight session must not pollute the
ruler, and it must fail OPEN before it has warmed up.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.features.day_range import DayRange  # noqa: E402

NS = 1_000_000_000
# 2026-08-17 09:30 ET == 13:30 UTC (a Monday)
D0 = 1_786_973_400 * NS
DAY = 86_400 * NS


def session(dr: DayRange, day: int, lo: float, hi: float, minute0: int = 0):
    """Walk a session between lo and hi, inside RTH."""
    t = D0 + day * DAY + minute0 * 60 * NS
    for k, px in enumerate((lo, hi, (lo + hi) / 2)):
        dr.note(t + k * 60 * NS, px)


def test_it_reports_the_fraction_of_a_typical_day_consumed():
    dr = DayRange(min_sessions=3)
    for d in range(4):                       # four 40-point sessions
        session(dr, d, 7000.0, 7040.0)
        dr.roll_session()
    session(dr, 4, 7000.0, 7034.4)           # 34.4 of a typical 40
    assert dr.typical() == 40.0
    assert abs(dr.used() - 0.86) < 1e-9, dr.used()
    assert dr.extended(0.8)
    assert not dr.extended(0.95)


def test_today_is_never_in_its_own_reference():
    """If today leaked into the denominator the ruler would stretch to meet
    whatever is happening, and an extended day would never look extended."""
    dr = DayRange(min_sessions=3)
    for d in range(4):
        session(dr, d, 7000.0, 7040.0)
        dr.roll_session()
    before = dr.typical()
    session(dr, 4, 7000.0, 7400.0)           # a 400-point monster
    assert dr.typical() == before, "today changed its own reference"
    assert dr.used() == 10.0                 # and is correctly seen as enormous


def test_the_overnight_session_does_not_pollute_the_ruler():
    """Globex ranges are far wider than RTH ones. Mixing them makes `used`
    meaningless -- an RTH range would never look large against an ETH ruler."""
    dr = DayRange(min_sessions=3)
    for d in range(4):
        session(dr, d, 7000.0, 7040.0)
        dr.roll_session()
    t = D0 + 4 * DAY
    dr.note(t - 5 * 3600 * NS, 6500.0)       # 04:30 ET, pre-open
    dr.note(t + 7 * 3600 * NS, 7500.0)       # 16:30 ET, after the close
    assert dr.today() is None, "out-of-hours prints were counted"
    session(dr, 4, 7000.0, 7020.0)
    assert dr.used() == 0.5


def test_it_fails_open_before_it_has_a_history():
    dr = DayRange(min_sessions=3)
    session(dr, 0, 7000.0, 7040.0)
    assert dr.typical() is None
    assert dr.used() is None
    assert not dr.extended(0.5), "unknown must never assert extension"


def test_rolling_banks_the_session_and_clears():
    dr = DayRange(min_sessions=1)
    session(dr, 0, 7000.0, 7050.0)
    assert dr.today() == 50.0
    dr.roll_session()
    assert dr.today() is None
    assert dr.typical() == 50.0


def test_a_new_day_rolls_automatically():
    dr = DayRange(min_sessions=1)
    session(dr, 0, 7000.0, 7050.0)
    session(dr, 1, 7100.0, 7120.0)           # no explicit roll
    assert dr.typical() == 50.0
    assert dr.today() == 20.0
    assert dr.used() == 0.4
