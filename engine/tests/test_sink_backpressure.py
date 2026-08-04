"""A slow observer must not be able to stop the engine trading.

2026-07-30, live: paper fills stopped at 13:00:50 ET and never resumed, while
claude_bars_live / claude_ticks_live / claude_sec_live / claude_depth_live all
kept advancing normally until the engine was shut down at ~16:35. Replaying that
same captured tape through the same engine produced the full day's fills,
including the 15:59 MOC flattens the live run never fired -- so the strategies,
the exits and the stale-event guard were all fine, and the difference was
live-only.

The trigger that day is NOT established. This test does not claim to reproduce
it. What it pins is the STRUCTURAL defect that makes that whole failure mode
possible: observer callbacks (chart painting, the QuestDB paper blotter) are
awaited inside the dispatch loop, so anything slow behind one of them applies
backpressure to trading itself -- and because recording happens on the feed pump
task, the engine goes on looking perfectly healthy while it silently stops
trading. A logging dependency must never be able to do that.

The slow sink here is a stand-in for a stalled QuestDB insert or an NT8 draw
socket that stopped reading. It blocks until the test releases it, which is just
the limiting case of "slow".
"""
from __future__ import annotations

import asyncio

import pytest

from engine.core.blotter import Blotter
from engine.core.clock import WallClock
from engine.core.events import Trade
from engine.core.live_engine import LiveEngine
from engine.core.orders import Order

NS = 1_000_000_000
N_TRADES = 6


def _mid_session() -> int:
    """A fixed 11:00 ET instant, well inside RTH.

    NOT time.time_ns(). The engine flattens intraday sleeves during the
    15:59-18:00 ET closing window, so a test anchored to "now" silently gains an
    extra flatten fill when it happens to run in that window -- it would pass or
    fail by time of day. Anchoring makes it deterministic.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return int(datetime(2026, 7, 31, 11, 0,
                        tzinfo=ZoneInfo("America/New_York")).timestamp() * 1e9)



class Sleeve:
    """Orders on every trade, so each event should produce one paper fill."""
    symbol = "ES"

    def __init__(self) -> None:
        self.seen = 0

    def on_trade(self, t):
        self.seen += 1
        return [Order("ES", 1 if self.seen % 2 else -1, 1, tag=f"t{self.seen}")]

    def on_fill(self, f):
        return None

    def on_position(self, p):
        return None


class ListFeed:
    finite = True

    def __init__(self, events):
        self._ev = events

    async def stream(self):
        for e in self._ev:
            yield e


class NullBroker:
    finite = True

    async def submit(self, o):
        return None

    async def events(self):
        return
        yield


def _run_with_stalled_sink(budget_s: float = 2.0):
    """Feed N trades while on_paper_fill is stuck. Returns what the engine
    managed to do while the observer was blocked."""
    base = _mid_session()
    ev = [Trade(base + i * NS, 7400.0 + i, 1, 1, symbol="ES") for i in range(N_TRADES)]
    strat = Sleeve()

    async def go():
        eng = LiveEngine(ListFeed(ev), NullBroker(), [strat], WallClock(),
                         Blotter("ES", 50.0), warmup_gate=False, live_owners=set())
        released = asyncio.Event()
        entered = asyncio.Event()

        async def stalled_sink(f):
            entered.set()                 # the sink got the first fill...
            await released.wait()         # ...and then never returns

        eng.on_paper_fill = stalled_sink
        task = asyncio.create_task(eng.run())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget_s
        # give the engine every chance to finish the tape while the sink hangs
        while loop.time() < deadline and eng._processed < N_TRADES:
            await asyncio.sleep(0.01)
        got = {"processed": eng._processed, "fills": len(eng.paper_fills),
               "seen": strat.seen, "sink_entered": entered.is_set()}
        released.set()                    # let everything unwind
        eng._stop.set()
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:      # pragma: no cover - shutdown safety
            task.cancel()
        return got

    return asyncio.run(go())


def test_stalled_observer_does_not_stop_trading():
    """THE REPRODUCTION. On the broken engine the dispatch loop awaits
    on_paper_fill, so the first fill's stuck sink freezes everything: the tape
    stops being consumed and no further orders are placed. Trading must be
    independent of an observer."""
    got = _run_with_stalled_sink()
    assert got["sink_entered"], "the sink never ran; test did not exercise the path"
    assert got["processed"] == N_TRADES, (
        f"dispatch stalled behind a slow observer: only {got['processed']} of "
        f"{N_TRADES} events dispatched")
    assert got["seen"] == N_TRADES, (
        f"strategies stopped receiving events: {got['seen']} of {N_TRADES}")
    assert got["fills"] == N_TRADES, (
        f"only {got['fills']} of {N_TRADES} fills made it while the sink hung")


def test_healthy_sink_still_receives_everything():
    """The fix must not silently drop work under normal conditions."""
    base = _mid_session()
    ev = [Trade(base + i * NS, 7400.0 + i, 1, 1, symbol="ES") for i in range(N_TRADES)]
    strat = Sleeve()
    seen = []

    async def go():
        eng = LiveEngine(ListFeed(ev), NullBroker(), [strat], WallClock(),
                         Blotter("ES", 50.0), warmup_gate=False, live_owners=set())

        async def sink(f):
            seen.append(f)

        eng.on_paper_fill = sink
        await eng.run()
        return eng

    eng = asyncio.run(go())
    assert len(eng.paper_fills) == N_TRADES
    assert len(seen) == N_TRADES, f"observer lost fills: {len(seen)}/{N_TRADES}"


@pytest.mark.parametrize("cb", ["on_live_fill", "on_bar_hook", "on_warmup_signal"])
def test_every_observer_hook_is_off_the_hot_path(cb):
    """Not just on_paper_fill: every observer hook is wired to something that
    touches a socket or the DB in run_live, so none of them may be awaited by the
    dispatch loop."""
    import inspect

    from engine.core import live_engine
    src = inspect.getsource(live_engine.LiveEngine)
    assert f"await self.{cb}(" not in src, (
        f"{cb} is awaited inside LiveEngine; a slow {cb} can stall trading")
