"""A replay "day" must be an ET session, not a UTC calendar date.

tools/portfolio_replay.py picks days with _live_days(), which returns ET session
dates, then fetches each one with

    WHERE ts >= '{day}T00:00:00Z' AND ts < '{day}T23:59:59Z'

which is UTC. For ET date D that window is really ET 20:00 of D-1 through 19:59
of D. Since run_day builds a FRESH strategy per day, a swing sleeve's first bar
is that 20:00 ET bar from the previous evening -- minute 1200, past its 15:59
DECISION_MIN, with `_decided` still False. It therefore commits the whole day's
decision to a single evening bar.

Seen in .cache/replay_fills_ES_live.csv:

    2026-07-29 15:59  ES:ibs  +1  7351.00  ibs-entry
    2026-07-29 20:00  ES:ibs  +1  7378.25  ibs-entry
    2026-07-30 15:59  ES:ibs  -1  7470.00  ibs-exit

Two entries, one exit. Every ibs/rsi2 figure produced by this harness is wrong
while the boundary is wrong -- including the +$11,075 quoted on 2026-08-04.

The live engine is NOT affected: it runs one long-lived strategy and never
re-slices, and claude_paper_fills shows no entries outside 09:20-15:59 ET.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.portfolio_replay import et_session_bounds_utc  # noqa: E402

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _et(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=ET)


def test_window_starts_at_et_midnight_not_utc_midnight():
    lo, hi = et_session_bounds_utc("2026-07-29")
    lo_et = datetime.fromisoformat(lo.replace("Z", "+00:00")).astimezone(ET)
    assert (lo_et.hour, lo_et.minute) == (0, 0), f"window starts at {lo_et} ET"
    assert lo_et.date().isoformat() == "2026-07-29"


def test_previous_evening_is_excluded():
    """THE REGRESSION. 20:00 ET on 07-28 is 00:00 UTC on 07-29 -- the exact bar
    that hijacked the swing sleeves' daily decision."""
    lo, _ = et_session_bounds_utc("2026-07-29")
    evening = _et("2026-07-28T20:00").astimezone(UTC)
    assert evening.isoformat().replace("+00:00", "Z") < lo, (
        "the previous evening's 20:00 ET bar is still inside the window")


def test_window_covers_the_whole_rth_session():
    lo, hi = et_session_bounds_utc("2026-07-29")
    for hhmm in ("09:30", "12:00", "15:59"):
        t = _et(f"2026-07-29T{hhmm}").astimezone(UTC).isoformat().replace("+00:00", "Z")
        assert lo <= t < hi, f"{hhmm} ET fell outside the window"


def test_windows_are_contiguous_and_non_overlapping():
    """Consecutive sessions must tile the timeline exactly -- no bar counted
    twice, none dropped."""
    _, hi1 = et_session_bounds_utc("2026-07-29")
    lo2, _ = et_session_bounds_utc("2026-07-30")
    assert hi1 == lo2, f"gap/overlap between sessions: {hi1} vs {lo2}"


def test_dst_boundary_is_handled():
    """A fixed UTC offset would silently shift by an hour either side of a DST
    change; the ET-anchored window must not."""
    for day in ("2026-03-07", "2026-03-09", "2026-10-31", "2026-11-02"):
        lo, hi = et_session_bounds_utc(day)
        lo_et = datetime.fromisoformat(lo.replace("Z", "+00:00")).astimezone(ET)
        assert (lo_et.hour, lo_et.minute) == (0, 0), f"{day}: starts {lo_et} ET"
        assert lo_et.date().isoformat() == day
