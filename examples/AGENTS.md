# AGENTS.md

## Scope

Top-level examples are runnable or copyable demonstrations of public engine interfaces.

## Rules

- Examples should use public APIs and sample configs.
- Do not depend on live credentials, live KIS calls, or local user state.
- Keep examples deterministic where possible.
- If an example demonstrates a strategy model, mirror the real contract: alpha emits insights, portfolio emits allocations, risk gates targets, execution emits intents.

## Tests

Important examples should be imported or exercised by tests so they do not drift.
