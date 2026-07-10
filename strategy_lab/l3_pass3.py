"""L3 pass 3 — the churn finding vs its last confound.

Q1 ACTIVITY-MATCHED CONTROLS: for each liftoff case, controls are quiet seconds
   with the CLOSEST recent event-rate (events/sec over [-300,0)) in the same
   night. If the pre-liftoff cancel-clear excess dies here, churn was just
   "busy tape", not a precursor.
Q2 TIMING CURVE: receding-side cancel-clears in 30s bins over [-300,0) — does
   the elevation BUILD into liftoff or is it flat?
Q3 SIDE-SPECIFICITY: same pre-window on the ADVANCING side — is the excess
   specific to the side about to give way (directional information) or
   symmetric (ambient nervousness)?

Writes strategy_lab/l3_pass3/<night>.json per night (checkpointed).
"""
import io
import json
import os
import sys
from collections import defaultdict

import httpx
import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=240)
NS = 1_000_000_000
TICK = 0.25
BASE = 16000
SIZEN = 14000
THETA = 8.0
W_PRE, W_DUR = 300, 120
NBIN = 10                      # 30s bins over the pre-window
OUT = "D:/Trading/strategy_lab/l3_pass3"
os.makedirs(OUT, exist_ok=True)

cat = pd.read_csv("D:/Trading/strategy_lab/eth_move_catalog.csv")
NIGHTS = sorted(cat.night.unique())


def fetch_night(skey):
    d1 = (pd.Timestamp(skey) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    sql = (f"SELECT ts_recv, action, side, price, size, order_id FROM mbo_events "
           f"WHERE ts_recv >= '{d1}T21:00:00.000000Z' AND ts_recv < '{skey}T13:00:00.000000Z' "
           f"AND is_trade=false ORDER BY ts_recv")
    with httpx.stream("GET", "http://localhost:9000/exp", params={"query": sql},
                      timeout=900.0) as r:
        r.raise_for_status()
        buf = io.StringIO("".join(r.iter_text()))
    df = pd.read_csv(buf)
    df.columns = [c.strip('"') for c in df.columns]
    return df


def replay(df):
    if pd.api.types.is_numeric_dtype(df["ts_recv"]):
        t = pd.to_datetime(df["ts_recv"], unit="us", utc=True)
    else:
        t = pd.to_datetime(df["ts_recv"], utc=True)
    t = t.dt.as_unit("ns")
    ts = (t.astype("int64") // NS).tolist()
    amap = {"A": 0, "C": 1, "M": 2, "F": 3}
    smap = {"B": 0, "A": 1}
    act = [amap.get(a, -1) for a in df["action"]]
    sid = [smap.get(s, -1) for s in df["side"]]
    tk = (np.round(df["price"].to_numpy() / TICK).astype(int) - BASE).tolist()
    sz = df["size"].tolist()
    oid = df["order_id"].tolist()

    lvl = [np.zeros(SIZEN), np.zeros(SIZEN)]
    best = [None, None]
    orders = {}
    # per-second: clears[side][cause] and event counts
    csec = {0: defaultdict(lambda: [0, 0]), 1: defaultdict(lambda: [0, 0])}  # sec -> [cancel, fill]
    evsec = defaultdict(int)
    phantom = 0

    def rescan(s):
        b = best[s]
        if b is None:
            return
        step = -1 if s == 0 else 1
        p = b
        for _ in range(4000):
            p += step
            if 0 <= p < SIZEN and lvl[s][p] > 0:
                best[s] = p
                return
        best[s] = None

    def add(s, p, z):
        lvl[s][p] += z
        b = best[s]
        if b is None or (p > b if s == 0 else p < b):
            best[s] = p

    def remove(s, p, z, cause_idx, sec):
        nonlocal phantom
        cur = lvl[s][p]
        new = cur - z
        if new < 0:
            phantom += 1
            new = 0.0
        lvl[s][p] = new
        if new <= 0 and best[s] == p:
            if cause_idx >= 0:
                csec[s][sec][cause_idx] += 1
            rescan(s)

    n = len(ts)
    for i in range(n):
        s = ts[i]
        evsec[s] += 1
        sd = sid[i]
        if sd < 0:
            continue
        a = act[i]
        p = tk[i]
        if not (0 <= p < SIZEN):
            continue
        if a == 0:
            add(sd, p, sz[i])
            orders[oid[i]] = (sd, p, sz[i])
        elif a == 1:
            remove(sd, p, sz[i], 0, s)
            orders.pop(oid[i], None)
        elif a == 3:
            remove(sd, p, sz[i], 1, s)
            o = orders.get(oid[i])
            if o is not None:
                rem = o[2] - sz[i]
                if rem <= 0:
                    orders.pop(oid[i], None)
                else:
                    orders[oid[i]] = (o[0], o[1], rem)
        elif a == 2:
            o = orders.pop(oid[i], None)
            if o is not None:
                remove(o[0], o[1], o[2], -1, s)   # repricing not counted as clear-cause
            add(sd, p, sz[i])
            orders[oid[i]] = (sd, p, sz[i])
    return csec, evsec, phantom, n


def night_px(skey):
    d1 = (pd.Timestamp(skey) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    g = q.df(f"SELECT ts, pxc FROM claude_sec_eth WHERE "
             f"ts >= '{d1}T21:00:00.000000Z' AND ts < '{skey}T13:00:00.000000Z' ORDER BY ts")
    grid = pd.date_range(g.ts.min().floor("s"), g.ts.max().ceil("s"), freq="1s")
    px = g.set_index("ts")["pxc"].reindex(grid).ffill().bfill().to_numpy()
    secs = (grid.astype("int64") // NS).to_numpy()
    return secs, px


def zig_legs(secs, px):
    n = len(px)
    hi_i = lo_i = 0
    mode = 0
    piv_i, piv_p = 0, px[0]
    legs = []
    for i in range(1, n):
        if mode == 0:
            if px[i] >= px[hi_i]:
                hi_i = i
            if px[i] <= px[lo_i]:
                lo_i = i
            if px[hi_i] - px[i] >= THETA:
                legs.append((lo_i if lo_i < hi_i else 0, hi_i, +1))
                piv_i, piv_p, mode, lo_i = hi_i, px[hi_i], -1, i
            elif px[i] - px[lo_i] >= THETA:
                legs.append((hi_i if hi_i < lo_i else 0, lo_i, -1))
                piv_i, piv_p, mode, hi_i = lo_i, px[lo_i], +1, i
            continue
        if mode == +1:
            if px[i] > px[hi_i]:
                hi_i = i
            if px[hi_i] - px[i] >= THETA:
                if px[hi_i] - piv_p >= THETA:
                    legs.append((piv_i, hi_i, +1))
                piv_i, piv_p, mode, lo_i = hi_i, px[hi_i], -1, i
        else:
            if px[i] < px[lo_i]:
                lo_i = i
            if px[i] - px[lo_i] >= THETA:
                if piv_p - px[lo_i] >= THETA:
                    legs.append((piv_i, lo_i, -1))
                piv_i, piv_p, mode, hi_i = lo_i, px[lo_i], +1, i
    return [(s, e, d) for s, e, d in legs if e > s and abs(px[e] - px[s]) >= THETA]


def liftoff(px, s_i, e_i, d):
    base = px[s_i]
    esc = None
    for t in range(s_i, e_i + 1):
        if d * (px[t] - base) >= 3.0:
            esc = t
            break
    if esc is None:
        return s_i
    for t in range(esc, s_i - 1, -1):
        if d * (px[t] - base) <= 1.0:
            return t
    return s_i


def measure(csec, t0, d):
    """Binned pre-window clears (receding + advancing) + DUR totals."""
    rec = 1 if d > 0 else 0
    adv = 1 - rec
    bins_rc = [0] * NBIN
    bins_rf = [0] * NBIN
    pre_ac = pre_af = 0
    dur_rc = dur_rf = 0
    for k in range(-W_PRE, 0):
        sec = t0 + k
        b = (k + W_PRE) // 30
        c, f = csec[rec].get(sec, (0, 0))
        bins_rc[b] += c
        bins_rf[b] += f
        c2, f2 = csec[adv].get(sec, (0, 0))
        pre_ac += c2
        pre_af += f2
    for k in range(W_DUR):
        c, f = csec[rec].get(t0 + k, (0, 0))
        dur_rc += c
        dur_rf += f
    return dict(bins_rc=bins_rc, bins_rf=bins_rf, pre_rc=sum(bins_rc),
                pre_rf=sum(bins_rf), pre_ac=pre_ac, pre_af=pre_af,
                dur_rc=dur_rc, dur_rf=dur_rf)


rng = np.random.default_rng(9)
for skey in NIGHTS:
    fn = os.path.join(OUT, f"{skey}.json")
    if os.path.exists(fn):
        continue
    try:
        df = fetch_night(skey)
        csec, evsec, phantom, nev = replay(df)
        del df
        # cumulative event counts for fast window rates
        allsecs = sorted(evsec)
        cum = {}
        acc = 0
        for s in allsecs:
            acc += evsec[s]
            cum[s] = acc
        cum_keys = np.array(allsecs)
        cum_vals = np.array([cum[s] for s in allsecs], dtype=float)

        def rate(t):
            i2 = np.searchsorted(cum_keys, t, side="right") - 1
            i1 = np.searchsorted(cum_keys, t - W_PRE, side="right") - 1
            if i2 < 0:
                return 0.0
            v2 = cum_vals[i2]
            v1 = cum_vals[i1] if i1 >= 0 else 0.0
            return (v2 - v1) / W_PRE

        secs, px = night_px(skey)
        legs = [(s, e, d) for s, e, d in zig_legs(secs, px)
                if s >= 1800 + W_PRE and pd.Timestamp(int(secs[s]), unit="s").hour != 21]
        blocked = set()
        for s, e, d in legs:
            blocked.update(range(int(secs[s]) - 900, int(secs[e]) + 900))
        quiet = [int(t) for t in cum_keys
                 if t not in blocked and pd.Timestamp(int(t), unit="s").hour != 21
                 and t - W_PRE > cum_keys[0]]
        qrates = np.array([rate(t) for t in quiet]) if quiet else np.array([])
        used = set()
        cases, ctrls = [], []
        for s, e, d in legs:
            t0 = int(secs[liftoff(px, s, e, d)])
            if pd.Timestamp(t0, unit="s").hour == 21:
                continue
            m = measure(csec, t0, d)
            r0 = rate(t0)
            cases.append(dict(t0=t0, dir=d, rate=r0, **m))
            if len(quiet) == 0:
                continue
            order = np.argsort(np.abs(qrates - r0))
            picked = 0
            for j in order:
                tq = quiet[j]
                if tq in used:
                    continue
                used.add(tq)
                dpc = 1 if rng.random() < 0.5 else -1
                mc = measure(csec, tq, dpc)
                ctrls.append(dict(t0=tq, dir=dpc, rate=float(qrates[j]),
                                  case_rate=r0, **mc))
                picked += 1
                if picked == 2:
                    break
        with open(fn, "w") as f:
            json.dump(dict(night=skey, events=nev, phantom=phantom,
                           cases=cases, ctrls=ctrls), f)
        print(f"{skey}: events={nev:,} cases={len(cases)} ctrls={len(ctrls)}", flush=True)
    except Exception as ex:                        # noqa: BLE001
        print(f"{skey}: FAILED {ex}", flush=True)
print("PASS3 SWEEP DONE")
