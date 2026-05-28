# Sleeve Strategy Profile Documents

Sleeve `README.md` and `AGENTS.md` files are for developers and sleeve agents.
They may include setup commands, validation notes, historical context, and
implementation details that are too noisy for an operator UI.

The operator UI should prefer this file when it wants to explain a sleeve's
currently adopted strategy:

```text
sleeves/<sleeve_id>/STRATEGY.md
```

## Contract

Each strategy profile starts with YAML front matter:

```yaml
---
schema_version: leaps_sleeve_strategy.v1
sleeve_id: example-sleeve
display_name: Example Sleeve
status: live_active
market_scope: domestic
currency: KRW
updated_at: 2026-05-22
---
```

Supported `status` values are:

- `live_active`: currently eligible to run in the live multi-sleeve loop.
- `live_suspended`: configured or implemented, but intentionally removed from
  the active live loop.
- `paper_research`: implemented for paper or research validation only.
- `scaffold`: workspace exists, but the strategy is not live-ready yet.

The Markdown body is intentionally short and UI-facing. The first section after
the title must be `## ABSTRACT`, written in Korean as one natural paragraph for
a human operator. It should answer:

- What is the strategy philosophy?
- What cadence controls runtime cycles, selection, alpha evaluation, targeting,
  and rebalance/order tracking?
- What does this sleeve trade?
- What signal family does it use?
- How does it construct portfolio targets?
- Which risk and execution style is active?
- What should an operator know before trusting the report?

See `.codex/skills/leaps-strategy-doc-writer/SKILL.md` before creating or
updating these files.

## Current Profiles

- `sleeves/LEaps/STRATEGY.md`
- `sleeves/kr-lowvol-defensive/STRATEGY.md`
- `sleeves/us_etf_rotation/STRATEGY.md`
- `sleeves/semiconduct-kor/STRATEGY.md`
- `sleeves/kr-domestic-4401/STRATEGY.md`

Do not put secrets, account numbers, or local-only runtime paths in these files.
The UI may render them directly.
