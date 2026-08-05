"""TrendJoinStrategy must reproduce the measured rule exactly.

The research (strategy_lab/move_catalog.py -> move_catchability.py) is only
worth anything if the live sleeve fires where the study fired. Each parameter
came from a measurement, not a sweep, and each is pinned here:

  * CONF 15pt beyond the 30-bar extreme -- the level where >=20pt legs still
    qualify 98% of the time and sub-20pt legs only 47%. The confirmation IS the
    filter; there is no separate predictor.
  * STOP 8pt -- the measured median MAE of a caught leg is 7.5pt. The book's
    existing chop_stop of 4.0pt ejects before the move works.
  * HOLD 90 min -- median duration of a >=20pt leg is 47 min and 42% run past
    an hour. Second-scale exits cannot hold these.

Also pinned: one position at a time (the backtest never pyramided), RTH only,
and that it does NOT hold overnight -- unlike the IBS/RSI2 swing sleeves this
is intraday and must obey the engine's session flat.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar  # noqa: E402
from engine.strategies.trend_join import TrendJoinStrategy  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(hh: int, mm: int) -> int:
    # callers walk minutes past 59 (e.g. 9:30 + 31 bars); normalise rather than
    # making every test do the arithmetic
    hh, mm = hh + mm // 60, mm % 60
    return int(datetime(2026, 3, 10, hh, mm, tzinfo=ET).timestamp() * 1e9)


def _bar(hh: int, mm: int, px: float, hi: float | None = None,
         lo: float | None = None) -> Bar:
    return Bar(_ts(hh, mm), "1m", px, hi if hi is not None else px,
               lo if lo is not None else px, px, 100, "ES")


def _flat_window(s: TrendJoinStrategy, px: float = 5000.0, n: int = 31):
    """Fill the lookback with a flat market so no signal is pending."""
    out = []
    for k in range(n):
        out.append(s.on_bar(_bar(9, 30 + k, px)))
    return out


def test_does_not_hold_overnight():
    assert TrendJoinStrategy.holds_overnight is False


def test_no_entry_before_window_is_full():
    s = TrendJoinStrategy("ES", lookback=30, conf_pts=15.0)
    for k in range(20):
        assert s.on_bar(_bar(9, 30 + k, 5000.0 + k)) == []


def test_long_entry_on_confirmation_above_window_low():
    s = TrendJoinStrategy("ES", lookback=30, conf_pts=15.0, stop_pts=8.0)
    _flat_window(s)
    assert s.on_bar(_bar(10, 5, 5010.0)) == []          # +10 only: not enough
    out = s.on_bar(_bar(10, 6, 5016.0))                 # +16 over the 30-bar low
    assert out and out[0].side == 1 and out[0].tag == "trendjoin-entry"


def test_short_entry_is_symmetric():
    s = TrendJoinStrategy("ES", lookback=30, conf_pts=15.0)
    _flat_window(s)
    out = s.on_bar(_bar(10, 6, 4984.0))
    assert out and out[0].side == -1


def test_stop_exits_at_the_measured_distance():
    s = TrendJoinStrategy("ES", lookback=30, conf_pts=15.0, stop_pts=8.0)
    _flat_window(s)
    s.on_bar(_bar(10, 6, 5016.0))
    s.on_position(type("P", (), {"qty": 1})())
    assert s.on_bar(_bar(10, 7, 5010.0)) == []          # -6: inside the stop
    out = s.on_bar(_bar(10, 8, 5007.5))                 # -8.5: through it
    assert out and out[0].side == -1 and out[0].reduce_only
    assert out[0].tag == "trendjoin-stop"


def test_time_cap_exits():
    s = TrendJoinStrategy("ES", lookback=30, conf_pts=15.0, stop_pts=8.0,
                          hold_min=5)
    _flat_window(s)
    s.on_bar(_bar(10, 6, 5016.0))
    s.on_position(type("P", (), {"qty": 1})())
    for k in range(1, 5):
        assert s.on_bar(_bar(10, 6 + k, 5016.0)) == []
    out = s.on_bar(_bar(10, 11, 5016.0))
    assert out and out[0].reduce_only and out[0].tag == "trendjoin-timeout"


def test_only_one_position_at_a_time():
    """The backtest never pyramided; a second confirmation while long must not
    add. Otherwise size drifts and the measured per-trade number is meaningless."""
    s = TrendJoinStrategy("ES", lookback=30, conf_pts=15.0, stop_pts=40.0)
    _flat_window(s)
    s.on_bar(_bar(10, 6, 5016.0))
    s.on_position(type("P", (), {"qty": 1})())
    assert s.on_bar(_bar(10, 7, 5040.0)) == [], "pyramided into an open position"


def test_ignores_non_1m_bars():
    s = TrendJoinStrategy("ES", lookback=30)
    assert s.on_bar(Bar(_ts(10, 0), "30m", 5000, 5100, 4900, 5050, 1, "ES")) == []


def test_no_entry_outside_rth():
    s = TrendJoinStrategy("ES", lookback=30, conf_pts=15.0)
    for k in range(31):
        s.on_bar(_bar(4, 0 + k, 5000.0))
    assert s.on_bar(_bar(4, 40, 5030.0)) == [], "entered outside RTH"


# ── two-phase exit ───────────────────────────────────────────────────────────
# The 90-minute clock was never an exit thesis. On 2026-08-03 it cut a +45.25pt
# winner at 11:05 while the day ran another 33pt. With a TwoPhaseExit attached
# the clock must become INSURANCE -- fired only when nothing else has.

from engine.core.exits import TwoPhaseExit  # noqa: E402


def _tp() -> TwoPhaseExit:
    # arm on a fixed distance (arm_pts), the ruler that measured better than the
    # adaptive one in tools/exit_causal_sweep.py
    return TwoPhaseExit(rev_kind="retrace", rev_f=0.25, arm_pts=24.0)


def test_two_phase_does_not_touch_the_trade_before_arming():
    """PHASE 1 RIDE: below the arming distance there is no exit but the stop.
    This is what preserves the tail, so it must be verified, not assumed."""
    s = TrendJoinStrategy("ES", lookback=30, conf_pts=15.0, stop_pts=8.0,
                          two_phase=_tp())
    _flat_window(s)
    out = s.on_bar(_bar(10, 6, 5016.0))
    assert out and out[0].side == 1
    s.on_position(type("P", (), {"qty": 1})())
    # +20pt: a real winner, but under the 24pt arming distance
    for k, px in enumerate((5026.0, 5031.0, 5036.0)):
        assert s.on_bar(_bar(10, 7 + k, px)) == [], f"exited early at {px}"


def test_two_phase_exits_after_arming_and_giving_back():
    """PHASE 2 PROTECT: once armed, a give-back must end the trade -- and the
    tag must say so, not report a timeout."""
    s = TrendJoinStrategy("ES", lookback=30, conf_pts=15.0, stop_pts=40.0,
                          two_phase=_tp())
    _flat_window(s)
    s.on_bar(_bar(10, 6, 5016.0))
    s.on_position(type("P", (), {"qty": 1})())
    for k, px in enumerate((5030.0, 5045.0, 5060.0)):      # run to +44, arms
        s.on_bar(_bar(10, 7 + k, px))
    out = None
    for k, px in enumerate((5050.0, 5040.0, 5030.0)):      # give it back
        out = s.on_bar(_bar(10, 10 + k, px)) or out
    assert out, "armed, then gave back 30pt, and never exited"
    assert out[0].reduce_only and "timeout" not in out[0].tag, (
        f"exited by the clock rather than the thesis: {out[0].tag}")


def test_clock_still_fires_when_two_phase_never_arms():
    """Insurance must remain. A trade that drifts sideways forever still has to
    end."""
    s = TrendJoinStrategy("ES", lookback=30, conf_pts=15.0, stop_pts=40.0,
                          hold_min=5, two_phase=_tp())
    _flat_window(s)
    s.on_bar(_bar(10, 6, 5016.0))
    s.on_position(type("P", (), {"qty": 1})())
    out = None
    for k in range(6):
        out = s.on_bar(_bar(10, 7 + k, 5017.0)) or out
    assert out and out[0].tag == "trendjoin-timeout"


def test_no_two_phase_keeps_the_old_behaviour():
    """Default stays exactly as before, so the existing paper record is not
    silently redefined mid-stream."""
    s = TrendJoinStrategy("ES", lookback=30, conf_pts=15.0, stop_pts=40.0,
                          hold_min=3)
    _flat_window(s)
    s.on_bar(_bar(10, 6, 5016.0))
    s.on_position(type("P", (), {"qty": 1})())
    out = None
    for k in range(4):
        out = s.on_bar(_bar(10, 7 + k, 5100.0)) or out
    assert out and out[0].tag == "trendjoin-timeout"
