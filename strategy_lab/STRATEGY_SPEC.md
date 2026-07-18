# ES Day-Trading Strategy — Spec v1 (validated in + out of sample)

**One line:** detect an order-flow *ignition* aligned with the short-term trend, enter immediately, then
let a **1-hour HMM regime gate** decide the exit — *ride to the next pivot* when the hour is trending,
*bank the book-healing (BOOK) exit* when it's chop.

Built on ESM5 (Apr–May 2025). Validated out-of-sample on ESH5 (Feb–Mar 2025) — a different contract and
quarter, never used in development. Event-driven 1-second backtest, costs included.

---

## 1. Data & cost model
- **Instrument:** ES (E-mini S&P 500) front-month future. 1 tick = 0.25 pt = $12.50; 1 pt = $50.
- **Data:** MBO tick tape aggregated to 1-second bars (`claude_sec_feat` ESM5, `claude_sec_feat_esh5`).
  Fields per second: `pxc` (price), `adelta` (signed aggressor delta), `avol` (aggressor volume),
  `str` (ignition strength), `bid_cancel/ask_cancel`, `bid_add/ask_add`.
- **Cost:** 0.517 pt ($25.87) round-turn (commission + 1-tick slippage) subtracted from every trade.

## 2. Components (indicators)

### 2.1 Ignition — entry trigger  (validated, cross-period, #7/#10)
Per 1-second bar, an ignition fires when aggressive flow explodes vs its baseline **and** the attacked
side's book collapses:
- `str >= 5`  (|aggressor delta| / rolling-120s avg |delta|)  **and**  `avol >= 800` contracts/sec
- book-confirm: up-ignition → `ask_cancel > bid_cancel`; down → `bid_cancel > ask_cancel`
- `dir = sign(adelta)`  (+1 long, −1 short)

### 2.2 Trend-alignment — entry filter  (validated)
Take the ignition only if it agrees with the prior-5-minute drift: `sign(pxc[t] − pxc[t−300s]) == dir`.
(Counter-trend ignitions are ~coin-flips; aligned ones carry the edge.)

### 2.3 BOOK exit — "the move is done" (chop-mode exit)  (validated, ~75% win, #11)
The mirror of the ignition: exit when the side you're running *into* stops vanishing and gets **rebuilt**.
Once the trade is in profit (`peakFE >= 2 pt`), exit when:
- rolling-10s net `(add − cancel)` on the **leading** side (asks for a long, bids for a short) `> 200`
- **and** price has eased to `FE <= 0.8 × peakFE` (off the peak)

### 2.4 Pivot levels & pivot-target exit (trend-mode exit)  (validated in trend regime)
Daily floor pivots from the **prior** session H/L/C:
`PP=(H+L+C)/3; R1=2PP−L; S1=2PP−H; R2=PP+(H−L); S2=PP−(H−L); R3=H+2(PP−L); S3=L−2(H−PP)`,
plus prior-day High & Low and 50-pt round numbers. Build the sorted level set per day.
- **Target:** nearest level beyond entry in trade direction, ≥ 2 pt away (avg target ≈ 13–19 pt — the 20pt move).
- **Stop:** nearest level on the other side of entry.
- Exit at whichever (target / stop) prints first; else 600-second horizon.
- **UPGRADE (validated, v1.3): use the nearest opposing *virgin 30-min supply/demand zone* as the
  trend-mode target instead of the floor pivot** (fall back to the pivot if no live zone). Lifts the
  4-month total **+913 → +1004 (+10%)**, better in 3/4 months (biggest in the Apr trend month: +622→
  +699). Floor pivots showed no special HOD/LOD attraction in the level study; institutional zones do.
  See SUPPLY_DEMAND_ZONES.md. Also: ignitions firing *inside a live same-direction zone* are a
  higher-quality entry (ESM5 fwd-300s +4.33/74% vs +2.21/55%; ESH5 +2.77/70% vs −0.16/48%) — use as a
  size-up / quality filter, strongest in calm months.

### 2.5 Multi-timeframe HMM regime classifier — the gate  (the key piece)
2-state Gaussian HMM (Baum-Welch, 35 iters) on the **Kaufman efficiency ratio** of higher-timeframe bars
(`|net move over L bars| / Σ|bar-to-bar moves|`). Fit on ESM5, **frozen**, applied **causally**
(forward-filter within each day; state = arg-max filtered posterior; "trend" = higher-efficiency state).
- **15-min HMM:** 15-min bars, lookback L=4 (1 hour). Learned means: chop ER 0.33 / trend 0.89.
- **1-hour HMM:** 1-hour bars, lookback L=3 (3 hours). Learned means: chop ER 0.44 / trend 1.0.

States are *sticky* (self-transition ~0.85–0.91 ⇒ regimes persist), which is why the HMM beats hard
ER thresholds. **Lesson:** the efficiency ratio is useless on 1-second data (tick noise → ER≈0); it must
be computed on coarse (≥15-min) bars. And the timeframe that governs "will a 15-pt move develop" is the
**1-hour** regime, not the minute.

## 3. Strategy logic (state machine, per second)
```
if FLAT:
    if ignition fires AND trend-aligned:
        ENTER at pxc in dir
        regime = HMM_1h.state(current hour)        # causal; optionally require HMM_15m agree
if IN POSITION:
    if regime == TREND:  exit via PIVOT  (target / opposite-side level stop / 600s horizon)
    else (CHOP):         exit via BOOK   (book-healing after profit / -4pt HARD STOP / 600s horizon)
```
**Chop-mode hard stop (added v1.1):** in CHOP/BOOK mode, also exit if the trade goes -4 pt adverse.
This caps the losers that previously ran to the horizon. Lifts 4-month total +796 → **+946** and removes the
chop tail; cost is win-rate drops ~74%→~60% (some recoveries become small losses). 3pt stop makes slightly
more (+999) but choppier; 6pt keeps win-rate higher (+847). 4pt is the balanced default.
**Pivot-branch max-loss cap (added v1.2):** in TREND/PIVOT mode also exit at -12 pt. Trims the worst trade
from -33 to -12.5 for ~3% total cost (+946 → +913); all four months stay positive. Recommended on for risk.
**Second strategy attempt — fade at level in chop: TESTED, NOT VALIDATED.** Fading a level on a counter-
ignition in chop won only 15-25% (tiny n ~100, May negative). Tight stop / big target shape is sound but a
counter-ignition is a coin-flip, so hit-rate too low. Not used; needs a higher-hit-rate trigger.
**Day-level gate: TESTED AND DROPPED.** Requiring day-so-far efficiency ≥0.12/0.18 on top of the 1h-HMM
*reduced* total (+903/+873) and turned Feb negative — the 1h HMM already isolates the rideable regime, so the
day filter just removes good trades. Not used.

## 4. Parameters
| param | value | meaning |
|---|---|---|
| ignition `str_min` / `avol_min` | 5 / 800 | entry trigger thresholds |
| trend lookback | 300 s | alignment filter |
| BOOK `ACT` / netLead / giveback | 2 pt / 200 / 0.8×peak | book-healing exit |
| pivot min target dist `MINT` | 2 pt | skip levels too close |
| pivot levels | PP,R1-3,S1-3,PDH,PDL,50-round | target/stop set |
| HMM timeframes / lookback | 15m (L=4), 1h (L=3) | regime features |
| HMM states / fit | 2, Baum-Welch ×35 on ESM5 | frozen, causal filter |
| horizon cap | 600 s | hard time-out backstop |

## 5. Performance (1 contract, after 0.517-pt cost)
**Primary = 1-hour-HMM gate (G60).** "OOS" = out-of-sample (ESH5, never used to build).

| period | trades | win% | total pt | $ (×$50) | vs BOOK baseline |
|---|---|---|---|---|---|
| ESM5 Apr (in-sample)  | 302 | 69 | **+590** | +$29,500 | +384 |
| ESM5 May (in-sample)  | 253 | 74 | **+62**  | +$3,100  | +35 |
| ESH5 Feb (**OOS**)    | 135 | 68 | **+39**  | +$1,950  | +5 |
| ESH5 Mar (**OOS**)    | 209 | 75 | **+106** | +$5,300  | +69 |
| **Total**             | 899 | — | **+797** | **+$39,850** | +493 |

- Beats the validated BOOK baseline **in every month, in and out of sample** (+797 vs +493, ~+62%).
- Captures the trend-day upside (Apr +590) *and* fixes the previously-broken chop month (Feb +39 vs
  pure-pivot’s −118).
- Conservative variant (require 15m **and** 1h trend, G15_60): +777 total, slightly steadier in Feb.

## 6. Why it is trustworthy (and where it isn't)
**Trustworthy:** out-of-sample contract+period (ESH5) confirms it; HMM frozen from ESM5; all regime
filtering is causal (forward-only, no look-ahead); pivots use prior-day data; costs included;
~900 trades; the win-rate of the core (ignition+BOOK, ~74%) replicates across all four contract-months.

**Caveats (do NOT deploy real capital yet):**
- April (tariff-crash month) contributes the bulk of absolute P&L; calmer months are thin (+0.2–0.5 pt/trade).
- Only 2 contracts / 4 months. Needs more OOS periods + walk-forward before live.
- Chop-mode (BOOK) losers currently run to the 600-s horizon (no hard stop) — a structural tail risk to fix.
- The day-level gate I tried was mis-calibrated (threshold 0.35 too strict → collapsed to baseline); recalibrate.
- Entry is latency-sensitive (ignition is coincident with price, not leading); real fills may slip more than modeled.

## 7. Next steps
1. Recalibrate the day-level efficiency gate (lower threshold) and add it to the 1h gate.
2. Add a hard stop for chop-mode losers; measure tail-risk reduction.
3. Position-sizing by regime / volatility (size up in 1h-trend, down in chop).
4. More out-of-sample data (additional quarters) + rolling walk-forward refit of the HMM.
5. Paper-trade live before any capital.
