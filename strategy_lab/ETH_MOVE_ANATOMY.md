# Overnight move anatomy — observation-first catalog (2026-07)

**Mandate (user):** stop hypothesis-first testing. Observe the actual overnight moves — tick by
tick if needed — and answer *what makes price move* (MMs pulling quotes? people hitting a thin
book?) BEFORE theorizing. This is the first observation pass + verification.

**Instrument:** `claude_sec_eth_book` (NEW: per-second bid/ask ADD+CANCEL volumes for all ETH
hours, both contracts, 3.5M rows from raw L3 `mbo_events`; `tools/build_eth_book.py`) joined
with trades (`claude_sec_eth`). Every zigzag leg ≥8pt across the 72 nights became a case file:
volume, aligned-aggressor share, contracts-per-point, receding-side cancels vs consumption
(WithdrawalIndex), quote-gap seconds, start/end tagged vs the USER'S toolkit (overnight VWAP
±2σ/3σ, daily/weekly/monthly floor pivots, prior-RTH H/L/C, ON H/L). **2,613 moves catalogued**
(36/night) → `strategy_lab/eth_move_catalog.csv` (browsable per-move).

## Verified observations (each candidate finding was baseline-checked)
1. **Aggressor flow during moves is nearly BALANCED: median aligned share +0.09** (≈54.5/45.5).
   A ≥8pt overnight move is NOT driven by one-sided hitting. This is the mechanical reason every
   aggressor/imbalance signal failed — the tape's aggressor column barely leans during moves.
2. **The withdrawal ratio is structural, not move-specific.** During moves, receding-side
   cancels/(cancels+consumption) = 0.81 — but the QUIET baseline is 0.82. The overnight book
   always churns ~4–5 contracts of cancels per contract consumed (MM repricing). Gross
   add/cancel aggregates do NOT distinguish moving from quiet periods. (First-pass "100% of
   moves are withdrawal-dominant!" was an artifact of not checking the baseline.)
3. **Levels showed NO real pull, after removing a tautology.** Raw tagging said 42%/41% of
   moves start/end at a toolkit level vs 34% random — but ONH/ONL end-tags are outcome-coupled
   (a leg that MAKES the overnight high tags it trivially). Excluding them: starts 34%, ends 33%
   = exactly the random control. On this data, ≥8pt overnight moves neither launch from nor die
   at VWAP-band/pivot/prior-day levels more than chance (±2pt).
4. **Moves are cheap to fuel but not tradeless:** median 232 contracts per point (p25 121);
   quote-gap seconds (price moves with zero prints) ≈ 0%. Small prints walk through a thin book;
   it's never a pure no-trade quote gap at 1s resolution.
5. **Hourly uniformity:** median extent 13–15pt and identical churn ratios across ALL hours —
   the anatomy is not Asia-specific; it is the overnight norm. (21:00 is the exception: ~6× the
   contracts-per-point — the book is still thick right after the RTH close.)
6. **The monsters are news:** the 10 biggest legs (66–114pt, some in <5 min) are 2025-04-09/10
   (tariff pause), 03-20, 05-12 — all with the same balanced-aggressor, normal-churn signature.

## Current honest answer to "what makes overnight price move"
Not visible in 1-second side-aggregates. During a move the book shows its normal massive
churn and near-balanced aggression — so the signature must be POSITIONAL (WHICH quotes vanish:
at-the-touch vs deep; queue depletion at best; cancel-then-trade vs trade-then-cancel sequencing)
rather than in gross volumes. Answering that requires **L3 book reconstruction** (we hold
order_id-level MBO): rebuild the top-of-book around a sample of move-starts vs matched quiet
controls and inspect the touch tick by tick. That is the next observation step — the microscope
goes one level down. No strategy until the mechanism is seen.

Scripts: `strategy_lab/move_anatomy.py` (catalog), `anatomy_checks.py` (baselines),
`tools/build_eth_book.py` (data). Catalog: `strategy_lab/eth_move_catalog.csv`.

## L3 TOP-OF-BOOK RECONSTRUCTION — first pass (2026-07)
Built the full L3 replayer (`strategy_lab/l3_touch.py`): replays raw MBO A/C/M/F events
(2–6.4M/night), maintains per-side price-level aggregates + best bid/ask, and classifies every
CLEAR of the best level by terminal event — **filled (eaten) vs cancelled (abandoned) vs
repriced**. Validated: correct session spans, 48k–193k classified clears/night, phantom-removal
rate ~7% (empty-book bootstrap at 21:00, no snapshots in our MBO; touch state populates fast).
Case-control: catalogued move-starts vs random quiet controls, 4 nights (calm/median/news).

**Pooled first-pass "findings" — BOTH DIED in the within-night confound check:**
| pooled (seductive) | per-night (true) |
|---|---|
| depth@best at move-start 2 vs 8 quiet ("thin touch precedes moves!") | 03-07: 8v7 · 03-11: 6v4 (case THICKER) · 05-19: 9v13 · 04-09: no controls exist |
| pre-move cancel-clears 148 vs 89 ("abandonment spikes before moves!") | 98v116 / 199v100 / 61v85 — inconsistent |

The pooled effects were **Simpson's paradox via night mix**: 281 of 349 cases came from the
2025-04-09 news night (thin book ALL night, zero quiet controls), controls from calm nights.
Within nights, move-starts are NOT distinguishable from quiet moments by touch depth or clear
composition at 1s resolution — on this 4-night design.

**Design lessons for pass 2:** (a) within-night controls ONLY, hour-matched; (b) scale to all 72
nights (news nights analyzed separately or dropped); (c) the zigzag pivot is a coarse anchor —
initiation should be re-anchored in event time (first tick of the run). The microscope works;
the first 4-night sample answered "nothing at this resolution/design," not "nothing exists."

Session tally of candidate discoveries killed by verification: **four** (gross withdrawal ratio,
level pull, thin-touch precursor, abandonment spike). This is the observation discipline working.

## PASS 2 — all 72 nights, within-night controls, liftoff anchoring (2026-07)
Full-population replay (~350M events, per-night checkpoints, 0 failures): 2,282 liftoff-anchored
cases vs 4,531 within-night hour-matched controls (fallback chain when the hour is blocked).
Night-level paired deltas + sign tests across nights (`l3_pass2.py` / `l3_pass2_agg.py`):

| metric (per window, receding side) | median night-delta (case−ctrl) | nights positive | p (sign) |
|---|---|---|---|
| PRE cancel-clears [−300,0) | **+14.5** | **43/65** | **~0.01** |
| PRE fill-clears | +10.0 | 40/67 | 0.14 (ns) |
| DUR cancel-clears [0,+120) | +17.0 | 49/67 | ~0.00 |
| DUR fill-clears | +13.0 | 46/67 | ~0.00 |
| depth@liftoff | **+0.8 (THICKER)** | 39/55 | ~0.00 |
| within-case depth Δ (0 vs −300s) | 0.0 | — | — |

**The surviving observation:** overnight moves emerge from **front-of-book CHURN, not thinness.**
In the 5 minutes before liftoff, receding-side best-level **abandonment (cancel-clears) is
elevated ~15% (significant across nights) while consumption is NOT** (fills ns pre-move); during
the move both rise (partly mechanical — an 8pt move ≥32 best-clears by definition), cancels
leading. Depth at best is NOT reduced — marginally thicker. The touch signature near initiations
is **instability of the quote layer** (cancel-led repricing at the front), not depletion, and not
aggressor pressure. DUR-window composition: the receding front is more often *abandoned* than
*eaten* even as the move runs. This is the first candidate that survived the within-night design.

## PASS 3 — the confound test: the churn precursor is DEAD (2026-07)
Activity-matched controls (per case: the same-night quiet moments with the CLOSEST recent
event-rate; match quality excellent — median rate error 2%), timing curve in 30s bins, and
side-specificity (`l3_pass3.py`/`l3_pass3_agg.py`, 2,282 cases / 4,531 controls, all 72 nights):

| question | result | verdict |
|---|---|---|
| Q1 pre-liftoff cancel-clears vs activity-matched controls | **−12.0** (27/67 nights+, ns) | the +14.5 excess REVERSES — it was activity clustering |
| Q1 pre-liftoff fill-clears | −13.0 (17/67, p≈0.000 — FEWER) | see artifact note below |
| Q2 timing curve (10×30s bins) | controls ABOVE cases in every bin; both ramp with activity | no move-specific build-up |
| Q3 receding−advancing side churn | median 0.0 within-case; night-level −4.5 (18/60+, p≈0.003 NEGATIVE) | no directional tell; the sign reflects the PRIOR leg's mechanics |

(The Q1 fill-clear "inversion" — busy-but-quiet moments have MORE front consumption than
pre-move moments — is likely a composition artifact: controls were matched on TOTAL event rate,
not on trade/quote mix. Not claimed.)

**Campaign conclusion (Feb–May 2025 data, all resolutions tried):** overnight ES ≥8pt moves have
**no detectable endogenous precursor** in gross flow, aggressor imbalance, toolkit levels, touch
depth, best-level clear composition, or side-specific churn — at 1-second and L3-event
resolution, under within-night and activity-matched designs. Moves cluster in busy tape; given
equal busyness, the moments before a move look like any other busy moment. The most consistent
reading: overnight initiations are **exogenous arrivals** (news / cross-market impulses — Nikkei,
USDJPY, bonds — which this tape only sees as their ES shadow) hitting a book that churns
identically at all times. The remaining honest paths: (a) 2026-regime retest on recorder data,
(b) cross-market inputs as conditioning features, (c) sub-second event-sequencing (the one
unexplored resolution). Verification tally for the campaign: **five** candidate discoveries
killed before reaching a trading decision.

**Standing confound (named, untested — RESOLVED by pass 3 above): activity clustering.** Controls are hour-matched QUIET
moments; cases sit amid action (the PRE window overlaps the preceding opposite leg). "Elevated
churn near moves" may partly restate "moves cluster in locally-active periods." To claim a
PRECURSOR, pass 3 must use **activity-matched controls** (same recent event-rate, no subsequent
move) + a timing curve (when does the elevation start, in 30s bins?) + side-specificity (does
ASK-front churn precede UP moves specifically = directional information, or is it symmetric?).
No trading claims until then.
