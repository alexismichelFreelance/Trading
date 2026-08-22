"""Type A, FROZEN, on the 2026 live record. Tops and bottoms separately.

DEFINITION, unchanged from strategy_lab/top_catalogue.py where it was found on
2025 MBO data. Not re-fitted, not re-tuned, not softened:

    push  >= 15 points travelled in the 10 minutes INTO the extreme
    conv   < 0.12  |net delta| / volume ON the extreme minute

On 51 ES tops in Feb-May 2025 that selected 8, and all 8 gave back >= 16.75
points in the following 10 minutes (median -24.6) against a median of -6.25 for
the other 43. Three tops had the push but conviction HELD at the high (conv
0.130, 0.136, 0.296) and gave back only -5.75, -14.25, -3.75.

The thresholds were read off that same 51-row catalogue, so this is the first
honest test: different year, different contracts, different exchange feed, and
NQ as well as ES.

BOTTOMS ARE NOT ASSUMED TO MIRROR TOPS. The same arithmetic is applied to
session lows -- a >=15 point DECLINE into the low with no conviction on the low
minute -- and reported separately. If the parameters do not transfer, that is
worth knowing rather than hiding inside a combined number. Nothing here averages
tops together with bottoms.

15 points is an ES quantity. NQ moves roughly 6x as far, so for NQ the push
threshold is scaled by the ratio of the two instruments' median daily ranges --
measured from the data, not chosen.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

PUSH_ES = 15.0
CONV = 0.12
WIN = 10
q = QuestDB(timeout=900)


def load(sym):
    d = q.df("SELECT ts,pxc,adelta,avol FROM claude_sec_live WHERE symbol='"
             + sym + "' AND pxc > 0 ORDER BY ts").copy()
    d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
    d["day"] = d.et.dt.strftime("%Y-%m-%d")
    d["mod"] = d.et.dt.hour * 60 + d.et.dt.minute
    d = d[(d["mod"] >= 570) & (d["mod"] < 960)]
    g = d.groupby(["day", "mod"]).agg(px=("pxc", "last"), hi=("pxc", "max"),
                                      lo=("pxc", "min"), delta=("adelta", "sum"),
                                      vol=("avol", "sum")).reset_index()
    return g


def catalogue(sym, push_th):
    g = load(sym)
    rows = []
    for day, d in g.groupby("day"):
        d = d.sort_values("mod").reset_index(drop=True)
        if len(d) < 200:
            continue
        for kind, idx in (("TOP", int(d.hi.values.argmax())),
                          ("BOTTOM", int(d.lo.values.argmin()))):
            if idx < WIN + 2 or idx > len(d) - WIN - 2:
                continue
            e = d.iloc[idx]
            pre = d.iloc[idx - WIN:idx]
            post = d.iloc[idx + 1:idx + 1 + WIN]
            travel = float(e.px - pre.px.iloc[0])
            push = travel if kind == "TOP" else -travel
            conv = abs(float(e.delta)) / max(float(e.vol), 1.0)
            move = float(post.px.iloc[-1] - e.px)
            rows.append(dict(day=day, sym=sym, kind=kind, push=round(push, 2),
                             conv=round(conv, 3),
                             typeA=bool(push >= push_th and conv < CONV),
                             after=round(move if kind == "TOP" else -move, 2)))
    return pd.DataFrame(rows)


def main():
    # scale the push threshold by measured median daily range, not by guesswork
    scale = {}
    for s in ("ES", "NQ"):
        g = load(s)
        r = g.groupby("day").agg(h=("hi", "max"), l=("lo", "min"))
        scale[s] = float((r.h - r.l).median())
    ratio = scale["NQ"] / scale["ES"]
    print("median RTH range   ES %.1f   NQ %.1f   -> NQ push threshold %.1f pts"
          % (scale["ES"], scale["NQ"], PUSH_ES * ratio))
    print("(ES push threshold %.1f, conviction < %.2f -- both frozen from 2025)\n"
          % (PUSH_ES, CONV))

    t = pd.concat([catalogue("ES", PUSH_ES),
                   catalogue("NQ", PUSH_ES * ratio)], ignore_index=True)
    print("%d extremes catalogued over %d sessions\n" % (len(t), t.day.nunique()))

    for kind in ("TOP", "BOTTOM"):
        k = t[t.kind == kind]
        a, b = k[k.typeA], k[~k.typeA]
        print("=== %s ===  (`after` is the give-back, negative = reversed) " % kind)
        print("  Type A     n=%2d  median %+7.2f   worst %+7.2f  best %+7.2f"
              % (len(a), a.after.median() if len(a) else np.nan,
                 a.after.min() if len(a) else np.nan,
                 a.after.max() if len(a) else np.nan))
        print("  everything n=%2d  median %+7.2f" % (len(b), b.after.median()))
        for r in a.sort_values("after").itertuples():
            print("      %s %s  push %+7.2f  conv %.3f  ->  %+7.2f"
                  % (r.day, r.sym, r.push, r.conv, r.after))
        print("")
    t.to_csv(r"D:\Trading\strategy_lab\type_a_2026.csv", index=False)


if __name__ == "__main__":
    main()
