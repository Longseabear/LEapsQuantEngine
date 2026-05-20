# LEaps Sleeve Workspace

This workspace owns sleeve-specific strategy code and settings for the `LEaps` sleeve.

Operational notes and live/debugging heuristics live in
[`OPERATIONS.md`](OPERATIONS.md).

Initial layout:

```text
sleeves/LEaps/
  alphas/
    kospi_conviction.py
    krx_etf_safety.py
    us_stability_hedge.py
    momentum.py
    volatility_trailing_stop.py
    etf_rotation.py
  selections/
    stock_momentum.py
    krx_etf_safety.py
    etf_rotation.py
    operational_symbols.py
  portfolios/
    equal_weight.py
    research_adaptive_allocator.py
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
      "selections/krx_etf_safety.py:KrxEtfSafetySelectionModel",
      "selections/etf_rotation.py:EtfRotationSelectionModel",
      "selections/operational_symbols.py:OperationalSymbolsSelectionModel"
    ]
  }
},
"alpha": {
  "input_selections": {
    "leaps-kospi-conviction": "leaps-stock-momentum",
    "leaps-krx-etf-safety": "leaps-krx-etf-safety",
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

The active `live_multi_sleeve` LEaps profile now uses
`portfolios/research_adaptive_allocator.py` with a separate KRX ETF safety
bucket. Stock momentum and pullback insights still form the growth book, while
`leaps-krx-etf-safety` can reserve target weight for KRX cash-like ETFs,
KODEX 200, and the 1x inverse ETF when the KODEX 200 regime deteriorates.
Those ETF insights are explicitly excluded from the stock top-k ranking.

The v4 candidate profile is wired in `configs/runtime/live_multi_sleeve_v4.json`
and uses `portfolios/v4_banded_momentum.py`. It is a deterministic
portfolio-construction model, not a separately trained PPO policy. Alpha still
decides which symbols have actionable UP/FLAT insights; v4 only decides target
continuity and allocation. Its core rules are entry/hold/trim bands
(`entry_top_n=12`, `hold_top_n=60`, `trim_top_n=85`), a three-trading-day
minimum hold preference, hard exit pass-through for stop/exit insights, and a
priority turnover budget (`max_target_turnover_pct=7%` per portfolio run,
`daily_turnover_budget_pct=15%`). It blocks same-day re-entry after a hard exit
or confirmed missing-target exit. A model-level drift threshold exists for
experiments, but the candidate config keeps `target_drift_threshold_pct=0.0`
because a 2.5% threshold reduced order count while worsening the 2026-05-11..15
minute replay drawdown. The turnover cap allocates budget to top ranked,
whole-share-buyable targets first instead of spreading tiny unfillable targets
across many names. This keeps the model LEAN-style while reducing the same-day
target churn seen in minute replays.

The smoke/research RL config remains available via `portfolios/rl_ppo_constructor.py`,
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

Experimental v2 state-aware PPO profile:

```text
config = artifacts/backtests/strategy_sweep_20260517/leaps_rl_v2_state_candidate_runtime.json
universe = configs/universes/leaps_kr_research_200.json
feature_schema = v2_state
top_k = 30
attention layers = 2
training window = 2021-01-04..2025-12-30
training cash = 17,329,806 KRW
rollout_length = 256
random_rollout = true
max_target_turnover_pct = 0.25
target_smoothing_alpha = 0.55
```

This candidate keeps alpha/ranking outside PPO. PPO receives the ranked top-k
tokens and allocates weights/cash; the deterministic portfolio layer then
persists target anchors, applies target drift smoothing, and caps per-cycle
target turnover before risk/execution sizing. Treat it as a research candidate
until it beats the active rule-based allocator on held-out Sharpe, MDD, and
operator-acceptable turnover. The runtime-compatible `v2_state` schema remains a
single-cycle top-k token tensor; `lookback_window` is used for warmup and random
rollout eligibility in that profile.

Temporal PPO research profile:

```text
feature_schema = v2_temporal
observation_shape = [lookback_window, top_k, feature_dim]
default lookback_window = 64
feature extractor = TemporalPortfolioFeaturesExtractor
```

This profile trains on the actual candidate time series instead of duplicating
the latest token. Training may use a price-matrix ranking proxy as the research
alpha, but runtime/live portfolio construction is alpha-gated: PPO only sees
active UP insights whose metadata includes a point-in-time
`rl_temporal_features` window. If alpha emits no signal, or emits a signal
without the temporal feature window, the temporal PPO path creates no new
entry target. Portfolio state fields (`current_weight`,
`previous_target_weight`, `current_exposure`) are populated only on the final
time step so historical rows do not pretend to know today's holdings. Do not
wire a `v2_temporal` policy into live by repeating the current observation.

The engine now supplies these windows through `TemporalFeatureWindowProvider`
when the portfolio config uses a temporal `feature_schema`. LEaps alpha modules
copy `context.metadata_value(symbol_key, "rl_temporal_features")` into the
insight metadata only after they decide to emit an UP insight. That keeps PPO
alpha-gated and point-in-time.

Alpha-score research variant:

```text
feature_schema = v2_temporal_residual
observation_shape = [lookback_window, top_k, 13]
default lookback_window = 84
candidate score = residual momentum + total momentum + recent return
                  + trend quality - volatility/drawdown penalties
candidate buckets = enhanced momentum top-k + large-cap core reserve
```

This variant follows the residual/risk-managed momentum literature more closely.
It ranks on stock-specific momentum after removing an equal-weight market beta
proxy, penalizes noisy and drawn-down trends, and reserves a small candidate
bucket for the largest liquid KRX stocks. The reserve is meant to keep strong
but less explosive core names, such as Samsung Electronics, visible to PPO
instead of letting pure high-beta momentum consume every top-k slot. It remains
research-only until it wins held-out shape metrics; the runtime can supply the
required temporal windows.

CUDA deep candidate:

```text
config = artifacts/backtests/strategy_sweep_20260517/leaps_rl_v2_state_deep_candidate_runtime.json
policy = data/rl/v2_state_ppo_20260517/top30_deep_s2701/leaps_ppo_portfolio_allocator.zip
metadata = data/rl/v2_state_ppo_20260517/top30_deep_s2701/leaps_ppo_portfolio_allocator.json
torch = 2.6.0+cu126
training_device = cuda
timesteps = 20,000
attention layers = 4
attention embed dim = 64
features dim = 128
```

The deeper CUDA run improves drawdown versus the first shallow v2 candidate, but
it does not dominate it on OOS return or Sharpe. Treat the deep artifact as a
comparison checkpoint rather than the live default.

The selected runtime profile is the direct allocator:

```text
attention PPO allocator
top_k = 8
allocation_mode = rl_weights
action_space = Box(top_k + 1)
action = top-k asset scores + cash score
selected search profile = identity_turnover_top8_compact
target_smoothing_alpha = 1.0
target_drift_threshold_pct = 0.035
```

Expanded-universe candidate:

```text
config = configs/runtime/leaps_workspace_kr200_candidate.json
universe = configs/universes/leaps_kr_research_200.json
symbols = KRX turnover top 200, FDR names attached
active max symbols = 60
top_k = 12
training cash = 17,329,806 KRW
policy ensemble = seeds 941, 947
target_smoothing_alpha = 0.6
target_drift_threshold_pct = 0.05
```

The candidate is intentionally kept separate from the live config. It expands
the opportunity set for KRX momentum, but should be promoted only after the
operator accepts the higher turnover and broader small/mid-cap exposure.

The earlier gross-exposure controller remains available for comparison and
fallback, but it is no longer the active LEaps runtime mode.

The target drift guard uses `context.model_state` and `StatePatch` anchors in
the `target_anchor` namespace. It does not delay large target changes; it only
reuses the previous target when the new target is within 3.5 percentage points,
which suppresses small rank/noise churn while keeping explicit FLAT/DOWN stop
exits immediate.

Engine target resolution now runs before portfolio blend. LEaps treats portfolio
output as a complete desired target set: symbols present in the previous target
snapshot but missing from the new valid target set are resolved to explicit 0%
targets, then the blend layer fades them out over the configured transition
window unless the target is tagged as an urgent exit. Empty raw target batches
remain no-action by default; explicit 0% targets are required when a model wants
to close everything.

Alpha v0.3 notes:

- `leaps-kospi-conviction` is the active KRW growth alpha. It only emits KRX
  UP insights and reflects the working thesis that KOSPI upside should receive
  the primary risk budget. Its current score includes recency-weighted
  momentum, trend strength, entry-timing metadata, liquidity, and a
  volatility-regime adjustment. In high volatility it favors confirmed
  breakouts near 20-day highs and penalizes choppy pullbacks or negative
  short-term momentum.
- `leaps-kospi-pullback-reversion` is active as the timing alpha for strong KRX
  stocks. It looks for healthy pullbacks or shallow rebreaks inside an existing
  uptrend. In volatile windows it no longer treats every pullback as buyable:
  falling pullbacks must stabilize first, while positive rebreaks can still
  pass with a smaller volatility haircut.
- `leaps-kospi-swing-rebalance` is active as the swing alpha for a choppy
  uptrend. It buys liquid KRX names on controlled pullbacks while 5-day and
  20-day momentum stay positive, emits partial-trim FLAT insights on 10-day
  moving-average breaks, volatility shocks, or near-high overextension, and
  emits full-exit FLAT insights on 20-day moving-average breaks.
- `leaps-krx-etf-safety` is active in `live_multi_sleeve` and the KR200
  candidate config. It reads KODEX 200 daily indicators as the local market
  proxy and emits target-bucket ETF insights for cash-like KRX ETFs, KODEX 200,
  or KODEX Inverse. In shock regimes it now treats defense as a replacement
  book rather than a small overlay: stock gross is capped near 20%, cash-like
  ETFs can receive roughly 60%, and KODEX Inverse can receive roughly 20%
  before portfolio/risk clamps. The portfolio model consumes these as a
  separate safety bucket instead of mixing them into stock ranking.
- `leaps-us-stability-hedge` is kept as a research module, but it is not active
  in the current LEaps live profile. US ETF rotation/stability is handled by the
  separate `us_etf_rotation` sleeve.
- `leaps-volatility-trailing-stop` remains the exit/risk-reduction alpha for
  operationally watched or held symbols. It reads prior high-watermark state
  through `context.model_state` and requests updates with `StatePatch` records
  in the `trailing_stop` namespace; it does not read or write virtual account
  files directly.
- `risks/kospi_growth_us_hedge.py` currently applies the KRW growth budget and
  regime exposure cap for this KRX-only LEaps profile. It also has an
  intraday KODEX 200 guard for minute/live cycles: before 09:40 it freezes new
  stock entries, then it freezes or risk-off clamps KRW exposure when KODEX 200
  is weak versus the prior daily reference or rolls over from the session high.
  Defensive KRX ETF targets are exempt from the entry freeze and from the
  intraday guard's stock-risk exposure cap, so cash-like and inverse ETF
  protection can still be approved while ordinary stock exposure is reduced.

Execution v0.2 notes:

- `executions/leaps_immediate.py` is session-aware through the engine
  `ExecutionContext`. Daily alpha and RL portfolio training remain valid; the
  session policy is an execution overlay applied after targets are approved.
- Regular open/close auctions reduce new buy size, while sells keep full size.
- KRX pre-open after-hours, KRX after-hours close, US pre-market, and US
  after-market reduce new buy size more aggressively, while exits and
  reductions keep full size.
- KRX after-hours single-price remains blocked by default unless symbol/venue
  support is explicitly verified later.

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

The current RL allocator remains operationally valid but can be turnover-heavy
without a target drift guard. Treat turnover tuning as portfolio/risk policy
work, not as a reason to put order or state side effects inside alpha or
portfolio modules.

## Train/Live Parity Notes

The LEaps owner must verify that RL portfolio training constraints and live
portfolio-construction constraints match before promotion.

2026-05-18 train/live mismatch:

- Training used `allocation_mode=rl_weights` and allowed PPO to emit direct
  candidate weights plus cash weight.
- Training applied turnover control and integer-lot sizing, but it did not
  apply a hard single-name `max_position_pct = 10%` cap.
- Live config added the hard 10% cap inside portfolio construction. The model
  wanted roughly 88% gross exposure across three names, but live clipped the
  three raw weights to 10% each and left the clipped exposure in cash.
- The resulting 25-30% exposure was therefore a live post-processing artifact,
  not a learned risk-off allocation.

Current correction: live LEaps inference should not use a portfolio-stage hard
single-name cap unless the policy was trained with that exact cap and
redistribution logic. Single-name concentration limits should be enforced in
`risks/kospi_growth_us_hedge.py` unless a future PPO policy is retrained with
matching capped-allocation behavior.

The live intraday market guard is intentionally smooth rather than a hard
entry cutoff. `KRX:069500` weakness lowers the KRW gross-exposure cap along a
configured curve, while `intraday_guard_hard_entry_freeze=false` lets risk
approve entries that fit inside the reduced cap. This keeps the sleeve from
going all-or-nothing on a single KODEX 200 tick while still cutting risk budget
as the market deteriorates.

It also uses a recovery release rule so the cap is not pinned to the absolute
selloff level forever. The guard stores the session low in model state; after a
low-to-current rebound is large enough and confirmed for multiple live cycles,
the cap can lift from the risk-off budget to a smaller probe budget before the
market fully recovers. This keeps rebound participation controlled instead of
switching directly from defensive exposure back to full risk.

The live risk model also has a per-symbol guard. The market guard answers "how
much KRW exposure may the sleeve carry?", while the per-symbol guard answers
"may this specific stock be added, held, reduced, or exited?" In live config it
blocks adding to a held loser once unrealized loss is around -1.5%, blocks new
or additional buys after sharp intraday selloffs, halves positions around
deeper symbol-level damage or 10-day-line breaks, and exits on severe loss or
20-day-line breaks. Cash-like and inverse safety ETFs are exempt so defensive
rotation is not blocked by stock-specific rules. For stocks with volatility or
ATR-style metadata from active alpha insights, live risk scales loss/drawdown
thresholds around a 4% reference volatility: low-volatility names get tighter
reduce/exit thresholds, high-volatility names get wider noise bands, and entry
blocking has its own multiplier cap so high-volatility pullbacks do not become
automatic add candidates.

Promotion checklist:

- Confirm policy metadata path, policy zip path, config hash, and runtime
  config point to the same intended model.
- Compare live portfolio parameters against training metadata:
  `allocation_mode`, `feature_schema`, `lookback_window`, `top_k`,
  `max_target_turnover_pct`, action space, cash handling, integer-lot behavior,
  and any smoothing/drift/cap settings.
- Run a raw-action diagnostic from the latest framework state and compare raw
  PPO weights to final portfolio targets. Identical repeated weights should be
  treated as a clamp symptom until proven otherwise.
- Attribute every exposure difference to a named deterministic layer:
  turnover cap, integer-lot rounding, order-sizing threshold, risk regime,
  intraday guard, or session/execution policy.
- After live reload, inspect the next non-reused portfolio target batch before
  interpreting exposure or order behavior.

`configs/runtime/leaps_workspace_smoke.json` now points LEaps at
`configs/universes/leaps_kr_research_core.json`, a KRX research universe. US
ETF rotation/stability runs in its own sleeve. The indicator plan includes the
names used by the configured selectors and alphas:

- KOSPI conviction: `identity_close`, `ema_8_close`, `sma_20_close`,
  `momentum_5_close`, `roc_20_close`, `stddev_20_close`, `atr_14`,
  `rolling_dollar_volume_20`. `roc_60_close` is registered in the live universe
  with `readiness="optional"`, so models can use it when warmed while shorter
  required indicators still control `warmup_not_ready` entry gating.
- KOSPI pullback/rebreak: `identity_close`, `ema_8_close`, `sma_20_close`,
  `momentum_5_close`, `roc_20_close`, `rolling_max_20_close`,
  `rolling_min_20_close`, `stddev_20_close`, `atr_14`,
  `rolling_dollar_volume_20`
- KOSPI swing rebalance: `identity_close`, `sma_10_close`, `sma_20_close`,
  `momentum_5_close`, `roc_20_close`, `rolling_max_20_close`,
  `stddev_20_close`, `atr_14`, `rolling_dollar_volume_20`
- KRX ETF safety: confirmed daily `identity_close`, `sma_20_close`,
  `momentum_5_close`, `roc_20_close`, `roc_60_close`,
  `rolling_max_20_close`, `stddev_20_close`, and `atr_14`, plus optional
  `live_close` for intraday crash detection. `live_close` is intentionally
  separate so live/minute bars do not advance confirmed daily SMA/ROC windows.
- trailing stop: `identity_close`, `rolling_max_20_close`, `atr_14`,
  `stddev_20_close`, plus optional runtime model state
  `leaps-volatility-trailing-stop/trailing_stop/<symbol>`

The default sleeve remains in the same runtime config with zero cash and no
alpha modules, so research backtests should create targets and order intents
only when run for `--sleeve-id LEaps`.
