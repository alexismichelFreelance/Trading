"""RecorderTee — wraps any live feed and records the normalized stream into
QuestDB as new historical data: forward-only library building so we stop
re-buying data and every session grows the backtestable sample. Transparent:
yields every event onward unchanged; recording failures never break the feed.

Records 1m Bars (backfill + live) into `claude_bars_live`, AND — with
record_sec=True — the PER-SECOND aggressor + book-flow features into
`claude_sec_live` (pxc, adelta, avol, ntr, bid/ask add/cancel), the causal
columns that `claude_sec_feat` carries. Without the per-second stream the
ignition and flow sleeves cannot be replayed from recorded data at all — the
whole point of recording is to be able to simulate every strategy, so we record
what every strategy consumes. Both tables are WAL DEDUP UPSERT KEYS(ts, symbol),
batched <=100 rows (the /exec GET-URL limit).
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator

import pandas as pd

from ...core.events import Bar, BookFlow, MarketEvent, Trade
from ..ingest_check import (DEFAULT_TIMEOUT_S, PROBE_SYMBOL,
                            verify_ingest_async)
from ..questdb import AsyncQuestDB

log = logging.getLogger("engine.recorder")

BATCH = 100
SEC_BATCH = 30           # per-second rows per flush (~2 HTTP POSTs/min, not 60)

# RECORDING IS AN OBSERVER. The engine already rules that nothing an observer
# touches may apply backpressure to trading (LiveEngine._emit_sink queues and
# drops rather than wait), and this tee was the last one awaiting a database
# inline in the feed loop. On 2026-08-17 that cost the session: the loop went
# from 2,198 ev/s to 47 against ~190 ev/s of demand, 245 minutes behind, and
# ZERO session flats fired.
#
# THE FIRST FIX WAS WRONG AND IS RECORDED HERE SO IT IS NOT REPEATED. Capping
# each await at 2.0s and dropping on timeout looked safe -- "far beyond a
# healthy write" -- but that number was a guess. Measured afterwards, a 100-row
# INSERT into this QuestDB runs median 0.864s, p90 1.379s, p99 1.951s, max
# 2.089s, so the cap sat ON the p99 of NORMAL operation. Worse, once a lane is
# live every bar flushes on its own, one HTTP round trip per minute per lane, so
# any single slow write lost that bar. 2026-08-25, the first session running it,
# stored 319 of 390 ES RTH bars; every prior session stored 390.
#
# Raising the cap does not work either: SEC_BATCH=30 puts a sec-flush roughly
# every 30 seconds, so any timeout generous enough for a slow database still
# stalls the feed for a large share of the time against a hung one.
#
# So the write LEAVES the feed path. stream() hands batches to a queue and never
# waits; a writer task drains it. A slow database costs LATENCY. Only a database
# so far behind that the queue fills costs rows, and then the loss is reported.
QUEUE_MAX = 256          # ~4 hours of 1-per-minute bars per lane before dropping

# Table creation is a ONE-OFF at stream start, and it stays bounded for a
# different reason than the writes: a hang there leaves _ready False, so the tee
# silently records nothing all session. 10s is ~10x a measured healthy statement
# and still bounds a dead database. It is NOT on the per-event path, so it cannot
# cost rows the way the old 2.0s flush cap did -- but there are TWO such calls,
# so a dead database delays the feed STARTING by up to ~10s. Once. Then never.
DDL_TIMEOUT_S = 5.0

# At END OF STREAM the writer is given this long to land what is still queued.
# Bounded on purpose: an unbounded join hangs shutdown on a dead database, and a
# restart that cannot exit is worse than a few unrecorded rows. This is the ONLY
# place the tee waits on the database, and it is not on the per-event path.
DRAIN_TIMEOUT_S = 10.0


def _ts(ns: int) -> str:
    return pd.Timestamp(ns // 1000, unit="us", tz="UTC").strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class RecorderTee:
    def __init__(self, inner, qdb: AsyncQuestDB | None = None,
                 symbol: str = "ES", table: str = "claude_bars_live",
                 record_sec: bool = True, sec_table: str = "claude_sec_live",
                 probe_timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.inner = inner
        self.qdb = qdb or AsyncQuestDB()
        self.symbol = symbol
        self.table = table
        self.record_sec = record_sec
        self.sec_table = sec_table
        self.n_timeouts = 0          # kept: callers and tests read it
        self.n_dropped = 0
        self._wq: asyncio.Queue | None = None
        self._writer: asyncio.Task | None = None
        self._buf: list[str] = []
        self._sbuf: list[str] = []
        self._live = False
        self._ready = False
        self._sready = False
        self.n_recorded = 0
        self.n_sec = 0
        self.probe_timeout_s = probe_timeout_s
        # None = not probed yet. False = a table accepts writes and stores
        # nothing, so this session's recording is going nowhere.
        self.ingest_ok: bool | None = None
        # per-second trade accumulators (reset on each BookFlow second boundary)
        self._pxc = 0.0
        self._adelta = 0
        self._avol = 0
        self._ntr = 0

    async def _ensure_table(self) -> None:
        # bounded for the same reason as _flush: a hung database must cost the
        # RECORDING, never the feed. Uncapped, the tee never yields its first
        # event and the engine never starts.
        await asyncio.wait_for(self.qdb.query(
            f"CREATE TABLE IF NOT EXISTS {self.table} (symbol SYMBOL, ts TIMESTAMP, "
            f"o DOUBLE, h DOUBLE, l DOUBLE, c DOUBLE, vol LONG) "
            f"TIMESTAMP(ts) PARTITION BY DAY WAL DEDUP UPSERT KEYS(ts, symbol)"),
            timeout=DDL_TIMEOUT_S)

    async def _ensure_sec_table(self) -> None:
        await asyncio.wait_for(self.qdb.query(
            f"CREATE TABLE IF NOT EXISTS {self.sec_table} (symbol SYMBOL, ts TIMESTAMP, "
            f"pxc DOUBLE, adelta LONG, avol LONG, ntr LONG, bid_cancel LONG, "
            f"ask_cancel LONG, bid_add LONG, ask_add LONG) "
            f"TIMESTAMP(ts) PARTITION BY DAY WAL DEDUP UPSERT KEYS(ts, symbol)"),
            timeout=DDL_TIMEOUT_S)

    def _enqueue(self, table: str, rows: list[str]) -> None:
        """Hand rows to the writer. NEVER waits; drops only if the writer is so
        far behind that the queue is full, and says so."""
        if self._wq is None:
            self._wq = asyncio.Queue(QUEUE_MAX)
        try:
            self._wq.put_nowait((table, rows))
        except asyncio.QueueFull:
            self.n_dropped += len(rows)
            if self.n_dropped in (1, 100) or self.n_dropped % 1000 == 0:
                log.error("RECORDER QUEUE FULL: %d rows dropped in total. The "
                          "database is persistently behind, not merely slow. "
                          "The feed is unaffected; the recorded library has "
                          "holes.", self.n_dropped)

    async def _run_writer(self) -> None:
        """Drain the queue. Every database await lives HERE, on its own task,
        where being slow delays recording and nothing else."""
        assert self._wq is not None
        while True:
            table, rows = await self._wq.get()
            try:
                await self.qdb.query(f"INSERT INTO {table} VALUES " + ",".join(rows))
                if table == self.table:
                    self.n_recorded += len(rows)
                else:
                    self.n_sec += len(rows)
            except asyncio.CancelledError:
                raise
            except Exception as ex:                  # noqa: BLE001 - never break
                log.warning("recorder write failed (%d rows dropped): %s",
                            len(rows), ex)
            finally:
                self._wq.task_done()

    async def _flush(self) -> None:
        if not self._buf:
            return
        rows, self._buf = self._buf[:BATCH], self._buf[BATCH:]
        self._enqueue(self.table, rows)

    async def _flush_sec(self) -> None:
        if not self._sbuf:
            return
        rows, self._sbuf = self._sbuf[:SEC_BATCH], self._sbuf[SEC_BATCH:]
        self._enqueue(self.sec_table, rows)

    def _rearm(self) -> None:
        """Reset the state that belongs to ONE run of stream().

        A live feed is infinite, so LiveEngine reconnects by calling stream()
        again on this same object. Two things must not cross that boundary:

        * the per-second accumulators — a disconnect lands mid-second, and the
          trades it stranded would otherwise be folded into the first second
          recorded after the reconnect, whose adelta/avol would then describe
          two moments minutes apart. adelta is what the ignition and flow
          sleeves consume.
        * `_live` — NT8 replays its whole chart on reconnect (9466 bars on
          2026-08-05). With `_live` still True from the previous run, every one
          of those backfill bars takes its own HTTP POST instead of batching,
          flooding QuestDB exactly while the feed is catching up.

        `_buf`/`_sbuf` deliberately survive: rows buffered when the feed dropped
        are still real data and belong in the table.
        """
        self._live = False
        self._pxc = 0.0
        self._adelta = self._avol = self._ntr = 0

    async def _verify_ingest(self) -> bool:
        """Round-trip a probe row into every table this tee writes, through the
        same INSERT path the real rows take. Both tables broke independently on
        2026-08-05, so proving one says nothing about the other."""
        ok = True
        if self._ready:
            ok &= await verify_ingest_async(
                self.qdb, self.table,
                lambda ts: self.qdb.query(
                    f"INSERT INTO {self.table} VALUES "
                    f"('{PROBE_SYMBOL}','{_ts(ts)}',0,0,0,0,0)"),
                timeout_s=self.probe_timeout_s)
        if self._sready:
            ok &= await verify_ingest_async(
                self.qdb, self.sec_table,
                lambda ts: self.qdb.query(
                    f"INSERT INTO {self.sec_table} VALUES "
                    f"('{PROBE_SYMBOL}','{_ts(ts)}',0,0,0,0,0,0,0,0)"),
                timeout_s=self.probe_timeout_s)
        return bool(ok)

    async def stream(self) -> AsyncIterator[MarketEvent]:
        self._rearm()
        self._wq = asyncio.Queue(QUEUE_MAX)
        self._writer = asyncio.create_task(self._run_writer())
        try:
            await self._ensure_table()
            self._ready = True
        except Exception as ex:                      # noqa: BLE001
            log.warning("recorder disabled (table init failed): %s", ex)
            self._ready = False
        if self.record_sec:
            try:
                await self._ensure_sec_table()
                self._sready = True
            except Exception as ex:                  # noqa: BLE001
                log.warning("sec-recorder disabled (table init failed): %s", ex)
                self._sready = False
        self.ingest_ok = await self._verify_ingest()
        async for ev in self.inner.stream():
            if isinstance(ev, Trade):
                self._live = True
                # accumulate this second's aggressor flow (signed by aggressor)
                self._pxc = ev.price
                self._adelta += ev.aggressor * ev.size
                self._avol += ev.size
                self._ntr += 1
            elif isinstance(ev, BookFlow):
                if self._sready and self._ntr > 0:   # close the second: one row
                    self._sbuf.append(
                        f"('{self.symbol}','{_ts(ev.ts)}',{self._pxc},{self._adelta},"
                        f"{self._avol},{self._ntr},{ev.bid_cancel},{ev.ask_cancel},"
                        f"{ev.bid_add},{ev.ask_add})")
                    # BATCH: flush ~2x/min, not every second — a per-second HTTP POST
                    # backpressures the market socket NT8 writes on (chart lag).
                    if len(self._sbuf) >= SEC_BATCH:
                        await self._flush_sec()
                self._adelta = self._avol = self._ntr = 0     # reset for next second
            elif self._ready and isinstance(ev, Bar) and ev.tf == "1m":
                self._buf.append(
                    f"('{self.symbol}','{_ts(ev.ts)}',{ev.o},{ev.h},{ev.l},{ev.c},{int(ev.v)})")
                # backfill floods -> drain in 100-row batches; live bars (1/min)
                # flush immediately for durability
                while len(self._buf) >= BATCH:
                    await self._flush()
                if self._live and self._buf:
                    await self._flush()
            yield ev
        while self._buf:                             # final drain at end-of-stream
            await self._flush()
        while self._sbuf:
            await self._flush_sec()
        if self._wq is not None:                     # let the writer finish
            try:
                await asyncio.wait_for(self._wq.join(), timeout=DRAIN_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("recorder shutdown: %d batches still unwritten after "
                            "%.0fs; abandoning them so the process can exit",
                            self._wq.qsize(), DRAIN_TIMEOUT_S)
        if self._writer is not None:
            self._writer.cancel()


__all__ = ["RecorderTee"]
