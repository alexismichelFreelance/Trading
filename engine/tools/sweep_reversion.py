"""Does price REVERT after an aggressive sweep? Measured directly on the tape.

Every number this week came from a sleeve's P&L, and every one of them inverted
on the next sample. P&L is the wrong instrument for the question: it convolves
the signal with an exit thesis, a stop, a hold parameter, costs, and a position
that can be blocked when the next signal arrives. This measures the SIGNAL and
nothing else.

Definition (identical to SweepFollowStrategy, so the number describes the sleeve
we actually run): cluster the trade tape by aggressor direction with a 1ms gap.
A cluster that spans >= N ticks is a sweep. At the instant the cluster ENDS --
the first trade of the next cluster, which is the earliest you could act, since
the sweep is how you learn of it -- record p0, then the price at fixed horizons.

FADE return in ticks = -sweep_dir * (p_h - p0) / tick.
Positive = price came back = fading the sweep was right.

Read on the tape only. No orders, no fills, no exits, no P&L.

Reported per contract (ESH5 Feb-Mar 2025, ESM5 Mar-May 2025 -- disjoint), per
span bucket and per horizon, with PER-SESSION sign consistency, because the
failure mode all week has been a large aggregate carried by a handful of days.

    .venv/Scripts/python.exe tools/sweep_reversion.py --symbol ESM5
    .venv/Scripts/python.exe tools/sweep_reversion.py --symbol ESH5 --verify 2025-03-05
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.core.timeutil import et_minute_of_day  # noqa: E402

CACHE = ROOT / ".cache" / "sweeprev"
URL = "http://localhost:9000"
TICK = 0.25
GAP_NS = 1_000_000                 # 1ms -- the measured optimum, see sweep_follow.py
RTH_LO, RTH_HI = 9 * 60 + 30, 15 * 60 + 59
HORIZONS_S = (1, 5, 15, 30, 60, 300)
BUCKETS = ((6, 8), (8, 12), (12, 20), (20, 10_000))
RANGES = {"ESH5": ("2025-02-18", "2025-03-19"),
          "ESM5": ("2025-03-20", "2025-05-30")}


def fetch_day(symbol: str, day: str) -> pd.DataFrame | None:
    """Raw trade tape for one session, via /exp (CSV) -- /exec JSON is far too
    slow for ~500k rows/day across 70 sessions."""
    sql = ("SELECT ts_recv, price, side FROM mbo_events "
           f"WHERE action='T' AND symbol='{symbol}' "
           f"AND ts_recv >= '{day}T00:00:00.000000Z' "
           f"AND ts_recv <  '{day}T23:59:59.999999Z' ORDER BY ts_recv")
    r = httpx.get(f"{URL}/exp", params={"query": sql}, timeout=600.0)
    r.raise_for_status()
    if len(r.text) < 200:
        return None
    df = pd.read_csv(io.StringIO(r.text))
    return df if len(df) > 1000 else None


def _ns(col: pd.Series) -> np.ndarray:
    """Timestamps as NANOSECONDS.

    pd.to_datetime() on these CSV strings resolves to datetime64[us], so a bare
    .astype("int64") yields MICROseconds -- 1000x off. That silently turned the
    1ms cluster gap into 1s and made et_minute_of_day read every sweep as a 1970
    date, so the RTH filter dropped every one of them and the study reported zero
    sweeps on a day the sleeve trades 40 times. Always pin the unit.
    """
    return pd.to_datetime(col, utc=True).values.astype("datetime64[ns]").astype("int64")


def sweeps(ts: np.ndarray, px: np.ndarray, dirn: np.ndarray,
           min_span: int = 6) -> pd.DataFrame:
    """Vectorised cluster detection + forward returns.

    A cluster BREAKS when direction flips or the inter-trade gap exceeds 1ms --
    exactly the condition in SweepFollowStrategy.on_trade. Cluster k's span is
    evaluated at the first trade of cluster k+1, which is where the strategy
    would enter, so p0 is that trade's price and t0 its timestamp.
    """
    brk = np.empty(len(ts), dtype=bool)
    brk[0] = True
    brk[1:] = (dirn[1:] != dirn[:-1]) | ((ts[1:] - ts[:-1]) > GAP_NS)
    start = np.flatnonzero(brk)                  # first index of each cluster
    if len(start) < 3:
        return pd.DataFrame()
    end = np.append(start[1:], len(ts))          # one past last index
    hi = np.maximum.reduceat(px, start)
    lo = np.minimum.reduceat(px, start)
    span = np.rint((hi - lo) / TICK).astype(int)
    # cluster k is evaluated at start[k+1]; the last cluster has no successor
    ev = start[1:]
    span, cdir = span[:-1], dirn[start][:-1]
    t0, p0 = ts[ev], px[ev]

    keep = span >= min_span
    if not keep.any():
        return pd.DataFrame()
    span, cdir, t0, p0 = span[keep], cdir[keep], t0[keep], p0[keep]
    mins = np.array([et_minute_of_day(int(t)) for t in t0])
    keep = (mins >= RTH_LO) & (mins < RTH_HI)
    if not keep.any():
        return pd.DataFrame()
    span, cdir, t0, p0 = span[keep], cdir[keep], t0[keep], p0[keep]

    out = {"t0": t0, "span": span, "dir": cdir, "p0": p0}
    for h in HORIZONS_S:
        # last trade at or before t0 + h  (searchsorted on the full tape)
        j = np.searchsorted(ts, t0 + h * 1_000_000_000, side="right") - 1
        out[f"h{h}"] = -cdir * (px[j] - p0) / TICK     # FADE return, in ticks
    return pd.DataFrame(out)


def day_records(symbol: str, day: str) -> pd.DataFrame | None:
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / f"{symbol}_{day}.pkl"
    if f.exists():
        return pd.read_pickle(f)
    df = fetch_day(symbol, day)
    if df is None:
        return None
    ts = _ns(df["ts_recv"])
    px = df["price"].to_numpy(float)
    dirn = np.where(df["side"].to_numpy() == "B", 1, -1).astype(np.int8)
    rec = sweeps(ts, px, dirn)
    rec.to_pickle(f)
    return rec


def reference_sweeps(ts, px, dirn, min_span: int = 6) -> list[tuple[int, float, int, int]]:
    """Sequential transcription of SweepFollowStrategy.on_trade's clustering,
    used only to prove the vectorised version above is the same detector.
    Deliberately WITHOUT the sleeve's position/max_entries gating: the point is
    to measure the signal, not to re-measure the sleeve."""
    out, c_dir, c_hi, c_lo, c_ts = [], 0, 0.0, 0.0, 0
    for i in range(len(ts)):
        d, p, t = int(dirn[i]), float(px[i]), int(ts[i])
        if d == c_dir and (t - c_ts) <= GAP_NS:
            c_hi, c_lo, c_ts = max(c_hi, p), min(c_lo, p), t
            continue
        if c_dir != 0:
            span = round((c_hi - c_lo) / TICK)
            if span >= min_span and RTH_LO <= et_minute_of_day(t) < RTH_HI:
                out.append((t, p, c_dir, span))
        c_dir, c_hi, c_lo, c_ts = d, p, p, t
    return out


def verify(symbol: str, day: str) -> None:
    df = fetch_day(symbol, day)
    if df is None:
        raise SystemExit(f"no tape for {symbol} {day}")
    ts = _ns(df["ts_recv"])
    px = df["price"].to_numpy(float)
    dirn = np.where(df["side"].to_numpy() == "B", 1, -1).astype(np.int8)
    ref = reference_sweeps(ts, px, dirn)
    vec = sweeps(ts, px, dirn)
    print(f"\n  reference (sequential, mirrors the strategy): {len(ref):>6} sweeps")
    print(f"  vectorised (used for the study):              {len(vec):>6} sweeps")
    assert len(ref) == len(vec), "DETECTOR MISMATCH — study is invalid"
    r = pd.DataFrame(ref, columns=["t0", "p0", "dir", "span"])
    for c in ("t0", "p0", "dir", "span"):
        bad = int((r[c].to_numpy() != vec[c].to_numpy()).sum())
        assert bad == 0, f"DETECTOR MISMATCH on {c}: {bad} rows"
    print(f"  identical on t0, p0, dir, span across {len(ref)} sweeps on "
          f"{len(ts):,} trades — vectorisation is faithful\n")


def report(rec: pd.DataFrame, per_day: dict[str, pd.DataFrame], symbol: str,
           days: int, cost_ticks: float) -> None:
    print(f"\n{'='*98}")
    print(f"SWEEP REVERSION — {symbol}, {days} sessions, {len(rec):,} sweeps "
          f"(span>=6t, RTH, tape only)")
    print(f"{'='*98}")
    print(f"{'span':<12}{'n':>8}" + "".join(f"{f'{h}s':>13}" for h in HORIZONS_S))
    print(f"{'':<12}{'':>8}" + "".join(f"{'med  win%':>13}" for _ in HORIZONS_S))
    for lo, hi in BUCKETS:
        g = rec[(rec.span >= lo) & (rec.span < hi)]
        if len(g) < 50:
            continue
        lab = f"{lo}-{hi-1}t" if hi < 10_000 else f"{lo}t+"
        row = f"{lab:<12}{len(g):>8,}"
        for h in HORIZONS_S:
            v = g[f"h{h}"]
            row += f"{v.median():>+7.2f}{100*(v>0).mean():>6.0f}"
        print(row)

    print(f"\n  ALL span>=6, median fade return in TICKS, and net of "
          f"{cost_ticks:.1f}t round-trip crossing cost:")
    row_g = f"{'gross':<12}{len(rec):>8,}"
    row_n = f"{'net':<12}{'':>8}"
    for h in HORIZONS_S:
        m = rec[f"h{h}"].median()
        row_g += f"{m:>+7.2f}{100*(rec[f'h{h}']>0).mean():>6.0f}"
        row_n += f"{m-cost_ticks:>+7.2f}{'':>6}"
    print(row_g)
    print(row_n)

    print(f"\n  PER-SESSION sign consistency (median fade return of each session):")
    print(f"{'horizon':<12}{'sessions +':>14}{'of':>5}{'  %':>6}{'  worst':>9}{'  best':>9}")
    for h in HORIZONS_S:
        med = np.array([d[f"h{h}"].median() for d in per_day.values() if len(d) >= 20])
        if not len(med):
            continue
        print(f"{f'{h}s':<12}{int((med>0).sum()):>14}{len(med):>5}"
              f"{100*(med>0).mean():>6.0f}{med.min():>+9.2f}{med.max():>+9.2f}")
    print(f"{'='*98}\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ESM5")
    ap.add_argument("--verify", default=None, help="cross-check the detector on one day")
    ap.add_argument("--cost-ticks", type=float, default=1.0)
    a = ap.parse_args()
    if a.verify:
        verify(a.symbol, a.verify)
        return
    lo, hi = RANGES[a.symbol]
    days = [d.strftime("%Y-%m-%d") for d in pd.bdate_range(lo, hi)]
    per_day, n_ok = {}, 0
    for d in days:
        try:
            rec = day_records(a.symbol, d)
        except Exception as ex:                        # holiday / gap / bad day
            print(f"  {d}: {type(ex).__name__} {ex}")
            continue
        if rec is None or rec.empty:
            continue
        per_day[d], n_ok = rec, n_ok + 1
        print(f"  {d}: {len(rec):>5} sweeps")
    if not per_day:
        raise SystemExit("no sessions")
    report(pd.concat(per_day.values(), ignore_index=True), per_day,
           a.symbol, n_ok, a.cost_ticks)


if __name__ == "__main__":
    main()
