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

RECONNECT. A live feed is infinite: when the NT8 socket drops, LiveEngine calls
`feed.stream()` again on the SAME tee. Everything per-run therefore has to be
re-armed in `stream()`, and `_stop` above all — the previous run's finally block
set it, and a writer thread that starts with `_stop` already set exits on its
first loop check, before opening a socket. That is what happened on 2026-08-05
(see `_start_writer` and tests/test_raw_capture.py).

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

    # ── writer lifecycle ──────────────────────────────────────────────────
    @property
    def writer_alive(self) -> bool:
        return self._writer is not None and self._writer.is_alive()

    def _start_writer(self) -> None:
        """Arm the writer thread for THIS run of stream().

        `_stop` is sticky: the previous run's finally block set it, so a thread
        started without clearing it first returns on its first loop check and
        the queue silently grows until it overflows. Any rows still queued from
        before the disconnect belong to the tape, so they are kept and the new
        writer drains them.
        """
        prev = self._writer
        if prev is not None and prev.is_alive():
            self._stop.set()                 # ask the old one to finish first…
            prev.join(timeout=10)
            if prev.is_alive():
                log.error("raw capture [%s]: previous writer thread will not "
                          "exit -- refusing to run two writers on one queue; "
                          "rows will queue until it does", self.symbol)
                return
        self._stop.clear()                   # …then re-arm for this run
        self._writer = threading.Thread(target=self._run_writer, daemon=True,
                                        name=f"rawcap-{self.symbol}")
        self._writer.start()

    # ── hot path: enqueue, never block ────────────────────────────────────
    def _push(self, row: tuple) -> None:
        try:
            self._q.put_nowait(row)
        except queue.Full:
            self.n_dropped += 1
            now = time.monotonic()
            if now - self._last_drop_log >= 1.0:     # rate-limit the alarm
                self._last_drop_log = now
                # Name the actual fault. "writer/DB can't keep up" was the only
                # thing this said on 2026-08-05, and it pointed the diagnosis at
                # QuestDB for two hours while the truth was that no thread was
                # running at all.
                log.error("RAW CAPTURE OVERFLOW [%s]: queue full, dropping rows "
                          "(total dropped=%d) — writer thread %s",
                          self.symbol, self.n_dropped,
                          "is behind: the DB/ILP sink cannot keep up"
                          if self.writer_alive else
                          "is DEAD -- nothing is draining the queue")

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
    @property
    def finite(self) -> bool:
        """Transparent tee: a bounded inner feed stays bounded."""
        return bool(getattr(self.inner, "finite", False))

    async def stream(self) -> AsyncIterator[MarketEvent]:
        try:
            self._ensure_tables()
        except Exception as ex:                      # noqa: BLE001 - never break the feed
            log.error("raw capture DISABLED [%s] (table init failed): %s", self.symbol, ex)
            async for ev in self.inner.stream():
                yield ev
            return
        self._start_writer()
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
                    lvl = logging.INFO if self.writer_alive else logging.ERROR
                    log.log(lvl, "raw capture [%s]: %.0f rows/s  written=%d  "
                            "queue=%d  dropped=%d%s",
                            self.symbol, rate, self.n_written, self._q.qsize(),
                            self.n_dropped,
                            "" if self.writer_alive else "  WRITER THREAD DEAD")
                    last_log, prev_written = now, self.n_written
        finally:
            self._stop.set()
            if self._writer is not None:
                self._writer.join(timeout=10)
            log.info("raw capture stopped [%s]: written=%d dropped=%d",
                     self.symbol, self.n_written, self.n_dropped)


__all__ = ["RawCaptureTee", "TICKS", "DEPTH"]
