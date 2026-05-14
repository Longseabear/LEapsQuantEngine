# KRX Live Readiness Checklist

Last updated: 2026-05-14 15:30 KST

This checklist is for running `LEaps` against the Korean market through
the default live multi-sleeve config, `configs/runtime/live_multi_sleeve.json`.

For the short morning operating procedure, see
`docs/krx-market-open-runbook.md`.

The live boundary is:

```text
runtime-run-multi-once
  -> OrderIntent artifact
  -> order-runtime-submit dry-run
  -> order-runtime-submit --commit --confirm-live-submit
  -> order-runtime-supervise
  -> virtual account reconciliation
```

Do not skip the dry-run boundary. Strategy output is not a broker order until
`order-runtime-submit` commits it.

## Current Readiness Snapshot

Checked on 2026-05-11 before the 2026-05-12 KRX session.

Passed:

- Runtime config loads.
- Runtime preflight is `ok`.
- Runtime health is `ok`.
- Order runtime store exists: `data/order-runtime/kis_domestic.jsonl`.
- Open ticket count is `0`.
- Unallocated fill count is `0`.
- Notification engine status is `ok`.
- KIS direct health is `ok`.
- Samsung Electronics quote lookup works through the KIS adapter.
- Indicator warmup loads 16 of 16 symbols and reaches ready ratio `1.0`.

Changed during readiness:

- `market_data.rate_limit_per_second` was reduced from `20` to `5`.
  At `20`, KIS returned `EGW00201` rate-limit errors during quote collection.
- `LEaps.portfolio.account_store_path` now points to the domestic virtual
  account store: `../../data/virtual-accounts/kis_domestic.json`.

Current blockers before real live submit:

- KIS broker holdings do not match the virtual account projection.
  Broker account has 7 holdings, while LEaps virtual holdings are empty.
- LEaps virtual cash in `data/virtual-accounts/kis_domestic.json` is
  `100,000 KRW`; the runtime config allocation is no longer the live source of
  truth after account-store alignment.
- The real KIS domestic cash read was `6,385,012 KRW`, with total evaluation
  amount `14,040,672 KRW` and 7 holdings. Decide how much of this belongs to
  LEaps before any live submit.
- Local MCP market-data health is `ok`, but MCP calls that require
  `127.0.0.1:8755` broker-engine fail when that service is down. The current
  runtime path uses the in-process KIS adapter; do not assume the old local
  broker-engine server is running.

Broker holdings currently needing virtual ownership assignment or explicit
ignore/default-sleeve ownership:

```text
KRX:005930  broker 20  virtual 0
KRX:010120  broker 4   virtual 0
KRX:226490  broker 1   virtual 0
KRX:228790  broker 1   virtual 0
KRX:229200  broker 1   virtual 0
KRX:396500  broker 5   virtual 0
KRX:487240  broker 11  virtual 0
```

## Go / No-Go Gates

Go only if every item is true:

- `runtime-preflight` returns `status: ok`.
- `runtime-health` returns `status: ok`.
- `runtime-recovery-status` returns `status: ok`.
- `order-runtime-status` returns `needs_attention: false`.
- `virtual-account-reconcile` is either `ok`, or every mismatch is deliberately
  assigned to a non-LEaps sleeve and documented.
- LEaps virtual cash is the intended live allocation.
- `runtime-run-multi-once` snapshot quality is `fresh` for the LEaps sleeve.
- `runtime-run-multi-once` has `failed_symbol_count: 0` for the LEaps sleeve.
- `order-runtime-submit` dry-run is not `blocked`.
- Dry-run guard has no `reserved_cash_exceeded`, `oversell`, route mismatch, or
  unsupported broker route decision.
- Operator has reviewed intended symbols, quantities, limit prices, and total
  notional.

No-go immediately if any item is true:

- KIS health fails.
- KIS quote lookup fails for a core symbol such as `005930`.
- Snapshot quality is `degraded` and `allows_new_entries` is `false`.
- Quote collection shows `EGW00201` rate-limit errors. Lower the rate limit and
  re-run.
- Open tickets exist from an older run and have not been supervised.
- Unallocated fills exist.
- Virtual account cash/holdings do not reflect intended sleeve ownership.
- `order-runtime-submit` dry-run is blocked.
- The artifact was generated from a different config version than the current
  preflight config version.

## Pre-Open Checklist

Run from the repository root:

```powershell
$env:PYTHONPATH='src'
```

1. Confirm the target day is a KRX trading day.

Use the official KRX calendar or a trusted market-hours source. The engine must
not treat a holiday as a live orderable day.

2. Check config and code identity.

```powershell
py -3 -m leaps_quant_engine.cli runtime-config-validate configs/runtime/live_multi_sleeve.json

py -3 -m leaps_quant_engine.cli runtime-preflight configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --include-order-status `
  --summary-only
```

3. Check KIS read path.

```powershell
py -3 -m leaps_quant_engine.cli kis-health

py -3 -m leaps_quant_engine.cli kis-quote 005930 --market KRX
```

4. Check order and recovery state.

```powershell
py -3 -m leaps_quant_engine.cli runtime-recovery-status configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --summary-only

py -3 -m leaps_quant_engine.cli runtime-health configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --broker broker-engine `
  --summary-only

py -3 -m leaps_quant_engine.cli order-runtime-status configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --summary-only
```

5. Check broker holdings against virtual sleeve state.

```powershell
py -3 -m leaps_quant_engine.cli virtual-account-reconcile configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --market domestic `
  --summary-only
```

If this reports mismatches, do not live submit until ownership is assigned or
the mismatch is explicitly accepted as another sleeve's responsibility.

6. Generate a dry-run cycle artifact.

```powershell
py -3 -m leaps_quant_engine.cli --log-level ERROR runtime-run-multi-once configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --summary-only `
  --order-batch-output "$PWD/artifacts/prelive/multi_sleeve_order_intents.json"
```

Required readout:

- LEaps report `snapshot_quality.status` is `fresh`.
- LEaps report `failed_symbol_count` is `0`.
- `order_batch_artifact.order_count` matches expectation.
- LEaps `portfolio_state.current.cash_by_currency.KRW` is the intended cash.

7. Dry-run order submit.

```powershell
py -3 -m leaps_quant_engine.cli order-runtime-submit configs/runtime/live_multi_sleeve.json `
  artifacts/prelive/multi_sleeve_order_intents.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --broker broker-engine `
  --summary-only
```

Required readout:

- `status` is not `blocked`.
- `guard.blocked` is `false`.
- No `reserved_cash_exceeded`.
- No oversell.
- No unsupported broker route.
- Collision count is `0`.

KRX tick warnings are acceptable only if the broker gateway is expected to
side-safe round domestic limit prices at submit. Review the rounded prices
before live commit.

## Live Submit Checklist

Only after the pre-open checklist is green:

```powershell
py -3 -m leaps_quant_engine.cli order-runtime-submit configs/runtime/live_multi_sleeve.json `
  artifacts/prelive/multi_sleeve_order_intents.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --broker broker-engine `
  --commit `
  --confirm-live-submit `
  --notify `
  --summary-only
```

Live submit is allowed only when the artifact was generated in the same intended
cycle. Do not reuse an old artifact after config, model, market-data, cash, or
holdings changed.

## Real-Time Checklist

Run this loop while the engine is operating:

```powershell
py -3 -m leaps_quant_engine.cli runtime-health configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --broker broker-engine `
  --summary-only

py -3 -m leaps_quant_engine.cli order-runtime-status configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --summary-only
```

Watch these fields:

- `last_cycle_age` stays below the configured threshold.
- `open_ticket_count` does not stay nonzero unexpectedly.
- `unallocated_fill_count` stays `0`.
- `needs_attention` stays `false`.
- Recent events progress from submitted/accepted to filled/cancelled/rejected.
- Virtual holdings change only from broker/order fill events.
- Snapshot quality remains `fresh` before new entries.

If a live order is submitted, supervise boundedly:

```powershell
py -3 -m leaps_quant_engine.cli order-runtime-supervise configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --broker broker-engine `
  --summary-only
```

If `order-runtime-supervise` cannot fetch history or holdings, do not loop
forever. Record the warning, check KIS/MTS manually, and retry the supervisor
after the broker path is healthy.

## Emergency Stop Rules

Stop new submissions when:

- Runtime health is not `ok`.
- Snapshot quality is `degraded`.
- Quote collection hits rate limits repeatedly.
- Virtual account reconciliation reports unexpected mismatches.
- Any order is rejected by broker and the reason is not understood.
- Open tickets are stale.
- An operator manually trades the account outside the engine.
- Code or model files change during market hours without a successful preflight
  and fresh dry-run.

If manual intervention is needed, record it as an operational event and run
recovery/status before restarting automated submission.

## Post-Market Checklist

After the session:

```powershell
py -3 -m leaps_quant_engine.cli order-runtime-supervise configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --broker broker-engine `
  --summary-only

py -3 -m leaps_quant_engine.cli virtual-account-reconcile configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --market domestic `
  --summary-only

py -3 -m leaps_quant_engine.cli runtime-recovery-status configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --summary-only
```

Expected end state:

- Open tickets are `0`, unless intentionally left open and documented.
- Unallocated fills are `0`.
- Virtual holdings reconcile to broker holdings for LEaps-owned positions.
- Unknown/manual broker fills are assigned or deliberately left for operator
  allocation.
- Cycle journal has the final cycle and current config/code identity.

