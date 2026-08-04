"""The once-a-day swing decision must belong to the session it names.

Found via .cache/replay_fills_ES_live.csv, which shows ibs/rsi2 entries at
20:00 ET -- four hours after the 15:59 decision bar:

    2026-07-29 15:59  ES:ibs  +1  7351.00  ibs-entry
    2026-07-29 20:00  ES:ibs  +1  7378.25  ibs-entry
    2026-07-30 15:59  ES:ibs  -1  7470.00  ibs-exit

DIAGNOSIS (my first one was wrong -- recorded because the wrong one is the
tempting one). I assumed the ETH reopen rolled `et_session_date`, resetting
`_decided` and letting the sleeve decide twice in a session. It does not: at
18:00 ET on 07-29 the session date is still 2026-07-29 and `_decided` stays True.
The LIVE record confirms it -- claude_paper_fills has zero entries outside
09:20-15:59 ET.

The real cause is the REPLAY HARNESS. tools/portfolio_replay.py slices events by
UTC calendar day and constructs a fresh strategy per day. A UTC day starts at
20:00 ET the PREVIOUS evening, so a brand-new instance meets that 20:00 bar
first, sees minute 1200 > DECISION_MIN 959 with `_decided` False, and commits the
day's decision to a single evening bar. The sleeves are sound; the replay numbers
for ibs and rsi2 are not.

These tests still pin the invariant the sleeves must honour, and would have
caught the failure had it been where I first thought. The harness fix is
separate: slice by ET SESSION, not by UTC date.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar  # noqa: E402
from engine.strategies.ibs_swing import IBSSwingStrategy  # noqa: E402
from engine.strategies.rsi2_swing import RSI2SwingStrategy  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(day: int, hh: int, mm: int) -> int:
    from datetime import timedelta
    d = datetime(2026, 7, 29, hh, mm, tzinfo=ET) + timedelta(days=day)
    return int(d.timestamp() * 1e9)


def _bar(day: int, hh: int, mm: int, o, h, l, c) -> Bar:
    return Bar(_ts(day, hh, mm), "1m", o, h, l, c, 100, "ES")


class P:
    def __init__(self, q):
        self.qty = q


def _weak_rth_session(s, day: int):
    """A normal RTH session closing near its low -> IBS < 0.20, a real entry."""
    s.on_bar(_bar(day, 10, 0, 7400, 7420, 7340, 7410))
    s.on_bar(_bar(day, 12, 0, 7410, 7420, 7340, 7360))
    return s.on_bar(_bar(day, 15, 59, 7360, 7365, 7340, 7345))


@pytest.mark.parametrize("make", [
    lambda: IBSSwingStrategy("ES"),
    lambda: RSI2SwingStrategy("ES", ma_slow=3, seed=[]),
])
def test_evening_session_does_not_re_fire_the_decision(make):
    """THE REPRODUCTION. After a legitimate 15:59 entry, the 18:00-20:00 ETH bars
    of the NEXT session date must not produce a second entry."""
    s = make()
    for d in range(6):                      # warm any indicator history
        _weak_rth_session(s, d)
        s.on_position(P(0))
    out = _weak_rth_session(s, 6)
    # Deliberately leave the sleeve FLAT. Holding a position would block a second
    # entry via the pos==0 guard and the test would pass without the window
    # working at all -- which is exactly how this bug survived. The invariant
    # under test is the WINDOW: at 20:00 ET there is no meaningful daily IBS to
    # compute, so no decision may be taken whatever the position happens to be.
    s.on_position(P(0))
    # the evening bar must itself LOOK like an entry, otherwise the test passes
    # for the wrong reason: a bar closing mid-range gives IBS ~0.33 and would be
    # rejected on its value rather than on the window.
    evening = []
    for hh, mm in ((18, 0), (18, 30), (20, 0), (23, 0)):
        evening += s.on_bar(_bar(6, hh, mm, 7355, 7360, 7340, 7341))
    assert not evening, (
        f"decided again during the evening session: "
        f"{[(o.side, o.tag) for o in evening]}")


def test_the_1559_decision_still_fires():
    """The window must still admit the bar it exists for. IBS only: its signal
    can be constructed from one session, whereas RSI(2)+200dMA needs a contrived
    multi-day series and would be testing the fixture, not the window."""
    s = IBSSwingStrategy("ES")
    for d in range(6):
        _weak_rth_session(s, d)
        s.on_position(P(0))
    out = _weak_rth_session(s, 6)
    assert out, "the 15:59 decision no longer fires at all"
    assert out[0].side == 1


def test_position_cannot_reach_two_from_one_session():
    """The consequence that matters: size must not accumulate. Even if the
    evening bars are fed, the sleeve holds exactly what it entered with."""
    s = IBSSwingStrategy("ES")
    for d in range(3):
        _weak_rth_session(s, d)
        s.on_position(P(0))
    out = _weak_rth_session(s, 3)
    assert out
    s.on_position(P(1))
    extra = []
    for hh, mm in ((18, 0), (19, 0), (20, 0)):
        extra += s.on_bar(_bar(3, hh, mm, 7355, 7360, 7340, 7341))
    adds = [o for o in extra if o.side > 0]
    assert not adds, f"accumulated a second lot overnight: {[o.tag for o in adds]}"
