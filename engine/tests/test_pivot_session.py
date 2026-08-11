"""Pivot periods are the ET CALENDAR DAY, matched to the user's NT8 indicator.

Settled empirically on 2026-08-11 rather than by reading a docstring. The user's
chart showed PP 7779.44; candidates built from the recorded traded contract:

    00:00 -> 24:00 ET   H 7798.00  L 7764.50  C 7775.75  ->  7779.42   MATCH
    18:00 -> 17:00 ET (Globex)                           ->  7778.92
    09:30 -> 16:00 ET (RTH)                              ->  7778.83
    prior week                                           ->  7715.42
    July                                                 ->  7503.50

The 0.02 is rounding of the thirds. The value that discriminates is the LOW --
7764.50, not the 7763.00 overnight print, which falls outside a
midnight-to-midnight day. Weekly and monthly are ruled out by 64 and 276 points.

Their NT8 settings: Standard pivots, D+W+M ranges, calculation mode "intraday
data" -- so NT8 aggregates the chart's own bars over the session its Trading
Hours template defines, and that template is producing calendar days.

HISTORY, because this cost two wrong turns:

  * The class docstring says "prior RTH session" and SessionLevels.update_bar
    does filter to RTH, so on 2026-08-10 I filtered MultiPivots to match. That
    moved the grid a MEDIAN 7.42 POINTS off the chart (max 24.42) across 39
    sessions. The docstring was wrong; et_session_date -- which rolls at ET
    midnight -- was right all along.
  * I then inferred a Globex (18:00 ET) boundary from the fact that the user's
    chart displays pre-open bars. Also wrong, by 0.52.

What was genuinely broken was never the boundary: it was the SEED. The grid was
primed from Yahoo ES=F -- a continuous series in 24-hour bars, i.e. the wrong
instrument. That fix stands; this file pins the boundary so neither wrong turn
can be taken again.
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


def test_the_period_is_the_et_calendar_day():
    """THE ANCHOR. Reproduces the 2026-08-10 session that was matched against the
    chart: the 23:00 ET print is part of that calendar day and must be included;
    a print after midnight belongs to the next day."""
    mp = MultiPivots()
    mp.update(_ns("2026-08-10 09:35"), 7798.00, 7770.00, 7790.00)
    mp.update(_ns("2026-08-10 15:59"), 7780.00, 7764.50, 7775.75)
    mp.update(_ns("2026-08-10 23:00"), 7779.00, 7770.00, 7775.75)   # same ET day
    mp.update(_ns("2026-08-11 02:00"), 7760.00, 7700.00, 7710.00)   # NEXT day: rolls
    h, l, c = mp.prior["D"]
    assert (h, l) == (7798.00, 7764.50), f"prior D is H{h} L{l}, not the calendar day"
    pp = daily_pivots(h, l, c)["PP"]
    assert abs(pp - 7779.42) < 0.01, f"PP {pp:.2f}; the chart shows 7779.44"


def test_overnight_before_midnight_counts_toward_that_day():
    """An 18:00-23:59 ET print is part of the SAME calendar day. Excluding it (a
    Globex 18:00 boundary) put PP 0.52 off; excluding all overnight (RTH) put it
    a median 7.42 off."""
    mp = MultiPivots()
    mp.update(_ns("2026-08-10 10:00"), 7790.00, 7770.00, 7780.00)
    mp.update(_ns("2026-08-10 20:00"), 7805.00, 7769.00, 7800.00)   # evening: same day
    mp.update(_ns("2026-08-11 10:00"), 7700.00, 7690.00, 7695.00)
    h, l, _ = mp.prior["D"]
    assert h == 7805.00, f"the 20:00 ET high was dropped (got {h})"


def test_after_midnight_belongs_to_the_next_period():
    mp = MultiPivots()
    mp.update(_ns("2026-08-10 10:00"), 7790.00, 7770.00, 7780.00)
    mp.update(_ns("2026-08-11 01:00"), 7900.00, 7600.00, 7650.00)   # next day
    mp.update(_ns("2026-08-11 10:00"), 7700.00, 7690.00, 7695.00)
    h, l, _ = mp.prior["D"]
    assert (h, l) == (7790.00, 7770.00), (
        f"a post-midnight print leaked into the prior day: H{h} L{l}")


def test_rth_filtering_is_available_but_off_by_default():
    """Kept as an opt-in for callers that genuinely want an RTH grid -- but it is
    NOT what the chart uses, so it must never be the default."""
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
