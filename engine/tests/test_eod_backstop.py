"""The wall-clock EOD flatten backstop in LiveEngine.

The 2026-10-02 incident: the live market-data feed (a 10-minute delayed CME
subscription) stalled around the close, so RiskSupervisor.on_market — which
only runs when a market EVENT lands in the 15:58-18:00 ET window — never fired
and an intraday sleeve carried positions overnight.

The fix is a wall-clock task (_eod_backstop) wired only for WallClock engines.
It polls self.clock.now() independent of the feed, so a quiet/stalled feed
cannot make the flatten miss. These tests prove the guarantee end to end: the
feed stalls before the close, no market event lands in the window, and the
engine still flattens because the wall clock crossed the boundary.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.blotter import Blotter          # noqa: E402
from engine.core.clock import EventClock, WallClock  # noqa: E402
from engine.core.events import Trade             # noqa: E402
from engine.core.live_engine import LiveEngine   # noqa: E402
from engine.core.orders import Order             # noqa: E402
from engine.core.risk import RiskConfig, RiskSupervisor  # noqa: E402
from tests.test_session_boundary import _ns_at_et  # noqa: E402

NS = 1_000_000_000


class FakeWall(WallClock):
    """WallClock, but with a settable time so a test can drive 'the close'.

    Real WallClock.now() reads the OS clock; the backstop keys off
    isinstance(self.clock, WallClock) and uses clock.now() as its wall-clock
    feed. Subclassing keeps that gate true while letting the test control the
    instant. set() is a no-op, exactly like the real wall clock."""

    def __init__(self, ns: int) -> None:
        self.t = ns

    def now(self) -> int:
        return self.t

    def set(self, ts: int) -> None:              # noqa: D401 - wall clock wins
        return None


class HoldOnce:
    """Enters once on the first trade and never exits — a genuinely intraday
    sleeve that would ride overnight if nothing flattened it."""
    symbol = "ES"
    holds_overnight = False

    def __init__(self):
        self.n = 0

    def on_trade(self, t):
        self.n += 1
        return [Order("ES", 1, 3, tag="entry")] if self.n == 1 else []

    def on_bar(self, b):
        return []

    def on_fill(self, f):
        return None

    def on_position(self, p):
        return None


class StallFeed:
    """Yields the given events then stalls forever — the 2026-10-02 shape:
    the feed simply stops delivering around the close."""
    finite = False

    def __init__(self, ev):
        self.ev = ev

    async def stream(self):
        for e in self.ev:
            yield e
        await asyncio.sleep(1e9)                 # feed goes quiet and stays so


class NullBroker:
    finite = True

    async def submit(self, o):
        return None

    async def events(self):
        return
        yield


def _morning(day="2026-10-02"):
    y, m, d = (int(x) for x in day.split("-"))
    return _ns_at_et(y, m, d, 10, 0)


async def _run_to_flat(eng, fake, after_eod_ns):
    """Run the engine; the feed stalls after the morning entry. Then push the
    wall clock past the EOD flatten time and assert the sleeve is flattened
    even though NOT A SINGLE market event landed in the flatten window."""
    eng._eod_backstop_s = 0.02                   # fast poll for the test
    task = asyncio.create_task(eng.run())
    loop = asyncio.get_running_loop()
    # let the engine go live and put the sleeve long
    await eng.wait_processed(1)
    await asyncio.sleep(0.03)
    s = eng.strategies[0]
    assert eng.strategy_position(s) == 3, "sleeve failed to enter"
    # the STALL: no more feed events from here. Only the wall clock advances.
    fake.t = after_eod_ns
    deadline = loop.time() + 5.0
    while loop.time() < deadline and eng.strategy_position(s) != 0:
        await asyncio.sleep(0.01)
    eng.stop()
    await task
    return eng


def test_backstop_flattens_on_a_stalled_feed():
    """The guarantee: a feed that stalls before the close cannot leave the
    intraday sleeve holding overnight once the wall clock passes the flatten
    time. No market event lands in the 15:58 window — the wall clock does the
    work."""
    morning = _morning()

    async def go():
        fake = FakeWall(morning)
        cfg = RiskConfig(eod_flatten_et=(15, 58))
        s = HoldOnce()
        eng = LiveEngine(StallFeed([Trade(morning, 7500.0, 1, 1, symbol="ES")]),
                         NullBroker(), [s], fake, Blotter("ES", 50.0),
                         warmup_gate=False, live_owners=set(),
                         risk=RiskSupervisor(cfg))
        await _run_to_flat(eng, fake, _ns_at_et(2026, 10, 2, 16, 0))
        assert eng.strategy_position(s) == 0, \
            "intraday sleeve carried a position overnight on a stalled feed"
        assert eng._eod_backstop_fired >= 1, "backstop never emitted a flatten"
        assert eng.risk._eod_done, "supervisor never marked EOD done"

    asyncio.run(go())


def test_backstop_is_a_noop_before_the_flatten_time():
    """The backstop must not flatten early: an intraday sleeve still trading at
    15:57 stays where it is; only the wall-clock crossing 15:58 releases it."""
    morning = _morning()

    async def go():
        fake = FakeWall(_ns_at_et(2026, 10, 2, 15, 57))   # before EOD
        cfg = RiskConfig(eod_flatten_et=(15, 58))
        s = HoldOnce()
        eng = LiveEngine(StallFeed([Trade(morning, 7500.0, 1, 1, symbol="ES")]),
                         NullBroker(), [s], fake, Blotter("ES", 50.0),
                         warmup_gate=False, live_owners=set(),
                         risk=RiskSupervisor(cfg))
        eng._eod_backstop_s = 0.02
        task = asyncio.create_task(eng.run())
        await eng.wait_processed(1)
        await asyncio.sleep(0.15)                 # several backstop polls pass
        eng.stop()
        await task
        assert eng.strategy_position(s) == 3, "flattened before the flatten time"
        assert eng._eod_backstop_fired == 0

    asyncio.run(go())


def test_event_clock_engines_are_not_wired():
    """Replay (EventClock) engines must stay byte-for-byte unchanged: they drive
    everything off event time, so no wall-clock backstop task is created."""
    ev = [Trade(_ns_at_et(2026, 10, 2, 10, 0), 7500.0, 1, 1, symbol="ES")]

    class ListFeed:
        finite = True

        def __init__(self, ev):
            self.ev = ev

        async def stream(self):
            for e in self.ev:
                yield e

    s = HoldOnce()
    eng = LiveEngine(ListFeed(ev), NullBroker(), [s], EventClock(),
                     Blotter("ES", 50.0), warmup_gate=False, live_owners=set())
    asyncio.run(eng.run())
    assert not eng._eod_backstop_wired, \
        "replay engine must not run the wall-clock backstop"


def test_wall_clock_engine_wires_the_backstop():
    ev = [Trade(_ns_at_et(2026, 10, 2, 10, 0), 7500.0, 1, 1, symbol="ES")]

    class ListFeed:
        finite = True

        def __init__(self, ev):
            self.ev = ev

        async def stream(self):
            for e in self.ev:
                yield e

    s = HoldOnce()
    eng = LiveEngine(ListFeed(ev), NullBroker(), [s], WallClock(),
                     Blotter("ES", 50.0), warmup_gate=False, live_owners=set())
    asyncio.run(eng.run())
    assert eng._eod_backstop_wired, \
        "live engine must wire the wall-clock backstop task"
