"""Evaluate a sleeve against the promotion gate (see PROMOTION_GATE.md).

The gate exists so that routing real money is a measurement, not a mood. This
runs it: G1 and G5-G9 are computed from the live-paper record in
claude_paper_fills; G2-G4 are declarations in config/promotion.yaml and are
checked for presence and internal consistency (an UNDECLARED sleeve FAILS --
silence is never a pass).

    .venv/Scripts/python.exe tools/promotion_check.py
    .venv/Scripts/python.exe tools/promotion_check.py --sleeve ES:sweepfade

Exit status is 0 if at least one sleeve is ELIGIBLE, 1 otherwise, so this can
gate a script. Passing makes a sleeve ELIGIBLE; routing money is still an
explicit human decision (G10 and the closing section of the doc).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB              # noqa: E402
from engine.core.config import load_instruments          # noqa: E402

MIN_FIRE_PCT = 10.0
MIN_SIGN_PCT = 70.0
MIN_SESSIONS = 40
WORST_MULT = 3.0
OOS_MEDIAN_TOL = 0.5


def paper_pnl(qdb: QuestDB) -> pd.DataFrame:
    """Realized per-session P&L per sleeve, average-cost, from the live-paper
    fills. This is the FORWARD record -- replay never substitutes for it."""
    df = qdb.df("SELECT ts, symbol, sleeve, side, qty, price "
                "FROM claude_paper_fills ORDER BY sleeve, ts")
    if df.empty:
        return df
    df["day"] = pd.to_datetime(df["ts"]).dt.tz_convert(
        "America/New_York").dt.strftime("%Y-%m-%d")
    rows = []
    for (sleeve, day), g in df.groupby(["sleeve", "day"], sort=True):
        pos, cost, real = 0, 0.0, 0.0
        for r in g.itertuples(index=False):
            qd = int(r.side * r.qty)
            while qd:
                if pos and (pos > 0) != (qd > 0):
                    n = min(abs(qd), abs(pos))
                    sgn = 1 if pos > 0 else -1
                    real += n * (r.price - cost) * sgn
                    pos -= n * sgn
                    qd -= n * (1 if qd > 0 else -1)
                else:
                    tot = abs(pos) + abs(qd)
                    cost = (cost * abs(pos) + r.price * abs(qd)) / tot
                    pos += qd
                    qd = 0
        rows.append({"sleeve": sleeve, "day": day, "pnl": real,
                     "symbol": g["symbol"].iloc[0]})
    return pd.DataFrame(rows)


def check(sleeve: str, pnl: pd.DataFrame, decl: dict, twin_pnl) -> list[tuple]:
    """Returns [(gate, passed, detail)] -- every gate always reported, so a
    failure is visible next to what it would have taken to pass."""
    out = []
    v = pnl["pnl"].to_numpy()
    n = len(v)

    # G1 alive -- fires at all, and often enough to measure
    out.append(("G1 alive", n > 0,
                f"{n} sessions with fills" if n else "NEVER FIRED"))

    # G2 not fitted on its own evidence
    cal = (decl or {}).get("calibration")
    oos = (decl or {}).get("out_of_sample")
    if decl is None:
        out.append(("G2 not in-sample", False, "UNDECLARED in promotion.yaml"))
    elif cal is None:
        out.append(("G2 not in-sample", True, "no parameters were tuned"))
    else:
        # a tuned sleeve needs an evaluation window distinct from calibration;
        # the forward paper record is that window only once it is long enough
        ok = n >= MIN_SESSIONS
        out.append(("G2 not in-sample", ok,
                    f"tuned on {cal.get('window')} via {cal.get('tool')}; "
                    f"forward record {n}/{MIN_SESSIONS} sessions"))

    # G3 replicated out of sample
    if decl is None:
        out.append(("G3 out-of-sample", False, "UNDECLARED"))
    elif not oos:
        out.append(("G3 out-of-sample", False, "no OOS replication declared"))
    else:
        r = oos.get("median_ratio")
        ok = bool(oos.get("same_sign")) and r is not None and \
            abs(r - 1.0) <= OOS_MEDIAN_TOL
        out.append(("G3 out-of-sample", ok,
                    f"{oos.get('window')} same_sign={oos.get('same_sign')} "
                    f"median_ratio={r}"))

    # G4 cost-honest
    c = (decl or {}).get("costs") or {}
    ok = c.get("entry") == "cross" and c.get("exit") == "cross" \
        and not c.get("needs_passive_fills", True)
    out.append(("G4 cost-honest", bool(decl) and ok,
                f"entry={c.get('entry')} exit={c.get('exit')} "
                f"passive_needed={c.get('needs_passive_fills')}"))

    # G5 sign consistency
    sign = 100.0 * max((v > 0).mean(), (v < 0).mean()) if n else 0.0
    winning = n and (v > 0).mean() >= (v < 0).mean()
    out.append(("G5 sign>=70%", bool(n and sign >= MIN_SIGN_PCT and winning),
                f"{sign:.1f}% consistent, "
                f"{'winning' if winning else 'LOSING'} direction"))

    # G6 mean and median both positive
    out.append(("G6 mean&median>0", bool(n and v.mean() > 0 and np.median(v) > 0),
                f"mean {v.mean():+.2f}pt, median {np.median(v):+.2f}pt" if n else "-"))

    # G7 forward record length
    out.append((f"G7 >={MIN_SESSIONS} sessions", n >= MIN_SESSIONS,
                f"{n} live-paper sessions"))

    # G8 worst session bounded
    if n:
        med = np.median(v)
        worst = v.min()
        ok = med > 0 and worst >= -WORST_MULT * med
        out.append((f"G8 worst>=-{WORST_MULT:g}x median", ok,
                    f"worst {worst:+.2f}pt vs limit {-WORST_MULT*med:+.2f}pt"))
    else:
        out.append((f"G8 worst>=-{WORST_MULT:g}x median", False, "-"))

    # G9 beats its control twin
    twin = (decl or {}).get("control_twin")
    if not twin:
        out.append(("G9 beats twin", True, "no twin defined"))
    elif twin_pnl is None or not len(twin_pnl):
        out.append(("G9 beats twin", False, f"twin {twin} has no record"))
    else:
        tv = twin_pnl["pnl"].to_numpy()
        ok = bool(n) and np.median(v) > np.median(tv)
        out.append(("G9 beats twin", ok,
                    f"{np.median(v):+.2f} vs {twin} {np.median(tv):+.2f}pt"))

    # G10 is a config assertion made at routing time, not derivable here
    out.append(("G10 risk cfg", False, "verify at routing time (manual)"))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sleeve", default=None, help="e.g. ES:sweepfade")
    a = ap.parse_args()

    decls = yaml.safe_load(open(ROOT / "config" / "promotion.yaml"))["sleeves"]
    qdb = QuestDB(timeout=180.0)
    pnl = paper_pnl(qdb)
    if pnl.empty:
        raise SystemExit("no paper fills recorded yet")

    # Union of what has traded and what is DECLARED. A declared sleeve with no
    # record must appear and FAIL G1, not vanish -- an absent row reads as "not
    # applicable" when it actually means "never fired", which is the exact
    # failure mode this gate exists to catch.
    traded = sorted(pnl["sleeve"].unique())
    bases = {s.split(":")[-1] for s in traded}
    sleeves = ([a.sleeve] if a.sleeve else
               traded + [d for d in sorted(decls) if d not in bases])
    print("\n" + "=" * 78)
    print("PROMOTION GATE — see PROMOTION_GATE.md")
    print("ELIGIBLE means every gate passed. It does NOT mean route it; that is")
    print("always an explicit human decision.")
    print("=" * 78)

    eligible = []
    for s in sleeves:
        sub = pnl[pnl["sleeve"] == s]
        base = s.split(":")[-1]
        decl = decls.get(base)
        twin = (decl or {}).get("control_twin")
        tp = None
        if twin:
            pref = s.rsplit(":", 1)[0] + ":" if ":" in s else ""
            tp = pnl[pnl["sleeve"] == f"{pref}{twin}"]
        res = check(s, sub, decl, tp)
        npass = sum(1 for _, ok, _ in res if ok)
        verdict = "ELIGIBLE" if npass == len(res) else f"BLOCKED ({npass}/{len(res)})"
        if npass == len(res):
            eligible.append(s)
        print(f"\n[{s}]  {verdict}")
        for gate, ok, detail in res:
            print(f"    {'PASS' if ok else 'FAIL'}  {gate:22s} {detail}")

    print("\n" + "-" * 78)
    print(f"ELIGIBLE: {', '.join(eligible) if eligible else 'none'}")
    print("-" * 78 + "\n")
    sys.exit(0 if eligible else 1)


if __name__ == "__main__":
    main()
