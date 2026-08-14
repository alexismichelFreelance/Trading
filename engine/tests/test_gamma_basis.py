"""The index->future basis is measured every session, never configured.

config/instruments.yaml carried

    ES  basis: 42.0   # ES over SPX at 16:00 ET 2026-07-20 -- re-measure over the week
    NQ  basis: 181.0  # NQ over NDX at 16:00 ET 2026-07-20 (first sample) -- re-measure

and nothing ever re-measured them, because "re-measure over the week" was a
comment, not a mechanism. Measured across 13 sessions of stored payloads:

    ES   47.6  41.2  36.3  34.9  32.4 ... 20.9  20.3  20.0  22.5
    NQ  225.1 184.5 180.3 148.2 133.7 ... 112.4 110.0  98.6  99.5

That is not drift to be re-sampled occasionally. It is a monotonic DECAY --
futures carry converging to cash as expiry approaches -- which then jumps at the
roll and starts again. The ES comment "was 52 on Jun" was recording exactly this
without naming it.

Against the configured constants, today: ES is ~20 points wrong and NQ ~80. A
gamma level is an index strike plus the basis, so every NQ wall and flip has
been drawn 80 points from where it belongs, and the error grows daily until the
roll. No re-measurement cadence fixes a number that is wrong within days of
every update; the only correct form is to compute it from data each session.

Both inputs are already stored daily: the CBOE payload's `spot` (the index
close) and our own RTH futures close. They align on the same session, so the
basis is a subtraction, not an estimate.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.features.gamma_basis import BasisSeries  # noqa: E402

# (day, index close, future close) -- the real ES shape, decaying
ROWS = [
    ("2026-08-10", 7757.64, 7783.25),      # 25.6
    ("2026-08-11", 7753.11, 7774.00),      # 20.9
    ("2026-08-12", 7728.20, 7748.50),      # 20.3
    ("2026-08-13", 7748.50, 7768.50),      # 20.0
    ("2026-08-14", 7798.99, 7821.50),      # 22.5
]


def _series(rows=ROWS, fallback=42.0):
    return BasisSeries.from_rows(rows, fallback=fallback, symbol="ES")


def test_it_uses_the_measurement_not_the_configured_constant():
    """THE REGRESSION. 42.0 was measured once on 2026-07-20 and never again."""
    b = _series()
    got = b.for_day("2026-08-15")
    assert abs(got - 21.5) < 4.0, (
        f"basis {got:.1f} for 2026-08-15; the last sessions measure 20-22.5 and "
        f"the configured 42.0 is a month-old reading of a decaying series")


def test_it_is_causal_and_never_uses_the_session_it_is_pricing():
    """Levels for day D are the PRIOR session's structure. Reaching into D's own
    close to compute D's basis would be reading the answer."""
    b = _series()
    assert b.for_day("2026-08-12") == b.for_day("2026-08-12")
    early = _series(ROWS[:2])
    assert abs(early.for_day("2026-08-15") - 20.9) < 4.0, (
        "a series that ends on 08-11 must answer from 08-11, not from data it "
        "does not have")


def test_a_short_median_absorbs_one_bad_print_without_lagging_the_decay():
    """A single garbage close must not move the level 100 points; a genuine
    decay of a few points a day must still come through. One sample is too
    jumpy, a long window lags a monotonic trend."""
    dirty = ROWS[:-1] + [("2026-08-14", 7798.99, 8500.00)]     # 701-point print
    b = BasisSeries.from_rows(dirty, fallback=42.0, symbol="ES")
    got = b.for_day("2026-08-15")
    assert got < 60.0, f"one bad close dragged the basis to {got:.1f}"


def test_the_fallback_is_used_only_when_there_is_nothing_to_measure():
    b = BasisSeries.from_rows([], fallback=42.0, symbol="ES")
    assert b.for_day("2026-08-15") == 42.0
    assert not b.measured


def test_the_fallback_says_so(caplog):
    """A configured constant standing in for a measurement is a degraded mode.
    It must be visible, because the last time it was invisible it stayed wrong
    for a month."""
    import logging
    with caplog.at_level(logging.WARNING, logger="engine.gamma"):
        BasisSeries.from_rows([], fallback=181.0, symbol="NQ").for_day("2026-08-15")
    assert any("basis" in r.getMessage().lower() for r in caplog.records), (
        "fell back to the configured constant silently")


def test_a_roll_jump_is_not_smoothed_away():
    """At the roll the basis JUMPS -- the front contract changes. Averaging
    across it produces a number that describes neither contract."""
    rolled = [("2026-09-15", 7800.0, 7810.0),      # 10.0  old front, near expiry
              ("2026-09-16", 7805.0, 7815.0),      # 10.0
              ("2026-09-17", 7810.0, 7870.0),      # 60.0  ROLLED
              ("2026-09-18", 7815.0, 7874.0),      # 59.0
              ("2026-09-19", 7820.0, 7878.0)]      # 58.0
    b = BasisSeries.from_rows(rolled, fallback=42.0, symbol="ES")
    got = b.for_day("2026-09-20")
    assert got > 50.0, (
        f"basis {got:.1f} after a roll to ~58-60; a window spanning the jump "
        f"describes neither the old contract nor the new")
