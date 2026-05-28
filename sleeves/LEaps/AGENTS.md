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

Current LEaps live target wiring is intentionally alpha-less. The active
`live_multi_sleeve` profile reads `data/operator-targets/LEaps/latest_target.json`
through `selections/agent_daily_target.py` and
`portfolios/agent_daily_target.py`.

```text
agent daily target artifact
  -> leaps-agent-daily-target selection
  -> AgentDailyTargetPortfolioModel target percentages
  -> risk
  -> execution
```

Alpha research modules remain in the workspace, but the active config sets
`alpha.modules=[]`. Do not reintroduce an alpha module into live just to make a
portfolio target. If the target artifact is missing or stale, portfolio
construction must fail closed by emitting no fresh batch. If the artifact is
valid and omits a held KRX symbol, the portfolio model emits an explicit 0%
target for that symbol so the complete-target rebalance is auditable.
Operational symbols still exist so held/open/manual symbols remain visible to
risk and execution even when they are not selected as fresh entries.

The agent target artifact is a read-only operator input, not model-owned state.
Do not write it from a sleeve model. If model diagnostics are needed, store only
compact status through `StatePatch` under
`leaps-agent-daily-target-portfolio/target_artifact`.

Agent operating memory lives under `agent_state/`. Read
`agent_state/current_state.json` before making material LEaps decisions, and
update it after changing the target portfolio, live application status,
validation status, or the reasoning behind the current thesis. This file is
memory only; it is not a runtime trading input.

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

Current per-symbol risk behavior: market guard caps the KRW budget first, while
the symbol guard catches symbol-specific damage. For the agent daily target
profile, do not let a single intraday high-to-current drawdown routinely defeat
portfolio rebalancing. Symbol high-drawdown reduce/exit thresholds should be
wide and reserved for severe damage; ordinary target drift after a pullback may
be staged back toward target when unrealized loss, intraday move, trend
metadata, and target freshness permit it. Missing SMA/alpha metadata should not
by itself block an agent target rebalance; use it only when present. Complete
symbol exits still need cooldown plus a fresh target artifact before re-entry.
Keep these controls in risk, not alpha or execution, and report their clamp
reason as `symbol_guard_*`.

Execution anti-oscillation is a release blocker. Same-target drift rebalancing
is allowed, but a reused `source_target_batch_id` must not produce a buyback
after a sell, or a non-risk sell after a buy, just because minute prices or
whole-share rounding moved. Keep these guards enabled in live configs:
`whole_share_rounding_churn_guard`,
`opposite_rebalance_require_small_change=false`, and
`same_source_opposite_rebalance_guard=true`. Risk exits may still sell
immediately, but risk-reduction tags such as `risk:symbol_guard_exit` and
`risk:currency_policy_reduce` must be preserved so execution can block re-entry
until the cooldown or a fresh target artifact. When validating changes, inspect
both total turnover and same-source opposite transitions; `sell -> buy` under
the same target artifact should be 0 unless it is an explicit manual/operator
override.
Rebalance noise guards should block rounding dust, not meaningful one-lot
drift. For the agent daily target profile, keep `min_order_notional` near
50,000 KRW, `reused_target_churn_lot_fraction` near 0.25, and execution
`rebalance_no_trade_min_notional` near 100,000 KRW unless a backtest/live
diagnostic shows renewed churn. High-priced KRX entries may use
`whole_share_entry_floor_min_fraction=0.75` so a target worth at least three
quarters of one share can enter one lot instead of staying permanently in cash.
When `target_count=0`, first inspect order-sizing metadata such as
`zero_delta_symbols`, `below_min_notional_suppressed_symbols`, and
`reused_target_churn_suppressed_symbols`.

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
