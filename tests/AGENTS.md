# AGENTS.md

## Scope

Tests prove deterministic engine behavior, state transitions, adapter normalization, and CLI/runtime contracts.

## Rules

- Add tests for every new pipeline layer, state transition, or public contract.
- Prefer deterministic fixtures and temp directories.
- Mock or fake external providers and brokers.
- Do not require live KIS credentials or network access in normal tests.
- Keep tests focused on behavior rather than implementation details.
- When a bug is fixed, add a regression test that would have failed before the fix.

## Command

Run the full suite before reporting completed code changes:

```powershell
py -3 -m pytest -q
```
