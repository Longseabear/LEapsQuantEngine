---
name: leaps-strategy-doc-writer
description: Use when creating or updating UI-facing LEaps sleeve strategy documents such as sleeves/<sleeve_id>/STRATEGY.md. Ensures the first section is a Korean ABSTRACT that explains the strategy philosophy in human language before technical model details.
---

# LEaps Strategy Doc Writer

## Purpose

Use this skill for UI-facing strategy descriptions. These documents are not
developer runbooks and not agent implementation notes. They should help an
operator quickly understand what a sleeve is trying to do, why it exists, and
how to interpret its reports.

## Required Shape

Strategy docs live here:

```text
sleeves/<sleeve_id>/STRATEGY.md
```

Use YAML front matter first:

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

Then put the title and the required first section:

```markdown
# Example Sleeve

## ABSTRACT

이 전략은 ... 한다. ...

## Cadence

- runtime cycle:
- universe / selection:
- alpha model:
- targeting / portfolio construction:
- rebalance / order tracking:

## Recent Judgment Rationale

YYYY-MM-DD의 최근 판단은 ... 이다. 이 판단은 ... 근거를 반영했고,
현금/노출/주문이 남거나 줄어드는 경우에는 ... 로 해석한다.
```

The `ABSTRACT` must be the first human-facing paragraph. Write it in Korean,
as one natural paragraph, for a human operator. It should explain the strategy's
philosophy, not the file layout or config wiring.

`Recent Judgment Rationale` should come immediately after `Cadence`. It explains
the latest live or research judgment in operator language: why the sleeve chose
the current target, regime, exposure, cash posture, or wait state. It is not an
implementation note and must not create a trade instruction by itself.

## Writing Rules

- Start with `## ABSTRACT` immediately after the title.
- Write the ABSTRACT in Korean, one paragraph, no bullets.
- Explain the strategy's intent, temperament, and trade-off in plain language.
- Add `## Cadence` immediately after the ABSTRACT. Include the runtime cycle,
  universe/selection cadence, alpha model cadence, targeting/portfolio
  construction cadence, and rebalance/order-tracking cadence.
- Add `## Recent Judgment Rationale` immediately after Cadence. Keep it short,
  Korean by default, and focused on the latest target/regime/cash/exposure
  reasoning that an operator should understand from reports.
- Keep the rest of the document free-form, but prefer concise sections:
  `Strategy Shape`, `Portfolio Policy`, `Risk And Execution`, `Operator Notes`.
- Describe the LEAN-style pipeline honestly: universe/selection, alpha,
  portfolio, risk, execution.
- State whether the sleeve is `live_active`, `live_suspended`,
  `paper_research`, or `scaffold`.
- Do not include secrets, account numbers, tokens, or personal local paths.
- Do not claim a sleeve is live-active unless runtime config/control confirms it.
- Avoid stale operational instructions. Link to runbooks for commands instead
  of embedding long command blocks.
- For model details, name the behavior before naming the file. Humans should
  understand the strategy without reading Python module paths.

## Good ABSTRACT Pattern

```markdown
## ABSTRACT

이 전략은 시장을 자주 예측하려 하기보다, 비교적 안정적인 종목을 골라 낮은 회전율로 보유하면서 큰 흔들림을 피하려는 방어형 전략이다. 강한 상승장을 모두 따라잡는 것보다 손실과 과잉매매를 줄이는 것을 더 중요하게 보며, 포트폴리오 변화는 천천히 반영되도록 설계한다.
```

## When Updating Existing Docs

1. Read the sleeve `README.md`, `AGENTS.md`, runtime config, and active-sleeve
   control file if live status matters.
2. Preserve facts that are still current; remove or soften stale claims.
3. Keep the ABSTRACT stable enough for UI display.
4. Keep Cadence values tied to the runtime config and alpha model constants.
5. Update `Recent Judgment Rationale` when the sleeve target, regime, exposure,
   cash posture, or wait-state explanation changes. Use current artifacts or
   sleeve-agent feedback as the source; distinguish inference from fact.
6. Put technical specifics after the ABSTRACT, Cadence, and Recent Judgment
   Rationale.
7. If the strategy has changed but runtime has not reloaded, say that clearly.
