"""Do short-gamma pockets amplify and long-gamma pockets pin?

THE CLAIM, which is the only one worth testing: where cumulative dealer gamma is
NEGATIVE, hedging is with the move (buy strength, sell weakness) so moves are
amplified; where it is POSITIVE, hedging is against the move, so price is pinned.

Everything I tested before this was the wrong question or the wrong sample:
  * treating the flip as a support/resistance LEVEL -- it is a boundary between
    regimes, not a wall, and it tested no better than a random line on ES;
  * classifying a whole SESSION by the regime at its open -- 25 SHORT vs 4 LONG
    on ES, so there was no long-gamma sample to compare against, and it ignored
    that price crosses pockets DURING the day, which is the entire point of
    keeping the curve.

Per BAR instead: which pocket is price in right now, and what does price do
next. ~390 bars a session over ~20 sessions is a usable sample even when one
regime is rare.

THE STATISTIC is the variance ratio. Over horizon k,

    VR(k) = Var(k-bar return) / (k * Var(1-bar return))

VR > 1  moves compound -- amplification
VR = 1  random walk
VR < 1  moves revert -- pinning

That is the hypothesis stated as a number, and it does not care about direction,
which is what makes it the right test: the claim is about the CHARACTER of
movement, not about which way price goes.

Also reported, because a difference in variance ratio is only tradeable if it is
big enough to pay for costs: mean absolute per-bar move in each regime, and the
same split by DISTANCE from the pocket edge (a claim about pockets should be
strongest deep inside one and weakest next to the boundary).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB              # noqa: E402
from engine.features.gamma_profile import gamma_profile  # noqa: E402

ET = "America/New_York"
PAIRS = {"SPX": "ES", "NDX": "NQ"}
HORIZONS = (5, 15, 30)


def variance_ratio(r: np.ndarray, k: int) -> float:
    """VR(k) on 1-bar returns r. Overlapping k-sums, unbiased-ish."""
    r = r[np.isfinite(r)]
    if len(r) < k * 8:
        return float("nan")
    v1 = r.var(ddof=1)
    if v1 <= 0:
        return float("nan")
    s = np.convolve(r, np.ones(k), mode="valid")      # overlapping k-bar sums
    return float(s.var(ddof=1) / (k * v1))


def main() -> None:
    q = QuestDB(timeout=240)
    gx = q.df("SELECT ts,underlying,strike,call_gex,put_gex,spot "
              "FROM claude_gex_strikes ORDER BY ts")
    gx["day"] = gx["ts"].dt.strftime("%Y-%m-%d")
    curves = {}
    for (d, u), g in gx.groupby(["day", "underlying"]):
        agg: dict = {}
        for r in g.itertuples():
            c, p = agg.get(r.strike, (0.0, 0.0))
            agg[r.strike] = (c + float(r.call_gex), p + float(r.put_gex))
        curves[(d, u)] = (agg, float(g["spot"].iloc[0]))

    for und, fut in PAIRS.items():
        b = q.df(f"SELECT ts,o,h,l,c FROM claude_bars_live WHERE symbol='{fut}' "
                 f"ORDER BY ts")
        et = pd.to_datetime(b["ts"], utc=True).dt.tz_convert(ET)
        m = et.dt.hour * 60 + et.dt.minute
        b = b[(m >= 570) & (m < 960)].copy()
        b["day"] = et[(m >= 570) & (m < 960)].dt.strftime("%Y-%m-%d")
        cdays = sorted({d for d, u in curves if u == und})
        # per-session basis: the payload's spot is the PRIOR day's index close
        closes = b.groupby("day")["c"].last()
        basis = {}
        for d in cdays:
            prev = [x for x in closes.index if x < d]
            if prev:
                basis[d] = float(closes[prev[-1]]) - curves[(d, und)][1]

        parts = []
        for day, g in b.groupby("day"):
            prior = [x for x in cdays if x < day]
            if not prior or len(g) < 300:
                continue
            src = prior[-1]
            agg, _spot = curves[(src, und)]
            bs = basis.get(src)
            if bs is None:
                continue
            px = g["c"].to_numpy(float)
            prof = gamma_profile(agg, px[0] - bs)
            if prof is None:
                continue
            curve = prof["curve"]
            ks = np.array([k for k, _, _ in curve])
            cums = np.array([c for _, _, c in curve])
            # cumulative gamma at each bar's price, interpolated, in index terms
            cum = np.interp(px - bs, ks, cums)
            flips = np.array([f + bs for f in prof["flips"]])
            if len(flips):
                edge = np.min(np.abs(px[:, None] - flips[None, :]), axis=1)
            else:
                edge = np.full(len(px), np.inf)
            d1 = np.diff(px, prepend=np.nan)
            parts.append(pd.DataFrame(dict(day=day, px=px, cum=cum, edge=edge,
                                           ret=d1)))
        if not parts:
            print(f"\n{und}->{fut}: no overlapping sessions")
            continue
        d = pd.concat(parts, ignore_index=True)
        d["regime"] = np.where(d["cum"] > 0, "LONG", "SHORT")

        print(f"\n{'=' * 74}\n{und} -> {fut}   {d['day'].nunique()} sessions, "
              f"{len(d):,} bars\n{'=' * 74}")
        print(f"  {'regime':7} {'bars':>7} {'mean|1m|':>9} {'sd 1m':>8}   "
              + "  ".join(f"VR({k})" for k in HORIZONS))
        for reg, g in d.groupby("regime"):
            r = g["ret"].to_numpy(float)
            vrs = "  ".join(f"{variance_ratio(r, k):6.2f}" for k in HORIZONS)
            print(f"  {reg:7} {len(g):>7,} {np.nanmean(np.abs(r)):>9.3f} "
                  f"{np.nanstd(r):>8.3f}   {vrs}")

        # deep inside a pocket vs next to its edge: the claim should be
        # strongest where the regime is least ambiguous
        print(f"\n  BY DISTANCE FROM THE POCKET EDGE")
        med = d["edge"].replace(np.inf, np.nan).median()
        d["depth"] = np.where(d["edge"] > med, "deep", "near edge")
        print(f"  {'regime':7} {'depth':10} {'bars':>7} {'mean|1m|':>9}   "
              + "  ".join(f"VR({k})" for k in HORIZONS))
        for (reg, dep), g in d.groupby(["regime", "depth"]):
            r = g["ret"].to_numpy(float)
            vrs = "  ".join(f"{variance_ratio(r, k):6.2f}" for k in HORIZONS)
            print(f"  {reg:7} {dep:10} {len(g):>7,} "
                  f"{np.nanmean(np.abs(r)):>9.3f}   {vrs}")


if __name__ == "__main__":
    main()
