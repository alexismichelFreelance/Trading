"""Which breakouts RUN and which come back? Feature search on labelled events.

236 breakouts, each labelled by what actually happened: 43 never returned to the
level (the move you want) and 193 came back within 60min. The question is
whether anything OBSERVABLE AT THE BREAKOUT BAR separates them.

Every feature is computed from bars up to and including the breakout bar. No
feature may look forward -- that is the only thing that makes this worth
running.

Scored two ways, because either alone is misleading:
  * separation of the RUN label (does the feature identify the rippers?)
  * forward $ at the close (does acting on it make money?)
A feature that predicts the label but not the money is a curiosity.

    python breakout_discriminator.py
"""
from __future__ import annotations

import numpy as np
import pandas as pd

import breakout_retest as B

TICK = 0.25


def features(day_bars: pd.DataFrame, lv: dict) -> list[dict]:
    rth = day_bars[(day_bars["min"] >= B.RTH_OPEN) & (day_bars["min"] <= B.RTH_CLOSE)]
    if len(rth) < 60:
        return []
    m = rth["min"].to_numpy()
    o, c = rth.o.to_numpy(), rth.c.to_numpy()
    h, l, v = rth.h.to_numpy(), rth.l.to_numpy(), rth.vol.to_numpy()
    eth = day_bars[day_bars["min"] < B.RTH_OPEN]
    out = []
    for name, (px, d) in lv.items():
        if not np.isfinite(px):
            continue
        first_ok = B.RTH_OPEN + B.OR_MIN if name.startswith("OR") else B.RTH_OPEN
        arm = np.flatnonzero(m >= first_ok)
        if not len(arm):
            continue
        if (c[int(arm[0])] - px) * d > 0:          # gapped past it: not a break
            continue
        beyond = np.flatnonzero((m >= first_ok) & ((c - px) * d > 0))
        if not len(beyond):
            continue
        i = int(beyond[0])
        if i < 11:                                  # need history for the features
            continue
        pre = slice(max(0, i - 10), i)
        rng = h[pre] - l[pre]
        atr = rng.mean() if len(rng) else np.nan
        if not np.isfinite(atr) or atr <= 0:
            continue
        bar_rng = h[i] - l[i]

        # --- did it run, or come back? (the label) ---
        j_end = np.searchsorted(m, m[i] + B.RETEST_WIN_MIN, side="right")
        seg = np.arange(i + 1, j_end)
        ran = True
        if len(seg):
            run = (c[seg] - px) * d / TICK
            armed = np.flatnonzero(run >= B.MIN_PENETRATION_T)
            if len(armed):
                s = seg[armed[0]]
                back = np.arange(s + 1, j_end)
                if len(back):
                    touched = (l[back] <= px) if d > 0 else (h[back] >= px)
                    ran = not touched.any()

        f = {
            "level": name, "dir": d, "ran": ran, "mkt_px": c[i], "min": m[i],
            # how decisively the bar closed through
            "penetration_t": (c[i] - px) * d / TICK,
            # conviction of the breakout bar itself
            "vol_ratio": v[i] / max(v[pre].mean(), 1e-9),
            "range_ratio": bar_rng / atr,
            "body_frac": abs(c[i] - o[i]) / bar_rng if bar_rng > 0 else 0.0,
            # closed at the extreme of its own bar = no rejection wick
            "close_pos": ((c[i] - l[i]) / bar_rng if d > 0
                          else (h[i] - c[i]) / bar_rng) if bar_rng > 0 else 0.5,
            # how tight was the base before the break
            "coil": (h[pre].max() - l[pre].min()) / (10 * atr),
            "pre_range_t": (h[pre].max() - l[pre].min()) / TICK,
            # momentum into the level over the prior 20 bars
            "approach_t": (c[i] - c[max(0, i - 20)]) * d / TICK,
            # how many bars touched the level before it gave way
            "touches": int(((l[pre] <= px) & (h[pre] >= px)).sum()),
            "tod": m[i] - B.RTH_OPEN,
            "on_range_t": ((eth.h.max() - eth.l.min()) / TICK
                           if len(eth) else np.nan),
        }
        f["fwd_eod_t"] = (c[-1] - c[i]) * d / TICK
        k = np.searchsorted(m, m[i] + 60, side="right") - 1
        f["fwd_60_t"] = (c[k] - c[i]) * d / TICK
        out.append(f)
    return out


def main() -> None:
    rows = []
    for sym in ("ESH5", "ESM5"):
        b = B.bars(sym)
        days = sorted(b.day.unique())
        for k, day in enumerate(days):
            dd = b[b.day == day]
            eth = dd[dd["min"] < B.RTH_OPEN]
            orb = dd[(dd["min"] >= B.RTH_OPEN) & (dd["min"] < B.RTH_OPEN + B.OR_MIN)]
            prev = b[b.day == days[k - 1]] if k else b.iloc[0:0]
            pr = prev[(prev["min"] >= B.RTH_OPEN) & (prev["min"] <= B.RTH_CLOSE)]
            for e in features(dd, B.levels(pr, eth, orb)):
                e.update(sym=sym, day=day)
                rows.append(e)
    E = pd.DataFrame(rows)
    print(f"\n{'='*100}")
    print(f"BREAKOUT DISCRIMINATOR — {len(E)} events, {int(E.ran.sum())} RAN, "
          f"{int((~E.ran).sum())} came back")
    print(f"{'='*100}")

    feats = ["penetration_t", "vol_ratio", "range_ratio", "body_frac",
             "close_pos", "coil", "pre_range_t", "approach_t", "touches",
             "tod", "on_range_t"]
    print(f"  {'feature':<16}{'RAN mean':>10}{'BACK mean':>11}{'t':>7}"
          f"{'  | top tercile:':<16}{'ran%':>7}{'fwdEOD':>9}{'  bottom:':<10}"
          f"{'ran%':>7}{'fwdEOD':>9}")
    res = []
    for f in feats:
        x = E[f].to_numpy(dtype=float)
        ok = np.isfinite(x)
        if ok.sum() < 100:
            continue
        a, bk = x[ok & E.ran.to_numpy()], x[ok & ~E.ran.to_numpy()]
        sp = (a.mean() - bk.mean()) / np.sqrt(a.var(ddof=1)/len(a) + bk.var(ddof=1)/len(bk))
        q1, q3 = np.nanpercentile(x[ok], [33, 67])
        hi, lo = E[ok & (x >= q3)], E[ok & (x <= q1)]
        res.append((abs(sp), f, a.mean(), bk.mean(), sp,
                    100*hi.ran.mean(), hi.fwd_eod_t.mean(),
                    100*lo.ran.mean(), lo.fwd_eod_t.mean()))
    for _, f, am, bm, sp, hr, hf, lr, lf in sorted(res, reverse=True):
        print(f"  {f:<16}{am:>10.2f}{bm:>11.2f}{sp:>+7.1f}{'':<16}"
              f"{hr:>6.0f}%{hf:>+9.1f}{'':<10}{lr:>6.0f}%{lf:>+9.1f}")
    print(f"\n  t is the separation between RAN and CAME-BACK groups. |t|>2 is a")
    print(f"  real difference; fwdEOD is mean ticks to the close from the "
          f"breakout bar.")
    print(f"{'='*100}\n")
    E.to_pickle(".cache_breakout_events.pkl")


if __name__ == "__main__":
    main()
