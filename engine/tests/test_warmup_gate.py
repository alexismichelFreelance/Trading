"""LiveEngine warmup gate: backfill bars (bars only, no trades) warm strategy
state but must NOT produce live orders; the first live Trade flips to LIVE and
subsequent signals DO submit. Guards against historical signals filling at the
live price on connect."""
import asyncio
import json

from engine.adapters.brokers.ninjatrader import NinjaTraderBroker
from engine.adapters.feeds.ninjatrader import NinjaTraderFeed
from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.events import BUY
from engine.core.live_engine import LiveEngine
from engine.core.orders import Order

NS = 1_000_000_000


class EveryBarStrategy:
    """Emits an order on EVERY bar and every trade — a worst case for the gate."""
    symbol = "ES"

    def __init__(self):
        self.bars = 0
        self.trades = 0

    def on_bar(self, e):
        self.bars += 1
        return [Order("ES", BUY, 1, tag="bar-order")]

    def on_trade(self, e):
        self.trades += 1
        return [Order("ES", BUY, 1, tag="trade-order")]

    def on_bookflow(self, e):
        return []

    def on_quote(self, e):
        return []

    def on_depth(self, e):
        return []

    def on_fill(self, e):
        return None

    def on_position(self, e):
        return None


def _run(msgs, warmup_gate):
    async def go():
        async def market(reader, writer):
            for m in msgs:
                writer.write((json.dumps(m) + "\n").encode())
            await writer.drain()
            writer.close()

        fills = {"n": 0}

        async def broker(reader, writer):
            async for raw in reader:
                line = raw.strip()
                if not line:
                    continue
                m = json.loads(line)
                if m.get("t") == "place":
                    fills["n"] += 1
                    signed = m["side"] * m["qty"]
                    writer.write((json.dumps({"t": "fill", "ts": 9 * NS, "order_id": m["order_id"],
                                              "symbol": m["symbol"], "price": 5000.0, "size": signed,
                                              "commission": 0.0, "tag": ""}) + "\n").encode())
                    await writer.drain()

        msrv = await asyncio.start_server(market, "127.0.0.1", 0)
        bsrv = await asyncio.start_server(broker, "127.0.0.1", 0)
        mport = msrv.sockets[0].getsockname()[1]
        bport = bsrv.sockets[0].getsockname()[1]
        strat = EveryBarStrategy()
        eng = LiveEngine(NinjaTraderFeed("127.0.0.1", mport),
                         NinjaTraderBroker("127.0.0.1", bport),
                         [strat], WallClock(), Blotter("ES", 50.0),
                         drain_timeout=0.3, warmup_gate=warmup_gate)
        await asyncio.wait_for(eng.run(), timeout=10)
        msrv.close()
        bsrv.close()
        return strat, eng, fills["n"]

    return asyncio.run(go())


def test_backfill_bars_do_not_trade():
    # 3 historical backfill bars, then a live trade, then a live-built context
    msgs = [
        {"t": "bar", "ts": 1 * NS, "tf": "1m", "o": 5000, "h": 5001, "l": 4999, "c": 5000, "v": 10},
        {"t": "bar", "ts": 61 * NS, "tf": "1m", "o": 5000, "h": 5002, "l": 4999, "c": 5001, "v": 12},
        {"t": "bar", "ts": 121 * NS, "tf": "1m", "o": 5001, "h": 5003, "l": 5000, "c": 5002, "v": 8},
        {"t": "trade", "ts": 200 * NS, "price": 5002.0, "size": 1, "aggressor": 1},
    ]
    strat, eng, nfills = _run(msgs, warmup_gate=True)
    assert strat.bars == 3                      # all bars warmed state
    assert eng._backfill_bars == 3
    assert eng._suppressed_orders == 3          # the 3 bar-orders were dropped
    assert nfills == 1                          # only the live trade's order filled


def test_gate_off_lets_bars_trade():
    msgs = [{"t": "bar", "ts": 1 * NS, "tf": "1m", "o": 5000, "h": 5001, "l": 4999, "c": 5000, "v": 10}]
    strat, eng, nfills = _run(msgs, warmup_gate=False)
    assert nfills == 1                          # gate off: the bar order submitted
