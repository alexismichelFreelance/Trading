"""A resting limit fills AT its limit price, not at the price that tripped it.

LiveEngine._check_resting did:

    await self._fill_paper(s, o, px, ts)     # the price that triggered it

Correct for a STOP -- a stop becomes a market order and the trigger is the first
price that actually traded beyond the level, so the only slippage is a real gap.
Wrong for a LIMIT, and wrong in the flattering direction.

A resting limit is already in the book at its price. For the market to print
BEYOND that price it must first trade THROUGH the order. A buy limit at 7757
does not fill at 7756 because the tape printed 7756 -- it fills at 7757, and the
7756 print is what happened after it was filled. Price improvement belongs to
the aggressor, never to the resting side.

The measured effect on the pivot sleeve was ~1 point of phantom improvement per
entry, on every limit fill, in the sleeve's favour -- and it is invisible in P&L
because it looks like the strategy simply doing well.

Second defect in the same place: ES ticks in 0.25 and pivot levels come from
(H+L+C)/3, so orders were resting at prices like 7757.08 and 7784.92 that cannot
exist in the book. A limit must be placed on the instrument's tick grid.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.blotter import Blotter  # noqa: E402
from engine.core.clock import EventClock  # noqa: E402
from engine.core.events import BUY, Trade  # noqa: E402
from engine.core.live_engine import LiveEngine  # noqa: E402
from engine.core.orders import Order, OrderType  # noqa: E402

NS = 1_000_000_000
T0 = 1_785_949_200 * NS


class _Once:
    symbol = "ES"

    def __init__(self, order):
        self.order = order
        self.sent = False

    def on_trade(self, e):
        if self.sent:
            return []
        self.sent = True
        return [self.order]

    def on_fill(self, f):
        return None

    def on_position(self, p):
        return None


class _Feed:
    finite = True

    def __init__(self, evs):
        self.evs = evs

    async def stream(self):
        for e in self.evs:
            yield e


class _Broker:
    async def submit(self, o):
        return None

    async def events(self):
        return
        yield


def _run(order, prints):
    s = _Once(order)
    evs = [Trade(T0 + i * NS, p, 1, BUY, "ES") for i, p in enumerate(prints)]

    async def go():
        eng = LiveEngine(_Feed(evs), _Broker(), [s], EventClock(),
                         Blotter("ES", 50.0), live_owners=set())
        await eng.run()
        return eng

    eng = asyncio.run(go())
    return [f for f in eng.paper_fills if f.tag == "t"]


def _lim(side, px):
    return Order("ES", side, 1, type=OrderType.LIMIT, limit_price=px, tag="t")


def test_buy_limit_fills_at_its_price_when_the_tape_trades_through():
    """THE REGRESSION: filled at 7490 (the print) instead of 7500 (the order)."""
    f = _run(_lim(1, 7500.0), [7510.0, 7490.0])
    assert f, "resting buy limit never filled"
    assert f[0].price == 7500.0, (
        f"filled at {f[0].price} -- that is the tape's price, not the order's. "
        f"A resting limit cannot be improved by the market trading through it.")


def test_sell_limit_fills_at_its_price():
    f = _run(_lim(-1, 7500.0), [7490.0, 7515.0])
    assert f and f[0].price == 7500.0, (
        f"filled at {f[0].price if f else None}, expected the 7500.00 limit")


def test_an_exact_touch_still_fills_at_the_limit():
    f = _run(_lim(1, 7500.0), [7510.0, 7500.0])
    assert f and f[0].price == 7500.0


def test_a_gap_straight_through_does_not_hand_the_resting_side_a_windfall():
    """Price gapping 20 points past a resting buy does not fill it 20 better --
    it was in the book at its price the whole time."""
    f = _run(_lim(1, 7500.0), [7510.0, 7480.0])
    assert f and f[0].price == 7500.0, (
        f"filled at {f[0].price} on a gap -- {7500.0 - f[0].price:+.2f} of "
        f"invented price improvement")


def test_stops_still_fill_at_the_trigger():
    """Unchanged and deliberate: a stop becomes a market order, so the price that
    actually traded beyond the level IS the fill, and a gap is real slippage."""
    o = Order("ES", -1, 1, type=OrderType.STOP, stop_price=7500.0, tag="t")
    f = _run(o, [7510.0, 7488.0])
    assert f and f[0].price == 7488.0, (
        f"stop filled at {f[0].price if f else None}; it must take the trigger "
        f"price 7488.00, not the stop level")


# ── tick alignment ───────────────────────────────────────────────────────────
#
# ES ticks in 0.25. Pivot levels are (H+L+C)/3 and VWAP is a volume-weighted
# mean, so both land on prices that cannot exist in the book -- the pivot sleeve
# was resting orders at 7757.08 and 7784.92. That was harmless while a resting
# limit filled at the TRIGGER price; now that it fills at its own price, an
# unsnapped limit produces a fill at a price the exchange could never print.

def test_a_limit_is_snapped_to_the_instrument_tick():
    """A buy limit must never be snapped UP (that would fill somewhere the order
    could not have rested); a sell limit never DOWN. Round away from the fill."""
    f = _run(_lim(1, 7500.08), [7510.0, 7495.0])
    assert f, "snapped buy limit never filled"
    assert abs(f[0].price % 0.25) < 1e-9, f"{f[0].price} is not on the 0.25 grid"
    assert f[0].price <= 7500.08, f"buy limit snapped UP to {f[0].price}"
    assert f[0].price == 7500.0


def test_a_sell_limit_snaps_the_other_way():
    f = _run(_lim(-1, 7499.92), [7490.0, 7510.0])
    assert f and abs(f[0].price % 0.25) < 1e-9, f"{f[0].price if f else None} off-grid"
    assert f[0].price >= 7499.92, f"sell limit snapped DOWN to {f[0].price}"
    assert f[0].price == 7500.0


def test_an_already_aligned_limit_is_untouched():
    f = _run(_lim(1, 7500.25), [7510.0, 7495.0])
    assert f and f[0].price == 7500.25
