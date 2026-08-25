"""A sick QuestDB must not be able to starve the engine loop.

THE 2026-08-17 FAILURE. The engine ran flat at 601s lag / queue 0 all night,
broke somewhere between 14:58 and 15:45 local, and ended 245 minutes behind with
47,991 events queued and ZERO session flats that day -- every intraday sleeve
carried its position overnight because the flat is driven by event timestamps
and the engine never reached the 15:59 events.

It is NOT a throughput deficit. The loop benches 3,196 ev/s against ~190 ev/s of
live demand, holding 68 resting orders costs 0.82x, and _check_resting -- the
long-standing suspect -- is 0.34s of a 6.26s profile over 4,000 calls.

What broke was I/O. That day carried 29 recorder flush failures (zero on 08-18
and 08-24) and ILP writer timeouts. Reproduced directly against a socket that
accepts and never answers (scratch starve_test.py):

    no recorder                2,198 ev/s
    recorder -> healthy DB     1,351 ev/s   0.61x
    recorder -> SICK DB           47 ev/s   0.021x

RecorderTee.stream() awaits the flush INLINE in the feed loop, so a hung
database stops the feed yielding and drags the whole loop down with it. The
engine already solved this shape for observers -- `_emit_sink` puts on a bounded
queue that DROPS, because "nothing they touch can apply backpressure to trading".
Recording is an observer too. It must obey the same rule: recorded rows are
worth less than the engine staying current, so a slow write is abandoned, not
waited on.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.feeds.recorder_tee import RecorderTee   # noqa: E402


class _HangingQDB:
    """Accepts every write and never returns."""

    def __init__(self):
        self.calls = 0

    async def query(self, sql):
        self.calls += 1
        await asyncio.sleep(3600)


class _Inner:
    finite = True

    def __init__(self, ev):
        self.ev = ev

    async def stream(self):
        for e in self.ev:
            yield e


def test_a_hung_database_cannot_stall_the_feed():
    """The feed must keep yielding while the recorder is stuck."""
    from engine.core.events import Bar

    ns = 1_786_968_000_000_000_000
    ev = [Bar(ns + i * 60_000_000_000, "1m", 7700.0, 7701.0, 7699.0, 7700.5,
              100, "ES") for i in range(400)]
    qdb = _HangingQDB()
    tee = RecorderTee(_Inner(ev), qdb, symbol="ES", probe_timeout_s=0.1)

    async def drain():
        n = 0
        async for _ in tee.stream():
            n += 1
        return n

    t = time.perf_counter()
    n = asyncio.run(asyncio.wait_for(drain(), timeout=30))
    el = time.perf_counter() - t
    assert n == len(ev), f"feed dropped events: {n} of {len(ev)}"
    assert el < 15, (f"a hung database held the feed for {el:.1f}s -- recording "
                     f"must never apply backpressure to trading")
