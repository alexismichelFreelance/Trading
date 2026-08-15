"""Measure the index->future basis, then test the gamma curve against the tape.

ORDER MATTERS. Every gamma level is an INDEX strike (SPX/NDX) and every sleeve
trades a FUTURE, so the basis is what puts a level on the chart. It is currently
a single reading -- ES 42.0, NQ 181.0, both taken at 16:00 ET on 2026-07-20 --
and if it is wrong or drifting then a level test measures the basis error, not
the level. So: basis first, then levels, using the per-session basis rather than
a constant.

There is also an ambiguity worth resolving rather than assuming: the CBOE
payload is fetched in the MORNING, so `data["close"]` is probably the PRIOR
session's index close, not the same day's. Both alignments are computed and the
one with the tighter dispersion is the one that is true -- an empirical answer
instead of a guess that would bias every level by a day's move.

Then, causally: the curve from the PRIOR session, evaluated at TODAY'S OPEN, is
known before the session starts. Questions:

  1. does the local regime separate session CHARACTER (range, efficiency)?
  2. does the flip act as a level -- do highs/lows cluster at it?
  3. do the walls?
  4. does local_sign say anything the existing gexp gate does not?

n is small: 44 payloads, fewer once matched to recorded futures sessions. Large
effects are visible, subtle ones are not, and anything found here is a
hypothesis for forward testing, not a result.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB              # noqa: E402
from engine.features.gamma_profile import gamma_profile  # noqa: E402

ET = "America/New_York"
PAIRS = {"SPX": ("ES", 50.0), "NDX": ("NQ", 20.0)}


def rth(q: QuestDB, sym: str) -> pd.DataFrame:
    b = q.df(f"SELECT ts,o,h,l,c,vol FROM claude_bars_live WHERE symbol='{sym}' "
             f"ORDER BY ts")
    et = pd.to_datetime(b["ts"], utc=True).dt.tz_convert(ET)
    m = et.dt.hour * 60 + et.dt.minute
    b = b[(m >= 570) & (m < 960)].copy()
    b["day"] = et[(m >= 570) & (m < 960)].dt.strftime("%Y-%m-%d")
    return b


def sessions(b: pd.DataFrame) -> pd.DataFrame:
    g = b.groupby("day")
    out = g.agg(open=("o", "first"), high=("h", "max"), low=("l", "min"),
                close=("c", "last"), n=("c", "size")).reset_index()
    trav = g["c"].apply(lambda s: float(np.abs(np.diff(s.to_numpy())).sum()))
    out["travel"] = trav.values
    out = out[out["n"] >= 300]
    out["range"] = out["high"] - out["low"]
    out["net"] = out["close"] - out["open"]
    out["eff"] = out["net"].abs() / out["travel"].replace(0, np.nan)
    return out


def curves(q: QuestDB) -> dict:
    d = q.df("SELECT ts,underlying,strike,call_gex,put_gex,spot "
             "FROM claude_gex_strikes ORDER BY ts")
    d["day"] = d["ts"].dt.strftime("%Y-%m-%d")
    out = {}
    for (day, u), g in d.groupby(["day", "underlying"]):
        agg: dict = {}
        for r in g.itertuples():
            c, p = agg.get(r.strike, (0.0, 0.0))
            agg[r.strike] = (c + float(r.call_gex), p + float(r.put_gex))
        out[(day, u)] = (agg, float(g["spot"].iloc[0]))
    return out


def main() -> None:
    q = QuestDB(timeout=240)
    cv = curves(q)
    for und, (fut, _pv) in PAIRS.items():
        ses = sessions(rth(q, fut)).set_index("day")
        days = sorted({d for d, u in cv if u == und})
        print(f"\n{'=' * 72}\n{und} -> {fut}   {len(days)} curve sessions, "
              f"{len(ses)} recorded {fut} sessions\n{'=' * 72}")

        # ── 1. BASIS, and WHICH close the payload carries ────────────────
        same, prior = [], []
        for d in days:
            _agg, spot = cv[(d, und)]
            if d in ses.index:
                same.append((d, ses.loc[d, "close"] - spot))
            prev = [x for x in ses.index if x < d]
            if prev:
                prior.append((d, ses.loc[prev[-1], "close"] - spot))
        for name, rows in (("same-day close", same), ("PRIOR-day close", prior)):
            if len(rows) < 3:
                continue
            v = np.array([x for _, x in rows])
            print(f"  basis vs {name:16}  n={len(v):>3}  median {np.median(v):>8.1f}"
                  f"  std {v.std():>6.1f}  min {v.min():>8.1f}  max {v.max():>8.1f}")
        use = prior if len(prior) >= len(same) and prior else same
        bmap = dict(use)
        if not bmap:
            print("  no overlap; skipping level tests")
            continue
        bmed = float(np.median(list(bmap.values())))
        print(f"  -> using per-session basis (median {bmed:+.1f})")

        # ── 2. REGIME AT THE OPEN vs what the session then did ───────────
        rows = []
        for d in sorted(ses.index):
            prev = [x for x in days if x < d]
            if not prev:
                continue
            agg, spot = cv[(prev[-1], und)]
            basis = bmap.get(prev[-1], bmed)
            o = float(ses.loc[d, "open"])
            pr = gamma_profile(agg, o - basis)          # evaluated AT THE OPEN
            if pr is None:
                continue
            flip = (pr["flip"] + basis) if pr["flip"] is not None else np.nan
            rows.append(dict(day=d, sign=pr["local_sign"], total=pr["total_sign"],
                             flip=flip, dist=(flip - o) if flip == flip else np.nan,
                             cw=pr["call_wall"] + basis, pw=pr["put_wall"] + basis,
                             rng=float(ses.loc[d, "range"]),
                             eff=float(ses.loc[d, "eff"]),
                             net=float(ses.loc[d, "net"]),
                             high=float(ses.loc[d, "high"]),
                             low=float(ses.loc[d, "low"]), open=o))
        r = pd.DataFrame(rows)
        if len(r) < 6:
            print(f"  only {len(r)} usable sessions; not enough to say anything")
            continue
        print(f"\n  SESSION CHARACTER BY LOCAL REGIME AT THE OPEN   (n={len(r)})")
        print(f"    {'regime':8} {'n':>3} {'med range':>10} {'med eff':>8} "
              f"{'med |net|':>10}")
        for s, g in r.groupby("sign"):
            print(f"    {'LONG' if s > 0 else 'SHORT':8} {len(g):>3} "
                  f"{g['rng'].median():>10.2f} {g['eff'].median():>8.3f} "
                  f"{g['net'].abs().median():>10.2f}")
        if r["sign"].nunique() > 1:
            a = r[r["sign"] > 0]["rng"]
            b = r[r["sign"] < 0]["rng"]
            print(f"    short-gamma range / long-gamma range = "
                  f"{b.median() / a.median():.2f}x  "
                  f"(the mechanism predicts > 1: short gamma amplifies)")

        # ── 3. IS THE FLIP A LEVEL? ──────────────────────────────────────
        f = r[r["flip"] == r["flip"]].copy()
        if len(f) >= 6:
            f["hi_to_flip"] = (f["high"] - f["flip"]).abs()
            f["lo_to_flip"] = (f["low"] - f["flip"]).abs()
            f["nearest"] = f[["hi_to_flip", "lo_to_flip"]].min(axis=1)
            # a session extreme landing ON the level is only meaningful against
            # how big the session was: normalise by the range
            f["frac"] = f["nearest"] / f["rng"].replace(0, np.nan)
            touched = f[(f["low"] <= f["flip"]) & (f["high"] >= f["flip"])]
            print(f"\n  FLIP AS A LEVEL   (n={len(f)})")
            print(f"    session extreme nearest the flip: median {f['nearest'].median():.2f} pts"
                  f"  ({100 * f['frac'].median():.0f}% of the session range)")
            print(f"    sessions whose range CONTAINS the flip: {len(touched)}/{len(f)}")
            print(f"    ...of those, closed on the far side: "
                  f"{int(((touched['close'] if 'close' in touched else touched['open'] + touched['net']) > touched['flip']).sum())}"
                  f"/{len(touched)}")
            # null: how close would a RANDOM level inside the day be?
            rng_ = np.random.default_rng(7)
            null = []
            for _ in range(400):
                lvl = f["open"] + rng_.normal(0, f["rng"].median(), len(f))
                null.append(float(np.minimum((f["high"] - lvl).abs(),
                                             (f["low"] - lvl).abs()).median()))
            print(f"    null (level scattered ~1 range from the open): "
                  f"median {np.median(null):.2f} pts")

        # ── 4. WALLS ─────────────────────────────────────────────────────
        w = r.copy()
        w["hi_to_cw"] = (w["high"] - w["cw"]).abs()
        w["lo_to_pw"] = (w["low"] - w["pw"]).abs()
        print(f"\n  WALLS   high vs call wall: median {w['hi_to_cw'].median():.2f} pts"
              f"   |   low vs put wall: median {w['lo_to_pw'].median():.2f} pts")
        print(f"    (session range for scale: median {w['rng'].median():.2f})")


if __name__ == "__main__":
    main()
