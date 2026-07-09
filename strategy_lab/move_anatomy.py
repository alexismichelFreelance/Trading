"""OVERNIGHT MOVE ANATOMY — observation first, no strategy, no thresholds tuned.

For every >=8pt zigzag leg in the 72 overnight sessions, a case file:
  WHEN   start hour (UTC), duration, month
  SIZE   extent (pt), max 1s jump
  FUEL   traded volume, aligned-aggressor share, contracts consumed PER POINT
  BOOK   receding-side cancels vs consumption -> WithdrawalIndex =
         cancels_receding / (cancels_receding + consumed_receding)
         (>0.5 = liquidity LEFT faster than it was eaten = quote-pulling move)
  GAP    share of price-moving seconds with ZERO trades (pure quote moves)
  LEVELS start/end tagged (+-2pt) vs the USER'S toolkit: overnight VWAP, VWAP
         +-2sig/3sig, daily/weekly/monthly floor pivots (P,R1-3,S1-3), prior RTH
         high/low/close, running overnight high/low. Enrichment vs random
         same-night control points.
Output: full per-move CSV + aggregate phenomenology.
"""
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=180)
NS = 1_000_000_000
THETA = 8.0


def load_eth(table, cols):
    rows = []
    for d in q.df(f"SELECT DISTINCT to_str(ts,'yyyy-MM-dd') d FROM {table} ORDER BY d")["d"]:
        rows.append(q.df(f"SELECT ts, {cols} FROM {table} "
                         f"WHERE ts >= '{d}T00:00:00.000000Z' AND ts < '{d}T23:59:59.999999Z' ORDER BY ts"))
    return pd.concat(rows, ignore_index=True)


E = load_eth("claude_sec_eth", "pxc, adelta, avol")
B = load_eth("claude_sec_eth_book", "bid_add, bid_cancel, ask_add, ask_cancel")
E = E.merge(B, on="ts", how="left").fillna(0)
E["skey"] = np.where(E.ts.dt.hour >= 21,
                     (E.ts + pd.Timedelta(hours=13)).dt.strftime("%Y-%m-%d"),
                     E.ts.dt.strftime("%Y-%m-%d"))

# ── levels from RTH (user toolkit) ───────────────────────────────────────
rth = q.df("SELECT symbol, ts, o, h, l, c FROM claude_bars_1m ORDER BY ts")
rth["day"] = rth.ts.dt.strftime("%Y-%m-%d")
daily = rth.groupby("day").agg(h=("h", "max"), l=("l", "min"), c=("c", "last")).reset_index()
daily["week"] = pd.to_datetime(daily.day).dt.strftime("%G-%V")
daily["mon"] = daily.day.str[:7]


def floor_pivots(h, l, c):
    p = (h + l + c) / 3
    return {"P": p, "R1": 2 * p - l, "S1": 2 * p - h, "R2": p + (h - l),
            "S2": p - (h - l), "R3": h + 2 * (p - l), "S3": l - 2 * (h - p)}


daily_piv, weekly_piv, monthly_piv, prior_rth = {}, {}, {}, {}
days = daily.day.tolist()
for i in range(1, len(days)):
    r = daily.iloc[i - 1]
    daily_piv[days[i]] = floor_pivots(r.h, r.l, r.c)
    prior_rth[days[i]] = {"pdH": r.h, "pdL": r.l, "pdC": r.c}
wk = daily.groupby("week").agg(h=("h", "max"), l=("l", "min"), c=("c", "last"),
                               last_day=("day", "max")).reset_index()
for i in range(1, len(wk)):
    r = wk.iloc[i - 1]
    weekly_piv[wk.iloc[i].week] = floor_pivots(r.h, r.l, r.c)
mo = daily.groupby("mon").agg(h=("h", "max"), l=("l", "min"), c=("c", "last")).reset_index()
for i in range(1, len(mo)):
    r = mo.iloc[i - 1]
    monthly_piv[mo.iloc[i].mon] = floor_pivots(r.h, r.l, r.c)


def night_levels(skey, px, vwap, sig):
    """Return dict name->price for this night (VWAP bands are per-second, handled
    separately)."""
    lv = {}
    dp = daily_piv.get(skey)
    if dp:
        lv.update({f"d{k}": v for k, v in dp.items()})
    wp = weekly_piv.get(pd.Timestamp(skey).strftime("%G-%V"))
    if wp:
        lv.update({f"w{k}": v for k, v in wp.items()})
    mp = monthly_piv.get(skey[:7])
    if mp:
        lv.update({f"m{k}": v for k, v in mp.items()})
    pr = prior_rth.get(skey)
    if pr:
        lv.update(pr)
    return lv


def tag_levels(p, i, lv, vwap, sig, onh, onl, tol=2.0):
    tags = [name for name, v in lv.items() if abs(p - v) <= tol]
    if abs(p - vwap[i]) <= tol:
        tags.append("VWAP")
    for mlt, nm in ((2, "VW2s"), (-2, "VW-2s"), (3, "VW3s"), (-3, "VW-3s")):
        if abs(p - (vwap[i] + mlt * sig[i])) <= tol:
            tags.append(nm)
    if abs(p - onh[i]) <= tol:
        tags.append("ONH")
    if abs(p - onl[i]) <= tol:
        tags.append("ONL")
    return tags


moves = []
rng = np.random.default_rng(7)
ctrl_hits = 0
ctrl_n = 0
for skey, g in E.groupby("skey"):
    g = g.sort_values("ts")
    if (g.ts.max() - g.ts.min()) < pd.Timedelta(hours=8):
        continue
    grid = pd.date_range(g.ts.min().floor("s"), g.ts.max().ceil("s"), freq="1s")
    D = g.set_index("ts").reindex(grid)
    px = D["pxc"].ffill().bfill().to_numpy()
    ad = D["adelta"].fillna(0).to_numpy()
    vol = D["avol"].fillna(0).to_numpy()
    ba = D["bid_add"].fillna(0).to_numpy()
    bc = D["bid_cancel"].fillna(0).to_numpy()
    aa = D["ask_add"].fillna(0).to_numpy()
    ac = D["ask_cancel"].fillna(0).to_numpy()
    hrs = grid.hour.to_numpy()
    # overnight VWAP + sigma (causal)
    cv = np.cumsum(vol)
    cpv = np.cumsum(px * vol)
    vwap = np.where(cv > 0, cpv / np.maximum(cv, 1), px)
    cd2 = np.cumsum(vol * (px - vwap) ** 2)
    sig = np.sqrt(np.where(cv > 0, cd2 / np.maximum(cv, 1), 0.0))
    onh = np.maximum.accumulate(px)
    onl = np.minimum.accumulate(px)
    lv = night_levels(skey, px, vwap, sig)

    # zigzag legs (explicit warmup: mode 0 until the first THETA move resolves)
    n = len(px)
    hi_i = lo_i = 0
    mode = 0                       # +1 tracking a high, -1 tracking a low
    piv_i, piv_p = 0, px[0]        # last confirmed pivot
    legs = []
    for i in range(1, n):
        if mode == 0:
            if px[i] >= px[hi_i]:
                hi_i = i
            if px[i] <= px[lo_i]:
                lo_i = i
            if px[hi_i] - px[i] >= THETA:      # first swing: a high confirmed
                piv_i, piv_p = lo_i if lo_i < hi_i else 0, px[lo_i if lo_i < hi_i else 0]
                legs.append((piv_i, hi_i, +1))
                piv_i, piv_p, mode, lo_i = hi_i, px[hi_i], -1, i
            elif px[i] - px[lo_i] >= THETA:    # first swing: a low confirmed
                piv_i, piv_p = hi_i if hi_i < lo_i else 0, px[hi_i if hi_i < lo_i else 0]
                legs.append((piv_i, lo_i, -1))
                piv_i, piv_p, mode, hi_i = lo_i, px[lo_i], +1, i
            continue
        if mode == +1:                          # up-swing: track the high
            if px[i] > px[hi_i]:
                hi_i = i
            if px[hi_i] - px[i] >= THETA:
                if px[hi_i] - piv_p >= THETA:
                    legs.append((piv_i, hi_i, +1))
                piv_i, piv_p, mode, lo_i = hi_i, px[hi_i], -1, i
        else:                                   # down-swing: track the low
            if px[i] < px[lo_i]:
                lo_i = i
            if px[i] - px[lo_i] >= THETA:
                if piv_p - px[lo_i] >= THETA:
                    legs.append((piv_i, lo_i, -1))
                piv_i, piv_p, mode, hi_i = lo_i, px[lo_i], +1, i
    for s_i, e_i, d in legs:
        if e_i <= s_i:
            continue
        if e_i <= s_i:
            continue
        seg = slice(s_i, e_i + 1)
        extent = abs(px[e_i] - px[s_i])
        v = vol[seg].sum()
        adel = ad[seg].sum()
        buyv = (v + adel) / 2
        sellv = (v - adel) / 2
        consumed = buyv if d > 0 else sellv            # eats the receding side
        canc_rec = ac[seg].sum() if d > 0 else bc[seg].sum()
        wdx = canc_rec / max(1.0, canc_rec + consumed)
        dpx = np.abs(np.diff(px[seg]))
        movsec = dpx >= 0.25
        gap_share = float(np.mean(vol[seg][1:][movsec] == 0)) if movsec.any() else 0.0
        stags = tag_levels(px[s_i], s_i, lv, vwap, sig, onh, onl)
        etags = tag_levels(px[e_i], e_i, lv, vwap, sig, onh, onl)
        moves.append(dict(
            night=skey, month=skey[:7], hr=int(hrs[s_i]), dir=d,
            extent=round(extent, 2), dur_min=round((e_i - s_i) / 60, 1),
            vol=int(v), aligned_aggr=round(d * adel / max(1.0, v), 2),
            cpp=round(v / max(0.25, extent), 0),
            wdx=round(wdx, 2), gap_share=round(gap_share, 2),
            start_px=px[s_i], start_tags=",".join(stags) or "-",
            end_tags=",".join(etags) or "-"))
        # matched random controls for level-tag enrichment
        for _ in range(3):
            j = int(rng.integers(1800, n - 1))
            ctrl_hits += 1 if tag_levels(px[j], j, lv, vwap, sig, onh, onl) else 0
            ctrl_n += 1

M = pd.DataFrame(moves)
M.to_csv("D:/Trading/strategy_lab/eth_move_catalog.csv", index=False)
print(f"moves >= {THETA}pt: {len(M)} over {M.night.nunique()} nights "
      f"({len(M)/M.night.nunique():.1f}/night)\n")

print("== mechanism mix ==")
print(f"withdrawal-dominant (wdx>0.5): {(M.wdx>0.5).mean():.0%} of moves; "
      f"median wdx={M.wdx.median():.2f}")
print(f"median contracts/point: {M.cpp.median():.0f}  (p25 {M.cpp.quantile(.25):.0f}, "
      f"p75 {M.cpp.quantile(.75):.0f})")
print(f"median aligned aggressor share: {M.aligned_aggr.median():+.2f}  "
      f"(share of volume on the move's side)")
print(f"pure quote-gap seconds (px moved, zero trades): median {M.gap_share.median():.0%} "
      f"of moving seconds")

print("\n== by hour (UTC): n, median extent, median wdx, median cpp ==")
for h in [21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]:
    sub = M[M.hr == h]
    if len(sub) < 3:
        continue
    print(f"  {h:02d}: n={len(sub):3d}  ext={sub.extent.median():5.1f}  "
          f"wdx={sub.wdx.median():.2f}  cpp={sub.cpp.median():5.0f}  "
          f"aggr={sub.aligned_aggr.median():+.2f}")

start_hit = (M.start_tags != "-").mean()
end_hit = (M.end_tags != "-").mean()
ctrl = ctrl_hits / max(1, ctrl_n)
print(f"\n== LEVELS (the user's toolkit, +-2pt) ==")
print(f"move STARTS at a level: {start_hit:.0%}   ENDS at a level: {end_hit:.0%}   "
      f"random control: {ctrl:.0%}")
cnt = defaultdict(int)
for t in M.end_tags:
    for x in t.split(","):
        if x != "-":
            cnt[x.rstrip('0123456789') if x[0] in 'dwm' else x] += 1
print("end-tag families:", dict(sorted(cnt.items(), key=lambda kv: -kv[1])))

print("\n== the 10 biggest moves, full anatomy ==")
cols = ["night", "hr", "dir", "extent", "dur_min", "vol", "aligned_aggr", "cpp",
        "wdx", "gap_share", "start_tags", "end_tags"]
print(M.nlargest(10, "extent")[cols].to_string(index=False))
