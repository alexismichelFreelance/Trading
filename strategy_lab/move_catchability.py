"""Of a big leg, how much is LEFT once you can tell it is a big leg?

move_catalog.py established the target: ~3.8 legs of 20+pt per session, median
39.8pt, median 47 minutes. This asks whether any of that is reachable.

For every leg, walk forward from its start and find the moment price has moved
CONF points in the leg's direction -- the earliest a trend-follower could act
with that much confirmation. Then measure what happens NEXT:

    remaining   the move still to come, from the confirmation price
    mae         how far underwater it goes after entry, before the extreme
    mins_left   how long you still have to hold

If `remaining` collapses as CONF rises, these moves are unreachable by
confirmation and only a predictive entry can work. If it holds up, the trade is
simply "join late and hold", and the reason nothing in the book catches these is
the exits, not the entries.

HONESTY: leg boundaries come from a zigzag that uses the whole session, so
membership in "big leg" is hindsight. This measures the CEILING -- what a
perfect leg-detector would have left to capture. Nothing here is a tradeable
result on its own; it sizes the prize before anything is built to chase it.

    python move_catchability.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import breakout_retest as B
import move_catalog as M

CONFS = (5.0, 10.0, 15.0, 20.0)


def main() -> None:
    rows = []
    for sym in ("ESH5", "ESM5"):
        b = B.bars(sym)
        for day, g in b.groupby("day"):
            r = g[(g["min"] >= M.RTH_OPEN) & (g["min"] <= M.RTH_CLOSE)].sort_values("min")
            if len(r) < 60:
                continue
            rng = r.h.max() - r.l.min()
            thr = max(6.0, M.RETRACE_FRAC * rng)
            m = r["min"].to_numpy()
            h, l, c = r.h.to_numpy(), r.l.to_numpy(), r.c.to_numpy()
            for L in M.legs(r, thr):
                d = L["dir"]
                s = int(np.searchsorted(m, L["start_min"]))
                e = int(np.searchsorted(m, L["end_min"]))
                if e <= s:
                    continue
                start_px = h[s] if d < 0 else l[s]     # the leg's origin extreme
                ext_px = l[e] if d < 0 else h[e]
                for conf in CONFS:
                    # first bar whose CLOSE is conf points into the move
                    k = None
                    for j in range(s, e + 1):
                        if (c[j] - start_px) * d >= conf:
                            k = j
                            break
                    if k is None:
                        continue
                    entry = c[k]
                    remaining = (ext_px - entry) * d
                    seg = slice(k, e + 1)
                    adv = ((l[seg] - entry) * d if d > 0 else (h[seg] - entry) * d)
                    rows.append({"sym": sym, "day": day, "pts": L["pts"],
                                 "conf": conf, "remaining": remaining,
                                 "mae": float(adv.min()) if len(adv) else 0.0,
                                 "mins_left": int(m[e] - m[k]),
                                 "big": L["pts"] >= 20})
    R = pd.DataFrame(rows)

    print(f"\n{'='*98}")
    print("MOVE CATCHABILITY — what is left after N points of confirmation")
    print(f"{'='*98}")
    for big, lab in ((True, "legs >= 20pt"), (False, "legs < 20pt")):
        G = R[R.big == big]
        if G.empty:
            continue
        print(f"\n  {lab}")
        print(f"  {'confirm':<10}{'legs reached':>14}{'med remaining':>15}"
              f"{'med MAE':>10}{'med mins left':>15}{'remain/MAE':>12}")
        for conf in CONFS:
            g = G[G.conf == conf]
            if g.empty:
                continue
            tot = G[G.conf == CONFS[0]].shape[0]
            rm, ma = g.remaining.median(), g.mae.median()
            print(f"  {conf:>5.0f} pt   {len(g):>7} ({100*len(g)/max(tot,1):>3.0f}%)"
                  f"{rm:>15.1f}{ma:>10.1f}{g.mins_left.median():>15.0f}"
                  f"{(rm/abs(ma) if ma else np.inf):>12.2f}")

    print(f"\n  THE 40+ POINT LEGS specifically (the ones worth the risk)")
    G = R[R.pts >= 40]
    print(f"  {'confirm':<10}{'legs':>7}{'med remaining':>15}{'med MAE':>10}"
          f"{'med mins left':>15}{'p25 remaining':>15}")
    for conf in CONFS:
        g = G[G.conf == conf]
        if g.empty:
            continue
        print(f"  {conf:>5.0f} pt   {len(g):>7}{g.remaining.median():>15.1f}"
              f"{g.mae.median():>10.1f}{g.mins_left.median():>15.0f}"
              f"{g.remaining.quantile(0.25):>15.1f}")

    print(f"\n  Read: at 10pt confirmation a >=20pt leg still has its median")
    print(f"  remaining move to give, and MAE says how much heat you take to hold it.")
    print(f"  remain/MAE under ~1.5 means the entry is not worth the stop it needs.")
    print(f"{'='*98}\n")
    R.to_pickle(".cache_catch.pkl")


if __name__ == "__main__":
    main()
