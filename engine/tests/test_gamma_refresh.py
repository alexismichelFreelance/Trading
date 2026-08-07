"""Gamma auto-refresh across ET session rollover: GammaRegime reloads in place,
and LiveEngine fires the rollover hook once per ET-day change (live only)."""
import asyncio

import pandas as pd

from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.events import Bar, Trade
from engine.core.live_engine import LiveEngine
from engine.features.gamma import GammaRegime


class _SeqQDB:
    """Returns a different frame on each .df() call — simulates the DB gaining
    a new gamma row between the initial load and a reload()."""

    def __init__(self, frames):
        self.frames = frames
        self.i = 0

    def df(self, _sql):
        f = self.frames[min(self.i, len(self.frames) - 1)]
        self.i += 1
        return f


def _gexdf(rows):
    return pd.DataFrame({"ts": pd.to_datetime([r[0] for r in rows], utc=True),
                         "gexp": [r[1] for r in rows]})


def test_gamma_regime_reload_picks_up_new_session():
    before = _gexdf([("2026-07-17", 0.10), ("2026-07-20", 0.20)])
    after = _gexdf([("2026-07-17", 0.10), ("2026-07-20", 0.20), ("2026-07-21", 0.90)])
    gr = GammaRegime(_SeqQDB([before, after]))
    # 07-22's prior session is 07-20 (0.20) -> short gamma
    assert gr.gexp_prev("2026-07-22") == 0.20
    assert gr.is_short_gamma("2026-07-22") is True
    gr.reload()
    # now 07-21 (0.90) is the prior session -> long gamma
    assert gr.gexp_prev("2026-07-22") == 0.90
    assert gr.is_short_gamma("2026-07-22") is False


def _et_ns(s):
    return int(pd.Timestamp(s, tz="America/New_York").value)


class _Feed:
    finite = True      # bounded fake: stream-end is DONE, not a disconnect
    def __init__(self, evs):
        self._evs = evs

    async def stream(self):
        for e in self._evs:
            yield e


class _Broker:
    async def submit(self, o):
        pass

    async def events(self):
        if False:
            yield None


def _run(events, warmup_gate):
    fired = []

    async def go():
        eng = LiveEngine(_Feed(events), _Broker(), [], WallClock(),
                         Blotter("ES", 50.0), drain_timeout=0.2,
                         warmup_gate=warmup_gate, live_owners=set())

        async def hook(day):
            fired.append(day)

        eng.on_session_rollover = hook
        await asyncio.wait_for(eng.run(), timeout=5)

    asyncio.run(go())
    return fired


def test_rollover_fires_once_per_et_day_change():
    evs = [Trade(_et_ns("2026-07-20 10:00"), 5000.0, 1, 1, "ES"),
           Trade(_et_ns("2026-07-20 15:00"), 5001.0, 1, 1, "ES"),   # same day
           Trade(_et_ns("2026-07-21 10:00"), 5002.0, 1, 1, "ES")]   # rollover
    assert _run(evs, warmup_gate=False) == ["2026-07-21"]


def test_rollover_silent_during_warmup():
    # only backfill bars (no live Trade) crossing a day boundary -> never live,
    # so historical day changes must NOT fire the refresh
    evs = [Bar(_et_ns("2026-07-20 10:00"), "1m", 5000, 5001, 4999, 5000, 10, "ES"),
           Bar(_et_ns("2026-07-21 10:00"), "1m", 5000, 5001, 4999, 5000, 10, "ES")]
    assert _run(evs, warmup_gate=True) == []


# ── the engine must never depend on being restarted to have current gamma ────
#
# The rollover hook alone is not enough, and the reasons are all silent:
#
#  1. it is delivered through _emit_sink, a BOUNDED queue that DROPS on overflow.
#     One dropped callback = stale gamma for the whole session, counted in
#     _sink_dropped and nowhere else.
#  2. it fires at ET midnight; the gamma fetch runs at 09:00 ET. A fetch that is
#     late, retried, or simply slower than usual lands AFTER the only reload the
#     engine will attempt for 24 hours.
#  3. a reload that "succeeds" only means the query ran. If the fetch failed that
#     morning the snapshot is byte-identical and nothing says so.
#
# The engine is meant to run for weeks. So freshness has to be a property it
# maintains from its own state, not an event it hopes to receive.

def test_regime_knows_when_its_snapshot_cannot_answer_for_today():
    """THE PRIMITIVE. Without this the engine cannot tell 'I have what today
    needs' from 'I am serving a snapshot from last week'."""
    gr = GammaRegime(_SeqQDB([_gexdf([("2026-07-17", 0.10), ("2026-08-04", 0.55)])]))
    assert gr.needs_reload("2026-08-05") is False      # 08-04 answers for 08-05
    assert gr.needs_reload("2026-08-12") is True       # 08-04 is too old now


def test_a_late_fetch_is_picked_up_without_a_restart():
    """The 09:00 ET fetch lands after midnight's reload. The engine must still
    end up on the right value the same day -- not tomorrow, not after a restart."""
    stale = _gexdf([("2026-08-03", 0.10)])                      # 08-04 missing
    fresh = _gexdf([("2026-08-03", 0.10), ("2026-08-04", 0.92)])
    gr = GammaRegime(_SeqQDB([stale, stale, fresh]))
    assert gr.needs_reload("2026-08-11") is True                # 08-03 too old
    gr.reload()                                                 # midnight: still stale
    assert gr.needs_reload("2026-08-11") is True                # so it must ask again
    gr.reload()                                                 # the fetch has landed
    assert gr.gexp_prev("2026-08-05") == 0.92
    assert gr.needs_reload("2026-08-05") is False


def test_reload_reports_whether_the_data_actually_moved():
    """A reload that changes nothing must be distinguishable from one that does,
    or a dead fetch looks exactly like a healthy one."""
    same = _gexdf([("2026-08-04", 0.55)])
    more = _gexdf([("2026-08-04", 0.55), ("2026-08-05", 0.31)])
    gr = GammaRegime(_SeqQDB([same, same, more]))
    assert gr.reload() is False        # nothing new
    assert gr.reload() is True         # gained a session


def test_needs_reload_is_cheap_and_touches_no_database():
    """It runs on the hot path's schedule, so it must be pure snapshot
    arithmetic -- a query here would put QuestDB in front of trading."""
    q = _SeqQDB([_gexdf([("2026-08-04", 0.55)])])
    gr = GammaRegime(q)
    calls = q.i
    for _ in range(1000):
        gr.needs_reload("2026-08-05")
    assert q.i == calls, "needs_reload hit the database"
