"""AnchoredVWAP / MultiAVWAP: the incremental math matches a direct
volume-weighted mean, sigma is the volume-weighted dispersion, and the periodic
anchors reset on their own boundaries. Also cross-checks the study's prefix-sum
AVWAP against the incremental class on a random series."""
import numpy as np

from engine.features.avwap import AnchoredVWAP, MultiAVWAP


def _direct(px, vol):
    px, vol = np.asarray(px, float), np.asarray(vol, float)
    v = vol.sum()
    m = (px * vol).sum() / v
    var = (vol * (px - m) ** 2).sum() / v
    return m, np.sqrt(max(var, 0.0))


def test_value_and_sigma_match_direct():
    px = [100.0, 101.0, 102.5, 99.0, 100.25]
    vol = [10, 5, 8, 20, 3]
    a = AnchoredVWAP()
    for p, v in zip(px, vol):
        a.add(p, v)
    m, s = _direct(px, vol)
    assert abs(a.value - m) < 1e-9
    assert abs(a.sigma - s) < 1e-9
    lo, hi = a.band(2.0)
    assert lo < a.value < hi and abs((hi - lo) / 2 - 2 * s) < 1e-9


def test_reset_and_zero_volume():
    a = AnchoredVWAP()
    a.add(50.0, 100)
    a.reset(123)
    assert a.n == 0 and a.anchor_ts == 123 and a.cum_v == 0.0
    assert a.value != a.value                       # NaN before any volume
    a.add(200.0, 0)                                 # zero volume contributes nothing
    assert a.n == 0 and a.value != a.value
    a.add(200.0, 5)
    assert a.value == 200.0


def test_multiavwap_periodic_resets():
    mp = MultiAVWAP(("rth_open", "wtd", "mtd"))
    # day1 (week W1, month M1)
    mp.update(1, 100.0, 10, "2025-04-01", "2025-W14", "2025-04")
    mp.update(2, 102.0, 10, "2025-04-01", "2025-W14", "2025-04")
    # day2, same week+month -> rth_open resets, wtd/mtd keep accumulating
    mp.update(3, 108.0, 10, "2025-04-02", "2025-W14", "2025-04")
    assert abs(mp.value("rth_open") - 108.0) < 1e-9           # only day2's bar
    assert abs(mp.value("wtd") - (100 + 102 + 108) / 3) < 1e-9  # all three
    assert abs(mp.value("mtd") - (100 + 102 + 108) / 3) < 1e-9
    # new month -> mtd resets, wtd (new week too) resets
    mp.update(4, 90.0, 10, "2025-05-01", "2025-W18", "2025-05")
    assert abs(mp.value("mtd") - 90.0) < 1e-9
    assert abs(mp.value("wtd") - 90.0) < 1e-9


def test_prefix_sum_matches_incremental():
    """The study computes AVWAP by prefix sums; assert it equals the class for a
    fixed anchor on a random walk."""
    rng = np.random.default_rng(0)
    p = 100 + np.cumsum(rng.normal(0, 1, 500))
    v = rng.integers(1, 100, 500).astype(float)
    anchor = 137
    P = np.concatenate([[0.0], np.cumsum(p * v)])
    V = np.concatenate([[0.0], np.cumsum(v)])
    prefix = (P[500] - P[anchor]) / (V[500] - V[anchor])
    a = AnchoredVWAP()
    for i in range(anchor, 500):
        a.add(p[i], v[i])
    assert abs(prefix - a.value) < 1e-6
