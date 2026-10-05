"""Wall-clock EOD flatten timer — the 2026-10-02 feed-stall repro.

The supervisor's flatten (risk.on_market) is EVENT-timestamp gated: it only
fires while a market event carries a ts inside the 15:58-18:00 ET window. On
2026-10-02 the 10-minute-delayed CME feed stalled around the close, no event
landed in the window, and positions carried overnight.

Fix: LiveEngine._eod_timer checkpoints the WALL clock and, once it is past the
flatten minute and the supervisor has not flattened, drives on_market() with a
wall-clock timestamp. These tests prove that with a controllable clock.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from engine.core.blotter import Blotter
from engine.core.clock import EventClock, WallClock
from engine.core.events import Fill, Trade
from engine.core.live_engine import LiveEngine
from engine.core.orders import Order
from engine.core.risk import RiskConfig, RiskSupervisor

NS = 1_000_000_000


def _ns_at_et(y, m, d, hh, mm) -> int:
    dt = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("America/New_York"))
    return int(dt.timestamp() * 1e9)


class FakeWallClock(WallClock):
    """A WallClock whose now() the test controls (still isinstance WallClock,
    so the engine's live-only timer arms exactly as in production)."""

    def __init__(self, start_ns: int) -> None:
        self._ns = start_ns

    def now(self) -> int:
        return self._ns

    def set(self, ts: int) -> None:      # wall clock is authoritative: no-op
        return None


class Forgetful:
    """Enters 3 long on its first trade and never exits on its own."""
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


class ListFeed:
    finite = True

    def __init__(self, ev):
        self.ev = ev

    async def stream(self):
        for e in self.ev:
            yield e


class FillingBroker:
    """Submits get echoed back as instant Fill events (live-lane fills)."""
    finite = False

    def __init__(self, base_ns: int) -> None:
        self.submitted = []
        self._idx = 0
        self._base = base_ns

    async def submit(self, o):
        self.submitted.append(o)

    async def events(self):
        while True:
            if self._idx < len(self.submitted):
                o = self.submitted[self._idx]
                self._idx += 1
                yield Fill(self._base, o.order_id, o.symbol, 5000.0,
                           o.side * o.qty, 0.0, 0.0, tag=o.tag)
            else:
                await asyncio.sleep(0.005)


async def _wait_pos(eng, s, broker):
    """Wait until an entry was filled, then (if a flatten is expected) until
    the final flat order was submitted."""
    deadline = asyncio.get_event_loop().time() + 3.0
    while eng.strategy_position(s) == 0:
        if asyncio.get_event_loop().time() >= deadline:
            raise TimeoutError("entry never filled")
        await asyncio.sleep(0.01)


def test_fix1_timer_flattens_when_feed_stops_before_close():
    """Feed's last event is stamped 15:00 ET and the tape goes quiet; the WALL
    clock advances past 15:58; the timer must still drive the flatten."""
    base = _ns_at_et(2026, 7, 31, 15, 0)
    ev = [Trade(base, 5000.0, 1, 1, symbol="ES")]     # tape stops here
    # advance the wall clock to 15:58:30 ET, read by the timer
    clock_ns = _ns_at_et(2026, 7, 31, 15, 58) + 30 * 1_000_000_000

    async def go():
        s = Forgetful()
        broker = FillingBroker(clock_ns)
        risk = RiskSupervisor(RiskConfig(eod_flatten_et=(15, 58), point_value=50.0))
        eng = LiveEngine(ListFeed(ev), broker, [s], FakeWallClock(clock_ns),
                         Blotter("ES", 50.0), warmup_gate=False,
                         live_owners=None, risk=risk)
        eng.eod_poll_s = 0.05
        task = asyncio.create_task(eng.run())
        try:
            await _wait_pos(eng, s, broker)
            # let the timer tick a few times; it must submit a flatten
            deadline = asyncio.get_event_loop().time() + 3.0
            while eng.strategy_position(s) != 0:
                if asyncio.get_event_loop().time() >= deadline:
                    break
                await asyncio.sleep(0.05)
        finally:
            eng._stop.set()
            await asyncio.wait_for(task, timeout=5.0)
        return eng, s, broker

    eng, s, broker = asyncio.run(go())
    assert eng.strategy_position(s) == 0, \
        "wall-clock timer must flatten a stale-tape position"
    flats = [o for o in broker.submitted if o.reduce_only]
    assert flats and flats[-1].side == -1 and flats[-1].qty == 3


def test_fix1_timer_is_nop_for_event_clock():
    """A replay (EventClock) engine must not arm the wall-clock timer at all."""
    base = _ns_at_et(2026, 7, 31, 15, 0)
    feed = ListFeed([Trade(base, 5000.0, 1, 1, symbol="ES")])
    eng = LiveEngine(feed, FillingBroker(base), [Forgetful()], EventClock(),
                     Blotter("ES", 50.0), warmup_gate=False)
    assert eng._wallclock_eod is False
