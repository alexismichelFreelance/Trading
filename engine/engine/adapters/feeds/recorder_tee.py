"""RecorderTee — wraps any live feed and records the normalized 1-minute bar
stream into QuestDB as new historical data: forward-only library building so we
stop re-buying data and every session grows the backtestable sample (e.g. the
HTF-confluence study's limiting factor). Transparent: yields every event onward
unchanged; recording failures never break the feed.

Records 1m Bars (backfill + live) into `claude_bars_live`, a WAL table with
DEDUP UPSERT KEYS(ts, symbol) so re-recording the same bar across sessions is
idempotent. Batched INSERTs of <=100 rows (the /exec GET-URL limit). Raw
tick/trade recording is intentionally out of scope here — that volume needs
QuestDB's ILP path (port 9009), a separate future extension; 1m bars already
serve the zone / confluence / daily-level studies.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

import pandas as pd

from ...core.events import Bar, MarketEvent, Trade
from ..questdb import AsyncQuestDB

log = logging.getLogger("engine.recorder")

BATCH = 100


def _ts(ns: int) -> str:
    return pd.Timestamp(ns // 1000, unit="us", tz="UTC").strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class RecorderTee:
    def __init__(self, inner, qdb: AsyncQuestDB | None = None,
                 symbol: str = "ES", table: str = "claude_bars_live") -> None:
        self.inner = inner
        self.qdb = qdb or AsyncQuestDB()
        self.symbol = symbol
        self.table = table
        self._buf: list[str] = []
        self._live = False
        self._ready = False
        self.n_recorded = 0

    async def _ensure_table(self) -> None:
        await self.qdb.query(
            f"CREATE TABLE IF NOT EXISTS {self.table} (symbol SYMBOL, ts TIMESTAMP, "
            f"o DOUBLE, h DOUBLE, l DOUBLE, c DOUBLE, vol LONG) "
            f"TIMESTAMP(ts) PARTITION BY DAY WAL DEDUP UPSERT KEYS(ts, symbol)")

    async def _flush(self) -> None:
        if not self._buf:
            return
        rows, self._buf = self._buf[:BATCH], self._buf[BATCH:]
        try:
            await self.qdb.query(f"INSERT INTO {self.table} VALUES " + ",".join(rows))
            self.n_recorded += len(rows)
        except Exception as ex:                      # noqa: BLE001 - never break the feed
            log.warning("recorder flush failed (%d rows dropped): %s", len(rows), ex)

    async def stream(self) -> AsyncIterator[MarketEvent]:
        try:
            await self._ensure_table()
            self._ready = True
        except Exception as ex:                      # noqa: BLE001
            log.warning("recorder disabled (table init failed): %s", ex)
            self._ready = False
        async for ev in self.inner.stream():
            if isinstance(ev, Trade):
                self._live = True
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


__all__ = ["RecorderTee"]
