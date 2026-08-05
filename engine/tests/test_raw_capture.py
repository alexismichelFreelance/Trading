"""RawCaptureTee: ILP line format, capture-forward behavior (raw depth NOT
forwarded to the engine), buffered write to a local ILP sink, overflow drop,
and survival across a feed reconnect.

The stub DB answers the startup ingest probe (engine/adapters/ingest_check), so
these tests take the same path the live tee does. The probe opens its own ILP
connection per table and writes one PROBE_SYMBOL row into each; those lines are
filtered out of the assertions below."""
import asyncio
import socket
import threading
import time

from engine.adapters.feeds.raw_capture import RawCaptureTee
from engine.adapters.ingest_check import PROBE_SYMBOL
from engine.core.events import BUY, Bar, DepthUpdate, Trade


class _Inner:
    finite = True      # bounded fake: stream-end is DONE, not a disconnect
    def __init__(self, evs):
        self._evs = evs

    async def stream(self):
        for e in self._evs:
            yield e


class _FakeQDB:
    def query(self, sql):
        if sql.lstrip().lower().startswith("select count()"):
            return {"dataset": [[1]]}          # the probe row landed
        return {}


def _tape(buf):
    """Sink contents with the startup probe rows removed."""
    return "\n".join(ln for ln in bytes(buf).decode().splitlines()
                     if PROBE_SYMBOL not in ln)


def _ilp_sink_multi(idle=1.0):
    """Stand-in for QuestDB's ILP port, collecting everything sent.

    Accepts any number of CONCURRENT connections: a single run of the tee opens
    one per table for the startup ingest probe plus one for the writer thread,
    and a reconnect opens a fresh set. Serialising them would deadlock the test
    rather than the code."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)
    port = srv.getsockname()[1]
    buf = bytearray()

    def read(conn):
        conn.settimeout(idle)
        try:
            while True:
                d = conn.recv(65536)
                if not d:
                    return
                buf.extend(d)            # GIL makes the append atomic enough here
        except OSError:
            return

    def accept_loop():
        srv.settimeout(5)
        while True:
            try:
                conn, _addr = srv.accept()
            except OSError:
                return
            threading.Thread(target=read, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    return port, buf, srv


def test_fmt_ilp_lines():
    tee = RawCaptureTee(None, "ES", qdb=_FakeQDB())
    assert tee._fmt(("t", 123, 7543.5, 2, 1)) == \
        "claude_ticks_live,symbol=ES price=7543.5,size=2i,aggressor=1i 123\n"
    assert tee._fmt(("d", 124, -1, 3, 7544.0, 10)) == \
        "claude_depth_live,symbol=ES side=-1i,level=3i,price=7544.0,size=10i 124\n"


def test_captures_all_forwards_trades_not_depth_and_writes_ilp():
    port, buf, srv = _ilp_sink_multi()
    evs = [Trade(1, 7543.5, 2, BUY, "ES"),
           DepthUpdate(2, -1, 7544.0, 10, 3, "ES"),
           DepthUpdate(3, 1, 7543.0, 40, 0, "ES"),
           Bar(4, "1m", 1, 2, 0, 1, 5, "ES"),
           Trade(5, 7545.0, 1, BUY, "ES")]
    tee = RawCaptureTee(_Inner(evs), "ES", ilp_port=port, qdb=_FakeQDB(),
                        flush_ms=50, batch=2)

    async def go():
        return [type(e).__name__ async for e in tee.stream()]

    forwarded = asyncio.run(go())
    # engine sees trades + bar; raw depth is captured but NOT forwarded
    assert forwarded == ["Trade", "Bar", "Trade"]
    time.sleep(0.4)
    txt = _tape(buf)
    srv.close()
    # everything (both trades AND both depth updates) reached the ILP sink
    assert txt.count("claude_ticks_live") == 2
    assert txt.count("claude_depth_live") == 2
    assert "price=7543.5,size=2i,aggressor=1i 1" in txt
    assert "side=-1i,level=3i,price=7544.0,size=10i 2" in txt
    assert tee.n_written == 4 and tee.n_dropped == 0


def test_overflow_drops_and_counts_loudly():
    tee = RawCaptureTee(None, "ES", qdb=_FakeQDB(), queue_cap=3)
    for i in range(10):
        tee._push(("t", i, 1.0, 1, 1))
    assert tee._q.qsize() == 3          # cap respected
    assert tee.n_dropped == 7           # the rest dropped, counted (not silent)


# ── feed reconnect ───────────────────────────────────────────────────────────

def test_writer_survives_a_feed_reconnect():
    """THE REGRESSION (2026-08-05, live ES+NQ session).

    LiveEngine._pump_feed reconnects a live feed by re-entering feed.stream() on
    the SAME object. stream()'s finally block sets self._stop -- and nothing ever
    cleared it. So the writer thread spawned by the SECOND stream() evaluated

        while not (self._stop.is_set() and self._q.empty() and not pending)

    exactly once, found _stop already set and the queue not yet filled, and
    returned before it ever opened a socket. From then on every captured row went
    into a queue that nobody drained.

    Live evidence -- both feeds dropped at 09:54:53 and reconnected 6s later:

        09:55:19  raw capture [NQ]: written=23982  queue=0       dropped=0
        12:10:04  raw capture [NQ]: written=23982  queue=500000  dropped=515084
        12:10:13  raw capture [ES]: written=6153   queue=332499  dropped=0

    n_written frozen at its pre-disconnect value for 2h15m, and not a single "ILP
    writer reconnect" warning in the whole log: the thread was GONE, not
    struggling. 666k rows of NQ tape lost by 12:27, ES silently filling toward
    the same cliff, and ~100 MB per symbol pinned in a queue with no reader.

    Trading was unaffected (Trades are yielded before the push), but the raw
    tape this capture exists to collect was lost for the session.
    """
    port, buf, srv = _ilp_sink_multi()
    tee = RawCaptureTee(None, "ES", ilp_port=port, qdb=_FakeQDB(),
                        flush_ms=50, batch=2)

    async def run(evs):
        tee.inner = _Inner(evs)
        return [e async for e in tee.stream()]

    asyncio.run(run([Trade(1, 7543.5, 2, BUY, "ES")]))
    first = tee.n_written
    assert first == 1, f"first connection wrote {first} rows"

    # the disconnect/reconnect: same tee, stream() entered a second time
    asyncio.run(run([Trade(2, 7544.0, 3, BUY, "ES"),
                     DepthUpdate(3, -1, 7544.5, 10, 0, "ES")]))
    assert tee.n_written == 3, (
        f"after reconnect the tee wrote {tee.n_written - first} of 2 rows -- "
        f"the writer thread died with _stop still set")
    assert tee.n_dropped == 0
    assert tee._q.qsize() == 0, "rows left stranded in the queue after reconnect"

    time.sleep(0.3)
    txt = _tape(buf)
    srv.close()
    assert "price=7544.0,size=3i" in txt, "post-reconnect tick never reached ILP"
    assert "side=-1i,level=0i,price=7544.5" in txt


def test_reconnect_flushes_rows_queued_before_the_disconnect():
    """A disconnect mid-batch leaves rows in the queue. The reconnected writer
    must pick them up, not orphan them -- otherwise every disconnect punches a
    hole in the tape even once the thread is alive again."""
    port, buf, srv = _ilp_sink_multi()
    tee = RawCaptureTee(None, "ES", ilp_port=port, qdb=_FakeQDB(),
                        flush_ms=50, batch=2)
    tee._stop.set()                      # as the previous run's finally left it
    tee._q.put(("t", 99, 7000.0, 1, 1))  # stranded by the disconnect

    async def go():
        tee.inner = _Inner([Trade(100, 7001.0, 1, BUY, "ES")])
        return [e async for e in tee.stream()]

    asyncio.run(go())
    assert tee.n_written == 2, f"stranded row was orphaned (wrote {tee.n_written})"
    time.sleep(0.3)
    txt = _tape(buf)
    srv.close()
    assert "price=7000.0" in txt and "price=7001.0" in txt


def test_second_writer_is_not_started_on_top_of_a_live_one():
    """Re-arming must not leave two threads draining the same queue -- ILP order
    would interleave and n_written would double-count."""
    port, _buf, srv = _ilp_sink_multi()
    tee = RawCaptureTee(None, "ES", ilp_port=port, qdb=_FakeQDB(), flush_ms=50)

    async def go():
        tee.inner = _Inner([Trade(1, 7000.0, 1, BUY, "ES")])
        return [e async for e in tee.stream()]

    for _ in range(3):
        asyncio.run(go())
        live = [t for t in threading.enumerate() if t.name == "rawcap-ES" and t.is_alive()]
        assert len(live) <= 1, f"{len(live)} writer threads alive for one tee"
    srv.close()


def test_overflow_names_a_dead_writer_rather_than_blaming_the_db(caplog):
    """The live message read "writer/DB can't keep up", which sent the diagnosis
    at QuestDB for hours while the real answer was that no thread was running.
    An alarm that misnames the fault is worse than a quiet one."""
    import logging
    tee = RawCaptureTee(None, "NQ", qdb=_FakeQDB(), queue_cap=1)
    with caplog.at_level(logging.ERROR, logger="engine.rawcapture"):
        tee._push(("t", 1, 1.0, 1, 1))
        tee._push(("t", 2, 1.0, 1, 1))
    assert tee.n_dropped == 1
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "DEAD" in msg, f"overflow with no writer thread did not say so: {msg}"
