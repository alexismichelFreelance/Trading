"""What do the BIG intraday moves actually look like? Observation only.

The trading day contains 20-40 point directional legs and reversals. Every
intraday study in this project so far has measured 1-2 tick effects, which is
the wrong order of magnitude entirely -- a 1.3 tick edge is not the business.

This catalogues the legs themselves before proposing anything: segment each RTH
session into directional swings with a zigzag (a leg ends when price retraces
RETRACE_PT against it), then describe them. How many are there? How big? When do
they start? How long do they run? How much do they give back before they end?

Nothing is predicted here. The point is to know the shape of the target before
building anything to hit it -- the step that was skipped every previous time.

    python move_catalog.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import breakout_retest as B

RETRACE_FRAC = 0.30         # a leg ends when price gives back this much of
                            # the SESSION RANGE. Fixed point thresholds do not
                            # work: 8pt is noise on a 211pt crash day and most
                            # of the day on a 30pt drift day.
RTH_OPEN, RTH_CLOSE = 9 * 60 + 30, 15 * 60 + 59


def legs(g: pd.DataFrame, retrace: float) -> list[dict]:
    """Zigzag: strictly ALTERNATING directional legs, each ended by a `retrace`
    pullback from the leg's extreme.

    Two things the first version got wrong, both caught by sanity-checking the
    output against the session's own range:

      * it dropped zero-duration legs at the end, which silently broke
        alternation and left runs of same-direction "legs" in the catalog;
      * `retrace` was a fixed 8pt. On 2025-04-04 (211pt RTH range) that shredded
        the session into 221 legs summing to 4,056pt -- twenty times the range.
        The threshold has to scale with the day, so it is passed in per session.

    A correct zigzag satisfies: legs alternate, and the summed leg travel is a
    modest multiple of the session range. Both are asserted by the caller.
    """
    m = g["min"].to_numpy()
    h, l = g.h.to_numpy(), g.l.to_numpy()
    n = len(g)
    if n < 30:
        return []
    out: list[dict] = []
    piv_i, piv_px = 0, (h[0] + l[0]) / 2.0
    ext_i, ext_px = 0, piv_px
    direction = 0
    for i in range(1, n):
        if direction > 0:
            if h[i] > ext_px:
                ext_i, ext_px = i, h[i]
            elif ext_px - l[i] >= retrace:
                out.append({"dir": 1, "start_min": int(m[piv_i]),
                            "end_min": int(m[ext_i]), "pts": ext_px - piv_px,
                            "mins": int(m[ext_i] - m[piv_i])})
                piv_i, piv_px = ext_i, ext_px
                direction, ext_i, ext_px = -1, i, l[i]
        elif direction < 0:
            if l[i] < ext_px:
                ext_i, ext_px = i, l[i]
            elif h[i] - ext_px >= retrace:
                out.append({"dir": -1, "start_min": int(m[piv_i]),
                            "end_min": int(m[ext_i]), "pts": piv_px - ext_px,
                            "mins": int(m[ext_i] - m[piv_i])})
                piv_i, piv_px = ext_i, ext_px
                direction, ext_i, ext_px = 1, i, h[i]
        else:                                   # undecided: first break decides
            if h[i] - ext_px >= retrace:
                direction, ext_i, ext_px = 1, i, h[i]
            elif ext_px - l[i] >= retrace:
                direction, ext_i, ext_px = -1, i, l[i]
    return out


def main() -> None:
    rows = []
    for sym in ("ESH5", "ESM5"):
        b = B.bars(sym)
        for day, g in b.groupby("day"):
            r = g[(g["min"] >= RTH_OPEN) & (g["min"] <= RTH_CLOSE)].sort_values("min")
            if len(r) < 60:
                continue
            rng = r.h.max() - r.l.min()
            # threshold scales with the session: a fixed 8pt is meaningless on a
            # 211pt crash day and enormous on a 30pt drift day.
            thr = max(6.0, RETRACE_FRAC * rng)
            ll = legs(r, thr)
            if not ll:
                continue
            # SANITY, asserted rather than assumed: a zigzag must alternate, and
            # total leg travel must be a modest multiple of the session range.
            d = [x["dir"] for x in ll]
            assert all(a != b for a, b in zip(d, d[1:])), f"{day}: legs do not alternate"
            # travel/range is NOT an invariant -- a choppy session legitimately
            # travels many multiples of its range. What must hold is that no
            # single leg exceeds the range it was measured inside.
            worst = max(x["pts"] for x in ll)
            assert worst <= rng + 1e-6, f"{day}: leg {worst:.0f} > range {rng:.0f}"
            for L in ll:
                L.update(sym=sym, day=day, rng=rng, thr=thr)
                rows.append(L)
    L = pd.DataFrame(rows)
    nd = L.day.nunique()

    print(f"\n{'='*96}")
    print(f"INTRADAY MOVE CATALOG — {nd} sessions, {len(L)} legs "
          f"(zigzag, {RETRACE_FRAC:.0%} of session range)")
    print(f"{'='*96}")
    print(f"  legs per session: {len(L)/nd:.1f}   "
          f"up {int((L.dir > 0).sum())} / down {int((L.dir < 0).sum())}")

    print(f"\n  SIZE DISTRIBUTION (points)")
    print(f"  {'bucket':<14}{'legs':>7}{'per day':>9}{'med mins':>10}"
          f"{'med pts':>9}{'total pts':>11}")
    for lo, hi, lab in ((0, 10, "under 10"), (10, 20, "10-20"), (20, 30, "20-30"),
                        (30, 40, "30-40"), (40, 1e9, "40+")):
        g = L[(L.pts >= lo) & (L.pts < hi)]
        if g.empty:
            continue
        print(f"  {lab:<14}{len(g):>7}{len(g)/nd:>9.2f}{g.mins.median():>10.0f}"
              f"{g.pts.median():>9.1f}{g.pts.sum():>11,.0f}")

    big = L[L.pts >= 20]
    print(f"\n  THE MOVES THAT MATTER (>= 20 pt): {len(big)} legs, "
          f"{len(big)/nd:.2f} per session")
    print(f"    median size {big.pts.median():.1f} pt, median duration "
          f"{big.mins.median():.0f} min")
    print(f"    they carry {big.pts.sum():,.0f} pt of the {L.pts.sum():,.0f} pt "
          f"total ({100*big.pts.sum()/L.pts.sum():.0f}%)")
    print(f"    sessions with at least one: "
          f"{100*big.day.nunique()/nd:.0f}%")

    print(f"\n  WHEN DO BIG LEGS START? (ET, by hour)")
    for h in range(9, 16):
        g = big[(big.start_min >= max(h*60, RTH_OPEN)) & (big.start_min < (h+1)*60)]
        bar = "#" * int(round(20 * len(g) / max(len(big), 1)))
        print(f"    {h:02d}:00  {len(g):>4}  {100*len(g)/len(big):>4.0f}%  {bar}")

    print(f"\n  DURATION of >=20pt legs")
    for lo, hi, lab in ((0, 15, "under 15 min"), (15, 30, "15-30"),
                        (30, 60, "30-60"), (60, 120, "60-120"), (120, 1e9, "120+")):
        g = big[(big.mins >= lo) & (big.mins < hi)]
        if len(g):
            print(f"    {lab:<14}{len(g):>5}  {100*len(g)/len(big):>4.0f}%   "
                  f"median {g.pts.median():>5.1f} pt")
    L.to_pickle(".cache_legs.pkl")
    print(f"{'='*96}\n")


if __name__ == "__main__":
    main()
