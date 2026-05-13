# Reporting Contract

## Live/Paper Portfolio Report

Canonical source: `tools/leaps_portfolio_report.py`.

Required sections:

1. Header
   - sleeve id
   - generated time
   - snapshot status and collected/requested symbol count

2. Portfolio summary
   - cash by currency
   - equity by currency when available
   - gross exposure and percentage
   - current cycle order candidate count

3. Current vs target quantities
   - symbol
   - current quantity
   - target quantity
   - delta quantity
   - status: hold, approved, clamped, rejected, or not_run
   - non-approved reason if present

4. Price context
   - market price for held symbols
   - average price for held symbols

5. Diagnostics
   - snapshot degraded/stale status
   - rejected risk decisions
   - missing price or insufficient cash/position-too-small reasons

Rules:

- Reporting must be read-only.
- Reporting must never call `order-runtime-submit`.
- Do not infer a sell target from absence of an active target.
- If risk decisions do not cover a held symbol, show target equal to current
  quantity and status `hold`.
- If a target is rejected, show the requested target quantity and the reason,
  but do not describe it as a pending order.

## Backtest Report

Required sections:

1. Run metadata
   - command or config
   - sleeve id
   - mode/source
   - period and warmup period
   - cash and currency

2. Performance
   - final equity
   - total return
   - max drawdown
   - average or final exposure
   - turnover when available

3. Pipeline diagnostics
   - selected symbols
   - new and active insights
   - portfolio targets
   - order sizing result
   - risk decisions
   - execution/order/fill counts

4. Data quality
   - warmup readiness
   - failed/missing symbols
   - snapshot quality
   - fundamentals coverage when used

5. Interpretation
   - explain whether zero orders came from no alpha, no target, rounding,
     risk rejection, execution policy, or fill model.

## Incident Report

For unexpected live trades, include:

- exact event time
- order intent id, ticket id, and broker order id
- created/accepted/filled/cancelled chain
- source tag from portfolio/execution
- current virtual account state after fills
- whether full KIS account holdings are intentionally ignored
- whether the event was intended, blocked, operational, or bug-like
