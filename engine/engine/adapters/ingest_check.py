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

This runs ONCE per stream, at startup, and never again. It is a precondition
check, not a background monitor — it answers a question at the only moment the
answer is actionable, when the session has not started and the database can
still be repaired. A failed probe never stops the feed: recording is not worth a
missed trade. It logs loudly and sets a flag the heartbeat surfaces.
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
