"""Regenerate the frozen per-hour regime states (deterministic) for both
contracts, exactly per engine_reference/hmm_regime_reference.md:

  - 1h Kaufman-efficiency computed PER SESSION DAY (L=3; first 3 bars null->chop),
  - causal forward-filter (frozen params, reset each day),
  - regime keyed by the UTC hour string "yyyy-MM-dd HH:00".

Writes config/hmm_1h_states.json {hour: 0|1}. The ignition strategy loads this
for the replay-parity path (the research applies the state by entry hour). Live
uses the online causal filter instead. Validates against the reference sample.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB           # noqa: E402
from engine.features.efficiency import kaufman_er     # noqa: E402
from engine.features.hmm import GaussianHMM2          # noqa: E402

CONTRACTS = ["ESM5", "ESH5"]
L = 3
OUT = ROOT / "config" / "hmm_1h_states.json"

# spot-check from the reference (UTC hour -> 1 TREND / 0 CHOP)
SAMPLE = {
    "2025-04-01 13:00": 0, "2025-04-01 14:00": 0, "2025-04-01 17:00": 0, "2025-04-01 18:00": 1,
    "2025-04-03 18:00": 1, "2025-04-03 19:00": 1, "2025-04-03 20:00": 1, "2025-04-04 13:00": 0,
    "2025-04-04 20:00": 1, "2025-04-07 13:00": 0, "2025-04-07 20:00": 0, "2025-04-08 16:00": 0,
    "2025-04-08 17:00": 1, "2025-04-08 18:00": 1, "2025-04-08 19:00": 1, "2025-04-08 20:00": 1,
}


def main() -> None:
    q = QuestDB()
    hmm = GaussianHMM2.load(ROOT / "config" / "hmm_es_1h.json")
    states: dict[str, int] = {}
    for sym in CONTRACTS:
        df = q.df(f"SELECT ts, last(c) c FROM claude_bars_1m "
                  f"WHERE symbol='{sym}' SAMPLE BY 1h ALIGN TO CALENDAR")
        df = df.dropna(subset=["c"]).reset_index(drop=True)
        df["date"] = df["ts"].dt.strftime("%Y-%m-%d")
        df["hour"] = df["ts"].dt.strftime("%Y-%m-%d %H:00")
        for date, g in df.groupby("date"):
            er = kaufman_er(g["c"].to_numpy(), L)
            for m, (hourkey, ev) in enumerate(zip(g["hour"], er)):
                if m < L or np.isnan(ev):
                    states[hourkey] = 0                       # warmup -> chop
                else:
                    raw = hmm.update(float(ev), date)         # filter resets per day
                    states[hourkey] = 1 if raw == hmm.trend_state else 0

    OUT.write_text(json.dumps(states, indent=0), encoding="utf-8")
    n_trend = sum(states.values())
    print(f"wrote {OUT}: {len(states)} hours, {n_trend} TREND ({100*n_trend/len(states):.0f}%)")

    bad = [(h, states.get(h), v) for h, v in SAMPLE.items() if states.get(h) != v]
    if bad:
        print("SAMPLE MISMATCHES (hour, got, expected):")
        for h, got, exp in bad:
            print(f"  {h}: got {got}, expected {exp}")
        raise SystemExit(1)
    print(f"sample check: all {len(SAMPLE)} reference hours match OK")


if __name__ == "__main__":
    main()
