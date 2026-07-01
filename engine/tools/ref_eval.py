"""CLI for the ignition parity oracle (research per-signal evaluation).

    .venv/Scripts/python.exe tools/ref_eval.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.strategies.ignition_oracle import evaluate_ignition   # noqa: E402

STATES = json.loads((ROOT / "config" / "hmm_1h_states.json").read_text())


def main() -> None:
    grand = 0.0
    for sym in ("ESM5", "ESH5"):
        bm = evaluate_ignition(sym, STATES)
        print(f"\n=== ignition oracle {sym} ===")
        for m, (n, pnl) in bm.items():
            print(f"  {m}  n={n:4d}  net={pnl:+8.1f}")
            grand += pnl
    print(f"\nGRAND TOTAL net points = {grand:+.1f}  (target +1004, research per-signal eval)")


if __name__ == "__main__":
    main()
