"""Nightly scorecard: ENGINE vs MANUAL, per ET day, from the NinjaTrader db.

The NT8 sqlite is the source of truth for what actually FILLED live — both the
engine's orders (Account Sim101, Name='O<n>') and the user's manual trades
(Sim101, any other Name). This tool copies the db (it is locked while NT8 runs),
computes per-day avg-cost realized P&L for each side, contextualises with the
dealer-gamma regime + the opening gap, and FLAGS the failure modes we care about:
  - engine daily realized loss beyond the kill-switch budget
  - engine order-count / position bursts (the 2026-07-09 pathology)
  - engine-vs-user sign disagreement on ES (are we fighting the user's edge?)

    python tools/scorecard.py [--days 20] [--sym ES] [--json out.json]

Read-only w.r.t. the live db (works on a copy). ASCII-safe output.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.features.gamma import GammaRegime          # noqa: E402

NT_DB = Path.home() / "Documents" / "NinjaTrader 8" / "db" / "NinjaTrader.sqlite"
TICK0 = 621355968000000000
ESF = ROOT.parent / "gamma" / "esf_daily.csv"
BURST_FILLS = 12          # engine fills/day above this -> flag
CAP = 10                  # engine |position| above this -> flag (sleeve cap breach)


def load_execs(db: Path) -> pd.DataFrame:
    import sqlite3
    tmp = Path(tempfile.gettempdir()) / "scorecard_nt8.sqlite"
    shutil.copy(db, tmp)
    con = sqlite3.connect(tmp)
    E = pd.read_sql_query("""
      SELECT e.Time ticks, a.Name acct, mi.Name sym, mi.PointValue pv,
             e.MarketPosition mp, e.Price px, e.Quantity qty, e.Name oname,
             e.Commission comm, e.Rate rate
      FROM Executions e JOIN Accounts a ON e.Account=a.Id
      JOIN Instruments i ON e.Instrument=i.Id
      JOIN MasterInstruments mi ON i.MasterInstrument=mi.Id
      WHERE a.Name='Sim101' """, con)
    con.close()
    E["t"] = pd.to_datetime((E.ticks - TICK0) * 100, unit="ns", utc=True)
    E["day"] = E.t.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    E["sq"] = np.where(E.mp == 0, E.qty, -E.qty)
    E["usd"] = np.where(E.rate > 0, E.rate, 1.0)
    E["cls"] = np.where(E.oname.str.fullmatch(r"O\d+", na=False), "engine", "manual")
    return E.sort_values("t")


def ledger_by_day(g: pd.DataFrame) -> dict:
    """avg-cost realized $ per day + fills + peak |pos|, one instrument/class."""
    pv = g.pv.iloc[0]
    pos = 0; avg = 0.0
    daily, fills, peak = {}, {}, {}
    for r in g.itertuples():
        d = r.day
        fills[d] = fills.get(d, 0) + 1
        q, px = r.sq, r.px
        while q != 0:
            if pos == 0:
                pos = q; avg = px; q = 0
            elif (q > 0) == (pos > 0):
                avg = (avg * abs(pos) + px * abs(q)) / (abs(pos) + abs(q)); pos += q; q = 0
            else:
                closed = min(abs(q), abs(pos))
                daily[d] = daily.get(d, 0.0) + (px - avg) * np.sign(pos) * closed * pv * r.usd
                pos += np.sign(q) * closed; q -= np.sign(q) * closed
        peak[d] = max(peak.get(d, 0), abs(pos))
    return {d: dict(pnl=daily.get(d, 0.0), fills=fills.get(d, 0), peak=peak.get(d, 0))
            for d in fills}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--sym", default="ES")
    ap.add_argument("--halt", type=float, default=-5000.0)
    ap.add_argument("--json", default="")
    a = ap.parse_args()
    if not NT_DB.exists():
        raise SystemExit(f"NT8 db not found at {NT_DB}")

    E = load_execs(NT_DB)
    sym = E[E.sym == a.sym]
    eng = ledger_by_day(sym[sym.cls == "engine"]) if (sym.cls == "engine").any() else {}
    man = ledger_by_day(sym[sym.cls == "manual"]) if (sym.cls == "manual").any() else {}

    try:
        gr = GammaRegime()
    except Exception:                                    # noqa: BLE001
        gr = None
    D = pd.read_csv(ESF) if ESF.exists() else pd.DataFrame(columns=["date"])
    D["p_c"] = D["c"].shift(1) if "c" in D else np.nan
    gapmap = {row.date: (row.o - row.p_c) for row in D.itertuples()} if len(D) else {}

    days = sorted(set(eng) | set(man))[-a.days:]
    print(f"=== scorecard ({a.sym}, last {len(days)} days)  ENGINE vs MANUAL ===")
    print(f"{'day':<12}{'reg':>6}{'gap':>6} | {'ENG$':>9}{'eFill':>6}{'ePk':>4} | "
          f"{'MAN$':>10}{'mFill':>6} | flags")
    rows = []
    eng_tot = man_tot = 0.0
    for d in days:
        e = eng.get(d, {}); m = man.get(d, {})
        ep, mp = e.get("pnl", 0.0), m.get("pnl", 0.0)
        eng_tot += ep; man_tot += mp
        reg = ""
        if gr is not None:
            sg = gr.is_short_gamma(d)
            reg = "" if sg is None else ("SHORT" if sg else "long")
        gap = gapmap.get(d)
        flags = []
        if ep <= a.halt:
            flags.append(f"ENG LOSS {ep:,.0f}")
        if e.get("fills", 0) > BURST_FILLS:
            flags.append(f"burst {e['fills']}f")
        if e.get("peak", 0) > CAP:
            flags.append(f"CAP {e['peak']}")
        if e and m and np.sign(ep) != np.sign(mp) and abs(ep) > 500 and abs(mp) > 500:
            flags.append("vs-user")
        print(f"{d:<12}{reg:>6}{(f'{gap:+.0f}' if gap is not None else ''):>6} | "
              f"{ep:>9,.0f}{e.get('fills',0):>6}{e.get('peak',0):>4} | "
              f"{mp:>10,.0f}{m.get('fills',0):>6} | {'; '.join(flags)}")
        rows.append(dict(day=d, regime=reg, gap=gap, eng_pnl=ep, eng_fills=e.get("fills", 0),
                         eng_peak=e.get("peak", 0), man_pnl=mp, man_fills=m.get("fills", 0),
                         flags=flags))
    ed = np.array([r["eng_pnl"] for r in rows]); md = np.array([r["man_pnl"] for r in rows])
    print(f"\n{'TOTAL':<12}{'':>12} | {eng_tot:>9,.0f}{'':>10} | {man_tot:>10,.0f}")
    if len(ed):
        print(f"engine: win days {np.mean(ed>0):.0%}  mean/day ${ed.mean():,.0f}  "
              f"worst ${ed.min():,.0f}")
        print(f"manual: win days {np.mean(md>0):.0%}  mean/day ${md.mean():,.0f}  "
              f"best ${md.max():,.0f}")
    nflag = sum(1 for r in rows if r["flags"])
    print(f"flagged days: {nflag}/{len(rows)}")

    _paper_section(days, a.sym)

    if a.json:
        Path(a.json).write_text(json.dumps(rows, default=float, indent=0))
        print(f"wrote {a.json}")


def _paper_section(days, sym):
    """Per-sleeve paper P&L from claude_paper_fills (avg-cost, over the window)."""
    try:
        from engine.adapters.questdb import QuestDB
        pf = QuestDB().df(f"SELECT ts, sleeve, side, qty, price FROM claude_paper_fills "
                          f"WHERE symbol='{sym}' ORDER BY ts")
    except Exception:                                # noqa: BLE001
        return
    if not len(pf):
        print("\npaper sleeves: (no claude_paper_fills yet - run run_live --record)")
        return
    pf["day"] = pf.ts.dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d")
    pf = pf[pf.day.isin(days)]
    pf["sq"] = np.where(pf.side > 0, pf.qty, -pf.qty)
    print(f"\n=== PAPER sleeves ({sym}, {pf.day.nunique()} days, avg-cost pts) ===")
    print(f"{'sleeve':<14}{'fills':>6}{'days':>6}{'realized_pt':>12}{'net':>5}")
    for sl, g in pf.groupby("sleeve"):
        pos = 0; avg = 0.0; real = 0.0
        for r in g.sort_values("ts").itertuples():
            q, px = r.sq, r.price
            while q != 0:
                if pos == 0 or (q > 0) == (pos > 0):
                    avg = (avg * abs(pos) + px * abs(q)) / (abs(pos) + abs(q)) if pos + q else px
                    pos += q; q = 0
                else:
                    c = min(abs(q), abs(pos)); real += (px - avg) * (1 if pos > 0 else -1) * c
                    pos += (1 if q > 0 else -1) * c; q -= (1 if q > 0 else -1) * c
        print(f"{sl:<14}{len(g):>6}{g.day.nunique():>6}{real:>+12.1f}{pos:>+5d}")


if __name__ == "__main__":
    main()
