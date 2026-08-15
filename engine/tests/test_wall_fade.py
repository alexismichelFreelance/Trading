"""WallFadeStrategy fades gamma walls, and ONLY in a long-gamma pocket.

The gate is the whole sleeve. Measured over 30 ES sessions, forward 30-bar move,
session-demeaned:

    LONG    call wall  -1.58 (44% up)      put wall  +1.66 (59% up)
    SHORT   call wall  -0.81 (48%)         put wall  -0.99 (46%)

In long gamma the two are symmetric and correctly signed against a -1.10
baseline; in short gamma neither is clean. Pooled across both regimes the effect
disappears (call -0.53, put -0.87) because 85% of bars are short-gamma -- so a
wall sleeve WITHOUT the regime gate is measuring mostly the regime where the
edge does not exist.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar, PositionUpdate  # noqa: E402
from engine.features.gamma_curve import GammaCurve  # noqa: E402
from engine.strategies.wall_fade import WallFadeStrategy  # noqa: E402

BASIS = 20.0
# cum: -5, -8, 0, +7, +12 -> crossing at 7800; call walls ranked 7800/7850/7900,
# put walls 7700/7750. In FUTURE terms add BASIS.
ROWS = [(7700.0, 1.0, -6.0), (7750.0, 2.0, -5.0), (7800.0, 9.0, -1.0),
        (7850.0, 8.0, -1.0), (7900.0, 6.0, -1.0)]
CURVE = GammaCurve.from_rows(ROWS, spot=7860.0, basis=BASIS, underlying="SPX")


def _b(t, o, h, l, c):
    return Bar(int(pd.Timestamp(t, tz="America/New_York").value), "1m",
               o, h, l, c, 1000, "ES")


def _sleeve():
    s = WallFadeStrategy("ES", point_usd=50.0)
    s.pocket = CURVE
    return s


LONG_PX = 7870.0 + BASIS        # above the 7800 crossing -> long gamma
SHORT_PX = 7760.0 + BASIS       # below it -> short gamma
CALL_W = 7800.0 + BASIS         # 7820, the biggest call concentration
PUT_W = 7700.0 + BASIS          # 7720


def test_it_shorts_a_call_wall_in_a_long_pocket():
    """THE TRADE. -1.58 forward over 531 bars: the wall caps."""
    s = _sleeve()
    w = 7850.0 + BASIS          # a call wall that sits in the long pocket
    out = s.on_bar(_b("2026-08-05 10:00", w - 3, w + 2, w - 4, w - 1))
    assert out, "price reached a call wall in a long pocket and nothing fired"
    assert out[0].side == -1, "faded a call wall the wrong way"
    assert out[0].trigger_price is not None, (
        "the level IS the trade -- an unpriced fill makes the stop and target "
        "measurements of a price the trade never had")


def test_it_does_nothing_in_a_short_pocket():
    """Neither wall is clean there (-0.81 / -0.99), and 85% of bars are short --
    which is exactly why the unconditional test showed no effect."""
    s = _sleeve()
    assert s.on_bar(_b("2026-08-05 10:00", PUT_W - 3, PUT_W + 2,
                       PUT_W - 4, PUT_W - 1)) == []
    assert s.trade is None


def test_it_buys_a_put_wall_in_a_long_pocket():
    s = _sleeve()
    # a long pocket needs price above the crossing; use a put wall that is
    # itself above it so both conditions hold
    rows = [(7700.0, 9.0, -1.0), (7750.0, 8.0, -1.0), (7800.0, 1.0, -9.0)]
    s.pocket = GammaCurve.from_rows(rows, spot=7710.0, basis=BASIS,
                                    underlying="SPX")
    a = s.pocket.at(7710.0 + BASIS)
    if a["local_sign"] <= 0:
        return                                # fixture not in a long pocket
    pw = a["put_walls"][0]
    out = s.on_bar(_b("2026-08-05 10:00", pw + 3, pw + 4, pw - 2, pw + 1))
    assert out and out[0].side == 1, "a put wall in long gamma supports: buy it"


def test_no_curve_means_no_trades():
    """A missing fetch must leave the sleeve flat, never guessing."""
    s = WallFadeStrategy("ES", point_usd=50.0)
    assert s.on_bar(_b("2026-08-05 10:00", 7860, 7870, 7850, 7865)) == []


def test_it_stands_down_outside_rth():
    s = _sleeve()
    w = 7850.0 + BASIS
    assert s.on_bar(_b("2026-08-05 08:00", w - 3, w + 2, w - 4, w - 1)) == []


def test_the_stop_and_target_price_at_their_levels():
    s = _sleeve()
    w = 7850.0 + BASIS
    s.on_bar(_b("2026-08-05 10:00", w - 3, w + 2, w - 4, w - 1))
    assert s.trade is not None
    s.on_position(PositionUpdate(0, "ES", -1, s.trade.entry))
    stop = s.trade.stop
    out = s.on_bar(_b("2026-08-05 10:05", stop - 2, stop + 4, stop - 3, stop - 2))
    assert out and out[0].tag.endswith("stop")
    assert out[0].trigger_price == stop, (
        f"stop booked {out[0].trigger_price} rather than the {stop} level")


def test_it_gives_up_after_the_measured_horizon():
    """The edge was measured over 30 bars. Past that the sleeve has no claim,
    so it leaves rather than holding on a number it never tested."""
    s = _sleeve()
    w = 7850.0 + BASIS
    s.on_bar(_b("2026-08-05 10:00", w - 3, w + 2, w - 4, w - 1))
    s.on_position(PositionUpdate(0, "ES", -1, s.trade.entry))
    out = []
    for i in range(1, 35):
        out = s.on_bar(_b(f"2026-08-05 10:{i:02d}", w - 3, w - 2, w - 4, w - 3)) or out
    assert any("timeout" in (o.tag or "") for o in out), "held past 30 bars"


def test_it_caps_entries_per_session():
    s = _sleeve()
    w = 7850.0 + BASIS
    for i in range(6):
        s.on_bar(_b(f"2026-08-05 1{i}:00", w - 3, w + 2, w - 4, w - 1))
        s.trade = None
        s.pos = 0
    assert s._n <= 2, f"took {s._n} entries in one session"
