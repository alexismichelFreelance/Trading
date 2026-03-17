"""
db.py — QuestDB I/O helpers.

Architecture change: raw MBO events are never transferred to Python.
QuestDB does the 1-second resampling via SAMPLE BY.
Python only receives ~86400 rows/day instead of ~10M+.
"""

from __future__ import annotations

import io
import logging
import socket
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pandas as pd
import requests

from config import QUESTDB_HOST, QUESTDB_HTTP_PORT, QUESTDB_ILP_PORT

log = logging.getLogger(__name__)

_BASE_URL = f"http://{QUESTDB_HOST}:{QUESTDB_HTTP_PORT}"


# ── HTTP query ────────────────────────────────────────────────────────────────

def query_df(sql: str, *, retries: int = 3, pause: float = 2.0) -> pd.DataFrame:
    import time
    t0 = time.time()
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                f"{_BASE_URL}/exp",
                params={"query": sql},
                timeout=300,
                stream=True,       # stream to avoid loading entire response into RAM
            )
            resp.raise_for_status()
            elapsed = time.time() - t0
            log.info("  HTTP response in %.1fs", elapsed)
            t1 = time.time()
            # Read directly from the streaming response — avoids the 930MB string
            df = pd.read_csv(io.BytesIO(resp.content))
            log.info("  CSV parsed in %.1fs — %d rows", time.time() - t1, len(df))
            return df
        except MemoryError as exc:
            log.error(
                "MemoryError parsing query response — chunk too large. "
                "Reduce BOOK_CHUNK_MINUTES in config.py. Error: %s", exc
            )
            raise
        except Exception as exc:
            log.warning("Query attempt %d/%d failed: %s  (%s)",
                        attempt, retries, type(exc).__name__, exc)
            if attempt == retries:
                raise
            time.sleep(pause * attempt)
    return pd.DataFrame()


def execute_ddl(sql: str) -> None:
    resp = requests.get(f"{_BASE_URL}/exec", params={"query": sql}, timeout=60)
    resp.raise_for_status()
    result = resp.json()
    if result.get("error"):
        raise RuntimeError(f"QuestDB DDL error: {result['error']}")


# ── Trade bars iterator ───────────────────────────────────────────────────────

def iter_trade_bars(
    symbol: str,
    start: datetime,
    end: datetime,
    chunk_minutes: int = 1440,
    lookback_buffer_s: int = 120,
) -> Iterator[tuple[pd.DataFrame, datetime, datetime]]:
    """
    Yield pre-aggregated 1-second OHLCV trade bars from QuestDB.

    QuestDB SAMPLE BY does the resampling — we transfer ~86400 rows/day
    instead of ~10M raw events.

    Aggressor side mapping (CME MBO convention):
        side = 'A' (ask resting, hit by buyer)  → buy_vol
        side = 'B' (bid resting, hit by seller) → sell_vol
    """
    chunk_delta = timedelta(minutes=chunk_minutes)
    buffer      = timedelta(seconds=lookback_buffer_s)
    current     = start

    while current < end:
        chunk_end   = min(current + chunk_delta, end)
        fetch_start = current - buffer

        sql = f"""
            SELECT
                ts_recv,
                first(price)                                      AS open,
                max(price)                                        AS high,
                min(price)                                        AS low,
                last(price)                                       AS close,
                sum(CASE WHEN side = 'A' THEN size ELSE 0 END)   AS buy_vol,
                sum(CASE WHEN side = 'B' THEN size ELSE 0 END)   AS sell_vol,
                sum(size)                                         AS total_vol,
                count()                                           AS trade_count
            FROM mbo_events
            WHERE symbol = '{symbol}'
              AND ts_recv >= '{_fmt(fetch_start)}'
              AND ts_recv  < '{_fmt(chunk_end)}'
              AND action IN ('T', 'F')
            SAMPLE BY 1s FILL(NULL)
            ALIGN TO CALENDAR
        """
        log.info("Fetching trade bars %s → %s", _fmt(current), _fmt(chunk_end))
        df = query_df(sql)

        if df.empty:
            log.warning("Empty trade bar chunk, skipping.")
            current = chunk_end
            continue

        df["ts_recv"]    = pd.to_datetime(df["ts_recv"], utc=True)
        df["_in_buffer"] = df["ts_recv"] < pd.Timestamp(current)

        # carry close price forward into NULL-filled seconds (no trades)
        for col in ["open", "high", "low", "close"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["close"] = df["close"].ffill()
        df["open"]  = df["open"].ffill()
        df["high"]  = df["high"].where(df["high"].notna(), df["close"])
        df["low"]   = df["low"].where(df["low"].notna(),   df["close"])

        for col in ["buy_vol", "sell_vol", "total_vol", "trade_count"]:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)

        yield df, current, chunk_end
        current = chunk_end


# ── Book events fetcher ───────────────────────────────────────────────────────

def fetch_book_events(
    symbol: str,
    start: datetime,
    end: datetime,
    chunk_minutes: int = 30,   # reduced from 360 — ES has ~millions of events/hour
) -> pd.DataFrame:
    """
    Fetch raw book events (A/C/M/R) for book snapshot building.

    chunk_minutes controls memory usage — smaller = less RAM but more queries.
    30 minutes is ~150-200MB per chunk for ES during RTH, manageable.

    Note: if you still hit memory errors, reduce further to 15 or 10 minutes
    in config.py: BOOK_CHUNK_MINUTES = 15
    """
    from datetime import timedelta

    chunk_delta = timedelta(minutes=chunk_minutes)
    current     = start
    chunks      = []

    while current < end:
        chunk_end = min(current + chunk_delta, end)

        sql = f"""
            SELECT
                ts_recv,
                action,
                side,
                price,
                size,
                order_id,
                sequence
            FROM mbo_events
            WHERE symbol = '{symbol}'
              AND ts_recv >= '{_fmt(current)}'
              AND ts_recv  < '{_fmt(chunk_end)}'
              AND action IN ('A', 'C', 'M', 'R')
            ORDER BY ts_recv ASC
        """
        log.info("Fetching book events %s → %s", _fmt(current), _fmt(chunk_end))
        df = query_df(sql)

        if not df.empty:
            df["ts_recv"]  = pd.to_datetime(df["ts_recv"], utc=True)
            df["price"]    = df["price"].astype(float)
            df["size"]     = df["size"].astype(int)
            df["order_id"] = df["order_id"].astype(int)
            df["sequence"] = df["sequence"].astype(int)
            chunks.append(df)

        current = chunk_end

    if not chunks:
        return pd.DataFrame()

    result = pd.concat(chunks, ignore_index=True)
    log.info("Book events total: %d rows across %d sub-chunks",
             len(result), len(chunks))
    return result


# ── ILP writer ────────────────────────────────────────────────────────────────

class ILPWriter:
    def __init__(self, host=QUESTDB_HOST, port=QUESTDB_ILP_PORT, batch_size=5_000):
        self._host, self._port = host, port
        self._batch_size = batch_size
        self._buf: list[str] = []
        self._sock: socket.socket | None = None

    def __enter__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.connect((self._host, self._port))
        return self

    def __exit__(self, *_):
        self.flush()
        if self._sock:
            self._sock.close()
            self._sock = None

    def write_row(self, table: str, tags: dict, fields: dict, ts_ns: int) -> None:
        tag_str   = ",".join(f"{k}={v}" for k, v in tags.items())
        field_str = ",".join(_ilp_field(k, v) for k, v in fields.items())
        self._buf.append(f"{table},{tag_str} {field_str} {ts_ns}\n")
        if len(self._buf) >= self._batch_size:
            self.flush()

    def flush(self):
        if not self._buf or self._sock is None:
            return
        self._sock.sendall("".join(self._buf).encode())
        self._buf.clear()


def _ilp_field(k, v):
    if isinstance(v, bool):   return f"{k}={str(v).lower()}"
    if isinstance(v, float):  return f"{k}={v}"
    if isinstance(v, int):    return f"{k}={v}i"
    return f'{k}="{v}"'


# ── DDL ───────────────────────────────────────────────────────────────────────

CREATE_FEATURE_TABLE = """
CREATE TABLE IF NOT EXISTS es_swing_features (
    ts                    TIMESTAMP,
    symbol                SYMBOL,
    -- Price reference (viz only)
    mid_price             DOUBLE,
    last_trade_price      DOUBLE,
    -- Stage 2: Absorption (CNN inputs)
    bid_absorption        DOUBLE,
    ask_absorption        DOUBLE,
    -- Stage 2: Delta
    cum_delta             DOUBLE,     -- viz only
    delta_flip            BOOLEAN,    -- viz only
    delta_velocity        DOUBLE,     -- CNN input
    delta_momentum_30s    DOUBLE,     -- CNN input (sigma units)
    delta_momentum_90s    DOUBLE,     -- CNN input (sigma units)
    -- Stage 1: Walls
    bid_wall_ratio        DOUBLE,     -- CNN input
    bid_wall_abs          LONG,       -- viz sizing only
    bid_wall_abs_log      DOUBLE,     -- CNN input
    bid_wall_dist         DOUBLE,     -- CNN input
    bid_wall_zone_ticks   DOUBLE,     -- viz / diagnostics
    ask_wall_ratio        DOUBLE,     -- CNN input
    ask_wall_abs          LONG,       -- viz sizing only
    ask_wall_abs_log      DOUBLE,     -- CNN input
    ask_wall_dist         DOUBLE,     -- CNN input
    ask_wall_zone_ticks   DOUBLE,     -- viz / diagnostics
    wall_proximity        DOUBLE,     -- viz only
    bid_wall_consumed     BOOLEAN,    -- viz only
    ask_wall_consumed     BOOLEAN,    -- viz only
    -- Stage 1: Imbalance
    bid_ask_imbalance     DOUBLE,     -- CNN input
    imbalance_ema         DOUBLE,     -- viz / additional CNN input
    -- Stage 2: Wall hits (CNN inputs)
    bid_wall_hit_rate     DOUBLE,
    ask_wall_hit_rate     DOUBLE,
    -- Stage 2: Volume intensity
    roll_volume           LONG,       -- viz only
    signed_dom            DOUBLE,     -- viz only
    excess_dom            DOUBLE,     -- viz only
    excess_dom_ema20      DOUBLE,     -- CNN input
    excess_dom_ema60      DOUBLE      -- CNN input
) TIMESTAMP(ts) PARTITION BY DAY WAL;
"""

def ensure_feature_table() -> None:
    execute_ddl(CREATE_FEATURE_TABLE)
    log.info("Feature table ready.")


# ── Book levels table ─────────────────────────────────────────────────────────
#
# One row per (second, side, price) in the wall zone.
# Written once during Stage 1 (book replay). Read by Stage 2 (wall hits)
# and visualiser (full wall display without replaying 19M+ mbo_events).
#
# This replaces the on-the-fly mbo_events replay in the visualiser which
# produced 19M+ rows and caused MemoryErrors.

CREATE_BOOK_LEVELS_TABLE = """
CREATE TABLE IF NOT EXISTS es_book_levels (
    ts      TIMESTAMP,
    symbol  SYMBOL,
    side    SYMBOL,
    price   DOUBLE,
    size    LONG
) TIMESTAMP(ts) PARTITION BY DAY WAL;
"""

def ensure_book_levels_table() -> None:
    execute_ddl(CREATE_BOOK_LEVELS_TABLE)
    log.info("Book levels table ready.")


def write_book_levels(levels_df: pd.DataFrame, symbol: str) -> None:
    """
    Write wall zone level rows to es_book_levels via ILP.
    levels_df columns: ts, side, price, size
    """
    if levels_df.empty:
        return
    df = levels_df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"])
    with ILPWriter() as w:
        for _, row in df.iterrows():
            w.write_row(
                table  = "es_book_levels",
                tags   = {"symbol": symbol, "side": str(row["side"])},
                fields = {"price": float(row["price"]), "size": int(row["size"])},
                ts_ns  = int(row["ts"].value),
            )
    log.info("Wrote %d book level rows to QuestDB.", len(df))


def load_book_levels(date: str, symbol: str) -> pd.DataFrame:
    """Load wall zone levels for one day. Used by Stage 2 and visualiser."""
    sql = f"""
        SELECT ts, side, price, size
        FROM es_book_levels
        WHERE ts >= '{date}T00:00:00.000000Z'
          AND ts <  '{date}T23:59:59.999999Z'
          AND symbol = '{symbol}'
        ORDER BY ts
    """
    df = query_df(sql)
    if not df.empty and "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    return df


def book_levels_exist(date: str, symbol: str) -> bool:
    """Check whether book levels have been written for this date (Stage 1 guard)."""
    sql = f"""
        SELECT count() as n
        FROM es_book_levels
        WHERE ts >= '{date}T00:00:00.000000Z'
          AND ts <  '{date}T23:59:59.999999Z'
          AND symbol = '{symbol}'
    """
    try:
        df = query_df(sql)
        return int(df["n"].iloc[0]) > 0 if not df.empty else False
    except Exception:
        return False


def load_wall_features(date: str, symbol: str) -> pd.DataFrame:
    """
    Load Stage 1 wall + imbalance columns from es_swing_features.
    Used by Stage 2 to get bid/ask_wall_abs for wall hit normalisation.
    """
    sql = f"""
        SELECT ts,
               bid_wall_abs, bid_wall_abs_log, bid_wall_dist, bid_wall_ratio,
               bid_wall_zone_ticks, bid_wall_consumed,
               ask_wall_abs, ask_wall_abs_log, ask_wall_dist, ask_wall_ratio,
               ask_wall_zone_ticks, ask_wall_consumed,
               wall_proximity, bid_ask_imbalance, imbalance_ema
        FROM es_swing_features
        WHERE ts >= '{date}T00:00:00.000000Z'
          AND ts <  '{date}T23:59:59.999999Z'
          AND symbol = '{symbol}'
        ORDER BY ts
    """
    df = query_df(sql)
    if not df.empty and "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    return df


# ── Wall events table ─────────────────────────────────────────────────────────
#
# One row per WallAbsorptionEvent.  Written once per book-replay pass;
# read by the FSM and visualiser without needing to re-replay the book.
#
# touch_volumes is stored as a comma-separated string — e.g. "800,530,210"
# QuestDB has no array type, and this is only needed for human inspection /
# visualiser display, not for any computation.

CREATE_WALL_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS es_wall_events (
    ts                  TIMESTAMP,
    symbol              SYMBOL,
    wall_price          DOUBLE,
    wall_side           SYMBOL,
    direction           SYMBOL,
    wall_size_peak      LONG,
    n_touches           INT,
    touch_volumes       STRING,
    defence_ratio       DOUBLE,
    exhaustion_slope    DOUBLE,
    pattern_duration_s  DOUBLE
) TIMESTAMP(ts) PARTITION BY DAY WAL;
"""


def ensure_wall_events_table() -> None:
    execute_ddl(CREATE_WALL_EVENTS_TABLE)
    log.info("Wall events table ready.")


def write_wall_events(events: list, symbol: str) -> None:
    """
    Persist a list of WallAbsorptionEvent objects to es_wall_events.

    Parameters
    ----------
    events  : list[WallAbsorptionEvent] from wall_detector.WallDetector
    symbol  : e.g. "ESH5"
    """
    if not events:
        return

    with ILPWriter() as w:
        for evt in events:
            ts_ns = int(evt.ts.value)
            w.write_row(
                table  = "es_wall_events",
                tags   = {
                    "symbol":    symbol,
                    "wall_side": evt.wall_side,
                    "direction": evt.direction,
                },
                fields = {
                    "wall_price":         float(evt.wall_price),
                    "wall_size_peak":     int(evt.wall_size_peak),
                    "n_touches":          int(evt.n_touches),
                    "touch_volumes":      ",".join(str(v) for v in evt.touch_volumes),
                    "defence_ratio":      float(evt.defence_ratio),
                    "exhaustion_slope":   float(evt.exhaustion_slope),
                    "pattern_duration_s": float(evt.pattern_duration_s),
                },
                ts_ns = ts_ns,
            )

    log.info("Wrote %d wall events to QuestDB.", len(events))


def load_wall_events(date: str, symbol: str) -> "pd.DataFrame":
    """
    Load wall events for a single day from QuestDB.
    Returns empty DataFrame if none found.
    Used by the FSM and visualiser.
    """
    sql = f"""
        SELECT *
        FROM es_wall_events
        WHERE ts >= '{date}T00:00:00.000000Z'
          AND ts <  '{date}T23:59:59.999999Z'
          AND symbol = '{symbol}'
        ORDER BY ts
    """
    df = query_df(sql)
    if not df.empty and "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    return df


def wall_events_exist(date: str, symbol: str) -> bool:
    """
    Check whether wall events have already been computed for this date.
    Used to skip the expensive book replay when data is already cached.
    """
    sql = f"""
        SELECT count() as n
        FROM es_wall_events
        WHERE ts >= '{date}T00:00:00.000000Z'
          AND ts <  '{date}T23:59:59.999999Z'
          AND symbol = '{symbol}'
    """
    try:
        df = query_df(sql)
        return int(df["n"].iloc[0]) > 0 if not df.empty else False
    except Exception:
        return False


# ── Book features table ───────────────────────────────────────────────────────
#
# 5 normalised features per second, computed from L3 book snapshots.
# Written once per book-replay pass; read by the CNN training pipeline.
# All values are dimensionless ratios — no absolute sizes stored.

CREATE_BOOK_FEATURES_TABLE = """
CREATE TABLE IF NOT EXISTS es_book_features (
    ts                TIMESTAMP,
    symbol            SYMBOL,
    bid_wall_ratio    DOUBLE,
    bid_wall_dist     DOUBLE,
    ask_wall_ratio    DOUBLE,
    ask_wall_dist     DOUBLE,
    trade_intensity   DOUBLE
) TIMESTAMP(ts) PARTITION BY DAY WAL;
"""


def ensure_book_features_table() -> None:
    execute_ddl(CREATE_BOOK_FEATURES_TABLE)
    log.info("Book features table ready.")


def write_book_features(df: pd.DataFrame, symbol: str) -> None:
    """
    Write a book features DataFrame to es_book_features via ILP.

    df must have columns: ts, bid_wall_ratio, bid_wall_dist,
                          ask_wall_ratio, ask_wall_dist, trade_intensity
    """
    if df.empty:
        return

    df = df.copy()
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df = df.dropna(subset=["ts"])

    with ILPWriter() as w:
        for _, row in df.iterrows():
            ts_ns = int(row["ts"].value)
            w.write_row(
                table  = "es_book_features",
                tags   = {"symbol": symbol},
                fields = {
                    "bid_wall_ratio":  float(row["bid_wall_ratio"]),
                    "bid_wall_dist":   float(row["bid_wall_dist"]),
                    "ask_wall_ratio":  float(row["ask_wall_ratio"]),
                    "ask_wall_dist":   float(row["ask_wall_dist"]),
                    "trade_intensity": float(row["trade_intensity"]),
                },
                ts_ns = ts_ns,
            )
    log.info("Wrote %d book feature rows to QuestDB.", len(df))


def load_book_features(date: str, symbol: str) -> pd.DataFrame:
    """Load book features for one day from QuestDB."""
    sql = f"""
        SELECT ts, bid_wall_ratio, bid_wall_dist,
               ask_wall_ratio, ask_wall_dist, trade_intensity
        FROM es_book_features
        WHERE ts >= '{date}T00:00:00.000000Z'
          AND ts <  '{date}T23:59:59.999999Z'
          AND symbol = '{symbol}'
        ORDER BY ts
    """
    df = query_df(sql)
    if not df.empty and "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    return df


def book_features_exist(date: str, symbol: str) -> bool:
    """Check whether book features have already been computed for this date."""
    sql = f"""
        SELECT count() as n
        FROM es_book_features
        WHERE ts >= '{date}T00:00:00.000000Z'
          AND ts <  '{date}T23:59:59.999999Z'
          AND symbol = '{symbol}'
    """
    try:
        df = query_df(sql)
        return int(df["n"].iloc[0]) > 0 if not df.empty else False
    except Exception:
        return False


def _fmt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")