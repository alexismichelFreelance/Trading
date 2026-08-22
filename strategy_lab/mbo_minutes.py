"""Reduce 1.13bn MBO rows to per-minute trade statistics, once, to a CSV.

The exhaustion work was stuck on 10 sessions of live per-second data. This gives
86 sessions (2025-02-18 -> 2025-05-30, ESH5 + ESM5) of full order-by-order CME
data that was purchased for exactly this, and which is completely independent of
the 2026 live record -- so it is a real out-of-sample test, not more of the same
sample.

SIDE CONVENTION, VALIDATED NOT ASSUMED. On this feed a trade's `side` is the
side that was RESTING, so side='B' is an aggressive BUY. Checked over 7,735
minute buckets across 7 sessions: side='B'==buy agrees with the minute's price
direction 75% of the time overall and 85% when the imbalance exceeds 15%. The
opposite reading agrees 25%/15%, so this is not ambiguous.

Aggregation happens SERVER-SIDE (SAMPLE BY 1m) -- pulling 44M trade rows to
pandas would be pointless when QuestDB can bucket them in place.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\Trading\engine")
import pandas as pd
from engine.adapters.questdb import QuestDB

OUT = Path(r"D:\Trading\strategy_lab\mbo_minutes.csv")
CHUNKS = [("2025-02-18", "2025-03-05"), ("2025-03-05", "2025-03-20"),
          ("2025-03-20", "2025-04-05"), ("2025-04-05", "2025-04-20"),
          ("2025-04-20", "2025-05-05"), ("2025-05-05", "2025-05-20"),
          ("2025-05-20", "2025-05-31")]

Q = ("select ts_recv, symbol, last(price) px, max(price) hi, min(price) lo, "
     "sum(size) vol, "
     "sum(case when side='B' then size else 0 end) buy_v, "
     "sum(case when side='A' then size else 0 end) sell_v, "
     "sum(case when side='B' then 1 else 0 end) buy_n, "
     "sum(case when side='A' then 1 else 0 end) sell_n "
     "from mbo_events where action='T' "
     "and ts_recv >= '{a}' and ts_recv < '{b}' "
     "sample by 1m")


def main():
    q = QuestDB(timeout=3600)
    parts = []
    for a, b in CHUNKS:
        t0 = time.time()
        try:
            d = q.df(Q.format(a=a, b=b))
        except Exception as ex:                       # noqa: BLE001
            print("  %s -> %s  FAILED: %s" % (a, b, str(ex)[:110]), flush=True)
            continue
        print("  %s -> %s  %7d rows  %5.0fs"
              % (a, b, len(d), time.time() - t0), flush=True)
        if len(d):
            parts.append(d)
    if not parts:
        print("nothing extracted")
        return
    m = pd.concat(parts, ignore_index=True)
    m = m[m.vol > 0].copy()
    m["et"] = pd.to_datetime(m["ts_recv"]).dt.tz_convert("America/New_York")
    m["day"] = m.et.dt.strftime("%Y-%m-%d")
    m["mod"] = m.et.dt.hour * 60 + m.et.dt.minute
    # front month only: ESH5 through its expiry, ESM5 after
    m = m[~((m.symbol == "ESH5") & (m.day >= "2025-03-20"))]
    m = m[~((m.symbol == "ESM5") & (m.day < "2025-03-20"))]
    m.to_csv(OUT, index=False)
    print("\n%d minute rows, %d sessions, %s -> %s"
          % (len(m), m.day.nunique(), m.day.min(), m.day.max()))
    print("-> " + str(OUT))


if __name__ == "__main__":
    main()
