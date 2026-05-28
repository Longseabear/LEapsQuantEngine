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
now, wants phase-based Telegram updates, or asks current vs target quantities.

Use **Backtest Report** when the user asks how a sleeve performed over a period,
why orders did or did not happen, or how a model behaved in replay.

Use **Incident Report** when unexpected buys/sells, stale snapshots, rejected
orders, or cash/account mismatches are being investigated.

## Live/Paper Portfolio Report

Current live operation uses the multi-sleeve single runner:

```text
runtime-run-multi-once configs/runtime/live_multi_sleeve.json
  --sleeve-id LEaps --sleeve-id us_etf_rotation
```

Portfolio reports are still sleeve-scoped and read-only. The default operator
mode is `latest-target`, which reads the latest live-cycle artifacts instead of
running a fresh model cycle. Use `recompute` only for explicit diagnostics.

Use the repo helper:

```powershell
py -3 tools/leaps_portfolio_report.py --config configs/runtime/live_multi_sleeve.json --sleeve-id LEaps --notify
```

This is read-only. It builds a current-vs-target quantity report for one sleeve
from stored live-cycle state and sends it through `notify-user-message` when
`--notify` is present. The default message layout is mobile-first stacked text.
Use `--layout table` only for temporary desktop diagnostics.

Report modes:

- `--mode latest-target`: default. Reads virtual account, order runtime status,
  framework-state, cycle journal, and latest candidate-order artifact. It must
  not collect market data, run alpha, or create fresh order intents.
- `--mode fast-current`: account/order state only. Use when the operator asks
  for actual current holdings, cash, or open tickets without target context.
- `--mode recompute`: diagnostic. Runs `runtime-run-once` and recomputes the
  single-sleeve framework path.

The report must include:

- snapshot quality and coverage
- report source/mode
- cash, equity, gross exposure, and exposure percentage
- order candidate count for this cycle
- open ticket count and open-ticket detail when present
- per-symbol current quantity, target quantity, and delta
- risk status/reason for rejected or clamped targets
- portfolio blend status/progress when
  `portfolio_target_batch.metadata.portfolio_blend` is present
- market price and average price for held positions
- current holding unrealized PnL, cumulative FIFO realized PnL estimate, and
  their combined estimate when both are present

For the live Telegram process, use a phase schedule rather than hourly spam:

```powershell
Start-Process -FilePath powershell.exe `
  -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File','tools\leaps_portfolio_report_loop.ps1','-ScheduleMode','phase','-MarketScope','domestic','-IntervalSeconds','60','-Config','configs/runtime/live_multi_sleeve.json','-SleeveId','LEaps','-Title','LEaps') `
  -WindowStyle Hidden -PassThru
```

Phase mode sends at most one successful report per market-local date and phase:
pre-market, regular-market, and after-market. A failed phase attempt is logged
and retried inside the same phase; it is not marked sent until notification
success. Use `-ScheduleMode interval` only for temporary diagnostics.

Notification routing is category-based: portfolio reports stay on the default
LEaps report bot, while `category=order` notifications such as order submit,
supervisor, and fill lifecycle messages route to the order/fill bot
(`LEAPS_ORDER_TELEGRAM_*`, falling back to `STOCKPROGRAM_TELEGRAM_*`).

Check it with:

```powershell
Get-Process -Id <pid>
Get-Content data/runtime/portfolio-reports/LEaps_portfolio_report_loop.log -Tail 80
```

Never submit orders from a reporting process.

Operator UI value labels are deliberately separate. `EOD` values are
after-hours daily-performance snapshots and may remain in the payload for
diagnostics, but they should not be the primary sleeve summary/detail display.
`Current estimate` first uses the local quote-lane market-data snapshot store
plus virtual-account cash/holdings and framework-state target plans. It displays
estimated equity, cumulative realized PnL estimate from the virtual-account
fill ledger, current unrealized PnL, combined total PnL/return using current
book value (cash plus holding cost basis) as the return denominator, cash-flow
adjusted today `+/-` and today `%` versus the latest EOD snapshot, and held `%`
versus target `%`. Sleeve summary cards should be sorted by current total return
and show total P&L/return with Today `+/-`/`%` inside the same Total return
block, plus compact visual bars for total/realized/unrealized PnL and
stock/cash exposure. The summary panel should include a very compact all-sleeve
total row with Today `+/-` and Today `%` by currency, and the sleeve detail view
should support left/right navigation between sleeves. It may fall back to
`data/runtime/live-order-loop/multi_sleeve_runtime_run_latest_by_sleeve.json`
only before quote snapshots exist. Missing/stale values are marked
stale/unavailable instead of falling back. `Cost basis` is virtual-account cash
plus holding cost basis and is not market-value equity. The UI must not call
KIS, broker gateways, or market-data providers from the HTTP request path.

To check the live submit loop, inspect the multi-sleeve loop heartbeat and log
instead:

```powershell
py -3 -m leaps_quant_engine.cli runtime-health configs/runtime/live_multi_sleeve.json `
  --heartbeat data/runtime/live-order-loop/multi_sleeve_heartbeat.json `
  --heartbeat-component multi_sleeve_live_order_loop `
  --summary-only

Get-Content data/runtime/live-order-loop/multi_sleeve.log -Tail 80 -Encoding UTF8
```

After a reboot or operator machine restart, prefer the idempotent safe-start
wrapper:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File tools\leaps_safe_start_live_stack.ps1
```

It checks KIS Gateway and broker-engine through HTTP `/health`, checks the
multi-sleeve live loop/report loops/EOD scheduler through heartbeat JSON
artifacts, runs strict live preflight, and reads the active sleeve file. It
starts only missing components and writes the final machine-readable summary to
`data/runtime/startup/leaps_safe_start_live_stack_status.json`. Use `-DryRun
true -VerifySeconds 0` when the operator only wants to see what would happen.
The wrapper must not submit manual/ad-hoc orders; live orders still come only
from the multi-sleeve live loop. Windows process/PID scanning is opt-in only
with `-UseProcessScan true`; normal operation should not depend on it.

The multi-sleeve loop hot-reloads at cycle boundaries by draining
`data/runtime/control/live.jsonl`. Add or remove live sleeves with
`runtime-control-submit --command activate-sleeve` and
`runtime-control-submit --command deactivate-sleeve`; removal is blocked when
the sleeve still has holdings or open tickets.

When an agent is unsure which files are live, do not infer paths from memory,
old docs, or a sleeve workspace. Use the artifact index:

```powershell
py -3 -m leaps_quant_engine.cli runtime-artifact-status configs/runtime/live_multi_sleeve.json `
  --active-only `
  --summary-only
```

This is read-only. It reports active sleeves, broker-account routes, virtual
account stores, order stores, framework-state files, report-loop files, cycle
journal, snapshot store, startup status, and live-loop artifacts. It must not
sync KIS, run models, submit orders, or mutate accounts.

The loop also schedule-gates framework runs. By default `LEaps` is eligible
during KRX 08:30-18:30 KST, `kr-lowvol-defensive` during KRX 08:50-15:30 KST,
and `us_etf_rotation` during US regular market hours, 09:30-16:00 Eastern Time.
The schedule gate also blocks weekends and dates listed in
`configs/market-calendars/krx_holidays.json` or
`configs/market-calendars/us_holidays.json`. A skipped sleeve should appear in
the live-order-loop log as `skipped=<sleeve>(outside_schedule:...)`,
`market_weekend`, or `market_holiday:<scope>`.

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

## Sleeve Daily Performance

Use this when the user asks for sleeve daily PnL/return, NAV, or historical
portfolio holdings from live/paper EOD snapshots:

```powershell
py -3 -m leaps_quant_engine.cli sleeve-daily-performance `
  --snapshot-root data/eod-snapshots `
  --sleeve-id LEaps `
  --include-holdings
```

The command reads `data/eod-snapshots`, groups by `sleeve_id + currency + date`,
and calculates daily PnL/return after subtracting net sleeve cash transfers for
that period. This is the LEaps equivalent of LEAN's result/statistics layer,
but sleeve-scoped. When explaining results, call out that the return is
cash-flow adjusted using the virtual account `cash_transfers` ledger, not raw
equity delta.

## Operator Status

Use this when the user asks how much cash can be moved into a sleeve:

```powershell
py -3 -m leaps_quant_engine.cli sleeve-cash-availability `
  configs/runtime/live_multi_sleeve.json `
  --sleeve-id LEaps `
  --summary-only
```

The command is read-only and reports `available_cash_by_currency` from the
residual `default sleeve` cash in the virtual account store. Sync KIS cash first
with `virtual-account-sync-cash` when the user needs a broker-current number.

Use this for EOD snapshot scheduler status:

```powershell
py -3 -m leaps_quant_engine.cli eod-snapshot-status --summary-only
```

## Incident Report

For live issues, gather these before answering:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli order-runtime-status configs/runtime/live_multi_sleeve.json --sleeve-id LEaps --sleeve-id us_etf_rotation --recent-events 20 --summary-only
py -3 tools/leaps_portfolio_report.py --config configs/runtime/live_multi_sleeve.json --sleeve-id LEaps
```

Also inspect:

```text
data/virtual-accounts/kis_domestic.json
data/virtual-accounts/kis_overseas.json
data/order-runtime/kis_domestic.jsonl
data/order-runtime/kis_overseas.jsonl
data/runtime/leaps_live_operator_latest.json
data/cycle-journal/live_multi_sleeve.jsonl
data/runtime/live-order-loop/multi_sleeve.log
```

Report whether the event was:

- strategy intended: alpha/portfolio/risk/execution agree
- operational transition: Portfolio Blend is moving from a previous target
  snapshot toward a new raw target; include progress, duration, and bypassed
  symbols
- risk/guard blocked: target exists but rejected or clamped
- operational: stale snapshot, unsupported route, open-ticket issue
- bug-like: state transition or target persistence produced unintended orders

## Formatting Rules

Use Korean operator wording when reporting to the user or Telegram. Keep it
compact and concrete. For live reports, prioritize quantities and order status
over prose.

Use the mobile-first layout by default. Avoid pipe tables in routine Telegram
portfolio reports because they wrap poorly on phones. Prefer one short block per
symbol:

```text
- 삼성전자 (005930)
  수량 12주 -> 12주 (유지)
  현재 296,000 / 평단 277,293 / 평가 3,552,000
  미실현 +224,481 6.7%
  누적실현 -115,750 / 보유+누적 +108,731
```

Call realized PnL `누적 실현 추정` or `누적실현`, not just `실현`, because
the helper reconstructs it from the virtual account fill ledger using FIFO.
This prevents old closed lots from being confused with the currently held
position's PnL.

Do not show a missing target for a held position as `target 0` unless there is
an explicit sell/exit target or approved risk decision to zero. If a current
holding has no delta this cycle, show it as `hold`.

## References

Read `references/reporting-contract.md` when changing report content, fields,
or scheduling behavior.
