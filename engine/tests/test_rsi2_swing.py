"""RSI2SwingStrategy must reproduce the researched rule exactly.

The 16-year record (t=+3.5, ret/DD 5.96, and the only sleeve that improves IBS
in a blend) is only worth anything if the live sleeve computes the same signal
the study did. These pin the four ways that can silently go wrong:

  * the indicators must use DAILY closes, not 1m bars -- a 2-period RSI over
    minute bars is a different, meaningless number;
  * today's close must participate in today's decision, and must not then be
    double-counted at the session rollover;
  * the 200dMA filter must actually gate (it is what separates this sleeve from
    the unfiltered version, which is worse);
  * it holds overnight, so it must be exempt from the engine's session flat.

Tests that exercise COLD warm-up pass seed=[] explicitly: the sleeve now ships
with config/rsi2_seed_ES.json, so a bare constructor is seeded and would not be
testing the cold path at all.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.events import Bar  # noqa: E402
from engine.strategies.rsi2_swing import RSI2SwingStrategy  # noqa: E402

ET = ZoneInfo("America/New_York")


def _ts(day: int, hh: int, mm: int) -> int:
    d = datetime(2026, 1, 5, hh, mm, tzinfo=ET) + timedelta(days=day)
    return int(d.timestamp() * 1e9)


def _session(s: RSI2SwingStrategy, day: int, close: float):
    """One session: a mid-day bar then the 15:59 decision bar."""
    s.on_bar(Bar(_ts(day, 11, 0), "1m", close, close, close, close, 1, "ES"))
    return s.on_bar(Bar(_ts(day, 15, 59), "1m", close, close, close, close, 1, "ES"))


def _warm(s: RSI2SwingStrategy, n: int, start: float, step: float) -> float:
    """Feed a rising series so price sits above the slow MA and RSI2 is high."""
    px = start
    for d in range(n):
        _session(s, d, px)
        px += step
    return px


def test_holds_overnight_is_declared():
    assert RSI2SwingStrategy.holds_overnight is True


def test_no_signal_before_enough_history():
    s = RSI2SwingStrategy("ES", ma_slow=20, seed=[])
    for d in range(5):
        assert _session(s, d, 5000.0 + d) == []


def test_entry_requires_rsi_below_threshold_and_price_above_slow_ma():
    """Two down closes after a long rise drives RSI(2) to ~0 while price is
    still well above the 20-day MA -> the sleeve must buy."""
    s = RSI2SwingStrategy("ES", ma_slow=20, seed=[])
    px = _warm(s, 30, 5000.0, 10.0)
    assert _session(s, 30, px - 30) == []          # one down close: RSI2 not low enough yet
    out = _session(s, 31, px - 60)
    assert out and out[0].side == 1 and out[0].tag == "rsi2-entry", (
        f"expected an entry after two down closes, got {out}")


def test_slow_ma_filter_blocks_entry_below_it():
    """Identical RSI(2) condition, but price under the slow MA: no trade. This
    filter is the difference between this sleeve and a worse one."""
    s = RSI2SwingStrategy("ES", ma_slow=20, seed=[])
    px = _warm(s, 30, 5000.0, 10.0)
    # collapse far below the MA, still producing consecutive down closes
    assert _session(s, 30, px - 2000) == []
    assert _session(s, 31, px - 2100) == []


def test_exit_on_close_above_fast_ma():
    s = RSI2SwingStrategy("ES", ma_slow=20, seed=[])
    px = _warm(s, 30, 5000.0, 10.0)
    _session(s, 30, px - 30)
    out = _session(s, 31, px - 60)
    assert out and out[0].side == 1
    s.on_position(type("P", (), {"qty": 1})())
    ex = _session(s, 32, px + 500)                 # far above the 5-day MA
    assert ex and ex[0].side == -1 and ex[0].reduce_only, f"expected exit, got {ex}"


def test_decides_once_per_session():
    """Repeated late bars must not re-fire the decision."""
    s = RSI2SwingStrategy("ES", ma_slow=20, seed=[])
    px = _warm(s, 30, 5000.0, 10.0)
    _session(s, 30, px - 30)
    first = _session(s, 31, px - 60)
    assert first
    again = s.on_bar(Bar(_ts(31, 15, 59), "1m", px, px, px, px, 1, "ES"))
    assert again == [], "the 15:59 decision fired twice in one session"


def test_todays_close_is_not_double_counted():
    """The decision consults today's close, then the rollover appends it once.
    If it were appended twice the MAs would be wrong from day two onward."""
    s = RSI2SwingStrategy("ES", ma_slow=20, seed=[])
    _warm(s, 10, 5000.0, 10.0)
    assert len(s._closes) == 9, (
        f"expected 9 completed closes after 10 sessions, got {len(s._closes)}")


def test_only_1m_bars_drive_it():
    s = RSI2SwingStrategy("ES", ma_slow=20, seed=[])
    assert s.on_bar(Bar(_ts(0, 15, 59), "30m", 5000, 5000, 5000, 5000, 1, "ES")) == []


# ── seeding ──────────────────────────────────────────────────────────────────
# Without a seed the 200-day MA needs 200 SESSIONS to warm from cold and the
# sleeve sits inert for a year while looking perfectly healthy. These pin that
# it arms immediately AND that seeding cannot double-count the live session.

def test_seed_arms_the_slow_ma_on_the_first_bar():
    # dates must all precede the live session (_ts(0,..) = 2026-01-05), else
    # they are correctly dropped as "the feed owns the current day"
    seed = [(f"2025-{1+i//28:02d}-{1+i%28:02d}", 5000.0 + i) for i in range(210)]
    s = RSI2SwingStrategy("ES", seed=seed)
    assert s._ma(200) is None, "must not be armed before any bar"
    s.on_bar(Bar(_ts(0, 11, 0), "1m", 5300, 5300, 5300, 5300, 1, "ES"))
    assert s._ma(200) is not None, "seed did not arm the slow MA"


def test_seed_rows_on_or_after_the_live_session_are_dropped():
    """The live feed owns the current day. A seed row dated today would be
    counted once from the file and again at the session rollover."""
    day = "2026-01-05"                     # the session _ts(0, ...) lands on
    seed = [("2026-01-02", 4990.0), (day, 5000.0), ("2026-01-06", 5010.0)]
    s = RSI2SwingStrategy("ES", ma_slow=2, seed=seed)
    s.on_bar(Bar(_ts(0, 11, 0), "1m", 5000, 5000, 5000, 5000, 1, "ES"))
    assert list(s._closes) == [4990.0], (
        f"seed kept a row dated >= the live session: {list(s._closes)}")


def test_missing_seed_file_does_not_crash():
    """A missing seed must log loudly and degrade, never take the engine down."""
    s = RSI2SwingStrategy("ES", ma_slow=20, seed_path="does/not/exist.json")
    assert s._seed == []
    assert s.on_bar(Bar(_ts(0, 11, 0), "1m", 5000, 5000, 5000, 5000, 1, "ES")) == []


def test_shipped_seed_file_loads_and_is_long_enough():
    """The real config/rsi2_seed_ES.json must exist and cover the slow MA."""
    s = RSI2SwingStrategy("ES")
    assert len(s._seed) >= 200, f"shipped seed has only {len(s._seed)} closes"
