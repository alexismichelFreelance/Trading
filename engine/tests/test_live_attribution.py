"""Per-strategy attribution in LiveEngine (the 2026-07-07 runaway fixes):
- fills route ONLY to the strategy that owns the order; other sleeves and the
  account net never contaminate a sleeve's position
- each owner receives a synthetic PositionUpdate of ITS OWN book
- reduce_only is enforced against the owner's attributed position (dropped
  when flat/wrong-side, clamped when oversized)
- unattributed (manual) fills hit the blotter only
"""
import asyncio
import json

from engine.adapters.brokers.ninjatrader import NinjaTraderBroker
from engine.adapters.feeds.ninjatrader import NinjaTraderFeed
from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.events import BUY, SELL
from engine.core.live_engine import LiveEngine
from engine.core.orders import Order

NS = 1_000_000_000


class Sleeve:
    symbol = "ES"

    def __init__(self, plan):
        # plan: {trade_index: Order-to-emit}
        self.plan = plan
        self.n_trades = 0
        self.fills = []
        self.positions = []

    def on_trade(self, e):
        self.n_trades += 1
        o = self.plan.get(self.n_trades)
        return [o] if o is not None else []

    def on_bar(self, e):
        return []

    def on_bookflow(self, e):
        return []

    def on_quote(self, e):
        return []

    def on_depth(self, e):
        return []

    def on_fill(self, e):
        self.fills.append(e)

    def on_position(self, e):
        self.positions.append(e)


async def _until(pred, timeout=10.0):
    """Wait for a CONDITION, never for a duration. wait_idle() alone races here:
    the last order's fill has to round-trip through the broker socket, and a quiet
    window can elapse while it is still in flight — the test then 'passes' with a
    fill missing (this was intermittent, ~1 run in 3, before this wait existed)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"condition never met within {timeout}s")


def _run(sleeves, extra_broker_lines=None, until=None):
    msgs = [{"t": "trade", "ts": (i + 1) * NS, "price": 5000.0 + i, "size": 1,
             "aggressor": 1} for i in range(4)]

    async def go():
        async def market(reader, writer):
            for m in msgs:
                writer.write((json.dumps(m) + "\n").encode())
                await writer.drain()
                await asyncio.sleep(0.15)      # let fills round-trip (live cadence)
            writer.close()

        async def broker(reader, writer):
            if extra_broker_lines:
                for line in extra_broker_lines:
                    writer.write((json.dumps(line) + "\n").encode())
                await writer.drain()
            async for raw in reader:
                line = raw.strip()
                if not line:
                    continue
                m = json.loads(line)
                if m.get("t") == "place":
                    signed = m["side"] * m["qty"]
                    writer.write((json.dumps({
                        "t": "fill", "ts": 5 * NS, "order_id": m["order_id"],
                        "symbol": m["symbol"], "price": 5000.0, "size": signed,
                        "commission": 0.0, "tag": m.get("tag", "")}) + "\n").encode())
                    await writer.drain()

        msrv = await asyncio.start_server(market, "127.0.0.1", 0)
        bsrv = await asyncio.start_server(broker, "127.0.0.1", 0)
        blot = Blotter("ES", 50.0)
        eng = LiveEngine(NinjaTraderFeed("127.0.0.1", msrv.sockets[0].getsockname()[1]),
                         NinjaTraderBroker("127.0.0.1", bsrv.sockets[0].getsockname()[1]),
                         sleeves, WallClock(), blot,
                         drain_timeout=0.3, warmup_gate=False)
        # A real feed is never `finite`: a clean socket close is a
        # DISCONNECT now (2026-07-28 ES outage), so run() does not return
        # on its own. Wait on the engine's OWN progress, never on a clock.
        _t = asyncio.create_task(eng.run())
        await eng.wait_idle(timeout=10)
        if until is not None:
            await _until(until)          # every expected fill has landed
        eng._stop.set()
        await asyncio.wait_for(_t, timeout=10)
        msrv.close()
        bsrv.close()
        return eng, blot

    return asyncio.run(go())


def test_fills_route_to_owner_only():
    a = Sleeve({1: Order("ES", BUY, 2, tag="a-entry")})
    b = Sleeve({})                                   # b never orders
    eng, blot = _run([a, b])
    assert len(a.fills) == 1 and a.fills[0].size == 2
    assert a.positions and a.positions[-1].qty == 2   # a's OWN book
    assert b.fills == [] and b.positions == []        # b untouched by a's trading
    assert eng.strategy_position(a) == 2 and eng.strategy_position(b) == 0


def test_reduce_only_dropped_when_flat_and_clamped_when_oversized():
    # sleeve tries to "flatten" 5 while owning nothing -> dropped;
    # then buys 1; then tries to reduce 3 -> clamped to 1
    s = Sleeve({1: Order("ES", SELL, 5, tag="ghost-flat", reduce_only=True),
                2: Order("ES", BUY, 1, tag="entry"),
                3: Order("ES", SELL, 3, tag="exit", reduce_only=True)})
    eng, blot = _run([s], until=lambda: len(s.fills) >= 2)
    sizes = [f.size for f in s.fills]
    assert sizes == [1, -1]                           # drop, fill +1, clamp -3 -> -1
    assert eng.strategy_position(s) == 0
    assert s.positions[-1].qty == 0


def test_on_live_fill_fires_for_engine_fills_only():
    painted = []

    class Sleeve2(Sleeve):
        pass

    a = Sleeve({1: Order("ES", BUY, 1, tag="a-entry")})

    async def go():
        msgs = [{"t": "trade", "ts": (i + 1) * NS, "price": 5000.0 + i, "size": 1,
                 "aggressor": 1} for i in range(4)]
        manual = {"t": "fill", "ts": 1, "order_id": "ManualZZ", "symbol": "ES",
                  "price": 5001.0, "size": 2, "commission": 0.0, "tag": "hand"}

        async def market(reader, writer):
            for m in msgs:
                writer.write((json.dumps(m) + "\n").encode())
                await writer.drain()
                await asyncio.sleep(0.15)
            writer.close()

        async def broker(reader, writer):
            writer.write((json.dumps(manual) + "\n").encode())     # a manual fill first
            await writer.drain()
            async for raw in reader:
                line = raw.strip()
                if not line:
                    continue
                m = json.loads(line)
                if m.get("t") == "place":
                    writer.write((json.dumps({
                        "t": "fill", "ts": 5 * NS, "order_id": m["order_id"],
                        "symbol": m["symbol"], "price": 5000.0, "size": m["side"] * m["qty"],
                        "commission": 0.0, "tag": m.get("tag", "")}) + "\n").encode())
                    await writer.drain()

        msrv = await asyncio.start_server(market, "127.0.0.1", 0)
        bsrv = await asyncio.start_server(broker, "127.0.0.1", 0)
        eng = LiveEngine(NinjaTraderFeed("127.0.0.1", msrv.sockets[0].getsockname()[1]),
                         NinjaTraderBroker("127.0.0.1", bsrv.sockets[0].getsockname()[1]),
                         [a], WallClock(), Blotter("ES", 50.0),
                         drain_timeout=0.3, warmup_gate=False)

        async def on_fill(f):
            painted.append((f.order_id, f.price, f.tag))
        eng.on_live_fill = on_fill
        # A real feed is never `finite`: a clean socket close is a
        # DISCONNECT now (2026-07-28 ES outage), so run() does not return
        # on its own. Wait on the engine's OWN progress, never on a clock.
        _t = asyncio.create_task(eng.run())
        await eng.wait_idle(timeout=10)
        eng._stop.set()
        await asyncio.wait_for(_t, timeout=10)
        msrv.close()
        bsrv.close()

    asyncio.run(go())
    # only the engine's own fill is painted; the manual fill is not
    assert len(painted) == 1 and painted[0][2] == "a-entry"
    assert all(oid != "ManualZZ" for oid, _, _ in painted)


def test_manual_fills_do_not_reach_strategies():
    manual = {"t": "fill", "ts": 1, "order_id": "ManualXYZ", "symbol": "ES",
              "price": 5001.0, "size": 3, "commission": 0.0, "tag": "Close"}
    s = Sleeve({})
    eng, blot = _run([s], extra_broker_lines=[manual])
    assert s.fills == [] and s.positions == []        # strategy never sees it
    assert blot.n_fills == 1                          # blotter (account view) does
