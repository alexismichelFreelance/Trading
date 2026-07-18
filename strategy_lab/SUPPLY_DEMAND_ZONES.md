# Institutional Supply/Demand Zones — VALIDATED (the level that works)

The "impulse → balance → impulse" levels. Tested correctly (30-min timeframe, virgin first touch)
they are the strongest level-based signal found. Earlier 1-minute version was wrong (overmarking noise).

## Definition (per documented methodology — NinjaTrader / Seiden)
- **Zone = base → departure.** A *base* of 1–3 tight bars (the balance), then a *departure*: a
  decisive, above-average-volume bar that moves sharply away (the impulse). It is an **area**
  (base high→low), not a line.
- **Demand** zone = base then strong UP departure (sits below price, support).
  **Supply** = base then strong DOWN departure (above price, resistance).
- **Timeframe matters — these are higher-TF constructs.** 30-min is the intraday workhorse for ES;
  daily / 4H / weekly carry the most institutional weight; multi-TF overlap = strongest. 1-min is noise.
- **Freshness is everything.** A *virgin* (untested) zone is highest-probability; it **degrades with
  each revisit and is ~dead after 2 touches**. (Opposite of classic S/R, which strengthens with tests.)
- **Strength enhancers:** departure size/volume (gap > extended-range bar), tight/short base,
  trend alignment, R:R to the opposing zone.
- **Invalidation / flip:** stop sits *beyond* the zone — a close fully through kills it. A convincingly
  broken zone **flips polarity** (broken demand→supply; former support→resistance) and is single-use.
- **Targets:** the next opposing zone (zone-to-zone), min ~2:1 R:R.

## Detector (this build, 30-min bars from claude_bars_1m)
- departure bar j: `range[j] >= 1.4×avg20(range)`, `|c-o| >= 0.5×range[j]` (decisive), `vol[j] >= avg20(vol)`.
- base: 1–3 immediately-preceding bars with `range <= 0.8×avg20(range)`.
- zone = [min(base lows), max(base highs)]; dir = sign(departure).
- Reaction metric = symmetric bounce/break: at a touch of the proximal edge, did price move +3pt
  (favorable) before −3pt (adverse) over the next 8 bars. Baseline (random price ±3) ≈ 52%.

## Results (1-second-equivalent reaction on 30m bars; both contracts, Feb–May 2025)
| metric | ESM5 | ESH5 | baseline |
|---|---|---|---|
| **virgin zone — 1st touch bounce** | **88%** (n=17) | **93%** (n=15) | ~52% |
| 2nd touch | 44% (n=106) | 51% (n=69) | ~52% |
| flip after break | 100% (n=15) | 100% (n=9) | ~52% |
| zones/day | 0.51 | 0.82 | — |

**Reads:** (1) virgin first touch holds ~90% vs 52% chance — a large edge; (2) the edge is GONE by the
second touch (44–51%) — the freshness/2-touch-invalidation rule, confirmed; (3) broken zones flip and
hold ~100%. Replicates across two contracts and matches the documented behavior → real, not fit.

## Honest limits
- **Small n** (15–17 virgin touches/contract; zones are genuinely rare). Effect size is large and
  structurally consistent, but the exact hit-rate needs more data (more months/contracts) to pin down.
- Only the **30-min** TF built so far. Daily / 4H / weekly zones (heavier weight) and multi-TF
  confluence not yet added — likely even stronger.
- Strength enhancers (gap vs ERC departure, virgin-and-HTF-aligned) not yet split out (n too small).

## Strategy backtest (30m virgin zones, limit entry, stop beyond zone; cost 0.5pt)
| execution | ESM5 | ESH5 |
|---|---|---|
| hold to opposing zone (zone-to-zone) | +128 / 65% win / 0.53R (n=17) | −10 / 47% (n=15) |
| scalp +4pt only | +60 / ~100% / 0.2R | +53 / ~100% |
| **scale: ½ at +4, BE stop, runner→zone** | **+170 / 0.60R** | **+114 / 0.48R** |
| virgin AND 4h-confluent (zone-to-zone) | +42 / 80% (n=5) | +68 / 67% (n=3) |

**Takeaways:** the far zone-to-zone target gives the edge back (flat on ESH5); the **near reaction is
near-certain** (~90% true, all 15–17 hit +4 in-sample). Best execution = **scale out**: bank the
high-prob scalp on half, breakeven stop, let the runner target the opposing zone risk-free → positive
on BOTH contracts. 4h-confluence lifts win-rate (80%/67%) but n=3–5.
**Caveats:** ~100% scalp is ~90% in truth (small n + same-bar fill optimism → expect ~10% full losers);
n=15–17 virgin touches/contract — structure solid & cross-contract, magnitude needs more data.

## Multi-timeframe + integration (extension)
- **Virgin bounce by TF** (1st touch, ±3pt metric): 30m 88/93%, 1h 100% (n=6–7), 4h 100% (n=2–3).
  Higher TF = stronger, but n shrinks. **Daily/weekly zones can't be built on 2–4 months of data**
  (need years of daily bars).
- **Confluence:** on the small scalp metric, HTF-confluent 30m zones ≈ standalone (~90–100%, saturated).
  Confluence pays on the HARD target (zone-to-zone win 80/67% confluent vs 65/47% standalone) — it
  helps the **runner reach the far zone**, not the near scalp.
- **Ignition × zone confluence (ESM5):** an ignition firing INSIDE a live same-dir zone → fwd-300s
  **+4.33 / 74% up (n=23)** vs open-air **+2.21 / 55% (n=545)**. Zones ~double the ignition edge and
  lift directional accuracy 55→74%. The two best edges reinforce. (ESH5 confirm + full P&L = next.)

## Integration into the validated ignition strategy (DONE — both work)
- **Ignition × zone entry confluence, OOS confirm (ESH5):** in-zone fwd-300s **+2.77 / 70% up (n=33)**
  vs open-air **−0.16 / 48% (n=289)**. On ESH5 the open-air ignition had NO edge — the signal was
  entirely the zone subset. Replicates ESM5 (74 vs 55).
- **Zone as trend-mode TARGET (replaces floor pivots) → validated strategy +913 → +1004 (+10%)**,
  better in 3/4 months (Apr +622→+699, Feb +42→+64, Mar +166→+170, May +83→+72). Trend winners run to
  the next opposing institutional zone instead of the nearer pivot. Well-sampled (~900 trades).
- **Zone-confluent entry subset:** 54 trades, mean **+2.30/trade, 73–83% win** — strongest in CALM
  months (Mar +3.01, May +2.41 mean vs ~0.8/0.3 overall). A sizing/quality signal where base edge is
  thin; adds less in April (open-air ignitions already excel in crash vol).

## Zone LIFECYCLE — three setups, not one (2026-06)
The zone has a full lifecycle; each stage is a setup. Sized 2%/$2k, NT-ish cost, look-ahead-corrected
(targets only use zones that formed BEFORE entry):
| setup | n | net $ | avg $/trade |
|---|---|---|---|
| Fade (fresh zone, 1st touch, scale-out) | 27 | +17,534 | +649 |
| Break (trade WITH the break when zone fails) | 31 | +5,180 | +167 |
| Flip (broken zone retested from other side) | 19 | +24,789 | +1,305 |
| **Combined sleeve** | 77 | **+47,502** | +617 |

- Break + flip roughly DOUBLE the old fade-only sleeve ($20k→$47.5k). Flip is the biggest contributor.
- **Composite strength (departure 0-2 + base 0-2; freshness via 1st-touch) ranks fade quality
  MONOTONICALLY**: comp1 +$233 → comp2 +$538 → comp3 +$861/trade. Validates Seiden scoring; corrects the
  earlier "departure-alone doesn't predict" — the COMPOSITE does. Freshness is the heaviest weight (max 3).
- **DIVERSIFIER:** per-month Feb +15.9k / Mar +16.6k / Apr +5.9k / May +9.2k — April is the SMALLEST month,
  opposite of ignition/flow (which are ~half April). Real structural diversification (~0.06 corr).
- Hold-vs-break by touch: 1st 63% hold, 2nd 50%, 3rd survivorship-biased. Trade the BREAK (+5.85pt fwd
  raw) and the FLIP (+2.11pt fwd raw), don't fade the stale zone.
- CAVEATS: 90-100% "win%" is scale-out construction (read net$, not win%); flip never-loses (n=19) is
  small-sample/easy-scalp; 4 months, ~20-30/setup — magnitudes directional, signs clean.

## Next (the actual edge to build)
1. Add daily / 4H / weekly zones; test multi-TF confluence (30m zone inside a 4H/daily zone).
2. Build the strategy: limit entry into a **virgin** zone, stop beyond the zone, target the opposing
   zone (zone-to-zone, ≥2:1). Trade the **flip** as a second setup.
3. Use virgin-zone proximity to gate/target the ignition + reversion strategies (zones as targets/walls).
4. Gather more data to firm up the hit-rate (the one thing that needs volume, not cleverness).
