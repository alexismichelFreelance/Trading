"""THE REPRODUCTION: a one-lot book with nothing between all-in and flat.

2026-08-21, both instruments printed the RTH high at 11:53. Nine long sleeves
reached their maximum profit in that minute. Every one held. Five were still
holding at 15:59 when the clock closed them.

    TOTAL had +18,565$   booked -4,668$   gave back 23,232$
    24 of 25 legs were in profit at some point; 8 closed NEGATIVE

Not one of them could take a partial profit, because every sleeve was one lot
with a single exit: the only expressible choices were all-in and flat. These
tests pin the third choice, and the guards that keep it honest.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.features.day_range import DayRange     # noqa: E402
from engine.strategies.base import BaseStrategy    # noqa: E402

NS = 1_000_000_000
D0 = 1_786_973_400 * NS          # 2026-08-17 09:30 ET
DAY = 86_400 * NS


def warmed(level=0.8, typical=40.0, today=36.0):
    """A sleeve whose ruler knows a typical day and has seen `today` of range."""
    s = BaseStrategy()
    dr = DayRange(min_sessions=3)
    for d in range(4):
        dr.note(D0 + d * DAY, 7000.0)
        dr.note(D0 + d * DAY + 60 * NS, 7000.0 + typical)
        dr.roll_session()
    dr.note(D0 + 5 * DAY, 7000.0)
    dr.note(D0 + 5 * DAY + 60 * NS, 7000.0 + today)
    s.day_range = dr
    s.scale_at = level
    s.scale_reset()
    return s


def test_it_sheds_half_once_the_day_is_spent():
    s = warmed()                                   # 36 of 40 == 0.90 used
    assert s.day_range.used() == 0.9
    assert s.scale_out_qty(price=7030.0, entry_px=7000.0, my_dir=1, pos=2) == 1
    assert s.scale_out_qty(price=7030.0, entry_px=7000.0, my_dir=1, pos=4) == 2


def test_it_holds_everything_while_the_day_is_young():
    s = warmed(today=20.0)                         # 0.50 of a typical day
    assert s.scale_out_qty(7030.0, 7000.0, 1, 2) == 0


def test_it_never_scales_a_LOSER():
    """This is profit-taking, not risk management. Shedding half of a losing
    position at extension would be inventing a second stop with no evidence
    behind it, and would fire on exactly the trades that most need their size
    to recover."""
    s = warmed()
    assert s.scale_out_qty(price=6990.0, entry_px=7000.0, my_dir=1, pos=2) == 0
    assert s.scale_out_qty(price=7010.0, entry_px=7000.0, my_dir=-1, pos=-2) == 0


def test_a_short_scales_on_ITS_own_profit():
    s = warmed()
    assert s.scale_out_qty(price=6970.0, entry_px=7000.0, my_dir=-1, pos=-2) == 1


def test_it_only_fires_once_per_trade():
    s = warmed()
    assert s.scale_out_qty(7030.0, 7000.0, 1, 2) == 1
    s._scaled = True
    assert s.scale_out_qty(7030.0, 7000.0, 1, 2) == 0
    s.scale_reset()
    assert s.scale_out_qty(7030.0, 7000.0, 1, 2) == 1


def test_a_single_lot_cannot_be_halved():
    """Scaling a one-lot position is just exiting, and the exit rules already
    own that decision."""
    s = warmed()
    assert s.scale_out_qty(7030.0, 7000.0, 1, 1) == 0


def test_it_fails_CLOSED_when_the_ruler_is_cold():
    """Opposite of the entry gates. An entry filter that does not know must let
    the trade through; a scale-out that does not know must change nothing,
    because acting on an unwarmed ruler would shed size for no reason."""
    s = BaseStrategy()
    s.day_range = DayRange(min_sessions=3)         # no history at all
    s.scale_at = 0.8
    s.scale_reset()
    s.day_range.note(D0, 7000.0)
    s.day_range.note(D0 + 60 * NS, 7100.0)
    assert s.day_range.used() is None
    assert s.scale_out_qty(7030.0, 7000.0, 1, 2) == 0


def test_it_is_off_unless_a_sleeve_opts_in():
    """Every existing sleeve must be untouched by this."""
    s = BaseStrategy()
    assert s.scale_at == 0.0
    assert s.day_range is None
    assert s.scale_out_qty(7030.0, 7000.0, 1, 4) == 0


# ── arrival speed: the second, independent reason to halve ────────────────
# Fast-arriving extremes reverse about twice as hard as slow ones -- the only
# reversal feature that replicated out of sample (2025 MBO and 2026 live, tops
# and bottoms, 4 of 4). Unlike everything order-level it needs price only, so it
# can actually run in the engine.

def arrived(push_frac, typical=40.0, at_high=True, mins=12):
    """A sleeve whose ruler is warm and which has just travelled `push_frac` of
    a typical day's range in the last 10 minutes, ending at the session high."""
    s = BaseStrategy()
    dr = DayRange(min_sessions=3)
    for d in range(4):
        dr.note(D0 + d * DAY, 7000.0)
        dr.note(D0 + d * DAY + 60 * NS, 7000.0 + typical)
        dr.roll_session()
    t0 = D0 + 5 * DAY
    move = push_frac * typical
    start = 7000.0
    dr.note(t0, start - 5.0)                       # establish some range first
    for k in range(mins):                          # walk up over `mins` minutes
        dr.note(t0 + k * 60 * NS, start + move * k / (mins - 1))
    if not at_high:
        dr.note(t0 + mins * 60 * NS, start - 3.0)  # walk away from the high
    s.day_range = dr
    s.scale_at = 0.0                               # ONLY the push trigger armed
    s.scale_push = 0.15
    s.scale_reset()
    return s


def test_a_fast_arrival_at_a_new_high_sheds_half():
    s = arrived(push_frac=0.40)
    assert s.day_range.push(1) is not None and s.day_range.push(1) >= 0.15
    assert s.day_range.at_extreme(1)
    assert s.scale_out_qty(price=s.day_range.last, entry_px=6990.0,
                           my_dir=1, pos=2) == 1


def test_a_SLOW_arrival_at_the_same_high_does_not():
    """Same level, same profit -- only the speed of arrival differs, and that is
    the whole finding."""
    s = arrived(push_frac=0.05)
    assert s.day_range.at_extreme(1)
    assert s.scale_out_qty(price=s.day_range.last, entry_px=6990.0,
                           my_dir=1, pos=2) == 0


def test_a_fast_move_that_is_NOT_at_the_extreme_does_not_fire():
    """Speed alone is not the signal -- a fast move that has already pulled back
    is not an extreme, and the measurement was made at extremes."""
    s = arrived(push_frac=0.40, at_high=False)
    assert not s.day_range.at_extreme(1)
    assert s.scale_out_qty(price=s.day_range.last, entry_px=6990.0,
                           my_dir=1, pos=2) == 0


def test_the_push_trigger_also_refuses_a_loser():
    s = arrived(push_frac=0.40)
    assert s.scale_out_qty(price=s.day_range.last, entry_px=s.day_range.last + 20,
                           my_dir=1, pos=2) == 0


def test_push_fails_closed_before_the_ruler_is_warm():
    s = BaseStrategy()
    dr = DayRange(min_sessions=3)
    t0 = D0
    for k in range(12):
        dr.note(t0 + k * 60 * NS, 7000.0 + k)
    s.day_range = dr
    s.scale_at = 0.0
    s.scale_push = 0.15
    s.scale_reset()
    assert dr.push(1) is None
    assert s.scale_out_qty(7011.0, 6990.0, 1, 2) == 0


# ── the one-way suppressor ────────────────────────────────────────────────
# A day that reached 0.80 of a typical range with NO pullback keeps expanding
# 56-60% of the time against 39-41% for one that has already swung (44 sessions
# of 2025 MBO, 27 of 2026 live). On the four 2026 days where the day-spent
# trigger fired disastrously early -- 07-29, 07-31, 08-03, 08-04, each adding
# +0.45 to +1.24 of a range afterwards -- ALL had zero counter-moves.

def walked(path, typical=40.0):
    """Build a session by walking `path` (a list of prices) inside RTH."""
    dr = DayRange(min_sessions=3)
    for d in range(4):
        dr.note(D0 + d * DAY, 7000.0)
        dr.note(D0 + d * DAY + 60 * NS, 7000.0 + typical)
        dr.roll_session()
    t0 = D0 + 5 * DAY
    for k, px in enumerate(path):
        dr.note(t0 + k * 60 * NS, px)
    return dr


def test_a_one_way_day_is_recognised():
    """Straight up, never a pullback."""
    dr = walked([7000.0 + k for k in range(35)])
    assert dr.used() >= 0.80
    assert dr.legs() == 0
    assert dr.one_way()


def test_a_day_that_has_swung_is_not_one_way():
    """Up 20, back 10 (50% of range), up again -- one completed counter-move."""
    path = ([7000.0 + k for k in range(21)] +
            [7020.0 - k for k in range(11)] +
            [7010.0 + k for k in range(26)])
    dr = walked(path)
    assert dr.legs() >= 1
    assert not dr.one_way()


def test_the_suppressor_blocks_scaling_on_a_one_way_day():
    s = BaseStrategy()
    s.day_range = walked([7000.0 + k for k in range(35)])
    s.scale_at = 0.80
    s.skip_one_way = True
    s.scale_reset()
    assert s.day_range.extended(0.80)
    assert s.scale_out_qty(s.day_range.last, 7000.0, 1, 2) == 0


def test_the_same_day_WOULD_scale_without_the_suppressor():
    """The suppressor is the only difference -- everything else is identical."""
    s = BaseStrategy()
    s.day_range = walked([7000.0 + k for k in range(35)])
    s.scale_at = 0.80
    s.skip_one_way = False
    s.scale_reset()
    assert s.scale_out_qty(s.day_range.last, 7000.0, 1, 2) == 1


def test_a_swung_day_still_scales_with_the_suppressor_on():
    path = ([7000.0 + k for k in range(21)] +
            [7020.0 - k for k in range(11)] +
            [7010.0 + k for k in range(26)])
    s = BaseStrategy()
    s.day_range = walked(path)
    s.scale_at = 0.80
    s.skip_one_way = True
    s.scale_reset()
    assert s.day_range.legs() >= 1
    assert s.scale_out_qty(s.day_range.last, 7000.0, 1, 2) == 1
