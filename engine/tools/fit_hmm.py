"""Offline: fit & FREEZE the 2-state Gaussian HMM regime gate on ESM5 1h
Kaufman-efficiency, writing config/hmm_es_1h.json.

The research learned means are chop ER ~0.44 / trend ~1.0 with sticky
self-transitions (~0.85-0.91). This script reproduces that and freezes the
params the live/replay forward-filter loads. Run once (re-run to refit):

    .venv/Scripts/python.exe tools/fit_hmm.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np                                    # noqa: E402

from engine.adapters.questdb import QuestDB          # noqa: E402
from engine.features.efficiency import kaufman_er    # noqa: E402
from engine.features.hmm import fit_gaussian_hmm      # noqa: E402

LOOKBACK = 3
SYMBOL = "ESM5"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", type=float, default=1e-6,
                    help="variance floor (larger => broader/stickier trend state)")
    ap.add_argument("--out", default=str(ROOT / "config" / "hmm_es_1h.json"))
    a = ap.parse_args()
    out = Path(a.out)

    q = QuestDB()
    # continuous 1h closes across the ESM5 range (trading hours 13-21 UTC)
    df = q.df(f"SELECT ts, last(c) c FROM claude_bars_1m "
              f"WHERE symbol='{SYMBOL}' SAMPLE BY 1h ALIGN TO CALENDAR")
    df = df.dropna(subset=["c"]).reset_index(drop=True)
    er = kaufman_er(df["c"].to_numpy(), LOOKBACK)
    obs = er[~np.isnan(er)]
    print(f"1h bars: {len(df)} | ER observations: {len(obs)} | var_floor={a.floor}")

    params = fit_gaussian_hmm(obs, n_iter=35, seed_means=(0.3, 0.9), var_floor=a.floor)
    params["lookback"] = LOOKBACK
    params["symbol_root"] = "ES"
    params["fit_on"] = SYMBOL
    params["timeframe"] = "1h"

    means = params["means"]
    A = params["A"]
    ts = params["trend_state"]
    print(f"learned means: chop={means[1 - ts]:.3f}  trend={means[ts]:.3f}  (target ~0.44 / ~1.0)")
    print(f"self-transitions: {A[0][0]:.3f} / {A[1][1]:.3f}  (target sticky ~0.85-0.91)")
    print(f"trend_state = {ts}")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(params, indent=2), encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
