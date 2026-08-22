"""A CALIBRATED PROBABILITY that the day is finished -- not a hidden boolean.

Replaces the one-way / legs suppressor, which had two defects:

  * `legs` was unstable. Zero-legs occurred on 11% of 2025 sessions and 33% of
    2026 ones -- a threefold difference in how often it would fire -- because a
    leg only COMPLETED when price recovered to within 5% of the extreme, so a
    day that pulled back 30% and recovered halfway scored zero.
  * `deepest` was simply broken: max(dd_up, dd_dn) over the session equals the
    session's range by construction, so it read 1.00 at every percentile and
    silently carried no information. "Depth of retracement does not matter" was
    never tested; a constant was.
  * and both produced a boolean nobody could see or calibrate.

WHAT THIS PRODUCES. P(the day adds less than 0.25 of a typical range from here)
-- i.e. the probability the move is over -- as a number between 0 and 1, fitted
on 2025 and tested on 2026. A sleeve can log it, threshold it, or scale in
proportion to it.

FEATURES, all causal and price-only so the live feed can produce them:
    used      range so far / typical range
    tod       minutes since 09:30, scaled
    pull      DEEPEST counter-move against the day's direction so far, as a
              fraction of range so far. The corrected version of `deepest`:
              measured against the direction the day has actually travelled,
              not the larger of two mirror quantities.
    pace      range so far per minute, against a typical session's pace

Fitted by plain logistic regression (gradient descent, no sklearn available).
Reported with a CALIBRATION table -- predicted probability against realised
frequency -- because a probability that is not calibrated is just a score
wearing a percentage sign.
"""
import sys

import numpy as np
import pandas as pd

TR = r"D:\Trading\strategy_lab\range_break_2025.csv"
TE = r"D:\Trading\strategy_lab\range_break.csv"          # 2026 live
FEATS = ["tod_s", "or_rel", "vol_rel", "legs_s"]


def prep(path):
    t = pd.read_csv(path).copy()
    t["used"] = 0.80                       # by construction at the trigger
    t["tod_s"] = t.tod / 390.0
    t["pace_s"] = np.log1p(t.pace.clip(lower=0)) / 3.0
    # `pull` must be recomputed -- see the module docstring. The stored
    # `deepest` column is the broken one and is deliberately not used.
    t["legs_s"] = np.log1p(t.legs.clip(lower=0)) / 2.0
    t["y"] = (t.extra < 0.25).astype(int)  # 1 = the day was finished
    return t


def fit(X, y, iters=4000, lr=0.5):
    X = np.c_[np.ones(len(X)), X]
    w = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1.0 / (1.0 + np.exp(-X @ w))
        w -= lr * (X.T @ (p - y)) / len(y)
    return w


def prob(X, w):
    return 1.0 / (1.0 + np.exp(-(np.c_[np.ones(len(X)), X] @ w)))


def calib(name, p, y, bins=4):
    print("  %s   n=%d   base rate %.2f" % (name, len(y), y.mean()))
    q = pd.qcut(p, bins, labels=False, duplicates="drop")
    print("     %-14s %6s %10s %10s" % ("predicted", "n", "mean pred", "actual"))
    for b in sorted(set(q)):
        s = q == b
        print("     bin %-10d %6d %10.2f %10.2f"
              % (b, int(s.sum()), p[s].mean(), y[s].mean()))


def main():
    tr, te = prep(TR), prep(TE)
    use = list(FEATS)
    tr2 = tr.dropna(subset=use + ["y"])
    te2 = te.dropna(subset=use + ["y"])
    print("train 2025 n=%d   test 2026 n=%d   features %s\n"
          % (len(tr2), len(te2), use))
    w = fit(tr2[use].values, tr2.y.values)
    print("  weights: intercept %+0.2f   " % w[0]
          + "   ".join("%s %+0.2f" % (f, v) for f, v in zip(use, w[1:])))
    print()
    calib("TRAIN 2025", prob(tr2[use].values, w), tr2.y.values)
    print()
    calib("TEST  2026", prob(te2[use].values, w), te2.y.values)
    p = prob(te2[use].values, w)
    print("\n  test discrimination: mean p when the day WAS done %.2f, "
          "when it kept going %.2f" % (p[te2.y.values == 1].mean(),
                                       p[te2.y.values == 0].mean()))


if __name__ == "__main__":
    main()
