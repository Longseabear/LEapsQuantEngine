# AGENTS.md

## Scope

Risk models for `us_etf_rotation`.

## Rules

- Approve, reject, or clamp targets only.
- Do not create broker orders or mutate account stores.
- Keep the sleeve long-only unless runtime config explicitly changes policy.
