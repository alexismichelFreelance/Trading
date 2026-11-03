"""Pivot periods are the GLOBEX FUTURES SESSION, matched to the user's NT8
Pivots indicator.

A session opens at 18:00 ET and runs to 17:00 ET the next day, so the D key is
the date the session OPENED: a bar at 02:00 or 10:00 ET belongs to the prior
evening's session, and a bar at 18:00 ET or later starts a new one. Weekly and
monthly keys stay calendar-based.

HISTORY, because this cost wrong turns:

  * The class once used et_session_date -- ET midnight -- and this file pinned
    that boundary after a 2026-08-10 sample matched the chart at
    midnight-to-midnight. It is the wrong key for an NT8 Pivots indicator on a
    Globex template: NT8 rolls at 18:00 ET while the engine rolled at midnight,
    so from midnight until the next 18:00 the engine's "prior" session already
    contained the evening/overnight prints NT8 still counted in the live one.
  * An RTH-only boundary was also tried; it moved the grid a median 7.42
    points off and is kept only as an opt-in.
  * What was genuinely broken in that episode was the SEED: the grid was
    primed from Yahoo ES=F, a continuous 24-hour series, i.e. the wrong
    instrument. That fix stands.

The anchor below still reproduces the 2026-08-10 RTH H/L/C, because the 18:00
roll leaves the prior session's RTH window intact.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.features.pivots import MultiPivots, daily_pivots  # noqa: E402


def _ns(s):
    return int(pd.Timestamp(s, tz="America/New_York").value)


def test_the_anchor_matches_the_nt8_grid():
    """THE ANCHOR. The 2026-08-10 RTH session that was matched against the
    chart is the Aug-9 Globex session (it opened 18:00 ET the evening before).
    Its H/L/C must survive the 18:00 ET roll on Aug 10 and still produce the
    chart's PP."""
    mp = MultiPivots()
    mp.update(_ns("2026-08-10 09:35"), 7798.00, 7770.00, 7790.00)
    mp.update(_ns("2026-08-10 15:59"), 7780.00, 7764.50, 7775.75)
    mp.update(_ns("2026-08-10 23:00"), 7779.00, 7770.00, 7775.75)   # opens Aug-10 session
    h, l, c = mp.prior["D"]
    assert (h, l) == (7798.00, 7764.50), f"prior D is H{h} L{l}, not the Aug-9 session"
    pp = daily_pivots(h, l, c)["PP"]
    assert abs(pp - 7779.42) < 0.01, f"PP {pp:.2f}; the chart shows 7779.44"


def test_evening_print_belongs_to_the_next_session():
    """An 18:00 ET print opens the next Globex session, so it must roll the
    prior period immediately; a 10:00 ET print the following morning is still
    part of that same session, and the evening high only becomes "prior" when
    the next 18:00 rolls it."""
    mp = MultiPivots()
    mp.update(_ns("2026-08-10 10:00"), 7790.00, 7770.00, 7780.00)   # Aug-9 session
    mp.update(_ns("2026-08-10 20:00"), 7805.00, 7769.00, 7800.00)   # rolls -> Aug-10 session
    h, l, _ = mp.prior["D"]
    assert (h, l) == (7790.00, 7770.00), f"18:00 ET did not roll the session: H{h} L{l}"

    mp.update(_ns("2026-08-11 10:00"), 7700.00, 7690.00, 7695.00)   # still Aug-10 session
    h, l, _ = mp.prior["D"]
    assert (h, l) == (7790.00, 7770.00), f"a pre-18:00 bar rolled the session early: H{h} L{l}"

    mp.update(_ns("2026-08-11 20:00"), 7710.00, 7680.00, 7700.00)   # rolls -> Aug-11 session
    h, _, _ = mp.prior["D"]
    assert h == 7805.00, f"the 20:00 ET high was dropped (got {h})"


def test_session_does_not_roll_at_midnight():
    """Overnight prints after 00:00 ET belong to the session that opened the
    previous evening; the D period must not roll at midnight."""
    mp = MultiPivots()
    mp.update(_ns("2026-08-10 20:00"), 7805.00, 7769.00, 7800.00)   # opens Aug-10 session
    mp.update(_ns("2026-08-11 01:00"), 7810.00, 7760.00, 7650.00)   # same session
    mp.update(_ns("2026-08-11 10:00"), 7700.00, 7690.00, 7695.00)   # same session
    assert "D" not in mp.prior, "the session rolled at midnight"


def test_rth_filtering_is_available_but_off_by_default():
    """Kept as an opt-in for callers that genuinely want an RTH grid -- but it is
    NOT what the NT8 Pivots indicator builds, so it must never be the default."""
    mp = MultiPivots()
    mp.update(_ns("2026-08-10 10:00"), 7790.0, 7770.0, 7780.0)
    mp.update(_ns("2026-08-10 20:00"), 7805.0, 7769.0, 7800.0, rth_only=True)
    mp.update(_ns("2026-08-11 10:00"), 7700.0, 7690.0, 7695.0)
    assert mp.prior["D"][0] == 7790.0, "rth_only=True still admitted a 20:00 bar"


def test_weekly_and_monthly_still_form():
    mp = MultiPivots()
    for d in ("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07"):
        mp.update(_ns(f"{d} 10:00"), 7600.0, 7580.0, 7590.0)
    mp.update(_ns("2026-08-10 10:00"), 7700.0, 7690.0, 7695.0)
    assert "D" in mp.prior and "W" in mp.prior
    assert mp.grid(), "grid empty after a normal week"
