"""Forward-only GEX LEVELS collector — CBOE free delayed-quotes JSON -> daily
per-strike dealer-gamma profile with TRUE open interest -> zero-gamma flip /
call wall / put wall -> QuestDB `claude_gex_levels` (+ raw JSON archived).

Underlyings: SPX (-> ES levels) and NDX (-> NQ levels). One row per session per
underlying; rows written before the NDX extension have underlying NULL and are
treated as SPX by the reader (engine/features/gamma_levels.py).

Historical strike-level data for Feb-May 2025 is NOT freely available
(OptionsDX ends 2023, DoltHub lacks OI, CBOE per-strike is paid, Wayback has
~7 snapshots ever). This collector builds the library FORWARD from today —
the same philosophy as RecorderTee: collect now, never re-buy.

Run once per day after the close (or any time; data is ~15-min delayed):
    .venv/Scripts/python.exe tools/fetch_cboe_gex.py
    .venv/Scripts/python.exe tools/fetch_cboe_gex.py --dry-run   # no DB writes

Math mirrors gamma/build_gex.py (dealer convention: call gamma +, put gamma -,
spot^2 dollar scaling, near-dated expiries only) but weights by TRUE OI.
"""
from __future__ import annotations

import gzip
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB   # noqa: E402

# underlying -> CBOE delayed-quotes endpoint (weeklies SPXW/NDXP appear in the
# same chain and match the option-symbol regex below)
UNDERLYINGS = {
    "SPX": "https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json",
    "NDX": "https://cdn.cboe.com/api/global/delayed_quotes/options/_NDX.json",
}
RAW_DIR = ROOT.parent / "gamma" / "raw_cboe"
TABLE = "claude_gex_levels"
MAX_DTE = 7          # intraday-relevant gamma lives in near-dated expiries
MULT = 100.0
# option symbol: ROOT + yymmdd + C/P + strike*1000 (SPX/SPXW, NDX/NDXP)
SYM_RE = re.compile(r"^[A-Z]+W?(\d{6})([CP])(\d{8})$")


def fetch(url: str) -> dict:
    r = httpx.get(url, timeout=60, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    r.raise_for_status()
    return r.json()


def compute_levels(payload: dict) -> dict | None:
    data = payload["data"]
    spot = float(data["close"])
    today = date.today()
    per_strike = defaultdict(lambda: [0.0, 0.0])       # strike -> [call$, put$]
    n_used = 0
    for o in data.get("options", []):
        m = SYM_RE.match(o.get("option", ""))
        if not m:
            continue
        exp = datetime.strptime(m.group(1), "%y%m%d").date()
        dte = (exp - today).days
        if dte < 0 or dte > MAX_DTE:
            continue
        gamma = float(o.get("gamma") or 0.0)
        oi = float(o.get("open_interest") or 0.0)
        if gamma == 0.0 or oi == 0.0:
            continue
        strike = int(m.group(3)) / 1000.0
        gex = gamma * oi * MULT * spot * spot * 0.01
        if m.group(2) == "C":
            per_strike[strike][0] += gex
        else:
            per_strike[strike][1] -= gex               # dealer short puts
        n_used += 1
    if len(per_strike) < 5:
        return None
    net = sorted((k, c, p, c + p) for k, (c, p) in per_strike.items())
    total = sum(x[3] for x in net)
    call_wall = max(net, key=lambda x: x[1])[0]
    put_wall = min(net, key=lambda x: x[2])[0]
    cum, flips, prev_k, prev_cum = 0.0, [], None, None
    for k, _c, _p, ng in net:
        cum_prev = cum
        cum += ng
        if prev_cum is not None and ((cum_prev <= 0 < cum) or (cum_prev >= 0 > cum)) and cum != cum_prev:
            flips.append(prev_k + (0 - cum_prev) / (cum - cum_prev) * (k - prev_k))
        prev_k, prev_cum = k, cum_prev
    zero_gamma = min(flips, key=lambda z: abs(z - spot)) if flips else None
    return dict(sess=today.isoformat(), spot=spot, zero_gamma=zero_gamma,
                call_wall=call_wall, put_wall=put_wall,
                net_sign=1 if total > 0 else -1, total_gex=total, n_options=n_used)


def ensure_schema(q: QuestDB) -> None:
    q.query(f"CREATE TABLE IF NOT EXISTS {TABLE} (ts TIMESTAMP, spot DOUBLE, "
            f"zero_gamma DOUBLE, call_wall DOUBLE, put_wall DOUBLE, "
            f"net_sign INT, total_gex DOUBLE, n_options LONG) "
            f"TIMESTAMP(ts) PARTITION BY YEAR")
    try:                                   # migration: pre-NDX rows lack the column
        q.query(f"ALTER TABLE {TABLE} ADD COLUMN underlying SYMBOL")
    except Exception:                      # noqa: BLE001 - already exists
        pass


def store(q: QuestDB, u: str, lv: dict) -> bool:
    """Insert one (session, underlying) row unless already present."""
    dup = (f"SELECT count() n FROM {TABLE} WHERE ts = '{lv['sess']}T00:00:00.000000Z' "
           f"AND (underlying = '{u}'" + (" OR underlying IS NULL" if u == "SPX" else "") + ")")
    if int(q.df(dup)["n"].iloc[0]) > 0:
        return False
    zg = "null" if lv["zero_gamma"] is None else f"{lv['zero_gamma']:.2f}"
    q.query(f"INSERT INTO {TABLE} (ts, spot, zero_gamma, call_wall, put_wall, "
            f"net_sign, total_gex, n_options, underlying) VALUES "
            f"('{lv['sess']}T00:00:00.000000Z', {lv['spot']}, {zg}, "
            f"{lv['call_wall']}, {lv['put_wall']}, {lv['net_sign']}, "
            f"{lv['total_gex']:.1f}, {lv['n_options']}, '{u}')")
    return True


def main() -> None:
    dry = "--dry-run" in sys.argv
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    stamp = date.today().strftime("%Y%m%d")
    q = None
    if not dry:
        q = QuestDB()
        ensure_schema(q)
    for u, url in UNDERLYINGS.items():
        try:
            payload = fetch(url)
        except Exception as ex:                        # noqa: BLE001
            print(f"{u}: fetch FAILED ({ex}) — other underlyings continue")
            continue
        raw_path = RAW_DIR / f"{u}_{stamp}.json.gz"
        raw_path.write_bytes(gzip.compress(json.dumps(payload).encode()))
        lv = compute_levels(payload)
        if lv is None:
            print(f"{u}: not enough near-dated strikes; raw archived only")
            continue
        wrote = store(q, u, lv) if q is not None else False
        print(f"{u} {lv['sess']}  spot={lv['spot']:.0f}  "
              f"flip={lv['zero_gamma'] and round(lv['zero_gamma'], 1)}  "
              f"call_wall={lv['call_wall']:.0f}  put_wall={lv['put_wall']:.0f}  "
              f"net={'LONG' if lv['net_sign'] > 0 else 'SHORT'}-gamma  "
              f"({lv['n_options']} options, raw -> {raw_path.name}"
              f"{', DB row written' if wrote else ', dry/dup — no DB write'})")


if __name__ == "__main__":
    main()
