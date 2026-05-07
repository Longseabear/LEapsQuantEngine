# PROJECT

## Goal

Build a LEAN-style dynamic quant engine with first-class sleeve support.

The engine should feel like LEAN at the strategy boundary while retaining the useful operating lessons from the legacy stack:

- deterministic event loop
- explicit portfolio state
- sleeve-level capital/risk isolation
- order intent generation before broker submission
- replayable configs and samples

## v0 Scope

- Define core domain models for symbols, data slices, holdings, targets, and order intents.
- Provide a minimal algorithm interface.
- Run a synchronous backtest/replay loop over in-memory bars.
- Route algorithm targets through sleeve policy before execution.
- Keep broker connectivity out of v0 core; execution emits order intents only.

## Reference Policy

Legacy code lives in `reference/stockprogram_legacy`.

Use it to understand orchestration, order-chain semantics, sleeve workspaces, and operational safeguards. Do not extend that tree for the new engine unless explicitly asked.
