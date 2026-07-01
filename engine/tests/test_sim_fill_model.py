import asyncio

from engine.adapters.brokers.sim import SimBroker
from engine.core.blotter import TradeLedger
from engine.core.clock import EventClock
from engine.core.events import BUY, SELL, Fill, Trade
from engine.core.orders import Order, OrderType


def run(coro):
    return asyncio.run(coro)


def _fills(events):
    return [e for e in events if isinstance(e, Fill)]


def test_market_round_trip_pnl():
    clk = EventClock()
    b = SimBroker("ESM5", clk)
    led = TradeLedger("ESM5", 50.0)

    clk.set(1)
    b.on_market_event(Trade(1, 5000.0, 1, BUY))
    run(b.submit(Order("ESM5", BUY, 2, tag="entry")))
    for f in _fills(b.drain()):
        led.on_fill(f)
    assert b.qty == 2 and b.avg_px == 5000.0

    clk.set(2)
    b.on_market_event(Trade(2, 5010.0, 1, SELL))
    run(b.submit(Order("ESM5", SELL, 2, tag="exit")))
    for f in _fills(b.drain()):
        led.on_fill(f)
    assert b.qty == 0
    assert len(led.trades) == 1
    assert led.trades[0].gross_points == 20.0   # 2 contracts * 10 pts
    assert led.trades[0].tags == ["entry", "exit"]


def test_limit_rests_then_fills_on_cross():
    clk = EventClock()
    b = SimBroker("ESM5", clk)
    clk.set(1)
    b.on_market_event(Trade(1, 5000.0, 1, BUY))
    run(b.submit(Order("ESM5", SELL, 1, OrderType.LIMIT, limit_price=5005.0)))
    assert _fills(b.drain()) == []          # not marketable at 5000

    clk.set(2)
    b.on_market_event(Trade(2, 5006.0, 1, BUY))   # trades up through 5005
    fills = _fills(b.drain())
    assert len(fills) == 1 and fills[0].price == 5005.0 and fills[0].size == -1


def test_stop_triggers_through_level():
    clk = EventClock()
    b = SimBroker("ESM5", clk)
    clk.set(1)
    b.on_market_event(Trade(1, 5000.0, 1, BUY))
    run(b.submit(Order("ESM5", BUY, 1)))          # long 1 @ 5000
    b.drain()
    run(b.submit(Order("ESM5", SELL, 1, OrderType.STOP, stop_price=4990.0, reduce_only=True)))
    assert _fills(b.drain()) == []                # not triggered at 5000

    clk.set(2)
    b.on_market_event(Trade(2, 4989.0, 1, SELL))  # trades down through 4990
    fills = _fills(b.drain())
    assert len(fills) == 1 and fills[0].price == 4990.0
    assert b.qty == 0


def test_reduce_only_when_flat_is_noop():
    clk = EventClock()
    b = SimBroker("ESM5", clk)
    clk.set(1)
    b.on_market_event(Trade(1, 5000.0, 1, BUY))
    run(b.submit(Order("ESM5", BUY, 1, reduce_only=True)))
    assert _fills(b.drain()) == [] and b.qty == 0


def test_flip_through_zero_closes_then_reopens():
    clk = EventClock()
    b = SimBroker("ESM5", clk)
    led = TradeLedger("ESM5", 50.0)
    clk.set(1)
    b.on_market_event(Trade(1, 5000.0, 1, BUY))
    run(b.submit(Order("ESM5", BUY, 2)))
    for f in _fills(b.drain()):
        led.on_fill(f)

    clk.set(2)
    b.on_market_event(Trade(2, 5010.0, 1, SELL))
    run(b.submit(Order("ESM5", SELL, 3)))         # closes 2, flips to short 1
    for f in _fills(b.drain()):
        led.on_fill(f)
    assert b.qty == -1 and b.avg_px == 5010.0
    assert len(led.trades) == 1 and led.trades[0].gross_points == 20.0
