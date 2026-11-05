"""Every fill the engine books reaches the record -- paper AND live.

The dashboard's /api/fills and /api/performance read claude_paper_fills and
take each row's mode from the roster. Live NT8 executions used to be wired only
to the chart painter, so they existed in the engine log and nowhere the
dashboard could read them: /api/fills returned {"fills": []} for every live
session. Paper fills had the mirror-image failure: the sink that persisted them
referenced a `label_by_id` that commit 2ae6ea1 deleted, raised NameError on
every fill, and the chained-sink wrapper swallowed it without a log line, so
nothing was recorded for weeks.

These pin the shared wiring: both hooks persist, with the owning sleeve's
label, and neither painting nor a missing owner can skip the record.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Fill                       # noqa: E402
from tools.run_live import wire_fill_persistence          # noqa: E402

NS = 1_000_000_000


class _Blot:
    def __init__(self):
        self.rows: list = []

    async def record(self, f, sleeve):
        self.rows.append((f.order_id, sleeve, f.size))


class _Engine:
    def __init__(self):
        self._owner: dict = {}
        self.on_live_fill = None
        self.on_paper_fill = None


def _fill(oid, size=1):
    return Fill(5 * NS, oid, "ES", 5000.0, size, 0.0, 0.0, "entry")


def test_both_live_and_paper_fills_are_recorded_with_their_sleeve():
    eng, blot = _Engine(), _Blot()
    paper_owner, live_owner = object(), object()
    eng._owner = {"P1": paper_owner, "O9": live_owner}
    wire_fill_persistence(eng, blot, {id(paper_owner): "ES:dip3",
                                      id(live_owner): "ES:onfade"})

    async def go():
        await eng.on_paper_fill(_fill("P1"))
        await eng.on_live_fill(_fill("O9", -1))

    asyncio.run(go())
    assert blot.rows == [("P1", "ES:dip3", 1), ("O9", "ES:onfade", -1)]


def test_live_fill_is_recorded_even_when_painting_raises():
    eng, blot = _Engine(), _Blot()
    owner = object()
    eng._owner = {"O1": owner}

    async def paint(_f):
        raise RuntimeError("draw socket gone")

    wire_fill_persistence(eng, blot, {id(owner): "ES:pivot"}, paint_live=paint)

    asyncio.run(eng.on_live_fill(_fill("O1")))
    assert blot.rows == [("O1", "ES:pivot", 1)]


def test_paper_is_persisted_before_painting():
    eng, blot = _Engine(), _Blot()
    owner = object()
    eng._owner = {"P1": owner}
    seen = []

    async def paint(_f):
        seen.append(list(blot.rows))

    wire_fill_persistence(eng, blot, {id(owner): "ES:dip3"}, paint_paper=paint)

    asyncio.run(eng.on_paper_fill(_fill("P1")))
    assert seen == [[("P1", "ES:dip3", 1)]]


def test_unknown_owner_still_records_under_placeholder():
    eng, blot = _Engine(), _Blot()
    wire_fill_persistence(eng, blot, {})

    async def go():
        await eng.on_live_fill(_fill("UNSEEN"))

    asyncio.run(go())
    assert blot.rows == [("UNSEEN", "?", 1)]
