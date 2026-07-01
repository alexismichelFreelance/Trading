import asyncio

from _helpers import FakeFeed, ScriptedStrategy, mkt

from engine.adapters.brokers.sim import SimBroker
from engine.core.blotter import Blotter
from engine.core.clock import EventClock
from engine.core.engine import ReplayEngine
from engine.core.events import BUY, SELL, Trade


def test_engine_round_trip_and_feedback():
    clk = EventClock()
    broker = SimBroker("ESM5", clk)
    strat = ScriptedStrategy("ESM5", {
        1: [mkt("ESM5", BUY, 1, "entry")],
        3: [mkt("ESM5", SELL, 1, "exit")],
    })
    blotter = Blotter("ESM5", 50.0)
    feed = FakeFeed([
        Trade(1, 5000.0, 1, BUY),
        Trade(2, 5005.0, 1, BUY),
        Trade(3, 5010.0, 1, SELL),
    ])
    eng = ReplayEngine(feed, broker, [strat], clk, blotter)
    asyncio.run(eng.run())

    # one closed round-trip, +10 contract-points gross
    assert len(blotter.trades) == 1
    assert blotter.trades[0].gross_points == 10.0
    # flat-cost reporting subtracts 0.517 per trade
    assert abs(blotter.net_points(flat_cost_pts=0.517) - (10.0 - 0.517)) < 1e-9
    # strategy received its fill callbacks
    assert len(strat.fills) == 2
    assert broker.qty == 0


def test_engine_dispatch_is_causal_order():
    # strategy should only see events up to "now"; verify it sees them in order
    seen = []

    class Recorder(ScriptedStrategy):
        def on_trade(self, e):
            seen.append(e.ts)
            return super().on_trade(e)

    clk = EventClock()
    broker = SimBroker("ESM5", clk)
    strat = Recorder("ESM5", {})
    blotter = Blotter("ESM5", 50.0)
    feed = FakeFeed([Trade(t, 5000.0 + t, 1, BUY) for t in (10, 20, 30)])
    asyncio.run(ReplayEngine(feed, broker, [strat], clk, blotter).run())
    assert seen == [10, 20, 30]
