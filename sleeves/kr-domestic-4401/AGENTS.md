# kr-core-compass Sleeve

## Scope

This sleeve is the `kr-core-compass` KRX domestic-equity strategy. Its stable
system ID remains `kr-domestic-4401`, and it routes through the real KIS account
ending in `4401` / product `01`.

## Current Status

- Runtime config: `configs/runtime/kr_domestic_4401_sleeve.json`
- Universe: `configs/universes/kr_domestic_4401_core.json`
- Broker route: `kis-domestic-4401`
- Market: KRX domestic equities
- Currency: KRW only
- Live multi-sleeve stack: enrolled as `kr-domestic-4401`
- Strategy: KRX ETF / liquid large-cap core regime allocator

## Active Models

- Selection: `selections/watchlist.py`
- Operational selection: `selections/operational_symbols.py`
- Alpha: `alphas/core_regime_allocator.py`
- Portfolio: `portfolios/hold_existing.py`
- Risk: `risks/basic.py`
- Execution: `executions/immediate.py`

## Rules

- Keep `kr-domestic-4401` as the runtime sleeve ID. Use `kr-core-compass` only
  as the operator-facing strategy alias unless the operator explicitly approves
  a deeper migration.
- Do not hard-code KIS credentials or account numbers in model code.
- The runtime account route resolves the real KIS account through env-scoped
  account settings. Model code must not call KIS directly.
- Alpha emits insights only.
- Portfolio emits percentage targets only.
- Risk clamps or rejects targets; it does not submit orders.
- Execution emits order intents only; order runtime and the broker adapter own
  actual submission, tickets, fills, cancellation, and reconciliation.
- The active alpha expresses broad KRX regime exposure through insights for
  broad-market ETFs, liquid large-cap stocks, cash-like ETFs, and a small
  inverse hedge only in shock regimes.

## State

The starter models are stateless. Future model state such as trailing stops,
target smoothing, drawdown guards, or execution replacement memory must use
`context.model_state` / `StatePatch`, not module globals or sleeve-local files.

## Validation

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-config-validate configs/runtime/kr_domestic_4401_sleeve.json
py -3 -m leaps_quant_engine.cli runtime-preflight configs/runtime/kr_domestic_4401_sleeve.json --sleeve-id kr-domestic-4401 --summary-only
```
