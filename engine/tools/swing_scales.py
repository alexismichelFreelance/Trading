"""Layer 1 — at which SCALES are moves worth targeting, and at what risk?

No flow here on purpose. Before asking "which flow statistic predicts entries",
we need the target structure: what does the opportunity set actually look like
at each timeframe? A signal that predicts 3pt moves you must sit through 8pt of
heat to collect is worthless; one that predicts 20pt moves with 4pt of heat is a
business. That is a property of the SCALE, not of the signal, and it is knowable
before any predictor exists.

Method:
  * Scales are a LADDER in units of each session's own volatility (the median
    absolute 60s price change), never points -- so a scale means the same thing
    across sessions, instruments, feeds and years. Price is fractal, so the
    ladder is run simultaneously and the nesting is explicit.
  * Two detectors on the same ladder:
      zigzag  ORACLE -- reports the exact extreme (hindsight). The ceiling.
      dc      CAUSAL -- reports the reversal-confirmation bar (live-knowable).
    The gap between them is the cost of not having a crystal ball, and it bounds
    how much of the oracle edge any real signal could ever reach.
  * Outcomes per swing: MFE (gain on offer), MAE (heat taken), duration.
  * Reported PER SESSION and aggregated with medians + cross-session spread.
    Never a single mean over months: if the interesting structure is a cluster
    that moves, a long-window mean describes nothing that exists.

    .venv/Scripts/python.exe tools/swing_scales.py
    .venv/Scripts/python.exe tools/swing_scales.py --source live --detector dc
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB                          # noqa: E402
from engine.features.swings import DETECTORS, swing_outcomes, vol_unit  # noqa: E402

SOURCES = {
    "research": [("claude_sec_feat_esh5", None), ("claude_sec_feat", None)],
    "live": [("claude_sec_live", "ES")],
}
# scale ladder in units of the session's own vol_unit (median |60s move|)
SCALES = [0.5, 1.0, 2.0, 4.0, 8.0, 16.0]


def load(qdb: QuestDB, source: str, symbol: str) -> pd.DataFrame:
    frames = []
    for table, symcol in SOURCES[source]:
        where = f" WHERE symbol = '{symbol}'" if symcol else ""
        df = qdb.df(f"SELECT ts, pxc FROM {table}{where} ORDER BY ts")
        if not df.empty:
            frames.append(df)
    if not frames:
        raise SystemExit(f"no rows for source {source!r}")
    out = pd.concat(frames, ignore_index=True)
    out = out[out["pxc"].notna()].reset_index(drop=True)
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    out["day"] = out["ts"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    return out


def per_session(df: pd.DataFrame, detector: str) -> pd.DataFrame:
    det = DETECTORS[detector]
    rows = []
    for day, g in df.groupby("day", sort=True):
        px = g["pxc"].to_numpy(dtype=float)
        if len(px) < 600:
            continue
        u = vol_unit(px)
        if u <= 0:
            continue
        for k in SCALES:
            piv = det(px, k * u)
            sw = swing_outcomes(px, piv)
            if not sw:
                continue
            mfe = np.array([s["mfe"] for s in sw])
            mae = np.array([s["mae"] for s in sw])
            bars = np.array([s["bars"] for s in sw])
            rows.append({
                "day": day, "scale": k, "unit": u, "n": len(sw),
                "amp_pt": k * u,
                "mfe": float(np.median(mfe)),
                "mae": float(np.median(mae)),
                "edge": float(np.median(mfe + mae)),   # mae<=0: gain net of heat
                # NOTE: an ORACLE detector enters at the exact extreme, so MAE
                # is 0 BY CONSTRUCTION and the ratio is undefined -- not a bug,
                # just what "perfect entry" means. Risk is only measurable with
                # a causal detector, which enters after confirmation.
                "ratio": float(np.median(mfe) / abs(np.median(mae)))
                if np.median(mae) < 0 else np.nan,
                "mins": float(np.median(bars) / 60.0),
            })
    return pd.DataFrame(rows)


def run(source: str, symbol: str, detector: str, show_days: int) -> None:
    qdb = QuestDB(timeout=180.0)
    print(f"loading {source} ({symbol}) ...", flush=True)
    df = load(qdb, source, symbol)
    ndays = df["day"].nunique()
    print(f"  {len(df):,} seconds over {ndays} sessions "
          f"({df['day'].min()}..{df['day'].max()})", flush=True)
    ps = per_session(df, detector)
    if ps.empty:
        raise SystemExit("no swings at any scale")

    print("\n" + "=" * 88)
    print(f"SWING SCALE SURVEY — {source} {symbol}, detector={detector}, "
          f"{ndays} sessions")
    print("scales are multiples of each session's own vol unit (median |60s move|)")
    print("MFE = gain on offer, MAE = heat taken to get it, both in points")
    print("=" * 88)

    print(f"\n{'scale':>6} {'amp pt':>7} {'swings/day':>11} {'hold min':>9} "
          f"{'MFE':>7} {'MAE':>7} {'MFE/|MAE|':>10} {'edge':>7}  {'day-to-day spread':>18}")
    for k in SCALES:
        s = ps[ps.scale == k]
        if s.empty:
            continue
        # cross-session spread of the per-session median: is the scale STABLE?
        r = s["ratio"].dropna()
        iqr = float(r.quantile(0.75) - r.quantile(0.25)) if len(r) else float("nan")
        rat = f"{r.median():10.2f}" if len(r) else f"{'n/a':>10}"
        spread = f"ratio IQR {iqr:7.2f}" if len(r) else "(oracle: MAE=0 by constr.)"
        print(f"{k:6.1f} {s['amp_pt'].median():7.2f} {s['n'].median():11.0f} "
              f"{s['mins'].median():9.1f} {s['mfe'].median():7.2f} "
              f"{s['mae'].median():7.2f} {rat} "
              f"{s['edge'].median():7.2f}  {spread}")

    print("\n  MFE/|MAE| >1 means the move pays more than the heat it puts you")
    print("  through. A ratio of exactly 1.00 is the random-walk signature: no")
    print("  asymmetry exists at that scale, so no signal can extract one.")
    print("  ORACLE runs show MAE=0 / ratio n/a BY CONSTRUCTION -- entering at the")
    print("  exact extreme cannot take heat. Use ORACLE for the gain ceiling (MFE)")
    print("  and CAUSAL (--detector dc) for anything about risk.")

    if show_days:
        print(f"\n[PER SESSION — last {show_days}, so drift is visible at the "
              f"granularity it actually happens]")
        days = sorted(ps["day"].unique())[-show_days:]
        hdr = "  ".join(f"{k:>6.1f}" for k in SCALES)
        print(f"{'day':<12} {'unit':>6}  {hdr}      (MFE/|MAE| per scale)")
        for d in days:
            s = ps[ps.day == d].set_index("scale")
            cells = []
            for k in SCALES:
                cells.append(f"{s.loc[k,'ratio']:6.2f}" if k in s.index else f"{'-':>6}")
            u = s["unit"].iloc[0] if len(s) else float("nan")
            print(f"{d:<12} {u:6.2f}  " + "  ".join(cells))
    print("=" * 88 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=list(SOURCES), default="research")
    ap.add_argument("--symbol", default="ES")
    ap.add_argument("--detector", choices=list(DETECTORS), default="zigzag")
    ap.add_argument("--show-days", type=int, default=15)
    a = ap.parse_args()
    run(a.source, a.symbol, a.detector, a.show_days)


if __name__ == "__main__":
    main()
