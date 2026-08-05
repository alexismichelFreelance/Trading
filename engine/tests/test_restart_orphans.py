"""An open position the engine will not manage must be CLOSED in the record.

On restart the runner rebuilds each sleeve's open paper position from
claude_paper_fills and offers it back via restore_paper_position(). A sleeve
that cannot resume from just (pos, avg_px) — every intraday sleeve, which needs
its entry ts, stop and MFE — correctly declines. The engine then goes flat.

But nothing closed the position in the TABLE. So claude_paper_fills kept showing
an entry with no exit, forever:

    2026-08-05 10:31  open paper position for OpenDriveStrategy  (+2 @ 29003.56)
                      NOT restorable — left flat, orphaned in claude_paper_fills
    ... x21

Twenty-one of twenty-four positions from the session that died at 11:10 the day
before. Every downstream number that sums fills — the daily scorecard, per-sleeve
P&L, the promotion gate — then reads a position that never closed. The engine
thinks flat, the record thinks long, and the two never reconcile. That is the
phantom.

Declining to HOLD an unmanageable position is right; a sleeve that cannot exit
should not be left carrying risk. Declining to ACCOUNT for it is not. So an
unrestorable position is VOIDED: closed in the record at its own entry price,
booking exactly zero P&L.

Not marked to the market, and this is the important part. An abandoned position
is not evidence about the sleeve -- the engine was dead and never managed the
exit. Marking it to any market price invents a result and writes it into
claude_paper_fills, which is what scorecard.py and promotion_check.py read to
decide what earns real money; neither reads the tag, so every invented point
would count. Measured on the real record: 30 orphans back to 2026-07-17, worth
+8159 points (~$180k) if marked to their session closes. That is not a P&L, it
is a bug with a number attached. What the position would have made is logged,
where a human weighs it, and nowhere else.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.blotter import Blotter  # noqa: E402
from engine.core.clock import EventClock  # noqa: E402
from engine.core.events import BUY, Trade  # noqa: E402
from engine.core.live_engine import LiveEngine  # noqa: E402

NS = 1_000_000_000
# 2026-08-05 13:00:00 ET == 17:00 UTC — inside RTH, so "now" is a live session
NOW = 1_785_949_200 * NS


class _Sleeve:
    """An intraday sleeve: cannot resume from (pos, avg_px), like all of them."""
    symbol = "ES"
    holds_overnight = False

    def __init__(self, label="opendrive"):
        self.label = label
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
        return False                      # honest: needs richer entry state


class _Resumable(_Sleeve):
    def restore_state(self, pos, avg_px):
        self.pos = pos
        return True


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


def _run(sleeves, restores, evs=None):
    """Drive a real warmup->live flip: restores are applied there and nowhere
    else, so a test that bypasses the gate would prove nothing about production.
    The first live Trade is what flips the lane."""
    evs = [Trade(NOW, 7100.0, 1, BUY, "ES")] if evs is None else evs

    async def go():
        eng = LiveEngine(_Feed(evs), _Broker(), list(sleeves), EventClock(),
                         Blotter("ES", 50.0), live_owners=set())
        for s, spec in restores.items():
            eng.restore_paper_position(s, *spec)
        await eng.run()
        return eng

    return asyncio.run(go())


def test_unrestorable_position_is_closed_in_the_record():
    """THE REGRESSION. The sleeve declines, the engine goes flat — and a closing
    fill must appear so the table reconciles."""
    s = _Sleeve()
    eng = _run([s], {s: (2, 29003.56)})
    closes = [f for f in eng.paper_fills if "restart" in (f.tag or "")]
    assert closes, ("no closing fill for an unrestorable position -- it stays "
                    "open in claude_paper_fills forever")
    f = closes[0]
    assert f.size == -2, f"closing fill must flatten +2, got {f.size:+d}"
    assert f.symbol == "ES"


def test_the_book_nets_to_flat_after_the_close():
    """The whole point: entry + synthetic exit must sum to zero."""
    s = _Sleeve()
    eng = _run([s], {s: (-3, 7412.25)})
    net = -3 + sum(f.size for f in eng.paper_fills if "restart" in (f.tag or ""))
    assert net == 0, f"position did not reconcile to flat: net {net:+d}"


def test_a_resumable_sleeve_is_not_closed_out():
    """IBS and RSI2 hold overnight and CAN resume. Flattening them would destroy
    a live thesis — the close is only for positions nobody will manage."""
    s = _Resumable("ibs")
    eng = _run([s], {s: (1, 7340.25)})
    assert not [f for f in eng.paper_fills if "restart" in (f.tag or "")], (
        "a restorable sleeve's position was closed out from under it")
    assert s.pos == 1


def test_the_void_books_zero_pnl_even_when_the_market_moved():
    """The whole reason this is a void and not a mark-to-market: the sleeve gets
    neither credit nor blame for a move it was not there for."""
    s = _Sleeve()
    eng = _run([s], {s: (1, 7000.0)},
               evs=[Trade(NOW, 7123.75, 1, BUY, "ES")])
    f = [f for f in eng.paper_fills if "restart" in (f.tag or "")][0]
    assert f.price == 7000.0, (
        f"voided at {f.price} instead of the 7000.00 entry -- that books "
        f"{(7123.75 - 7000.0):+.2f} pts of invented P&L into the table that "
        f"gates real money")


def test_a_supplied_close_price_is_reported_but_never_booked(caplog):
    """The runner supplies the session close so the loss/gain that was abandoned
    can be SEEN. It must reach the log and not the record."""
    s = _Sleeve()
    with caplog.at_level(logging.WARNING, logger="engine.live"):
        eng = _run([s], {s: (2, 28980.06, 29866.50)})
    f = [f for f in eng.paper_fills if "restart" in (f.tag or "")][0]
    assert f.price == 28980.06, "a supplied close price was booked as P&L"
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "1772" in msg or "+1772" in msg, (
        f"what was abandoned was not reported at all: {msg}")


def test_no_mark_price_is_fine_because_the_entry_is_always_the_close(caplog):
    """With nothing to mark against, nothing changes: the void has always used
    the entry price."""
    s = _Sleeve()

    async def go():
        eng = LiveEngine(_Feed([]), _Broker(), [s], EventClock(),
                         Blotter("ES", 50.0), live_owners=set())
        await eng._close_orphan(s, 2, 6981.5, None)
        return eng

    with caplog.at_level(logging.WARNING, logger="engine.live"):
        eng = asyncio.run(go())
    closes = [f for f in eng.paper_fills if "restart" in (f.tag or "")]
    assert closes and closes[0].price == 6981.5, (
        f"closed at {closes[0].price if closes else None} -- not the entry")


def test_restores_never_applied_are_reported_not_silently_dropped(caplog):
    """A lane that never goes live (dead feed, engine stopped during warmup)
    never reaches _apply_restore, so every registered position was dropped on
    the floor without a word -- the same silent-loss class as the rest.

    It must NOT be closed out here: the engine never traded, so booking a
    synthetic exit would destroy a still-legitimate open position. The next
    start rebuilds it from the same table. It must be said out loud instead."""
    s = _Sleeve()
    with caplog.at_level(logging.WARNING, logger="engine.live"):
        eng = _run([s], {s: (2, 6981.5)}, evs=[])
    assert not [f for f in eng.paper_fills if "restart" in (f.tag or "")], (
        "an engine that never went live booked a synthetic exit anyway")
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "NEVER APPLIED" in msg, (
        f"registered restores vanished without a word: {msg}")


def test_the_close_is_tagged_so_it_is_never_read_as_a_strategy_exit():
    """A synthetic flatten is an accounting entry, not a trading decision. Every
    scorecard must be able to tell them apart."""
    s = _Sleeve()
    eng = _run([s], {s: (1, 7000.0)})
    f = [f for f in eng.paper_fills if "restart" in (f.tag or "")][0]
    assert f.tag == "restart-void", f"opaque tag {f.tag!r}"


def test_it_is_attributed_to_the_sleeve_that_held_it():
    """Orphan P&L belongs to whoever opened the position, or the scorecard is
    wrong in a new way."""
    a, b = _Sleeve("opendrive"), _Sleeve("vwapbreak")
    eng = _run([a, b], {b: (1, 7000.0)})
    assert eng.strategy_position(a) == 0
    assert eng.strategy_position(b) == 0, "the held sleeve did not end flat"
    closes = [f for f in eng.paper_fills if "restart" in (f.tag or "")]
    assert len(closes) == 1, f"expected one closing fill, got {len(closes)}"
