# AGENTS.md

## Scope

Universe configs define precomputed or sample symbol sets for research, active selection, and smoke runs.

## Rules

- Keep symbols provider-neutral where possible and include exchange/market metadata when needed.
- Do not put alpha ranking formulas or trading decisions in universe files.
- Use universe files as inputs to selection models, not as hidden portfolio target lists.
- Keep operational symbols forced by runtime state, not manually duplicated here unless the sample explicitly demonstrates it.

## Tests

Update universe loader or selection tests when the file shape changes.
