"""Gate D — flow-following parity. Oracle reproduces +814 pts (asymmetric hold,
a1/h5), all four months positive. Opt-in: pytest --run-parity ... -s"""
import pytest

from engine.adapters.questdb import QuestDB
from engine.strategies.flow_oracle import evaluate_flow

TARGET = 814.0
TOL = 0.15


def _db_up() -> bool:
    try:
        QuestDB(timeout=5).query("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = [pytest.mark.parity, pytest.mark.skipif(not _db_up(), reason="QuestDB not reachable")]


@pytest.fixture(scope="session")
def flow_months():
    q = QuestDB()
    months: dict[str, float] = {}
    for sym in ("ESM5", "ESH5"):
        for m, (net, _turn) in evaluate_flow(sym, q).items():
            months[m] = months.get(m, 0.0) + net
    return months


def test_flow_total_within_band(flow_months):
    total = sum(flow_months.values())
    print("\n=== Flow parity (Gate D) ===")
    for m in sorted(flow_months):
        print(f"  {m}  {flow_months[m]:+8.1f}")
    print(f"TOTAL {total:+.1f}  (target {TARGET:+.0f}, band +/-{TOL:.0%})")
    assert (1 - TOL) * TARGET <= total <= (1 + TOL) * TARGET, \
        f"flow total {total:.1f} outside parity band"


def test_flow_all_four_months_positive(flow_months):
    assert len(flow_months) == 4, f"expected 4 months, got {sorted(flow_months)}"
    for m, pnl in flow_months.items():
        assert pnl > 0, f"month {m} not positive ({pnl:.1f})"
