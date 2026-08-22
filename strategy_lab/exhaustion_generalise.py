"""Does the 2026-08-21 top signature generalise? Judged conditionally, not 100%.

THE SIGNATURE, from the ES high at 11:53 (strategy_lab/top_anatomy_0821.py):
volume stayed high while net conviction collapsed and size dominance flipped.

    11:51  delta +566  vol 1174  buy_sz 15.00  sell_sz  6.33   |d|/vol 48%
    11:52  delta +207  vol 1067  buy_sz 10.80  sell_sz  9.56   |d|/vol 19%
    11:53  delta  +20  vol 1224  buy_sz 11.11  sell_sz 13.68   |d|/vol  2%  <- HIGH

CRITERIA FIXED BEFORE SEEING ANY RESULT, because the standing failure mode is
demanding that an edge work nearly always and discarding it when it does not:

  1. The question is CONDITIONAL. Not "does this only appear at tops" -- it will
     not. It is whether the forward distribution DIFFERS when it appears.
  2. The baseline is EXTENDED-vs-EXTENDED. Reversion after a big move is
     expected anyway, so the control is minutes equally near the high and
     equally far into the day's range, WITHOUT the signature. Anything else
     measures extension, not the signature.
  3. FULL DISTRIBUTIONS are reported -- quartiles and tails, not a mean and a
     p-value. A 58/42 skew with asymmetric payoff is an edge.
  4. Consistency of DIRECTION across ES and NQ is what counts, not equality of
     magnitude. Different instruments, different scale.
  5. Firing RARELY is a feature. A signal present at 20% of tops and 5% of other
     extended minutes is valuable. "Misses most tops" is not a criticism.

WHERE IT IS MEASURED. Only at minutes NEAR THE SESSION HIGH SO FAR -- that is
where the exit decision actually arises. Measuring it across all minutes would
dilute it with times nobody would be asking the question.

Everything is causal: session high, range used, medians and rolling references
all use data up to that minute only.
"""
import sys

sys.path.insert(0, r"D:\Trading\engine")
import numpy as np
import pandas as pd
from engine.adapters.questdb import QuestDB

q = QuestDB(timeout=900)
FWD = (15, 30, 60)


def minutes(sym):
    t = q.df("SELECT ts,price,size,aggressor FROM claude_ticks_live WHERE symbol='"
             + sym + "' ORDER BY ts").copy()
    t["et"] = pd.to_datetime(t["ts"]).dt.tz_convert("America/New_York")
    t["day"] = t.et.dt.strftime("%Y-%m-%d")
    t["mod"] = t.et.dt.hour * 60 + t.et.dt.minute
    t = t[(t["mod"] >= 570) & (t["mod"] < 960)]
    t["signed"] = t["size"] * t.aggressor
    g = t.groupby(["day", "mod"]).agg(
        px=("price", "last"), vol=("size", "sum"), delta=("signed", "sum"),
        n=("size", "size")).reset_index()
    bs = t[t.aggressor > 0].groupby(["day", "mod"])["size"].mean().rename("buy_sz")
    ss = t[t.aggressor < 0].groupby(["day", "mod"])["size"].mean().rename("sell_sz")
    return g.join(bs, on=["day", "mod"]).join(ss, on=["day", "mod"])


def build(sym):
    g = minutes(sym)
    out = []
    prior = []
    for day, d in g.groupby("day", sort=True):
        d = d.sort_values("mod").reset_index(drop=True)
        if len(d) < 200:
            prior.append(d.px.max() - d.px.min())
            continue
        med = float(np.median(prior[-10:])) if len(prior) >= 3 else np.nan
        d["hi"] = d.px.cummax()
        d["lo"] = d.px.cummin()
        d["rng"] = d.hi - d.lo
        d["range_used"] = d.rng / med if med and med > 0 else np.nan
        # near the session high so far, relative to the day's own range
        d["from_hi"] = (d.hi - d.px) / d.rng.replace(0, np.nan)
        d["conv"] = d.delta.abs() / d.vol.replace(0, np.nan)
        d["dom"] = d.buy_sz / d.sell_sz.replace(0, np.nan)
        # references from the PRIOR 15 minutes only
        d["conv_ref"] = d.conv.shift(1).rolling(15, min_periods=8).median()
        d["dom_ref"] = d.dom.shift(1).rolling(15, min_periods=8).median()
        d["vol_ref"] = d.vol.shift(1).expanding(20).median()
        for f in FWD:
            d["fwd%d" % f] = (d.px.shift(-f) - d.px) / med if med and med > 0 else np.nan
        d["sym"] = sym
        out.append(d)
        prior.append(d.px.max() - d.px.min())
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


def describe(lab, s, f):
    c = s["fwd%d" % f].dropna()
    if len(c) < 12:
        print("    %-34s n=%-4d (too few)" % (lab, len(c)))
        return
    print("    %-34s n=%-4d  med %+6.3f  p25 %+6.3f  p75 %+6.3f  down %3.0f%%  "
          "mean %+6.3f" % (lab, len(c), c.median(), c.quantile(.25),
                           c.quantile(.75), 100 * (c < 0).mean(), c.mean()))


def main():
    frames = [build(s) for s in ("ES", "NQ")]
    A = pd.concat([f for f in frames if len(f)], ignore_index=True)
    A = A.dropna(subset=["range_used", "conv", "dom", "conv_ref", "dom_ref"])
    print("%d minute-rows, %d sessions, %s -> %s"
          % (len(A), A.day.nunique(), A.day.min(), A.day.max()))

    # WHERE the question arises: near the session high, day already extended
    near = A[(A.from_hi <= 0.15) & (A.range_used >= 0.6)]
    print("near session high AND range_used>=0.6: %d rows (%.1f%% of all)\n"
          % (len(near), 100 * len(near) / len(A)))

    # the signature, causally: conviction collapsed vs its own recent level,
    # size dominance flipped against the up-move, participation NOT falling
    sig = ((near.conv <= 0.5 * near.conv_ref) &
           (near.dom < 1.0) & (near.dom_ref >= 1.0) &
           (near.vol >= near.vol_ref))
    print("signature present on %d of %d extended-near-high minutes (%.1f%%)\n"
          % (int(sig.sum()), len(near), 100 * sig.mean()))

    for f in FWD:
        print("  forward %d minutes (in units of a median daily range):" % f)
        describe("EXTENDED near high, signature", near[sig], f)
        describe("EXTENDED near high, no signature", near[~sig], f)
        describe("all minutes (context baseline)", A, f)
        for s in ("ES", "NQ"):
            n2 = near[near.sym == s]
            s2 = sig[near.sym == s]
            describe("  %s signature" % s, n2[s2], f)
            describe("  %s no signature" % s, n2[~s2], f)
        print("")


if __name__ == "__main__":
    main()
