"""PaperBlotter: records paper fills to QuestDB; a broken QuestDB never raises.

The stub answers the startup ingest probe -- claude_paper_fills silently stored
11 of 56 writes on 2026-08-05 and took a whole session's positions with it, so
this table is round-tripped at startup like the recorders. Probe rows are tagged
PROBE_SYMBOL and filtered out of the assertions."""
import asyncio

from engine.adapters.ingest_check import PROBE_SYMBOL
from engine.adapters.paper_blotter import PaperBlotter
from engine.core.events import Fill

NS = 1_000_000_000


class _FakeQDB:
    def __init__(self, fail_after=None):
        self.queries = []
        self.fail_after = fail_after

    async def query(self, sql):
        self.queries.append(sql)
        if self.fail_after is not None and len(self.queries) > self.fail_after:
            raise RuntimeError("qdb down")
        if sql.lstrip().lower().startswith("select count()"):
            return {"dataset": [[1]]}          # the probe row landed
        return {}


def _fill(oid, price, size, tag):
    return Fill(5 * NS, oid, "ES", price, size, 0.0, 0.0, tag)


def test_records_paper_fills_with_sleeve():
    qdb = _FakeQDB()
    pb = PaperBlotter(qdb, symbol="ES")

    async def go():
        await pb.start()
        await pb.record(_fill("O1", 5000.0, 2, "dipA-entry"), "dipbuy")
        await pb.record(_fill("O2", 5004.0, -2, "dip-vwap"), "dipbuy")

    asyncio.run(go())
    assert pb.n == 2
    ins = [q for q in qdb.queries if q.startswith("INSERT") and PROBE_SYMBOL not in q]
    assert len(ins) == 2
    assert "claude_paper_fills" in qdb.queries[0] and "DEDUP UPSERT KEYS(ts, order_id)" in qdb.queries[0]
    # side derived from signed size; sleeve + tag present
    assert "'dipbuy'" in ins[0] and "'dipA-entry'" in ins[0] and ",1,2," in ins[0]
    assert ",-1,2," in ins[1]                          # the sell


def test_broken_qdb_never_raises():
    qdb = _FakeQDB(fail_after=1)                        # CREATE ok, INSERT fails
    pb = PaperBlotter(qdb, symbol="ES")

    async def go():
        await pb.start()
        await pb.record(_fill("O1", 5000.0, 1, "e"), "zones")   # must not raise

    asyncio.run(go())
    assert pb.n == 0                                    # nothing counted, no exception


def test_table_init_failure_disables_quietly():
    qdb = _FakeQDB(fail_after=0)                        # CREATE itself fails
    pb = PaperBlotter(qdb, symbol="ES")

    async def go():
        await pb.start()
        await pb.record(_fill("O1", 5000.0, 1, "e"), "zones")

    asyncio.run(go())
    assert pb._ready is False and pb.n == 0
