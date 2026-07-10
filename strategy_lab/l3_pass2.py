"""L3 pass 2 — ALL overnight sessions, within-night hour-matched controls,
liftoff-anchored initiations, per-night checkpointing.

For each night: replay full MBO (fast int-tick book), classify best-level
clears (fill/cancel/modify), sample depth@best per second; detect zigzag legs
(>=8pt) on the 1s tape, re-anchor each start at LIFTOFF (last second within 1pt
of the pivot extreme before the first 3pt escape); measure PRE[-300,0) and
DUR[0,+120) on the receding side; controls = same night, same HOUR, >=900s from
any leg, one pseudo-direction each. Writes strategy_lab/l3_pass2/<night>.json
(skipped if present). Aggregation/sign-tests in l3_pass2_agg.py.
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
BASE = 16000          # tick offset (=4000.0 px); array covers 4000..7500
SIZEN = 14000
THETA = 8.0
W_PRE, W_DUR = 300, 120
OUT = "D:/Trading/strategy_lab/l3_pass2"
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
    """Fast book: numpy level arrays indexed by tick, python-list event loop."""
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

    lvl = [np.zeros(SIZEN), np.zeros(SIZEN)]          # [bid, ask]
    best = [None, None]
    orders = {}
    clears = []                                        # (sec, side, cause)
    samples = {}                                       # sec -> (bb,ba,szb,sza)
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

    def remove(s, p, z, cause, sec):
        nonlocal phantom
        cur = lvl[s][p]
        new = cur - z
        if new < 0:
            phantom += 1
            new = 0.0
        lvl[s][p] = new
        if new <= 0 and best[s] == p:
            clears.append((sec, s, cause))
            rescan(s)

    cur_sec = ts[0]
    n = len(ts)
    for i in range(n):
        s = ts[i]
        if s != cur_sec:
            bb, ba = best[0], best[1]
            samples[cur_sec] = (bb, ba,
                                lvl[0][bb] if bb is not None else 0,
                                lvl[1][ba] if ba is not None else 0)
            cur_sec = s
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
            remove(sd, p, sz[i], "cancel", s)
            orders.pop(oid[i], None)
        elif a == 3:
            remove(sd, p, sz[i], "fill", s)
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
                remove(o[0], o[1], o[2], "modify", s)
            add(sd, p, sz[i])
            orders[oid[i]] = (sd, p, sz[i])
    bb, ba = best[0], best[1]
    samples[cur_sec] = (bb, ba,
                        lvl[0][bb] if bb is not None else 0,
                        lvl[1][ba] if ba is not None else 0)
    return clears, samples, phantom, n


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
    """Last second within 1pt of the pivot extreme before the first 3pt escape."""
    base = px[s_i]
    esc = None
    for t in range(s_i, e_i + 1):
        if d * (px[t] - base) >= 3.0:
            esc = t
            break
    if esc is None:
        return s_i
    t0 = s_i
    for t in range(esc, s_i - 1, -1):
        if d * (px[t] - base) <= 1.0:
            t0 = t
            break
    return t0


def wstats(clears_by_side, samples, t0, d):
    rec = 1 if d > 0 else 0                       # receding: ask for up, bid for down
    pre = dict(fill=0, cancel=0, modify=0)
    dur = dict(fill=0, cancel=0, modify=0)
    for sec, cause in clears_by_side[rec]:
        if t0 - W_PRE <= sec < t0:
            pre[cause] += 1
        elif t0 <= sec < t0 + W_DUR:
            dur[cause] += 1
    idx = 2 if rec == 0 else 3
    traj = {}
    for off in (-300, -120, -60, -10, 0, 30):
        v = [samples[t0 + off + k][idx] for k in range(-4, 5) if (t0 + off + k) in samples]
        traj[str(off)] = float(np.median(v)) if v else None
    return pre, dur, traj


rng = np.random.default_rng(5)
for skey in NIGHTS:
    fn = os.path.join(OUT, f"{skey}.json")
    if os.path.exists(fn):
        continue
    try:
        df = fetch_night(skey)
        clears, samples, phantom, nev = replay(df)
        del df
        cbs = {0: [], 1: []}
        for sec, s, cause in clears:
            cbs[s].append((sec, cause))
        secs, px = night_px(skey)
        legs = [(s, e, d) for s, e, d in zig_legs(secs, px)
                if s >= 1800 + W_PRE and pd.Timestamp(int(secs[s]), unit="s").hour != 21]
        blocked = set()
        for s, e, d in legs:
            blocked.update(range(int(secs[s]) - 900, int(secs[e]) + 900))
        cases, ctrls = [], []
        by_hour_quiet = defaultdict(list)
        all_quiet = []
        for t in samples:
            if t not in blocked and pd.Timestamp(t, unit="s").hour != 21:
                by_hour_quiet[pd.Timestamp(t, unit="s").hour].append(t)
                all_quiet.append(t)
        used = set()
        for s, e, d in legs:
            t0 = int(secs[liftoff(px, s, e, d)])
            hr = pd.Timestamp(t0, unit="s").hour
            if hr == 21:
                continue
            pre, dur, traj = wstats(cbs, samples, t0, d)
            cases.append(dict(t0=t0, hr=hr, dir=d, pre=pre, dur=dur, traj=traj))
            # control pool: matched hour -> adjacent hours -> ANY quiet second
            pool = [t for t in by_hour_quiet.get(hr, []) if t not in used] \
                or [t for t in (by_hour_quiet.get((hr + 1) % 24, [])
                                + by_hour_quiet.get((hr - 1) % 24, [])) if t not in used] \
                or [t for t in all_quiet if t not in used]
            for tc in rng.choice(pool, size=min(2, len(pool)), replace=False) if pool else []:
                used.add(int(tc))
                dpc = 1 if rng.random() < 0.5 else -1
                prc, duc, trc = wstats(cbs, samples, int(tc), dpc)
                ctrls.append(dict(t0=int(tc), hr=int(pd.Timestamp(int(tc), unit='s').hour),
                                  case_hr=hr, dir=dpc, pre=prc, dur=duc, traj=trc))
        with open(fn, "w") as f:
            json.dump(dict(night=skey, events=nev, phantom=phantom,
                           n_clears=len(clears), cases=cases, ctrls=ctrls), f)
        print(f"{skey}: events={nev:,} clears={len(clears):,} cases={len(cases)} "
              f"ctrls={len(ctrls)}", flush=True)
    except Exception as ex:                        # noqa: BLE001 - keep the sweep going
        print(f"{skey}: FAILED {ex}", flush=True)
print("PASS2 SWEEP DONE")
