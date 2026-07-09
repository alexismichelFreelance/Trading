"""Overnight (ETH) tradeability — test DIFFERENT strategy families, not just flow.

User thesis: thin book + Asia-open volume => clean, tradeable moves (30+ pt
night ranges, traded manually). Prior negative result only covered wall-clock
flow. Here:
  S. structure: night range distribution, hourly |move| & volume, autocorr
  A. VOLUME-TIME flow: event bars of V contracts; flow on bar deltas with
     STRICTLY-PRIOR adaptive z-threshold in bar space (clock adapts to activity)
  B. Asia-open range breakout: 21:00->form_end UTC range, first close beyond
     +-0.5 -> enter, stop = other side (capped), trail 0.75*range (floor 8),
     flat 13:00. One trade/night.
  C. slow momentum: hourly, pos = sign(trailing 2h move) with 3pt deadband.
Robustness battery everywhere: cost 0.30 AND 0.60, per-month signs, win-night %,
top-5 nights % of total. 72 nights, both contracts (claude_sec_eth).
"""
import sys
from collections import defaultdict, deque

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=180)

rows = []
for d in q.df("SELECT DISTINCT to_str(ts,'yyyy-MM-dd') d FROM claude_sec_eth ORDER BY d")["d"]:
    rows.append(q.df(f"SELECT ts, pxc, adelta, avol FROM claude_sec_eth "
                     f"WHERE ts >= '{d}T00:00:00.000000Z' AND ts < '{d}T23:59:59.999999Z' ORDER BY ts"))
E = pd.concat(rows, ignore_index=True)
E["sess"] = np.where(E.ts.dt.hour >= 21,
                     (E.ts + pd.Timedelta(hours=13)).dt.strftime("%Y-%m-%d"),
                     E.ts.dt.strftime("%Y-%m-%d"))

sessions = {}
for k, g in E.groupby("sess"):
    g = g.sort_values("ts")
    if (g.ts.max() - g.ts.min()) < pd.Timedelta(hours=8):
        continue
    grid = pd.date_range(g.ts.min().floor("s"), g.ts.max().ceil("s"), freq="1s")
    px = g.set_index("ts")["pxc"].reindex(grid).ffill().bfill().to_numpy()
    ad = g.set_index("ts")["adelta"].reindex(grid, fill_value=0).to_numpy()
    vol = g.set_index("ts")["avol"].reindex(grid, fill_value=0).to_numpy()
    hours = grid.hour.to_numpy()
    sessions[k] = dict(px=px, ad=ad, vol=vol, hr=hours, grid=grid)
print(f"nights: {len(sessions)}")


def battery(name, nightly_net):
    for cost_lbl, net in nightly_net.items():
        v = np.array(sorted(net.values())[::-1])
        tot = v.sum()
        bym = defaultdict(float)
        for kk, x in net.items():
            bym[kk[:7]] += x
        mplus = sum(1 for x in bym.values() if x > 0)
        conc = 100 * v[:5].sum() / tot if tot > 0 else float("inf")
        pm = " ".join(f"{m[-2:]}:{x:+.0f}" for m, x in sorted(bym.items()))
        ntr = getattr(battery, "_ntr", "")
        print(f"  {name:<26} {cost_lbl}: net={tot:+7.0f} win_n={100*(np.array(list(net.values()))>0).mean():3.0f}% "
              f"top5={conc:5.0f}% mo+={mplus}/4 | {pm}")


# ── S. structure ─────────────────────────────────────────────────────────
ranges = {k: s["px"].max() - s["px"].min() for k, s in sessions.items()}
rv = np.array(list(ranges.values()))
print(f"\nS. night range: median {np.median(rv):.1f}  p25 {np.percentile(rv,25):.1f}  "
      f"p75 {np.percentile(rv,75):.1f}  max {rv.max():.1f}  |  >=30pt: {(rv>=30).mean():.0%} of nights")
hr_mov = defaultdict(list)
hr_vol = defaultdict(list)
for s in sessions.values():
    pxs = pd.Series(s["px"])
    for h in np.unique(s["hr"]):
        m = s["hr"] == h
        if m.sum() > 600:
            hr_mov[h].append(pxs[m].max() - pxs[m].min())
            hr_vol[h].append(s["vol"][m].sum())
order_h = [21, 22, 23, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
print("   hourly avg range(pt)/volume(k): " + " ".join(
    f"{h:02d}:{np.mean(hr_mov[h]):.0f}/{np.mean(hr_vol[h])/1000:.0f}" for h in order_h if h in hr_mov))
rets = []
for s in sessions.values():
    p30 = pd.Series(s["px"]).iloc[::1800].to_numpy()
    rets += list(np.diff(p30))
rets = np.array(rets)
ac1 = np.corrcoef(rets[:-1], rets[1:])[0, 1]
print(f"   30-min return lag-1 autocorr (pooled): {ac1:+.3f}  (positive => trends persist)")

# ── A. volume-time flow ──────────────────────────────────────────────────
def vt_flow(V=300, Wb=18, k=4.0, maxp=5):
    out = {"c0.30": {}, "c0.60": {}}
    for key, s in sessions.items():
        px, ad, vol = s["px"], s["ad"], s["vol"]
        # build volume bars
        bars = []
        acc_v = 0.0
        acc_d = 0.0
        for i in range(len(px)):
            acc_v += vol[i]
            acc_d += ad[i]
            if acc_v >= V:
                bars.append((acc_d, px[i]))
                acc_v = 0.0
                acc_d = 0.0
        if len(bars) < 60:
            for c in out:
                out[c][key] = 0.0
            continue
        deltas = np.array([b[0] for b in bars])
        closes = np.array([b[1] for b in bars])
        absd = pd.Series(np.abs(deltas))
        th = (absd.rolling(100, min_periods=30).mean()
              + k * absd.rolling(100, min_periods=30).std()).shift(1).to_numpy()
        F = 0.0
        buf = deque()
        held = 0
        pnl = 0.0
        turn = 0.0
        for i in range(len(bars)):
            t = th[i]
            x = deltas[i] if (not np.isnan(t) and abs(deltas[i]) >= t) else 0
            buf.append(x)
            F += x
            if len(buf) > Wb:
                F -= buf.popleft()
            sc = 15.0 * t if (not np.isnan(t) and t > 0) else np.nan
            tgt = 0.0 if np.isnan(sc) else max(-maxp, min(maxp, F / sc))
            delta = tgt - held
            band = 1 if held == 0 else (1 if np.sign(delta) == np.sign(held) else 5)
            if abs(delta) > band:
                nv = int(np.floor(tgt + 0.5))
                turn += abs(nv - held)
                held = nv
            if i < len(bars) - 1:
                pnl += held * (closes[i + 1] - closes[i])
        out["c0.30"][key] = pnl - turn * 0.30
        out["c0.60"][key] = pnl - turn * 0.60
    return out


# ── B. Asia-open range breakout ──────────────────────────────────────────
def breakout(form_end=0, trail_mult=0.75, trail_floor=8.0):
    out = {"c0.60": {}, "c0.30": {}}
    for key, s in sessions.items():
        px, hr = s["px"], s["hr"]
        if form_end >= 21:                      # same-evening end: 21:00 -> form_end
            form = (hr >= 21) & (hr < form_end)
        elif form_end > 0:                      # wraps midnight: 21:00 -> form_end
            form = (hr >= 21) | (hr < form_end)
        else:                                   # form_end == 0 -> 21:00 -> 00:00
            form = hr >= 21
        after = ~form & (hr < 13)
        fi = np.where(form)[0]
        ai = np.where(after)[0]
        pnl = 0.0
        if len(fi) > 1800 and len(ai) > 1800:
            hi, lo = px[fi].max(), px[fi].min()
            rng = hi - lo
            side = 0
            entry = stop = 0.0
            peak = 0.0
            for i in ai:
                p = px[i]
                if side == 0:
                    if p > hi + 0.5:
                        side, entry = 1, p
                        stop = max(lo, entry - rng)
                        peak = p
                    elif p < lo - 0.5:
                        side, entry = -1, p
                        stop = min(hi, entry + rng)
                        peak = p
                else:
                    peak = max(peak, p) if side > 0 else min(peak, p)
                    trail = max(trail_floor, trail_mult * rng)
                    tstop = peak - side * trail
                    eff = max(stop, tstop) if side > 0 else min(stop, tstop)
                    if (side > 0 and p <= eff) or (side < 0 and p >= eff):
                        pnl = side * (eff - entry)
                        side = 0
                        break
            if side != 0:
                pnl = side * (px[ai[-1]] - entry)
        out["c0.60"][key] = pnl - (0.60 if pnl != 0.0 else 0.0)
        out["c0.30"][key] = pnl - (0.30 if pnl != 0.0 else 0.0)
    return out


# ── C. slow momentum ─────────────────────────────────────────────────────
def slow_mom(lookback_s=7200, dead=3.0):
    out = {"c0.60": {}, "c0.30": {}}
    for key, s in sessions.items():
        px = s["px"]
        pnl = 0.0
        turn = 0.0
        held = 0
        for i in range(lookback_s, len(px) - 3600, 3600):
            mv = px[i] - px[i - lookback_s]
            tgt = 1 if mv > dead else (-1 if mv < -dead else 0)
            if tgt != held:
                turn += abs(tgt - held)
                held = tgt
            pnl += held * (px[min(i + 3600, len(px) - 1)] - px[i])
        out["c0.60"][key] = pnl - turn * 0.60
        out["c0.30"][key] = pnl - turn * 0.30
    return out


print("\nA. volume-time flow (V=300, Wb=18, strictly-prior z):")
for k in (3.0, 4.0):
    r = vt_flow(k=k)
    battery(f"vt-flow k={k}", r)
print("\nB. Asia-open range breakout:")
for fe, lbl in ((0, "form 21-00 UTC"), (23, "form 21-23 UTC")):
    r = breakout(form_end=fe)
    battery(f"breakout {lbl}", r)
print("\nC. slow momentum (2h lookback, 3pt deadband, hourly):")
battery("slow-mom 2h", slow_mom())
