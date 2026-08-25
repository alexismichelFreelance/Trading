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

# A flush gets this long and no longer. RECORDING IS AN OBSERVER: the engine
# already rules that nothing an observer touches may apply backpressure to
# trading (LiveEngine._emit_sink drops rather than wait), and the recorder was
# the one observer still awaiting a database inline in the feed loop.
#
# On 2026-08-17 that cost the whole session: 29 flush failures, the loop dragged
# from 2,198 ev/s to 47 against ~190 ev/s of demand, the engine 245 minutes
# behind with 47,991 events queued, and ZERO session flats -- so every intraday
# sleeve carried its position overnight. Reproduced in
# tests/test_recorder_backpressure.py.
#
# 2.0s is well beyond a healthy write (single-digit ms) and far short of the
# 60s client default, which is what turned a slow database into a stalled
# engine. A row that cannot be written in two seconds is dropped: the recorded
# library is worth less than the engine being current.
FLUSH_TIMEOUT_S = 2.0


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
        self.n_timeouts = 0
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
            timeout=FLUSH_TIMEOUT_S)

    async def _ensure_sec_table(self) -> None:
        await asyncio.wait_for(self.qdb.query(
            f"CREATE TABLE IF NOT EXISTS {self.sec_table} (symbol SYMBOL, ts TIMESTAMP, "
            f"pxc DOUBLE, adelta LONG, avol LONG, ntr LONG, bid_cancel LONG, "
            f"ask_cancel LONG, bid_add LONG, ask_add LONG) "
            f"TIMESTAMP(ts) PARTITION BY DAY WAL DEDUP UPSERT KEYS(ts, symbol)"),
            timeout=FLUSH_TIMEOUT_S)

    async def _flush(self) -> None:
        if not self._buf:
            return
        rows, self._buf = self._buf[:BATCH], self._buf[BATCH:]
        try:
            await asyncio.wait_for(
                self.qdb.query(f"INSERT INTO {self.table} VALUES " + ",".join(rows)),
                timeout=FLUSH_TIMEOUT_S)
            self.n_recorded += len(rows)
        except asyncio.TimeoutError:
            self.n_timeouts += 1
            if self.n_timeouts in (1, 10) or self.n_timeouts % 100 == 0:
                log.error("RECORDER ABANDONED A WRITE after %.1fs (%d rows dropped, "
                          "%d timeouts). The database is not keeping up. Rows are "
                          "being lost ON PURPOSE so the engine stays current -- see "
                          "FLUSH_TIMEOUT_S.", FLUSH_TIMEOUT_S, len(rows),
                          self.n_timeouts)
        except Exception as ex:                      # noqa: BLE001 - never break the feed
            log.warning("recorder flush failed (%d rows dropped): %s", len(rows), ex)

    async def _flush_sec(self) -> None:
        if not self._sbuf:
            return
        rows, self._sbuf = self._sbuf[:BATCH], self._sbuf[BATCH:]
        try:
            await asyncio.wait_for(
                self.qdb.query(f"INSERT INTO {self.sec_table} VALUES " + ",".join(rows)),
                timeout=FLUSH_TIMEOUT_S)
            self.n_sec += len(rows)
        except asyncio.TimeoutError:
            self.n_timeouts += 1
        except Exception as ex:                      # noqa: BLE001
            log.warning("sec-recorder flush failed (%d rows dropped): %s", len(rows), ex)

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


__all__ = ["RecorderTee"]
