"""The zone sleeve gates entries by UTC HOUR, like the painter used to.

    gate_utc: tuple[int, int] | None = (13, 21)
    in_window = self.gate_utc[0] <= ns_to_utc(b.ts).hour < self.gate_utc[1]

13:00 UTC is 09:00 ET in summer and 08:00 ET in winter. So the entry window is
half an hour wider than RTH at the open, an hour wider at the close, and it
SLIDES BY AN HOUR at every DST change while the session does not move at all.

I fixed exactly this in engine/painters.py on 2026-08-06 (the zone that appeared
on the user's chart at 15:00 every day) and left the trading copy alone --
fixing the display and not the sleeve that places the orders.

The detection window has the same shape: `9*60 <= m < 17*60`, i.e. 09:00-17:00
ET, which admits pre-open and post-close bars into the detector. That one is
deliberate (it matches the validated 2025 backtest window) and is left as is; a
parity-gated detector is not something to change on a hunch. The ENTRY gate is
not parity-gated and is simply wrong.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.strategies.zones_strategy import ZoneLifecycleStrategy  # noqa: E402


def _ns(s):
    return int(pd.Timestamp(s, tz="America/New_York").value)


def _in_window(s, when):
    """The sleeve's own entry-window predicate for a given ET time."""
    return s._entry_window_open(_ns(when))


def test_summer_window_is_rth_not_utc_hours():
    s = ZoneLifecycleStrategy("ES")
    assert not _in_window(s, "2026-07-15 09:00"), "09:00 ET (13:00 UTC) allowed entries"
    assert not _in_window(s, "2026-07-15 09:29")
    assert _in_window(s, "2026-07-15 09:30"), "the RTH open was excluded"
    assert _in_window(s, "2026-07-15 15:30")
    assert not _in_window(s, "2026-07-15 16:30"), "16:30 ET (20:30 UTC) allowed entries"


def test_winter_window_does_not_slide_with_dst():
    """A fixed UTC hour moves an hour against ET twice a year; the session does
    not. Same ET clock times must give the same answer in January as in July."""
    s = ZoneLifecycleStrategy("ES")
    for day in ("2026-01-15", "2026-07-15"):
        assert not _in_window(s, f"{day} 09:00"), f"{day}: pre-open allowed"
        assert _in_window(s, f"{day} 10:00"), f"{day}: mid-session blocked"
        assert not _in_window(s, f"{day} 16:30"), f"{day}: post-close allowed"


def test_the_window_can_still_be_disabled():
    s = ZoneLifecycleStrategy("ES", gate_utc=None)
    assert _in_window(s, "2026-07-15 03:00"), "gate_utc=None must allow anything"
