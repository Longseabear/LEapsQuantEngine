# AGENTS.md

## Scope

Runtime configs define executable runtime snapshots for CLI, paper, live, and smoke flows.

## Rules

- Treat runtime config as a validated snapshot loaded by bootstrap.
- Do not make normal cycles poll config files directly.
- Runtime reload should enter through explicit control commands and swap at a cycle boundary.
- Keep domestic and overseas broker accounts explicit.
- Live broker-engine submit/poll/reconcile must remain blocked for unsupported market adapters.

## Tests

Update runtime config tests whenever fields are added, renamed, defaulted, or deprecated.
