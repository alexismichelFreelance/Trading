"""A continuation entry taken near a gamma pocket edge is taken into reversion.

Per bar, 30 ES / 20 NQ sessions, variance ratio at 30 bars
(strategy_lab/gamma_pocket_behaviour.py):

    deep in a SHORT pocket   1.25 ES / 1.34 NQ    moves compound
    near the pocket EDGE     0.86 ES / 0.73 NQ    moves revert
    deep in a LONG pocket    0.84 ES / 0.50 NQ    moves revert

VR > 1 means moves compound, < 1 that they revert. Amplification is a
SHORT-pocket phenomenon and it dies at the boundary -- near an edge BOTH regimes
revert, which is not something a spurious correlation would organise itself into.

The same split applied to trades the sleeves already made in the pinned replay
(strategy_lab/gamma_gate_check.py):

    trend sleeves, SHORT pocket, deep        442 trips   +50,722   (+115/trip)
    trend sleeves, SHORT pocket, near edge   105 trips   -15,212   (-145/trip)

the only losing bucket in the table. So the rule is narrow on purpose: it does
not pick a regime, it does not size, it declines ONE case -- a continuation
entry within `pocket_min_edge` points of a sign change.

Deliberately fail-open. 8 of 28 SPX sessions have no crossing at all, and a
missing curve or a stale DB must never stop a sleeve trading; the filter is an
improvement on a good day, not a precondition for trading.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.features.gamma_curve import GammaCurve  # noqa: E402
from engine.strategies.base import BaseStrategy  # noqa: E402

# cum: -5, -8, 0, +7, +12 -> one crossing at the 7800 strike
ROWS = [(7700.0, 1.0, -6.0), (7750.0, 2.0, -5.0), (7800.0, 9.0, -1.0),
        (7850.0, 8.0, -1.0), (7900.0, 6.0, -1.0)]
BASIS = 20.0                      # measured ES basis, not the stale 42


def _sleeve(min_edge=15.0, curve=True):
    s = BaseStrategy()
    s.pocket = (GammaCurve.from_rows(ROWS, spot=7748.5, basis=BASIS,
                                     underlying="SPX") if curve else None)
    s.pocket_min_edge = min_edge
    return s


FLIP = 7800.0 + BASIS             # 7820


def test_an_entry_far_from_the_edge_is_allowed():
    """Deep in the pocket is where continuation works: VR30 1.25."""
    assert _sleeve().pocket_entry_ok(FLIP - 60.0)


def test_an_entry_near_the_edge_is_declined():
    """THE RULE. 105 trips, -15,212, the only losing bucket."""
    assert not _sleeve().pocket_entry_ok(FLIP - 5.0)
    assert not _sleeve().pocket_entry_ok(FLIP + 5.0), (
        "the edge cuts both ways -- reversion near the boundary is not a "
        "property of the side you approach from")


def test_the_threshold_is_the_threshold():
    s = _sleeve(min_edge=15.0)
    assert not s.pocket_entry_ok(FLIP - 14.9)
    assert s.pocket_entry_ok(FLIP - 15.1)


def test_no_curve_means_no_filter():
    """A stale DB or a missing fetch must not stop the book trading."""
    assert _sleeve(curve=False).pocket_entry_ok(FLIP - 1.0)


def test_zero_threshold_disables_it_even_with_a_curve():
    """The deployed sleeves keep their behaviour; only the twin opts in."""
    assert _sleeve(min_edge=0.0).pocket_entry_ok(FLIP - 1.0)


def test_a_book_with_no_crossing_allows_everything():
    """8 of 28 SPX sessions have no flip at all. No boundary, no near-edge."""
    s = BaseStrategy()
    s.pocket = GammaCurve.from_rows([(7700.0, 5.0, -1.0), (7800.0, 5.0, -1.0)],
                                    spot=7750.0, basis=BASIS, underlying="SPX")
    s.pocket_min_edge = 15.0
    assert s.pocket_entry_ok(7750.0 + BASIS)


def test_the_default_strategy_is_unfiltered():
    """Every existing sleeve must be untouched until a variant opts in."""
    s = BaseStrategy()
    assert s.pocket is None and s.pocket_min_edge == 0.0
    assert s.pocket_entry_ok(7800.0)
