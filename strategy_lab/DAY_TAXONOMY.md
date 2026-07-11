# Day-type taxonomy — observation-first (task D)

Method per the user's mandate: do NOT assume archetypes and check them. Compute
the morning-state features that are **causally knowable by 10:00 ET and that the
data actually contains**, then OBSERVE what the day and the P&L do conditional on
that state. The 2025 research bars (`claude_bars_1m`) cover only 08:00–16:00 ET,
so there is no true overnight feature in Part 1; the 2026 recorded window has
full 24h and supplies the intraday cross-check.

Scripts: `day_taxonomy.py` (Part 1, sleeves), `day_taxonomy_user.py` (Part 2,
the user). Both write per-day CSVs.

---

## Part 1 — how the SLEEVES behave by morning state (2025, 66 days)

Median RTH range 86pt, median efficiency 0.05 (chop-dominated window).

**Finding 1 — the first 30-minute range is the dominant causal day axis.**
Bucketing by `f30_rng` (09:30–10:00 high-low, in ATR14 units), everything sorts:

| first-30m | RTH range (ATR) | portfolio $ | who earns |
|---|---|---|---|
| quiet (low third) | 0.62 | +3,400 | zones +9,029; trend sleeves negative |
| mid | 0.92 | +1,961 | opendrive +14,436; zones −13,691 |
| **wide (top third)** | **1.73** | **+69,744** | ignition +26,489, opendrive +28,230 |

The morning's first-30m energy predicts the day's energy **and ~85% of the
sleeve P&L**. Wide-morning days are trend-sleeve days; quiet-morning days are
zones days. This axis is orthogonal to GEX and, on this window, stronger.

**Finding 2 — first-30m direction persists to the close 72–73%.** A directional
first half-hour closes the same side ~72% of the time (mean |c−o| 48pt down-days,
68pt up-days) — a real trend-day tell, symmetric up/down.

**Finding 3 — the sleeves are day-type-complementary (why the portfolio diversifies).**
Zones makes money precisely where the trend sleeves bleed (f30-quiet +9,029;
gexMID +7,466) and loses where they win. The diversification is not statistical
luck — it is a regime/day-type split.

**Finding 4 — GEX reconfirmed** (independent of task B): gexLOW/short-gamma 48 days
carry +66k of the +68k trend-sleeve P&L; gexMID/HIGH trend sleeves flat/negative.
Gap-down opens (gapdn++) are big trend days (+34k port); exhaustion gap-ups
(gapup++) are portfolio losers (−9.5k).

---

## Part 2 — how the USER behaves by day type (manual ES, 186 days, +$285k)

**Headline — the user is a long-gamma dip-buyer / mean-reverter, and it is robust.**

| prior-day GEX regime | days | win% | median $ | total $ | robustness |
|---|---|---|---|---|---|
| **gexHIGH (long-gamma, pinning)** | 69 | 54% | **+800** | **+321,437** | +194,775 after dropping best 3 days — BROAD |
| gexMID | 66 | 42% | −25 | +21,100 | lumpy, ≈noise (top-5 days > total) |
| gexLOW (short-gamma, trending) | 51 | 49% | 0 | −57,153 | median $0; +104,220 ex-worst-3 — a TAIL, not a broad loss |

The user earns a broad, median-positive edge when dealers pin (long gamma) and
carries **left-tail risk** when dealers amplify (short gamma) — exactly the
signature of dip-buying: it works into mean-reversion and occasionally gets run
over by a trend that does not bounce. This is the **opposite regime** to the
engine's trend sleeves, and the **same regime** as the zones / band-retest sleeve.

**The July 9 stand-down is real.** On 2026-07-09 the user placed **2 fills** vs
30–100 on every surrounding recorded day — a genuine stand-aside, and it was a
short-gamma day (gexp_prev 0.27), the regime that carries their tail risk. One
point, but it fits the pattern precisely.

**Participation, not selection.** The user trades essentially every session; they
express "stand-down" by collapsing fill count (≈30 → 2), not by skipping the day.
So the gap vs the engine is **how** they trade a day (dip-buy toward levels, size,
tail-avoidance), not **whether** they show up.

**Flags (more data needed, NOT acted on):** Monday is the money day-of-week
(+192k, 61% win, median +2,575) with Tuesday negative (−42k); big gap-downs are
bought (80% win) and big gap-ups faded/lost. Both are observations, not yet edges.

---

## Synthesis → where this points the engine (pre-registered, nothing deployed)

1. **The user and the trend sleeves are regime-complementary.** The single
   highest-value engine direction is to lean into the sleeve that matches the
   user's proven edge — the long-gamma, dip-buy, mean-reversion sleeve
   (zones / band-retest) — and keep the GEX-gated trend sleeves as the
   short-gamma complement. The engine has been over-weighted to trend.

2. **Pre-registered hypotheses to test forward (anchor/holdout discipline):**
   - *first-30m-range gate* for the trend sleeves (entries only when f30_rng ≥
     ~1 ATR). In-sample it captures ~all their P&L; must be validated on
     recorder days before deployment (terciles here are in-sample).
   - *long-gamma dip-buy sleeve* modeled on the user's band-retest entries, gated
     to gexHIGH, with an explicit **short-gamma tail guard** (their one weakness).
   - *Monday / gap-down-buy* as day-of-week and gap flags — collect more data.

3. **Do not deploy any of these yet.** Part 1 terciles and Part 2 buckets are
   in-sample; the value here is the DIRECTION (the user's edge is long-gamma
   mean-reversion), which is robust, not the specific thresholds.
