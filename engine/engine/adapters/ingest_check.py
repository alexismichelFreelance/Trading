"""Prove a table actually ingests, once, before the session starts.

A write path can report complete success and store nothing. On 2026-08-05
claude_ticks_live, claude_depth_live and claude_sec_live accepted every write
and applied none — not suspended, no error, no exception. ILP `sendall`
returned, `INSERT` returned OK, the row counters climbed. The tables had been
dead since the previous afternoon, through a whole session, unnoticed, because
every signal the engine had counted rows SENT.

So the counters cannot be trusted to answer "is this working". Only a round trip
can: write one row through the same path the real data will take, then read it
back.

This runs at startup AND on a timer for the rest of the session (see
`recheck_ingest` on RawCaptureTee and RecorderTee). It used to run once and
never again, on the reasoning that startup was "the only moment the answer is
actionable, when the database can still be repaired". 2026-09-01 disproved
that: QuestDB saturated around 14:30, stopped committing, and the engine ran to
16:13 believing itself healthy — dropped=0, every counter climbing, the
heartbeat's TABLE-NOT-INGESTING alarm structurally unable to fire because the
flag had been decided at 09:30. The last ~90 minutes of RTH were lost on every
table.

Mid-session the answer is still actionable; it is simply a different answer.
Not "repair before you start" but "everything you record from here is going
nowhere" — worth knowing while the session runs rather than the next morning.

A failed probe never stops the feed: recording is not worth a missed trade. It
logs loudly and sets a flag the heartbeat surfaces.
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger("engine.ingest")

# Deliberately unmistakable: no instrument is ever called this, so a probe row
# can never be confused with market data by any consumer or any human.
PROBE_SYMBOL = "__ingest_probe__"

# QuestDB applies WAL commits asynchronously, so an immediate read can miss a
# row that is perfectly fine. Poll until it shows up or the budget runs out.
DEFAULT_TIMEOUT_S = 15.0
POLL_S = 0.5


def _count_sql(table: str, ts_ns: int) -> str:
    # microseconds: QuestDB timestamps are us-precision
    return (f"select count() from {table} where symbol = '{PROBE_SYMBOL}' "
            f"and ts = {ts_ns // 1000}")


def _probe_ts() -> int:
    return time.time_ns()


def _explain(table: str, waited: float) -> None:
    log.error(
        "INGEST PROBE FAILED: %s is NOT LANDING WRITES. A probe row was written "
        "through the real write path and was still not readable %.1fs later. The "
        "table is accepting writes and storing nothing, so every row recorded "
        "into it this session will be lost silently. Recording continues so the "
        "feed is never blocked, but treat this table as DEAD until repaired.",
        table, waited)


def verify_ingest(qdb, table: str, write_probe, timeout_s: float = DEFAULT_TIMEOUT_S,
                  poll_s: float = POLL_S) -> bool:
    """Sync round trip. `write_probe(ts_ns)` must write one row tagged
    PROBE_SYMBOL at that timestamp, using the caller's real write path."""
    ts = _probe_ts()
    try:
        write_probe(ts)
    except Exception as ex:                      # noqa: BLE001 - never break the feed
        log.error("INGEST PROBE FAILED: %s could not be written at all: %s", table, ex)
        return False
    started = time.monotonic()
    while True:
        try:
            if qdb.query(_count_sql(table, ts))["dataset"][0][0]:
                return True
        except Exception:                        # noqa: BLE001 - DB may be catching up
            pass
        waited = time.monotonic() - started
        if waited >= timeout_s:
            _explain(table, waited)
            return False
        time.sleep(min(poll_s, timeout_s - waited))


async def verify_ingest_async(qdb, table: str, write_probe,
                              timeout_s: float = DEFAULT_TIMEOUT_S,
                              poll_s: float = POLL_S) -> bool:
    """Async twin. `write_probe(ts_ns)` may be a coroutine function."""
    ts = _probe_ts()
    try:
        r = write_probe(ts)
        if asyncio.iscoroutine(r):
            await r
    except Exception as ex:                      # noqa: BLE001
        log.error("INGEST PROBE FAILED: %s could not be written at all: %s", table, ex)
        return False
    started = time.monotonic()
    while True:
        try:
            res = await qdb.query(_count_sql(table, ts))
            if res["dataset"][0][0]:
                return True
        except Exception:                        # noqa: BLE001
            pass
        waited = time.monotonic() - started
        if waited >= timeout_s:
            _explain(table, waited)
            return False
        await asyncio.sleep(min(poll_s, timeout_s - waited))


__all__ = ["verify_ingest", "verify_ingest_async", "PROBE_SYMBOL",
           "DEFAULT_TIMEOUT_S"]
