"""
validate.py — Offline validation of the feature pipeline.

Generates a tiny synthetic MBO event stream and runs the full pipeline
against it so you can verify correctness without a live QuestDB connection.

Run:
    python validate.py
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
import numpy as np
import pandas as pd

# ── Minimal stub config ────────────────────────────────────────────────────────
cfg = types.ModuleType("config")
cfg.TICK_SIZE                = 0.25
cfg.POINT_SIZE               = 50.0
cfg.ABSORPTION_PRICE_WINDOW  = 4
cfg.ABSORPTION_TIME_WINDOW_S = 10
cfg.ABSORPTION_MIN_VOLUME    = 100   # lower threshold for test data
cfg.DELTA_LOOKBACK_S         = 30
cfg.WALL_DEPTH_LEVELS        = 5
cfg.WALL_MIN_SIZE            = 50    # lower for test
cfg.WALL_CLUSTER_TICKS       = 2
cfg.IMBALANCE_DEPTH_LEVELS   = 3
cfg.IMBALANCE_WINDOW_S       = 5
cfg.SWEEP_LOOKBACK_S         = 3
cfg.SWEEP_MIN_TICKS          = 2
cfg.SWEEP_RETURN_TICKS       = 1
cfg.BUBBLE_WINDOW_S          = 2
cfg.BUBBLE_MIN_VOLUME        = 100
sys.modules["config"] = cfg


def build_synthetic_mbo(
    base_price: float = 5750.0,
    n_seconds:  int   = 120,
    seed:       int   = 42,
) -> pd.DataFrame:
    """Generate a realistic-looking MBO stream with known features."""
    rng   = np.random.default_rng(seed)
    start = pd.Timestamp("2025-02-19 14:30:00", tz="UTC")
    rows  = []
    price = base_price
    seq   = 1

    # Pre-populate the book with resting orders
    for side, prices in [
        ("B", np.arange(base_price - 0.25, base_price - 5.25, -0.25)),
        ("A", np.arange(base_price + 0.25, base_price + 5.25,  0.25)),
    ]:
        for p in prices:
            size = int(rng.integers(5, 100))
            rows.append({
                "ts_recv":  start,
                "action":   "A",
                "side":     side,
                "price":    round(p, 2),
                "size":     size,
                "order_id": seq,
                "sequence": seq,
                "_in_buffer": False,
            })
            seq += 1

    # Inject absorption episode at t=30s (heavy selling, price holds)
    t_absorb = start + pd.Timedelta(seconds=30)
    for i in range(20):
        ts = t_absorb + pd.Timedelta(milliseconds=i * 400)
        rows.append({
            "ts_recv":  ts,
            "action":   "T",
            "side":     "B",   # resting bid hit → seller aggressor
            "price":    base_price,
            "size":     10,
            "order_id": seq,
            "sequence": seq,
            "_in_buffer": False,
        })
        seq += 1

    # Inject delta flip at t=60s
    t_flip = start + pd.Timedelta(seconds=60)
    for i in range(15):
        ts = t_flip + pd.Timedelta(milliseconds=i * 300)
        rows.append({
            "ts_recv":  ts,
            "action":   "T",
            "side":     "A",   # resting ask hit → buyer aggressor
            "price":    base_price + 0.25,
            "size":     15,
            "order_id": seq,
            "sequence": seq,
            "_in_buffer": False,
        })
        seq += 1

    # Inject sweep at t=90s (price spikes up then snaps back)
    t_sweep = start + pd.Timedelta(seconds=90)
    sweep_prices = [5750.0, 5750.5, 5751.0, 5751.5, 5751.0, 5750.5, 5750.0]
    for i, sp in enumerate(sweep_prices):
        ts = t_sweep + pd.Timedelta(milliseconds=i * 300)
        rows.append({
            "ts_recv":  ts,
            "action":   "T",
            "side":     "A",
            "price":    sp,
            "size":     20,
            "order_id": seq,
            "sequence": seq,
            "_in_buffer": False,
        })
        seq += 1

    df = pd.DataFrame(rows)
    df["ts_recv"] = pd.to_datetime(df["ts_recv"], utc=True)
    df["price"]   = df["price"].astype(float)
    df["size"]    = df["size"].astype(int)
    df["order_id"]= df["order_id"].astype(int)
    return df.sort_values("ts_recv").reset_index(drop=True)


def run_validation():
    print("=" * 60)
    print("Swing Detection Feature Pipeline — Offline Validation")
    print("=" * 60)

    from features import assemble_features

    df = build_synthetic_mbo()
    print(f"\nSynthetic MBO events: {len(df)} rows")
    print(f"  Actions: {df['action'].value_counts().to_dict()}")
    print(f"  Time range: {df['ts_recv'].min()} → {df['ts_recv'].max()}")

    print("\nRunning assemble_features()...")
    features = assemble_features(df, symbol="ESH5", cfg=cfg)

    print(f"\nFeature rows: {len(features)}")
    print("\nColumn summary:")
    for col in features.columns:
        if col in ("ts", "symbol", "ts_ns"):
            continue
        series = features[col].dropna()
        if series.empty:
            print(f"  {col:<28} [empty]")
        elif series.dtype == object or series.dtype.name == "bool":
            print(f"  {col:<28} values={series.unique()[:5].tolist()}")
        else:
            print(f"  {col:<28} min={series.min():.3f}  max={series.max():.3f}  "
                  f"mean={series.mean():.3f}")

    # ── Assertions ────────────────────────────────────────────────────────────
    print("\nRunning assertions...")
    errors = []

    # Absorption: should see non-zero score around t=30s
    t_absorb = pd.Timestamp("2025-02-19 14:30:30", tz="UTC")
    nearby = features[
        (features["ts"] >= t_absorb - pd.Timedelta(seconds=5)) &
        (features["ts"] <= t_absorb + pd.Timedelta(seconds=15))
    ]
    if not (nearby["absorption_score"] > 0).any():
        errors.append("FAIL: No absorption score detected around t=30s")
    else:
        print("  ✓ Absorption score detected at sell-heavy episode")

    # Delta: should see negative delta around t=30s (sellers dominating)
    if not (nearby["cum_delta"] < 0).any():
        errors.append("FAIL: Expected negative delta at t=30s absorption episode")
    else:
        print("  ✓ Negative delta at absorption episode")

    # Delta flip: should see flip around t=60s
    t_flip = pd.Timestamp("2025-02-19 14:31:00", tz="UTC")
    flip_window = features[
        (features["ts"] >= t_flip) &
        (features["ts"] <= t_flip + pd.Timedelta(seconds=30))
    ]
    if not flip_window["delta_flip"].any():
        errors.append("FAIL: No delta flip detected around t=60s")
    else:
        print("  ✓ Delta flip detected at sign change")

    # Sweep: should flag around t=90s
    t_sweep = pd.Timestamp("2025-02-19 14:31:30", tz="UTC")
    sweep_window = features[
        (features["ts"] >= t_sweep - pd.Timedelta(seconds=3)) &
        (features["ts"] <= t_sweep + pd.Timedelta(seconds=10))
    ]
    if not sweep_window["sweep_detected"].any():
        errors.append("FAIL: No sweep detected around t=90s spike")
    else:
        print("  ✓ Sweep detected at price spike / snap-back")

    # Imbalance: should always be in [-1, 1]
    imb = features["bid_ask_imbalance"].dropna()
    if (imb.abs() > 1.0).any():
        errors.append("FAIL: bid_ask_imbalance out of [-1, 1] range")
    else:
        print("  ✓ bid_ask_imbalance within valid range")

    print()
    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print("All assertions passed ✓")
        print("\nSample feature rows (first 5):")
        pd.set_option("display.max_columns", None)
        pd.set_option("display.width", 200)
        print(features.head(5).to_string(index=False))


if __name__ == "__main__":
    run_validation()
