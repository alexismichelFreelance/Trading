"""The gamma regime is LOCAL to where price is, not a single number for the book.

2026-08-13, SPX. The stored row said:

    spot 7748.50   zero_gamma 7795.20   net_sign +1   total_gex +2.34e10

and `net_sign = +1` is what every *_gex sleeve reads as "dealers long gamma,
pinning, mean-reversion". But net_sign is the sign of the TOTAL across every
strike in the book, and spot was 47 points BELOW the flip -- in the pocket where
cumulative dealer gamma is NEGATIVE. Short gamma amplifies: dealers buy strength
and sell weakness.

What ES did that morning: opened 7792.50, ran 45 points to 7838.25, and reversed
straight back to 7798.50. The flip in future terms is 7795.20 + 42.0 basis =
7837.20. The high missed it by 1.05 points.

So the day amplified up through the short-gamma pocket, stopped dead at the sign
change, and mean-reverted -- exactly the textbook behaviour -- while the engine's
one scalar had the regime backwards the whole way.

`compute_levels` already builds the full per-strike curve and already finds EVERY
zero crossing into a list called `flips`. It then keeps the one nearest spot, one
call wall, one put wall, and the book total, and discards the rest. This module
keeps the curve, so the questions that matter -- which pocket is price in, where
does that pocket end, which walls bound it -- can be asked at all.

The construction is deliberately the SAME cumulative-sum approximation the
fetcher already used (net GEX summed across strikes in ascending order; the flip
is where the running total crosses zero). Changing the method is a separate
question from keeping what the method produces.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.features.gamma_profile import gamma_profile  # noqa: E402


def _book(rows):
    """rows = [(strike, call_gex, put_gex)] with put_gex already negative."""
    return {k: (c, p) for k, c, p in rows}


# A book whose TOTAL is positive while the low strikes are net short: the
# 2026-08-13 shape, reduced to five strikes.
SPLIT = _book([
    (7700.0, 1.0, -6.0),        # cum -5
    (7750.0, 2.0, -5.0),        # cum -8
    (7800.0, 9.0, -1.0),        # cum  0  -> flip between 7750 and 7800
    (7850.0, 8.0, -1.0),        # cum +7
    (7900.0, 6.0, -1.0),        # cum +12   TOTAL +12, positive
])


def test_the_book_total_and_the_local_regime_disagree():
    """THE REGRESSION, in one assertion. The engine stored +1 for a day that was
    locally short gamma where price actually was."""
    p = gamma_profile(SPLIT, spot=7748.5)
    assert p["total_sign"] == 1, "fixture is meant to have a positive book total"
    assert p["local_sign"] == -1, (
        f"spot 7748.5 sits below the flip at {p['flip']:.1f} -- cumulative gamma "
        f"there is negative and moves are AMPLIFIED. Reporting the book total "
        f"(+1, 'pinning') is the wrong regime for where price is.")


def test_above_the_flip_the_two_agree():
    p = gamma_profile(SPLIT, spot=7860.0)
    assert p["local_sign"] == 1 and p["total_sign"] == 1


def test_every_crossing_is_kept_not_just_the_nearest():
    """`flips` was computed as a list and then thrown away except for one. A
    book can change sign more than once, and the pocket price is in is bounded
    by the crossings either side of it, not by the closest one."""
    w = _book([
        (7600.0, 1.0, -5.0),     # cum -4
        (7650.0, 6.0, -1.0),     # cum +1     crossing up
        (7700.0, 1.0, -4.0),     # cum -2     crossing down
        (7750.0, 7.0, -1.0),     # cum +4     crossing up
    ])
    p = gamma_profile(w, spot=7680.0)
    assert len(p["flips"]) >= 3, f"kept {len(p['flips'])} crossings: {p['flips']}"
    assert p["local_sign"] == -1, "7680 is in the negative pocket between two flips"


def test_the_pocket_price_is_in_has_both_edges():
    """What a 'zone of negative gamma' actually is: bounded below and above by
    sign changes. Knowing only the nearest one cannot tell you which way out."""
    p = gamma_profile(SPLIT, spot=7748.5)
    lo, hi = p["pocket"]
    assert lo is None or lo < 7748.5
    assert hi is not None and hi > 7748.5
    assert abs(hi - p["flip"]) < 1e-9, "the pocket's upper edge IS the next flip up"


def test_walls_are_ranked_not_singular():
    """One call wall and one put wall were kept out of a whole distribution. The
    second and third concentrations bound the move after the first gives way."""
    p = gamma_profile(SPLIT, spot=7800.0)
    assert [k for k, _ in p["call_walls"]][:2] == [7800.0, 7850.0]
    assert [k for k, _ in p["put_walls"]][:2] == [7700.0, 7750.0]


def test_the_single_wall_still_matches_what_the_fetcher_used_to_write():
    """The reduced values must not change meaning -- old rows stay comparable."""
    p = gamma_profile(SPLIT, spot=7800.0)
    assert p["call_wall"] == 7800.0
    assert p["put_wall"] == 7700.0


def test_a_book_with_no_crossing_reports_its_one_sign_and_no_flip():
    allpos = _book([(7700.0, 5.0, -1.0), (7800.0, 5.0, -1.0)])
    p = gamma_profile(allpos, spot=7750.0)
    assert p["flips"] == [] and p["flip"] is None
    assert p["local_sign"] == 1 and p["total_sign"] == 1
    assert p["pocket"] == (None, None)


def test_an_empty_book_is_none_rather_than_a_crash():
    assert gamma_profile({}, spot=7750.0) is None


def test_distance_to_the_next_sign_change_is_reported():
    """A binary regime says nothing about how much room is left in it. The
    2026-08-13 rally ran 45 points and stopped at the flip; the useful number is
    how far the pocket extends, not merely which pocket it is."""
    p = gamma_profile(SPLIT, spot=7748.5)
    assert p["dist_to_flip"] is not None
    assert abs(p["dist_to_flip"] - (p["flip"] - 7748.5)) < 1e-9


# ── the chart label must describe where PRICE is ─────────────────────────────
#
# painters.py:270 drew the flip labelled from `net_sign` -- the sign of the
# WHOLE book. Measured across the 44 rebuilt CBOE payloads, that disagreed with
# the regime at spot on 25 of 44 sessions (57%), and every single disagreement
# was the same way round: book LONG, local SHORT. Spot sat below the flip almost
# continuously from 2026-08-06 to 08-13 on both SPX and NDX.
#
# So the annotation on the chart said "pinning" through a fortnight of sessions
# that were structurally amplifying where price actually was. No trading gate
# read it -- the *_gex sleeves gate on GammaRegime, a percentile of aggregate
# GEX from a different table -- but it is the label a human reads.

def test_the_regime_label_follows_price_not_the_book():
    """Above the flip is long gamma, below it is short, whatever the book totals
    to. The label is a statement about price, so it has to move with price."""
    from engine.painters import gamma_label
    assert "SHORT" in gamma_label(price=7748.5, flip=7795.2, net_sign=1)
    assert "long" in gamma_label(price=7850.0, flip=7795.2, net_sign=1)


def test_the_label_falls_back_to_the_book_when_there_is_no_flip():
    """14 of the 44 sessions had no crossing at all. With no boundary the book
    sign is the only answer there is, and it is the right one."""
    from engine.painters import gamma_label
    assert "SHORT" in gamma_label(price=7748.5, flip=None, net_sign=-1)
    assert "long" in gamma_label(price=7748.5, flip=None, net_sign=1)


def test_the_label_says_so_when_it_has_no_price_yet():
    """Before the first bar the painter has no price. Guessing from the book
    would reproduce exactly the bug this replaces."""
    from engine.painters import gamma_label
    assert gamma_label(price=0.0, flip=7795.2, net_sign=1) == "regime unknown"
