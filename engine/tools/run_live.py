"""Run the LiveEngine against the NT8 EngineRelay sockets.

    # observation mode (default): consume the feed, no strategies, no orders
    .venv/Scripts/python.exe tools/run_live.py --seconds 60

    # trade on Sim101 with chosen sleeves (deployable causal configs)
    .venv/Scripts/python.exe tools/run_live.py --strategies opendrive,ignition --seconds 3600

Requires: NT8 running, connected, EngineRelay strategy ENABLED on an ES chart
(market socket 36001, broker socket 36002). Ctrl-C stops cleanly and prints the
blotter. Live ignition uses the CAUSAL config (trailing exit, no frozen states);
flow runs at 0.1x study size (maxp=5).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.brokers.ninjatrader import NinjaTraderBroker   # noqa: E402
from engine.adapters.feeds.ninjatrader import NinjaTraderFeed       # noqa: E402
from engine.adapters.feeds.recorder_tee import RecorderTee          # noqa: E402
from engine.adapters.painter import NTChartPainter                  # noqa: E402
from engine.adapters.questdb import AsyncQuestDB                     # noqa: E402
from engine.core.blotter import Blotter                             # noqa: E402
from engine.core.clock import WallClock                             # noqa: E402
from engine.core.config import load_instruments                     # noqa: E402
from engine.core.events import Bar, BookFlow, Trade                 # noqa: E402
from engine.core.live_engine import LiveEngine                      # noqa: E402
from engine.core.risk import RiskConfig, RiskSupervisor             # noqa: E402
from engine.painters import PaintController                         # noqa: E402

HMM_PATH = str(ROOT / "config" / "hmm_es_1h.json")
SYMBOL = "ES"
INSTRUMENTS = load_instruments(ROOT / "config" / "instruments.yaml")


def fetch_daily_bars(symbol: str = "ES=F", days: int = 320) -> list:
    """Daily bars for the 1d zone view (Yahoo continuous front ~ current ES,
    small basis). Returns [] on any failure so painting degrades gracefully."""
    import time

    import httpx

    from engine.core.events import Bar
    try:
        p1 = int(time.time()) - days * 86400
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?period1={p1}&period2={int(time.time())}&interval=1d")
        r = httpx.get(url, timeout=20, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        ts = res["timestamp"]
        q = res["indicators"]["quote"][0]
        out = []
        for i, t in enumerate(ts):
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
            if None in (o, h, l, c):
                continue
            out.append(Bar((t + 21 * 3600) * 1_000_000_000, "1d", o, h, l, c,
                           int(q["volume"][i] or 0)))
        return out
    except Exception as ex:                       # noqa: BLE001
        print(f"  (daily-zone fetch failed: {ex}; 1d zones disabled)")
        return []


class ObserveStrategy:
    """Counts events; places no orders. Used to validate the live pipe.
    symbol='' = observer role: sees EVERY lane's events (dispatch broadcast)."""
    symbol = ""

    def __init__(self) -> None:
        self.trades = self.flows = self.bars = 0
        self.last_px = None

    def on_trade(self, e):
        self.trades += 1
        self.last_px = e.price
        return []

    def on_bookflow(self, e):
        self.flows += 1
        return []

    def on_bar(self, e):
        self.bars += 1
        self.last_px = e.c
        return []

    def on_quote(self, e):
        return []

    def on_depth(self, e):
        return []

    def on_fill(self, e):
        print(f"  FILL {e}")

    def on_position(self, e):
        print(f"  POSITION {e.qty} @ {e.avg_px}")


def _make(label: str, flow_th: int = 30, symbol: str = SYMBOL,
          hmm_path: str = HMM_PATH):
    """One strategy instance by roster label (includes every variant)."""
    from engine.strategies.dip_buy import DipBuyStrategy
    from engine.strategies.flow import FlowFollowingStrategy
    from engine.strategies.ibs_swing import IBSSwingStrategy
    from engine.strategies.ignition import IgnitionStrategy
    from engine.strategies.open_drive import OpenDriveStrategy
    from engine.strategies.zones_strategy import ZoneLifecycleStrategy
    if label == "ignition":
        return IgnitionStrategy(symbol, hmm_path, exit_mode="trailing", regime_states=None)
    if label == "ignition_fixed":    # variant: fixed/regime exits instead of trailing
        return IgnitionStrategy(symbol, hmm_path, exit_mode="fixed", regime_states=None)
    if label == "opendrive":
        return OpenDriveStrategy(symbol)
    if label == "flow":              # adaptive z-score threshold (scale-invariant)
        return FlowFollowingStrategy(symbol, maxp=5, adaptive=True, adapt_k=4.0)
    if label == "flow_fixed":        # variant: fixed threshold (research default)
        return FlowFollowingStrategy(symbol, maxp=5, adaptive=False, th=flow_th)
    pu = INSTRUMENTS[symbol].point_usd if symbol in INSTRUMENTS else 50.0
    if label == "zones":
        return ZoneLifecycleStrategy(symbol, point_usd=pu)
    if label == "zones_gap":         # variant: leave-and-return gap zones on
        return ZoneLifecycleStrategy(symbol, gap_thr=5.0, point_usd=pu)
    if label == "dipbuy":
        return DipBuyStrategy(symbol, point_usd=pu)
    if label == "ibs":
        return IBSSwingStrategy(symbol)
    if label == "ibs_gex":           # variant: size up in long-gamma
        from engine.features.gamma import GammaRegime
        try:
            return IBSSwingStrategy(symbol, gamma=GammaRegime())
        except Exception:            # noqa: BLE001 - no GEX table -> base IBS
            return IBSSwingStrategy(symbol)
    raise SystemExit(f"unknown strategy '{label}'")


# every strategy + variant — the full paper roster (--paper all)
ALL_LABELS = ("ignition", "ignition_fixed", "opendrive", "flow", "flow_fixed",
              "zones", "zones_gap", "dipbuy", "ibs", "ibs_gex")


def build_roster(paper: str, flow_th: int = 30, symbol: str = SYMBOL,
                 hmm_path: str = HMM_PATH, prefix: str = ""):
    """Roster for one instrument lane. `prefix` namespaces labels in multi-
    instrument mode ('NQ:zones'); empty in the single-instrument default."""
    labels = list(ALL_LABELS) if paper.strip() in ("all", "") else \
        [x for x in paper.split(",") if x]
    roster = []
    for lb in labels:
        s = _make(lb, flow_th, symbol, hmm_path)
        s.label = prefix + lb        # for display / paper reporting
        roster.append((prefix + lb, s))
    return roster


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", default="all",
                    help="roster that ALWAYS paper-trades (visible signals): 'all' (every "
                         "strategy + variant) or a comma list of labels. Default 'all'.")
    ap.add_argument("--live", default="",
                    help="comma list of roster labels that ALSO route to the NT8 broker "
                         "(the few that trade live). Empty = pure paper/observation.")
    ap.add_argument("--strategies", default="",
                    help="DEPRECATED alias for --live (kept for old commands)")
    ap.add_argument("--seconds", type=float, default=0, help="stop after N seconds (0 = run until Ctrl-C)")
    ap.add_argument("--instruments", default="",
                    help="comma list of instrument lanes (e.g. ES,NQ) read from "
                         "--live-config; each lane = its own relay ports + roster. "
                         "Empty (default) = single-ES from the flat args below.")
    ap.add_argument("--live-config", default=str(ROOT / "config" / "live.yaml"),
                    help="per-instrument lane config (ports, paper/live rosters)")
    ap.add_argument("--market-port", type=int, default=36001)
    ap.add_argument("--broker-port", type=int, default=36002)
    ap.add_argument("--draw-port", type=int, default=36004,
                    help="EngineOverlay INDICATOR's draw socket (drawing lives in an "
                         "indicator, on the chart, so it never hides your orders)")
    ap.add_argument("--no-warmup-gate", action="store_true",
                    help="DANGER: let strategies trade on backfill bars (debug only)")
    ap.add_argument("--no-paint", action="store_true",
                    help="disable NT8 chart drawing (ghost signals, zones, status box)")
    ap.add_argument("--paper-paint", action="store_true",
                    help="also PAINT paper-sleeve fills (cyan). Default OFF: 10 paper "
                         "sleeves add many chart objects; they're always recorded to "
                         "claude_paper_fills regardless (review via tools/scorecard.py)")
    ap.add_argument("--panel", default="bottomleft",
                    choices=["bottomleft", "topright", "bottomright", "topleft"],
                    help="corner for the engine info panel (default bottomleft)")
    ap.add_argument("--record", action="store_true",
                    help="record 1m bars to QuestDB claude_bars_live (grow the library)")
    ap.add_argument("--flow-th", type=int, default=30,
                    help="flow threshold for THIS feed (validated=200 but the live feed's "
                         "|adelta| tops ~66; default 30 lets flow respond here)")
    ap.add_argument("--max-sleeve", type=int, default=10,
                    help="risk: cap on any sleeve's |position| (default 10)")
    ap.add_argument("--max-gross", type=int, default=15,
                    help="risk: cap on account gross exposure across sleeves (default 15)")
    ap.add_argument("--max-sleeve-usd", type=float, default=None,
                    help="risk: DOLLAR-notional cap per sleeve (contracts x px x $/pt); "
                         "comparable across instruments. Off by default.")
    ap.add_argument("--max-gross-usd", type=float, default=None,
                    help="risk: DOLLAR-notional cap on account gross. Off by default.")
    ap.add_argument("--risk-halt", type=float, default=-5000.0,
                    help="risk: daily marked-loss kill switch in USD (default -5000)")
    ap.add_argument("--no-risk", action="store_true",
                    help="DANGER: disable production risk limits (in-flight vetting stays on)")
    ap.add_argument("--gex-gate", action="store_true",
                    help="allocate by dealer-gamma regime: trend sleeves (ignition/opendrive/"
                         "flow) take entries only on short-gamma days (gexp_prev<=1/3); "
                         "zones/ibs always on. Validated on 2025 (gamma/GEX_FINDINGS.md D).")
    ap.add_argument("--gex-levels", action="store_true",
                    help="draw prior-session gamma strikes (put wall/call wall/flip) as S/R "
                         "lines on the chart (claude_gex_levels, CBOE true-OI).")
    ap.add_argument("--gex-basis", type=float, default=52.0,
                    help="SPX->ES basis added to gamma strikes (measured ~+52pt; recalibrate)")
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        datefmt="%H:%M:%S")
    # silence per-request HTTP logs (QuestDB recorder calls via httpx) — pure noise
    for noisy in ("httpx", "httpcore", "hpack", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # ── instrument lanes ──────────────────────────────────────────────────
    # Default (no --instruments): ONE ES lane from the flat CLI args — exactly
    # the pre-multi behavior. --instruments ES,NQ reads per-lane ports+rosters
    # from config/live.yaml; every lane gets its own relay (chart) + sockets.
    if a.instruments:
        import yaml as _yaml
        _cfg = _yaml.safe_load(Path(a.live_config).read_text(encoding="utf-8"))
        _all = _cfg.get("instruments") or {}
        lanes_cfg = {}
        for sym in [s.strip() for s in a.instruments.split(",") if s.strip()]:
            if sym not in _all:
                raise SystemExit(f"--instruments {sym}: no entry in {a.live_config}")
            lanes_cfg[sym] = _all[sym]
    else:
        lanes_cfg = {SYMBOL: {"market_port": a.market_port, "broker_port": a.broker_port,
                              "draw_port": a.draw_port, "paper": a.paper,
                              "live": (a.live or a.strategies)}}
    multi = bool(a.instruments)

    roster: list = []            # (label, strategy) across all lanes
    live_names: set = set()
    feeds: list = []
    brokers: dict = {}
    recorders: list = []
    lane_meta: dict = {}         # sym -> {draw_port, roster, lc}
    blot: Blotter | None = None
    for sym, lc in lanes_cfg.items():
        spec = INSTRUMENTS.get(sym)
        if spec is None:
            raise SystemExit(f"lane {sym}: no spec in config/instruments.yaml")
        prefix = f"{sym}:" if multi else ""
        hmm = str(ROOT / spec.hmm_path) if spec.hmm_path else HMM_PATH
        lane_roster = build_roster(str(lc.get("paper", "all")), flow_th=a.flow_th,
                                   symbol=sym, hmm_path=hmm, prefix=prefix)
        lane_live = {prefix + x for x in str(lc.get("live") or "").split(",") if x}
        unknown = lane_live - {lb for lb, _ in lane_roster}
        if unknown:
            raise SystemExit(f"--live names not in the {sym} roster: {sorted(unknown)}")
        # lane discipline: feed stamp == strategy symbols == broker key
        assert all(s.symbol == sym for _, s in lane_roster), f"symbol mismatch in {sym} lane"
        feed = NinjaTraderFeed("127.0.0.1", int(lc.get("market_port", 36001)), symbol=sym)
        if a.record:
            rec = RecorderTee(feed, AsyncQuestDB(), symbol=sym)
            recorders.append(rec)
            feed = rec
        feeds.append(feed)
        brokers[sym] = NinjaTraderBroker("127.0.0.1", int(lc.get("broker_port", 36002)),
                                         symbol=sym)
        if blot is None:
            blot = Blotter(sym, spec.point_usd, verbose=True)
        else:
            blot.add_instrument(sym, spec.point_usd)
        roster += lane_roster
        live_names |= lane_live
        lane_meta[sym] = {"draw_port": int(lc.get("draw_port", 36004)),
                          "roster": lane_roster, "lc": lc}

    paper_blot = None
    if a.record:
        from engine.adapters.paper_blotter import PaperBlotter
        paper_blot = PaperBlotter(AsyncQuestDB())
        await paper_blot.start()
        print("recording -> QuestDB: 1m bars (claude_bars_live) + per-second "
              "aggressor/book (claude_sec_live) + paper fills (claude_paper_fills)")
    obs = ObserveStrategy()
    strategies = [obs] + [s for _, s in roster]
    live_owners = {id(s) for lb, s in roster if lb in live_names}
    label_by_id = {id(s): lb for lb, s in roster}
    # production risk config (the 2026-07-09 burst fixes). IBS carries positions
    # overnight by design, so EOD flatten stays compatible only because IBS
    # isn't in the default live set; revisit eod_flatten_et before enabling it.
    point_usd = {sym: INSTRUMENTS[sym].point_usd for sym in lanes_cfg}
    if a.no_risk:
        risk = RiskSupervisor(RiskConfig(point_usd=point_usd))
        print("risk: PRODUCTION LIMITS OFF (--no-risk); in-flight vetting only")
    else:
        risk = RiskSupervisor(RiskConfig(
            point_usd=point_usd,
            max_pos_per_sleeve=a.max_sleeve, max_account_gross=a.max_gross,
            max_sleeve_notional_usd=a.max_sleeve_usd,
            max_gross_notional_usd=a.max_gross_usd,
            rate_max_orders=4, rate_window_s=5.0,
            daily_loss_halt=a.risk_halt,
            entry_lockout_et=(15, 45), eod_flatten_et=(15, 58),
            swing_sleeves=("IBSSwingStrategy",)))     # IBS enters 15:59 + holds overnight
        usd_caps = ""
        if a.max_sleeve_usd:
            usd_caps += f", sleeve ${a.max_sleeve_usd:,.0f}"
        if a.max_gross_usd:
            usd_caps += f", gross ${a.max_gross_usd:,.0f}"
        print(f"risk: sleeve cap {a.max_sleeve}, gross cap {a.max_gross}{usd_caps}, "
              f"4 orders/5s, halt at ${a.risk_halt:+,.0f}, "
              f"entry lockout 15:45 ET, EOD flatten 15:58 ET (IBS exempt: swing)")
    regime = None
    if a.gex_gate:
        from engine.core.regime import RegimeGate
        from engine.features.gamma import GammaRegime
        try:
            # SPX dealer-gamma is an ES signal: scope the gate to the ES lane
            # so NQ/GC sleeves are never gated by it.
            regime = RegimeGate(GammaRegime(), symbols={"ES"} if multi else None)
            print("GEX allocation gate ON: trend sleeves only in short-gamma "
                  "(gexp_prev<=1/3); zones/ibs always on"
                  + (" [ES lane only]" if multi else ""))
        except Exception as ex:                      # noqa: BLE001
            print(f"  (GEX gate disabled: {ex})")
    eng = LiveEngine(feeds, brokers, strategies, WallClock(), blot,
                     warmup_gate=not a.no_warmup_gate, risk=risk, regime=regime,
                     live_owners=live_owners)
    live_lbls = sorted(lb for lb, s in roster if id(s) in live_owners)
    paper_lbls = sorted(lb for lb, s in roster if id(s) not in live_owners)
    print(f"roster ({len(roster)}): LIVE->NT8 {live_lbls or '(none)'}  |  "
          f"PAPER {paper_lbls}")

    # ── chart painting: ONE painter + PaintController PER LANE (each lane's
    # EngineOverlay indicator has its own draw socket). Engine hooks fan out,
    # routed by the event/fill symbol so a lane only paints its own chart.
    painters: list[NTChartPainter] = []
    pcs: dict[str, PaintController] = {}          # symbol -> lane controller
    pc: PaintController | None = None             # primary lane (back-compat refs)
    if not a.no_paint:
        for sym, meta in lane_meta.items():
            ptr = NTChartPainter("127.0.0.1", meta["draw_port"])
            if not await ptr.connect():
                print(f"  ({sym}: no draw socket on {meta['draw_port']}; painting off)")
                continue
            painters.append(ptr)
            lane_strats = [s for _, s in meta["roster"]]
            lane_pc = PaintController(ptr, lane_strats, panel_pos=a.panel)
            pcs[sym] = lane_pc
            if pc is None:
                pc = lane_pc
            yahoo = INSTRUMENTS[sym].extra.get("yahoo",
                                               "ES=F" if sym == "ES" else None)
            if yahoo:
                daily = fetch_daily_bars(yahoo)
                if daily:
                    lane_pc.zv.seed_daily(daily)
                    print(f"seeded {len(daily)} daily bars for {sym} 1d zones")
            print(f"chart painting ON [{sym}] (zones, S/R, gamma, signals, panel)")
            # gamma S/R levels per lane: ES <- SPX chain, NQ <- NDX chain
            # (instruments.yaml gex: {underlying, basis}); --gex-basis still
            # overrides the ES basis for back-compat.
            gexc = INSTRUMENTS[sym].extra.get("gex") if a.gex_levels else None
            if gexc:
                from datetime import datetime, timezone

                from engine.features.gamma_levels import GammaLevels
                try:
                    und = gexc.get("underlying", "SPX")
                    basis = a.gex_basis if sym == "ES" else float(gexc.get("basis", 0.0))
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    lv = GammaLevels(underlying=und).levels_prev(today, basis=basis)
                    if lv:
                        lane_pc.set_gamma_levels(lv)
                        print(f"gamma S/R levels ON [{sym}<-{und}] ({lv['sess']}, "
                              f"+{basis:.0f} basis): "
                              f"putW {lv['put_wall']:.0f}  callW {lv['call_wall']:.0f}  "
                              f"flip {lv['flip'] and round(lv['flip'])}  "
                              f"{'long' if lv['net_sign']>0 else 'SHORT'}-gamma")
                        if basis == 0.0 and sym != "ES":
                            print(f"  ({sym}: basis UNCALIBRATED — eyeball wall vs price, "
                                  f"then set gex.basis = {sym}_close - {und}_close "
                                  f"in config/instruments.yaml)")
                    else:
                        print(f"  ({sym}: no prior {und} gamma-levels row; "
                              f"run tools/fetch_cboe_gex.py)")
                except Exception as ex:                  # noqa: BLE001
                    print(f"  ({sym} gamma levels disabled: {ex})")
        if pcs:
            def _pc_of(sym: str) -> PaintController | None:
                return pcs.get(sym) or (pc if len(pcs) == 1 else None)

            async def _live_fill(f):
                p = _pc_of(f.symbol)
                if p is not None:
                    await p.live_fill(f)

            async def _bar_hook(bar, live, bf):
                p = _pc_of(getattr(bar, "symbol", ""))
                if p is not None:
                    await p.on_bar(bar, live, bf)

            async def _ghost(ts, side, qty, tag, px, symbol=""):
                p = _pc_of(symbol)
                if p is not None:
                    await p.ghost_one(ts, side, qty, tag, px)
            eng.on_live_fill = _live_fill        # mark actual fills, not decisions
            eng.on_bar_hook = _bar_hook
            eng.on_warmup_signal = _ghost        # paint ghosts as backfill replays

    # paper fills: paint (muted cyan, if painting) AND persist to claude_paper_fills
    # (if recording), tagged with the owning sleeve label.
    async def _paper_sink(f):
        p = pcs.get(f.symbol) or pc
        if p is not None and a.paper_paint:      # painting paper fills is opt-in (chart load)
            await p.paper_fill(f)
        if paper_blot is not None:
            await paper_blot.record(f, label_by_id.get(id(eng._owner.get(f.order_id)), "?"))
    eng.on_paper_fill = _paper_sink

    # DayScore morning read (co-pilot: fade-friendliness lean + VWAP posture).
    # A moderate-tilt SIZING input, not a switch; Crabel prior-range is the most
    # robust live component (volume/overnight are noisy on the delayed feed).
    try:
        from engine.adapters.questdb import QuestDB as _SyncQDB
        from engine.features.day_score import morning_read
        # short timeout: a busy QuestDB (e.g. a replay running) must never
        # stall session startup — the day read is a nice-to-have.
        _b = _SyncQDB(timeout=10).df("SELECT ts,o,h,l,c,vol FROM claude_bars_live "
                                     "WHERE ts > dateadd('d',-40,now()) ORDER BY ts")
        _r = morning_read(_b) if len(_b) else None
        if _r:
            _c = "  ".join(f"{k} {v:.0%}" for k, v in _r["components"].items())
            _tag = "pre-market lean" if _r.get("premarket") else "last session"
            print(f"DAY READ [{_tag}] {_r['day']}: fade-friendliness {_r['score']:.0f}/100 "
                  f"[{_r['label']}]  |  posture: {_r['posture']}  |  {_c}")
            print("  (moderate-tilt sizing input, not a switch; Crabel is the robust "
                  "live component — volume/overnight are noisy on the delayed feed)")
    except Exception as ex:                          # noqa: BLE001 - never block startup
        print(f"  (day read unavailable: {ex})")

    mode = "PAPER-ONLY" if not live_owners else "LIVE(" + ",".join(live_lbls) + ")+PAPER"
    ports = "  ".join(f"{sym} m:{m['lc'].get('market_port', 36001)}"
                      f"/b:{m['lc'].get('broker_port', 36002)}"
                      for sym, m in lane_meta.items())
    print(f"live session [{mode}] {ports} "
          f"{'for %.0fs' % a.seconds if a.seconds else 'until Ctrl-C'}  "
          f"(warmup gate {'OFF' if a.no_warmup_gate else 'ON'})")

    async def stopper():
        await asyncio.sleep(a.seconds)
        print("(time cap reached, stopping)")
        eng.stop()

    async def heartbeat():
        last = -1
        while True:
            await asyncio.sleep(5)
            state = "LIVE" if eng._live else f"WARMUP({eng._backfill_bars} bars)"
            ghosts = sum(p._n_ghost for p in pcs.values())
            if obs.trades != last or not eng._live:
                print(f"  [{state}] trades={obs.trades} bars={obs.bars} "
                      f"last={obs.last_px} ghosts_painted={ghosts} "
                      f"live_orders={blot.n_orders} fills={blot.n_fills}")
                last = obs.trades

    tasks = [asyncio.create_task(eng.run()), asyncio.create_task(heartbeat())]
    if a.seconds:
        tasks.append(asyncio.create_task(stopper()))
    try:
        await tasks[0]
    except KeyboardInterrupt:
        eng.stop()
        await asyncio.sleep(1)
    finally:
        for t in tasks[1:]:
            t.cancel()
        for ptr in painters:
            await ptr.close()

    for rec in recorders:
        print(f"recorded [{rec.symbol}] {rec.n_recorded} 1m bars -> claude_bars_live, "
              f"{rec.n_sec} per-second rows -> claude_sec_live"
              + (f", {paper_blot.n} paper fills -> claude_paper_fills" if paper_blot else ""))
    print(f"\nsession summary: {obs.trades} trades, {obs.flows} bookflow-seconds, "
          f"{obs.bars} 1m bars ({eng._backfill_bars} backfill), last px {obs.last_px}")
    print(f"warmup orders suppressed: {eng._suppressed_orders}  |  live orders {blot.n_orders}, fills {blot.n_fills}")
    if blot.trades:
        print(blot.summary(flat_cost_pts=0.0))
    # per-sleeve PAPER activity (avg-cost realized pts, net position)
    if eng.paper_fills:
        book: dict[str, list] = {}
        for f in eng.paper_fills:
            lb = label_by_id.get(id(eng._owner.get(f.order_id)), "?")
            book.setdefault(lb, []).append((f.size, f.price))
        print(f"\npaper sleeves ({len(eng.paper_fills)} fills):")
        for lb in sorted(book):
            pos = 0; avg = 0.0; real = 0.0; n = len(book[lb])
            for q, px in book[lb]:
                if pos == 0 or (q > 0) == (pos > 0):
                    avg = (avg * abs(pos) + px * abs(q)) / (abs(pos) + abs(q)) if pos + q else px
                    pos += q
                else:
                    c = min(abs(q), abs(pos)); real += (px - avg) * (1 if pos > 0 else -1) * c
                    pos += (1 if q > 0 else -1) * c
            print(f"  {lb:16s} fills={n:3d}  realized {real:+7.1f}pt  net pos {pos:+d}")


if __name__ == "__main__":
    asyncio.run(main())
