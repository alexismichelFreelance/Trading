"""A dead leg inside a multi-instrument facade must not look like a healthy one.

MergeFeed and SymbolRouterBroker each wrap N children behind a single-child
interface. Both were written to keep going when one child's stream ends:
MergeFeed pops that leg off the heap and carries on with the rest, the router
counts a DONE and keeps fanning in the others.

For a bounded replay that is exactly right — legs finishing is how a replay
ends. For a LIVE feed it is the silent-death pattern that has now cost two
sessions. LiveEngine only reconnects when `stream()` / `events()` RETURNS. A
facade that outlives its dead child never returns, so:

  * the NQ socket drops,
  * MergeFeed keeps yielding ES events,
  * LiveEngine sees a healthy stream and never reconnects,
  * NQ is gone for the rest of the session and every counter still climbs.

Same fault as the raw-capture writer and the swallowed QuestDB writes: the
component reports activity, not the work it was built to do. So the rule is:
when the merge is not finite, one leg ending ends the stream, loudly, and the
reconnect loop above rebuilds every leg.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.brokers.router import SymbolRouterBroker  # noqa: E402
from engine.adapters.feeds.merge import MergeFeed  # noqa: E402
from engine.core.events import BUY, Fill, Trade  # noqa: E402

NS = 1_000_000_000


class _Leg:
    """A feed leg. `finite` mirrors a bounded replay feed vs a live socket."""

    def __init__(self, sym, n, finite=False, start=0):
        self.symbol, self.n, self.finite, self.start = sym, n, finite, start
        self.runs = 0

    async def stream(self):
        self.runs += 1
        for i in range(self.n):
            await asyncio.sleep(0)
            yield Trade((self.start + i) * NS, 100.0 + i, 1, BUY, self.symbol)


class _BrokerLeg:
    def __init__(self, sym, n):
        self.symbol, self.n = sym, n
        self.runs = 0

    async def events(self):
        self.runs += 1
        for i in range(self.n):
            await asyncio.sleep(0)
            yield Fill(i * NS, f"{self.symbol}-{i}", self.symbol, 100.0, 1, 0.0, 0.0, "t")

    def on_market_event(self, e):
        pass


async def _take(agen, limit=200):
    out = []
    async for e in agen:
        out.append(e)
        if len(out) >= limit:
            break
    return out


# ── MergeFeed ────────────────────────────────────────────────────────────────

def test_live_merge_ends_when_a_leg_dies_instead_of_serving_the_survivor():
    """THE REGRESSION. A short-lived NQ leg must not leave ES streaming on
    alone under a facade that never returns."""
    short = _Leg("NQ", 2)            # socket drops after 2 events
    long_ = _Leg("ES", 500)          # still healthy
    out = asyncio.run(_take(MergeFeed([short, long_]).stream()))
    assert len(out) < 100, (
        f"merge yielded {len(out)} events after a leg died -- it kept serving "
        f"the survivor and LiveEngine never saw a disconnect")
    assert {e.symbol for e in out} <= {"ES", "NQ"}


def test_live_merge_says_which_leg_died(caplog):
    with caplog.at_level(logging.WARNING, logger="engine.merge"):
        asyncio.run(_take(MergeFeed([_Leg("NQ", 1), _Leg("ES", 500)]).stream()))
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "NQ" in msg, f"the dead leg was not named: {msg}"


def test_bounded_replay_still_drains_every_leg_to_the_end():
    """The parity path must be untouched: finite legs finishing is not a fault,
    and every event from every leg must still come out in ts order."""
    a = _Leg("ES", 3, finite=True, start=0)
    b = _Leg("NQ", 5, finite=True, start=0)
    out = asyncio.run(_take(MergeFeed([a, b]).stream()))
    assert len(out) == 8, f"bounded merge dropped events: {len(out)}"
    assert [e.ts for e in out] == sorted(e.ts for e in out), "ts order broken"


def test_merge_can_be_re_entered_after_a_leg_died():
    """LiveEngine's reconnect calls stream() again -- every leg must restart."""
    legs = [_Leg("NQ", 2), _Leg("ES", 100)]
    m = MergeFeed(legs)
    asyncio.run(_take(m.stream()))
    asyncio.run(_take(m.stream()))
    assert all(l.runs == 2 for l in legs), (
        f"legs restarted {[l.runs for l in legs]} times; a reconnect must "
        f"rebuild every leg")


# ── SymbolRouterBroker ───────────────────────────────────────────────────────

def test_live_router_ends_when_a_broker_leg_dies():
    """Same rule on the broker side: a dead NQ broker must surface as a
    disconnect, not be hidden behind a still-flowing ES broker."""
    r = SymbolRouterBroker({"NQ": _BrokerLeg("NQ", 2), "ES": _BrokerLeg("ES", 500)})
    out = asyncio.run(_take(r.events()))
    assert len(out) < 100, (
        f"router fanned in {len(out)} events after a broker leg died -- the "
        f"dead leg was invisible to LiveEngine's reconnect loop")


def test_router_can_be_re_entered():
    legs = {"ES": _BrokerLeg("ES", 3), "NQ": _BrokerLeg("NQ", 3)}
    r = SymbolRouterBroker(legs)
    asyncio.run(_take(r.events()))
    asyncio.run(_take(r.events()))
    assert all(b.runs == 2 for b in legs.values()), (
        f"broker legs restarted {[b.runs for b in legs.values()]} times")
