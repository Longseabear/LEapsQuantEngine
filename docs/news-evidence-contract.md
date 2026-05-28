# News Evidence Contract

LEapsQuantEngine separates news into three layers. For the operating workflow,
see `docs/news-data-manager-runbook.md`.

```text
raw collection -> shared market evidence -> sleeve-specific news context
```

The shared evidence path remains small and deterministic for backtests. Raw
collection keeps provider records for audit/rebuild. Sleeve context records what
one strategy actually consumed.

## Raw Collection

Raw news is append-only provider output.

```text
data/research/news_raw/<provider>/<market>/YYYY/MM/YYYY-MM-DD.jsonl
```

Each JSONL line should represent one collected article or one collector event.
Keep provider fields, normalized fields, query text, collection time, and error
metadata when present. Do not store portfolio targets, order instructions, or
strategy decisions here.

Minimal article row:

```json
{
  "schema_version": "leaps.news_raw.v1",
  "provider": "google_news_rss",
  "market": "KRX",
  "query": "KOSPI semiconductor Samsung Electronics SK Hynix",
  "collected_at": "2026-05-22T08:40:00+09:00",
  "published_at": "2026-05-22T00:50:26+00:00",
  "seen_at_utc": "2026-05-22T00:50:26+00:00",
  "title": "Example title",
  "url": "https://example.com/article",
  "canonical_url": "https://example.com/article",
  "source_name": "Example News",
  "language": "ko",
  "raw": {}
}
```

Current KRX raw provider paths include:

```text
data/research/news_raw/google-news-rss/krx/YYYY/MM/YYYY-MM-DD.jsonl
data/research/news_raw/kis/domestic/YYYY/MM/YYYY-MM-DD.jsonl
```

KIS domestic news rows are title-level records from the KIS `news-title`
operation. They should use `provider: "kis_domestic_news_title"` and preserve
the provider row under `raw`. They may not have a public article URL; in that
case use a stable synthetic canonical URL such as
`kis://domestic/news-title/<date>/<id>`.

## Shared Market Evidence

```text
data/research/news_evidence/<market>/YYYY-MM-DD.json
```

For Korean equities, use:

```text
data/research/news_evidence/krx/YYYY-MM-DD.json
```

This path is common input for sleeves, reports, and replay builders. It is not
owned by one sleeve, and it must not contain target portfolios, orders, or
strategy decisions.

For backtesting, treat the file date as a decision date. KRX pre-open evidence
should normally include provider records from previous day `08:00` through
decision date `09:00` Asia/Seoul. Later news belongs to the next decision date.

Minimal schema:

```json
{
  "schema_version": "leaps.news_evidence.v1",
  "evidence_id": "krx-news-20260522",
  "market": "KRX",
  "decision_date": "2026-05-22",
  "decision_timezone": "Asia/Seoul",
  "news_window_start_at": "2026-05-21T08:00:00+09:00",
  "decision_cutoff_at": "2026-05-22T09:00:00+09:00",
  "recorded_at": "2026-05-22T17:00:00+09:00",
  "recording_mode": "historical_or_live_cutoff_news_context",
  "news_evidence": {
    "provider": "google_news_rss",
    "query": "...",
    "articles": [],
    "summary": {
      "topic_tags": [],
      "positive_count": 0,
      "negative_count": 0
    }
  },
  "provenance": {
    "writer": "tool-or-agent-name",
    "raw_sources": [
      "data/research/news_raw/google-news-rss/krx/2026/05/2026-05-22.jsonl"
    ],
    "purpose": "Common market/news evidence shared across sleeves."
  }
}
```

When multiple providers are merged, set `news_evidence.provider` to
`multi_source`, list provider names under `provider_sources`, and keep raw
source file paths in `provenance.raw_sources`.

## Sleeve News Context

Sleeves can use different news filters and rankings. A sleeve-specific context
records the exact news view used by that sleeve for a decision date.

```text
sleeves/<sleeve_id>/agent_state/news_context/YYYY-MM-DD.json
```

Minimal schema:

```json
{
  "schema_version": "leaps.sleeve_news_context.v1",
  "sleeve_id": "semiconduct-kor",
  "decision_date": "2026-05-22",
  "decision_timezone": "Asia/Seoul",
  "decision_cutoff_at": "2026-05-22T08:50:00+09:00",
  "source_evidence_paths": [
    "data/research/news_evidence/krx/2026-05-22.json"
  ],
  "strategy_news_profile": {
    "topics": ["semiconductor", "hbm", "memory"],
    "symbols": ["KRX:005930", "KRX:000660"],
    "query_hints": ["Samsung Electronics HBM", "SK Hynix memory"]
  },
  "articles": [],
  "summary": {
    "topic_tags": [],
    "strategy_relevance": "unknown",
    "risk_flags": []
  },
  "provenance": {
    "writer": "tool-or-agent-name",
    "purpose": "Sleeve-specific filtered news context."
  }
}
```

## Consumer Rules

- Sleeve daily judgment files may store `news_evidence_ref.path` for shared
  evidence and `sleeve_news_context_ref.path` for the sleeve-specific view.
- Portfolio target files may store `source_news_evidence_path`.
- Pseudo portfolio builders and backtests should read the common evidence file
  or sleeve context file instead of fetching news directly.
- News collectors must respect `decision_cutoff_at`; no article with a provider
  timestamp after the cutoff should be included.
- If the news filler is unavailable, consumers should fail closed or run with a
  clearly marked missing-news mode rather than fabricating articles.
- Korean text must be written as UTF-8. If provider text arrives mojibaked,
  keep the raw value under `raw` and store repaired text in normalized fields
  only when repair is deterministic.
