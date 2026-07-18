"""
es_indicators.py — concrete, reusable ES indicator library (pandas).
Each function is a clean, documented definition operating on a 1-minute bar
DataFrame with columns: ts, open, high, low, close, vol, delta (signed = buy - sell),
and a 'session' key (ET trading date) for intraday-resetting indicators.

These mirror the SQL used in research but are packaged so they can be read,
tested, and reused. Validation status of each is in INDICATORS.md. Parameters
are principled/data-driven, not curve-fit; defaults are stated, not magic.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

TICK = 0.25
POINT_USD = 50.0


# ── 1. Order Flow Imbalance (OFI) ────────────────────────────────────────────
def order_flow_imbalance(df: pd.DataFrame, window: int = 1) -> pd.Series:
    """Net aggressor pressure in [-1,1]: rolling signed delta / rolling volume.
    +1 = all buy-initiated, -1 = all sell-initiated. window=1 => per-bar.
    Evidence: weak as a standalone directional forecast (decays <10min, < cost);
    strongest at the RTH open. Best used as a confirmation filter, not a predictor."""
    d = df["delta"].rolling(window, min_periods=1).sum()
    v = df["vol"].rolling(window, min_periods=1).sum().replace(0, np.nan)
    return (d / v).fillna(0.0).clip(-1, 1)


# ── 2. Session VWAP, sigma-bands, z-score ────────────────────────────────────
def session_vwap_bands(df: pd.DataFrame) -> pd.DataFrame:
    """Cumulative session VWAP + volume-weighted sigma + z-score, reset each session.
    Adds: sess_vwap, sess_sigma, vwap_z, and band prices vwap_{p/m}{1,2,3}sigma.
    z>0 above VWAP. The user trades the REBOUND (pullback to VWAP then resume),
    and uses +-2/3 sigma as EXIT zones (see vwap_rebound_signals)."""
    out = df.copy()
    tp = (out["high"] + out["low"] + out["close"]) / 3.0  # typical price proxy
    sess = out["session"]
    cv = out["vol"].groupby(sess).cumsum()
    cpv = (tp * out["vol"]).groupby(sess).cumsum()
    cpv2 = (tp * tp * out["vol"]).groupby(sess).cumsum()
    vwap = cpv / cv
    var = (cpv2 / cv - vwap * vwap).clip(lower=0)
    sigma = np.sqrt(var)
    out["sess_vwap"] = vwap
    out["sess_sigma"] = sigma
    out["vwap_z"] = np.where(sigma > 0, (out["close"] - vwap) / sigma, 0.0)
    for k in (1, 2, 3):
        out[f"vwap_p{k}"] = vwap + k * sigma
        out[f"vwap_m{k}"] = vwap - k * sigma
    return out


# ── 3. Kaufman Efficiency Ratio (trend/chop regime) ──────────────────────────
def efficiency_ratio(close: pd.Series, window: int = 30) -> pd.Series:
    """|net move over window| / sum(|bar moves|). ~1 = clean trend, ~0 = chop.
    Look-ahead-free. Used as a regime gate; partially discriminative on its own."""
    close = pd.Series(close)
    net = close.diff(window).abs()
    path = close.diff().abs().rolling(window, min_periods=window).sum().replace(0, np.nan)
    return (net / path).clip(0, 1)


# ── 4. Pivot levels + confluence map ─────────────────────────────────────────
def daily_pivots(prior_high: float, prior_low: float, prior_close: float) -> dict:
    """Classic floor pivots from the PRIOR session's H/L/C, plus prior H/L."""
    pp = (prior_high + prior_low + prior_close) / 3.0
    rng = prior_high - prior_low
    return {
        "PP": pp, "R1": 2 * pp - prior_low, "S1": 2 * pp - prior_high,
        "R2": pp + rng, "S2": pp - rng,
        "R3": prior_high + 2 * (pp - prior_low), "S3": prior_low - 2 * (prior_high - pp),
        "PDH": prior_high, "PDL": prior_low,
    }


def confluence_count(levels: dict, tol: float = 1.5, round_step: float = 25.0,
                     lo: float | None = None, hi: float | None = None) -> dict:
    """For each named level, count how many levels (incl. round numbers in [lo,hi])
    sit within `tol` points. >=2 = confluent zone (higher reversal probability — lead)."""
    pts = list(levels.values())
    if lo is not None and hi is not None and round_step:
        pts += list(np.arange(np.floor(lo / round_step) * round_step, hi + round_step, round_step))
    pts = np.asarray(pts, float)
    return {name: int(np.sum(np.abs(pts - px) <= tol)) for name, px in levels.items()}


# ── 5. VWAP-rebound (impulse -> pullback -> resume) — the user's core setup ───
def vwap_rebound_signals(df_z: pd.DataFrame, impulse_sigma: float = 1.5,
                         pullback_z: float = 0.4, lookback: int = 20,
                         confirm_delta: bool = True, dedupe: bool = True) -> pd.Series:
    """+1 long / -1 short / 0 none. Logic, per bar i:
       1. IMPULSE: in the prior `lookback` bars, vwap_z reached >= +impulse_sigma
          (one-sided up) or <= -impulse_sigma (one-sided down).
       2. PULLBACK: current |vwap_z| < pullback_z (price returned near VWAP).
       3. RESUME (optional): current bar delta confirms the impulse direction.
    Enter in the impulse direction (continuation). `dedupe` keeps only the first
    signal of each impulse->pullback cycle (avoids overlapping entries).
    Requires column 'vwap_z' (from session_vwap_bands) and 'delta'."""
    z = df_z["vwap_z"].to_numpy()
    dlt = df_z["delta"].to_numpy()
    n = len(df_z)
    sig = np.zeros(n, int)
    for i in range(lookback, n):
        win = z[i - lookback:i]
        up = (win.max() >= impulse_sigma) and (win.min() > -0.5)
        dn = (win.min() <= -impulse_sigma) and (win.max() < 0.5)
        if abs(z[i]) < pullback_z:
            if up and (not confirm_delta or dlt[i] > 0):
                sig[i] = 1
            elif dn and (not confirm_delta or dlt[i] < 0):
                sig[i] = -1
    s = pd.Series(sig, index=df_z.index)
    if dedupe:  # suppress consecutive repeats of the same-direction signal
        s = s.where(s != s.shift(1).fillna(0), 0)
    return s


# ── 6. Supply/Demand zones: impulse -> balance -> impulse ────────────────────
def detect_sd_zones(df: pd.DataFrame, impulse_body_mult: float = 1.5,
                    wick_body_max: float = 0.5, body_window: int = 20) -> pd.DataFrame:
    """Institutional S/D zones per the user's definition: an IMPULSE candle, a
    BALANCE candle (total wick < `wick_body_max` x body — i.e. body-dominant), then
    another IMPULSE in the SAME direction. The balance candle's [low,high] is the zone.
    NOTE: 'balance = wick<50% of body' is the user's literal criterion; I read it as a
    decisive (body-dominant) origin candle and also require its range be below average
    so it's a genuine base, not just another impulse — flag if you meant otherwise.
    Returns rows: ts, zone_low, zone_high, direction (+1 demand/up, -1 supply/down)."""
    o, h, l, c = (df[x].to_numpy() for x in ("open", "high", "low", "close"))
    body = c - o
    abody = np.abs(body)
    upper = h - np.maximum(o, c)
    lower = np.minimum(o, c) - l
    wick = upper + lower
    rng = h - l
    avg_body = pd.Series(abody).rolling(body_window, min_periods=1).mean().to_numpy()
    avg_rng = pd.Series(rng).rolling(body_window, min_periods=1).mean().to_numpy()
    is_impulse = abody >= impulse_body_mult * avg_body
    is_balance = (wick < wick_body_max * np.maximum(abody, 1e-9)) & (rng < avg_rng)
    rows = []
    for i in range(1, len(df) - 1):
        if (is_balance[i] and is_impulse[i - 1] and is_impulse[i + 1]
                and np.sign(body[i - 1]) == np.sign(body[i + 1]) and body[i + 1] != 0):
            rows.append((df["ts"].iloc[i], l[i], h[i], int(np.sign(body[i + 1]))))
    return pd.DataFrame(rows, columns=["ts", "zone_low", "zone_high", "direction"])


# ── 7. Ignition (tick/second-level) — VALIDATED cross-period ─────────────────
def ignition_signal(sec_df: pd.DataFrame, str_min: float = 5.0, avol_min: float = 800.0,
                    lookback: int = 120) -> pd.Series:
    """Tick/second ignition detector. Returns +1 (up-ignition) / -1 (down) / 0 (none) per second.

    Fires when a real directional move is *igniting*: aggressive trade delta explodes vs its
    recent baseline, real volume is present, AND the attacked side's resting book collapses
    (asks pulled on an up-move, bids pulled on a down-move).

    `sec_df` columns required (per 1-second bar of MBO data):
        adelta      signed aggressor delta  (buy_initiated - sell_initiated size)
        avol        total aggressor volume (contracts) in the second
        bid_cancel  size of resting BID orders cancelled in the second
        ask_cancel  size of resting ASK orders cancelled in the second

    Validated cross-period (ESM5 Apr-May and ESH5 Feb-Mar): when it fires, price leans the
    signalled direction ~56-58% at 60s (vs ~51.5% base) with a ~2:1 right:wrong ratio on real
    (5pt) moves; dose-responds with volume (`avol_min=2000` = "huge" tier, strongest).
    USE AS AN INDICATOR (confirmation / size-up when aligned; immediate exit when opposite),
    NOT a standalone strategy. The book collapse is coincident with the prints (latency-sensitive)."""
    d = sec_df
    base = d["adelta"].abs().rolling(lookback, min_periods=20).mean()
    strength = d["adelta"].abs() / (base + 1.0)
    up = d["adelta"] > 0
    book_confirm = np.where(up, d["ask_cancel"] > d["bid_cancel"],
                                d["bid_cancel"] > d["ask_cancel"])
    fire = (strength >= str_min) & (d["avol"] >= avol_min) & book_confirm
    return pd.Series(np.where(fire, np.sign(d["adelta"]).astype(int), 0), index=d.index)


if __name__ == "__main__":
    print("self-test in test_indicators.py")
