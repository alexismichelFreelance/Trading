"""LIVE sleeve fills must be VISIBLE: a trade drawn as a trade, uncapped, on top.

The complaint this pins, in the user's words: "not some kind of faded whatever
label hidden behind, not some limited to 8 whatever arrows, I want to actually
see it."

Three separate defects were behind that, and each gets a test here:

  1. A trade was drawn as two unrelated arrows. Entry and exit were never
     connected, so nothing on the chart said "this was ONE trade, it made this
     much" -- you had to find two marks and do the arithmetic by eye.
  2. Every fill went through one arrow store capped at 80 and re-asserted on a
     4-minute bucket, shared with paper and manual fills. Ten paper sleeves
     firing all day evicted the handful of LIVE marks that actually matter.
  3. An OPEN position drew nothing at all between entry and exit. A trade that
     is still on is the one you most want to see.

Paper fills stay capped and muted ON PURPOSE -- they are the clutter, and the
whole point is that the live marks are not buried in them.
"""
from __future__ import annotations

import asyncio
import functools

import pandas as pd

from engine.core.events import Bar, Fill
from engine.painters import PaintController


class FakePainter:
    """Records the drawing calls instead of talking to NT8."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def arrow(self, tag, ts, price, direction, color="", label=""):
        self.calls.append(("arrow", tag, ts, price, direction, color, label))

    async def line(self, tag, t1, p1, t2, p2, color="", width=0, style=""):
        self.calls.append(("line", tag, t1, p1, t2, p2, color, width, style))

    async def text(self, tag, ts, price, label, color=""):
        self.calls.append(("text", tag, ts, price, label, color))

    async def rect(self, *a, **k):
        self.calls.append(("rect",))

    async def hline(self, *a, **k):
        self.calls.append(("hline",))

    async def status(self, *a, **k):
        self.calls.append(("status",))

    async def remove(self, tag):
        self.calls.append(("remove", tag))

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


def _ts(et_str: str) -> int:
    return int(pd.Timestamp(et_str, tz="America/New_York").value)


def _fill(ts, price, size, tag=""):
    return Fill(ts=ts, order_id=f"O{ts}", symbol="ES", price=price, size=size,
                commission=0.0, slippage=0.0, tag=tag)


def _pc(painter):
    return PaintController(painter, strategies=[], point_usd=50.0)


def sync(fn):
    """pytest-asyncio is not installed in this project; the convention is to
    drive coroutines with asyncio.run (see tests/test_panel_positions_only.py).
    This lets the test bodies stay `async def` and read normally."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        return asyncio.run(fn(*a, **k))
    return wrapper


@sync
async def test_a_closed_live_trade_is_drawn_as_one_line_entry_to_exit():
    p = FakePainter()
    pc = _pc(p)
    await pc.live_fill(_fill(_ts("2026-09-08 09:30"), 6000.0, -1, "entry-onfade"),
                       sleeve="ES:onfade")
    await pc.live_fill(_fill(_ts("2026-09-08 11:00"), 5985.0, +1, "onfade-target"),
                       sleeve="ES:onfade")
    lines = [c for c in p.of("line") if c[1].startswith("eng-trade-")]
    assert len(lines) == 1, "the round trip must be ONE line from entry to exit"
    t1, p1, t2, p2 = lines[0][2], lines[0][3], lines[0][4], lines[0][5]
    assert (p1, p2) == (6000.0, 5985.0)
    assert t1 < t2
    # SOLID and THICK. The overlay's default line is a 1px dash, which is the
    # "faded whatever" this whole change exists to stop drawing.
    assert lines[0][7] >= 3 and lines[0][8] == "solid"
    # and it must SAY what it made, in points and dollars, naming the sleeve
    txt = [c for c in p.of("text") if c[1].startswith("eng-trade")]
    assert txt, "a closed trade must carry a P&L label"
    label = txt[0][4]
    assert "onfade" in label and "+15" in label and "750" in label


@sync
async def test_a_losing_trade_is_coloured_differently_from_a_winner():
    p = FakePainter()
    pc = _pc(p)
    await pc.live_fill(_fill(_ts("2026-09-08 09:30"), 6000.0, +1, "e"), sleeve="ES:pivot")
    await pc.live_fill(_fill(_ts("2026-09-08 10:00"), 5990.0, -1, "x"), sleeve="ES:pivot")
    win = _ts("2026-09-08 09:30")
    loss_line = [c for c in p.of("line") if c[1].startswith("eng-trade-")][0]
    p2 = _pc(FakePainter())
    assert loss_line[6] != "", "a trade line must be coloured"
    # a winner and a loser must not share a colour
    q = FakePainter()
    pcw = _pc(q)
    await pcw.live_fill(_fill(win, 6000.0, +1, "e"), sleeve="ES:pivot")
    await pcw.live_fill(_fill(_ts("2026-09-08 10:00"), 6010.0, -1, "x"), sleeve="ES:pivot")
    win_line = [c for c in q.of("line") if c[1].startswith("eng-trade-")][0]
    assert win_line[6] != loss_line[6]


@sync
async def test_an_open_live_position_is_drawn_while_it_is_still_on():
    """THE ONE THAT MATTERED MOST: a trade you are in must be visible NOW."""
    p = FakePainter()
    pc = _pc(p)
    await pc.live_fill(_fill(_ts("2026-09-08 09:30"), 6000.0, +1, "e"), sleeve="ES:zones")
    bar = Bar(_ts("2026-09-08 09:45"), "1m", 6004, 6006, 6003, 6005.0, 100, "ES")
    await pc.paint_open_trades(bar)
    live = [c for c in p.of("line") if "open" in c[1]]
    assert live, "an open position must draw a line from its entry to now"
    assert live[-1][3] == 6000.0 and live[-1][5] == 6005.0
    # and it must disappear once the position is closed
    p.calls.clear()
    await pc.live_fill(_fill(_ts("2026-09-08 10:00"), 6010.0, -1, "x"), sleeve="ES:zones")
    assert any(c[0] == "remove" and "open" in c[1] for c in p.calls)
    await pc.paint_open_trades(bar)
    assert not [c for c in p.of("line") if "open" in c[1]]


@sync
async def test_live_fills_are_not_capped_the_way_paper_fills_are():
    """40 live fills = 40 marks. Paper fills stay capped; that is the point."""
    p = FakePainter()
    pc = _pc(p)
    t0 = _ts("2026-09-08 09:30")
    for i in range(40):
        await pc.live_fill(_fill(t0 + i * 60 * 10**9, 6000.0 + i, 1 if i % 2 == 0 else -1,
                                 "e"), sleeve="ES:zones_gap")
    assert len([c for c in p.of("arrow") if c[1].startswith("eng-fill-")]) == 40

    q = FakePainter()
    pcp = _pc(q)
    for i in range(40):
        await pcp.paper_fill(_fill(t0 + i * 60 * 10**9, 6000.0 + i, 1, "trendjoin-entry"))
    assert len(q.of("arrow")) < 40, "paper fills must still be capped"


@sync
async def test_live_marks_are_not_evicted_by_a_flood_of_paper_fills():
    """Defect 2: one shared store of 80 meant paper traffic pushed the live
    marks out of the set that gets re-asserted above the zones."""
    p = FakePainter()
    pc = _pc(p)
    t0 = _ts("2026-09-08 09:30")
    await pc.live_fill(_fill(t0, 6000.0, +1, "e"), sleeve="ES:onfade")
    for i in range(200):
        await pc.paper_fill(_fill(t0 + i * 10**9, 6000.0, 1, f"sleeve{i % 12}-entry"))
    kept = " ".join(lbl for (_, _, _, _, lbl) in pc._live_arrows.values())
    assert "onfade" in kept, "the live mark must survive any amount of paper traffic"


@sync
async def test_target_and_stop_rails_are_drawn_for_an_open_position():
    """He asked to see, for a live trade: where it entered, where it exited, and
    the target and stop. Entry/exit are the arrows and the connector; target and
    stop come from the sleeve's declared chart_marks()."""
    class FakeSleeve:
        label = "ES:onfade"
        pos = -1
        trade = {"side": -1, "entry": 6000.0, "target": 5985.0, "stop": 6022.5}

        def chart_marks(self):
            from engine.strategies.base import BaseStrategy
            return BaseStrategy.chart_marks(self)

    p = FakePainter()
    pc = PaintController(p, strategies=[FakeSleeve()], point_usd=50.0)
    await pc.live_fill(_fill(_ts("2026-09-08 09:30"), 6000.0, -1, "entry-onfade"),
                       sleeve="ES:onfade")
    bar = Bar(_ts("2026-09-08 10:00"), "1m", 5996, 5997, 5994, 5995.0, 100, "ES")
    await pc.paint_open_trades(bar)
    drawn = {c[1] for c in p.of("line")}
    assert "eng-entry-onfade" in drawn
    assert "eng-target-onfade" in drawn
    assert "eng-stop-onfade" in drawn
    prices = {c[1]: c[3] for c in p.of("line")}
    assert prices["eng-target-onfade"] == 5985.0
    assert prices["eng-stop-onfade"] == 6022.5
    labels = " ".join(c[4] for c in p.of("text"))
    assert "TARGET 5985.00" in labels and "STOP 6022.50" in labels
    # and every rail is torn down when the position closes
    p.calls.clear()
    await pc.live_fill(_fill(_ts("2026-09-08 10:30"), 5985.0, +1, "onfade-target"),
                       sleeve="ES:onfade")
    removed = {c[1] for c in p.calls if c[0] == "remove"}
    assert {"eng-target-onfade", "eng-stop-onfade", "eng-entry-onfade"} <= removed


@sync
async def test_a_sleeve_without_chart_marks_still_draws_its_trade():
    """No guessing: a sleeve that declares nothing simply gets fewer rails."""
    class Bare:
        label = "ES:zones"
        pos = 1

    p = FakePainter()
    pc = PaintController(p, strategies=[Bare()], point_usd=50.0)
    await pc.live_fill(_fill(_ts("2026-09-08 09:30"), 6000.0, +1, "e"), sleeve="ES:zones")
    await pc.paint_open_trades(Bar(_ts("2026-09-08 09:45"), "1m", 6004, 6006, 6003,
                                   6005.0, 100, "ES"))
    drawn = {c[1] for c in p.of("line")}
    assert "eng-open-zones" in drawn          # the trade still shows
    assert "eng-target-zones" not in drawn    # but nothing is invented


def test_chart_marks_reads_every_trade_shape_in_the_roster():
    """The four sleeves routed live store their working levels three different
    ways. chart_marks() has to read all of them or the rails silently vanish for
    whichever sleeve is actually in the market.

      pivot, overnight_fade   `trade` is a DICT of prices
      zones_strategy          `trade` is a _Trade DATACLASS, same field names
      (others)                nothing -- and must return {} rather than guess
    """
    from dataclasses import dataclass

    from engine.strategies.base import BaseStrategy

    class DictSleeve(BaseStrategy):
        trade = {"dir": -1, "entry": 6000.0, "target": 5985.0, "stop": 6022.5}

    @dataclass
    class _Trade:
        setup: str = "z"
        dir: int = 1
        entry: float = 5900.0
        stop: float = 5890.0
        target: float = 5930.0

    class DataclassSleeve(BaseStrategy):
        trade = _Trade()

    class BareSleeve(BaseStrategy):
        pass

    assert DictSleeve().chart_marks() == {"entry": 6000.0, "target": 5985.0,
                                          "stop": 6022.5}
    assert DataclassSleeve().chart_marks() == {"entry": 5900.0, "target": 5930.0,
                                               "stop": 5890.0}
    assert BareSleeve().chart_marks() == {}


@sync
async def test_rails_are_not_redrawn_on_every_bar():
    """Redraw volume is how this chart was made unusable before. The rails are
    static for the life of the trade, so they refresh on the 4-minute bucket;
    only the entry->now connector runs per bar."""
    class Sleeve:
        label = "ES:onfade"
        pos = -1
        trade = {"entry": 6000.0, "target": 5985.0, "stop": 6022.5}

        def chart_marks(self):
            from engine.strategies.base import BaseStrategy
            return BaseStrategy.chart_marks(self)

    p = FakePainter()
    pc = PaintController(p, strategies=[Sleeve()], point_usd=50.0)
    t0 = _ts("2026-09-08 09:30")
    await pc.live_fill(_fill(t0, 6000.0, -1, "e"), sleeve="ES:onfade")
    for i in range(1, 13):                     # twelve consecutive 1-minute bars
        await pc.paint_open_trades(Bar(t0 + i * 60 * 10**9, "1m", 5999, 6000,
                                       5998, 5999.0, 10, "ES"))
    rails = [c for c in p.of("line") if c[1].startswith("eng-target-")]
    conn = [c for c in p.of("line") if c[1].startswith("eng-open-")]
    assert len(conn) == 12, "the connector must be redrawn every bar"
    # 12 minutes spans three 4-minute buckets, so at most 4 rail redraws
    # (boundaries can fall either side of the first bar). The point is that it
    # is bucketed, not per-bar.
    assert len(rails) <= 4, f"the target rail redrew {len(rails)}x in 12 bars"


@sync
async def test_replaying_recorded_fills_rebuilds_the_days_marks():
    """A RESTART MUST NOT ERASE THE CHART.

    2026-09-07: "ALL markers of live sleeve trades disappeared" after a restart.
    Nothing was broken -- the marks live in PaintController's memory and a new
    process starts with none. The fills themselves are recorded, so the fix is to
    replay them through the same live_fill() path on startup. This pins that a
    replayed round trip produces the identical closed-trade line, so the chart
    after a restart matches the chart before it."""
    def build(fills):
        p = FakePainter()
        pc = _pc(p)
        return p, pc

    seq = [(_ts("2026-09-08 09:30"), 6000.0, -1, "entry-onfade"),
           (_ts("2026-09-08 11:00"), 5985.0, +1, "onfade-target")]

    live_p, live_pc = build(seq)
    for ts, px, sz, tag in seq:                     # as the session drew it
        await live_pc.live_fill(_fill(ts, px, sz, tag), sleeve="ES:onfade")

    replay_p, replay_pc = build(seq)
    for ts, px, sz, tag in seq:                     # as a restart replays it
        await replay_pc.live_fill(_fill(ts, px, sz, tag), sleeve="ES:onfade")

    def trade_lines(p):
        return [(c[2], c[3], c[4], c[5], c[6]) for c in p.of("line")
                if c[1].startswith("eng-trade-")]

    assert trade_lines(replay_p) == trade_lines(live_p) != []
    live_lbl = [c[4] for c in live_p.of("text") if c[1].startswith("eng-trade")]
    replay_lbl = [c[4] for c in replay_p.of("text") if c[1].startswith("eng-trade")]
    assert replay_lbl == live_lbl


def test_a_live_fill_is_labelled_with_the_orders_tag_not_the_order_id():
    """NT8 echoes the order id back as the fill's tag, so a live fill arrives as
    tag='O37'. Every chart mark and every recorded row built from it then said
    "O37" instead of "entry-onfade". Paper fills are built in-process and keep
    the real tag, which is why this only ever showed on the LIVE path -- the one
    nobody could check until sleeves were actually routed."""
    from engine.core.orders import Order

    class Eng:
        _order_tag: dict = {}

        def tag_of(self, f):
            from engine.core.live_engine import LiveEngine
            return LiveEngine.tag_of(self, f)

    def fill(order_id, tag):
        return Fill(ts=1, order_id=order_id, symbol="ES", price=7715.25, size=1,
                    commission=0.0, slippage=0.0, tag=tag)

    e = Eng()
    o = Order("ES", 1, 1, tag="entry-onfade", order_id="O37")
    e._order_tag[o.order_id] = o.tag
    # this is the real shape: NT8 hands back tag == order_id
    assert e.tag_of(fill("O37", "O37")) == "entry-onfade"
    # a manual/external fill is not ours; keep whatever it carried
    assert e.tag_of(fill("", "")) == ""
