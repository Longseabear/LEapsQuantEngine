---
schema_version: leaps_sleeve_strategy.v1
sleeve_id: semiconduct-kor
display_name: KR Rally Relay
status: live_active
market_scope: domestic
currency: KRW
updated_at: 2026-05-28
---

# KR Rally Relay

## ABSTRACT

KR Rally Relay는 한국 시장에서 그날 아침 가장 강하게 이어질 랠리 후보로 자본을 옮겨 타는 agent sleeve다. 매일 08:00 KST 무렵 뉴스, 웹 정보, 공개 주가 맥락, 로컬 리서치 자료를 함께 보고 포트폴리오를 한 번 정하며, 장중에는 새 판단으로 흔들기보다 주문과 리스크 상태를 추적한다. 정상적인 시장에서는 현금을 오래 남기지 않고 gross 95%에 가깝게 쓰되, 뉴스와 시장 정보가 명확히 위험 국면을 가리킬 때만 노출을 낮춘다. 예전처럼 삼성전자와 SK하이닉스만 모으는 buy-only sleeve가 아니라, 반도체, 시장 베타, AI 하드웨어, 조선, 방산, 전력망, 보안, 핵융합/원전, 자동차, 금융 등 국내 랠리 후보판에서 가장 설득력 있는 테마로 갈아타는 리밸런싱 sleeve다.

## Cadence

- runtime cycle: 1 minute while the live KRX sleeve is scheduled
- universe / selection: daily at 08:00 Asia/Seoul from the latest agent target artifact, with held/open-order symbols kept operationally visible
- alpha model: none; the daily agent target artifact is the alpha source
- targeting / portfolio construction: daily at 09:05 KST, complete target mode, reading `data/operator-targets/semiconduct-kor/latest_target.json`
- rebalance / order tracking: every scheduled cycle; execution may buy or sell inside `09:05-14:50 Asia/Seoul`

## Recent Judgment Rationale

최신 target artifact 기준 판단일은 2026-05-28이며, risk_on 판단 아래 `Samsung Electro-Mechanics`, `KODEX Semiconductor`, `SK hynix`, `KODEX 200`, `TIGER 200`로 95% gross target을 구성했다. artifact상 risk_score는 0.0이고 뉴스 근거 수는 12건이다. gross가 95%에 가까우면 현금 잔류는 전략적 대기라기보다 5% 리스크 버퍼와 실행단 반올림/체결 상태로 해석한다. 다음 08:00 자동화가 새 뉴스, 웹 정보, 공개 주가 맥락을 반영해 target을 바꾸면 이 섹션도 함께 갱신해야 한다.

## Strategy Shape

```text
KRX rally universe
  -> daily agent target selection
  -> no in-process alpha
  -> complete target portfolio construction
  -> basic long-only risk
  -> standard limit execution intents
```

The sleeve keeps the existing `semiconduct-kor` id for account, report, and control continuity. The operator-facing name is now `KR Rally Relay`.

## Universe

The live universe is no longer limited to Samsung Electronics and SK hynix. The coarse universe is a 66-name rally board made of liquid KRX ETFs and representative stocks across semiconductor, broad market, AI hardware, shipbuilding, defense, power grid, energy, cybersecurity, fusion/nuclear proxies, auto, bank, battery, bio, and internet/platform themes.

The active universe comes from the daily target artifact. Symbols with current holdings or open orders remain monitored through the engine's operational-symbol invariant even when they are missing from the new daily artifact.

## Agent Target Policy

The target builder reads KRX news evidence, public market context, and local daily bars. Live automation may actively use fresh news, web search, RSS, and public stock-market information available at the 08:00 KST decision time. Replay must not fetch fresh web data or fabricate missing news; for reconstructed research targets, the point-in-time window is:

- news window: previous day 08:00 through decision day 08:00 KST
- price window: bars strictly before the decision date
- output: `data/operator-targets/semiconduct-kor/latest_target.json`

Base gross exposure is high because this sleeve is expected to keep cash working:

- normal mornings: gross target starts at 95%
- missing news coverage is not defensive by itself; it still targets 95%
- explicit pre-open risk evidence can lower gross exposure, with a research floor near 85%
- live risk clamps total exposure to 95% with a 5% cash buffer

## Portfolio Policy

Portfolio construction uses complete target mode. If a held symbol is omitted from the daily artifact, the model emits a 0% target so the sleeve can rotate out. This is intentional and replaces the old buy-only policy.

The runtime refreshes portfolio targets once in the morning at 09:05 KST. The agent still writes a single 08:00 target artifact, and the engine turns that target into integer quantities once after the regular session becomes orderable. Whole-share rounding and reused-target churn guards remain enabled for any reused target handling.

Reused-target churn protection is intentionally narrow. It should suppress only tiny adjacent-lot noise from whole-share rounding, not meaningful drift from the daily target. For this sleeve the reused-target lot threshold is 0.25 lots, and minimum order notional is treated as an orderability floor rather than as a churn/noise threshold.

## Risk And Execution

Risk is long-only, caps position size, and rejects invalid or stale snapshots for entries. Execution emits day limit order intents only; broker submission and ticket lifecycle remain owned by the runtime.

Sells are now allowed for this sleeve. That is required because the strategy is a capital relay, not an accumulator. Sell intent should come from complete target rotation, not from hidden execution logic.

## Backtest Read

Point-in-time daily target generation and replay were tested with 5,000,000 KRW.

- Daily replay, 2026-05-04 to 2026-05-22, parquet daily source, KIS fee model, 5 bps slippage, 66-name rally board: final equity 6,191,879 KRW, total return 23.84%, MDD 6.91%, average exposure 78.80%, 38 orders.
- Minute replay, 2026-04-21 09:00 to 2026-05-15 14:59, largest local merged KRX minute feed, KIS fee model, 5 bps slippage, pre-66-name board: final equity 5,950,160 KRW, total return 19.00%, MDD 7.00%, average exposure 79.15%, 57 orders.

The minute result showed that repeated intraday target reconstruction can create unnecessary quantity churn, so live portfolio construction runs once at 09:05 KST from the 08:00 target artifact. Runtime cycles still monitor orders, fills, risk, and session state.

## Operator Notes

The sleeve is live-active in `configs/runtime/live_multi_sleeve.json`. A separate 08:00 automation builds the daily target artifact before the KRX session. If that artifact is missing or expired, the model fails closed rather than inventing a portfolio.
