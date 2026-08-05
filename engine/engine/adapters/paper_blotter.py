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

from .ingest_check import (DEFAULT_TIMEOUT_S, PROBE_SYMBOL,
                           verify_ingest_async)
from .questdb import AsyncQuestDB

log = logging.getLogger("engine.paper")


def _ts(ns: int) -> str:
    return pd.Timestamp(ns // 1000, unit="us", tz="UTC").strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class PaperBlotter:
    def __init__(self, qdb: AsyncQuestDB | None = None, symbol: str = "ES",
                 table: str = "claude_paper_fills",
                 probe_timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self.qdb = qdb or AsyncQuestDB()
        self.symbol = symbol
        self.table = table
        self._ready = False
        self.n = 0
        self.probe_timeout_s = probe_timeout_s
        # None = not probed. False = the table takes writes and stores nothing,
        # so this session's entire position record is going nowhere.
        self.ingest_ok: bool | None = None

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
            return
        # THE TABLE THAT MATTERS MOST. On 2026-08-05 claude_paper_fills accepted
        # writes and stored 11 of 56 -- the same broken WAL state as the capture
        # tables, on the one table that holds every position the engine takes.
        # A whole session of fills was lost, so the next restart had nothing to
        # resume and every open position vanished. The startup probes shipped
        # that morning covered the recorders and NOT this. They do now.
        self.ingest_ok = await verify_ingest_async(
            self.qdb, self.table,
            lambda ts: self.qdb.query(
                f"INSERT INTO {self.table} VALUES "
                f"('{_ts(ts)}','{PROBE_SYMBOL}','{PROBE_SYMBOL}',1,0,0.0,"
                f"'{PROBE_SYMBOL}','{PROBE_SYMBOL}-{ts}')"),
            timeout_s=self.probe_timeout_s)
        if self.ingest_ok is False:
            log.error("PAPER BLOTTER: %s IS NOT STORING WRITES. Every position "
                      "this session will be missing from the record and NOTHING "
                      "will resume after a restart. Fix the table before "
                      "trusting anything this session produces.", self.table)

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
