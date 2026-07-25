"""Oracle calibration of the flow signal: what WOULD have been the right
threshold, measured in hindsight at the moments that mattered.

Forward tuning (pick th or adapt_k, see how it does) has failed twice on this
sleeve: fixed th=200 never fires on the live feed, and adapt_k sits on a flat
plateau that falls off a cliff -- both signs that the knob is being guessed
rather than measured. This tool inverts the question:

  1. Label the moments that WERE perfect, in hindsight: zigzag swings of at
     least `amp` points. A swing low is a perfect LONG entry, the following
     swing high is that trade's perfect EXIT (and a perfect SHORT entry).
  2. At each such moment compute the strategy's actual signal
        F(th) = sum over the trailing w seconds of (ad if |ad| >= th else 0)
     for a GRID of th, exactly as FlowFollowingStrategy computes it.
  3. Ask which th values would have signalled correctly, whether those values
     CLUSTER (stability), and how the cluster moves over time (drift).

ENTRY and EXIT are treated as separate problems throughout, because they are:
entry asks "is a move starting", exit asks "is this move done".

The headline diagnostic is the SIGN of the aligned flow. `aligned = F * dir`,
where dir is the direction of the move that followed. If aligned > 0 at entries,
flow-FOLLOWING is right. If aligned < 0, the perfect entry systematically has
flow against the coming move -- i.e. the sleeve's whole premise is backwards and
it should be fading, not following. No amount of threshold tuning fixes a sign.

    .venv/Scripts/python.exe tools/flow_stability.py                  # ES 2025 research
    .venv/Scripts/python.exe tools/flow_stability.py --source live    # ES 2026 live
    .venv/Scripts/python.exe tools/flow_stability.py --amp 8 --w 300

Sources deliberately span a year so drift is measurable: the 2025 research
tables are the feed the sleeve was validated on, `live` is the NT8 bridge feed
it actually runs on now.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB              # noqa: E402

SOURCES = {
    # the tables the sleeve was validated on (per-second, same columns)
    "research": [("claude_sec_feat_esh5", None), ("claude_sec_feat", None)],
    # the NT8 bridge feed it runs on today
    "live": [("claude_sec_live", "ES")],
}
TH_GRID = [0, 5, 10, 20, 30, 50, 75, 100, 150, 200, 300]


def load(qdb: QuestDB, source: str, symbol: str) -> pd.DataFrame:
    frames = []
    for table, symcol in SOURCES[source]:
        where = f" WHERE symbol = '{symbol}'" if symcol else ""
        df = qdb.df(f"SELECT ts, pxc, adelta FROM {table}{where} ORDER BY ts")
        if not df.empty:
            df["src"] = table
            frames.append(df)
    if not frames:
        raise SystemExit(f"no rows for source {source!r}")
    out = pd.concat(frames, ignore_index=True)
    out = out[out["pxc"].notna()].reset_index(drop=True)
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    out["day"] = out["ts"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    out["month"] = out["ts"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m")
    return out


def zigzag(px: np.ndarray, amp: float) -> list[tuple[int, int]]:
    """Confirmed swing points as (index, kind), kind=+1 high, -1 low, strictly
    alternating. A high is confirmed once price falls `amp` below the running
    max; a low once price rises `amp` above the running min.

    NOTE: the running max and min must be tracked INDEPENDENTLY. A single
    'extreme' that follows price in whichever direction it moves can never build
    the `amp` separation -- it just equals the current price forever, and the
    function silently returns no swings at all."""
    n = len(px)
    if n < 3:
        return []
    piv: list[tuple[int, int]] = []
    hi_i = lo_i = 0
    hi_v = lo_v = px[0]
    direction = 0                      # 0 unknown, +1 seeking a high, -1 seeking a low
    for i in range(1, n):
        v = px[i]
        if v > hi_v:
            hi_i, hi_v = i, v
        if v < lo_v:
            lo_i, lo_v = i, v
        if direction >= 0 and v <= hi_v - amp:       # confirmed HIGH at hi_i
            if not piv or piv[-1][1] != +1:
                piv.append((hi_i, +1))
                direction = -1
                lo_i, lo_v = i, v                    # restart the low search here
                hi_i, hi_v = i, v
        elif direction <= 0 and v >= lo_v + amp:     # confirmed LOW at lo_i
            if not piv or piv[-1][1] != -1:
                piv.append((lo_i, -1))
                direction = +1
                hi_i, hi_v = i, v
                lo_i, lo_v = i, v
    return piv


def flow_matrix(ad: np.ndarray, w: int) -> dict[int, np.ndarray]:
    """F(th) at every second, for each th in the grid: the trailing-w sum of the
    thresholded signed adelta -- exactly FlowFollowingStrategy's `self._F`."""
    out = {}
    for th in TH_GRID:
        x = np.where(np.abs(ad) >= th, ad, 0.0)
        c = np.concatenate([[0.0], np.cumsum(x)])
        F = c[w:] - c[:-w]                            # sum of the last w samples
        out[th] = np.concatenate([np.full(w - 1, np.nan), F])
    return out


def collect(df: pd.DataFrame, amp: float, w: int) -> pd.DataFrame:
    """One row per oracle moment: its kind (entry/exit), the move direction, the
    move size, and the aligned flow at every th."""
    rows = []
    for day, g in df.groupby("day", sort=True):       # F resets per session
        g = g.reset_index(drop=True)
        px = g["pxc"].to_numpy(dtype=float)
        ad = g["adelta"].to_numpy(dtype=float)
        if len(px) < w + 10:
            continue
        piv = zigzag(px, amp)
        if len(piv) < 2:
            continue
        F = flow_matrix(ad, w)
        for (i0, k0), (i1, _k1) in zip(piv, piv[1:]):
            direction = 1 if k0 == -1 else -1         # low -> long, high -> short
            size = abs(px[i1] - px[i0])
            for idx, role in ((i0, "entry"), (i1, "exit")):
                if not np.isfinite(F[TH_GRID[0]][idx]):
                    continue
                r = {"day": day, "month": g["month"].iloc[0], "role": role,
                     "dir": direction, "size": size}
                for th in TH_GRID:
                    v = F[th][idx]
                    # ALIGNED with the move the trade is trying to capture
                    r[f"th{th}"] = v * direction if np.isfinite(v) else np.nan
                rows.append(r)
    return pd.DataFrame(rows)


def _tbl(sub: pd.DataFrame, title: str) -> None:
    print(f"\n[{title}]  n={len(sub)}")
    print(f"{'th':>5} {'mean aligned F':>15} {'median':>10} {'% >0':>7} "
          f"{'|F| p50':>9}   read")
    for th in TH_GRID:
        v = sub[f"th{th}"].to_numpy(dtype=float)
        v = v[np.isfinite(v)]
        if not len(v):
            continue
        pos = 100.0 * (v > 0).mean()
        read = ("FOLLOW" if pos >= 60 else "FADE" if pos <= 40 else "-")
        print(f"{th:5d} {v.mean():15.1f} {np.median(v):10.1f} {pos:6.1f}% "
              f"{np.median(np.abs(v)):9.1f}   {read}")


def run(source: str, symbol: str, amp: float, w: int) -> None:
    qdb = QuestDB(timeout=180.0)
    print(f"loading {source} ({symbol}) ...", flush=True)
    df = load(qdb, source, symbol)
    print(f"  {len(df):,} seconds over {df['day'].nunique()} sessions "
          f"({df['day'].min()}..{df['day'].max()})", flush=True)
    ev = collect(df, amp, w)
    if ev.empty:
        raise SystemExit("no oracle swings found -- try a smaller --amp")

    print("\n" + "=" * 78)
    print(f"FLOW ORACLE CALIBRATION — {source} {symbol}, swings >= {amp}pt, "
          f"window {w}s")
    print("aligned F = F(th) * direction-of-the-move-that-followed")
    print("  aligned > 0  => flow ran WITH the move   -> following is right")
    print("  aligned < 0  => flow ran AGAINST it      -> the premise is backwards")
    print("=" * 78)
    print(f"\noracle moments: {len(ev)}  "
          f"({(ev.role=='entry').sum()} entries / {(ev.role=='exit').sum()} exits)"
          f"   median swing {ev['size'].median():.1f}pt")

    _tbl(ev[ev.role == "entry"], "ENTRY — is a move starting?")
    _tbl(ev[ev.role == "exit"], "EXIT — is this move done?")

    # ── drift: does the picture hold still over time? ───────────────────────
    print("\n[DRIFT] mean aligned F per month (entries), by th")
    months = sorted(ev["month"].unique())
    hdr = "  ".join(f"{m:>9}" for m in months)
    print(f"{'th':>5}  {hdr}")
    for th in TH_GRID:
        cells = []
        for m in months:
            v = ev[(ev.role == "entry") & (ev.month == m)][f"th{th}"]
            v = v[np.isfinite(v)]
            cells.append(f"{v.mean():9.1f}" if len(v) else f"{'-':>9}")
        print(f"{th:5d}  " + "  ".join(cells))
    print("\nStability = the same th column keeps the same sign and rough size")
    print("across months. A column that flips sign is not a parameter, it is noise.")
    print("=" * 78 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=list(SOURCES), default="research")
    ap.add_argument("--symbol", default="ES", help="only used by --source live")
    ap.add_argument("--amp", type=float, default=5.0,
                    help="minimum swing size in points to count as an oracle move")
    ap.add_argument("--w", type=int, default=120, help="flow window, seconds")
    a = ap.parse_args()
    run(a.source, a.symbol, a.amp, a.w)


if __name__ == "__main__":
    main()
