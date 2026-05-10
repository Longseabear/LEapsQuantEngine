# LEaps Quant Engine Implementation Map

This package is the deterministic engine core. The intended flow is:

```text
market data
  -> universe
  -> indicators / snapshots
  -> alpha
  -> portfolio construction
  -> risk
  -> execution
  -> order coordination
  -> broker / fill events
  -> sleeve portfolio state
```

The engine should stay LEAN-like at the strategy boundary, but sleeve-aware at every ownership boundary.

## Main Modules

- `models.py`: common immutable-ish domain records such as `Symbol`, `Bar`, `DataSlice`, `PortfolioTarget`, and `OrderIntent`.
- `market_data.py`: provider protocol for normalized bars.
- `adapters/`: provider adapters, including in-process KIS, compatibility broker/market-data clients, and FinanceDataReader history.
- `universe/`: coarse/fine/active universe selection and forced live-universe invariants.
- `indicators/`: LEAN-like indicator objects and sleeve-namespaced indicator runtime.
- `snapshots/`: immutable `IndicatorSnapshot` records and snapshot quality/freshness policy.
- `alpha/`: insight domain, alpha model loading, runtime staging, and active insight management.
- `framework/portfolio_construction.py`: active insights to auditable `PortfolioAllocationTarget`, `PortfolioTargetBatch`, and desired-value plans.
- `framework/order_sizing.py`: allocation targets to quantity-based `PortfolioTarget` records with rounding loss and rebalance noise filtering.
- `framework/risk.py`: target approval, rejection, and clamp decisions.
- `execution.py`: sleeve execution models that convert approved targets into `OrderIntentBatch`.
- `orders.py`: global order coordination, tickets, order events, collision reporting, and simulated fills.
- `order_orchestrator.py`: account-level multi-sleeve order orchestration after sleeve execution models run.
- `order_state.py`: append-only ticket/event runtime store for restart-safe open-order polling.
- `order_smoke.py`: paper submit -> supervisor poll -> final status smoke runner.
- `order_status.py`: agent/operator read model that combines order runtime state with virtual sleeve accounts.
- `order_submit.py`: guarded conversion from submit-ready `OrderIntentBatch` artifacts into order tickets and broker submit events.
- `order_supervisor.py`: bounded poll/reconcile/status operation for order runtime maintenance.
- `order_worker.py`: open-ticket polling and execution-history reconciliation workers.
- `brokerage.py`: broker gateway boundary for paper execution and KIS/broker command submission.
- `portfolio.py`: sleeve portfolio state and fill-event application.
- `portfolio_state.py`: agent-readable portfolio engine state snapshots.
- `virtual_account.py`: file-backed virtual sleeve account source of truth for live/paper ownership.
- `account_sync.py`: read-only KIS account sync into the virtual account ledger.
- `runtime_config.py`: validated config snapshot schema, including broker account profiles and sleeve-to-account routing.
- `runtime_bootstrap.py`: converts config snapshots into executable sleeve runtimes.
- `control.py`: explicit runtime reload/control commands.
- `snapshot_worker.py`: live snapshot collection and indicator publication.
- `backtesting.py`: deterministic replay and simulated-fill backtests.

## Boundary Rules

- Strategy modules must not place broker orders directly.
- Alpha emits `Insight` only.
- Portfolio construction emits target percentages and desired values only.
- Order sizing converts portfolio allocations into quantity targets.
- Risk approves, rejects, or clamps targets.
- Execution emits order intents only.
- Runtime can persist execution output as an order-intent artifact for operator review and later submit.
- Order submit commits order intents into tickets and broker submit events; it is not part of sleeve strategy logic.
- Order tickets sync order lifecycle. They do not mutate portfolio holdings.
- Order runtime status is a read model. It must not submit, poll, or reconcile broker orders.
- Broker gateways emit normalized order events. They do not mutate sleeve holdings.
- Portfolio and virtual sleeve account holdings change from fill or reconciliation events.
- KIS-specific payloads belong behind adapters or sync code, never inside strategy, alpha, portfolio, risk, or execution models.
- Domestic and overseas broker accounts must be explicit routes. A sleeve should choose a `broker_account_id`; account-level order runtime commands use that route before old portfolio store fallbacks.
- The current broker-engine gateway is domestic-only. Overseas routes may run status and paper flows, but live broker-engine submit, poll, and reconciliation must stay blocked until an overseas adapter exists.

## Sleeve Rules

- Each sleeve owns its alpha, portfolio, risk, execution, cash, holdings, pending orders, and workspace.
- Infrastructure can be shared, but state must be sleeve-namespaced.
- A sleeve owns virtual account state through its broker account route; cross-account or cross-sleeve movement must be explicit.
- Research/backtests may run one sleeve in isolation.
- Live/paper runtime should run active sleeves together and coordinate order intents through the global order layer.
