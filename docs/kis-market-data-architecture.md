# KIS Market Data Architecture

## Decision

The new engine must not call KIS directly from algorithms, alpha models, universe selection, or execution logic.

KIS access goes through the local broker-engine gateway.

```text
new engine core
  -> MarketDataProvider port
  -> KIS broker-engine adapter
  -> local broker-engine
  -> KIS
```

This mirrors the most important StockProgram lesson: one local broker-facing process should own KIS credentials, token reuse, websocket approval, request pacing, broker errors, and broker-specific payload normalization.

## Why Broker-Engine Is Mandatory

KIS has a low request throughput budget. Treat the AppKey as a shared rate-limited lane. The practical operating rule is:

- never let multiple local processes independently spend the same KIS quota
- never let alpha/universe code call KIS directly
- route broker-backed quotes, account reads, order actions, and historical KIS calls through broker-engine
- keep the broker-engine rate limit configured at or below the KIS limit, with headroom for retries and operational calls

StockProgram already implements the right boundary:

- `broker-engine` owns direct KIS REST/websocket communication
- `BrokerEngineService` owns per-lane `RateLimitedSession`
- lane key is based on KIS base URL, AppKey, and mock/real mode
- `/broker/call` exposes whitelisted broker operations
- `/broker/commands` handles command queue/idempotency for submitted order workflows

The new engine should consume this as a local adapter, not copy the broker implementation into the core.

## Layering

Use separate responsibilities:

```text
broker-engine
  KIS auth, token, rate limit, broker REST/websocket mechanics

market-data adapter
  normalized Bar/DataSlice/Quote objects, cache policy, history replay conversion

universe
  symbols of interest and membership snapshots

alpha
  signals over ActiveUniverse and DataSlice

portfolio/risk/execution
  target creation, risk decisions, order intents, order lifecycle
```

If the code still makes sense after replacing KIS with another broker, it does not belong in broker-engine.

## Historical Data Path

Historical data is not the same as live quote polling.

StockProgram has separate historical operations and cache wrappers:

- `get_daily_ohlcv`
- `get_or_cache_daily_ohlcv`
- `get_intraday_bars`
- `build_position_replay_feed`
- `get_or_cache_domestic_minute_bars`
- `get_overseas_daily_ohlcv`
- `get_overseas_intraday_bars`

The new engine should preserve this split:

```text
KIS historical operation
  -> broker-engine /broker/call
  -> market-data adapter
  -> local history cache
  -> normalized Bars
  -> replay DataSlice feed
  -> backtest / alpha feature pipeline
```

Daily OHLCV and replay/minute bars should prefer cache-first reads. `refresh=true` should be explicit.

## KIS Throughput Strategy

Do not design universe or alpha around polling hundreds of symbols live.

Recommended flow:

```text
large universe, e.g. 300 symbols
  -> cached daily history/features
  -> coarse alpha/universe filters
  -> active watchlist, e.g. 10-50 symbols
  -> broker-engine live quote or websocket path
  -> execution-time fresh quote/orderbook checks
```

Use KIS for:

- broker-truth live quotes when needed
- orderbook/time-and-sales for symbols close to action
- account/holdings/orders/fills
- historical KIS data when it is specifically required

Avoid KIS for:

- repeated full-universe polling
- bulk research refresh when public or cached data is sufficient
- alpha feature recomputation that can use cached daily bars

## Universe And Alpha Implication

Universe should not be a KIS request loop.

The recommended design is:

```text
UniverseDefinition
  -> UniverseStore / cached metadata
  -> ActiveUniverse
  -> AlphaModel.update(data, active_universe)
```

For 300 symbols, the default alpha input should be cached history/features plus selected fresh quotes. Alpha models should declare their data needs:

- daily bars
- intraday bars
- latest quote
- orderbook
- time-and-sales
- fundamentals/metadata

The runtime can then batch, cache, or throttle those needs before constructing `DataSlice`.

## New Engine Implementation Direction

Current new-engine adapter:

- `leaps_quant_engine.market_data.MarketDataProvider`
- `leaps_quant_engine.adapters.kis.KISBrokerEngineMarketDataProvider`
- `leaps_quant_engine.backtesting.VirtualMarketDataProvider`

Keep evolving this into ports/adapters:

```text
ports:
  MarketDataProvider
  HistoricalDataProvider
  QuoteProvider
  MarketDataCache

adapters:
  KISBrokerEngineMarketDataProvider
  CachedMarketDataProvider
  VirtualMarketDataProvider
  PublicResearchDataProvider
```

The deterministic core should only receive normalized objects:

- `Symbol`
- `Bar`
- `DataSlice`
- later `Quote`, `Tick`, `OrderBook`, `SubscriptionEvent`

It should never depend on:

- KIS TR IDs
- raw KIS field names
- raw broker-engine response payloads
- request pacing details
- tokens or AppKeys

## Proposed Next Slice

Build a cache-first history provider:

```text
CachedHistoricalDataProvider
  - takes an upstream MarketDataProvider
  - reads/writes data/market-data/cache
  - normalizes to list[Bar]
  - supports refresh=False by default
```

Then connect:

```text
Universe symbols
  -> history provider
  -> daily DataSlice replay
  -> alpha model
```

This gives the engine a scalable path for 300-symbol universes without spending KIS throughput on every alpha pass.

Indicator plans should be attached to universe definitions and registered in memory before the runtime starts consuming `DataSlice` events.

## Operating Rules

- KIS direct calls are forbidden outside broker-engine.
- New code may call broker-engine only through an adapter.
- Default historical workflows are cache-first.
- Live polling is allowed only for small active watchlists or execution checks.
- Backtests should use cached or virtual providers, not live KIS calls.
- Any new KIS operation must be documented as broker-engine-backed and rate-limit aware.
