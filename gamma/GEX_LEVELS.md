# Gamma STRIKE levels as support/resistance — and short-gamma mean reversion

Two questions from the user: (1) does short-gamma mean reversion apply, not just
long-gamma? (2) can we get the gamma *strikes* — put wall / call wall / flip — as
S/R levels where forced dealer hedging triggers covering?

They are the same idea. Gamma **levels** are what make short-gamma mean reversion
tradeable instead of suicidal.

## The mechanism (why the two questions converge)
- **Long gamma** (dealers net long): they sell rallies and buy dips continuously →
  price is **pinned** → dip-buying reverts smoothly. Walls act as soft magnets/edges.
- **Short gamma** (dealers net short): they buy rallies and sell dips → moves are
  **amplified** (trending). Buying a dip-in-progress gets run over — this is the
  user's documented left-tail in short-gamma regimes (DAY_TAXONOMY.md Part 2).
  Mean reversion still exists, but only as the **exhaustion snap-back** after the
  move overshoots — the user's own "3 pushes then reversal." The safe version
  anchors that fade to a gamma **level** (put wall as a covering floor), not to an
  arbitrary dip.

## Data we have (forward-only; honest about the sample)
Historical per-strike OI for 2025 is not freely available. The forward collector
`tools/fetch_cboe_gex.py` (CBOE true-OI, ≤7 DTE, dealer convention) has archived
**7 daily chains** (2026-07-02 … 07-10) to `gamma/raw_cboe/`, with summary levels
in `claude_gex_levels`. `gamma/gamma_profile.py` reconstructs the FULL gamma-by-
strike curve from each archive (validated: it reproduces the stored flip/walls to
0.1pt) and extracts secondary strikes, not just the three headline numbers.

ES trades at a **~+52pt basis** to SPX on this window (median 52, std 6, measured
ES@16:00 − SqueezeMetrics SPX). Strikes are placed on the ES chart as SPX+52.

## What price actually did around the levels (6 causal day-pairs)
Prior-session levels (ES terms) vs next-day RTH:

| tradeday | net γ | put wall | call wall | what happened |
|---|---|---|---|---|
| 07-03 | long | 7552 | 7552 | 13pt day, **pinned to 7552** (classic long-γ pin) |
| 07-07 | long | 7552 | 7602 | broke put wall to 7530, recovered, **closed pinned 7555** |
| 07-08 | **short** | 7552 | 7602 | opened 7510 already **below** the wall, fell to 7470 — wall failed, price amplified down (short-γ) |
| 07-09 | short | 7552 | 7552 | **put wall held (+9 bounce)**; range 7530–7595 |
| 07-10 | long | 7552 | 7552 | dipped to 7553 (**the put wall was the exact low**) and rallied **+34** to close 7621 |

The standout is **07-10**: the put wall at 7552 was the precise low of the day and
launched a 34pt rally — and that was the user's best recorded manual day (+$38k),
buying that morning dip. **07-08** is the counter-example the user asked about: on
the net-short-gamma day the put wall did NOT hold; price opened below it and
trended down. Short-gamma mean reversion there needed the exhaustion low (~7470),
not the wall.

## Read (illustrative, n=6 — NOT yet statistical)
- The **put wall is a real S/R candidate** in long-gamma regimes — it pinned
  (07-03, 07-07) or floored a reversal (07-09, 07-10) on 4 of 5 long-γ days.
- In **short gamma the wall is unreliable**; the tradeable MR is the exhaustion
  fade, consistent with the amplification mechanism and the user's tail risk.
- Net sign (long vs short gamma) is the switch between "fade the wall" and "wait
  for exhaustion."

## Shipped now
- `gamma/gamma_profile.py` — full-curve reconstruction from the raw archive.
- `engine/features/gamma_levels.py` `GammaLevels` — **causal** prior-session
  levels in ES terms (basis param).
- Chart overlay: `run_live --gex-levels [--gex-basis 52]` draws put wall (blue),
  call wall (orange), flip (violet, labeled long/short-γ) as horizontal S/R lines
  on the one chart. 5 unit tests.

## Pre-registered forward test (nothing deployed as a signal)
The daily fetcher grows the sample automatically. When ≥30 day-pairs exist
(~late Aug), test: put-wall-touch reaction split by net gamma sign (long: fade
toward wall / expect hold; short: expect break, fade only at exhaustion). Until
then the levels are **decoration + context on the chart**, not an automated entry.
Next natural build: the long-gamma dip-buy sleeve (modeled on the user) using the
put wall / flip as the entry anchor and a short-gamma tail guard.
