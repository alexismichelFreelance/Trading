"""Preview: run DipBuyStrategy over the 2026 recorded bars with a simple bar
fill model and show the round-trip trades (entry -> scale/stop/vwap/moc) with
approximate points. Lets the user SEE the sleeve trade before a live session.
Approximate fills: entry at the touched level, scale at +4, stop at the stop,
vwap-runner at the entry-VWAP, moc at bar close. Cost 0.517 pt/round-turn/contract.
"""
import sys

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB
from engine.core.events import Bar
from engine.core.timeutil import et_minute_of_day, et_session_date
from engine.strategies.dip_buy import DipBuyStrategy

COST = 0.517
q = QuestDB()
B = q.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live ORDER BY ts")

s = DipBuyStrategy("ES")
pos = 0
cur = None                # active round trip
trades = []
for r in B.itertuples():
    ts = int(r.ts.value)
    bar = Bar(ts, "1m", r.o, r.h, r.l, r.c, int(r.vol))
    orders = s.on_bar(bar)
    for o in orders:
        if "entry" in o.tag:
            t = s.trade
            cur = dict(day=et_session_date(ts), emin=et_minute_of_day(ts), dir=o.side,
                       entry=t.entry, runner=t.runner_tgt, size=o.qty, scaled=False,
                       stop0=t.stop, pts=0.0, legs=[])
        else:                                     # an exit leg
            d = cur["dir"]; e = cur["entry"]
            if o.tag == "dip-scale":
                px = e + d * 4.0; cur["scaled"] = True
            elif o.tag == "dip-vwap":
                px = cur["runner"]
            elif o.tag == "dip-stop":
                px = e if cur["scaled"] else cur["stop0"]
            else:                                 # dip-moc
                px = r.c
            cur["pts"] += d * (px - e) * o.qty
            cur["legs"].append(o.tag.replace("dip-", ""))
        pos += o.side * o.qty
    s.on_position(type("P", (), {"qty": pos})())
    if cur is not None and pos == 0 and cur["legs"]:
        cur["net"] = cur["pts"] - COST * cur["size"]
        trades.append(cur); cur = None

print(f"DipBuy preview over {B.ts.dt.strftime('%Y-%m-%d').nunique()} recorded days: "
      f"{len(trades)} round trips")
print(f"{'day':<12}{'entry ET':>9}{'dir':>4}{'entry':>9}{'exit legs':>16}{'net pt':>8}")
tot = 0.0
for t in trades:
    tot += t["net"]
    print(f"{t['day']:<12}{t['emin']//60:>6}:{t['emin']%60:02d}{'L' if t['dir']>0 else 'S':>4}"
          f"{t['entry']:>9.2f}{'+'.join(t['legs']):>16}{t['net']:>8.1f}")
wins = sum(1 for t in trades if t["net"] > 0)
print(f"\ntotal {tot:+.1f} pt  ({tot*50:+,.0f} at $50/pt, 1 lot)  win {wins}/{len(trades)}")
print("NB approximate sim on the delayed recorder feed; illustration, not validation.")
