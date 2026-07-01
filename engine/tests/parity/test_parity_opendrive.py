"""Open-drive parity: the 1s oracle reproduces the studied +815 pts (64 sec-covered
days), all four months positive. Opt-in: pytest --run-parity ... -s"""
import pytest

from engine.adapters.questdb import QuestDB
from engine.strategies.open_drive_oracle import evaluate_open_drive

TARGET = 815.0
TOL = 0.15


def _db_up() -> bool:
    try:
        QuestDB(timeout=5).query("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = [pytest.mark.parity, pytest.mark.skipif(not _db_up(), reason="QuestDB not reachable")]


@pytest.fixture(scope="session")
def od_months():
    q = QuestDB()
    months: dict[str, tuple[int, float]] = {}
    for sym in ("ESM5", "ESH5"):
        for m, (n, pts) in evaluate_open_drive(sym, q).items():
            n0, p0 = months.get(m, (0, 0.0))
            months[m] = (n0 + n, p0 + pts)
    return months


def test_opendrive_total_within_band(od_months):
    total = sum(p for _, p in od_months.values())
    print("\n=== Open-drive parity ===")
    for m in sorted(od_months):
        n, p = od_months[m]
        print(f"  {m}  n={n:3d}  {p:+8.1f}")
    print(f"TOTAL {total:+.1f} (target {TARGET:+.0f} +/-{TOL:.0%})")
    assert (1 - TOL) * TARGET <= total <= (1 + TOL) * TARGET


def test_opendrive_all_months_positive(od_months):
    assert len(od_months) == 4
    for m, (_, p) in od_months.items():
        assert p > 0, f"month {m} not positive ({p:+.1f})"
