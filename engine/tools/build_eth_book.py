"""Build claude_sec_eth_book: per-second BOOK aggregates (bid/ask adds+cancels)
for the hours OUTSIDE 13-21 UTC, both contracts, from raw mbo_events — the
missing half of the overnight tape (claude_sec_eth has trades only). Same
semantics as claude_sec_feat's book columns: action 'A'=add / 'C'=cancel,
side 'B'/'A', snapshots excluded. Chunked weekly INSERTs (proven pattern)."""
import sys

import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=300)

q.query("CREATE TABLE IF NOT EXISTS claude_sec_eth_book (symbol SYMBOL, ts TIMESTAMP, "
        "bid_add LONG, bid_cancel LONG, ask_add LONG, ask_cancel LONG) "
        "TIMESTAMP(ts) PARTITION BY DAY")

existing = int(q.df("SELECT count() n FROM claude_sec_eth_book")["n"].iloc[0])
if existing > 0:
    print(f"table already has {existing} rows; skipping build")
else:
    ranges = [("ESH5", "2025-02-18", "2025-03-20"), ("ESM5", "2025-03-20", "2025-05-31")]
    for sym, a, b in ranges:
        for start in pd.date_range(a, b, freq="7D"):
            end = min(start + pd.Timedelta(days=7), pd.Timestamp(b))
            if start >= end:
                continue
            s, e = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
            q.query(
                f"INSERT INTO claude_sec_eth_book "
                f"SELECT symbol, ts_recv ts, "
                f"sum(CASE WHEN action='A' AND side='B' THEN size ELSE 0 END) bid_add, "
                f"sum(CASE WHEN action='C' AND side='B' THEN size ELSE 0 END) bid_cancel, "
                f"sum(CASE WHEN action='A' AND side='A' THEN size ELSE 0 END) ask_add, "
                f"sum(CASE WHEN action='C' AND side='A' THEN size ELSE 0 END) ask_cancel "
                f"FROM mbo_events WHERE symbol='{sym}' AND is_trade=false AND is_snapshot=false "
                f"AND ts_recv >= '{s}T00:00:00.000000Z' AND ts_recv < '{e}T00:00:00.000000Z' "
                f"AND (hour(ts_recv) < 13 OR hour(ts_recv) >= 21) "
                f"SAMPLE BY 1s ALIGN TO CALENDAR")
            n = int(q.df(f"SELECT count() n FROM claude_sec_eth_book WHERE symbol='{sym}' "
                         f"AND ts >= '{s}T00:00:00.000000Z' AND ts < '{e}T00:00:00.000000Z'")["n"].iloc[0])
            print(f"{sym} {s}..{e}: {n} rows")

print(q.df("SELECT symbol, count() n, min(ts) a, max(ts) b FROM claude_sec_eth_book "
           "GROUP BY symbol").to_string(index=False))
