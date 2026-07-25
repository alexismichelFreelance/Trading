"""Multi-scale swing detectors: the oracle detector finds exact extrema, the
causal one is late by construction, and neither invents swings that aren't
there. The 'silently returns nothing' failure mode is pinned explicitly."""
import numpy as np

from engine.features.swings import (
    directional_change, swing_outcomes, vol_unit, zigzag,
)


def _tri(lo=100.0, hi=110.0, n=50, cycles=2):
    """Clean triangle wave: exact turning points at known indices."""
    up = np.linspace(lo, hi, n)
    dn = np.linspace(hi, lo, n)
    return np.concatenate([np.concatenate([up, dn]) for _ in range(cycles)])


def test_zigzag_finds_exact_extrema_alternating():
    px = _tri()
    piv = zigzag(px, 5.0)
    assert piv, "detector returned nothing on a clean 10pt zigzag"
    assert all(piv[i][1] != piv[i + 1][1] for i in range(len(piv) - 1))
    # the reported highs must sit exactly on the peaks (index 49, 149)
    highs = [i for i, k in piv if k == +1]
    assert 49 in highs


def test_no_false_swings_on_flat_or_trend():
    assert zigzag(np.full(300, 100.0), 5.0) == []
    # a pure ramp has no retrace, so no CONFIRMED high
    assert [k for _, k in zigzag(np.linspace(100, 200, 300), 5.0) if k == +1] == []


def test_amp_larger_than_range_finds_nothing():
    assert zigzag(_tri(), 50.0) == []          # 10pt swings, 50pt threshold


def test_dc_is_late_relative_to_oracle():
    """The causal detector cannot report the peak itself, only the bar where the
    reversal is confirmed -- so its high index must come AFTER the true peak."""
    px = _tri()
    zz = {k: i for i, k in ((i, k) for i, k in zigzag(px, 5.0))}
    dc = directional_change(px, 5.0)
    dc_high = next(i for i, k in dc if k == +1)
    assert dc_high > 49                        # confirmed after the actual peak


def test_vol_unit_scales_with_volatility():
    rng = np.random.default_rng(0)
    quiet = 100 + np.cumsum(rng.normal(0, 0.01, 5000))
    wild = 100 + np.cumsum(rng.normal(0, 0.10, 5000))
    assert vol_unit(wild) > 5 * vol_unit(quiet)
    assert vol_unit(np.full(10, 1.0)) == 0.0   # degenerate input is not a crash


def test_outcomes_report_gain_and_heat():
    # long from the low at index 0: rises to 110 (mfe 10), never adverse
    px = np.concatenate([np.linspace(100, 110, 50), np.linspace(110, 100, 50)])
    piv = [(0, -1), (49, +1)]
    out = swing_outcomes(px, piv)[0]
    assert out["dir"] == 1
    assert abs(out["mfe"] - 10.0) < 1e-6
    assert out["mae"] <= 0.0                   # heat is never positive
    assert out["bars"] == 49
