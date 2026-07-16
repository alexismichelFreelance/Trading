"""Validate the DayScore fade-friendliness read (it separates rotational from
trending days) and print TODAY's read. Feature logic lives in
engine/features/day_score.py (shared with run_live's startup print).

    python strategy_lab/day_read.py
"""
import sys

import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from scipy.stats import spearmanr

from engine.adapters.questdb import QuestDB
from engine.features.day_score import (DayScore, daily_aggregates, latest_read,
                                       score_history)

q = QuestDB(timeout=180)


def validate(tag, S):
    S = S.dropna(subset=["score", "onesided"])
    if len(S) < 10:
        print(f"{tag}: only {len(S)} scored days"); return
    rho, p = spearmanr(S.score, S.onesided)
    hi = S[S.score >= S.score.median()]; lo = S[S.score < S.score.median()]
    print(f"{tag}: {len(S)} days  corr(score, one-sidedness)={rho:+.2f} (p={p:.2f}; "
          f"negative = high score -> rotational, as intended)")
    print(f"    high-score days one-sidedness {hi.onesided.mean():.2f}  |  "
          f"low-score {lo.onesided.mean():.2f}")


if __name__ == "__main__":
    live = q.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live ORDER BY ts")
    validate("2026 recorded (24h)", score_history(daily_aggregates(live, True)))
    r25 = q.df("SELECT symbol,ts,o,h,l,c,vol FROM claude_bars_1m ORDER BY ts")
    et = r25.ts.dt.tz_convert("America/New_York"); d = et.dt.strftime("%Y-%m-%d")
    r25 = r25[~(((r25.symbol == "ESH5") & (d >= "2025-03-20")) |
               ((r25.symbol == "ESM5") & (d < "2025-03-20")))]
    validate("2025 research (crabel+volcalm)", score_history(daily_aggregates(r25, False)))

    print("\n=== TODAY'S READ ===")
    r = latest_read(live, has_overnight=True)
    if r is None:
        print("  (not enough history)")
    else:
        print(f"  {r['day']}   fade-friendliness {r['score']:.0f}/100  [{r['label']}]")
        print("  components (high=rotational): " +
              "  ".join(f"{k} {v:.0%}" for k, v in r["components"].items()))
        print(f"  posture: {r['posture']}")
