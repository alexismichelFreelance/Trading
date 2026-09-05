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
from ..ingest_check import DEFAULT_TIMEOUT_S, PROBE_SYMBOL, verify_ingest
from ..questdb import QuestDB

log = logging.getLogger("engine.rawcapture")

TICKS = "claude_ticks_live"
DEPTH = "claude_depth_live"


class RawCaptureTee:
    def __init__(self, inner, symbol: str = "ES", host: str = "127.0.0.1",
                 ilp_port: int = 9009, queue_cap: int = 500_000,
                 batch: int = 5000, flush_ms: int = 250, log_every_s: float = 15.0,
                 qdb: QuestDB | None = None,
                 probe_timeout_s: float = DEFAULT_TIMEOUT_S,
                 recheck_s: float = 300.0, recheck_fail_n: int = 3,
                 recheck_timeout_s: float = 60.0,
                 capture_depth: bool = True) -> None:
        self.inner = inner
        self.symbol = symbol
        self.capture_depth = capture_depth
        self.host, self.ilp_port = host, ilp_port
        self.batch, self.flush_ms = batch, flush_ms
        self.log_every_s = log_every_s
        self._qdb = qdb or QuestDB()
        self._q: queue.Queue = queue.Queue(maxsize=queue_cap)
        self._stop = threading.Event()
        self._writer: threading.Thread | None = None
        self._monitor: threading.Thread | None = None
        self.recheck_s = recheck_s
        # A single failed probe is NOT a death. QuestDB commits WAL
        # asynchronously and under write load a specific row often is not
        # readable inside the budget -- the first version of this monitor
        # flapped 62 times in one session on tables that were ingesting
        # normally the whole time. Only a STREAK counts, and the re-probe
        # gets a longer budget than the startup one, which runs before any
        # load exists.
        self.recheck_fail_n = max(1, int(recheck_fail_n))
        self.recheck_timeout_s = recheck_timeout_s
        self._fail_streak = 0
        # counters (read by the monitor; thread-safe enough for logging)
        self.n_written = 0
        self.n_dropped = 0
        self._last_drop_log = 0.0
        self.probe_timeout_s = probe_timeout_s
        # None = not probed yet. False = the table accepts writes and stores
        # nothing; everything captured this session is going nowhere.
        self.ingest_ok: bool | None = None

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

    # ── startup precondition: do these tables actually store anything? ────
    def _send_ilp(self, payload: str) -> None:
        s = socket.create_connection((self.host, self.ilp_port), timeout=5)
        try:
            s.sendall(payload.encode())
        finally:
            s.close()

    def _verify_ingest(self, timeout_s: float | None = None) -> bool:
        """Round-trip one probe row into EACH table, over ILP — the same path
        the tape takes. Both tables broke independently on 2026-08-05, so
        proving one says nothing about the other."""
        ok = True
        budget = self.probe_timeout_s if timeout_s is None else timeout_s
        probes = [(TICKS, f"{TICKS},symbol={PROBE_SYMBOL} price=0.0,size=0i,"
                          f"aggressor=0i {{ts}}\n")]
        if self.capture_depth:
            # A lane with raw depth switched off (live.yaml capture_depth) never
            # receives a DepthUpdate, so probing the depth table on its behalf
            # tests a path it does not use and writes probe rows into a table it
            # is no longer a customer of.
            probes.append((DEPTH, f"{DEPTH},symbol={PROBE_SYMBOL} side=0i,level=0i,"
                                  f"price=0.0,size=0i {{ts}}\n"))
        for table, line in probes:
            ok &= verify_ingest(self._qdb, table,
                                lambda ts, ln=line: self._send_ilp(ln.format(ts=ts)),
                                timeout_s=budget)
        return bool(ok)

    def recheck_ingest(self) -> None:
        """Re-run the round trip and update `ingest_ok`, announcing transitions.

        The startup probe answers "can this table store anything". It cannot
        answer "is it storing anything NOW", and on 2026-09-01 those diverged at
        about 14:30: QuestDB stopped committing, every counter kept climbing,
        and 90 minutes of RTH were lost in silence because ingest_ok had been
        decided at 09:30 and never revisited.

        Mid-session the answer is still actionable -- not "repair before you
        start" but "everything from here is going nowhere", which is worth
        knowing while the session is running rather than the next morning."""
        was = self.ingest_ok
        ok = self._verify_ingest(timeout_s=self.recheck_timeout_s)
        if ok:
            self._fail_streak = 0
        else:
            self._fail_streak += 1
            if self._fail_streak < self.recheck_fail_n:
                log.warning("raw capture [%s]: ingest probe missed (%d/%d) -- "
                            "a slow commit, not yet a death",
                            self.symbol, self._fail_streak, self.recheck_fail_n)
                return
        self.ingest_ok = ok
        if ok == was:
            return
        if ok:
            log.warning("raw capture [%s]: ingest RECOVERED -- %s are storing "
                        "rows again. Everything written while it was down is "
                        "gone; the gap does not backfill.",
                        self.symbol, f'{TICKS}/{DEPTH}' if self.capture_depth else TICKS)
        else:
            log.error("raw capture [%s]: INGEST DIED MID-SESSION. %s accept "
                      "writes and store nothing. Every row from here is lost, "
                      "counters and dropped=0 notwithstanding. Recording "
                      "continues so the feed is never blocked.",
                      self.symbol, f'{TICKS}/{DEPTH}' if self.capture_depth else TICKS)

    def _run_monitor(self) -> None:
        """Re-probe on a timer. Its own thread because the probe blocks for up
        to probe_timeout_s and must never stall the writer or the hot path."""
        while not self._stop.wait(self.recheck_s):
            try:
                self.recheck_ingest()
            except Exception as ex:                  # noqa: BLE001 - never die
                log.warning("raw capture [%s]: ingest re-probe failed: %s",
                            self.symbol, ex)

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
        self.ingest_ok = self._verify_ingest()
        self._start_writer()
        if self.recheck_s > 0:
            self._monitor = threading.Thread(target=self._run_monitor, daemon=True,
                                             name=f"rawcap-probe-{self.symbol}")
            self._monitor.start()
        last_log = time.monotonic()
        prev_written = 0
        prev_dropped = 0
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
                    # SILENT WHEN HEALTHY. This used to print a rows/s line every
                    # 15s forever, which trains the eye to skip the one line that
                    # matters. The counters still exist and are still checked --
                    # they are only SAID when one of them is wrong:
                    #   writer thread dead   -> nothing is being stored at all
                    #   dropped rows rising  -> tape is being lost right now
                    #   queue over half full -> the writer is losing the race
                    # Anything healthy goes to DEBUG, so -v still shows the rate.
                    rate = (self.n_written - prev_written) / (now - last_log)
                    qn, cap = self._q.qsize(), self._q.maxsize or 1
                    backed_up = qn > cap // 2
                    losing = self.n_dropped > prev_dropped
                    if not self.writer_alive:
                        lvl, why = logging.ERROR, "  WRITER THREAD DEAD"
                    elif losing:
                        lvl, why = logging.ERROR, (
                            f"  DROPPING ROWS (+{self.n_dropped - prev_dropped})")
                    elif backed_up:
                        lvl, why = logging.WARNING, f"  QUEUE BACKED UP ({qn}/{cap})"
                    else:
                        lvl, why = logging.DEBUG, ""
                    log.log(lvl, "raw capture [%s]: %.0f rows/s  written=%d  "
                            "queue=%d  dropped=%d%s",
                            self.symbol, rate, self.n_written, qn,
                            self.n_dropped, why)
                    last_log, prev_written = now, self.n_written
                    prev_dropped = self.n_dropped
        finally:
            self._stop.set()
            if self._writer is not None:
                self._writer.join(timeout=10)
            log.info("raw capture stopped [%s]: written=%d dropped=%d",
                     self.symbol, self.n_written, self.n_dropped)


__all__ = ["RawCaptureTee", "TICKS", "DEPTH"]
