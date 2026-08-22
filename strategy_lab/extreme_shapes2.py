"""Enriched shape catalogue of session extremes, for typing them properly.

extreme_shapes.py showed tops and bottoms are different animals -- conviction at
the extreme separates tops 2:1 and does nothing at all at bottoms, and the
characteristic shapes are inverted (46% of bottoms are V-spikes, 56% of tops are
worked levels). That was five hand-picked features. This adds the ones needed to
recognise the OTHER structures -- failed retests, coil breaks, decelerating
runs, absorption, trend-day ends -- and to let clustering propose types rather
than only testing the ones somebody thought of.

Everything is measured with the sign MIRRORED for bottoms, so `push`, `rev` and
the rest always mean the same thing regardless of which end of the day it is.
Tops and bottoms are never averaged together.

FEATURES

  approach     v_in / v_in30      speed into the extreme, near and far
               accel              v_in / v_in30: >1 is a final acceleration
                                  onto the level, <1 a run that was already
                                  slowing before it got there
               run_len            minutes since the move began (last time price
                                  was 50% of the day's range away)
  base         pre_rng            range of the 30 min before the final push
               coil               push10 / pre_rng: the last leg against its base
  structure    marginal           how far past the prior hour's extreme, as a
                                  fraction of the day's range. ~0 = failed
                                  retest of an old level
               retest             minutes until price came back within 10% of
                                  the day's range of the extreme, 999 if never.
                                  Small = the level got tested again
  activity     v_hi               volume on the extreme minute vs session median
               v_trend            volume of the last 5 min into it vs the 5
                                  before that: is participation rising or dying
               conv               |net delta| / volume on the extreme minute
               dwell              minutes within 15% of range of the extreme in
                                  the +/-20 window
  timing       tod                minutes since 09:30 -- open, midday, late
  outcome      rev                move away over 30 min as a FRACTION of the
                                  day's range, negative = reversed
               rev60              the same over 60 minutes
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd

SRC = r"D:\Trading\strategy_lab\mbo_minutes.csv"
OUT = r"D:\Trading\strategy_lab\extreme_shapes2.csv"


def features(d, i, kind, med_v, rng):
    s = 1.0 if kind == "TOP" else -1.0
    px = d.px.values * s
    hi = d.hi.values * s if kind == "TOP" else -d.lo.values
    e = d.iloc[i]
    v_in = (px[i] - px[i - 10]) / 10.0
    v_in30 = (px[i] - px[i - 30]) / 30.0
    push10 = px[i] - px[i - 10]
    pre = px[i - 40:i - 10]
    pre_rng = float(pre.max() - pre.min()) if len(pre) else np.nan
    prior = px[max(0, i - 70):i - 10]
    marginal = (px[i] - float(prior.max())) / rng if len(prior) else np.nan
    # how long the move had been running: last time price was half a range away
    away = np.where(px[:i] <= px[i] - 0.5 * rng)[0]
    run_len = i - int(away[-1]) if len(away) else i
    # retest: first bar after +5 that comes back within 10% of range
    back = np.where(px[i + 5:] >= px[i] - 0.10 * rng)[0]
    retest = int(back[0]) + 5 if len(back) else 999
    v5 = float(d.vol.iloc[i - 5:i].sum())
    v10 = float(d.vol.iloc[i - 10:i - 5].sum())
    # CAUSAL: only the 20 minutes BEFORE. The +/-20 version leaked the
    # outcome -- a hard reversal leaves the level fast, so it produced a
    # low dwell by construction and 'predicted' what it had measured.
    near = np.abs(px[max(0, i - 20):i + 1] - px[i]) <= 0.15 * rng
    return dict(
        v_in=round(v_in, 2), v_in30=round(v_in30, 2),
        accel=round(v_in / v_in30, 2) if abs(v_in30) > 0.02 else None,
        run_len=int(run_len),
        pre_rng=round(pre_rng, 2),
        coil=round(push10 / pre_rng, 2) if pre_rng and pre_rng > 0.5 else None,
        marginal=round(marginal, 3),
        retest=retest,
        v_hi=round(float(e.vol) / med_v, 2) if med_v else None,
        v_trend=round(v5 / v10, 2) if v10 > 0 else None,
        conv=round(abs(float(e.buy_v - e.sell_v)) / max(float(e.vol), 1.0), 3),
        dwell=int(near.sum()),
        tod=int(e["mod"]) - 570,
        rev=round((px[min(i + 30, len(px) - 1)] - px[i]) / rng, 3),
        rev60=round((px[min(i + 60, len(px) - 1)] - px[i]) / rng, 3))


def main():
    m = pd.read_csv(SRC)
    m = m[(m["mod"] >= 570) & (m["mod"] < 960)].copy()
    rows = []
    for day, d in m.groupby("day", sort=True):
        d = d.sort_values("mod").reset_index(drop=True)
        if len(d) < 250:
            continue
        rng = float(d.px.max() - d.px.min())
        if rng <= 0:
            continue
        med_v = float(d.vol.median())
        for kind, i in (("TOP", int(d.hi.values.argmax())),
                        ("BOTTOM", int(d.lo.values.argmin()))):
            if i < 45 or i > len(d) - 35:
                continue
            r = dict(day=day, sym=d.symbol.iloc[0], kind=kind, rng=round(rng, 2))
            r.update(features(d, i, kind, med_v, rng))
            rows.append(r)
    t = pd.DataFrame(rows)
    t.to_csv(OUT, index=False)
    print("%d extremes (%d tops, %d bottoms) -> %s"
          % (len(t), (t.kind == "TOP").sum(), (t.kind == "BOTTOM").sum(), OUT))

    # ---- FEATURE SCREEN, tops and bottoms measured SEPARATELY ----
    feats = ["v_in", "v_in30", "accel", "run_len", "pre_rng", "coil",
             "marginal", "retest", "v_hi", "v_trend", "conv", "dwell", "tod"]
    for kind in ("TOP", "BOTTOM"):
        k = t[t.kind == kind].dropna(subset=["rev"])
        print("\n=== %s  n=%d   overall median rev %+0.3f ==="
              % (kind, len(k), k.rev.median()))
        print("  feature      low-half median   high-half median    gap")
        out = []
        for f in feats:
            s = k.dropna(subset=[f])
            if len(s) < 12:
                continue
            med = s[f].median()
            lo, hi = s[s[f] <= med], s[s[f] > med]
            if len(lo) < 5 or len(hi) < 5:
                continue
            out.append((abs(lo.rev.median() - hi.rev.median()), f,
                        lo.rev.median(), hi.rev.median(), len(lo), len(hi)))
        for gap, f, l, h, nl, nh in sorted(out, reverse=True):
            print("  %-10s      %+7.3f (n=%2d)     %+7.3f (n=%2d)    %.3f"
                  % (f, l, nl, h, nh, gap))


if __name__ == "__main__":
    main()
