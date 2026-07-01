"""Loopback integration test: LiveEngine + NinjaTraderFeed + NinjaTraderBroker
against fake in-process socket servers (no NT8 needed). Validates the full live
path: market JSON -> normalized events -> strategy -> order JSON -> fill JSON ->
strategy.on_fill, plus per-second BookFlow aggregation from depth ticks.
"""
import asyncio
import json

from engine.adapters.brokers.ninjatrader import NinjaTraderBroker
from engine.adapters.feeds.ninjatrader import NinjaTraderFeed
from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.events import BUY, BookFlow, Trade
from engine.core.live_engine import LiveEngine
from engine.core.orders import Order

NS = 1_000_000_000


class OneShotStrategy:
    symbol = "ES"

    def __init__(self):
        self.trades, self.bookflows, self.fills = [], [], []

    def on_trade(self, e):
        self.trades.append(e)
        return [Order("ES", BUY, 1, tag="entry")] if len(self.trades) == 1 else []

    def on_bookflow(self, e):
        self.bookflows.append(e)
        return []

    def on_quote(self, e):
        return []

    def on_depth(self, e):
        return []

    def on_bar(self, e):
        return []

    def on_fill(self, e):
        self.fills.append(e)

    def on_position(self, e):
        return None


async def _market_server(reader, writer):
    msgs = [
        {"t": "trade", "ts": 1 * NS, "price": 5000.0, "size": 3, "aggressor": 1},
        {"t": "depth", "ts": 1 * NS, "side": 1, "price": 4999.75, "size": 50},
        {"t": "depth", "ts": 1 * NS, "side": -1, "price": 5000.25, "size": 40},
        {"t": "trade", "ts": 2 * NS, "price": 5001.0, "size": 2, "aggressor": 1},
        {"t": "depth", "ts": 2 * NS, "side": -1, "price": 5000.25, "size": 10},  # 40->10 = cancel 30
    ]
    for m in msgs:
        writer.write((json.dumps(m) + "\n").encode())
    await writer.drain()
    writer.close()


async def _broker_server(reader, writer):
    async for raw in reader:
        line = raw.strip()
        if not line:
            continue
        m = json.loads(line)
        if m.get("t") == "place":
            signed = m["side"] * m["qty"]
            writer.write((json.dumps({
                "t": "fill", "ts": 2 * NS, "order_id": m["order_id"], "symbol": m["symbol"],
                "price": 5000.0, "size": signed, "commission": 5.28, "tag": m.get("tag", "")}) + "\n").encode())
            writer.write((json.dumps({
                "t": "position", "ts": 2 * NS, "symbol": m["symbol"],
                "qty": signed, "avg_px": 5000.0}) + "\n").encode())
            await writer.drain()


def test_live_loopback_round_trip():
    async def go():
        msrv = await asyncio.start_server(_market_server, "127.0.0.1", 0)
        bsrv = await asyncio.start_server(_broker_server, "127.0.0.1", 0)
        mport = msrv.sockets[0].getsockname()[1]
        bport = bsrv.sockets[0].getsockname()[1]
        strat = OneShotStrategy()
        blot = Blotter("ES", 50.0)
        eng = LiveEngine(NinjaTraderFeed("127.0.0.1", mport),
                         NinjaTraderBroker("127.0.0.1", bport),
                         [strat], WallClock(), blot, drain_timeout=0.3)
        await asyncio.wait_for(eng.run(), timeout=10)
        msrv.close()
        bsrv.close()
        return strat, blot

    strat, blot = asyncio.run(go())
    # market events reached the strategy
    assert len(strat.trades) == 2
    assert all(isinstance(t, Trade) for t in strat.trades)
    # BookFlow was aggregated from depth and closed the second (bid_add 50, ask_add 40 in sec 1)
    assert len(strat.bookflows) >= 1
    bf1 = strat.bookflows[0]
    assert isinstance(bf1, BookFlow) and bf1.bid_add == 50 and bf1.ask_add == 40
    # the order round-tripped platform -> fill -> strategy
    assert len(strat.fills) == 1 and strat.fills[0].size == 1
    assert blot.n_fills == 1
