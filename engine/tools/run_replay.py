"""Run a strategy over QuestDB replay and print the per-month P&L table.

    .venv/Scripts/python.exe tools/run_replay.py --strategy ignition --all-months
    .venv/Scripts/python.exe tools/run_replay.py --strategy ignition --symbol ESM5 --days 2025-04-01 2025-04-02
    # co-simulated portfolio (one engine, shared clock, per-symbol books):
    .venv/Scripts/python.exe tools/run_replay.py --strategy zones --portfolio ESM5,ESH5

Instrument specs (point_usd, replay tables, HMM path) come from
config/instruments.yaml via the registry — contracts resolve to their root
(ESM5 -> ES) with root_symbol().
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.brokers.router import SymbolRouterBroker          # noqa: E402
from engine.adapters.brokers.sim import CleanFill, SimBroker          # noqa: E402
from engine.adapters.feeds.merge import MergeFeed                      # noqa: E402
from engine.adapters.feeds.replay_questdb import ReplayFeed            # noqa: E402
from engine.adapters.questdb import AsyncQuestDB                       # noqa: E402
from engine.core.blotter import Blotter                               # noqa: E402
from engine.core.clock import EventClock                              # noqa: E402
from engine.core.config import load_instruments, root_symbol          # noqa: E402
from engine.core.engine import ReplayEngine                           # noqa: E402

INSTRUMENTS = load_instruments(ROOT / "config" / "instruments.yaml")
STATES_PATH = ROOT / "config" / "hmm_1h_states.json"


def spec_of(contract: str):
    root = root_symbol(contract, INSTRUMENTS)
    spec = INSTRUMENTS.get(root)
    if spec is None:
        raise SystemExit(f"no instrument spec for {contract!r} (root {root!r}) "
                         f"in config/instruments.yaml")
    return spec


def sec_table(contract: str) -> str:
    tables = spec_of(contract).extra.get("replay", {}).get("sec_tables", {})
    t = tables.get(contract)
    if t is None:
        raise SystemExit(f"no replay sec table for {contract!r} "
                         f"(have {sorted(tables)}) — add it to instruments.yaml")
    return t


def hmm_path_of(contract: str) -> str:
    p = spec_of(contract).hmm_path
    return str(ROOT / p) if p else str(ROOT / "config" / "hmm_es_1h.json")


HMM_PATH = hmm_path_of("ESM5")                 # legacy default (ES)
POINT_USD = INSTRUMENTS["ES"].point_usd        # legacy alias


def load_states():
    import json
    return json.loads(STATES_PATH.read_text()) if STATES_PATH.exists() else None


NEEDS_BARS = {"ignition": True, "zones": True, "flow": False, "opendrive": False,
              "ibs": True}


def make_strategy(name: str, symbol: str, hmm_path: str = HMM_PATH, **kw):
    spec = spec_of(symbol)
    if name == "ignition":
        from engine.strategies.ignition import IgnitionStrategy
        kw.setdefault("regime_states", load_states())
        if spec.round_step is not None:
            kw.setdefault("round_step", spec.round_step)
        return IgnitionStrategy(symbol, hmm_path, **kw)
    if name == "flow":
        from engine.strategies.flow import FlowFollowingStrategy
        return FlowFollowingStrategy(symbol, **kw)
    if name == "zones":
        from engine.strategies.zones_strategy import ZoneLifecycleStrategy
        kw.setdefault("point_usd", spec.point_usd)
        return ZoneLifecycleStrategy(symbol, **kw)
    if name == "opendrive":
        from engine.strategies.open_drive import OpenDriveStrategy
        return OpenDriveStrategy(symbol, **kw)
    if name == "ibs":
        from engine.strategies.ibs_swing import IBSSwingStrategy
        return IBSSwingStrategy(symbol, **kw)
    raise SystemExit(f"unknown strategy '{name}' (ignition/flow/zones/opendrive/ibs)")


def _make_feed(name: str, symbol: str, days, start, end) -> ReplayFeed:
    bars_symbol = symbol if NEEDS_BARS.get(name, True) else None
    return ReplayFeed(AsyncQuestDB(), table=sec_table(symbol), bars_symbol=bars_symbol,
                      days=days, start=start, end=end, symbol=symbol)


async def run_one(name: str, symbol: str, days=None, start=None, end=None,
                  hmm_path: str | None = None, **kw) -> Blotter:
    feed = _make_feed(name, symbol, days, start, end)
    clk = EventClock()
    broker = SimBroker(symbol, clk, CleanFill())
    strat = make_strategy(name, symbol, hmm_path or hmm_path_of(symbol), **kw)
    blot = Blotter(symbol, spec_of(symbol).point_usd)
    await ReplayEngine(feed, broker, [strat], clk, blot).run()
    return blot


async def run_portfolio(name: str, symbols: list[str], days=None, start=None,
                        end=None, hmm_path: str | None = None, **kw) -> Blotter:
    """Co-simulated: one engine/clock, MergeFeed lanes, per-symbol SimBrokers,
    one portfolio Blotter. Per-symbol results equal independent runs
    (composition invariance, pinned by tests/test_multi_sim.py)."""
    clk = EventClock()
    feeds, brokers, strats = [], {}, []
    blot: Blotter | None = None
    for sym in symbols:
        feeds.append(_make_feed(name, sym, days, start, end))
        brokers[sym] = SimBroker(sym, clk, CleanFill())
        strats.append(make_strategy(name, sym, hmm_path or hmm_path_of(sym), **kw))
        if blot is None:
            blot = Blotter(sym, spec_of(sym).point_usd)
        else:
            blot.add_instrument(sym, spec_of(sym).point_usd)
    await ReplayEngine(MergeFeed(feeds), SymbolRouterBroker(brokers),
                       strats, clk, blot).run()
    return blot


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", default="ignition")
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--portfolio", default=None,
                    help="comma-separated contracts co-simulated in ONE engine "
                         "(e.g. ESM5,ESH5); one strategy instance per contract")
    ap.add_argument("--all-months", action="store_true")
    ap.add_argument("--days", nargs="*")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--cost", type=float, default=0.5175)
    ap.add_argument("--hmm-path", default=None,
                    help="override; default = the instrument's hmm_path from the registry")
    ap.add_argument("--round-step", type=float)
    ap.add_argument("--horizon", type=float)
    ap.add_argument("--chop-stop", type=float)
    ap.add_argument("--trend-cap", type=float)
    ap.add_argument("--book-net", type=float)
    ap.add_argument("--gap-thr", type=float)
    ap.add_argument("--th", type=int)
    ap.add_argument("--scale", type=float)
    ap.add_argument("--adaptive", action="store_true")
    ap.add_argument("--adapt-k", type=float)
    a = ap.parse_args()
    symbols = ["ESM5", "ESH5"] if a.all_months else [a.symbol or "ESM5"]
    if a.portfolio and a.all_months:
        raise SystemExit("--portfolio and --all-months are mutually exclusive")

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
    if a.th is not None:
        kw["th"] = a.th
    if a.scale is not None:
        kw["scale"] = a.scale
    if a.adaptive:
        kw["adaptive"] = True
    if a.adapt_k is not None:
        kw["adapt_k"] = a.adapt_k

    if a.portfolio:
        syms = [s.strip() for s in a.portfolio.split(",") if s.strip()]
        t0 = time.time()
        blot = asyncio.run(run_portfolio(a.strategy, syms, days=a.days,
                                         start=a.start, end=a.end,
                                         hmm_path=a.hmm_path, **kw))
        print(f"\n=== {a.strategy} PORTFOLIO {'+'.join(syms)} ===  "
              f"({time.time()-t0:.1f}s, {len(blot.trades)} trades)")
        print(blot.summary(flat_cost_pts=a.cost))
        return

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
