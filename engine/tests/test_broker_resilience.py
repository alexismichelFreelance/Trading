"""The live order path must survive its socket, and must never take the engine
down with it.

Three defects found 2026-08-01, all on the path that submits REAL orders:

  1. SocketBroker._ensure() returns early whenever _writer is not None, and
     nothing in the codebase ever sets _writer back to None. After a disconnect
     the dead socket is reused forever: _pump_broker loops re-calling events(),
     gets the dead reader, returns instantly, and spins -- while every submit()
     writes into a closed pipe. The feed got a reconnect fix on 2026-07-28; the
     broker never did.
  2. _submit_owned is awaited in the dispatch loop with no exception handling
     anywhere around it, so a ConnectionResetError from drain() propagates out of
     run() and shuts down every strategy in the process, mid-session.
  3. _send awaits drain() unbounded, so a platform that stops reading the broker
     socket stalls ALL trading -- the same blocking-I/O defect already fixed for
     the observer sinks and in the C# relay.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from engine.adapters.brokers.socket_broker import SocketBroker
from engine.core.orders import Order


class Sink:
    """A broker-side listener that records the order messages it receives."""

    def __init__(self) -> None:
        self.msgs: list[dict] = []
        self.server = None
        self.port = 0
        self._conns: list = []

    async def start(self, port: int = 0):
        async def handle(reader, writer):
            self._conns.append(writer)
            try:
                async for raw in reader:
                    line = raw.strip()
                    if line:
                        self.msgs.append(json.loads(line))
            except Exception:                       # noqa: BLE001
                pass

        self.server = await asyncio.start_server(handle, "127.0.0.1", port)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        for w in self._conns:
            try:
                w.close()
            except Exception:                       # noqa: BLE001
                pass
        self._conns.clear()
        self.server.close()
        await self.server.wait_closed()


async def _settle(pred, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


def test_broker_reconnects_after_the_socket_dies():
    """THE DEFECT: kill the far end, bring it back, and the next order must
    actually arrive. Before the fix the order vanished into a dead socket."""
    async def go():
        sink = await Sink().start()
        port = sink.port
        b = SocketBroker(host="127.0.0.1", port=port)

        # Production ALWAYS has _pump_broker consuming events(); that read loop is
        # what observes the peer closing. Mirror it, or the test measures a state
        # the engine never runs in.
        async def pump():
            while True:
                try:
                    async for _ in b.events():
                        pass
                except Exception:                    # noqa: BLE001
                    pass
                await asyncio.sleep(0.02)

        pumper = asyncio.create_task(pump())
        await asyncio.sleep(0.05)
        await b.submit(Order("ES", 1, 1, tag="before"))
        assert await _settle(lambda: len(sink.msgs) == 1), "first order never arrived"

        await sink.stop()                            # platform goes away
        await asyncio.sleep(0.1)
        sink2 = await Sink().start(port=port)        # ...and comes back
        try:
            await b.submit(Order("ES", -1, 1, tag="after"))
            got = await _settle(lambda: len(sink2.msgs) >= 1)
            assert got, "order after reconnect never arrived (dead socket reused)"
            assert sink2.msgs[-1]["tag"] == "after"
            assert b.reconnects >= 1, "reconnect was not recorded"
        finally:
            pumper.cancel()
            await sink2.stop()

    asyncio.run(go())


def test_submit_raises_rather_than_silently_dropping_an_order():
    """If the order genuinely cannot be delivered, the caller MUST hear about it.
    Silently swallowing is worse than failing: the blotter would show a live
    order that does not exist."""
    async def go():
        b = SocketBroker(host="127.0.0.1", port=1)   # nothing listening
        with pytest.raises(OSError):
            await b.submit(Order("ES", 1, 1, tag="doomed"))

    asyncio.run(go())


def test_submit_does_not_block_forever_on_a_stalled_reader():
    """A platform that accepts but never reads must not stall order submission.
    Bounded by send_timeout; the order fails loudly instead of hanging."""
    async def go():
        conns = []

        async def deaf(reader, writer):
            conns.append(writer)
            await asyncio.sleep(3600)                # never reads

        srv = await asyncio.start_server(deaf, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        b = SocketBroker(host="127.0.0.1", port=port, send_timeout=0.25)
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        big = "x" * 200_000                          # overflow the send buffer
        try:
            for i in range(50):
                try:
                    await b.submit(Order("ES", 1, 1, tag=big))
                except (OSError, asyncio.TimeoutError):
                    break
            assert loop.time() - t0 < 10.0, "submit hung on a stalled reader"
        finally:
            srv.close()
            await srv.wait_closed()

    asyncio.run(go())


def test_engine_survives_a_broker_that_cannot_accept_orders():
    """A dead broker must degrade to 'this order failed', never to 'the engine
    stopped'. Other sleeves keep running and the loop keeps consuming the tape."""
    import time

    from engine.core.blotter import Blotter
    from engine.core.clock import WallClock
    from engine.core.events import Trade
    from engine.core.live_engine import LiveEngine

    NS = 1_000_000_000


def _mid_session() -> int:
    """A fixed 11:00 ET instant, well inside RTH.

    NOT time.time_ns(). The engine flattens intraday sleeves during the
    15:59-18:00 ET closing window, so a test anchored to "now" silently gains an
    extra flatten fill when it happens to run in that window -- it would pass or
    fail by time of day. Anchoring makes it deterministic.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return int(datetime(2026, 7, 31, 11, 0,
                        tzinfo=ZoneInfo("America/New_York")).timestamp() * 1e9)


    class Sleeve:
        symbol = "ES"

        def __init__(self):
            self.seen = 0

        def on_trade(self, t):
            self.seen += 1
            return [Order("ES", 1, 1, tag=f"o{self.seen}")]

        def on_fill(self, f):
            return None

        def on_position(self, p):
            return None

    class DeadBroker:
        finite = True

        async def submit(self, o):
            raise ConnectionResetError("broker socket is gone")

        async def events(self):
            return
            yield

    class ListFeed:
        finite = True

        def __init__(self, ev):
            self.ev = ev

        async def stream(self):
            for e in self.ev:
                yield e

    base = _mid_session()
    ev = [Trade(base + i * NS, 7400.0 + i, 1, 1, symbol="ES") for i in range(5)]
    s = Sleeve()
    eng = LiveEngine(ListFeed(ev), DeadBroker(), [s], WallClock(),
                     Blotter("ES", 50.0), warmup_gate=False)   # all LIVE
    asyncio.run(eng.run())                    # must not raise
    assert s.seen == 5, f"engine stopped consuming after a broker error: {s.seen}/5"
    assert eng.failed_orders == 5
