---
name: leaps-reporting
description: Use when creating, scheduling, debugging, or interpreting LEapsQuantEngine reports for live trading, paper trading, or backtesting, especially portfolio current-vs-target quantity reports, order lifecycle status, Telegram operator reports, cycle journal summaries, and backtest performance/diagnostic reports.
---

# LEaps Reporting

## Overview

Use this skill to produce operator-readable reports without changing trading
state. Reports must make the current state, target state, order lifecycle, and
diagnostic reason clear enough for an agent or human to decide the next action.

## Report Types

Use **Live/Paper Portfolio Report** when the user asks what the engine is doing
now, wants hourly Telegram updates, or asks current vs target quantities.

Use **Backtest Report** when the user asks how a sleeve performed over a period,
why orders did or did not happen, or how a model behaved in replay.

Use **Incident Report** when unexpected buys/sells, stale snapshots, rejected
orders, or cash/account mismatches are being investigated.

## Live/Paper Portfolio Report

Use the repo helper:

```powershell
py -3 tools/leaps_portfolio_report.py --config configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --notify
```

This is read-only. It runs `runtime-run-once`, builds a current-vs-target
quantity report, and sends it through `notify-user-message` when `--notify` is
present.

The report must include:

- snapshot quality and coverage
- cash, equity, gross exposure, and exposure percentage
- order candidate count for this cycle
- per-symbol current quantity, target quantity, and delta
- risk status/reason for rejected or clamped targets
- market price and average price for held positions

For an hourly Telegram process, use:

```powershell
Start-Process -FilePath powershell.exe `
  -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','tools\leaps_portfolio_report_loop.ps1','-IntervalSeconds','3600','-Config','configs/runtime/leaps_workspace_smoke.json','-SleeveId','LEaps') `
  -WindowStyle Hidden -PassThru
```

Check it with:

```powershell
Get-Process -Id <pid>
Get-Content data/runtime/portfolio-reports/leaps_portfolio_report_loop.log -Tail 80
```

Never submit orders from a reporting process.

## Backtest Report

Prefer the `leaps-backtesting` skill for command selection. After running a
backtest, summarize:

- sleeve id, runtime config, source, cash, currency, start/end, warmup start
- final equity, return, MDD, exposure, turnover, order count, fill count
- insight count by alpha/model when available
- rejected/clamped risk decisions and no-order reasons
- current-vs-target quantity examples from key cycles if diagnosing behavior
- data quality: warmup readiness, missing symbols, fundamentals availability

When the request is diagnostic, re-run with insight and journal artifacts where
supported:

```powershell
--include-insights --journal data/runtime/<name>.jsonl
```

If a report says zero orders, inspect the pipeline in this order:

```text
selection -> indicators/warmup -> alpha -> active insights -> portfolio targets
-> order sizing -> risk/guard -> execution -> order runtime/fill model
```

## Incident Report

For live issues, gather these before answering:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli order-runtime-status configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --recent-events 20 --summary-only
py -3 tools/leaps_portfolio_report.py --config configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps
```

Also inspect:

```text
data/virtual-accounts/kis_domestic.json
data/order-runtime/kis_domestic.jsonl
data/runtime/leaps_live_operator_latest.json
data/cycle-journal/leaps_workspace_smoke.jsonl
```

Report whether the event was:

- strategy intended: alpha/portfolio/risk/execution agree
- risk/guard blocked: target exists but rejected or clamped
- operational: stale snapshot, unsupported route, open-ticket issue
- bug-like: state transition or target persistence produced unintended orders

## Formatting Rules

Use Korean operator wording when reporting to the user or Telegram. Keep it
compact and concrete. For live reports, prioritize quantities and order status
over prose.

Do not show a missing target for a held position as `target 0` unless there is
an explicit sell/exit target or approved risk decision to zero. If a current
holding has no delta this cycle, show it as `hold`.

## References

Read `references/reporting-contract.md` when changing report content, fields,
or scheduling behavior.
