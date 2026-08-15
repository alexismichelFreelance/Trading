"""onbreak_lg gates on LONG gamma, and the live runner actually attaches a curve.

THE HYPOTHESIS, made before the data that will test it exists. Applying the
random-cull null to every filter we have (strategy_lab/filter_null_test.py, 36
pinned ES sessions) every one came out as noise -- except one inversion:

    ES:onbreak_lg  as built (want="short")   22 of 29 days   -3,800   p = 0.974
    ES:onbreak_lg  INVERTED (its rejects)     7 of 29 days   +5,488   p = 0.033

Trading only the days the gate REJECTED beat a random 7-day draw (+485 mean).
The mechanism, if it is real: onbreak trades a break of the OVERNIGHT range, and
I classified it as a continuation sleeve and gated it want="short" on that
basis. An overnight-range break may simply work better in the pinning regime --
in which case the gate was pointed the wrong way, not useless.

WHY THIS IS A FORWARD TEST AND NOT A RESULT. p=0.033 is one of 7 tests run, so
the chance of something this extreme by luck is ~21%. The inversion arm only
fires when p>0.80, meaning I built a rule that searches for exactly this shape --
finding it is not independent evidence. n=7 days. And onbreak is the sleeve
whose "+3,912 in LONG pockets" turned out to be a single trade. So this is
flipped and left to run: a prediction made before the data, which is the only
way it can count for anything.

Also pinned here: the live runner must ATTACH the curve. _wants_curve was set on
the twin and read only by the replay, so a forward test would have run
ungated -- the twin identical to its original, and weeks of "evidence" worth
nothing. That is the same failure as the 2025 _gex twins being byte-identical to
their raw counterparts because the wire was never connected.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from engine.features.gamma_curve import GammaCurve  # noqa: E402
from engine.strategies.base import BaseStrategy  # noqa: E402
from run_live import _make  # noqa: E402

BASIS = 20.0
ROWS = [(7700.0, 1.0, -6.0), (7750.0, 2.0, -5.0), (7800.0, 9.0, -1.0),
        (7850.0, 8.0, -1.0), (7900.0, 6.0, -1.0)]     # crossing at 7800
CURVE = GammaCurve.from_rows(ROWS, spot=7860.0, basis=BASIS, underlying="SPX")
LONG_PX = 7870.0 + BASIS       # above the crossing
SHORT_PX = 7740.0 + BASIS      # below it


def test_onbreak_lg_now_wants_long_gamma():
    """THE FLIP."""
    s = _make("onbreak_lg", symbol="ES")
    assert getattr(s, "regime_want", None) == "long", (
        "onbreak_lg still gates want='short'; the inversion that beat the null "
        "was its REJECTS (+5,488 over 7 days, p=0.033)")


def test_the_other_lg_twins_are_unchanged():
    """Only onbreak showed the inversion. Flipping the others too would be
    fitting the whole family to one result."""
    for lb in ("ignition_lg", "flow_lg"):
        s = _make(lb, symbol="ES")
        assert getattr(s, "regime_want", None) is None, f"{lb} was flipped too"


def test_an_override_actually_changes_the_answer():
    s = BaseStrategy()
    s.pocket = CURVE
    s.regime_want = "long"
    assert s.gamma_entry_ok(0, "short", LONG_PX), (
        "want='long' must ALLOW a long-gamma price even though the call site "
        "asked for 'short'")
    assert not s.gamma_entry_ok(0, "short", SHORT_PX)


def test_without_an_override_the_call_site_wins():
    s = BaseStrategy()
    s.pocket = CURVE
    assert s.gamma_entry_ok(0, "short", SHORT_PX)
    assert not s.gamma_entry_ok(0, "short", LONG_PX)


def test_the_live_runner_attaches_a_curve_to_every_sleeve_that_wants_one():
    """_wants_curve was read ONLY by the replay. Live, the twin would have run
    with pocket=None -- no gate at all -- and looked like its original while
    accumulating weeks of meaningless 'evidence'."""
    import run_live
    assert hasattr(run_live, "attach_curves"), (
        "run_live has no way to give a sleeve its GammaCurve; the forward test "
        "would run ungated")
    sleeves = [_make(lb, symbol="ES") for lb in
               ("onbreak_lg", "ignition_lg", "flow_lg", "trendjoin_pk", "wallfade")]
    run_live.attach_curves(sleeves, "ES", "2026-08-15", curve=CURVE)
    for s in sleeves:
        assert s.pocket is CURVE, f"{type(s).__name__} never got the curve"


def test_attaching_fails_open():
    """No gamma for this instrument, a dead DB, a missing fetch -- the sleeve
    must keep trading UNGATED, never halt and never guess. GC has no gex
    mapping, so it exercises the same early return as a failed load."""
    import run_live
    s = _make("onbreak_lg", symbol="ES")
    run_live.attach_curves([s], "GC", "2026-08-15")
    assert s.pocket is None
    assert s.gamma_entry_ok(0, "short", LONG_PX) is True
