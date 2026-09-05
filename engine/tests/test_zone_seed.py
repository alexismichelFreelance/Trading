"""Zones must remember structure older than the engine process.

Fed only from the live stream, the 1h detector holds ~6 bars a session and the
4h holds one or two -- below the 20 a ZoneDetector needs before it can score a
departure at all. So a reversal level from last week could never become a zone.
Nothing expired; nothing was ever detected.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar                       # noqa: E402
from engine.painters import ZoneView                     # noqa: E402

NS = 1_000_000_000
OPEN_ET = 1_786_973_400          # a 09:30 ET boundary


def session(day: int, drive: bool, base: float = 5000.0):
    """One RTH session of 1m bars.

    The detector runs on AGGREGATED bars, so contrast has to exist at 30m/1h/4h,
    not at 1m. A QUIET session oscillates inside ~2 points, so every aggregated
    bar is narrow; a DRIVE session runs 40 points one way, so its aggregated
    bars are wide with a decisive body -- which is what a departure is.
    """
    t0 = OPEN_ET + day * 86_400
    out = []
    for i in range(390):
        if drive:
            o = base + i * (40.0 / 390)
            h, l, c = o + 0.6, o - 0.15, o + 0.5
        else:
            o = base + (i % 4) * 0.5
            h, l, c = o + 0.3, o - 0.3, o + 0.1
        out.append(Bar((t0 + (i + 1) * 60) * NS, "1m", o, h, l, c, 1000, "ES"))
    return out


def history(n_sessions: int):
    """Mostly quiet, with a drive session every fifth day."""
    bars = []
    for d in range(n_sessions):
        bars += session(d, drive=(d % 5 == 4))
    return bars


def test_without_seeding_the_slow_timeframes_never_detect():
    zv = ZoneView()
    for b in session(0, drive=True):
        zv.update(b)
    assert len(zv.active("4h")) == 0, "4h cannot warm inside one session"


def test_seeding_warms_the_slow_timeframes():
    zv = ZoneView()
    bars = history(30)
    zv.seed_intraday(bars)
    assert len(zv.book["30m"].zones) > 0
    assert len(zv.book["1h"].zones) > 0, "1h still cold after 30 sessions"
    assert len(zv.book["4h"].zones) > 0, "4h still cold after 30 sessions"


def test_seeding_is_deterministic_so_a_restart_rebuilds_the_book():
    bars = history(20)
    a, b = ZoneView(), ZoneView()
    a.seed_intraday(bars)
    b.seed_intraday(bars)                      # a "restart"
    for tf in ("30m", "1h", "4h"):
        assert [(z.top, z.bot, z.direction, z.broken) for z in a.book[tf].zones] == \
               [(z.top, z.bot, z.direction, z.broken) for z in b.book[tf].zones]


def test_seeded_zones_carry_older_structure_than_one_session():
    zv = ZoneView()
    bars = history(20)
    zv.seed_intraday(bars)
    newest = max(b.ts for b in bars)
    old = [z for z in zv.book["30m"].zones if z.formed_ts < newest - 5 * 86_400 * NS]
    assert old, "no zone survives from more than five sessions ago"


# ── the BOOK is uncapped; the CHART is not ──────────────────────────────────
# Seeding takes ES from ~8 unbroken zones to ~200. Drawing all of them, every
# four minutes, is how NT8 stops responding -- so the view draws the ones
# nearest price and the model keeps the rest.

class FakePainter:
    def __init__(self):
        self.rects, self.removed = set(), set()
    async def rect(self, tag, *a, **k): self.rects.add(tag)
    async def remove(self, tag): self.removed.add(tag); self.rects.discard(tag)
    async def text(self, *a, **k): pass
    async def hline(self, *a, **k): pass
    async def status(self, *a, **k): pass
    async def arrow(self, *a, **k): pass


def test_book_keeps_everything_but_the_chart_draws_a_subset():
    import asyncio
    from engine.painters import PaintController, ZONE_DRAW_PER_TF
    zv = ZoneView()
    zv.seed_intraday(history(40))
    total = sum(len([z for z in zv.book[tf].zones if not z.broken])
                for tf in ("30m", "1h", "4h"))
    assert total > ZONE_DRAW_PER_TF * 3, "not enough zones to exercise the cap"
    p = FakePainter()
    pc = PaintController(p, [])
    pc.zv = zv
    pc._last_px = 5000.0
    asyncio.run(pc._paint_zones(history(1)[-1].ts))
    drawn = len([t for t in p.rects if t.startswith("eng-zone-")])
    assert drawn <= ZONE_DRAW_PER_TF * len(("30m", "1h", "4h", "1d")), \
        f"drew {drawn} rectangles; the cap is {ZONE_DRAW_PER_TF} per timeframe"
    assert total > drawn, "the book should hold more than the chart shows"
