"""L3 TOP-OF-BOOK RECONSTRUCTION — the tick-by-tick microscope at the touch.

Replays raw MBO (A/C/M/F) for selected overnight sessions, maintaining per-side
price-level aggregates + best bid/ask, and CLASSIFIES every clear of the best
level by its terminal event:
    fill   -> the level was EATEN (sweep)
    cancel -> the level was ABANDONED (withdrawal)
    modify -> the level was REPRICED away
Case-control: for each catalogued >=8pt move-start (after 22:00 UTC), windows
[-300s, +300s] on the RECEDING side vs random quiet controls (pseudo-direction).
Also depth-at-best trajectory into the move-start (precursor check) and
bootstrap-quality metrics (book starts empty at 21:00 -> phantom removals).

Pure observation. No strategy.
"""
import io
import sys
from collections import defaultdict

import httpx
import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=180)
NS = 1_000_000_000
TICK = 0.25
THETA = 8.0
W_PRE, W_DUR = 300, 300

# ── pick study nights from the catalog ───────────────────────────────────
cat = pd.read_csv("D:/Trading/strategy_lab/eth_move_catalog.csv")
per_night = cat.groupby("night").agg(n=("extent", "size"), tot=("extent", "sum")).reset_index()
esh = per_night[per_night.night < "2025-03-20"].sort_values("tot")
esm = per_night[per_night.night >= "2025-03-20"].sort_values("tot")
NIGHTS = sorted({
    "2025-04-09",                                   # the news monster
    esm.iloc[len(esm) // 2].night,                  # median ESM5 night
    esh.iloc[-1].night,                             # biggest ESH5 night
    esh.iloc[len(esh) // 2].night,                  # median ESH5 night
})
print(f"study nights: {NIGHTS}")


def fetch_night(skey):
    """MBO events for the overnight session key skey: 21:00 (skey-1) -> 13:00 skey."""
    d1 = (pd.Timestamp(skey) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    sql = (f"SELECT ts_recv, action, side, price, size, order_id FROM mbo_events "
           f"WHERE ts_recv >= '{d1}T21:00:00.000000Z' AND ts_recv < '{skey}T13:00:00.000000Z' "
           f"AND is_trade=false ORDER BY ts_recv")
    with httpx.stream("GET", "http://localhost:9000/exp", params={"query": sql},
                      timeout=600.0) as r:
        r.raise_for_status()
        buf = io.StringIO("".join(r.iter_text()))
    df = pd.read_csv(buf)
    df.columns = [c.strip('"') for c in df.columns]
    return df


class Book:
    __slots__ = ("lvl", "best", "orders", "phantom", "clears", "sec_samples")

    def __init__(self):
        self.lvl = {"B": defaultdict(float), "A": defaultdict(float)}
        self.best = {"B": None, "A": None}
        self.orders = {}                      # id -> (side, price, size)
        self.phantom = 0
        self.clears = []                      # (sec, side, cause)
        self.sec_samples = {}                 # sec -> (bb, ba, szb, sza)

    def _better(self, side, p, cur):
        return cur is None or (p > cur if side == "B" else p < cur)

    def _rescan(self, side):
        cur = self.best[side]
        if cur is None:
            return
        step = -TICK if side == "B" else TICK
        p = cur
        for _ in range(4000):
            if self.lvl[side].get(p, 0) > 0:
                self.best[side] = p
                return
            p += step
        self.best[side] = None

    def add(self, side, p, sz):
        self.lvl[side][p] += sz
        if self._better(side, p, self.best[side]):
            self.best[side] = p

    def remove(self, side, p, sz, cause, sec):
        cur = self.lvl[side].get(p, 0)
        new = cur - sz
        if new < 0:
            self.phantom += 1
            new = 0
        self.lvl[side][p] = new
        if new <= 0 and self.best[side] == p:
            self.clears.append((sec, side, cause))
            self._rescan(side)

    def sample(self, sec):
        bb, ba = self.best["B"], self.best["A"]
        self.sec_samples[sec] = (
            bb, ba,
            self.lvl["B"].get(bb, 0) if bb is not None else 0,
            self.lvl["A"].get(ba, 0) if ba is not None else 0)


def replay(df):
    book = Book()
    if pd.api.types.is_numeric_dtype(df["ts_recv"]):
        t = pd.to_datetime(df["ts_recv"], unit="us", utc=True)   # QuestDB /exp = µs epoch
    else:
        t = pd.to_datetime(df["ts_recv"], utc=True)
    t = t.dt.as_unit("ns")        # pandas 3.0 parses as µs; pin ns (the adapter trap)
    print(f"    book span: {t.iloc[0]} .. {t.iloc[-1]}")
    ts = (t.astype("int64") // NS).to_numpy()
    act = df["action"].to_numpy()
    side = df["side"].to_numpy()
    px = df["price"].to_numpy()
    sz = df["size"].to_numpy()
    oid = df["order_id"].to_numpy()
    cur_sec = ts[0]
    skipped = 0
    for i in range(len(df)):
        s = ts[i]
        if s != cur_sec:
            book.sample(cur_sec)
            cur_sec = s
        if side[i] != "B" and side[i] != "A":     # side='N' rows (rare)
            skipped += 1
            continue
        a = act[i]
        if a == "A":
            book.add(side[i], px[i], sz[i])
            book.orders[oid[i]] = (side[i], px[i], sz[i])
        elif a == "C":
            book.remove(side[i], px[i], sz[i], "cancel", s)
            book.orders.pop(oid[i], None)
        elif a == "F":
            book.remove(side[i], px[i], sz[i], "fill", s)
            o = book.orders.get(oid[i])
            if o is not None:
                rem = o[2] - sz[i]
                if rem <= 0:
                    book.orders.pop(oid[i], None)
                else:
                    book.orders[oid[i]] = (o[0], o[1], rem)
        elif a == "M":
            o = book.orders.get(oid[i])
            if o is not None:
                book.remove(o[0], o[1], o[2], "modify", s)
            book.add(side[i], px[i], sz[i])
            book.orders[oid[i]] = (side[i], px[i], sz[i])
    book.sample(cur_sec)
    return book


# ── move starts (same zigzag as the catalog) on claude_sec_eth px ────────
def night_px(skey):
    d1 = (pd.Timestamp(skey) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    g = q.df(f"SELECT ts, pxc FROM claude_sec_eth WHERE "
             f"ts >= '{d1}T21:00:00.000000Z' AND ts < '{skey}T13:00:00.000000Z' ORDER BY ts")
    grid = pd.date_range(g.ts.min().floor("s"), g.ts.max().ceil("s"), freq="1s")
    px = g.set_index("ts")["pxc"].reindex(grid).ffill().bfill().to_numpy()
    secs = (grid.astype("int64") // NS).to_numpy()
    return secs, px


def zig_starts(secs, px):
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
    return [(secs[s], secs[e], d) for s, e, d in legs if e > s and abs(px[e] - px[s]) >= THETA]


def window_stats(book, t0, direction):
    """Receding-side clear counts by cause + depth trajectory around t0."""
    rec = "A" if direction > 0 else "B"
    pre = dict(fill=0, cancel=0, modify=0)
    dur = dict(fill=0, cancel=0, modify=0)
    for sec, sd, cause in book.clears:
        if sd != rec:
            continue
        if t0 - W_PRE <= sec < t0:
            pre[cause] += 1
        elif t0 <= sec < t0 + W_DUR:
            dur[cause] += 1
    idx = 2 if rec == "B" else 3
    traj = {}
    for off in (-300, -120, -60, -30, -10, 0, 30, 120):
        v = [book.sec_samples[t0 + off + k][idx] for k in range(-4, 5)
             if (t0 + off + k) in book.sec_samples]
        traj[off] = float(np.median(v)) if v else np.nan
    return pre, dur, traj


rng = np.random.default_rng(3)
rows = []
trajs_case, trajs_ctrl = [], []
for skey in NIGHTS:
    print(f"\n--- {skey}: fetching...", flush=True)
    df = fetch_night(skey)
    print(f"    {len(df):,} book events; replaying...", flush=True)
    book = replay(df)
    total_clears = len(book.clears)
    print(f"    clears of best level: {total_clears:,}   phantom removals: {book.phantom:,} "
          f"({book.phantom/max(1,len(df)):.1%} of events)")
    secs, px = night_px(skey)
    legs = [(s, e, d) for (s, e, d) in zig_starts(secs, px)
            if pd.Timestamp(s, unit="s").hour not in (21,)
            and s >= secs[0] + W_PRE + 120]      # skip bootstrap hour + warmup artifacts
    move_secs = set()
    for s, e, d in legs:
        move_secs.update(range(s - 600, e + 600))
    for s, e, d in legs:
        pre, dur, traj = window_stats(book, s, d)
        rows.append(dict(night=skey, kind="case", dir=d, pre=pre, dur=dur, traj=traj))
        trajs_case.append(traj)
    # controls: quiet seconds, random pseudo-direction
    quiet = [t for t in book.sec_samples if t not in move_secs
             and pd.Timestamp(t, unit="s").hour != 21]
    for t0 in rng.choice(quiet, size=min(len(legs) * 2, len(quiet)), replace=False):
        d = 1 if rng.random() < 0.5 else -1
        pre, dur, traj = window_stats(book, int(t0), d)
        rows.append(dict(night=skey, kind="ctrl", dir=d, pre=pre, dur=dur, traj=traj))
        trajs_ctrl.append(traj)

# ── aggregate ────────────────────────────────────────────────────────────
def agg(kind, phase):
    sel = [r[phase] for r in rows if r["kind"] == kind]
    f = np.array([x["fill"] for x in sel], float)
    c = np.array([x["cancel"] for x in sel], float)
    m = np.array([x["modify"] for x in sel], float)
    tot = f + c + m
    share_f = np.where(tot > 0, f / np.maximum(tot, 1), np.nan)
    return (len(sel), np.median(tot) / (W_PRE / 60), np.nanmedian(share_f),
            np.median(f), np.median(c), np.median(m))


print("\n===== RECEDING-SIDE BEST-LEVEL CLEARS (median per window) =====")
print(f"{'group':<14}{'n':>4} {'clears/min':>11} {'fill-share':>11} {'F':>5} {'C':>5} {'M':>5}")
for kind in ("case", "ctrl"):
    for phase, lbl in (("pre", "PRE -300..0"), ("dur", "DUR 0..+300")):
        n, cpm, fs, f, c, m = agg(kind, phase)
        print(f"{kind}-{lbl:<10}{n:>4} {cpm:>11.1f} {fs:>11.0%} {f:>5.0f} {c:>5.0f} {m:>5.0f}")

print("\n===== DEPTH AT BEST (receding side), median contracts, by offset from t0 =====")
offs = (-300, -120, -60, -30, -10, 0, 30, 120)
for lbl, T in (("cases", trajs_case), ("controls", trajs_ctrl)):
    line = "  ".join(f"{o:+d}s:{np.nanmedian([t[o] for t in T]):.0f}" for o in offs)
    print(f"{lbl:<9} {line}")

print("\n===== PER-NIGHT (confound check: within each night, cases vs controls) =====")
print(f"{'night':<12}{'kind':<6}{'n':>4} {'depth@0':>8} {'C-clears pre':>13} {'F-clears pre':>13}")
for skey in NIGHTS:
    for kind in ("case", "ctrl"):
        sel = [r for r in rows if r["night"] == skey and r["kind"] == kind]
        if not sel:
            continue
        d0 = np.nanmedian([r["traj"].get(0, np.nan) for r in sel])
        cpre = np.median([r["pre"]["cancel"] for r in sel])
        fpre = np.median([r["pre"]["fill"] for r in sel])
        print(f"{skey:<12}{kind:<6}{len(sel):>4} {d0:>8.0f} {cpre:>13.0f} {fpre:>13.0f}")
