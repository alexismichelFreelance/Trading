"""The shape catalogue on the 2026 LIVE record -- out of sample for all of it.

Everything found so far comes from ESH5/ESM5, Feb-May 2025, Databento MBO:

  * tops and bottoms are different populations. Five features separate bottoms
    (dwell, marginal, coil, retest, v_in, all agreeing); nothing separates tops
    by more than 0.083 on a median split.
  * bottoms are events, tops are processes: 46% of bottoms are V-spikes against
    18% of tops.
  * bottoms carry a taxonomy -- capitulation flush -0.433, morning low -0.377,
    late active -0.302, late QUIET low only -0.116 against a -0.294 median.
  * tops do not cluster: one rare blowoff pair and then a continuum.

This recomputes the identical features on 2026 ES and NQ from the live
per-second record. Different year, different contracts, different feed, and NQ
has never been in any of it. Nothing is re-fitted here -- the groupings are the
ones already defined, applied as they stand.

The reversal is expressed as a fraction of the day's range throughout, so ES and
NQ are directly comparable and no volatility regime is doing the ranking.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=900)


def minutes(sym):
    d = q.df("SELECT ts,pxc,adelta,avol FROM claude_sec_live WHERE symbol='"
             + sym + "' AND pxc > 0 ORDER BY ts").copy()
    d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
    d["day"] = d.et.dt.strftime("%Y-%m-%d")
    d["mod"] = d.et.dt.hour * 60 + d.et.dt.minute
    d = d[(d["mod"] >= 570) & (d["mod"] < 960)]
    return d.groupby(["day", "mod"]).agg(
        px=("pxc", "last"), hi=("pxc", "max"), lo=("pxc", "min"),
        delta=("adelta", "sum"), vol=("avol", "sum")).reset_index()


def features(d, i, kind, med_v, rng):
    s = 1.0 if kind == "TOP" else -1.0
    px = d.px.values * s
    e = d.iloc[i]
    v_in = (px[i] - px[i - 10]) / 10.0
    v_in30 = (px[i] - px[i - 30]) / 30.0
    push10 = px[i] - px[i - 10]
    pre = px[i - 40:i - 10]
    pre_rng = float(pre.max() - pre.min()) if len(pre) else np.nan
    prior = px[max(0, i - 70):i - 10]
    marginal = (px[i] - float(prior.max())) / rng if len(prior) else np.nan
    back = np.where(px[i + 5:] >= px[i] - 0.10 * rng)[0]
    retest = int(back[0]) + 5 if len(back) else 999
    v5, v10 = float(d.vol.iloc[i - 5:i].sum()), float(d.vol.iloc[i - 10:i - 5].sum())
    # CAUSAL: only the 20 minutes BEFORE. The +/-20 version leaked the
    # outcome -- a hard reversal leaves the level fast, so it produced a
    # low dwell by construction and 'predicted' what it had measured.
    near = np.abs(px[max(0, i - 20):i + 1] - px[i]) <= 0.15 * rng
    return dict(
        v_in=round(v_in, 2),
        accel=round(v_in / v_in30, 2) if abs(v_in30) > 0.02 else None,
        coil=round(push10 / pre_rng, 2) if pre_rng and pre_rng > 0.5 else None,
        marginal=round(marginal, 3), retest=retest,
        v_hi=round(float(e.vol) / med_v, 2) if med_v else None,
        v_trend=round(v5 / v10, 2) if v10 > 0 else None,
        conv=round(abs(float(e.delta)) / max(float(e.vol), 1.0), 3),
        dwell=int(near.sum()), tod=int(e["mod"]) - 570,
        rev=round((px[min(i + 30, len(px) - 1)] - px[i]) / rng, 3))


def main():
    rows = []
    for sym in ("ES", "NQ"):
        g = minutes(sym)
        for day, d in g.groupby("day"):
            d = d.sort_values("mod").reset_index(drop=True)
            if len(d) < 250:
                continue
            rng = float(d.px.max() - d.px.min())
            med_v = float(d.vol.median())
            if rng <= 0 or med_v <= 0:
                continue
            for kind, i in (("TOP", int(d.hi.values.argmax())),
                            ("BOTTOM", int(d.lo.values.argmin()))):
                if i < 45 or i > len(d) - 35:
                    continue
                r = dict(day=day, sym=sym, kind=kind, rng=round(rng, 2))
                r.update(features(d, i, kind, med_v, rng))
                rows.append(r)
    t = pd.DataFrame(rows).dropna(subset=["rev"])
    t.to_csv(r"D:\Trading\strategy_lab\shapes_2026.csv", index=False)
    print("%d extremes (%d tops, %d bottoms) over %d sessions, ES+NQ 2026\n"
          % (len(t), (t.kind == "TOP").sum(), (t.kind == "BOTTOM").sum(),
             t.day.nunique()))

    for kind in ("TOP", "BOTTOM"):
        k = t[t.kind == kind]
        print("=== %s  n=%d  median rev %+0.3f ===" % (kind, len(k), k.rev.median()))
        # median split -- causal dwell runs 0..21, so the old absolute cuts
        # (<=20 / >=35) no longer partition anything. A split keeps this
        # directly comparable to the 2025 feature screen, which also splits.
        md = k.dwell.median()
        sp, wk = k[k.dwell <= md], k[k.dwell > md]
        print("  spike  (dwell<=%2.0f) n=%2d  %+0.3f     worked (dwell>%2.0f) n=%2d  %+0.3f"
              % (md, len(sp), sp.rev.median() if len(sp) else np.nan,
                 md, len(wk), wk.rev.median() if len(wk) else np.nan))
        ch, nc = k[k.conv < 0.12], k[k.conv >= 0.12]
        print("  churn  (conv<0.12) n=%2d  %+0.3f     conviction        n=%2d  %+0.3f"
              % (len(ch), ch.rev.median() if len(ch) else np.nan,
                 len(nc), nc.rev.median() if len(nc) else np.nan))
        if kind == "BOTTOM":
            # the 2025 taxonomy, applied as defined
            quiet = k[(k.tod >= 240) & (k.v_hi < 1.5) & (k.retest <= 10)]
            morn = k[k.tod <= 120]
            flush = k[(k.v_hi >= 3.0) & (k.dwell <= 20)]
            print("  --- 2025 bottom taxonomy applied unchanged ---")
            for lab, s, ref in (("late QUIET low", quiet, -0.116),
                                ("morning low", morn, -0.377),
                                ("capitulation flush", flush, -0.433)):
                print("    %-20s n=%2d  %+0.3f   (2025: %+0.3f)"
                      % (lab, len(s), s.rev.median() if len(s) else np.nan, ref))
        print("")


if __name__ == "__main__":
    main()
