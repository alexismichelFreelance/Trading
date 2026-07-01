"""Parity fixtures: run each strategy once over the full QuestDB replay and
share the resulting blotters across the parity assertions (session-scoped)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from engine.adapters.brokers.sim import CleanFill, SimBroker
from engine.adapters.feeds.replay_questdb import ReplayFeed
from engine.adapters.questdb import AsyncQuestDB, QuestDB
from engine.core.blotter import Blotter
from engine.core.clock import EventClock
from engine.core.engine import ReplayEngine

ROOT = Path(__file__).resolve().parents[2]
HMM_PATH = str(ROOT / "config" / "hmm_es_1h.json")
SEC_TABLE = {"ESM5": "claude_sec_feat", "ESH5": "claude_sec_feat_esh5"}
POINT_USD = 50.0


def db_up() -> bool:
    try:
        QuestDB(timeout=5).query("SELECT 1")
        return True
    except Exception:
        return False


async def _run(symbol: str, strat_factory) -> Blotter:
    feed = ReplayFeed(AsyncQuestDB(), table=SEC_TABLE[symbol], bars_symbol=symbol)
    clk = EventClock()
    broker = SimBroker(symbol, clk, CleanFill())
    blot = Blotter(symbol, POINT_USD)
    await ReplayEngine(feed, broker, [strat_factory(symbol)], clk, blot).run()
    return blot


def run_both(strat_factory) -> dict[str, Blotter]:
    return {s: asyncio.run(_run(s, strat_factory)) for s in ("ESM5", "ESH5")}


def combine_months(results: dict[str, Blotter], cost_pts: float) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for blot in results.values():
        for m, r in blot.by_month(cost_pts).items():
            agg = out.setdefault(m, {"n": 0, "pts": 0.0, "usd": 0.0})
            agg["n"] += r["n"]
            agg["pts"] += r["pts"]
            agg["usd"] += r["usd"]
    return dict(sorted(out.items()))


@pytest.fixture(scope="session")
def ignition_results():
    from engine.strategies.ignition import IgnitionStrategy
    return run_both(lambda sym: IgnitionStrategy(sym, HMM_PATH))
