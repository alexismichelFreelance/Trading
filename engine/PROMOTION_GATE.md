# Promotion gate — what it takes to route a sleeve to real money

`GO_LIVE.md` covers running the engine in **paper**. This covers the decision
after that: when a sleeve is allowed to trade **real money**, at what size, and
— just as importantly — when it is taken off automatically.

Written 2026-07-26, deliberately at a moment when **no sleeve qualifies**. That
is the point. Promotion criteria written while looking at a tempting result are
criteria written to admit that result.

Every threshold below traces to something that actually went wrong in this
project, cited inline. None of them are round numbers picked for feel.

---

## Why a written gate at all

The engine has never placed a live order. Meanwhile the manual account makes
real money. So the temptation is always to route *something* live to close that
gap, and the moment a sleeve posts a good week the argument for doing it will
feel strong. The failures below all looked convincing at the time:

| What happened | Why the eye missed it |
|---|---|
| `pivot` produced **0 orders in 34 sessions** | a dead sleeve and a signal-less sleeve look identical from outside |
| `flow`, `flow_gex` also **0 orders** | `adapt_k=4.0` sat one step past a cliff |
| `opendrive_orb` built from **one day**, repeated the same −24.75pt loss the next | fitting to a single vivid session |
| sweep-**follow** +0.58pt/contract, replicated — but **loses in all 20 cells** as a real sleeve | impact measurement ≠ tradeable edge |
| sweep long-horizon edge on ESM5, **sign-flipped** on ESH5 | one contract, one regime |
| `pivot`/`flow` thresholds calibrated on **the same capture** used to judge them | in-sample by construction |

---

## The gates

A sleeve must pass **all ten**. `tools/promotion_check.py` evaluates G1 and
G5–G9 mechanically from the paper record; G2–G4 are declarations recorded in
`config/promotion.yaml` and are checked for *presence and consistency*, not
taken on trust.

### G1 — Alive
`tools/sleeve_audit.py` verdict is **OK**, firing on **≥10%** of captured
sessions, with **zero** exceptions raised.
> Three sleeves sat silently dead. "It didn't trade" must be a test result, not
> a discovery.

### G2 — Not fitted on its own evidence
The declared calibration window **must not overlap** the evaluation window.
Any sleeve whose parameters were chosen by a calibration tool must name that
tool and the data range it used.
> `ER_MIN_NORM` and `FLOW_K` were both picked on the same capture later used to
> score them. Those numbers are optimistic by construction and cannot count.

### G3 — Replicated out of sample
Same sign, and median within **50%**, on a **second contract or a second
period** not used in construction.
> Sweep-follow's long horizons were beautiful on ESM5 and negative on ESH5.
> One contract in one regime is an anecdote.

### G4 — Cost-honest
Evaluated with **pessimistic** execution: crossing the spread on **both** entry
and exit, plus commission. A sleeve that needs passive fills must say so
explicitly and is capped at the smallest size tier until live fills prove it.
> The sweep edge was +0.078pt/contract against a ~0.125pt exit — real, and
> smaller than the cost of taking it. Passive-exit assumptions are where
> backtests go to lie, because you get filled precisely when you're wrong.

### G5 — Sign consistency ≥ 70% of sessions
Not the mean. The share of sessions whose P&L has the same sign.
> The flow feature panel had respectable ICs at 52–63% sign consistency. That
> is noise with good days. 70% is the lowest bar at which a sleeve's direction
> is a property rather than a coincidence.

### G6 — Mean **and** median both positive
Both, over the evaluation window.
> `span>=10/60s` showed median **+0.75** with mean **−1.76**: many small wins
> and occasional large losses. Median alone hides a short-volatility tail;
> mean alone hides a lottery. Requiring both kills both disguises.

### G7 — Forward paper record ≥ 40 sessions
Sessions the sleeve traded **live-paper**, not replayed. Replay evidence never
substitutes; it only qualifies a sleeve to *start* accumulating this.
> ~2 months. Long enough to span more than one regime, short enough to be
> reachable. Replay cannot capture feed gaps, restarts, latency, or the
> engine's own bugs.

### G8 — Worst session ≥ −3× median daily P&L
The worst single paper session must not exceed three times the typical daily
gain, in loss.
> A short-vol payoff earns steadily and then gives it all back. This bounds
> how much one bad day can undo, and it is the gate the resting-bracket idea
> would have failed.

### G9 — Beats its control twin
Where a twin exists (`raw` vs `_gex`, `sweepfade` vs `sweepfollow`), the
promoted variant must beat it on G5–G8 over the same window.
> Twins exist so the comparison is measured rather than argued. On NQ the
> `_gex` twins were byte-identical duplicates for weeks — a twin that isn't
> actually different proves nothing.

### G10 — Risk configuration committed before the first live order
Per-sleeve dollar cap, max position, daily loss limit, and kill-switch
threshold set in config and verified by `RiskSupervisor` — not defaults.
> The 2026-07-09 runaway is why in-flight vetting exists. Size limits are
> decided before there is money on them, or they are decided under pressure.

---

## Size ladder

Promotion is to a **tier**, never straight to target size.

| Tier | Size | To advance |
|---|---|---|
| T0 paper | 0 | the ten gates above |
| T1 | 1 contract | 20 live sessions, G5–G8 still hold **on live fills** |
| T2 | 2 contracts | 40 live sessions, live median within 50% of paper median |
| T3 | review | explicit decision, not automatic |

Live fills are the only evidence that counts at T1+. If live diverges from
paper by more than the gap between them can explain, the sleeve is answering a
different question than the backtest was.

---

## De-promotion — automatic, pre-committed

Most of the value here. A sleeve is **flattened and de-routed immediately**,
without discussion, on any of:

1. **Daily loss** beyond its configured limit (existing kill switch).
2. **Cumulative live loss** > 2× its worst paper session.
3. **Live-vs-paper divergence**: live median P&L outside 50% of paper median
   over any 20-session window — the sleeve is not doing what was measured.
4. **Stops firing**: zero orders in 10 sessions when the audit says it should
   fire (silent death, live this time).
5. **Burst**: order count or position beyond the supervisor's per-sleeve caps.

Re-promotion after de-promotion starts at **T0** and requires the full gate
again. There is no "it was just a bad patch" path, because that is exactly what
a broken sleeve looks like from inside.

---

## Who decides

The gate is necessary, not sufficient. Passing it makes a sleeve **eligible**;
routing real money is always an explicit human decision. Nothing in this repo
should ever promote a sleeve automatically.
