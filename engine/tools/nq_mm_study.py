"""Does a two-sided resting bracket ("buy low / sell high") work on NQ?

The idea under test: rest a bid at ref-w and an offer at ref+w and let NQ's
volatility hit both, banking 2w. The intuition that "both would almost always be
hit" is exactly what this measures -- and what it is likely to disprove, because
fill probability is CONDITIONAL ON DIRECTION. On a trend day only one side
fills, and it is the wrong one: the unfilled side is unfilled precisely because
price ran away from it. That is adverse selection, and it is the reason naive
market making loses money. This tool quantifies it instead of arguing about it.

Note this is NOT market making in the professional sense (no rebates on CME for
retail, and a retail order sits at the back of a FIFO queue at every level). It
is a mean-reversion bracket, so it must be judged as a directional bet on range
vs trend -- which is what the numbers below do.

Measured per session on 24h NQ 1m bars (claude_bars_live):
  - hit rates: both sides / one side only / neither, vs half-width w
  - naive P&L: both-filled banks 2w; one-filled is marked at the 15:59 close
  - the adverse-selection split: P&L of one-sided days, by which side filled
  - contingencies for the one-filled case:
      eod       flat at 15:59 (baseline)
      stop      hard stop k*w beyond the fill
      breakeven move the opposite quote to the entry (scratch the inventory)
      recenter  move the opposite quote to ref (take half the intended edge)

    .venv/Scripts/python.exe tools/nq_mm_study.py
    .venv/Scripts/python.exe tools/nq_mm_study.py --symbol ES --widths 5,10,15

Caveats stated up front: 1m bars cannot resolve intrabar path, so when both
quotes are touched inside the SAME bar the fill order is unknowable -- those
sessions are reported separately as `ambig` rather than silently counted as
wins (counting them as wins is the single easiest way to fake a good result
here). Fills assume the limit is filled when touched, which FLATTERS the
strategy: a real resting retail order at the back of the queue often does not
fill on a one-tick touch.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB                      # noqa: E402
from engine.core.timeutil import et_minute_of_day, et_session_date  # noqa: E402

OPEN_MIN, CLOSE_MIN = 570, 959      # 09:30 .. 15:59 ET
POINT_USD = {"NQ": 20.0, "ES": 50.0}


def load(qdb: QuestDB, symbol: str) -> pd.DataFrame:
    df = qdb.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live "
                f"WHERE symbol = '{symbol}' ORDER BY ts")
    if df.empty:
        raise SystemExit(f"no claude_bars_live rows for {symbol}")
    tns = df["ts"].astype("int64").to_numpy()
    df["sd"] = [et_session_date(int(t)) for t in tns]
    df["em"] = [et_minute_of_day(int(t)) for t in tns]
    for c in ("o", "h", "l", "c"):
        df[c] = df[c].astype(float)
    return df


def session_frames(df: pd.DataFrame):
    """Yield (day, rth_frame) for sessions with a usable RTH."""
    for day, g in df.groupby("sd"):
        r = g[(g["em"] >= OPEN_MIN) & (g["em"] <= CLOSE_MIN)]
        if len(r) >= 60:
            yield day, r.reset_index(drop=True)


def simulate(r: pd.DataFrame, w: float, stop_k: float, through: float = 0.0):
    """One bracket per session, anchored on the RTH open. Returns a dict of the
    outcome plus P&L under each contingency rule (in points).

    `through`: points price must trade BEYOND the limit before it counts as
    filled. through=0 is the optimistic touch=fill assumption; a real resting
    retail order sits at the back of a FIFO queue and typically needs the level
    to trade through before it is filled. This is the assumption the narrow-w
    results are most sensitive to, so it is a knob, not a hardcoded freebie."""
    ref = float(r["o"].iloc[0])
    bid, ask = ref - w, ref + w
    close = float(r["c"].iloc[-1])
    hi = r["h"].to_numpy(); lo = r["l"].to_numpy(); n = len(r)

    # first fill index for each side (-1 = never)
    ib = next((i for i in range(n) if lo[i] <= bid - through), -1)
    ia = next((i for i in range(n) if hi[i] >= ask + through), -1)
    both = ib >= 0 and ia >= 0
    ambig = both and ib == ia                    # same bar: path unknowable

    out = {"ref": ref, "both": both, "ambig": ambig,
           "one": (ib >= 0) != (ia >= 0), "none": ib < 0 and ia < 0,
           "side": (1 if ib >= 0 else -1) if (ib >= 0) != (ia >= 0) else 0}

    if both:                                     # round trip banked either order
        pnl = 2 * w
        out.update(eod=pnl, stop=pnl, breakeven=pnl, recenter=pnl)
        return out
    if ib < 0 and ia < 0:
        out.update(eod=0.0, stop=0.0, breakeven=0.0, recenter=0.0)
        return out

    # ── exactly one side filled: this is where the money is decided ──────────
    if ib >= 0:                                  # long from `bid`
        entry, i0, sgn = bid, ib, 1
        tgt_be, tgt_rc = bid, ref                # scratch / half-edge exits
        stop_px = bid - stop_k * w
    else:                                        # short from `ask`
        entry, i0, sgn = ask, ia, -1
        tgt_be, tgt_rc = ask, ref
        stop_px = ask + stop_k * w

    out["eod"] = sgn * (close - entry)

    def first_exit(target: float | None, stop: float | None) -> float:
        """Walk forward from the fill; whichever level is touched first wins.
        When both are touched in one bar, assume the STOP (conservative)."""
        for i in range(i0, n):
            hit_s = stop is not None and (lo[i] <= stop if sgn > 0 else hi[i] >= stop)
            hit_t = target is not None and (hi[i] >= target if sgn > 0 else lo[i] <= target)
            if hit_s:
                return sgn * (stop - entry)
            if hit_t:
                return sgn * (target - entry)
        return sgn * (close - entry)

    out["stop"] = first_exit(None, stop_px)
    out["breakeven"] = first_exit(tgt_be, stop_px)
    out["recenter"] = first_exit(tgt_rc, stop_px)
    return out


def run(symbol: str, widths: list[float], stop_k: float, through: float) -> None:
    qdb = QuestDB(timeout=120.0)
    df = load(qdb, symbol)
    sess = list(session_frames(df))
    pu = POINT_USD.get(symbol, 50.0)
    rng = np.mean([float(r["h"].max() - r["l"].min()) for _, r in sess])

    print("\n" + "=" * 84)
    print(f"TWO-SIDED RESTING BRACKET  —  {symbol}, {len(sess)} RTH sessions "
          f"({sess[0][0]}..{sess[-1][0]})")
    print(f"anchor = RTH open;  mean RTH range = {rng:.1f}pt;  "
          f"stop = {stop_k}x width;  1pt = ${pu:.0f};  through = {through}pt")
    print("through=0 means touch=fill, which FLATTERS: a queued retail limit often misses")
    print("=" * 84)

    print(f"\n{'w':>5} {'both':>5} {'one':>4} {'none':>5} {'ambig':>6} | "
          f"{'eod':>18} {'stop':>10} {'bkeven':>10} {'recentr':>10}")
    print(f"{'':>5} {'':>5} {'':>4} {'':>5} {'':>6} | "
          f"{'tot pt   ($/day)':>18} {'tot pt':>10} {'tot pt':>10} {'tot pt':>10}")
    rows = []
    for w in widths:
        res = [simulate(r, w, stop_k, through) for _, r in sess]
        nb = sum(x["both"] for x in res); no = sum(x["one"] for x in res)
        nn = sum(x["none"] for x in res); na = sum(x["ambig"] for x in res)
        tot = {k: sum(x[k] for x in res) for k in ("eod", "stop", "breakeven", "recenter")}
        print(f"{w:5.0f} {nb:5d} {no:4d} {nn:5d} {na:6d} | "
              f"{tot['eod']:+9.1f} ({tot['eod']*pu/len(sess):+7,.0f}) "
              f"{tot['stop']:+10.1f} {tot['breakeven']:+10.1f} {tot['recenter']:+10.1f}")
        rows.append((w, res))

    # ── the adverse-selection diagnostic ────────────────────────────────────
    print("\n[ADVERSE SELECTION] one-sided sessions only — did the filled side lose?")
    print(f"{'w':>5} {'longs':>6} {'meanP&L':>9} {'shorts':>7} {'meanP&L':>9} "
          f"{'combined':>10}  (eod mark)")
    for w, res in rows:
        L = [x["eod"] for x in res if x["one"] and x["side"] > 0]
        S = [x["eod"] for x in res if x["one"] and x["side"] < 0]
        both = L + S
        print(f"{w:5.0f} {len(L):6d} {np.mean(L) if L else 0:+9.2f} "
              f"{len(S):7d} {np.mean(S) if S else 0:+9.2f} "
              f"{np.mean(both) if both else 0:+10.2f}")
    print("\nIf 'combined' is negative, the side that fills is systematically the")
    print("wrong side — the bracket is selling cheap optionality, not making a")
    print("market. That loss must be smaller than 2w x (both-hit days) to survive.")
    print("=" * 84 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NQ")
    ap.add_argument("--widths", default="20,30,40,60,80,120",
                    help="comma list of half-widths in points")
    ap.add_argument("--stop-k", type=float, default=2.0,
                    help="hard stop distance as a multiple of the half-width")
    ap.add_argument("--through", type=float, default=0.0,
                    help="points price must trade BEYOND the limit to fill "
                         "(0 = optimistic touch=fill; try 1-2 ticks)")
    a = ap.parse_args()
    run(a.symbol, [float(x) for x in a.widths.split(",")], a.stop_k, a.through)


if __name__ == "__main__":
    main()
