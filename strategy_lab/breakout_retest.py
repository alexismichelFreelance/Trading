"""Breakout entry: market-on-break vs a patient limit AT the broken level.

The manual read being tested: never buy the first bar that closes beyond a
level -- rest a limit at the level itself. If it rips you miss it; more often it
retraces to the level and hands you a clean entry with the risk defined by the
level you just reclaimed.

Two things have to be measured together, or the comparison is dishonest:

  1. FILL RATE. A better entry price on the trades you get is worthless if you
     only get a third of them. The missed breakouts are counted as ZERO, not
     dropped, so both methods are scored per BREAKOUT EVENT -- the same
     opportunity set.
  2. ADVERSE SELECTION. The retests you get filled on are not a random sample of
     breakouts: the ones that come back are disproportionately the ones that
     were going to fail. That is the whole risk of the idea, and comparing only
     filled trades would hide it.

Levels tested (each fixed BEFORE the breakout can occur, so nothing is
forward-looking): overnight high/low, opening-range 15m high/low, prior-day RTH
high/low.

Forward returns are signed in the direction of the break and measured from each
entry at +15/30/60 minutes and at the 15:59 close. No stops, no targets -- this
measures the ENTRY, which is the question. Exits are a separate conversation.

    python breakout_retest.py
"""
from __future__ import annotations

import io

import httpx
import numpy as np
import pandas as pd

URL = "http://localhost:9000"
TICK = 0.25
RETEST_WIN_MIN = 60          # how long we leave the limit resting
HORIZONS_MIN = (15, 30, 60)
RTH_OPEN, RTH_CLOSE = 9 * 60 + 30, 15 * 60 + 59
OR_MIN = 15                  # opening-range length
MIN_PENETRATION_T = 4        # ticks price must clear the level before a
                             # return counts as a retest, not oscillation


def bars(symbol: str) -> pd.DataFrame:
    sql = (f"SELECT ts,o,h,l,c,vol FROM claude_bars_1m WHERE symbol='{symbol}' "
           f"ORDER BY ts")
    r = httpx.get(f"{URL}/exp", params={"query": sql}, timeout=300.0)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    t = pd.to_datetime(df.ts, utc=True).dt.tz_convert("America/New_York")
    df["day"] = t.dt.strftime("%Y-%m-%d")
    df["min"] = t.dt.hour * 60 + t.dt.minute
    return df


def levels(prev_rth: pd.DataFrame, eth: pd.DataFrame,
           orb: pd.DataFrame) -> dict[str, tuple[float, int]]:
    """name -> (price, direction) where direction +1 means a break ABOVE it."""
    out = {}
    if len(eth):
        out["ONH"] = (eth.h.max(), +1)
        out["ONL"] = (eth.l.min(), -1)
    if len(orb):
        out["ORH"] = (orb.h.max(), +1)
        out["ORL"] = (orb.l.min(), -1)
    if len(prev_rth):
        out["PDH"] = (prev_rth.h.max(), +1)
        out["PDL"] = (prev_rth.l.min(), -1)
    return out


def session_events(day_bars: pd.DataFrame, lv: dict) -> list[dict]:
    """First bar to CLOSE beyond each level, then whether price returned to it."""
    rth = day_bars[(day_bars["min"] >= RTH_OPEN) & (day_bars["min"] <= RTH_CLOSE)]
    if len(rth) < 60:
        return []
    m = rth["min"].to_numpy()
    c, h, l = rth.c.to_numpy(), rth.h.to_numpy(), rth.l.to_numpy()
    out = []
    for name, (px, d) in lv.items():
        if not np.isfinite(px):
            continue
        # the opening range cannot be broken before it is complete
        first_ok = RTH_OPEN + OR_MIN if name.startswith("OR") else RTH_OPEN
        arm = np.flatnonzero(m >= first_ok)
        if not len(arm):
            continue
        a0 = int(arm[0])
        # A GAP IS NOT A BREAKOUT. If the session already opens beyond the level
        # there is nothing to break: the first RTH bar's close would be recorded
        # as the "break", metres away from the level, and the resulting entry
        # improvement is just the gap. Require price to start INSIDE and cross.
        if (c[a0] - px) * d > 0:
            continue
        beyond = np.flatnonzero((m >= first_ok) & ((c - px) * d > 0))
        if not len(beyond):
            continue
        i = int(beyond[0])
        mkt_px, mkt_min = c[i], m[i]
        # The retest only means something once price has actually LEFT the
        # level. Without this, a bar closing one tick beyond while the next bar
        # wicks back counts as a "retrace", which is oscillation at the level,
        # not the retracement being traded.
        j_end = np.searchsorted(m, mkt_min + RETEST_WIN_MIN, side="right")
        seg_i = np.arange(i + 1, j_end)
        ret_min = None
        if len(seg_i):
            run = (c[seg_i] - px) * d / TICK            # excursion beyond level
            armed = np.flatnonzero(run >= MIN_PENETRATION_T)
            if len(armed):
                s = seg_i[armed[0]]                     # first bar truly beyond
                back = np.arange(s + 1, j_end)
                if len(back):
                    touched = (l[back] <= px) if d > 0 else (h[back] >= px)
                    hit = np.flatnonzero(touched)
                    ret_min = int(m[back[hit[0]]]) if len(hit) else None
        ev = {"level": name, "dir": d, "px": px, "mkt_px": mkt_px,
              "mkt_min": mkt_min, "filled": ret_min is not None,
              "ret_min": ret_min}
        for hz in HORIZONS_MIN:
            k = np.searchsorted(m, mkt_min + hz, side="right") - 1
            ev[f"mkt_{hz}"] = (c[k] - mkt_px) * d / TICK
            if ret_min is not None:
                k2 = np.searchsorted(m, ret_min + hz, side="right") - 1
                ev[f"ret_{hz}"] = (c[k2] - px) * d / TICK
        # MAE: how far underwater each entry goes before the horizon. This is
        # what sets the stop and therefore the SIZE, so a smaller MAE at equal
        # return is a strictly better trade even when the P&L looks identical.
        w = np.arange(i + 1, min(i + 1 + 60, len(m)))
        if len(w):
            adv = (l[w] - mkt_px) * d / TICK if d > 0 else (h[w] - mkt_px) * d / TICK
            ev["mae_mkt"] = float(adv.min())
        if ret_min is not None:
            r0 = int(np.searchsorted(m, ret_min, side="left"))
            w2 = np.arange(r0 + 1, min(r0 + 1 + 60, len(m)))
            if len(w2):
                adv2 = (l[w2] - px) * d / TICK if d > 0 else (h[w2] - px) * d / TICK
                ev["mae_ret"] = float(adv2.min())
        ev["mkt_eod"] = (c[-1] - mkt_px) * d / TICK
        ev["ret_eod"] = ((c[-1] - px) * d / TICK) if ret_min is not None else np.nan
        out.append(ev)
    return out


def main() -> None:
    allev = []
    for sym in ("ESH5", "ESM5"):
        b = bars(sym)
        days = sorted(b.day.unique())
        for k, day in enumerate(days):
            d = b[b.day == day]
            eth = d[d["min"] < RTH_OPEN]
            orb = d[(d["min"] >= RTH_OPEN) & (d["min"] < RTH_OPEN + OR_MIN)]
            prev = b[b.day == days[k - 1]] if k else b.iloc[0:0]
            prev_rth = prev[(prev["min"] >= RTH_OPEN) & (prev["min"] <= RTH_CLOSE)]
            for e in session_events(d, levels(prev_rth, eth, orb)):
                e.update(sym=sym, day=day)
                allev.append(e)
    E = pd.DataFrame(allev)

    print(f"\n{'='*104}")
    print(f"BREAKOUT ENTRY — market-on-break vs resting limit AT the level")
    print(f"{E.day.nunique()} sessions, {len(E)} breakout events, "
          f"{RETEST_WIN_MIN}min retest window, returns in TICKS")
    print(f"{'='*104}")
    print(f"  {'level':<8}{'events':>7}{'fill%':>7}   " +
          "".join(f"{'MKT '+str(h)+'m':>12}{'RETEST '+str(h)+'m':>15}"
                  for h in HORIZONS_MIN))
    for name, g in E.groupby("level"):
        row = f"  {name:<8}{len(g):>7}{100*g.filled.mean():>6.0f}%   "
        for hz in HORIZONS_MIN:
            mk = g[f"mkt_{hz}"].mean()
            # per-EVENT: a missed retest contributes 0, same opportunity set
            rt = g[f"ret_{hz}"].fillna(0.0).mean()
            row += f"{mk:>+12.2f}{rt:>+15.2f}"
        print(row)

    print(f"\n  ALL LEVELS POOLED")
    print(f"  {'horizon':<10}{'MKT mean':>11}{'MKT win%':>10}"
          f"{'RETEST/event':>14}{'RETEST|filled':>15}{'win%|filled':>13}")
    for hz in list(HORIZONS_MIN) + ["eod"]:
        mk, rt = E[f"mkt_{hz}"], E[f"ret_{hz}"]
        f_ = rt.dropna()
        print(f"  {str(hz)+'m':<10}{mk.mean():>+11.2f}{100*(mk>0).mean():>9.0f}%"
              f"{rt.fillna(0).mean():>+14.2f}{f_.mean():>+15.2f}"
              f"{100*(f_>0).mean():>12.0f}%")

    print(f"\n  fill rate overall {100*E.filled.mean():.0f}%  "
          f"({int(E.filled.sum())} of {len(E)} breakouts retraced to the level "
          f"within {RETEST_WIN_MIN}min)")
    print(f"  median wait to retest "
          f"{np.nanmedian((E.ret_min - E.mkt_min).to_numpy(dtype=float)):.0f}min")
    ent = (E.mkt_px - E.px) * E.dir / TICK
    print(f"  entry improvement when filled: {ent[E.filled].mean():+.2f} ticks "
          f"better than the market entry (that is the prize)")
    print(f"{'='*104}\n")


if __name__ == "__main__":
    main()
