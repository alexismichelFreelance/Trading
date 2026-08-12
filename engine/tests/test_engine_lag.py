"""The engine must know how far behind the tape it is.

2026-08-12: restarted at 09:45 ET, went LIVE at 10:48:55 wall clock on an event
stamped 09:39:39. Sixty-nine minutes behind, placing orders the whole way, and
nothing in the log said so. Reconstructing it afterwards took the paper-fill
timestamps, the NQ 1m bars and the raw-capture row counters, and still did not
identify the cause.

The existing staleness guard cannot catch this and never could. It asks "is this
event behind the NEWEST ONE THIS LANE HAS SEEN?" -- a relative test, and the
right one for its job (an NT8 chart reload replays history, so timestamps jump
BACKWARDS). A uniformly delayed stream jumps nowhere: it ascends perfectly, one
event at a time, sixty-nine minutes late. It is indistinguishable from a healthy
feed by that test, by construction.

So the engine measures the absolute thing too, and reports two numbers:

    LAG    wall clock - the timestamp of the event being dispatched
    QUEUE  depth of the dispatch queue

which between them say WHOSE fault it is. Large lag with a deep queue: the
engine is not keeping up, the backlog is ours. Large lag with an empty queue:
the events are arriving late and no engine tuning will touch it. On 2026-08-12
neither number existed, so the question could not be answered at all.

Off by default -- replay timestamps are years from the wall clock and would
report nonsense -- and turned on by the live runner. One opt-in, one call site.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.blotter import Blotter  # noqa: E402
from engine.core.clock import EventClock  # noqa: E402
from engine.core.events import BUY, Trade  # noqa: E402
from engine.core.live_engine import LiveEngine  # noqa: E402

NS = 1_000_000_000


class _Quiet:
    symbol = "ES"

    def on_trade(self, e):
        return []

    def on_fill(self, f):
        return None

    def on_position(self, p):
        return None


class _Feed:
    finite = True

    def __init__(self, evs):
        self.evs = evs

    async def stream(self):
        for e in self.evs:
            yield e


class _Broker:
    async def submit(self, o):
        return None

    async def events(self):
        return
        yield


def _run(lag_s: float, n: int = 1100, report_s: float = 0.0):
    """`n` > 1024 because the lag is sampled every 1024 events -- measuring it on
    every tick would cost a clock read per event on the hot path."""
    base = time.time_ns() - int(lag_s * NS)
    evs = [Trade(base + i * 1_000_000, 7700.0 + i * 0.25, 1, BUY, "ES")
           for i in range(n)]

    async def go():
        eng = LiveEngine(_Feed(evs), _Broker(), [_Quiet()], EventClock(),
                         Blotter("ES", 50.0), live_owners=set(),
                         warmup_gate=False)
        eng.lag_report_s = report_s
        await eng.run()
        return eng

    return asyncio.run(go())


def test_an_engine_an_hour_behind_the_tape_knows_it():
    """THE 2026-08-12 CASE."""
    eng = _run(69 * 60)
    assert eng.feed_lag_s > 60 * 60, (
        f"engine reports {eng.feed_lag_s:.0f}s behind while dispatching events "
        f"69 minutes old -- exactly the blindness that made 2026-08-12 "
        f"invisible while it happened")


def test_a_current_feed_reports_no_meaningful_lag():
    eng = _run(0.0)
    assert eng.feed_lag_s < 30.0, f"healthy feed reported {eng.feed_lag_s:.1f}s"


def test_it_is_off_unless_the_runner_asks():
    """Measuring is free and always on, so the number is there for any
    diagnostic. REPORTING is opt-in: replay drives timestamps years from the
    wall clock and would fill the log with alarm about a 2020 backtest."""
    eng = _run(69 * 60, report_s=0.0)
    assert LiveEngine(_Feed([]), _Broker(), [], EventClock(),
                      Blotter("ES", 50.0)).lag_report_s == 0.0
    assert eng.feed_lag_s > 0  # still measured; just not logged


def test_a_big_lag_is_logged_loudly(caplog):
    with caplog.at_level(logging.ERROR, logger="engine.live"):
        _run(69 * 60, report_s=0.001)
    hits = [r for r in caplog.records if "BEHIND THE TAPE" in r.message]
    assert hits, "69 minutes behind and the log said nothing"
    assert "queue" in hits[0].getMessage().lower(), (
        "the lag alone does not say whose fault it is -- the queue depth "
        "separates 'we are slow' from 'they are late'")


def test_a_shallow_queue_blames_upstream_not_the_engine():
    """The distinction that matters. These events are fed one at a time and
    dispatched immediately, so the queue is empty: the engine is keeping up
    perfectly and the data is simply old. The message must say so rather than
    send someone profiling the dispatch loop for a week."""
    import io

    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    log = logging.getLogger("engine.live")
    log.addHandler(h)
    try:
        _run(69 * 60, report_s=0.001)
    finally:
        log.removeHandler(h)
    out = buf.getvalue()
    assert "ARRIVING late" in out, (
        f"empty queue but the log blames the engine:\n{out[:500]}")
