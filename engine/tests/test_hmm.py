import json
from pathlib import Path

import numpy as np

from engine.features.efficiency import OnlineKaufmanER, kaufman_er
from engine.features.hmm import GaussianHMM2, fit_gaussian_hmm

CONFIG = Path(__file__).resolve().parents[1] / "config" / "hmm_es_1h.json"


def test_online_er_matches_vectorized():
    rng = np.random.default_rng(0)
    closes = 5000 + np.cumsum(rng.normal(0, 1, 200))
    vec = kaufman_er(closes, 3)
    online = OnlineKaufmanER(3)
    got = [online.update(c) for c in closes]
    for i in range(len(closes)):
        if np.isnan(vec[i]):
            assert got[i] is None
        else:
            assert abs(got[i] - vec[i]) < 1e-12


def test_efficiency_bounds():
    # monotone up -> ER == 1; zigzag -> ER < 1
    assert abs(kaufman_er([1, 2, 3, 4], 3)[-1] - 1.0) < 1e-12
    assert kaufman_er([1, 2, 1, 2], 3)[-1] < 0.5


def test_fit_recovers_separated_regimes():
    rng = np.random.default_rng(1)
    chop = rng.normal(0.35, 0.05, 150)
    trend = rng.normal(0.95, 0.03, 150)
    obs = np.concatenate([chop, trend, chop])
    p = fit_gaussian_hmm(obs, n_iter=35, seed_means=(0.3, 0.9))
    ts = p["trend_state"]
    assert p["means"][ts] > p["means"][1 - ts]
    assert p["means"][ts] > 0.8 and p["means"][1 - ts] < 0.55


def test_frozen_params_match_research():
    d = json.loads(CONFIG.read_text())
    ts = d["trend_state"]
    assert abs(d["means"][1 - ts] - 0.44) < 0.03    # chop ~0.44
    assert abs(d["means"][ts] - 1.0) < 0.03          # trend ~1.0


def test_online_filter_classifies_and_resets():
    hmm = GaussianHMM2.load(CONFIG)
    # a chop session stays chop
    for er in (0.4, 0.45, 0.42, 0.5, 0.38):
        hmm.update(er, "2025-04-01")
    assert not hmm.is_trend()
    # efficient hours flip it to trend (2nd obs onward; day starts in chop via pi)
    hmm.update(1.0, "2025-04-01")
    hmm.update(1.0, "2025-04-01")
    assert hmm.is_trend()
    # new session resets the forward filter -> first obs is chop again
    s = hmm.update(1.0, "2025-04-02")
    assert s == (1 - hmm.trend_state) or not hmm.is_trend()
