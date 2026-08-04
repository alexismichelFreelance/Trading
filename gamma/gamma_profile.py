"""Reconstruct the FULL dealer-gamma-by-strike profile from an archived CBOE
chain (gamma/raw_cboe/SPX_YYYYMMDD.json.gz), not just the 3 summary levels.

Mirrors tools/fetch_cboe_gex.py compute_levels EXACTLY, with one correction for
reprocessing: DTE is measured relative to the FILE's session date (parsed from
the name), not date.today(). Gives the gamma curve + all significant strikes
(local |net-gamma| peaks) that can act as S/R, plus the zero-gamma flip.

    python gamma/gamma_profile.py            # dump all archived days
    from gamma_profile import load_profile   # -> dict(strikes, net, cum, ...)
"""
from __future__ import annotations

import gzip
import json
import re
from datetime import datetime
from pathlib import Path

import numpy as np

RAW = Path(__file__).resolve().parent / "raw_cboe"
SYM_RE = re.compile(r"^[A-Z]+W?(\d{6})([CP])(\d{8})$")
MAX_DTE = 7
MULT = 100.0


def load_profile(path: Path) -> dict:
    sess = datetime.strptime(path.name.split("_")[1][:8], "%Y%m%d").date()
    payload = json.loads(gzip.decompress(path.read_bytes()))
    data = payload["data"]
    spot = float(data["close"])
    per = {}                                            # strike -> [call$, put$]
    n = 0
    for o in data.get("options", []):
        m = SYM_RE.match(o.get("option", ""))
        if not m:
            continue
        exp = datetime.strptime(m.group(1), "%y%m%d").date()
        dte = (exp - sess).days
        if dte < 0 or dte > MAX_DTE:
            continue
        gamma = float(o.get("gamma") or 0.0)
        oi = float(o.get("open_interest") or 0.0)
        if gamma == 0.0 or oi == 0.0:
            continue
        strike = int(m.group(3)) / 1000.0
        gex = gamma * oi * MULT * spot * spot * 0.01
        rec = per.setdefault(strike, [0.0, 0.0])
        if m.group(2) == "C":
            rec[0] += gex
        else:
            rec[1] -= gex                               # dealer short puts
        n += 1
    net = sorted((k, c + p) for k, (c, p) in per.items())
    ks = np.array([k for k, _ in net])
    ng = np.array([g for _, g in net])
    cw = ks[np.argmax([per[k][0] for k in ks])]         # call wall (max call gamma)
    pw = ks[np.argmin([per[k][1] for k in ks])]         # put wall (min = most neg put)
    cum = np.cumsum(ng)
    flip = None
    for i in range(1, len(cum)):
        if (cum[i - 1] <= 0 < cum[i]) or (cum[i - 1] >= 0 > cum[i]):
            if cum[i] != cum[i - 1]:
                z = ks[i - 1] + (0 - cum[i - 1]) / (cum[i] - cum[i - 1]) * (ks[i] - ks[i - 1])
                if flip is None or abs(z - spot) < abs(flip - spot):
                    flip = z
    # significant strikes: local peaks in |net gamma| within +-3% of spot
    near = (ks > spot * 0.97) & (ks < spot * 1.03)
    peaks = []
    for i in range(1, len(ks) - 1):
        if near[i] and abs(ng[i]) >= abs(ng[i - 1]) and abs(ng[i]) >= abs(ng[i + 1]) \
                and abs(ng[i]) > 0.15 * np.abs(ng[near]).max():
            peaks.append((ks[i], ng[i]))
    return dict(sess=sess.isoformat(), spot=spot, strikes=ks, net=ng, cum=cum,
                call_wall=float(cw), put_wall=float(pw), flip=flip,
                total=float(cum[-1]), n_opt=n, peaks=peaks)


def main() -> None:
    for f in sorted(RAW.glob("SPX_*.json.gz")):
        p = load_profile(f)
        sig = " ".join(f"{k:.0f}{'+' if g>0 else '-'}" for k, g in
                       sorted(p["peaks"], key=lambda x: -abs(x[1]))[:6])
        print(f"{p['sess']}  spot={p['spot']:.0f}  flip={p['flip'] and round(p['flip'],1)}  "
              f"callwall={p['call_wall']:.0f}  putwall={p['put_wall']:.0f}  "
              f"net={'LONG' if p['total']>0 else 'SHORT'}  ({p['n_opt']} opt)")
        print(f"    significant strikes (|gamma| peaks near spot): {sig}")


if __name__ == "__main__":
    main()
