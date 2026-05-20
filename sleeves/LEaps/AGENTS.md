# AGENTS.md

## Scope

`LEaps` is an active sleeve workspace. Its models should be runnable by the engine through configured module references.

## Folder Roles

- `alphas/`: prediction models that emit insights.
- `portfolios/`: portfolio construction models that emit target allocations.
- `risks/`: sleeve-level risk models that approve, reject, or clamp sized targets.
- `executions/`: sleeve-level execution models that convert approved targets into order intents.

## Rules

- Keep model code deterministic for the same snapshot/context input.
- Keep broker-specific behavior out of model code.
- Include clear model ids or class names so runtime status can explain which model acted.
- Preserve lineage fields when passing records downstream.
- Update local README or tests when a model's public behavior changes.

## Current Engine Contract For Sleeve Agents

LEaps is a mixed KRW/USD logical sleeve. Treat KRW and USD as separate cash and
equity buckets unless an explicit FX conversion layer is added later. In
backtest reports, prefer `metrics_by_currency`. The aggregate `metrics` block is
only a native-currency sum when both KRW and USD are present and should carry
`valid_without_fx=false`.

Runtime backtests now separate indicator warmup from the evaluation window. When
running `runtime-backtest-daily`, the engine can load pre-start daily bars into
`IndicatorEngine` and only starts alpha/portfolio/risk/execution cycles at
`--start`. Use `warmup_data_slice_count` in the report to confirm this happened.
Do not diagnose a one-day or short-window test as weak signal until warmup is
non-zero or an explicit `--warmup-start` was provided.

Temporal PPO is alpha-gated. When a LEaps portfolio config uses
`feature_schema=v2_temporal` or `v2_temporal_residual`, the engine can attach a
point-in-time daily `rl_temporal_features` window to `SnapshotContext`
metadata. Alpha modules should copy that window into emitted UP insight
metadata after deciding the symbol is actionable. If the window is missing, the
temporal PPO portfolio path must fail closed rather than inventing candidates
or repeating the latest token.

Current LEaps alpha input wiring is intentional:

```text
leaps-stock-momentum       -> leaps-kospi-conviction
leaps-etf-rotation         -> leaps-us-stability-hedge
leaps-operational-symbols  -> leaps-volatility-trailing-stop
```

`leaps-stock-momentum` is KRX-only so US single stocks cannot consume KOSPI
alpha candidate slots. ETF rotation is for US ETF hedge/stability candidates.
Operational symbols exist so held/open/manual symbols remain visible to exit or
stop logic even when they are not selected as fresh entries.

`leaps-volatility-trailing-stop` emits `InsightDirection.FLAT`. Portfolio
construction must respect active FLAT/DOWN insights for a symbol over UP
insights from another alpha at the same or later timestamp. Do not work around
exit signals by emitting broker orders from alpha code.

The active RL portfolio constructor is configured as a complete target
portfolio allocator for KRW buckets. When active KRW UP insights produce a new
target set, any currently held KRW symbol missing from that set should receive
a 0% target tagged `no_longer_in_target_portfolio`. If no actionable KRW
insights exist, do not interpret that as an implicit all-sell signal; rely on
explicit FLAT/DOWN insights or the next valid target set.

Known current strategy risk: the active RL allocator plus daily rebalance can
produce high turnover. If tuning this, adjust portfolio/risk policy such as
rebalance cadence, minimum drift, cooldown, or turnover guard. Do not add hidden
state mutations inside alpha/portfolio/execution modules.

V4 candidate note: `configs/runtime/live_multi_sleeve_v4.json` uses
`portfolios/v4_banded_momentum.py` as a deterministic replacement candidate for
the PPO allocator. The model consumes active alpha insights only, keeps held
symbols through wider hold/trim bands, emits explicit zero targets for confirmed
missing or hard-exit cases, and stores only portfolio/position bookkeeping
through `StatePatch`. Its turnover budget is intentionally priority based: when
the daily budget is tight, allocate to the highest-ranked whole-share-buyable
candidate first instead of thinly spreading target percent across unbuyable
names. If this profile is promoted, keep the live config and backtest config on
the same `max_target_turnover_pct`, `daily_turnover_budget_pct`,
`target_drift_threshold_pct`, `reentry_cooldown_days`,
`entry_top_n/hold_top_n/trim_top_n`, and whole-share guard settings. The
cooldown is an active 5-minute-cycle guard. The drift threshold is available
but disabled in the candidate config after the 2.5% experiment reduced order
count while worsening the 2026-05-11..15 minute replay; do not enable it
without re-running minute replays.

Current intraday risk behavior: LEaps uses `KRX:069500` as the domestic market
guard. The live config enables smoothed intraday guard caps, so the guard should
reduce KRW gross exposure continuously from the base regime cap toward the
risk-off cap instead of hard-blocking every new stock entry at one threshold.
The guard also tracks the same-session low and only releases a recovery probe
cap after a configured low-to-current rebound is confirmed for multiple cycles.
If `intraday_guard_hard_entry_freeze` is re-enabled, document that as a
deliberate kill-switch style override and test it against live target output.

Current per-symbol risk behavior: market guard caps the KRW budget, while the
symbol guard decides whether an individual stock can be added, reduced, or
exited. The symbol guard blocks adding to a held loser, blocks entries after
large intraday selloffs or high-to-current drawdowns, halves positions on
deeper per-symbol loss or 10-day-line breaks, and exits on severe loss,
high-drawdown, or 20-day-line breaks. The live config enables volatility
adjusted symbol thresholds using alpha metadata such as `volatility` or ATR
percent: low-volatility names tighten faster, high-volatility names get wider
held-position noise bands, and entry/add blocks have a separate upper
multiplier so high volatility alone does not loosen new buys too far. Keep
these controls in risk, not alpha or execution, and report their clamp reason
as `symbol_guard_*`.

## Train/Live Parity Checklist

LEaps is an active live sleeve. Treat train/live parity as a release blocker for
portfolio models, especially PPO/RL allocators.

2026-05-18 incident: the temporal PPO allocator was trained without a hard
single-name `max_position_pct` cap in the training environment. Live inference
added `portfolio.parameters.max_position_pct = 0.10`, which clipped raw PPO
weights such as 34%, 31%, and 23% into three equal 10% targets. The clipped
weight was not redistributed, so the model's intended gross exposure collapsed
from roughly 88% to roughly 30% target exposure and about 25% realized exposure.
This was a train/live mismatch, not a deliberate PPO risk-off decision.

Before promoting, reloading, or diagnosing any LEaps RL portfolio profile:

- Compare the policy metadata JSON with the runtime config for
  `allocation_mode`, `feature_schema`, `lookback_window`, `top_k`,
  `max_target_turnover_pct`, integer-lot handling, action space, cash handling,
  and any smoothing or drift settings.
- Do not add portfolio-construction-time constraints in live unless the same
  constraint exists in training, or unless the mismatch is explicitly documented
  as a deterministic live-only overlay with an expected effect.
- Single-name caps are allowed as risk controls, but if they belong in
  portfolio construction they must be trained with the same cap and
  redistribution behavior. If they are live safety controls, keep them in the
  risk layer and report them as risk clamps.
- Reconstruct the latest raw PPO action after promotion. Compare raw weights,
  final portfolio targets, order-sizing output, risk decisions, and order
  intents. Repeated identical target weights such as `10%, 10%, 10%` are a
  warning sign unless the raw action also produced them.
- Verify that target gross exposure, realized exposure, and cash weight line up
  with the policy output after deterministic layers. Explain any missing
  exposure as turnover cap, whole-share rounding, risk clamp, session guard, or
  cash reserve.
- Save or report the config hash, policy artifact path, metadata path, latest
  target batch id, and latest cycle time when a model is promoted.
- Run `runtime-config-validate` and `runtime-preflight` before relying on a
  changed live config. For material portfolio changes, run at least one short
  replay/backtest using the same config family and inspect target-vs-order
  lineage.

Backtests default to zero simulated slippage. Use `--slippage-bps` when checking
execution friction. The report's `metrics.slippage_cost` and
`metrics.slippage_bps` come from fill events, not from alpha or portfolio code.

Execution models are the only sleeve models that should choose order style.
Use `order_type`, `limit_price`, `time_in_force`, and execution metadata on
`OrderIntent`; do not call broker APIs or encode KIS order codes in alpha,
portfolio, or risk modules. `executions/leaps_immediate.py` wraps the engine's
`StandardExecutionModel`, so it can be configured as limit, market, or sliced
execution through `execution.parameters` without changing portfolio logic.
New execution models can accept `execution_context` or `market_session` in
`create_orders`. Use `execution_context.session_for_symbol(symbol)` when the
policy must differ between KRX regular, KRX after-hours, US pre-market, and US
after-market.

KIS-style execution constraints are now engine-owned. Domestic KRX orders are
whole-share only, limit prices must fit the KRX tick grid, and the broker
gateway rounds domestic limit prices to the nearest side-safe tick before
submission. Confirmed live submit can require an orderable market session. Do
not bypass these checks from sleeve code. KRX after-hours submit is supported
through runtime-stamped `order_session` metadata and gateway-owned KIS order
division mapping, so sleeve execution models should keep choosing only
`order_type`, `limit_price`, and `time_in_force`. Do not opt into
`allow_after_hours_single_price` unless the symbol/venue combination has been
verified; KIS rejects some NXT-traded symbols in that phase.

Backtests can simulate KIS-like transaction costs with `--fee-model kis`.
Treat this as a configurable preset, not a promise that a specific account's
promotion rate is known. Fee metadata is recorded on fill events and included
in backtest metrics.

Open-ticket maintenance is owned by the order supervisor. Stale tickets can be
reported or cancelled by policy, including partially filled tickets. Sleeve
models should express desired holdings and execution style, not direct cancel
or replace side effects.

Before live/paper operation after engine, config, universe, or sleeve model
changes, run `runtime-preflight`. It fingerprints engine source plus the
configured universe/model files, compares the latest cycle journal identity,
checks account/order store paths, and bootstrap-loads this sleeve without
submitting orders. If it reports `config_changed_since_last_cycle`,
`engine_code_changed_since_last_cycle`, or `latest_cycle_missing_code_identity`,
stage/reload and run one journaled cycle before live order submission.

Useful verification commands:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-preflight configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --summary-only
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2026-05-08 --end 2026-05-08 --source finance-datareader --summary-only
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2026-05-08 --end 2026-05-08 --source finance-datareader --fee-model kis --summary-only
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2023-05-10 --end 2026-05-08 --cash 2000000 --currency KRW --source finance-datareader --summary-only
py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id "default sleeve" --start 2026-05-08 --end 2026-05-08 --source finance-datareader --summary-only
```
