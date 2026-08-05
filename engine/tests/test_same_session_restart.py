"""A restart INSIDE a live session must not void live positions.

2026-08-05: the engine was stopped at 11:00 ET with positions open and restarted
at 12:25 ET — same session, market open, positions an hour old and real. The
restore path treated that identically to picking up a three-week-old orphan and
voided every one of them at entry.

Voiding is right for a position the engine abandoned in a session that has since
CLOSED: the engine's own 15:59 flat would have taken it out, whatever the record
shows is fiction, and marking it to any price invents a P&L (see
tests/test_restart_orphans.py).

It is wrong for a position that is still live. That position really is open, the
market really is trading, and an engine that resumes and cannot manage it is
really closing it right now. That is an ordinary fill at the market, not an
accounting correction — so it books its actual P&L and is tagged `restart-flat`,
distinct from `restart-void`.

The runner knows which case it is: session_close_px() returns None precisely when
the last fill is in the session still running.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.blotter import Blotter  # noqa: E402
from engine.core.clock import EventClock  # noqa: E402
from engine.core.events import BUY, Trade  # noqa: E402
from engine.core.live_engine import LiveEngine  # noqa: E402

NS = 1_000_000_000
NOW = 1_785_949_200 * NS          # 2026-08-05 13:00 ET, inside RTH


class _Sleeve:
    symbol = "ES"
    holds_overnight = False

    def __init__(self):
        self.pos = 0

    def on_trade(self, e):
        return []

    def on_bar(self, e):
        return []

    def on_fill(self, f):
        return None

    def on_position(self, p):
        return None

    def restore_state(self, pos, avg_px):
        return False


class _Feed:
    finite = True

    def __init__(self, evs):
        self._evs = evs

    async def stream(self):
        for e in self._evs:
            yield e


class _Broker:
    async def submit(self, o):
        return None

    async def events(self):
        return
        yield


def _run(s, spec, px=7123.75):
    async def go():
        eng = LiveEngine(_Feed([Trade(NOW, px, 1, BUY, "ES")]), _Broker(), [s],
                         EventClock(), Blotter("ES", 50.0), live_owners=set())
        eng.restore_paper_position(s, *spec)
        await eng.run()
        return eng

    return asyncio.run(go())


def test_same_session_position_is_flattened_at_the_market_not_voided():
    """THE REGRESSION. Stopped at 11:00 ET, restarted at 12:25 ET, market open:
    the position is live and its close is a real fill at a real price."""
    s = _Sleeve()
    eng = _run(s, (1, 7000.0), px=7123.75)          # same_session defaults on
    f = [f for f in eng.paper_fills if (f.tag or "").startswith("restart-")][0]
    assert f.tag == "restart-flat", f"live position tagged {f.tag!r}"
    assert f.price == 7123.75, (
        f"closed at {f.price} -- a position that was genuinely open across an "
        f"intra-session gap is closed at the market, not voided at its entry")


def test_finished_session_position_is_still_voided():
    """The cross-session case is unchanged: no invented P&L."""
    s = _Sleeve()
    eng = _run(s, (2, 28980.06, 29866.50), px=30000.0)
    f = [f for f in eng.paper_fills if (f.tag or "").startswith("restart-")][0]
    assert f.tag == "restart-void"
    assert f.price == 28980.06, "a closed session's orphan was marked to market"


def test_the_two_cases_are_distinguishable_in_the_record():
    """A scorecard must be able to keep one and drop the other: `restart-flat`
    is a real trade, `restart-void` is an accounting entry."""
    assert "restart-flat" != "restart-void"
    a, b = _Sleeve(), _Sleeve()
    tags = {_run(a, (1, 7000.0)).paper_fills[-1].tag,
            _run(b, (1, 7000.0, 7050.0)).paper_fills[-1].tag}
    assert tags == {"restart-flat", "restart-void"}, tags


def test_both_cases_still_reconcile_to_flat():
    for spec in ((3, 7000.0), (3, 7000.0, 7050.0)):
        s = _Sleeve()
        eng = _run(s, spec)
        closes = [f for f in eng.paper_fills if (f.tag or "").startswith("restart-")]
        assert sum(f.size for f in closes) == -3, f"{spec} did not flatten"
