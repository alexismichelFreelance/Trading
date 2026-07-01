# Phase 2 — NinjaTrader 8 & Quantower bridge integration plan

**No live work until the Phase-1 parity gates pass** — they do (A exact; B +990.6/+1004;
C +$47,502/+$47,502; D +811/+814). Phase 2 wires the *same* Strategy objects to live feeds/brokers.
All platform-specific code is isolated inside the adapters; `engine/core/` stays pure Python.

## Principle
The core speaks one normalized language (`Trade/Quote/DepthUpdate/Bar/BookFlow`, `Fill/Position/Account`).
A bridge's only job is to translate a platform's wire format ↔ these events. The strategies, features,
sizing, blotter, and cost model are untouched from replay.

## Lowest-friction transport: a thin relay over a local TCP socket
Both NinjaTrader and Quantower run .NET add-ins in-process with the platform and can open a local socket.
The friction-minimizing design is a **line-delimited JSON relay on `127.0.0.1`**:

- **Market channel** (platform → engine): the add-on subscribes to trades / best bid-ask / DOM and emits
  one JSON object per update. The Python adapter (`NinjaTraderFeed` / a Quantower feed) reads the socket
  and maps each message to a normalized event.
- **Order channel** (engine → platform → engine): the adapter sends `place/modify/cancel` JSON; the add-on
  submits them to the **SIM account** via the platform's order API and relays `execution/order/position/
  account` messages back, which the broker adapter maps to `Fill/PositionUpdate/AccountUpdate`.

JSON-over-socket keeps the C#/.NET shim tiny (subscribe, serialize, submit, serialize-back) and language-
agnostic; no FIX, no DLL interop. Swap to a faster codec later if latency matters (the NT feed is 40-min
delayed, so it won't for evaluation).

## BookFlow parity (the important bit)
The strategies read per-second **gross resting-book add/cancel flow** (`BookFlow`). Live, the feed adapter
reconstructs it by **aggregating DOM (MarketDepth) add/remove deltas per second** — exactly how the research
built `bid_add/ask_add/bid_cancel/ask_cancel` from the raw L3 `mbo_events`. This is the one derived input; do
it in the adapter so the strategy sees identical `BookFlow` events live and in replay.

## NinjaTrader 8
- **Add-on:** a NinjaScript `AddOnFramework` (or an indicator/strategy host) that (a) subscribes via
  `BarsRequest` / `MarketData` / `MarketDepth`, (b) relays market JSON, (c) accepts order JSON and submits
  through `Account.CreateOrder` / `SubmitOrder` on the **Sim101** account, (d) relays `OnExecutionUpdate` /
  `OnOrderUpdate` / `OnPositionUpdate`.
- **Adapters:** `adapters/feeds/ninjatrader.py` (`NinjaTraderFeed`), `adapters/brokers/ninjatrader.py`
  (`NinjaTraderBroker`) — both stubbed with the exact TODOs.
- **Delayed feed:** the free feed is ~40 min delayed; the `EventClock` already makes timeouts/sessions
  event-time, so delayed data replays correctly. Watch fills/positions render in the NT8 Control Center + chart.

## Quantower
- **Connector:** a Quantower add-on (its .NET API / algo plugin) mirroring the same JSON socket protocol —
  subscribe to quotes/trades/DOM, submit to a Quantower **SIM** account, relay executions/positions.
- **Adapter:** `adapters/brokers/quantower.py` (`QuantowerBroker`) — same protocol, so only the connector
  class differs. This is how the two front-ends are compared side-by-side on the same engine + strategies.

## RecorderTee (forward-only data library)
`adapters/feeds/recorder_tee.py` wraps any live feed, yields events unchanged, and batch-writes them to
QuestDB (tables mirroring `claude_sec_feat` / `claude_bars_1m`). Use the project's proven fire-and-forget
INSERT + per-day count verification to dodge the WAL-commit race. Forward-only: stop re-buying history.

## Live engine driver (to add)
`ReplayEngine` is single-task and deterministic (event-time). Phase 2 adds a `LiveEngine` that concurrently
consumes `feed.stream()` and `broker.events()` with `asyncio` (e.g. merge via a queue), using `WallClock`,
with reconnection/backoff on both sockets and a heartbeat. The dispatch/among-strategies logic is identical
to `ReplayEngine._dispatch` / `_drain`; only the sourcing of events becomes concurrent.

## Test / evaluation plan
1. Loopback test the relay (a fake socket server emitting canned market JSON) — no platform needed; assert
   the adapter yields the right normalized events and `BookFlow` aggregation matches.
2. Paper session on NT8 delayed feed → `NinjaTraderBroker` (Sim101): run ignition+flow+zones, watch the
   blotter vs the NT8 UI.
3. Repeat routing to Quantower SIM; compare the two UIs (fills, DOM, position, latency-of-display).
4. Turn on `RecorderTee`; verify per-day row counts land in QuestDB and re-replay reproduces the session.
