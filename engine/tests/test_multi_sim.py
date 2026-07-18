"""Multi-instrument co-simulation: MergeFeed + SymbolRouterBroker + filtered
dispatch. The anchor assertion is COMPOSITION INVARIANCE — a two-instrument run
produces, per symbol, exactly the same trades as two independent single runs."""
import asyncio

import pytest

from engine.adapters.brokers.router import SymbolRouterBroker
from engine.adapters.brokers.sim import CleanFill, SimBroker
from engine.adapters.feeds.merge import MergeFeed
from engine.core.blotter import Blotter
from engine.core.clock import EventClock
from engine.core.engine import ReplayEngine
from engine.core.events import BUY, SELL, Trade
from engine.core.orders import Order, OrderType
from engine.strategies.base import BaseStrategy

NS = 1_000_000_000


class ListFeed:
    def __init__(self, events):
        self.events = list(events)

    async def stream(self):
        for e in self.events:
            yield e


class EveryN(BaseStrategy):
    """Deterministic probe: alternates 1-lot buy/sell every `n` trades."""

    def __init__(self, symbol: str, n: int):
        self.symbol = symbol
        self.n = n
        self.count = 0
        self.side = 1
        self.seen: list[Trade] = []

    def on_trade(self, e):
        self.seen.append(e)
        self.count += 1
        if self.count % self.n == 0:
            o = Order(self.symbol, self.side, 1, OrderType.MARKET)
            self.side = -self.side
            return [o]
        return []


def es_events():
    return [Trade(i * NS, 6000.0 + 0.25 * i, 1, BUY if i % 2 else SELL, "ES")
            for i in range(1, 21)]


def nq_events():
    # offset ts by half-second so lanes interleave in the merge
    return [Trade(i * NS + NS // 2, 20000.0 - 1.0 * i, 2, BUY, "NQ")
            for i in range(1, 21)]


def run_single(events, symbol, point_usd, n):
    clk = EventClock()
    strat = EveryN(symbol, n)
    blot = Blotter(symbol, point_usd)
    eng = ReplayEngine(ListFeed(events), SimBroker(symbol, clk, CleanFill()),
                       [strat], clk, blot)
    asyncio.run(eng.run())
    return strat, blot


def run_multi(n_es=3, n_nq=4):
    clk = EventClock()
    es, nq = EveryN("ES", n_es), EveryN("NQ", n_nq)
    broker = SymbolRouterBroker({"ES": SimBroker("ES", clk, CleanFill()),
                                 "NQ": SimBroker("NQ", clk, CleanFill())})
    blot = Blotter("ES", 50.0)
    blot.add_instrument("NQ", 20.0)
    feed = MergeFeed([ListFeed(es_events()), ListFeed(nq_events())])
    eng = ReplayEngine(feed, broker, [es, nq], clk, blot)
    asyncio.run(eng.run())
    return es, nq, blot


def _sig(trades, sym):
    return [(t.symbol, t.entry_ts, t.entry_px, t.exit_ts, t.exit_px, t.gross_points)
            for t in trades if t.symbol == sym]


def test_merge_feed_deterministic_order():
    async def collect():
        return [e async for e in
                MergeFeed([ListFeed(es_events()), ListFeed(nq_events())]).stream()]
    out = asyncio.run(collect())
    assert len(out) == 40
    assert all(out[i].ts <= out[i + 1].ts for i in range(len(out) - 1))
    # ties impossible here (offset), but ordering must be reproducible
    out2 = asyncio.run(collect())
    assert [(e.ts, e.symbol) for e in out] == [(e.ts, e.symbol) for e in out2]


def test_merge_feed_tie_break_by_feed_index():
    a = [Trade(NS, 1.0, 1, BUY, "A")]
    b = [Trade(NS, 2.0, 1, BUY, "B")]

    async def collect(feeds):
        return [e.symbol async for e in MergeFeed(feeds).stream()]
    assert asyncio.run(collect([ListFeed(a), ListFeed(b)])) == ["A", "B"]
    assert asyncio.run(collect([ListFeed(b), ListFeed(a)])) == ["B", "A"]


def test_event_isolation():
    es, nq, _ = run_multi()
    assert all(e.symbol == "ES" for e in es.seen) and len(es.seen) == 20
    assert all(e.symbol == "NQ" for e in nq.seen) and len(nq.seen) == 20


def test_fill_routing_and_composition_invariance():
    es_m, nq_m, blot_m = run_multi()
    es_s, blot_es = run_single(es_events(), "ES", 50.0, 3)
    nq_s, blot_nq = run_single(nq_events(), "NQ", 20.0, 4)
    # per-symbol trade sequences byte-equal between multi and single runs
    assert _sig(blot_m.trades, "ES") == _sig(blot_es.trades, "ES")
    assert _sig(blot_m.trades, "NQ") == _sig(blot_nq.trades, "NQ")
    assert blot_m.trades                                # sanity: something traded
    # portfolio dollars = sum of the independent runs
    assert blot_m.net_usd() == pytest.approx(blot_es.net_usd() + blot_nq.net_usd())


def test_router_rejects_unknown_symbol():
    clk = EventClock()
    broker = SymbolRouterBroker({"ES": SimBroker("ES", clk, CleanFill())})

    async def go():
        await broker.submit(Order("GC", 1, 1, OrderType.MARKET))
    with pytest.raises(KeyError):
        asyncio.run(go())
