"""RecorderTee: transparently passes events through AND records 1m bars as
batched INSERTs; a broken QuestDB never breaks the feed.

The stub DB answers the startup ingest probe (see engine/adapters/ingest_check)
so these tests exercise the same path the live recorder takes. Probe rows are
tagged PROBE_SYMBOL and filtered out of the assertions below -- that tag exists
precisely so a probe can never be mistaken for recorded market data."""
import asyncio

from engine.adapters.feeds.recorder_tee import RecorderTee
from engine.adapters.ingest_check import PROBE_SYMBOL
from engine.core.events import Bar, BookFlow, Trade

NS = 1_000_000_000


class _FakeQDB:
    def __init__(self, fail=False):
        self.queries = []
        self.fail = fail

    async def query(self, sql):
        self.queries.append(sql)
        if self.fail:
            raise RuntimeError("qdb down")
        if sql.lstrip().lower().startswith("select count()"):
            return {"dataset": [[1]]}          # the probe row landed
        return {}


class _FakeFeed:
    finite = True      # bounded fake: stream-end is DONE, not a disconnect
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
    inserts = [q for q in qdb.queries if q.startswith("INSERT") and PROBE_SYMBOL not in q]
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
    inserts = [q for q in qdb.queries if q.startswith("INSERT") and PROBE_SYMBOL not in q]
    assert all(q.count("),(") < 100 for q in inserts)     # each batch <=100 rows


def test_broken_qdb_does_not_break_feed():
    qdb = _FakeQDB(fail=True)
    feed = RecorderTee(_FakeFeed(_events(2)), qdb, symbol="ES")

    async def go():
        return [e async for e in feed.stream()]

    out = asyncio.run(go())
    assert len(out) == 3                           # feed still fully transparent
    assert feed.n_recorded == 0                    # nothing recorded, no exception


def test_records_per_second_aggressor_features():
    qdb = _FakeQDB()
    NSs = NS
    # second 1: BUY 3 @5000, SELL 1 @5001 -> adelta=+2, avol=4, ntr=2, pxc=5001
    # BookFlow at the 2s boundary closes second 1; then SELL 2 in second 2
    evs = [
        Trade(1 * NSs + 10, 5000.0, 3, 1),
        Trade(1 * NSs + 20, 5001.0, 1, -1),
        BookFlow(1 * NSs, 7, 4, 2, 9),             # bid_cancel/ask_cancel/bid_add/ask_add
        Trade(2 * NSs + 10, 5002.0, 2, -1),
        BookFlow(2 * NSs, 0, 0, 1, 0),
    ]
    feed = RecorderTee(_FakeFeed(evs), qdb, symbol="ES")

    async def go():
        return [e async for e in feed.stream()]

    out = asyncio.run(go())
    assert len(out) == 5                            # everything passed through
    assert feed.n_sec == 2
    sec_ins = [q for q in qdb.queries if q.startswith("INSERT") and "claude_sec_live" in q
                and PROBE_SYMBOL not in q]
    assert len(sec_ins) >= 1
    # first second: adelta=+2, avol=4, ntr=2, pxc=5001, plus the bookflow 7,4,2,9
    assert "5001.0,2,4,2,7,4,2,9" in sec_ins[0]
    # a sec-table was created with DEDUP
    assert any("claude_sec_live" in q and "DEDUP UPSERT" in q for q in qdb.queries)


def test_record_sec_off_disables_second_stream():
    qdb = _FakeQDB()
    feed = RecorderTee(_FakeFeed(_events(2)), qdb, symbol="ES", record_sec=False)

    async def go():
        return [e async for e in feed.stream()]

    asyncio.run(go())
    assert feed.n_sec == 0
    assert not any("claude_sec_live" in q for q in qdb.queries)
