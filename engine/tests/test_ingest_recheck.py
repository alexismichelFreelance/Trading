"""A database that dies MID-SESSION must be noticed.

On 2026-09-01 QuestDB saturated around 14:30 and stopped committing. The engine
ran until 16:13 believing itself healthy: raw capture reported dropped=0, every
counter climbed, and the heartbeat's !!TABLE-NOT-INGESTING alarm never fired --
because ingest_ok is set by a single probe at stream start and never revisited.
The last ~90 minutes of RTH were lost on every table.

verify_ingest's own docstring calls itself "a precondition check, not a
background monitor... the only moment the answer is actionable". That reasoning
is wrong once the session is running: mid-session the answer is not "repair the
database before starting", it is "everything you record from here is going
nowhere", and that is worth knowing at 14:30 rather than the next morning.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.feeds.raw_capture import RawCaptureTee    # noqa: E402


class FakeQDB:
    """Answers the probe read. `alive` flips to simulate the table dying.
    A whole PROBE failing (not one read) is what a slow commit looks like: the
    row is simply not readable inside the budget, so every poll in that probe
    returns 0. QuestDB does this routinely under write load."""
    def __init__(self): self.alive = True
    def query(self, sql): return {"dataset": [[1 if self.alive else 0]]}


def make():
    q = FakeQDB()
    rc = RawCaptureTee(inner=None, symbol="ES", qdb=q, probe_timeout_s=0.2,
                       recheck_timeout_s=0.2)
    rc._send_ilp = lambda payload: None            # the write always "succeeds"
    return rc, q


def test_probe_passes_while_the_table_is_alive():
    rc, q = make()
    rc.recheck_ingest()
    assert rc.ingest_ok is True


def test_a_table_that_dies_midsession_is_detected():
    rc, q = make()
    rc.recheck_ingest()
    assert rc.ingest_ok is True
    q.alive = False                                 # QuestDB stops committing
    for _ in range(rc.recheck_fail_n):              # a death needs a STREAK
        rc.recheck_ingest()
    assert rc.ingest_ok is False, (
        "ingest_ok stayed True after the table stopped storing -- this is the "
        "2026-09-01 failure: 90 minutes of tape lost with every counter healthy")


def test_recovery_is_detected_too():
    rc, q = make()
    q.alive = False
    for _ in range(rc.recheck_fail_n):
        rc.recheck_ingest()
    assert rc.ingest_ok is False
    q.alive = True
    rc.recheck_ingest()
    assert rc.ingest_ok is True


# ── the async twin: RecorderTee lost claude_bars_live for 101 minutes ────────
class FakeAsyncQDB:
    def __init__(self): self.alive = True
    async def query(self, sql):
        if sql.lstrip().upper().startswith("INSERT"):
            return {"dataset": []}
        return {"dataset": [[1 if self.alive else 0]]}


def make_recorder():
    from engine.adapters.feeds.recorder_tee import RecorderTee
    q = FakeAsyncQDB()
    r = RecorderTee(inner=None, qdb=q, symbol="ES", probe_timeout_s=0.2,
                    recheck_timeout_s=0.2)
    r._ready = True
    r._sready = True
    return r, q


def test_recorder_detects_a_table_that_dies_midsession():
    import asyncio
    r, q = make_recorder()
    asyncio.run(r.recheck_ingest())
    assert r.ingest_ok is True
    q.alive = False
    for _ in range(r.recheck_fail_n):
        asyncio.run(r.recheck_ingest())
    assert r.ingest_ok is False, (
        "recorder ingest_ok stayed True after the tables stopped storing")


# ── a slow commit is not a death ────────────────────────────────────────────
# The first version of this fix declared INGEST DIED on any single failed probe.
# In production that flapped 62 times in one session -- DIED 02:12, RECOVERED
# 02:23, DIED 02:34 -- while all four tables were ingesting normally, because a
# specific row is not always readable within the 15s budget under write load.
# The probe rows were landing the whole time (320 of them in the table).

def test_one_slow_commit_does_not_declare_death():
    rc, q = make()
    rc.recheck_ingest()
    assert rc.ingest_ok is True
    q.alive = False                                 # one probe misses its window
    rc.recheck_ingest()
    q.alive = True                                  # ...and the next is fine
    assert rc.ingest_ok is True, "a single slow commit was treated as a death"
    rc.recheck_ingest()
    assert rc.ingest_ok is True


def test_sustained_failure_is_still_declared():
    rc, q = make()
    rc.recheck_ingest()
    q.alive = False
    for _ in range(rc.recheck_fail_n):
        rc.recheck_ingest()
    assert rc.ingest_ok is False, "a real outage must still be reported"


def test_the_streak_resets_on_success():
    rc, q = make()
    rc.recheck_ingest()
    q.alive = False
    rc.recheck_ingest()                             # 1 of N
    q.alive = True
    rc.recheck_ingest()                             # resets
    q.alive = False
    rc.recheck_ingest()                             # 1 of N again, not N
    assert rc.ingest_ok is True
