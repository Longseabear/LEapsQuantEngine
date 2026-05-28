# AGENTS.md

## Scope

`semiconduct-kor` is now the `kr-rally-relay` sleeve. The sleeve id is kept
for account/report/control continuity, but the strategy is no longer limited to
Samsung Electronics and SK hynix.

The workspace owns selection, portfolio, risk, and execution models for a
KRW-only agent-target strategy. The daily agent target artifact is the alpha
source; in-process alpha modules are not wired in the live profile.

## Rules

- Keep model code deterministic and replayable.
- Do not call KIS, broker-engine, market-data-engine, web APIs, or news
  providers from sleeve model code.
- Daily target generation may read stored research evidence and local market
  data, but it must preserve the decision cutoff.
- Portfolio models emit percentage targets only.
- Execution models emit order intents only; they do not submit broker orders.
- Complete target mode is intentional. Missing held symbols should become 0%
  targets so the sleeve can rotate capital.
- Sells are allowed for this sleeve because it is a relay/rotation strategy,
  not a buy-only accumulator.

## Current Contract

Runtime configs:

```text
configs/runtime/live_multi_sleeve.json
configs/runtime/semiconduct_kor_sleeve.json
configs/runtime/semiconduct_kor_shadow.json
```

Universe:

```text
configs/universes/semiconduct_kor_narrative_core.json
```

Live target artifact:

```text
data/operator-targets/semiconduct-kor/latest_target.json
```

Active default models:

```text
selections/agent_narrative_target.py
portfolios/agent_narrative_target.py
risks/basic.py
executions/immediate.py
```

Daily target automation:

```text
tools/build_semiconduct_kor_live_target.ps1
scripts/research/build_semiconduct_kor_narrative_targets.py
```

The older Samsung/SK buy-only models remain in the workspace as research and
rollback references, but they are not the active live contract.
