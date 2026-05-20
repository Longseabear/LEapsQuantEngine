# Model Authoring Guide

This guide is for writing LEapsQuantEngine strategy models without breaking the
deterministic LEAN-style pipeline.

For the runtime-level rules behind daily indicator resolution, alpha cadence,
portfolio target persistence, and urgent exits, see
`docs/runtime-cadence-resolution.md`.

The engine boundary is:

```text
UniverseSelectionModel
  -> AlphaModel
  -> PortfolioConstructionModel
  -> RiskManagementModel
  -> ExecutionModel
  -> OrderIntent
```

Sleeves own the model wiring, cash, holdings, policy, and workspace. Models
should be small Python modules that implement one pipeline contract and avoid
side effects outside that contract.

## Core Rules

- Do not call KIS, broker-engine, market-data-engine, FinanceDataReader, or a
  database from a model.
- Do not place broker orders from alpha, portfolio, or risk code.
- Do not mutate `Portfolio`, `IndicatorEngine`, virtual accounts, tickets, or
  order stores from model code.
- Read only the immutable context passed to the model.
- Emit the layer's artifact only:
  - selection emits selected symbols
  - alpha emits `Insight`
  - portfolio emits allocation targets
  - risk emits risk decisions
  - execution emits order intents
- Prefer skipping a symbol when required indicator or fundamental data is
  missing. Missing PER/PBR for ETFs is normal.
- Treat optional indicators as nullable research context. If a universe marks
  an indicator with `readiness="optional"`, use it when present and fall back
  when absent; do not make the model assume warmup blocked on that feature.
- Keep model IDs stable. They are used in insight superseding, logs, journals,
  and runtime wiring.

## Model Vs Engine Responsibility

Use this split when deciding where a feature belongs:

- Selection models decide the active candidate set. The engine still forces
  held, open-order, exit-watch, and manual symbols into the live universe.
- Alpha models decide predictions only. They do not decide position quantity
  and they do not clear broker tickets.
- Portfolio models decide desired allocation percentages. They should not emit
  share quantities or mutate current holdings.
- Risk models decide sleeve strategy constraints such as max exposure,
  concentration, stale-data tolerance, stop/reduce, and model-specific exits.
  Engine guards always own oversell prevention, missing route blocks,
  duplicate submit/idempotency, unsupported sessions, and broker capability
  checks.
- Execution models decide order style and trading urgency: market vs limit,
  limit offset, time-in-force, slicing, extended-session permission, max order
  age, price-drift tolerance, minimum replace interval, and max replacement
  count. The order runtime executes only approved lifecycle transitions.
- Virtual account reconciliation is not a strategy model. Unknown broker fills
  must be explicitly assigned to a sleeve or explicitly ignored as
  operator/manual activity. The engine records that decision and keeps it out of
  strategy state unless allocation is requested.

If a behavior would make every sleeve inherit one strategy's assumption, keep it
out of core and express it through a model contract or an explicit operator
workflow.

## Recommended Sleeve Layout

```text
sleeves/<sleeve_id>/
  alphas/
    momentum.py
    etf_rotation.py
    trailing_stop.py
  portfolios/
    equal_weight.py
  risks/
    basic.py
  executions/
    immediate.py
```

Runtime config can resolve alpha, portfolio, risk, and execution file paths from
`workspace_path`. Selection model references should use importable
`module:object` references in v0:

```json
{
  "sleeve_id": "LEaps",
  "workspace_path": "sleeves/LEaps",
  "universe": {
    "coarse_path": "configs/universes/swing_kor_core.json",
    "active": {
      "max_symbols": 40,
      "selection_models": [
        "leaps_quant_engine.universe.selection:StaticUniverseSelectionModel",
        "leaps_quant_engine.universe.selection:MomentumUniverseSelectionModel"
      ]
    }
  },
  "alpha": {
    "modules": [
      {"ref": "alphas/momentum.py"},
      {"ref": "alphas/etf_rotation.py"},
      {"ref": "alphas/trailing_stop.py"}
    ],
    "input_selections": {
      "leaps-momentum": "momentum-active-selection",
      "leaps-etf-rotation": "momentum-active-selection",
      "leaps-trailing-stop": "static-top-n"
    }
  },
  "portfolio": {
    "model": "portfolios/equal_weight.py",
    "parameters": {"max_portfolio_pct": 1.0},
    "rebalance": {"cadence": "once_per_day"}
  },
  "risk": {
    "model": "risks/basic.py",
    "parameters": {"max_position_pct": 0.35}
  },
  "execution": {
    "model": "executions/immediate.py"
  }
}
```

`alpha.input_selections` is runtime wiring. A selection model must not import or
call an alpha model, and alpha code must not call selection code. Custom
selection IDs such as `etf-rotation-top-20` or `operational-symbols` require
matching `UniverseSelectionModel` implementations with the same `selection_id`.

Workspace model wiring is managed with the same sleeve boundary for every
pipeline stage:

```powershell
py -3 -m leaps_quant_engine.cli sleeve-alpha-list configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-alpha-enable configs/runtime/leaps_workspace_smoke.json momentum --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-portfolio-set configs/runtime/leaps_workspace_smoke.json equal_weight --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-risk-set configs/runtime/leaps_workspace_smoke.json basic --sleeve-id LEaps
py -3 -m leaps_quant_engine.cli sleeve-execution-set configs/runtime/leaps_workspace_smoke.json immediate --sleeve-id LEaps
```

Each command prints a `reload_sleeve` control command. For a long-running
runtime, enqueue that command and apply it at a cycle boundary; for the current
bounded live loop, run preflight or restart the affected loop after live edits
when you need deterministic reload timing.

## Universe Selection Models

Selection chooses which symbols a sleeve should monitor and feed into one or
more alpha models.

Contract:

```python
class MySelectionModel:
    selection_id = "my-selection"

    def select(self, context: UniverseSelectionContext) -> UniverseSelectionResult:
        ...
```

Minimal example:

```python
from dataclasses import dataclass

from leaps_quant_engine.universe.selection import (
    UniverseSelectionCandidate,
    UniverseSelectionContext,
    build_universe_selection_result,
)


@dataclass(frozen=True, slots=True)
class FirstNSelectionModel:
    max_active_symbols: int = 20
    selection_id: str = "first-n"

    def select(self, context: UniverseSelectionContext):
        selected = tuple(context.universe.symbols[: self.max_active_symbols])
        selected_keys = {symbol.key for symbol in selected}
        candidates = {
            symbol.key: UniverseSelectionCandidate(
                symbol=symbol,
                score=None,
                selected=symbol.key in selected_keys,
                reasons=("first_n",) if symbol.key in selected_keys else (),
            )
            for symbol in context.universe.symbols
        }
        return build_universe_selection_result(
            context,
            selected,
            selection_id=self.selection_id,
            candidates=candidates,
            rejected={},
        )
```

Notes:

- `selected_symbols` should mean strategy-selected candidates only.
- Forced held/open/exit/manual symbols are added by the selection result builder
  and runtime invariant.
- If a selector uses indicators, read from `context.indicator_snapshot`.
- Keep `selection_id` stable. Alpha input wiring refers to it.
- Multiple selectors can run in one sleeve cycle. The runtime unions their
  selections into the live universe.
- For backtest debugging, pass `--include-insights` to
  `framework-backtest-daily` or `runtime-backtest-daily`. The report then keeps
  the default summary shape but adds cycle-level new/active insight ledgers and
  expands selection cycles, even when `--summary-only` is also used.

## Alpha Models

Alpha converts selected symbols and immutable snapshot context into predictions.

Supported module shapes:

- `create_alpha_model()`
- `ALPHA_MODEL`
- module-level `generate(context)`

Minimal function alpha:

```python
from datetime import timedelta

from leaps_quant_engine.alpha import Insight, InsightDirection, SnapshotContext


ALPHA_ID = "leaps-momentum"
VERSION = "0.1.0"
EVALUATION_CADENCE = "once_per_day"
INPUT_RESOLUTION = "daily"
HORIZON = timedelta(days=5)


def generate(context: SnapshotContext) -> list[Insight]:
    if not context.allows_new_entries:
        return []

    insights: list[Insight] = []
    for symbol_key in context.symbol_keys:
        close = context.value(symbol_key, "close")
        momentum = context.value(symbol_key, "momentum_20_close")
        per = context.fundamental(symbol_key, "per")

        if close is None or momentum is None:
            continue
        if per is not None and per <= 0:
            continue
        if momentum <= 0:
            continue

        insights.append(
            Insight(
                sleeve_id=context.sleeve_id,
                symbol=context.symbol(symbol_key),
                direction=InsightDirection.UP,
                generated_at=context.as_of,
                expires_at=context.as_of + HORIZON,
                source_snapshot_id=context.source_snapshot_id,
                alpha_id=ALPHA_ID,
                alpha_version=VERSION,
                confidence=0.7,
                weight=0.1,
                score=momentum,
                reason="positive_momentum",
                metadata={"close": close, "momentum": momentum, "per": per},
            )
        )
    return insights
```

LEaps' current production-style sleeve uses thesis-specific alpha modules rather
than generic examples:

- `leaps-kospi-conviction`: KRX-only trend/momentum alpha for the KRW growth
  pocket.
- `leaps-us-stability-hedge`: US ETF stability alpha for minimum-volatility,
  dividend-quality, treasury, and gold hedge candidates.
- `leaps-volatility-trailing-stop`: FLAT insights for risk reduction.

Keep this split when authoring similar sleeves: each alpha should own one
economic role, while `alpha.input_selections` decides which selector output it
receives.

Cadence fields are optional but recommended for non-intraday models:

- `EVALUATION_CADENCE = "once_per_day"` keeps daily/swing alpha from being
  regenerated every minute. Existing insights remain active until expiry.
- `EVALUATION_CADENCE = "daily_at 08:50 Asia/Seoul"` waits until the scheduled
  wall-clock time before generating the day's alpha. Use this when the model
  depends on pre-open confirmed daily features and should not run on an earlier
  startup cycle.
- `INPUT_RESOLUTION = "daily"` documents the expected snapshot resolution for
  reviewers and agents. Indicator update protection is enforced by the universe
  indicator definitions' `resolution` field.
- Use `"every_cycle"` only for models that truly need live or intraday
  reassessment. Emergency exits should normally be handled by always-on risk or
  explicitly quote-resolution exit models rather than by a daily alpha loop.

Portfolio cadence is configured in runtime config, not inside the portfolio
model formula:

```json
{
  "portfolio": {
    "rebalance": {
      "cadence": "week_start_at 08:55 Asia/Seoul"
    }
  }
}
```

For patient entries, configure the execution model rather than hiding clock
checks in alpha or portfolio code:

```json
{
  "execution": {
    "parameters": {
      "buy_window": "09:05-14:50 Asia/Seoul",
      "window_timezone": "Asia/Seoul"
    }
  }
}
```

The standard execution model blocks new buy intents outside `buy_window` but
does not block sells unless `sell_window` is explicitly configured.

Important context fields:

- `context.symbol_keys`: the alpha's selected input symbols. Runtime selection
  wiring may narrow this per alpha.
- `context.available_symbol_keys`: all symbols in the underlying
  `IndicatorSnapshot`.
- `context.value(symbol_key, indicator_name)`: indicator value.
- `context.fundamental(symbol_key, name)`: latest point-in-time fundamental
  value, if available.
- `context.allows_new_entries`: snapshot quality gate for entry alpha.

Use `InsightDirection.FLAT` for an exit/control signal when the alpha believes a
symbol should no longer be held. The order still belongs downstream.

Alpha must not:

- emit portfolio targets or order quantities
- read `IndicatorEngine` directly
- fetch external data
- mutate portfolio state

## Optional Model State

Models are stateless by default. A model that needs restart-safe state, such as
a trailing stop high watermark or a target-smoothing anchor, should request
state changes with `StatePatch` records instead of writing files or mutating
portfolio state directly.

State ownership rule:

```text
model decides what state means
runtime stores and replays the state
engine guard validates order/account safety separately
```

Use `ModelStateKey` to namespace state by sleeve, model, namespace, symbol, and
optionally position. Runtime contexts expose a read-only `context.model_state`
view; models may add an optional `state_patches(...)` method to request updates
after their normal decision output is created.

For JSON-object state, models can use the helper surface instead of constructing
keys by hand:

```python
state = context.model_state.object_get(
    model_id="trailing-stop",
    namespace="trailing_stop",
    symbol_key="KRX:005930",
)
patch = context.model_state.object_merge(
    {"high_watermark_price": 84000},
    model_id="trailing-stop",
    namespace="trailing_stop",
    symbol_key="KRX:005930",
    reason="trailing_stop_mark",
)
```

Use `object_set(...)` for full replacement, `object_merge(...)` for patch-style
updates, and `object_delete(...)` when a position or model state should be
cleared at a cycle boundary.

Alpha example:

```python
from leaps_quant_engine.runtime_state import StatePatch


class TrailingStopAlpha:
    alpha_id = "trailing-stop"
    version = "0.1.0"

    def generate(self, context):
        state = context.model_state.get(
            model_id=self.alpha_id,
            namespace="trailing_stop",
            symbol_key="KRX:005930",
        )
        ...

    def state_patches(self, context, insights):
        return (
            StatePatch(
                key=context.model_state.key(
                    model_id=self.alpha_id,
                    namespace="trailing_stop",
                    symbol_key="KRX:005930",
                ),
                value={"high_watermark_price": 84000},
                reason="trailing_stop_mark",
            ),
        )
```

Supported state hook shapes:

- alpha: `state_patches(context, insights)`
- portfolio: `state_patches(context, targets)`
- execution: `state_patches(context, orders)`
- risk: return `RiskDecisionBatch(..., state_patches=(...))`

Model-owned examples:

- trailing stop high-watermark state
- target smoothing or lerp anchors that are part of a strategy thesis
- portfolio blend transition state when the engine blend layer is enabled
- daily loss limit baseline and max-drawdown peak in opt-in risk models

Core guard examples, not model state:

- oversell prevention
- cash/reserved quantity checks
- unsupported broker routes or sessions
- duplicate submit/idempotency checks
- missing price validation

The runtime commits patches only after a successful framework cycle and records
patch/event counts in the framework result and cycle journal. Live loops must
pass `--runtime-state` explicitly; without it, state reads return empty and
patches are reported but not persisted.

When testing a new stateful model against real live state during market hours,
fork the SQLite runtime DB first:

```powershell
py -3 -m leaps_quant_engine.cli runtime-state-fork `
  --source data/runtime/runtime-state/live_multi_sleeve.sqlite `
  --target data/runtime/runtime-state/sandbox/model_probe.sqlite `
  --overwrite
```

Run experiments against the forked DB. Do not replace the live DB with a sandbox
DB; promote changes through model code, config reload, or an explicit seed
command.

## Portfolio Construction Models

Portfolio construction consumes active insights and produces desired allocation
percentages. It should not produce integer share quantities.

Supported module shapes:

- `create_portfolio_model(params)`
- `create_model(params)`
- `PORTFOLIO_MODEL`

Minimal model:

```python
from typing import Any, Mapping

from leaps_quant_engine.framework import EqualWeightPortfolioConstructionModel


def create_portfolio_model(params: Mapping[str, Any] | None = None):
    values = dict(params or {})
    return EqualWeightPortfolioConstructionModel(
        max_portfolio_pct=float(values.get("max_portfolio_pct", 1.0)),
        long_only=bool(values.get("long_only", True)),
    )
```

Custom model contract:

```python
from leaps_quant_engine.framework import PortfolioAllocationTarget


class MyPortfolioModel:
    def create_targets(self, context):
        return (
            PortfolioAllocationTarget(
                symbol=context.active_insights[0].symbol,
                target_percent=0.25,
                tag="my-portfolio-model",
            ),
        )
```

Notes:

- Emit `PortfolioAllocationTarget.target_percent`.
- Do not round to lots or shares. `OrderSizingEngine` owns quantity conversion
  and recomputes current quantity targets from live portfolio state every
  cycle.
- `PortfolioTarget Ledger` is an engine responsibility. The portfolio model owns
  desired target percentages; `FrameworkRunner` persists active insights,
  rebalance cadence, and the last target batch through framework state, while
  `OrderSizingEngine` recomputes target share quantities from current virtual
  account state every cycle.
- If target smoothing is strategic, implement the smoothing policy in the
  portfolio model and store its anchors with `StatePatch`. Example: a model that
  always wants to reduce turnover by partially following yesterday's model
  weights owns that policy.
- If target smoothing is operational, use `portfolio.blend`. Example: a model or
  config changes substantially and the operator wants the sleeve to move from
  the previous committed target snapshot to the new raw target snapshot over
  five orderable hours. The model still emits raw desired percentages; the
  engine-owned `PortfolioBlendEngine` handles the transition before
  `OrderSizingEngine`.
- Use `context.active_insights`, `context.portfolio`, and `context.data`.
- Multi-currency sleeves are bucket-aware. Do not use mixed global equity for
  KRW and USD decisions unless an FX conversion layer is explicitly added later.
- `portfolio.rebalance.cadence` controls how often the engine rebuilds target
  allocations. If cadence is not due, `FrameworkRunner` reuses the last
  allocation targets; `OrderSizingEngine`, risk, and execution still run every
  cycle.
- Tiny churn from reused allocation batches is an opt-in rebalance policy, not
  a portfolio model rule. Set `portfolio.rebalance.reused_target_churn_guard`
  only when a sleeve wants `OrderSizingEngine` to suppress adjacent-lot
  non-exit deltas from a reused `PortfolioTargetBatch`. Fresh target batches
  and explicit exit/flat targets still pass through.
- Active FLAT/DOWN insights should be allowed to override same-symbol UP
  insights in portfolio construction. In LEaps, the RL constructor treats a
  same-or-newer non-UP insight as a reason to avoid a long target and to emit an
  exit target for held quantities.

Example:

```json
{
  "portfolio": {
    "rebalance": {
      "cadence": "every_5_minutes",
      "reused_target_churn_guard": true,
      "reused_target_churn_max_quantity_delta": 1,
      "reused_target_churn_lot_fraction": 0.5,
      "reused_target_churn_equity_bps": 5
    },
    "target_resolution": {
      "mode": "complete"
    },
    "blend": {
      "enabled": true,
      "duration_minutes": 300,
      "target_drift_threshold_pct": 0.08,
      "clock": "orderable_session"
    }
  }
}
```

Target Resolution notes:

- Portfolio models normally emit a complete desired target set, not a partial
  patch. With `portfolio.target_resolution.mode="complete"`, a previously
  targeted or currently held symbol missing from a non-empty new raw output is
  converted to an explicit 0% target before blend.
- Empty raw target batches are `empty_no_action` by default. Do not rely on
  "emit nothing" to mean all-cash; emit explicit 0% targets when the model
  wants positions closed.
- Use `mode="patch"` only for a model that intentionally emits partial changes.
  The resolver carries omitted previous targets forward, then blend runs on the
  merged complete vector.
- Blend must never infer missing-target semantics. Its input is the resolved
  complete vector.

Portfolio Blend notes:

- Do not register an "old model" and "new model" together. The old side is the
  stored target snapshot.
- For complete-portfolio allocators, omitted old-only targets fade toward 0%
  because target resolution has already made them explicit 0% targets.
- Tags containing `:flat`, `:down`, `stop`, `urgent`, `manual`, `operator`,
  `force`, `risk`, `no_longer_in_target_portfolio`, or
  `missing_target_zero` bypass the blend for that symbol, so trailing stops,
  risk exits, and explicit old-only target exits are not delayed.
- Retargeting during an active blend does not restart the blend clock. The
  active transition keeps its original `started_at` and `deadline_at`; only the
  destination target vector is updated. The engine records the retarget point as
  `from_elapsed_minutes`, so a model's small percentage changes do not cause the
  effective target to jump or extend the transition.
- Reports and cycle output expose `portfolio_target_batch.metadata.portfolio_blend`
  with status, progress, transition id, elapsed minutes, duration, drift, and
  bypassed symbols.

### Reinforcement Learning Constructors

RL portfolio construction models are allowed when they stay inside the same
contract: they consume immutable framework context and emit allocation targets.
They must not fetch data, train during runtime cycles, mutate portfolio state, or
submit orders.

Recommended shape:

```text
training command
  -> historical universe data
  -> saved policy artifact under data/rl/
  -> runtime portfolio model loads policy
  -> target percentages only
  -> OrderSizingEngine / Risk / Execution unchanged
```

For LEaps, `sleeves/LEaps/portfolios/rl_ppo_constructor.py` loads a
Stable-Baselines3 PPO policy through
`ReinforcementLearningPortfolioConstructionModel`. Missing policy artifacts must
fall back to deterministic exposure rather than failing a runtime smoke.

For finance RL, prefer ensemble inference and shape-aware rewards over a single
policy optimized for raw return. The LEaps PPO constructor supports multiple
`policy_paths` and uses median action selection. The current training reward
penalizes downside return, rolling volatility, drawdown increase, underwater
state, turnover, and missed upside; CAGR is a report metric, not the direct
training objective.

The current LEaps RL constructor uses a compact attention encoder before the PPO
policy head. Selector/alpha outputs are represented as top-k candidate tokens
instead of being averaged into one basket vector. Each token carries fields such
as momentum, volatility, short returns, drawdown, rank score, and current
exposure. This follows the portfolio-RL literature pattern of using attention to
model cross-asset relationships while keeping the engine boundary unchanged:
the RL model still emits only allocation percentages.

State-aware RL schemas may add portfolio-local fields such as
`current_weight` and `previous_target_weight`. Store those anchors through
portfolio `StatePatch` records and read them back from `context.model_state`;
do not write files from the model. If a policy needs deterministic turnover
control, implement it inside the portfolio model as target smoothing or
`max_target_turnover_pct`, then let risk/execution handle sizing and order
lifecycle. Urgent FLAT/DOWN/stop exits should bypass smoothing-style delays.
Keep the runtime observation shape compatible with the policy artifact; a
lookback setting used for training warmup is not the same as exposing a full
temporal tensor to the live portfolio model.

Temporal RL policies must use an explicit temporal schema. In LEaps this is
`feature_schema=v2_temporal`, whose training observation shape is
`[lookback_window, top_k, feature_dim]`. The temporal extractor attends across
both time and candidate rank, with point-in-time features built only from bars
available through the decision date. The runtime now has a
`TemporalFeatureWindowProvider` that attaches those daily windows to
`SnapshotContext` metadata when the portfolio config uses a temporal
`feature_schema`. Alpha models that want temporal PPO must copy
`context.metadata_value(symbol_key, "rl_temporal_features")` into each emitted
UP insight's metadata. Do not emulate a temporal model at runtime by repeating
the latest top-k token. Runtime temporal PPO must also stay alpha-gated:
Portfolio construction only builds temporal tokens from active UP insights that
carry an explicit `rl_temporal_features` window in metadata. A missing alpha
signal, or an alpha signal without that feature window, must not let PPO scan
the universe and create a fresh buy target on its own.

Temporal windows are different from indicator warmup. Warmup prepares the
current SMA/momentum/ATR values; the temporal feature provider prepares a
historical tensor such as the last 64 confirmed daily rows. Backtests should use
`--warmup-start` far enough before `--start` for both requirements. Minute
replay still gets temporal PPO windows from the daily history provider; minute
bars do not advance the daily temporal window.

When raw momentum over-selects high-beta jumpers, prefer a separate schema over
silently changing an existing policy's observation semantics. LEaps uses
`feature_schema=v2_temporal_residual` for this research path: it adds residual
momentum, market beta, and trend-quality fields, ranks candidates with
volatility/drawdown penalties, and reserves a small large-cap core bucket before
PPO allocation. This keeps the alpha/ranking decision explicit and lets old
`v2_temporal` artifacts remain reproducible.

When an RL constructor is used as a complete target portfolio allocator, set
`emit_zero_for_missing_held_targets=true`. In that mode, if the model has an
actionable target set for a currency bucket and a currently held symbol in that
same bucket is missing from the target set, the model emits a zero allocation
tagged `no_longer_in_target_portfolio`. If there are no actionable insights for
that currency, it does not mass-flatten holdings; exits still require explicit
FLAT/DOWN insights or another active target set in the same bucket.

The runtime supports both the older gross-exposure controller modes and the
newer direct allocator mode. In `allocation_mode=rl_weights`, PPO emits a
continuous action vector with one score per top-k candidate plus one cash score.
The constructor normalizes that vector into `PortfolioAllocationTarget`
percentages, then risk applies currency and position clamps. Keep unselected
variants as research candidates until they win on held-out Sharpe/MDD/turnover,
not only in-sample return.

## Risk Models

Risk approves, clamps, rejects, or adds risk-driven quantity targets after order
sizing. Strategy risk belongs here. Engine safety guards remain core and always
on.

Supported module shapes:

- `create_risk_model(params)`
- `create_model(params)`
- `RISK_MODEL`

Recommended v0 wrapper:

```python
from leaps_quant_engine.framework import BasicRiskManagementModel, RiskLimits


def create_risk_model(params):
    return BasicRiskManagementModel(
        limits=RiskLimits(
            long_only=bool(params.get("long_only", True)),
            max_position_pct=float(params.get("max_position_pct", 0.35)),
            max_total_exposure_pct=float(params.get("max_total_exposure_pct", 0.95)),
            cash_buffer_pct=float(params.get("cash_buffer_pct", 0.03)),
            require_fresh_for_entries=bool(params.get("require_fresh_for_entries", True)),
            reject_invalid_snapshot=bool(params.get("reject_invalid_snapshot", True)),
        )
    )
```

Custom model contract:

```python
from leaps_quant_engine.framework import RiskDecision, RiskDecisionBatch, RiskDecisionStatus


class MyRiskModel:
    def manage_risk(self, context):
        decisions = []
        for target in context.targets:
            decisions.append(
                RiskDecision(
                    original_target=target,
                    approved_target=target,
                    status=RiskDecisionStatus.APPROVED,
                    reason="approved_by_my_risk",
                )
            )
        return RiskDecisionBatch(sleeve_id=context.sleeve_id, decisions=tuple(decisions))
```

Notes:

- Risk runs every framework cycle.
- Risk should explain every clamp or rejection with a reason and metadata.
- Risk should not submit orders or mutate holdings.
- Oversell prevention, route mismatch, duplicate submit, and unsupported broker
  route checks are core engine guards, not strategy risk models.

## Execution Models

Execution converts approved quantity targets into `OrderIntent` records. It does
not submit to a broker.

Supported module shapes:

- `create_execution_model(params)`
- `create_model(params)`
- `EXECUTION_MODEL`

Recommended v0 wrapper:

```python
from leaps_quant_engine.execution import StandardExecutionModel


def create_execution_model(params):
    return StandardExecutionModel(
        order_type=params.get("order_type", "limit"),
        time_in_force=params.get("time_in_force", "day"),
        limit_offset_bps=float(params.get("limit_offset_bps", 0.0)),
        max_slice_quantity=params.get("max_slice_quantity"),
        max_slice_notional=params.get("max_slice_notional"),
        max_slices=params.get("max_slices"),
    )
```

Built-in execution model choices:

- `ImmediateExecutionModel`: default one-ticket limit order intent.
- `LimitExecutionModel`: one-ticket limit order intent with optional
  side-aware `limit_offset_bps`.
- `MarketExecutionModel`: one-ticket market order intent.
- `SlicedExecutionModel`: same target-delta logic, but can split a parent
  quantity by `max_slice_quantity`, `max_slice_notional`, and `max_slices`.

`OrderIntent` now carries execution instructions:

- `order_type`: `limit` or `market`
- `limit_price`: optional; if omitted, broker adapters may fall back to
  `reference_price`
- `time_in_force`: `day`, `gtc`, `ioc`, or `fok`
- `metadata`: execution lineage such as slice index/count

Custom model contract:

```python
from leaps_quant_engine.models import OrderIntent, OrderSide, OrderType


class MyExecutionModel:
    def create_orders(self, sleeve_id, portfolio, data, targets, execution_context=None):
        orders = []
        for target in targets:
            current = portfolio.quantity(target.symbol)
            delta = target.quantity - current
            if delta == 0:
                continue
            bar = data.get(target.symbol)
            if bar is None:
                continue
            orders.append(
                OrderIntent(
                    sleeve_id=sleeve_id,
                    symbol=target.symbol,
                    side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
                    quantity=abs(delta),
                    reference_price=bar.close,
                    order_type=OrderType.LIMIT,
                    limit_price=bar.close,
                    tag=target.tag,
                )
            )
        return orders
```

Existing execution models may keep the shorter four-argument signature. New
session-aware execution models can optionally accept `execution_context` or
`market_session`. The context exposes `market_session`, `market_sessions`, and
`session_for_symbol(symbol)`, so a mixed-market sleeve can decide KRX and US
order policy independently.

```python
class SessionAwareExecutionModel:
    def create_orders(self, sleeve_id, portfolio, data, targets, execution_context=None):
        for target in targets:
            session = execution_context.session_for_symbol(target.symbol) if execution_context else None
            if session and session.session_phase == "after_hours_single_price":
                # Only emit an order here when this symbol/venue has been verified.
                ...
```

Notes:

- Execution emits intent only.
- Broker submission happens later through order runtime submit/orchestration.
- Same-symbol buy/sell collisions across sleeves are account-level coordination
  events, not execution model side effects.
- Execution should set a clean `reference_price`; simulated backtests may apply
  slippage later through `SimulatedFillModel`, and realized broker fills can be
  compared against the same reference.
- Limit orders with an explicit `limit_price` can remain unfilled in simulated
  fills when `SimulatedFillModel(enforce_limit_price=True)` is used and the
  side-adjusted fill price is not marketable. The default backtest fill path
  remains immediate-fill for backward-compatible research runs.
- Domestic broker-engine submission maps `limit/day` to KIS order division
  `00`, `market/day` to `01`, `limit/ioc` to `11`, `limit/fok` to `12`,
  `market/ioc` to `13`, and `market/fok` to `14`. Keep broker-specific codes
  out of strategy models; set `order_type` and `time_in_force` instead.
- Domestic live broker submission defaults to KIS `exchange_scope=SOR` so the
  broker can route Korean orders across supported venues. A model should leave
  this unset for normal operation. Only set `exchange_scope`, `venue_policy`, or
  related venue metadata when the strategy intentionally wants `KRX` or `NXT`
  for a tested venue-specific reason.
- Execution models may choose market, limit, and slicing policy, but broker
  capability checks are core guards. KIS routes are whole-share only in v0.
  Domestic KRX limit prices are rounded to the KRX tick grid at broker-submit
  time: buys round up, sells round down.
- Backtests can add KIS-style simulated fees with `--fee-model kis`. The preset
  is configurable and should be adjusted for the actual account/event rate.
  The domestic preset includes a 2026 sell-side securities transaction tax
  component and broker commission. Fill events record `fee`, `commission`,
  `taxes`, and `fee_model` metadata.
- Live execution-history sync uses actual KIS/broker cost fields when present.
  Realized `VirtualFillEvent.fee` is the amount applied to sleeve cash, and
  `metadata.transaction_costs` keeps fee/commission/tax/regulatory breakdowns.
  Strategy models should never replace actual broker costs with simulated
  estimates.
- Market/session checks belong in the order guard, not alpha/portfolio/risk.
  Confirmed live broker-engine submit can require a normalized
  `MarketSession`; non-orderable phases should block order submission before
  KIS is touched.
- Strategy models should not hard-code KIS after-hours order divisions. The
  runtime submitter stamps the current `order_session`, and the broker gateway
  maps KRX after-hours sessions to KIS divisions: `05` for pre-open after-hours,
  `06` for after-hours close, and `07` for after-hours single-price. US
  pre-market and after-market are also treated as orderable sessions.
- Extended-session live submit is intentionally restricted to `limit/day`
  orders. Market/IOC/FOK behavior belongs in regular-session execution unless
  a broker-specific extension is explicitly implemented and tested.
- Do not set `allow_after_hours_single_price` from a model unless symbol/venue
  support has been verified. In live operation KIS may reject NXT-traded Korean
  symbols during `after_hours_single_price`, so the engine guard blocks that
  phase by default and still keeps the `07` mapping available for verified
  exceptions.
- Stale open tickets and stale partial fills are maintained by the order
  supervisor. Strategy models should not cancel or replace broker orders
  directly.
- `day` order expiry is also order-runtime responsibility. A model may choose
  `time_in_force=day`, but it should not manually clear stale pending tickets;
  the supervisor expires them after the relevant market-local date rolls over.
- If a model needs more aggressive exit behavior, express that as execution
  policy through order type, limit offset, and `execution_policy` metadata.
  `StandardExecutionModel` can stamp `urgency`, `max_order_age_seconds`,
  `price_drift_bps`, `min_replace_interval_seconds`, and `max_replacements`
  onto each `OrderIntent`. Do not hide broker cancel/replace calls inside the
  model.
- Opening-auction and extended-session data can arrive as minute bars with
  `Bar.metadata["market_session_phase"]`. Use this as opening/execution
  context, not as confirmed daily data. A model that reacts to pre-open gaps
  should check `is_extended_market_hours` or the explicit session phase and
  keep daily momentum/SMA inputs on daily-confirmed indicators.
- Long daily backtests can expose an opening proxy through
  `Bar.metadata["opening_context_source"] == "daily_ohlc_proxy"`. Values such
  as `opening_gap_pct` and `gap_filled` are derived from daily OHLC and the
  previous close. Alpha models read them through
  `context.metadata_value(symbol, "opening_gap_pct")`. Treat them as proxy
  features, not historical pre-open book observations.
- Cancel/replace policy is an execution-model responsibility, while the order
  runtime owns broker lifecycle execution. The runtime should cancel/replace
  only at ticket-safe boundaries and should use model-provided policy fields
  rather than inventing one sleeve's urgency as a core default.

## Validation Checklist

Before enabling a model in runtime:

- Run unit tests for the model's contract.
- Run a one-sleeve backtest with isolated cash.
- For short-window backtests, verify `warmup_data_slice_count` is non-zero or
  pass `--warmup-start`; otherwise daily indicators may be cold at `--start`.
- For mixed KRW/USD runs, inspect `metrics_by_currency`. Aggregate `metrics`
  without FX conversion should be treated as informational only when
  `valid_without_fx=false`.
- Run `runtime-config-validate` for the config file.
- Run `runtime-run-once --summary-only` for a single-sleeve model diagnostic.
- Run `runtime-run-multi-once --summary-only` before the live multi-sleeve
  submit path.
- Inspect the journal/status output for selected symbols, insight counts,
  target counts, risk decisions, and order intent counts.

Useful commands:

```powershell
$env:PYTHONPATH='src'
py -3 -m pytest -q

py -3 -m leaps_quant_engine.cli runtime-config-validate configs/runtime/leaps_workspace_smoke.json

py -3 -m leaps_quant_engine.cli runtime-run-once configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --skip-warmup --summary-only

py -3 -m leaps_quant_engine.cli runtime-run-multi-once configs/runtime/live_multi_sleeve.json --sleeve-id LEaps --sleeve-id us_etf_rotation --summary-only

py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2023-05-10 --end 2026-05-08 --cash 2000000 --source finance-datareader --summary-only

py -3 -m leaps_quant_engine.cli runtime-backtest-daily configs/runtime/leaps_workspace_smoke.json --sleeve-id LEaps --start 2023-05-10 --end 2026-05-08 --cash 2000000 --source finance-datareader --slippage-bps 5 --summary-only

py -3 -m leaps_quant_engine.cli framework-backtest-daily configs/universes/swing_kor_core.json sleeves/LEaps/alphas/momentum.py --sleeve-id LEaps --start 2023-05-10 --end 2026-05-08 --cash 2000000 --source finance-datareader --summary-only
```

## Common Mistakes

- Looping over `context.available_symbol_keys` in alpha by accident. Use
  `context.symbol_keys` unless the model explicitly needs the full snapshot.
- Treating missing fundamentals as false negatives. For ETFs, fundamentals may
  simply not exist.
- Putting ranking formulas in JSON config. Config should carry module refs and
  simple parameters; model logic belongs in Python.
- Emitting share quantities from portfolio construction. That belongs to
  `OrderSizingEngine`.
- Mutating `Portfolio` in risk or execution. Portfolio state changes from fills
  or reconciliation events.
- Calling broker APIs inside a model. Broker access is outside deterministic
  strategy layers.
