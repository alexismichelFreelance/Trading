"""
Ingest ESH5/ESM5 MBO data into QuestDB
=======================================
Filters to front-month outright only, skips SOD snapshots.
Handles the H5->M5 roll around March 20, 2025.

Before running:
1. Make sure QuestDB service is running
2. Open http://localhost:9000 and run the CREATE TABLE statement printed below
3. Run: python ingest_to_questdb.py

Requires: pip install databento questdb
"""

import os
import glob
import time
import databento as db
import datetime
from questdb.ingress import Sender, TimestampNanos

# ── Config ───────────────────────────────────────────────────────────────────
DATA_DIR     = r"D:\Data"
QUESTDB_HOST = "localhost"
QUESTDB_PORT = 9009
BATCH_SIZE   = 20_000
PROGRESS_FILE = r"D:\Data\ingest_progress.txt"
# ─────────────────────────────────────────────────────────────────────────────

ROLL_DATE = "20250320"   # ESM5 becomes front month on/after this date
SOD_FLAG  = 0x20         # flags bit 5 — start-of-day snapshot record

DDL = """
CREATE TABLE IF NOT EXISTS mbo_events (
    ts_recv       TIMESTAMP,
    ts_event_ns   LONG,
    symbol        SYMBOL,
    action        SYMBOL,
    side          SYMBOL,
    price         DOUBLE,
    size          LONG,
    order_id      LONG,
    flags         INT,
    sequence      LONG,
    is_trade      BOOLEAN,
    is_snapshot   BOOLEAN
) TIMESTAMP(ts_recv) PARTITION BY DAY WAL;
"""


def find_files(data_dir):
    files = []
    for p in [os.path.join(data_dir, "*.dbn.zst"),
              os.path.join(data_dir, "**", "*.dbn.zst")]:
        files.extend(glob.glob(p, recursive=True))
    return sorted(set(files))


def front_month_for_file(filename):
    base      = os.path.basename(filename)   # glbx-mdp3-20250318.mbo.dbn.zst
    date_part = base.split("-")[2][:8]       # 20250318
    return "ESM5" if date_part >= ROLL_DATE else "ESH5"


def ts_to_nanos(ts):
    try:
        return int(ts.value)
    except AttributeError:
        return int(ts)


def ingest_file(path, sender):
    front_month = front_month_for_file(path)
    store       = db.DBNStore.from_file(path)

    rows_sent   = 0
    rows_skip   = 0
    trades_seen = 0
    t0          = time.time()

    for df in store.to_df(count=BATCH_SIZE):
        if df.index.name in ("ts_recv", "ts_event"):
            df = df.reset_index()

        # Filter to front month outright only
        if "symbol" in df.columns:
            df = df[df["symbol"] == front_month]

        if len(df) == 0:
            continue

        # Filter out SOD snapshots
        is_snapshot = (df["flags"].astype(int) & SOD_FLAG) > 0
        rows_skip  += int(is_snapshot.sum())
        df          = df[~is_snapshot].copy()

        if len(df) == 0:
            continue

        # Compute derived columns vectorised
        df["is_trade"]    = df["action"].astype(str) == "T"
        df["is_snapshot"] = False
        trades_seen      += int(df["is_trade"].sum())

        # Convert timestamps to int64 nanoseconds
        df["ts_recv_ns"]  = df["ts_recv"].astype("int64")
        df["ts_event_ns"] = df["ts_event"].astype("int64")

        # NaN-safe price
        df["price"] = df["price"].fillna(0.0)

        flush_batch_vectorised(sender, df)
        rows_sent += len(df)

    elapsed = time.time() - t0
    rate    = rows_sent / elapsed if elapsed > 0 else 0
    print(f"    symbol      : {front_month}")
    print(f"    rows sent   : {rows_sent:>10,}  ({rate:>8,.0f} rows/sec)")
    print(f"    trades      : {trades_seen:>10,}")
    print(f"    skipped(SOD): {rows_skip:>10,}")
    return rows_sent, trades_seen


def flush_batch_vectorised(sender, df):
    # Build ILP rows from DataFrame columns directly
    # Still a Python loop but with pre-vectorised data — much faster
    # than extracting from row objects via itertuples
    ts_recv_arr    = df["ts_recv_ns"].to_numpy()
    ts_event_arr   = df["ts_event_ns"].to_numpy()
    symbol_arr     = df["symbol"].to_numpy()
    action_arr     = df["action"].to_numpy()
    side_arr       = df["side"].to_numpy()
    price_arr      = df["price"].to_numpy()
    size_arr       = df["size"].to_numpy()
    order_id_arr   = df["order_id"].to_numpy()
    flags_arr      = df["flags"].to_numpy()
    sequence_arr   = df["sequence"].to_numpy()
    is_trade_arr   = df["is_trade"].to_numpy()

    for i in range(len(df)):
        sender.row(
            "mbo_events",
            symbols={
                "symbol": str(symbol_arr[i]),
                "action": str(action_arr[i]),
                "side":   str(side_arr[i]),
            },
            columns={
                "ts_event_ns": int(ts_event_arr[i]),
                "price":       float(price_arr[i]),
                "size":        int(size_arr[i]),
                "order_id":    int(order_id_arr[i]),
                "flags":       int(flags_arr[i]),
                "sequence":    int(sequence_arr[i]),
                "is_trade":    bool(is_trade_arr[i]),
                "is_snapshot": False,
            },
            at=TimestampNanos(int(ts_recv_arr[i])),
        )
    sender.flush()


if __name__ == "__main__":
    print("=" * 62)
    print("Run this DDL ONCE in http://localhost:9000 before continuing:")
    print("=" * 62)
    print(DDL)
    print("=" * 62)

    confirm = input("Have you created the table? Type YES to continue: ").strip()
    if confirm.upper() != "YES":
        print("Aborted.")
        raise SystemExit

    files = find_files(DATA_DIR)
    if not files:
        print(f"No .dbn.zst files found in {DATA_DIR}")
        raise SystemExit

    print(f"\nFound {len(files)} files.")
    print(f"Connecting to QuestDB at {QUESTDB_HOST}:{QUESTDB_PORT} ...\n")

    total_rows = total_trades = 0
    t_all      = time.time()

    def log_progress(msg):
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp}  {msg}"
        print(line)
        with open(PROGRESS_FILE, "a") as f:
            f.write(line + "\n")
            
    with Sender.from_conf(f"tcp::addr={QUESTDB_HOST}:{QUESTDB_PORT};") as sender:
        for i, path in enumerate(files, 1):
            fname = os.path.basename(path)
            log_progress(f"[{i:02d}/{len(files)}] START  {fname}")
            rows, trades  = ingest_file(path, sender)
            total_rows   += rows
            total_trades += trades
            log_progress(f"[{i:02d}/{len(files)}] DONE   {fname}  rows={rows:,}  trades={trades:,}")

    elapsed = time.time() - t_all
    print(f"\n{'='*62}")
    print(f"COMPLETE")
    print(f"  Total rows   : {total_rows:,}")
    print(f"  Total trades : {total_trades:,}")
    print(f"  Elapsed      : {elapsed:.1f}s  ({elapsed/60:.1f} min)")
    print(f"  Overall rate : {total_rows/elapsed:,.0f} rows/sec")
    print()
    print("Verify in QuestDB console (http://localhost:9000):")
    print("  SELECT count(), min(ts_recv), max(ts_recv) FROM mbo_events;")
    print("  SELECT action, count() FROM mbo_events GROUP BY action ORDER BY 2 DESC;")
    print("  SELECT count() FROM mbo_events WHERE is_trade = true;")
    
