"""Is the best exit FINDABLE — from the trade alone, or only from the roster?

tools/exit_oracle.py established the prize: on 41 real trades the engine made
-$4,218 while the best exits available INSIDE the holds it already had were
worth +$71,768. It also showed the shape -- best exit at ~4 min, actual hold
~63 min, and only ~36% of the achieved excursion captured.

This asks the next question without assuming any rule: at the moment the best
exit was available, was ANYTHING observably different from every other moment
of the same trade?

Two feature families, deliberately kept apart so we learn which one matters:

  SELF   what the trade knows about itself -- excursion, peak, give-back
         fraction, time held, recent velocity, drawdown from peak.
  PEER   what the rest of the roster was doing at that instant, reconstructed
         from claude_paper_fills: how many sleeves are positioned, how many
         align with this trade, how many oppose, net agreement, and how those
         counts have CHANGED recently (peers joining / leaving / flipping).

No peer interaction shape is presupposed: these are plain counts over whichever
sleeves happen to exist, so adding or removing sleeves changes the numbers but
never the definition. If PEER features separate the oracle moment and SELF ones
do not, exits need the roster. If neither separates it, no rule of this kind can
work and the answer lies elsewhere.

Discrimination is measured as AUC -- the probability that a random oracle-window
second scores above a random other second of the SAME trade. 0.5 is noise.
Comparing within a trade removes trade-level differences, so a feature cannot
score well merely by being large on trades that happened to work.

    .venv/Scripts/python.exe tools/exit_signal_search.py
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
from tools.exit_oracle import load, round_trips      # noqa: E402

PU = {"ES": 50.0, "NQ": 20.0}


def position_grid(f: pd.DataFrame, t0: pd.Timestamp, t1: pd.Timestamp,
                  freq: str = "5s") -> pd.DataFrame:
    """Signed position of EVERY sleeve on a regular grid. This is the roster's
    state through time, rebuilt from what the engine actually did."""
    idx = pd.date_range(t0, t1, freq=freq, tz="UTC")
    out = {}
    for sl, g in f.groupby("sleeve"):
        g = g.sort_values("t")
        pos = (g["side"] * g["qty"]).cumsum()
        s = pd.Series(pos.to_numpy(), index=g["t"].to_numpy())
        s = s[~s.index.duplicated(keep="last")]
        out[sl] = s.reindex(idx, method="ffill").fillna(0.0)
    return pd.DataFrame(out, index=idx)


def features(trade, path: pd.DataFrame, grid: pd.DataFrame, lookback: int = 6):
    """Per-instant SELF and PEER features across one trade's hold."""
    d = trade.dir
    p = path.price.to_numpy(float)
    adv = d * (p - trade.entry)
    peak = np.maximum.accumulate(adv)
    ti = pd.DatetimeIndex(path.t)
    mins = (ti - trade.entry_t).total_seconds().to_numpy() / 60.0

    g = grid.reindex(ti, method="ffill")
    me = trade.sleeve
    others = [c for c in g.columns if c != me]
    sign = np.sign(g[others].to_numpy())
    active = (sign != 0).sum(axis=1)
    withme = (sign == d).sum(axis=1)
    against = (sign == -d).sum(axis=1)
    agree = np.divide(withme - against, np.maximum(active, 1))

    def delta(a):
        b = np.concatenate([np.full(lookback, a[0]), a])[:-lookback]
        return a - b

    withpeak = np.maximum.accumulate(withme)
    return pd.DataFrame({
        # ── SELF ───────────────────────────────────────────────────────────
        "self_fe": adv,
        "self_peak": peak,
        "self_giveback": np.divide(peak - adv, np.maximum(peak, 1e-9)),
        "self_frac_of_peak": np.divide(adv, np.maximum(peak, 1e-9)),
        "self_mins": mins,
        "self_velocity": delta(adv),
        # ── PEER ───────────────────────────────────────────────────────────
        "peer_active": active,
        "peer_with": withme,
        "peer_against": against,
        "peer_agree": agree,
        "peer_with_lost": withpeak - withme,
        "peer_d_with": delta(withme.astype(float)),
        "peer_d_against": delta(against.astype(float)),
        "peer_d_agree": delta(agree),
        "_adv": adv,
    })


def auc_within(df: pd.DataFrame, col: str, lab: str) -> float:
    """AUC pooled WITHIN trades: P(feature higher at an oracle second)."""
    num = den = 0.0
    for _, g in df.groupby("trade"):
        pos = g.loc[g[lab] == 1, col].to_numpy()
        neg = g.loc[g[lab] == 0, col].to_numpy()
        if not len(pos) or not len(neg):
            continue
        pos = pos[np.isfinite(pos)]; neg = neg[np.isfinite(neg)]
        if not len(pos) or not len(neg):
            continue
        num += (pos[:, None] > neg[None, :]).sum() + 0.5 * (pos[:, None] == neg[None, :]).sum()
        den += len(pos) * len(neg)
    return num / den if den else np.nan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="since", default="2026-07-27")
    ap.add_argument("--band", type=float, default=0.9,
                    help="a second counts as ORACLE if adv >= band * max(adv)")
    ap.add_argument("--min-hold", type=float, default=2.0, help="minutes")
    a = ap.parse_args()

    qdb = QuestDB(timeout=180.0)
    f, px = load(qdb, a.since)
    rt = round_trips(f)
    lo, hi = f["t"].min(), f["t"].max() + pd.Timedelta(hours=1)
    grid = position_grid(f, lo, hi)

    frames = []
    for i, r in enumerate(rt.itertuples(index=False)):
        p = px.get(r.symbol)
        if p is None or p.empty:
            continue
        seg = p[(p.t >= r.entry_t) & (p.t <= r.exit_t)]
        if len(seg) < 20 or (r.exit_t - r.entry_t).total_seconds() / 60 < a.min_hold:
            continue
        fr = features(r, seg.reset_index(drop=True), grid)
        best = fr["_adv"].max()
        if best <= 0:
            continue                      # never in profit: no exit to find
        fr["oracle"] = (fr["_adv"] >= a.band * best).astype(int)
        fr["trade"] = i
        frames.append(fr)
    if not frames:
        raise SystemExit("no usable trades")
    D = pd.concat(frames, ignore_index=True)

    print("\n" + "=" * 78)
    print(f"EXIT SIGNAL SEARCH — {D.trade.nunique()} trades, {len(D):,} instants")
    print(f"ORACLE = any instant within {100*a.band:.0f}% of that trade's best "
          f"excursion ({D.oracle.mean()*100:.1f}% of instants)")
    print("AUC is computed WITHIN each trade, so 0.50 is genuinely noise.")
    print("=" * 78)
    cols = [c for c in D.columns if c.startswith(("self_", "peer_"))]
    res = sorted(((c, auc_within(D, c, "oracle")) for c in cols),
                 key=lambda x: -abs((x[1] or 0.5) - 0.5))
    print(f"\n{'feature':>20} {'AUC':>7}   read")
    for c, v in res:
        if not np.isfinite(v):
            continue
        edge = abs(v - 0.5)
        read = ("STRONG" if edge >= 0.15 else "useful" if edge >= 0.07
                else "weak" if edge >= 0.03 else "noise")
        print(f"{c:>20} {v:7.3f}   {read}")
    sf = [v for c, v in res if c.startswith("self_") and np.isfinite(v)]
    pf = [v for c, v in res if c.startswith("peer_") and np.isfinite(v)]
    print(f"\n  best SELF feature  {max(sf, key=lambda x: abs(x-.5)):.3f}")
    print(f"  best PEER feature  {max(pf, key=lambda x: abs(x-.5)):.3f}")
    print("\n  If PEER beats SELF, exits need the roster and a per-sleeve rule")
    print("  cannot get there. If neither leaves 0.50, the oracle moment is not")
    print("  identifiable from these observables at all.")
    print("=" * 78 + "\n")


if __name__ == "__main__":
    main()
