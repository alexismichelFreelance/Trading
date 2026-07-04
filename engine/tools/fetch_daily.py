"""Fetch daily OHLC history (Yahoo chart API) for the swing sleeve's oracle.

    .venv/Scripts/python.exe tools/fetch_daily.py          # ES=F and SPY

Writes gamma/esf_daily.csv and gamma/spy_daily.csv (gitignored; rerun to
refresh). Used by the IBS parity oracle and future daily studies.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT.parent / "gamma"
SYMS = {"ES=F": "esf_daily.csv", "SPY": "spy_daily.csv"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def fetch(symbol: str) -> pd.DataFrame:
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
           f"?period1=1262304000&period2={int(time.time())}&interval=1d")
    r = httpx.get(url, timeout=60, follow_redirects=True, headers=UA)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    df = pd.DataFrame({
        "date": pd.to_datetime(res["timestamp"], unit="s", utc=True)
                  .tz_convert("America/New_York").strftime("%Y-%m-%d"),
        "o": q["open"], "h": q["high"], "l": q["low"], "c": q["close"],
    }).dropna()
    return df[df.h > df.l].reset_index(drop=True)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for sym, fname in SYMS.items():
        df = fetch(sym)
        df.to_csv(OUT / fname, index=False)
        print(f"{sym}: {len(df)} rows {df.date.iloc[0]}..{df.date.iloc[-1]} -> {OUT / fname}")


if __name__ == "__main__":
    main()
