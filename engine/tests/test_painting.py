"""Chart-painting path: warmup signals are captured WITH their historical
time/price for ghost arrows; live orders trigger the painter callback; the
painter emits well-formed draw JSON; PaintController paints zones from
strategy state."""
import asyncio
import json

from engine.adapters.painter import NTChartPainter
from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.events import BUY, Bar, Trade
from engine.core.live_engine import LiveEngine
from engine.core.orders import Order
from engine.painters import PaintController

NS = 1_000_000_000


class _FakeWriter:
    def __init__(self):
        self.lines = []

    def write(self, b: bytes):
        self.lines += [json.loads(x) for x in b.decode().strip().split("\n") if x]

    async def drain(self):
        return None

    def close(self):
        return None

    async def wait_closed(self):
        return None


def _painter():
    p = NTChartPainter()
    p._w = _FakeWriter()
    p.enabled = True
    return p


class _SigStrategy:
    symbol = "ES"

    def on_bar(self, e):
        return [Order("ES", BUY, 1, tag="sig")]

    def on_trade(self, e):
        return []

    def on_bookflow(self, e):
        return []

    def on_quote(self, e):
        return []

    def on_depth(self, e):
        return []

    def on_fill(self, e):
        return None

    def on_position(self, e):
        return None


class _NullFeed:
    def __init__(self, events):
        self._events = events

    async def stream(self):
        for e in self._events:
            yield e


class _NullBroker:
    def __init__(self):
        self.submitted = []

    async def submit(self, o):
        self.submitted.append(o)

    async def events(self):
        if False:
            yield None


def test_warmup_signals_capture_ts_and_px_and_live_hook_fires():
    bar1 = Bar(100 * NS, "1m", 5000, 5001, 4999, 5000.5, 10)
    bar2 = Bar(160 * NS, "1m", 5000, 5002, 5000, 5001.5, 12)
    trade = Trade(200 * NS, 5002.0, 1, 1)

    painted = []

    async def on_live(ts, side, qty, tag, px):
        painted.append((ts, side, qty, tag, px))

    async def go():
        eng = LiveEngine(_NullFeed([bar1, bar2, trade]), _NullBroker(),
                         [_SigStrategy()], WallClock(), Blotter("ES", 50.0),
                         drain_timeout=0.2, warmup_gate=True)
        eng.on_live_order = on_live
        await asyncio.wait_for(eng.run(), timeout=5)
        return eng

    eng = asyncio.run(go())
    # two warmup bars -> two ghost signals at HISTORICAL ts with the bar close px
    assert eng.warmup_signals == [
        (100 * NS, 1, 1, "sig", 5000.5),
        (160 * NS, 1, 1, "sig", 5001.5),
    ]
    assert painted == []          # no bar after the live flip -> no live signals


def test_painter_json_vocabulary():
    p = _painter()

    async def go():
        await p.arrow("a1", 5 * NS, 5000.0, 1, color="#FF00FF00", label="entry")
        await p.rect("z1", 1 * NS, 5010.0, 9 * NS, 5005.0, color="#5532CD32")
        await p.hline("stop", 4998.25)
        await p.status("ENGINE LIVE")
        await p.remove("z1")

    asyncio.run(go())
    kinds = [m["kind"] for m in p._w.lines]
    assert kinds == ["arrow", "rect", "hline", "status", "remove"]
    assert all(m["t"] == "draw" for m in p._w.lines)
    arrow = p._w.lines[0]
    assert arrow["ts"] == 5 * NS and arrow["dir"] == 1 and arrow["label"] == "entry"


def test_paint_controller_zones_and_ghosts():
    class _Z:
        def __init__(self, ts, d, top, bot, fade=False, broke=False):
            self.ts, self.dir, self.top, self.bot = ts, d, top, bot
            self.fade_done, self.broke = fade, broke

    class _ZoneStrat:
        pos = 0
        zones = [_Z(10 * NS, 1, 5010.0, 5005.0),                 # virgin demand
                 _Z(20 * NS, -1, 5030.0, 5025.0, broke=True)]    # broken -> removed

    p = _painter()
    pc = PaintController(p, [_ZoneStrat()])

    async def go():
        await pc.ghost_one(5 * NS, -1, 2, "opendrive-entry", 5007.0)
        await pc.on_bar(60 * NS, 5008.0, live=True)

    asyncio.run(go())
    kinds = [m["kind"] for m in p._w.lines]
    assert kinds.count("arrow") == 1                  # the ghost
    assert kinds.count("rect") == 1                   # virgin zone painted...
    assert kinds.count("remove") == 1                 # ...broken zone removed, not grayed
    assert kinds.count("status") == 1
    ghost = next(m for m in p._w.lines if m["kind"] == "arrow")
    assert ghost["ts"] == 5 * NS and ghost["price"] == 5007.0 and "opendrive" in ghost["label"]
    rect = next(m for m in p._w.lines if m["kind"] == "rect")
    assert rect["opacity"] == 30                      # virgin = vivid
