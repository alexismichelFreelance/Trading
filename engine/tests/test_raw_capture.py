"""RawCaptureTee: ILP line format, capture-forward behavior (raw depth NOT
forwarded to the engine), buffered write to a local ILP sink, overflow drop."""
import asyncio
import socket
import threading
import time

from engine.adapters.feeds.raw_capture import RawCaptureTee
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
        return {}


def _ilp_sink():
    """Local TCP server that collects everything sent (stand-in for QuestDB ILP)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    buf = bytearray()

    def run():
        srv.settimeout(5)
        try:
            conn, _ = srv.accept()
            conn.settimeout(3)
            while True:
                try:
                    d = conn.recv(65536)
                except socket.timeout:
                    break
                if not d:
                    break
                buf.extend(d)
        except OSError:
            pass

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return port, buf, srv


def test_fmt_ilp_lines():
    tee = RawCaptureTee(None, "ES", qdb=_FakeQDB())
    assert tee._fmt(("t", 123, 7543.5, 2, 1)) == \
        "claude_ticks_live,symbol=ES price=7543.5,size=2i,aggressor=1i 123\n"
    assert tee._fmt(("d", 124, -1, 3, 7544.0, 10)) == \
        "claude_depth_live,symbol=ES side=-1i,level=3i,price=7544.0,size=10i 124\n"


def test_captures_all_forwards_trades_not_depth_and_writes_ilp():
    port, buf, srv = _ilp_sink()
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
    txt = bytes(buf).decode()
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
