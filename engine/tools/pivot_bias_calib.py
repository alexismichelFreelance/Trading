"""Calibrate PivotStrategy's overnight-bias gate against real sessions.

The original absolute ER_MIN=0.30 was unreachable: Kaufman ER over N bars has a
random-walk baseline of ~RW_C/sqrt(N), which at the ~570 one-minute bars of an
overnight is ~0.05. The sleeve took ZERO trades in 34 live sessions. The gate is
now normalized (er * sqrt(N) / RW_C, so 1.0 == random walk) and this tool picks
the threshold from the observed distribution instead of guessing again.

    .venv/Scripts/python.exe tools/pivot_bias_calib.py
    .venv/Scripts/python.exe tools/pivot_bias_calib.py --symbol NQ

Prints the per-session normalized ER, the distribution, and how many sessions
would get a non-zero bias at candidate thresholds — so the knob is chosen for a
target trade frequency, then frozen.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB                      # noqa: E402
from engine.core.events import Bar                               # noqa: E402
from engine.core.timeutil import et_session_date                 # noqa: E402
from engine.strategies.pivot import (                            # noqa: E402
    BELOW_HI, BELOW_LO, RW_C, PivotStrategy,
)


def sessions(qdb: QuestDB, symbol: str) -> pd.DataFrame:
    df = qdb.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live "
                f"WHERE symbol = '{symbol}' ORDER BY ts")
    if df.empty:
        raise SystemExit(f"no claude_bars_live rows for {symbol}")
    return df


def collect(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Replay the sleeve's own accumulators and snapshot each session's read at
    the 09:30 freeze — using the strategy code itself, so this can never drift
    from what actually runs live."""
    s = PivotStrategy(symbol)
    rows, seen = [], set()
    for r in df.itertuples(index=False):
        ts = int(pd.Timestamp(r.ts).value)
        s.on_bar(Bar(ts, "1m", float(r.o), float(r.h), float(r.l), float(r.c),
                     int(r.vol), symbol))
        d = et_session_date(ts)
        if s._bias_done and d not in seen:
            seen.add(d)
            n = s._on_n
            closes = s._on_closes
            if n < 30 or not closes:
                continue
            net = closes[-1] - closes[0]
            churn = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
            raw = abs(net) / churn if churn > 0 else 0.0
            rows.append({"day": d, "bars": n, "raw_er": raw,
                         "norm_er": raw * (len(closes) ** 0.5) / RW_C,
                         "below": s._below / max(n, 1), "net": net,
                         "bias_now": s.bias})
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ES")
    a = ap.parse_args()
    df = collect(sessions(QuestDB(timeout=120.0), a.symbol), a.symbol)
    if df.empty:
        raise SystemExit("no sessions with enough overnight bars")

    print(f"\n=== PivotStrategy overnight-bias calibration — {a.symbol}, "
          f"{len(df)} sessions ===")
    print("normalized ER: 1.0 == random walk; higher == genuinely directional\n")
    print(df[["day", "bars", "raw_er", "norm_er", "below", "net"]]
          .to_string(index=False,
                     formatters={"raw_er": "{:.4f}".format,
                                 "norm_er": "{:.2f}".format,
                                 "below": "{:.3f}".format,
                                 "net": "{:+.2f}".format}))

    ne = df["norm_er"].to_numpy()
    print(f"\nnorm_er distribution: min={ne.min():.2f}  p25={np.percentile(ne,25):.2f}  "
          f"median={np.median(ne):.2f}  p75={np.percentile(ne,75):.2f}  max={ne.max():.2f}")
    print(f"raw_er  (the old absolute gate): max={df['raw_er'].max():.4f} "
          f"-- the old ER_MIN=0.30 was unreachable, hence 0 trades\n")

    # a session only trades if ER passes AND the VWAP posture agrees
    posture = ((df["below"] >= BELOW_HI) & (df["net"] < 0)) | \
              ((df["below"] <= BELOW_LO) & (df["net"] > 0))
    print("threshold  ER-pass  ER+posture (actual trading days)")
    for th in (1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.75, 2.0):
        p = int((ne >= th).sum())
        both = int(((ne >= th) & posture).sum())
        print(f"   {th:4.2f}     {p:3d}/{len(df)}      {both:3d}/{len(df)} "
              f"({both/len(df)*100:4.1f}% of sessions)")
    print("\npick the threshold for the trade frequency you want, then FREEZE it.")
    print("(posture gate alone passes "
          f"{int(posture.sum())}/{len(df)} sessions)\n")


if __name__ == "__main__":
    main()
