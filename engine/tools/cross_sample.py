"""Does a sleeve keep its SIGN across independent samples?

The one question that matters after this week. Every effect measured on a single
sample this week (the gamma gate, the two-phase exit, the sweepfade hold) looked
strong and then inverted on the next contract. So the test is not "how much did
it make" -- it is "did it make money on ESH5 (Mar 2025), ESM5 (May 2025) AND the
live ES rebuild (Jul 2026), three disjoint samples on two contract cycles a year
apart".

A sleeve that flips sign between samples is not a strategy, it is a fitted
parameter. Only sign-stable sleeves are reported as candidates.

    .venv/Scripts/python.exe tools/cross_sample.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PU = 50.0
SAMPLES = {"ESH5 Mar-2025": "replay_fills_ESH5.csv",
           "ESM5 May-2025": "replay_fills_ESM5.csv",
           "ES   Jul-2026": "replay_fills_ES_live.csv"}


def daily_pnl(f: pd.DataFrame) -> pd.DataFrame:
    """Per (sleeve, session) realised $ from a position walk over the fills."""
    out = []
    for (sl, day), g in f.groupby(["s", "day"]):
        pos, avg, pnl = 0, 0.0, 0.0
        for r in g.sort_values("t").itertuples(index=False):
            side, q, p = int(r.side), int(r.qty), float(r.price)
            if pos == 0 or (pos > 0) == (side > 0):
                avg = (avg * abs(pos) + p * q) / (abs(pos) + q)
                pos += side * q
            else:
                n = min(q, abs(pos))
                pnl += (p - avg) * (1 if pos > 0 else -1) * n * PU
                pos += side * q
                if pos != 0 and (pos > 0) == (side > 0):
                    avg = p
        out.append({"s": sl, "day": day, "usd": pnl})
    return pd.DataFrame(out)


def main() -> None:
    tabs = {}
    for name, fn in SAMPLES.items():
        src = ROOT / ".cache" / fn
        if not src.exists():
            print(f"  (missing {fn})")
            continue
        f = pd.read_csv(src)
        f["s"] = f.sleeve.str.split(":").str[-1]
        f["t"] = pd.to_datetime(f.ts, unit="ns", utc=True).dt.tz_convert("America/New_York")
        f["day"] = f.t.dt.strftime("%Y-%m-%d")
        d = daily_pnl(f)
        tabs[name] = d.groupby("s").agg(
            usd=("usd", "sum"), days=("day", "nunique"),
            up=("usd", lambda x: 100.0 * (x > 0).mean()))

    sleeves = sorted(set().union(*(set(t.index) for t in tabs.values())))
    names = list(tabs)
    print(f"\n{'='*100}")
    print("CROSS-SAMPLE SIGN STABILITY — three disjoint samples, two contract cycles")
    print(f"{'='*100}")
    hdr = f"{'sleeve':<24}"
    for n in names:
        hdr += f"{n:>26}"
    print(hdr + "   verdict")
    print(f"{'':<24}" + "".join(f"{'total$':>12}{'days+%':>14}" for _ in names))

    stable, flip = [], []
    for s in sleeves:
        row, signs = f"{s:<24}", []
        for n in names:
            t = tabs[n]
            if s in t.index:
                r = t.loc[s]
                row += f"{r.usd:>+12,.0f}{r.days:>7.0f}d {r.up:>4.0f}%"
                if r.days >= 3:                 # ignore sleeves that barely fired
                    signs.append(1 if r.usd > 0 else -1)
            else:
                row += f"{'-':>12}{'':>14}"
        if len(signs) >= 2 and len(set(signs)) == 1:
            v = "STABLE +" if signs[0] > 0 else "STABLE -"
            (stable if signs[0] > 0 else flip).append(s)
        elif len(signs) >= 2:
            v, _ = "FLIPS", flip.append(s)
        else:
            v = "n/a"
        print(row + f"   {v}")

    print(f"\n{'-'*100}")
    print("SIGN-STABLE POSITIVE across every sample where it fired:")
    for s in stable:
        print(f"    {s}")
    print(f"{'='*100}\n")


if __name__ == "__main__":
    main()
