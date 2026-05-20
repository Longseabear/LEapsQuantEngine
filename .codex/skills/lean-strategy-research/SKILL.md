---
name: lean-strategy-research
description: Use when researching QuantConnect LEAN, Quantpedia, or public LEAN-style community strategies, embedded backtest results, strategy leaderboards, model logic, and how to translate public strategy/backtest evidence into LEapsQuantEngine research candidates, especially Korea/KRX proxy ideas such as EWY, FLKR, country ETF rotation, sector ETF rotation, and LEAN alpha/portfolio/risk model patterns.
---

# LEAN Strategy Research

## Overview

Use this skill to collect public LEAN strategy evidence and turn it into a
reviewable LEapsQuantEngine research note. Treat public backtests as leads, not
proof. Always preserve provenance and verify current facts by browsing when the
user asks for recent, latest, best, leaderboard, or current performance.

## Research Workflow

1. Define the scope: market, instrument type, benchmark, recency window, and
   whether the user wants public QuantConnect evidence, Quantpedia models,
   community discussion, or a LEaps implementation plan.
2. Search primary and near-primary sources first:
   - QuantConnect Strategies and Leaderboard pages
   - QuantConnect embedded backtest result pages
   - QuantConnect Forum and LEAN GitHub issues/discussions
   - Quantpedia strategy articles linked from QuantConnect examples
3. Record provenance for every candidate:
   - source URL
   - accessed date
   - embedded backtest generated date, if present
   - strategy name/class
   - data universe and symbols
   - backtest start/end dates if visible
   - fees, brokerage model, capacity, and benchmark if visible
4. Extract comparable performance fields:
   - Compounding Annual Return
   - Net Profit
   - Sharpe Ratio
   - Sortino Ratio
   - Drawdown
   - Win Rate / Loss Rate
   - Total Orders
   - capacity and fees, when shown
5. Read enough model code or article text to classify the model:
   - universe selection
   - alpha/signal
   - rebalance cadence
   - portfolio construction
   - risk management
   - execution/order assumptions
6. Translate into LEaps terms:
   - `Universe`: eligible symbols and refresh cadence
   - `Alpha`: insight direction, confidence, expiry, and reason
   - `Portfolio`: target weights or sizing rule
   - `Risk`: clamps, exits, stop logic, liquidity limits
   - `Execution`: order intent style and rebalance timing
   - `Backtest`: data source, warmup, fee/slippage assumptions

## Reporting Format

Prefer this table for candidate comparison:

| Strategy | Source | Market exposure | Generated | CAR | Sharpe | Drawdown | Net | Model | LEaps fit | Caveats |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |

Then add a short model breakdown:

```text
Universe:
Alpha:
Portfolio:
Risk:
Execution:
Backtest assumptions:
LEaps implementation candidate:
```

## Korea/KRX Proxy Research

Read `references/korea-proxy-snapshot.md` when the task is about Korea,
KOSPI/KOSDAQ, "gukjang", EWY, FLKR, country ETF rotation, or adapting public
LEAN examples to LEapsQuantEngine.

Do not label a US-listed Korea ETF strategy as a domestic Korean stock strategy.
Write it as "Korea exposure via EWY/FLKR" unless the source explicitly uses KRX
cash equities or KRX-listed ETFs.

## Extraction Helper

Use the helper for QuantConnect embedded backtest HTML:

```powershell
py -3 .codex\skills\lean-strategy-research\scripts\extract_quantconnect_backtest.py `
  https://www.quantconnect.com/terminal/cache/embedded_backtest_1f8bbd5874aac17e3e3b65b78deee22a.html `
  --json
```

The helper extracts the generated date, statistic name/value pairs, detected
`QCAlgorithm` classes, and watched symbol mentions such as EWY and FLKR. It is
a convenience parser only; verify important conclusions by opening the source.

## Guardrails

- Browse before answering "latest", "recent", "best", or leaderboard questions.
- Prefer direct embedded backtest pages over copied screenshots or summaries.
- Avoid overfitting to CAR alone; compare drawdown, Sharpe, turnover, fees, and
  capacity.
- Flag survivorship bias, short backtest windows, missing delisted symbols,
  missing fees/slippage, and ETF-vs-domestic-market mismatches.
- Do not recommend live deployment from a public backtest. Recommend a local
  LEaps replay with the same pipeline interfaces first.
