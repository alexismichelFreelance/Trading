"""DAY-TYPE TAXONOMY — Part 1: sleeve behavior by causal morning state.

Observation-first (per the user's mandate): I do NOT assume trend/balance
archetypes and then check them. I compute the morning-state features that are
CAUSALLY knowable by 10:00 ET and that the 2025 research bars actually contain
(claude_bars_1m covers 08:00-16:00 ET only — no true overnight, so no overnight
feature here; that is added for the 2026 recorded window in Part 2). Then I
OBSERVE the RTH outcome and each sleeve's realized P&L conditional on that state.

Causal features (known by 10:00 ET):
  gap        RTH open - prior RTH close        (in ATR14 units)
  pm_rng     08:00-09:30 pre-market range      (ATR units)
  f30_rng    09:30-10:00 range                 (ATR units)
  f30_dir    sign(px@10:00 - open)
  f30_loc    (px@10:00 - f30_low)/f30_rng      close location in the first 30m
  open_loc   open vs prior-day RTH range       (above / inside / below)
  gexp_prev  prior-session GEX percentile (dealer regime)
  dixp_prev  prior-session DIX percentile
Outcomes (end-of-day):
  rng        RTH high-low (ATR units)          — the "energy" of the day
  eff        Kaufman efficiency |c-o|/path     — trendiness
  close_loc  (c-low)/rng                       — who won
  + per-sleeve daily $ (ignition/opendrive/flow/zones) from the replay dumps
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

PF = Path("C:/Users/alexi/AppData/Local/Temp/claude/D--Trading/"
          "74634737-f114-4630-9ca7-feccef7ea3b7/scratchpad/pf")
PT, COST_PT = 50.0, 0.5175
SCALE = {"ignition": 1.0, "opendrive": 1.0, "flow": 0.1, "zones": 1.0}
q = QuestDB(timeout=120)


def trade_usd(s, t):
    if s == "zones":
        return t["gross_points"] * PT - 5.28 * t["contracts"]
    return (t["gross_points"] - COST_PT * t["contracts"]) * PT * SCALE[s]


def sleeve_daily():
    out = {}
    for s in SCALE:
        d = {}
        for t in json.loads((PF / f"daily_{s}.json").read_text()):
            d[t["day"]] = d.get(t["day"], 0.0) + trade_usd(s, t)
        out[s] = d
    return out


def features_2025():
    df = q.df("SELECT symbol, ts, o, h, l, c, vol FROM claude_bars_1m ORDER BY ts")
    et = df.ts.dt.tz_convert("America/New_York")
    df["day"] = et.dt.strftime("%Y-%m-%d")
    df["mod"] = et.dt.hour * 60 + et.dt.minute
    # front-contract split (matches the sleeve replay universe)
    df = df[~((df.symbol == "ESH5") & (df.day >= "2025-03-20"))]
    df = df[~((df.symbol == "ESM5") & (df.day < "2025-03-20"))]
    rows = []
    prev = None
    for day, g in df.groupby("day"):
        g = g.sort_values("mod")
        rth = g[(g["mod"] >= 570) & (g["mod"] < 960)]
        if len(rth) < 300:
            continue
        o = rth.o.iloc[0]; c = rth.c.iloc[-1]
        hi = rth.h.max(); lo = rth.l.min(); rng = hi - lo
        cc = rth.c.to_numpy()
        path = np.abs(np.diff(cc)).sum()
        eff = abs(c - o) / path if path > 0 else 0.0
        f30 = rth[rth["mod"] < 600]
        f30h, f30l = f30.h.max(), f30.l.min()
        f30c = f30.c.iloc[-1]; f30rng = f30h - f30l
        pm = g[(g["mod"] >= 480) & (g["mod"] < 570)]
        pm_rng = (pm.h.max() - pm.l.min()) if len(pm) else np.nan
        row = dict(day=day, o=o, c=c, rng=rng, eff=eff,
                   close_loc=(c - lo) / rng if rng > 0 else 0.5,
                   f30_rng=f30rng, f30_dir=int(np.sign(f30c - o)),
                   f30_loc=(f30c - f30l) / f30rng if f30rng > 0 else 0.5,
                   pm_rng=pm_rng, hi=hi, lo=lo)
        if prev is not None:
            row["gap"] = o - prev["c"]
            row["open_loc"] = ("above" if o > prev["hi"] else
                               "below" if o < prev["lo"] else "inside")
        else:
            row["gap"] = np.nan; row["open_loc"] = "na"
        rows.append(row); prev = row
    F = pd.DataFrame(rows)
    atr = F["rng"].rolling(14, min_periods=5).mean().shift(1)   # causal ATR14
    F["atr"] = atr
    for col in ("gap", "pm_rng", "f30_rng", "rng"):
        F[col + "_a"] = F[col] / F["atr"]
    return F.dropna(subset=["atr"]).reset_index(drop=True)


def merge_regime(F):
    gx = q.df("SELECT ts, gexp, dixp FROM claude_gex ORDER BY ts")
    gx["day"] = gx.ts.dt.strftime("%Y-%m-%d")
    days = gx["day"].tolist(); gexp = gx["gexp"].tolist(); dixp = gx["dixp"].tolist()
    import bisect

    def prev_val(day, vals):
        i = bisect.bisect_left(days, day)
        return vals[i - 1] if i > 0 else np.nan
    F["gexp_prev"] = [prev_val(d, gexp) for d in F.day]
    F["dixp_prev"] = [prev_val(d, dixp) for d in F.day]
    return F


def obs(F, by, cols=("rng_a", "eff", "close_loc"), sleeves=True):
    print(f"\n  by {by}:")
    hdr = f"    {'bucket':<16}{'n':>4}" + "".join(f"{c:>10}" for c in cols)
    if sleeves:
        hdr += "".join(f"{s[:5]:>8}" for s in SCALE) + f"{'PORT':>9}"
    print(hdr)
    for b, g in F.groupby(by):
        line = f"    {str(b):<16}{len(g):>4}" + "".join(f"{g[c].mean():>10.2f}" for c in cols)
        if sleeves:
            port = 0.0
            for s in SCALE:
                v = g[s + "_d"].sum()
                port += v
                line += f"{v:>8,.0f}"
            line += f"{port:>9,.0f}"
        print(line)


def bucketize(F):
    F = F.copy()
    F["gap_b"] = pd.cut(F["gap_a"], [-9, -0.5, -0.1, 0.1, 0.5, 9],
                        labels=["gapdn++", "gapdn", "flat", "gapup", "gapup++"])
    F["f30dir_b"] = F["f30_dir"].map({-1: "f30 down", 0: "f30 flat", 1: "f30 up"})
    F["f30rng_b"] = pd.qcut(F["f30_rng_a"], 3, labels=["f30 quiet", "f30 mid", "f30 wide"])
    F["gex_b"] = pd.cut(F["gexp_prev"], [-.01, 1/3, 2/3, 1.01],
                        labels=["gexLOW(short)", "gexMID", "gexHIGH(long)"])
    F["dix_b"] = pd.cut(F["dixp_prev"], [-.01, 1/3, 2/3, 1.01],
                        labels=["dixLOW", "dixMID", "dixHIGH"])
    return F


if __name__ == "__main__":
    F = merge_regime(features_2025())
    sd = sleeve_daily()
    for s in SCALE:
        F[s + "_d"] = F.day.map(lambda d: sd[s].get(d, 0.0))
    F = bucketize(F)
    print(f"=== PART 1: sleeve window {F.day.min()}..{F.day.max()}  ({len(F)} days) ===")
    print(f"median RTH range {F.rng.median():.0f}pt   median ATR14 {F.atr.median():.0f}pt   "
          f"median eff {F.eff.median():.2f}   median close_loc {F.close_loc.median():.2f}")
    obs(F, "gap_b")
    obs(F, "open_loc")
    obs(F, "f30dir_b")
    obs(F, "f30rng_b")
    obs(F, "gex_b")
    obs(F, "dix_b")
    # cross: does first-30 direction PERSIST to the close? (trend-day tell)
    print("\n  persistence: P(close on same side as first-30m dir):")
    for b, g in F.groupby("f30dir_b"):
        if b == "f30 flat" or len(g) == 0:
            continue
        same = np.mean(np.sign(g.c - g.o) == g.f30_dir)
        print(f"    {b:<10} n={len(g):>3}  same-side close {same:.0%}  "
              f"mean |c-o| {np.abs(g.c-g.o).mean():.1f}pt")
    F.to_csv(PF / "taxonomy_2025.csv", index=False)
    print(f"\nwrote {PF/'taxonomy_2025.csv'}")
