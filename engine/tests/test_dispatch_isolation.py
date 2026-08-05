"""One sleeve raising must never stop the other thirty-five.

2026-08-04: paper fills stopped dead at 11:10:00 ET and never resumed. No 15:59
session flat fired, so 21 sleeves were left holding positions into the close.
The feed, the recorders and the raw-capture thread all ran on to 16:01 and the
terminal heartbeat showed nothing wrong, because it prints the LIVE blotter
counters, which read zero all day in a paper-only setup.

Underneath, the NVMe controller had hung (stornvme 11 + 129, then 45 paging
errors and 567 NTFS delayed-write failures over 47 minutes). QuestDB writes were
failing. A failing sink raises -- and dispatch_market has no try/except around
the per-strategy call, so ONE exception ends the loop for every strategy after
it, for that event and every event after.

Proven before this fix:
    events fed     : 20
    observer saw   : 1     (first in the list)
    exploder saw   : 1
    downstream saw : 0
    paper fills    : 0     ENGINE DIED: ValueError

A hardware fault is not preventable. Twenty-one abandoned positions because of
it is. The blast radius of a broken sleeve must be that sleeve.

Design the tests pin:
  * a raising strategy does not stop the ones after it;
  * it does not stop LATER EVENTS either -- otherwise the loop limps on but the
    book still stops trading;
  * the failure is counted and named, never swallowed silently;
  * a sleeve that keeps raising is DISABLED rather than called forever;
  * the observer position in the list is irrelevant (it was masking the fault).
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.blotter import Blotter  # noqa: E402
from engine.core.clock import EventClock  # noqa: E402
from engine.core.dispatch import dispatch_broker, dispatch_market  # noqa: E402
from engine.core.events import Fill, Trade  # noqa: E402
from engine.core.live_engine import LiveEngine  # noqa: E402
from engine.core.orders import Order  # noqa: E402

NS = 1_000_000_000
BASE = 1_700_000_000 * NS


class Good:
    """A normal sleeve: orders on every trade."""
    symbol = "ES"

    def __init__(self, tag="good"):
        self.n = 0
        self.tag = tag

    def on_trade(self, t):
        self.n += 1
        return [Order("ES", 1, 1, tag=self.tag)]

    def on_fill(self, f):
        return None

    def on_position(self, p):
        return None


class Exploder:
    """The sleeve whose QuestDB-backed sink blew up."""
    symbol = "ES"

    def __init__(self):
        self.n = 0

    def on_trade(self, t):
        self.n += 1
        raise ValueError("sink write failed")

    def on_fill(self, f):
        raise ValueError("sink write failed")

    def on_position(self, p):
        return None


class ListFeed:
    finite = True

    def __init__(self, ev):
        self._ev = ev

    async def stream(self):
        for e in self._ev:
            yield e


class NullBroker:
    async def submit(self, o):
        return None

    async def events(self):
        return
        yield


def _trades(n=20):
    return [Trade(BASE + i * NS, 7000.0 + i, 1, 1, "ES") for i in range(n)]


# ── unit level: the dispatcher itself ────────────────────────────────────────

def test_raising_strategy_does_not_block_the_ones_after_it():
    """THE REPRODUCTION, at the dispatcher. The sleeve listed after the one that
    raises must still be called and its order must still come back."""
    a, boom, b = Good("first"), Exploder(), Good("last")
    out = dispatch_market([a, boom, b], _trades(1)[0])
    assert a.n == 1, "strategy before the exploder was not called"
    assert b.n == 1, "strategy AFTER the exploder never ran"
    tags = {o.tag for o in out}
    assert tags == {"first", "last"}, f"orders lost: {tags}"


def test_broker_events_are_isolated_too():
    """Fills route through a separate dispatcher with the same defect."""
    a, boom, b = Good("first"), Exploder(), Good("last")
    seen = []
    a.on_fill = lambda f: seen.append("a")
    b.on_fill = lambda f: seen.append("b")
    dispatch_broker([a, boom, b], Fill(BASE, "O1", "ES", 7000.0, 1, 0.0, 0.0, "x"))
    assert seen == ["a", "b"], f"a raising on_fill blocked the rest: {seen}"


def test_failure_is_recorded_not_swallowed():
    """Silent recovery is its own bug: the reason this went unnoticed for a
    session is that nothing was counted. The dispatcher must expose what failed."""
    from engine.core import dispatch
    boom = Exploder()
    dispatch.reset_failures()
    dispatch_market([Good(), boom, Good()], _trades(1)[0])
    f = dispatch.strategy_failures()
    assert f, "a raising strategy produced no failure record"
    assert any("Exploder" in k or "sink write failed" in str(v) for k, v in f.items()), f


def test_persistently_failing_strategy_is_disabled():
    """A sleeve raising on every event must stop being called, not raise 500k
    times a session and drown the log."""
    from engine.core import dispatch
    dispatch.reset_failures()
    boom = Exploder()
    good = Good()
    for e in _trades(60):
        dispatch_market([good, boom, good], e)
    assert good.n == 120, f"good sleeve stopped being called: {good.n}"
    assert boom.n < 60, (
        f"failing sleeve was called {boom.n} times in 60 events -- never disabled")


# ── engine level: the behaviour that actually cost the positions ─────────────

def test_engine_keeps_trading_and_still_flattens_after_a_sleeve_dies():
    """The whole point. With one sleeve raising on every event, the engine must
    process the full tape and the healthy sleeve must still be filled."""
    good, boom = Good(), Exploder()

    async def go():
        eng = LiveEngine(ListFeed(_trades(20)), NullBroker(), [good, boom],
                         EventClock(), Blotter("ES", 50.0),
                         warmup_gate=False, live_owners=set())
        await eng.run()
        return eng

    eng = asyncio.run(go())
    assert good.n == 20, f"healthy sleeve saw {good.n} of 20 events"
    # every trade the healthy sleeve ordered on must have filled. The engine may
    # add a session flat on top (BASE lands at 17:13 ET, inside the 15:59-18:00
    # closing window), so count the sleeve's own fills rather than the total.
    mine = [f for f in eng.paper_fills if f.tag == "good"]
    assert len(mine) == 20, (
        f"only {len(mine)} of 20 fills for the healthy sleeve; a broken sleeve "
        f"stopped the book")


def test_observer_first_does_not_mask_the_failure():
    """run_live builds `strategies = [obs] + roster`, so obs.trades kept climbing
    while everything behind it was dead -- the terminal looked healthy all
    session. Whatever the ordering, the sleeves behind must still run."""
    obs, boom, good = Good("obs"), Exploder(), Good("real")
    for e in _trades(5):
        dispatch_market([obs, boom, good], e)
    assert obs.n == 5
    assert good.n == 5, (
        f"observer counted {obs.n} events while the real sleeve saw {good.n}")
