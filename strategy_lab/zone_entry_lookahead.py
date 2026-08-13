"""Can a zone be traded on the very bar that created it?

Pricing a zone entry at the zone edge is only honest if an order could have been
RESTING at that edge before the bar arrived. That is true for a zone detected on
an earlier bar and false for one detected on the bar being traded -- there the
edge is that bar's own extreme, chosen with knowledge of where it went.

_on_tf_bar appends zones from det.update(b) and THEN calls _scan(b), so a zone
born on bar b is immediately eligible. This counts how often that actually
happens on the recorded ES tape, and what it is worth, before any claim is made
about the +$68k the entry re-pricing added to the replay.

    .venv/Scripts/python.exe ../strategy_lab/zone_entry_lookahead.py [tf]
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ROOT))

from engine.adapters.questdb import QuestDB          # noqa: E402
from engine.core.events import Bar                   # noqa: E402
from engine.strategies.zones_strategy import ZoneLifecycleStrategy  # noqa: E402


class _Probe(ZoneLifecycleStrategy):
    """Records, for every entry, the age in TF bars of the zone being traded."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seen: list[tuple[str, int, float]] = []   # (setup, age_bars, edge)

    def _enter(self, setup, d, entry, stop, target, z=None, bar=None):
        if z is not None:
            self.seen.append((setup, self._k - z.k, entry))
        return super()._enter(setup, d, entry, stop, target, z, bar)


def main(tf: str = "30m", gap_thr: float = 0.0) -> None:
    q = QuestDB(timeout=180)
    b = q.df("SELECT ts,o,h,l,c,vol FROM claude_bars_live "
             "WHERE symbol='ES' ORDER BY ts")
    s = _Probe("ES", point_usd=50.0, tf=tf, gap_thr=gap_thr)
    for r in b.itertuples():
        s.on_bar(Bar(int(pd.Timestamp(r.ts).value), "1m", r.o, r.h, r.l, r.c,
                     float(r.vol or 0), "ES"))

    if not s.seen:
        print(f"tf={tf} gap_thr={gap_thr}: no entries")
        return
    ages = Counter(age for _, age, _ in s.seen)
    same = sum(n for a, n in ages.items() if a == 0)
    tot = len(s.seen)
    print(f"\ntf={tf} gap_thr={gap_thr}   {tot} entries over {len(b):,} 1m bars")
    print(f"  SAME-BAR as zone creation : {same:>4}  ({100 * same / tot:.0f}%)"
          "   <- edge is this bar's own extreme; no order could have rested there")
    print(f"  zone already existed      : {tot - same:>4}  "
          f"({100 * (tot - same) / tot:.0f}%)   <- a resting order is legitimate")
    print("  age distribution (TF bars between zone creation and entry):")
    for a in sorted(ages)[:10]:
        print(f"    {a:>3} bars  {ages[a]:>4}")
    by = Counter()
    for setup, age, _ in s.seen:
        by[(setup, age == 0)] += 1
    print("  by setup:")
    for setup in sorted({k[0] for k in by}):
        z, nz = by.get((setup, True), 0), by.get((setup, False), 0)
        print(f"    {setup:6} same-bar {z:>4}   older {nz:>4}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "30m",
         float(sys.argv[2]) if len(sys.argv) > 2 else 0.0)
