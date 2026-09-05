"""ONE-OFF: backfill claude_bars_live with pre-recorder history from NT8 .ncd.

WHY. The engine's zone detectors need ~20 bars of their own timeframe before
they can score anything (range >= 1.4 x avg20). Fed only from the live stream
they never accumulate that on 1h/4h, so a reversal level from last week can
never become a zone -- not because zones expire (ZoneBook never prunes) but
because no detector ever saw the bars. Seeding fixes it, and seeding wants a
deep table.

WHY THIS IS A ONE-OFF AND NOT A DEPENDENCY. .ncd is NinjaTrader's proprietary
binary and dies with a broker change. So it is used ONCE, here, to fill the
years before our own recorder existed. From then on claude_bars_live feeds
itself from whatever feed is attached, and nothing at runtime ever reads .ncd.

TIMESTAMP CONVENTION -- the thing that must not be wrong. .ncd stamps a bar at
its OPEN; claude_bars_live stamps it at its CLOSE. Verified empirically on the
2026-06-25 overlap: ncd ts + 1 minute matches claude_bars_live OHLC EXACTLY on
390 of 390 RTH bars, while +0 and -1 match zero. Every row is shifted +1 min.

SAFETY. Two guards, both necessary.
  1. Only sessions STRICTLY BEFORE the recorder's own first row for that symbol
     are inserted, so a duplicate is impossible by construction.
  2. Every session is CROSS-CHECKED against the independent daily CSV before it
     is sent: the decoded RTH midpoint must be within 0.4% of the daily bar's,
     and the decoded range within 0.35-1.6x of it. This is not paranoia about
     the decoder -- several contracts hold the same date, and read_best can pick
     the back month, whose prices differ by the roll basis. Unguarded, ~20-45%
     of sessions in EVERY era fail that check and would write wrong-contract
     prices into the table that feeds zones, ranges and every study.
Dry run by default; --write is required to touch the database.

    python tools/import_ncd_bars.py                 # show what it would do
    python tools/import_ncd_bars.py --write
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import socket
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import ncd  # noqa: E402

TABLE = "claude_bars_live"
DAILY = {"ES": "D:/Trading/gamma/esf_daily.csv",
         "NQ": "D:/Trading/gamma/nqf_daily.csv"}
NCD_ROOT = "C:/Users/alexi/Documents/NinjaTrader 8/db/minute"
NS = 1_000_000_000


def query(sql: str):
    url = "http://localhost:9000/exec?query=" + urllib.parse.quote(sql)
    with urllib.request.urlopen(url, timeout=300) as r:
        return json.load(r)


def recorder_start() -> dict:
    """First row the RECORDER wrote, per symbol. The import stops there."""
    out = {}
    for sym, lo in query(f"SELECT symbol, min(ts) FROM {TABLE} GROUP BY symbol")["dataset"]:
        if sym.startswith("__"):
            continue
        out[sym] = _dt.datetime.fromisoformat(lo.replace("Z", "+00:00"))
    return out


def sessions(sym: str, before: _dt.date):
    """Session dates for `sym` in the .ncd archive that predate `before`.

    Layout is ROOT/"<SYM> <contract>"/YYYYMMDD.ncd, one folder per contract, so
    the same date can appear under more than one; read_best picks between them.
    """
    seen = set()
    for folder in sorted(Path(NCD_ROOT).iterdir()):
        if not folder.is_dir() or folder.name.split()[0] != sym:
            continue
        for p in sorted(folder.glob("*.ncd")):
            st = p.name.split(".")[0]     # files are YYYYMMDD.Last.ncd
            if len(st) != 8 or not st.isdigit():
                continue
            d = _dt.date(int(st[:4]), int(st[4:6]), int(st[6:]))
            if d < before and d not in seen:
                seen.add(d)
                yield d


def daily_ref(sym: str):
    import csv
    out = {}
    with open(DAILY[sym], newline="") as fh:
        for r in csv.DictReader(fh):
            y, m, d = r["date"][:10].split("-")
            out[_dt.date(int(y), int(m), int(d))] = (float(r["h"]), float(r["l"]))
    return out


def usable(b, day: _dt.date, ref) -> bool:
    """Does this decode agree with the independent daily bar?"""
    if day not in ref:
        return False
    et = [t.astimezone(_NY) for t in b["ts"]]
    m = np.array([t.date() == day and 570 <= t.hour * 60 + t.minute < 960 for t in et])
    if m.sum() < 300:
        return False
    hi, lo = float(b["h"][m].max()), float(b["l"][m].min())
    rh, rl = ref[day]
    if abs((hi + lo) / 2 - (rh + rl) / 2) / ((rh + rl) / 2) > 0.004:
        return False                      # wrong contract: off by the roll basis
    rr = (hi - lo) / max(rh - rl, 1e-9)
    return 0.35 <= rr <= 1.6


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--ilp-port", type=int, default=9009)
    a = ap.parse_args()
    start = recorder_start()
    print(f"recorder's own history starts: "
          + "  ".join(f"{k} {v.date()}" for k, v in sorted(start.items())))
    sock = None
    if a.write:
        sock = socket.create_connection(("127.0.0.1", a.ilp_port), timeout=10)
    grand = 0
    for sym in ("ES", "NQ"):
        if sym not in start:
            print(f"  {sym}: no recorder rows; skipping (nothing to bound the import)")
            continue
        cut = start[sym].date()
        ref = daily_ref(sym)
        days, rows, rejected = 0, 0, 0
        buf = []
        for d in sessions(sym, cut):
            b = ncd.read_best(NCD_ROOT, sym, d)
            if b is None or b["n"] < 100:
                rejected += 1
                continue
            if not usable(b, d, ref):
                rejected += 1
                continue
            days += 1
            for i in range(b["n"]):
                # +1 MINUTE: ncd stamps the OPEN, this table stamps the CLOSE
                ts = int(b["ts"][i].timestamp()) * NS + 60 * NS
                buf.append(f"{TABLE},symbol={sym} o={float(b['o'][i])},"
                           f"h={float(b['h'][i])},l={float(b['l'][i])},"
                           f"c={float(b['c'][i])},vol={int(b['v'][i])}i {ts}\n")
                rows += 1
                if a.write and len(buf) >= 5000:
                    sock.sendall("".join(buf).encode()); buf = []
        if a.write and buf:
            sock.sendall("".join(buf).encode()); buf = []
        grand += rows
        print(f"  {sym}: {days} sessions kept, {rejected} rejected by the daily "
              f"cross-check, {rows:,} bars "
              + ("SENT" if a.write else "(dry run)"))
    if sock:
        sock.close()
    print(f"  total {grand:,} rows " + ("written" if a.write else "would be written"))
    if not a.write:
        print("  re-run with --write to apply")


if __name__ == "__main__":
    main()
