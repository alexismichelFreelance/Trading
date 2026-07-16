# Range-capture: mechanizing the user's VWAP-band confluence fade

The user's success bar (2026-07-14): *an engine strategy is useless unless it
RELIABLY extracts ≥20% of the day's range.* `strategy_lab/range_capture.py` scores
any strategy by exactly that, per day.

## The method — decoded from 2026-07-14 and CONFIRMED (analyze_today.py)
The user's stated method matches their fills to the tick:
- **SOLD at avg +2.5σ above VWAP** — all 10 of their ≥+2σ fills were sells; the day
  HIGH they faded, 7603.8, was pivot **R1 (7601.8) sitting on VWAP+2σ (7602.3)** —
  a clean confluence.
- **BOUGHT at/below VWAP** — all near-VWAP and −1..−2σ fills were buys, down to the
  prior-day close (7568) / put wall (7552).
- Cover shorts at VWAP; buy VWAP/support; target the next resistance.
Real and large: their 1-lot-equivalent fade of that 7602 high → VWAP was ~17pt =
**36% of the 47pt range in one swing.** The opportunity is unambiguous.

## Mechanized + measured — and it FAILS the bar
Confluence band-fade (short VWAP+2σ at a resistance level, long VWAP−2σ at a
support level; levels = prior-day pivots, weekly pivots, prior-day gamma walls/flip
+52 ES basis, prior close), 1 lot:

| variant | dataset | median capture | P(≥20% of range) | mean |
|---|---|---|---|---|
| one-shot (tight stop) | 2026 recorded (20d) | +0% | **15%** | −10% |
| one-shot | 2025 research (72d) | −7% | 6% | −14% |
| **scaling** (add-in, cover VWAP) | 2026 recorded | −4% | **15%** | −3% |
| scaling | 2025 research | −1% | 8% | −8% |

Scaling (the user's actual behaviour — average into the fade, cover at VWAP, wide
catastrophe stop) roughly halves the loss but does **not** clear the bar: ~15% of
days hit 20%, median still negative.

## Why — the gap is DAY-SELECTION, not structure
The scorer is correct (it caught 2026-07-14 at **+33%** and 07-06 at +48%). The
strategy WINS on clean range days and gets run over on trend/chop days
(07-13 −50%, 07-08 −39%). Regime conditioning helps only weakly: mid-gamma days
median +4% / 25% hit-rate, short-gamma −14%, long-gamma −45% (n=3). So a simple
gamma gate does not separate the good days from the traps.

**The edge that survives naive mechanization is the structure (levels + bands +
confluence). The edge that does NOT is the discretionary discrimination — which
+2σ touch is exhaustion vs the start of a trend, and which days to press vs stand
down.** That is the same wall the band-retest study and the day-selection research
hit: entries mechanize, day/setup selection doesn't (yet).

## Honest conclusion + next levers
1. The user's intraday opportunity is real and large (36% of range in one swing
   today), so the target is not crazy — the machine just can't yet tell the good
   setups from the traps.
2. Highest near-term value: **co-pilot** — surface the confluence setups (VWAP
   bands × pivots × gamma × zones) on the chart in real time so the user (who CAN
   discriminate) acts on them. The plumbing exists (gamma levels, zones, bands).
3. Research lever to crack autonomy: a **range-day vs trend-day classifier** known
   early (the day-taxonomy found first-30m range predicts the day) to gate the
   fade — fade only on days predicted to mean-revert. Pre-registered next test.
Nothing deployed. `range_capture.py` is the permanent scorecard for any future
variant.

## Exit experiments (2026-07-17) — motivated by 07-16
On 2026-07-16 the live engine shorted a rally (right side — the day closed −22pt)
but covered at breakeven and missed the decline, suggesting a regime-aware exit
(let a with-trend fade RIDE instead of covering at VWAP). Two versions, both FAIL:
- **Posture-runner** (`run_scale(runner=True)`: on a directional day per the early
  VWAP side, the with-trend fade rides to the next level): **zero aggregate effect**
  (2025/2026 identical). And it cannot help 07-16 — at the 10:20 entry the day's
  first-hour posture was **balanced (e_below 0.43)**; the weakness developed later.
  "Should have ridden" is hindsight; the direction isn't known at entry.
- **Trailing exit** (classification-free: ride the move, cover on a retrace):
  **worse** — mean −48pt (2025) / −16pt vs −9.7 / −6.4 for cover-at-VWAP. Letting
  winners run also keeps the fade sitting in losing trends.
Conclusion: the exit is not the fix. No exit rule rescues a fade that enters wrong
~half the time; the missing piece stays the discretionary read of which setup is
real. `runner=` is kept off by default as a documented negative.
