# Differentiating VWAP-respecting (range) vs VWAP-traversing (trend) days

The user's distinction (2026-07-15): some days VWAP gets traversed and price spends
the session on one side (fading VWAP fails); other days (like 07-14) VWAP/slightly
below offers great longs and levels offer great covers (fading works). Can we tell
which EARLY? `strategy_lab/day_type_classifier.py`, observation-first.

## Null result — "how one-sided / range-y" is NOT predictable from the morning
Labeling each day trend vs range by the rest-of-day one-sidedness + efficiency, the
first-hour features are **identical across day types**: first-hour one-sidedness
0.69 (range) vs 0.70 (trend) in 2026, 0.77 vs 0.77 in 2025; all early→outcome
correlations near zero (|rho| ≤ 0.21, p > 0.3). Gating the fade to "predicted-range"
days on this made capture WORSE. The magnitude of range-vs-trend does not announce
itself in the first hour.

## Positive result — the SIDE of VWAP persists (directional, not magnitude)
The right question is not "how one-sided" but "WHICH side owns the session", and
that persists:

| corr(early frac-below-VWAP, rest-of-day frac-below) | 60 min | 90 min | 120 min |
|---|---|---|---|
| 2025 research (72d) | **+0.45** (p<0.01) | +0.42 | +0.40 |
| 2026 recorded (20d) | +0.14 | +0.46 (p=.04) | **+0.55** (p=.01) |

Conditional (2025): a first hour spent **above** VWAP → stays above 65–71% of the
day (**VWAP acts as support → dip-buys work**); a first hour spent **below** → stays
below ~68%, closes below 65% (**VWAP acts as resistance → dip-buys fail** — the
user's "traversed, spends the day below"). 2025 is significant from the first hour;
the 2026 delayed/thin feed needs until ~11:00–11:30 ET.

This is exactly the user's distinction, quantified: **whether VWAP is today's
support or resistance is foretold (moderately) by which side price occupies early.**

## Payoff — real tilt, not a switch
Gating the fade directionally (no dip-buys on a below-VWAP day, no rally-shorts on
an above-VWAP day) cut the loss (2025 mean −8%→−4%, 2026 −3%) but still clears the
≥20%-of-range bar only ~15% of days. The persistence is a ~65–70% lean; the ~30%
of days it's wrong still run the fade over, and the mechanical entry lacks the
user's precision.

## Where this lands
- **Validated:** the day-character distinction is real, and the best early,
  causal predictor is the VWAP side price occupies in the first 60–90 min.
- **Not enough for autonomy:** it doesn't lift the mechanical fade to the user's
  20% standard on its own.
- **High-value as a co-pilot input:** show, by ~10:30–11:00 ET, whether VWAP is
  today's support or resistance (which side is persisting) — it directly tells the
  user (who supplies the precision) whether to buy dips to VWAP or fade rallies to
  it. That is the single most useful daily read this research produced.
- **Next lever:** the persistence is stronger by noon — an afternoon-only fade
  gated on the confirmed morning side is the next test; and cross-market/news
  context (exogenous, per the L3 finding) likely holds the rest of the signal.

## Update — the afternoon-fade test (b) FAILED
`run_scale(enter_after=720, gate_min=150, dir_gate=True)`: afternoon-only fade,
gated on the morning VWAP side confirmed by noon. Clears the 20% bar on 3% (2025)
/ 5% (2026) of days, median negative, ~0 trades/day — WORSE than the full-day
version. Restricting to the afternoon discards most of the day's range, and the
+0.55 side-persistence biases direction without making the mechanical fade
capture. Fifth mechanization of the user's edge to miss the bar (one-shot,
scaling, dir-gate full-day, afternoon-gate). The consistent gap is the user's
discretionary execution + exogenous day-character that price history does not
contain — bar-level hand-rules capture the structure but not the edge. Path
forward is data-driven (mine the recorded per-second tape + all-sleeve paper
signals for what actually precedes the good setups), not more hand-crafted rules.
