# AGENTS.md

## Scope

This folder contains the active Python package source. New engine code should live under `leaps_quant_engine/`.

## Rules For Agents

- Follow the root pipeline contract: universe -> alpha -> portfolio -> risk -> execution -> order lifecycle.
- Keep strategy-facing code LEAN-like and sleeve-aware.
- Do not add generated files, caches, notebooks, or runtime state under `src/`.
- Do not import from `reference/stockprogram_legacy`; legacy code is reference-only.
- Keep provider-specific code behind adapters or brokerage boundaries.

## Handoff

When changing public interfaces, update package exports, tests, and the nearest README or AGENTS.md that describes the boundary.
