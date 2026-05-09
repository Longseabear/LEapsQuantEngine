# AGENTS.md

## Scope

`default sleeve` is the baseline virtual sleeve workspace. Use it for neutral defaults, migration placeholders, and simple smoke flows.

## Rules

- Keep this sleeve boring and predictable.
- Do not hide strategy-specific behavior here if it belongs in an active sleeve such as `LEaps`.
- Do not submit broker orders directly from this workspace.
- Keep cash, holdings, and model references explicit when examples use this sleeve.

## Tests

Default sleeve changes should be covered by runtime bootstrap, virtual account, or CLI smoke tests.
