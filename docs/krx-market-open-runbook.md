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
- `08:45-08:50`: Run LEaps runtime preflight, order-runtime status, and
  runtime model-state seed.
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

Preferred safe start:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File tools\leaps_safe_start_live_stack.ps1
```

This is the default recovery command after a reboot. It is idempotent: it
checks KIS Gateway, broker-engine, runtime preflight, the active sleeve file,
the multi-sleeve live loop, phase report loops, and the EOD snapshot scheduler,
then starts only missing components. It writes the machine-readable result to:

```text
data/runtime/startup/leaps_safe_start_live_stack_status.json
```

The script uses `data/runtime/live-order-loop/multi_sleeve_active_sleeves.json`
as the source of truth for active sleeves. A suspended sleeve such as `LEaps`
stays out of the live loop unless the operator explicitly resumes it through
runtime control. The script never submits manual/ad-hoc orders; order submits
still happen only inside `tools/leaps_multi_sleeve_live_order_loop.ps1`.

For diagnostics without starting anything:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File tools\leaps_safe_start_live_stack.ps1 `
  -DryRun true `
  -VerifySeconds 0
```

1. Start or repair local services.

Manual service recovery is now a fallback. The safe-start script starts the
engine-owned KIS Gateway and the local broker-engine boundary when they are
missing, then verifies health before touching the live loop. If doing the old
manual path, check and start services from the StockProgram workspace:

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

3. Seed runtime model state from the virtual account.

Stateful models such as trailing stop must not start from an empty in-memory
state after restart. Seed the model state from the virtual account
`position_states` before the live loop is started. This writes only
`data/runtime/runtime-state/live_multi_sleeve.sqlite`; it does not submit orders
and does not mutate framework active insights or portfolio cadence state.

```powershell
py -3 -m leaps_quant_engine.cli runtime-state-seed-trailing-stop `
  configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --account-store "$RepoRoot\data\virtual-accounts\kis_domestic.json" `
  --runtime-state "$RepoRoot\data\runtime\runtime-state\live_multi_sleeve.sqlite" `
  --summary-only
```

Required readout:

- `status` is `seeded` or `no_positions`
- when LEaps has holdings, `seeded_count` equals the number of open
  `position_states`
- `event_count` equals `seeded_count`

`tools/leaps_safe_start_live_stack.ps1` is the preferred wrapper for service
health, preflight, loop startup, reports, and EOD snapshots. The older
`tools/leaps_start_live_stack.ps1` is a lower-level helper and should not be
used as the morning default because it does not preserve the active sleeve file
as carefully.

For 장중 engine experiments, do not point test cycles at the live runtime state
with write access. Fork the live DB first and run the probe against the fork:

```powershell
py -3 -m leaps_quant_engine.cli runtime-state-fork `
  --source "$RepoRoot\data\runtime\runtime-state\live_multi_sleeve.sqlite" `
  --target "$RepoRoot\data\runtime\runtime-state\sandbox\LEaps_probe.sqlite" `
  --overwrite
```

4. Start the guarded multi-sleeve live loop.

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
  '-IntervalSeconds', '10',
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

Submit notional caps are not enabled by default. Strategy size should be
controlled by portfolio/risk models plus engine guards for cash, oversell,
route/session support, and idempotency. If an operator wants a temporary
blast-radius guard, pass `DomesticMaxSubmitNotional` or
`OverseasMaxSubmitNotional` explicitly; keep them separate because KRW and USD
notionals are not comparable.

The loop drains `data/runtime/control/live.jsonl` at cycle boundaries. Use
`runtime-control-submit --command reload-sleeve`, `activate-sleeve`,
`suspend-sleeve`, `resume-sleeve`, or `deactivate-sleeve` to change the active
sleeve set without restarting the process. Use `suspend-sleeve` for a temporary
pause when the sleeve still owns holdings. Deactivation is rejected while the
sleeve still has holdings or open tickets.

The active sleeve set is additionally filtered by a live schedule before the
framework run. Defaults:

- `LEaps`: KRX 08:30-18:30 KST
- `kr-lowvol-defensive`: KRX 08:50-15:30 KST
- `us_etf_rotation`: US regular market, 09:30-16:00 Eastern Time

The live loop also blocks scheduled sleeves on weekends and configured market
holidays before calling `runtime-run-multi-once`. Default holiday files live at
`configs/market-calendars/krx_holidays.json` and
`configs/market-calendars/us_holidays.json`. Keep these files current before a
market-open safe-start. `runtime-preflight --strict-live` reports a closed
market session as a warning, not a process-start blocker; the live loop may stay
up on weekends/holidays so it can heartbeat and supervise order state, but it
must not run models or submit orders for closed markets.

The skipped sleeve is not passed to `runtime-run-multi-once`, so its market data
is not collected and its alpha/portfolio/risk/execution stack is not called
outside its scheduled strategy window. `order-runtime-supervise` can still
inspect active sleeves for open-ticket maintenance.

After the schedule window, the loop also applies each sleeve's
`worker.cycle_interval_seconds` from runtime config. This lets one
multi-sleeve process keep different runtime cadences without duplicating market
data work. The process `-IntervalSeconds` is only the supervisor wake-up tick;
set it less than or equal to the fastest sleeve cadence you need. Skips appear
in the log as `cadence_wait:<seconds>`.

5. Watch the first cycles.

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

6. Confirm the submit latch.

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

7. Confirm framework cadence state.

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

8. Confirm runtime model state.

For trailing stop, the runtime state store should contain one record per open
position after the seed step or the first successful live cycle:

```powershell
py -3 -m leaps_quant_engine.cli runtime-state-seed-trailing-stop `
  configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --account-store "$RepoRoot\data\virtual-accounts\kis_domestic.json" `
  --runtime-state "$RepoRoot\data\runtime\runtime-state\live_multi_sleeve.sqlite" `
  --summary-only
```

This command is idempotent. It never lowers an existing high-watermark, so it is
safe to repeat during startup recovery.

Portfolio Blend also uses the same runtime state store when
`portfolio.blend.enabled=true`. On the first cycle after enabling it, expect a
`engine-portfolio-blend / last_target` record. During a target transition,
expect an additional `engine-portfolio-blend / active_transition` record. Do not
delete these records during market hours unless the operator intentionally wants
to cancel the smooth transition and restart from the next raw target snapshot.

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

First request a clean cycle-boundary shutdown through the control queue:

```powershell
py -3 -m leaps_quant_engine.cli runtime-control-submit `
  --queue data/runtime/control/live.jsonl `
  --command shutdown `
  --reason "operator emergency stop"
```

Then verify the heartbeat stopped updating:

```powershell
py -3 -m leaps_quant_engine.cli runtime-health configs/runtime/live_multi_sleeve.json `
  --heartbeat data/runtime/live-order-loop/multi_sleeve_heartbeat.json `
  --heartbeat-component multi_sleeve_live_order_loop `
  --summary-only
```

If the worker is genuinely stuck and does not respond, use the `process_id`
inside the heartbeat only as operator context for a manual stop. Do not make PID
scanning the normal liveness check.

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
