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
from engine.core.events import Bar, BookFlow, Trade                 # noqa: E402
from engine.core.live_engine import LiveEngine                      # noqa: E402
from engine.core.risk import RiskConfig, RiskSupervisor             # noqa: E402
from engine.painters import PaintController                         # noqa: E402

HMM_PATH = str(ROOT / "config" / "hmm_es_1h.json")
SYMBOL = "ES"


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
    """Counts events; places no orders. Used to validate the live pipe."""
    symbol = SYMBOL

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


def _make(label: str, flow_th: int = 30):
    """One strategy instance by roster label (includes every variant)."""
    from engine.strategies.dip_buy import DipBuyStrategy
    from engine.strategies.flow import FlowFollowingStrategy
    from engine.strategies.ibs_swing import IBSSwingStrategy
    from engine.strategies.ignition import IgnitionStrategy
    from engine.strategies.open_drive import OpenDriveStrategy
    from engine.strategies.zones_strategy import ZoneLifecycleStrategy
    if label == "ignition":
        return IgnitionStrategy(SYMBOL, HMM_PATH, exit_mode="trailing", regime_states=None)
    if label == "ignition_fixed":    # variant: fixed/regime exits instead of trailing
        return IgnitionStrategy(SYMBOL, HMM_PATH, exit_mode="fixed", regime_states=None)
    if label == "opendrive":
        return OpenDriveStrategy(SYMBOL)
    if label == "flow":              # adaptive z-score threshold (scale-invariant)
        return FlowFollowingStrategy(SYMBOL, maxp=5, adaptive=True, adapt_k=4.0)
    if label == "flow_fixed":        # variant: fixed threshold (research default)
        return FlowFollowingStrategy(SYMBOL, maxp=5, adaptive=False, th=flow_th)
    if label == "zones":
        return ZoneLifecycleStrategy(SYMBOL)
    if label == "zones_gap":         # variant: leave-and-return gap zones on
        return ZoneLifecycleStrategy(SYMBOL, gap_thr=5.0)
    if label == "dipbuy":
        return DipBuyStrategy(SYMBOL)
    if label == "ibs":
        return IBSSwingStrategy(SYMBOL)
    if label == "ibs_gex":           # variant: size up in long-gamma
        from engine.features.gamma import GammaRegime
        try:
            return IBSSwingStrategy(SYMBOL, gamma=GammaRegime())
        except Exception:            # noqa: BLE001 - no GEX table -> base IBS
            return IBSSwingStrategy(SYMBOL)
    raise SystemExit(f"unknown strategy '{label}'")


# every strategy + variant — the full paper roster (--paper all)
ALL_LABELS = ("ignition", "ignition_fixed", "opendrive", "flow", "flow_fixed",
              "zones", "zones_gap", "dipbuy", "ibs", "ibs_gex")


def build_roster(paper: str, flow_th: int = 30):
    labels = list(ALL_LABELS) if paper.strip() in ("all", "") else \
        [x for x in paper.split(",") if x]
    roster = []
    for lb in labels:
        s = _make(lb, flow_th)
        s.label = lb                 # for display / paper reporting
        roster.append((lb, s))
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
    ap.add_argument("--market-port", type=int, default=36001)
    ap.add_argument("--broker-port", type=int, default=36002)
    ap.add_argument("--no-warmup-gate", action="store_true",
                    help="DANGER: let strategies trade on backfill bars (debug only)")
    ap.add_argument("--no-paint", action="store_true",
                    help="disable NT8 chart drawing (ghost signals, zones, status box)")
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

    # full paper roster; a subset (--live, or the deprecated --strategies) also
    # routes to the NT8 broker. Everything else paper-trades and is visible.
    roster = build_roster(a.paper, flow_th=a.flow_th)
    live_names = set(x for x in (a.live or a.strategies).split(",") if x)
    unknown = live_names - {lb for lb, _ in roster}
    if unknown:
        raise SystemExit(f"--live names not in the paper roster: {sorted(unknown)}")
    obs = ObserveStrategy()
    strategies = [obs] + [s for _, s in roster]
    live_owners = {id(s) for lb, s in roster if lb in live_names}
    label_by_id = {id(s): lb for lb, s in roster}
    feed = NinjaTraderFeed("127.0.0.1", a.market_port, symbol=SYMBOL)
    recorder = None
    if a.record:
        recorder = RecorderTee(feed, AsyncQuestDB(), symbol=SYMBOL)
        feed = recorder
        print("recording -> QuestDB: 1m bars (claude_bars_live) + per-second "
              "aggressor/book (claude_sec_live, so ignition/flow are replayable)")
    broker = NinjaTraderBroker("127.0.0.1", a.broker_port, symbol=SYMBOL)
    blot = Blotter(SYMBOL, 50.0, verbose=True)
    # production risk config (the 2026-07-09 burst fixes). IBS carries positions
    # overnight by design, so EOD flatten stays compatible only because IBS
    # isn't in the default live set; revisit eod_flatten_et before enabling it.
    if a.no_risk:
        risk = RiskSupervisor(RiskConfig())
        print("risk: PRODUCTION LIMITS OFF (--no-risk); in-flight vetting only")
    else:
        risk = RiskSupervisor(RiskConfig(
            max_pos_per_sleeve=a.max_sleeve, max_account_gross=a.max_gross,
            rate_max_orders=4, rate_window_s=5.0,
            daily_loss_halt=a.risk_halt,
            entry_lockout_et=(15, 45), eod_flatten_et=(15, 58),
            swing_sleeves=("IBSSwingStrategy",)))     # IBS enters 15:59 + holds overnight
        print(f"risk: sleeve cap {a.max_sleeve}, gross cap {a.max_gross}, "
              f"4 orders/5s, halt at ${a.risk_halt:+,.0f}, "
              f"entry lockout 15:45 ET, EOD flatten 15:58 ET (IBS exempt: swing)")
    regime = None
    if a.gex_gate:
        from engine.core.regime import RegimeGate
        from engine.features.gamma import GammaRegime
        try:
            regime = RegimeGate(GammaRegime())
            print("GEX allocation gate ON: trend sleeves only in short-gamma "
                  "(gexp_prev<=1/3); zones/ibs always on")
        except Exception as ex:                      # noqa: BLE001
            print(f"  (GEX gate disabled: {ex})")
    eng = LiveEngine(feed, broker, strategies, WallClock(), blot,
                     warmup_gate=not a.no_warmup_gate, risk=risk, regime=regime,
                     live_owners=live_owners)
    live_lbls = sorted(lb for lb, s in roster if id(s) in live_owners)
    paper_lbls = sorted(lb for lb, s in roster if id(s) not in live_owners)
    print(f"roster ({len(roster)}): LIVE->NT8 {live_lbls or '(none)'}  |  "
          f"PAPER {paper_lbls}")

    painter = NTChartPainter("127.0.0.1", a.broker_port)
    pc: PaintController | None = None
    if not a.no_paint:
        if await painter.connect():
            pc = PaintController(painter, strategies, panel_pos=a.panel)
            daily = fetch_daily_bars()
            if daily:
                pc.zv.seed_daily(daily)
                print(f"seeded {len(daily)} daily bars for 1d zones")
            eng.on_live_fill = pc.live_fill          # mark actual fills, not decisions
            eng.on_paper_fill = pc.paper_fill        # paper sleeves' signals (muted cyan)
            eng.on_bar_hook = pc.on_bar
            eng.on_warmup_signal = pc.ghost_one      # paint ghosts as backfill replays
            print("chart painting ON (zones, S/R, gamma, live + paper signals, panel)")
            if a.gex_levels:
                from datetime import datetime, timezone

                from engine.features.gamma_levels import GammaLevels
                try:
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    lv = GammaLevels().levels_prev(today, basis=a.gex_basis)
                    if lv:
                        pc.set_gamma_levels(lv)
                        print(f"gamma S/R levels ON ({lv['sess']}, +{a.gex_basis:.0f} ES basis): "
                              f"putW {lv['put_wall']:.0f}  callW {lv['call_wall']:.0f}  "
                              f"flip {lv['flip'] and round(lv['flip'])}  "
                              f"{'long' if lv['net_sign']>0 else 'SHORT'}-gamma")
                    else:
                        print("  (no prior gamma-levels row; run tools/fetch_cboe_gex.py)")
                except Exception as ex:                  # noqa: BLE001
                    print(f"  (gamma levels disabled: {ex})")

    mode = "PAPER-ONLY" if not live_owners else "LIVE(" + ",".join(live_lbls) + ")+PAPER"
    print(f"live session [{mode}] market:{a.market_port} broker:{a.broker_port} "
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
            ghosts = pc._n_ghost if pc is not None else 0
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
        await painter.close()

    if recorder is not None:
        print(f"recorded {recorder.n_recorded} 1m bars -> claude_bars_live, "
              f"{recorder.n_sec} per-second rows -> claude_sec_live")
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
