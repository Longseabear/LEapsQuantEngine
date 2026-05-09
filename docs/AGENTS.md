# AGENTS.md

## Scope

Docs describe public architecture, current status, operational contracts, and integration boundaries.

## Rules

- Update docs when a public interface, runtime contract, or operational workflow changes.
- Prefer concrete pipeline diagrams and exact module names over vague prose.
- Keep status docs current; remove or mark stale claims when implementation changes.
- Do not document KIS direct access from deterministic core as an acceptable path.
- Keep legacy references clearly labeled as reference-only.

## Handoff

When an agent finishes a layer change, docs should say what changed, where the interface lives, and how to verify it.
