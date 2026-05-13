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
py -3 tools\leaps_portfolio_report.py --config configs\runtime\leaps_workspace_smoke.json --sleeve-id LEaps --notify
```

The helper is read-only. It runs a runtime cycle, compares current virtual
account quantities against freshly sized targets, and can send the message to
Telegram through the engine notification module.

The message is UTF-8 Korean text and includes:

- sleeve equity, cash, stock exposure, active insight count, and order-intent count
- current quantity vs target quantity for each held/targeted symbol
- symbol names when the universe file or common mapping knows them
- unrealized, estimated realized, and combined estimated PnL
- risk clamp/reject reasons such as `max_position_pct` or
  `insufficient_cash_or_position_too_small`
- current cycle order candidates

`runtime-run-once` also emits an `engine_status` object in its JSON output.
Agents should prefer that compact object for quick health/status checks and use
the full `framework` / `portfolio_state` payload only for deeper diagnostics.

Position lifecycle state is persisted by the virtual account store, not by
reporting. A report may display fields such as entry time, high-watermark price,
or latest stop price after those fields are wired into the report payload, but
the source of truth remains the store's `PositionState` records that are updated
from fills and explicit price marks.

Hourly process:

```powershell
Start-Process -FilePath powershell.exe `
  -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','tools\leaps_portfolio_report_loop.ps1','-IntervalSeconds','3600','-Config','configs/runtime/leaps_workspace_smoke.json','-SleeveId','LEaps') `
  -WindowStyle Hidden -PassThru
```

The report must show held positions as `hold` unless there is an explicit
sell/exit target. A missing target alone is not a sell instruction.

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
py -3 -m leaps_quant_engine.cli order-runtime-status configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --recent-events 20 --summary-only
py -3 tools\leaps_portfolio_report.py --config configs\runtime\leaps_workspace_smoke.json --sleeve-id LEaps
```

Then inspect the order runtime JSONL, virtual account store, latest live
operator artifact, and cycle journal. Classify the event as strategy intended,
risk/guard blocked, operational, or bug-like.
