# NinjaTrader 8 relay (EngineRelay)

Reference NinjaScript that bridges NT8 ↔ the Python engine over two local sockets
(the JSON protocol in `engine/adapters/protocol.py`). All NT-specific code lives here;
the Python core is untouched.

## Install
1. NT8 → **New → NinjaScript Editor**. Add `MiniJson.cs` and `EngineRelay.cs` as new
   Strategy files (or paste into the editor) and **Compile** (F5).
2. Open an **ES** chart on your **SIM** account. Add the **EngineRelay** strategy;
   set **Calculate = On each tick**. Enable it. It logs `market:36001 broker:36002`.
3. (For L2 `BookFlow`) ensure the data series provides market depth.

## Wire the engine
```python
from engine.adapters.feeds.ninjatrader import NinjaTraderFeed
from engine.adapters.brokers.ninjatrader import NinjaTraderBroker
feed   = NinjaTraderFeed("127.0.0.1", 36001, symbol="ES")
broker = NinjaTraderBroker("127.0.0.1", 36002, account="Sim101", symbol="ES")
# LiveEngine(feed, broker, [IgnitionStrategy(...), ...], WallClock(), Blotter("ES", 50))
```
Fills/positions render natively in the NT8 Control Center + chart. Route the same engine to
`QuantowerBroker` (port 36003) to compare the two front-ends.

## Protocol (line-delimited JSON, epoch-ns timestamps)
- market (relay→engine): `trade` / `quote` / `depth`  → engine aggregates per-second `BookFlow`
- broker (engine→relay): `place` / `cancel` / `modify`
- broker (relay→engine): `fill` / `position` / `account`

## Caveats / to verify in NT8
- **Aggressor** is inferred (last vs bid/ask); NT L1 doesn't tag it.
- **Timestamps** use `e.Time` → UTC epoch ns; confirm your feed's clock.
- **`modify`** needs the order's Limit/Stop set before `Account.Change` (left as a TODO in the reference).
- Test with the Python **loopback** first (`tests/test_live_loopback.py`) — it validates the whole
  path with fake sockets, no NT8 needed.
