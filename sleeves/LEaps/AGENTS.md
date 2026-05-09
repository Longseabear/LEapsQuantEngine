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
