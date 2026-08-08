"""What did the 09:30-10:00 window actually DO, and does its shape predict the day?

OpenDriveStrategy takes one number: sign(price@10:00 - price@09:30). A window
that ground straight up and a window that spiked up, collapsed through the open
and limped back to +1 tick produce the SAME signal. So does a window that faked
down 20 points and recovered. The sleeve cannot tell them apart because it never
looks at the path.

This measures the path. For each session:

  travel   sum of |close-to-close| moves inside the window -- distance walked
  net      close(10:00) - open(09:30)                     -- distance covered
  ER       |net| / travel, 0..1. 1.0 = one clean drive, no give-back.
           ~0.1 = walked ten times as far as it got.
  legs     zigzag segmentation with a threshold scaled to the WINDOW's own
           range, never a fixed point value -- a fixed threshold shreds a wide
           day into dozens of legs and finds none on a quiet one.
  shape    DRIVE      1 leg
           PULLBACK   2 legs, net keeps the first leg's direction
           FAKEOUT    net OPPOSITE to the first leg -- the case that ravages it
           CHOP       3+ legs
  adverse  worst excursion against the eventual signal direction, in points,
           inside the window: how much the "drive" was already wrong by 10:00

Then it joins each session's shape to what ES:opendrive actually made that day
in the replay, so the question is answered with outcomes rather than adjectives.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB   # noqa: E402

ET = "America/New_York"
OPEN_MIN, ENTRY_MIN = 9 * 60 + 30, 10 * 60
LEG_FRAC = 0.25          # a leg must retrace 25% of the window range to count
PV = 50.0


def zigzag(px: np.ndarray, thr: float) -> list[int]:
    """Indices of the alternating pivots.

    The move must be measured from the running extreme BEFORE that extreme is
    advanced -- updating `ext` to `i` and then computing px[i] - px[ext] is
    always zero, so no reversal can ever register and every window comes back as
    one clean leg. Alternation is enforced: a continuation extends the current
    leg, only a `thr` reversal starts a new one."""
    n = len(px)
    if n < 2 or thr <= 0:
        return [0, n - 1]
    piv = [0]
    direction = 0
    ext = 0
    for i in range(1, n):
        if direction > 0:
            if px[i] >= px[ext]:
                ext = i
            elif px[ext] - px[i] >= thr:
                piv.append(ext); ext = i; direction = -1
        elif direction < 0:
            if px[i] <= px[ext]:
                ext = i
            elif px[i] - px[ext] >= thr:
                piv.append(ext); ext = i; direction = 1
        else:                                   # not yet committed to a side
            if px[i] - px[:i + 1].min() >= thr:
                direction = 1
                ext = i
                piv[0] = int(np.argmin(px[:i + 1]))
            elif px[:i + 1].max() - px[i] >= thr:
                direction = -1
                ext = i
                piv[0] = int(np.argmax(px[:i + 1]))
    if piv[-1] != n - 1:
        piv.append(n - 1)
    return piv


def classify(px: np.ndarray) -> dict:
    hi, lo = float(px.max()), float(px.min())
    rng = hi - lo
    net = float(px[-1] - px[0])
    travel = float(np.abs(np.diff(px)).sum())
    er = abs(net) / travel if travel > 0 else 0.0
    piv = zigzag(px, LEG_FRAC * rng)
    legs = max(1, len(piv) - 1)
    sgn = 1 if net > 0 else (-1 if net < 0 else 0)
    first = px[piv[1]] - px[piv[0]] if len(piv) > 1 else net
    first_sgn = 1 if first > 0 else (-1 if first < 0 else 0)
    if legs == 1:
        shape = "DRIVE"
    elif sgn and first_sgn and sgn != first_sgn:
        shape = "FAKEOUT"
    elif legs == 2:
        shape = "PULLBACK"
    else:
        shape = "CHOP"
    # how wrong the signal already was, inside its own measuring window
    adverse = (px[0] - lo) if sgn > 0 else (hi - px[0]) if sgn < 0 else 0.0
    return dict(rng=rng, net=net, travel=travel, er=er, legs=legs, shape=shape,
                adverse=float(adverse), sgn=sgn)


def main(symbol: str = "ES") -> None:
    q = QuestDB(timeout=120)
    b = q.df(f"SELECT ts,o,h,l,c FROM claude_bars_live WHERE symbol='{symbol}' ORDER BY ts")
    b["et"] = pd.to_datetime(b["ts"], utc=True).dt.tz_convert(ET)
    b["mod"] = b["et"].dt.hour * 60 + b["et"].dt.minute
    b["day"] = b["et"].dt.strftime("%Y-%m-%d")
    win = b[(b["mod"] >= OPEN_MIN) & (b["mod"] <= ENTRY_MIN)]

    # what the sleeve actually made each day, from the replay
    fills = pd.read_csv(ROOT.parent / "engine" / ".cache" / f"replay_fills_{symbol}_live.csv")
    fills["day"] = (pd.to_datetime(fills["ts"], utc=True).dt.tz_convert(ET)
                    .dt.strftime("%Y-%m-%d"))
    od = fills[fills["sleeve"] == f"{symbol}:opendrive"]
    pnl = {}
    pos, avg = 0, 0.0
    for r in od.sort_values("ts").itertuples():
        qy = r.side * r.qty
        if pos and (qy > 0) != (pos > 0):
            pnl[r.day] = pnl.get(r.day, 0.0) + (r.price - avg) * (1 if pos > 0 else -1) * \
                min(abs(qy), abs(pos)) * PV
            pos += qy
            if pos:
                avg = r.price
        else:
            avg = (avg * abs(pos) + r.price * abs(qy)) / (abs(pos) + abs(qy)) if pos else r.price
            pos += qy

    rows = []
    for day, g in win.groupby("day"):
        if len(g) < 25:
            continue
        c = classify(g["c"].to_numpy())
        c["day"] = day
        c["pnl"] = pnl.get(day, np.nan)
        rows.append(c)
    d = pd.DataFrame(rows)
    traded = d[d["pnl"].notna()]

    print(f"\nOPEN-DRIVE WINDOW SHAPE — {symbol}, {len(d)} sessions "
          f"({len(traded)} with an opendrive trade)\n")
    print(f"{'shape':10} {'n':>3} {'traded':>6} {'total$':>9} {'$/day':>8} "
          f"{'win%':>6} {'medER':>6} {'medlegs':>7} {'medadv':>7}")
    for sh, g in d.groupby("shape"):
        t = g[g["pnl"].notna()]
        print(f"{sh:10} {len(g):>3} {len(t):>6} "
              f"{t['pnl'].sum() if len(t) else 0:>9,.0f} "
              f"{t['pnl'].mean() if len(t) else 0:>8,.0f} "
              f"{(t['pnl'] > 0).mean() * 100 if len(t) else 0:>5.0f}% "
              f"{g['er'].median():>6.2f} {g['legs'].median():>7.0f} "
              f"{g['adverse'].median():>7.1f}")

    print("\nBY EFFICIENCY RATIO (how much of the walk the window kept):")
    if len(traded) > 4:
        traded = traded.copy()
        traded["bucket"] = pd.qcut(traded["er"], min(4, traded["er"].nunique()),
                                   duplicates="drop")
        for bk, g in traded.groupby("bucket", observed=True):
            print(f"  ER {str(bk):22} n={len(g):>3}  total {g['pnl'].sum():>9,.0f}  "
                  f"mean {g['pnl'].mean():>8,.0f}  win {(g['pnl'] > 0).mean() * 100:>3.0f}%")

    print("\nWORST OPEN-DRIVE DAYS — what the window looked like:")
    for r in traded.nsmallest(6, "pnl").itertuples():
        print(f"  {r.day}  {r.shape:9} legs={r.legs}  ER={r.er:.2f}  "
              f"net={r.net:+7.2f}  travel={r.travel:6.1f}  adverse={r.adverse:5.1f}  "
              f"pnl={r.pnl:>8,.0f}")
    print("\nBEST:")
    for r in traded.nlargest(4, "pnl").itertuples():
        print(f"  {r.day}  {r.shape:9} legs={r.legs}  ER={r.er:.2f}  "
              f"net={r.net:+7.2f}  travel={r.travel:6.1f}  adverse={r.adverse:5.1f}  "
              f"pnl={r.pnl:>8,.0f}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ES")
