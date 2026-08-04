"""Move catalogue + catchability for ANY instrument, from any bar table.

move_catalog.py / move_catchability.py hard-wired the 2025 ESH5+ESM5 tape. The
parameters TrendJoinStrategy ships with (conf 15pt, stop 8pt, hold 90min) were
read off that one window, and two things follow that the original scripts cannot
answer:

  1. Do they survive OUT OF SAMPLE? claude_bars_live holds 43 ES sessions from
     Jun-Aug 2026 -- a different year, a different contract, the live feed rather
     than the MBO tape.
  2. What are they for NQ? They are ES POINTS. NQ ranges several times wider, so
     transplanting 15/8 unscaled would be meaningless. NQ has to be measured on
     NQ.

Same method as before, unchanged, so the numbers are comparable:
    legs      zigzag, reversal threshold = RETRACE_FRAC of the SESSION range
    conf      the confirmation level where big legs still qualify but small ones
              stop qualifying -- the filter IS the wait
    stop      the median MAE after that confirmation
    hold      the median remaining duration

    python move_catalog_any.py
"""
from __future__ import annotations

import io

import httpx
import numpy as np
import pandas as pd

import move_catalog as M

URL = "http://localhost:9000"
RTH_OPEN, RTH_CLOSE = 9 * 60 + 30, 15 * 60 + 59
CONFS_FRAC = (0.10, 0.20, 0.30, 0.40)      # confirmation as a fraction of the
                                           # session range, so it ports across
                                           # instruments without hand-tuning

SOURCES = [
    ("ES 2025 (MBO tape)", "claude_bars_1m", ("ESH5", "ESM5")),
    ("ES 2026 (live feed)", "claude_bars_live", ("ES",)),
    ("NQ 2026 (live feed)", "claude_bars_live", ("NQ",)),
]


def bars(table: str, symbol: str) -> pd.DataFrame:
    sql = f"SELECT ts,o,h,l,c FROM {table} WHERE symbol='{symbol}' ORDER BY ts"
    r = httpx.get(f"{URL}/exp", params={"query": sql}, timeout=900.0)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    t = pd.to_datetime(df.ts, utc=True).dt.tz_convert("America/New_York")
    df["day"] = t.dt.strftime("%Y-%m-%d")
    df["min"] = t.dt.hour * 60 + t.dt.minute
    return df


def analyse(label: str, table: str, symbols: tuple[str, ...]) -> None:
    legs, catch = [], []
    for sym in symbols:
        b = bars(table, sym)
        for day, g in b.groupby("day"):
            r = g[(g["min"] >= RTH_OPEN) & (g["min"] <= RTH_CLOSE)].sort_values("min")
            if len(r) < 120:
                continue
            rng = r.h.max() - r.l.min()
            if rng <= 0:
                continue
            thr = max(1e-9, M.RETRACE_FRAC * rng)
            m = r["min"].to_numpy()
            h, l, c = r.h.to_numpy(), r.l.to_numpy(), r.c.to_numpy()
            for L in M.legs(r, thr):
                d, s = L["dir"], int(np.searchsorted(m, L["start_min"]))
                e = int(np.searchsorted(m, L["end_min"]))
                if e <= s:
                    continue
                L.update(day=day, sym=sym, rng=rng)
                legs.append(L)
                start_px = h[s] if d < 0 else l[s]
                ext_px = l[e] if d < 0 else h[e]
                for cf in CONFS_FRAC:
                    conf = cf * rng
                    k = None
                    for j in range(s, e + 1):
                        if (c[j] - start_px) * d >= conf:
                            k = j
                            break
                    if k is None:
                        continue
                    entry = c[k]
                    seg = slice(k, e + 1)
                    adv = (l[seg] - entry) * d if d > 0 else (h[seg] - entry) * d
                    catch.append({"cf": cf, "pts": L["pts"], "rng": rng,
                                  "conf_pts": conf,
                                  "remaining": (ext_px - entry) * d,
                                  "mae": float(adv.min()) if len(adv) else 0.0,
                                  "mins_left": int(m[e] - m[k]),
                                  "big": L["pts"] >= 0.5 * rng})
    Lg, C = pd.DataFrame(legs), pd.DataFrame(catch)
    nd = Lg.day.nunique()
    med_rng = Lg.rng.median()
    big = Lg[Lg.pts >= 0.5 * Lg.rng]

    print(f"\n{'='*94}")
    print(f"{label} — {nd} sessions, median RTH range {med_rng:.1f} pt")
    print(f"{'='*94}")
    print(f"  legs/session {len(Lg)/nd:.1f}   'big' (>=50% of range) "
          f"{len(big)/nd:.2f}/session, median {big.pts.median():.1f} pt, "
          f"{big.mins.median():.0f} min")
    print(f"\n  {'confirm':<22}{'big reach':>11}{'small reach':>13}"
          f"{'med remain':>12}{'med MAE':>10}{'med mins':>10}")
    for cf in CONFS_FRAC:
        g = C[C.cf == cf]
        if g.empty:
            continue
        gb, gs = g[g.big], g[~g.big]
        nb = len(big)
        ns = len(Lg) - nb
        print(f"  {cf:>5.0%} of range = {cf*med_rng:>5.1f}pt "
              f"{100*len(gb)/max(nb,1):>10.0f}%{100*len(gs)/max(ns,1):>12.0f}%"
              f"{gb.remaining.median():>12.1f}{gb.mae.median():>10.1f}"
              f"{gb.mins_left.median():>10.0f}")
    print(f"\n  -> the useful confirmation is the row where 'big reach' stays high")
    print(f"     while 'small reach' collapses; stop ~= |med MAE|, hold ~= med mins.")


def main() -> None:
    for label, table, syms in SOURCES:
        try:
            analyse(label, table, syms)
        except Exception as ex:
            print(f"\n{label}: FAILED {type(ex).__name__}: {ex}")
    print(f"\n{'='*94}\n")


if __name__ == "__main__":
    main()
