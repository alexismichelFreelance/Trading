"""
features.py — Feature computation from 1-second bars + L3 book snapshots.

Design principles:
  - No binary detectors — everything is continuous
  - No magic distance thresholds — full book depth scanned, ratio defines wall zone
  - Regime-independent: ratios and normalised values throughout
  - Pre-smoothed CNN inputs: model receives signal, not noise

Pipeline stages:
  Stage 1 (book, expensive, run once):
    compute_walls      → wall features + book_levels_df for es_book_levels
    compute_imbalance  → bid_ask_imbalance, imbalance_ema
    assemble_book_features

  Stage 2 (trades, fast, re-run freely):
    compute_absorption      → bid/ask_absorption
    compute_delta           → cum_delta (viz), delta_momentum_Ns (CNN)
    compute_wall_hits       → bid/ask_wall_hit_rate
    compute_volume_intensity → roll_volume (viz), excess_dom_emaN (CNN)
    assemble_trade_features

CNN input features (16):
  bid_absorption, ask_absorption,
  delta_momentum_30s, delta_momentum_90s, delta_velocity,
  bid_wall_ratio, bid_wall_abs_log, bid_wall_dist,
  ask_wall_ratio, ask_wall_abs_log, ask_wall_dist,
  bid_wall_hit_rate, ask_wall_hit_rate,
  bid_ask_imbalance,
  excess_dom_ema20, excess_dom_ema60

Stored but not CNN inputs (visualisation / diagnostics):
  mid_price, last_trade_price,
  cum_delta, delta_flip,
  bid_wall_abs, ask_wall_abs,
  bid_wall_zone_ticks, ask_wall_zone_ticks,
  wall_proximity, bid_wall_consumed, ask_wall_consumed,
  imbalance_ema, roll_volume, signed_dom, excess_dom
"""

from __future__ import annotations

import logging
from typing import List, Tuple

import numpy as np
import pandas as pd

from book import BookSnapshot

log = logging.getLogger(__name__)


# ===============================================================================
# 1. ABSORPTION  (Stage 2 — trade bars only)
#
#    bid_absorption = sell aggressor vol / price range ticks
#    ask_absorption = buy  aggressor vol / price range ticks
#    Rolling window accumulates pressure. Range_ticks as denominator makes it
#    regime-independent: fast moves produce more range, so same vol = lower ratio.
# ===============================================================================

def compute_absorption(
    bars: pd.DataFrame,
    tick_size: float,
    time_window_s: int,
    min_vol_gate: int = 5,
) -> pd.DataFrame:
    w         = time_window_s
    roll_buy  = bars["buy_vol"].rolling(w,  min_periods=1).sum()
    roll_sell = bars["sell_vol"].rolling(w, min_periods=1).sum()
    roll_vol  = bars["total_vol"].rolling(w, min_periods=1).sum()
    roll_high = bars["high"].rolling(w, min_periods=1).max()
    roll_low  = bars["low"].rolling(w,  min_periods=1).min()

    range_ticks = ((roll_high - roll_low) / tick_size).clip(lower=1.0)
    active      = roll_vol >= min_vol_gate

    return pd.DataFrame({
        "bid_absorption": (roll_sell / range_ticks).where(active, 0.0).round(2),
        "ask_absorption": (roll_buy  / range_ticks).where(active, 0.0).round(2),
    }, index=bars.index)


# ===============================================================================
# 2. DELTA  (Stage 2 — trade bars only)
#
#    cum_delta          : rolling 60s (buy-sell) — visualisation reference only
#    delta_flip         : sign change event flag — visualisation only
#    delta_velocity     : first difference of cum_delta — CNN input
#    delta_momentum_Ns  : normalised rolling sum of per-second net delta.
#                         = rolling_sum(buy-sell, N) / rolling_std(buy-sell, 60s)
#                         Units: sigma, clipped +-5. Regime-independent CNN inputs.
# ===============================================================================

def compute_delta(
    bars: pd.DataFrame,
    lookback_s: int,
    momentum_windows: List[int] = (30, 90),
) -> pd.DataFrame:
    w         = lookback_s
    cum_delta = (bars["buy_vol"].rolling(w, min_periods=1).sum()
                 - bars["sell_vol"].rolling(w, min_periods=1).sum())
    prev      = cum_delta.shift(1)
    flip      = (np.sign(cum_delta) != np.sign(prev)) & prev.notna() & (prev != 0)
    velocity  = (cum_delta - prev).fillna(0.0)

    # Per-second net delta — basis for momentum windows
    net_per_s   = (bars["buy_vol"] - bars["sell_vol"]).astype(float)
    # Normalisation denominator: rolling std over lookback_s — regime-adaptive
    rolling_std = net_per_s.rolling(lookback_s, min_periods=5).std().replace(0, np.nan)

    result = pd.DataFrame({
        "cum_delta":      cum_delta.round(0),
        "delta_flip":     flip.fillna(False),
        "delta_velocity": velocity.round(2),
    }, index=bars.index)

    for mw in momentum_windows:
        rolled     = net_per_s.rolling(mw, min_periods=max(5, mw // 4)).sum()
        normalised = (rolled / rolling_std).fillna(0.0).clip(-5.0, 5.0).round(3)
        result[f"delta_momentum_{mw}s"] = normalised

    return result


# ===============================================================================
# 3. WALLS  (Stage 1 — requires L3 book replay)
#
#    Full book depth scanned on each side — no distance constraint.
#    The CNN will learn which distances matter; we don't pre-filter.
#
#    Wall zone definition:
#      All price levels where size > median(all levels with size >= min_size_abs)
#      min_size_abs is a noise floor only — not a signal threshold.
#
#    Dominant wall: largest level within the zone.
#
#    Per-second outputs (feature rows):
#      bid_wall_ratio      dominant / median — anomaly magnitude
#      bid_wall_abs        raw contracts at dominant level (viz sizing)
#      bid_wall_abs_log    log1p(bid_wall_abs) — CNN input (compresses scale)
#      bid_wall_dist       ticks from mid to dominant level — CNN input
#      bid_wall_zone_ticks width of zone in ticks — viz / diagnostics
#      bid_wall_consumed   True when last second's dominant level disappeared
#      ask_*               same for ask side
#      wall_proximity      min(bid_wall_dist, ask_wall_dist)
#
#    Also returns book_levels_df:
#      All zone level rows (ts, side, price, size) — written to es_book_levels.
#      Used by Stage 2 (wall hits) and visualiser (full wall display).
# ===============================================================================

def compute_walls(
    snapshots: List[BookSnapshot],
    snap_index: pd.DatetimeIndex,
    tick_size: float,
    min_size_abs: int = 10,
    wall_multiplier: float = 3.0,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns (wall_features_df, book_levels_df).

    wall_features_df: indexed by ts, aligned to snap_index
    book_levels_df:   columns ts, side, price, size — all wall zone levels
    """
    feature_rows = []
    level_rows   = []

    prev_bid_price: float | None = None
    prev_ask_price: float | None = None

    for snap in snapshots:
        mid = snap.mid()

        bid_ratio = 0.0; bid_abs = 0; bid_dist = np.nan; bid_zone_ticks = np.nan
        ask_ratio = 0.0; ask_abs = 0; ask_dist = np.nan; ask_zone_ticks = np.nan
        bid_wall_price: float | None = None
        ask_wall_price: float | None = None

        if mid is not None:
            # ── Bid side — full book depth ────────────────────────────────────
            all_bids = {p: s for p, s in snap.bids.items()
                        if p < mid and s >= min_size_abs}

            if len(all_bids) >= 2:
                median_sz = float(np.median(list(all_bids.values())))
                if median_sz > 0:
                    zone_bids = {p: s for p, s in all_bids.items()
                                 if s > median_sz * wall_multiplier}
                    if zone_bids:
                        best_p         = max(zone_bids, key=zone_bids.get)
                        best_s         = zone_bids[best_p]
                        bid_ratio      = round(best_s / median_sz, 2)
                        bid_abs        = best_s
                        bid_dist       = round((mid - best_p) / tick_size, 2)
                        bid_wall_price = best_p
                        zp             = list(zone_bids.keys())
                        bid_zone_ticks = round((max(zp) - min(zp)) / tick_size, 2)
                        for p, s in zone_bids.items():
                            level_rows.append((snap.ts, 'B', p, s))

            elif len(all_bids) == 1:
                best_p         = list(all_bids.keys())[0]
                bid_abs        = all_bids[best_p]
                bid_dist       = round((mid - best_p) / tick_size, 2)
                bid_ratio      = 1.0
                bid_wall_price = best_p
                level_rows.append((snap.ts, 'B', best_p, bid_abs))

            # ── Ask side — full book depth ────────────────────────────────────
            all_asks = {p: s for p, s in snap.asks.items()
                        if p > mid and s >= min_size_abs}

            if len(all_asks) >= 2:
                median_sz = float(np.median(list(all_asks.values())))
                if median_sz > 0:
                    zone_asks = {p: s for p, s in all_asks.items()
                                 if s > median_sz * wall_multiplier}
                    if zone_asks:
                        best_p         = max(zone_asks, key=zone_asks.get)
                        best_s         = zone_asks[best_p]
                        ask_ratio      = round(best_s / median_sz, 2)
                        ask_abs        = best_s
                        ask_dist       = round((best_p - mid) / tick_size, 2)
                        ask_wall_price = best_p
                        zp             = list(zone_asks.keys())
                        ask_zone_ticks = round((max(zp) - min(zp)) / tick_size, 2)
                        for p, s in zone_asks.items():
                            level_rows.append((snap.ts, 'A', p, s))

            elif len(all_asks) == 1:
                best_p         = list(all_asks.keys())[0]
                ask_abs        = all_asks[best_p]
                ask_dist       = round((best_p - mid) / tick_size, 2)
                ask_ratio      = 1.0
                ask_wall_price = best_p
                level_rows.append((snap.ts, 'A', best_p, ask_abs))

        proximity = min(
            bid_dist if not np.isnan(bid_dist) else 999,
            ask_dist if not np.isnan(ask_dist) else 999,
        )
        bid_consumed = (prev_bid_price is not None and bid_abs == 0
                        and prev_bid_price not in getattr(snap, 'bids', {}))
        ask_consumed = (prev_ask_price is not None and ask_abs == 0
                        and prev_ask_price not in getattr(snap, 'asks', {}))

        prev_bid_price = bid_wall_price
        prev_ask_price = ask_wall_price

        feature_rows.append({
            "ts":                  snap.ts,
            "bid_wall_ratio":      bid_ratio,
            "bid_wall_abs":        bid_abs,
            "bid_wall_abs_log":    round(float(np.log1p(bid_abs)), 4),
            "bid_wall_dist":       bid_dist,
            "bid_wall_zone_ticks": bid_zone_ticks,
            "ask_wall_ratio":      ask_ratio,
            "ask_wall_abs":        ask_abs,
            "ask_wall_abs_log":    round(float(np.log1p(ask_abs)), 4),
            "ask_wall_dist":       ask_dist,
            "ask_wall_zone_ticks": ask_zone_ticks,
            "wall_proximity":      round(proximity, 2) if proximity < 999 else np.nan,
            "bid_wall_consumed":   bid_consumed,
            "ask_wall_consumed":   ask_consumed,
        })

    # Wall features DataFrame
    feat_df = pd.DataFrame(feature_rows).set_index("ts")
    feat_df.index = pd.DatetimeIndex(feat_df.index, tz="UTC")
    feat_df = feat_df.reindex(snap_index)

    # Book levels DataFrame
    if level_rows:
        lvl_df = pd.DataFrame(level_rows, columns=["ts", "side", "price", "size"])
        lvl_df["ts"] = pd.to_datetime(lvl_df["ts"], utc=True)
    else:
        lvl_df = pd.DataFrame(columns=["ts", "side", "price", "size"])

    return feat_df, lvl_df


# ===============================================================================
# 4. IMBALANCE  (Stage 1 — requires L3 book snapshots)
#
#    (bid_size - ask_size) / (bid_size + ask_size) across top N levels.
#    Range [-1, +1].
#    bid_ask_imbalance : raw per-second — CNN input
#    imbalance_ema     : EMA-smoothed — viz / additional CNN input
# ===============================================================================

def compute_imbalance(
    snapshots: List[BookSnapshot],
    snap_index: pd.DatetimeIndex,
    n_levels: int,
    ema_span_s: int,
) -> pd.DataFrame:
    rows = []
    for snap in snapshots:
        bv = snap.total_bid_size(n_levels)
        av = snap.total_ask_size(n_levels)
        d  = bv + av
        rows.append({
            "ts":                snap.ts,
            "bid_ask_imbalance": round((bv - av) / d, 4) if d else 0.0,
        })
    df = pd.DataFrame(rows).set_index("ts")
    df.index = pd.DatetimeIndex(df.index, tz="UTC")
    df = df.reindex(snap_index)
    df["imbalance_ema"] = (
        df["bid_ask_imbalance"].ewm(span=ema_span_s, adjust=False).mean().round(4)
    )
    return df


# ===============================================================================
# 5. WALL HITS  (Stage 2 — requires book_levels_df from Stage 1)
#
#    Replaces wick intensity entirely.
#
#    Wall hit: aggressor trade printing at a price within the wall zone.
#    ES trades at exact tick boundaries → close price == zone price is precise.
#
#    bid_wall_hit_rate = rolling_sum(sell_vol at bid zone prices, W)
#                        / rolling_max(bid_wall_abs, W)
#    ask_wall_hit_rate = rolling_sum(buy_vol  at ask zone prices, W)
#                        / rolling_max(ask_wall_abs, W)
#
#    Range [0, inf+]:
#      0.0  = no aggressor activity at wall this window
#      0.5  = half wall size attacked
#      1.0  = full wall size hit (wall under sustained attack)
#      >1.0 = volume exceeds wall size (wall refreshed, or zone is wide)
#
#    Self-normalising: denominator is the wall's own size, not an absolute threshold.
# ===============================================================================

def compute_wall_hits(
    bars: pd.DataFrame,
    book_levels_df: pd.DataFrame,
    wall_features_df: pd.DataFrame,
    tick_size: float,
    hit_window_s: int,
) -> pd.DataFrame:
    empty = pd.DataFrame({
        "bid_wall_hit_rate": 0.0,
        "ask_wall_hit_rate": 0.0,
    }, index=bars.index)

    if book_levels_df.empty:
        return empty

    lvl = book_levels_df.copy()
    lvl["ts_s"] = pd.to_datetime(lvl["ts"]).dt.floor("1s")

    bid_zone = (lvl[lvl["side"] == "B"]
                .groupby("ts_s")["price"]
                .apply(set))
    ask_zone = (lvl[lvl["side"] == "A"]
                .groupby("ts_s")["price"]
                .apply(set))

    bars_ts_s  = bars.index.floor("1s")
    close_vals = bars["close"].values

    bid_zone_a = bid_zone.reindex(bars_ts_s).values
    ask_zone_a = ask_zone.reindex(bars_ts_s).values

    def _in_zone(close_val, zone):
        if zone is None or (isinstance(zone, float) and np.isnan(zone)):
            return 0.0
        return 1.0 if close_val in zone else 0.0

    bid_hit = pd.Series(
        [_in_zone(c, z) for c, z in zip(close_vals, bid_zone_a)],
        index=bars.index, dtype=float,
    )
    ask_hit = pd.Series(
        [_in_zone(c, z) for c, z in zip(close_vals, ask_zone_a)],
        index=bars.index, dtype=float,
    )

    # Weight by aggressor volume in each second
    bid_hit_vol  = bars["sell_vol"].astype(float) * bid_hit
    ask_hit_vol  = bars["buy_vol"].astype(float)  * ask_hit

    bid_hit_roll = bid_hit_vol.rolling(hit_window_s, min_periods=1).sum()
    ask_hit_roll = ask_hit_vol.rolling(hit_window_s, min_periods=1).sum()

    # Normalise by peak wall size in same window
    if "bid_wall_abs" not in wall_features_df.columns:
        return empty

    # Deduplicate index — QuestDB WAL tables can produce duplicate timestamps
    # when chunks overlap at boundaries. Keep last occurrence (most recent write).
    wf = wall_features_df
    if wf.index.duplicated().any():
        wf = wf[~wf.index.duplicated(keep="last")]

    bid_abs      = wf["bid_wall_abs"].reindex(bars.index).fillna(0)
    ask_abs      = wf["ask_wall_abs"].reindex(bars.index).fillna(0)
    bid_abs_roll = bid_abs.rolling(hit_window_s, min_periods=1).max().clip(lower=1)
    ask_abs_roll = ask_abs.rolling(hit_window_s, min_periods=1).max().clip(lower=1)

    return pd.DataFrame({
        "bid_wall_hit_rate": (bid_hit_roll / bid_abs_roll).round(4),
        "ask_wall_hit_rate": (ask_hit_roll / ask_abs_roll).round(4),
    }, index=bars.index)


# ===============================================================================
# 6. VOLUME INTENSITY  (Stage 2 — trade bars only)
#
#    roll_volume      : total contracts in rolling window — viz only
#    signed_dom       : directional_dom × sign(buy-sell), raw — viz only
#    excess_dom       : (dom - 0.5) × sign — range [-0.5, +0.5], viz only
#                       0 = perfectly balanced, ±0.5 = completely one-sided
#    excess_dom_emaN  : EMA-smoothed excess_dom — CNN inputs
#                       Pre-smoothed so CNN sees signal, not per-second noise
# ===============================================================================

def compute_volume_intensity(
    bars: pd.DataFrame,
    window_s: int,
    excess_dom_ema_spans: List[int] = (20, 60),
) -> pd.DataFrame:
    w         = window_s
    roll_buy  = bars["buy_vol"].rolling(w,   min_periods=1).sum()
    roll_sell = bars["sell_vol"].rolling(w,  min_periods=1).sum()
    roll_vol  = bars["total_vol"].rolling(w, min_periods=1).sum()

    dom        = (roll_buy.combine(roll_sell, max) / roll_vol.clip(lower=1)
                  ).clip(0.5, 1.0).round(4)
    sign       = np.sign(roll_buy - roll_sell).replace(0, 1)
    signed_dom = (dom * sign).round(4)
    excess_dom = ((dom - 0.5) * sign).round(4)

    result = pd.DataFrame({
        "roll_volume": roll_vol.astype(int),
        "signed_dom":  signed_dom,
        "excess_dom":  excess_dom,
    }, index=bars.index)

    for span in excess_dom_ema_spans:
        result[f"excess_dom_ema{span}"] = (
            excess_dom.ewm(span=span, adjust=False).mean().round(4)
        )
    return result


# ===============================================================================
# ASSEMBLER — Stage 1  (book-derived, expensive, run once)
#
# Returns (feature_df, book_levels_df).
# feature_df     → written to es_swing_features (book columns only)
# book_levels_df → written to es_book_levels
# ===============================================================================

def assemble_book_features(
    bars_df: pd.DataFrame,
    book_events_df: pd.DataFrame,
    symbol: str,
    cfg,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    import book as bookmod

    if bars_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    bars       = bars_df.set_index("ts_recv").sort_index()
    bars       = bars.drop(columns=["_in_buffer"], errors="ignore")
    snap_index = bars.index

    log.info("[%s] Building book from %d events…", symbol, len(book_events_df))
    snaps = bookmod.build_snapshots(book_events_df, freq="1s") if not book_events_df.empty else []
    log.info("[%s] %d snapshots.", symbol, len(snaps))
    log.info("[%s] Computing book features over %d seconds…", symbol, len(bars))

    walls_df, book_levels_df = compute_walls(
        snaps, snap_index, cfg.TICK_SIZE,
        min_size_abs=getattr(cfg, "WALL_MIN_SIZE_ABS", 10),
        wall_multiplier=getattr(cfg, "WALL_MULTIPLIER", 3.0),
    )
    imbalance_df = compute_imbalance(
        snaps, snap_index, cfg.IMBALANCE_DEPTH_LEVELS, cfg.IMBALANCE_WINDOW_S,
    )

    price_ref = pd.DataFrame({
        "mid_price":        ((bars["high"] + bars["low"]) / 2).round(4),
        "last_trade_price": bars["close"],
    }, index=snap_index)

    result = price_ref.join(walls_df, how="left").join(imbalance_df, how="left")
    result.index.name = "ts"
    result.reset_index(inplace=True)
    result["symbol"] = symbol
    result["ts_ns"]  = result["ts"].astype("int64")

    for c in ["bid_wall_consumed", "ask_wall_consumed"]:
        if c in result.columns:
            result[c] = result[c].fillna(False)

    for c in ["bid_wall_ratio", "bid_wall_abs", "bid_wall_abs_log", "bid_wall_dist",
              "bid_wall_zone_ticks", "ask_wall_ratio", "ask_wall_abs", "ask_wall_abs_log",
              "ask_wall_dist", "ask_wall_zone_ticks", "wall_proximity",
              "bid_ask_imbalance", "imbalance_ema"]:
        if c in result.columns:
            result[c] = result[c].fillna(0.0)

    log.info("[%s] Book features: %d rows, %d wall zone level rows.",
             symbol, len(result), len(book_levels_df))
    return result, book_levels_df


# ===============================================================================
# ASSEMBLER — Stage 2  (trade-derived, fast, re-run freely)
#
# Merges onto existing es_swing_features rows that Stage 1 wrote.
# wall_features_df: Stage 1 output loaded from DB (needs bid/ask_wall_abs).
# book_levels_df:   loaded from es_book_levels.
# ===============================================================================

def assemble_trade_features(
    bars_df: pd.DataFrame,
    book_levels_df: pd.DataFrame,
    wall_features_df: pd.DataFrame,
    symbol: str,
    cfg,
) -> pd.DataFrame:
    if bars_df.empty:
        return pd.DataFrame()

    bars = bars_df.set_index("ts_recv").sort_index()
    bars = bars.drop(columns=["_in_buffer"], errors="ignore")

    log.info("[%s] Computing trade features over %d seconds…", symbol, len(bars))

    momentum_windows = getattr(cfg, "DELTA_MOMENTUM_WINDOWS", [30, 90])
    ema_spans        = getattr(cfg, "EXCESS_DOM_EMA_SPANS", [20, 60])

    absorption_df = compute_absorption(
        bars, cfg.TICK_SIZE, cfg.ABSORPTION_TIME_WINDOW_S,
        min_vol_gate=getattr(cfg, "ABSORPTION_MIN_VOL_GATE", 5),
    )
    delta_df = compute_delta(
        bars, cfg.DELTA_LOOKBACK_S, momentum_windows=momentum_windows,
    )

    # Ensure wall_features_df is indexed by ts for reindex calls inside compute_wall_hits
    wf = (wall_features_df.set_index("ts")
          if "ts" in wall_features_df.columns
          else wall_features_df)
    # Deduplicate — QuestDB WAL can produce duplicate timestamps at chunk boundaries
    if wf.index.duplicated().any():
        wf = wf[~wf.index.duplicated(keep="last")]

    # Deduplicate book_levels_df on (ts, side, price) — same root cause
    if not book_levels_df.empty:
        book_levels_df = book_levels_df.drop_duplicates(subset=["ts", "side", "price"])

    wall_hits_df = compute_wall_hits(
        bars, book_levels_df, wf, cfg.TICK_SIZE,
        hit_window_s=getattr(cfg, "WALL_HIT_WINDOW_S", 10),
    )
    vol_df = compute_volume_intensity(
        bars,
        window_s=getattr(cfg, "VOLUME_WINDOW_S", 10),
        excess_dom_ema_spans=ema_spans,
    )

    result = (absorption_df
              .join(delta_df,     how="left")
              .join(wall_hits_df, how="left")
              .join(vol_df,       how="left"))

    result.index.name = "ts"
    result.reset_index(inplace=True)
    result["symbol"] = symbol
    result["ts_ns"]  = result["ts"].astype("int64")

    if "delta_flip" in result.columns:
        result["delta_flip"] = result["delta_flip"].fillna(False)

    fill_cols = (
        ["bid_absorption", "ask_absorption", "cum_delta", "delta_velocity"]
        + [f"delta_momentum_{w}s" for w in momentum_windows]
        + ["bid_wall_hit_rate", "ask_wall_hit_rate",
           "roll_volume", "signed_dom", "excess_dom"]
        + [f"excess_dom_ema{s}" for s in ema_spans]
    )
    for c in fill_cols:
        if c in result.columns:
            result[c] = result[c].fillna(0.0)

    log.info("[%s] Trade features done: %d rows.", symbol, len(result))
    return result
