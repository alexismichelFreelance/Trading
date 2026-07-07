"""The warmup->live reset (fix for 'zones displayed but never traded'):
during warmup the gate suppresses orders but strategies still mutate trade
state (zones mark fade_done + set self.trade on every historical touch). At the
live flip LiveEngine calls reset_for_live() so strategies start flat with
re-armed zones. Verified at the strategy level (deterministic) + that the engine
invokes it."""
import asyncio
import json

from engine.adapters.brokers.ninjatrader import NinjaTraderBroker
from engine.adapters.feeds.ninjatrader import NinjaTraderFeed
from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.live_engine import LiveEngine
from engine.strategies.zones_strategy import ZoneLifecycleStrategy, _ZoneRec

NS = 1_000_000_000


def test_zone_rearmed_by_reset_for_live():
    s = ZoneLifecycleStrategy("ES")
    # a demand zone that got "used up" during warmup (touched -> faded) and a
    # phantom trade left dangling by the suppressed order
    z = _ZoneRec(k=1, dir=1, top=7543.0, bot=7535.0, fade_done=True, ts=5 * NS)
    s.zones.append(z)
    s.trade = object()                 # phantom warmup trade
    s.pos = 0

    s.reset_for_live()

    assert s.trade is None             # phantom trade cleared
    assert z.fade_done is False        # zone re-armed -> will fade on a live touch
    # a broken zone stays dead
    zb = _ZoneRec(k=2, dir=-1, top=7600.0, bot=7592.0, broke=True, ts=6 * NS)
    zb.fade_done = True
    s.zones.append(zb)
    s.reset_for_live()
    assert zb.fade_done is True and zb.broke is True


def test_engine_calls_reset_on_live_flip():
    calls = {"n": 0}

    class Strat:
        symbol = "ES"

        def reset_for_live(self):
            calls["n"] += 1

        def on_bar(self, e):
            return []

        def on_trade(self, e):
            return []

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

    async def go():
        async def market(reader, writer):
            for m in [{"t": "bar", "ts": 1 * NS, "tf": "1m", "o": 5000, "h": 5001,
                       "l": 4999, "c": 5000, "v": 9},
                      {"t": "trade", "ts": 120 * NS, "price": 5000.0, "size": 1, "aggressor": 1}]:
                writer.write((json.dumps(m) + "\n").encode())
            await writer.drain()
            writer.close()

        async def broker(reader, writer):
            async for _ in reader:
                pass

        msrv = await asyncio.start_server(market, "127.0.0.1", 0)
        bsrv = await asyncio.start_server(broker, "127.0.0.1", 0)
        eng = LiveEngine(NinjaTraderFeed("127.0.0.1", msrv.sockets[0].getsockname()[1]),
                         NinjaTraderBroker("127.0.0.1", bsrv.sockets[0].getsockname()[1]),
                         [Strat()], WallClock(), Blotter("ES", 50.0),
                         drain_timeout=0.3, warmup_gate=True)
        await asyncio.wait_for(eng.run(), timeout=8)
        msrv.close()
        bsrv.close()

    asyncio.run(go())
    assert calls["n"] == 1             # exactly once, at the warmup->live flip
