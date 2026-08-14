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

from engine.adapters.questdb import QuestDB
from engine.features.gamma_profile import gamma_profile   # noqa: E402

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


def dte_bucket(dte: int) -> str:
    """0DTE is the one that matters most and is the one open interest cannot
    see: OI is published END OF DAY, so today's 0DTE positioning is invisible
    here no matter how the curve is cut. Bucketing it separately at least makes
    the blind spot measurable instead of blended away."""
    if dte <= 0:
        return "0dte"
    if dte <= 7:
        return "1-7"
    if dte <= 30:
        return "8-30"
    return "31+"


def compute_levels(payload: dict, asof: date | None = None) -> dict | None:
    """The full per-strike book plus everything read off it.

    Was: build per_strike, find every zero crossing into `flips`, then return
    one flip, one call wall, one put wall and the book's total sign -- throwing
    the curve, the other crossings and the ranked walls away. See
    engine/features/gamma_profile.py for what that cost on 2026-08-13.
    """
    data = payload["data"]
    spot = float(data["close"])
    # AS OF the session the payload belongs to, never date.today():
    # rebuilding an archived raw would otherwise measure every DTE
    # from the wrong day and drop the whole chain through MAX_DTE.
    today = asof or date.today()
    per_strike = defaultdict(lambda: [0.0, 0.0])       # strike -> [call$, put$]
    by_bucket = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
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
        side = 0 if m.group(2) == "C" else 1
        signed = gex if side == 0 else -gex             # dealer short puts
        per_strike[strike][side] += signed
        by_bucket[dte_bucket(dte)][strike][side] += signed
        n_used += 1
    if len(per_strike) < 5:
        return None
    prof = gamma_profile({k: tuple(v) for k, v in per_strike.items()}, spot)
    if prof is None:
        return None
    return dict(sess=today.isoformat(), n_options=n_used, prof=prof,
                per_strike={k: tuple(v) for k, v in per_strike.items()},
                by_bucket={b: {k: tuple(v) for k, v in d.items()}
                           for b, d in by_bucket.items()},
                # legacy flat fields, unchanged in meaning so old readers work
                spot=spot, zero_gamma=prof["flip"], call_wall=prof["call_wall"],
                put_wall=prof["put_wall"], net_sign=prof["total_sign"],
                total_gex=prof["total_gex"])


STRIKES = "claude_gex_strikes"


def ensure_schema(q: QuestDB) -> None:
    q.query(f"CREATE TABLE IF NOT EXISTS {TABLE} (ts TIMESTAMP, spot DOUBLE, "
            f"zero_gamma DOUBLE, call_wall DOUBLE, put_wall DOUBLE, "
            f"net_sign INT, total_gex DOUBLE, n_options LONG) "
            f"TIMESTAMP(ts) PARTITION BY YEAR")
    # Migrations, each independently optional so a half-migrated table still
    # comes up. local_sign is the one that matters: net_sign is the sign of the
    # WHOLE book, local_sign is the sign where price actually is, and on
    # 2026-08-13 they disagreed for the entire session.
    for ddl in (f"ALTER TABLE {TABLE} ADD COLUMN underlying SYMBOL",
                f"ALTER TABLE {TABLE} ADD COLUMN local_sign INT",
                f"ALTER TABLE {TABLE} ADD COLUMN pocket_lo DOUBLE",
                f"ALTER TABLE {TABLE} ADD COLUMN pocket_hi DOUBLE",
                f"ALTER TABLE {TABLE} ADD COLUMN n_flips INT"):
        try:
            q.query(ddl)
        except Exception:                  # noqa: BLE001 - already exists
            pass
    # THE CURVE. Previously computed in full and discarded; without it no
    # question about walls beyond the first, pockets, or 0DTE concentration can
    # be asked at all, and no past day can be re-examined.
    q.query(f"CREATE TABLE IF NOT EXISTS {STRIKES} (ts TIMESTAMP, "
            f"underlying SYMBOL, bucket SYMBOL, strike DOUBLE, "
            f"call_gex DOUBLE, put_gex DOUBLE, net_gex DOUBLE, spot DOUBLE) "
            f"TIMESTAMP(ts) PARTITION BY MONTH")


def store_strikes(q: QuestDB, u: str, lv: dict) -> int:
    """One row per (session, underlying, DTE bucket, strike)."""
    ts = f"{lv['sess']}T00:00:00.000000Z"
    n = int(q.df(f"SELECT count() n FROM {STRIKES} WHERE ts = '{ts}' "
                 f"AND underlying = '{u}'")["n"].iloc[0])
    if n:
        return 0
    rows = []
    for bucket, book in lv["by_bucket"].items():
        for k, (c, pv) in sorted(book.items()):
            rows.append(f"('{ts}','{u}','{bucket}',{k},{c:.1f},{pv:.1f},"
                        f"{c + pv:.1f},{lv['spot']})")
    # 500-row statements disconnected QuestDB mid-rebuild (RemoteProtocolError)
    # while the live engine was writing to the same instance. Smaller batches
    # with a retry: a backfill that dies half way leaves a session partially
    # written, which the dup-check above would then treat as done.
    import time
    for i in range(0, len(rows), 100):
        chunk = ",".join(rows[i:i + 100])
        for attempt in range(4):
            try:
                q.query(f"INSERT INTO {STRIKES} (ts, underlying, bucket, "
                        f"strike, call_gex, put_gex, net_gex, spot) VALUES "
                        + chunk)
                break
            except Exception:                          # noqa: BLE001
                if attempt == 3:
                    raise
                time.sleep(1.0 + attempt)
    return len(rows)


def store(q: QuestDB, u: str, lv: dict) -> bool:
    """Insert one (session, underlying) row unless already present."""
    dup = (f"SELECT count() n FROM {TABLE} WHERE ts = '{lv['sess']}T00:00:00.000000Z' "
           f"AND (underlying = '{u}'" + (" OR underlying IS NULL" if u == "SPX" else "") + ")")
    if int(q.df(dup)["n"].iloc[0]) > 0:
        return False
    pr = lv["prof"]
    zg = "null" if lv["zero_gamma"] is None else f"{lv['zero_gamma']:.2f}"
    lo = "null" if pr["pocket"][0] is None else f"{pr['pocket'][0]:.2f}"
    hi = "null" if pr["pocket"][1] is None else f"{pr['pocket'][1]:.2f}"
    q.query(f"INSERT INTO {TABLE} (ts, spot, zero_gamma, call_wall, put_wall, "
            f"net_sign, total_gex, n_options, underlying, local_sign, "
            f"pocket_lo, pocket_hi, n_flips) VALUES "
            f"('{lv['sess']}T00:00:00.000000Z', {lv['spot']}, {zg}, "
            f"{lv['call_wall']}, {lv['put_wall']}, {lv['net_sign']}, "
            f"{lv['total_gex']:.1f}, {lv['n_options']}, '{u}', "
            f"{pr['local_sign']}, {lo}, {hi}, {len(pr['flips'])})")
    return True


def rebuild(q: QuestDB) -> None:
    """Re-derive the curve for every archived raw payload.

    The raws were kept from the start, so the per-strike history is not lost --
    only the derived rows were thrown away. Session date comes from the FILENAME
    (UNDERLYING_YYYYMMDD.json.gz), which is what makes the DTE arithmetic right.
    """
    files = sorted(RAW_DIR.glob("*.json.gz"))
    if not files:
        print("no archived raws to rebuild from")
        return
    done = 0
    for f in files:
        try:
            u, stamp = f.name.split(".")[0].rsplit("_", 1)
            asof = datetime.strptime(stamp, "%Y%m%d").date()
            payload = json.loads(gzip.decompress(f.read_bytes()))
        except Exception as ex:                        # noqa: BLE001
            print(f"  {f.name}: unreadable ({ex})")
            continue
        lv = compute_levels(payload, asof=asof)
        if lv is None:
            print(f"  {f.name}: too few strikes")
            continue
        lv["sess"] = asof.isoformat()
        n = store_strikes(q, u, lv)
        pr = lv["prof"]
        done += 1
        print(f"  {u} {lv['sess']}  spot={lv['spot']:.0f}  "
              f"LOCAL={'LONG' if pr['local_sign'] > 0 else 'SHORT'}  "
              f"book={'LONG' if lv['net_sign'] > 0 else 'SHORT'}  "
              f"flips={len(pr['flips'])}  "
              f"{n} strike rows{' (already present)' if not n else ''}")
    print(f"rebuilt {done} of {len(files)} archived payloads")


def main() -> None:
    dry = "--dry-run" in sys.argv
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if "--rebuild" in sys.argv:
        q = QuestDB()
        ensure_schema(q)
        rebuild(q)
        return
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
        nstr = store_strikes(q, u, lv) if q is not None else 0
        pr = lv["prof"]
        lo, hi = pr["pocket"]
        pocket = (f"{lo:.0f}" if lo else "-") + ".." + (f"{hi:.0f}" if hi else "-")
        # LOCAL first: it is the regime that applies where price actually is.
        # The book total is printed second and only as context.
        print(f"{u} {lv['sess']}  spot={lv['spot']:.0f}  "
              f"LOCAL={'LONG' if pr['local_sign'] > 0 else 'SHORT'}-gamma "
              f"pocket {pocket}"
              + (f" (next flip {pr['dist_to_flip']:+.0f})" if pr['dist_to_flip'] else "")
              + f"  |  book={'LONG' if lv['net_sign'] > 0 else 'SHORT'}"
              f"  flips={len(pr['flips'])}  "
              f"calls={[int(k) for k, _ in pr['call_walls'][:3]]} "
              f"puts={[int(k) for k, _ in pr['put_walls'][:3]]}  "
              f"({lv['n_options']} options, raw -> {raw_path.name}"
              f"{f', {nstr} strike rows' if nstr else ''}"
              f"{', DB row written' if wrote else ', dry/dup — no DB write'})")


if __name__ == "__main__":
    main()
