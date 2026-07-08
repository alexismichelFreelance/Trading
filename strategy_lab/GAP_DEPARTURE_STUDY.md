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

## Verdict / recommendation — BUILD IT (this one survives)
Unlike the confluence study (too thin), gaps-as-departures is a **strong, well-sampled,
mechanically-sensible improvement**. Recommend:
1. Add gap-open detection to `ZoneDetector` (threshold ≥5pt, gap-up→demand / gap-down→supply),
   feeding the same lifecycle. RTH-only detection (already shipped) is the prerequisite — gaps
   only exist on RTH bars.
2. Re-run the SINGLE-POSITION engine (`run_replay zones`) to get the realistic deployable number
   and confirm months stay same-sign; update the zones parity target to the new base+gap oracle.
3. Then it's live automatically (same detector powers live + the chart's ZoneView).

Script: `strategy_lab/gap_departure.py` (reproducible; reuses the oracle).
