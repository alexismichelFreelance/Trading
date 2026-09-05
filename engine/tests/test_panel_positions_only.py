"""The chart panel names open positions, not the whole roster.

With 40 sleeves per lane the panel was ~40 lines of "+0" and the one sleeve
actually in the market was buried in it. A panel that has to be READ to find
the signal is not a panel.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.painters import PaintController          # noqa: E402


class FakePainter:
    def __init__(self): self.text = ""
    async def status(self, text, pos=None): self.text = text


class Sleeve:
    def __init__(self, pos=0, entry_px=None):
        self.pos = pos
        if entry_px is not None:
            self.entry_px = entry_px


def render(strategies, close=5000.0):
    p = FakePainter()
    pc = PaintController(p, strategies)
    asyncio.run(pc._paint_status(live=True, backfill_bars=0, close=close))
    return p.text


def test_flat_sleeves_are_counted_not_listed():
    txt = render([Sleeve() for _ in range(40)])
    assert "40 sleeves armed" in txt
    assert "flat" in txt
    assert txt.count("Sleeve") == 0


def test_open_positions_are_named_with_entry_and_open_pnl():
    txt = render([Sleeve(), Sleeve(pos=2, entry_px=4990.0), Sleeve()],
                 close=5000.0)
    assert "Sleeve" in txt
    assert "+2" in txt and "@4990.00" in txt
    assert "+10.00" in txt              # open P&L on a long from 4990 at 5000
    assert "(2 armed)" in txt


def test_short_open_pnl_has_the_right_sign():
    txt = render([Sleeve(pos=-1, entry_px=5010.0)], close=5000.0)
    assert "+10.00" in txt
