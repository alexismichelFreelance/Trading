"""Flow must be able to GET OUT when the flow it follows reverses.

2026-08-03: NQ:flow went short at 09:28 and was still short at the 15:59 flat,
through a 489-point rally -- the largest up move in weeks. It never reduced.
That is not a bad read, it is a position it could not exit.

The cause is the band selection in on_bookflow:

    band = add_band if held == 0 else (
        add_band if sign(delta) == sign(held) else hold_band)
    if abs(delta) > band:

with add_band=1 and hold_band=5. When the target moves AGAINST the position the
band becomes 5, so for a 1-lot short the target must reach +4 contracts before
`delta` clears the band. Adding costs a band of 1; leaving costs a band of 5.
The smaller the position, the more untouchable it is -- exactly backwards.

The anti-churn intent is real and worth keeping for SIZE. What is not defensible
is that a position can require a bigger signal to exit than it took to enter.
The band for reducing must never exceed the position being reduced.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import BUY, SELL, BookFlow, Trade  # noqa: E402
from engine.strategies.flow import FlowFollowingStrategy  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(hh: int, mm: int, ss: int = 0) -> int:
    return int(datetime(2026, 8, 3, hh, mm, ss, tzinfo=ET).timestamp() * 1e9)


class P:
    def __init__(self, q):
        self.qty = q


def _drive(s: FlowFollowingStrategy, adelta: int, secs: int, start_min: int = 40):
    """Feed `secs` seconds of one-sided aggressor flow, returning every order."""
    out = []
    for k in range(secs):
        t = _ts(10, start_min + k // 60, k % 60)
        side = BUY if adelta > 0 else SELL
        s.on_trade(Trade(t, 20000.0, abs(adelta), side, "NQ"))
        out += s.on_bookflow(BookFlow(t, 0, 0, 0, 0, "NQ"))
    return out


def test_short_one_lot_reduces_when_flow_turns_against_it():
    """THE REPRODUCTION. Short 1, then sustained BUYING flow strong enough to
    target +2 contracts long -- a decisive reversal of the signal the position
    was opened on. delta = 2 - (-1) = 3, under the hold_band of 5, so nothing
    happens and the short rides on.

    Note the flow must be REALISTIC: drive it hard enough to target +40 and even
    the broken band clears. What traps the position is an ordinary reversal.
    """
    s = FlowFollowingStrategy("NQ", gate_utc=None, th=10, scale=3000.0)
    s.on_position(P(-1))
    orders = _drive(s, +50, 240)
    # tag matters: the first BookFlow of a session triggers flow's own reset and
    # emits a `session-flat`. That is not the sleeve reacting to flow, and
    # counting it would make this test pass on the broken code.
    buys = [o for o in orders if o.side > 0 and o.tag == "flow"]
    assert buys, (
        "short 1 lot held through 240s of one-sided BUYING flow and never "
        "reduced -- the exit band is larger than the position")


def test_reducing_never_needs_a_bigger_band_than_the_position():
    """The invariant. Whatever the anti-churn band is, getting OUT of a position
    must not require a larger signal swing than the position's own size."""
    s = FlowFollowingStrategy("NQ", gate_utc=None, th=10, scale=300.0,
                              add_band=1, hold_band=5)
    for held in (-1, -2, 1, 2):
        s.on_position(P(held))
        assert s._reduce_band(held) <= max(1, abs(held)), (
            f"held={held}: reduce band {s._reduce_band(held)} exceeds the "
            f"position it is meant to let out")


def test_large_positions_keep_the_anti_churn_band():
    """The band exists for a reason -- a big position should not thrash on
    noise. Only SMALL positions were being trapped."""
    s = FlowFollowingStrategy("NQ", gate_utc=None, add_band=1, hold_band=5)
    assert s._reduce_band(-20) == 5, "anti-churn band lost for large positions"


def test_adding_still_uses_the_add_band():
    """The fix must not make it easier to ADD; only to leave."""
    s = FlowFollowingStrategy("NQ", gate_utc=None, th=10, scale=300.0)
    s.on_position(P(0))
    orders = _drive(s, +50, 240)
    assert any(o.side > 0 and o.tag == "flow" for o in orders), (
        "flow stopped entering entirely")
