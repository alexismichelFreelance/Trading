"""Anchor test: run DipBuyStrategy over the 2026 recorded 1m bars (ungated) and
confirm it catches the user's 2026-07-08 (~7492) and 07-10 (~7586) long entries.
A sleeve that mechanizes the user's edge MUST fire near those fills."""
import sys

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB
from engine.core.events import Bar
from engine.core.timeutil import et_minute_of_day, et_session_date
from engine.strategies.dip_buy import DipBuyStrategy

q = QuestDB()
B = q.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live ORDER BY ts")

s = DipBuyStrategy("ES")
entries = []
pos = 0
for r in B.itertuples():
    ts = int(r.ts.value)
    bar = Bar(ts, "1m", r.o, r.h, r.l, r.c, int(r.vol))
    orders = s.on_bar(bar)
    for o in orders:
        if "entry" in o.tag:
            entries.append((et_session_date(ts), et_minute_of_day(ts), o.side,
                            round(s.trade.entry, 2) if s.trade else None, o.tag))
        pos += o.side * o.qty                      # simulate fill
    s.on_position(type("P", (), {"qty": pos})())   # engine feeds owner's book

print(f"{len(entries)} entries over {B.ts.dt.strftime('%Y-%m-%d').nunique()} recorded days")
for d, m, side, px, tag in entries:
    star = ""
    if d == "2026-07-08" and side > 0 and px and abs(px - 7492) < 20:
        star = "  <-- ANCHOR 07-08 (user 7492.5)"
    if d == "2026-07-10" and side > 0 and px and abs(px - 7586) < 20:
        star = "  <-- ANCHOR 07-10 (user 7586.9)"
    print(f"  {d} {m//60:02d}:{m%60:02d} {'L' if side>0 else 'S'} @{px} [{tag}]{star}")

a8 = any(d == "2026-07-08" and side > 0 and px and abs(px - 7492) < 20
         for d, m, side, px, tag in entries)
a10 = any(d == "2026-07-10" and side > 0 and px and abs(px - 7586) < 25
          for d, m, side, px, tag in entries)
print(f"\nANCHOR 07-08 caught: {a8}   ANCHOR 07-10 caught: {a10}")
