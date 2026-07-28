"""A live feed that ends CLEANLY must reconnect, loudly — never die silently.

2026-07-28: the ES relay socket closed cleanly at 11:47:40 ET (the NT8 DOM
close button resets the chart's strategy). asyncio's StreamReader ends its
iteration without raising, the pump took its `return` path, and the ES lane
was gone for the rest of the session. NQ kept streaming so the engine looked
healthy. 4h15m of tick/depth/bar capture lost, with no error anywhere.
"""
import asyncio

from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.events import Trade
from engine.core.live_engine import LiveEngine


class _DropsThenRecovers:
    # deliberately NOT finite: this simulates a LIVE socket closing cleanly
    """Ends its stream cleanly twice (a socket close), then keeps serving."""
    symbol = "ES"

    def __init__(self):
        self.connects = 0

    async def stream(self):
        self.connects += 1
        yield Trade(1_000_000_000 * self.connects, 100.0 + self.connects, 1, 1, "ES")
        return                      # clean end == disconnect, NOT "finished"


class _Bounded:
    symbol = "NQ"
    finite = True                   # declares itself done

    async def stream(self):
        yield Trade(1_000_000_000, 200.0, 1, 1, "NQ")


class _NullBroker:
    finite = True

    async def events(self):
        return
        yield

    async def submit(self, o):
        pass


def test_clean_disconnect_reconnects_instead_of_dying():
    asyncio.run(_clean_disconnect_reconnects())


async def _clean_disconnect_reconnects():
    feed = _DropsThenRecovers()
    eng = LiveEngine(feed, _NullBroker(), [], WallClock(), Blotter("ES", 50.0),
                     reconnect_delay=0.01, warmup_gate=False)
    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.25)
    eng._stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert feed.connects > 1, "clean stream end must be retried, not terminal"
    assert eng._disconnects >= 1, "a disconnect must be COUNTED, never silent"


def test_finite_feed_still_terminates():
    asyncio.run(_finite_terminates())


async def _finite_terminates():
    """Bounded feeds must not be turned into infinite reconnect loops."""
    eng = LiveEngine(_Bounded(), _NullBroker(), [], WallClock(),
                     Blotter("NQ", 20.0), reconnect_delay=0.01, warmup_gate=False)
    await asyncio.wait_for(eng.run(), timeout=5)


def test_disconnect_fires_the_callback():
    asyncio.run(_disconnect_callback())


async def _disconnect_callback():
    seen = []
    feed = _DropsThenRecovers()
    eng = LiveEngine(feed, _NullBroker(), [], WallClock(), Blotter("ES", 50.0),
                     reconnect_delay=0.01, warmup_gate=False)

    async def cb(name, attempt, got):
        seen.append((name, attempt, got))

    eng.on_feed_disconnect = cb
    task = asyncio.create_task(eng.run())
    await asyncio.sleep(0.25)
    eng._stop.set()
    await asyncio.wait_for(task, timeout=5)
    assert seen and seen[0][0] == "ES" and seen[0][1] == 1
