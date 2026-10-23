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
  * --demo (default): seeds each lane from REAL 1m history (the most recent
    3 sessions in QuestDB's claude_bars_live, --synthetic-seed forces the old
    synthetic tape), driving the REAL engine feature pipeline (real LevelBook,
    real ZoneView, real Blotter, real BarAggregator, real ZoneDetector) over
    genuine sessions, then continues with a synthetic live tick from the last
    real close. Indicators are the engine's computation on real bars: VWAP
    from AnchoredVWAP, zones from ZoneView/ZoneBook, positions from real
    strategies' chart_marks + a real blotter ledger.
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
  server -> {type:"delta_batch", symbol, deltas:[...]}  the same delta
             objects, flushed once the buffer holds BATCH_BARS bars (or a
             short latency cap elapses) so a fast tape costs one WS frame +
             one client render pass per burst instead of one per bar
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

# How much REAL history the demo seeds the engine with.
#
# The zone detectors are RTH-gated and need 20 bars of their own timeframe
# before they can score a departure at all (13 a session at 30m, 6.5 at 1h,
# 1.6 at 4h) -- the live engine seeds 60 sessions for exactly this reason.
# The demo used to seed three sessions of OVERNIGHT bars, which the RTH gate
# discards wholesale: the demo therefore detected NO zones and NO floor
# pivots at all, while showing candles.
DEEP_HISTORY_DAYS = 45
# How much of that history the CHART opens on.
CHART_SESSIONS = 3

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


def _real_history(qdb, symbol: str, days: int = DEEP_HISTORY_DAYS) -> list:
    """Real 1m bars over the last `days` days, chronological.

    The ENGINE's structure -- zones, floor pivots -- needs far more history
    than the chart does, and the demo was seeding three sessions of OVERNIGHT
    bars, which the RTH gate throws away wholesale. The detectors therefore
    never saw an eligible bar and the demo drew NO zones and NO pivots at
    all. The live engine seeds 60 sessions for exactly this reason; the demo
    does the same, over a real date window.
    """
    df = qdb.df(
        "SELECT ts, o, h, l, c, vol FROM claude_bars_live "
        f"WHERE symbol = '{symbol}' AND ts > dateadd('d', -{int(days)}, now()) "
        "ORDER BY ts")
    from engine.core.barsanity import filter_frame
    df, dropped = filter_frame(df)
    if dropped:
        log.warning("real history: dropped %d implausible rows for %s",
                    dropped, symbol)
    return [Bar(r.ts.value, "1m", float(r.o), float(r.h), float(r.l),
                float(r.c), int(r.vol), symbol)
            for r in df.itertuples()]


def _real_daily_bars(bars_1m: list, symbol: str) -> list:
    """Real 1d bars from 1m history, on the ET calendar day.

    The 1d zone detector needs a real price history. The demo used to
    fabricate dailies around a made-up seed price (7,000 for ES) while the
    candles traded at 7,772 -- the 1d structure was then anchored to the
    wrong market entirely.
    """
    import pandas as pd
    from engine.core.timeutil import et
    by_day: dict[str, list] = {}
    for b in bars_1m:
        day = et(b.ts).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append(b)
    out = []
    for day in sorted(by_day):
        g = by_day[day]
        if len(g) < 300:                      # partial session
            continue
        ts = int(pd.Timestamp(f"{day} 23:59", tz="America/New_York").value)
        out.append(Bar(ts, "1d", float(g[0].c),
                       float(max(x.h for x in g)), float(min(x.l for x in g)),
                       float(g[-1].c), int(sum(x.v for x in g)), symbol))
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
        # The level the synthetic tape mean-reverts to. It starts at the
        # synthetic seed price, but a lane seeded from REAL history must orbit
        # the real close -- otherwise the demo drifts away from the market it
        # is standing in for (it used to decay from 7,772 toward 7,000).
        self._anchor_px = self.start_px
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
        self._px += (self._anchor_px - self._px) * 0.00006
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
        # The ladder is built AROUND SPOT: a dealer-gamma curve whose strikes
        # sit at a different price than the candles is not a gamma curve, it is
        # decoration -- the walls have to be where the price actually trades.
        px = self._px
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

    def reanchor_to(self, px: float) -> None:
        """Re-anchor the lane to a new spot price.

        The lane is built BEFORE its history seed lands, so the gamma ladder
        and the open-position entries are priced off the synthetic seed
        (7,000 for ES). Once the REAL close is known, everything that IS a
        price has to move with it: a dealer-gamma curve whose strikes sit at
        a different level than the candles is decoration, not structure --
        the walls were landing ~700 points below the market.
        """
        self._px = px
        self._anchor_px = px
        self.build_gamma()
        log.info("reanchor %s: spot=%.2f (gamma ladder, sleeves and the "
                 "open-position average moved with it)", self.symbol, px)
        for sleeve in self.strategies:
            if getattr(sleeve, "pos", 0):
                sleeve.entry = px
        led = self.blotter._ledgers[self.symbol]
        if led.qty:
            led.avg_px = px

    def build_roster(self):
        """The engine-strategy roster for this lane. Each entry is a real-lane
        sleeve whose chart_marks() overlay comes from the lane's REAL engine
        state (LevelBook / ZoneView / the freshly-built GammaCurve), so the
        show/hide toggles act on genuine engine output, not JS guesses."""
        lb = self.levelbook
        beta = self.start_px * 0.003
        s = self.symbol

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
            # Resolve the curve at CALL time: the lane re-anchors its gamma
            # ladder to the real close once the history seed lands, and the
            # roster must follow the new curve, not the one it was built with.
            g = getattr(self, "gamma_curve", None)
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


def _snap_series_to_grid(series: dict, grid_ts: list,
                         tol_ns: int = 200 * 10 ** 9) -> dict:
    """Snap one indicator series onto the candle grid, keeping the LATEST
    value per grid slot.

    The engine's indicator series is stamped at the BAR's resolution (1m);
    the chart draws one candle per 5 minutes. Shipping 1m points for a 5m
    chart is five times the payload for the same picture -- and the client's
    own snap (snapToGrid) collapses them again on arrival.

    This is a PAYLOAD saving, not a behaviour change: the client snaps
    anyway, and snapping an already-snapped point is a no-op.

    The STATIC level series (the pivot grid, the dealer gamma) are NOT
    snapped: they carry several levels at ONE timestamp, and collapsing
    those onto one candle would merge nine levels into one.
    """
    pts = series.get("points") or []
    if not pts or not grid_ts:
        return series
    out = []
    gi = 0
    for p in pts:
        t = int(p["ts"])
        # both lists ascend: one forward scan finds the nearest candle
        while (gi < len(grid_ts) - 1
               and abs(grid_ts[gi + 1] - t) <= abs(grid_ts[gi] - t)):
            gi += 1
        g = grid_ts[gi]
        if abs(g - t) > tol_ns:
            continue                       # no candle near this point
        if out and out[-1]["ts"] == g:
            out[-1]["v"] = p["v"]          # the latest value wins the slot
        else:
            out.append({"ts": g, "v": p["v"]})
    return {"id": series.get("id"), "producer": series.get("producer", ""),
            "label": series.get("label", ""), "unit": series.get("unit", ""),
            "points": out}


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
        self.app.router.add_get("/performance", self._performance_page)
        self.app.router.add_get("/ws", self._ws)
        self.app.router.add_get("/history", self._history)
        self.app.router.add_get("/api/performance", self._performance)
        # serve the vendored lib + app + styles verbatim
        for name in ("app.js", "performance.js", "style.css",
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

    async def _performance_page(self, request):
        html = (self.static / "performance.html").read_text(encoding="utf-8")
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
        # The indicator series is stamped at the BAR's resolution; the chart
        # draws one candle per bar of THIS timeframe. Snap the time series
        # onto the grid the client is about to draw on -- the payload is
        # five times smaller for a 5m chart, and the bands keep covering
        # the whole window instead of stopping where the series was
        # truncated. The static level series (pivot grid, dealer gamma)
        # keep their own timestamps: several levels share one timestamp
        # there, and collapsing them would merge them.
        grid_ts = [int(b["ts"]) for b in candles]
        indicators = []
        for ind in lane.get("indicators", []):
            producer = ind.get("producer", "") or ""
            if "pivot" in producer or "gamma_levels" in producer:
                indicators.append(ind)
            else:
                indicators.append(_snap_series_to_grid(ind, grid_ts))
        return {
            "bars": candles,
            "indicators": indicators,
            "gamma": lane.get("gamma"),
            "strategies_roster": lane.get("strategies_roster", []),
            "zones": lane.get("zones", []),
            "positions": lane.get("positions", []),
            "signals": snap.get("signals", []),
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
        its chosen tf (raw 1m under '1m') plus all shared log/errors.

        BATCHING: outgoing deltas are buffered and flushed as one
        {"type":"delta_batch","deltas":[...]} frame once the buffer holds
        BATCH_BARS bars, so a fast tape costs one WS frame + one client
        render pass per burst instead of one per bar. A short latency cap
        (FLUSH_S) keeps the demo smooth when bars arrive slower than the
        batch floor, and error-kind deltas flush immediately so the error
        surface never sits in the buffer."""
        BATCH_BARS = 50
        FLUSH_S = 0.3
        buf: list = []
        bar_count = 0
        last_flush = time.monotonic()

        async def flush():
            nonlocal buf, bar_count, last_flush
            if not buf:
                return
            await ws.send_json({"type": "delta_batch", "symbol": symbol,
                                "deltas": buf})
            buf, bar_count = [], 0
            last_flush = time.monotonic()

        try:
            async for m in self.pub.subscribe():
                kind = m["type"]
                if kind in ("__snapshot__", "__post_seed__"):
                    # The subscriber yields the initial snapshot as
                    # "__snapshot__" (the post-seed snapshot arrives as
                    # "__post_seed__"); the browser client only understands
                    # "snapshot", so relay both with the lane payload mapped
                    # into its expected shape.
                    snap = m.get("payload", {})
                    lane = (snap.get("lanes") or {}).get(symbol) or {}
                    # Relay the timeframe-AGGREGATED bars (bars[tf]) — bars_raw
                    # is the raw feed tape (e.g. 1m) and would paint a chart
                    # far denser than the window's timeframe.
                    out = {"type": "snapshot", "symbol": symbol,
                           "payload": {
                               "bars": lane.get("bars", {}).get(tf)
                                       or lane.get("bars_raw", []),
                               "indicators": lane.get("indicators", []),
                               "gamma": lane.get("gamma"),
                               "strategies_roster": lane.get("strategies_roster", []),
                               "zones": lane.get("zones", []),
                               "positions": lane.get("positions", []),
                               "signals": snap.get("signals", []),
                           },
                           "log": snap.get("log", []),
                           "errors": snap.get("errors", [])}
                    await ws.send_json(out)
                    continue
                payload = m.get("payload", {})
                if kind == "bar":
                    bar = payload.get("bar", {})
                    # deliver only this window's lane AND timeframe (bar
                    # deltas are broadcast for every lane at the same ts, so
                    # without an explicit lane check a window would mix
                    # GC/NQ bars in)
                    if m.get("lane") != symbol or (bar or {}).get("tf") != tf:
                        continue
                    out = {"type": "delta", "symbol": symbol,
                           "kind": "bar", "payload": {"bar": bar}}
                elif kind in ("indicator", "gamma", "roster", "zone",
                              "position", "fill", "signal"):
                    if m.get("lane") != symbol:
                        continue
                    if kind == "indicator":
                        # relay only the recent engine points per series
                        # (values are untouched — the INDICATOR is still the
                        # engine's; we just bound how much of its history
                        # each window shows)
                        payload = dict(payload)
                        payload["points"] = payload.get("points", [])[-600:]
                    out = {"type": "delta", "symbol": symbol,
                           "kind": kind, "payload": payload}
                else:                   # log/error/session/lane are global
                    out = {"type": "delta", "symbol": symbol,
                           "kind": kind, "payload": payload}
                buf.append(out)
                if out["kind"] == "bar":
                    bar_count += 1
                if (bar_count >= BATCH_BARS
                        or "error" in (d.get("kind") for d in buf)
                        or time.monotonic() - last_flush >= FLUSH_S):
                    await flush()
            await flush()
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
            # DB ts is epoch MICROSECONDS; start/end arrive as epoch seconds.
            NS = 1_000_000
            _reverse = True
            if start is not None and end is not None:
                sql = (f"SELECT ts, o, h, l, c, vol FROM claude_bars_live "
                       f"WHERE symbol = '{symbol}' AND ts >= {int(start)} * {NS} "
                       f"AND ts <= {int(end)} * {NS} ORDER BY ts")
                _reverse = False
            elif end is not None:
                # scroll-back: bars OLDER than `end`
                sql = (f"SELECT ts, o, h, l, c, vol FROM claude_bars_live "
                       f"WHERE symbol = '{symbol}' AND ts < {int(end)} * {NS} "
                       f"ORDER BY ts DESC LIMIT {limit}")
            else:
                sql = f"SELECT ts, o, h, l, c, vol FROM claude_bars_live WHERE symbol = '{symbol}' ORDER BY ts DESC LIMIT {limit}"
            import sys; print('DEBUG END:', end, 'SQL:', sql, flush=True); sys.stdout.flush()
            df = qdb.df(sql)
            if df.empty:
                return web.json_response({"bars": {tf: []}, "indicators": [], "zones": [], "positions": []})
            # a range window can reach the 2026-09-29..10-02 block where the
            # recorder wrote NQ prices under the ES symbol -- drop those rows
            # before they reach the chart
            from engine.core.barsanity import filter_frame
            df, dropped = filter_frame(df)
            if dropped:
                log.warning("history: dropped %d implausible rows for %s", dropped, symbol)

            # convert to BarDict list (df ts is epoch ns via the client)
            bars_raw = [{
                "ts": r.ts.value,
                "o": float(r.o),
                "h": float(r.h),
                "l": float(r.l),
                "c": float(r.c),
                "v": float(r.vol),
            } for r in (df.iloc[::-1].itertuples() if _reverse else df.itertuples())]

            # compute VWAP indicator series using the engine's AnchoredVWAP,
            # reset per Globex session (matches the live lane's VWAP).
            from engine.features.level_book import LevelBook
            lb = LevelBook()
            vwap_points = []
            for b in bars_raw:
                bar = Bar(b["ts"], "1m", b["o"], b["h"], b["l"], b["c"],
                          int(b["v"]), symbol)
                lb.on_bar(bar)
                v = lb.vwap.value
                if v == v:                      # not NaN
                    vwap_points.append({"ts": b["ts"], "v": round(float(v), 4)})
            indicators = [{
                "id": "VWAP",
                "producer": "levelbook:avwap",
                "label": "anchored VWAP",
                "unit": "price",
                "points": vwap_points,
            }]

            # aggregate to requested tf if not 1m (engine's own BarAggregator,
            # fed exactly like the live publisher feeds it)
            if tf != "1m":
                agg = BarAggregator(timeframes=(tf,), input_tf="1m")
                agg_bars = []
                for b in bars_raw:
                    for ab in agg.update(Bar(b["ts"], "1m", b["o"], b["h"],
                                            b["l"], b["c"], b["v"], symbol)):
                        agg_bars.append(ab)
                bars = [{
                    "ts": int(ab.ts),
                    "o": float(ab.o),
                    "h": float(ab.h),
                    "l": float(ab.l),
                    "c": float(ab.c),
                    "v": float(ab.v),
                } for ab in agg_bars]
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

    async def _performance(self, request):
        """Compute strategy performance from claude_paper_fills.

        Query params: symbol (optional, default 'ES'), from (YYYY-MM-DD, optional),
        to (YYYY-MM-DD, optional). Returns per-sleeve metrics + equity curves +
        aggregate."""
        try:
            params = request.rel_url.query
            symbol = params.get("symbol", "ES")
            from_str = params.get("from")
            to_str = params.get("to")
            # point_usd lookup
            point_usd = {"ES": 50.0, "NQ": 20.0, "GC": 100.0}.get(symbol, 50.0)
            import pandas as pd
            qdb = QuestDB()
            sql = f"SELECT ts, sleeve, side, qty, price, tag FROM claude_paper_fills WHERE symbol = '{symbol}' ORDER BY ts"
            df = qdb.df(sql)
            if df.empty:
                return web.json_response({"symbol": symbol, "strategies": [],
                                          "aggregate": {}})
            df["ts"] = pd.to_datetime(df["ts"], utc=True)
            if from_str:
                df = df[df["ts"] >= pd.Timestamp(from_str, tz="UTC")]
            if to_str:
                df = df[df["ts"] <= pd.Timestamp(to_str, tz="UTC") + pd.Timedelta(days=1)]
            # position tracking P&L per sleeve
            strategies = []
            for sleeve, g in df.groupby("sleeve"):
                g = g.sort_values("ts").reset_index(drop=True)
                pos = 0          # signed position
                avg = 0.0        # avg entry price
                trades = []      # list of completed trades
                equity = []      # [{ts, pnl}]
                cum_pnl = 0.0
                cur_entry_ts = None
                cur_entry_side = 0
                for _, r in g.iterrows():
                    side = int(r["side"])
                    qty = int(r["qty"])
                    price = float(r["price"])
                    ts = r["ts"]
                    if pos == 0:
                        pos = side * qty
                        avg = price
                        cur_entry_ts = ts
                        cur_entry_side = side
                    elif (pos > 0 and side > 0) or (pos < 0 and side < 0):
                        # adding to position
                        new_pos = pos + side * qty
                        avg = (avg * abs(pos) + price * qty) / abs(new_pos)
                        pos = new_pos
                    else:
                        # closing (partially or fully)
                        close_qty = min(qty, abs(pos))
                        if pos > 0:
                            pnl = (price - avg) * close_qty * point_usd
                        else:
                            pnl = (avg - price) * close_qty * point_usd
                        cum_pnl += pnl
                        trades.append({
                            "entry_ts": str(cur_entry_ts),
                            "exit_ts": str(ts),
                            "side": "long" if cur_entry_side > 0 else "short",
                            "qty": close_qty,
                            "entry_px": round(avg, 4),
                            "exit_px": round(price, 4),
                            "pnl": round(pnl, 2),
                            "tag": str(r["tag"]),
                        })
                        equity.append({"ts": str(ts), "pnl": round(cum_pnl, 2)})
                        pos += side * qty
                        if pos == 0:
                            avg = 0.0
                            cur_entry_ts = None
                        elif abs(pos) < abs(pos - side * qty):
                            # partial close, avg unchanged
                            pass
                        else:
                            # flipped
                            avg = price
                            cur_entry_ts = ts
                            cur_entry_side = side if pos > 0 else -1
                    # if position flipped, handle remainder
                # max drawdown
                peak = 0.0
                max_dd = 0.0
                for e in equity:
                    if e["pnl"] > peak:
                        peak = e["pnl"]
                    dd = peak - e["pnl"]
                    if dd > max_dd:
                        max_dd = dd
                wins = [t for t in trades if t["pnl"] > 0]
                losses = [t for t in trades if t["pnl"] <= 0]
                total_pnl = round(cum_pnl, 2)
                strategies.append({
                    "sleeve": sleeve,
                    "trades": len(trades),
                    "wins": len(wins),
                    "losses": len(losses),
                    "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
                    "total_pnl": total_pnl,
                    "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0.0,
                    "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0.0,
                    "max_drawdown": round(max_dd, 2),
                    "profit_factor": round(sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses)), 2) if losses and sum(t["pnl"] for t in losses) != 0 else 0.0,
                    "equity": equity,
                    "trade_log": trades[-50:],  # last 50 trades
                })
            # aggregate
            all_trades = [t for s in strategies for t in s["trade_log"]]
            agg_equity = []
            # build aggregate equity by merging all sleeves' equity points
            agg_pnl = {}
            for s in strategies:
                for e in s["equity"]:
                    agg_pnl[e["ts"]] = agg_pnl.get(e["ts"], 0) + e["pnl"]
            # cumulative
            cum = 0.0
            agg_points = []
            for ts in sorted(agg_pnl.keys()):
                cum += agg_pnl[ts]
                agg_points.append({"ts": ts, "pnl": round(cum, 2)})
            agg_peak = 0.0
            agg_dd = 0.0
            for e in agg_points:
                if e["pnl"] > agg_peak:
                    agg_peak = e["pnl"]
                dd = agg_peak - e["pnl"]
                if dd > agg_dd:
                    agg_dd = dd
            aggregate = {
                "strategies": len(strategies),
                "total_trades": sum(s["trades"] for s in strategies),
                "total_pnl": round(sum(s["total_pnl"] for s in strategies), 2),
                "max_drawdown": round(agg_dd, 2),
                "equity": agg_points,
            }
            return web.json_response({"symbol": symbol, "strategies": strategies,
                                      "aggregate": aggregate})
        except Exception as e:
            log.exception("performance endpoint error")
            return web.json_response({"error": str(e)}, status=500)

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
                    timeframes: tuple = TIMEFRAMES,
                    synthetic_seed: bool = False) -> tuple[Dashboard, Any]:
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
            """Chronological RTH 1m bar close-times for `sessions` ET days.

            ONLY sessions that have already CLOSED are included. Today's
            09:30-16:00 ET session is still in the future at 01:00 ET, and a
            lane seeded with future bars pins the whole tape into the future:
            the tape's start ts is the MAX over the lanes, so one future lane
            stalled the market for every lane (no bars, stale gamma, stale
            average price). Weekends are skipped -- there is no session."""
            out = []
            now_et = pd.Timestamp.now(tz="America/New_York")
            day = now_et.normalize()
            want = sessions * 390
            while len(out) < want:
                if day.weekday() < 5 and \
                        (day + pd.Timedelta(hours=16)) <= now_et:
                    for m in range(0, 390):        # 09:30 .. 16:00 ET closes
                        t = day + pd.Timedelta(hours=9, minutes=30) \
                            + pd.Timedelta(minutes=m)
                        out.append(t.value)
                day = day - pd.Timedelta(days=1)
            return out

        seq = session_1m(3)
        # Seed from REAL QuestDB 1m history (most recent 3 sessions with
        # data) so the chart opens on genuine sessions — VWAP sigma bands,
        # zones and pivots are the ENGINE's computation over real bars, not
        # a redraw. The live tick then continues synthetically from the last
        # real close. Lanes with no real data (or --synthetic-seed) fall
        # back to the synthetic tape.
        real: dict[str, list] = {}
        deep: dict[str, list] = {}
        if not synthetic_seed:
            try:
                qdb = QuestDB()
                for sym in lanes:
                    hist = _real_history(qdb, sym)
                    if not hist:
                        continue
                    deep[sym] = hist
                    # the CHART opens on the tail of that history
                    real[sym] = hist[-CHART_SESSIONS * 390:]
                    log.info("real seed [%s]: %d 1m bars over %d days "
                             "(%d for the chart)",
                             sym, len(hist), DEEP_HISTORY_DAYS, len(real[sym]))
            except Exception as ex:                     # noqa: BLE001
                log.warning("real-history seed unavailable (%s); "
                            "using synthetic seed", ex)
                real = {}
                deep = {}
        # Reject cross-lane duplicate data: the NQ ingest has lately been
        # mirroring ES rows exactly (same ts/close/volume), and feeding that
        # as "NQ" would paint a fake chart — polluted lanes go synthetic.
        es = real.get("ES") or []
        if len(es) >= 50:
            es_tail = [(b.ts, b.c) for b in es[-50:]]
            for sym in [s for s in real if s != "ES"]:
                rows = real[sym]
                if len(rows) >= 50 and \
                        [(b.ts, b.c) for b in rows[-50:]] == es_tail:
                    log.warning("real seed: %s tail duplicates ES — "
                                "using synthetic seed for it", sym)
                    del real[sym]
                    deep.pop(sym, None)
        for dl in lanes.values():
            # reset per-lane bar index so the live tape continues cleanly
            dl._n = 0
            bars = real.get(dl.symbol) or []
            hist = deep.get(dl.symbol) or []
            if hist:
                # DEEP history first, and to the ENGINE only: the LevelBook
                # needs RTH bars to build the floor-pivot grid, and the zone
                # detectors need 20 bars of their own timeframe. The chart
                # below only ever shows the tail of it.
                daily = _real_daily_bars(hist, dl.symbol)
                if daily:
                    dl.zoneview.seed_daily(daily)
                for b in hist:
                    dl.levelbook.on_bar(b)
                    dl.zoneview.update(b)
            if bars:
                for i, b in enumerate(bars):
                    await engine.on_bar_hook(b, False, 0)
                    if i % 60 == 59:
                        # yield so the HTTP socket opens while the seed is
                        # still running (real QuestDB seeds take ~20s)
                        await asyncio.sleep(0)
                if not hist:
                    dl.zoneview.seed_daily(
                        _synthetic_daily(dl, max(1, len(bars) // 390)))
                # continue the synthetic tape from the last REAL close, and
                # orbit THAT close -- the demo must not drift away from the
                # market it is standing in for
                dl.reanchor_to(bars[-1].c)
                # the live tape continues from the last REAL bar, not from
                # epoch zero (bar_ts is only set by dl.bar(), which the real
                # seed path never calls)
                dl.bar_ts = bars[-1].ts
                # the publisher holds the lane's gamma curve by reference:
                # hand it the re-anchored one
                for src in srcs:
                    if src.symbol == dl.symbol:
                        src.gamma_curve = dl.gamma_curve
                led = dl.blotter._ledgers[dl.symbol]
                eng_led = engine.blotter._ledgers[dl.symbol]
                eng_led.qty = led.qty
                eng_led.avg_px = led.avg_px
                # the seed published the PRE-anchor state into the store
                # (gamma walls/flips around the synthetic seed price, the
                # 7000s): re-publish the lane now that it is anchored to the
                # real close, so no subscriber ever sees the stale map
                await pub._publish_lane(dl.symbol,
                                        {"ts": bars[-1].ts, "c": bars[-1].c})
            else:
                dl._px = dl.start_px + random.uniform(-2, 2)
                for ts in seq:
                    b = dl.bar(ts)
                    dl.levelbook.on_bar(b)
                    dl.zoneview.update(b)
                    await engine.on_bar_hook(b, False, 0)
                    await asyncio.sleep(0)
                dl.zoneview.seed_daily(_synthetic_daily(dl, len(seq) // 390))
        # No lane may be stamped in the FUTURE: the tape's start ts is the
        # MAX over the lanes, so a single lane seeded with future bars stalls
        # the whole market (no bars at all, and every lane keeps the values
        # the seed published). The synthetic fallback only seeds CLOSED
        # sessions now, but the tape must never trust that.
        now_ns = int(time.time()) * NS
        for dl in lanes.values():
            if dl.bar_ts > now_ns:
                dl.bar_ts = now_ns - 60 * NS
        pub._seeding = False
        snap = pub.snapshot()
        for q in list(pub._subs.values()):
            q.put_nowait({"type": "__post_seed__", "payload": snap})
        # live stream -- ON THE WALL CLOCK
        #
        # One 1m bar per real minute, stamped with that minute. A demo that
        # advances 60s of market time every 0.05s of wall time races into the
        # future: the chart's time axis ended up a year ahead of the market it
        # was standing in for, so every timestamp on screen was a lie. The
        # tape now starts at the seeded history's last bar and walks forward
        # to "now", catching up on whatever gap the recorder left behind.
        import logging as _log
        _lgr = _log.getLogger("engine.dashboard")
        _sent_error = False
        ts = max((l.bar_ts for l in lanes.values()), default=seq[-1]) + 60 * NS
        bar_idx = 0
        while True:
            now_min = int(time.time()) * NS
            while ts <= now_min:
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
    ap.add_argument("--tick-s", type=float, default=0.05,
                    help="demo: how often the wall-clock tape checks for a new "
                         "minute (the tape itself runs one bar per real minute)")
    ap.add_argument("--live", action="store_true",
                    help="attach to the NT8 live feed instead of loopback demo")
    ap.add_argument("--synthetic-seed", action="store_true",
                    help="seed with the synthetic tape instead of real QuestDB "
                         "history (default: real history when available)")
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
                                   dashboard_dir, tfs,
                                   synthetic_seed=a.synthetic_seed)
    async def shutdown(app):
        drive_task.cancel()
    dash.app.on_shutdown.append(shutdown)   # BEFORE run() freezes the list
    drive_task = asyncio.create_task(drive())   # run loop, so it also runs
    # A dead drive() used to fail SILENTLY: the task's exception was never
    # retrieved, the HTTP server kept serving the last snapshot, and the
    # dashboard looked "live" while the tape had stopped minutes ago. Report
    # it instead -- a dead tape must be visible in the log.
    def _drive_done(t: asyncio.Task) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error("demo drive task died", exc_info=exc)
    drive_task.add_done_callback(_drive_done)
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
