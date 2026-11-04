"""One NT8 execution must update the book exactly once.

The AddOn publishes every account execution to EVERY connected broker client,
and run_live opens one broker client per lane on the same port. On 2026-11-03
that delivered one NQ execution to the ES, NQ and GC sockets, so
`_attribute_fill` applied it three times: a 6-lot flatten became
-6 -> +12 -> -24 -> +48 -> -96 -> +192, and NT8 refused `SELL 192` every 20s
for hours.

The test is deliberately stricter than the incident: the owning lane's socket
does not receive its own fill at all, so only another lane's socket can deliver
it. Idempotency must come from the execution identity, not from which socket
happened to see it first.
"""
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


class OneEntry:
    """Emit exactly one 1-lot order on the first trade of its own lane."""

    def __init__(self, symbol):
        self.symbol = symbol
        self.trades = 0
        self.fills = []

    def on_trade(self, e):
        self.trades += 1
        if self.trades != 1:
            return []
        return [Order(self.symbol, BUY, 1, tag=f"{self.symbol}-entry")]

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
        pass


def test_one_execution_is_attributed_once_when_all_lanes_receive_it():
    symbols = ("ES", "NQ", "GC")

    async def go():
        ready = asyncio.Event()
        writers = []
        subs = {}                                       # writer -> lane symbol

        async def broker(reader, writer):
            writers.append(writer)
            if len(writers) == len(symbols):
                ready.set()
            try:
                async for raw in reader:
                    line = raw.strip()
                    if not line:
                        continue
                    m = json.loads(line)
                    if m.get("t") == "sub":
                        subs[writer] = m.get("symbol", "")
                        continue
                    if m.get("t") != "place":
                        continue
                    fill = {
                        "t": "fill", "ts": 9 * NS, "order_id": m["order_id"],
                        "symbol": m["symbol"], "price": 100.0,
                        "size": m["side"] * m["qty"], "commission": 0.0,
                        "tag": m.get("tag", ""),
                    }
                    # The AddOn broadcasts to every client. Make the test even
                    # harder: the owning lane's socket MISSES this execution,
                    # so only the other lanes receive it. The engine must still
                    # attribute the fill exactly once (by order_id).
                    dead = []
                    for w in writers:
                        if subs.get(w) == m["symbol"]:
                            continue
                        try:
                            w.write((json.dumps(fill) + "\n").encode())
                            await w.drain()
                        except (ConnectionError, RuntimeError):
                            dead.append(w)
                    for w in dead:
                        writers.remove(w)
            finally:
                if writer in writers:
                    writers.remove(writer)

        def market_for(sym):
            async def market(reader, writer):
                await ready.wait()                  # all broker sockets are up
                writer.write((json.dumps({
                    "t": "trade", "ts": 2 * NS, "price": 100.0, "size": 1,
                    "aggressor": 1, "symbol": sym}) + "\n").encode())
                await writer.drain()
                # Hold the socket open: a close is a reconnect (and another
                # trade) in the live engine.
                await asyncio.sleep(30)
            return market

        msrvs = [await asyncio.start_server(market_for(s), "127.0.0.1", 0)
                 for s in symbols]
        bsrv = await asyncio.start_server(broker, "127.0.0.1", 0)
        bport = bsrv.sockets[0].getsockname()[1]

        feeds = [NinjaTraderFeed("127.0.0.1", srv.sockets[0].getsockname()[1],
                                 symbol=s)
                 for srv, s in zip(msrvs, symbols)]
        brokers = {s: NinjaTraderBroker("127.0.0.1", bport, symbol=s)
                   for s in symbols}
        strats = {s: OneEntry(s) for s in symbols}
        blot = Blotter(symbols[0], 50.0)
        for s in symbols[1:]:
            blot.add_instrument(s, 50.0)
        eng = LiveEngine(feeds, brokers, list(strats.values()), WallClock(),
                         blot, drain_timeout=0.3, warmup_gate=False)
        task = asyncio.create_task(eng.run())

        async def until(pred, timeout=10.0):
            loop = asyncio.get_running_loop()
            end = loop.time() + timeout
            while loop.time() < end:
                if pred():
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("fills never landed")

        try:
            await until(lambda: all(len(s.fills) >= 1 for s in strats.values()))
            # Give any duplicate copy time to arrive before asserting.
            await asyncio.sleep(0.5)
        finally:
            eng._stop.set()
            await asyncio.wait_for(task, timeout=10)
            for srv in msrvs + [bsrv]:
                srv.close()

        for sym, strat in strats.items():
            assert len(strat.fills) == 1, (sym, [f.size for f in strat.fills])
            assert strat.fills[0].size == 1
            assert eng.strategy_position(strat) == 1, \
                (sym, eng.strategy_position(strat))
        assert blot.n_fills == len(symbols), \
            f"one execution was counted {blot.n_fills} times"

    asyncio.run(go())
