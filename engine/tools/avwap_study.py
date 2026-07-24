"""Anchored-VWAP horse-race — which anchor actually earns its keep on ES.

Answers "which anchor gives the best results?" empirically instead of by
opinion, on the only intraday data we have with real volume: claude_rth_1m,
ES RTH 1-minute bars (ESH5 Feb-Mar + ESM5 Apr-May 2025, ~72 sessions). Runs
each contract separately (sidesteps the ESH5->ESM5 roll and gives a 2-fold
"does the ranking agree?" check), then pools.

For each anchor it draws the anchored VWAP with bar_vwap as the per-minute
price and scores it on three jobs:

  REACTION (is it support/resistance?) — every time price tests the line from
     above (support) or below (resistance), measure the H-minute reversion.
     A real S/R line reverts price; an arbitrary one does not.
  BIAS (does side predict direction?) — at 10:00 ET, does sign(close - AVWAP)
     predict the sign of the rest-of-session return?
  CONFLUENCE — do tests that ALSO sit on a floor-pivot (the MultiPivots D/W/M
     grid) react better than tests that don't? This is the payoff for the
     engine: AVWAP as a confirmation filter on the pivot bias, not alone.

Controls: a `rand` anchor (an arbitrary earlier bar each session) shows what
noise scores; a pivot-only reaction baseline shows the grid on its own. Read
the EDGE (anchor - rand), never the raw number.

Caveats (stated, not hidden): ES only (no NQ intraday exists yet), RTH only
(no Globex, so no overnight-hi/lo or event anchors), 72 sessions, one 2025
window. This is a first-cut horse-race to rank anchors, NOT a validated edge.

    .venv/Scripts/python.exe tools/avwap_study.py
    .venv/Scripts/python.exe tools/avwap_study.py --eps 1.5 --horizon 20
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB          # noqa: E402
from engine.core.timeutil import et_minute_of_day, et_session_date  # noqa: E402
from engine.features.pivots import MultiPivots        # noqa: E402

OPEN_MIN, CLOSE_MIN = 570, 959       # 09:30 .. 15:59 ET
DECIDE_MIN = 600                     # 10:00 ET — the bias decision time

# rth1m: curated ES RTH minute table (2025). live: 24h ES+NQ capture (2026+),
# which unlocks the overnight-hi/lo anchors and NQ, but is thinner/growing.
SOURCES = {
    "rth1m": {"contracts": ("ESH5", "ESM5"),
              "anchors": ["rth_open", "wtd", "mtd", "pdh", "pdl",
                          "swing_hi", "swing_lo", "rand"]},
    "live": {"contracts": None,       # discovered from the table (ES, NQ)
             "anchors": ["rth_open", "overnight_hi", "overnight_lo", "wtd", "mtd",
                         "pdh", "pdl", "swing_hi", "swing_lo", "rand"]},
}


# ── data ─────────────────────────────────────────────────────────────────────
def _decorate(df: pd.DataFrame) -> pd.DataFrame:
    """Common columns: session date, ISO week, month, per-minute price, floats."""
    sd = pd.to_datetime(df["sd"]).dt.tz_localize(None)
    iso = sd.dt.isocalendar()
    df["wk"] = iso["year"].astype(str) + "-W" + iso["week"].astype(int).map("{:02d}".format)
    df["mo"] = sd.dt.strftime("%Y-%m")
    for col in ("o", "h", "l", "c", "vol"):
        df[col] = df[col].astype(float)
    return df.reset_index(drop=True)


def load_rth1m(qdb: QuestDB, symbol: str) -> pd.DataFrame:
    df = qdb.df(
        "SELECT sess_date, et_min, ts, o, h, l, c, vol, bar_vwap "
        f"FROM claude_rth_1m WHERE symbol = '{symbol}' ORDER BY ts")
    if df.empty:
        return df
    df["sd"] = pd.to_datetime(df["sess_date"]).dt.tz_localize(None).dt.strftime("%Y-%m-%d")
    df["p"] = df["bar_vwap"].astype(float)            # per-minute price for AVWAP
    return _decorate(df)


def load_live(qdb: QuestDB, symbol: str) -> pd.DataFrame:
    """24h live 1m bars — no bar_vwap column, so use the typical price (h+l+c)/3
    and derive ET session-date / minute-of-day from the timestamp."""
    df = qdb.df(
        "SELECT ts, o, h, l, c, vol FROM claude_bars_live "
        f"WHERE symbol = '{symbol}' ORDER BY ts")
    if df.empty:
        return df
    tns = df["ts"].astype("int64").to_numpy()
    df["sd"] = [et_session_date(int(t)) for t in tns]
    df["et_min"] = [et_minute_of_day(int(t)) for t in tns]
    df["p"] = (df["h"].astype(float) + df["l"].astype(float) + df["c"].astype(float)) / 3.0
    return _decorate(df)


def first_index_of(keys: np.ndarray) -> np.ndarray:
    """For each row, the index of the first row sharing its key (keys are runs
    in ts order — session date, week, month all satisfy this)."""
    out = np.empty(len(keys), dtype=np.int64)
    start = 0
    for i in range(len(keys)):
        if i == 0 or keys[i] != keys[i - 1]:
            start = i
        out[i] = start
    return out


def swing_anchors(h: np.ndarray, l: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    """Most-recent CONFIRMED k-fractal swing-high / swing-low index at each bar.
    A swing high at j is a strict max of h[j-k..j+k]; it is only 'known' at j+k
    (causal). Returns anchor-index arrays (-1 until the first confirmed swing)."""
    n = len(h)
    sh = np.full(n, -1, dtype=np.int64)
    sl = np.full(n, -1, dtype=np.int64)
    last_h = last_l = -1
    for i in range(n):
        j = i - k                                     # confirm the pivot at j now
        # window [j-k, j+k] == [j-k, i] is fully in range once j-k >= 0
        if j - k >= 0:
            wnd_h = h[j - k:i + 1]
            wnd_l = l[j - k:i + 1]
            if h[j] == wnd_h.max() and (wnd_h == h[j]).sum() == 1:
                last_h = j
            if l[j] == wnd_l.min() and (wnd_l == l[j]).sum() == 1:
                last_l = j
        sh[i] = last_h
        sl[i] = last_l
    return sh, sl


def first_rth_index(df: pd.DataFrame) -> np.ndarray:
    """Per bar, the index of the first RTH bar (et_min>=570) of its session — the
    'since the bell' anchor. On rth1m every bar is RTH so this is the session
    start; on 24h live data it correctly skips the overnight and anchors at 09:30."""
    sd = df["sd"].to_numpy(); em = df["et_min"].to_numpy()
    n = len(df)
    out = np.full(n, -1, dtype=np.int64)
    starts = first_index_of(sd)
    cur_day = None; anchor = -1
    for i in range(n):
        if sd[i] != cur_day:
            cur_day = sd[i]
            anchor = -1                               # reset each session
        if anchor < 0 and em[i] >= OPEN_MIN:
            anchor = i
        out[i] = anchor if anchor >= 0 else starts[i]
    return out


def overnight_extreme_index(df: pd.DataFrame, which: str) -> np.ndarray:
    """Per bar, the index of this session's OVERNIGHT (00:00-09:30 ET, the
    Asia-late/London window) high or low bar; -1 for sessions with no pre-RTH
    bars captured. The AVWAP anchored here spans the overnight into RTH."""
    sd = df["sd"].to_numpy(); em = df["et_min"].to_numpy()
    col = df["h"].to_numpy() if which == "hi" else df["l"].to_numpy()
    n = len(df)
    out = np.full(n, -1, dtype=np.int64)
    # per session, find the overnight extreme bar index
    ext: dict[str, int] = {}
    for i in range(n):
        if em[i] < OPEN_MIN:
            d = sd[i]; cur = ext.get(d)
            if cur is None or (col[i] > col[cur] if which == "hi" else col[i] < col[cur]):
                ext[d] = i
    for i in range(n):
        out[i] = ext.get(sd[i], -1)
    return out


def prior_day_extreme_index(df: pd.DataFrame, which: str) -> np.ndarray:
    """Per bar, the index of the PRIOR session's high (or low) bar; -1 on day 1."""
    n = len(df)
    out = np.full(n, -1, dtype=np.int64)
    sds = df["sd"].to_numpy()
    col = df["h"].to_numpy() if which == "hi" else df["l"].to_numpy()
    starts = first_index_of(sds)
    prev_ext_idx = -1
    cur_day = sds[0]
    # running extreme index within the CURRENT day, committed at day change
    run_idx = 0
    for i in range(n):
        if sds[i] != cur_day:
            prev_ext_idx = run_idx
            cur_day = sds[i]
            run_idx = i
        else:
            better = col[i] > col[run_idx] if which == "hi" else col[i] < col[run_idx]
            if i == starts[i] or better:
                run_idx = i
        out[i] = prev_ext_idx
    return out


# ── AVWAP via prefix sums (matches AnchoredVWAP incrementally; see test) ──────
def avwap_for(p: np.ndarray, v: np.ndarray, anchor_idx: np.ndarray) -> np.ndarray:
    pv = p * v
    P = np.concatenate([[0.0], np.cumsum(pv)])        # P[i] = Σ_{j<i} p·v
    V = np.concatenate([[0.0], np.cumsum(v)])
    n = len(p)
    out = np.full(n, np.nan)
    a = anchor_idx
    ok = a >= 0
    num = P[np.arange(n) + 1] - P[np.where(ok, a, 0)]
    den = V[np.arange(n) + 1] - V[np.where(ok, a, 0)]
    good = ok & (den > 0)
    out[good] = num[good] / den[good]
    return out


def build_anchor_indices(df: pd.DataFrame, anchors: list[str], k_swing: int,
                         seed: int) -> dict[str, np.ndarray]:
    sd = df["sd"].to_numpy()
    idx = {}
    idx["rth_open"] = first_rth_index(df)
    idx["wtd"] = first_index_of(df["wk"].to_numpy())
    idx["mtd"] = first_index_of(df["mo"].to_numpy())
    idx["pdh"] = prior_day_extreme_index(df, "hi")
    idx["pdl"] = prior_day_extreme_index(df, "lo")
    sh, sl = swing_anchors(df["h"].to_numpy(), df["l"].to_numpy(), k_swing)
    idx["swing_hi"], idx["swing_lo"] = sh, sl
    if "overnight_hi" in anchors:
        idx["overnight_hi"] = overnight_extreme_index(df, "hi")
        idx["overnight_lo"] = overnight_extreme_index(df, "lo")
    # control: an arbitrary earlier bar in the same session (uniform, seeded)
    rng = np.random.default_rng(seed)
    starts = first_index_of(sd)
    rnd = np.empty(len(df), dtype=np.int64)
    for i in range(len(df)):
        lo = starts[i]
        rnd[i] = lo if i == lo else rng.integers(lo, i + 1)
    idx["rand"] = rnd
    return {a: idx[a] for a in anchors}


# ── metrics ──────────────────────────────────────────────────────────────────
def reaction_events(df: pd.DataFrame, av: np.ndarray, eps: float, H: int):
    """Yield (i, reaction_pts, is_support, avwap_at_i) for each test of the line."""
    sd = df["sd"].to_numpy(); em = df["et_min"].to_numpy()
    c = df["c"].to_numpy(); h = df["h"].to_numpy(); l = df["l"].to_numpy()
    out = []
    for i in range(1, len(df) - H):
        if em[i] < OPEN_MIN or em[i] > CLOSE_MIN:      # only score RTH reactions
            continue
        if sd[i] != sd[i - 1] or sd[i] != sd[i + H]:
            continue                                   # keep the window intraday
        a0, a1 = av[i - 1], av[i]
        if not (a0 == a0 and a1 == a1):                # NaN guard
            continue
        if l[i] <= a1 + eps and c[i - 1] > a0 + eps:   # support test (approach from above)
            out.append((i, c[i + H] - c[i], True, a1))
        elif h[i] >= a1 - eps and c[i - 1] < a0 - eps:  # resistance test (from below)
            out.append((i, c[i] - c[i + H], False, a1))
    return out


def summarize(reactions: list[float]) -> dict:
    if not reactions:
        return {"n": 0, "mean": float("nan"), "hit": float("nan"), "t": float("nan")}
    a = np.asarray(reactions, dtype=float)
    mean = a.mean()
    sd = a.std(ddof=1) if len(a) > 1 else 0.0
    t = mean / (sd / np.sqrt(len(a))) if sd > 0 else float("nan")
    return {"n": len(a), "mean": mean, "hit": float((a > 0).mean() * 100), "t": t}


def bias_score(df: pd.DataFrame, av: np.ndarray):
    """At 10:00 ET: does sign(close - AVWAP) predict the rest-of-session return?"""
    sd = df["sd"].to_numpy(); em = df["et_min"].to_numpy(); c = df["c"].to_numpy()
    signed = []
    for day in pd.unique(sd):
        idxs = np.where(sd == day)[0]
        rth = idxs[(em[idxs] >= DECIDE_MIN) & (em[idxs] <= CLOSE_MIN)]   # 10:00..15:59
        if len(rth) < 2:
            continue
        dt, end = rth[0], rth[-1]                       # decide at 10:00, close at 15:59
        a = av[dt]
        if not (a == a):
            continue
        sig = np.sign(c[dt] - a)
        if sig == 0:
            continue
        signed.append(sig * (c[end] - c[dt]))
    if not signed:
        return {"n": 0, "mean": float("nan"), "hit": float("nan")}
    a = np.asarray(signed, float)
    return {"n": len(a), "mean": a.mean(), "hit": float((a > 0).mean() * 100)}


def pivot_grid_by_day(df: pd.DataFrame) -> dict[str, list[float]]:
    """MultiPivots D/W/M grid frozen per session, seeded from this contract's own
    daily H/L/C aggregates (prior D/W/M periods)."""
    daily = (df.groupby("sd").agg(h=("h", "max"), l=("l", "min"), c=("c", "last"))
             .reset_index())
    mp = MultiPivots()
    out: dict[str, list[float]] = {}
    ts = 0
    for _, row in daily.iterrows():
        grid = list(mp.grid().keys())                  # prior-period grid, before today
        out[row["sd"]] = grid
        # advance the tracker by one synthetic daily bar at noon ET of that day
        ts = int(pd.Timestamp(row["sd"] + " 12:00", tz="America/New_York").value)
        mp.update(ts, row["h"], row["l"], row["c"])
    return out


def confluence_split(df, av, events, grids, delta):
    """Split reaction events by whether the touched AVWAP sits on a grid pivot."""
    sd = df["sd"].to_numpy()
    conf, non = [], []
    for i, react, _is_sup, aval in events:
        grid = grids.get(sd[i], [])
        near = any(abs(aval - g) <= delta for g in grid)
        (conf if near else non).append(react)
    return conf, non


# ── driver ───────────────────────────────────────────────────────────────────
def discover_symbols(qdb: QuestDB) -> list[str]:
    df = qdb.df("SELECT DISTINCT symbol FROM claude_bars_live ORDER BY symbol")
    return [s for s in df["symbol"].tolist()]


def coverage(df: pd.DataFrame) -> dict:
    """Per-symbol data-quality: sessions, RTH bars, and overnight availability —
    so the study self-documents whether there is 'enough captured' yet."""
    em = df["et_min"].to_numpy(); sd = df["sd"].to_numpy()
    days = pd.unique(sd)
    on_days = sum(1 for d in days if (em[sd == d] < OPEN_MIN).any())
    rth_per = np.mean([int(((em[sd == d] >= OPEN_MIN) & (em[sd == d] <= CLOSE_MIN)).sum())
                       for d in days]) if len(days) else 0
    on_per = np.mean([int((em[sd == d] < OPEN_MIN).sum()) for d in days]) if len(days) else 0
    return {"days": len(days), "on_days": on_days,
            "rth_per": rth_per, "on_per": on_per,
            "lo": str(df["sd"].iloc[0]), "hi": str(df["sd"].iloc[-1])}


def run(source: str, eps: float, H: int, k_swing: int, delta: float, seed: int) -> None:
    qdb = QuestDB(timeout=120.0)
    cfg = SOURCES[source]
    anchors = cfg["anchors"]
    loader = load_rth1m if source == "rth1m" else load_live
    contracts = cfg["contracts"] or discover_symbols(qdb)
    LEVEL_ANCHORS = [a for a in ("rth_open", "overnight_hi", "overnight_lo",
                                 "pdh", "pdl", "swing_hi", "swing_lo") if a in anchors]
    per_contract = {}
    cov = {}
    pooled_react = {a: [] for a in anchors}
    pooled_bias = {a: [] for a in anchors}
    pooled_conf = {a: {"conf": [], "non": []} for a in LEVEL_ANCHORS}

    for sym in contracts:
        df = loader(qdb, sym)
        if df.empty:
            print(f"{sym}: no data"); continue
        cov[sym] = coverage(df)
        idx = build_anchor_indices(df, anchors, k_swing, seed)
        av = {a: avwap_for(df["p"].to_numpy(), df["vol"].to_numpy(), idx[a]) for a in anchors}
        grids = pivot_grid_by_day(df)
        rows = {}
        for a in anchors:
            ev = reaction_events(df, av[a], eps, H)
            r = [e[1] for e in ev]
            pooled_react[a].extend(r)
            bs = bias_score(df, av[a])
            pooled_bias[a].append(bs)
            rows[a] = (summarize(r), bs)
            if a in pooled_conf:
                c, n = confluence_split(df, av[a], ev, grids, delta)
                pooled_conf[a]["conf"].extend(c); pooled_conf[a]["non"].extend(n)
        per_contract[sym] = rows

    _print(source, anchors, per_contract, cov, pooled_react, pooled_bias,
           pooled_conf, eps, H, k_swing, delta)


def _print(source, anchors, per_contract, cov, pooled_react, pooled_bias,
           pooled_conf, eps, H, k_swing, delta):
    title = ("ES RTH 1m, 2025 (bar_vwap-weighted)" if source == "rth1m"
             else "ES+NQ 24h live 1m (hlc3-weighted, RTH-scored)")
    print("\n" + "=" * 82)
    print(f"ANCHORED-VWAP HORSE-RACE  —  {title}")
    print(f"params: eps={eps}pt  reaction_horizon={H}m  swing_k={k_swing}  "
          f"confluence_delta={delta}pt  decide=10:00ET")
    print("read the EDGE vs `rand` (arbitrary anchor). raw pts scale with vol.")
    print("=" * 82)

    if cov:
        print("\n[COVERAGE]")
        for sym, c in cov.items():
            print(f"  {sym:5s} {c['lo']}..{c['hi']}  sessions={c['days']:3d}  "
                  f"overnight-sessions={c['on_days']:3d}  "
                  f"~{c['rth_per']:.0f} RTH + {c['on_per']:.0f} overnight bars/session")

    def prow(a, rs, bs):
        return (f"  {a:12s}  react N={rs['n']:5d}  mean={rs['mean']:+6.2f}pt  "
                f"hit={rs['hit']:4.1f}%  t={rs['t']:+5.2f}   |  "
                f"bias N={bs['n']:3d} mean={bs['mean']:+6.2f} hit={bs['hit']:4.1f}%")

    for sym, rows in per_contract.items():
        print(f"\n[{sym}]")
        for a in anchors:
            rs, bs = rows[a]
            print(prow(a, rs, bs))

    print(f"\n[POOLED  {'+'.join(per_contract)}]")
    for a in anchors:
        rs = summarize(pooled_react[a])
        bl = pooled_bias[a]
        N = sum(b["n"] for b in bl)
        mean = (np.nansum([b["mean"] * b["n"] for b in bl]) / N) if N else float("nan")
        hit = (np.nansum([b["hit"] * b["n"] for b in bl]) / N) if N else float("nan")
        print(prow(a, rs, {"n": N, "mean": mean, "hit": hit}))

    rand = summarize(pooled_react["rand"])["mean"]
    print(f"\n  reaction EDGE vs rand ({rand:+.2f}pt):")
    for a in anchors:
        if a == "rand":
            continue
        m = summarize(pooled_react[a])["mean"]
        print(f"    {a:12s} {m - rand:+.2f}pt")

    print("\n[CONFLUENCE  — each level-anchor's tests split by pivot-grid proximity]")
    print("  (positive reaction = line held/reverted; lift = on-pivot minus off-pivot)")
    for a, d in pooled_conf.items():
        conf, non = summarize(d["conf"]), summarize(d["non"])
        lift = (conf["mean"] - non["mean"]) if (conf["n"] and non["n"]) else float("nan")
        print(f"    {a:12s}  on-pivot N={conf['n']:4d} mean={conf['mean']:+.2f}  "
              f"off-pivot N={non['n']:4d} mean={non['mean']:+.2f}   lift={lift:+.2f}pt")
    print("=" * 82 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=list(SOURCES), default="rth1m",
                    help="rth1m: curated ES RTH 2025. live: 24h ES+NQ capture "
                         "(adds overnight anchors + NQ; thinner/growing).")
    ap.add_argument("--eps", type=float, default=2.0, help="touch band, points")
    ap.add_argument("--horizon", type=int, default=15, help="reaction horizon, minutes")
    ap.add_argument("--swing-k", type=int, default=30, help="fractal half-window, bars")
    ap.add_argument("--delta", type=float, default=2.0, help="confluence tolerance, points")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    run(a.source, a.eps, a.horizon, a.swing_k, a.delta, a.seed)


if __name__ == "__main__":
    main()
