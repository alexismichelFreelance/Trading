"""
pipeline.py — Orchestrator: fetch bars + book events → features → write.

Two independent stages:

  Stage 1 (book) — expensive, run once per date range:
    Replays L3 book, computes walls + imbalance.
    Writes to es_swing_features (book columns) + es_book_levels (wall zones).
    ~5 hours for full ESH5 date range.

  Stage 2 (trades) — fast, re-run freely when tuning features:
    Reads trade bars only (no book replay).
    Reads es_book_levels + wall columns from es_swing_features.
    Computes absorption, delta, wall hits, volume intensity.
    Overwrites trade columns in es_swing_features in place.
    ~5-10 minutes for full date range.

Usage:
    python pipeline.py --symbol ESH5 --start 2025-02-18 --end 2025-03-21
    python pipeline.py --symbol ESH5 --start 2025-02-18 --end 2025-03-21 --stage book
    python pipeline.py --symbol ESH5 --start 2025-02-18 --end 2025-03-21 --stage trades
    python pipeline.py --symbol ESH5 --start 2025-02-18 --end 2025-02-19 --dry-run
    python pipeline.py --symbol ESH5 --start 2025-02-18 --end 2025-03-21 --stage book --force
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from typing import Literal

import pandas as pd

import config as cfg
import db
from features import assemble_book_features, assemble_trade_features

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("pipeline")


# ── Column registries — keep in sync with CREATE_FEATURE_TABLE DDL ─────────────

# Stage 1: book-derived columns
BOOK_FLOAT_COLS = [
    "mid_price", "last_trade_price",
    "bid_wall_ratio", "bid_wall_abs_log", "bid_wall_dist", "bid_wall_zone_ticks",
    "ask_wall_ratio", "ask_wall_abs_log", "ask_wall_dist", "ask_wall_zone_ticks",
    "wall_proximity",
    "bid_ask_imbalance", "imbalance_ema",
]
BOOK_INT_COLS  = ["bid_wall_abs", "ask_wall_abs"]
BOOK_BOOL_COLS = ["bid_wall_consumed", "ask_wall_consumed"]

# Stage 2: trade-derived columns
TRADE_FLOAT_COLS = [
    "bid_absorption", "ask_absorption",
    "cum_delta", "delta_velocity",
    "delta_momentum_30s", "delta_momentum_90s",
    "bid_wall_hit_rate", "ask_wall_hit_rate",
    "signed_dom", "excess_dom", "excess_dom_ema20", "excess_dom_ema60",
]
TRADE_INT_COLS  = ["roll_volume"]
TRADE_BOOL_COLS = ["delta_flip"]


# ── Stage 1 ────────────────────────────────────────────────────────────────────

def run_book_stage(
    symbol: str,
    start: datetime,
    end: datetime,
    dry_run: bool = False,
    force: bool = False,
) -> None:
    """
    Book replay stage: walls + imbalance → es_swing_features + es_book_levels.

    Skips dates where book levels already exist unless --force is passed.
    This means you can safely re-run after an interruption — it continues
    from where it left off.
    """
    import time

    log.info("=== STAGE 1 (book): %s  %s → %s ===", symbol, start.date(), end.date())

    if not dry_run:
        db.ensure_feature_table()
        db.ensure_book_levels_table()

    chunks_written = rows_written = level_rows_written = 0

    # Determine which calendar dates to process.
    # The idempotency check must happen at the date level — before any chunk
    # for that date runs — so that we either process ALL chunks for a date or NONE.
    # Checking inside the chunk loop caused the bug where the first chunk wrote
    # levels and the guard then skipped all remaining chunks for the same date.

    current_date = start.date()
    end_date     = end.date()

    while current_date < end_date:
        date_str  = current_date.strftime("%Y-%m-%d")
        day_start = datetime(current_date.year, current_date.month, current_date.day,
                             tzinfo=timezone.utc)
        day_end   = day_start + timedelta(days=1)

        # Skip entire date if already computed (idempotency guard)
        if not force and not dry_run and db.book_levels_exist(date_str, symbol):
            log.info("Book levels already exist for %s %s — skipping. Use --force to recompute.",
                     symbol, date_str)
            current_date += timedelta(days=1)
            continue

        log.info("--- Processing date %s ---", date_str)

        for bars_df, chunk_start, chunk_end in db.iter_trade_bars(
            symbol=symbol, start=day_start, end=day_end,
            chunk_minutes=cfg.CHUNK_MINUTES,
            lookback_buffer_s=cfg.LOOKBACK_BUFFER_S,
        ):
            t0 = time.time()
            book_start = chunk_start - timedelta(minutes=cfg.BOOK_SEED_MINUTES)
            log.info("Fetching book events %s → %s…", book_start, chunk_end)
            book_events = db.fetch_book_events(
                symbol, book_start, chunk_end,
                chunk_minutes=cfg.BOOK_CHUNK_MINUTES,
            )
            log.info("Book events: %d rows in %.1fs", len(book_events), time.time() - t0)

            t1 = time.time()
            feat_df, levels_df = assemble_book_features(bars_df, book_events, symbol, cfg)
            log.info("Book features assembled in %.1fs", time.time() - t1)

            if feat_df.empty:
                log.warning("No feature rows for chunk, skipping.")
                continue

            # Strip lookback buffer rows
            cutoff  = pd.Timestamp(chunk_start)
            feat_df = feat_df[feat_df["ts"] >= cutoff]
            if not levels_df.empty:
                levels_df = levels_df[levels_df["ts"] >= cutoff]

            log.info("After buffer trim: %d feature rows, %d level rows",
                     len(feat_df), len(levels_df))

            if dry_run:
                _print_stats(feat_df, stage="book")
                continue

            with db.ILPWriter() as writer:
                _write_book_features(writer, feat_df)
            db.write_book_levels(levels_df, symbol)

            chunks_written     += 1
            rows_written       += len(feat_df)
            level_rows_written += len(levels_df)

        current_date += timedelta(days=1)

    log.info("Stage 1 done: %d chunks, %d feature rows, %d level rows.",
             chunks_written, rows_written, level_rows_written)


# ── Stage 2 ────────────────────────────────────────────────────────────────────

def run_trade_stage(
    symbol: str,
    start: datetime,
    end: datetime,
    dry_run: bool = False,
) -> None:
    """
    Trade feature stage: absorption, delta, wall hits, volume → overwrite es_swing_features.

    Iterates day by day. Loads book_levels + wall_features once per day (not per chunk)
    — these are full-day datasets and loading them per-chunk multiplied query time by
    the number of chunks per day (~12x overhead).
    """
    import time

    log.info("=== STAGE 2 (trades): %s  %s → %s ===", symbol, start.date(), end.date())

    # Verify Stage 1 has been run for the start date
    start_date_str = start.strftime("%Y-%m-%d")
    if not db.book_levels_exist(start_date_str, symbol):
        log.error(
            "No book levels found for %s %s. Run Stage 1 first:\n"
            "  python pipeline.py --symbol %s --start %s --end <end> --stage book",
            symbol, start_date_str, symbol, start_date_str,
        )
        sys.exit(1)

    chunks_written = rows_written = 0

    current_date = start.date()
    end_date     = end.date()

    while current_date < end_date:
        date_str  = current_date.strftime("%Y-%m-%d")
        day_start = datetime(current_date.year, current_date.month, current_date.day,
                             tzinfo=timezone.utc)
        day_end   = day_start + timedelta(days=1)

        # Load Stage 1 output once per day — not per chunk
        t0 = time.time()
        log.info("--- %s: loading book levels + wall features ---", date_str)
        book_levels_df   = db.load_book_levels(date_str, symbol)
        wall_features_df = db.load_wall_features(date_str, symbol)

        if wall_features_df.empty:
            log.warning("No wall features for %s %s — skipping. Run Stage 1 first.",
                        symbol, date_str)
            current_date += timedelta(days=1)
            continue

        log.info("Loaded %d book level rows, %d wall feature rows in %.1fs",
                 len(book_levels_df), len(wall_features_df), time.time() - t0)

        for bars_df, chunk_start, chunk_end in db.iter_trade_bars(
            symbol=symbol, start=day_start, end=day_end,
            chunk_minutes=cfg.CHUNK_MINUTES,
            lookback_buffer_s=cfg.LOOKBACK_BUFFER_S,
        ):
            t1      = time.time()
            feat_df = assemble_trade_features(
                bars_df, book_levels_df, wall_features_df, symbol, cfg
            )
            log.info("Trade features assembled in %.1fs", time.time() - t1)

            if feat_df.empty:
                log.warning("No feature rows for chunk, skipping.")
                continue

            # Strip lookback buffer rows
            cutoff  = pd.Timestamp(chunk_start)
            feat_df = feat_df[feat_df["ts"] >= cutoff]
            log.info("After buffer trim: %d rows", len(feat_df))

            if dry_run:
                _print_stats(feat_df, stage="trades")
                continue

            with db.ILPWriter() as writer:
                _write_trade_features(writer, feat_df)

            chunks_written += 1
            rows_written   += len(feat_df)

        current_date += timedelta(days=1)

    log.info("Stage 2 done: %d chunks, %d rows.", chunks_written, rows_written)


# ── Writers ────────────────────────────────────────────────────────────────────

def _write_book_features(writer: db.ILPWriter, features: pd.DataFrame) -> None:
    _write_rows(writer, features, BOOK_FLOAT_COLS, BOOK_INT_COLS, BOOK_BOOL_COLS)


def _write_trade_features(writer: db.ILPWriter, features: pd.DataFrame) -> None:
    _write_rows(writer, features, TRADE_FLOAT_COLS, TRADE_INT_COLS, TRADE_BOOL_COLS)


def _write_rows(
    writer: db.ILPWriter,
    features: pd.DataFrame,
    float_cols: list,
    int_cols: list,
    bool_cols: list,
) -> None:
    for row in features.itertuples(index=False):
        d      = row._asdict()
        tags   = {"symbol": d["symbol"]}
        fields = {}

        for col in float_cols:
            val = d.get(col)
            if val is not None and not (isinstance(val, float) and math.isnan(val)):
                fields[col] = float(val)

        for col in int_cols:
            val = d.get(col)
            if val is not None and not (isinstance(val, float) and math.isnan(val)):
                fields[col] = int(val)

        for col in bool_cols:
            val = d.get(col)
            if val is not None:
                fields[col] = bool(val)

        if not fields:
            continue

        writer.write_row(cfg.FEATURE_TABLE, tags, fields, int(d["ts_ns"]))


# ── Diagnostics ────────────────────────────────────────────────────────────────

def _print_stats(features: pd.DataFrame, stage: str = "") -> None:
    log.info("[dry-run/%s] columns: %s", stage, list(features.columns))
    numeric = features.select_dtypes(include="number")
    log.info("[dry-run/%s] stats:\n%s", stage, numeric.describe().round(3).to_string())


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_dt(s: str) -> datetime:
    fmt = "%Y-%m-%dT%H:%M:%S" if "T" in s else "%Y-%m-%d"
    return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)


def main():
    p = argparse.ArgumentParser(description="ES feature pipeline")
    p.add_argument("--symbol",  required=True, choices=cfg.SYMBOLS)
    p.add_argument("--start",   required=True, help="YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    p.add_argument("--end",     required=True, help="YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    p.add_argument("--stage",   choices=["book", "trades", "all"], default="all",
                   help="book=Stage1 only, trades=Stage2 only, all=both (default)")
    p.add_argument("--dry-run", action="store_true",
                   help="Compute but do not write to QuestDB")
    p.add_argument("--force",   action="store_true",
                   help="Recompute Stage 1 even if book levels already exist")
    args = p.parse_args()

    start = parse_dt(args.start)
    end   = parse_dt(args.end)

    if args.stage in ("book", "all"):
        run_book_stage(args.symbol, start, end,
                       dry_run=args.dry_run, force=args.force)

    if args.stage in ("trades", "all"):
        run_trade_stage(args.symbol, start, end, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
