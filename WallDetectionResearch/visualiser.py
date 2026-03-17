"""
visualiser.py — ES Swing Detection Dashboard.

8 panels showing every CNN input feature plus its raw companion:
  1. Price + walls (top-1% levels from es_book_levels, server-side filtered)
  2. Wall ratio + wall dist (bid/ask_wall_ratio, bid/ask_wall_dist)
  3. Wall abs log (CNN: bid/ask_wall_abs_log) — raw on secondary right axis
  4. Absorption (bid/ask_absorption) + EMA 20/60
  5. Delta — delta_velocity bars + EMA overlays + momentum 30s/90s lines
  6. Wall Hit Rate — raw (faint) + EMA 10s/30s overlays (CNN: bid/ask_wall_hit_rate)
  7. Imbalance — raw bars (faint) + imbalance_ema dominant line
  8. Volume / DOM (roll_volume + excess_dom raw + ema20/60)

Subsampling strategy:
  Full day loaded from DB. Features are resampled to 1-minute OHLCV bars
  (open/high/low/close for price; mean for most signals; sum for volume) giving
  ~1440 bars — full day, full structure, fast render.
  Wall levels are NOT resampled — they are pre-aggregated into persistent
  horizontal segments server-side (top 1% by size).
"""

from __future__ import annotations

import logging
import webbrowser
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

STATE_COLOURS = {
    "NEUTRAL":     "rgba(100,100,120,0.10)",
    "POTENTIAL":   "rgba(255,200,0,0.18)",
    "CONFIRMED":   "rgba(0,200,100,0.18)",
    "EXHAUSTED":   "rgba(0,150,255,0.18)",
    "INVALIDATED": "rgba(255,60,60,0.18)",
}
TRANSITION_MARKER = {
    "POTENTIAL":   ("triangle-up",  "gold",       14),
    "CONFIRMED":   ("star",         "lime",        16),
    "EXHAUSTED":   ("diamond",      "deepskyblue", 14),
    "INVALIDATED": ("x",            "red",         12),
}


# ─────────────────────────────────────────────────────────────────────────────
# QuestDB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch(sql: str) -> pd.DataFrame:
    import requests
    from config import QUESTDB_HOST, QUESTDB_HTTP_PORT
    url = f"http://{QUESTDB_HOST}:{QUESTDB_HTTP_PORT}/exp"
    try:
        r = requests.get(url, params={"query": sql, "limit": "0,2000000"}, timeout=60)
        r.raise_for_status()
        from io import StringIO
        df = pd.read_csv(StringIO(r.text))
        if "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
        return df
    except Exception as exc:
        log.warning("QuestDB fetch failed: %s", exc)
        return pd.DataFrame()


def _load_features(date: str, symbol: str) -> pd.DataFrame:
    """
    Load 1s features for one full day.
    Stage 1 + Stage 2 wrote separate rows per timestamp — deduplicate with
    groupby.last() which keeps the last non-null value per column per second.
    """
    df = _fetch(f"""
        SELECT ts, mid_price, last_trade_price,
               bid_absorption, ask_absorption,
               cum_delta, delta_flip, delta_velocity,
               delta_momentum_30s, delta_momentum_90s,
               bid_wall_ratio, bid_wall_abs, bid_wall_abs_log, bid_wall_dist,
               bid_wall_zone_ticks,
               ask_wall_ratio, ask_wall_abs, ask_wall_abs_log, ask_wall_dist,
               ask_wall_zone_ticks,
               wall_proximity, bid_wall_consumed, ask_wall_consumed,
               bid_ask_imbalance, imbalance_ema,
               bid_wall_hit_rate, ask_wall_hit_rate,
               roll_volume, signed_dom, excess_dom,
               excess_dom_ema20, excess_dom_ema60
        FROM es_swing_features
        WHERE ts >= '{date}T00:00:00.000000Z'
          AND ts <  '{date}T23:59:59.999999Z'
          AND symbol = '{symbol}'
        ORDER BY ts
    """)
    if df.empty:
        return df

    # Coalesce Stage 1 + Stage 2 duplicate rows
    df = df.groupby("ts", sort=True).last().reset_index()
    log.info("Features: %d 1s rows after dedup (mid_price nulls: %d)",
             len(df), df["mid_price"].isna().sum())
    return df


def _resample_to_1min(df: pd.DataFrame) -> pd.DataFrame:
    """
    Resample 1-second feature rows to 1-minute bars.

    Price: OHLC from mid_price (close = last mid in the minute).
    Signals: mean per minute — preserves the shape without spike noise.
    Volume: sum per minute.
    Boolean flags (delta_flip, bid/ask_wall_consumed): any() per minute
      — True if the event fired at any second in that minute.

    Returns a plain DataFrame with a 'ts' column (tz-naive, minute-aligned).
    """
    if df.empty:
        return df

    # Work on a UTC-indexed copy
    d = df.set_index("ts").sort_index()

    price_col = "mid_price" if "mid_price" in d.columns else "last_trade_price"

    # ── Price OHLC ────────────────────────────────────────────────────────────
    price_ohlc = d[price_col].resample("1min").ohlc()
    price_ohlc.columns = ["open", "high", "low", "close"]

    # ── Signal columns: mean ──────────────────────────────────────────────────
    mean_cols = [
        "bid_absorption", "ask_absorption",
        "cum_delta", "delta_velocity",
        "delta_momentum_30s", "delta_momentum_90s",
        "bid_wall_ratio", "bid_wall_abs", "bid_wall_abs_log", "bid_wall_dist",
        "bid_wall_zone_ticks",
        "ask_wall_ratio", "ask_wall_abs", "ask_wall_abs_log", "ask_wall_dist",
        "ask_wall_zone_ticks",
        "wall_proximity",
        "bid_ask_imbalance", "imbalance_ema",
        "bid_wall_hit_rate", "ask_wall_hit_rate",
        "signed_dom", "excess_dom", "excess_dom_ema20", "excess_dom_ema60",
    ]
    mean_cols = [c for c in mean_cols if c in d.columns]
    mean_df = d[mean_cols].resample("1min").mean()

    # ── Volume: sum ───────────────────────────────────────────────────────────
    sum_cols = ["roll_volume"]
    sum_cols = [c for c in sum_cols if c in d.columns]
    sum_df = d[sum_cols].resample("1min").sum()

    # ── Boolean flags: any ────────────────────────────────────────────────────
    bool_cols = ["delta_flip", "bid_wall_consumed", "ask_wall_consumed"]
    bool_cols = [c for c in bool_cols if c in d.columns]
    bool_df = d[bool_cols].astype(float).resample("1min").max().astype(bool)

    # ── Assemble ──────────────────────────────────────────────────────────────
    out = pd.concat([price_ohlc, mean_df, sum_df, bool_df], axis=1)
    # Drop minutes with no price data at all
    out = out[out["close"].notna()].copy()
    out.index.name = "ts"
    out = out.reset_index()
    # Strip timezone — Plotly silently renders nothing with tz-aware timestamps
    out["ts"] = out["ts"].dt.tz_localize(None)

    log.info("Resampled to %d 1-minute bars.", len(out))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Wall rendering
# ─────────────────────────────────────────────────────────────────────────────

def _load_wall_levels(date: str, symbol: str) -> pd.DataFrame:
    """
    Load all rows from es_book_levels for the day.
    Every row in this table already passed the wall-zone filter in features.py
    (size > median * wall_multiplier) so no further size filtering is applied here.
    Returns DataFrame with columns: ts (UTC), side, price, size.
    """
    from config import QUESTDB_HOST, QUESTDB_HTTP_PORT
    import requests
    from io import StringIO

    sql = f"""
        SELECT ts, side, price, size
        FROM es_book_levels
        WHERE ts >= '{date}T00:00:00.000000Z'
          AND ts <  '{date}T23:59:59.999999Z'
          AND symbol = '{symbol}'
        ORDER BY price, side, ts
    """
    try:
        r = requests.get(
            f"http://{QUESTDB_HOST}:{QUESTDB_HTTP_PORT}/exp",
            params={"query": sql},
            timeout=120,
        )
        r.raise_for_status()
        df = pd.read_csv(StringIO(r.text))
        if df.empty:
            log.warning("No wall levels found for %s %s", symbol, date)
            return pd.DataFrame()
        df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
        log.info("Wall levels loaded: %d rows for %s %s", len(df), symbol, date)
        return df
    except Exception as exc:
        log.warning("Wall levels fetch failed: %s", exc)
        return pd.DataFrame()


def _add_walls(fig, feat: pd.DataFrame, row: int,
               wall_levels: pd.DataFrame = None) -> None:
    """
    Render wall levels on the price panel as horizontal lines.

    For each (side, price) group, consecutive 1-second appearances are merged
    into a single horizontal segment. A gap of more than 5 seconds between two
    appearances of the same price level starts a new segment.

    Opacity encodes size relative to p95 of all visible levels so larger walls
    are visually more prominent.
    """
    import plotly.graph_objects as go

    if wall_levels is None or wall_levels.empty:
        return

    lvl = wall_levels.copy()
    lvl["ts"] = lvl["ts"].dt.tz_localize(None)

    p95_all = float(lvl["size"].quantile(0.95)) or 1.0

    for side_code, r, g, b, label in [
        ("B", 0,   220, 100, "Bid wall"),
        ("A", 255, 70,  70,  "Ask wall"),
    ]:
        side_df = lvl[lvl["side"] == side_code].copy()
        if side_df.empty:
            continue

        # Group by price, sort by time, find gaps > 5s → new segment
        side_df = side_df.sort_values(["price", "ts"])
        side_df["gap"] = (
            side_df.groupby("price")["ts"]
            .diff().dt.total_seconds().fillna(0) > 5
        ).astype(int)
        side_df["seg"] = side_df.groupby("price")["gap"].cumsum()

        segs = (side_df.groupby(["price", "seg"])
                .agg(t0=("ts", "min"), t1=("ts", "max"), size=("size", "mean"))
                .reset_index())

        # Build one trace per side — all segments batched with None separators
        x_vals, y_vals, opacities, widths = [], [], [], []
        for _, s in segs.iterrows():
            op = min(0.95, 0.25 + 0.70 * (s["size"] / p95_all))
            lw = min(4.0,  1.0  + 3.0  * (s["size"] / p95_all))
            x_vals  += [s["t0"], s["t1"], None]
            y_vals  += [s["price"], s["price"], None]
            opacities.append(round(op, 2))
            widths.append(round(lw, 2))

        if not x_vals:
            continue

        # Use median opacity/width for the trace — per-point styling needs
        # separate traces which is too slow at this scale
        med_op = float(np.median(opacities))
        med_lw = float(np.median(widths))

        fig.add_trace(go.Scatter(
            x=x_vals, y=y_vals,
            mode="lines",
            line=dict(color=f"rgba({r},{g},{b},{med_op:.2f})", width=med_lw),
            name=label,
            showlegend=True,
            connectgaps=False,
            hovertemplate="%{x|%H:%M}  " + label + "=%{y:.2f}<extra></extra>",
        ), row=row, col=1)
# ─────────────────────────────────────────────────────────────────────────────
# Figure builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_figure(
    feat_1s: pd.DataFrame,
    date: str,
    symbol: str,
    annotated: Optional[pd.DataFrame] = None,
    wall_levels: Optional[pd.DataFrame] = None,
) -> "go.Figure":
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        raise ImportError("pip install plotly")

    from config import VIZ_THEME

    dark   = VIZ_THEME == "dark"
    bg     = "#0e1117" if dark else "#ffffff"
    paper  = "#161b22" if dark else "#f8f9fa"
    fg     = "#e0e0e0" if dark else "#111111"
    grid_c = "rgba(255,255,255,0.06)" if dark else "rgba(0,0,0,0.07)"

    # ── Resample 1s → 1min ───────────────────────────────────────────────────
    feat = _resample_to_1min(feat_1s)
    if feat.empty:
        raise ValueError("No data after resampling.")

    ts = feat["ts"]

    has_fsm = annotated is not None and "fsm_state" in annotated.columns

    # Default x-range: show full day (all 1440 bars)
    d0 = ts.iloc[0]
    d1 = ts.iloc[-1]
    rx0 = d0.strftime("%Y-%m-%dT%H:%M:%S")
    rx1 = d1.strftime("%Y-%m-%dT%H:%M:%S")

    # RTH mask for y-range initialisation (13:30–21:00 UTC)
    rth0 = pd.Timestamp(f"{date}T13:30:00")
    rth1 = pd.Timestamp(f"{date}T21:00:00")
    rth_mask = (feat["ts"] >= rth0) & (feat["ts"] <= rth1)
    feat_rth  = feat[rth_mask] if rth_mask.any() else feat

    def _yrange(series, pad=0.05):
        v = series.dropna()
        if len(v) == 0:
            return None
        lo, hi = float(v.quantile(0.01)), float(v.quantile(0.99))
        if lo == hi:
            lo, hi = lo - 1, hi + 1
        span = hi - lo
        return [lo - span * pad, hi + span * pad]

    # ── 8-panel layout ────────────────────────────────────────────────────────
    fig = make_subplots(
        rows=8, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.012,
        row_heights=[0.22, 0.10, 0.10, 0.12, 0.12, 0.10, 0.10, 0.14],
        specs=[
            [{"secondary_y": False}],  # 1 price
            [{"secondary_y": True}],   # 2 wall ratio + dist right
            [{"secondary_y": True}],   # 3 wall abs log left, raw right
            [{"secondary_y": False}],  # 4 absorption + EMAs
            [{"secondary_y": True}],   # 5 delta velocity/momentum left, cum_delta right
            [{"secondary_y": False}],  # 6 wall hit rate + EMAs
            [{"secondary_y": False}],  # 7 imbalance bars + EMA
            [{"secondary_y": True}],   # 8 volume + excess_dom right
        ],
        subplot_titles=[
            f"<b>{symbol}  {date}</b>  —  Price  +  Walls  (1-min bars, full day)",
            "Wall Ratio  (CNN: bid/ask_wall_ratio)  +  Wall Dist right axis  (CNN: bid/ask_wall_dist)",
            "Wall Abs log  (CNN: bid/ask_wall_abs_log)  —  raw on right axis",
            "Absorption  (CNN: bid/ask_absorption)  +  EMA 20 / 60",
            "Delta  —  velocity bars  +  momentum 30s/90s  (CNN inputs)  |  cum_delta right axis",
            "Wall Hit Rate  (CNN: bid/ask_wall_hit_rate)  +  EMA 10 / 30",
            "Imbalance  —  raw bars (faint)  +  EMA dominant  (CNN: bid_ask_imbalance)",
            "Volume  +  Excess DOM  raw/ema20/ema60  (CNN: excess_dom_ema20/60)",
        ],
    )

    # ── PANEL 1 — Price + walls ───────────────────────────────────────────────
    price_col = "close"  # OHLC close from resampled mid_price
    fig.add_trace(go.Scatter(
        x=ts, y=feat[price_col], mode="lines",
        line=dict(color="#4fc3f7", width=1.2), name="Mid price (close)",
        hovertemplate="%{x|%H:%M}  %{y:.2f}<extra></extra>",
    ), row=1, col=1)

    _add_walls(fig, feat, row=1, wall_levels=wall_levels)

    for consumed_col, colour, label in [
        ("bid_wall_consumed", "rgba(0,255,120,0.9)",  "Bid wall consumed"),
        ("ask_wall_consumed", "rgba(255,80,80,0.9)",  "Ask wall consumed"),
    ]:
        if consumed_col in feat.columns:
            hit = feat[feat[consumed_col].astype(bool)]
            if not hit.empty:
                fig.add_trace(go.Scatter(
                    x=hit["ts"], y=feat.loc[hit.index, price_col],
                    mode="markers",
                    marker=dict(symbol="x", color=colour, size=10,
                                line=dict(width=1.5, color="white")),
                    name=label,
                    hovertemplate="%{x|%H:%M}  " + label + "<extra></extra>",
                ), row=1, col=1)

    if has_fsm:
        _add_state_bands(fig, annotated, ts, row=1)

    # ── PANEL 2 — Wall ratio (primary) + wall dist (secondary right) ──────────
    for src, colour_rgb, label in [
        ("bid_wall_ratio", "0,230,120",   "Bid ratio"),
        ("ask_wall_ratio", "255,100,100", "Ask ratio"),
    ]:
        if src in feat.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=feat[src].fillna(0), mode="lines",
                line=dict(color=f"rgba({colour_rgb},0.9)", width=1.2),
                name=label,
                hovertemplate="%{x|%H:%M}  " + label + "=%{y:.1f}×<extra></extra>",
            ), row=2, col=1, secondary_y=False)

    fig.add_hline(y=1.0, line=dict(color="rgba(255,255,255,0.12)", dash="dot", width=1), row=2, col=1)
    fig.add_hline(y=3.0, line=dict(color="rgba(255,215,0,0.25)", dash="dash", width=1),
                  annotation_text="3×", annotation_font=dict(color="rgba(255,215,0,0.5)", size=9),
                  row=2, col=1)

    for src, colour_rgb, label in [
        ("bid_wall_dist", "0,230,120",   "Bid dist"),
        ("ask_wall_dist", "255,100,100", "Ask dist"),
    ]:
        if src in feat.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=feat[src].fillna(0), mode="lines",
                line=dict(color=f"rgba({colour_rgb},0.40)", width=0.8, dash="dot"),
                name=label + " (ticks)",
                hovertemplate="%{x|%H:%M}  " + label + "=%{y:.0f}t<extra></extra>",
            ), row=2, col=1, secondary_y=True)

    # ── PANEL 3 — Wall abs: log (CNN) on left, raw on right ───────────────────
    # Log-scaled CNN input dominates the left axis — it's what the model sees.
    # Raw absolute size on right axis gives context for the log transform.
    for src_log, src_raw, colour_rgb, label in [
        ("bid_wall_abs_log", "bid_wall_abs", "0,230,120",   "Bid abs"),
        ("ask_wall_abs_log", "ask_wall_abs", "255,100,100", "Ask abs"),
    ]:
        if src_log in feat.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=feat[src_log].fillna(0), mode="lines",
                line=dict(color=f"rgba({colour_rgb},0.90)", width=1.4),
                name=f"{label} log (CNN)",
                hovertemplate="%{x|%H:%M}  " + label + "_log=%{y:.2f}<extra></extra>",
            ), row=3, col=1, secondary_y=False)
        if src_raw in feat.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=feat[src_raw].fillna(0), mode="lines",
                line=dict(color=f"rgba({colour_rgb},0.20)", width=0.8),
                name=f"{label} raw",
                hovertemplate="%{x|%H:%M}  " + label + "_raw=%{y:.0f}<extra></extra>",
            ), row=3, col=1, secondary_y=True)

    # ── PANEL 4 — Absorption + EMA 20 / 60 ────────────────────────────────────
    for src, colour_rgb, label in [
        ("bid_absorption", "0,230,120",   "Bid abs"),
        ("ask_absorption", "255,100,100", "Ask abs"),
    ]:
        if src not in feat.columns:
            continue
        raw = feat[src].fillna(0)
        # Raw signal — very faint so EMAs stand out
        fig.add_trace(go.Scatter(
            x=ts, y=raw, mode="lines",
            line=dict(color=f"rgba({colour_rgb},0.20)", width=0.8),
            name=f"{label} raw",
            hovertemplate="%{x|%H:%M}  " + label + "=%{y:.2f}<extra></extra>",
        ), row=4, col=1)
        # EMA 20
        ema20 = raw.ewm(span=20, adjust=False).mean()
        fig.add_trace(go.Scatter(
            x=ts, y=ema20, mode="lines",
            line=dict(color=f"rgba({colour_rgb},0.65)", width=1.2),
            name=f"{label} EMA20",
            hovertemplate="%{x|%H:%M}  " + label + "_ema20=%{y:.2f}<extra></extra>",
        ), row=4, col=1)
        # EMA 60
        ema60 = raw.ewm(span=60, adjust=False).mean()
        fig.add_trace(go.Scatter(
            x=ts, y=ema60, mode="lines",
            line=dict(color=f"rgba({colour_rgb},0.95)", width=1.8),
            name=f"{label} EMA60",
            hovertemplate="%{x|%H:%M}  " + label + "_ema60=%{y:.2f}<extra></extra>",
        ), row=4, col=1)

    fig.add_hline(y=0, line=dict(color="rgba(255,255,255,0.08)", dash="dot", width=1), row=4, col=1)

    # ── PANEL 5 — Delta: velocity bars + momentum lines / cum_delta right ─────
    # Left axis: delta_velocity bars (CNN input) + momentum 30s/90s (normalised)
    # Right axis: cum_delta raw — very different scale, kept separate
    if "cum_delta" in feat.columns:
        fig.add_trace(go.Scatter(
            x=ts, y=feat["cum_delta"].fillna(0), mode="lines",
            line=dict(color="rgba(128,203,196,0.25)", width=0.8),
            name="Cum delta (raw)",
            hovertemplate="%{x|%H:%M}  cum=%{y:.0f}<extra></extra>",
        ), row=5, col=1, secondary_y=True)

    if "delta_velocity" in feat.columns:
        dv = feat["delta_velocity"].fillna(0)
        colours_dv = np.where(dv >= 0, "rgba(100,220,120,0.45)", "rgba(255,100,100,0.45)")
        fig.add_trace(go.Bar(
            x=ts, y=dv, marker_color=colours_dv,
            name="Delta velocity (CNN)",
            hovertemplate="%{x|%H:%M}  vel=%{y:.1f}<extra></extra>",
        ), row=5, col=1, secondary_y=False)
        # EMA overlays on velocity so you can see the trend through the noise
        for span, opacity, lw in [(10, 0.55, 1.0), (30, 0.80, 1.6), (90, 1.00, 2.2)]:
            ema_dv = dv.ewm(span=span, adjust=False).mean()
            fig.add_trace(go.Scatter(
                x=ts, y=ema_dv, mode="lines",
                line=dict(color=f"rgba(100,220,120,{opacity})", width=lw),
                name=f"Vel EMA{span}",
                hovertemplate=f"%{{x|%H:%M}}  vel_ema{span}=%{{y:.1f}}<extra></extra>",
            ), row=5, col=1, secondary_y=False)

    for col, colour, lw, label in [
        ("delta_momentum_30s", "rgba(255,210,80,0.70)", 1.2, "Δmom 30s (CNN)"),
        ("delta_momentum_90s", "rgba(255,210,80,1.00)", 1.8, "Δmom 90s (CNN)"),
    ]:
        if col in feat.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=feat[col].fillna(0), mode="lines",
                line=dict(color=colour, width=lw),
                name=label,
                hovertemplate="%{x|%H:%M}  " + label + "=%{y:.2f}σ<extra></extra>",
            ), row=5, col=1, secondary_y=False)

    if "delta_flip" in feat.columns:
        flips = feat[feat["delta_flip"].astype(bool)]
        if not flips.empty:
            dv_at_flip = (feat["delta_velocity"].reindex(flips.index).fillna(0)
                          if "delta_velocity" in feat.columns
                          else pd.Series(0, index=flips.index))
            fig.add_trace(go.Scatter(
                x=flips["ts"], y=dv_at_flip, mode="markers",
                marker=dict(symbol="triangle-up", color="gold", size=8,
                            line=dict(width=0.5, color="black")),
                name="Delta flip",
                hovertemplate="%{x|%H:%M}  flip<extra></extra>",
            ), row=5, col=1, secondary_y=False)

    fig.add_hline(y=0, line=dict(color="rgba(128,203,196,0.15)", dash="dot", width=1), row=5, col=1)

    # ── PANEL 6 — Wall hit rate: raw (faint) + EMA 10 / 30 ───────────────────
    # The goal is to see aggression building (rising EMA), wall holding or
    # collapsing. Raw 1s signal (even after 1min mean) is noisy — EMAs carry
    # the story.
    for src, colour_rgb, label in [
        ("bid_wall_hit_rate", "0,230,120",   "Bid hit"),
        ("ask_wall_hit_rate", "255,100,100", "Ask hit"),
    ]:
        if src not in feat.columns:
            continue
        raw = feat[src].fillna(0)
        # Raw — very faint background
        fig.add_trace(go.Scatter(
            x=ts, y=raw, mode="lines",
            line=dict(color=f"rgba({colour_rgb},0.15)", width=0.8),
            name=f"{label} raw",
            hovertemplate="%{x|%H:%M}  " + label + "=%{y:.3f}<extra></extra>",
        ), row=6, col=1)
        # EMA 10 — fast (shows immediate pressure)
        ema10 = raw.ewm(span=10, adjust=False).mean()
        fig.add_trace(go.Scatter(
            x=ts, y=ema10, mode="lines",
            line=dict(color=f"rgba({colour_rgb},0.65)", width=1.2),
            name=f"{label} EMA10",
            hovertemplate="%{x|%H:%M}  " + label + "_ema10=%{y:.3f}<extra></extra>",
        ), row=6, col=1)
        # EMA 30 — slower trend
        ema30 = raw.ewm(span=30, adjust=False).mean()
        fig.add_trace(go.Scatter(
            x=ts, y=ema30, mode="lines",
            line=dict(color=f"rgba({colour_rgb},0.95)", width=1.8),
            name=f"{label} EMA30",
            hovertemplate="%{x|%H:%M}  " + label + "_ema30=%{y:.3f}<extra></extra>",
        ), row=6, col=1)

    # Fixed range [0, 1] — hit rate is bounded; auto-range would exaggerate quiet periods
    fig.update_yaxes(range=[0, 1.05], autorange=False, row=6, col=1)
    fig.add_hline(y=1.0, line=dict(color="rgba(255,255,255,0.12)", dash="dot", width=1),
                  annotation_text="1.0", row=6, col=1,
                  annotation_font=dict(color="rgba(255,255,255,0.3)", size=8))

    # ── PANEL 7 — Imbalance: faint raw bars + dominant EMA line ───────────────
    # Y-range keyed to the EMA, not the raw — raw spikes can be ×10 the EMA
    # and would crush the scale.
    if "bid_ask_imbalance" in feat.columns:
        raw_imb = feat["bid_ask_imbalance"].fillna(0)
        fig.add_trace(go.Bar(
            x=ts, y=raw_imb,
            marker_color=np.where(raw_imb >= 0,
                                   "rgba(100,220,120,0.12)",
                                   "rgba(255,100,100,0.12)"),
            name="Imbalance raw",
            hovertemplate="%{x|%H:%M}  imb=%{y:.4f}<extra></extra>",
        ), row=7, col=1)

    if "imbalance_ema" in feat.columns:
        ema_imb = feat["imbalance_ema"].fillna(0)
        fig.add_trace(go.Scatter(
            x=ts, y=ema_imb, mode="lines",
            line=dict(color="rgba(255,210,60,0.95)", width=1.8),
            name="Imbalance EMA (CNN)",
            hovertemplate="%{x|%H:%M}  imb_ema=%{y:.4f}<extra></extra>",
        ), row=7, col=1)

    fig.add_hline(y=0, line=dict(color="rgba(255,210,60,0.12)", dash="dot", width=1), row=7, col=1)

    # ── PANEL 8 — Volume + excess DOM ─────────────────────────────────────────
    if "roll_volume" in feat.columns:
        rv = feat["roll_volume"].fillna(0)
        ed = feat.get("excess_dom", pd.Series(0, index=feat.index)).fillna(0)
        colours_rv = np.where(ed >= 0, "rgba(100,220,120,0.45)", "rgba(255,100,100,0.45)")
        fig.add_trace(go.Bar(
            x=ts, y=rv, marker_color=colours_rv,
            name="Roll volume",
            hovertemplate="%{x|%H:%M}  vol=%{y:.0f}<extra></extra>",
        ), row=8, col=1, secondary_y=False)

    for col, colour, lw, label in [
        ("excess_dom",       "rgba(255,210,80,0.20)", 0.8, "Excess DOM raw"),
        ("excess_dom_ema20", "rgba(255,210,80,0.70)", 1.2, "Excess DOM ema20 (CNN)"),
        ("excess_dom_ema60", "rgba(255,210,80,1.00)", 1.8, "Excess DOM ema60 (CNN)"),
    ]:
        if col in feat.columns:
            fig.add_trace(go.Scatter(
                x=ts, y=feat[col].fillna(0), mode="lines",
                line=dict(color=colour, width=lw),
                name=label,
                hovertemplate="%{x|%H:%M}  " + label + "=%{y:.3f}<extra></extra>",
            ), row=8, col=1, secondary_y=True)

    fig.add_hline(y=0, line=dict(color="rgba(255,210,80,0.15)", dash="dot", width=1), row=8, col=1)

    # ── Layout ────────────────────────────────────────────────────────────────
    ax_common = dict(
        showgrid=True, gridcolor=grid_c, zeroline=False,
        color=fg, tickfont=dict(size=9, color=fg),
        fixedrange=False, autorange=True,
        showspikes=True, spikecolor="rgba(200,200,200,0.5)",
        spikethickness=1, spikedash="dot", spikemode="across",
    )
    yax_common = dict(
        showgrid=True, gridcolor=grid_c, zeroline=False,
        color=fg, tickfont=dict(size=9, color=fg),
        fixedrange=False,
        showspikes=True, spikecolor="rgba(200,200,200,0.25)",
        spikethickness=1, spikedash="dot",
    )

    range_buttons = dict(
        buttons=[
            dict(count=1,  label="1h",  step="hour", stepmode="backward"),
            dict(count=2,  label="2h",  step="hour", stepmode="backward"),
            dict(count=4,  label="4h",  step="hour", stepmode="backward"),
            dict(count=8,  label="8h",  step="hour", stepmode="backward"),
            dict(step="all", label="All"),
        ],
        bgcolor="rgba(40,44,60,0.9)" if dark else "rgba(220,222,230,0.9)",
        activecolor="rgba(79,195,247,0.9)",
        font=dict(color=fg, size=9),
    )

    fig.update_layout(
        height=1900,
        paper_bgcolor=paper,
        plot_bgcolor=bg,
        font=dict(color=fg, size=10),
        hovermode="x",
        hoverdistance=5,
        spikedistance=-1,
        dragmode="zoom",
        uirevision="constant",
        legend=dict(
            orientation="v", x=1.08, y=1.0,
            xanchor="left", yanchor="top",
            bgcolor="rgba(0,0,0,0.0)",
            font=dict(size=8),
            itemclick="toggle",
            tracegroupgap=2,
        ),
        margin=dict(l=65, r=160, t=40, b=40),
        title=dict(
            text=(f"ES Swing Features — {symbol}  {date}  "
                  f"<span style='font-size:10px;color:rgba(160,160,160,0.8)'>"
                  f"1-min bars · scroll=zoom x · scroll on y-label=zoom y · dbl-click=reset"
                  f"</span>"),
            font=dict(size=12, color=fg), x=0.01,
        ),
        xaxis=dict(**ax_common, range=[rx0, rx1],
                   rangeselector=range_buttons, rangeslider=dict(visible=False)),
        **{f"xaxis{i}": dict(**ax_common) for i in range(2, 9)},
        barmode="overlay",
    )

    fig.update_yaxes(**yax_common)

    # Per-panel y-axis labels
    fig.update_yaxes(title_text="Price",            row=1, col=1)
    fig.update_yaxes(title_text="Wall ratio (×)",   row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Dist (ticks)",     row=2, col=1, secondary_y=True,
                     showgrid=False, color="rgba(200,200,200,0.4)")
    fig.update_yaxes(title_text="Wall abs log",     row=3, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Wall abs raw",     row=3, col=1, secondary_y=True,
                     showgrid=False, color="rgba(200,200,200,0.4)")
    fig.update_yaxes(title_text="Absorption",       row=4, col=1)
    fig.update_yaxes(title_text="Vel / Δmom (σ)",   row=5, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Cum delta",        row=5, col=1, secondary_y=True,
                     showgrid=False, color="rgba(128,203,196,0.4)")
    fig.update_yaxes(title_text="Hit rate",         row=6, col=1)
    fig.update_yaxes(title_text="Imbalance",        row=7, col=1)
    fig.update_yaxes(title_text="Roll volume",      row=8, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Excess DOM",       row=8, col=1, secondary_y=True,
                     range=[-0.52, 0.52], autorange=False,
                     showgrid=False, zeroline=True,
                     zerolinecolor="rgba(255,210,80,0.20)", zerolinewidth=1,
                     color="rgba(255,210,80,0.7)",
                     tickfont=dict(size=9, color="rgba(255,210,80,0.7)"),
                     fixedrange=False)

    # Initial y-ranges from full-day p1/p99 (RTH subset used for range calc
    # so pre/post-market outliers don't compress the scale)
    def _apply_yr(col_or_series, row, secondary_y=False, pad=0.05):
        if isinstance(col_or_series, str):
            if col_or_series not in feat_rth.columns:
                return
            s = feat_rth[col_or_series]
        else:
            s = col_or_series
        yr = _yrange(s, pad=pad)
        if yr:
            fig.update_yaxes(range=yr, autorange=False, row=row, col=1,
                             secondary_y=secondary_y)

    _apply_yr(price_col, row=1)
    _apply_yr(pd.concat([feat_rth.get("bid_wall_ratio", pd.Series(dtype=float)),
                         feat_rth.get("ask_wall_ratio", pd.Series(dtype=float))]).dropna(),
              row=2, pad=0.08)
    _apply_yr(pd.concat([feat_rth.get("bid_wall_abs_log", pd.Series(dtype=float)),
                         feat_rth.get("ask_wall_abs_log", pd.Series(dtype=float))]).dropna(),
              row=3)
    _apply_yr(pd.concat([feat_rth.get("bid_absorption",  pd.Series(dtype=float)),
                         feat_rth.get("ask_absorption",  pd.Series(dtype=float))]).dropna(),
              row=4)
    _apply_yr(pd.concat([feat_rth.get("delta_momentum_30s", pd.Series(dtype=float)),
                         feat_rth.get("delta_momentum_90s", pd.Series(dtype=float)),
                         feat_rth.get("delta_velocity",     pd.Series(dtype=float))]).dropna(),
              row=5)
    # row 6 hit rate is hardcoded [0, 1.05] above
    _apply_yr(feat_rth.get("imbalance_ema", pd.Series(dtype=float)), row=7, pad=0.15)
    _apply_yr(feat_rth.get("roll_volume", pd.Series(dtype=float)), row=8)

    return fig


# ─────────────────────────────────────────────────────────────────────────────
# FSM / transition helpers (unchanged)
# ─────────────────────────────────────────────────────────────────────────────

def _add_state_bands(fig, annotated, ts, row):
    if "fsm_state" not in annotated.columns:
        return
    states = annotated["fsm_state"].values
    times  = annotated["ts"].values if "ts" in annotated.columns else ts.values
    if len(states) == 0:
        return
    prev, seg = states[0], times[0]
    for i in range(1, len(states)):
        if states[i] != prev or i == len(states) - 1:
            c = STATE_COLOURS.get(str(prev), "rgba(0,0,0,0)")
            if c != "rgba(0,0,0,0)":
                fig.add_vrect(x0=str(seg), x1=str(times[i]),
                              fillcolor=c, opacity=1.0,
                              layer="below", line_width=0, row=row, col=1)
            prev, seg = states[i], times[i]


def _add_transition_markers(fig, trans, feat, price_col, row):
    import plotly.graph_objects as go
    for to_state, group in trans.groupby("to_state"):
        if to_state not in TRANSITION_MARKER:
            continue
        sym, colour, size = TRANSITION_MARKER[to_state]
        prices = []
        for t in group["ts"]:
            idx = feat["ts"].searchsorted(t)
            idx = min(idx, len(feat) - 1)
            prices.append(feat[price_col].iloc[idx])
        fig.add_trace(go.Scatter(
            x=group["ts"], y=prices, mode="markers",
            marker=dict(symbol=sym, color=colour, size=size,
                        line=dict(width=1, color="white")),
            name=f"→{to_state}",
            hovertemplate="%{x|%H:%M}  → " + to_state + "<extra></extra>",
        ), row=row, col=1)


def _add_swing_arrows(fig, trans, feat, price_col):
    import plotly.graph_objects as go
    confirmed = trans[trans["to_state"] == "CONFIRMED"]
    if confirmed.empty:
        return
    for _, row_t in confirmed.iterrows():
        if pd.isna(row_t.get("anchor_price")) or pd.isna(row_t.get("price")):
            continue
        fig.add_annotation(
            x=row_t["ts"], y=row_t["price"],
            ax=row_t["ts"], ay=row_t["anchor_price"],
            xref="x", yref="y", axref="x", ayref="y",
            showarrow=True, arrowhead=2, arrowwidth=1.5,
            arrowcolor="rgba(0,200,100,0.7)",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def show_run(
    date: str,
    symbol: str = "ESH5",
    features_df: Optional[pd.DataFrame] = None,
    annotated_df: Optional[pd.DataFrame] = None,
    auto_open: bool = True,
    
) -> Path:
    """
    Build and save the feature inspection chart for one day.

    Parameters
    ----------
    date : str
        Date string, e.g. '2025-03-05'.
    symbol : str
        Instrument symbol.
    features_df : DataFrame, optional
        Pre-loaded 1s feature data. If None, loaded from QuestDB.
    annotated_df : DataFrame, optional
        FSM annotation data with 'fsm_state' column.
    auto_open : bool
        Open the saved HTML in the browser.
    wall_size_pct : float
        Percentile threshold for wall level filtering (0.99 = top 1%).
        Reduce to 0.98 or 0.95 if no walls appear; raise toward 0.999
        if too many appear.
    """
    from config import VIZ_OUTPUT_DIR, VIZ_AUTO_OPEN

    if features_df is None or features_df.empty:
        features_df = _load_features(date, symbol)
    if features_df.empty:
        log.warning("No feature data for %s.", date)
        return Path()

    log.info("Loading wall levels…")
    wall_levels = _load_wall_levels(date, symbol)

    log.info("Building chart for %s (%d 1s bars)…", date, len(features_df))
    fig = _build_figure(features_df, date, symbol, annotated_df, wall_levels=wall_levels)

    out_dir = Path(VIZ_OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = out_dir / f"swing_{symbol}_{date}.html"

    html_str = fig.to_html(
        full_html=True,
        include_plotlyjs="cdn",
        config={"scrollZoom": True, "displayModeBar": True, "doubleClick": "reset"},
    )

    # JS: scroll on y-axis label zooms that panel's y-axis only
    yaxis_zoom_js = """
<script>
(function() {
  function waitForPlotly() {
    var divs = document.querySelectorAll('.js-plotly-plot');
    if (!divs.length) { setTimeout(waitForPlotly, 200); return; }
    var gd = divs[0];
    gd.addEventListener('wheel', function(e) {
      var yaxisArea = e.target.closest('.ytick,.y2tick,.y3tick,.y4tick,.y5tick,.y6tick,.y7tick,.y8tick,.ytitle,.y2title,.y3title,.y4title,.y5title,.y6title,.y7title,.y8title,.yaxislayer');
      if (!yaxisArea) return;
      e.preventDefault();
      e.stopPropagation();
      var cls = yaxisArea.className.baseVal || yaxisArea.className || '';
      var match = cls.match(/y(\\d*)(?:tick|title|axislayer)?/);
      var axName = match ? ('yaxis' + (match[1] || '')) : 'yaxis';
      var ax = gd.layout[axName];
      if (!ax || !ax.range) return;
      var r = ax.range.slice();
      var factor = e.deltaY > 0 ? 1.15 : 0.87;
      var mid = (r[0] + r[1]) / 2;
      var half = (r[1] - r[0]) / 2 * factor;
      var update = {}; update[axName + '.range'] = [mid - half, mid + half];
      update[axName + '.autorange'] = false;
      Plotly.relayout(gd, update);
    }, {passive: false});
  }
  waitForPlotly();
})();
</script>
"""
    html_str = html_str.replace("</body>", yaxis_zoom_js + "\n</body>", 1)
    fname.write_text(html_str, encoding="utf-8")
    log.info("Chart saved → %s", fname.resolve())

    if VIZ_AUTO_OPEN if auto_open else False:
        webbrowser.open(fname.resolve().as_uri())

    return fname


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s  %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--date",           required=True)
    ap.add_argument("--symbol",         default="ESH5")
    ap.add_argument("--no-open",        action="store_true")
    ap.add_argument("--wall-pct",       type=float, default=0.95,
                    help="Wall size percentile threshold (default 0.99 = top 1%%)")
    args = ap.parse_args()
    show_run(date=args.date, symbol=args.symbol,
             auto_open=not args.no_open)
