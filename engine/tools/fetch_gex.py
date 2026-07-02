"""Fetch SqueezeMetrics daily DIX/GEX and load it into QuestDB (claude_gex).

Free daily SPX aggregate gamma exposure (2011 -> present, EOD). Rerun any time
(idempotent: drops and rebuilds the table); run daily in live to keep the
gamma regime feature current.

    .venv/Scripts/python.exe tools/fetch_gex.py

Columns written: date, spx_close, dix, gex, gexp (trailing-252-session
percentile of gex, CAUSAL — excludes the current day), dixp (same for dix).
Day D's trading uses row D-1 (prior EOD) — the GammaRegime feature handles that.
"""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB   # noqa: E402

URL = "https://squeezemetrics.com/monitor/static/DIX.csv"
TABLE = "claude_gex"


def trailing_pct(s: pd.Series, window: int = 253) -> pd.Series:
    return s.rolling(window).apply(lambda w: (w.iloc[:-1] < w.iloc[-1]).mean(), raw=False)


def main() -> None:
    csv_path = ROOT.parent / "gamma" / "DIX.csv"
    csv_path.parent.mkdir(exist_ok=True)
    try:
        print(f"downloading {URL} ...")
        r = httpx.get(URL, timeout=60, follow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        r.raise_for_status()
        csv_path.write_bytes(r.content)
    except Exception as ex:                              # noqa: BLE001
        if csv_path.exists():
            print(f"download failed ({ex}); using existing {csv_path}")
        else:
            raise

    df = pd.read_csv(csv_path).sort_values("date").reset_index(drop=True)
    df["gexp"] = trailing_pct(df["gex"])
    df["dixp"] = trailing_pct(df["dix"])
    print(f"{len(df)} rows {df.date.min()} .. {df.date.max()}")

    q = QuestDB()
    q.query(f"DROP TABLE IF EXISTS {TABLE}")
    q.query(f"CREATE TABLE {TABLE} (ts TIMESTAMP, spx_close DOUBLE, dix DOUBLE, "
            f"gex DOUBLE, gexp DOUBLE, dixp DOUBLE) TIMESTAMP(ts) PARTITION BY YEAR")
    rows = []
    for r_ in df.itertuples():
        gexp = "null" if pd.isna(r_.gexp) else f"{r_.gexp:.4f}"
        dixp = "null" if pd.isna(r_.dixp) else f"{r_.dixp:.4f}"
        rows.append(f"('{r_.date}T00:00:00.000000Z', {r_.price}, {r_.dix}, {r_.gex}, {gexp}, {dixp})")
    for i in range(0, len(rows), 100):     # small batches: /exec is a GET URL
        q.query(f"INSERT INTO {TABLE} VALUES " + ",".join(rows[i:i + 100]))
    n = q.df(f"SELECT count() n FROM {TABLE}")["n"].iloc[0]
    print(f"loaded {n} rows into {TABLE}")


if __name__ == "__main__":
    main()
