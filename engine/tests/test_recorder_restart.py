"""RecorderTee across a feed reconnect — same shape as the RawCaptureTee bug.

A live feed is infinite: LiveEngine reconnects by calling `feed.stream()` again
on the SAME tee object. Anything the previous run left behind is still there.
RecorderTee carries two pieces of state that must NOT survive a reconnect:

1. the per-second trade accumulators (_pxc/_adelta/_avol/_ntr). A disconnect
   lands mid-second, so trades already counted but never closed by a BookFlow
   get folded into the FIRST second after the reconnect -- a second whose adelta
   and avol are the sum of two moments minutes apart. adelta is exactly what the
   ignition and flow sleeves consume, so this silently poisons the recorded
   feature the sleeves are replayed against.

2. `_live`. It exists to distinguish "backfill flood, batch it" from "live bar,
   flush it now for durability". On reconnect NT8 replays its whole chart -- 9466
   bars on 2026-08-05 -- but `_live` was still True from before the disconnect,
   so every one of those backfill bars took its own HTTP POST instead of being
   batched 100 at a time. That is ~9400 extra round trips fired at QuestDB during
   precisely the window when the feed is trying to catch up.

Neither breaks loudly. Both corrupt or degrade quietly, which is the class of
bug this suite exists to stop.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.feeds.recorder_tee import RecorderTee  # noqa: E402
from engine.adapters.ingest_check import PROBE_SYMBOL  # noqa: E402
from engine.core.events import BUY, SELL, Bar, BookFlow, Trade  # noqa: E402


class _Inner:
    finite = True

    def __init__(self, evs):
        self._evs = evs

    async def stream(self):
        for e in self._evs:
            yield e


class _FakeQDB:
    """Records every statement; INSERTs are captured so a test can read back
    exactly what would have been written."""

    def __init__(self):
        self.sql: list[str] = []

    async def query(self, sql):
        self.sql.append(sql)
        if sql.lstrip().lower().startswith("select count()"):
            return {"dataset": [[1]]}          # the startup ingest probe landed
        return {}

    def inserts(self, table):
        # probe rows are tagged and never counted as recorded market data
        return [s for s in self.sql
                if s.startswith(f"INSERT INTO {table}") and PROBE_SYMBOL not in s]

    def rows(self, table):
        out = []
        for s in self.inserts(table):
            out += s.split(" VALUES ", 1)[1].split("),(")
        return [r.strip("()") for r in out]


def _drain(tee, evs):
    async def go():
        tee.inner = _Inner(evs)
        return [e async for e in tee.stream()]
    return asyncio.run(go())


def test_per_second_accumulators_do_not_survive_a_reconnect():
    """THE REGRESSION. Trades counted just before the disconnect must not be
    added to the first second recorded after it."""
    qdb = _FakeQDB()
    tee = RecorderTee(None, qdb=qdb, symbol="ES")

    # run 1 ends mid-second: two trades accumulated, no BookFlow to close them
    _drain(tee, [Trade(1_000_000_000, 7000.0, 5, BUY, "ES"),
                 Trade(1_100_000_000, 7000.5, 7, BUY, "ES")])
    assert not qdb.rows("claude_sec_live"), "a second was closed with no BookFlow"

    # run 2 (the reconnect): ONE trade, then the second closes
    _drain(tee, [Trade(9_000_000_000, 7100.0, 2, SELL, "ES"),
                 BookFlow(9_000_000_000, 1, 2, 3, 4, "ES")])

    rows = qdb.rows("claude_sec_live")
    assert len(rows) == 1, f"expected one recorded second, got {rows}"
    f = rows[0].split(",")
    adelta, avol, ntr = int(f[3]), int(f[4]), int(f[5])
    assert (adelta, avol, ntr) == (-2, 2, 1), (
        f"the first second after the reconnect recorded adelta={adelta} avol={avol} "
        f"ntr={ntr}; it should describe only the one trade that happened in it, "
        f"not the two stranded by the disconnect")


def test_backfill_after_a_reconnect_is_batched_not_one_post_per_bar():
    """`_live` must be re-armed per run. Otherwise NT8's post-reconnect chart
    replay flushes once per bar, hammering the DB exactly when it is busiest."""
    qdb = _FakeQDB()
    tee = RecorderTee(None, qdb=qdb, symbol="ES")

    # run 1: a live session (a Trade sets _live), ending cleanly
    _drain(tee, [Trade(1_000_000_000, 7000.0, 1, BUY, "ES")])

    # run 2: the reconnect delivers a 250-bar backfill flood, no Trade yet
    bars = [Bar(2_000_000_000 + i * 60_000_000_000, "1m", 1, 2, 0, 1, 5, "ES")
            for i in range(250)]
    qdb.sql.clear()
    _drain(tee, bars)

    posts = len(qdb.inserts("claude_bars_live"))
    assert posts <= 5, (
        f"{posts} separate INSERTs for a 250-bar backfill -- _live survived the "
        f"reconnect, so nothing batched")


def test_a_reconnect_does_not_lose_buffered_rows():
    """The opposite guard: state that SHOULD survive must survive. Rows buffered
    but not yet flushed when the feed dropped still belong in the table."""
    qdb = _FakeQDB()
    tee = RecorderTee(None, qdb=qdb, symbol="ES")
    _drain(tee, [Trade(1_000_000_000, 7000.0, 1, BUY, "ES"),
                 BookFlow(1_000_000_000, 1, 1, 1, 1, "ES")])
    _drain(tee, [Trade(2_000_000_000, 7001.0, 1, BUY, "ES"),
                 BookFlow(2_000_000_000, 1, 1, 1, 1, "ES")])
    assert len(qdb.rows("claude_sec_live")) == 2, "a recorded second was lost"


def test_tables_are_reprepared_after_a_failed_first_attempt():
    """If the DB was down when the first run started, recording is disabled for
    that run. The reconnect must retry -- not stay disabled for the session."""
    class _FlakyQDB(_FakeQDB):
        def __init__(self):
            super().__init__()
            self.fail = True

        async def query(self, sql):
            if self.fail and sql.startswith("CREATE TABLE"):
                raise RuntimeError("questdb down")
            return await super().query(sql)

    qdb = _FlakyQDB()
    tee = RecorderTee(None, qdb=qdb, symbol="ES")
    _drain(tee, [Trade(1_000_000_000, 7000.0, 1, BUY, "ES"),
                 BookFlow(1_000_000_000, 1, 1, 1, 1, "ES")])
    assert not qdb.rows("claude_sec_live")      # correctly disabled

    qdb.fail = False                            # DB comes back
    _drain(tee, [Trade(2_000_000_000, 7001.0, 1, BUY, "ES"),
                 BookFlow(2_000_000_000, 1, 1, 1, 1, "ES")])
    assert qdb.rows("claude_sec_live"), (
        "recorder stayed disabled after the database recovered")
