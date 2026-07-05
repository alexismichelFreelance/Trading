"""Bubble scanner — PSY BSADF explosiveness dating + LPPLS tc estimate.

Self-contained monthly-cadence tool (run any time; ~3 min for the MC CVs):

    .venv/Scripts/python.exe tools/bubble_scan.py            # scan the default set
    .venv/Scripts/python.exe tools/bubble_scan.py ^NDX NVDA  # scan specific symbols

Method + validation: see strategy_lab/BUBBLE_DETECTION.md. Doctrine:
  - BSADF flag ON  = explosive (bubble) REGIME — can persist for YEARS; a
    sizing/hedging posture signal, never an exit timer.
  - BSADF flag OFF after a long ON stretch = pop-in-progress CONFIRMATION
    (validated: Nikkei 1990-07, NASDAQ 2001, oil 2008-08, BTC 2018-04/2021-04).
  - LPPLS tc = curiosity only. On 6/6 labeled bubbles the single-window fit was
    LATE by +5..+23 months. Do not trade tc.
"""
from __future__ import annotations

import sys
import time

import httpx
import numpy as np
import pandas as pd

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
DEFAULT = ["^GSPC", "^IXIC", "^NDX", "NVDA", "^N225", "BTC-USD"]


def fetch_yahoo(symbol: str) -> pd.Series:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1=0&period2={int(time.time())}&interval=1d")
    r = httpx.get(url, timeout=60, follow_redirects=True, headers=UA)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({"date": pd.to_datetime(res["timestamp"], unit="s", utc=True)
                       .tz_convert("America/New_York").tz_localize(None).normalize(),
                       "c": q["close"]}).dropna()
    return df.set_index("date")["c"].resample("ME").last().dropna()


# ── BSADF (lag-0 ADF, vectorized via prefix sums) ────────────────────────
def bsadf_series(y: np.ndarray):
    y = np.asarray(y, float)
    T = len(y)
    w0 = max(int(np.floor(T * (0.01 + 1.8 / np.sqrt(T)))), 12)
    dy = np.diff(y)
    x = y[:-1]
    Z = lambda a: np.concatenate([[0], np.cumsum(a)])         # noqa: E731
    P1, Px, Pxx = Z(np.ones_like(dy)), Z(x), Z(x * x)
    Pd, Pxd, Pdd = Z(dy), Z(x * dy), Z(dy * dy)
    out = np.full(T, np.nan)
    for t in range(w0, T):
        s = np.arange(0, t - w0 + 1)
        n = P1[t] - P1[s]
        Sx, Sxx = Px[t] - Px[s], Pxx[t] - Pxx[s]
        Sy, Sxy, Syy = Pd[t] - Pd[s], Pxd[t] - Pxd[s], Pdd[t] - Pdd[s]
        det = n * Sxx - Sx * Sx
        ok = det > 1e-12
        b = np.where(ok, (n * Sxy - Sx * Sy) / np.where(ok, det, 1), np.nan)
        a = (Sy - b * Sx) / n
        ssr = np.maximum(Syy - a * Sy - b * Sxy, 1e-12)
        seb = np.sqrt(ssr / np.maximum(n - 2, 1) * n / np.where(ok, det, np.nan))
        out[t] = np.nanmax(b / seb)
    return out, w0


_cv_cache: dict = {}


def bsadf_cv95(T: int, reps: int = 99, seed: int = 11) -> np.ndarray:
    if T in _cv_cache:
        return _cv_cache[T]
    rng = np.random.default_rng(seed)
    stats = np.full((reps, T), np.nan)
    for r in range(reps):
        stats[r], _ = bsadf_series(np.cumsum(rng.standard_normal(T)))
    cv = np.nanpercentile(stats, 95, axis=0)
    _cv_cache[T] = cv
    return cv


def episodes(dates, flag):
    out, start = [], None
    for i, f in enumerate(flag):
        if f and start is None:
            start = i
        elif not f and start is not None:
            if i - start >= 2:
                out.append((dates[start], dates[i - 1]))
            start = None
    if start is not None and len(flag) - start >= 2:
        out.append((dates[start], dates[-1]))
    return out


# ── LPPLS (Filimonov-Sornette; reported for context only) ────────────────
def lppls_tc(dates: pd.DatetimeIndex, prices: np.ndarray) -> str:
    t = (dates.year + (dates.dayofyear - 1) / 365.25).to_numpy()
    y = np.log(prices)
    t2, span = t[-1], t[-1] - t[0]
    one = np.ones_like(t)
    best = (np.inf, None)
    for tc in t2 + np.linspace(0.01, 0.6 * span, 40):
        dt = tc - t
        ldt = np.log(dt)
        for m in np.linspace(0.1, 0.9, 9):
            f = dt ** m
            for w in np.linspace(6, 13, 8):
                X = np.column_stack([one, f, f * np.cos(w * ldt), f * np.sin(w * ldt)])
                coef, res, *_ = np.linalg.lstsq(X, y, rcond=None)
                sse = res[0] if len(res) else np.sum((y - X @ coef) ** 2)
                if sse < best[0]:
                    best = (sse, tc)
    tc = best[1]
    if tc is None or tc > 2100:
        return "degenerate"
    return str((pd.Timestamp(f"{int(tc)}-01-01")
                + pd.Timedelta(days=(tc % 1) * 365.25)).date())


def main() -> None:
    symbols = sys.argv[1:] or DEFAULT
    for sym in symbols:
        m = fetch_yahoo(sym)
        y = np.log(m.to_numpy())
        stat, _ = bsadf_series(y)
        cv = bsadf_cv95(len(y))
        flag = stat > cv
        eps = episodes(m.index.strftime("%Y-%m").tolist(), flag)
        state = "EXPLOSIVE (bubble regime)" if flag[-1] else "not explosive"
        print(f"\n{sym}  through {m.index[-1].date()}  ->  {state}")
        for a, b in eps[-6:]:
            print(f"   episode: {a} .. {b}")
        if flag[-1]:
            w = m[m.index >= m.index[-1] - pd.DateOffset(months=30)]
            print(f"   LPPLS tc (context only, historically +5..+23mo late): "
                  f"{lppls_tc(w.index, w.to_numpy())}")


if __name__ == "__main__":
    main()
