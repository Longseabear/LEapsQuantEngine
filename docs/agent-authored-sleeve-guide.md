# Agent-Authored Sleeve Guide

This guide describes how to build a sleeve whose target portfolio is authored by
an operator or agent outside the deterministic model stack, while the engine
still owns selection, portfolio construction, risk, execution, order lifecycle,
and fills.

The pattern is useful when a sleeve is intentionally research/operator driven:
an agent reviews evidence, writes an auditable target artifact, and the runtime
turns that artifact into normal LEAN-style targets. The strategy thesis itself
belongs in the sleeve workspace, not in this guide.

## Contract

An agent-authored sleeve has three separate records:

- `data/operator-targets/<sleeve_id>/latest_target.json`
  Live runtime input. The configured selection and portfolio models read this
  file.
- `sleeves/<sleeve_id>/agent_state/current_state.json`
  Agent memory. This explains the current operating mode, last target, last
  validations, risks, and follow-ups. The engine must not trade from this file.
- `sleeves/<sleeve_id>/agent_state/daily_judgments/YYYY-MM-DD.json`
  Audit and replay record. This captures point-in-time reasoning and evidence
  references for later review or pseudo-backtesting.

Keep this boundary strict. The target artifact is the only live input; the
state and judgment files explain why the target exists.

## Target Artifact Shape

The target artifact should be small, deterministic, and easy to validate:

```json
{
  "sleeve_id": "example-sleeve",
  "target_id": "example-sleeve-agent-20260527-0830",
  "generated_at": "2026-05-27T08:30:00+09:00",
  "expires_at": "2026-05-28T08:50:00+09:00",
  "max_gross_exposure": 0.95,
  "flatten": false,
  "targets": [
    {
      "symbol": "KRX:005930",
      "name": "Samsung Electronics",
      "target_percent": 0.25,
      "confidence": 0.72,
      "reason": "short_machine_reason"
    }
  ]
}
```

Rules:

- Use explicit `sleeve_id`; loaders should reject mismatches.
- Use market-qualified symbols such as `KRX:005930`.
- Use fractional weights, not share quantities.
- Set `expires_at`; missing or stale targets should fail closed.
- Keep `reason` compact enough to fit order tags.
- Use `flatten: true` only as an explicit operator decision.
- Keep cash/currency buckets separate. Do not mix KRW and USD unless the sleeve
  has an explicit FX conversion layer.

## Model Wiring

Use a selection model that reads the artifact and selects only target symbols
that are present in the sleeve universe. Use a portfolio model that reads the
same artifact and emits `PortfolioAllocationTarget` percentages.

Reference shape:

```text
agent target artifact
  -> AgentDailyTargetSelectionModel
  -> AgentDailyTargetPortfolioModel
  -> OrderSizingEngine
  -> Risk
  -> Execution
  -> OrderIntent
```

The selection model should:

- load the artifact through `load_agent_target_artifact`
- reject missing, stale, expired, malformed, or wrong-sleeve artifacts
- reject symbols outside the configured market or coarse universe
- return no selected symbols when the artifact is unusable

The portfolio model should:

- load the artifact through the same loader
- clamp gross exposure and single-name exposure using simple parameters
- emit percentages, never quantities
- emit zero targets for held symbols missing from a complete target artifact
  when the sleeve is meant to be complete-target based
- store compact diagnostics through `StatePatch`, not by writing files

Runtime config should carry only module references and simple parameters:

```json
{
  "universe": {
    "active": {
      "selection_models": [
        "selections/agent_daily_target.py:AgentDailyTargetSelectionModel"
      ]
    }
  },
  "alpha": {
    "modules": []
  },
  "portfolio": {
    "model": "portfolios/agent_daily_target.py",
    "parameters": {
      "target_path": "data/operator-targets/example-sleeve/latest_target.json",
      "max_gross_exposure": 0.98,
      "max_position_pct": 0.35,
      "max_age_hours": 36,
      "allowed_markets": ["KRX"],
      "emit_zero_for_missing_held_targets": true
    }
  }
}
```

Do not put portfolio judgment, news summaries, ranking formulas, or trading
thesis prose in runtime config.

## Agent Memory

Each agent sleeve should have:

```text
sleeves/<sleeve_id>/agent_state/
  README.md
  AGENTS.md
  current_state.json
  daily_judgments/
    README.md
    manifest.json
    YYYY-MM-DD.json
```

`current_state.json` should record:

- active operating mode and configured model refs
- current target artifact id/path/generated/expiry
- current target summary and gross exposure
- why the current target exists
- last validation commands and results
- live application status
- open risks and follow-ups

`daily_judgments/YYYY-MM-DD.json` should record:

- decision cutoff time
- whether it is live, pre-live, posthoc, or reconstructed
- evidence references, not full copied articles
- target artifact reference
- what data was available before the cutoff
- constraints or vetoes used by the agent

For shared news, store reusable evidence outside the sleeve, for example:

```text
data/research/news_evidence/krx/YYYY-MM-DD.json
```

The sleeve judgment should reference that file. Do not mix shared news data
with sleeve-specific target decisions.

## Inference Workflow

The agent should follow a repeatable pre-target workflow:

1. Read `current_state.json`.
2. Check runtime artifact status and latest portfolio report.
3. Verify news, daily bars, minute snapshots, and market session freshness.
4. Compare current holdings to the previous target.
5. Decide one of:
   - carry forward the previous target
   - write a revised target
   - write a flatten target
   - fail closed by leaving no usable target
6. Write the daily judgment.
7. Write or atomically replace the live target artifact.
8. Validate config/model loading and run a read-only preflight or one-cycle
   diagnostic.
9. Update `current_state.json` with the decision and validation status.

Freshness rules matter. A weekend, holiday, closed session, or missing pre-open
quote is usually a session/data gate, not a reason to de-risk by itself. Change
weights only when there is concrete source-backed market, company, portfolio,
or risk evidence.

## Backtesting

Agent-authored sleeves cannot be honestly backtested from only today's
`latest_target.json`. Backtests need a point-in-time target series.

Preferred layout:

```text
data/operator-targets/<sleeve_id>/history/YYYY-MM-DD.json
```

or a sleeve-local research set:

```text
sleeves/<sleeve_id>/agent_state/pseudo_portfolios/<experiment_id>/targets/YYYY-MM-DD.json
```

The loader supports date templates and directories. A backtest config can use a
path like:

```text
sleeves/<sleeve_id>/agent_state/pseudo_portfolios/news_pseudo_v1/targets/{date}.json
```

or a directory where `YYYY-MM-DD.json` files are stored. During replay, each
cycle resolves the artifact for the replay date.

Backtest rules:

- Use only target files that existed before, or were reconstructed with an
  explicit information cutoff.
- Mark `recording_mode` clearly. Posthoc files are useful for audit but should
  not be treated as proof of live tradability.
- Filter news and evidence by the decision cutoff. Future headlines or later
  daily bars are leakage.
- Keep risk and execution enabled. The target artifact is not the final trade;
  risk may clamp it and execution may stage or reject orders.
- Report target turnover separately from actual order turnover.
- Compare current-vs-target quantity and cash after order sizing, not only raw
  weights.

For a pseudo-backtest, generate one daily target per trading date, then run the
normal runtime backtest using the dated target path. Inspect:

- gross target exposure
- realized exposure after whole-share sizing
- cash left from whole-share rounding
- risk clamp reasons
- order count and turnover
- stale/missing target dates
- target changes caused by the agent vs changes caused by risk/execution

## Live Readiness Checklist

Before enabling a new agent-authored sleeve:

- `runtime-config-validate` passes.
- The target artifact loads and has the expected `sleeve_id`.
- The target artifact is not expired or older than `max_age_hours`.
- Selection returns the target symbols and rejects out-of-universe symbols.
- Portfolio construction emits target percentages and zero targets for omitted
  held symbols when complete-target mode is intended.
- Risk decisions are explainable and do not silently block all entries.
- Execution produces order intents only in orderable sessions.
- Open tickets are zero or understood before applying a new target.
- Runtime state patches are committed only after successful cycles.
- Live models do not call KIS, broker stores, order stores, process lists, or
  external news APIs directly.

Useful read-only checks:

```powershell
$env:PYTHONPATH='src'
py -3 -m leaps_quant_engine.cli runtime-artifact-status configs/runtime/live_multi_sleeve.json --active-only --summary-only
py -3 tools/leaps_portfolio_report.py --config configs/runtime/live_multi_sleeve.json --sleeve-id <sleeve_id> --mode latest-target
py -3 -m leaps_quant_engine.cli runtime-health configs/runtime/live_multi_sleeve.json --heartbeat data/runtime/live-order-loop/multi_sleeve_heartbeat.json --heartbeat-component multi_sleeve_live_order_loop --summary-only
```

## Common Failure Modes

- Treating `current_state.json` as a live target source.
- Writing a target artifact without `expires_at`.
- Reducing exposure because the market is closed or data is stale on a holiday.
- Embedding full news articles in sleeve-specific judgment files.
- Reusing one `latest_target.json` in historical backtests.
- Letting models call broker/KIS/news APIs directly.
- Forgetting explicit zero targets for held symbols removed from the target.
- Ignoring whole-share rounding and reporting 95% gross target as if it were
  guaranteed live exposure.
- Diagnosing cash as a target problem when risk clamps or open tickets are the
  actual reason.
- Allowing a fresh target to bypass risk, execution, session, and order-runtime
  lifecycle controls.
