"""Ignition parity ORACLE — the research per-signal evaluation.

This reproduces the research's +1004 (engine_reference/hmm_regime_reference.md
Section C): each validated ignition is evaluated INDEPENDENTLY for its forward
P&L (overlapping allowed; trade count == candidate count), the forward walk
capped at the session end. It validates that the engine's feature/regime/exit
LOGIC is faithful to the research.

This is distinct from the live `IgnitionStrategy`, which applies the SAME
decision logic in a realistic SINGLE-POSITION portfolio (one position at a time,
event-driven fills) and therefore reports a lower, realistic number. The oracle
is the parity gate; the strategy is the deployable engine.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd

from ..adapters.questdb import QuestDB
from ..core.events import Bar
from ..core.timeutil import et_session_date, utc_hour
from ..features.pivots import daily_pivots
from ..features.zones import ZoneBook, ZoneDetector

SEC_TABLE = {"ESM5": "claude_sec_feat", "ESH5": "claude_sec_feat_esh5"}
COST = 0.517
MINT = 2.0
HORIZON_ROWS = 600
BOOK_MIN_HOLD_ROWS = 8
ROUND = 50.0
NS = 1_000_000_000


def _zones(q: QuestDB, sym: str):
    df = q.df(f"SELECT ts, first(o) o, max(h) h, min(l) l, last(c) c, sum(vol) v "
              f"FROM claude_bars_1m WHERE symbol='{sym}' SAMPLE BY 30m ALIGN TO CALENDAR").dropna(subset=["c"])
    det, book = ZoneDetector(), ZoneBook()
    for r in df.itertuples():
        bar = Bar(int(r.ts.value) + 30 * 60 * NS, "30m", r.o, r.h, r.l, r.c, int(r.v))
        z = det.update(bar)
        if z is not None:
            book.add(z)
        book.on_bar(bar)
    return book.zones


def _day_pivots(q: QuestDB, sym: str) -> dict[str, list[float]]:
    df = q.df(f"SELECT ts, h, l, c FROM claude_bars_1m WHERE symbol='{sym}'")
    et = df["ts"].dt.tz_convert("America/New_York")
    df["etmin"] = et.dt.hour * 60 + et.dt.minute
    df["d"] = et.dt.strftime("%Y-%m-%d")
    rth = df[(df.etmin >= 570) & (df.etmin < 960)].sort_values("ts")
    agg = rth.groupby("d").agg(h=("h", "max"), l=("l", "min"), c=("c", "last")).reset_index()
    piv, prev = {}, None
    for row in agg.itertuples():
        piv[row.d] = list(daily_pivots(*prev).values()) if prev else []
        prev = (row.h, row.l, row.c)
    return piv


def _nearest_level(price, direction, pivots):
    cands = [x for x in pivots if (x >= price + MINT if direction > 0 else x <= price - MINT)]
    rnd = (np.ceil((price + MINT) / ROUND) * ROUND if direction > 0
           else np.floor((price - MINT) / ROUND) * ROUND)
    cands.append(float(rnd))
    return min(cands) if direction > 0 else max(cands)


def _opp_pivot(price, direction, pivots):
    side = [x for x in pivots if (x >= price + MINT if -direction > 0 else x <= price - MINT)]
    return (min(side) if -direction > 0 else max(side)) if side else None


def evaluate_ignition(sym: str, states: dict, q: QuestDB | None = None) -> dict[str, tuple[int, float]]:
    """Return {month: (n_trades, net_points)} for the per-signal research eval."""
    q = q or QuestDB()
    d = q.df(f"SELECT ts, pxc, adelta, avol, str, bid_cancel, ask_cancel, bid_add, ask_add "
             f"FROM {SEC_TABLE[sym]} ORDER BY ts")
    ts = d["ts"].astype("int64").to_numpy()
    px = d["pxc"].ffill().to_numpy()
    ad = d["adelta"].to_numpy()
    av, st = d["avol"].to_numpy(), d["str"].to_numpy()
    bc, ac = d["bid_cancel"].to_numpy(), d["ask_cancel"].to_numpy()
    ba, aa = d["bid_add"].to_numpy(), d["ask_add"].to_numpy()
    n = len(d)
    netAsk = pd.Series(aa - ac).rolling(10, min_periods=1).sum().to_numpy()
    netBid = pd.Series(ba - bc).rolling(10, min_periods=1).sum().to_numpy()
    etday = d["ts"].dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d").to_numpy()
    ends = np.append(np.where(np.diff(pd.factorize(etday)[0]) != 0)[0], n - 1)
    day_end = ends[np.searchsorted(ends, np.arange(n))]

    s_all = np.sign(ad)
    book_ok = ((ad > 0) & (ac > bc)) | ((ad < 0) & (bc > ac))
    prior = np.concatenate([np.full(300, np.nan), px[:-300]])
    aligned = np.sign(px - prior) == s_all
    cand = np.where((st >= 5) & (av >= 800) & book_ok & aligned & (ad != 0))[0]

    zones = _zones(q, sym)
    piv = _day_pivots(q, sym)
    out: dict[str, list] = defaultdict(lambda: [0, 0.0])

    for i0 in cand:
        s = int(s_all[i0]); px0 = float(px[i0]); te = int(ts[i0])
        t60 = states.get(utc_hour(te), 0) == 1
        pv = piv.get(et_session_date(te), [])
        zt = None
        for z in zones:
            if z.direction != -s or z.formed_ts > te or (z.broken_ts is not None and z.broken_ts <= te):
                continue
            if s > 0 and z.bot > px0 + MINT:
                zt = z.bot if zt is None else min(zt, z.bot)
            elif s < 0 and z.top < px0 - MINT:
                zt = z.top if zt is None else max(zt, z.top)
        tgt = zt if zt is not None else _nearest_level(px0, s, pv)
        lstop = _opp_pivot(px0, s, pv)

        end = min(n - 1, i0 + HORIZON_ROWS, int(day_end[i0]))
        peak = 0.0
        bookIdx = fa4 = faCap = znIdx = -1
        znPx = None
        for k in range(i0 + 1, end + 1):
            fe = s * (px[k] - px0)
            peak = max(peak, fe)
            if bookIdx < 0 and peak >= 2 and (k - i0) >= BOOK_MIN_HOLD_ROWS:
                lead = netAsk[k] if s > 0 else netBid[k]
                if lead > 200 and fe <= 0.8 * peak:
                    bookIdx = k
            if fa4 < 0 and fe <= -4:
                fa4 = k
            if faCap < 0 and fe <= -12:
                faCap = k
            if znIdx < 0:
                if tgt is not None and (px[k] >= tgt if s > 0 else px[k] <= tgt):
                    znIdx, znPx = k, tgt
                elif lstop is not None and (px[k] <= lstop if s > 0 else px[k] >= lstop):
                    znIdx, znPx = k, lstop
        hz = s * (px[end] - px0) - COST
        if t60:
            pnl = (s * (znPx - px0) - COST if (znIdx >= 0 and (faCap < 0 or znIdx <= faCap))
                   else (-12 - COST if faCap >= 0 else hz))
        else:
            pnl = (s * (px[bookIdx] - px0) - COST if (bookIdx >= 0 and (fa4 < 0 or bookIdx <= fa4))
                   else (-4 - COST if fa4 >= 0 else hz))
        m = pd.Timestamp(te, unit="ns", tz="UTC").strftime("%Y-%m")
        out[m][0] += 1
        out[m][1] += pnl
    return {m: (v[0], v[1]) for m, v in sorted(out.items())}


__all__ = ["evaluate_ignition", "SEC_TABLE"]
