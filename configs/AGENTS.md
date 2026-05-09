# AGENTS.md

## Scope

Config files describe runtime settings, module references, universe inputs, broker account routes, and sample virtual-account data.

## Rules

- Config is a settings contract, not a strategy container.
- Do not put ranking formulas, buy/sell rules, risk logic, or prose strategy decisions directly in config.
- Module paths may point to sleeve workspace models, but executable logic belongs in Python modules.
- Do not commit secrets, app keys, account numbers, tokens, or live broker payloads.
- Broker account routes must be explicit when market scope matters.

## Required Updates

When config schema changes, update `runtime_config.py`, tests, docs, and at least one sample config.
