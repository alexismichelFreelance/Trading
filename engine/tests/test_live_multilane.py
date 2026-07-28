"""LiveEngine multi-lane: N feeds + {symbol: broker}, per-symbol last_px and
per-lane warmup (a silent NQ feed must never block ES from going live)."""
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


class OnTrade:
    """Orders 1-lot on every trade of its symbol."""

    def __init__(self, symbol):
        self.symbol = symbol
        self.trades = 0
        self.fills = []

    def on_bar(self, e):
        return []

    def on_trade(self, e):
        self.trades += 1
        return [Order(self.symbol, BUY, 1, tag=f"{self.symbol}-t")]

    def on_bookflow(self, e):
        return []

    def on_quote(self, e):
        return []

    def on_depth(self, e):
        return []

    def on_fill(self, e):
        self.fills.append(e)

    def on_position(self, e):
        return None


def _mk_market(msgs):
    async def market(reader, writer):
        for m in msgs:
            writer.write((json.dumps(m) + "\n").encode())
        await writer.drain()
        writer.close()
    return market


def _mk_broker(fills_by_symbol):
    async def broker(reader, writer):
        async for raw in reader:
            line = raw.strip()
            if not line:
                continue
            m = json.loads(line)
            if m.get("t") == "place":
                fills_by_symbol.setdefault(m["symbol"], 0)
                fills_by_symbol[m["symbol"]] += 1
                signed = m["side"] * m["qty"]
                writer.write((json.dumps(
                    {"t": "fill", "ts": 9 * NS, "order_id": m["order_id"],
                     "symbol": m["symbol"], "price": 1.0, "size": signed,
                     "commission": 0.0, "tag": m.get("tag", "")}) + "\n").encode())
                await writer.drain()
    return broker


def test_two_lanes_route_and_warmup_independently():
    async def go():
        es_msgs = [
            {"t": "bar", "ts": 1 * NS, "tf": "1m", "o": 5000, "h": 5001, "l": 4999, "c": 5000, "v": 5},
            {"t": "trade", "ts": 200 * NS, "price": 6000.0, "size": 1, "aggressor": 1},
            {"t": "trade", "ts": 201 * NS, "price": 6001.0, "size": 1, "aggressor": 1},
        ]
        # NQ sends ONLY backfill bars — its lane must stay in warmup,
        # while ES flips live on its first trade.
        nq_msgs = [
            {"t": "bar", "ts": 1 * NS, "tf": "1m", "o": 20000, "h": 20010, "l": 19990, "c": 20000, "v": 5},
            {"t": "bar", "ts": 61 * NS, "tf": "1m", "o": 20000, "h": 20010, "l": 19990, "c": 20005, "v": 5},
        ]
        fills: dict[str, int] = {}
        msrv_es = await asyncio.start_server(_mk_market(es_msgs), "127.0.0.1", 0)
        msrv_nq = await asyncio.start_server(_mk_market(nq_msgs), "127.0.0.1", 0)
        bsrv = await asyncio.start_server(_mk_broker(fills), "127.0.0.1", 0)
        bport = bsrv.sockets[0].getsockname()[1]

        es_feed = NinjaTraderFeed("127.0.0.1", msrv_es.sockets[0].getsockname()[1], symbol="ES")
        nq_feed = NinjaTraderFeed("127.0.0.1", msrv_nq.sockets[0].getsockname()[1], symbol="NQ")
        es_broker = NinjaTraderBroker("127.0.0.1", bport, symbol="ES")
        nq_broker = NinjaTraderBroker("127.0.0.1", bport, symbol="NQ")
        s_es, s_nq = OnTrade("ES"), OnTrade("NQ")
        eng = LiveEngine([es_feed, nq_feed], {"ES": es_broker, "NQ": nq_broker},
                         [s_es, s_nq], WallClock(), Blotter("ES", 50.0),
                         drain_timeout=0.3, warmup_gate=True)
        # A real feed is never `finite`: a clean socket close is a
        # DISCONNECT now (2026-07-28 ES outage), so run() does not return
        # on its own. Wait on the engine's OWN progress, never on a clock.
        _t = asyncio.create_task(eng.run())
        await eng.wait_idle(timeout=10)
        eng._stop.set()
        await asyncio.wait_for(_t, timeout=10)
        for srv in (msrv_es, msrv_nq, bsrv):
            srv.close()
        return s_es, s_nq, eng, fills

    s_es, s_nq, eng, fills = asyncio.run(go())
    # ES lane went live and traded; NQ lane never left warmup
    assert "ES" in eng._live_lanes and "NQ" not in eng._live_lanes
    assert fills.get("ES", 0) == 2 and "NQ" not in fills
    # event isolation: each strategy saw only its lane
    assert s_es.trades == 2 and s_nq.trades == 0
    # fills routed to their owners only
    assert len(s_es.fills) == 2 and len(s_nq.fills) == 0
    # per-symbol last price: ES has its trade price; NQ its backfill close
    assert eng.px_for("ES") == 6001.0
    assert eng.px_for("NQ") == 20005.0
