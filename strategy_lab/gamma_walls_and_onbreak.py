"""Two follow-ups: do the WALLS do anything, and why does onbreak invert?

1. WALLS. The earlier test asked "how far is the session extreme from the wall",
   which conflates a level being respected with a level being reachable. The
   right question is conditional: WHEN price is near a wall, what does it do
   NEXT. If the call wall caps, bars near it should have negative forward
   returns; if the put wall supports, bars near it should have positive ones.
   Measured against the same statistic far from any wall, which is the null --
   without it, any level looks meaningful on a trending day.

   Ranked walls, not just the largest: the 2nd and 3rd concentrations are what
   the single stored wall threw away.

2. ONBREAK inverts the hypothesis. It is a trend sleeve -- it trades the break
   of the overnight range -- and in the pinned replay it LOST in short-gamma
   pockets (-2,325 to -3,625 across its twins, ~20 trips each) while MAKING
   money in long-gamma ones (+2,762 to +3,912, 7 trips). Backwards. Seven trips
   is small, so this prints every one of them: a single outlier and a consistent
   pattern look identical in a total.
"""
from __future__ import annotations

import collections
import csv
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB              # noqa: E402
from engine.features.gamma_profile import gamma_profile  # noqa: E402

ET_TZ = dt.timezone(dt.timedelta(hours=-4))
NEAR = 10.0          # ES points that count as "at" a wall
FWD = 30             # bars forward
PV = 50.0


def load(q):
    gx = q.df("SELECT ts,strike,call_gex,put_gex,spot FROM claude_gex_strikes "
              "WHERE underlying='SPX' ORDER BY ts")
    gx["day"] = gx["ts"].dt.strftime("%Y-%m-%d")
    curves = {}
    for d, g in gx.groupby("day"):
        agg = {}
        for r in g.itertuples():
            c, p = agg.get(r.strike, (0.0, 0.0))
            agg[r.strike] = (c + float(r.call_gex), p + float(r.put_gex))
        curves[d] = (agg, float(g["spot"].iloc[0]))
    b = q.df("SELECT ts,c FROM claude_bars_live WHERE symbol='ES' ORDER BY ts")
    et = pd.to_datetime(b["ts"], utc=True).dt.tz_convert("America/New_York")
    m = et.dt.hour * 60 + et.dt.minute
    b = b[(m >= 570) & (m < 960)].copy()
    b["day"] = et[(m >= 570) & (m < 960)].dt.strftime("%Y-%m-%d")
    return curves, b


def main() -> None:
    q = QuestDB(timeout=240)
    curves, bars = load(q)
    closes = bars.groupby("day")["c"].last()
    cdays = sorted(curves)
    basis = {}
    for d in cdays:
        prev = [x for x in closes.index if x < d]
        if prev:
            basis[d] = float(closes[prev[-1]]) - curves[d][1]

    rows = []
    for day, g in bars.groupby("day"):
        prior = [x for x in cdays if x < day and x in basis]
        if not prior or len(g) < 300:
            continue
        src = prior[-1]
        pr = gamma_profile(curves[src][0], curves[src][1])
        if pr is None:
            continue
        bs = basis[src]
        cw = [k + bs for k, _ in pr["call_walls"][:3]]
        pw = [k + bs for k, _ in pr["put_walls"][:3]]
        px = g["c"].to_numpy(float)
        fwd = np.full(len(px), np.nan)
        fwd[:-FWD] = px[FWD:] - px[:-FWD]
        ks = np.array([k for k, _, _ in pr["curve"]])
        cums = np.array([c for _, _, c in pr["curve"]])
        cum = np.interp(px - bs, ks, cums)
        rows.append(pd.DataFrame(dict(
            day=day, px=px, fwd=fwd,
            regime=np.where(cum > 0, "LONG", "SHORT"),
            dcw=np.min(np.abs(px[:, None] - np.array(cw)[None, :]), axis=1),
            dpw=np.min(np.abs(px[:, None] - np.array(pw)[None, :]), axis=1))))
    d = pd.concat(rows, ignore_index=True).dropna(subset=["fwd"])
    # DEMEAN per session. ES drifted up over this window, so an unconditional
    # forward return measures the drift as much as the level -- which is what
    # made "far from every wall" print +3.41 and look like a result.
    d["fwd_dm"] = d["fwd"] - d.groupby("day")["fwd"].transform("mean")

    print(f"\nWALLS BY REGIME — ES, {d['day'].nunique()} sessions, {len(d):,} bars")
    print(f"  forward {FWD}-bar move, SESSION-DEMEANED, by pocket sign\n")
    print("  Testing walls without the regime averages two opposite predictions")
    print("  into mush. The mechanism says: in LONG gamma dealers hedge AGAINST")
    print("  the move, so a call wall CAPS (negative) and a put wall SUPPORTS")
    print("  (positive). In SHORT gamma they hedge WITH it, so price is carried")
    print("  THROUGH both -- the wall is where the acceleration starts, not")
    print("  where it stops.\n")
    print(f"  {'regime':7} {'where':27} {'bars':>7} {'mean':>8} {'median':>8} {'up%':>6}")
    for reg in ("LONG", "SHORT"):
        r = d[d["regime"] == reg]
        for name, mask in (
                (f"within {NEAR:.0f}pt of a CALL wall", r["dcw"] <= NEAR),
                (f"within {NEAR:.0f}pt of a PUT wall", r["dpw"] <= NEAR),
                ("far from every wall (>40pt)", (r["dcw"] > 40) & (r["dpw"] > 40))):
            g = r[mask]
            if len(g) < 40:
                print(f"  {reg:7} {name:27} {len(g):>7}   (too few)")
                continue
            print(f"  {reg:7} {name:27} {len(g):>7,} {g['fwd_dm'].mean():>8.2f} "
                  f"{g['fwd_dm'].median():>8.2f} {100 * (g['fwd_dm'] > 0).mean():>5.0f}%")
        print()

    # ── 2. every onbreak trade, by pocket ────────────────────────────────
    print(f"\n{'=' * 70}\nONBREAK, trade by trade (pocket at entry)\n{'=' * 70}")
    prep = {}
    for dd in cdays:
        if dd not in basis:
            continue
        pr = gamma_profile(curves[dd][0], curves[dd][1])
        if pr:
            prep[dd] = (np.array([k for k, _, _ in pr["curve"]]),
                        np.array([c for _, _, c in pr["curve"]]), basis[dd])
    fills = sorted(csv.DictReader(open(ROOT / ".cache" / "replay_fills_ES_live.csv",
                                       encoding="utf-8-sig")),
                   key=lambda r: float(r["ts"]))
    book = collections.defaultdict(lambda: [0, 0.0, None, None])
    out = []
    for r in fills:
        s = r["sleeve"]
        if not s.startswith("ES:onbreak"):
            continue
        qty = int(float(r["side"])) * int(float(r["qty"]))
        px = float(r["price"])
        when = dt.datetime.fromtimestamp(int(float(r["ts"])) / 1e9, ET_TZ)
        day = when.strftime("%Y-%m-%d")
        pos, avg, reg, t0 = book[s]
        if pos == 0:
            src = [x for x in prep if x < day]
            reg = None
            if src:
                ks, cums, bs = prep[src[-1]]
                reg = "LONG" if float(np.interp(px - bs, ks, cums)) > 0 else "SHORT"
            book[s] = [qty, px, reg, when]
            continue
        if (qty > 0) != (pos > 0):
            n = min(abs(qty), abs(pos))
            pl = (px - avg) * (1 if pos > 0 else -1) * n * PV
            out.append((s, t0, reg, pl))
            book[s] = [pos + qty, px, None, None]
        else:
            book[s] = [pos + qty,
                       (avg * abs(pos) + px * abs(qty)) / (abs(pos) + abs(qty)),
                       reg, t0]
    base = [o for o in out if o[0] == "ES:onbreak"]
    print(f"  ES:onbreak alone (the un-duplicated sleeve), {len(base)} trips:")
    for _s, t0, reg, pl in sorted(base, key=lambda x: x[1]):
        print(f"    {t0:%Y-%m-%d %H:%M}  {reg or '?':6} {pl:>9,.0f}")
    for reg in ("LONG", "SHORT"):
        g = [pl for _s, _t, r_, pl in base if r_ == reg]
        if g:
            print(f"    {reg:6} n={len(g):>2}  total {sum(g):>9,.0f}  "
                  f"mean {np.mean(g):>7,.0f}  win {100 * np.mean([x > 0 for x in g]):>3.0f}%"
                  f"  best {max(g):>8,.0f}  worst {min(g):>8,.0f}")


if __name__ == "__main__":
    main()
