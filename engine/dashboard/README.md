# es-engine dashboard v1 (inspect-first, engine-embedded)

A local web dashboard served BY the engine process itself. It binds the
state-publish adapter's transport-free contract to a single WebSocket that
carries live state to the browser, where TradingView lightweight-charts renders
it. Same socket becomes the future control channel; for v1 it only
subscribes/re-configures windows (no strategy controls).

Fixes the user's gripes: invisible indicators, invisible errors, geometrically
wrong zones, no dark theme.

## Files

  tools/run_dashboard.py            THE server (embedded aiohttp + WS + demo engine)
  dashboard/index.html              single-page UI
  dashboard/app.js                  chart rendering + WS client
  dashboard/style.css               dark theme
  dashboard/vendor/lightweight-charts.standalone.production.js   vendored v4.2.3

## Vendored assets (offline-friendly)

  dashboard/vendor/lightweight-charts.standalone.production.js
    TradingView Lightweight Charts v4.2.3, Apache-2.0.
    Fetched once into the worktree so the UI works with no internet.
    Source: https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js
    (download scripted rather than committed in a package manager so the
    dashboard has zero node/npm dependency — it's plain files served by aiohttp.)

  Python runtime dependency added to the engine venv: `aiohttp`
    (.venv/Scripts/pip install aiohttp) — local, free, no cloud.

## How to launch

  # from the engine worktree root (D:/HermesHome/worktrees/ms-dashboard/ms-dashboard/engine):
  .venv/Scripts/python.exe tools/run_dashboard.py --port 8787

  # open as many windows as you like, one per tab:
  #   http://127.0.0.1:8787/?symbol=ES&tf=5m
  #   http://127.0.0.1:8787/?symbol=NQ&tf=1m
  #   http://127.0.0.1:8787/?symbol=GC&tf=4h
  # Each tab is its own window (symbol + timeframe). Change either in the
  # topbar and it reconnects scoped to that selection; the ↗ button copies the
  # window's link.

  Options:
    --host 127.0.0.1      bind address (localhost only, per hard rule)
    --port 8787
    --lane ES             repeatable; default ES,NQ,GC
    --tf 5m               aggregate timeframes; default all standard
    --tick-s 0.5          demo: seconds per 1m bar per lane
    --live                (see note below)

## Demo vs live

  Default run is `--demo`: it drives the REAL engine feature pipeline (real
  LevelBook, real ZoneView, real Blotter, real BarAggregator, real
  ZoneDetector, real AnchoredVWAP) from a synthetic-but-plausible 1m tape, and
  publishes the exact state-publish schema. Indicators/zones/positions are the
  engine's computed values — never re-derived in JS. Use it to inspect the UI
  without NT8/QuestDB.

  `--live` attaches the same StatePublisher to the engine's NT8 feed. Wiring
  the NT8 loopback is the ms-single-socket prerequisite's area (do not touch),
  so this build keeps that path as a clearly-marked hook; the transport/UI
  layer is identical. To surface the live tape, add the StatePublisher from
  run_live.py's engine to the same Dashboard server.

## WS protocol (JSON)

  client -> {op:"subscribe", symbol, tf}   (optional; default from querystring)
  client -> {op:"ping"}
  server -> {type:"hello", lanes:[...], timeframes:[...], symbol, tf}
  server -> {type:"snapshot", symbol, tf, payload:{bars, indicators, zones,
                                                  positions}, log, errors, seq}
  server -> {type:"delta", symbol, kind, payload}
                kind in {bar, indicator, zone, position, fill, log, error,
                         session, lane}
               - bars delivered for the window's tf only (raw 1m under '1m');
               - indicator/zone/position/fill deltas are per-lane;
               - log/error/session/lane deltas are global (delivered to all).

Bars/indicators/zones/positions match the state-publish schema (see
engine/features/state_store.py). All time in epoch-ns (UI converts to epoch-s
for light-charts).

## Verifying (what the PR runs in CI / local)

  - Server starts:  .venv/Scripts/python.exe tools/run_dashboard.py --port 8787
  - WS smoke test:   python - <<'PY' (connects, gets hello+snapshot, reads live
    deltas) — see tools/run_dashboard.py --help and the module docstring.
  - Browser: open the URL; clock shows CHART RENDERING confirmed by
    candlesticks + translucent zone bands + indicator lines + positions on a
    dark background; engine log streams live and WARNING/ERROR lines surface
    in red with a counter badge.

## Notes / hard rules honored

  - Consumes the state-publish adapter's schema; zones/indicators drawn from
    ENGINE state only (LevelBook/ZoneView), never re-implemented in JS.
  - No new paid infra, no cloud, localhost only.
  - Engine core stays pure; this server/UI is the dirty adapter layer.
  - DB/QuestDB and NT8 connection code are NOT touched (that's the
    ms-single-socket prerequisite's area).
  - Paper trading only; nothing sent to NT8, nothing deployed.
