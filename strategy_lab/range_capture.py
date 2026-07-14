"""RANGE-CAPTURE harness + the user's confluence band-fade, 1 lot.

The user's success bar: an engine strategy is useless unless it RELIABLY extracts
>= 20% of the day's range. This measures exactly that, per day.

Strategy (their stated method, mechanized):
  FADE SHORT when a bar reaches VWAP+2sigma AND that extension sits within TOL of a
    RESISTANCE level (pivot R/day-high/gamma call-wall/flip). Cover at VWAP.
  FADE LONG  when a bar reaches VWAP-2sigma AND within TOL of a SUPPORT level
    (pivot S/day-low/put-wall/prior-close). Target the NEXT resistance level.
  Stop beyond the level; one position at a time; flat by 15:59 ET.
Score per day = points captured / RTH range. Report the distribution + P(>=20%).
"""
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

BASIS = 52.0
TOL = 4.0
STOP = 6.0
COST = 0.517
q = QuestDB(timeout=180)


def gamma_levels():
    gl = q.df("SELECT ts,put_wall,call_wall,zero_gamma FROM claude_gex_levels ORDER BY ts")
    gl["day"] = gl.ts.dt.strftime("%Y-%m-%d")
    out = {}
    days = gl["day"].tolist()
    for i, d in enumerate(days):
        if i == 0:
            continue
        p = gl.iloc[i - 1]                              # prior session (causal)
        lv = {"putWall": p.put_wall + BASIS, "callWall": p.call_wall + BASIS}
        if pd.notna(p.zero_gamma):
            lv["flip"] = p.zero_gamma + BASIS
        out[d] = lv
    return out


def run(df, gam):
    days = sorted(df.day.unique())
    prev = None
    week = []                                          # rolling (h,l,c) for weekly pivot
    rows = []
    for d in days:
        g = df[df.day == d].sort_values("mod")
        if len(g) < 200:
            prev = g
            continue
        h = g.h.to_numpy(); l = g.l.to_numpy(); c = g.c.to_numpy()
        vol = g.vol.to_numpy().astype(float); mod = g["mod"].to_numpy()
        cv = np.cumsum(vol); vwap = np.cumsum(c * vol) / np.maximum(cv, 1)
        sd = np.sqrt(np.cumsum(vol * (c - vwap) ** 2) / np.maximum(cv, 1))
        rng = h.max() - l.min()
        # levels for the day (all causal: prior day / prior week / prior gamma)
        res, sup = [], []
        if prev is not None and len(prev):
            ph, pl, pc = prev.h.max(), prev.l.min(), prev.c.iloc[-1]
            P = (ph + pl + pc) / 3
            res += [2 * P - pl, P + (ph - pl), ph]     # R1,R2,prior high
            sup += [2 * P - ph, P - (ph - pl), pl, pc]  # S1,S2,prior low,prior close
        if len(week) >= 3:
            wh = max(x[0] for x in week[-5:]); wl = min(x[1] for x in week[-5:]); wc = week[-1][2]
            WP = (wh + wl + wc) / 3
            res += [2 * WP - wl]; sup += [2 * WP - wh]
        for k, vv in gam.get(d, {}).items():
            (res if k in ("callWall", "flip") else sup).append(vv)
        res = np.array(res); sup = np.array(sup)

        def near(px, arr):
            return len(arr) and np.min(np.abs(arr - px)) <= TOL

        def next_res(px):
            up = res[res > px + 1]
            return up.min() if len(up) else px + 0.5 * rng

        pos = 0; entry = 0.0; stop = 0.0; tgt = 0.0; captured = 0.0; ntr = 0
        i = 30
        while i < len(g):
            if mod[i] >= 959:
                if pos != 0:
                    captured += pos * (c[i] - entry) - COST
                break
            if pos == 0:
                up = vwap[i] + 2 * sd[i]; dn = vwap[i] - 2 * sd[i]
                if h[i] >= up and (near(up, res) or near(h[i], res)):
                    pos = -1; entry = up; stop = up + STOP; tgt = vwap[i]; ntr += 1
                elif l[i] <= dn and (near(dn, sup) or near(l[i], sup)):
                    pos = 1; entry = dn; stop = dn - STOP; tgt = next_res(dn); ntr += 1
            else:
                hit_stop = (l[i] <= stop) if pos > 0 else (h[i] >= stop)
                hit_tgt = (h[i] >= tgt) if pos > 0 else (l[i] <= tgt)
                if hit_stop:
                    captured += pos * (stop - entry) - COST; pos = 0
                elif hit_tgt:
                    captured += pos * (tgt - entry) - COST; pos = 0
            i += 1
        rows.append(dict(day=d, rng=rng, captured=captured,
                         pct=captured / rng if rng > 0 else 0, ntr=ntr))
        if prev is not None and len(prev):
            week.append((prev.h.max(), prev.l.min(), prev.c.iloc[-1]))
        prev = g
    return pd.DataFrame(rows)


def load_2026():
    df = q.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live ORDER BY ts")
    et = df.ts.dt.tz_convert("America/New_York")
    df["day"] = et.dt.strftime("%Y-%m-%d"); df["mod"] = et.dt.hour * 60 + et.dt.minute
    return df[(df["mod"] >= 570) & (df["mod"] < 960)]


def load_2025():
    df = q.df("SELECT symbol,ts,o,h,l,c,vol FROM claude_bars_1m ORDER BY ts")
    et = df.ts.dt.tz_convert("America/New_York")
    df["day"] = et.dt.strftime("%Y-%m-%d"); df["mod"] = et.dt.hour * 60 + et.dt.minute
    df = df[~((df.symbol == "ESH5") & (df.day >= "2025-03-20"))]
    df = df[~((df.symbol == "ESM5") & (df.day < "2025-03-20"))]
    return df[(df["mod"] >= 570) & (df["mod"] < 960)]


def report(tag, R):
    if not len(R):
        print(f"{tag}: no days"); return
    p = R.pct.to_numpy()
    print(f"{tag}: {len(R)} days  median range {R.rng.median():.0f}pt  "
          f"median capture {np.median(R.captured):+.1f}pt ({np.median(p)*100:+.0f}% of range)  "
          f"mean {np.mean(p)*100:+.0f}%")
    print(f"    P(capture >= 20% of range) = {np.mean(p >= 0.20):.0%}   "
          f">=10% = {np.mean(p >= 0.10):.0%}   positive = {np.mean(p > 0):.0%}   "
          f"trades/day median {int(R.ntr.median())}")


def run_scale(df, gam, maxu=5, add_step=4.0, cat=12.0, dir_gate=False):
    """The user's SCALING fade: add a unit every add_step against you (up to maxu),
    cover ALL at VWAP; catastrophe-exit if price runs `cat` beyond the first entry.
    Per-unit capture (avg entry vs cover) so it's 1-lot-comparable to run().
    dir_gate: use the first-60m VWAP side (which persists, +0.45 corr) to block
    LONG fades on below-VWAP days and SHORT fades on above-VWAP days."""
    days = sorted(df.day.unique()); prev = None; week = []; rows = []
    for d in days:
        g = df[df.day == d].sort_values("mod")
        if len(g) < 200:
            prev = g; continue
        h = g.h.to_numpy(); l = g.l.to_numpy(); c = g.c.to_numpy()
        vol = g.vol.to_numpy().astype(float); mod = g["mod"].to_numpy()
        cv = np.cumsum(vol); vwap = np.cumsum(c * vol) / np.maximum(cv, 1)
        sd = np.sqrt(np.cumsum(vol * (c - vwap) ** 2) / np.maximum(cv, 1))
        rng = h.max() - l.min()
        res, sup = [], []
        if prev is not None and len(prev):
            ph, pl, pc = prev.h.max(), prev.l.min(), prev.c.iloc[-1]; P = (ph + pl + pc) / 3
            res += [2 * P - pl, P + (ph - pl), ph]; sup += [2 * P - ph, P - (ph - pl), pl, pc]
        if len(week) >= 3:
            wh = max(x[0] for x in week[-5:]); wl = min(x[1] for x in week[-5:]); wc = week[-1][2]
            WP = (wh + wl + wc) / 3; res += [2 * WP - wl]; sup += [2 * WP - wh]
        for k, vv in gam.get(d, {}).items():
            (res if k in ("callWall", "flip") else sup).append(vv)
        res = np.array(res); sup = np.array(sup)

        def near(px, arr):
            return len(arr) and np.min(np.abs(arr - px)) <= TOL

        # first-60m VWAP side (causal, known by 10:30 ET): it persists, so it says
        # whether VWAP is support (price above -> buy dips) or resistance (below).
        em = mod < 630
        e_below = float(np.mean(c[em] < vwap[em])) if em.any() else 0.5
        allow_long = (not dir_gate) or e_below <= 0.65      # block longs on down-days
        allow_short = (not dir_gate) or e_below >= 0.35      # block shorts on up-days

        pos = 0; avg = 0.0; units = 0; first = 0.0; nextadd = 0.0; captured = 0.0; ntr = 0
        i = 30
        while i < len(g):
            if mod[i] >= 959:
                if pos != 0:
                    captured += pos * (c[i] - avg); ntr += 1
                break
            if pos == 0:
                up = vwap[i] + 2 * sd[i]; dn = vwap[i] - 2 * sd[i]
                if allow_short and h[i] >= up and (near(up, res) or near(h[i], res)):
                    pos = -1; avg = up; units = 1; first = up; nextadd = up + add_step
                elif allow_long and l[i] <= dn and (near(dn, sup) or near(l[i], sup)):
                    pos = 1; avg = dn; units = 1; first = dn; nextadd = dn - add_step
            else:
                if pos < 0:                              # short fade
                    if h[i] >= first + cat:              # catastrophe
                        captured += -(first + cat - avg); pos = 0; ntr += 1
                    elif l[i] <= vwap[i]:                # cover all at VWAP
                        captured += (avg - vwap[i]); pos = 0; ntr += 1
                    elif h[i] >= nextadd and units < maxu:
                        avg = (avg * units + nextadd) / (units + 1); units += 1; nextadd += add_step
                else:                                    # long fade
                    if l[i] <= first - cat:
                        captured += (first - cat - avg); pos = 0; ntr += 1
                    elif h[i] >= vwap[i]:
                        captured += (vwap[i] - avg); pos = 0; ntr += 1
                    elif l[i] <= nextadd and units < maxu:
                        avg = (avg * units + nextadd) / (units + 1); units += 1; nextadd -= add_step
            i += 1
        rows.append(dict(day=d, rng=rng, captured=captured - COST * max(1, ntr),
                         pct=(captured - COST * max(1, ntr)) / rng if rng > 0 else 0, ntr=ntr))
        if prev is not None and len(prev):
            week.append((prev.h.max(), prev.l.min(), prev.c.iloc[-1]))
        prev = g
    return pd.DataFrame(rows)


if __name__ == "__main__":
    gam = gamma_levels()
    print("== confluence band-fade, 1 lot, scored by % of daily range ==")
    report("2026 one-shot", run(load_2026(), gam))
    report("2025 one-shot", run(load_2025(), gam))
    print("\n== SCALING fade (add into it, cover at VWAP) ==")
    report("2026 scaling ", run_scale(load_2026(), gam))
    report("2025 scaling ", run_scale(load_2025(), gam))
    print("\n== + DIRECTIONAL gate (VWAP side persists: no dip-buys on below-VWAP days) ==")
    report("2026 dir-gate", run_scale(load_2026(), gam, dir_gate=True))
    report("2025 dir-gate", run_scale(load_2025(), gam, dir_gate=True))
