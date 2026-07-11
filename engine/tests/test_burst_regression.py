"""Regression: the 2026-07-09 16:09 ET burst, reproduced end-to-end.

Anatomy of the incident: with fills round-tripping slowly, a sleeve re-emitted
its flatten every tick; each copy was vetted against the CONFIRMED position
(still unreduced), so duplicates passed, overshot the book, and the engine
oscillated (sell 21, sell 21, buy 21, buy 21, sell 21...).

Here: a spam sleeve emits `flatten 21` on EVERY trade while the broker delays
fills by 0.4s. With the RiskSupervisor the engine must submit the entry plus
EXACTLY ONE flatten and end flat — no oscillation, everything else denied."""
import asyncio
import json

from engine.adapters.brokers.ninjatrader import NinjaTraderBroker
from engine.adapters.feeds.ninjatrader import NinjaTraderFeed
from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.events import BUY, SELL
from engine.core.live_engine import LiveEngine
from engine.core.orders import Order
from engine.core.risk import RiskConfig, RiskSupervisor

NS = 1_000_000_000


class SpamFlattener:
    """Buys 21, then emits a full-size flatten on every subsequent trade —
    the July 9 pathology."""
    symbol = "ES"

    def __init__(self):
        self.n = 0
        self.fills = []
        self.positions = []

    def on_trade(self, e):
        self.n += 1
        if self.n == 1:
            return [Order("ES", BUY, 21, tag="entry")]
        return [Order("ES", SELL, 21, tag="flat", reduce_only=True)]

    def on_bar(self, e):
        return []

    def on_quote(self, e):
        return []

    def on_depth(self, e):
        return []

    def on_bookflow(self, e):
        return []

    def on_fill(self, e):
        self.fills.append(e)

    def on_position(self, e):
        self.positions.append(e)


def test_burst_cannot_oscillate():
    s = SpamFlattener()
    submitted = []

    async def go():
        msgs = [{"t": "trade", "ts": (i + 1) * NS, "price": 5000.0, "size": 1,
                 "aggressor": 1} for i in range(12)]

        async def market(reader, writer):
            for m in msgs:
                writer.write((json.dumps(m) + "\n").encode())
                await writer.drain()
                await asyncio.sleep(0.05)          # 12 rapid ticks
            writer.close()

        async def broker(reader, writer):
            async for raw in reader:
                line = raw.strip()
                if not line:
                    continue

                m = json.loads(line)
                if m.get("t") == "place":
                    submitted.append((m["tag"], m["side"] * m["qty"]))
                    await asyncio.sleep(0.4)       # slow fill round-trip
                    writer.write((json.dumps({
                        "t": "fill", "ts": 99 * NS, "order_id": m["order_id"],
                        "symbol": m["symbol"], "price": 5000.0,
                        "size": m["side"] * m["qty"], "commission": 0.0,
                        "tag": m["tag"]}) + "\n").encode())
                    await writer.drain()

        msrv = await asyncio.start_server(market, "127.0.0.1", 0)
        bsrv = await asyncio.start_server(broker, "127.0.0.1", 0)
        risk = RiskSupervisor(RiskConfig())         # in-flight vetting alone
        eng = LiveEngine(NinjaTraderFeed("127.0.0.1", msrv.sockets[0].getsockname()[1]),
                         NinjaTraderBroker("127.0.0.1", bsrv.sockets[0].getsockname()[1]),
                         [s], WallClock(), Blotter("ES", 50.0),
                         drain_timeout=0.7, warmup_gate=False, risk=risk)
        await asyncio.wait_for(eng.run(), timeout=15)
        msrv.close()
        bsrv.close()
        return eng

    eng = asyncio.run(go())
    tags = [t for t, _ in submitted]
    assert tags.count("entry") == 1
    assert tags.count("flat") == 1, f"duplicate flattens got through: {submitted}"
    assert eng.strategy_position(s) == 0            # entry +21, ONE flatten -21
    assert sum(q for _, q in submitted) == 0
    # spam before the entry fill lands is dropped on pos==0 (legacy path);
    # spam after it is DENIED as duplicate-in-flight — both must have fired
    assert eng.risk.denials >= 1
    assert len(s.fills) == 2


def test_rate_limit_stops_tick_refire():
    """Second July 9 pathology: 10 one-lot orders in ~1s from one sleeve."""

    class TickSpammer(SpamFlattener):
        def on_trade(self, e):
            self.n += 1
            return [Order("ES", SELL, 1, tag=f"s{self.n}")]

    s = TickSpammer()
    submitted = []

    async def go():
        msgs = [{"t": "trade", "ts": (i + 1) * NS, "price": 5000.0, "size": 1,
                 "aggressor": 1} for i in range(10)]

        async def market(reader, writer):
            for m in msgs:
                writer.write((json.dumps(m) + "\n").encode())
                await writer.drain()
                await asyncio.sleep(0.04)
            writer.close()

        async def broker(reader, writer):
            async for raw in reader:
                line = raw.strip()
                if not line:
                    continue
                m = json.loads(line)
                if m.get("t") == "place":
                    submitted.append(m["tag"])
                    writer.write((json.dumps({
                        "t": "fill", "ts": 99 * NS, "order_id": m["order_id"],
                        "symbol": m["symbol"], "price": 5000.0,
                        "size": m["side"] * m["qty"], "commission": 0.0,
                        "tag": m["tag"]}) + "\n").encode())
                    await writer.drain()

        msrv = await asyncio.start_server(market, "127.0.0.1", 0)
        bsrv = await asyncio.start_server(broker, "127.0.0.1", 0)
        risk = RiskSupervisor(RiskConfig(rate_max_orders=3, rate_window_s=5.0))
        eng = LiveEngine(NinjaTraderFeed("127.0.0.1", msrv.sockets[0].getsockname()[1]),
                         NinjaTraderBroker("127.0.0.1", bsrv.sockets[0].getsockname()[1]),
                         [s], WallClock(), Blotter("ES", 50.0),
                         drain_timeout=0.5, warmup_gate=False, risk=risk)
        await asyncio.wait_for(eng.run(), timeout=15)
        msrv.close()
        bsrv.close()

    asyncio.run(go())
    assert len(submitted) == 3, f"rate limit failed: {len(submitted)} orders through"
