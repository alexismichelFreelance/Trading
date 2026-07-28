"""Restoring open paper positions across an engine restart (the IBS-overnight
orphan bug): strategy restore_state hooks, LiveEngine restore-at-flip, and the
runner's avg-cost reconstruction from claude_paper_fills."""
import asyncio
import sys
from pathlib import Path

import pandas as pd

from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.events import Bar, Trade
from engine.core.live_engine import LiveEngine
from engine.strategies.base import BaseStrategy
from engine.strategies.ibs_swing import IBSSwingStrategy

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from run_live import load_open_paper_positions  # noqa: E402

NS = 1_000_000_000


class _Feed:
    finite = True      # bounded fake: stream-end is DONE, not a disconnect
    def __init__(self, events):
        self._events = events

    async def stream(self):
        for e in self._events:
            yield e


class _Broker:
    async def submit(self, o):
        pass

    async def events(self):
        if False:
            yield None


# ── strategy hooks ────────────────────────────────────────────────────────
def test_base_strategy_not_restorable_by_default():
    assert BaseStrategy().restore_state(1, 100.0) is False


def test_ibs_restore_state_sets_position():
    s = IBSSwingStrategy("ES")
    assert s.pos == 0
    assert s.restore_state(1, 7484.5) is True
    assert s.pos == 1


# ── avg-cost reconstruction ────────────────────────────────────────────────
class _FakeQDB:
    def __init__(self, df):
        self._df = df

    def df(self, _sql):
        return self._df


def _pf(rows):
    df = pd.DataFrame(rows, columns=["sleeve", "side", "qty", "price"])
    return df


def test_load_open_positions_nets_closed_and_keeps_open():
    df = _pf([
        ("ES:ibs", 1, 1, 7484.5),                      # open long, never closed
        ("ES:dipbuy", 1, 2, 6000.0),
        ("ES:dipbuy", -1, 2, 6005.0),                  # dipbuy round-trip -> flat
        ("ES:flow", -1, 3, 6000.0),                    # open short
        ("ES:flow", 1, 1, 6001.0),                     # partial reduce -> -2 @ 6000
    ])
    out = load_open_paper_positions(_FakeQDB(df), ["ES"])
    assert out["ES:ibs"] == (1, 7484.5)
    assert "ES:dipbuy" not in out                       # netted flat
    assert out["ES:flow"] == (-2, 6000.0)              # avg unchanged on reduce


def test_load_open_positions_handles_flip():
    df = _pf([
        ("ES:x", 1, 1, 100.0),
        ("ES:x", -1, 3, 110.0),                        # flip to -2 @ 110
    ])
    out = load_open_paper_positions(_FakeQDB(df), ["ES"])
    assert out["ES:x"] == (-2, 110.0)


# ── LiveEngine: restore applied at the warmup->live flip ────────────────────
def _run(strategy, restore=None):
    async def go():
        # a backfill bar (warmup) then the first live trade (flip)
        feed = _Feed([Bar(1 * NS, "1m", 5000, 5001, 4999, 5000, 10, "ES"),
                      Trade(200 * NS, 5002.0, 1, 1, "ES")])
        # live_owners=set(): nothing routes live -> the sleeve is PAPER, which is
        # what restore applies to (a live position lives in the broker, not here)
        eng = LiveEngine(feed, _Broker(), [strategy], WallClock(),
                         Blotter("ES", 50.0), drain_timeout=0.2, warmup_gate=True,
                         live_owners=set())
        if restore is not None:
            eng.restore_paper_position(strategy, *restore)
        await asyncio.wait_for(eng.run(), timeout=5)
        return eng

    return asyncio.run(go())


def test_engine_restores_ibs_position_at_flip():
    s = IBSSwingStrategy("ES")
    eng = _run(s, restore=(1, 7484.5))
    assert s.pos == 1                                   # sleeve resumed the long
    assert eng._spos[id(s)] == 1                        # engine attributed book seeded
    assert eng._savg[id(s)] == 7484.5


def test_engine_drops_unrestorable_position(caplog):
    import logging

    class _NoRestore(BaseStrategy):
        symbol = "ES"

    s = _NoRestore()
    with caplog.at_level(logging.WARNING, logger="engine.live"):
        eng = _run(s, restore=(1, 6000.0))
    assert id(s) not in eng._spos                       # not seeded (would orphan)
    assert any("NOT restorable" in r.message for r in caplog.records)


def test_no_restore_registered_is_noop():
    s = IBSSwingStrategy("ES")
    eng = _run(s, restore=None)
    assert s.pos == 0 and id(s) not in eng._spos

    # zero position is never registered
    s2 = IBSSwingStrategy("ES")
    LiveEngine(_Feed([]), _Broker(), [s2], WallClock(),
               Blotter("ES", 50.0)).restore_paper_position(s2, 0, 0.0)
