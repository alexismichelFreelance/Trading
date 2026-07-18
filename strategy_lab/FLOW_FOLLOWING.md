# Strategy #3 (candidate) — Thresholded Order-Flow Following

User's idea: hold a position proportional to recent net aggressive flow — lean long as size hits the
ask, short as it hits the bid — but take only a small, *thresholded* slice so you trade rarely, and let
the position self-net and reverse as flow flips (exit/reverse "at the tops"). Pure tape; no levels, no
discrete events. Inherently automatable.

## Formalization
Per second t (from `claude_sec_feat`: `adelta` = signed aggressor delta):
- thresholded flow: `a[t] = adelta[t] if |adelta[t]| >= TH else 0`  (only act on significant bursts)
- windowed signed flow: `F[t] = sum(a) over last W seconds`  (memory; self-nets as flow flips)
- target position: `pos[t] = clamp(F[t] / SCALE, -MAXP, +MAXP)`
- P&L: `pos[t] * (px[t+1]-px[t])`; cost charged on turnover `|pos[t]-pos[t-1]|`.
- The make-or-break metric = **points captured per contract turned over (ppc)**; must beat the
  aggressive cost ~0.25-0.30 pt/contract (spread + commission, hitting bid/ask).

## Results (after full 0.30 pt/contract aggressive cost)
- **Unthresholded loses** (ppc 0.11-0.18 < cost) — confirms "follow everything → lose to costs."
- **Thresholding fixes it.** ppc climbs to 0.6-0.8 at W=120-300s, th=200-500. Gross edge is positive
  at EVERY setting (flow-following genuinely captures direction); turnover is the whole problem and the
  threshold solves it.
- **Robust config W=120s, TH=500: net-positive in ALL 4 months incl. OOS ESH5** —
  Feb +2 · Mar +32 · Apr +96 · May +34 (net pts), ppc 0.62 (~2× cost).
- Lower threshold (th200) makes much more in April but goes NEGATIVE in calm months → the threshold is
  the **regime-robustness knob**. Higher TH = lower turnover, every month green.

## Why it's promising
- Mechanically distinct 3rd edge; positive in every month (more calm-robust than ignition/zones, which
  lean on April). Naturally automatable (sizing algo on the tape).
- Clears 2× the cost while paying the FULL spread aggressively — real headroom.

## Next levers (in priority)
1. **Execution** — adjust position with partial LIMIT orders instead of always crossing; cuts the 0.30
   cost toward ~0.10. Caveat: limits have fill uncertainty / partial adverse selection for a follower,
   so model carefully — but the 2× margin gives room.
2. **Conditioning** — size up where ppc is highest (regime / time-of-day), down where thin.
3. **Full position-sim** — sized $ P&L, equity curve, drawdown, both contracts, per month.
4. Confirm robustness of W/TH (avoid overfitting 4 months); test passive vs aggressive properly.

## Execution / deadband — the cost problem is SOLVED
Adding a **deadband** (only re-trade when target position moves > B contracts) collapses turnover ~6×
while keeping most of the gross, so breakeven cost rockets far above the 0.30 you actually pay:
| config (both contracts) | gross | turnover | breakeven cost | net @0.30 | net @0.075 |
|---|---|---|---|---|---|
| W120/th200, no band | 596 | 686 | 0.87 | +390 | +545 |
| W120/th200, band 1 | 326 | 117 | 2.79 | +291 | +318 |
| **W120/th200, band 2** | 300 | 46 | **6.52** | +286 | +297 |

Consequences:
- **Cost can't kill it** (breakeven 2.8-6.5 vs 0.30) and net is ~insensitive to execution quality
  (net@0.075 ≈ net@0.30) — so you do NOT need passive execution; the fix is to stop overtrading.
- With the deadband cutting turnover, the LOWER threshold (th200, more signal) now beats th500.
- Per-month (band2): Feb +2 · Mar +15 · Apr +268 · May +2 — positive every month, OOS included.

## BREAKTHROUGH — asymmetric hold ("easy in, hard out") unlocks the calm months
Replace the symmetric deadband with TWO bands: add-band `a` (small, to build in the flow direction) and
hold/flip-band `h` (wide, to resist reducing/reversing). Rule: update position if moving further in the
held direction by >a, OR against it by >h. `h` large ⇒ sticky ⇒ holds while flow persists.
| config (W120/th200) | gross | turn | pts/contract | avg hold | per-month net (F/M/A/M) |
|---|---|---|---|---|---|
| a1, h1 (symmetric, old) | 326 | 117 | 2.8 | 7 min | +2 / +15 / +268 / +2 |
| **a1, h≥4 (asymmetric)** | **829** | **50** | **16.6** | **119 min** | **+175 / +271 / +328 / +41** |

- **2.8× the net (+286 → +814)**, HALF the turnover, ppc 16.6 (55× the 0.30 cost — cost is irrelevant).
- **April share drops 94% → 40%** — the hold captures the slower Feb/Mar flow-trends the scalper missed.
- **h=4…18 identical** = wide flat optimum (robust, not tuned). The transition is all in h=1→4.
- Asymmetry direction matters: a=1 (easy in) beats a=2 (slow in misses the move). Quick in, slow out.
- **Validates "persistence is the signal"** (user idea #3): the edge is HOLDING while windowed flow
  persists, not trading it. Character shifts from convexity overlay → broad session flow-trend follower.
- Caveat: at this scale it rarely flips intraday (resets flat daily, ~0.7 changes/day, ~2h holds). Edge =
  "windowed flow direction predicts session drift." Positive 4/4 months on flow-set direction; needs more
  data/regimes to confirm it generalizes. Open threads: true two-timescale (fast-in/slow-hold), scale-in
  on persistence, cross-session carry.

## Three refinements tested (two help, one is a trap)
Baseline = W120 / asym-hold (a1,h5) / daily reset → +814, all months +, ppc 16.6.
- **SCALE-IN (pyramid on persistence) — WIN.** Grow position magnitude with the sign-run-length of the
  slow flow. Net **+814 → +1084 (+33%)**, all 4 months positive (Feb+165/Mar+253/Apr+576/May+89). Costs
  bigger positions (ppc 9.3, more April-weight) but robust. ⇒ adopt.
- **TWO-TIMESCALE (60s-in / 300s-hold) — marginal.** +888 total but loses May (−16). Single-window
  asym-hold already captures most of it; not worth the extra part.
- **CROSS-SESSION CARRY — TRAP.** Biggest total (+1355) but only by becoming a ~9-DAY multi-day bet:
  won huge Apr/May, but Feb −171 and Mar −570. The daily flat-reset is PROTECTION, not a limit — keep it.
  (Carry = a deliberately higher-variance swing variant, if ever wanted.)

**Improved flow config:** W120 + asymmetric hold (a1/h5) + scale-in, reset daily → **+1084, all months
positive, ppc 9.3 (~30× cost)**.

## Honest status — a convexity overlay, not a calm-market printer
~94% of profit is April; calm months are barely positive (+2 to +15 net pts). The strategy's CHARACTER:
a cheap, cost-bulletproof **long-volatility sleeve** — near-free to run in quiet markets (barely trades,
breakeven 6.5pt so it can't bleed), pays off hard on volatility expansion. Best used as a diversifying
CONVEXITY component alongside ignition + zones, not as a standalone calm-market edge.
Next: build it as the 3rd sleeve in the portfolio sim ($ P&L, sized); gather more volatile episodes to
confirm the convexity repeats; test conditioning (size up where ppc highest).
