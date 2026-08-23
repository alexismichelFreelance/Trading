"""Size the DAY from the PRIOR session's range.

Sizing, not filtering, and the distinction is the whole point. Every trade-level
rule tried on 2026-08-22 failed because trendjoin's P&L is 3 trades on ES and 5
on NQ -- a filter either misses them or kills them, and an exit rule changes
WHICH TRADES EXIST (the 10-minute scratch predicted +3,575 on ES from a fixed
ledger and delivered -6,262 in replay, because ES went 213 -> 293 trades).
Scaling quantity changes no entry, no exit and no sequence, so it is the one
intervention a fixed ledger can actually measure.

MEASURED by portfolio_replay, 2026 live -- trendjoin_daysize vs trendjoin_narrow:

                                  ES            NQ
  trades (IDENTICAL both ways)    213           127
  contracts                       213 -> 305    127 -> 174
  P&L per contract                +39 -> +74    +275 -> +367  (+91% / +34%)
  up-sized days, positive/negative  8 / 5         8 / 2
  best day's share of the gain      35%           28%

corr(prior session range, today's range) is +0.54 (ES) / +0.61 (NQ), and it is a
strictly prior measurement. The identical trade counts are the point: sizing
changed nothing about which trades happened.

Ships OFF by default anyway -- 34 and 28 sessions, 13 and 10 up-sized days, and
ES/NQ are the same calendar days so they are not independent. Doubling size
doubles the bad days too (-1,212 and -2,500 are in there).

WITHDRAWN, and why it is not used here: an "overnight range" version measured
+0.52/+0.51 and was LOOKAHEAD -- the cache files run 00:00-16:59 ET, so a window
of (et<09:30)|(et>=16:00) swallowed the hour AFTER the close, which sits near the
day's own extreme. Cleaned to 00:00-09:29 it is +0.16 / -0.06. Prior-session
range has no such overlap by construction, which is why it is the one used.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.features.day_range import DayRange     # noqa: E402

NS = 1_000_000_000
D0 = 1_786_973_400 * NS          # 09:30 ET


def session(dr, day, lo, hi):
    """One RTH session spanning lo..hi, then rolled."""
    t = D0 + day * 86_400 * NS
    dr.note(t, lo)
    dr.note(t + 60 * NS, hi)
    dr.roll_session()


def test_a_wider_than_usual_prior_session_sizes_up():
    dr = DayRange(min_sessions=3)
    for d in range(4):
        session(dr, d, 7000.0, 7040.0)        # four ordinary 40pt days
    session(dr, 4, 7000.0, 7120.0)            # then a 120pt day
    assert dr.prior_wide() is True
    assert dr.size_mult(wide=2.0, narrow=1.0) == 2.0


def test_a_narrower_than_usual_prior_session_sizes_down():
    dr = DayRange(min_sessions=3)
    for d in range(4):
        session(dr, d, 7000.0, 7040.0)
    session(dr, 4, 7000.0, 7010.0)            # a 10pt day
    assert dr.prior_wide() is False
    assert dr.size_mult(wide=2.0, narrow=1.0) == 1.0


def test_it_compares_against_the_sessions_BEFORE_it_not_including_itself():
    """A median that contains the day being judged drags itself toward the
    middle and blunts exactly the comparison being made."""
    dr = DayRange(min_sessions=3)
    for lo_hi in ((7000.0, 7010.0), (7000.0, 7020.0), (7000.0, 7030.0)):
        session(dr, 0, *lo_hi)
    session(dr, 1, 7000.0, 7025.0)            # 25 > median(10,20,30)=20
    assert dr.prior_wide() is True


def test_it_fails_closed_while_cold():
    dr = DayRange(min_sessions=3)
    assert dr.prior_wide() is None
    assert dr.size_mult(wide=2.0, narrow=1.0) == 1.0, "cold must not size up"
    session(dr, 0, 7000.0, 7040.0)
    assert dr.prior_wide() is None
    assert dr.size_mult(wide=2.0) == 1.0


# ── the sleeve side ──────────────────────────────────────────────────────
from engine.core.events import Bar                          # noqa: E402
from engine.strategies.trend_join import TrendJoinStrategy   # noqa: E402


def _warm(dr, n=5, rng=40.0):
    for d in range(n):
        session(dr, d, 7000.0, 7000.0 + rng)


def _sleeve(dr, size_wide=2.0):
    s = TrendJoinStrategy("ES", conf_pts=2.0, stop_pts=20.0, lookback=5)
    s.wants_day_range = True
    s.day_range = dr
    s.size_wide = size_wide
    return s


def _enter(s, day=9):
    """Walk a flat window then break out; return the entry order."""
    t0 = D0 + day * 86_400 * NS
    out = []
    for k, px in enumerate([7000.0] * 6 + [7004.0]):
        out += s.on_bar(Bar(t0 + k * 60 * NS, "1m", px, px + 0.25, px - 0.25,
                            px, 100, "ES"))
    return out


def test_the_entry_is_sized_up_after_a_wide_prior_session():
    dr = DayRange(min_sessions=3)
    _warm(dr)
    session(dr, 8, 7000.0, 7120.0)            # yesterday was wide
    s = _sleeve(dr)
    orders = _enter(s)
    assert orders and orders[0].qty == 2, f"expected 2 lots, got {orders}"


def test_the_entry_is_normal_size_after_a_narrow_prior_session():
    dr = DayRange(min_sessions=3)
    _warm(dr)
    session(dr, 8, 7000.0, 7005.0)            # yesterday was narrow
    s = _sleeve(dr)
    orders = _enter(s)
    assert orders and orders[0].qty == 1


def test_sizing_is_off_unless_the_sleeve_opts_in():
    dr = DayRange(min_sessions=3)
    _warm(dr)
    session(dr, 8, 7000.0, 7120.0)
    s = _sleeve(dr, size_wide=0.0)            # 0 = feature disabled
    orders = _enter(s)
    assert orders and orders[0].qty == 1, "default behaviour must be unchanged"
