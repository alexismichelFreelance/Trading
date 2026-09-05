"""A level matters because things AGREE there, not because it is close.

WHAT WENT WRONG BEFORE. `pullback_to` returned the NEAREST level. Floor pivots
alone are nine per session, so something is almost always a few points away, and
the entry degenerated into a shallow constant -- ES:trendjoin_lvl +4,550 against
a +7,212 baseline, NQ -15,065 at a 65.5% day-win-rate, the same win-often-lose-
big signature as the 4-point version. Worse, 97% of its entries were pivots:
zones never produced a virgin edge in reach and the gamma walls were NEVER
INJECTED at all, so three of the four sources were not in the book being queried.

WHAT A LEVEL ACTUALLY IS. "Whether it is a pivot, some call walls, a zone" is a
description of CONFLUENCE. A price where a call wall, a prior-day high and a
zone edge coincide is a different object from a lone pivot two points away, and
the market treats it differently. So levels are CLUSTERED and scored by how many
distinct sources agree, and the sleeve waits for the STRONGEST structure within
reach rather than the first one it meets.

SCALE-FREE. The clustering tolerance is a fraction of a typical session's range,
never a point count -- the mistake that produced 4pt on ES and 20pt on NQ.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar                     # noqa: E402
from engine.features.level_book import LevelBook        # noqa: E402

NS = 1_000_000_000
D0 = 1_786_973_400 * NS


def bars(lb, path, vol=1000):
    for k, px in enumerate(path):
        lb.on_bar(Bar(D0 + k * 60 * NS, "1m", px, px + 0.5, px - 0.5, px, vol, "ES"))


def test_confluence_beats_proximity():
    """A lone level nearby must lose to three sources agreeing further away.
    This is the whole correction."""
    lb = LevelBook()
    bars(lb, [7700.0] * 5)
    lb.set_gamma({"put_wall": 7660.0, "call_wall": 7900.0, "flip": 7660.5})
    lb.set_extra([(7659.5, "prior_day_low")])
    got = lb.pullback_to(7700.0, 1, max_dist=60.0, tol=2.0)
    assert got is not None
    px, src, n = got
    assert n >= 3, f"three sources agree near 7660, got score {n} ({src})"
    assert abs(px - 7660.0) <= 2.0, f"expected the 7660 cluster, got {px}"


def test_a_lone_level_still_answers_when_it_is_all_there_is():
    lb = LevelBook()
    bars(lb, [7700.0] * 5)
    lb.set_gamma({"put_wall": 7680.0, "call_wall": 7900.0, "flip": None})
    px, src, n = lb.pullback_to(7700.0, 1, max_dist=60.0, tol=2.0)
    assert n == 1 and abs(px - 7680.0) < 1e-9


def test_the_cluster_reports_every_source_that_agrees():
    """The fill is tagged with WHAT it waited for; a confluence must say so, or
    the decision cannot be audited afterwards."""
    lb = LevelBook()
    bars(lb, [7700.0] * 5)
    lb.set_gamma({"put_wall": 7660.0, "call_wall": 7900.0, "flip": 7660.5})
    _, src, _ = lb.pullback_to(7700.0, 1, max_dist=60.0, tol=2.0)
    assert "put_wall" in src and "flip" in src, src


def test_direction_and_reach_are_still_respected():
    lb = LevelBook()
    bars(lb, [7700.0] * 5)
    lb.set_gamma({"put_wall": 7660.0, "call_wall": 7740.0, "flip": 7660.5})
    assert lb.pullback_to(7700.0, 1, max_dist=60.0, tol=2.0)[0] < 7700.0
    assert lb.pullback_to(7700.0, -1, max_dist=60.0, tol=2.0)[0] > 7700.0
    assert lb.pullback_to(7700.0, 1, max_dist=5.0, tol=2.0) is None


def test_it_fails_closed_with_no_structure():
    lb = LevelBook()
    assert lb.pullback_to(7700.0, 1, max_dist=60.0, tol=2.0) is None
