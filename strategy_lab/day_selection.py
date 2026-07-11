"""DAY-SELECTION — does the user modulate AGGRESSION by causal day type?

The taxonomy showed the user trades nearly every day but expresses "stand-down"
by collapsing fill count (July 9 = 2 fills). Here I test whether that modulation
is CAUSAL and systematic. Morning-conviction proxy = fills before 10:30 ET
(exact NT8 timestamps), which — unlike total fills — does not grow endogenously
as a trade works intraday. Dependent: early_fills, early peak size, day $.
Causal features: gap, prior-day range/|return|, GEX/DIX regime, day-of-week.

Central hypothesis: the user presses LESS in short-gamma regimes (gexLOW), i.e.
they have learned to stand down in the regime that carries their dip-buy tail
risk (July 9 was a short-gamma day). Observation only.
"""
import sqlite3
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, "D:/Trading/engine")
from engine.adapters.questdb import QuestDB

SP = ("C:/Users/alexi/AppData/Local/Temp/claude/D--Trading/"
      "74634737-f114-4630-9ca7-feccef7ea3b7/scratchpad")
TICK0 = 621355968000000000
PT = 50.0
EARLY_MIN = 10 * 60 + 30       # 10:30 ET
q = QuestDB(timeout=120)


def user_days():
    con = sqlite3.connect(SP + "/nt8.sqlite")
    E = pd.read_sql_query("""
      SELECT e.Time ticks, e.MarketPosition mp, e.Price px, e.Quantity qty, e.Name oname
      FROM Executions e JOIN Accounts a ON e.Account=a.Id
      JOIN Instruments i ON e.Instrument=i.Id
      JOIN MasterInstruments mi ON i.MasterInstrument=mi.Id
      WHERE a.Name='Sim101' AND mi.Name='ES' """, con)
    E["t"] = pd.to_datetime((E.ticks - TICK0) * 100, unit="ns", utc=True)
    et = E.t.dt.tz_convert("America/New_York")
    E["day"] = et.dt.strftime("%Y-%m-%d"); E["mod"] = et.dt.hour * 60 + et.dt.minute
    E = E[~E.oname.str.fullmatch(r"O\d+", na=False)].sort_values("t")
    E["sq"] = np.where(E.mp == 0, E.qty, -E.qty)
    rows = []
    for day, g in E.groupby("day"):
        early = g[g["mod"] < EARLY_MIN]
        # peak |pos| reached during the early window
        pos = 0; peak = 0
        for s in early.sq:
            pos += s; peak = max(peak, abs(pos))
        # realized $ for the whole day (avg-cost)
        pos = 0; avg = 0.0; real = 0.0
        for r in g.itertuples():
            qq, px = r.sq, r.px
            while qq != 0:
                if pos == 0:
                    pos = qq; avg = px; qq = 0
                elif (qq > 0) == (pos > 0):
                    avg = (avg*abs(pos)+px*abs(qq))/(abs(pos)+abs(qq)); pos += qq; qq = 0
                else:
                    c = min(abs(qq), abs(pos))
                    real += (px-avg)*np.sign(pos)*c*PT
                    pos += np.sign(qq)*c; qq -= np.sign(qq)*c
        rows.append(dict(day=day, early_fills=len(early), early_peak=peak,
                         total_fills=len(g), day_pnl=real,
                         early_long=float((early.sq > 0).mean()) if len(early) else np.nan))
    return pd.DataFrame(rows)


def features():
    D = pd.read_csv("D:/Trading/gamma/esf_daily.csv")
    D["rng"] = D.h - D.l; D["ret"] = D.c - D.o
    D["p_rng"] = D.rng.shift(1); D["p_absret"] = D.ret.abs().shift(1); D["p_c"] = D.c.shift(1)
    D["gap"] = D.o - D.p_c
    D["dow"] = pd.to_datetime(D.date).dt.strftime("%a")
    return D.rename(columns={"date": "day"})[["day", "gap", "p_rng", "p_absret", "dow"]]


def regime(F):
    gx = q.df("SELECT ts, gexp, dixp FROM claude_gex ORDER BY ts"); gx["day"] = gx.ts.dt.strftime("%Y-%m-%d")
    import bisect
    days = gx["day"].tolist()

    def pv(d, c):
        i = bisect.bisect_left(days, d); return gx[c].iloc[i-1] if i > 0 else np.nan
    F["gexp"] = [pv(d, "gexp") for d in F.day]; F["dixp"] = [pv(d, "dixp") for d in F.day]
    return F


if __name__ == "__main__":
    U = regime(user_days().merge(features(), on="day", how="left"))
    U = U[U.total_fills > 0]
    U["gex_b"] = pd.cut(U.gexp, [-.01, 1/3, 2/3, 1.01], labels=["gexLOW(short)", "gexMID", "gexHIGH(long)"])
    print(f"=== day-selection: {len(U)} user ES days ({U.day.min()}..{U.day.max()}) ===")
    print(f"early fills (<10:30 ET): median {U.early_fills.median():.0f}  "
          f"stand-down (<=2 early) days: {int((U.early_fills<=2).sum())}")

    # H: does morning conviction predict the day's P&L? (real selection skill)
    from scipy.stats import spearmanr
    for v in ("early_fills", "early_peak", "total_fills"):
        rho, p = spearmanr(U[v], U.day_pnl, nan_policy="omit")
        print(f"  rank-corr({v:<12}, day_pnl) = {rho:+.2f}  (p={p:.3f})")

    print("\n  morning conviction by regime (the stand-down hypothesis):")
    print(f"    {'regime':<16}{'days':>5}{'medEarlyF':>10}{'medPeak':>9}{'standdn%':>9}"
          f"{'medDay$':>9}{'winday%':>8}")
    for b, g in U.groupby("gex_b"):
        if not len(g):
            continue
        print(f"    {str(b):<16}{len(g):>5}{g.early_fills.median():>10.0f}{g.early_peak.median():>9.0f}"
              f"{np.mean(g.early_fills<=2):>9.0%}{g.day_pnl.median():>9,.0f}{np.mean(g.day_pnl>0):>8.0%}")

    print("\n  P&L by morning-conviction tercile (do they press the right days?):")
    U["conv_b"] = pd.qcut(U.early_fills.rank(method="first"), 3, labels=["quiet AM", "mid AM", "heavy AM"])
    print(f"    {'conviction':<12}{'days':>5}{'medEarlyF':>10}{'medDay$':>9}{'meanDay$':>10}{'winday%':>8}")
    for b, g in U.groupby("conv_b"):
        print(f"    {str(b):<12}{len(g):>5}{g.early_fills.median():>10.0f}"
              f"{g.day_pnl.median():>9,.0f}{g.day_pnl.mean():>10,.0f}{np.mean(g.day_pnl>0):>8.0%}")

    print("\n  the stand-down days (<=2 early fills) vs the rest:")
    sd = U[U.early_fills <= 2]; rest = U[U.early_fills > 2]
    for nm, g in (("standdown", sd), ("engaged", rest)):
        print(f"    {nm:<10} n={len(g):>3}  gap {g.gap.mean():+5.1f}  prior|ret| {g.p_absret.mean():5.1f}"
              f"  gexp {g.gexp.mean():.2f}  dixp {g.dixp.mean():.2f}  medDay$ {g.day_pnl.median():>7,.0f}")
    U.to_csv(SP + "/pf/day_selection.csv", index=False)
    print(f"\nwrote {SP}/pf/day_selection.csv")
