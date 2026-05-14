# KRX Market Open Runbook

Last updated: 2026-05-14 08:30 KST

This runbook is the short morning procedure for starting the `LEaps` Korean
market live loop. Use it with the fuller readiness checklist in
`docs/krx-live-readiness-checklist.md`.

The goal is to make market open boring:

```text
services healthy
  -> runtime preflight ok
  -> order state clean
  -> live loop running before open
  -> first usable market snapshot submits at most once
  -> supervisor keeps polling/reconciling
```

## Why Market Open Has Been Painful

Morning failures usually come from one of these conditions:

- Local StockProgram services are not running, but the current live route still
  depends on the local `market-data-engine`, `broker-engine`, and notification
  service boundaries.
- Before 09:00 KST, KIS domestic quotes can return reference prices with
  unusable live bid/ask or latest trade fields. In that state
  `runtime-run-multi-once` may report a degraded or failed KRX snapshot. This
  is expected before the market is actually usable.
- A long-running PowerShell loop starts a fresh Python process every cycle.
  Live loops therefore need persisted framework state for active insights,
  portfolio cadence, and the last target batch. The submit state file is still
  used only as an exact-artifact safety latch.
- Open tickets, unallocated fills, or virtual-account mismatches must be handled
  before new live submits. Do not paper over these at 08:59.

## Standard Timeline

Use this timing on normal KRX trading days:

- `08:35-08:45`: Start local services and check health.
- `08:45-08:50`: Run LEaps runtime preflight and order-runtime status.
- `08:50-08:55`: Start the live order loop with a submit state file.
- `08:55-09:00`: Expect snapshot failures if KIS live prices are not usable yet.
- `09:00+`: Confirm the first successful runtime cycle, order submit, and
  supervisor result.

Do not wait until 08:59 to start service recovery.

## One-Page Procedure

Run from the engine repository:

```powershell
$RepoRoot = Resolve-Path .
cd $RepoRoot
$env:PYTHONPATH='src'
```

1. Start or repair local services.

The current LEaps domestic live route uses local StockProgram-style service
boundaries. Check and start them from the StockProgram workspace:

```powershell
$StockProgramRoot = Resolve-Path ..\StockProgram
cd $StockProgramRoot
stockprogram-stack status
stockprogram-stack start --force
Start-Sleep -Seconds 10
stockprogram-stack status
```

Minimum acceptable state:

- `broker-engine` healthy
- `market-data-engine` healthy
- `notification-engine` healthy
- workers do not need to be perfect for LEaps submit, but stale workers explain
  poor morning reports and should be repaired after the open is stable

Return to LEaps:

```powershell
cd $RepoRoot
$env:PYTHONPATH='src'
```

2. Run the live readiness gates.

```powershell
py -3 -m leaps_quant_engine.cli runtime-preflight configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --include-order-status `
  --summary-only

py -3 -m leaps_quant_engine.cli order-runtime-status configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --summary-only
```

Required readout:

- `runtime-preflight.status` is `ok`
- `open_ticket_count` is `0`, unless those tickets are intentionally being
  supervised
- `needs_attention` is `false`
- LEaps virtual cash and holdings match the intended live sleeve ownership

3. Start the guarded multi-sleeve live loop.

Default live operation now uses one multi-sleeve runner for `LEaps` and
`us_etf_rotation`. It collects one union market snapshot, runs sleeve-specific
alpha/portfolio/risk/execution separately, then lets `order-runtime-submit`
split domestic and overseas orders by broker account route.

Use a submit state file. The state file is the morning safety latch:
after a successful live submit with orders, the loop records the last submitted
artifact for audit and exact-artifact idempotency. It does not block every later
buy for the same date. Duplicate or stale orders are blocked by
`order-runtime-submit` through the engine guard, which checks target quantity,
open tickets, and fill state.

```powershell
$args = @(
  '-NoProfile',
  '-ExecutionPolicy', 'Bypass',
  '-File', 'tools\leaps_multi_sleeve_live_order_loop.ps1',
  '-Config', 'configs/runtime/live_multi_sleeve.json',
  '-SleeveIds', 'LEaps', 'us_etf_rotation',
  '-IntervalSeconds', '60',
  '-DomesticMaxSubmitNotional', '7000000',
  '-OverseasMaxSubmitNotional', '2500',
  '-OrderBatchOutput', 'data/runtime/live-order-loop/multi_sleeve_candidate_orders.json',
  '-Journal', 'data/cycle-journal/live_multi_sleeve.jsonl',
  '-LogPath', 'data/runtime/live-order-loop/multi_sleeve.log',
  '-FrameworkStateDir', 'data/runtime/framework-state/multi-sleeve',
  '-ReconcileEveryCycles', '5',
  '-SubmitStatePath', 'data/runtime/live-order-loop/multi_sleeve_submit_state.json',
  '-ControlQueue', 'data/runtime/control/live.jsonl',
  '-ActiveSleevesPath', 'data/runtime/live-order-loop/multi_sleeve_active_sleeves.json',
  '-HotReload', 'true'
)

Start-Process -FilePath powershell -ArgumentList $args -WindowStyle Hidden -PassThru
```

`DomesticMaxSubmitNotional` and `OverseasMaxSubmitNotional` are intentionally
separate because KRW and USD notionals are not comparable.

The loop drains `data/runtime/control/live.jsonl` at cycle boundaries. Use
`runtime-control-submit --command reload-sleeve`, `activate-sleeve`, or
`deactivate-sleeve` to change the active sleeve set without restarting the
process. Deactivation is rejected while the sleeve still has holdings or open
tickets.

The active sleeve set is additionally filtered by a live schedule before the
framework run. Defaults:

- `LEaps`: KRX 08:30-18:30 KST
- `us_etf_rotation`: US regular market, 09:30-16:00 Eastern Time

The skipped sleeve is not passed to `runtime-run-multi-once`, so its market data
is not collected and its alpha/portfolio/risk/execution stack is not called
outside its scheduled strategy window. `order-runtime-supervise` can still
inspect active sleeves for open-ticket maintenance.

4. Watch the first cycles.

```powershell
Get-Content data/runtime/live-order-loop/multi_sleeve.log -Tail 120 -Encoding UTF8

Select-String -Path data/runtime/live-order-loop/multi_sleeve.log `
  -Pattern 'cycle begin|runtime-run-multi-once exit|order-runtime-submit exit|submit guard|submit state saved|order-runtime-supervise exit|cycle end' |
  Select-Object -Last 40
```

Before 09:00, this degraded cycle is acceptable:

```text
runtime-run-multi-once exit=0
order-runtime-submit skipped: no candidate orders
order-runtime-supervise exit=0
```

It means no usable order was generated or submitted. The loop should keep
running and try again on the next interval. A continuing `exit=1` is not normal
and should be debugged as a runtime error.

After 09:00, the expected success path is:

```text
runtime-run-multi-once exit=0
order-runtime-submit exit=0
submit state saved ...
order-runtime-supervise exit=0
```

If `runtime-run-multi-once exit=1` continues after live KRX quotes should be
usable, stop and debug the market-data snapshot path instead of forcing an old
artifact.

5. Confirm the submit latch.

After a successful live submit:

```powershell
Get-Content data/runtime/live-order-loop/LEaps_submit_state.json -Raw -Encoding UTF8
```

Expected fields:

- `trade_date` is the KST date when the submit occurred
- `order_count` is greater than `0`
- `batch_hash` is populated
- `submitted_at` is the actual submission time
- `guard_mode` is `engine_target_lineage`

If a later cycle produces the exact same order artifact, the live loop should
log a submit-guard skip:

```text
submit guard blocked: identical order batch already submitted
order-runtime-submit skipped by submit guard
```

If the target state changes intraday, the loop should call
`order-runtime-submit` again. The core guard then decides from current virtual
holdings, open tickets, and unapplied fills whether the new intent is still
valid.

6. Confirm framework cadence state.

```powershell
Get-Content data/runtime/framework-state/LEaps.json -Raw -Encoding UTF8
```

Expected fields:

- `active_insights` is populated when alpha is producing signals.
- `last_portfolio_run_at` advances only when portfolio cadence is due.
- `last_portfolio_target_batch.metadata.cadence` is `every_5_minutes`.

In normal operation, alpha runs every cycle. Portfolio construction rebuilds
targets every five minutes and reuses the previous target batch between those
cycles. Risk, execution, open-ticket polling, and bounded fill reconciliation
continue on the live loop cadence.

## Telegram Operator Note

Send a concise status after the loop is started:

```powershell
@'
KRX LEaps live loop ready.
- interval: 60 seconds
- alpha: every cycle
- portfolio: 5 minute cadence
- framework-state: enabled
- reconcile: every 5 cycles or after order submit
- preflight: OK
- open orders: 0
'@ | py -3 -m leaps_quant_engine.cli notify-user-message `
  --title "KRX live ready" `
  --message-stdin `
  --summary-only
```

For Korean text, prefer stdin or `--message-file`; do not put long Korean
messages directly into command-line string literals.

## Go / No-Go At 09:00

Go:

- loop process is alive
- `runtime-run-multi-once exit=0`
- `order-runtime-submit exit=0` if there are orders
- submit state saved after a nonzero order submit
- `order-runtime-supervise exit=0`
- `order-runtime-status.needs_attention=false`

No-go:

- local broker or market-data service is unhealthy
- `runtime-preflight` is not `ok`
- open tickets exist and are unexplained
- unallocated fills exist
- `runtime-run-multi-once exit=1` continues after live quotes are usable
- submit state exists from today but the operator expected a new first batch
- `order-runtime-submit` reports blocked guards, oversell, route mismatch, or
  notional limit breach

## Emergency Stop

Find the loop:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*leaps_multi_sleeve_live_order_loop.ps1*' } |
  Select-Object ProcessId,CommandLine
```

Stop only the multi-sleeve live loop:

```powershell
Stop-Process -Id <PID>
```

Then supervise and inspect:

```powershell
py -3 -m leaps_quant_engine.cli order-runtime-supervise configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --broker broker-engine `
  --summary-only

py -3 -m leaps_quant_engine.cli order-runtime-status configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --summary-only
```

Do not delete `multi_sleeve_submit_state.json` during market hours unless the operator
explicitly decides a second same-day submit is intended.

## End Of Session

After close:

```powershell
py -3 -m leaps_quant_engine.cli order-runtime-supervise configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --broker broker-engine `
  --summary-only

py -3 -m leaps_quant_engine.cli order-runtime-status configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --summary-only
```

Expected end state:

- open tickets are `0`
- fills are reflected in the virtual account
- no unallocated fills remain
- Telegram/order reports match the broker state
- the submit state file remains as the audit record for that trade date
