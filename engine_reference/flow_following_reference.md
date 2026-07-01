# Reference: flow-following (Gate D) — exact spec, parity +814 pts

Continuous-tape strategy. Holds a position proportional to a thresholded windowed aggressor-flow signal,
self-netting/reversing, with an **asymmetric hold band** (easy to add to a position, hard to reverse it).
Parity target is reported in **points** (unsized, per-unit), **net of turnover cost**: **+814 pts** on
ESM5+ESH5 (Feb–May 2025), all months positive. Runs on **1-second** rows from `claude_sec_feat` /
`claude_sec_feat_esh5`: fields `ts, day, pxc` (price), `adelta` (signed aggressor delta this second).

## Constants (the +814 config)
```
W      = 120      // flow window, seconds
TH     = 200      // per-second |adelta| threshold (ignore seconds quieter than this)
SCALE  = 3000     // flow → target-position divisor
MAXP   = 50       // target-position clamp (± contracts, abstract units)
a      = 1        // ADD band  (grow/reduce a position in its own direction)
h      = 5        // HOLD/FLIP band (cut toward zero or reverse) — must be >= ~4
COST   = 0.30     // per unit of turnover (|Δheld|), in points
```
`a < h` is the whole point: the position adds on small signal moves but only reverses on large ones.
Any `h` in the plateau (≈4–6) reproduces +814; use `h=5`.

## Algorithm (per day; reset state at each new `day`)
```
for each day D (rows in ts order, price p[], adelta d[], length n):
  F = 0;  buf = [] (FIFO of length<=W);  held = 0
  for i in 0 .. n-1:
     x = (|d[i]| >= TH) ? d[i] : 0          // threshold: only decisive seconds count
     buf.push(x);  F += x
     if buf.length > W:  F -= buf.shift()   // rolling W-second sum of thresholded signed flow
     tgt   = clamp( F / SCALE, -MAXP, +MAXP )
     delta = tgt - held
     band  = (held == 0) ? a
                          : (sign(delta) == sign(held) ? a : h)   // adding -> a ; cutting/reversing -> h
     if |delta| > band:
        nv   = round(tgt)
        turn += |nv - held|                 // turnover accumulates the size actually traded
        held = nv
     if i < n-1:  pnl += held * (p[i+1] - p[i])   // mark-to-market the held position over next second
  // end of day: held is dropped to 0 at the next day's open (flat reset; no overnight)

net_pts = sum_over_days(pnl)  -  turn * COST
```

## Notes that matter for parity
- **Threshold is on the raw per-second `adelta`**, applied *before* the windowed sum (seconds with
  `|adelta| < 200` contribute 0 to `F`, not their actual value).
- `held` is an **integer** (`round(tgt)`); turnover counts integer position changes.
- The **band gate** compares `|tgt - held|` to the band; when it fires, jump `held` all the way to
  `round(tgt)` (not a 1-unit step).
- **Flat reset each session** — do not carry `held`, `F`, or `buf` across the day boundary.
- P&L convention: position held during second `i` earns `p[i+1]-p[i]`. The final second of the day earns
  nothing (no next price); the day-end position is closed at no extra cost (its turnover was already
  counted when it was opened, and the flat happens at next open in `pnl` terms it just stops marking).
- Cost is **0.30 pt per unit of turnover** (one-way), i.e. `turn * 0.30`. The +814 = gross ≈ +829 − (turn≈50)×0.30.

## Per-side sanity (should reproduce)
All four months net positive; ESM5 (Apr/May heavier flow) carries most of it, ESH5 (Feb/Mar) positive but
smaller. If a month goes negative, suspect (a) threshold applied after the window instead of before, or
(b) state not reset at the day boundary, or (c) `held` not integer-rounded.

## Optional enhancement (NOT the parity target)
The **scale-in** variant lifts this to **+1084 pts**: when the signal's sign persists (a run of same-sign
`tgt` for ≥ R consecutive seconds), allow `MAXP` to step up (e.g. ×1.5) so winners press. Keep this OFF for
the Gate-D parity test (+814); add it only after parity passes, as a separately-gated improvement.
