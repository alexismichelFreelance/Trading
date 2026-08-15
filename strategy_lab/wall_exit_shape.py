"""What does price DO after touching a wall in a long-gamma pocket?

WallFadeStrategy shipped with STOP=12, TARGET=20, MAX_BARS=30. The 30 came from
the horizon the signal was measured over, which is at least an argument; 12 and
20 were invented. An exit picked out of the air is not an exit policy, and it
decides most of the P&L -- 9 of the sleeve's 14 trades timed out, which means
the numbers chosen were not even in contact with what price was doing.

So measure the excursions instead, and read the levels off them:

  MFE   max favourable excursion -- how far the fade goes in our favour before
        it is over. The TARGET has to sit inside this or it is never reached.
  MAE   max adverse excursion -- how far it goes against us first. The STOP has
        to sit outside the MAE of the trades that eventually work, or it cuts
        the winners.
  decay how the edge behaves as the horizon lengthens. The HOLD should be where
        the mean stops improving, not a round number.

Conditioned exactly as the signal was: price within NEAR of a wall, in a
long-gamma pocket, direction from the wall's own net (call-heavy fade short,
put-heavy fade long). Session-demeaned so the window's drift is not read as edge.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB              # noqa: E402
from engine.features.gamma_profile import gamma_profile  # noqa: E402

ET = "America/New_York"
NEAR = 10.0
HORIZONS = (5, 10, 15, 20, 30, 45, 60, 90)


def main() -> None:
    q = QuestDB(timeout=240)
    gx = q.df("SELECT ts,strike,call_gex,put_gex,spot FROM claude_gex_strikes "
              "WHERE underlying='SPX' ORDER BY ts")
    gx["day"] = gx["ts"].dt.strftime("%Y-%m-%d")
    curves = {}
    for d, g in gx.groupby("day"):
        agg = {}
        for r in g.itertuples():
            c, p = agg.get(r.strike, (0.0, 0.0))
            agg[r.strike] = (c + float(r.call_gex), p + float(r.put_gex))
        curves[d] = (agg, float(g["spot"].iloc[0]))

    b = q.df("SELECT ts,o,h,l,c FROM claude_bars_live WHERE symbol='ES' ORDER BY ts")
    et = pd.to_datetime(b["ts"], utc=True).dt.tz_convert(ET)
    m = et.dt.hour * 60 + et.dt.minute
    b = b[(m >= 570) & (m < 960)].copy()
    b["day"] = et[(m >= 570) & (m < 960)].dt.strftime("%Y-%m-%d")
    closes = b.groupby("day")["c"].last()
    cdays = sorted(curves)
    basis = {d: float(closes[[x for x in closes.index if x < d][-1]]) - curves[d][1]
             for d in cdays if [x for x in closes.index if x < d]}

    ev = []                       # one row per wall touch
    for day, g in b.groupby("day"):
        prior = [x for x in cdays if x < day and x in basis]
        if not prior or len(g) < 300:
            continue
        src = prior[-1]
        pr = gamma_profile(curves[src][0], curves[src][1])
        if pr is None:
            continue
        bs = basis[src]
        walls = sorted(((k + bs, n) for k, n, _ in pr["curve"]),
                       key=lambda x: -abs(x[1]))[:8]
        ks = np.array([k for k, _, _ in pr["curve"]])
        cums = np.array([c for _, _, c in pr["curve"]])
        px = g["c"].to_numpy(float)
        hi = g["h"].to_numpy(float)
        lo = g["l"].to_numpy(float)
        cum = np.interp(px - bs, ks, cums)
        last_i = -99
        for i in range(len(px) - max(HORIZONS)):
            if cum[i] <= 0 or i - last_i < 30:        # long pockets, no overlap
                continue
            near = [(w, n) for w, n in walls if abs(px[i] - w) <= NEAR and n != 0]
            if not near:
                continue
            w, n = min(near, key=lambda x: abs(x[0] - px[i]))
            d = -1 if n > 0 else 1                    # call-heavy caps, put-heavy holds
            last_i = i
            row = dict(day=day, i=i, dir=d, entry=px[i])
            for H in HORIZONS:
                fut_h = hi[i + 1:i + 1 + H]
                fut_l = lo[i + 1:i + 1 + H]
                # in TRADE terms: favourable is d * move
                fav = (fut_h.max() - px[i]) if d > 0 else (px[i] - fut_l.min())
                adv = (px[i] - fut_l.min()) if d > 0 else (fut_h.max() - px[i])
                row[f"mfe{H}"] = fav
                row[f"mae{H}"] = adv
                row[f"ret{H}"] = d * (px[i + H] - px[i])
            ev.append(row)
    if not ev:
        print("no wall touches in a long pocket")
        return
    e = pd.DataFrame(ev)
    # demean the signed return per session
    for H in HORIZONS:
        e[f"ret{H}"] -= e.groupby("day")[f"ret{H}"].transform("mean")

    print(f"\nWALL TOUCHES IN A LONG-GAMMA POCKET — {len(e)} events, "
          f"{e['day'].nunique()} sessions\n")
    print(f"  {'hold':>5} {'mean ret':>9} {'median':>8} {'win%':>6} "
          f"{'med MFE':>8} {'p75 MFE':>8} {'med MAE':>8} {'p75 MAE':>8}")
    for H in HORIZONS:
        r = e[f"ret{H}"]
        print(f"  {H:>5} {r.mean():>9.2f} {r.median():>8.2f} "
              f"{100 * (r > 0).mean():>5.0f}% "
              f"{e[f'mfe{H}'].median():>8.2f} {e[f'mfe{H}'].quantile(.75):>8.2f} "
              f"{e[f'mae{H}'].median():>8.2f} {e[f'mae{H}'].quantile(.75):>8.2f}")

    print("\n  READ THE EXITS OFF THIS, do not choose them:")
    best = max(HORIZONS, key=lambda H: e[f"ret{H}"].mean())
    print(f"    HOLD   the mean return peaks at {best} bars "
          f"({e[f'ret{best}'].mean():+.2f}); past that it is not improving")
    print(f"    TARGET median MFE at that hold is {e[f'mfe{best}'].median():.1f} "
          f"-- a target beyond it is unreachable for half the trades")
    win = e[e[f"ret{best}"] > 0]
    if len(win):
        print(f"    STOP   winners' MAE p75 is {win[f'mae{best}'].quantile(.75):.1f} "
              f"-- a stop inside that cuts trades that go on to work")
    print(f"\n  currently shipped: STOP 12, TARGET 20, HOLD 30")


if __name__ == "__main__":
    main()
