"""A write that reports success but never lands must be caught at STARTUP.

On 2026-08-05 three QuestDB tables — claude_ticks_live, claude_depth_live and
claude_sec_live — accepted every write and applied none. Not suspended, no
error, no exception: ILP `sendall` returned, `INSERT` returned OK, the row
counters climbed, and nothing reached the table. Proven with two ILP lines on
one connection at the same instant: a brand-new table took its row, and
claude_ticks_live took the identical write and dropped it. The tables had been
dead since 2026-08-04 16:01 and nobody noticed for twenty hours.

The counters lie because they count what was SENT. The only honest check is to
write one row through the real write path and read it back. That is a startup
precondition, not a background watchdog: it runs once, before the session, when
there is still time to fix the database — and then never again.

Failing the probe must not stop the feed or the trading. It must be impossible
to miss.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.feeds.raw_capture import RawCaptureTee  # noqa: E402
from engine.adapters.feeds.recorder_tee import RecorderTee  # noqa: E402
from engine.adapters.ingest_check import (PROBE_SYMBOL,  # noqa: E402
                                          verify_ingest, verify_ingest_async)
from engine.core.events import BUY, BookFlow, Trade  # noqa: E402


class _Inner:
    finite = True

    def __init__(self, evs):
        self._evs = evs

    async def stream(self):
        for e in self._evs:
            yield e


class _QDB:
    """Sync QuestDB stand-in. `lands` decides whether a written probe is ever
    visible — False reproduces the 2026-08-05 fault exactly."""

    def __init__(self, lands=True):
        self.lands = lands
        self.sql: list[str] = []

    def query(self, sql):
        self.sql.append(sql)
        if sql.lstrip().lower().startswith("select count()"):
            return {"dataset": [[1 if self.lands else 0]]}
        return {}


class _AQDB(_QDB):
    async def query(self, sql):          # type: ignore[override]
        return _QDB.query(self, sql)


def _sink(discard=True):
    """TCP sink that accepts and silently discards — a socket that works while
    the table behind it does not."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    port = srv.getsockname()[1]

    def run():
        srv.settimeout(5)
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=lambda s=c: _eat(s), daemon=True).start()

    def _eat(c):
        c.settimeout(2)
        try:
            while c.recv(65536):
                pass
        except OSError:
            pass

    threading.Thread(target=run, daemon=True).start()
    return port, srv


# ── the helper itself ────────────────────────────────────────────────────────

def test_verify_ingest_passes_when_the_row_comes_back():
    qdb = _QDB(lands=True)
    sent = []
    assert verify_ingest(qdb, "t_ok", sent.append, timeout_s=2.0) is True
    assert len(sent) == 1, "probe was never written"


def test_verify_ingest_fails_when_the_row_never_lands():
    """THE REGRESSION, at the helper: the write path succeeds, the table stays
    empty, and the old code had no way to tell the two apart."""
    qdb = _QDB(lands=False)
    assert verify_ingest(qdb, "t_broken", lambda ts: None, timeout_s=1.0) is False


def test_probe_is_tagged_so_it_can_never_be_mistaken_for_market_data():
    qdb = _QDB(lands=True)
    got = []
    verify_ingest(qdb, "t_ok", got.append, timeout_s=2.0)
    reads = [s for s in qdb.sql if s.lstrip().lower().startswith("select count()")]
    assert reads and PROBE_SYMBOL in reads[0], reads
    assert PROBE_SYMBOL.startswith("__"), "probe symbol must be unmistakable"


def test_async_helper_agrees_with_the_sync_one():
    async def go(lands):
        return await verify_ingest_async(_AQDB(lands=lands), "t",
                                         lambda ts: None, timeout_s=1.0)
    assert asyncio.run(go(True)) is True
    assert asyncio.run(go(False)) is False


# ── wired into the tees ──────────────────────────────────────────────────────

def test_raw_capture_says_so_when_its_table_swallows_writes(caplog):
    """The exact 2026-08-05 shape: ILP connects fine, the table eats the rows."""
    port, srv = _sink()
    tee = RawCaptureTee(_Inner([Trade(1, 7000.0, 1, BUY, "ES")]), "ES",
                        ilp_port=port, qdb=_QDB(lands=False),
                        flush_ms=50, probe_timeout_s=1.0)
    with caplog.at_level(logging.ERROR, logger="engine.rawcapture"):
        asyncio.run(_collect(tee))
    srv.close()
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert tee.ingest_ok is False
    assert "claude_ticks_live" in msg, f"the dead table was not named: {msg}"
    assert "NOT LANDING" in msg, msg


def test_raw_capture_stays_quiet_when_ingest_works(caplog):
    port, srv = _sink()
    tee = RawCaptureTee(_Inner([Trade(1, 7000.0, 1, BUY, "ES")]), "ES",
                        ilp_port=port, qdb=_QDB(lands=True),
                        flush_ms=50, probe_timeout_s=2.0)
    with caplog.at_level(logging.ERROR, logger="engine.rawcapture"):
        asyncio.run(_collect(tee))
    srv.close()
    assert tee.ingest_ok is True
    assert not [r for r in caplog.records if "NOT LANDING" in r.getMessage()]


def test_a_dead_table_does_not_stop_the_feed_or_the_trading():
    """Recording is not worth a single missed trade. The events must still flow."""
    port, srv = _sink()
    evs = [Trade(1, 7000.0, 1, BUY, "ES"), Trade(2, 7001.0, 1, BUY, "ES")]
    tee = RawCaptureTee(_Inner(evs), "ES", ilp_port=port, qdb=_QDB(lands=False),
                        flush_ms=50, probe_timeout_s=1.0)
    out = asyncio.run(_collect(tee))
    srv.close()
    assert len(out) == 2, f"a broken recorder swallowed market events: {out}"


def test_recorder_says_so_when_its_table_swallows_writes(caplog):
    """claude_sec_live died the same way and mattered more: without it the
    ignition and flow sleeves cannot be replayed at all."""
    tee = RecorderTee(_Inner([Trade(1_000_000_000, 7000.0, 1, BUY, "ES"),
                              BookFlow(1_000_000_000, 1, 1, 1, 1, "ES")]),
                      qdb=_AQDB(lands=False), symbol="ES", probe_timeout_s=1.0)
    with caplog.at_level(logging.ERROR, logger="engine.recorder"):
        asyncio.run(_collect(tee))
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert tee.ingest_ok is False
    assert "claude_sec_live" in msg and "NOT LANDING" in msg, msg


def test_recorder_probes_every_table_it_writes_not_just_the_first():
    """Both tables broke independently on 2026-08-05. Probing one proves
    nothing about the other."""
    qdb = _AQDB(lands=True)
    tee = RecorderTee(_Inner([]), qdb=qdb, symbol="ES", probe_timeout_s=2.0)
    asyncio.run(_collect(tee))
    probed = {t for s in qdb.sql if PROBE_SYMBOL in s
              for t in ("claude_bars_live", "claude_sec_live") if t in s}
    assert probed == {"claude_bars_live", "claude_sec_live"}, probed


async def _collect(tee):
    return [e async for e in tee.stream()]
