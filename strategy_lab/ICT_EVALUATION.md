# ICT ("Inner Circle Trader") — research & empirical evaluation (2026-07)

**Ask:** evaluate ICT and its variants as a trading strategy.
**Method:** literature review + EMPIRICAL TESTS of the testable claims on our own ES tape
(Feb–May 2025, both contracts, 5m/1m RTH bars; ±4pt race metric, conservative ambiguity,
same conventions as the rest of this project). Verdict at the end.

## 1. What it is
"ICT" is the methodology of **Michael J. Huddleston** (Inner Circle Trader), a forex educator
whose free YouTube "mentorships" made his vocabulary the dominant retail trading dialect of
2020–2025 (TikTok/YouTube/prop-firm-challenge culture). Claimed premise: an institutional
"algorithm" (IPDA) delivers price to engineered liquidity; retail stops are the target; time
windows matter. "Smart Money Concepts" (SMC) is the genericized fork of the same material.

**Core concepts:** fair value gaps (FVG), order blocks (OB), liquidity pools/raids (BSL/SSL),
market-structure shifts (MSS/BOS/CHoCH), premium/discount + OTE (62–79% retracement),
killzones (London/NY/close), power of three (accumulate→manipulate→distribute), judas swing,
breaker/mitigation blocks, SMT divergence (correlated-pair disagreement), turtle soup.
**Named variants/models:** the 2022 Model (sweep→MSS→FVG→entry), Silver Bullet (10–11am ET
FVG window), Unicorn (breaker+FVG overlap), Venom, Market Maker Buy/Sell Model, Asian-range Po3.

## 2. Lineage — the rebranding table
| ICT term | Prior art (decades old) |
|---|---|
| Order block | Supply/demand zone (Sam Seiden) — base→departure institutional footprint |
| Turtle soup | Linda Raschke, *Street Smarts* (1996), literally the same name |
| Liquidity raid / stop hunt | Stop-run / spring / upthrust (Wyckoff, 1930s) |
| Killzones | Session volatility profile (open/close U-shape, floor-trader lore) |
| FVG / imbalance | 3-bar gap folklore; "gaps get filled" |
| Premium/discount, OTE | Fibonacci retracement zones |
| MSS / BOS / CHoCH | Swing-point trend definition (Dow theory) |

## 3. Founder track record & epistemics
- Huddleston **failed his public 2016 "$10k→$1M in a year" challenge** and **blew up in the
  2024 Robbins World Cup** (the two occasions his trading was publicly verifiable). No audited
  track record exists; revenue is education.
- The narrative layer ("the algorithm hunts YOUR stops") is **unfalsifiable** — any outcome
  confirms it (filled = "rebalanced", not filled = "displacement", reversal = "manipulation").
- Concepts are defined **after the candle closes**; in live time multiple contradictory PD
  arrays coexist, so discretion decides — which is why "backtests" of ICT are mostly cherry-
  picked chart replays, and why no rigorous public evaluation existed. So we ran our own.

## 4. Empirical tests on OUR data (the part nobody does)
±4pt race, next 2h, conservative (ambiguous bar = loss; unresolved = loss); a 4pt scalp needs
**~57%** to clear the 0.517pt cost; coin-flip baseline ~50–52%.

| claim | test | n | win% | verdict |
|---|---|---|---|---|
| FVG retrace entry (core PD array) | enter at gap edge on first retrace, gap direction | 432 | **47%** | no edge |
| … + displacement filter (purist spec) | mid-candle ≥1.5× avg range | 49 | **45%** | no edge |
| … Silver Bullet window (10–11am ET) | same, entries in the window | 130 | **48%** | no edge |
| Turtle soup (fade PDH/PDL sweep) | breach ≤8pt then close back inside → fade | 56 | **38%** | INVERTED — sweeps continue |
| Judas swing at NY open (9:30–9:45 fakes) | open move reverses rest-of-day? | 69 | **36%** | INVERTED — opens continue |
| Judas (9:30–10:00 version) | | 70 | **41%** | inverted |

Consistent across both contracts. The two "manipulation" trades aren't just edgeless — they're
**backwards on this tape**: the PDH/PDL sweep *continues* (that's our validated zones-BREAK
setup, +$5.2k sized), and the opening move *continues* (that's our open-drive sleeve, +815 pts).
Also consistent with this project's earlier graveyard: single-level fading 44%, "fakeout from
weak break" ~50%.

## 5. What ICT gets right (because it was already true)
- **Killzones:** real. Our validated time-of-day structure (#9/#10): morning ignition hit 60.3%
  vs afternoon-counter 44%; open has 5–12× the directional flow content of midday. ICT's NY
  killzone is the session-vol U-shape with new packaging.
- **Order blocks:** the underlying pattern is real — as **Seiden supply/demand zones**, which we
  validated properly (88–93% virgin first-touch bounce; +$47.5k lifecycle sleeve). ICT's version
  adds vaguer selection rules to a concept that predates it.
- **Liquidity clusters at swing points:** stops do cluster; levels do matter (our zones/pivots).
  What fails is ICT's *directional* prescription (fade the sweep) — the tape says trade WITH it.

## 6. Verdict
**As a body of knowledge:** ~80% rebranded classics + an unfalsifiable manipulation narrative +
a discretionary layer that makes every loss the trader's "misreading" and every win the model's.
**As a testable strategy:** every distinctly-ICT entry we could operationalize is a coin flip or
inverted on 72 days of ES: FVG 45–48%, turtle-soup fade 38%, judas fade 36–41% — all before
costs. **As a business:** the founder's two public, verifiable performances were failures.

**For this project:** nothing to adopt — we already trade the *correct* versions of the parts
that work (zones = supply/demand with lifecycle; break-continuation instead of sweep-fading;
session timing; open-drive instead of judas-fading). If a specific ICT model with an exact
mechanical spec surfaces (e.g., a fully parameterized 2022 Model), it can be dropped into the
oracle harness in an afternoon — but on this evidence the burden of proof is on ICT.

**Untestable here (data limits):** midnight-open/Asian-range Po3 (no overnight session in our
RTH tables), SMT divergence (needs NQ tick data), London killzone (pre-8am ET not covered).
These could be tested after RecorderTee accumulates 24h data or NQ is added.
