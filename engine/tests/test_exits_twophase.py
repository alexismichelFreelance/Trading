"""TwoPhaseExit: the arming ruler must mean the same thing everywhere.

`vol_win` used to be a count of note_price() CALLS. A sleeve feeding it from
on_bar got 600 minutes; one feeding it from on_trade got about thirty seconds.
Same arm_mult, ~50x different arming distance, no error and no warning -- the
trade-fed sleeve armed after 3 points instead of 42, which deletes the ride
phase the rule exists to protect. These tests pin the fix: the unit is built on
a fixed one-minute grid, so feed density cannot change it.
"""
from __future__ import annotations

import math

import pytest

from engine.core.exits import ExitCtx, TwoPhaseExit

MIN = 60_000_000_000            # ns per minute


def _walk(n_min: int) -> list[float]:
    """Deterministic sawtooth: +1/-1 per minute, so the 30-minute move has a
    known, non-degenerate distribution."""
    return [5000.0 + (i % 7) - 3.0 for i in range(n_min)]


def test_unit_is_feed_density_invariant():
    """The whole point. Tick-fed and bar-fed sleeves must get the same ruler."""
    px = _walk(120)
    bar_fed = TwoPhaseExit()
    for i, p in enumerate(px):
        bar_fed.note_price(p, i * MIN)                 # one call per minute

    tick_fed = TwoPhaseExit()
    for i, p in enumerate(px):
        for k in range(50):                            # 50 prints per minute
            # intra-minute noise, but the minute's CLOSE is the same price
            mid = p + (0.25 if k < 49 else 0.0)
            tick_fed.note_price(mid if k < 49 else p, i * MIN + k * 1_000_000_000)

    assert bar_fed.unit() == pytest.approx(tick_fed.unit())
    assert bar_fed.unit() > 0


def test_unit_needs_more_than_win_minutes():
    """Five minutes into a session there is no ruler, so the rule must not arm
    -- it must never invent a scale from too little data."""
    e = TwoPhaseExit(win_min=30)
    for i in range(5):
        for k in range(500):                           # a torrent of prints
            e.note_price(5000.0 + k * 0.25, i * MIN + k * 1_000_000)
    assert e.unit() == 0.0                             # 5 minutes, win is 30

    e.start(1, 5000.0)
    # a huge favourable move must NOT arm while the ruler is unknown
    for i in range(20):
        assert e.check(ExitCtx(ts=0, price=5200.0, dir=1,
                               entry_px=5000.0, entry_ts=0)) is None
    assert not e.armed


def test_vol_win_is_minutes_and_evicts():
    e = TwoPhaseExit(win_min=5, vol_win_min=60)
    for i in range(500):                               # 500 minutes of data
        e.note_price(5000.0 + i, i * MIN)
    assert len(e._vol) == 60                           # bounded in MINUTES
    # window holds the last 60 minutes, which rose 1pt/min -> 5-min move is 5
    assert e.unit() == pytest.approx(5.0)


def test_arms_only_past_the_multiple_then_protects():
    e = TwoPhaseExit(arm_mult=4.0, rev_kind="retrace", rev_f=0.5, win_min=5,
                     vol_win_min=60)
    for i in range(120):                               # unit -> 5.0
        e.note_price(5000.0 + i, i * MIN)
    u = e.unit()
    assert u == pytest.approx(5.0)

    e.start(1, 5000.0)
    ctx = lambda p: ExitCtx(ts=0, price=p, dir=1, entry_px=5000.0, entry_ts=0)
    assert e.check(ctx(5010.0)) is None                # +10 < 4 x 5 = 20
    assert not e.armed
    assert e.check(ctx(5025.0)) is None                # +25 >= 20 -> arms, holds
    assert e.armed
    assert e.check(ctx(5024.0)) is None                # tiny give-back, stays in
    assert e.check(ctx(5012.0)) == "twophase-retrace"  # gave back half the run


def test_start_freezes_the_unit_at_entry():
    """A ruler that moves mid-trade would let the arm threshold drift under the
    position. It is fixed the moment the trade opens."""
    e = TwoPhaseExit(arm_mult=4.0, win_min=5, vol_win_min=60)
    for i in range(120):
        e.note_price(5000.0 + i, i * MIN)
    e.start(1, 5000.0)
    frozen = e._unit_at_entry
    assert frozen == pytest.approx(5.0)
    for i in range(120, 240):                          # volatility explodes
        e.note_price(5000.0 + 120 + (i - 120) * 40, i * MIN)
    assert e.unit() > frozen * 5                       # ruler moved a lot
    assert e._unit_at_entry == pytest.approx(frozen)   # the trade's did not


def test_note_price_requires_a_timestamp():
    """Mandatory by design: a sleeve that forgets must fail at the call, not
    silently run a 50x-different rule."""
    e = TwoPhaseExit()
    with pytest.raises(TypeError):
        e.note_price(5000.0)                           # type: ignore[call-arg]


def test_arm_pts_arms_at_a_fixed_distance_with_no_ruler_at_all():
    """The measured-better variant, and the answer to 'what is the session's
    typical move five minutes in?' -- it does not need to know."""
    e = TwoPhaseExit(arm_pts=24.0, rev_kind="retrace", rev_f=0.5)
    assert e.unit() == 0.0                             # never fed a single price
    e.start(1, 5000.0)
    ctx = lambda p: ExitCtx(ts=0, price=p, dir=1, entry_px=5000.0, entry_ts=0)
    assert e.check(ctx(5020.0)) is None                 # +20 < 24
    assert not e.armed
    assert e.check(ctx(5030.0)) is None                 # +30 >= 24 -> arms
    assert e.armed
    assert e.check(ctx(5014.0)) == "twophase-retrace"   # gave back half of +30


def test_arm_pts_overrides_arm_mult():
    e = TwoPhaseExit(arm_mult=999.0, arm_pts=10.0, win_min=5, vol_win_min=60)
    for i in range(120):
        e.note_price(5000.0 + i, i * MIN)              # ruler exists and is 5.0
    e.start(1, 5000.0)
    e.check(ExitCtx(ts=0, price=5011.0, dir=1, entry_px=5000.0, entry_ts=0))
    assert e.armed                                     # 999 x 5.0 would never arm


def test_reset_clears_trade_state_but_keeps_the_ruler():
    e = TwoPhaseExit(win_min=5, vol_win_min=60)
    for i in range(120):
        e.note_price(5000.0 + i, i * MIN)
    e.start(1, 5000.0)
    e.check(ExitCtx(ts=0, price=5100.0, dir=1, entry_px=5000.0, entry_ts=0))
    assert e._adv and e._peak > 0
    e.reset()
    assert e._adv == [] and not e.armed
    assert math.isclose(e.unit(), 5.0)                 # market history survives
