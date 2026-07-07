"""RecorderTee: transparently passes events through AND records 1m bars as
batched INSERTs; a broken QuestDB never breaks the feed."""
import asyncio

from engine.adapters.feeds.recorder_tee import RecorderTee
from engine.core.events import Bar, Trade

NS = 1_000_000_000


class _FakeQDB:
    def __init__(self, fail=False):
        self.queries = []
        self.fail = fail

    async def query(self, sql):
        self.queries.append(sql)
        if self.fail:
            raise RuntimeError("qdb down")
        return {}


class _FakeFeed:
    def __init__(self, events):
        self.events = events

    async def stream(self):
        for e in self.events:
            yield e


def _events(n_bars):
    evs = [Trade(1 * NS, 5000.0, 1, 1)]           # flips "live"
    for i in range(n_bars):
        ts = (100 + i) * 60 * NS
        evs.append(Bar(ts, "1m", 5000 + i, 5001 + i, 4999 + i, 5000 + i, 10 + i))
    return evs


def test_records_bars_and_passes_through():
    qdb = _FakeQDB()
    feed = RecorderTee(_FakeFeed(_events(3)), qdb, symbol="ES")

    async def go():
        return [e async for e in feed.stream()]

    out = asyncio.run(go())
    assert len(out) == 4                          # 1 trade + 3 bars, all passed through
    assert feed.n_recorded == 3
    inserts = [q for q in qdb.queries if q.startswith("INSERT")]
    assert len(inserts) >= 1
    assert "claude_bars_live" in qdb.queries[0]    # CREATE TABLE ... DEDUP
    assert "DEDUP UPSERT KEYS(ts, symbol)" in qdb.queries[0]
    assert "'ES'" in inserts[0] and "5000" in inserts[0]


def test_backfill_batches_at_100():
    qdb = _FakeQDB()
    # 250 bars BEFORE any trade (pure backfill) -> flushed in <=100 batches
    evs = [Bar((100 + i) * 60 * NS, "1m", 5000, 5001, 4999, 5000, 5) for i in range(250)]
    feed = RecorderTee(_FakeFeed(evs), qdb, symbol="ES")

    async def go():
        return [e async for e in feed.stream()]

    asyncio.run(go())
    assert feed.n_recorded == 250
    inserts = [q for q in qdb.queries if q.startswith("INSERT")]
    assert all(q.count("),(") < 100 for q in inserts)     # each batch <=100 rows


def test_broken_qdb_does_not_break_feed():
    qdb = _FakeQDB(fail=True)
    feed = RecorderTee(_FakeFeed(_events(2)), qdb, symbol="ES")

    async def go():
        return [e async for e in feed.stream()]

    out = asyncio.run(go())
    assert len(out) == 3                           # feed still fully transparent
    assert feed.n_recorded == 0                    # nothing recorded, no exception
