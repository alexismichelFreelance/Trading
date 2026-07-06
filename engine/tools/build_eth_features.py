#!/usr/bin/env python
"""Build claude_sec_eth: per-second trade aggregates (pxc, adelta, avol) for the
hours OUTSIDE the existing 13-21 UTC window, both contracts, from raw mbo_events.
Chunked weekly INSERTs with count verification (the pipeline's proven pattern)."""
import sys

import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=300)

q.query("CREATE TABLE IF NOT EXISTS claude_sec_eth (symbol SYMBOL, ts TIMESTAMP, "
        "pxc DOUBLE, adelta LONG, avol LONG) TIMESTAMP(ts) PARTITION BY DAY")

existing = q.df("SELECT count() n FROM claude_sec_eth")["n"].iloc[0]
if int(existing) > 0:
    print(f"table already has {existing} rows; skipping build")
else:
    ranges = [("ESH5", "2025-02-18", "2025-03-20"), ("ESM5", "2025-03-20", "2025-05-31")]
    for sym, a, b in ranges:
        for start in pd.date_range(a, b, freq="7D"):
            end = min(start + pd.Timedelta(days=7), pd.Timestamp(b))
            if start >= end:
                continue
            s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
            sql = (f"INSERT INTO claude_sec_eth "
                   f"SELECT symbol, ts_recv ts, last(price) pxc, "
                   f"sum(CASE WHEN side='B' THEN size ELSE 0 END) - "
                   f"sum(CASE WHEN side='A' THEN size ELSE 0 END) adelta, "
                   f"sum(size) avol FROM mbo_events "
                   f"WHERE symbol='{sym}' AND is_trade=true "
                   f"AND ts_recv >= '{s}T00:00:00.000000Z' AND ts_recv < '{e}T00:00:00.000000Z' "
                   f"AND (hour(ts_recv) < 13 OR hour(ts_recv) >= 21) "
                   f"SAMPLE BY 1s ALIGN TO CALENDAR")
            q.query(sql)
            n = q.df(f"SELECT count() n FROM claude_sec_eth WHERE symbol='{sym}' "
                     f"AND ts >= '{s}T00:00:00.000000Z' AND ts < '{e}T00:00:00.000000Z'")["n"].iloc[0]
            print(f"{sym} {s}..{e}: {int(n)} rows")

tot = q.df("SELECT symbol, count() n, min(ts) a, max(ts) b FROM claude_sec_eth GROUP BY symbol")
print(tot.to_string(index=False))
