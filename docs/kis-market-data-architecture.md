# KIS Market Data Architecture

## Decision

The new engine must not call KIS directly from algorithms, alpha models, universe selection, portfolio, risk, execution model logic, or indicators.

KIS access now goes through the engine-owned adapter boundary. The current
single-process boundary is the in-process `KISDirectClient`; multi-process
live operation should route through one local KIS gateway/rate-limit boundary
instead of letting every process spend the same AppKey quota independently. The
old broker-engine and market-data-engine servers remain reference/compatibility
concepts, not required runtime servers.

```text
new engine core
  -> MarketDataProvider port
  -> KIS adapter / local cache
  -> KIS
```

This keeps the most important StockProgram lesson without requiring separate local servers: one explicit broker-facing boundary owns KIS credentials, token reuse, request pacing, broker errors, and broker-specific payload normalization.

## Why The Adapter Boundary Is Mandatory

KIS has a low request throughput budget. Treat the AppKey as a shared
rate-limited lane. The current operating limits are 18 REST calls per second
for real trading and 1 REST call per second for paper trading. KIS can return
`EGW00201` for per-second transaction count excess. The quota is managed at the
AppKey lane, so different AppKeys can have independent REST budgets. Do not
treat one AppKey's capacity as free capacity for every local process; it is the
conservative upper bound for that shared KIS REST lane unless the operator
configures a stricter API-specific policy.

The practical operating rule is:

- never let multiple local processes independently spend the same KIS quota
- never let alpha/universe code call KIS directly
- route broker-backed quotes, account reads, order actions, and historical KIS calls through the adapter boundary
- keep adapter rate limits at or below the KIS limit, with headroom for retries and operational calls
- keep bulk collection lower priority than live order submit, order status,
  account sync, and execution reconciliation

StockProgram remains the reference for the operational details:

- `broker-engine` owns direct KIS REST/websocket communication
- `BrokerEngineService` owns per-lane `RateLimitedSession`
- lane key is based on KIS base URL, AppKey, and mock/real mode; account id is
  metadata for routing and reporting, not the primary quota key
- `/broker/call` exposes whitelisted broker operations
- `/broker/commands` handles command queue/idempotency for submitted order workflows

The new engine now takes ownership of the subset it needs inside `leaps_quant_engine.adapters.kis_direct`. The deterministic core still only sees normalized providers and order lifecycle records.

## Layering

Use separate responsibilities:

```text
KISDirectClient
  KIS auth, token, rate limit, REST mechanics, local cache

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
- `get_or_cache_overseas_minute_bars`

The new engine should preserve this split:

```text
KIS historical operation
  -> KISDirectClient
  -> market-data adapter
  -> local history cache
  -> normalized Bars
  -> replay DataSlice feed
  -> backtest / alpha feature pipeline
```

Daily OHLCV and replay/minute bars should prefer cache-first reads. `refresh=true` should be explicit.

KIS daily-history normalization must stamp daily rows as `resolution="daily"`.
Recent KIS rows can contain broker/reference artifacts such as zero-volume
current-day rows or adjusted-price discontinuities. The adapter quarantines the
conservative high-risk case where a zero-volume daily row jumps by a split-like
multiple from the previous close. That prevents confirmed daily indicators from
absorbing a clearly invalid current-day/reference row. More nuanced corporate
action adjustment belongs in a dedicated history repair layer, not in alpha or
portfolio models.

## KIS Throughput And Gateway Strategy

`KISDirectClient` caps its default per-process query/request lanes at 18/sec
for real trading and 1/sec for paper trading. Live runtime configs should use
18/sec only when calls are coordinated through one AppKey-level gateway/lane.
Use a lower explicit value for any temporary process that bypasses the shared
lane. This cap prevents a newly started CLI, MCP server, or collector from
defaulting to an unsafe high-rate burst.

For one live runner process, in-process rate limiting is acceptable. Once the
engine runs a live loop, report loop, collector, MCP server, and manual CLIs at
the same time, the correct shape is a local KIS gateway:

```text
live runtime / reports / collector / MCP / CLI
  -> KIS Gateway
  -> shared lane queues and cache
  -> KISDirectClient
  -> KIS
```

The gateway is not a load balancer that increases throughput. It is a shared
pacer and request coordinator:

- one token bucket per KIS lane, keyed by base URL, AppKey, mock/real, and
  operation class
- account id can be part of operation routing and audit metadata, but it must
  not accidentally merge or split quota lanes without the AppKey
- high priority for order submit, cancel/replace, order status, fills, account
  sync, and risk-critical quote checks
- normal priority for active-universe live quotes
- low priority for minute collectors, bulk history refresh, research cache
  warming, and agent diagnostics
- short TTL coalescing for identical quote/minute/history requests
- whole-lane backoff and warning metrics on `EGW00201`

Until that gateway is active, do not run multiple aggressive KIS callers in
parallel. Bulk cache builders should run off-hours or with an explicit low rate
limit.

Strict live preflight treats this as an operating rule. If a live runtime config
uses `market_data.provider` other than `kis-gateway`, `runtime-preflight
--strict-live` returns a critical `market_data_gateway_policy` check. Backtests,
paper experiments, and isolated research configs can still use other adapters,
but the live multi-sleeve stack should converge through the local gateway.

Runtime cycles must not wait for the gateway to finish polling every symbol.
Collectors update snapshots in the background; cycles read the latest immutable
snapshot and act according to freshness quality. The default engine thresholds
are:

- latest quote: fresh at 10 seconds or less during regular sessions
- extended-session quote: fresh at 30 seconds or less
- confirmed 1-minute bar: fresh when the latest completed minute bar is present;
  one missing bar is degraded, not fresh
- account/cash/holdings: fresh at 60 seconds or less
- open-ticket order status: fresh at 10 seconds or less
- no-open-ticket order status: fresh at 60 seconds or less
- confirmed daily: fresh when the expected last confirmed trading day is present

Freshness is resolution-aware. A 09:31:20 KST cycle expects the confirmed
09:30 1-minute bar; a 09:29 bar is degraded or stale depending on policy. A
live quote can be fresh without being a confirmed minute bar, and a minute bar
must never advance confirmed daily indicators.

Gateway commands:

```powershell
py -3 -m leaps_quant_engine.cli kis-gateway-serve --host 127.0.0.1 --port 8766
```

```powershell
py -3 -m leaps_quant_engine.cli kis-gateway-health --base-url http://127.0.0.1:8766
```

The health endpoint is:

```text
GET /health
```

The gateway is a FastAPI service served by uvicorn. Local OpenAPI docs are
available at `/docs` while the service is running.

`/health` is intentionally low risk. It does not submit orders and does not
perform a live KIS probe. It reports local gateway uptime, AppKey-lane
fingerprint, real/mock mode, effective query/request rate limits, and call
counters. The app key itself is never returned.

The initial call endpoint is:

```text
POST /call
{"operation": "get_stock_price", "arguments": {"market": "domestic", "symbol": "005930"}}
```

Only engine-owned tools should call `/call`. Strategies and models still use
normalized market-data, account, and broker ports; they must not call the
gateway directly.

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
- title-level domestic/overseas news snapshots for operator or agent context
- account/holdings/orders/fills
- historical KIS data when it is specifically required

For overseas holdings, KIS present-balance rows carry multiple quantity views.
The adapter must keep them explicit:

- `settled_quantity`: base/settled quantity such as `cblc_qty13`
- `current_quantity`: current executed/settlement quantity such as `ccld_qty_smtl1`
- `orderable_quantity`: broker orderable quantity such as `ord_psbl_qty1`

Engine reconciliation compares virtual sleeve holdings to `current_quantity`.
Operator reports may still show `settled_quantity` when matching an app or
settlement-view screen. Do not mix a settled quantity with a current evaluation
amount in the same normalized holding row.

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

Current new-engine adapters and snapshot path:

- `leaps_quant_engine.market_data.MarketDataProvider`
- `leaps_quant_engine.adapters.kis.KISBrokerEngineMarketDataProvider`
- `leaps_quant_engine.adapters.kis.KISCachedMarketDataProvider`
- `leaps_quant_engine.adapters.kis.MarketDataEngineLiveQuoteProvider`
- `leaps_quant_engine.adapters.kis_direct.KISDirectClient`
- `leaps_quant_engine.mcp_market_data_stdio`
- `leaps_quant_engine.backtesting.VirtualMarketDataProvider`
- `leaps_quant_engine.market_data_snapshot.MarketDataSnapshotEngine`
- `leaps_quant_engine.snapshots.IndicatorSnapshotStore`

`KISDirectClient` also exposes read-only KIS news-title operations:

- `get_domestic_news_titles`
- `get_overseas_news_titles`
- `get_overseas_breaking_news_titles`

They normalize KIS title rows into provider-neutral dictionaries while keeping
the original row in `raw_output`. These records are news context, not engine
orders or alpha decisions.

Live quote flow:

```text
KISDirectClient get_stock_price
  -> MarketDataEngineLiveQuoteProvider
  -> normalized Bar
  -> MarketDataSnapshot
  -> IndicatorEngine.on_data
  -> IndicatorSnapshot
```

Historical benchmark flow:

```text
KISDirectClient get_or_cache_daily_ohlcv
  -> KISCachedMarketDataProvider
  -> daily Bars
  -> daily replay DataSlice feed
  -> IndicatorEngine.on_data benchmark
```

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

The cache-first daily history benchmark path exists. The next slice is a long-running snapshot worker:

```text
BackgroundSnapshotWorker
  - reads active universe symbols
  - uses `kis-gateway` live market data when runtime config selects it
  - closes MarketDataSnapshot at cycle boundaries
  - updates IndicatorEngine
  - publishes IndicatorSnapshot
  - appends market snapshots to the configured snapshot store
  - logs collection/update/failure/freshness metrics
```

Then connect:

```text
IndicatorSnapshot
  -> UniverseSelectionModel
  -> AlphaModel
  -> PortfolioConstruction
  -> RiskManagement
  -> ExecutionModel
```

This gives the engine a live path where strategies consume the last complete snapshot instead of directly polling broker-backed data.

Indicator plans should be attached to universe definitions and registered in memory before the runtime starts consuming `DataSlice` events.

## Runtime Gateway Wiring

Live runtime config can select the local KIS Gateway explicitly:

```json
{
  "market_data": {
    "provider": "kis-gateway",
    "source": "kis-gateway",
    "history_provider": "kis-cache",
    "history_source": "kis-cache",
    "rate_limit_per_second": 18,
    "gateway_base_url": "http://127.0.0.1:8766",
    "snapshot_store_path": "../../data/market-data-snapshots/live_multi_sleeve.jsonl"
  }
}
```

In this mode `bootstrap_sleeve_runtime(...)` and the multi-sleeve runner build
their live quote provider with `KISGatewayClient`. Backtests stay isolated:
`runtime-backtest-daily` injects its replay/cache provider through
`RuntimeBootstrapDependencies`, so it does not require a running Gateway unless
a caller deliberately provides one.

`runtime-preflight --strict-live` and `runtime-health` include
`kis_gateway_liveness` whenever the provider is `kis-gateway`. A failed Gateway
health check is a live-start blocker under strict preflight.

## Snapshot Store

`FileMarketDataSnapshotStore` is an append-only JSONL store for the latest
runtime market snapshots. Each record contains:

- normalized bars by symbol
- the snapshot lane: `quote`, `minute`, `daily_confirmed`, or `unknown`
- source and snapshot id
- per-sleeve snapshot quality when available
- optional runtime metadata

The store is diagnostic and replay-supporting state, not a strategy data source
inside alpha models. Models still consume immutable `SnapshotContext` objects
published at cycle boundaries.

Snapshot lanes are intentionally strict. A single `MarketDataSnapshot` cannot
mix quote/minute/daily-confirmed bars; the caller must create separate snapshots
per lane. This mirrors LEAN's separate subscriptions/consolidated bars and keeps
daily indicators from sharing an event stream with live quote updates.

## Logging And Debugging

The market-data adapter and snapshot runtime emit structured logs.

Recommended command:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli --log-level INFO --log-json --log-file logs/live-snapshot.jsonl live-indicators-once configs/universes/us_live_smoke.json --sleeve-id us-live --min-success 3 --rate-limit-per-second 20
```

Important events:

- `market_data_engine.call.rate_limited`
- `market_data_engine.call.failed`
- `market_data_snapshot.collect.symbol_failed`
- `market_data_snapshot.collect.complete`
- `indicator_snapshot.publish`
- `live_indicator_snapshot.complete`

## Codex MCP Boundary

The global Codex market-data MCP entry should point at the new engine-local
stdio server:

```text
codex global MCP
  -> leaps_quant_engine.mcp_market_data_stdio
  -> KISGatewayClient
  -> local KIS gateway
  -> KISDirectClient
  -> local KIS cache / KIS REST
```

This replaces the old StockProgram `market_data_engine.server.app serve-mcp`
entry. The MCP server is intentionally thin: it exposes quote/history/cache
tools and delegates broker-specific work to the shared local KIS gateway. It
must not be used from alpha, portfolio, risk, or execution model code.

Codex launches stdio MCP servers per client/session, so multiple
`mcp_market_data_stdio` processes can exist at the same time. Those processes
must stay cheap proxies. The default backend is the shared HTTP gateway:

```powershell
$env:LEAPS_MARKET_DATA_MCP_BACKEND='gateway'
$env:LEAPS_KIS_GATEWAY_BASE_URL='http://127.0.0.1:8766'
```

Direct KIS mode is only for local diagnostics:

```powershell
$env:LEAPS_MARKET_DATA_MCP_BACKEND='direct'
```

Do not use direct mode for normal live operation because each stdio process
would create its own `KISDirectClient` and bypass the shared AppKey pacing
boundary.

Supported MCP tools include:

- `health_check`
- `get_stock_price`
- `get_daily_ohlcv`
- `get_or_cache_daily_ohlcv`
- `get_overseas_daily_ohlcv`
- `get_intraday_bars`
- `get_overseas_intraday_bars`
- `get_or_cache_domestic_minute_bars`
- `get_or_cache_overseas_minute_bars`
- `build_whitelist_live_facts`
- `get_market_session_status`

`get_market_session_status` is a local lightweight estimate and does not apply
holiday calendars yet.

## Operating Rules

- KIS direct calls are forbidden outside the adapter boundary.
- Strategy and model code may call market-data ports only, never `KISDirectClient`.
- Default historical workflows are cache-first.
- Live polling is allowed only for small active watchlists or execution checks.
- Backtests should use cached or virtual providers, not live KIS calls.
- Any new KIS operation must be documented as adapter-backed and rate-limit aware.
