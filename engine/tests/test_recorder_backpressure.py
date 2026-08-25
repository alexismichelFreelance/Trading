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
        n, t0 = 0, time.perf_counter()
        async for _ in tee.stream():
            n += 1
            if n == len(ev):
                stream_el.append(time.perf_counter() - t0)   # last event delivered
        return n

    stream_el = []
    n = asyncio.run(asyncio.wait_for(drain(), timeout=60))
    assert n == len(ev), f"feed dropped events: {n} of {len(ev)}"
    # A dead database costs the two bounded table-creation calls ONCE at
    # startup (2 x DDL_TIMEOUT_S) and nothing per event thereafter. 400 bars
    # must not add to that.
    assert stream_el and stream_el[0] < 13, (
        f"a hung database held the feed for {stream_el[0]:.1f}s -- recording "
        f"must never apply backpressure to trading")


def test_a_SLOW_database_must_not_lose_rows():
    """THE REGRESSION I SHIPPED. FLUSH_TIMEOUT_S=2.0 was chosen by judgement and
    called "far beyond a healthy write". Measured afterwards, a 100-row INSERT
    into this QuestDB runs median 0.864s, p90 1.379s, p99 1.951s, max 2.089s --
    the timeout sat ON the p99 of NORMAL operation and was cancelling healthy
    writes. 2026-08-25, the first session running it, recorded 319 of 390 ES RTH
    bars; every prior session recorded 390.

    Raising the number does not fix it either: SEC_BATCH=30 means a sec-flush
    roughly every 30 seconds, so any timeout long enough to be safe for a slow
    database still stalls the feed for a large fraction of the time against a
    hung one.

    So the write comes OFF the feed path, exactly as LiveEngine._emit_sink does
    for observers: the feed hands rows to a queue and never waits. A slow
    database then costs LATENCY, not rows -- which is the trade that should have
    been made in the first place.
    """
    from engine.core.events import Bar

    class _SlowQDB:
        """DDL and probes answer instantly; INSERTs are slow. That split
        matters: bars only buffer once _ensure_table has SUCCEEDED, so a stub
        that is slow at everything leaves _ready False and the test passes
        without recording a single row."""

        def __init__(self):
            self.rows = 0

        async def query(self, sql):
            if not sql.lstrip().upper().startswith("INSERT"):
                return {}
            await asyncio.sleep(2.5)          # past the old 2.0s cap; the
            # measured max for a healthy 100-row batch was 2.089s, and the
            # 18% loss on 2026-08-25 proves live writes exceed it routinely
            self.rows += sql.count("),(") + 1
            return {}

    from engine.core.events import Trade
    ns = 1_786_968_000_000_000_000
    # a Trade first: _live gates the per-bar flush, which is the LIVE path
    ev = [Trade(ns, 7700.0, 1, 1, "ES")]
    ev += [Bar(ns + i * 60_000_000_000, "1m", 7700.0, 7701.0, 7699.0, 7700.5,
               100, "ES") for i in range(20)]
    qdb = _SlowQDB()
    tee = RecorderTee(_Inner(ev), qdb, symbol="ES", record_sec=False,
                      probe_timeout_s=0.1)

    async def drain():
        n, t0 = 0, time.perf_counter()
        async for _ in tee.stream():
            n += 1
            if n == len(ev):
                stream_el.append(time.perf_counter() - t0)
        return n

    stream_el = []
    n = asyncio.run(asyncio.wait_for(drain(), timeout=180))
    assert n == len(ev)
    assert stream_el and stream_el[0] < 5, (
        f"a slow database held the feed for {stream_el[0]:.1f}s -- it must cost "
        f"LATENCY, not the feed")
    assert tee.n_dropped == 0, (
        f"{tee.n_dropped} rows dropped -- a merely SLOW database must cost "
        f"latency, not rows")
