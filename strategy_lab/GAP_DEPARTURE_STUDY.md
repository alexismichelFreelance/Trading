# Gaps-as-departures study (2026-07)

**Question (user, confirmed by literature + our spec):** on RTH bars, the overnight/RTH-open
gap is an imbalance the S/D methodology treats as a (strong) departure — a fresh zone. Our
detector needs a base + range-bar, so it MISSES clean gap-opens. Does adding gap zones help?

**Method:** on RTH 30m bars (`claude_bars_1m`, both contracts), keep the validated
base→departure zones AND add a GAP zone at each session open: `gap = first-bar open − prior
session's last close`; gap-up → DEMAND `[prev_close, open]`, gap-down → SUPPLY. Run the SAME
oracle fade/break/flip lifecycle + trade walk (`_walk`) on the gap zones as an ADD-ON (targeting
the real base structure; base baseline left untouched). Threshold sweep on |gap|.

**Harness validated:** base-only, isolated, reproduces the parity oracle EXACTLY — n=77,
+$47,502. So the add-on numbers are trustworthy.

## Results (oracle / per-signal ceiling — see caveat)
| |gap|≥ | gap zones | gap trades | win% | gap total $ | gap mean $ | base+gap combined |
|---|---|---|---|---|---|---|---|
| 3pt | 61 | 135 | 90% | +53,887 | +399 | +$101,388 |
| **5pt** | **58** | **130** | **90%** | **+50,124** | **+386** | **+$97,626** |
| 8pt | 56 | 125 | 90% | +36,260 | +290 | +$83,762 |

Per-setup (≥5pt): FADE +$23,091 (n=52, 88%) · BREAK +$8,878 (n=44, 89%) · FLIP +$18,156 (n=34, 94%).

## Read
1. **Gap zones add real, well-sampled, consistently-positive edge.** ~130 trades over 4 months
   (both contracts) — a solid sample (vs the confluence study's 9-12). Positive across EVERY
   threshold and EVERY setup. At ≥5pt they roughly **double the opportunity set** on top of base.
2. **Comparable, slightly-lower quality than base zones:** gap mean $386 vs base $617, win 90% vs
   95% (same oracle regime). Expected — gaps are more frequent, less selective — but still
   strongly positive. If base zones are worth trading, gap zones are too.
3. **Both "gap holds" and "gap fills" pay:** FADE (gap acts as support/resistance on first
   touch) AND BREAK (gap fills through, trade the continuation) are both positive — the
   lifecycle captures either outcome.
4. **Threshold:** the value lives in the 3-5pt band; ≥8pt keeps most $ but the sweet spot is
   **≥5pt** (filters noise-gaps, keeps 130 zones).

## Caveats (honest)
- **These are ORACLE numbers** — per-signal, generous walk, 90-95% win: the CEILING, NOT
  deployable P&L (the same oracle prints +$47.5k for base, while the realistic single-position
  engine is lower). The relative comparison is what's valid: gap zones ≈ base-zone quality.
- The **combined $97.6k overstates** what a single-position engine can take — gap and base
  signals overlap and compete for the one position. The realistic number needs the
  single-position replay.

## DEPLOYMENT CHECK — FAILED. Do NOT trade gaps (as implemented).
Implemented gap detection in the real `ZoneDetector` + oracle and ran the **single-position
engine** (`run_replay zones`), which is the deployment gate above the oracle:

| | oracle (per-signal ceiling) | single-position engine (deployable) |
|---|---|---|
| base only | +$47,502 | **+$6,961** |
| base + gaps (≥5pt) | +$152,251 | **−$35,907** |

Gaps TRIPLED the oracle but turned the realizable edge from +$6,961 to **−$35,907**. The oracle
was a mirage: it evaluates every signal independently with a generous walk, so it never pays for
the fatal path problem — **a gap zone forms with price sitting AT its proximal edge (the open),
so the single-position engine fades it immediately at formation** (buying the top of a gap-up
demand zone), rather than waiting for price to leave and genuinely RETURN to the level. Many of
those immediate fades stop out on gap-fill; flooded with ~130 extra gap signals, the one position
is constantly in bad gap trades and the base edge drowns.

## Verdict
- **Trading: gaps OFF** (`ZoneDetector` default `gap_thr=0`). The deployed sleeve stays base-only
  (+$6,961, RTH-fixed). The oracle's +$152k is NOT deployable.
- **Chart: gaps ON** as a VISUAL aid (the painter's `ZoneView` passes `gap_thr=5` for intraday) —
  seeing gap levels helps manual reads; we just don't auto-trade them.
- **The idea isn't dead — the ENTRY is wrong.** Proper S/D semantics require the zone to be left
  and RE-touched before fading; the gap-at-open violates that. A "wait for leave-and-return"
  fade (only arm a gap zone once price has cleared it, fade on the first true return) is the
  obvious fix and could recover the edge — a future study, not deployed on the current result.

Lesson (again): the per-signal oracle is a ceiling, not a P&L. The single-position replay is the
gate that protects real money — it caught a change the oracle loved.

Scripts: `strategy_lab/gap_departure.py` (add-on study), `evaluate_zones(gap_thr=5)` (oracle
reference). Detector `gap_thr` param retained (default 0) for the leave-and-return follow-up.
