"""Did the gamma refresh actually land? Exit non-zero if not.

The daily fetch failed silently for 8 days (2026-07-22..29): the scheduled task
was killed at its 15-minute limit under trading-day load, python's block-buffered
stdout was discarded with it, and nothing anywhere said the data had stopped
arriving. `GammaRegime` then served the newest row it had, so every *_gex sleeve
gated on stale gamma while reporting healthy.

"It ran" is not "it worked". This asserts the OUTCOME -- that each table has a
row recent enough to describe today -- and exits 1 if not, so the task's
LastTaskResult and this log both show the failure.

    .venv/Scripts/python.exe tools/gex_freshness.py [--max-age-days 4]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from engine.adapters.questdb import QuestDB          # noqa: E402
from engine.features.gamma import MAX_AGE_DAYS       # noqa: E402

# (table, human name, whether rows are per-underlying)
CHECKS = (("claude_gex", "SqueezeMetrics DIX/GEX", False),
          ("claude_gex_levels", "CBOE flip/walls", True))


def last_business_day(d: date) -> date:
    """Most recent weekday at or before d (holidays are not modelled — the
    max-age bound absorbs them)."""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS)
    a = ap.parse_args()
    today = date.today()
    ref = last_business_day(today)
    q = QuestDB(timeout=120.0)
    bad = []
    print(f"gamma freshness check — today {today}, reference session {ref}, "
          f"limit {a.max_age_days} days")
    for table, name, per_under in CHECKS:
        try:
            if per_under:
                # NOT NULL: rows written before the `underlying` column existed
                # (pre-2026-07-19) have a null tag and are frozen history — they
                # would fail a freshness check forever.
                df = q.df(f"SELECT underlying, max(ts) mx FROM {table} "
                          f"WHERE underlying IS NOT NULL GROUP BY underlying")
            else:
                df = q.df(f"SELECT max(ts) mx FROM {table}")
        except Exception as ex:                       # noqa: BLE001
            print(f"  FAIL {table}: query error {type(ex).__name__}: {ex}")
            bad.append(table)
            continue
        if df.empty or df["mx"].isna().all():
            print(f"  FAIL {table} ({name}): EMPTY")
            bad.append(table)
            continue
        for r in df.itertuples(index=False):
            who = f"{table}[{getattr(r, 'underlying', '-')}]" if per_under else table
            newest = r.mx.date() if hasattr(r.mx, "date") else date.fromisoformat(str(r.mx)[:10])
            age = (ref - newest).days
            ok = age <= a.max_age_days
            print(f"  {'ok  ' if ok else 'FAIL'} {who} ({name}): newest {newest}, "
                  f"{age} days behind {ref}")
            if not ok:
                bad.append(who)
    if bad:
        print(f"\n  GAMMA REFRESH FAILED for: {', '.join(bad)}")
        print("  Every *_gex sleeve will now DISABLE its gate (fails open) rather")
        print("  than trade on stale regime data. Fix the fetch before relying on")
        print("  any *_gex result. Check the lines above this in fetch_log.txt.")
        return 1
    print("\n  all gamma tables fresh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
