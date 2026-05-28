# LEaps Reporting Guide

This guide defines the reporting split for LEapsQuantEngine.

## Report Families

Live and paper reports answer: what does the engine see now, what does it want
to hold, and did any order lifecycle event happen?

Backtest reports answer: how did a sleeve behave over a replay period, and why
did performance/orders look the way they did?

Incident reports answer: why did a specific buy/sell/rejection/cash mismatch
happen?

## Live Portfolio Report

Use:

```powershell
py -3 tools\leaps_portfolio_report.py --config configs\runtime\live_multi_sleeve.json --sleeve-id LEaps --notify
```

The helper is read-only. By default it uses `--mode latest-target`, which reads
the latest live-cycle artifacts instead of running a fresh model cycle. It
compares current virtual account quantities against the last persisted
live-cycle target/order candidates and can send the message to Telegram through
the engine notification module.

Report modes:

- `--mode latest-target`: default operator mode. Reads virtual account,
  order-runtime status, framework-state, cycle journal, and the latest
  `multi_sleeve_candidate_orders.json`. It does not collect market data or run
  alpha/portfolio/risk/execution again.
- `--mode fast-current`: fastest current-state mode. Shows account/order state
  and open tickets while hiding latest targets.
- `--mode recompute`: diagnostic mode. Runs the old sleeve-scoped
  `runtime-run-once` path and recomputes snapshot, alpha, portfolio, risk, and
  execution.

Live trading itself uses the multi-sleeve single runner:

```powershell
py -3 -m leaps_quant_engine.cli runtime-run-multi-once configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id us_etf_rotation `
  --summary-only
```

Do not infer live order-loop health from the portfolio report process alone.
Portfolio reports are sleeve-scoped read models; order submission is owned by
`tools/leaps_multi_sleeve_live_order_loop.ps1`.

After a reboot, use the safe-start wrapper instead of hand-starting each
process:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File tools\leaps_safe_start_live_stack.ps1
```

It preserves the active sleeve file, checks KIS gateway and broker-engine
through HTTP health, checks loops through heartbeat JSON artifacts, runs strict
live preflight, starts only missing services/loops, and writes the final health
summary to `data/runtime/startup/leaps_safe_start_live_stack_status.json`. Use
`-DryRun true -VerifySeconds 0` to inspect what it would do without starting
missing components. PID/process scanning is opt-in only with `-UseProcessScan
true`; normal reporting/runbook checks should use heartbeat artifacts.

The message is UTF-8 Korean text and includes:

- sleeve equity, cash, stock exposure, active insight count, and order-intent count
- report source: latest live-cycle, fast current, or recompute
- current quantity vs target quantity for each held/targeted symbol
- symbol names when the universe file or common mapping knows them
- current holding unrealized PnL, cumulative estimated realized PnL, and
  combined estimated PnL
- risk clamp/reject reasons such as `max_position_pct` or
  `insufficient_cash_or_position_too_small`
- portfolio blend status/progress when
  `portfolio_target_batch.metadata.portfolio_blend` is present
- current cycle order candidates

Telegram delivery uses legacy `Markdown` parse mode for this helper. The
default current-vs-target and order-candidate sections are mobile-first stacked
text blocks. This avoids the horizontal wrapping that pipe/code-block tables
cause on phones. Use `--layout table` only for temporary desktop diagnostics.

Realized PnL in this report is labeled as `누적 실현 추정` because it is
reconstructed from the virtual account fill ledger using FIFO. It should not be
read as the current open position's PnL; use the per-symbol `미실현` line for
the current holding and `보유+누적` when both numbers are shown together.

`runtime-run-once` and `runtime-run-multi-once` emit compact engine status
objects in JSON output. Agents should prefer those compact objects for quick
health/status checks and use the full `framework` / `portfolio_state` payloads
only for deeper diagnostics.

Use `--mode recompute` only when the operator explicitly wants a fresh
hypothetical target. Routine Telegram reports should stay on `latest-target` so
reporting does not compete with the live order loop or KIS request budget.

## Runtime Artifact Status

Use this when an agent needs to know where the live runner, account stores,
order stores, report loops, framework state, cycle journal, and snapshot store
actually are:

```powershell
py -3 -m leaps_quant_engine.cli runtime-artifact-status configs/runtime/live_multi_sleeve.json `
  --active-only `
  --summary-only
```

The command is read-only. It loads the runtime config, reads the active-sleeve
file, and reports concrete artifact paths plus existence/modified-time metadata.
It must not sync KIS, run alpha/portfolio/risk/execution, submit orders, or
mutate virtual accounts.

Sleeve agents should use this command before inspecting logs or stores. Do not
guess from the sleeve workspace path. The runtime config owns live wiring and
account routes; `data/runtime`, `data/order-runtime`, `data/virtual-accounts`,
`data/cycle-journal`, and `data/eod-snapshots` own runtime state/read models.

## Operator UI

Use:

```powershell
py -3 -m leaps_quant_engine.cli operator-ui configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --sleeve-id kr-lowvol-defensive `
  --sleeve-id kr-domestic-4401 `
  --sleeve-id semiconduct-kor `
  --sleeve-id us_etf_rotation
```

The operator UI is read-only and snapshot-only. It reads local runtime config,
order runtime state, virtual-account state, cycle journal entries, recovery
status, health status, and synthetic market-session reports. It must not call
KIS, live market-data providers, broker gateways, or runtime control queues.
Only pass sleeves that are in the live active set. If a sleeve such as
`semiconduct-kor` is active in `data/runtime/live-order-loop/multi_sleeve_active_sleeves.json`,
include it in the UI process as well; otherwise the dashboard will hide its
virtual cash and holdings even when the account store is correct.

For the fixed tailnet URL, use the idempotent wrapper:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File tools\leaps_operator_ui_tailscale_serve.ps1 `
  -InstallScheduledTask true
```

It keeps the local UI on `127.0.0.1:8876` and persists Tailscale Serve on
`https://book-1ht7n5b40k.tail012ad1.ts.net:8877/` plus the HTTP fallback
`http://book-1ht7n5b40k.tail012ad1.ts.net:8876/` without changing existing
Tailscale Serve entries on 443, 8443, or 10000. The scheduled task runs at user
logon and reapplies the same fixed port/hostname mapping. If Windows denies
scheduled-task registration, the wrapper creates the same command in the user's
Startup folder as a fallback.

The UI separates three portfolio value surfaces:

- `EOD`: after-hours daily-performance snapshots from `data/eod-snapshots`.
  EOD may remain in the payload for diagnostics, but the default sleeve summary
  and sleeve detail views should not use EOD as their primary return display.
- `Current estimate`: first tries the local quote-lane snapshot store configured
  by `market_data.snapshot_store_path`, then virtual-account cash/holdings and
  sleeve framework-state targets. It shows estimated equity, stock value,
  cumulative realized P&L estimate from the virtual-account fill ledger,
  current unrealized P&L, their combined total P&L/return using current book
  value (cash plus holding cost basis) as the return denominator, cash-flow
  adjusted today `+/-` and today `%` versus the latest EOD snapshot, and
  per-symbol held `%` versus target `%`. It may fall back
  to `data/runtime/live-order-loop/multi_sleeve_runtime_run_latest_by_sleeve.json`
  only when no quote snapshot exists yet. If the selected source is missing,
  stale, or lacks prices for held symbols, the UI must mark the estimate
  unavailable or stale instead of silently falling back to EOD or cost basis.
- `Cost basis`: virtual-account cash plus holding cost basis from the account
  store. This is not market-value equity.

Sleeve summary cards should be visual-first and sorted by current total return.
The top of the summary panel shows a compact all-sleeve total for equity, total
P&L, Today P&L with Today %, and sleeve count. In each sleeve card the large number is
current total return with total P&L beside it, and the same Total return block
includes Today `+/-` and Today `%` as a compact subline. The compact chart
splits total/realized/unrealized P&L, and the asset bar shows stock versus cash
exposure. Use EOD only as the baseline for Today fields or as a separate
diagnostic surface, not as the primary sleeve return display. The sleeve detail
view should support left/right navigation between sleeve sections.

For CI or quick agent inspection, the same payload can be printed without
starting the HTTP server:

```powershell
py -3 -m leaps_quant_engine.cli operator-ui configs/runtime/live_multi_sleeve.json --snapshot-only
```

Position lifecycle state is persisted by the virtual account store, not by
reporting. A report may display fields such as entry time, high-watermark price,
or latest stop price after those fields are wired into the report payload, but
the source of truth remains the store's `PositionState` records that are updated
from fills and explicit price marks.

Phase-scheduled process:

```powershell
Start-Process -FilePath powershell.exe `
  -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','tools\leaps_portfolio_report_loop.ps1','-ScheduleMode','phase','-MarketScope','domestic','-IntervalSeconds','60','-Config','configs/runtime/live_multi_sleeve.json','-SleeveId','LEaps','-Title','LEaps') `
  -WindowStyle Hidden -PassThru
```

The live loop sends only one successful report per market-local date and phase:

- domestic: `pre_market` 08:30-09:00 KST, `regular_market` 09:00-15:30 KST, `after_market` 15:40-18:30 KST
- overseas: `pre_market` 04:00-09:30 ET, `regular_market` 09:30-16:00 ET, `after_market` 16:00-20:00 ET

If a phase attempt fails because live quotes are not yet available, the loop logs
the failure and retries inside that phase without marking the report as sent.
Once a report succeeds, the state file blocks duplicate notifications for that
same `market_date|phase`.

Telegram routing is split by category. Portfolio reports use the default
`LEAPS_TELEGRAM_BOT_TOKEN` / `LEAPS_TELEGRAM_CHAT_ID` route. Order submit,
supervisor, and fill lifecycle notifications use `category=order`, which routes
to `LEAPS_ORDER_TELEGRAM_*` when set, otherwise the existing
`STOCKPROGRAM_TELEGRAM_*` bot. Do not send routine portfolio reports through
the order/fill bot.

The report must show held positions as `hold` unless there is an explicit
sell/exit target. A missing target alone is not a sell instruction.

## Operator Cash Availability

Use this when deciding whether cash can be moved into a sleeve:

```powershell
py -3 -m leaps_quant_engine.cli sleeve-cash-availability `
  configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --summary-only
```

The command is read-only. It reads the virtual account store, the latest broker
cash snapshot stored there, and the residual `default sleeve` cash. The
`available_cash_by_currency` field is the amount that can be explicitly
transferred from `default sleeve` into the selected sleeve without changing
broker state.

Planned cash-policy fields:

```text
usable_cash
reserve_cash
temporary_buffer_limit
temporary_buffer_used
restore_required
```

These are not implemented yet. When added, live reports should show them
separately from ordinary cash so operator reserve money is not confused with
normal strategy capital.

## Backtest Report

Use `runtime-backtest-daily` or `runtime-backtest-minute` depending on the
research question. Prefer the repo skill `.codex/skills/leaps-backtesting` for
command details.

Backtest summaries should include run metadata, performance, data quality,
insight counts, target counts, risk decisions, execution counts, and a concise
explanation for zero-order or high-turnover behavior.

## Incident Report

When a live trade is surprising, collect:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli order-runtime-status configs/runtime/live_multi_sleeve.json --sleeve-id LEaps --sleeve-id us_etf_rotation --recent-events 20 --summary-only
py -3 tools\leaps_portfolio_report.py --config configs\runtime\live_multi_sleeve.json --sleeve-id LEaps
```

Then inspect the order runtime JSONL, virtual account store, latest live
operator artifact, multi-sleeve live loop log, and cycle journal. Classify the
event as strategy intended, risk/guard blocked, operational, or bug-like.
