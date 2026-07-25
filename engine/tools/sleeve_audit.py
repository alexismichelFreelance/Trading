"""Sleeve health audit — does every sleeve in the roster ACTUALLY fire?

A strategy that never triggers is indistinguishable, from the outside, from a
strategy with no signal: no fills, no errors, no scorecard row. Three sleeves
sat silently dead in this engine before anyone noticed (PivotStrategy's
unreachable ER_MIN gate, and all three `flow` variants on adapt_k=4.0). This
tool makes "does it fire, and how often" a measured property instead of a
discovery months later.

It replays every roster label for a lane over the CAPTURED LIVE history --
per-second flow from claude_sec_live (reconstructed into Trade + BookFlow
exactly as ReplayFeed does) merged with 1m bars from claude_bars_live -- and
reports orders, sessions fired, and a verdict:

    DEAD   0 orders                      -> broken or unreachable gate
    RARE   fires on < RARE_PCT of days   -> suspicious; check the gate
    OK     fires regularly

    .venv/Scripts/python.exe tools/sleeve_audit.py
    .venv/Scripts/python.exe tools/sleeve_audit.py --symbol NQ
    .venv/Scripts/python.exe tools/sleeve_audit.py --symbol ES --only flow,pivot

A DEAD verdict is a BUG until proven otherwise. A gate that legitimately stands
down (e.g. a gamma filter on a regime that did not occur) should be argued for
explicitly, not assumed.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB                       # noqa: E402
from engine.core.events import BUY, SELL, Bar, BookFlow, PositionUpdate, Trade  # noqa: E402
from engine.core.timeutil import et_session_date                  # noqa: E402

RARE_PCT = 10.0          # fires on fewer than this % of sessions -> RARE
_MIN_NS = 60 * 1_000_000_000


def _lane_labels(symbol: str) -> list[str]:
    """The labels this lane actually runs, straight from config/live.yaml."""
    import yaml
    from tools.run_live import ALL_LABELS
    cfg = yaml.safe_load(open(ROOT / "config" / "live.yaml"))["instruments"]
    paper = str(cfg.get(symbol, {}).get("paper", "all")).strip()
    return list(ALL_LABELS) if paper in ("all", "") else [x for x in paper.split(",") if x]


def build_events(qdb: QuestDB, symbol: str) -> list:
    """Merged (ts, priority, event) stream from the live capture. Mirrors
    ReplayFeed: bars at CLOSE time and ordered before same-second flow, so
    coarse features update first."""
    out: list[tuple[int, int, object]] = []

    bars = qdb.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live "
                  f"WHERE symbol = '{symbol}' ORDER BY ts")
    for r in bars.itertuples(index=False):
        ts = int(pd.Timestamp(r.ts).value) + _MIN_NS
        out.append((ts, 0, Bar(ts, "1m", float(r.o), float(r.h), float(r.l),
                               float(r.c), int(r.vol), symbol)))

    sec = qdb.df("SELECT ts,pxc,adelta,avol,bid_cancel,ask_cancel,bid_add,ask_add "
                 f"FROM claude_sec_live WHERE symbol = '{symbol}' ORDER BY ts")
    for r in sec.itertuples(index=False):
        ts = int(pd.Timestamp(r.ts).value)
        px, ad, av = r.pxc, int(r.adelta), int(r.avol)
        if px == px:                                   # not NaN
            buy, sell = (av + ad) // 2, (av - ad) // 2
            if buy > 0:
                out.append((ts, 1, Trade(ts, float(px), int(buy), BUY, symbol)))
            if sell > 0:
                out.append((ts, 1, Trade(ts, float(px), int(sell), SELL, symbol)))
        out.append((ts, 2, BookFlow(ts, int(r.bid_cancel), int(r.ask_cancel),
                                    int(r.bid_add), int(r.ask_add), symbol)))
    out.sort(key=lambda x: (x[0], x[1]))
    return out


def drive_all(strats: dict, events) -> dict:
    """ONE pass over the stream feeding every sleeve (20 separate passes over
    ~1M events is needlessly slow). Simulates immediate fills so position-aware
    sleeves can manage and exit. Returns per-label activity counts."""
    acc = {lb: {"orders": 0, "entries": 0, "days": set(), "errs": 0, "pos": 0}
           for lb in strats}
    last_px = None
    total = len(events)
    for i, (ts, _prio, e) in enumerate(events):
        if i % 200_000 == 0:
            print(f"    ...{i:,}/{total:,} events", flush=True)
        px = getattr(e, "price", None) or getattr(e, "c", None)
        if px:
            last_px = float(px)
        is_bar, is_trade = isinstance(e, Bar), isinstance(e, Trade)
        for lb, s in strats.items():
            a = acc[lb]
            try:
                out = (s.on_bar(e) if is_bar else
                       s.on_trade(e) if is_trade else s.on_bookflow(e))
            except Exception:                          # noqa: BLE001
                a["errs"] += 1
                if a["errs"] == 1:
                    print(f"  [{lb}] first exception:", flush=True)
                    traceback.print_exc(limit=2)
                continue
            for o in out or []:
                a["orders"] += 1
                if not getattr(o, "reduce_only", False):
                    a["entries"] += 1
                a["days"].add(et_session_date(ts))
                a["pos"] += o.side * o.qty
                s.on_position(PositionUpdate(ts, s.symbol, a["pos"], last_px or 0.0))
    return acc


def run(symbol: str, only: list[str] | None) -> None:
    from tools.run_live import _make
    from engine.core.config import load_instruments
    inst = load_instruments(ROOT / "config" / "instruments.yaml")
    spec = inst.get(symbol)
    hmm = str(ROOT / spec.hmm_path) if (spec and spec.hmm_path) \
        else str(ROOT / "config" / "hmm_es_1h.json")

    qdb = QuestDB(timeout=180.0)
    events = build_events(qdb, symbol)
    if not events:
        raise SystemExit(f"no captured events for {symbol}")
    all_days = sorted({et_session_date(ts) for ts, _, _ in events})
    labels = [x for x in _lane_labels(symbol) if not only or x in only]

    print("\n" + "=" * 78)
    print(f"SLEEVE HEALTH AUDIT — {symbol}: {len(labels)} sleeves over "
          f"{len(all_days)} captured sessions ({all_days[0]}..{all_days[-1]})")
    print(f"{len(events):,} events (bars + reconstructed trades/bookflow)")
    print("DEAD = never fired (a BUG until argued otherwise); "
          f"RARE = < {RARE_PCT:.0f}% of sessions")
    print("=" * 78)
    print(f"\n{'sleeve':18s} {'orders':>7} {'entries':>8} {'days':>5} "
          f"{'fire%':>6} {'err':>4}  verdict")

    strats = {}
    for lb in labels:
        try:
            strats[lb] = _make(lb, symbol=symbol, hmm_path=hmm)
        except Exception as ex:                        # noqa: BLE001
            print(f"{lb:18s} BUILD-FAIL {type(ex).__name__}: {ex}", flush=True)
    acc = drive_all(strats, events)

    rows = []
    for lb in strats:
        a = acc[lb]
        pct = 100.0 * len(a["days"]) / len(all_days)
        verdict = ("DEAD" if a["orders"] == 0
                   else "RARE" if pct < RARE_PCT else "OK")
        flag = "  <-- investigate" if verdict != "OK" else ""
        print(f"{lb:18s} {a['orders']:7d} {a['entries']:8d} {len(a['days']):5d} "
              f"{pct:5.1f}% {a['errs']:4d}  {verdict}{flag}", flush=True)
        rows.append((lb, verdict, a))

    dead = [lb for lb, v, _ in rows if v == "DEAD"]
    rare = [lb for lb, v, _ in rows if v == "RARE"]
    print("\n" + "-" * 78)
    print(f"DEAD ({len(dead)}): {', '.join(dead) or 'none'}")
    print(f"RARE ({len(rare)}): {', '.join(rare) or 'none'}")
    print(f"OK   ({len(rows)-len(dead)-len(rare)}) of {len(rows)} audited")
    errs = [lb for lb, _, a in rows if a["errs"]]
    if errs:
        print(f"RAISED EXCEPTIONS: {', '.join(errs)}")
    print("-" * 78 + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="ES")
    ap.add_argument("--only", default="", help="comma list of labels to audit")
    a = ap.parse_args()
    run(a.symbol, [x for x in a.only.split(",") if x] or None)


if __name__ == "__main__":
    main()
