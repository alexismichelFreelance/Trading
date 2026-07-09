"""Do OVERNIGHT touches of RTH-formed S/D zones bounce? (the 6th family — the
one matching how the user actually trades the night: off levels)

Chronology per contract-day: RTH 30m bars (13-21 UTC) update the validated
ZoneDetector + lifecycle book; then that evening's overnight session (21:00 ->
13:00 UTC, ETH 1s px) is scanned for FIRST touches of zones active at that
moment. Reaction metric = the original zones research: from the touch price,
does px move +3 (bounce, in the zone's direction) before -3, within 2h,
conservative. Baseline ~52%. Split VIRGIN (untested incl. RTH) vs any-active.
Zone state (touches/breaks) updates chronologically incl. overnight closes.
"""
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB
from engine.core.events import Bar
from engine.features.zones import ZoneBook, ZoneDetector

q = QuestDB(timeout=180)
NS = 1_000_000_000

# RTH 30m bars per symbol (validated zone source)
rth30 = {}
for sym in ("ESM5", "ESH5"):
    df = q.df(f"SELECT ts, first(o) o, max(h) h, min(l) l, last(c) c, sum(vol) v "
              f"FROM claude_bars_1m WHERE symbol='{sym}' SAMPLE BY 30m ALIGN TO CALENDAR"
              ).dropna(subset=["c"])
    df["day"] = df["ts"].dt.strftime("%Y-%m-%d")
    rth30[sym] = df

# ETH 1s px, sessions keyed by the MORNING date (21:00 D-1 -> 13:00 D)
rows = []
for d in q.df("SELECT DISTINCT to_str(ts,'yyyy-MM-dd') d FROM claude_sec_eth ORDER BY d")["d"]:
    rows.append(q.df(f"SELECT ts, pxc FROM claude_sec_eth "
                     f"WHERE ts >= '{d}T00:00:00.000000Z' AND ts < '{d}T23:59:59.999999Z' ORDER BY ts"))
E = pd.concat(rows, ignore_index=True)
E["skey"] = np.where(E.ts.dt.hour >= 21,
                     (E.ts + pd.Timedelta(hours=13)).dt.strftime("%Y-%m-%d"),
                     E.ts.dt.strftime("%Y-%m-%d"))
eth = {}
for k, g in E.groupby("skey"):
    g = g.sort_values("ts")
    grid = pd.date_range(g.ts.min().floor("s"), g.ts.max().ceil("s"), freq="1s")
    eth[k] = g.set_index("ts")["pxc"].reindex(grid).ffill().bfill().to_numpy()


def race(px, i0, d, tp=3.0, horizon=7200):
    """+1 bounce (d-direction +3 first), 0 otherwise (conservative)."""
    e = px[i0]
    for j in range(i0 + 1, min(i0 + horizon, len(px))):
        fav = (px[j] - e) * d
        if fav <= -tp:
            return 0
        if fav >= tp:
            return 1
    return 0


res = []                      # (virgin, bounce, month)
for sym in ("ESM5", "ESH5"):
    det = ZoneDetector(gap_thr=0)          # validated base-only detector
    book = ZoneBook()
    days = sorted(rth30[sym]["day"].unique())
    for day in days:
        # 1) RTH of `day`: form/update zones chronologically
        for r in rth30[sym][rth30[sym]["day"] == day].itertuples():
            b = Bar(int(r.ts.value) + 1800 * NS, "30m", r.o, r.h, r.l, r.c, int(r.v))
            for z in det.update(b):
                book.add(z)
            book.on_bar(b)
            book.on_price(b.c, b.ts)
        # 2) the following overnight (morning key = next calendar day with data)
        nxt = [k for k in eth if k > day]
        if not nxt:
            continue
        key = min(nxt)
        px = eth[key]
        month = key[:7]
        inside_prev = {}
        for i in range(0, len(px), 1):
            p = px[i]
            for z in book.zones:
                if z.broken:
                    continue
                zid = id(z)
                inside = z.bot - 0.5 <= p <= z.top + 0.5
                if inside and not inside_prev.get(zid, False):
                    # first entry into the zone band this pass
                    if z.touches == 0:
                        res.append((True, race(px, i, z.direction), month))
                    elif z.touches <= 1:
                        res.append((False, race(px, i, z.direction), month))
                    z.touches += 1
                inside_prev[zid] = inside
            # overnight 30m closes drive breaks too (close fully through kills)
            if i % 1800 == 1799:
                book.on_bar(Bar(0, "30m", p, p, p, p, 0))

print(f"overnight zone touches: {len(res)}")
for lbl, sel in (("VIRGIN first-touch", [r for r in res if r[0]]),
                 ("2nd touch", [r for r in res if not r[0]]),
                 ("ALL", res)):
    if not sel:
        print(f"  {lbl:<20} n=0")
        continue
    v = np.array([r[1] for r in sel])
    bym = defaultdict(list)
    for _, b, m in sel:
        bym[m].append(b)
    pm = "  ".join(f"{m[-2:]}:{np.mean(x):.0%}({len(x)})" for m, x in sorted(bym.items()))
    print(f"  {lbl:<20} n={len(v):3d}  bounce={v.mean():.0%}  (baseline ~52%)  | {pm}")
