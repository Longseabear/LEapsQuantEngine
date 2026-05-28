# kr-lowvol-defensive Sleeve

This sleeve trades Korean domestic equities with a defensive low-volatility
profile. It is intentionally separate from the LEaps KOSPI growth/PPO sleeve.

## Scope

- Market: KRX domestic equities, KRW only.
- Style: low realized volatility, sufficient liquidity, anti-lottery,
  anti-crowding, no falling-knife momentum, modest trend confirmation.
- Cadence: daily 08:50 KST alpha evaluation, week-start 08:55 KST portfolio
  refresh, and regular-session buy execution window.
- Broker route: paper by default.

## Active Models

- Selection: `selections/lowvol_rank.py`
- Alpha: `alphas/lowvol_defensive.py`
- Portfolio: `portfolios/inverse_vol.py`
- Risk: `risks/basic.py`
- Execution: `executions/immediate.py`

## State

The v2 model is stateless. If later versions add drawdown or rebalance
memory, store it through `context.model_state`, not module globals or files.

## Constraints

- Sleeve models must not call KIS, broker-engine, market-data-engine, yfinance,
  or external APIs directly.
- Alpha emits insights only. Portfolio emits percentage targets only.
- Execution emits order intents only; it does not submit broker orders.
- Live execution allows buys and trim sells. Keep churn controls in the sleeve
  config so routine price drift does not create daily rebalance orders.
- Portfolio construction should keep turnover low without allowing stale
  targets to live forever: unselected flat or losing positions become zero
  targets after 14 stale days, all unselected positions after 28 stale days, and
  fractional-lot expensive entries are skipped unless the desired value covers
  at least 1.1 shares.
- Fundamental, retail, or crowding data must arrive through normalized
  snapshots, fundamentals, or symbol metadata; the models must not fetch it
  directly.
- Keep this sleeve out of `live_multi_sleeve.json` unless the operator
  explicitly opts in after backtests.

## Validation

```powershell
$env:PYTHONPATH='src'
py -3 -m pytest tests\test_kr_lowvol_defensive_sleeve.py -q
py -3 -m leaps_quant_engine.cli runtime-config-validate configs/runtime/kr_lowvol_defensive_sleeve.json
py -3 -m leaps_quant_engine.cli runtime-preflight configs/runtime/kr_lowvol_defensive_sleeve.json --sleeve-id kr-lowvol-defensive --summary-only
```
