"""The live console must be quiet when healthy and loud when not.

The heartbeat used to print every 5 seconds for the whole session, so the one
line carrying a fault looked exactly like the thousands that did not. Making it
quiet introduces the opposite and worse risk -- staying quiet while something is
broken -- so the print policy is pinned here.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.run_live import heartbeat_should_speak as speak     # noqa: E402


def test_silent_when_healthy():
    assert not speak([], False, now=1000.0, last_alarm=0.0, repeat_s=60)


def test_a_fault_is_never_swallowed_even_inside_the_repeat_window():
    assert speak(["SINK drop=3"], False, now=0.0, last_alarm=0.0, repeat_s=60)


def test_faults_repeat_but_not_every_tick():
    assert not speak(["STRAT_FAIL=1"], False, now=1010.0, last_alarm=1000.0,
                     repeat_s=60)
    assert speak(["STRAT_FAIL=1"], False, now=1061.0, last_alarm=1000.0,
                 repeat_s=60)


def test_going_live_is_announced_once():
    assert speak([], True, now=1000.0, last_alarm=999.0, repeat_s=60)
    assert not speak([], False, now=1000.0, last_alarm=999.0, repeat_s=60)
