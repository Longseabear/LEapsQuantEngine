# AGENTS.md

## Scope

Reference material lives here. `stockprogram_legacy` is operational history and design reference only.

## Rules

- Do not extend legacy services for new LEapsQuantEngine features unless the user explicitly asks.
- Do not import legacy modules into the active engine.
- Extract lessons, interface constraints, and examples into new deterministic engine code instead of copying legacy complexity.
- KIS operational lessons from legacy should be implemented behind adapter or broker-engine boundaries.

## Handoff

When referencing legacy behavior, document the new smaller engine concept that replaces it.
