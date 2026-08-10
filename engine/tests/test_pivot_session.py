"""Pivot levels must come from the prior RTH session of the TRADED contract.

pivot.py's own docstring: "classic floor pivots (PP, R1-R3, S1-S3) off the prior
RTH session". Two things broke that promise, and they stacked:

1. MultiPivots.update() has NO RTH filter. SessionLevels.update_bar() does
   (`if not is_rth(bar.ts): return`), but the D/W/M grid the sleeve actually
   trades took every bar it was handed -- including the whole overnight Globex
   session.

2. run_live seeded that grid with fetch_daily_bars("ES=F") -- Yahoo daily bars,
   which are themselves 24-hour bars from a CONTINUOUS series rather than the
   contract being traded.

Measured on 2026-08-07:
    Yahoo ES=F daily   H 7786.75  L 7725.50  C 7779.75
    traded ES RTH      H 7786.75  L 7743.50  C 7783.25     <- low 18 pts lower
    pivots from Yahoo    PP 7764.00  R1 7802.50  S1 7741.25
    pivots from actual   PP 7771.17  R1 7798.83  S1 7755.58
PP off by 7 points, S1 off by 14. On ES that is the entire distance between "at
the pivot" and "nowhere near it" -- which is exactly what the user saw on the
chart when a pivot trade fired at 10:30 ET with price nowhere near a level.

Same class as the VWAP anchor and the zone gate: the arithmetic was right, the
series it was fed was not.
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


def test_overnight_bars_do_not_move_the_daily_pivot_period():
    """THE REGRESSION. An overnight bar printing far below the RTH low must not
    become the low that next session's S1/S2/S3 are computed from."""
    mp = MultiPivots()
    # RTH session: 7743.50 - 7786.75, closing 7783.25 (the real 08-07 numbers)
    mp.update(_ns("2026-08-07 09:35"), 7786.75, 7743.50, 7770.00)
    mp.update(_ns("2026-08-07 15:55"), 7780.00, 7760.00, 7783.25)
    # overnight: trades 18 points below the RTH low
    mp.update(_ns("2026-08-07 20:30"), 7760.00, 7725.50, 7730.00)
    # next session rolls the period
    mp.update(_ns("2026-08-10 09:35"), 7800.00, 7790.00, 7795.00)
    h, l, c = mp.prior["D"]
    assert l == 7743.50, (
        f"prior-session low is {l}, not the RTH low 7743.50 -- overnight bars "
        f"are still being folded into the daily pivot period")
    assert h == 7786.75 and c == 7783.25


def test_the_resulting_levels_match_the_rth_grid():
    """Every D-level must equal the classic pivot of the RTH H/L/C -- not of the
    session widened by overnight trade."""
    mp = MultiPivots()
    mp.update(_ns("2026-08-07 09:35"), 7786.75, 7743.50, 7770.00)
    mp.update(_ns("2026-08-07 15:55"), 7780.00, 7760.00, 7783.25)
    mp.update(_ns("2026-08-07 20:30"), 7760.00, 7725.50, 7730.00)   # overnight
    mp.update(_ns("2026-08-10 09:35"), 7800.00, 7790.00, 7795.00)
    want = daily_pivots(7786.75, 7743.50, 7783.25)      # from the RTH session
    grid = mp.grid()                                    # {price: 'D-PP', ...}
    # the grid carries the 7 floor pivots; PDH/PDL are prior-day extremes that
    # daily_pivots also returns but the merged grid does not label
    for name in ("PP", "R1", "R2", "R3", "S1", "S2", "S3"):
        px = want[name]
        got = [p for p, lbl in grid.items() if lbl == f"D-{name}"]
        assert got, f"D-{name} missing from the grid"
        assert abs(got[0] - px) < 0.01, (
            f"D-{name} is {got[0]:.2f}, RTH says {px:.2f} "
            f"(off by {got[0] - px:+.2f} -- overnight leaked in)")


def test_weekly_and_monthly_periods_are_rth_too():
    """The W and M periods share the same update path, so they inherit the same
    fault -- and a monthly pivot built from an overnight spike is wrong for a
    whole month."""
    mp = MultiPivots()
    mp.update(_ns("2026-08-03 10:00"), 7600.0, 7580.0, 7590.0)
    mp.update(_ns("2026-08-03 22:00"), 7605.0, 7500.0, 7510.0)   # overnight spike low
    mp.update(_ns("2026-08-10 10:00"), 7700.0, 7690.0, 7695.0)   # new week + day
    for tf in ("W", "M"):
        if tf in mp.prior:
            assert mp.prior[tf][1] == 7580.0, (
                f"{tf} period low is {mp.prior[tf][1]}, not the RTH low 7580.0")


def test_rth_bars_still_build_the_period_normally():
    """The guard must not silently stop the grid forming."""
    mp = MultiPivots()
    for hh in ("09:35", "11:00", "14:00", "15:55"):
        mp.update(_ns(f"2026-08-07 {hh}"), 7790.0, 7740.0, 7780.0)
    mp.update(_ns("2026-08-10 09:35"), 7800.0, 7790.0, 7795.0)
    assert "D" in mp.prior, "no daily period formed from pure RTH bars"
    assert mp.grid(), "grid is empty after a normal RTH session"
