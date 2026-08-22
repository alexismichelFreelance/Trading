"""Does DELTA CONFIRM the move into the break? Measured on every ORB break we
have per-second flow for.

THE MECHANISM (derived from 2026-08-19 NQ, strategy_lab/nq_0819_anatomy.py):
a breakout means something only if the move that produced it CONSUMED liquidity.
That day's ORB low broke on a vacuum -- 38% of the decline happened on minutes
with net BUYING, the low printed on delta -1, and opendrive shorted the single
heaviest selling minute of the morning, which was supply running out. It bled
until the clock closed it.

In a real distribution price and delta fall together: sellers hit bids, bids are
consumed, price moves. In a vacuum the book is PULLED faster than it is traded --
buyers can be net aggressive and price still falls. Nothing changed hands to
defend the new level, so it retraces.

THE MEASUREMENT, per break, with no lookahead:
  leg      the 20 minutes ending at the break instant (floored at 09:30)
  vac      share of the leg's movement IN THE BREAK DIRECTION that occurred on
           minutes whose delta pointed the OTHER way. 0 = every inch was paid
           for by aggressors; 1 = the whole move was air.
  confirm  sign(cumulative delta over the leg) == sign(break direction)

Everything -- opening range, break, entry, outcome -- is computed from the SAME
per-second series (pxc), so there is no cross-table alignment to get wrong.
Entry is the OR level itself, which is attainable: price is inside the range at
10:00 by construction, so the level is always ahead of price when we act.

Outcome is in R, where R = the opening-range width, so ES and NQ are comparable.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

LOOKBACK_MIN = 20


def sessions(sym):
    q = QuestDB(timeout=300)
    d = q.df("SELECT ts,pxc,adelta,avol FROM claude_sec_live "
             "WHERE symbol='" + sym + "' AND pxc > 0 ORDER BY ts").copy()
    d["et"] = pd.to_datetime(d["ts"]).dt.tz_convert("America/New_York")
    d["day"] = d.et.dt.strftime("%Y-%m-%d")
    d["mod"] = d.et.dt.hour * 60 + d.et.dt.minute
    return d


def one(g):
    """g = one session's seconds. Returns a dict or None."""
    orb = g[(g["mod"] >= 570) & (g["mod"] < 600)]
    post = g[(g["mod"] >= 600) & (g["mod"] < 960)]
    if len(orb) < 900 or len(post) < 3600:          # need real coverage
        return None
    hi, lo = orb.pxc.max(), orb.pxc.min()
    rng = hi - lo
    if rng <= 0:
        return None
    side = entry = bmod = None
    for r in post.itertuples(index=False):
        if r.pxc >= hi:
            side, entry, bmod = 1, hi, r.mod
            break
        if r.pxc <= lo:
            side, entry, bmod = -1, lo, r.mod
            break
    if side is None:
        return None

    # ---- the leg into the break, per minute ----
    # STRUCTURAL, not a clock. For a downside break the move that matters runs
    # from the session high that preceded it; for an upside break, from the low.
    # However long it took. The 20-minute version was a magic number and it
    # misclassified 2026-08-19 NQ -- the very move this came from.
    pre = g[(g["mod"] >= 570) & (g["mod"] <= bmod)]
    piv = pre.pxc.idxmax() if side < 0 else pre.pxc.idxmin()
    lo_mod = int(pre.loc[piv, "mod"])
    leg = g[(g["mod"] >= lo_mod) & (g["mod"] <= bmod)]
    m = leg.groupby("mod").agg(px=("pxc", "last"), ad=("adelta", "sum"))
    m["d"] = m.px.diff()
    m = m.dropna()
    if len(m) < 3:
        return None
    # movement in the break direction, split by whether delta agreed
    move = m[np.sign(m.d) == side]
    if not len(move) or move.d.abs().sum() == 0:
        return None
    wrong = move[np.sign(move.ad) != side]
    vac = wrong.d.abs().sum() / move.d.abs().sum()
    cum = m.ad.sum()

    # ---- outcome from the break to the close ----
    fwd = post[post["mod"] >= bmod]
    px = fwd.pxc.values
    hold = (px[-1] - entry) * side / rng
    fav = (px - entry) * side
    adv = -fav
    mfe, mae = fav.max() / rng, adv.max() / rng
    race = 0
    for f in fav:
        if f <= -rng:
            race = -1
            break
        if f >= rng:
            race = 1
            break
    return dict(side=side, rng=rng, bmod=bmod, vac=vac, cum=cum,
                confirm=int(np.sign(cum) == side), hold=hold, mfe=mfe,
                mae=mae, race=race)


def main():
    rows = []
    for sym in ("ES", "NQ"):
        d = sessions(sym)
        for day, g in d.groupby("day"):
            r = one(g)
            if r:
                r.update(sym=sym, day=day)
                rows.append(r)
    t = pd.DataFrame(rows)
    print("%d breaks, %d sessions, %s -> %s"
          % (len(t), t.day.nunique(), t.day.min(), t.day.max()))
    print("  ES %d, NQ %d\n" % ((t.sym == "ES").sum(), (t.sym == "NQ").sum()))

    def show(lab, s):
        if not len(s):
            print("  %-26s n=0" % lab)
            return
        w = (s.race == 1).sum()
        l = (s.race == -1).sum()
        print("  %-26s n=%2d  hold %+6.2fR (med %+5.2fR)  MFE %4.2f MAE %4.2f  "
              "race %d-%d  win %3.0f%%"
              % (lab, len(s), s.hold.sum(), s.hold.median(), s.mfe.mean(),
                 s.mae.mean(), w, l, 100 * (s.hold > 0).mean()))

    print("=== BY DELTA CONFIRMATION (sign of cum delta over the leg) ===")
    show("CONFIRMED", t[t.confirm == 1])
    show("NOT confirmed", t[t.confirm == 0])
    for sym in ("ES", "NQ"):
        s = t[t.sym == sym]
        print("   %s:" % sym)
        show("  confirmed", s[s.confirm == 1])
        show("  not confirmed", s[s.confirm == 0])

    print("\n=== BY VACUUM FRACTION (share of the leg moved on opposing delta) ===")
    med = t.vac.median()
    print("  median vac = %.2f" % med)
    show("LOW vac (real supply)", t[t.vac <= med])
    show("HIGH vac (air)", t[t.vac > med])
    for sym in ("ES", "NQ"):
        s = t[t.sym == sym]
        print("   %s:" % sym)
        show("  low vac", s[s.vac <= med])
        show("  high vac", s[s.vac > med])

    print("\n=== the 2026-08-19 NQ break, for reference ===")
    r = t[(t.sym == "NQ") & (t.day == "2026-08-19")]
    if len(r):
        x = r.iloc[0]
        print("  side %+d  vac %.2f  cumdelta %+.0f  confirm %d  hold %+.2fR"
              % (x.side, x.vac, x.cum, x.confirm, x.hold))
    t.to_csv(r"D:\Trading\strategy_lab\vacuum_break_trades.csv", index=False)
    print("\nper-trade detail -> strategy_lab/vacuum_break_trades.csv")


if __name__ == "__main__":
    main()
