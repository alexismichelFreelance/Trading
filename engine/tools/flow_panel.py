"""Layer 2 — which flow measurement, over which lookback, predicts the moves
that Layer 1 showed are worth targeting?

Layer 1 (tools/swing_scales.py) established the target structure: the gain/heat
asymmetry is absent at sub-point scales and real at ~6-12pt moves, and a perfect
entry is worth ~2.5x a confirmation-lagged one. That 2.5x is the entire prize on
offer, and it is the yardstick here: a feature matters only insofar as it closes
some of the gap between the CAUSAL baseline and the ORACLE ceiling.

Design decisions that follow from what we already measured:

* EVERY feature is offered in a RELATIVE form as well as a raw one. Flow
  magnitudes drift ~5x across months and run ~3.5x smaller on the live feed than
  the research feed, while the SIGN stays put (tools/flow_stability.py). Anything
  scored in absolute units is measuring the feed, not the market.
* Scoring is PER SESSION, then summarised by the distribution across sessions --
  never one number over months. If the useful value is a cluster that moves, a
  long-window mean describes something that never existed. `hit` (fraction of
  sessions with the same sign) is the stability measure that matters; a feature
  with a big average IC and hit ~50% is noise with a good day.
* The forward horizon is matched to the scale being targeted, not fixed.

Features (all causal -- only data up to t is used):
  fsum   signed aggressor flow summed over the window
  fz     the same, z-scored against its own trailing hour  (relative)
  bp     book pressure, (bid_add - ask_add) summed          (relative form: bpz)
  div    flow z MINUS price-move z -- absorption: flow without the move it
         should have caused, which is where a turn hides

    .venv/Scripts/python.exe tools/flow_panel.py
    .venv/Scripts/python.exe tools/flow_panel.py --source live --horizon 880

RESULT (ES, 65 research sessions, both Layer-1 scales) -- a clean negative:

  horizon   best sign%        best tail ratio      unconditional
    250s    66% (fsum120)     1.15 (bpz300 lo)         ~1.00
    880s    63% (div300)      1.18 (bpz120 lo)         ~1.00

32 feature/horizon combinations, NONE reach sign% >= 75. Most sit at 53-63%,
i.e. the feature's direction flips on a third to a half of all sessions, and
the per-session IC scatter runs 3-7x the median IC. The tails are weakly
informative (1.15-1.18 against 1.00) but nowhere near stable enough to trade.

Conclusion: per-second aggressor flow and L2 add/cancel pressure, AS DELIVERED
BY THIS FEED, do not reliably predict multi-point ES moves at 4-15 minute
horizons. Note the qualifier -- the aggressor side here is INFERRED via a
Lee-Ready quote rule over a delayed bridge feed, not a true CME MBO aggressor
flag, so this bounds the feed as much as the hypothesis.

What did survive: swing STRUCTURE. Layer 1's causal directional-change entry
scores 1.29-1.36 with no signal at all, better than anything conditioned on
flow here. Structure carries more than flow does.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB          # noqa: E402

SOURCES = {
    "research": [("claude_sec_feat_esh5", None), ("claude_sec_feat", None)],
    "live": [("claude_sec_live", "ES")],
}
COLS = "ts, pxc, adelta, avol, bid_add, ask_add, bid_cancel, ask_cancel"
WINDOWS = [30, 120, 300, 900]
ZWIN = 3600            # trailing hour: the reference scale for every z-score
MIN_ROWS = 5_000


def load(qdb: QuestDB, source: str, symbol: str) -> pd.DataFrame:
    frames = []
    for table, symcol in SOURCES[source]:
        where = f" WHERE symbol = '{symbol}'" if symcol else ""
        df = qdb.df(f"SELECT {COLS} FROM {table}{where} ORDER BY ts")
        if not df.empty:
            frames.append(df)
    if not frames:
        raise SystemExit(f"no rows for source {source!r}")
    out = pd.concat(frames, ignore_index=True)
    out = out[out["pxc"].notna()].reset_index(drop=True)
    out["ts"] = pd.to_datetime(out["ts"], utc=True)
    out["day"] = out["ts"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    return out


def _roll_sum(a: np.ndarray, w: int) -> np.ndarray:
    c = np.concatenate([[0.0], np.cumsum(a)])
    out = np.full(len(a), np.nan)
    out[w - 1:] = c[w:] - c[:-w]
    return out


def _zscore(a: np.ndarray, w: int) -> np.ndarray:
    """z against the trailing w samples -- the relative form that survives drift."""
    s = pd.Series(a)
    m = s.rolling(w, min_periods=w // 4).mean()
    sd = s.rolling(w, min_periods=w // 4).std()
    return ((s - m) / sd.replace(0.0, np.nan)).to_numpy()


def features(g: pd.DataFrame) -> dict[str, np.ndarray]:
    ad = g["adelta"].to_numpy(float)
    px = g["pxc"].to_numpy(float)
    bp = g["bid_add"].to_numpy(float) - g["ask_add"].to_numpy(float)
    bc = g["ask_cancel"].to_numpy(float) - g["bid_cancel"].to_numpy(float)
    out: dict[str, np.ndarray] = {}
    for w in WINDOWS:
        fs = _roll_sum(ad, w)
        out[f"fsum{w}"] = fs
        out[f"fz{w}"] = _zscore(fs, ZWIN)
        bps = _roll_sum(bp, w)
        out[f"bpz{w}"] = _zscore(bps, ZWIN)
        out[f"bcz{w}"] = _zscore(_roll_sum(bc, w), ZWIN)
        # absorption: flow that did NOT produce the price move it should have
        ret = np.concatenate([np.full(w, np.nan), px[w:] - px[:-w]])
        out[f"div{w}"] = out[f"fz{w}"] - _zscore(ret, ZWIN)
    return out


def forward(px: np.ndarray, h: int) -> dict[str, np.ndarray]:
    """Forward outcome over px[t .. t+h], from each t. The reversed-rolling
    window must be h+1, not h: it has to span the current sample plus h ahead,
    otherwise every excursion is measured one sample short."""
    s = pd.Series(px[::-1])
    fmax = s.rolling(h + 1, min_periods=1).max().to_numpy()[::-1].copy()
    fmin = s.rolling(h + 1, min_periods=1).min().to_numpy()[::-1].copy()
    nxt = np.concatenate([px[h:], np.full(h, np.nan)])
    mfe, mae = fmax - px, fmin - px
    if h:                       # the final h rows see a truncated future: drop them
        mfe[-h:] = np.nan
        mae[-h:] = np.nan
    return {"mfe": mfe, "mae": mae, "net": nxt - px}


def spearman(x: np.ndarray, y: np.ndarray) -> float:
    m = np.isfinite(x) & np.isfinite(y)
    if m.sum() < 200:
        return np.nan
    xr = pd.Series(x[m]).rank().to_numpy().astype(float, copy=True)
    yr = pd.Series(y[m]).rank().to_numpy().astype(float, copy=True)
    xr -= xr.mean(); yr -= yr.mean()
    d = np.sqrt((xr * xr).sum() * (yr * yr).sum())
    return float((xr * yr).sum() / d) if d > 0 else np.nan


def run(source: str, symbol: str, horizon: int, top: int) -> None:
    qdb = QuestDB(timeout=180.0)
    print(f"loading {source} ({symbol}) ...", flush=True)
    df = load(qdb, source, symbol)
    days = sorted(df["day"].unique())
    print(f"  {len(df):,} seconds over {len(days)} sessions", flush=True)

    per_day: dict[str, list[float]] = {}
    tail: dict[str, list[tuple[float, float]]] = {}
    for n, (day, g) in enumerate(df.groupby("day", sort=True)):
        if len(g) < MIN_ROWS:
            continue
        if n % 10 == 0:
            print(f"  ...{day}", flush=True)
        g = g.reset_index(drop=True)
        px = g["pxc"].to_numpy(float)
        fwd = forward(px, horizon)
        feats = features(g)
        for name, v in feats.items():
            ic = spearman(v, fwd["net"])
            per_day.setdefault(name, []).append(ic)
            # what do the tails actually deliver? (top/bottom decile by feature)
            m = np.isfinite(v) & np.isfinite(fwd["mfe"])
            if m.sum() > 500:
                vv, mfe, mae = v[m], fwd["mfe"][m], fwd["mae"][m]
                lo, hi = np.percentile(vv, [10, 90])
                for sel, sgn in ((vv <= lo, -1), (vv >= hi, +1)):
                    if sel.sum() > 50:
                        # signed so +1 == "feature high", read gain vs heat
                        tail.setdefault(f"{name}|{'hi' if sgn > 0 else 'lo'}",
                                        []).append(
                            (float(np.median(mfe[sel])), float(np.median(mae[sel]))))

    rows = []
    for name, ics in per_day.items():
        a = np.array([x for x in ics if np.isfinite(x)])
        if len(a) < 5:
            continue
        med = float(np.median(a))
        hit = float(max((a > 0).mean(), (a < 0).mean()) * 100)   # sign consistency
        rows.append((name, med, hit, float(a.std()), len(a)))
    rows.sort(key=lambda r: -abs(r[1]))

    print("\n" + "=" * 86)
    print(f"FLOW FEATURE PANEL — {source} {symbol}, {len(days)} sessions, "
          f"forward horizon {horizon}s ({horizon/60:.1f}min)")
    print("IC = per-session Spearman(feature, forward return), then the MEDIAN")
    print("across sessions. `sign%` = share of sessions agreeing on the sign --")
    print("that, not the IC size, is what makes a feature usable.")
    print("=" * 86)
    print(f"\n{'feature':>10} {'median IC':>10} {'sign%':>7} {'sd':>7} {'days':>5}   read")
    for name, med, hit, sd, n in rows[:top]:
        read = ("PREDICTS" if hit >= 75 and abs(med) >= 0.02 else
                "unstable" if hit < 60 else "weak")
        print(f"{name:>10} {med:10.4f} {hit:6.1f}% {sd:7.4f} {n:5d}   {read}")

    print("\n[TAIL BEHAVIOUR] median gain vs heat when a feature is in its extreme")
    print("decile (the regime you would actually trade). ratio>1 = pays its heat.")
    print(f"{'feature|tail':>16} {'MFE':>8} {'MAE':>8} {'ratio':>7}")
    tl = []
    for k, v in tail.items():
        arr = np.array(v)
        mfe, mae = float(np.median(arr[:, 0])), float(np.median(arr[:, 1]))
        if mae < 0:
            tl.append((k, mfe, mae, mfe / abs(mae)))
    tl.sort(key=lambda r: -r[3])
    for k, mfe, mae, r in tl[:top]:
        print(f"{k:>16} {mfe:8.2f} {mae:8.2f} {r:7.2f}")
    print("\nLayer-1 yardstick at this horizon: causal baseline ratio ~1.29-1.36.")
    print("A feature only earns attention if its tail beats that AND sign% is high.")
    print("=" * 86 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=list(SOURCES), default="research")
    ap.add_argument("--symbol", default="ES")
    ap.add_argument("--horizon", type=int, default=250,
                    help="forward seconds; match the scale (250=scale4, 880=scale8)")
    ap.add_argument("--top", type=int, default=18)
    a = ap.parse_args()
    run(a.source, a.symbol, a.horizon, a.top)


if __name__ == "__main__":
    main()
