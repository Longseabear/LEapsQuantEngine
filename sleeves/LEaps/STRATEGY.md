---
schema_version: leaps_sleeve_strategy.v1
sleeve_id: LEaps
display_name: LEaps Agent Daily Momentum
status: live_active_trading_day_preopen_recheck_reaffirmed
market_scope: domestic
currency: KRW
updated_at: 2026-05-28
---

# LEaps Agent Daily Momentum

## ABSTRACT

LEaps는 매일 08:00~08:30 KST 구간에서 KRX 타깃 비중을 확정합니다.
2026-05-28 재점검에서는 뉴스 증거 파일 존재와 프리오픈 호가 유효성은 확인됐고,
포지션 축소를 정당화할 신규 악화 근거가 없어 기존 유효 타깃을 유지했습니다.

## Cadence

- Runtime cycle: 5분 (`cycle_interval_seconds = 300`)
- Universe selection: `daily_at 08:45 Asia/Seoul`
- Portfolio refresh: `daily_at 08:50 Asia/Seoul`
- Alpha modules: 없음 (`[]`), `agent_daily_target_v1` mode
- Live target input: `data/operator-targets/LEaps/latest_target.json`

## Current Portfolio Thesis

- Target ID: `leaps-agent-20260528-0838-operator-recheck-carryforward`
- Generated At: `2026-05-28T08:39:22+09:00`
- Expires At: `2026-05-29T08:50:00+09:00`
- Gross Target: `95%` (cash 5%)
- Thesis: 뉴스 존재 + 프리오픈 호가 확인, 다만 비중 변경을 요구하는 악화 증거 부재로 carry-forward 유지

### Target Weights (KRW only)

- `KRX:011070` LG이노텍 24% (confidence 0.75)
- `KRX:036930` 주성엔지니어링 24% (confidence 0.74)
- `KRX:009150` 삼성전기 16% (confidence 0.73)
- `KRX:222800` 심텍 12% (confidence 0.66)
- `KRX:353200` 대덕전자 10% (confidence 0.62)
- `KRX:095610` 테스 9% (confidence 0.58)

## Freshness And Session Note

- 공통 뉴스 증거: `data/research/news_evidence/krx/2026-05-28.json` (article_count=70)
- 일봉 매니페스트 최신일 커버리지: `938/1000` (최신일 2026-05-27)
- 타깃 종목 중 3개(009150, 353200, 095610)는 최신 확정 일봉이 2026-05-22
- 08:35 KST read-only runtime: `session_phase=pre_open_after_hours`, `is_orderable=true`, `snapshot_quality=degraded \(manager usable news confirmed\)`

## Live Application Note

- 수동 주문 제출 없음
- 일반 live loop/order runtime에 적용 위임
- 비중 변경은 확인 가능한 시장/기업 악화 증거가 생길 때만 수행
