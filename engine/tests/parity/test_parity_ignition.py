"""Gate B — ignition strategy parity.

The PARITY GATE is the research per-signal evaluation oracle (independent forward
P&L per validated ignition; the methodology that produced +1004). It validates
that the engine's feature/regime/exit LOGIC is faithful to the research. The live
IgnitionStrategy applies the same logic single-position (realistic, lower); that
number is reported for context, not gated.

Opt-in: pytest --run-parity tests/parity/test_parity_ignition.py -s
"""
import json
from pathlib import Path

import pytest

from engine.adapters.questdb import QuestDB
from engine.strategies.ignition_oracle import evaluate_ignition

ROOT = Path(__file__).resolve().parents[2]
STATES = json.loads((ROOT / "config" / "hmm_1h_states.json").read_text())
TARGET = 1004.0      # research per-signal eval, zone-target (STRATEGY_SPEC v1.3)
TOL = 0.15
REF = {"2025-04": 699, "2025-05": 72, "2025-02": 64, "2025-03": 170}


def _db_up() -> bool:
    try:
        QuestDB(timeout=5).query("SELECT 1")
        return True
    except Exception:
        return False


pytestmark = [pytest.mark.parity,
              pytest.mark.skipif(not _db_up(), reason="QuestDB not reachable")]


@pytest.fixture(scope="session")
def oracle_months():
    q = QuestDB()
    months: dict[str, tuple[int, float]] = {}
    for sym in ("ESM5", "ESH5"):
        months.update(evaluate_ignition(sym, STATES, q))
    return months


def test_ignition_total_within_band(oracle_months):
    total = sum(pnl for _, pnl in oracle_months.values())
    print("\n=== Ignition parity oracle (Gate B) ===")
    print(f"{'month':<9}{'n':>6}{'net_pts':>10}{'ref':>8}")
    for m in sorted(oracle_months):
        n, pnl = oracle_months[m]
        print(f"{m:<9}{n:>6}{pnl:>10.1f}{REF.get(m, 0):>8}")
    print(f"{'TOTAL':<9}{'':>6}{total:>10.1f}{sum(REF.values()):>8}")
    print(f"target {TARGET:+.0f}  band +/-{TOL:.0%} -> [{(1-TOL)*TARGET:.0f}, {(1+TOL)*TARGET:.0f}]")
    assert (1 - TOL) * TARGET <= total <= (1 + TOL) * TARGET, \
        f"ignition oracle total {total:.1f} outside parity band"


def test_ignition_all_four_months_positive(oracle_months):
    assert len(oracle_months) == 4, f"expected 4 months, got {sorted(oracle_months)}"
    for m, (_, pnl) in oracle_months.items():
        assert pnl > 0, f"month {m} not positive ({pnl:.1f})"
