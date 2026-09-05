"""Zones must actually reach the LevelBook.

`Zone.virgin` is a @property. zones.py and painters.py both read it as
`z.virgin`; level_book.py called it as `z.virgin()` -- calling a bool, which
raises TypeError, which was swallowed by `except Exception: continue` under a
comment blaming "a zone mid-build". So EVERY zone was skipped, always, and the
book held pivots + VWAP + gamma only.

That invalidates the measurement trend_join records against itself -- that
confluence between level types happens on 1 of 108 ES entries, so "wait for the
strongest cluster" degenerates to "wait for the nearest lone pivot". Confluence
cannot involve a source that is never present.

Second instance of this exact failure in this file: its own set_gamma docstring
records shipping a hook nothing called, leaving 97% of entries on floor pivots.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar                        # noqa: E402
from engine.features.level_book import LevelBook          # noqa: E402
from engine.features.zones import Zone                    # noqa: E402

NS = 1_000_000_000
T0 = 1_786_973_400          # 09:30 ET


def test_a_virgin_zone_appears_in_levels():
    lb = LevelBook()
    lb.zbook.add(Zone(T0 * NS, top=5010.0, bot=5000.0, direction=1,
                      departure_score=2, base_score=2))
    srcs = {s for _, s in lb.levels()}
    assert "zone" in srcs, (
        "a virgin zone is absent from the level book -- z.virgin() raises "
        "TypeError on a bool property and the bare except hides it")


def test_a_touched_zone_is_excluded():
    lb = LevelBook()
    z = Zone(T0 * NS, top=5010.0, bot=5000.0, direction=1,
             departure_score=2, base_score=2)
    z.touches = 1                                   # no longer virgin
    lb.zbook.add(z)
    assert "zone" not in {s for _, s in lb.levels()}


def test_a_broken_zone_is_excluded():
    lb = LevelBook()
    z = Zone(T0 * NS, top=5010.0, bot=5000.0, direction=1,
             departure_score=2, base_score=2)
    z.broken = True
    lb.zbook.add(z)
    assert "zone" not in {s for _, s in lb.levels()}


def test_zones_can_form_confluence_with_another_source():
    """The point of the book: agreement between DIFFERENT kinds of evidence."""
    lb = LevelBook()
    # a DEMAND zone's proximal edge is its TOP -- the side price meets first
    lb.zbook.add(Zone(T0 * NS, top=5010.0, bot=5004.0, direction=1,
                      departure_score=2, base_score=2))
    lb.set_extra([(5009.0, "prior-day-high")])      # within tol of that edge
    best = max(lb.clusters(2.0), key=lambda c: c[2])
    assert best[2] >= 2, f"no multi-source cluster formed: {lb.clusters(2.0)}"
    assert "zone" in best[1]


# ── zones are a 30-MINUTE object, and they must be able to stop being virgin ──

def _bars(n, start=5000.0, step=0.0, vol=1000):
    out = []
    for i in range(n):
        p = start + i * step
        out.append(Bar((T0 + (i + 1) * 60) * NS, "1m", p, p + 0.5, p - 0.5, p, vol, "ES"))
    return out


def test_zones_are_detected_on_30m_not_on_1m():
    """A 1m detector turns a 2-4 minute wiggle into 'institutional structure'."""
    lb = LevelBook()
    for b in _bars(600, step=0.05):
        lb.on_bar(b)
    n = len(lb.zbook.zones)
    assert n < 40, (
        f"{n} zones from 10 hours of quiet tape -- the detector is running on "
        f"1m bars, so noise is being recorded as structure")


def test_price_entering_a_zone_removes_its_virgin_status():
    lb = LevelBook()
    z = Zone(T0 * NS, top=5001.0, bot=4999.0, direction=1,
             departure_score=2, base_score=2)
    lb.zbook.add(z)
    assert "zone" in {s for _, s in lb.levels()}
    for b in _bars(5, start=5000.0):          # price sits inside the zone
        lb.on_bar(b)
    assert z.touches > 0, "on_price is never called, so nothing is ever tested"
    assert "zone" not in {s for _, s in lb.levels()}


# ── the per-bar hot loop must not walk broken history ────────────────────────

def test_broken_zones_leave_the_hot_loop_but_stay_in_the_book():
    """on_price runs on every 1m bar. A book that never prunes made that cost
    grow without bound once LevelBook actually started receiving zones."""
    from engine.features.zones import ZoneBook
    bk = ZoneBook()
    for i in range(50):
        bk.add(Zone(T0 * NS, top=5000.0 + i, bot=4999.0 + i, direction=1,
                    departure_score=2, base_score=2))
    assert len(bk._live) == 50
    # one close far below breaks every demand zone
    bk.on_bar(Bar(T0 * NS, "30m", 4000.0, 4000.0, 4000.0, 4000.0, 1, "ES"))
    assert all(z.broken for z in bk.zones[:50])
    assert len(bk.zones) == 100, "the flips and the originals both stay on record"
    assert len(bk._live) == 50, "only the 50 flip zones remain in the hot loop"
    assert not any(z.broken for z in bk._live)
