# AGENTS.md

## Scope

Package examples demonstrate public engine APIs from inside the installed package.

## Rules

- Keep examples minimal, deterministic, and importable.
- Do not make examples depend on private test helpers.
- Do not call live brokers or KIS directly.
- Prefer tiny examples that clarify one interface at a time.

## Tests

If an example is intended to stay working, add or update a test that imports or runs it.
