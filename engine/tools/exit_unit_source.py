"""Where should the session's ruler come from?

TwoPhaseExit arms at `arm_mult` x "the session's typical move". Three ways to
know that number, and they are not equivalent:

  lookahead   median |30-bar move| over the WHOLE session -- what the original
              research used. Unknowable at 09:30. Included here only as the
              ceiling: no causal estimator can beat it.
  trailing    median |30-bar move| over the last `vol_win` bars -- what the LIVE
              TwoPhaseExit uses. Causal, but laggy by construction: at 09:35 a
              600-bar window is ~9.9 hours old, so it is describing the
              OVERNIGHT, not the session, while pretending to describe the
              session.
  overnight   built from the 00:00-09:30 ET window and frozen at the open --
              causal, complete at the moment it is needed, and not laggy: it is
              the most recent thing that finished rather than a window that
              trails behind.

The trailing window is already mostly overnight data early in the day. So the
overnight estimator is not a different idea from what live does -- it is the
same information, labelled honestly and cut off cleanly instead of smeared
through a lagging average. This measures which one actually predicts.

    .venv/Scripts/python.exe tools/exit_unit_source.py --symbol ESH5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RTH_OPEN, RTH_CLOSE = 9 * 60 + 30, 16 * 60
WIN = 30                      # the horizon the unit measures, in 1m bars


def typ_move(c: np.ndarray, win: int = WIN) -> float:
    """Median absolute move over `win` bars — the market's own ruler."""
    if len(c) <= win:
        return float("nan")
    d = np.abs(c[win:] - c[:-win])
    return float(np.median(d)) if d.size else float("nan")


def bars_from_npz(path: Path) -> pd.DataFrame:
    z = np.load(path)
    m = z["kind"] == 1
    df = pd.DataFrame({"ts": z["ts"][m], "px": z["a"][m]})
    t = pd.to_datetime(df["ts"], utc=True).dt.tz_convert("America/New_York")
    df["bar"] = t.dt.floor("1min")
    b = df.groupby("bar", as_index=False)["px"].agg(["last", "max", "min"])
    b.columns = ["bar", "c", "h", "l"]
    b["mod"] = b["bar"].dt.hour * 60 + b["bar"].dt.minute
    return b.reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESH5")
    ap.add_argument("--vol-win", type=int, default=600)
    a = ap.parse_args()

    rows = []
    for f in sorted((ROOT / ".cache" / "replay").glob(f"{a.symbol}_*.npz")):
        b = bars_from_npz(f)
        on = b[b["mod"] < RTH_OPEN]                        # 00:00-09:30 ET
        rth = b[(b["mod"] >= RTH_OPEN) & (b["mod"] < RTH_CLOSE)]
        if len(on) < 120 or len(rth) < 120:
            continue
        c_on, c_rth = on["c"].to_numpy(float), rth["c"].to_numpy(float)
        # first RTH bar. NOT `mod >= RTH_OPEN` — the file starts at 20:00 the
        # previous evening, whose mod is 1200 and would match that immediately.
        i_open = int(rth.index[0])
        trail = b["c"].to_numpy(float)[max(0, i_open - a.vol_win):i_open]
        rows.append({
            "day": f.stem.split("_")[1],
            # --- causal, all known at 09:30 -------------------------------
            "on_range": float(on["h"].max() - on["l"].min()),
            "on_typ": typ_move(c_on),
            "trail_typ": typ_move(trail),
            "trail_bars": len(trail),
            # --- the thing we are trying to predict ------------------------
            "rth_typ": typ_move(c_rth),
            "rth_range": float(rth["h"].max() - rth["l"].min()),
            # --- the ceiling (uses the whole day) --------------------------
            "look_typ": typ_move(b["c"].to_numpy(float)),
        })
    d = pd.DataFrame(rows).dropna()
    if len(d) < 5:
        raise SystemExit(f"only {len(d)} usable sessions")

    def fit(x: str, y: str) -> tuple[float, float, float]:
        """Spearman rho, R^2 of the through-origin scale fit, and that scale."""
        rho = float(d[x].corr(d[y], method="spearman"))
        k = float((d[x] * d[y]).sum() / (d[x] ** 2).sum())
        ss = float(((d[y] - k * d[x]) ** 2).sum())
        r2 = 1.0 - ss / float(((d[y] - d[y].mean()) ** 2).sum())
        return rho, r2, k

    print("\n" + "=" * 78)
    print(f"UNIT SOURCE — {a.symbol}, {len(d)} sessions")
    print(f"target: RTH typical {WIN}-minute move (median |{WIN}m move|, 09:30-16:00)")
    print("=" * 78)
    print(f"\n  trailing window holds {d.trail_bars.median():.0f} 1m bars at the open"
          f"  = {d.trail_bars.median()/60:.1f} hours")
    print("  i.e. at 09:30 the 'session unit' is built almost entirely from"
          " overnight bars\n  — it is an overnight estimate already, just an"
          " unlabelled and lagging one.\n")
    print(f"{'predictor':>12}{'known at':>10}{'spearman':>10}{'R^2':>9}{'scale':>8}")
    for name, lbl in (("on_range", "09:30"), ("on_typ", "09:30"),
                      ("trail_typ", "09:30"), ("look_typ", "16:00*")):
        rho, r2, k = fit(name, "rth_typ")
        print(f"{name:>12}{lbl:>10}{rho:>10.3f}{r2:>9.3f}{k:>8.3f}"
              + ("   * lookahead, not usable live" if "*" in lbl else ""))
    print("\n  and against the RTH RANGE (the user's original observation):")
    for name in ("on_range", "on_typ", "trail_typ"):
        rho, r2, k = fit(name, "rth_range")
        print(f"{name:>12}{'09:30':>10}{rho:>10.3f}{r2:>9.3f}{k:>8.3f}")

    print("\n  per-session detail (points):")
    print(f"\n{'day':>12}{'on_range':>10}{'on_typ':>9}{'trail':>9}"
          f"{'rth_typ':>9}{'rth_range':>11}")
    for r in d.itertuples(index=False):
        print(f"{r.day:>12}{r.on_range:10.2f}{r.on_typ:9.2f}{r.trail_typ:9.2f}"
              f"{r.rth_typ:9.2f}{r.rth_range:11.2f}")
    print("=" * 78 + "\n")
    d.to_csv(ROOT / ".cache" / f"unit_source_{a.symbol}.csv", index=False)


if __name__ == "__main__":
    main()
