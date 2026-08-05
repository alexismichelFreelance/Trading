"""The 2026-07-29 corruption: paper fills stamped from the wrong clock, and a
mid-session backfill traded as if live.

`_paper_submit` used to stamp fills with `self.clock.now()`. In live mode that is
WallClock, whose `set()` is a NO-OP, so the stamp came from the OS while the
PRICE came from the event stream. Whenever those two disagreed the engine wrote a
trade that never happened. On 2026-07-29 NT8 replayed history into the running
engine and claude_paper_fills -- the table every sleeve is judged on -- recorded
a fill at 16:53 ET priced 7459.50 while ES was trading 7337-7340, and sleeves
that hard-flatten at 15:59 logged fresh ENTRIES at 16:53. 85 of 163 rows were
fiction and the day's P&L was meaningless.

Two invariants, both pinned here:
  1. a paper fill's ts is the ts of the event that priced it -- never wall time;
  2. an event that regresses far behind the newest one a lane has already
     delivered is a REPLAY, and is never traded on -- even after that lane has
     gone live (the warmup gate only covers startup). Detected in event time, so
     one rule covers live, replay and tests with nothing to configure.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from engine.core.blotter import Blotter
from engine.core.clock import EventClock, WallClock
from engine.core.events import Bar, Trade
from engine.core.live_engine import LiveEngine
from engine.core.orders import Order

NS = 1_000_000_000


def _mid_session() -> int:
    """A fixed 11:00 ET instant, well inside RTH.

    NOT time.time_ns(): the engine now flattens intraday sleeves during the
    15:59-18:00 ET closing window, so a test anchored to "now" quietly gains an
    extra flatten fill when it happens to run in that window -- passing or
    failing by wall-clock time of day. Anchoring makes it deterministic.
    """
    return int(datetime(2026, 7, 31, 11, 0,
                        tzinfo=ZoneInfo("America/New_York")).timestamp() * 1e9)


class Sleeve:
    """Minimal paper sleeve: owns a position, so fills route back to it."""
    symbol = "ES"

    def __init__(self) -> None:
        self.seen = 0
        self.pos = 0

    def on_fill(self, f) -> None:
        return None

    def on_position(self, p) -> None:
        self.pos = p.qty


class EveryTrade(Sleeve):
    """Emits one order per trade, so every dispatched event is observable."""

    def on_trade(self, t: Trade) -> list[Order]:
        self.seen += 1
        return [Order("ES", 1, 1, tag="t")]


class ListFeed:
    finite = True

    def __init__(self, events) -> None:
        self._ev = events

    async def stream(self):
        for e in self._ev:
            yield e


class NullBroker:
    async def submit(self, o) -> None:
        return None

    async def events(self):
        if False:
            yield None
        return


def _run(events, clock, **kw):
    strat = EveryTrade()
    eng = LiveEngine(ListFeed(events), NullBroker(), [strat], clock,
                     Blotter("ES", 50.0), warmup_gate=False,
                     live_owners=set(), **kw)      # live_owners empty -> all paper
    asyncio.run(eng.run())
    return strat, eng


def test_paper_fill_ts_is_event_time_not_wall_time():
    """The core bug. Under a WallClock, a fill priced from an event two hours old
    must still be STAMPED two hours old -- otherwise price and time disagree."""
    now = _mid_session()
    old = now - 2 * 3600 * NS
    ev = [Trade(old, 7430.00, 1, 1, symbol="ES")]
    _, eng = _run(ev, WallClock())
    assert len(eng.paper_fills) == 1
    f = eng.paper_fills[0]
    assert f.price == 7430.00
    assert f.ts == old, "fill stamped from the wall clock, not the event"
    assert abs(f.ts - now) > 3600 * NS


def test_price_and_timestamp_always_come_from_the_same_event():
    """Several events at different times/prices: every fill must pair the price it
    filled at with that same event's ts."""
    base = _mid_session()
    ev = [Trade(base + i * 60 * NS, 7400.0 + i, 1, 1, symbol="ES") for i in range(5)]
    _, eng = _run(ev, WallClock())
    assert len(eng.paper_fills) == 5
    for i, f in enumerate(eng.paper_fills):
        assert f.ts == base + i * 60 * NS
        assert f.price == 7400.0 + i


def test_stale_events_after_going_live_are_not_traded():
    """A mid-session backfill. The lane goes live on a fresh trade, then the feed
    replays old data -- as NT8 did on 2026-07-29. Those must not produce fills."""
    now = _mid_session()
    ev = [
        Trade(now, 7340.00, 1, 1, symbol="ES"),               # live: flips the lane
        Trade(now - 6 * 3600 * NS, 7459.50, 1, 1, symbol="ES"),   # replayed history
        Trade(now - 6 * 3600 * NS + NS, 7471.25, 1, 1, symbol="ES"),
        Trade(now - 6 * 3600 * NS + 2 * NS, 7481.75, 1, 1, symbol="ES"),
    ]
    strat, eng = _run(ev, WallClock())
    assert strat.seen == 4, "state must still warm on historical events"
    prices = [f.price for f in eng.paper_fills]
    assert prices == [7340.00], f"traded on replayed history: {prices}"
    assert eng._stale_events.get("ES") == 3


def test_old_but_ascending_events_are_never_gated():
    """Year-old replay data, strictly ascending: legitimate, and must all trade.
    The criterion is regression, not age -- which is why replay, tests and live
    can share one rule with nothing to configure per call site."""
    old = _mid_session() - 400 * 24 * 3600 * NS
    ev = [Trade(old + i * NS, 5000.0 + i, 1, 1, symbol="ES") for i in range(3)]
    _, eng = _run(ev, EventClock())
    assert len(eng.paper_fills) == 3, "ascending replay must never be gated"
    assert not eng._stale_events
    # and identically under a WallClock: the clock type is irrelevant
    _, eng2 = _run(ev, WallClock())
    assert len(eng2.paper_fills) == 3
    assert not eng2._stale_events


def test_small_out_of_order_jitter_is_tolerated():
    """Sub-tolerance reordering between lanes is not a backfill; only a regression
    bigger than stale_event_s counts."""
    now = _mid_session()
    ev = [Trade(now, 7340.00, 1, 1, symbol="ES"),
          Trade(now - 2 * NS, 7340.25, 1, 1, symbol="ES")]   # 2s back, tol 60s
    _, eng = _run(ev, WallClock(), stale_event_s=60.0)
    assert len(eng.paper_fills) == 2
    assert not eng._stale_events


def test_bars_carry_their_own_timestamp_too():
    """Bar-driven sleeves fill at bar close; the stamp must be the bar's ts."""
    class OnBar(Sleeve):
        def on_bar(self, b):
            return [Order("ES", -1, 1, tag="b")] if b.tf == "1m" else []

    base = _mid_session()
    # Bar is (ts, tf, o, h, l, c, v, symbol)
    ev = [Bar(base, "1m", 7400.0, 7410.0, 7395.0, 7402.5, 100, symbol="ES")]
    eng = LiveEngine(ListFeed(ev), NullBroker(), [OnBar()], WallClock(),
                     Blotter("ES", 50.0), warmup_gate=False, live_owners=set())
    asyncio.run(eng.run())
    assert len(eng.paper_fills) == 1
    assert eng.paper_fills[0].ts == base
    assert eng.paper_fills[0].price == 7402.5
