# Higher-timeframe zone confluence study (2026-07)

**Question (user):** should the 30m zone sleeve require alignment with a higher-timeframe
(1h/4h/daily) zone? Does HTF confluence improve the fade/break/flip edge?

**Method:** reuse the VALIDATED zones oracle detection (`_detect_zones`) + trade walk
(`_walk`) unchanged, replicate the exact FADE/BREAK/FLIP enumeration on 30m, and tag every
trade by whether its 30m zone price-band overlaps an ACTIVE (formed-before, not-yet-broken)
**same-direction** zone on 1h / 4h / 1d. Both contracts (ESM5+ESH5), Feb–May 2025, $ P&L =
the oracle's own sizing + cost. Baseline reproduced EXACTLY (77 trades, +$47,502) → harness
is faithful.

## Results
| setup | bucket | n | win% | total $ | mean $ |
|---|---|---|---|---|---|
| **FADE** | baseline | 27 | 96% | +17,534 | +649 |
| | **confluent (1h/4h)** | **9** | **100%** | **+7,980** | **+887** |
| | non-confluent | 18 | 94% | +9,553 | +531 |
| | conf-4h only | 2 | 100% | +3,674 | +1,837 |
| **BREAK** | baseline | 31 | 90% | +5,180 | +167 |
| | **confluent** | **3** | **33%** | **−8,026** | **−2,676** |
| | non-confluent | 28 | 96% | +13,206 | +472 |
| **FLIP** | baseline | 19 | 100% | +24,788 | +1,305 |
| | confluent | 0 | — | — | — |
| **ALL** | confluent-ANY | 12 | 83% | −46 | −4 |
| | non-confluent | 65 | 97% | +47,548 | +732 |
| | conf-1h | 10 | 100% | +8,080 | +808 |
| | conf-1d | 0 | — | — | — |

## What it says (and doesn't)
1. **Confluence is SETUP-DEPENDENT — and the direction makes mechanical sense.**
   - **FADE** (trade the zone / with HTF support): confluence HELPS — 100% win, **+887 vs +531**
     mean. A 30m demand backed by a 1h/4h demand bounces better.
   - **BREAK** (trade AGAINST the zone): confluence HURTS — 33% win, −2,676 mean. Breaking a
     30m level that an HTF zone is defending is fighting the bigger level. A **contra-indicator**.
   - So the naive "only take confluent trades" filter is NET ≈ 0 (12 trades, −$4) precisely
     because it mixes the two — it helps one setup and poisons the other.

2. **1h confluence on fades is the cleanest positive** (10/10 wins, +808 mean) — but on **9–10
   trades over 4 months**. Directionally consistent, statistically thin.

3. **Daily confluence = 0 trades** in this window: the traded 30m zones never overlapped a
   daily zone's band (daily zones sit further out). Confirms the earlier caveat — daily
   confluence is **untestable** on 4 months, display-only for now.

4. **Ceiling caveat:** the oracle baseline is already 95%+ win (per-signal, generous walk — it's
   the ceiling, not the single-position engine's realistic number). Little headroom to "improve"
   win rate, so read the $/trade and direction, not the win%.

## Verdict / recommendation
**Do NOT wire a confluence filter into the algo on this evidence.** The one robust-looking
effect (1h/4h confluence lifts fades, contra-indicates breaks) rests on 9–12 trades — too few
to trust, and a blanket filter is net-negative. The honest move:
- **Keep the multi-TF zones as a VISUAL aid** (shipped) — they clearly help manual reads.
- **Hold the directional hypothesis** — "confluence backs FADES, warns against BREAKS" — and
  re-test once RecorderTee accumulates more live zone trades (the sample, not the method, is the
  limit).
- If we ever act on it: it would be a per-setup **weight** (size up confluent fades, never fade-
  filter breaks), not a global on/off gate. Not yet.

Script: `strategy_lab/zone_confluence.py` (reproducible; reuses the oracle).
