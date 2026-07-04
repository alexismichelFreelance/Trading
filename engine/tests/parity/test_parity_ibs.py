"""IBS swing parity: the daily oracle reproduces the fixed-window ES=F result
(2010-01-04..2026-06-30, 0.20/0.80, cost 0.517): n=435, total +4644.0 pts.
Needs gamma/esf_daily.csv (tools/fetch_daily.py); skips if absent.
Opt-in: pytest --run-parity tests/parity/test_parity_ibs.py -s"""
from pathlib import Path

import pytest

from engine.strategies.ibs_oracle import evaluate_ibs, load_daily_csv, summary

CSV = Path(__file__).resolve().parents[3] / "gamma" / "esf_daily.csv"
END = "2026-06-30"
TARGET_N = 435
TARGET_TOTAL = 4644.0
TOL = 0.02        # oracle vs itself on pinned data: tight

pytestmark = [pytest.mark.parity,
              pytest.mark.skipif(not CSV.exists(), reason="run tools/fetch_daily.py first")]


@pytest.fixture(scope="session")
def ibs_result():
    T = evaluate_ibs(load_daily_csv(CSV), end_date=END)
    return T, summary(T)


def test_ibs_fixed_window_parity(ibs_result):
    T, s = ibs_result
    print(f"\nIBS ES=F {END}: n={s['n']} total={s['total']:+.1f} mean={s['mean']:+.2f} "
          f"t={s['tstat']:+.1f} win={s['win']:.0%} worst={s['worst']:+.1f} maxDD={s['maxdd']:+.1f}")
    assert abs(s["n"] - TARGET_N) <= 3, f"trade count {s['n']} vs {TARGET_N}"
    assert abs(s["total"] - TARGET_TOTAL) <= TOL * abs(TARGET_TOTAL) + 25, \
        f"total {s['total']:.1f} vs {TARGET_TOTAL}"


def test_ibs_quality_floors(ibs_result):
    _, s = ibs_result
    assert s["tstat"] > 3.0
    assert s["win"] > 0.60
