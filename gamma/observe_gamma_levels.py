"""Observe how ES price interacted with the PRIOR session's gamma levels
(put wall / call wall / zero-gamma flip), shifted +52pt SPX->ES. Causal:
levels from day L are the standing structure for day L+1's RTH. Small sample
(6 day-pairs, all we have) — illustrative, not statistical."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, "D:/Trading/engine")
sys.path.insert(0, "D:/Trading/gamma")
from engine.adapters.questdb import QuestDB
from gamma_profile import load_profile

BASIS = 52.0
RAW = Path("D:/Trading/gamma/raw_cboe")
q = QuestDB()

B = q.df("SELECT ts,o,h,l,c FROM claude_bars_live ORDER BY ts")
et = B.ts.dt.tz_convert("America/New_York")
B["day"] = et.dt.strftime("%Y-%m-%d"); B["mod"] = et.dt.hour * 60 + et.dt.minute

profiles = {load_profile(f)["sess"]: load_profile(f) for f in sorted(RAW.glob("SPX_*.json.gz"))}
levdays = sorted(profiles)
tradedays = sorted(B.day.unique())


def next_trading(ld):
    for d in tradedays:
        if d > ld:
            return d
    return None


def react(bars, level, side):
    """side +1: level is support (approached from above). Return (touched, held).
    held = price was >=5pt above 'level' 30 min after first touch (bounce)."""
    lo = bars.l.to_numpy(); hi = bars.h.to_numpy(); c = bars.c.to_numpy()
    if side > 0:
        idx = np.where(lo <= level + 1.5)[0]
        if not len(idx):
            return False, None
        i = idx[0]
        j = min(i + 30, len(c) - 1)
        return True, (c[j] - level)
    else:
        idx = np.where(hi >= level - 1.5)[0]
        if not len(idx):
            return False, None
        i = idx[0]
        j = min(i + 30, len(c) - 1)
        return True, (c[j] - level)


print("causal: prior-day gamma levels (SPX+52 -> ES) vs next-day RTH")
print(f"{'tradeday':<11}{'net':>6}{'openVSflip':>11}{'putW':>7}{'callW':>7}"
      f"{'flip':>7} | {'RTH o/h/l/c':<26} interaction")
for ld in levdays:
    td = next_trading(ld)
    if td is None:
        continue
    p = profiles[ld]
    g = B[(B.day == td) & (B["mod"] >= 570) & (B["mod"] < 960)].sort_values("mod")
    if len(g) < 100:
        continue
    pw, cw = p["put_wall"] + BASIS, p["call_wall"] + BASIS
    flip = (p["flip"] + BASIS) if p["flip"] else None
    o, hi, lo, c = g.o.iloc[0], g.h.max(), g.l.min(), g.c.iloc[-1]
    net = "LONG" if p["total"] > 0 else "SHORT"
    ovf = ("n/a" if flip is None else "above" if o > flip else "below")
    notes = []
    t, r = react(g, pw, +1)
    if t:
        notes.append(f"putW {'HELD +%.0f' % r if r and r > 3 else 'broke %.0f' % (r or 0)}")
    t, r = react(g, cw, -1)
    if t:
        notes.append(f"callW {'rejected %.0f' % r if r and r < -3 else 'broke +%.0f' % (r or 0)}")
    if flip:
        cl = "closed above flip" if c > flip else "closed below flip"
        notes.append(cl)
    # pin: close within 6pt of a wall
    for nm, w in (("putW", pw), ("callW", cw)):
        if abs(c - w) <= 6:
            notes.append(f"PINNED to {nm}")
    fs = f"{flip:.0f}" if flip else "none"
    print(f"{td:<11}{net:>6}{ovf:>11}{pw:>7.0f}{cw:>7.0f}{fs:>7} | "
          f"{o:.0f}/{hi:.0f}/{lo:.0f}/{c:.0f}".ljust(26) + "  " + "; ".join(notes))
print(f"\nput wall / call wall are the dominant OI strikes; flip = short-gamma "
      f"below / long-gamma above (in ES terms, +{BASIS:.0f} basis)")
