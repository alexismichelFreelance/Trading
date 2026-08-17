"""What does the book look like with ONE sleeve per bet?

The replay report lists 35 ES rows totalling +108,304, and that number is
fiction: five of the rows are trendjoin, four are opendrive, four are onbreak,
nine are vwapbreak. Twins are the SAME signal with a different exit -- they win
and lose together, so summing them multiplies one bet and makes the book look
both bigger and more diversified than it is.

This picks one representative per family and reports what matters for deciding
whether to trade something, which is not the total:

    days      how often it acts at all -- a +5,000 sleeve that trades 5 days is
              a different proposition from one that trades 32
    $/day     the rate, comparable across sleeves with different activity
    sign%     how often a day is positive
    worst     the worst single session, because that is what actually hurts
    corr      pairwise daily correlation between the survivors: a "diversified"
              book of six sleeves that all trade the same idea is one bet

and then combines them equal-weight to see whether the group is steadier than
its parts, which is the only reason to run more than one.

IN SAMPLE. One 33-session window, and several of these were chosen on this same
window. Treat rankings as description, not prediction.
"""
from __future__ import annotations

import collections
import csv
import datetime as dt
import itertools
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ROOT))

ET = dt.timezone(dt.timedelta(hours=-4))


def point_usd(sym: str) -> float:
    """$ per point, from the instrument registry -- NEVER a literal.

    This module hardcoded 50.0 and so reported every NQ figure 2.5x too large
    (NQ is $20/pt): trendjoin_narrow read +92,312 against the replay's +36,925.
    The same class of error as the ES-scaled thresholds that made a 15pt
    confirmation meaningless on an instrument ranging 7x wider."""
    try:
        from engine.core.config import load_instruments, root_symbol
        inst = load_instruments(ROOT / "config" / "instruments.yaml")
        return float(inst[root_symbol(sym)].point_usd)
    except Exception:                                  # noqa: BLE001
        return 50.0

# one per family. Where a family has a settled representative that choice is
# recorded in tests/test_roster_decisions.py; the rest are the highest-activity
# member, NOT the highest-scoring, to avoid picking on this window's outcome.
FAMILIES = ["zones_gap", "trendjoin_narrow", "opendrive_2p24",
            "onbreak_2p_retrace", "pivot", "vwapbreak", "ignition_fixed",
            "flow", "wallfade"]


def book_for(sym: str) -> list:
    return [f"{sym}:{f}" for f in FAMILIES]


def daily(rows, sleeve, pv):
    """Per-day P&L, position RESET each session -- matching portfolio_replay's
    own report(), which calls pnl_of() on one day's fills at a time.

    Carrying position across days looks more correct and is not: a single
    session that ends non-flat mis-pairs every entry that follows it, and the
    error cascades for the rest of the window. ES:opendrive_2p24 has exactly one
    such day and read +13,344 carried against +700 reset -- the entire apparent
    edge was one dangling contract propagating through 40 sessions.

    The cost of resetting is that genuine overnight sleeves (ibs, rsi2) are
    unmeasurable here, which portfolio_replay.build_strategies already documents
    and which is why they report 0 rather than a number."""
    import collections as _c
    byday = _c.defaultdict(list)
    for r in rows:
        if r["sleeve"] != sleeve:
            continue
        day = dt.datetime.fromtimestamp(int(float(r["ts"])) / 1e9,
                                        ET).strftime("%Y-%m-%d")
        byday[day].append(r)
    out = _c.Counter()
    for day, fs in byday.items():
        pos, avg = 0, 0.0
        for r in fs:
            q = int(float(r["side"])) * int(float(r["qty"]))
            px = float(r["price"])
            if pos and (q > 0) != (pos > 0):
                m = min(abs(q), abs(pos))
                out[day] += (px - avg) * (1 if pos > 0 else -1) * m * pv
                new = pos + q
                if new and (new > 0) != (pos > 0):
                    avg = px
                pos = new
            else:
                avg = ((avg * abs(pos) + px * abs(q)) / (abs(pos) + abs(q))
                       if pos else px)
                pos += q
    return out


def main(sym: str = "ES") -> None:
    rows = sorted(csv.DictReader(
        open(ROOT / ".cache" / f"replay_fills_{sym}_live.csv", encoding="utf-8-sig")),
        key=lambda r: float(r["ts"]))
    pv = point_usd(sym)
    series = {s: daily(rows, s, pv) for s in book_for(sym)}
    series = {k: v for k, v in series.items() if v}
    allday = sorted({d for v in series.values() for d in v})

    print(f"\nONE SLEEVE PER BET — {sym}, {len(allday)} sessions\n")
    print(f"  {'sleeve':24} {'days':>5} {'total $':>9} {'$/day':>7} "
          f"{'sign%':>6} {'worst':>8}")
    for s, v in sorted(series.items(), key=lambda kv: -sum(kv[1].values())):
        vals = list(v.values())
        print(f"  {s:24} {len(vals):>5} {sum(vals):>9,.0f} "
              f"{sum(vals)/len(vals):>7,.0f} "
              f"{100*np.mean([x>0 for x in vals]):>5.0f}% {min(vals):>8,.0f}")
    tot = sum(sum(v.values()) for v in series.values())
    print(f"  {'BOOK':24} {len(allday):>5} {tot:>9,.0f} {tot/len(allday):>7,.0f}")

    # ── is it actually diversified? ──────────────────────────────────────
    print("\n  PAIRWISE DAILY CORRELATION (only pairs sharing >=6 days)")
    names = list(series)
    shown = 0
    for a, b in itertools.combinations(names, 2):
        common = sorted(set(series[a]) & set(series[b]))
        if len(common) < 6:
            continue
        x = np.array([series[a][d] for d in common])
        y = np.array([series[b][d] for d in common])
        if x.std() == 0 or y.std() == 0:
            continue
        r = float(np.corrcoef(x, y)[0, 1])
        if abs(r) >= 0.35:
            print(f"    {r:+.2f}  {a} ~ {b}   (n={len(common)})")
            shown += 1
    if not shown:
        print("    nothing above |0.35| -- the survivors are largely independent")

    # ── the group, equal weight ──────────────────────────────────────────
    eq = np.array([sum(series[s].get(d, 0.0) for s in series) for d in allday])
    cum = np.cumsum(eq)
    dd = cum - np.maximum.accumulate(cum)
    print(f"\n  BOOK AS ONE LINE: {len(allday)} sessions")
    print(f"    total {eq.sum():>10,.0f}   mean/day {eq.mean():>8,.0f}   "
          f"sd {eq.std():>8,.0f}")
    print(f"    positive days {100*np.mean(eq>0):>3.0f}%   best {eq.max():>8,.0f}   "
          f"worst {eq.min():>8,.0f}")
    print(f"    max drawdown {dd.min():>8,.0f}   "
          f"return/risk {eq.mean()/eq.std() if eq.std() else 0:>5.2f} per day")
    print(f"\n    Sharpe-like ratio is per-DAY and in-sample on one window; it is")
    print(f"    a description of these 33 sessions, not an expectation.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ES")
