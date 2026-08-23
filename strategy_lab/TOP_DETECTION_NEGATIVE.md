# Detecting tops and bottoms — what failed, and why it kept looking like it worked

2026-08-22. A day spent trying to make trendjoin scale out near session extremes.
Everything below was built, measured, and thrown away. The code is gone; this is
the record so it is not rebuilt.

## The fact that explains all of it

`trendjoin_narrow`, 2026 live replay, 213 ES trades for **+$8,262**, win rate
32%, median trade **−$325**.

| | ES | NQ |
|---|---|---|
| best 1 trade | 48% of total | 23% |
| best 3 trades | **107%** of total | 62% |
| best 5 | 162% | **89%** |
| without the best 3 | **−$600** | +$13,975 |

**The effective sample is three trades, not 213.** That is not a strategy defect
— it is what trend-following is — but it means no trade-level rule can be
validated on this sleeve. Every candidate below either drew its whole result
from two or three trades, or died by touching them. ES and NQ disagreeing in
direction is the signature of this, not a bug to chase.

## What was tried and what it measured

**1. `TopSignal` — the top/bottom conjunction.** Price at the extreme + a real
leg into it + volume still present + conviction collapsed. It genuinely fires
one minute after the 2026-08-21 ES high (11:54 vs an 11:53 high) and turns that
session from −$238/lot to +$181/lot. In the replay the *tape* trigger beat the
*ruler* trigger at equal size (10,238 vs 6,525 per lot, 15 scales vs 65). But
68% of its edge was one session (2026-07-29), and against a null of "scale at a
random winning minute of the same trades" it was **p = 0.24** on 2026 ES. It
never established that the *timing* was doing the work.

**2. The volume weight** (`extreme_share`, slice sized by `1/(1+share/0.10)`).
Lab measured it improving 11 of 12 cells. In the engine it is a no-op at 4 lots
(−$7/lot) and costs −$476/lot at 2 lots where it can actually act. The lab had
measured it sizing a **4-slice ladder**; the engine takes one slice, so there is
nothing for it to size and only its noise transferred.

**3. Drift vs climax extremes.** Two visibly different anatomies — the biggest
delta minute *is* the extreme (climax) versus 10–20 minutes stale (drift). Real
as description. As a signal it does not replicate: ES drift −0.27R vs climax
−0.13R at 30 min, but NQ **−0.24R vs −0.32R**, the other way round. "0 of 21
drift extremes went the wrong way" sounds strong and is not — the base rate of
continuation is ~8%, so 0/21 happens by luck 17% of the time.

**4. Flow at the extreme minute.** The 08-21 high advanced +0.50 on a delta of
+20 — price nobody bought. Ranked against the same session's other new-high
minutes it is the **47th percentile**. Across 46 middle extremes the median is
**49%**, i.e. exactly no information. It looked striking because it was stared at.

**5. Give-back cap.** Every level on both instruments: ES −6,425 to −9,900, NQ
−18,455 to −26,715. It exists to truncate tails and the tail is the strategy.

**6. The ten-minute "unproven" scratch.** A trendjoin trade that pays is in
profit within ten minutes — **none of the twenty biggest trades across both
instruments was still flat at that mark**, and in that bucket losses outweigh
recoveries 18:1 (ES) and 7:1 (NQ). Ledger arithmetic said +$3,575 on ES. The
replay delivered **−$6,262**. See the methodology note below.

**7. Time-of-day and opening-range gates.** The opening hour is ES's worst window
(−6,762) and NQ's best (+19,625). Opening-30-min range genuinely predicts the
rest of the day (+0.60 / +0.50, no overlap) but converts into no P&L: gating or
sizing on it flips direction between instruments every time.

## Three methodological traps, all of which caught me

**An exit rule changes WHICH TRADES EXIST.** It cannot be evaluated on a fixed
ledger of trades taken under the old exit. The ten-minute scratch took ES from
213 trades to 293 — eighty trades that were not in the ledger it was measured on
— and timeout exits, which carry *all* the profit (+57,012), fell from 63 to 42.
Predicted +3,575, delivered −6,262, opposite sign. Only `portfolio_replay`
decides. **Position sizing is the exception**: it changes no entry, no exit and
no sequence, so ledger arithmetic is valid for it. That is why the one thing
that survived the day was a sizing rule.

**Lookahead hides in the window, not the formula.** "Overnight range predicts the
rest of the day at +0.52/+0.51" was wrong. The `.cache/replay` files run
**00:00–16:59 ET**, so a window of `(et < 09:30) | (et >= 16:00)` swallowed the
hour *after* the close — which sits near the day's own extreme and therefore
encodes the range it claims to predict. Cleaned to 00:00–09:29 it is **+0.16 /
−0.06** and the sizing ratio flips between instruments. Production features were
audited and are clean: `day_range`, `top_signal`, `day_score` and `gamma_basis`
all gate strictly to 09:30–16:00.

**2025 Databento MBO and 2026 NT8 live are not the same measurement.**
423k prints/session vs 42k; mean trade size 2.80 vs 8.30; |delta|/volume median
**0.132 vs 0.200**. NT8 consolidates what Databento emits per order, so with 47
prints a minute instead of 342 the two sides cancel less and conviction reads
mechanically higher. Price-only measures cross the two datasets; anything built
on per-print delta does not. A "four-window validation" across them silently
changes the feature's definition between windows.

## What was kept

Only [`DayRange.prior_wide()` / `size_mult()`](../engine/engine/features/day_range.py)
and the `trendjoin_daysize` twin — size the day from the **prior session's**
range. Committed in "Size the day, not the trade: three trades carry the sleeve".

## Two structural facts worth keeping

**36% of ES and 41% of NQ session extremes are printed at the open or the close.**
They are not reversals; the session began or ended there. Any top detector should
be graded on the remaining ~60%.

**Session P&L tracks the day's RANGE** — +0.79 (ES), +0.65 (NQ), monotone across
terciles on both — **and not its trendiness** (+0.40 / +0.13). The sleeve needs
the day to move, not to move one way. This is contemporaneous: it says what the
sleeve needs, not how to know it in advance. No causal predictor of it has
survived yet.
