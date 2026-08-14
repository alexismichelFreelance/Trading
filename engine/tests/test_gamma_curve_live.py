"""The gamma regime is evaluated at the LIVE price, not fixed at midnight.

Two separate staleness problems, and only one of them is expensive:

  THE CURVE ages. Each option's gamma depends on spot, time and IV, and open
  interest is published end of day, so today's 0DTE positioning is invisible.
  Fixing that needs re-pricing, or intraday chain pulls, or data we do not buy.

  WHICH POCKET PRICE IS IN does not need any of that. The cumulative curve is a
  function of STRIKE -- the pocket boundaries sit at fixed prices. As price
  moves through them the regime changes, and reading that off the stored curve
  costs one interpolation.

The second is the one that was wrong. On 2026-08-13 the engine held a single
sign for the whole session while price travelled 45 points, crossed nothing, and
stopped dead at the flip. Even with a perfectly fresh curve, a scalar computed
once at midnight cannot answer "which side of the flip is price on now".

So: GammaCurve loads the prior session's per-strike rows once, and answers at
any price. Same data, evaluated where price actually is.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.features.gamma_curve import GammaCurve  # noqa: E402

# The 2026-08-13 shape: book total positive, low strikes net short.
ROWS = [
    # (strike, call_gex, put_gex)
    (7700.0, 1.0, -6.0),
    (7750.0, 2.0, -5.0),
    (7800.0, 9.0, -1.0),
    (7850.0, 8.0, -1.0),
    (7900.0, 6.0, -1.0),
]
BASIS = 42.0        # SPX -> ES


def _curve(basis=BASIS):
    return GammaCurve.from_rows(ROWS, spot=7748.5, basis=basis, underlying="SPX")


def test_the_same_curve_gives_different_regimes_at_different_prices():
    """THE POINT. One snapshot, two prices, two answers -- which is what a day
    that travels 45 points needs."""
    c = _curve()
    lo = c.at(7748.5 + BASIS)          # below the flip, in future terms
    hi = c.at(7860.0 + BASIS)
    assert lo["local_sign"] == -1, "below the flip must read SHORT gamma"
    assert hi["local_sign"] == 1, "above it must read LONG"


def test_levels_are_returned_in_future_terms():
    """Strikes are index points; the sleeves trade ES/NQ. Mixing the two is how
    a level lands 42 points from where it is drawn."""
    c = _curve()
    a = c.at(7748.5 + BASIS)
    assert abs(a["flip"] - (7800.0 + BASIS)) < 1e-9, a["flip"]
    assert a["call_wall"] == 7800.0 + BASIS
    assert a["put_wall"] == 7700.0 + BASIS


def test_distance_to_the_edge_of_the_pocket_is_live():
    """A binary regime cannot say how much room is left. 2026-08-13 had 45
    points of it and used all of them."""
    c = _curve()
    far = c.at(7748.5 + BASIS)["dist_to_flip"]
    near = c.at(7790.0 + BASIS)["dist_to_flip"]
    assert far > near > 0, f"{far} then {near}: distance must shrink as we approach"


def test_crossing_the_flip_flips_the_regime_and_nothing_else_has_to_change():
    """No refetch, no recompute -- the boundary is a fixed price and price moved
    across it."""
    c = _curve()
    # cumulative reaches zero at the 7800 strike, so that is the boundary
    before = c.at(7799.0 + BASIS)["local_sign"]
    after = c.at(7801.0 + BASIS)["local_sign"]
    assert before == -1 and after == 1


def test_the_walls_bracketing_price_are_reported():
    """Which wall is overhead and which is underneath -- not merely the largest
    two in the book."""
    c = _curve()
    a = c.at(7770.0 + BASIS)
    assert a["wall_above"] is not None and a["wall_above"] > 7770.0 + BASIS
    assert a["wall_below"] is not None and a["wall_below"] < 7770.0 + BASIS


def test_an_empty_curve_answers_none_rather_than_guessing():
    c = GammaCurve.from_rows([], spot=7748.5, basis=BASIS, underlying="SPX")
    assert c.at(7800.0) is None
    assert not c.ok


def test_the_curve_reports_its_own_age_so_staleness_is_visible():
    """The pocket boundaries stay valid as price moves; the CURVE still ages.
    A consumer has to be able to see how old the snapshot is rather than
    discover it from a bad level."""
    c = _curve()
    assert hasattr(c, "sess")
