#!/usr/bin/env python3
"""
build_gex.py  --  OptionsDX SPX EOD chains  ->  daily gamma-exposure (GEX) levels.

WHAT IT DOES
  1. Reads OptionsDX SPX "End of Day" monthly CSVs from ./raw/*.csv
  2. For each quote date, builds a NET DEALER GAMMA profile across strikes.
  3. Extracts the three levels that matter intraday:
        - zero_gamma  : strike where cumulative net gamma flips sign (the "gamma flip").
                        Above it dealers are net-long gamma (price mean-reverts / pins);
                        below it dealers are net-short gamma (moves accelerate / trend).
        - call_wall   : strike with the largest positive (call) gamma  -> resistance / pin-up cap.
        - put_wall    : strike with the largest negative (put)  gamma  -> support  / pin-down floor.
        - regime      : sign of total net gamma (long vs short gamma day).
  4. Writes ./gex_levels.csv  (one row per session date) for ingest into QuestDB.

IMPORTANT CAVEAT
  OptionsDX free chains carry VOLUME, not open interest. We weight gamma by volume
  as an OI proxy. This is reasonable for near-dated / 0DTE SPX (where intraday gamma
  concentrates and there is little overnight OI), but it is a PROXY. If you later get a
  source with true OI, set OI_FIELD = 'C_OI'/'P_OI' below and it just works.

USAGE
  put the monthly CSVs in  D:\\Trading\\gamma\\raw\\
  python build_gex.py
"""

import os, glob, csv, math
from collections import defaultdict

HERE      = os.path.dirname(os.path.abspath(__file__))
RAW_DIR   = os.path.join(HERE, "raw")
OUT_CSV   = os.path.join(HERE, "gex_levels.csv")

# ---- configuration -------------------------------------------------------
MAX_DTE       = 7.0     # only count expiries within this many days (intraday gamma lives here).
                        #   set to 9e9 to use ALL expiries.
SPOT_SCALE    = True    # multiply per-strike gamma by spot^2 (dollar-gamma). Levels are
                        #   scale-invariant, but this makes the profile economically correct.
CONTRACT_MULT = 100     # SPX multiplier
# weight field: OptionsDX free has no OI, so use volume. Swap to an OI field if you get one.
CALL_W_FIELD  = "C_VOLUME"
PUT_W_FIELD   = "P_VOLUME"
# --------------------------------------------------------------------------


def clean_key(k):
    """OptionsDX wraps headers in [BRACKETS] with stray spaces. Normalise to BARE_NAME."""
    return k.strip().strip("[]").strip().upper()


def to_f(x):
    try:
        return float(str(x).strip().strip("[]").strip())
    except Exception:
        return None


def read_rows(path):
    with open(path, "r", newline="") as f:
        rdr = csv.reader(f)
        header = None
        for row in rdr:
            if not row:
                continue
            if header is None:
                header = [clean_key(c) for c in row]
                continue
            yield dict(zip(header, row))


def build():
    files = sorted(glob.glob(os.path.join(RAW_DIR, "*.csv")))
    if not files:
        print(f"No CSVs found in {RAW_DIR}. Download SPX EOD chains there first.")
        return

    # per (date) -> per strike -> [call_gamma_$, put_gamma_$]
    day_strike = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    day_spot   = {}

    seen_fields = set()
    nrows = 0
    for path in files:
        print("reading", os.path.basename(path))
        for r in read_rows(path):
            if not seen_fields:
                seen_fields = set(r.keys())
            nrows += 1
            qdate = (r.get("QUOTE_DATE") or "").strip().strip("[]").strip()
            if not qdate:
                continue
            dte   = to_f(r.get("DTE"))
            if dte is None or dte > MAX_DTE or dte < 0:
                continue
            spot  = to_f(r.get("UNDERLYING_LAST"))
            strike= to_f(r.get("STRIKE"))
            if spot is None or strike is None:
                continue
            day_spot[qdate] = spot
            cg = to_f(r.get("C_GAMMA")); pg = to_f(r.get("P_GAMMA"))
            cw = to_f(r.get(CALL_W_FIELD)) or 0.0
            pw = to_f(r.get(PUT_W_FIELD)) or 0.0
            s2 = (spot * spot) if SPOT_SCALE else 1.0
            # dealer convention (standard naive GEX): long call gamma (+), short put gamma (-)
            if cg is not None:
                day_strike[qdate][strike][0] += cg * cw * CONTRACT_MULT * s2 * 0.01
            if pg is not None:
                day_strike[qdate][strike][1] -= pg * pw * CONTRACT_MULT * s2 * 0.01
        # end file
    print(f"parsed {nrows} option rows across {len(day_strike)} dates")
    if seen_fields:
        missing = [f for f in ("QUOTE_DATE","DTE","UNDERLYING_LAST","STRIKE","C_GAMMA","P_GAMMA",CALL_W_FIELD,PUT_W_FIELD) if f not in seen_fields]
        if missing:
            print("  WARNING missing expected fields:", missing, "\n  header was:", sorted(seen_fields))

    rows_out = []
    for qdate in sorted(day_strike):
        strikes = sorted(day_strike[qdate])
        if len(strikes) < 5:
            continue
        spot = day_spot[qdate]
        net = []            # (strike, call_g, put_g, net_g)
        for k in strikes:
            cgs, pgs = day_strike[qdate][k]
            net.append((k, cgs, pgs, cgs + pgs))
        total_net = sum(n[3] for n in net)
        # call wall / put wall = extreme single-strike gamma
        call_wall = max(net, key=lambda n: n[1])[0]
        put_wall  = min(net, key=lambda n: n[2])[0]   # most negative put gamma
        # zero-gamma flip: cumulative net gamma from low strike up; find sign change nearest spot
        cum = 0.0; flips = []
        prev_k = None; prev_cum = None
        for (k, cg, pg, ng) in net:
            cum_prev = cum
            cum += ng
            if prev_cum is not None and ((cum_prev <= 0 < cum) or (cum_prev >= 0 > cum)):
                # linear interp strike of the zero crossing
                if cum != cum_prev:
                    frac = (0 - cum_prev) / (cum - cum_prev)
                    flips.append(prev_k + frac * (k - prev_k))
            prev_k, prev_cum = k, cum_prev
        # choose flip nearest spot (the intraday-relevant one)
        zero_gamma = min(flips, key=lambda z: abs(z - spot)) if flips else ""
        rows_out.append({
            "sess_date": qdate,
            "spot": round(spot, 2),
            "zero_gamma": round(zero_gamma, 2) if zero_gamma != "" else "",
            "call_wall": round(call_wall, 2),
            "put_wall": round(put_wall, 2),
            "net_gamma_sign": 1 if total_net > 0 else -1,   # +1 long-gamma day, -1 short-gamma day
            "total_net_gamma": round(total_net, 1),
        })

    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["sess_date","spot","zero_gamma","call_wall","put_wall","net_gamma_sign","total_net_gamma"])
        w.writeheader()
        for r in rows_out:
            w.writerow(r)
    print(f"\nwrote {len(rows_out)} daily GEX rows -> {OUT_CSV}")
    for r in rows_out[:8]:
        print("  ", r)


if __name__ == "__main__":
    build()
