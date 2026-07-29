"""Does the roster signal actually PAY? — causal exit rules on real trades.

tools/exit_signal_search.py found that, once the tautological self-features are
discarded (self_fe IS the oracle definition), every non-trivial signal at the
best-exit moment is a PEER feature: peer_active 0.667, peer_with 0.661,
peer_agree 0.646, peer_with_lost 0.352 (inverted). The trade's own velocity and
peak size were noise at 0.52 / 0.51.

Discrimination is necessary, not sufficient. This tests whether a rule that can
only see the PRESENT converts it into P&L. Every rule here is strictly causal:
running maxima only, no forward information, decided tick by tick exactly as a
live sleeve would.

  ROSTER RULES
    support_lost(f)  leave when the peers agreeing with us have fallen by f
                     from their peak -- "they stopped agreeing"
    support_floor(k) leave when fewer than k peers still agree
    agree_below(x)   leave when net agreement drops under x
    oppose(k)        leave when k peers have opened against us

  SELF BASELINES (so the roster has to actually beat something)
    giveback(f)      leave once f of the best excursion is handed back
    time(m)          leave after m minutes
    actual           what the sleeve really did

Reported against the ORACLE (best exit inside the same hold), which is the
ceiling, and against `actual`, which is the bar.

    .venv/Scripts/python.exe tools/exit_rule_test.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB                    # noqa: E402
from tools.exit_oracle import load, round_trips                # noqa: E402
from tools.exit_signal_search import features, position_grid   # noqa: E402

PU = {"ES": 50.0, "NQ": 20.0}


def _first(mask: np.ndarray) -> int | None:
    idx = np.flatnonzero(mask)
    return int(idx[0]) if len(idx) else None


def apply_rules(fr: pd.DataFrame, args) -> dict[str, float]:
    """P&L (points) of each causal rule on one trade's path."""
    adv = fr["_adv"].to_numpy()
    peak = np.maximum.accumulate(adv)
    with_ = fr["peer_with"].to_numpy().astype(float)
    withpeak = np.maximum.accumulate(with_)          # causal running max
    against = fr["peer_against"].to_numpy().astype(float)
    agree = fr["peer_agree"].to_numpy()
    mins = fr["self_mins"].to_numpy()
    out: dict[str, float] = {}

    hold, idx = {}, {}

    def take(name, i):
        idx[name] = i
        hold[name] = mins[-1] if i is None else mins[i]
        return adv[-1] if i is None else adv[i]

    for f in args.support_fracs:
        armed = withpeak >= args.min_peak
        out[f"support_lost {f:g}"] = take(f"support_lost {f:g}", _first(armed & (with_ <= withpeak * (1 - f))))
    for k in args.support_floors:
        armed = withpeak >= max(args.min_peak, k + 1)
        out[f"support_floor {k}"] = take(f"support_floor {k}", _first(armed & (with_ < k)))
    for x in args.agree_levels:
        armed = mins >= args.arm_min
        out[f"agree_below {x:g}"] = take(f"agree_below {x:g}", _first(armed & (agree < x)))
    for k in args.oppose_ks:
        out[f"oppose {k}"] = take(f"oppose {k}", _first(against >= k))
    for f in args.giveback_fracs:
        armed = peak >= args.min_fe
        out[f"giveback {f:g}"] = take(f"giveback {f:g}", _first(armed & (adv <= peak * (1 - f))))
    for m in args.times:
        out[f"time {m}m"] = take(f"time {m}m", _first(mins >= m))
    out["actual"] = adv[-1]; hold["actual"] = mins[-1]; idx["actual"] = None

    # ── COMBINATIONS: whichever reason fires FIRST wins ────────────────────
    # A single exit reason is rarely both timely and informed. `time` caps the
    # damage of holding forever; the roster signal says WHEN the move is done.
    # Taking the earlier of the two is how a real sleeve would run them.
    def combo(name, parts):
        ii = [idx.get(x) for x in parts]
        ii = [x for x in ii if x is not None]
        j = min(ii) if ii else None
        out[name] = adv[-1] if j is None else adv[j]
        hold[name] = mins[-1] if j is None else mins[j]

    for tm in ("time 15m", "time 30m"):
        for other in ("support_lost 0.25", "giveback 0.5", "agree_below 0.25"):
            if tm in idx and other in idx:
                combo(f"{tm} + {other}", (tm, other))
    if "support_lost 0.25" in idx and "giveback 0.5" in idx:
        combo("support_lost 0.25 + giveback 0.5",
              ("support_lost 0.25", "giveback 0.5"))

    out["ORACLE"] = float(adv.max())
    hold["ORACLE"] = mins[int(np.argmax(adv))]
    return out, hold


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="since", default="2026-07-27")
    ap.add_argument("--min-hold", type=float, default=2.0)
    ap.add_argument("--min-peak", type=int, default=2, help="peers needed to arm")
    ap.add_argument("--min-fe", type=float, default=2.0)
    ap.add_argument("--arm-min", type=float, default=1.0)
    a = ap.parse_args()
    a.support_fracs = [0.25, 0.5, 0.75]
    a.support_floors = [1, 2, 3]
    a.agree_levels = [0.0, 0.25, 0.5]
    a.oppose_ks = [1, 2, 3]
    a.giveback_fracs = [0.25, 0.5]
    a.times = [5, 15, 30, 60]

    qdb = QuestDB(timeout=180.0)
    f, px = load(qdb, a.since)
    rt = round_trips(f)
    grid = position_grid(f, f["t"].min(), f["t"].max() + pd.Timedelta(hours=1))

    rows, syms, holds = [], [], []
    for r in rt.itertuples(index=False):
        p = px.get(r.symbol)
        if p is None or p.empty:
            continue
        seg = p[(p.t >= r.entry_t) & (p.t <= r.exit_t)]
        if len(seg) < 20 or (r.exit_t - r.entry_t).total_seconds() / 60 < a.min_hold:
            continue
        fr = features(r, seg.reset_index(drop=True), grid)
        _pnl, _h = apply_rules(fr, a)
        rows.append(_pnl); holds.append(_h)
        syms.append(r.symbol)
    if not rows:
        raise SystemExit("no usable trades")
    D = pd.DataFrame(rows)
    mult = pd.Series(syms).map(PU).to_numpy()

    print("\n" + "=" * 80)
    print(f"EXIT RULE TEST — {len(D)} trades, causal rules only")
    print("=" * 80)
    base = float((D["actual"].to_numpy() * mult).sum())
    orac = float((D["ORACLE"].to_numpy() * mult).sum())
    print(f"\n  actual  {base:+11,.0f}$     ORACLE ceiling {orac:+11,.0f}$")
    H = pd.DataFrame(holds)
    print(f"\n{'rule':>20}{'total $':>12}{'vs actual':>12}{'% of oracle':>13}"
          f"{'win%':>7}{'med pt':>9}{'med hold':>10}")
    res = []
    for c in D.columns:
        if c in ("ORACLE",):
            continue
        v = D[c].to_numpy() * mult
        res.append((c, v.sum(), 100 * (D[c] > 0).mean(), float(np.median(D[c])),
                    float(np.median(H[c])) if c in H else float("nan")))
    for c, tot, win, med, hld in sorted(res, key=lambda x: -x[1]):
        star = "  <<<" if c != "actual" and tot > base else ""
        print(f"{c:>20}{tot:+12,.0f}{tot-base:+12,.0f}"
              f"{100*tot/orac if orac else 0:12.1f}%{win:6.0f}%{med:9.2f}"
              f"{hld:10.1f}{star}")
    print(f"\n  ORACLE med hold {float(np.median(H['ORACLE'])):.1f} min "
          f"(so any rule near this hold is timing-matched to the ceiling)")
    print("\n  Rules are causal: running maxima only, no forward information.")
    print("  '% of oracle' is the share of the best-exit ceiling captured.")
    print("=" * 80 + "\n")


if __name__ == "__main__":
    main()
