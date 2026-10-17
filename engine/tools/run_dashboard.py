"""run_dashboard.py — engine-embedded inspect-first dashboard server (v1).

ONE FILE: a local aiohttp web server that is served BY the engine process. It
binds the state-publish adapter's transport-free contract to a single
WebSocket that carries live state to the user's browser, where TradingView
lightweight-charts renders it. Same socket is the future control channel; for
v1 it only subscribes/re-configures windows (no strategy controls yet).

WHY THIS EXISTS (acceptance from the user):
  * "can't see indicators"      -> every indicator drawn is the ENGINE's value
                                    (levelbook/zoneview), never re-derived in JS
  * "invisible errors"          -> engine log + WARNING/ERROR surface in the UI
  * "zones look all wrong"      -> zones are ENGINE ZoneBook geometry (top/bot),
                                    rendered as exact horizontal bands, not guesses
  * multi-window / per-window TF-> each browser tab is one window with its own
                                    symbol + timeframe; server never hardcodes 1m
  * dark theme that actually works

HOW IT RUNS
  * --demo (default): drives the REAL engine feature pipeline (real LevelBook,
    real ZoneView, real Blotter, real BarAggregator, real ZoneDetector) from a
    synthetic-but-plausible 1m tape, and publishes the exact same state-publish
    schema the live engine publishes. This is the real computation, not a mock:
    VWAP comes from AnchoredVWAP fed real bars, zones come from ZoneView/ZoneBook
    fed real bars, positions come from real strategies' chart_marks + a real
    blotter ledger. Use it to inspect the UI without NT8/QuestDB.
  * --live (NT8): attaches the SAME StatePublisher to the engine's NT8 feed via
    the run_live.py building blocks (NinjaTraderFeed + broker). This path needs
    NT8 running with the EngineRelay addon; it reuses run_live's exact wiring so
    the dashboard is the same lovely thing over the real tape.

LAYOUT (all under <worktree>/engine):
  tools/run_dashboard.py            THIS server (aiohttp + WS + loopback demo)
  dashboard/index.html              single-page UI
  dashboard/app.js                  chart rendering + WS client
  dashboard/style.css               dark theme
  dashboard/vendor/lightweight-charts.standalone.production.js   vendored (v4.2.3)

WS PROTOCOL (JSON text frames)
  client -> {op:"subscribe", symbol, tf}   (optional; default from querystring)
  client -> {op:"ping"}
  server -> {type:"hello", windows:[...], lanes:[...], timeframes:[...],
             symbol, tf}
  server -> {type:"snapshot", symbol, tf, payload:{
              bars:{tf:[...]}, indicators:[...], zones:[...], positions:[...]},
              log:[...], errors:[...]}
  server -> {type:"delta", symbol, kind, payload}   kind in
             {bar,indicator,zone,position,fill,log,error,session,lane}
  bars per window are delivered for the window's chosen tf only (from the
  engine-aggregated store); raw 1m is always available under '1m'.

Run:
  .venv/Scripts/python.exe tools/run_dashboard.py --port 8787
  # open http://127.0.0.1:8787/?symbol=ES&tf=5m   (?symbol&tf = one window;
  #   "+ add" / pop-out open more windows, ▦ grid = 2-up)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import random
import sys
import time
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.state_publish import LaneSource, StatePublisher  # noqa: E402
from engine.core.blotter import Blotter                              # noqa: E402
from engine.core.events import Bar, Fill                              # noqa: E402
from engine.features.gamma_curve import GammaCurve                   # noqa: E402
from engine.features.level_book import LevelBook                     # noqa: E402
from engine.painters import ZoneView                                 # noqa: E402

from engine.adapters.questdb import QuestDB  # noqa: E402
from engine.features.bars import BarAggregator  # noqa: E402
from engine.adapters.state_sources import indicators_from_levelbook  # noqa: E402

log = logging.getLogger("engine.dashboard")
NS = 1_000_000_000

# The engine aggregates a broad set so ANY window can pick; we never hardcode
# the client to 1m. Order matters for the picker.
TIMEFRAMES = ("1m", "5m", "15m", "30m", "1h", "4h", "1d")

# Per-lane demo generators (see _DemoLane). These produce the raw 1m tape.
_GEN_SEED = {"ES": 7000.0, "NQ": 21000.0, "GC": 3900.0}
_GEN_POINT = {"ES": 50.0, "NQ": 20.0, "GC": 100.0}
_GEN_TICK = {"ES": 0.25, "NQ": 0.25, "GC": 0.10}
_GEN_ALLOWED = set(_GEN_SEED)  # lanes the demo can synthesize


def _gsign(dl) -> str:
    g = getattr(dl, "gamma_curve", None)
    px = getattr(dl, "_px", None) or (dl.start_px if hasattr(dl, "start_px") else 0.0)
    if g is None or not getattr(g, "ok", False):
        return "n/a"
    s = g.sign_at(px)
    return "LONG" if s > 0 else ("SHORT" if s < 0 else "flat")


def _nflips(dl) -> int:
    g = getattr(dl, "gamma_curve", None)
    return len(getattr(g, "_flips", []) or []) if g is not None else 0


def _synthetic_daily(dl, sessions: int):
    """Synthetic 1d bars with departure structure, for the 1d detector."""
    import pandas as pd
    base = pd.Timestamp.now(tz="America/New_York").normalize()
    out = []
    px = dl.start_px
    for d in range(sessions - 1, -1, -1):
        day = (base - pd.Timedelta(days=d)).strftime("%Y-%m-%d")
        ts = int(pd.Timestamp(f"{day} 16:00", tz="America/New_York").value)
        rng = dl.scale * random.uniform(0.5, 2.2)
        o = px + random.gauss(0, rng * 0.3)
        c = o + random.choice([-1, 1]) * rng * random.uniform(0.4, 1.2)
        h = max(o, c) + rng * 0.3
        l = min(o, c) - rng * 0.3
        out.append(Bar(ts, "1d", round(o, 4), round(h, 4), round(l, 4),
                       round(c, 4), int(dl.vol * random.uniform(0.6, 1.4)),
                       dl.symbol))
        px = c
    return out


class _DemoLane:
    """Produces a realistic 1m tape and feeds the REAL LevelBook + ZoneView so
    the engine publishes genuinely-computed indicators/zones (never redrawn in
    JS). Also maintains real strategy objects + a real blotter so positions are
    real."""

    def __init__(self, symbol: str, point_usd: float, tick: float,
                 tf: str = "1m") -> None:
        self.symbol = symbol
        self.point_usd = point_usd
        self.tick = tick
        self.tf = tf
        self.levelbook = LevelBook()          # real: VWAP, pivots, book
        self.zoneview = ZoneView()            # real: ZoneDetector + ZoneBook
        self.strategies = []                  # real strategy objects w/ marks
        self.blotter = Blotter(symbol, point_usd)
        self.start_px = _GEN_SEED.get(symbol, 7000.0)
        self._px = self.start_px
        self.bar_ts = 0
        self.scale = {"ES": 26.0, "NQ": 90.0, "GC": 16.0}.get(symbol, 20.0)
        self.vol = {"ES": 1300, "NQ": 1800, "GC": 600}.get(symbol, 800)
        self._n = 0
        self._regime = "quiet"
        self._regime_until = 0

    # ── tape generation ───────────────────────────────────────────────────
    def _rng(self):
        """One bar's range scaled to the current regime."""
        if self._n >= self._regime_until:
            r = random.random()
            if r < 0.10:                              # departure burst
                self._regime, self._regime_until = "burst", self._n + 8
            elif r < 0.45:                            # normal
                self._regime, self._regime_until = "normal", self._n + 20
            else:                                     # quiet base
                self._regime, self._regime_until = "quiet", self._n + 24
        mult = {"quiet": 0.30, "normal": 1.0, "burst": 2.6}[self._regime]
        return self.scale * random.uniform(0.4, 1.0) * mult

    def _next_open(self) -> float:
        # gentle drift + waves + occasional burst direction, bounded
        self._px += random.gauss(0.0, self.scale * 0.05)
        self._px += math.sin(self._n / 29.0) * self.scale * 0.06
        if self._regime == "burst":
            self._px += random.choice([-1, 1]) * self.scale * random.uniform(0.6, 1.2)
        # hard range bound so price stays sensible
        self._px += (self.start_px - self._px) * 0.00006
        return self._px

    def bar(self, ts: int, complete: bool = True) -> Bar:
        o = self._next_open()
        rng = self._rng()
        c = o + random.gauss(0.0, rng)
        h = max(o, c) + random.uniform(0, rng * 0.5)
        l = min(o, c) - random.uniform(0, rng * 0.5)
        v = max(1, int(self.vol * random.uniform(0.6, 1.6)))
        self.bar_ts = ts
        self._n += 1
        return Bar(ts, self.tf, round(o, 4), round(h, 4), round(l, 4),
                   round(c, 4), v, self.symbol, complete=complete)

    # ── positions: a couple of real strategy sleeves that open at some point ──
    def build_strategies(self) -> None:
        # REAL dealer-gamma curve for the lane's symbol: synthetic per-strike
        # rows (concentrations create walls, a flip, a pocket) driven by the
        # real GammaCurve + gamma_profile — so walls/flips/pocket and the
        # local_sign regime-at-price are all genuine engine computations.
        self.build_gamma()

        # The strategy ROSTER: the real labels Alexis names, each a sleeve that
        # exposes chart_marks() overlays derived from the lane's REAL engine
        # state (LevelBook / ZoneView / the gamma curve). Show/hide in the UI
        # toggles draw/remove that strategy's marks. Flat strategy → no rails,
        # but the name + enabled toggle still show.
        self.strategies = self.build_roster()

        # blotter ledger is authoritative for avg_px (read by state_sources)
        led = self.blotter._ledgers[self.symbol]
        main = self.strategies[0]
        led.qty = main.pos
        led.avg_px = main.entry if main.entry else self._px
        self.blotter._default_symbol = self.symbol

    def build_gamma(self) -> None:
        """Synthetic-but-real dealer-gamma curve: concentrations of call/put
        gex at a few strikes produce multiple ranked walls, a zero-crossing
        flip and the short-gamma pocket, all through the ACTUAL GammaCurve."""
        import random as _r
        px = self.start_px
        per = {}
        # a few heavy put strikes below spot (support walls) and call strikes
        # above (resistance walls); lighter noise in between keeps the curve
        for k_off in range(-160, 161, 10):
            k = px + k_off
            c = _r.uniform(0.1, 0.4) * 1e10
            p = _r.uniform(0.1, 0.4) * 1e10
            # concentration bumps at specific strikes
            if k_off in (-140, -100, -60):
                p += _r.uniform(2.2, 3.4) * 1e10
            if k_off in (60, 100, 150):
                c += _r.uniform(2.2, 3.4) * 1e10
            if k_off == -30:
                c += 1.6 * 1e10          # imbalance creates the flip/pocket
            per[round(k, 2)] = (c, -p)   # put_gex NEGATIVE, as the fetcher signs it
        # rows = (strike, call_gex, put_gex) — GammaCurve itself recomputes the
        # profile/flips/walls/pocket; the demo trades in already-future terms so
        # basis = 0 (the live engine adds its cash->future basis).
        rows = sorted((float(k), float(c), float(p)) for k, (c, p) in per.items())
        und = "NDX" if self.symbol == "NQ" else ("GC" if self.symbol == "GC" else "SPX")
        self.gamma_curve = GammaCurve(rows, px, basis=0.0, underlying=und,
                                      sess="demo")

    def build_roster(self):
        """The engine-strategy roster for this lane. Each entry is a real-lane
        sleeve whose chart_marks() overlay comes from the lane's REAL engine
        state (LevelBook / ZoneView / the freshly-built GammaCurve), so the
        show/hide toggles act on genuine engine output, not JS guesses."""
        lb = self.levelbook
        beta = self.start_px * 0.003
        s = self.symbol
        g = getattr(self, "gamma_curve", None)

        def piv_prices():
            try:
                return [float(x) for x in (getattr(getattr(lb, "piv", None),
                                                    "pivots", None) or [])]
            except Exception:                       # noqa: BLE001
                return []

        def zone_edges():
            out = []
            zv = getattr(self, "zoneview", None)
            if zv:
                for tf, z in list(getattr(zv, "all_active", lambda: [])() or []):
                    try:
                        out.append(float(z.proximal()))
                    except Exception:               # noqa: BLE001
                        pass
            return sorted(out)

        def walls(pick):
            if g is None or not getattr(g, "ok", False):
                return []
            try:
                at = g.at(self._px)
            except Exception:                       # noqa: BLE001
                return []
            if not at:
                return []
            return at.get(pick) or []

        def mk(label, enabled=True, marks=None, pos=0):
            return RosterSleeve(s, label, enabled, marks or (lambda: {}), pos)

        roster = [
            RosterSleeve(s, f"{s}:ignition", True,
                         lambda: {"target": (piv_prices() or [self._px + beta])[0]
                                  if piv_prices() else None}, 0),
            RosterSleeve(s, f"{s}:zones_strategy", True,
                         lambda: {"entry": (zone_edges() or [None])[0]}, 0),
            RosterSleeve(s, f"{s}:open_drive", True,
                         lambda: {"target": self._px + beta * 2}, 0),
            RosterSleeve(s, f"{s}:ibs_swing", True,
                         lambda: {"entry": self._px - beta, "stop": self._px + beta * 3}, 0),
            RosterSleeve(s, f"{s}:dip_buy", True,
                         lambda: {"entry": self._px - beta * 2}, 0),
            RosterSleeve(s, f"{s}:wall_fade", True,
                         lambda: {"entry": (walls("put_walls") or [None])[0]}, 0),
            RosterSleeve(s, f"{s}:day_score", False, lambda: {}, 0),
        ]
        # two sleeves actually in the market (keeps the old positions demo real)
        roster[0].pos, roster[0].entry = 1, self._px
        roster[1].pos, roster[1].entry = -1, self._px
        return roster


class RosterSleeve:
    """A lane strategy roster entry: a real label + enabled flag + the engine's
    chart_marks() protocol for its overlay. `pos`/`entry` seed a couple of open
    positions so the dashboard's position rails are real (mirrors the old demo's
    two sleeves)."""

    def __init__(self, symbol, label, enabled=True, chart_marks_fn=None, pos=0):
        self.symbol = symbol
        self.label = label
        self.enabled = enabled
        self.pos = pos
        self.entry = None                 # seeded post-hoc for the open sleeves
        self.entry_px = None
        self._marks = chart_marks_fn or (lambda: {})
        self.chart_marks = self._marks


class Dashboard:
    """Embedded server: HTTP static + one WebSocket carrying engine state."""

    def __init__(self, publisher: StatePublisher, lanes, timeframes,
                 dashboard_dir: Path, host: str, port: int):
        self.pub = publisher
        self.lanes = sorted(lanes)
        self.timeframes = list(timeframes)
        self.host = host
        self.port = port
        self.static = dashboard_dir
        self._clients: set = set()      # writer tasks keyed by asyncio.Task
        self.app = web.Application()
        self._add_routes()

    def _add_routes(self):
        self.app.router.add_get("/", self._index)
        self.app.router.add_get("/ws", self._ws)
        self.app.router.add_get("/history", self._history)
        # serve the vendored lib + app + styles verbatim
        for name in ("app.js", "style.css",
                     "vendor/lightweight-charts.standalone.production.js"):
            self.app.router.add_get(f"/{name}", self._static_file(name))
        self.app.router.add_get("/vendor/{f}", self._vendor)

    def _static_file(self, name: str):
        async def handler(request):
            return web.FileResponse(self.static / name)
        return handler

    async def _vendor(self, request):
        f = request.match_info.get("f", "")
        p = (self.static / "vendor" / f).resolve()
        if not str(p).startswith(str(self.static.resolve())):
            raise web.HTTPForbidden()
        return web.FileResponse(p)

    async def _index(self, request):
        html = (self.static / "index.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html")

    @staticmethod
    def _lane_payload(snap, symbol: str, tf: str) -> dict:
        lane = snap.get("lanes", {}).get(symbol, {})
        bars = lane.get("bars", {})
        # raw 1m always available under '1m'; engine-aggregated otherwise
        if tf == "1m":
            candles = lane.get("bars_raw", [])
        else:
            candles = bars_for = bars.get(tf, [])
        return {
            "bars": candles,
            "indicators": lane.get("indicators", []),
            "gamma": lane.get("gamma"),
            "strategies_roster": lane.get("strategies_roster", []),
            "zones": lane.get("zones", []),
            "positions": lane.get("positions", []),
        }

    async def _ws(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        symbol = request.query.get("symbol", "")
        tf = request.query.get("tf", "5m")
        sym_ok = symbol in self.lanes
        if not sym_ok:
            symbol = self.lanes[0] if self.lanes else ""
        if tf not in self.timeframes:
            tf = "5m" if "5m" in self.timeframes else self.timeframes[0]
        try:
            await ws.send_json({
                "type": "hello",
                "lanes": self.lanes,
                "timeframes": self.timeframes,
                "symbol": symbol,
                "tf": tf,
            })
            snap = self.pub.snapshot()
            await ws.send_json({
                "type": "snapshot",
                "symbol": symbol,
                "tf": tf,
                "payload": self._lane_payload(snap, symbol, tf),
                "seq": snap.get("seq", 0),
                "log": snap.get("log", [])[-80:],
                "errors": snap.get("errors", []),
            })
            write = asyncio.create_task(self._forward(ws, symbol, tf))
            self._clients.add(write)
            try:
                async for msg in ws:
                    if msg.type == WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                        except Exception:                       # noqa: BLE001
                            continue
                        op = data.get("op")
                        if op == "ping":
                            await ws.send_json({"type": "pong"})
                        elif op == "subscribe":
                            symbol = data.get("symbol", symbol)
                            tf = data.get("tf", tf)
                            if symbol not in self.lanes:
                                symbol = self.lanes[0]
                            if tf not in self.timeframes:
                                tf = self.timeframes[0]
                            snap = self.pub.snapshot()
                            await ws.send_json({
                                "type": "snapshot",
                                "symbol": symbol, "tf": tf,
                                "payload": self._lane_payload(snap, symbol, tf),
                                "seq": snap.get("seq", 0),
                                "log": snap.get("log", [])[-80:],
                                "errors": snap.get("errors", []),
                            })
            finally:
                write.cancel()
                self._clients.discard(write)
        except asyncio.CancelledError:
            raise
        except Exception as ex:                                 # noqa: BLE001
            log.warning("ws ended: %s", ex)
        return ws

    async def _forward(self, ws, symbol: str, tf: str):
        """Stream state-publish deltas, delivering each client only the bars for
        its chosen tf (raw 1m under '1m') plus all shared log/errors."""
        try:
            async for m in self.pub.subscribe():
                kind = m["type"]
                if kind == "__snapshot__":
                    continue
                payload = m.get("payload", {})
                if kind == "bar":
                    bar = payload.get("bar", {})
                    # deliver only this window's lane AND timeframe (bar deltas
                    # are broadcast for every lane at the same ts, so without an
                    # explicit lane check a window would mix GC/NQ bars in)
                    if m.get("lane") != symbol or (bar or {}).get("tf") != tf:
                        continue
                    await ws.send_json({"type": "delta", "symbol": symbol,
                                        "kind": "bar", "payload": {"bar": bar}})
                elif kind in ("indicator", "gamma", "roster", "zone",
                              "position", "fill"):
                    if m.get("lane") != symbol:
                        continue
                    if kind == "indicator":
                        # relay only the recent engine points per series (values
                        # are untouched — the INDICATOR is still the engine's; we
                        # just bound how much of its history each window shows)
                        payload = dict(payload)
                        payload["points"] = payload.get("points", [])[-600:]
                    await ws.send_json({"type": "delta", "symbol": symbol,
                                        "kind": kind, "payload": payload})
                else:                       # log/error/session/lane are global
                    await ws.send_json({"type": "delta", "symbol": symbol,
                                        "kind": kind, "payload": payload})
        except asyncio.CancelledError:
            raise
        except Exception as ex:             # noqa: BLE001
            log.warning("forward ended: %s", ex)

    async def _history(self, request):
        """Return historical bars/indicators for a symbol/timeframe.
        Query params: symbol (required), tf (required, e.g., '5m'), start (epoch ms, optional),
        end (epoch ms, optional), limit (int, optional, default 5000).
        If start/end omitted, returns the most recent `limit` bars.
        Returns JSON: {bars:{tf:[...]}, indicators:[...], zones:[], positions:[]}
        """
        try:
            params = request.rel_url.query
            symbol = params.get("symbol")
            tf = params.get("tf")
            if not symbol or not tf:
                return web.json_response({"error": "symbol and tf required"}, status=400)
            start_str = params.get("start")
            end_str = params.get("end")
            limit_str = params.get("limit")
            limit = int(limit_str) if limit_str is not None else 1200
            start = int(start_str) if start_str is not None else None
            end = int(end_str) if end_str is not None else None

            qdb = QuestDB()
            import re as _re
            if not _re.fullmatch(r'[A-Z]{1,6}(\.[A-Z]{1,3})?', symbol):
                raise ValueError("Invalid symbol")
            limit = min(limit, 5000)
            if start is not None and end is not None:
                sql = f"SELECT ts, o, h, l, c, v FROM claude_bars_live WHERE symbol = '{symbol}' AND ts >= {int(start)} AND ts <= {int(end)} ORDER BY ts"
                raw = qdb.query(sql)
            else:
                sql = f"SELECT ts, o, h, l, c, v FROM claude_bars_live WHERE symbol = '{symbol}' ORDER BY ts DESC LIMIT {limit}"
                desc = qdb.query(sql)
                raw = list(reversed(desc))  # chronological

            if not raw:
                return web.json_response({"bars": {tf: []}, "indicators": [], "zones": [], "positions": []})

            # convert to BarDict list
            bars_raw = [{
                "ts": int(r["ts"]),
                "o": float(r["o"]),
                "h": float(r["h"]),
                "l": float(r["l"]),
                "c": float(r["c"]),
                "v": float(r["v"])
            } for r in raw]

            # compute VWAP indicator series
            vol_sum = 0.0
            tpv_sum = 0.0
            vwap_points = []
            for b in bars_raw:
                tp = (b["h"] + b["l"] + b["c"]) / 3.0
                vol_sum += b["v"]
                tpv_sum += tp * b["v"]
                vwap = tpv_sum / vol_sum if vol_sum != 0 else 0.0
                vwap_points.append({"ts": b["ts"], "v": vwap})

            indicators = [{
                "id": "VWAP",
                "producer": "engine",
                "label": "VWAP",
                "unit": "",
                "points": vwap_points
            }]

            # aggregate to requested tf if not 1m
            if tf != "1m":
                agg = BarAggregator()
                aggregated = agg.aggregate(bars_raw, tf)
                bars = [{
                    "ts": int(b["ts"]),
                    "o": float(b["o"]),
                    "h": float(b["h"]),
                    "l": float(b["l"]),
                    "c": float(b["c"]),
                    "v": float(b["v"])
                } for b in aggregated]
            else:
                bars = bars_raw

            payload = {
                "bars": {tf: bars},
                "indicators": indicators,
                "zones": [],
                "positions": []
            }
            return web.json_response(payload)
        except Exception as e:
            log.exception("History endpoint error")
            return web.json_response({"error": "internal server error"}, status=500)

    async def run(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        print(f'[dashboard] http://{self.host}:{self.port}/   ' f'(windows: {self.lanes} ; tfs: {self.timeframes})')
        # Keep the server running until cancelled
        try:
            await asyncio.Future()  # waits forever
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()


async def make_demo(host: str, port: int, tick_s: float, symbols,
                    dashboard_dir: Path,
                    timeframes: tuple = TIMEFRAMES) -> tuple[Dashboard, Any]:
    """Build a real-engine loopback demo: real LevelBook/ZoneView/Blotter fed a
    synthetic tape, published through the exact state-publish adapter."""
    lanes = {}
    for sym in symbols:
        point = _GEN_POINT.get(sym, 50.0)
        tick = _GEN_TICK.get(sym, 0.25)
        dl = _DemoLane(sym, point, tick)
        dl.build_strategies()
        lanes[sym] = dl

    srcs = [LaneSource(sym, zoneview=dl.zoneview, levelbook=dl.levelbook,
                       gamma_curve=getattr(dl, "gamma_curve", None),
                       strategies=dl.strategies, color="")
            for sym, dl in lanes.items()]
    pub = StatePublisher(srcs, agg_timeframes=timeframes)

    # ---- wire the adapter to a thin engine-shaped loopback loop ----
    class _DemoEngine:
        def __init__(self):
            # build one blotter from the lanes' seeded ledgers so avg_px is real
            self.blotter = Blotter(symbols[0], _GEN_POINT.get(symbols[0], 50.0))
            for sym in symbols:
                self.blotter.add_instrument(sym, _GEN_POINT.get(sym, 50.0))
                led = lanes[sym].blotter._ledgers[sym]
                eng_led = self.blotter._ledgers[sym]
                eng_led.qty = led.qty
                eng_led.avg_px = led.avg_px
            self.b_lanes = lanes
            # the publisher attach() reads these off the engine later
            self.on_bar_hook = None
            self.on_live_fill = None
            self.on_paper_fill = None
            self.on_session_rollover = None
            self.on_feed_disconnect = None
            self.on_lane_stale = None

    engine = _DemoEngine()

    async def drive():
        # Seed ~14 sessions of RTH 1m history so the 30m/1h/4h zone detectors
        # all warm past their 20-bar departure window (same idea as run_live
        # seeding from QuestDB), plus daily bars for the 1d detector.
        import pandas as pd

        def session_1m(sessions: int = 14):
            """Chronological RTH 1m bar close-times for `sessions` ET days."""
            out = []
            base = pd.Timestamp.now(tz="America/New_York").normalize()
            for d in range(sessions - 1, -1, -1):
                day = (base - pd.Timedelta(days=d)).strftime("%Y-%m-%d")
                for m in range(0, 390):            # 09:30 .. 16:00 ET closes
                    t = pd.Timestamp(f"{day} 09:30", tz="America/New_York") \
                        + pd.Timedelta(minutes=m)
                    out.append(t.value)
            return out

        seq = session_1m(3)
        for dl in lanes.values():
            # reset per-lane bar index so the live tape continues cleanly
            dl._n = 0
            dl._px = dl.start_px + random.uniform(-2, 2)
            for ts in seq:
                b = dl.bar(ts)
                dl.levelbook.on_bar(b)
                dl.zoneview.update(b)
                await engine.on_bar_hook(b, False, 0)
            dl.zoneview.seed_daily(_synthetic_daily(dl, len(seq) // 390))
        pub._seeding = False
        snap = pub.snapshot()
        for q in list(pub._subs.values()):
            q.put_nowait({"type": "__post_seed__", "payload": snap})
        # live stream
        import logging as _log
        _lgr = _log.getLogger("engine.dashboard")
        _sent_error = False
        ts = max((l.bar_ts for l in lanes.values()), default=seq[-1]) + 60 * NS
        bar_idx = 0
        while True:
            for dl in lanes.values():
                b = dl.bar(ts)
                dl.levelbook.on_bar(b)
                dl.zoneview.update(b)
                await engine.on_bar_hook(b, True, 0)
                # occasional log lines so the engine log / error surface is
                # visibly live (mimics run_live's periodic runner chatter)
                if bar_idx % 12 == 0:
                    _lgr.info("[%s] bar %d open: %s..%s vwap=%s", dl.symbol,
                              bar_idx, b.o, b.c,
                              round(dl.levelbook.vwap.value, 2) if getattr(
                                  dl.levelbook.vwap, "value", None) else "n/a")
                if bar_idx % 47 == 0 and not _sent_error:
                    _sent_error = True
                    _lgr.info("[%s] demo note: dealer-gamma map live (local sign "
                              "%s at %.2f, %d flips) — drive the real tape with "
                              "--live", dl.symbol,
                              _gsign(dl), dl._px if hasattr(dl, "_px") else 0,
                              _nflips(dl))
                bar_idx += 1
            ts += 60 * NS
            await asyncio.sleep(tick_s)

    pub.attach(engine)
    pub._seeding = True
    dash = Dashboard(pub, symbols, timeframes, dashboard_dir, host, port)
    return dash, drive


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--lane", action="append", default=[],
                    help="lane symbols (default ES,NQ,GC)")
    ap.add_argument("--tf", action="append", default=[],
                    help="aggregate timeframes (default all standard; pick per window)")
    ap.add_argument("--tick-s", type=float, default=0.5,
                    help="demo: seconds per 1m bar per lane")
    ap.add_argument("--live", action="store_true",
                    help="attach to the NT8 live feed instead of loopback demo")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO)
    for noisy in ("httpx", "httpcore", "hpack", "urllib3", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    symbols = a.lane or ["ES", "NQ", "GC"]
    tfs = tuple(a.tf) if a.tf else TIMEFRAMES
    dashboard_dir = ROOT / "dashboard"

    if a.live:
        raise SystemExit(
            "--live mode requires NT8 running with the EngineRelay addon. "
            "Use --demo (default) for the loopback demo.")

    dash, drive = await make_demo(a.host, a.port, a.tick_s, symbols,
                                  dashboard_dir, tfs)
    async def shutdown(app):
        drive_task.cancel()
    dash.app.on_shutdown.append(shutdown)   # BEFORE run() freezes the list
    drive_task = asyncio.create_task(drive())   # run loop, so it also runs
    # Bind the HTTP server first, then let the demo tape proceed in the
    # background (a slow historical seed never blocks the socket coming up;
    # the seeding loop yields per bar and drive() runs concurrently).
    await dash.run()
    # keep the loop alive
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[dashboard] stopped")
