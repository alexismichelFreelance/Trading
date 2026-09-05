"""Join the trend ON A PULLBACK, not on the breakout bar.

The sleeve currently enters the instant confirmation arrives -- price has moved
`conf_pts` off the window extreme, so it buys strength at its most extended
point of the move so far. Its own MFE/MAE record says that is expensive: the
median caught leg gives 7.5pt of heat, and this week ES:trendjoin_narrow took
six stops in a session flipping direction on each one.

A PULLBACK entry arms on the same confirmation but waits for price to retrace
`pullback_pts` from the extreme reached since arming, then enters in the
confirmed direction. Same thesis, better price, and it declines the moves that
never pull back at all -- which are the ones that ran away from the entry.

NOT A CLOCK GATE. Skipping the first hour was tested on 29 ES / 26 NQ replayed
sessions and does not replicate: it helps ES (+105,062 -> +119,275 skipping
before 10:30) and destroys NQ (+513,295 -> +110,160), and each instrument's
worst window is different (ES 10:00-10:30, NQ 11:30-14:00). This is a structural
change instead, and it is what the discretionary trade that beat the book on
2026-08-28 actually was: long into a pullback below VWAP, not a breakout.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar                          # noqa: E402
from engine.strategies.trend_join import TrendJoinStrategy   # noqa: E402

NS = 1_000_000_000
D0 = 1_786_973_400 * NS          # 09:30 ET


def drive(s, path, start=0):
    out = []
    for k, px in enumerate(path):
        for o in s.on_bar(Bar(D0 + (start + k) * 60 * NS, "1m", px, px + 0.25,
                              px - 0.25, px, 100, s.symbol)):
            out.append((start + k, o))

            class _P:
                qty = s.pos + o.side * o.qty
            s.on_position(_P())
    return out


def _sleeve(pullback_pts=0.0):
    s = TrendJoinStrategy("ES", conf_pts=4.0, stop_pts=40.0, lookback=5)
    s.pullback_pts = pullback_pts
    return s


def test_without_a_pullback_requirement_it_enters_on_the_breakout():
    s = _sleeve(0.0)
    got = drive(s, [7000.0] * 6 + [7005.0])
    assert got and got[0][1].side == 1, "baseline behaviour must be unchanged"


def test_with_a_pullback_it_does_not_enter_on_the_breakout_bar():
    s = _sleeve(2.0)
    got = drive(s, [7000.0] * 6 + [7005.0])
    assert not got, "armed, but must not buy the extended bar"
    assert s.pos == 0


def test_it_enters_once_price_retraces_the_required_amount():
    s = _sleeve(2.0)
    drive(s, [7000.0] * 6 + [7005.0])          # arm at 7005
    got = drive(s, [7004.0, 7002.5], start=7)  # retrace 2.5 from the high
    assert got, "a 2.5pt pullback must trigger the entry"
    side = got[0][1].side
    assert side == 1, "the entry must still be in the CONFIRMED direction"
    assert s.pos == 1


def test_a_move_that_never_pulls_back_is_declined():
    """The runaway is exactly the move a breakout entry catches at its worst
    price. Declining it is the point, not a missed trade."""
    s = _sleeve(2.0)
    drive(s, [7000.0] * 6 + [7005.0])
    got = drive(s, [7005.5 + 0.5 * k for k in range(12)], start=7)
    assert not got and s.pos == 0


def test_the_arm_expires_so_it_cannot_wait_forever():
    s = _sleeve(2.0)
    s.pullback_bars = 5
    drive(s, [7000.0] * 6 + [7005.0])
    drive(s, [7005.25] * 8, start=7)           # drifts, never retraces
    got = drive(s, [7002.0], start=15)         # retrace arrives too late
    assert not got, "a stale arm must expire rather than fire on old news"


# ── pull back to STRUCTURE, not to a distance ───────────────────────────────
def test_it_waits_for_the_level_and_names_it():
    """The magic-number version says "4 points". This says "the put wall", and
    the fill is tagged with WHICH level it waited for so the decision can be
    audited afterwards rather than taken on faith."""
    from engine.features.level_book import LevelBook

    s = TrendJoinStrategy("ES", conf_pts=4.0, stop_pts=40.0, lookback=5)
    s.levels = LevelBook()
    s.pullback_max = 30.0
    s.levels.set_gamma({"put_wall": 7001.0, "call_wall": 7060.0, "flip": None})

    got = drive(s, [7000.0] * 6 + [7005.0])
    assert not got, "confirmation must only ARM"
    got = drive(s, [7004.0, 7003.0], start=7)
    assert not got, "not into the level yet"
    got = drive(s, [7001.0], start=9)
    assert got, "a touch of the put wall must trigger the entry"
    assert "put_wall" in got[0][1].tag, got[0][1].tag


def test_no_structure_means_no_trade():
    """The whole point. A constant always produces an entry eventually; asking
    the market means sometimes the answer is 'nothing is there'."""
    from engine.features.level_book import LevelBook

    s = TrendJoinStrategy("ES", conf_pts=4.0, stop_pts=40.0, lookback=5)
    s.levels = LevelBook()
    # VWAP is ALWAYS structure and sits a few points below here, so the walls
    # alone are not enough to starve the setup -- the reach has to be tighter
    # than anything present. That VWAP counts is correct behaviour, not a bug.
    s.pullback_max = 0.1
    s.levels.set_gamma({"put_wall": 6900.0, "call_wall": 7300.0, "flip": None})
    got = drive(s, [7000.0] * 6 + [7005.0])
    assert not got
    got = drive(s, [7004.0, 7002.0, 7000.0], start=7)
    assert not got and s.pos == 0, "no level in reach -> no trade at all"
