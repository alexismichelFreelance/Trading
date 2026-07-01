"""Gate C — zone-lifecycle parity. Oracle reproduces the sized sleeve:
FADE +$17,534 / BREAK +$5,180 / FLIP +$24,789 / COMBINED +$47,502, all months +.
Opt-in: pytest --run-parity ... -s"""
from collections import defaultdict

import pytest

from engine.adapters.questdb import QuestDB
from engine.strategies.zones_oracle import evaluate_zones

TARGET = 47502.0
TOL = 0.15
SETUP_TARGET = {"FADE": 17534, "BREAK": 5180, "FLIP": 24789}


def _db_up() -> bool:
    try:
        QuestDB(timeout=5).query("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = [pytest.mark.parity, pytest.mark.skipif(not _db_up(), reason="QuestDB not reachable")]


@pytest.fixture(scope="session")
def zone_results():
    q = QuestDB()
    setups = defaultdict(lambda: [0, 0.0])
    months = defaultdict(lambda: [0, 0.0])
    for sym in ("ESM5", "ESH5"):
        r = evaluate_zones(sym, q)
        for k, (n, d) in r["setups"].items():
            setups[k][0] += n
            setups[k][1] += d
        for k, (n, d) in r["months"].items():
            months[k][0] += n
            months[k][1] += d
    return {"setups": dict(setups), "months": dict(months)}


def test_zones_combined_within_band(zone_results):
    total = sum(d for _, d in zone_results["setups"].values())
    print("\n=== Zone lifecycle parity (Gate C) ===")
    for k in ("FADE", "BREAK", "FLIP"):
        n, d = zone_results["setups"][k]
        print(f"  {k:6s} n={n:3d}  ${d:+11,.0f}  (target ${SETUP_TARGET[k]:+,})")
    print(f"  COMBINED    ${total:+11,.0f}  (target ${TARGET:+,.0f})")
    assert (1 - TOL) * TARGET <= total <= (1 + TOL) * TARGET, \
        f"zones combined {total:.0f} outside parity band"


def test_zones_each_setup_positive_and_in_band(zone_results):
    for k, tgt in SETUP_TARGET.items():
        n, d = zone_results["setups"][k]
        assert d > 0, f"{k} not positive"
        assert (1 - TOL) * tgt <= d <= (1 + TOL) * tgt, f"{k} ${d:.0f} outside band of ${tgt}"


def test_zones_all_four_months_positive(zone_results):
    months = zone_results["months"]
    assert len(months) == 4, f"expected 4 months, got {sorted(months)}"
    for m, (_, d) in months.items():
        assert d > 0, f"month {m} not positive (${d:.0f})"
