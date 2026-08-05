"""Stale regime data must degrade to "no opinion", never to a confident wrong one.

2026-07-29: the Trading_GEX_Daily task had been killed at its 15-minute Windows
time limit on every loaded trading day since 07-22 (exit 0xC000013A), and because
python block-buffers stdout into a redirect, the kill discarded every diagnostic
line -- the log held only a bare timestamp header. Nothing else noticed, because
`gexp_prev` returned the newest row it had with NO bound on its age. The user
spotted it by eye: "the gamma exposure numbers don't seem to change from one day
to the next".

So the bound is the fix, and this pins it. `gamma_entry_ok` fails OPEN on None, so
a stale table makes a *_gex sleeve behave as its ungated twin -- a known,
interpretable state -- instead of gating on fiction.
"""
from __future__ import annotations

import logging

from engine.features.gamma import MAX_AGE_DAYS, GammaRegime


def _regime(pairs):
    """A GammaRegime over (date, gexp) pairs, no database."""
    g = GammaRegime.__new__(GammaRegime)
    g._table = "claude_gex"
    g._stale_warned = None
    g._snap = ([d for d, _ in pairs], [v for _, v in pairs])
    return g


HIST = [("2026-07-21", 0.10), ("2026-07-22", 0.20), ("2026-07-23", 0.30)]


def test_fresh_prior_session_is_used():
    g = _regime(HIST)
    assert g.gexp_prev("2026-07-24") == 0.30      # yesterday
    assert g.age_days("2026-07-24") == 1
    assert g.is_short_gamma("2026-07-24") is True


def test_long_weekend_is_still_fresh():
    """Fri -> Mon is 3 days and a long weekend 4; both are still 'yesterday'."""
    g = _regime(HIST)
    assert g.gexp_prev("2026-07-27") == 0.30      # 4 days, at the bound
    assert g.age_days("2026-07-27") == 4
    assert MAX_AGE_DAYS == 4


def test_past_the_bound_returns_none_not_the_old_value():
    """The actual bug: this used to return 0.30 forever."""
    g = _regime(HIST)
    for day, age in (("2026-07-28", 5), ("2026-07-29", 6), ("2026-08-15", 23)):
        assert g.age_days(day) == age
        assert g.gexp_prev(day) is None, f"served {age}-day-old gamma for {day}"
        assert g.is_short_gamma(day) is None


def test_staleness_is_logged_loudly_and_once_per_day(caplog):
    g = _regime(HIST)
    with caplog.at_level(logging.ERROR, logger="engine.gamma"):
        for _ in range(50):                        # sleeves call this every bar
            g.gexp_prev("2026-07-29")
        g.gexp_prev("2026-07-30")                  # a new day warns again
    errs = [r for r in caplog.records if "GAMMA DATA STALE" in r.message]
    assert len(errs) == 2, f"expected one warning per day, got {len(errs)}"
    assert all(r.levelno >= logging.ERROR for r in errs)


def test_a_gex_sleeve_degrades_to_its_ungated_twin_when_stale():
    """End-to-end intent: stale gamma must not block or invert entries, it must
    simply stop having an opinion -- gamma_entry_ok fails open."""
    from engine.strategies.base import BaseStrategy

    class S(BaseStrategy):
        symbol = "ES"

        def __init__(self, gamma):
            self.gamma = gamma

    # 2026-07-29 12:00 UTC, comfortably inside that ET session
    ts = 1785499200 * 1_000_000_000
    stale = S(_regime(HIST))
    assert stale.gamma.is_short_gamma("2026-07-29") is None
    assert stale.gamma_entry_ok(ts, "short") is True   # open, not blocked
    assert stale.gamma_entry_ok(ts, "long") is True    # and not inverted

    fresh = S(_regime(HIST + [("2026-07-28", 0.90)]))
    assert fresh.gamma_entry_ok(ts, "short") is False  # long-gamma day: no trend
    assert fresh.gamma_entry_ok(ts, "long") is True


def test_no_history_at_all_is_none():
    g = _regime([])
    assert g.gexp_prev("2026-07-29") is None
    assert g.age_days("2026-07-29") is None
    assert g.is_short_gamma("2026-07-29") is None
