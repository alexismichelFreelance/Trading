# ES Indicators — definitions, parameters, and honest status

Concrete reference implementations in `es_indicators.py` (unit-tested in
`test_indicators.py`, 16/16 passing). These are the *building blocks*; status
reflects what one month of data (ESH5 Feb–Mar, ESM5 late-Mar) showed. **None is
yet a validated, reliable money-maker** — that requires the April–May data now
ingesting. Parameters listed are defaults; all are principled or data-driven.

| # | Indicator | What it measures | Key params (default) | Status on 1 month |
|---|-----------|------------------|----------------------|-------------------|
| 1 | **Order Flow Imbalance** `order_flow_imbalance` | Net aggressor pressure, signed delta / volume, in [-1,1] | window (1 bar) | Weak as standalone forecast (edge ~0.15pt, decays <10min, < 0.52pt cost). **Strongest at the RTH open** (5–12× midday). Use as a confirmation filter. |
| 2 | **Session VWAP z & σ-bands** `session_vwap_bands` | Distance of price from cumulative session VWAP in σ units; ±1/2/3σ band prices | none (session-reset) | Core context indicator. 2.5σ extension is a real inflection, but its *directional* meaning is regime-dependent. Bands used as exit zones in the rebound setup. |
| 3 | **Kaufman Efficiency Ratio** `efficiency_ratio` | Trend vs chop: |net move| / path length, in [0,1] | window (30 bars) | Look-ahead-free regime gauge. Partially discriminative; did **not** by itself reconcile the cross-contract direction flip. Candidate regime input, not sufficient alone. |
| 4 | **Pivot + confluence map** `daily_pivots`, `confluence_count` | Floor pivots (PP, R1–3, S1–3) + prior-day H/L; confluence = # levels (incl. round numbers) within tol | tol (1.5pt), round_step (25) | Single-level fading has **no** edge (44% win). **Confluent zones reverse better** (lead: +0.84 vs −1.18pt on ESH5) — supports the "coincidence weighting" thesis, not yet validated. |
| 5 | **VWAP rebound** `vwap_rebound_signals` | The user's setup: impulse away from VWAP → pullback to VWAP → resume in impulse direction | impulse_sigma (1.5), pullback_z (0.4), lookback (20), confirm_delta (True) | **Most promising.** Feasibility: ESM5 showed +21pt continuation vs 9.7pt adverse (favorable asymmetry, the 20pt+ move). ESH5 reversed. Regime gate not yet found — top priority for the new data. |
| 6 | **Supply/Demand zones** `detect_sd_zones` | impulse → balance (wick < 50% of body) → impulse; zone = balance candle range | impulse_body_mult (1.5), wick_body_max (0.5), body_window (20) | **New, untested.** Implemented to your literal definition. NOTE: I read "balance = wick<50% body" as a body-dominant origin candle and also require below-average range; tell me if you meant a small-body base instead. To be tested on April–May. |

## How they combine (the intended strategy frame, to validate on new data)
The reframe from round 2: **capture 20–30pt moves with asymmetric R:R**, not scalp.
- **Entry:** VWAP rebound (5) — enter near VWAP on the pullback after an impulse, with order-flow (1) confirming the resume, **only in the right regime** (3 + to-be-found gate).
- **Context/zones:** S/D zones (6) and confluent levels (4) as high-probability reaction areas to enter near or target.
- **Exits/targets:** σ-bands (2) at ±2/3σ and the nearest confluent level/zone in the move's direction; stop below the pullback (tight) → favorable expectancy.
- **Cost:** 0.52pt round-turn (es_costs.py) — negligible vs a 20pt target; this is why the trend-capture frame works where scalping didn't.

## 7. Ignition (tick/second-level) — VALIDATED, cross-period
**Definition (per 1-second bar of MBO data):** an *ignition* fires when aggressive trade delta
explodes relative to its recent baseline AND real volume is present AND the *attacked side's*
resting book collapses:
- `str` = |signed_aggressor_delta| / (rolling-120s avg |delta| + 1)   (ignition strength)
- condition: `str >= 5` AND `avol >= 800` (contracts/sec) — "huge" tier is `avol >= 2000`
- book-confirm: for an up-ignition (delta>0) `ask_cancel > bid_cancel`; for down, `bid_cancel > ask_cancel`
  (the side being run into is having its limit orders pulled — your "liquidity vanishing" tell)
- direction = sign(delta)

**Reliability (forward net move at 60s; this is an INDICATOR, not a standalone strategy):**

| tier | period | dir-accuracy (base ~51.5%) | avg net | 5pt right:wrong |
|---|---|---|---|---|
| huge+confirm | ESM5 Apr–May (n144) | 58% | +1.19 | 20%:10% |
| huge+confirm | ESH5 Feb–Mar (n68)  | 56% | +1.52 | 22%:9% |
| big+confirm  | ESM5 (n682) | 54% | +1.09 | 17%:13% |
| big+confirm  | ESH5 (n418) | 53% | +0.84 | 15%:8% |

Replicates across both independent periods; dose-responds with volume and book-collapse. **Use:**
strong confirmation / size-up when aligned with a position; **immediate exit when it fires opposite**
(the premise of the opposing trade just got invalidated by real flow). NOT a profitable standalone system.
**Caveat:** sub-second analysis shows the book collapse is *coincident* with the aggressive prints,
not leading — so acting on it is latency-sensitive (reaction-speed within the ~0.5–1s build).

## 8. Volatility persistence (regime) — VALIDATED, cross-period
**Definition:** recent realized range (e.g. high-low of the last 10 one-minute bars) predicts the
*next* 10-minute range. **Reliability (both periods, monotonic):** prior range tight(≤5pt)→next ~6pt;
mid→~9pt; wide(>12pt)→13–21pt. Strong, replicated. **NOT directional** — it tells you the expected
*move size* / whether you're in a big-move regime. Use: position sizing, stop width, and *gating*
directional signals (don't expect 20-pt ignitions in a tight-range regime). NB: the "squeeze →
expansion" folklore is FALSE here — tight ranges stay tight; volatility clusters.

## 9. Time-of-day volatility profile (context) — VALIDATED, cross-period
**Definition:** expected per-minute range / volume by time of session. **Reliability (both periods):**
classic U-shape — open(0-30min) ~5.9-6.7pt range, midday trough ~4.0-4.6, close lift ~5.1-5.3; volume
tracks it. **Use:** with #8 gives a live "expected move size right now" (recent-range x time-of-day);
gate directional signals (ignitions/big moves cluster at open & close, not midday); size/stop accordingly.

## Rejected after testing (honesty)
- **Absorption** (huge volume but book holds → expected reversal): did NOT replicate
  (ESM5 reversed 60%, ESH5 continued 58%; n=25/12 — small-sample noise). Not a reliable indicator.
- **Net resting-liquidity shift** (adds−cancels, bid vs ask): coin-flip 49–54%, doesn't replicate at extreme.
- **Price acceleration** (2nd derivative of price): pure coin-flip 49–51%, net ~0, both periods.
- **Sweeps / volume-following / volume-burst** (aggression only, no book): coin-flip ~50% — aggression
  alone has no directional edge; needs book confirmation (→ folds into Ignition).
- **Fakeout from weak-flow break**: weak breaks don't reliably reverse (~50%, net ~0). No edge.
- **Compression→expansion (squeeze)**: false (volatility persists instead — see #8).

## Methodology correction + combination findings (2026-06, ESH5 Feb18–Mar18)
**A bug invalidated this session's first combination results — retracted.** Two compounding errors:
1. *Filter-before-window*: QuestDB applies `WHERE` before `lead()`, so "60 rows ahead" was 60
   *qualifying events* ahead (tens of minutes), not 60s.
2. *Irregular/interleaved sampling*: `es_swing_features` is ~2 rows/sec, and its columns live on TWO
   interleaved 1-Hz streams that NEVER co-occur (`bid_ask_imbalance`/walls on stream-X with `mid_price`;
   `excess_dom`/`signed_dom`/`delta_momentum` on stream-Y with NO price). Row-count `lead()` is meaningless
   on it. Verified the *previously*-validated indicators (Ignition, vol-persistence, time-of-day) are SAFE —
   they used precomputed `nxt60` on the regular 1-Hz `claude_sec_feat` (nxt60 matched a fresh full-table
   `lead` 100%).

**Fix — clean grid `esh5_1s_fwd`** (materialised, 2.5M rows): unified both streams onto one 1-Hz clock via
`avg()`-per-second (ignores nulls) `SAMPLE BY 1s FILL(PREV)`, then precomputed `f30/f60/f300` (true time
horizons) and rolling `rng300`. All combination scans are now trivial, correct GROUP BYs.

**RETRACTED: "imbalance × wall-context is a 2.5× directional combination."** Re-tested correctly:
- It's a **volatility proxy** — "no support" (price far from any wall) has avg 300s range 8.1pt vs 3.7pt
  at support. That's indicator #8 (vol persistence) re-discovered, not a new signal.
- It's **non-directional** — sell-imbalance + no-support reaches +5pt 12.4% vs −5pt 12.9% (symmetric).
  Predicts move SIZE, not SIGN. Residual wall effect within a vol bucket is only ~1.5–2× and not shown directional.

**New robust meta-finding (the useful one): single instantaneous book/flow features are directional
COIN-FLIPS at 60–300s.** On the clean grid, bucketed strong-neg…strong-pos, up vs down stay within ~1pt:
- `excess_dom` (de-meaned DOM imbalance): strPOS 7.9%up/9.0%dn, strNEG 6.7%up/6.9%dn — flat, faint contrarian.
- `delta_momentum_30s/90s` (trade-flow momentum): every bucket symmetric (e.g. strNEG 11.3up/11.4dn).
- They DO carry **size**: extreme buckets ride higher `rng300`. Size ≠ direction.
- `edom × dm30` **alignment combo**: directionally *consistent contrarian* (both-buy→down 11.6 vs 10.3;
  both-sell→up 3.1 vs 2.8) but only ~1pt asymmetry — **not tradeable** vs 0.5pt cost.

**Implication for the program:** directional edge does NOT live in instantaneous *state* conditioning.
It lived in the *event* (Ignition: aggression + book-collapse, 56–58% at 60s). Next: pursue directional
edge via **events × context** (Ignition gated by vol-regime / time-of-day / level proximity), and test
transitions (book pulling, delta flips) rather than levels — not more single-state buckets.

## 10. VALIDATED directional COMBINATION — Ignition × trend-align × session (ESM5 Apr–May)
First combination to survive the three-gate and carry real DIRECTIONAL information (not just size).
On `claude_sec_feat` (1.2M 1-Hz rows, verified-correct `nxt60`), gating the Ignition event by two
replicated context filters:
- **Trend-alignment**: ignition `dir` agrees with sign(pxc − pxc[5min ago]). Aligned 57.2% hit
  (Apr 60.0 / May 53.6) vs counter-trend 48.2% (Apr 50.0 / May 46.1). ~9pt spread, replicates.
- **Session**: morning (10–13 ET / 14–16 UTC) > afternoon (13–16 ET / 17–19 UTC).
- **Best cell — morning + aligned**: 60.3% directional hit, n=219, replicated (Apr 61.5 / May 58.4),
  across 20 distinct days *each* month. Favorable/adverse 60s excursion 6.3 / 2.5pt (2.5:1).
- **Worst cell — afternoon + counter (a FADE)**: 44% follow-through (Apr 45 / May 43), n=150. Replicates.

**Honest caveat on profit (indicator ≠ strategy):** the HIT-RATE (~60%) is robust and regime-independent.
The realized EXPECTANCY on a naive fixed 60s time-exit is NOT: Apr avg +5.6pt is outlier-driven (one
trade +97pt; winsorised ±8 → +1.48), May only +0.23pt net after the 0.517 cost. So the edge monetises
mainly when the day trends; a flat time-exit barely clears costs in normal vol. The 6.3:2.5 excursion
asymmetry says the right exit is *let winners run / cut losers* (bracket or trail), not fixed-time —
TO BE TESTED with path-ordered data. Natural pairing: a trend-day detector (Market Profile / IB day-type)
to size up when the regime will pay. **Status: validated directional indicator-combination; strategy
exit + position-sizing still to build.**

## Rejected this session (entry+exit hunt, by many means — all null or April-only)
Tested rigorously on the clean grids with Apr/May replication + the 3-gate. None survived:
- **Deceleration at tops (EXIT)**: gorgeous in April (TOP accel −0.24, BOTTOM +0.20) but VANISHES in May
  (TOP +0.01 ≈ RISING). April-crash artifact.
- **Opposite-ignition / aggression-flip (EXIT)**: precise (6–7× concentration at tops, replicates) but
  fires at only ~1–2% of tops → can't be a primary exit (misses 98% of turns). Useful only as a rare hard-exit.
- **Trailing flow can't predict tops at all**: by construction a top's trailing flow looks identical to a
  rise (aggression 11.2→10.6, vel 2.24→2.12). → EXIT must be price/volatility-based (vol-scaled trailing stop).
- **Sweep displacement-efficiency (ENTRY)**: absorbed-big sweeps don't fade (49% continue = coin-flip);
  breakout-big sweeps continue only in April (+1.12) not May (+0.03).
- **Sub-second cancel-asymmetry lead-lag (ENTRY)**: at 250ms, asks-pulled is COINCIDENT with buy-trades
  (tdelta +10) and the next 1s is ~0 (p_up 38.9%). Book vacuum does not LEAD price.
- **Sub-second move-onset detection (ENTRY)**: first 250ms of a 3pt/5s move ≈ noise (tdelta +2/−2,
  vol 24 vs 15 quiet). Big moves are not flagged at their start; flow and move are simultaneous.
- **Absorption-with-replenishment (ENTRY)**: hypothesis backwards — selling + bid-refill CONTINUES down
  (−2.9pt), doesn't bounce; April-only, n small, no replication.

**Meta-conclusion (well-supported): for PREDICTION, ESM5 is efficient at 1s AND 250ms.** Order flow is
coincident with price, never leading. Robust structure is regime/event-based only (Ignition tilt #7/#10,
volatility persistence #8, time-of-day #9). The single validated DIRECTIONAL entry is the Ignition ×
trend-align × session combination (#10, ~60%). Honest open frontiers all need either a big build with
uncertain payoff (full depth/queue reconstruction, ML on many features — weak & overfit-prone) or NEW
DATA (options gamma/dealer levels; cross-asset NQ/ES/bond lead-lag; event calendar).

## 11. VALIDATED out-of-sample: Ignition entry + BOOK-healing exit (~75% win)
First component to survive TRUE out-of-sample (built on ESM5 Apr-May, validated on ESH5 Feb-Mar — different
contract AND quarter). Event-driven JS backtest, 0.517pt cost, 1 contract.
- **Entry**: aligned ignition (str>=5, avol>=800, book-confirm) in direction of prior-5min trend.
- **Exit = BOOK**: exit when the side being run into stops vanishing and gets REBUILT — rolling 10s
  net (add - cancel) on the leading side turns positive (>200) after price is in profit (peakFE>=2) and
  has eased off the peak (FE<=0.8*peak). The flow mirror of the ignition. NOT a trailing stop / timer.
- **Result (win% / median pts after cost)**: ESM5 Apr 70%/+1.39, May 75%/+1.23; ESH5 Feb 74%/+1.08,
  Mar 78%/+1.35. Win rate and median REPLICATE across all 4 contract-months. This is real.
- **Honest limits**: (a) MEAN expectancy is thin/positive (0.16-1.25) and tail-driven — not yet a strong
  money-maker. (b) The 5s-confirmation "dud filter" (C5_1) was OVERFIT — boosted in-sample April but went
  NEGATIVE on ESH5 Feb; DROPPED. (c) Structural asymmetry to fix next: BOOK only fires after a profit peak,
  so the ~25% losers run to the horizon as fat losers and eat the mean. Next lever = order-flow LOSER-CUT
  (bail when opposing side takes control before the trade works), validated in+out of sample.
- Exit bake-off context: vs candidates EXH/FLIP/ABSORB/STALL/SWING (all captured only 10-18% of MFE, fired
  on pauses); BOOK best for consistency (75% win), CFLIP (confirmed opposing flow) best for big-move capture
  (33-43% of MFE) — a candidate "let-it-run" exit to combine later. Oracle (perfect exit) median MFE was
  +10.6pt(Apr)/+4.7pt(May): the moves are big; capture is the open problem.

## 12. Run-winners exit + regime gate (in progress) — pivot-ride works in trends, needs a classifier
Building on #11. Two "let winners run" exits both BEAT BOOK in trending months and LOSE in chop — same shape:
- **PIVOT (user's method)**: target next level (floor pivots R/S, prior-day H/L, 50-round); stop = level
  below. ESM5 Apr w58 med+2.49 tot+545 (vs BOOK +384!) — rides the 13-19pt move to the level. But ESH5 Feb
  tot-118, May -17: a ~15pt target rarely fills in chop. CFLIP (confirmed opposing flow) behaves identically
  (Apr mean +7.89, Mar -2.12). So there is ONE missing piece: a trend/chop gate.
- **Regime gate attempts (all plateau "help 3/4, break Feb")**: efficiency ratio on 1s data = useless (tick
  noise → ER~0, fires 0%); ER on 1-MINUTE bars works (key lesson: regime must be measured on coarse bars,
  not ticks); multi-timeframe ER (5min&15min agree) → ADA beats BOOK in Apr/May/Mar but Feb -42; adding a
  vol-FEASIBILITY test (target within 1.5x recent 15min range) still Feb -50. Manual thresholds can't cleanly
  isolate the rideable regime → motivates an HMM (learned joint trend/vol/flow latent states).
- **NEXT**: 3-state sticky Gaussian HMM on per-minute features (ER, realized vol, flow imbalance, range) to
  classify trend vs chop; gate PIVOT-ride (trend) vs BOOK (chop); validate in+out of sample.

## 13. Absorption / price-impact axis — REAL phenomenon, NOT tradeable as a filter (2026-06)
Chased the user's "refine, don't drop" directive past the absorption idea. Two layers, both fail honest accounting:
- **Tercile impact-per-volume** (efficient bursts continue, absorbed bursts reverse): the SEPARATION replicates
  across all 4 contract-months over a long forward window — a real statistical property. BUT pushing to
  quintile extremes does NOT sharpen it (Mar Q5 −0.08): the tercile is the robust form, the extreme is noise.
- **Integration into the validated strategy**: looked like +913→+1018 (+11%), but that was LOOK-AHEAD — the
  "efficient" trades were silently given the ignition-price entry while the efficiency was only knowable 2s
  later. Corrected to honest entry at px[i0+2]: **+913 → +405** (May goes negative). The edge was entirely the
  free better entry. Root cause = the absorption signal is *contemporaneous* with the move it "predicts".
- **Causal version** (predict efficiency from resting wall AHEAD at t−1, fully causal, ESH5 n=353): direction is
  a **coin-flip** in every wall-distance bucket (near 53% / mid 48% / far 49% / all 49%). Wall-ahead does not
  predict continuation direction. → absorption joins the graveyard for *trading*; keep only as a description.

**Reinforced meta-conclusion (~2 dozen directional micro-signals now tested):** intrabar order-flow/book
*direction* is coincident with price, never leading — every directional filter collapses to ~50% once
look-ahead is removed. Tradeable edge is structural only: Ignition event (#7/#10) + regime gate (HMM) +
level/book EXIT (the +913 strategy). Micro-direction is a dry well; further EV is in NEW information = gamma.

## 14. Gamma path (NEW information, in progress)
Round-number-strike PINNING proxy on ES alone: NO broad pinning (close dist-to-50-strike 13.1 vs random 12.5;
time-near-strike ~uniform 11–12%). Round numbers ≠ gamma levels → must use real OI-weighted profile.
Pipeline built: `D:\Trading\gamma\build_gex.py` ingests OptionsDX SPX EOD chains → daily zero-gamma flip /
call wall / put wall / long-vs-short-gamma regime (volume-weighted, OI-proxy since free OptionsDX lacks OI).
Awaiting Feb–May 2025 SPX EOD download → ingest to QuestDB → test walls as S/R and gamma-sign as
mean-revert-vs-trend switch against the ES tape.

## What's still missing (deliberately)
- The **regime gate** that says when the rebound continues vs reverses (efficiency ratio and VWAP slope both insufficient on 1 month).
- **Higher-timeframe trend alignment** (multi-timeframe filter) — not yet built.
- **Gamma-exposure levels** — need an external options-data source.
- Validation: walk-forward + cross-contract + deflated Sharpe, on the full 3 months.
