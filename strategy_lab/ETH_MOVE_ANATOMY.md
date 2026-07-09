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
