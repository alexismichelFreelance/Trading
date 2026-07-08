"""Run a strategy over QuestDB replay and print the per-month P&L table.

    .venv/Scripts/python.exe tools/run_replay.py --strategy ignition --all-months
    .venv/Scripts/python.exe tools/run_replay.py --strategy ignition --symbol ESM5 --days 2025-04-01 2025-04-02
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.brokers.sim import CleanFill, SimBroker          # noqa: E402
from engine.adapters.feeds.replay_questdb import ReplayFeed            # noqa: E402
from engine.adapters.questdb import AsyncQuestDB                       # noqa: E402
from engine.core.blotter import Blotter                               # noqa: E402
from engine.core.clock import EventClock                              # noqa: E402
from engine.core.engine import ReplayEngine                           # noqa: E402

SEC_TABLE = {"ESM5": "claude_sec_feat", "ESH5": "claude_sec_feat_esh5"}
HMM_PATH = str(ROOT / "config" / "hmm_es_1h.json")
STATES_PATH = ROOT / "config" / "hmm_1h_states.json"
POINT_USD = 50.0


def load_states():
    import json
    return json.loads(STATES_PATH.read_text()) if STATES_PATH.exists() else None


NEEDS_BARS = {"ignition": True, "zones": True, "flow": False, "opendrive": False,
              "ibs": True}


def make_strategy(name: str, symbol: str, hmm_path: str = HMM_PATH, **kw):
    if name == "ignition":
        from engine.strategies.ignition import IgnitionStrategy
        kw.setdefault("regime_states", load_states())
        return IgnitionStrategy(symbol, hmm_path, **kw)
    if name == "flow":
        from engine.strategies.flow import FlowFollowingStrategy
        return FlowFollowingStrategy(symbol)
    if name == "zones":
        from engine.strategies.zones_strategy import ZoneLifecycleStrategy
        return ZoneLifecycleStrategy(symbol, **kw)
    if name == "opendrive":
        from engine.strategies.open_drive import OpenDriveStrategy
        return OpenDriveStrategy(symbol, **kw)
    if name == "ibs":
        from engine.strategies.ibs_swing import IBSSwingStrategy
        return IBSSwingStrategy(symbol, **kw)
    raise SystemExit(f"unknown strategy '{name}' (ignition/flow/zones/opendrive/ibs)")


async def run_one(name: str, symbol: str, days=None, start=None, end=None,
                  hmm_path: str = HMM_PATH, **kw) -> Blotter:
    bars_symbol = symbol if NEEDS_BARS.get(name, True) else None
    feed = ReplayFeed(AsyncQuestDB(), table=SEC_TABLE[symbol], bars_symbol=bars_symbol,
                      days=days, start=start, end=end)
    clk = EventClock()
    broker = SimBroker(symbol, clk, CleanFill())
    strat = make_strategy(name, symbol, hmm_path, **kw)
    blot = Blotter(symbol, POINT_USD)
    await ReplayEngine(feed, broker, [strat], clk, blot).run()
    return blot


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="ignition")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--all-months", action="store_true")
    ap.add_argument("--days", nargs="*")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--cost", type=float, default=0.5175)
    ap.add_argument("--hmm-path", default=HMM_PATH)
    ap.add_argument("--round-step", type=float)
    ap.add_argument("--horizon", type=float)
    ap.add_argument("--chop-stop", type=float)
    ap.add_argument("--trend-cap", type=float)
    ap.add_argument("--book-net", type=float)
    ap.add_argument("--gap-thr", type=float)
    a = ap.parse_args()
    symbols = ["ESM5", "ESH5"] if a.all_months else [a.symbol or "ESM5"]

    kw = {}
    if a.round_step is not None:
        kw["round_step"] = a.round_step
    if a.horizon is not None:
        kw["horizon_s"] = a.horizon
    if a.chop_stop is not None:
        kw["chop_stop"] = a.chop_stop
    if a.trend_cap is not None:
        kw["trend_cap"] = a.trend_cap
    if a.book_net is not None:
        kw["book_net"] = a.book_net
    if a.gap_thr is not None:
        kw["gap_thr"] = a.gap_thr

    total = 0.0
    for s in symbols:
        t0 = time.time()
        blot = asyncio.run(run_one(a.strategy, s, days=a.days, start=a.start, end=a.end,
                                   hmm_path=a.hmm_path, **kw))
        print(f"\n=== {a.strategy} {s} ===  ({time.time()-t0:.1f}s, {len(blot.trades)} trades)")
        print(blot.summary(flat_cost_pts=a.cost))
        total += blot.net_points(flat_cost_pts=a.cost)
    if len(symbols) > 1:
        print(f"\nTOTAL net points (all symbols): {total:+.1f}")


if __name__ == "__main__":
    main()
