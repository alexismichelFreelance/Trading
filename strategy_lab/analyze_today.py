"""Decode the user's 2026-07-14 manual ES trades against their stated method:
short VWAP+2/3sigma at confluence (gamma flip/wall, zone, pivot), cover at VWAP;
long at VWAP, cover at next resistance. Map each flat-to-flat episode's entry and
exit to VWAP-sigma and the nearest structural level. Set the 20%-of-range bar."""
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

SP = ("C:/Users/alexi/AppData/Local/Temp/claude/D--Trading/"
      "74634737-f114-4630-9ca7-feccef7ea3b7/scratchpad")
TICK0 = 621355968000000000
BASIS = 52.0
q = QuestDB()

# ── today's 1m bars -> session VWAP + sigma per minute ─────────────────────
B = q.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live "
         "WHERE ts>='2026-07-14T13:30:00Z' AND ts<'2026-07-14T20:00:00Z' ORDER BY ts")
et = B.ts.dt.tz_convert("America/New_York")
B["min"] = et.dt.strftime("%H:%M")
v = B.vol.to_numpy().astype(float); c = B.c.to_numpy()
cv = np.cumsum(v); vwap = np.cumsum(c * v) / np.maximum(cv, 1)
sd = np.sqrt(np.cumsum(v * (c - vwap) ** 2) / np.maximum(cv, 1))
vwap_by_min = dict(zip(B["min"], vwap)); sd_by_min = dict(zip(B["min"], sd))
rng = B.h.max() - B.l.min()

# ── levels: gamma (prior +52), daily & weekly pivots ───────────────────────
gl = q.df("SELECT put_wall,call_wall,zero_gamma FROM claude_gex_levels ORDER BY ts DESC LIMIT 1")
gamma = {"putWall": gl.put_wall.iloc[0] + BASIS, "callWall": gl.call_wall.iloc[0] + BASIS}
if pd.notna(gl.zero_gamma.iloc[0]):
    gamma["flip"] = gl.zero_gamma.iloc[0] + BASIS
r13 = q.df("SELECT h,l,c FROM claude_bars_live WHERE ts>='2026-07-13T13:30:00Z' "
           "AND ts<'2026-07-13T20:00:00Z' ORDER BY ts")
ph, pl, pc = r13.h.max(), r13.l.min(), r13.c.iloc[-1]; P = (ph + pl + pc) / 3
piv = {"P": P, "R1": 2 * P - pl, "S1": 2 * P - ph, "R2": P + (ph - pl), "S2": P - (ph - pl)}
wk = q.df("SELECT h,l,c FROM claude_bars_live WHERE ts>='2026-07-06T13:30:00Z' "
          "AND ts<'2026-07-11T20:00:00Z' ORDER BY ts")
wh, wl, wc = wk.h.max(), wk.l.min(), wk.c.iloc[-1]; WP = (wh + wl + wc) / 3
piv.update({"wP": WP, "wR1": 2 * WP - wl, "wS1": 2 * WP - wh})
LEVELS = {**gamma, **piv, "pdC": pc}


def nearest(px):
    k = min(LEVELS, key=lambda k: abs(LEVELS[k] - px))
    return f"{k}({LEVELS[k]:.0f},{px-LEVELS[k]:+.0f})" if abs(LEVELS[k] - px) <= 6 else "-"


# ── today's manual ES fills -> flat-to-flat episodes ───────────────────────
con = sqlite3.connect(SP + "/td.sqlite")
E = pd.read_sql_query(
    "SELECT e.Time t,e.MarketPosition mp,e.Quantity q,e.Price px,e.Name n,mi.Name sym "
    "FROM Executions e JOIN Accounts a ON e.Account=a.Id JOIN Instruments i ON e.Instrument=i.Id "
    "JOIN MasterInstruments mi ON i.MasterInstrument=mi.Id "
    "WHERE a.Name='Sim101' AND mi.Name='ES'", con)
E["dt"] = pd.to_datetime((E.t - TICK0) * 100, unit="ns", utc=True).dt.tz_convert("America/New_York")
E = E[(E.dt.dt.strftime("%Y-%m-%d") == "2026-07-14") & (~E.n.str.fullmatch(r"O\d+", na=False))].sort_values("t")
E["sq"] = np.where(E.mp == 0, E.q, -E.q)
E["min"] = E.dt.dt.strftime("%H:%M")

def sig_of(mn, px):
    vw = vwap_by_min.get(mn, np.nan); s = sd_by_min.get(mn, 1)
    return (px - vw) / s if s and s > 0 else 0.0


E["sigma"] = [sig_of(m, p) for m, p in zip(E["min"], E.px)]
sells = E[E.sq < 0]; buys = E[E.sq > 0]
avg_sell = (sells.px * -sells.sq).sum() / -sells.sq.sum()
avg_buy = (buys.px * buys.sq).sum() / buys.sq.sum()
print("=== 2026-07-14: your manual ES method, decoded ===")
print(f"RTH range {rng:.1f}pt ({B.l.min():.1f}-{B.h.max():.1f})  ->  20% engine bar = "
      f"{0.2*rng:.1f}pt/day (1 lot)")
print(f"you: {len(E)} fills, net still {int(E.sq.sum()):+d} (holding), "
      f"realized {957.0:.0f} pt-contracts (~${957*50:,.0f})")
print()
print("WHERE you SOLD (fades) vs BOUGHT (covers/longs), by VWAP-sigma:")
print(f"  SELLS: {len(sells)} clips, avg {avg_sell:.1f} (sigma {sig_of(sells['min'].iloc[len(sells)//2], avg_sell):+.1f}), "
      f"range {sells.px.min():.1f}-{sells.px.max():.1f}  top: {nearest(sells.px.max())}")
print(f"  BUYS : {len(buys)} clips, avg {avg_buy:.1f}, "
      f"range {buys.px.min():.1f}-{buys.px.max():.1f}  low: {nearest(buys.px.min())}")
print()
# sigma buckets: confirm you fade the +2/3 sigma extremes and cover near VWAP
print("your fills bucketed by VWAP-sigma at fill time:")
for lo, hi, lbl in [(2, 9, ">= +2sigma"), (1, 2, "+1..+2"), (-1, 1, "near VWAP"),
                    (-2, -1, "-1..-2"), (-9, -2, "<= -2sigma")]:
    seg = E[(E.sigma >= lo) & (E.sigma < hi)]
    if len(seg):
        s = int(seg[seg.sq < 0].q.sum()); b = int(seg[seg.sq > 0].q.sum())
        print(f"  {lbl:11s}: {len(seg):3d} fills   SELL {s:4d} / BUY {b:4d} contracts")
print()
# the day's extremes vs levels (where the fade/target levels were)
print(f"day HIGH {B.h.max():.1f} -> nearest {nearest(B.h.max())} ; "
      f"day LOW {B.l.min():.1f} -> nearest {nearest(B.l.min())}")
print(f"final VWAP {vwap[-1]:.1f}  +2s {vwap[-1]+2*sd[-1]:.1f}  +3s {vwap[-1]+3*sd[-1]:.1f}  "
      f"-2s {vwap[-1]-2*sd[-1]:.1f}")
print("levels in play today (ES terms):")
for k in ("flip", "callWall", "putWall", "R1", "P", "S1", "wR1", "wP", "pdC"):
    if k in LEVELS:
        print(f"  {k:9s} {LEVELS[k]:.1f}")
