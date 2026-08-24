"""The session flat under the PRODUCTION warmup gate.

tests/test_session_boundary.py proves the flat works with warmup_gate=False,
which makes LiveEngine._lane_live() unconditionally True. Live runs with
warmup_gate=True, where the flat at live_engine.py:899 is conditioned on
`lane_live` -- so the passing test does not cover the path the engine uses.

DIAGNOSED 2026-08-25, and the flat logic was NOT the fault. 80 sleeve-days in
the live record show a genuinely-intraday sleeve still holding at the close,
while the same sessions replayed leave ZERO open. Three unrelated causes:

  21  `restart-void` accounting entries counted as trades by the analysis, not
      by the engine -- they void a position inherited across a restart and have
      no same-day counterpart.
  21  2026-08-04, the session that died at 11:10, plus the 2026-08-05 WAL fault
      where claude_paper_fills stored 11 of 56 writes (see
      engine/adapters/paper_blotter.py).
  the rest  THE DISPATCH BACKLOG. The flat is driven by EVENT timestamps, so an
      engine running behind never processes the 15:59 events at all. On
      2026-08-17: "ENGINE 245.2 MINUTES BEHIND ... dispatch queue 47991" and
      zero session flats that day, while the process was alive throughout.

So these tests pin the mechanism under the production gate, which the existing
suite did not cover. They cannot catch the backlog -- that needs the throughput
work, and is the actual fix. (ibs and rsi2 are excluded from the counts above:
they declare holds_overnight=True and are supposed to carry.)
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.blotter import Blotter          # noqa: E402
from engine.core.clock import WallClock          # noqa: E402
from engine.core.events import Bar, Trade        # noqa: E402
from engine.core.live_engine import LiveEngine   # noqa: E402
from engine.core.orders import Order             # noqa: E402
from tests.test_session_boundary import _ns_at_et  # noqa: E402


class Forgetful:
    """Enters once and never exits."""
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


class NullBroker:
    finite = True

    async def submit(self, o):
        return None

    async def events(self):
        return
        yield


def _run(ev):
    s = Forgetful()
    eng = LiveEngine(ListFeed(ev), NullBroker(), [s], WallClock(),
                     Blotter("ES", 50.0), warmup_gate=True, live_owners=set())
    asyncio.run(eng.run())
    return eng, s


def _bar(y, mo, d, h, mi, px=7500.0):
    return Bar(_ns_at_et(y, mo, d, h, mi), "1m", px, px, px, px, 100, "ES")


def test_flat_fires_when_only_BARS_arrive_after_the_close():
    """A lane goes live on a trade in the morning, then the tape thins to bars
    by the close. 62 ES bars arrived in the 15:59-18:00 window on 2026-07-28
    and the position still rode overnight, so this is the shape to pin."""
    ev = [Trade(_ns_at_et(2026, 7, 31, 10, 0), 7500.0, 1, 1, symbol="ES"),
          _bar(2026, 7, 31, 15, 59),
          _bar(2026, 7, 31, 16, 5)]
    eng, s = _run(ev)
    assert eng.strategy_position(s) == 0, \
        "intraday sleeve carried a position overnight when only bars followed"


def test_flat_fires_when_a_trade_arrives_after_the_close():
    ev = [Trade(_ns_at_et(2026, 7, 31, 10, 0), 7500.0, 1, 1, symbol="ES"),
          Trade(_ns_at_et(2026, 7, 31, 16, 5), 7490.0, 1, 1, symbol="ES")]
    eng, s = _run(ev)
    assert eng.strategy_position(s) == 0
