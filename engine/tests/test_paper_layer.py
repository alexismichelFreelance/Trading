"""Paper-trading layer: strategies not in live_owners ALWAYS paper-trade — their
orders fill inline at last_px against their own book, fire on_paper_fill, and are
NOT sent to the broker nor tracked by the risk supervisor. Live strategies are
unchanged (broker + risk)."""
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
        self.plan = plan
        self.n = 0
        self.fills = []
        self.positions = []

    def on_trade(self, e):
        self.n += 1
        o = self.plan.get(self.n)
        return [o] if o is not None else []

    def on_bar(self, e): return []
    def on_quote(self, e): return []
    def on_depth(self, e): return []
    def on_bookflow(self, e): return []

    def on_fill(self, e):
        self.fills.append(e)

    def on_position(self, e):
        self.positions.append(e)


def _run(strategies, live_owners, place_sink):
    msgs = [{"t": "trade", "ts": (i + 1) * NS, "price": 5000.0 + i, "size": 1,
             "aggressor": 1} for i in range(4)]

    async def go():
        async def market(reader, writer):
            for m in msgs:
                writer.write((json.dumps(m) + "\n").encode())
                await writer.drain()
                await asyncio.sleep(0.12)
            writer.close()

        async def broker(reader, writer):
            async for raw in reader:
                line = raw.strip()
                if not line:
                    continue
                m = json.loads(line)
                if m.get("t") == "place":
                    place_sink.append(m["tag"])
                    writer.write((json.dumps({
                        "t": "fill", "ts": 5 * NS, "order_id": m["order_id"],
                        "symbol": m["symbol"], "price": 5000.0, "size": m["side"] * m["qty"],
                        "commission": 0.0, "tag": m.get("tag", "")}) + "\n").encode())
                    await writer.drain()

        msrv = await asyncio.start_server(market, "127.0.0.1", 0)
        bsrv = await asyncio.start_server(broker, "127.0.0.1", 0)
        eng = LiveEngine(NinjaTraderFeed("127.0.0.1", msrv.sockets[0].getsockname()[1]),
                         NinjaTraderBroker("127.0.0.1", bsrv.sockets[0].getsockname()[1]),
                         strategies, WallClock(), Blotter("ES", 50.0),
                         drain_timeout=0.3, warmup_gate=False, live_owners=live_owners)
        papers = []

        async def on_paper(f):
            papers.append(f)
        eng.on_paper_fill = on_paper
        await asyncio.wait_for(eng.run(), timeout=10)
        msrv.close()
        bsrv.close()
        return eng, papers

    return asyncio.run(go())


def test_paper_fills_inline_live_routes_to_broker():
    live = Sleeve({1: Order("ES", BUY, 2, tag="live-entry")})
    paper = Sleeve({1: Order("ES", BUY, 1, tag="paper-entry")})
    placed = []
    eng, papers = _run([live, paper], live_owners={id(live)}, place_sink=placed)

    # LIVE sleeve went to the broker and is risk-tracked
    assert placed == ["live-entry"]                       # ONLY the live order was placed
    assert eng.strategy_position(live) == 2
    assert eng.risk._pos.get(id(live), 0) == 2            # supervisor tracks live

    # PAPER sleeve filled inline, own book updated, visible, NOT risk-tracked
    assert eng.strategy_position(paper) == 1
    assert id(paper) not in eng.risk._pos                 # supervisor ignores paper
    assert len(papers) == 1 and papers[0].tag == "paper-entry"
    assert eng.paper_fills and eng.paper_fills[0].size == 1
    assert paper.positions and paper.positions[-1].qty == 1   # its own PositionUpdate


def test_paper_reduce_only_clamps_against_own_book():
    # paper sleeve: buy 1, then try to sell 5 reduce_only -> clamps to 1 (flat)
    paper = Sleeve({1: Order("ES", BUY, 1, tag="p-entry"),
                    2: Order("ES", SELL, 5, tag="p-exit", reduce_only=True)})
    eng, papers = _run([paper], live_owners=set(), place_sink=[])
    assert [f.size for f in papers] == [1, -1]            # +1 then clamped -1
    assert eng.strategy_position(paper) == 0


def test_default_all_live_preserves_behavior():
    # live_owners=None -> everything routes to the broker (pre-paper behavior)
    a = Sleeve({1: Order("ES", BUY, 1, tag="a")})
    placed = []
    eng, papers = _run([a], live_owners=None, place_sink=placed)
    assert placed == ["a"] and papers == []
    assert eng.strategy_position(a) == 1
