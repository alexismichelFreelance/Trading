"""A pullback is a retracement INTO SOMETHING, not a fixed distance.

"Wait for 4 points on ES, 20 on NQ" is a magic number: it asserts how far a
pullback should be instead of asking where the structure is, and the 5x
instrument fudge is the tell. LevelBook aggregates the level sources the engine
ALREADY computes -- pivots, prior-day H/L, round numbers, virgin zone edges,
anchored VWAP, prior-session gamma walls -- so a strategy can ask what price
would actually retrace into, and DECLINE when the answer is nothing.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar                     # noqa: E402
from engine.features.level_book import LevelBook        # noqa: E402

NS = 1_000_000_000
D0 = 1_786_973_400 * NS          # 09:30 ET


def bars(lb, path, day=0, vol=1000):
    t0 = D0 + day * 86_400 * NS
    for k, px in enumerate(path):
        lb.on_bar(Bar(t0 + k * 60 * NS, "1m", px, px + 0.5, px - 0.5, px, vol, "ES"))


def test_it_says_nothing_while_cold():
    lb = LevelBook()
    assert lb.pullback_to(7700.0, 1) is None, "no structure must mean no opinion"


def test_a_gamma_wall_below_is_a_pullback_target_for_a_long():
    lb = LevelBook()
    bars(lb, [7700.0] * 5)
    lb.set_gamma({"put_wall": 7680.0, "call_wall": 7760.0, "flip": 7710.0})
    got = lb.pullback_to(7700.0, 1)
    assert got is not None
    px, src, _n = got
    assert px == 7680.0 and "put_wall" in src, f"got {got}"


def test_nearest_wins_only_when_the_evidence_is_EQUAL():
    """This test used to assert nearest-wins outright. That was the bug: with
    floor pivots alone nine per session, "nearest" made the entry a shallow
    constant in disguise and it measured WORSE than the fixed distance it
    replaced (ES +4,550 vs a +7,212 baseline; NQ -15,065). Selection is now by
    CONFLUENCE first -- see tests/test_level_confluence.py -- and proximity only
    breaks ties between equally-supported structures, which is what this now
    pins."""
    lb = LevelBook()
    bars(lb, [7700.0] * 5)
    lb.set_gamma({"put_wall": 7600.0, "call_wall": 7800.0, "flip": 7690.0})
    px, src, n = lb.pullback_to(7700.0, 1, tol=1.0)
    assert n == 1, "both are lone levels here, so the tie-break decides"
    assert px == 7690.0 and "flip" in src


def test_direction_is_respected():
    lb = LevelBook()
    bars(lb, [7700.0] * 5)
    lb.set_gamma({"put_wall": 7680.0, "call_wall": 7720.0, "flip": None})
    assert lb.pullback_to(7700.0, 1)[0] == 7680.0     # long retraces DOWN
    assert lb.pullback_to(7700.0, -1)[0] == 7720.0    # short retraces UP


def test_max_dist_declines_structure_that_is_too_far_to_be_a_pullback():
    """A level half a session away is a different trade, not a pullback."""
    lb = LevelBook()
    bars(lb, [7700.0] * 5)
    lb.set_gamma({"put_wall": 7500.0, "call_wall": 7900.0, "flip": None})
    assert lb.pullback_to(7700.0, 1, max_dist=30.0) is None
    assert lb.pullback_to(7700.0, 1, max_dist=250.0)[0] == 7500.0


def test_vwap_counts_as_structure():
    lb = LevelBook()
    bars(lb, [7690.0] * 10 + [7700.0] * 2)     # vwap sits below the last price
    got = lb.pullback_to(7702.0, 1)
    assert got is not None and ("vwap" in got[1] or "pivot" in got[1]), got


def test_it_reports_WHICH_level_so_a_decision_can_be_audited():
    lb = LevelBook()
    bars(lb, [7700.0] * 5)
    lb.set_gamma({"put_wall": 7680.0, "call_wall": 7760.0, "flip": None})
    px, src, n = lb.pullback_to(7700.0, 1)
    assert isinstance(src, str) and src, "the caller must be able to log WHAT it waited for"
    assert n >= 1
