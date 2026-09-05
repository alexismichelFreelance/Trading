"""Write the macrodip warm-start seed for a symbol.

WHY THIS EXISTS. macro_dip decides on the TRAILING 250-day percentile of the
3-day return. From a cold start that needs 253 sessions -- a year -- during
which the sleeve sits inert while looking perfectly healthy in the roster. The
seed hands it that history on the first bar. The NQ seed was simply never
generated, so the NQ sleeve has been logging MACRODIP SEED MISSING and not
trading.

The seed is daily CLOSES only, because that is all the percentile needs. It is
deliberately longer than the 250-day window so a few missing sessions or a late
start cannot leave the window short.

    python tools/gen_macrodip_seed.py NQ
    python tools/gen_macrodip_seed.py ES --days 400
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {"ES": "gamma/esf_daily.csv", "NQ": "gamma/nqf_daily.csv"}
NEED = 253                     # PCTL_WIN 250 + LOOKBACK 3


def build(symbol: str, days: int) -> dict:
    rel = SOURCES[symbol]
    src = Path("D:/Trading") / rel
    if not src.exists():
        raise SystemExit(f"no daily file at {src}")
    d = pd.read_csv(src)
    d["date"] = pd.to_datetime(d["date"])
    d = d.dropna(subset=["c"]).sort_values("date").tail(days)
    if len(d) < NEED:
        raise SystemExit(f"{symbol}: only {len(d)} closes, need >= {NEED}")
    return {"symbol": symbol, "source": rel,
            "closes": [{"day": r.date.strftime("%Y-%m-%d"), "c": float(r.c)}
                       for r in d.itertuples()]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol", choices=sorted(SOURCES))
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    seed = build(a.symbol, a.days)
    out = Path(a.out) if a.out else ROOT / "config" / f"macrodip_seed_{a.symbol}.json"
    out.write_text(json.dumps(seed, indent=1))
    c = seed["closes"]
    print(f"{a.symbol}: {len(c)} closes {c[0]['day']} .. {c[-1]['day']} -> {out}")
    stale = (pd.Timestamp.now().normalize() - pd.Timestamp(c[-1]["day"])).days
    if stale > 7:
        print(f"  WARNING: last close is {stale} days old -- refresh {seed['source']}")


if __name__ == "__main__":
    main()
