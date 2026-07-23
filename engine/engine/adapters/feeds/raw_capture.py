"""RawCaptureTee — records the FULL raw feed (every trade + every L2 book
update, all levels) for faithful replay, WITHOUT ever backpressuring the feed.

Design (money-critical: the feed must never wait on the database):
  hot path (event loop):  put_nowait() a lightweight tuple onto a bounded queue.
                          O(1), no I/O, never blocks — so a slow/stalled DB
                          cannot lag the NT8 socket (unlike the inline HTTP
                          inserts in RecorderTee).
  writer THREAD:          drains the queue in batches and pushes via QuestDB
                          ILP over TCP (port 9009) — the high-throughput ingest
                          path. Reconnects on failure. Runs entirely off the
                          asyncio loop and off NT8's process.
  overflow:               if the writer ever falls behind the queue cap, rows
                          are DROPPED and the drop counter is logged LOUDLY — a
                          gap in money-critical data is never silent. (Chosen
                          over blocking, which would reintroduce feed backpressure.)

Captured onward: Trade + DepthUpdate are recorded; DepthUpdate is NOT forwarded
to the engine (nothing downstream consumes raw depth — the per-second BookFlow
the strategies use is still produced by NinjaTraderFeed). Requires the inner
feed to emit depth (NinjaTraderFeed(emit_depth=True)).

Tables (WAL, partition by DAY, no dedup — raw append stream):
  claude_ticks_live(symbol, price, size, aggressor, ts)
  claude_depth_live(symbol, side, level, price, size, ts)
"""
from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from collections.abc import AsyncIterator

from ...core.events import DepthUpdate, MarketEvent, Trade
from ..questdb import QuestDB

log = logging.getLogger("engine.rawcapture")

TICKS = "claude_ticks_live"
DEPTH = "claude_depth_live"


class RawCaptureTee:
    def __init__(self, inner, symbol: str = "ES", host: str = "127.0.0.1",
                 ilp_port: int = 9009, queue_cap: int = 500_000,
                 batch: int = 5000, flush_ms: int = 250, log_every_s: float = 15.0,
                 qdb: QuestDB | None = None) -> None:
        self.inner = inner
        self.symbol = symbol
        self.host, self.ilp_port = host, ilp_port
        self.batch, self.flush_ms = batch, flush_ms
        self.log_every_s = log_every_s
        self._qdb = qdb or QuestDB()
        self._q: queue.Queue = queue.Queue(maxsize=queue_cap)
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        # counters (read by the monitor; thread-safe enough for logging)
        self.n_written = 0
        self.n_dropped = 0
        self._last_drop_log = 0.0

    # ── table DDL (one-time, via HTTP; ILP would auto-create but without WAL/part) ─
    def _ensure_tables(self) -> None:
        self._qdb.query(
            f"CREATE TABLE IF NOT EXISTS {TICKS} (symbol SYMBOL, price DOUBLE, "
            f"size LONG, aggressor LONG, ts TIMESTAMP) TIMESTAMP(ts) "
            f"PARTITION BY DAY WAL")
        self._qdb.query(
            f"CREATE TABLE IF NOT EXISTS {DEPTH} (symbol SYMBOL, side LONG, "
            f"level LONG, price DOUBLE, size LONG, ts TIMESTAMP) TIMESTAMP(ts) "
            f"PARTITION BY DAY WAL")

    # ── hot path: enqueue, never block ────────────────────────────────────
    def _push(self, row: tuple) -> None:
        try:
            self._q.put_nowait(row)
        except queue.Full:
            self.n_dropped += 1
            now = time.monotonic()
            if now - self._last_drop_log >= 1.0:     # rate-limit the alarm
                self._last_drop_log = now
                log.error("RAW CAPTURE OVERFLOW [%s]: queue full, dropping rows "
                          "(total dropped=%d) — writer/DB can't keep up",
                          self.symbol, self.n_dropped)

    def _fmt(self, row: tuple) -> str:
        sym = self.symbol
        if row[0] == "t":
            _, ts, price, size, aggr = row
            return (f"{TICKS},symbol={sym} price={price},size={int(size)}i,"
                    f"aggressor={int(aggr)}i {int(ts)}\n")
        _, ts, side, level, price, size = row
        return (f"{DEPTH},symbol={sym} side={int(side)}i,level={int(level)}i,"
                f"price={price},size={int(size)}i {int(ts)}\n")

    # ── writer thread: batched ILP over TCP, reconnecting ─────────────────
    def _run_writer(self) -> None:
        sock: socket.socket | None = None
        pending: list[str] = []
        flush_s = self.flush_ms / 1000.0
        while not (self._stop.is_set() and self._q.empty() and not pending):
            deadline = time.monotonic() + flush_s
            while len(pending) < self.batch:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    pending.append(self._fmt(self._q.get(timeout=min(0.05, remaining))))
                except queue.Empty:
                    if self._stop.is_set() and self._q.empty():
                        break
            if not pending:
                continue
            try:
                if sock is None:
                    sock = socket.create_connection((self.host, self.ilp_port), timeout=5)
                sock.sendall("".join(pending).encode())
                self.n_written += len(pending)
                pending = []
            except OSError as ex:                    # reconnect; keep pending, retry
                log.warning("ILP writer [%s] reconnect: %s", self.symbol, ex)
                try:
                    if sock:
                        sock.close()
                except OSError:
                    pass
                sock = None
                time.sleep(0.5)
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    # ── stream: capture + forward (drop raw depth onward) ─────────────────
    async def stream(self) -> AsyncIterator[MarketEvent]:
        try:
            self._ensure_tables()
        except Exception as ex:                      # noqa: BLE001 - never break the feed
            log.error("raw capture DISABLED [%s] (table init failed): %s", self.symbol, ex)
            async for ev in self.inner.stream():
                yield ev
            return
        self._writer = threading.Thread(target=self._run_writer, daemon=True,
                                        name=f"rawcap-{self.symbol}")
        self._writer.start()
        last_log = time.monotonic()
        prev_written = 0
        log.info("raw capture ON [%s] -> %s/%s via ILP :%d (buffered, off hot path)",
                 self.symbol, TICKS, DEPTH, self.ilp_port)
        try:
            async for ev in self.inner.stream():
                if isinstance(ev, Trade):
                    self._push(("t", ev.ts, ev.price, ev.size, ev.aggressor))
                    yield ev
                elif isinstance(ev, DepthUpdate):
                    self._push(("d", ev.ts, ev.side, ev.level, ev.price, ev.size))
                    # NOT forwarded: the engine consumes per-second BookFlow, not raw depth
                else:
                    yield ev
                now = time.monotonic()
                if now - last_log >= self.log_every_s:
                    rate = (self.n_written - prev_written) / (now - last_log)
                    log.info("raw capture [%s]: %.0f rows/s  written=%d  queue=%d  dropped=%d",
                             self.symbol, rate, self.n_written, self._q.qsize(), self.n_dropped)
                    last_log, prev_written = now, self.n_written
        finally:
            self._stop.set()
            if self._writer is not None:
                self._writer.join(timeout=10)
            log.info("raw capture stopped [%s]: written=%d dropped=%d",
                     self.symbol, self.n_written, self.n_dropped)


__all__ = ["RawCaptureTee", "TICKS", "DEPTH"]
