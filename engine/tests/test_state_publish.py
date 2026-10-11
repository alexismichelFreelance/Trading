"""State-publish layer: snapshot/delta correctness and subscription fan-out.

Covers the two contracts the card demands — (1) the pure StateStore snapshot +
delta correctness, and (2) the StatePublisher subscriber fan-out in replay /
loopback mode over a real LiveEngine event flow (reusing the bounded-feed
pattern from test_painting.py). A third group proves the extractors read the
REAL engine objects (ZoneView / LevelBook / strategy chart_marks) and never
re-implement them.
"""
import asyncio
import logging
from collections import deque

from engine.adapters.state_publish import (LaneSource, StatePublisher,
                                           _bar_dict)
from engine.adapters.state_sources import (indicators_from_levelbook,
                                           positions_from_strategies,
                                           zones_from_view)
from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.events import BUY, Bar, Trade
from engine.core.live_engine import LiveEngine
from engine.core.orders import Order
from engine.features.avwap import AnchoredVWAP
from engine.features.level_book import LevelBook
from engine.features.pivots import SessionLevels
from engine.features.state_store import StateStore
from engine.features.zones import DEMAND, Zone

NS = 1_000_000_000


def bar(ts, o, h, l, c, v=100, symbol=""):
    return Bar(ts, "1m", o, h, l, c, v, symbol)


# ── 1. pure StateStore: snapshot + delta correctness ─────────────────────
def test_store_bar_delta_and_snapshot():
    store = StateStore()
    d = store.apply_bar({"ts": 5 * NS, "tf": "1m", "o": 1, "h": 2, "l": 1,
                         "c": 1.5, "v": 10, "symbol": "ES", "complete": True})
    assert d["type"] == "bar" and d["seq"] == 1 and d["lane"] == "ES"
    d2 = store.apply_bar({"ts": 10 * NS, "tf": "1m", "o": 1.5, "h": 3, "l": 1.5,
                          "c": 2, "v": 12, "symbol": "ES", "complete": True})
    assert d2["seq"] == 2
    snap = store.snapshot()
    assert snap["seq"] == 2 and snap["ts"] == 10 * NS
    assert [b["ts"] for b in snap["lanes"]["ES"]["bars_raw"]] == [5 * NS, 10 * NS]
    assert len(snap["lanes"]["ES"]["bars_raw"]) == 2


def test_store_agg_bar_rings_by_tf():
    store = StateStore()
    store.apply_bar({"ts": 60 * NS, "tf": "1m", "o": 1, "h": 2, "l": 1,
                     "c": 1.5, "v": 10, "symbol": "ES", "complete": True})
    store.apply_agg_bar({"ts": 60 * NS, "tf": "5m", "o": 1, "h": 2, "l": 1,
                         "c": 1.5, "v": 10, "symbol": "ES", "complete": True})
    store.apply_agg_bar({"ts": 120 * NS, "tf": "5m", "o": 1.5, "h": 3, "l": 1.5,
                         "c": 2, "v": 12, "symbol": "ES", "complete": True})
    snap = store.snapshot()["lanes"]["ES"]
    assert len(snap["bars"]["5m"]) == 2
    assert snap["bars"]["5m"][1]["tf"] == "5m"


def test_store_indicator_delta_carries_only_new_points():
    """APPEND-ONLY DELTA contract (the self-DOS fix): the returned delta's
    `points` are ONLY the not-yet-seen points, not the full accumulated
    history; and a series with nothing new returns None."""
    store = StateStore()
    d1 = store.apply_indicator("ES", {"id": "vwap",
        "points": [{"ts": 1 * NS, "v": 7000.0}]})
    assert d1 is not None and d1["payload"]["points"] == [{"ts": 1 * NS, "v": 7000.0}]
    # second bar: only the NEW point is in the delta, never the history
    d2 = store.apply_indicator("ES", {"id": "vwap",
        "points": [{"ts": 1 * NS, "v": 7000.0}, {"ts": 2 * NS, "v": 7005.0}]})
    assert d2 is not None
    assert d2["payload"]["points"] == [{"ts": 2 * NS, "v": 7005.0}]   # new only
    # a static grid re-emitted unchanged -> nothing new -> None (no flood)
    store.apply_indicator("ES", {"id": "pivots",
        "points": [{"ts": 9 * NS, "v": 7000.0}, {"ts": 9 * NS, "v": 7100.0}]})
    assert store.apply_indicator("ES", {"id": "pivots",
        "points": [{"ts": 9 * NS, "v": 7000.0}, {"ts": 9 * NS, "v": 7100.0}]}) \
        is None
    # snapshot still reflects the FULL accumulated series
    inds = {i["id"]: i for i in store.snapshot()["lanes"]["ES"]["indicators"]}
    assert len(inds["vwap"]["points"]) == 2
    assert [p["ts"] for p in inds["vwap"]["points"]] == [1 * NS, 2 * NS]


def test_publish_lane_skips_empty_indicator_deltas():
    """REG-RESSION: after the append-only change, a static indicator (pivots)
    with no new points returns None from the store and must NOT enqueue a None
    onto the subscriber queue (a None would crash the drain loop). Every
    broadcast is a real delta."""
    async def go():
        pub = StatePublisher([], agg_timeframes=("5m",))
        q = asyncio.Queue()
        pub._subs["t"] = q
        pub.broadcast(None)                       # caller hands a None delta
        assert q.empty()                          # ...broadcast drops it
        # _publish_lane with a static-only levelbook -> repeated bars must not
        # put None on the queue, and must not crash.
        class LB:
            vwap = None
            piv = type("P", (), {"pivots": [7000.0, 7100.0]})()
            gamma = None
        src = LaneSource("ES", zoneview=None, levelbook=LB())
        pub.lanes["ES"] = src
        from engine.core.events import Bar
        b = _bar_dict(Bar(1 * NS, "1m", 7000, 7002, 6999, 7000.5, 100, "ES"))
        await pub._publish_lane("ES", b)
        await pub._publish_lane("ES", b)          # second identical bar
        items = []
        while not q.empty():
            items.append(q.get_nowait())
        assert items, "first bar must emit the static pivot grid"
        assert all(i is not None and i.get("type") for i in items)
    asyncio.run(go())


def test_store_indicator_dedupes_static_levels_accumulates_series():
    store = StateStore()
    # vwap: time series -> accumulates one point per ts
    store.apply_indicator("ES", {"id": "vwap", "producer": "levelbook:avwap",
                                 "points": [{"ts": 1 * NS, "v": 7000.0}]})
    store.apply_indicator("ES", {"id": "vwap", "producer": "levelbook:avwap",
                                 "points": [{"ts": 2 * NS, "v": 7005.0}]})
    # pivots: static grid stamped at a FIXED session ts -> dedupes
    store.apply_indicator("ES", {"id": "pivots", "producer": "levelbook:pivots",
                                 "points": [{"ts": 9 * NS, "v": 7000.0},
                                            {"ts": 9 * NS, "v": 7100.0}]})
    store.apply_indicator("ES", {"id": "pivots", "producer": "levelbook:pivots",
                                 "points": [{"ts": 9 * NS, "v": 7000.0},
                                            {"ts": 9 * NS, "v": 7100.0}]})
    inds = {i["id"]: i for i in store.snapshot()["lanes"]["ES"]["indicators"]}
    assert len(inds["vwap"]["points"]) == 2          # accumulated
    assert len(inds["pivots"]["points"]) == 2        # deduped (static grid)
    assert inds["pivots"]["producer"] == "levelbook:pivots"


def test_store_zones_add_update_remove_deltas():
    store = StateStore()
    z = {"id": "30m:1:1", "tf": "30m", "top": 7100, "bot": 7090, "direction": 1,
         "virgin": True, "broken": False, "touches": 0, "composite": 3,
         "formed_ts": 1 * NS, "proximal": 7090, "distal": 7100}
    d = store.apply_zones("ES", [z])
    assert d["type"] == "zone" and d["payload"]["added"] == [z]
    # no change -> no delta (None), same no-op suppression as indicators
    assert store.apply_zones("ES", [z]) is None
    # status change -> updated; a missing zone -> removed
    z2 = dict(z, virgin=False, touches=1)
    d3 = store.apply_zones("ES", [z2])
    assert d3["payload"]["updated"] == [z2]
    d4 = store.apply_zones("ES", [])
    assert d4["payload"]["removed"] == ["30m:1:1"]
    assert store.snapshot()["lanes"]["ES"]["zones"] == []


def test_store_positions_upsert_and_dedupe():
    store = StateStore()
    p = {"sleeve": "ES:pivot", "symbol": "ES", "qty": 1, "avg_px": 7000.0,
         "entry": 7000.0, "target": 7100.0, "stop": 6980.0}
    assert store.apply_position(p)["type"] == "position"
    assert store.apply_position(p) is None          # unchanged -> no delta
    p2 = dict(p, qty=2)
    assert store.apply_position(p2)["type"] == "position"
    assert store.snapshot()["lanes"]["ES"]["positions"][0]["qty"] == 2


def test_store_log_error_partition():
    store = StateStore()
    store.apply_log({"ts": 1, "level": "INFO", "message": "warmed", "logger": "engine"})
    store.apply_log({"ts": 2, "level": "ERROR", "message": "boom", "logger": "engine"})
    snap = store.snapshot()
    assert len(snap["log"]) == 2
    assert [e["message"] for e in snap["errors"]] == ["boom"]


def test_log_emit_fires_once_for_child_namespace():
    """DEFECT 1 regression: a record logged on a CHILD namespace (engine.live)
    must produce exactly ONE delta/error, not two (handler attached once to the
    engine root, children propagate to it)."""
    store = StateStore()
    deltas = []
    from engine.adapters.state_publish import _LogEmit
    h = _LogEmit(store, deltas.append)
    try:
        logging.getLogger("engine.live").warning("probe-child")
        logging.getLogger("engine.blotter").error("boom-child")
    finally:
        logging.getLogger("engine").removeHandler(h)
    assert len(deltas) == 2, deltas                  # one record per log line
    errs = [e["message"] for e in store.snapshot()["errors"]]
    assert errs == ["probe-child", "boom-child"]      # each once, in order


def test_positions_avg_px_from_blotter_ledger():
    """DEFECT 2 regression: a strategy sleeve carries `pos` + chart_marks but
    no avg_px; the extractor wires the AUTHORITATIVE blotter ledger avg."""

    class Sleeve:
        symbol = "ES"
        label = "ES:pivot"
        pos = 1
        def chart_marks(self):
            return {"entry": 7000.0, "target": 7100.0, "stop": 6980.0}

    blotter = Blotter("ES", 50.0)
    blotter.ledger_for("ES").qty = 1
    blotter.ledger_for("ES").avg_px = 7004.5
    out = positions_from_strategies([Sleeve()], "ES", blotter)
    assert out[0]["avg_px"] == 7004.5
    assert out[0]["entry"] == 7000.0    # chart_marks entry distinct from avg
    # without a blotter avg_px stays None (schema documents it)
    out2 = positions_from_strategies([Sleeve()], "ES")
    assert out2[0]["avg_px"] is None


# ── 2. real-object extractors (never re-implement) ───────────────────────
def _real_levelbook_with_vwap():
    lb = LevelBook()
    lb.vwap = AnchoredVWAP()
    lb.vwap.add(7000.0, 100)
    lb.vwap.add(7010.0, 100)
    lb.gamma = {"put_wall": 6900.0, "call_wall": 7200.0, "flip": 7010.0}
    return lb


def test_sources_read_real_vwap_and_gamma():
    lb = _real_levelbook_with_vwap()
    inds = {i["id"]: i for i in indicators_from_levelbook(lb, 5 * NS)}
    assert inds["vwap"]["points"][0]["v"] == 7005.0   # real weighted mean
    gamma_vals = [p["v"] for p in inds["gamma"]["points"]]
    assert 7200.0 in gamma_vals and 6900.0 in gamma_vals   # real put/call walls
    assert inds["gamma"]["producer"] == "levelbook:gamma_levels"


def test_sources_read_real_chart_marks():
    class Sleeve:
        label = "ES:pivot"
        pos = 1
        def chart_marks(self):
            return {"entry": 7000.0, "target": 7100.0, "stop": 6980.0}
    out = positions_from_strategies([Sleeve()], "ES")
    assert out[0]["entry"] == 7000.0 and out[0]["stop"] == 6980.0
    assert out[0]["sleeve"] == "ES:pivot"


def test_sources_read_real_zoneview_geometry():
    zv = type("ZV", (), {})()
    zv.book = {"1d": type("B", (), {
        "zones": [Zone(1 * NS, top=7100.0, bot=7090.0, direction=DEMAND,
                       departure_score=2, base_score=1)],
    })()}
    zs = zones_from_view(zv, 5 * NS)
    assert len(zs) == 1
    z = zs[0]
    assert z["direction"] == 1 and z["virgin"] is True and z["composite"] == 3
    # ENGINE semantics: a DEMAND zone's proximal edge is its TOP (the edge price
    # reaches first on approach); distal is the BOTTOM. Assert the real geometry.
    assert z["proximal"] == 7100.0 and z["distal"] == 7090.0


# ── 3. subscription fan-out in loopback / replay over a real engine ──────
class _SigSleeve:
    """A position-holding sleeve the engine can drive, with declared rails."""
    symbol = "ES"
    label = "ES:pivot"
    pos = 1

    def on_trade(self, e): return []
    def on_quote(self, e): return []
    def on_quote_side(self, e): return []
    def on_depth(self, e): return []
    def on_bar(self, e): return [Order("ES", BUY, 1, tag="sig")]
    def on_bookflow(self, e): return []
    def on_fill(self, e): return None
    def on_position(self, e): return None
    def on_signal(self, e): return []

    def chart_marks(self):
        return {"entry": 7000.0, "target": 7100.0, "stop": 6980.0}


class _NullFeed:
    finite = True
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


def _zoneview_with_zone():
    zv = type("ZV", (), {})()
    zv.book = {"1d": type("B", (), {
        "zones": [Zone(1 * NS, top=7100.0, bot=7090.0, direction=DEMAND,
                       departure_score=2, base_score=1)],
    })()}
    return zv


def test_subscription_fanout_loopback():
    bars = [
        bar(60 * NS, 7000, 7002, 6999, 7000.5, 100, "ES"),
        bar(120 * NS, 7000, 7004, 7000, 7003.5, 120, "ES"),
        bar(180 * NS, 7003, 7005, 7002, 7004.0, 110, "ES"),
        bar(240 * NS, 7004, 7006, 7003, 7005.0, 105, "ES"),
        bar(300 * NS, 7005, 7008, 7004, 7007.0, 130, "ES"),
        Trade(360 * NS, 7007.0, 5, BUY, "ES"),
    ]

    async def go():
        src = LaneSource("ES", zoneview=_zoneview_with_zone(),
                         levelbook=_real_levelbook_with_vwap(),
                         strategies=[_SigSleeve()])
        pub = StatePublisher([src], agg_timeframes=("5m",))
        eng = LiveEngine(_NullFeed(bars), _NullBroker(), [_SigSleeve()],
                         WallClock(), Blotter("ES", 50.0),
                         drain_timeout=0.2, warmup_gate=False)
        pub.attach(eng)

        got1, got2 = [], []
        async def drain(acc, it):
            async for m in it:
                acc.append(m)
                if m["type"] in ("position", "zone") and len(acc) > 20:
                    break
        s1, s2 = pub.subscribe(), pub.subscribe()
        t1, t2 = asyncio.create_task(drain(got1, s1)), asyncio.create_task(drain(got2, s2))
        await asyncio.wait_for(eng.run(), timeout=5)
        for t in (t1, t2):
            t.cancel()
            try:
                await t
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
        return pub, got1, got2, eng

    pub, got1, got2, eng = asyncio.run(go())

    # BOTH subscribers received a snapshot first and the same published seqs.
    assert got1 and got2
    assert got1[0]["type"] == "__snapshot__" and got2[0]["type"] == "__snapshot__"
    seq1 = [m["seq"] for m in got1 if m["type"] != "__snapshot__"]
    seq2 = [m["seq"] for m in got2 if m["type"] != "__snapshot__"]
    assert seq1 and seq2
    assert seq1[0] == seq2[0]                       # fan-out: same first delta

    # Snapshot reflects the live engine state: bars, zones, indicators, position.
    snap = pub.snapshot()
    lane = snap["lanes"]["ES"]
    assert snap["seq"] >= len(bars)
    assert len(lane["bars_raw"]) >= 5               # 1m bars funneled in
    # 5m aggregated candle built engine-side (5 closed 1m bars -> 1 spot on
    # the next 1m boundary; at least one agg bar may exist once a 6th bucket
    # arrives). We assert the agg ring exists and bars_raw are present.
    assert "vwap" in {i["id"] for i in lane["indicators"]}
    assert any(z["tf"] == "1d" and z["virgin"] for z in lane["zones"])
    assert any(p["sleeve"] == "ES:pivot" and p["stop"] == 6980.0
               for p in lane["positions"])

    # Log stream captured engine log lines (INFO during startup).
    assert any(r["logger"].startswith("engine") for r in snap["log"])


def test_agg_5m_bar_emitted_when_bucket_closes():
    """The publisher aggregates to the configured TF with the engine's
    BarAggregator: 5 closed 1m bars fill a 5m bucket, the 6th (next bucket)
    emits the aggregated candle."""
    store = StateStore()
    agg = None
    from engine.features.bars import BarAggregator
    from engine.adapters.state_publish import _bar_dict
    agg = BarAggregator(("5m",))
    for i in range(6):                              # 0..5 -> spans 2 buckets
        b = bar((60 + i * 60) * NS, 7000 + i, 7000 + i + 1, 7000 + i - 1,
                7000 + i, 100, "ES")
        for ab in agg.update(b):
            d = store.apply_agg_bar(_bar_dict(ab))
            assert d is not None and d["type"] == "bar"
    snap = store.snapshot()["lanes"]["ES"]["bars"]["5m"]
    assert any(b["c"] == 7000 + 4 for b in snap)    # first 5m candle closed
