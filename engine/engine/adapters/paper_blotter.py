"""PaperBlotter — persists the paper sleeves' fills to QuestDB so every
strategy+variant's paper trades are reviewable (the nightly scorecard reads
them; "I saw a few entries" becomes a queryable table). Paper fills are inline
in the LiveEngine (never sent to the broker), so they exist nowhere else.

Writes to `claude_paper_fills` (WAL DEDUP UPSERT KEYS(ts, order_id); ts is
ns-precise so it is unique per fill and idempotent across reconnects). Low
volume (a few per sleeve per day) so it flushes each fill immediately; a QuestDB
failure never propagates into the engine.
"""
from __future__ import annotations

import logging

import pandas as pd

from .questdb import AsyncQuestDB

log = logging.getLogger("engine.paper")


def _ts(ns: int) -> str:
    return pd.Timestamp(ns // 1000, unit="us", tz="UTC").strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class PaperBlotter:
    def __init__(self, qdb: AsyncQuestDB | None = None, symbol: str = "ES",
                 table: str = "claude_paper_fills") -> None:
        self.qdb = qdb or AsyncQuestDB()
        self.symbol = symbol
        self.table = table
        self._ready = False
        self.n = 0

    async def start(self) -> None:
        try:
            await self.qdb.query(
                f"CREATE TABLE IF NOT EXISTS {self.table} (ts TIMESTAMP, symbol SYMBOL, "
                f"sleeve SYMBOL, side INT, qty INT, price DOUBLE, tag STRING, "
                f"order_id STRING) TIMESTAMP(ts) PARTITION BY DAY WAL "
                f"DEDUP UPSERT KEYS(ts, order_id)")
            self._ready = True
        except Exception as ex:                      # noqa: BLE001
            log.warning("paper blotter disabled (table init failed): %s", ex)
            self._ready = False

    async def record(self, f, sleeve: str) -> None:
        if not self._ready:
            return
        side = 1 if f.size > 0 else -1
        tag = (f.tag or "").replace("'", "")
        try:
            await self.qdb.query(
                f"INSERT INTO {self.table} VALUES ('{_ts(f.ts)}','{f.symbol}','{sleeve}',"
                f"{side},{abs(f.size)},{f.price},'{tag}','{f.order_id}')")
            self.n += 1
        except Exception as ex:                      # noqa: BLE001 - never break the engine
            log.warning("paper fill record failed: %s", ex)


__all__ = ["PaperBlotter"]
