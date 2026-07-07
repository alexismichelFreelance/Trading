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


def build(names: list[str]):
    out = []
    for n in names:
        if n == "ignition":
            from engine.strategies.ignition import IgnitionStrategy
            out.append(IgnitionStrategy(SYMBOL, HMM_PATH, exit_mode="trailing",
                                        regime_states=None))
        elif n == "opendrive":
            from engine.strategies.open_drive import OpenDriveStrategy
            out.append(OpenDriveStrategy(SYMBOL))
        elif n == "flow":
            from engine.strategies.flow import FlowFollowingStrategy
            out.append(FlowFollowingStrategy(SYMBOL, maxp=5))
        elif n == "zones":
            from engine.strategies.zones_strategy import ZoneLifecycleStrategy
            out.append(ZoneLifecycleStrategy(SYMBOL))
        elif n == "ibs":
            from engine.strategies.ibs_swing import IBSSwingStrategy
            out.append(IBSSwingStrategy(SYMBOL))
        else:
            raise SystemExit(f"unknown strategy '{n}'")
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategies", default="",
                    help="comma list: ignition,opendrive,flow,zones,ibs (empty = observe only)")
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
    a = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s",
                        datefmt="%H:%M:%S")

    names = [s for s in a.strategies.split(",") if s]
    obs = ObserveStrategy()
    strategies = [obs] + build(names)
    feed = NinjaTraderFeed("127.0.0.1", a.market_port, symbol=SYMBOL)
    recorder = None
    if a.record:
        recorder = RecorderTee(feed, AsyncQuestDB(), symbol=SYMBOL)
        feed = recorder
        print("recording 1m bars -> QuestDB claude_bars_live")
    broker = NinjaTraderBroker("127.0.0.1", a.broker_port, symbol=SYMBOL)
    blot = Blotter(SYMBOL, 50.0, verbose=True)
    eng = LiveEngine(feed, broker, strategies, WallClock(), blot,
                     warmup_gate=not a.no_warmup_gate)

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
            eng.on_bar_hook = pc.on_bar
            eng.on_warmup_signal = pc.ghost_one      # paint ghosts as backfill replays
            print("chart painting ON (30m/1h/4h/1d zones, S/R bracket, signals, panel)")

    mode = "OBSERVE" if not names else "TRADE(" + ",".join(names) + ")"
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
        print(f"recorded {recorder.n_recorded} 1m bars -> claude_bars_live")
    print(f"\nsession summary: {obs.trades} trades, {obs.flows} bookflow-seconds, "
          f"{obs.bars} 1m bars ({eng._backfill_bars} backfill), last px {obs.last_px}")
    print(f"warmup orders suppressed: {eng._suppressed_orders}  |  live orders {blot.n_orders}, fills {blot.n_fills}")
    if blot.trades:
        print(blot.summary(flat_cost_pts=0.0))


if __name__ == "__main__":
    asyncio.run(main())
