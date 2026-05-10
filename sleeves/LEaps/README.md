# LEaps Sleeve Workspace

This workspace owns sleeve-specific strategy code and settings for the `LEaps` sleeve.

Initial layout:

```text
sleeves/LEaps/
  alphas/
    kospi_conviction.py
    us_stability_hedge.py
    momentum.py
    volatility_trailing_stop.py
    etf_rotation.py
  selections/
    stock_momentum.py
    etf_rotation.py
    operational_symbols.py
  portfolios/
    equal_weight.py
    rl_ppo_constructor.py
  risks/
    kospi_growth_us_hedge.py
    basic.py
  executions/
    leaps_immediate.py
    immediate.py
```

Runtime configs can set:

```json
"workspace_path": "sleeves/LEaps"
```

With that setting, relative strategy module references such as `alphas/momentum.py` and `portfolios/equal_weight.py` resolve inside this workspace.

Selection models can be wired with workspace-relative `module.py:ClassName` references:

```json
"universe": {
  "active": {
    "selection_models": [
      "selections/stock_momentum.py:StockMomentumSelectionModel",
      "selections/etf_rotation.py:EtfRotationSelectionModel",
      "selections/operational_symbols.py:OperationalSymbolsSelectionModel"
    ]
  }
},
"alpha": {
  "input_selections": {
    "leaps-kospi-conviction": "leaps-stock-momentum",
    "leaps-us-stability-hedge": "leaps-etf-rotation",
    "leaps-volatility-trailing-stop": "leaps-operational-symbols",
  }
}
```

This keeps selectors and alpha modules independent. Config decides which selected symbols each alpha receives.

Manage active alpha modules through the runtime config:

```powershell
py -3 -m leaps_quant_engine.cli sleeve-alpha-list configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-alpha-enable configs/runtime/leaps_workspace_smoke.json alphas/momentum.py --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-alpha-disable configs/runtime/leaps_workspace_smoke.json alphas/momentum.py --sleeve-id LEaps
```

Manage the active portfolio construction model the same way:

```powershell
py -3 -m leaps_quant_engine.cli sleeve-portfolio-list configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-portfolio-set configs/runtime/leaps_workspace_smoke.json equal_weight --sleeve-id LEaps
```

After changing active alpha modules or the portfolio model, send the emitted `reload_sleeve` command to apply the new config at a runtime boundary.

The active LEaps research config now uses `portfolios/rl_ppo_constructor.py`,
which wraps a Stable-Baselines3 PPO policy ensemble. In the active
`allocation_mode=rl_weights` profile, PPO directly emits a top-k asset weight
vector plus a cash weight. The runtime maps those weights onto the ranked active
insights and then lets risk clamp currency/position limits. Quantity sizing,
risk checks, execution, order intents, and simulated fills remain in the engine
framework.

Train or refresh the local PPO policy with FinanceDataReader history:

```powershell
py -3 -m leaps_quant_engine.cli train-rl-portfolio-constructor configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2021-05-10 --end 2024-12-31 --source finance-datareader --timesteps 8000 --seed 53 --output-dir data/rl --summary-only
```

The command writes ignored local artifacts:

```text
data/rl/leaps_ppo_portfolio_allocator.zip
data/rl/leaps_ppo_portfolio_allocator_seed53.zip
data/rl/leaps_ppo_portfolio_allocator.json
```

The reward profile follows the FinRL Contest lesson that single-policy finance
RL is unstable: train multiple PPO seeds and use median-action ensemble
inference. Reward is shape-aware, penalizing downside return, rolling volatility,
drawdown increase, underwater time, turnover, and missed upside when a positive
basket is ignored.

The policy now uses an attention feature extractor. Selectors and alpha modules
still create the candidate set; the constructor converts the top-k candidates
into asset tokens and lets the attention encoder model cross-asset relationships
before PPO chooses direct candidate and cash weights.

The selected runtime profile is the direct allocator:

```text
attention PPO allocator
top_k = 8
allocation_mode = rl_weights
action_space = Box(top_k + 1)
action = top-k asset scores + cash score
selected search profile = identity_turnover_top8_compact
```

The earlier gross-exposure controller remains available for comparison and
fallback, but it is no longer the active LEaps runtime mode.

Alpha v0.2 notes:

- `leaps-kospi-conviction` is the active KRW growth alpha. It only emits KRX
  UP insights and reflects the working thesis that KOSPI upside should receive
  the primary risk budget.
- `leaps-us-stability-hedge` is the active USD stabilizer. It prefers
  defensive/low-volatility/dividend/treasury/gold ETFs over high-beta US
  growth exposure.
- `leaps-volatility-trailing-stop` remains the exit/risk-reduction alpha for
  operationally watched or held symbols.
- `risks/kospi_growth_us_hedge.py` applies different risk budgets by currency:
  KRW can carry the growth exposure, while USD is capped as a stability pocket.

If the policy file is absent, the runtime model falls back to a deterministic
configured exposure level so config validation and smoke tests still run.

Config-based backtest:

```powershell
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2023-05-10 --end 2026-05-08 --cash 2000000 --source finance-datareader --summary-only
```

Runtime backtests now separate indicator warmup from the evaluation window. The
LEaps config has `indicators.warmup_enabled=true`, so
`runtime-backtest-daily` automatically loads pre-start daily bars unless an
operator overrides it with `--warmup-start`. Check `warmup_data_slice_count` in
the report before judging a short or one-day test.

Recent engine-contract checks:

```text
2026-05-08 one-day LEaps:
  warmup_data_slice_count: 64
  framework cycles: 1
  insights: 6
  orders: 2
  collisions: 0

2023-05-10 -> 2026-05-08, KRW 2,000,000 only:
  warmup_data_slice_count: 65
  final equity: 2,351,274 KRW
  return: 17.56%
  MDD: 5.84%
  orders: 512
  collisions: 0

2021-05-10 -> 2026-05-08, configured KRW/USD cash:
  KRW final equity: 12,037,393 KRW
  KRW return: 20.37%
  USD final equity: 3,220.60 USD
  USD return: -6.22%
  collisions: 0
```

For mixed KRW/USD runs, read `metrics_by_currency`. The aggregate `metrics`
block is marked `valid_without_fx=false` because the engine does not yet convert
USD to KRW or KRW to USD for total-equity reporting.

The current RL allocator remains operationally valid but turnover-heavy. Treat
turnover tuning as portfolio/risk policy work, not as a reason to put order or
state side effects inside alpha or portfolio modules.

`configs/runtime/leaps_workspace_smoke.json` now points LEaps at
`configs/universes/leaps_kr_us_research_core.json`, a KR/US mixed research
universe with KRX stocks, US stocks, and US hedge/stability ETFs. The indicator
plan includes the names used by the configured selectors and alphas:

- KOSPI conviction: `identity_close`, `ema_8_close`, `sma_20_close`,
  `momentum_5_close`, `roc_20_close`, `stddev_20_close`, `atr_14`,
  `rolling_dollar_volume_20`
- US stability hedge: `identity_close`, `ema_8_close`, `sma_20_close`,
  `roc_20_close`, `stddev_20_close`, `atr_14`,
  `rolling_dollar_volume_20`
- trailing stop: `identity_close`, `rolling_max_20_close`, `atr_14`,
  `stddev_20_close`

The default sleeve remains in the same runtime config with zero cash and no
alpha modules, so research backtests should create targets and order intents
only when run for `--sleeve-id LEaps`.
