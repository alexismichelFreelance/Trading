"""THE REPRODUCTION: opendrive shorting a break that nothing traded into.

2026-08-19 NQ. Price fell ~350 points off the open. A large part of that decline
happened on minutes where BUYERS were the aggressive side -- the bids were being
cancelled, not hit, so price slid through an empty book. opendrive shorted the
break of the opening range at 29407, thirty points off the session low, and held
a loser until the 15:59 flat: -$2,190 realized, the worst single sleeve result of
the day.

Measured across every ORB break with per-second flow (45 breaks, 24 sessions,
strategy_lab/vacuum_break.py) the split is not subtle, and it holds on each
instrument separately:

    real trading   23 trades  +19,220$  won 65%
    fell on air    22 trades   +3,128$  won 50%

These tests pin the three things that have to be true for the gate to mean
anything: it must SEPARATE the two shapes, it must locate the leg STRUCTURALLY
(a fixed lookback misclassified the motivating trade), and it must FAIL OPEN.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.features.break_quality import BreakQuality  # noqa: E402

NS = 1_000_000_000
# 2026-08-19 09:30 ET == 13:30 UTC
T0930 = 1_787_146_200 * NS


def feed(bq: BreakQuality, legs, start_px=29700.0, t0=T0930):
    """legs = [(minutes, points_per_minute, delta_per_minute), ...]"""
    px, m = start_px, 0
    for mins, dpx, dlt in legs:
        for _ in range(mins):
            px += dpx
            # one trade carries the whole minute's signed volume
            bq.on_trade(t0 + m * 60 * NS, px, abs(dlt) or 1,
                        1 if dlt >= 0 else -1)
            m += 1
    return px


def warm(bq: BreakQuality, v=0.30):
    """Give it a prior history so the gate is armed."""
    for _ in range(bq.min_sessions):
        bq.hist.append(v)


def test_a_break_that_fell_through_air_is_flagged():
    """THE 2026-08-19 SHAPE: price grinds down while buyers are the aggressors.
    Nothing was distributed, so nothing has to be defended."""
    bq = BreakQuality()
    warm(bq)
    feed(bq, [(5, +2.0, +50), (20, -8.0, +40)])     # up, then DOWN on BUYING
    v = bq.vac(-1)
    assert v is not None and v > 0.9, f"air break scored vac={v}"
    assert not bq.ok(-1), "an air break must be refused"


def test_a_break_that_was_traded_into_is_allowed():
    """The mirror: the same decline, but sellers are doing it."""
    bq = BreakQuality()
    warm(bq)
    feed(bq, [(5, +2.0, +50), (20, -8.0, -40)])     # DOWN on SELLING
    v = bq.vac(-1)
    assert v is not None and v < 0.1, f"traded break scored vac={v}"
    assert bq.ok(-1), "a break with real supply behind it must be allowed"


def test_the_leg_is_found_structurally_not_by_a_fixed_lookback():
    """WHY A CONSTANT IS WRONG, and it is the bug that misclassified the trade
    this came from. Here the decline is 40 minutes of air followed by 8 minutes
    of genuine selling into the low. A 20-minute window sees only the selling and
    calls it healthy; the leg from the session HIGH sees the whole move."""
    bq = BreakQuality()
    warm(bq)
    feed(bq, [(3, +1.0, +20), (40, -5.0, +30), (8, -5.0, -60)])
    v = bq.vac(-1)
    assert v is not None and v > 0.6, (
        f"vac={v}: the leg was measured from a clock, not from the pivot -- the "
        f"40 air-minutes before the last push are part of this move")


def test_an_upside_break_is_the_mirror_image():
    bq = BreakQuality()
    warm(bq)
    feed(bq, [(5, -2.0, -50), (20, +8.0, -40)])     # UP on SELLING = air
    assert bq.vac(1) > 0.9
    assert not bq.ok(1)


def test_it_fails_open_before_it_has_a_history():
    """An absent measurement is not evidence of a bad break. A sleeve must never
    be silently stopped by a feature that has not warmed up."""
    bq = BreakQuality()
    feed(bq, [(5, +2.0, +50), (20, -8.0, +40)])     # unambiguous air
    assert bq.threshold() is None
    assert bq.ok(-1), "no history must ALLOW, not block"


def test_it_fails_open_on_a_session_with_no_move():
    bq = BreakQuality()
    warm(bq)
    feed(bq, [(10, 0.0, +5)])
    assert bq.vac(-1) is None
    assert bq.ok(-1)


def test_the_threshold_uses_only_prior_sessions():
    """Today cannot be in its own reference set, or the gate reads the future."""
    bq = BreakQuality(min_sessions=3)
    for v in (0.1, 0.2, 0.3):
        bq.hist.append(v)
    before = bq.threshold()
    feed(bq, [(5, +2.0, +50), (20, -8.0, +40)])
    assert bq.threshold() == before, "today leaked into its own threshold"


def test_rolling_the_session_banks_it_and_clears():
    bq = BreakQuality()
    feed(bq, [(5, +2.0, +50), (20, -8.0, +40)])
    n = len(bq.hist)
    bq.roll_session()
    assert len(bq.hist) == n + 1
    assert bq.vac(-1) is None, "session state must be cleared"
