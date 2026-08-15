"""Every filter, against the only null that matters: a random cull of the same size.

WHY. On 2026-08-15 ES:onbreak_gex showed +925 over 12 days against the ungated
sleeve's -1,488 over 23 -- an apparent +2,400 of value from the gamma gate. It
was noise. Drawing 12 of the 23 days at random gives a mean of -767 with sd
1,891, and 19.7% of random draws do at least as well.

The mechanism is arithmetic, not strategy: ONBREAK LOSES, so ANY filter that
removes half its days improves the total on average. Compare a filtered sleeve
against its unfiltered self and every filter on a losing sleeve looks like an
edge, while every filter on a winning sleeve looks like damage. The comparison
has to be against a RANDOM selection of the same size.

Caveat, stated rather than hidden: a filtered sleeve's days are not exactly a
subset of the raw sleeve's. These are single-position sleeves, so declining one
entry can free the sleeve to take a different one later. The null treats the
filter as a pure day-cull, which is the right first approximation and slightly
generous to the filter.

Usage: run after a portfolio replay; reads .cache/replay_fills_<SYM>_live.csv.
"""
from __future__ import annotations

import collections
import csv
import datetime as dt
import random
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ROOT))

ET = dt.timezone(dt.timedelta(hours=-4))
N_SIM = 20000

# (filtered, unfiltered, what the filter claims to do)
PAIRS = [
    ("ES:onbreak_lg", "ES:onbreak", "local gamma sign at price"),
    ("ES:ignition_lg", "ES:ignition", "local gamma sign at price"),
    ("ES:flow_lg", "ES:flow", "local gamma sign at price"),
    ("ES:trendjoin_pk", "ES:trendjoin", "not within 15pt of a pocket edge"),
    ("ES:opendrive_pk", "ES:opendrive", "not within 15pt of a pocket edge"),
    ("ES:ignition_fixed", "ES:ignition", "fixed exit, not the trailing one"),
    ("ES:trendjoin_narrow", "ES:trendjoin", "half the confirmation distance"),
]


def daily(rows, sleeve, pv=50.0):
    pos, avg = 0, 0.0
    out = collections.Counter()
    for r in rows:
        if r["sleeve"] != sleeve:
            continue
        q = int(float(r["side"])) * int(float(r["qty"]))
        px = float(r["price"])
        day = dt.datetime.fromtimestamp(int(float(r["ts"])) / 1e9,
                                        ET).strftime("%Y-%m-%d")
        if pos and (q > 0) != (pos > 0):
            m = min(abs(q), abs(pos))
            out[day] += (px - avg) * (1 if pos > 0 else -1) * m * pv
            new = pos + q
            if new and (new > 0) != (pos > 0):
                avg = px
            pos = new
        else:
            avg = (avg * abs(pos) + px * abs(q)) / (abs(pos) + abs(q)) if pos else px
            pos += q
    return out


def main(sym: str = "ES") -> None:
    path = ROOT / ".cache" / f"replay_fills_{sym}_live.csv"
    rows = sorted(csv.DictReader(open(path, encoding="utf-8-sig")),
                  key=lambda r: float(r["ts"]))
    rng = random.Random(11)
    print(f"\n{'filter':22} {'kept':>5} {'of':>4} {'filtered $':>11} "
          f"{'random mean':>12} {'sd':>8} {'p':>7}   verdict")
    print("-" * 92)
    for filt, raw, _what in PAIRS:
        fd, rd = daily(rows, filt), daily(rows, raw)
        if not rd or not fd:
            print(f"{filt:22} {'-':>5} {'-':>4}   (no trades on one side)")
            continue
        vals = list(rd.values())
        n = len(fd)
        if n >= len(vals):
            print(f"{filt:22} {n:>5} {len(vals):>4}   (filters nothing -- "
                  f"{sum(fd.values()):+,.0f} vs {sum(vals):+,.0f})")
            continue
        got = sum(fd.values())
        sims = [sum(rng.sample(vals, n)) for _ in range(N_SIM)]
        p = sum(1 for s in sims if s >= got) / N_SIM
        verdict = ("NOISE" if p > 0.10 else
                   "maybe" if p > 0.05 else "beats the null")
        print(f"{filt:22} {n:>5} {len(vals):>4} {got:>11,.0f} "
              f"{st.mean(sims):>12,.0f} {st.stdev(sims):>8,.0f} {p:>7.3f}   {verdict}")
        # THE INVERSION. A filter that is much WORSE than random removed days
        # that were much BETTER -- so trading only what it rejects is the
        # obvious question. Reported, with two reasons not to act on it below.
        dropped = sum(v for d, v in rd.items() if d not in fd)
        n_drop = len(vals) - n
        if n_drop >= 3 and p > 0.80:
            isims = [sum(rng.sample(vals, n_drop)) for _ in range(N_SIM)]
            ip = sum(1 for s in isims if s >= dropped) / N_SIM
            print(f"{'  ^ INVERTED (trade only what it rejects)':22} "
                  f"{n_drop:>5} {len(vals):>4} {dropped:>11,.0f} "
                  f"{st.mean(isims):>12,.0f} {st.stdev(isims):>8,.0f} {ip:>7.3f}")
    print("\np = fraction of random same-size day-culls that did AT LEAST as well.")
    print("A filter is only evidence of skill if p is small. Anything above ~0.10")
    print("is indistinguishable from removing days at random.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "ES")
