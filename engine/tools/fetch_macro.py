"""Fetch the macro gate inputs from FRED and cache them for the engine.

    DGS10   10-year Treasury constant-maturity yield
    T10YIE  10-year breakeven inflation

The gate is ON when BOTH sit below their own trailing 250-day median. Measured
over 2011-2026 (strategy_lab, 3,894 sessions): buying a 3-day dip returns +1.25%
over 5 days with the gate ON and +0.24% with it OFF -- the base rate is +0.23%,
so the dip edge exists ONLY under the gate.

Both series are published with a lag, so the value written for date D is the
last one FRED had on or before D. The sleeve additionally refuses to trade on a
file older than MAX_STALE_DAYS: a stale gate is not a neutral gate, it is
yesterday's regime asserted as today's.

Run daily (before the cash open is fine -- the inputs are prior-day closes):
    python tools/fetch_macro.py
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime
from pathlib import Path

FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"
SERIES = ("DGS10", "T10YIE")
WINDOW = 250
OUT = Path(__file__).resolve().parents[1] / "config" / "macro_gate.json"


def _series(name: str) -> list[tuple[str, float]]:
    with urllib.request.urlopen(FRED.format(name), timeout=60) as r:
        raw = r.read().decode()
    out = []
    for line in raw.splitlines()[1:]:
        d, _, v = line.partition(",")
        try:
            out.append((d.strip(), float(v)))
        except ValueError:
            continue                      # FRED writes '.' for a missing day
    return out


def build() -> dict:
    data = {n: _series(n) for n in SERIES}
    for n, s in data.items():
        if len(s) < WINDOW * 2:
            raise SystemExit(f"{n}: only {len(s)} rows, refusing to build a gate")
    dates = sorted(set(data[SERIES[0]]) & set(data[SERIES[1]]))
    idx = {n: {d: v for d, v in s} for n, s in data.items()}
    common = sorted(set(idx[SERIES[0]]) & set(idx[SERIES[1]]))
    gate: dict[str, bool] = {}
    hist = {n: [] for n in SERIES}
    for d in common:
        on = True
        for n in SERIES:
            v = idx[n][d]
            h = hist[n]
            if len(h) >= WINDOW:
                med = sorted(h[-WINDOW:])[WINDOW // 2]
                on = on and (v < med)
            else:
                on = False               # not warm yet: gate OFF, never ON by default
            h.append(v)
        gate[d] = on
    return {"asof": common[-1], "built": datetime.utcnow().isoformat(timespec="seconds"),
            "window": WINDOW, "series": list(SERIES), "gate": gate,
            "last": {n: idx[n][common[-1]] for n in SERIES}}


def main() -> None:
    g = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(g))
    on = sum(1 for v in g["gate"].values() if v)
    print(f"wrote {OUT}")
    print(f"  {len(g['gate'])} dates, gate ON {on} ({on/len(g['gate']):.0%}), "
          f"asof {g['asof']}  {g['last']}")
    print(f"  today's gate: {'ON' if g['gate'][g['asof']] else 'OFF'}")


if __name__ == "__main__":
    main()
