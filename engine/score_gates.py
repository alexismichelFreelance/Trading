"""Score every sleeve against the REPLAY-COMPUTABLE promotion gates.

PROMOTION_GATE.md has ten gates. Replay can speak to four and a half of them:

    G1  alive          fires on >= 10% of sessions, no exceptions
    G5  sign >= 70%    share of sessions whose P&L has the same sign
    G6  mean & median  both positive
    G8  worst >= -3x   worst session not worse than 3x the median gain
    G9  beats twin     where a control twin exists

It CANNOT speak to G7 (>= 40 forward paper sessions), and the gate doc is
explicit that replay never substitutes for it -- replay only qualifies a sleeve
to START accumulating that record. G2/G3/G4/G10 are declarations, not
measurements. So nothing here can make a sleeve eligible; it can only say which
sleeves are still standing when the measurable gates are applied, and which are
already dead.

P&L per sleeve per session is reconstructed from the replay fills: every sleeve
flattens daily, so a session's P&L is -sum(side * qty * price) * point_usd,
exact for a book that ends flat. Round-turn cost is charged per contract pair.
"""
import sys
import numpy as np
import pandas as pd

PU = {"ES": 50.0, "NQ": 20.0}
COST_PTS = 0.32          # ~$16 ES round turn: commission + one tick of spread
FIRE_MIN = 0.10          # G1
SIGN_MIN = 0.70          # G5
TWINS = {                # promoted variant -> its control
    "flow_gex": "flow", "flow_lg": "flow", "ignition_gex": "ignition",
    "ignition_lg": "ignition", "onbreak_gex": "onbreak", "onbreak_lg": "onbreak",
    "opendrive_gex": "opendrive", "opendrive_vac": "opendrive",
    "sweepfade": "sweepfollow", "ignition_fixed": "ignition",
}


def per_session(sym: str) -> pd.DataFrame:
    """Realised P&L per sleeve per session, by AVERAGE-COST round trips.

    Not `-sum(side*qty*price)` per day. That shortcut is exact only for a book
    that ends the session flat, and the swing sleeves (ibs, rsi2, dip3) hold
    overnight by design -- their unpaired entry fill makes the shortcut book the
    raw notional, which showed up as a -$374,233 "loss" on a single fill. A
    closed round trip is assigned to the session of its EXIT; a position still
    open at the end of the window is simply not counted."""
    f = pd.read_csv(f".cache/replay_fills_{sym}_live.csv").sort_values("ts")
    if f.empty:
        return pd.DataFrame()
    t = pd.to_datetime(f.ts, unit="ns", utc=True).dt.tz_convert("America/New_York")
    f["day"] = t.dt.strftime("%Y-%m-%d")
    rows = []
    for slv, d in f.groupby("sleeve"):
        pos, avg = 0.0, 0.0
        for r in d.itertuples():
            q = r.side * r.qty
            if pos == 0 or (pos > 0) == (q > 0):          # opening or adding
                avg = (avg * abs(pos) + r.price * abs(q)) / (abs(pos) + abs(q))
                pos += q
            else:                                         # reducing / closing
                closed = min(abs(q), abs(pos))
                direction = 1.0 if pos > 0 else -1.0
                rows.append(dict(sleeve=slv, day=r.day,
                                 pts=(r.price - avg) * direction * closed,
                                 contracts=2.0 * closed))
                pos += q
                if (pos > 0) != (direction > 0) and pos != 0:   # flipped
                    avg = r.price
    if not rows:
        return pd.DataFrame()
    tr = pd.DataFrame(rows)
    out = (tr.groupby(["sleeve", "day"])
             .agg(pts=("pts", "sum"), contracts=("contracts", "sum"))
             .reset_index())
    out["pnl"] = (out.pts - COST_PTS * out.contracts / 2.0) * PU[sym]
    return out


def score(sym: str, n_sessions: int) -> pd.DataFrame:
    ps = per_session(sym)
    if ps.empty:
        return pd.DataFrame()
    rows = []
    for slv, d in ps.groupby("sleeve"):
        p = d.pnl.to_numpy(float)
        med, mean = float(np.median(p)), float(p.mean())
        worst = float(p.min())
        eq = np.cumsum(p)
        rows.append(dict(
            sleeve=slv.split(":")[-1], n=len(p), fire=len(p) / n_sessions,
            sign=float((p > 0).mean()), mean=mean, median=med, worst=worst,
            total=float(p.sum()),
            dd=float((eq - np.maximum.accumulate(eq)).min()),
            g1=len(p) / n_sessions >= FIRE_MIN,
            g5=float((p > 0).mean()) >= SIGN_MIN,
            g6=(mean > 0) and (med > 0),
            g8=(worst >= -3 * med) if med > 0 else False,
        ))
    r = pd.DataFrame(rows).set_index("sleeve")
    # G9: beat the control twin on mean, median, sign and worst
    r["g9"] = True
    for promoted, control in TWINS.items():
        if promoted in r.index and control in r.index:
            a, b = r.loc[promoted], r.loc[control]
            r.loc[promoted, "g9"] = bool(
                a["mean"] > b["mean"] and a["median"] > b["median"]
                and a["sign"] >= b["sign"] and a["worst"] >= b["worst"])
    r["passed"] = r[["g1", "g5", "g6", "g8", "g9"]].sum(axis=1)
    return r.sort_values(["passed", "mean"], ascending=False)


def show(sym: str, n_sessions: int) -> None:
    r = score(sym, n_sessions)
    if r.empty:
        print(f"\n{sym}: no fills\n")
        return
    print(f"\n{'=' * 96}\n{sym} — {n_sessions} replayed sessions, "
          f"{len(r)} sleeves with fills\n{'=' * 96}")
    print(f"{'sleeve':>22}{'n':>5}{'fire':>6}{'sign':>6}{'mean$':>9}{'med$':>8}"
          f"{'worst$':>10}{'total$':>10}  G1 G5 G6 G8 G9  ok")
    for s, x in r.iterrows():
        f = lambda b: " Y" if b else " ."          # noqa: E731
        print(f"{s:>22}{int(x.n):>5}{x.fire:>6.0%}{x['sign']:>6.0%}"
              f"{x['mean']:>9,.0f}{x['median']:>8,.0f}{x.worst:>10,.0f}"
              f"{x.total:>10,.0f} {f(x.g1)} {f(x.g5)} {f(x.g6)} {f(x.g8)} {f(x.g9)}"
              f"{int(x.passed):>4}")
    alive = r[r.passed == 5]
    print(f"\n  passing every replay-computable gate: "
          f"{', '.join(alive.index) if len(alive) else 'NONE'}")
    print(f"  G7 (>=40 forward paper sessions) is NOT computable from replay and "
          f"is required. Nothing here is eligible.")


if __name__ == "__main__":
    for sym, n in (("ES", int(sys.argv[1]) if len(sys.argv) > 1 else 46),
                   ("NQ", int(sys.argv[2]) if len(sys.argv) > 2 else 46)):
        try:
            show(sym, n)
        except FileNotFoundError:
            print(f"\n{sym}: no replay fills cached yet")
