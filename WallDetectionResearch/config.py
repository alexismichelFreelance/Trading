"""
config.py — Central configuration for the ES swing detection pipeline.

Pipeline stages:
  Stage 1 (book)   — book replay, walls, imbalance. Expensive. Run once per date range.
  Stage 2 (trades) — absorption, delta, wall hits, volume. Cheap. Re-run freely.

Usage:
  python pipeline.py --symbol ESH5 --start 2025-02-18 --end 2025-03-21 --stage book
  python pipeline.py --symbol ESH5 --start 2025-02-18 --end 2025-03-21 --stage trades
  python pipeline.py --symbol ESH5 --start 2025-02-18 --end 2025-03-21  # both
"""

# ── QuestDB connection ────────────────────────────────────────────────────────
QUESTDB_HOST      = "localhost"
QUESTDB_HTTP_PORT = 9000
QUESTDB_ILP_PORT  = 9009

# ── Tables ────────────────────────────────────────────────────────────────────
MBO_TABLE         = "mbo_events"
FEATURE_TABLE     = "es_swing_features"
BOOK_LEVELS_TABLE = "es_book_levels"

# ── Symbols ───────────────────────────────────────────────────────────────────
SYMBOLS = ["ESH5", "ESM5"]

# ── Tick / contract ───────────────────────────────────────────────────────────
TICK_SIZE  = 0.25
POINT_SIZE = 50.0

# ── Feature parameters ────────────────────────────────────────────────────────

# Absorption — raw contracts/tick per side, no thresholds
ABSORPTION_TIME_WINDOW_S  = 10    # rolling window seconds
ABSORPTION_MIN_VOL_GATE   = 5     # zero-out floor (not a signal threshold)

# Delta
DELTA_LOOKBACK_S          = 60    # base rolling window for cum_delta (viz only)
DELTA_MOMENTUM_WINDOWS    = [30, 90]
#   delta_momentum_Ns = rolling_sum(net_delta, N) / rolling_std(net_delta, 60s)
#   Units: σ, clipped ±5.  Regime-independent directional pressure.  CNN inputs.

# Walls — relative anomaly detection, full book depth, no distance constraint
#   Wall zone = all levels where size > median(all levels >= WALL_MIN_SIZE_ABS)
#   No WALL_NEAR_TICKS — distance from mid is a feature, not a filter
WALL_MIN_SIZE_ABS  = 10    # noise floor only — not a signal threshold
WALL_MULTIPLIER    = 3.0   # wall zone = levels where size > median * WALL_MULTIPLIER

# Wall hits — aggressor volume hitting wall zone, normalised by wall size
WALL_HIT_WINDOW_S  = 10   # rolling window for hit rate computation

# Imbalance
IMBALANCE_DEPTH_LEVELS    = 5
IMBALANCE_WINDOW_S        = 5

# Volume intensity
VOLUME_WINDOW_S           = 10    # rolling window for roll_volume and excess_dom
EXCESS_DOM_EMA_SPANS      = [20, 60]   # EMA spans — CNN inputs

# ── Chunking ──────────────────────────────────────────────────────────────────
CHUNK_MINUTES      = 120   # pipeline processes this many minutes of data at once
BOOK_CHUNK_MINUTES = 30    # sub-chunk size for book event HTTP fetches
BOOK_SEED_MINUTES  = 10    # extra lookback to seed L3 book state
LOOKBACK_BUFFER_S  = 120   # extra trade bar seconds for rolling window warmup

# ── Visualiser ────────────────────────────────────────────────────────────────
VIZ_OUTPUT_DIR = "D:/Data/viz"
VIZ_AUTO_OPEN  = True
VIZ_THEME      = "dark"
VIZ_MAX_BARS   = 86400

# ── Confluence thresholds (visualiser only — not CNN features) ────────────────
POTENTIAL_MIN_CONFLUENCE = 3
ABSORPTION_POTENTIAL_MIN = 0.3
IMBALANCE_POTENTIAL_MIN  = 0.15
DELTA_VEL_POTENTIAL_MIN  = 20
WALL_PROX_POTENTIAL_MAX  = 10
GBT_THRESHOLD            = 0.5
