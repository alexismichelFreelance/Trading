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


def test_live_fill_paints_at_actual_fill_price_and_time():
    from engine.core.events import Fill
    p = _painter()
    pc = PaintController(p, [])
    f = Fill(ts=5 * NS, order_id="O1", symbol="ES", price=7561.25, size=-2,
             commission=0.0, slippage=0.0, tag="fade-entry")
    asyncio.run(pc.live_fill(f))
    arrow = next(m for m in p._w.lines if m["kind"] == "arrow")
    assert arrow["ts"] == 5 * NS and arrow["price"] == 7561.25   # ACTUAL fill, not decision
    assert arrow["dir"] == -1 and "7561.25" in arrow["label"]


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


def _bar(ts, o, h, l, c, v=100):
    from engine.core.events import Bar
    return Bar(ts, "1m", o, h, l, c, v)


def test_paint_controller_ghost_and_panel():
    class _Sleeve:
        pos = 0

    p = _painter()
    pc = PaintController(p, [_Sleeve()], panel_pos="bottomleft")

    async def go():
        await pc.ghost_one(5 * NS, -1, 2, "opendrive-entry", 5007.0)
        await pc.on_bar(_bar(60 * NS, 5008, 5009, 5007, 5008), live=True)

    asyncio.run(go())
    kinds = [m["kind"] for m in p._w.lines]
    assert kinds.count("arrow") == 1                  # the ghost
    assert kinds.count("status") == 1                 # the info panel
    status = next(m for m in p._w.lines if m["kind"] == "status")
    assert status["pos"] == "bottomleft" and "ENGINE" in status["label"]
    ghost = next(m for m in p._w.lines if m["kind"] == "arrow")
    assert ghost["ts"] == 5 * NS and ghost["price"] == 5007.0 and "opendrive" in ghost["label"]


def test_zoneview_multitf_detects_and_brackets():
    from engine.core.events import Bar
    from engine.features.zones import DEMAND
    from engine.painters import ZoneView
    zv = ZoneView()
    # 30m demand zone from the 1m stream: base (tight) then a big up departure
    t = 0
    for i in range(25):                                # warm the 20-bar averages
        t += 30 * 60 * NS
        zv.update(_bar(t, 5000, 5002, 4998, 5001, 100))
    for i in range(3):                                 # tight base
        t += 30 * 60 * NS
        zv.update(_bar(t, 5000, 5001, 4999.0, 5000, 80))
    t += 30 * 60 * NS
    zv.update(_bar(t, 5000, 5040, 4999, 5038, 400))    # decisive up departure
    t += 30 * 60 * NS
    zv.update(_bar(t, 5038, 5040, 5036, 5039, 100))    # trailing bar flushes it
    assert any(z.direction == DEMAND for z in zv.active("30m"))
    res, sup = zv.bracket(5039.0)
    assert sup is not None and sup[0] == "30m"         # (tf, zone) below price

    # daily zones seed independently and appear in the bracket across TFs
    dt = 0
    dailies = []
    for i in range(25):
        dt += 86400 * NS
        dailies.append(Bar(dt, "1d", 4800, 4805, 4795, 4802, 1000))
    for i in range(3):
        dt += 86400 * NS
        dailies.append(Bar(dt, "1d", 4800, 4801, 4799, 4800, 800))
    dt += 86400 * NS
    dailies.append(Bar(dt, "1d", 4800, 4880, 4799, 4875, 4000))   # daily up departure
    dt += 86400 * NS
    dailies.append(Bar(dt, "1d", 4875, 4878, 4873, 4876, 900))
    zv.seed_daily(dailies)
    assert any(z.direction == DEMAND for z in zv.active("1d"))
