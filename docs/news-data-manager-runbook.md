# News Data Manager Runbook

This runbook defines how the data manager operates LEapsQuantEngine news data.
It complements `docs/news-evidence-contract.md`.

## Role

The news data manager owns collection, storage, quality checks, and handoff of
news data. The data manager does not create target portfolios, orders, or
strategy decisions.

## Storage Layers

Use three layers:

```text
data/research/news_raw/<provider>/<market>/YYYY/MM/YYYY-MM-DD.jsonl
data/research/news_evidence/<market>/YYYY-MM-DD.json
sleeves/<sleeve_id>/agent_state/news_context/YYYY-MM-DD.json
```

Layer meanings:

- `news_raw`: append-only provider records and collector events.
- `news_evidence`: shared market-level daily evidence with cutoff protection.
- `news_context`: sleeve-specific filtered/ranked news view.

Current provider paths:

```text
data/research/news_raw/google-news-rss/krx/YYYY/MM/YYYY-MM-DD.jsonl
data/research/news_raw/kis/domestic/YYYY/MM/YYYY-MM-DD.jsonl
data/research/news_raw/kis/domestic/manifest_YYYY-MM-DD_YYYY-MM-DD.json
data/research/news_evidence/krx/YYYY-MM-DD.json
data/research/news_evidence/krx/manifest.json
```

Google News RSS rows are public search evidence with article URLs. KIS domestic
news rows are title-level KIS records from the read-only `news-title` endpoint;
they usually do not include public article URLs, so evidence records may use a
`kis://domestic/news-title/...` canonical URL and keep the provider row under
`raw`.

## Daily Workflow

1. Collect raw provider records.

   Write append-only JSONL rows under `data/research/news_raw`. Include provider,
   query, collected timestamp, provider publication/seen timestamp, URL, source,
   language, normalized title, and raw provider fields when useful.

2. Build shared market evidence.

   Write `data/research/news_evidence/<market>/YYYY-MM-DD.json` using
   `schema_version: leaps.news_evidence.v1`. For backtesting, interpret
   `YYYY-MM-DD` as the decision date, not the provider calendar day. The
   preferred KRX pre-open window is previous day `08:00` through decision date
   `09:00` Asia/Seoul. Store the start as `news_window_start_at` and the cutoff
   as `decision_cutoff_at`, and include only articles in that window.

3. Build sleeve contexts only when explicitly requested.

   The default operating mode is for the research data manager skill to inspect
   shared evidence directly and filter/rank it for the requested sleeve in the
   response. Persist
   `sleeves/<sleeve_id>/agent_state/news_context/YYYY-MM-DD.json` only when the
   operator explicitly asks for sleeve-specific artifacts. If persisted, use
   `schema_version: leaps.sleeve_news_context.v1` and reference the shared
   evidence file in `source_evidence_paths`.

4. Update refs in downstream artifacts.

   Daily judgment files may reference shared evidence with `news_evidence_ref`
   and sleeve context with `sleeve_news_context_ref`. Target files may reference
   `source_news_evidence_path`, but should not embed raw news unnecessarily.

## Sleeve Profiles

Default sleeve news focus:

- `LEaps`: KRX market regime, KOSPI/KOSDAQ momentum, semiconductor leadership,
  liquidity shocks, broad risk-on/risk-off context.
- `semiconduct-kor`: Samsung Electronics, SK Hynix, HBM, memory, foundry,
  semiconductor equipment, KOSDAQ semiconductor momentum.
- `kr-lowvol-defensive`: dividends, stable earnings, defensive sectors, rates,
  volatility, drawdown risk, market stress.
- `kr-domestic-4401`: universe-specific headlines, disclosures, event risk,
  unusual volume, liquidity shocks.
- `us_etf_rotation`: Fed, CPI, Treasury yields, USD, Nasdaq/S&P risk appetite,
  sector rotation, ETF flows.

## Quality Checks

Before handing news to strategy consumers:

- Confirm every article timestamp is at or before `decision_cutoff_at`.
- Confirm the file is UTF-8 JSON or JSONL.
- Deduplicate by canonical URL when available; otherwise use URL.
- Keep broken provider text under `raw`; normalized fields should only contain
  deterministic repairs.
- Ensure evidence/context files do not contain target weights, orders, or broker
  payloads.
- Ensure sleeve context references the source evidence path it was built from.

## Failure Modes

If raw collection fails:

- Write no fabricated articles.
- Build shared evidence only from already-collected raw records if available.
- Mark missing data in `summary` or provenance so consumers can fail closed or
  run in a clearly marked missing-news mode.

If Korean text arrives mojibaked:

- Preserve the raw provider value under `raw`.
- Store repaired text only when the repair is deterministic.
- Prefer UTF-8 files or stdin for Korean text generation; avoid command-line
  string literals for long Korean content.

## Backtest Rule

Backtests and pseudo portfolio builders must read stored evidence/context files.
They should not fetch fresh news during replay. This preserves point-in-time
behavior and prevents lookahead.

For KRX research replays, use `data/research/news_evidence/krx/YYYY-MM-DD.json`
as the shared daily input. Sleeve-specific filtering should usually happen at
read time through the research data manager skill. Persist sleeve context files
only for runs that need an auditable frozen sleeve view, and record the source
evidence path. Never fill missing historical news by inventing articles; use
existing raw rows, rerun a collector, or mark the missing-news mode clearly.

Backtest-oriented KRX evidence files should mean:

```text
decision_date YYYY-MM-DD
news_window_start_at = previous day 08:00 Asia/Seoul
decision_cutoff_at   = decision date 09:00 Asia/Seoul
```

News after the cutoff belongs to the next decision date.

## Current First Tasks

1. Add a raw collector that writes `leaps.news_raw.v1` JSONL.
2. Add a builder that converts raw KRX records into shared evidence.
3. Add sleeve context builders for the active sleeves.
4. Add lightweight validators for schema, cutoff, UTF-8, and no-trading-fields.
