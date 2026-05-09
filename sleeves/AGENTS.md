# AGENTS.md

## Scope

Sleeves are strategy workspaces. Each sleeve may own alpha, portfolio, risk, execution, settings, and local documentation.

## Rules

- Treat each sleeve as an isolated virtual portfolio/account compartment.
- Sleeve modules may express strategy logic through engine interfaces only.
- Do not submit broker orders directly from a sleeve module.
- Do not share mutable state across sleeves unless it is an explicit transfer, reconciliation, or order lifecycle event.
- Research backtests may run a sleeve in isolation; live/paper runtime may run many sleeves together and coordinate orders globally.

## Handoff

Agents working in a sleeve should state which model folder they changed and which engine contract it implements.
