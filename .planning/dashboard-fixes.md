# Dashboard fixes — long-running plan

Source: user review of the live dashboard (2026-10-22). Each item maps to the
code that owns it. Phases are ordered by dependency and by what blocks the
others; within a phase, tasks are independent unless noted.

## Phase 0 — Baseline (done)
- Source work committed in e9e8f69 / 8fa4b1b / 8103f8f; branch pushed.
- 95 targeted tests green; live (8787) + demo (8788) verified.

## Phase 1 — VWAP anchored to the Globex session (engine core)
**Why:** `LevelBook.vwap` is a plain `AnchoredVWAP` fed every 1m bar and never
reset, so the line is cumulative since process start, not a session VWAP.
**Files:** `engine/engine/features/avwap.py`, `engine/engine/features/level_book.py`,
`engine/engine/adapters/state_sources.py` (`_vwap_bands`), tests.
**Task:** Add a `SessionVWAP` (or reuse `MultiAVWAP` with a globex anchor) that
resets at the Globex session open (18:00 ET). `LevelBook.on_bar` must call
`vwap.add` only after checking the session key, resetting on a new Globex day.
`_vwap_bands` already reads `.value`/`.sigma`/`.band(k)` — no change needed
there. The dashboard book is fed bar-by-bar by `seed_dashboard_lane`, so a
per-session reset produces a moving per-session series automatically.
**Risk:** changes trading semantics (VWAP is a level). Must keep `value`/`sigma`
NaN-safe. Add unit test mirroring `test_avwap.py`.
**Acceptance:** on a multi-day chart the VWAP line resets at each Globex open;
bands track; engine's `pullback_to` still works.

## Phase 2 — Gamma per-day on the chart + labels
**Why:** `indicators_from_levelbook` stamps gamma at `_session_ts(bar_ts)` and the
client's `drawStaticLines` keeps only the MAX ts, so one wall set spans the whole
window. Also no labels.
**Files:** `engine/engine/adapters/state_sources.py`, `engine/dashboard/app.js`
(`drawStaticLines`, `drawBoundedLevel`, `renderGamma`), `engine/tools/run_dashboard.py`.
**Tasks:**
  2a. Keep gamma history per session in the store (already accumulated by ts).
  2b. Client: group gamma indicator points by ts; draw each session's walls
      bounded to that session's bar range (start = session open, end = next
      session open), instead of only the latest.
  2c. Add labels: use LWC `createPriceLine` with `title` for the current
      session's walls (axis label), and a faint bounded line for prior days.
  2d. Refresh dashboard `src.gamma_curve` on rollover (in `_feed_dashboard_book`)
      so multi-wall map updates daily.
**Acceptance:** walls change price each day; each wall labelled put/call/flip;
current-day walls labelled on the axis.

## Phase 3 — Pivot points D/W/M with labels
**Why:** `LevelBook` uses `SessionLevels` (daily only). `MultiPivots` (D/W/M)
exists but is not wired in or published.
**Files:** `engine/engine/features/level_book.py`, `engine/engine/adapters/state_sources.py`
(`indicators_from_levelbook`), `engine/dashboard/app.js`.
**Tasks:**
  3a. Add `LevelBook.mpiv = MultiPivots()` and call `mpiv.update(ts, h, l, c)`
      in `on_bar` (on every bar, not RTH-only, per MultiPivots contract).
  3b. Publish `levelbook:mpivots` series: one point per level per session ts,
      carrying a `key` like "D-PP" so the store keeps all levels (not deduped
      by ts-v). Need to extend `indicators_from_levelbook` to emit the grid
      with labels.
  3c. Client: render each pivot level as a bounded line spanning the session,
      with the label (D-PP, W-R1, …) shown. Use a color per timeframe.
**Acceptance:** daily pivots change each day, weekly each week, monthly each
month; labels visible.

## Phase 4 — Zones as ranges + lifecycle (broken/flipped)
**Why:** `zones_from_view` filters out broken zones; zones drawn as two edge
lines (top/bot) which is a range, but no fill and no broken/flipped styling.
**Files:** `engine/engine/adapters/state_sources.py` (`zones_from_view`),
`engine/engine/features/zones.py`, `engine/dashboard/app.js` (`drawZones`).
**Tasks:**
  4a. Decide whether to publish broken zones (probably yes, marked broken) so
      the chart can show them faded. Update `zones_from_view` to include broken
      with `broken=True`.
  4b. Check `zones.py` for a "flipped" state; if present publish `flipped`.
  4c. Client: draw each zone as a filled band (top↔bot) with opacity by status
      (virgin bright, touched faded, broken dashed/grey, flipped color).
**Acceptance:** zones appear as ranges; broken/flipped visually distinct.

## Phase 5 — Indicator show/hide toggles + solo view
**Why:** Strategies have toggles; indicators do not. Also want "show only this".
**Files:** `engine/dashboard/index.html`, `engine/dashboard/app.js`, `style.css`.
**Tasks:**
  5a. Add an "indicators" drawer listing: VWAP, VWAP bands (2s/3s), pivots
      (D/W/M), gamma walls, zones, volume.
  5b. Per-item checkbox toggles visibility (add/remove series or price lines).
  5c. "Solo" button: hide all others, show only the selected indicator.
  5d. Persist choices in localStorage.
**Acceptance:** each indicator can be hidden/shown; solo works.

## Phase 6 — Strategy trade vs display + solo
**Why:** Roster `enabled` flag conflates trading with display. Need both.
**Files:** engine strategy base (`enabled`), `state_sources.strategies_roster_from`,
`app.js` roster, command channel (new).
**Tasks:**
  6a. Client: separate "trade" toggle (sends command to engine) from "show"
      toggle (client-only). Trading toggle requires a WS command path
      (engine must accept `op: set_enabled`). Add minimal command handling
      in `StatePublisher`/`Dashboard._ws` and the engine.
  6b. Solo view for strategies (show only one).
**Acceptance:** toggling trade changes engine.enabled; toggling show changes
chart overlays; solo works.
**Note:** 6a is the only item requiring a new command channel; defer if
read-only scope is preferred.

## Phase 7 — Auto-scroll toggle + "go to latest"
**Why:** `followEnd()` always re-anchors; user wants to disable while browsing.
**Files:** `engine/dashboard/app.js` (`followEnd`, `updateBar`), `index.html`.
**Tasks:**
  7a. Add `_autoScroll` flag (default true). `followEnd` only re-anchors when
      true AND view is at right edge.
  7b. Add a "latest →" button that re-enables auto-scroll and jumps to end.
  7c. When user scrolls left, disable auto-scroll (detect via wheel/scroll).
**Acceptance:** scrolling back stops auto-follow; button restores it.

## Phase 8 — Scroll back / load previous days
**Why:** Client only shows the seeded window; no infinite scroll.
**Files:** `engine/dashboard/app.js`, `engine/tools/run_dashboard.py` (`_history`).
**Tasks:**
  8a. Detect when view scrolls near the left edge; fetch older bars via
      `/history?start=...&end=...` and prepend (merge + dedupe).
  8b. Fetch matching indicator history (or recompute VWAP from bars).
  8c. Avoid duplicate fetches; show loading indicator.
**Acceptance:** scrolling left loads older sessions; VWAP/zones follow.

## Phase 9 — RTH background shading (configurable)
**Why:** User wants RTH shading for UK/Europe/US sessions.
**Files:** `engine/dashboard/app.js`, `index.html`, `style.css`, config.
**Tasks:**
  9a. Define session windows (RTH per region) in a config object.
  9b. Render shading: LWC has no native time-based background. Use a
      histogram/area series on a separate scale, or a custom plugin. Simplest:
      draw vertical bands via `createPriceLine`? No. Use a second chart pane?
      Alternatively, shade via canvas overlay aligned to the time scale.
  9c. Make sessions configurable (checkboxes).
**Acceptance:** RTH periods shaded; configurable.
**Note:** LWC v4 lacks time-based background shading; may need a canvas overlay
or a plugin. Defer if too heavy.

## Phase 10 — Multiple put/call walls visible
**Why:** User sees only one of each. Code already publishes up to 5 ranked
walls from the curve; flat fallback publishes one. Live may be using the flat
fallback if `gamma_curve` is None.
**Files:** `engine/tools/run_live.py` (gamma_curve detection), `engine/dashboard/app.js`
(`renderGamma`).
**Tasks:**
  10a. Verify `gammacurves[sym]` is non-None in live (wall_fade in roster).
  10b. Ensure `renderGamma` draws all `call_walls`/`put_walls` (it does).
  10c. Label each wall with its rank/price.
**Acceptance:** multiple walls drawn and labelled when curve is available.

## Phase 11 — Strategy entries/exits display
**Why:** `strategies_roster_from` publishes entry/target/stop from `chart_marks()`.
Live strategies may not implement `chart_marks()` → no marks.
**Files:** `engine/engine/strategies/base.py`, strategies, `state_sources.py`.
**Tasks:**
  11a. Audit which strategies implement `chart_marks()`; add it to the base
      class default (return {}).
  11b. For strategies with open positions, publish entry/avg from blotter
      (already done in `positions_from_strategies`).
  11c. Client already draws strategy marks when present.
**Acceptance:** open positions show entry/target/stop lines.

## Phase 12 — NT8 risk profile / instrument mismatch
**Why:** Order submitted on the chart's specific contract (e.g. ES DEC 26) has
no risk profile; only "ES" does.
**Files:** `engine/bridges/ninjatrader/EngineRelay.cs` (CreateOrder),
`engine/tools/run_live.py`.
**Tasks:**
  12a. Option A: user adds a risk profile for the specific contract in NT8.
  12b. Option B: relay uses `Instrument.MasterInstrument` for the risk check
      (not recommended — trades master, not contract).
  12c. Code: make the relay log the exact instrument name used and the NT8
      error, so the user can add the right profile. Also verify `sub` symbol
      matches.
**Acceptance:** either a profile exists for the traded contract, or the engine
fails with a clear message naming the instrument.

## Phase 13 — Zones transmission verification
**Why:** Zones may not be transmitted/displayed correctly.
**Files:** `engine/engine/adapters/state_sources.py`, `engine/dashboard/app.js`.
**Tasks:**
  13a. Verify `zoneview` is passed to `LaneSource` in live (it is, via
      `zoneviews.get`).
  13b. Verify zones appear in snapshot (test).
  13c. Client `drawZones` already renders top/bot lines per zone.
**Acceptance:** zones visible; count matches engine.

## Test plan
- Run targeted subsets only (full suite hangs on live-feed tests).
- Add tests for VWAP session reset, gamma multi-wall count, pivot D/W/M,
  zones broken flag, indicator toggle (client-side, manual).
- Manual browser verification via canvas sampling + DOM probes.

## Execution order
1 → 10 → 11 → 13 (engine data correctness, low risk)
2 → 3 → 4 (chart overlays, medium risk)
5 → 6 → 7 (UI, low risk)
8 → 9 (heavy, defer)
12 (config/NT8, separate)
