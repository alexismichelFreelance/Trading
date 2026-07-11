"""DAY-TYPE TAXONOMY — Part 2: the USER's manual behavior by day type.

Question (the user's): does a causal day-type explain their day SELECTION —
which days they press, and which they stand down (e.g. 2026-07-09)?

Data: NT8 Sim101 MANUAL fills (their year, from the sqlite copy) -> per-day ES
realized P&L + participation (fills). Daily-resolution causal features from
gamma/esf_daily.csv (ES=F daily OHLC): gap, prior-day range, prior-day return,
day-of-week. Regime from claude_gex (prev-session gexp/dixp percentiles). For
the 18 recorded 2026 days, the intraday first-30m range (claude_bars_live) is
added to connect with Part 1. Observation only — no rule is deployed.
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
q = QuestDB(timeout=120)


def user_daily():
    """Per-ET-day ES Sim101 manual: realized $ (avg-cost, flat-to-flat) +
    fills + signed-entry direction bias."""
    con = sqlite3.connect(SP + "/nt8.sqlite")
    E = pd.read_sql_query("""
      SELECT e.Time ticks, e.MarketPosition mp, e.Price px, e.Quantity qty, e.Name oname,
             mi.Name sym FROM Executions e JOIN Accounts a ON e.Account=a.Id
             JOIN Instruments i ON e.Instrument=i.Id
             JOIN MasterInstruments mi ON i.MasterInstrument=mi.Id
             WHERE a.Name='Sim101' AND mi.Name='ES' """, con)
    E["t"] = pd.to_datetime((E.ticks - TICK0) * 100, unit="ns", utc=True)
    E["day"] = E.t.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    E = E[~E.oname.str.fullmatch(r"O\d+", na=False)]        # drop engine orders
    E["sq"] = np.where(E.mp == 0, E.qty, -E.qty)
    E = E.sort_values("t")
    pos = 0; avg = 0.0
    daily = {}
    fills = {}; buys = {}
    for r in E.itertuples():
        fills[r.day] = fills.get(r.day, 0) + 1
        buys[r.day] = buys.get(r.day, 0) + (1 if r.sq > 0 else 0)
        q_, px = r.sq, r.px
        while q_ != 0:
            if pos == 0:
                pos = q_; avg = px; q_ = 0
            elif (q_ > 0) == (pos > 0):
                avg = (avg * abs(pos) + px * abs(q_)) / (abs(pos) + abs(q_)); pos += q_; q_ = 0
            else:
                closed = min(abs(q_), abs(pos))
                daily[r.day] = daily.get(r.day, 0.0) + (px - avg) * np.sign(pos) * closed * PT
                pos += np.sign(q_) * closed; q_ -= np.sign(q_) * closed
    rows = [dict(day=d, es_pnl=daily.get(d, 0.0), fills=fills[d],
                 long_frac=buys[d] / fills[d]) for d in sorted(fills)]
    return pd.DataFrame(rows)


def daily_features():
    D = pd.read_csv("D:/Trading/gamma/esf_daily.csv")
    D["rng"] = D.h - D.l
    D["ret"] = D.c - D.o
    D["p_c"] = D.c.shift(1); D["p_rng"] = D.rng.shift(1); D["p_ret"] = D.ret.shift(1)
    D["gap"] = D.o - D.p_c
    D["atr"] = D.rng.rolling(14, min_periods=5).mean().shift(1)
    D["dow"] = pd.to_datetime(D.date).dt.strftime("%a")
    return D.rename(columns={"date": "day"})


def merge_regime(F):
    gx = q.df("SELECT ts, gexp, dixp FROM claude_gex ORDER BY ts")
    gx["day"] = gx.ts.dt.strftime("%Y-%m-%d")
    import bisect
    days = gx["day"].tolist()

    def pv(day, col):
        i = bisect.bisect_left(days, day)
        return gx[col].iloc[i - 1] if i > 0 else np.nan
    F["gexp_prev"] = [pv(d, "gexp") for d in F.day]
    F["dixp_prev"] = [pv(d, "dixp") for d in F.day]
    return F


def recorded_f30():
    B = q.df("SELECT ts, o, h, l, c FROM claude_bars_live ORDER BY ts")
    et = B.ts.dt.tz_convert("America/New_York")
    B["day"] = et.dt.strftime("%Y-%m-%d"); B["mod"] = et.dt.hour * 60 + et.dt.minute
    out = {}
    for day, g in B.groupby("day"):
        f = g[(g["mod"] >= 570) & (g["mod"] < 600)]
        if len(f):
            out[day] = f.h.max() - f.l.min()
    return out


def show(F, by, minn=3):
    print(f"\n  by {by}:")
    print(f"    {'bucket':<16}{'days':>5}{'traded%':>8}{'medFills':>9}"
          f"{'winday%':>8}{'mean$':>10}{'median$':>10}{'total$':>12}")
    for b, g in F.groupby(by):
        if len(g) < minn:
            continue
        tr = g[g.fills > 0]
        wd = np.mean(tr.es_pnl > 0) if len(tr) else np.nan
        print(f"    {str(b):<16}{len(g):>5}{len(tr)/len(g):>8.0%}"
              f"{(tr.fills.median() if len(tr) else 0):>9.0f}{wd:>8.0%}"
              f"{(tr.es_pnl.mean() if len(tr) else 0):>10,.0f}"
              f"{(tr.es_pnl.median() if len(tr) else 0):>10,.0f}"
              f"{g.es_pnl.sum():>12,.0f}")


if __name__ == "__main__":
    U = user_daily()
    D = daily_features()
    F = D.merge(U, on="day", how="inner")          # user's ES trading days only
    F = merge_regime(F)
    F["gap_a"] = F.gap / F.atr
    F["gap_b"] = pd.cut(F.gap_a, [-9, -0.5, -0.1, 0.1, 0.5, 9],
                        labels=["gapdn++", "gapdn", "flat", "gapup", "gapup++"])
    F["pday_b"] = pd.cut(F.p_ret, [-999, -5, 5, 999], labels=["pdayDN", "pdayFLAT", "pdayUP"])
    F["gex_b"] = pd.cut(F.gexp_prev, [-.01, 1/3, 2/3, 1.01],
                        labels=["gexLOW", "gexMID", "gexHIGH"])
    F["dix_b"] = pd.cut(F.dixp_prev, [-.01, 1/3, 2/3, 1.01],
                        labels=["dixLOW", "dixMID", "dixHIGH"])
    print(f"=== PART 2: user manual ES days {F.day.min()}..{F.day.max()}  "
          f"({len(F)} trading days) ===")
    print(f"total ES manual realized ${F.es_pnl.sum():,.0f}   "
          f"win days {np.mean(F.es_pnl>0):.0%}   median ${F.es_pnl.median():,.0f}")
    show(F, "dow")
    show(F, "gap_b")
    show(F, "pday_b")
    show(F, "gex_b")
    show(F, "dix_b")

    # recorded-window day-by-day, with intraday first-30 range
    f30 = recorded_f30()
    R = F[F.day >= "2026-06-16"].copy()
    R["f30_rng"] = R.day.map(f30)
    print("\n  recorded window (intraday available), day-by-day:")
    print(f"    {'day':<12}{'dow':>4}{'gap':>7}{'f30rng':>8}{'gexp':>6}{'fills':>6}"
          f"{'ES$':>10}")
    for r in R.sort_values("day").itertuples():
        print(f"    {r.day:<12}{r.dow:>4}{r.gap:>7.0f}"
              f"{(r.f30_rng if pd.notna(r.f30_rng) else 0):>8.0f}"
              f"{(r.gexp_prev if pd.notna(r.gexp_prev) else 0):>6.2f}{r.fills:>6.0f}"
              f"{r.es_pnl:>10,.0f}")
    F.to_csv(SP + "/pf/taxonomy_user.csv", index=False)
    print(f"\nwrote {SP}/pf/taxonomy_user.csv")
