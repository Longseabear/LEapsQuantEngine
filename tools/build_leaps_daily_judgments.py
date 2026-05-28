from __future__ import annotations

import argparse
from email.utils import parsedate_to_datetime
import json
import sys
import time as time_module
import urllib.parse
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
DEFAULT_NEWS_QUERIES = (
    'KOSPI semiconductor "Samsung Electronics" "SK Hynix"',
    "KOSDAQ KOSPI Korea stocks semiconductor",
)


@dataclass(frozen=True, slots=True)
class BuildConfig:
    start: date
    end: date
    proxy_target_dir: Path
    output_dir: Path
    news_output_dir: Path
    live_target_path: Path
    current_state_path: Path
    decision_time: time
    today: date
    max_news_articles: int
    news_window_hours: int
    sleep_seconds: float
    request_timeout_seconds: float
    refresh_news: bool


def main() -> int:
    args = _parse_args()
    cfg = BuildConfig(
        start=_parse_date(args.start),
        end=_parse_date(args.end),
        proxy_target_dir=Path(args.proxy_target_dir),
        output_dir=Path(args.output_dir),
        news_output_dir=Path(args.news_output_dir),
        live_target_path=Path(args.live_target_path),
        current_state_path=Path(args.current_state_path),
        decision_time=_parse_time(args.decision_time),
        today=_parse_date(args.today),
        max_news_articles=int(args.max_news_articles),
        news_window_hours=int(args.news_window_hours),
        sleep_seconds=float(args.sleep_seconds),
        request_timeout_seconds=float(args.request_timeout_seconds),
        refresh_news=bool(args.refresh_news),
    )
    if cfg.end < cfg.start:
        raise SystemExit("--end must be greater than or equal to --start")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    cfg.news_output_dir.mkdir(parents=True, exist_ok=True)

    name_map = _load_krx_name_map()
    current_state = _read_json(cfg.current_state_path, default={})
    news_cache_path = cfg.news_output_dir / "_news_cache.json"
    news_cache = {} if cfg.refresh_news else _read_json(news_cache_path, default={})

    summaries: list[dict[str, Any]] = []
    for target_path in _target_paths(cfg):
        target_date = cfg.today if target_path == cfg.live_target_path else _parse_date(target_path.stem)
        target = _read_json(target_path)
        decision_cutoff = _decision_cutoff(target_date, target, cfg)
        news_evidence = _news_evidence_for_day(
            target_date=target_date,
            cutoff=decision_cutoff,
            cfg=cfg,
            news_cache=news_cache,
        )
        news_evidence_ref = _write_common_news_evidence(
            target_date=target_date,
            cutoff=decision_cutoff,
            news_evidence=news_evidence,
            cfg=cfg,
        )
        cleaned_target = _target_portfolio(target, target_path, name_map)
        payload = _judgment_payload(
            target_date=target_date,
            decision_cutoff=decision_cutoff,
            recording_mode=_recording_mode(target_date, cfg.today),
            target_portfolio=cleaned_target,
            news_evidence_ref=news_evidence_ref,
            news_summary=news_evidence.get("summary", {}),
            current_state=current_state,
        )
        output_path = cfg.output_dir / f"{target_date.isoformat()}.json"
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        news_cache_path.write_text(json.dumps(news_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        summaries.append(
            {
                "date": target_date.isoformat(),
                "output_path": str(output_path.as_posix()),
                "news_evidence_path": news_evidence_ref.get("path"),
                "target_count": len(cleaned_target.get("targets", [])),
                "news_article_count": len(news_evidence.get("articles", [])),
                "recording_mode": payload["recording_mode"],
            }
        )

    news_cache_path.write_text(json.dumps(news_cache, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "schema_version": "leaps.daily_judgment_manifest.v1",
        "created_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "range": {"start": cfg.start.isoformat(), "end": cfg.end.isoformat()},
        "output_dir": str(cfg.output_dir.as_posix()),
        "news_evidence_dir": str(cfg.news_output_dir.as_posix()),
        "source_targets": {
            "proxy_target_dir": str(cfg.proxy_target_dir.as_posix()),
            "live_target_path": str(cfg.live_target_path.as_posix()),
        },
        "news_policy": {
            "provider": "Google News RSS",
            "cutoff_rule": "Only RSS pubDate/seendate values at or before each decision_cutoff_at are retained.",
            "window_hours": cfg.news_window_hours,
            "queries": list(DEFAULT_NEWS_QUERIES),
        },
        "historical_limitations": [
            "Past files are reconstructed from point-in-time proxy target artifacts, not originally written live agent decisions.",
            "Proxy target features use daily bars before the target date; news evidence uses provider timestamps before the decision cutoff.",
            "This artifact supports research replay/audit, but should not be represented as proof of live premarket judgment unless recording_mode says so.",
        ],
        "judgments": summaries,
    }
    (cfg.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps({"created": len(summaries), "manifest": str((cfg.output_dir / "manifest.json").as_posix())}, ensure_ascii=False))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build LEaps daily judgment artifacts with point-in-time news evidence.")
    parser.add_argument("--start", default="2026-04-22")
    parser.add_argument("--end", default="2026-05-22")
    parser.add_argument("--today", default="2026-05-22")
    parser.add_argument("--decision-time", default="08:50")
    parser.add_argument("--proxy-target-dir", default="data/research/leaps-agent-proxy-20260421-20260521/targets")
    parser.add_argument("--live-target-path", default="data/operator-targets/LEaps/latest_target.json")
    parser.add_argument("--current-state-path", default="sleeves/LEaps/agent_state/current_state.json")
    parser.add_argument("--output-dir", default="sleeves/LEaps/agent_state/daily_judgments")
    parser.add_argument("--news-output-dir", default="data/research/news_evidence/krx")
    parser.add_argument("--max-news-articles", type=int, default=8)
    parser.add_argument("--news-window-hours", type=int, default=72)
    parser.add_argument("--sleep-seconds", type=float, default=0.25)
    parser.add_argument("--request-timeout-seconds", type=float, default=12.0)
    parser.add_argument("--refresh-news", action="store_true")
    return parser.parse_args()


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _parse_time(value: str) -> time:
    hour, minute = value.split(":", 1)
    return time(int(hour), int(minute), tzinfo=KST)


def _read_json(path: Path, default: Any | None = None) -> Any:
    if not path.exists():
        if default is not None:
            return default
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _target_paths(cfg: BuildConfig) -> list[Path]:
    paths: list[Path] = []
    current = cfg.start
    while current <= cfg.end:
        proxy_path = cfg.proxy_target_dir / f"{current.isoformat()}.json"
        if proxy_path.exists():
            paths.append(proxy_path)
        elif current == cfg.today and cfg.live_target_path.exists():
            paths.append(cfg.live_target_path)
        current += timedelta(days=1)
    return paths


def _decision_cutoff(target_date: date, target: dict[str, Any], cfg: BuildConfig) -> datetime:
    if target_date == cfg.today:
        generated_at = _parse_datetime_maybe(target.get("generated_at"))
        if generated_at is not None:
            return generated_at.astimezone(KST)
    return datetime.combine(target_date, cfg.decision_time).astimezone(KST)


def _parse_datetime_maybe(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def _recording_mode(target_date: date, today: date) -> str:
    if target_date < today:
        return "historical_reconstruction_from_proxy_target_with_news_cutoff"
    return "posthoc_from_live_target_artifact_with_news_cutoff"


def _target_portfolio(target: dict[str, Any], target_path: Path, name_map: dict[str, str]) -> dict[str, Any]:
    targets: list[dict[str, Any]] = []
    for item in target.get("targets", []):
        copied = dict(item)
        symbol = str(copied.get("symbol", ""))
        ticker = symbol.split(":", 1)[-1]
        if ticker in name_map:
            copied["name"] = name_map[ticker]
        targets.append(copied)
    gross = sum(float(item.get("target_percent", 0.0)) for item in targets)
    return {
        "source_artifact": str(target_path.as_posix()),
        "target_id": target.get("target_id"),
        "generated_at": target.get("generated_at"),
        "expires_at": target.get("expires_at"),
        "max_gross_exposure": target.get("max_gross_exposure"),
        "gross_target": round(gross, 6),
        "flatten": bool(target.get("flatten", False)),
        "method": target.get("method", "live_agent_target_artifact"),
        "lookahead_guard": target.get("lookahead_guard"),
        "targets": targets,
    }


def _news_evidence_for_day(
    *,
    target_date: date,
    cutoff: datetime,
    cfg: BuildConfig,
    news_cache: dict[str, Any],
) -> dict[str, Any]:
    window_start = cutoff - timedelta(hours=cfg.news_window_hours)
    articles_by_url: dict[str, dict[str, Any]] = {}
    queries_used: list[str] = []
    for query in DEFAULT_NEWS_QUERIES:
        cache_key = "|".join(
            [
                target_date.isoformat(),
                cutoff.astimezone(timezone.utc).strftime("%Y%m%d%H%M%S"),
                query,
            ]
        )
        if cache_key not in news_cache:
            news_cache[cache_key] = _query_google_news_rss(
                query=query,
                start=window_start,
                end=cutoff,
                max_records=cfg.max_news_articles,
                timeout_seconds=cfg.request_timeout_seconds,
            )
            if cfg.sleep_seconds > 0:
                time_module.sleep(cfg.sleep_seconds)
        queries_used.append(query)
        for article in news_cache.get(cache_key, []):
            normalized = _normalize_article(article, cutoff)
            if not normalized:
                continue
            if "url" not in normalized:
                continue
            articles_by_url.setdefault(normalized["url"], normalized)
    articles = sorted(
        articles_by_url.values(),
        key=lambda item: item.get("seen_at_utc") or "",
        reverse=True,
    )[: cfg.max_news_articles]
    return {
        "provider": "Google News RSS",
        "provider_url": "https://news.google.com/rss/search",
        "decision_news_cutoff_at": cutoff.isoformat(timespec="seconds"),
        "window_start_at": window_start.isoformat(timespec="seconds"),
        "lookahead_guard": "Articles are retained only when RSS pubDate/seendate <= decision_news_cutoff_at.",
        "queries": queries_used,
        "articles": articles,
        "summary": _news_summary(articles),
    }


def _write_common_news_evidence(
    *,
    target_date: date,
    cutoff: datetime,
    news_evidence: dict[str, Any],
    cfg: BuildConfig,
) -> dict[str, Any]:
    output_path = cfg.news_output_dir / f"{target_date.isoformat()}.json"
    payload = {
        "schema_version": "leaps.news_evidence.v1",
        "evidence_id": f"krx-news-{target_date:%Y%m%d}",
        "market": "KRX",
        "decision_date": target_date.isoformat(),
        "decision_timezone": "Asia/Seoul",
        "decision_cutoff_at": cutoff.isoformat(timespec="seconds"),
        "recorded_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "recording_mode": "historical_or_live_cutoff_news_context",
        "news_evidence": news_evidence,
        "provenance": {
            "writer": "tools/build_leaps_daily_judgments.py",
            "purpose": "Common market/news evidence shared across sleeves and portfolio builders.",
        },
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "schema_version": "leaps.news_evidence.v1",
        "path": output_path.as_posix(),
        "market": "KRX",
        "decision_cutoff_at": cutoff.isoformat(timespec="seconds"),
        "article_count": len(news_evidence.get("articles", [])),
        "topic_tags": list((news_evidence.get("summary") or {}).get("topic_tags") or []),
    }


def _query_google_news_rss(
    *,
    query: str,
    start: datetime,
    end: datetime,
    max_records: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    # Google News RSS only accepts date filters in the query string. We query a
    # slightly broad calendar window and later enforce exact pubDate <= cutoff.
    before = (end.astimezone(timezone.utc).date() + timedelta(days=1)).isoformat()
    after = (start.astimezone(timezone.utc).date() - timedelta(days=1)).isoformat()
    rss_query = f"{query} after:{after} before:{before}"
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": rss_query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    )
    try:
        import requests  # type: ignore

        response = requests.get(
            url,
            timeout=max(1.0, timeout_seconds),
            headers={"User-Agent": "LEapsQuantEngine/1.0 daily-judgment-builder"},
        )
        response.raise_for_status()
        root = ElementTree.fromstring(response.content)
    except Exception as exc:
        return [{"error": f"{type(exc).__name__}: {exc}", "query_url": url}]
    articles: list[dict[str, Any]] = []
    for item in root.findall("./channel/item"):
        source = item.find("source")
        pub_date = _parse_rss_pubdate(item.findtext("pubDate"))
        articles.append(
            {
                "url": item.findtext("link"),
                "title": item.findtext("title"),
                "seendate": pub_date.strftime("%Y%m%dT%H%M%SZ") if pub_date else None,
                "domain": source.attrib.get("url") if source is not None else None,
                "language": "Korean/English",
                "sourcecountry": "South Korea/global",
                "source_name": source.text if source is not None else None,
                "query_url": url,
            }
        )
        if len(articles) >= max_records * 4:
            break
    return articles


def _parse_rss_pubdate(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_article(article: dict[str, Any], cutoff: datetime) -> dict[str, Any] | None:
    if article.get("error"):
        return {
            "error": article.get("error"),
            "query_url": article.get("query_url"),
            "used_as": "provider_error",
        }
    url = str(article.get("url") or "").strip()
    if not url:
        return None
    seen_at = _parse_compact_utc(article.get("seendate"))
    if seen_at is not None and seen_at > cutoff.astimezone(timezone.utc):
        return None
    return {
        "title": str(article.get("title") or "").strip(),
        "url": url,
        "domain": article.get("domain"),
        "source_country": article.get("sourcecountry"),
        "source_name": article.get("source_name"),
        "language": article.get("language"),
        "seen_at_utc": seen_at.isoformat(timespec="seconds") if seen_at else article.get("seendate"),
        "used_as": "pre_cutoff_market_news_context",
    }


def _parse_compact_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _news_summary(articles: list[dict[str, Any]]) -> dict[str, Any]:
    titles = " ".join(str(article.get("title", "")) for article in articles).lower()
    tags: list[str] = []
    for label, keywords in {
        "semiconductor": ("semiconductor", "chip", "반도체", "삼성전자", "하이닉스"),
        "market_index": ("kospi", "kosdaq", "코스피", "코스닥", "증시"),
        "us_market": ("s&p", "nasdaq", "dow", "미 증시", "나스닥"),
        "rate_macro": ("rate", "fed", "yield", "금리", "연준", "환율"),
        "ai": ("ai", "artificial intelligence", "인공지능"),
    }.items():
        if any(keyword in titles for keyword in keywords):
            tags.append(label)
    return {
        "article_count": len(articles),
        "topic_tags": tags,
        "interpretation": (
            "News evidence is used as context only; target weights remain tied to the point-in-time target artifact "
            "and should be separately validated in replay."
        ),
    }


def _judgment_payload(
    *,
    target_date: date,
    decision_cutoff: datetime,
    recording_mode: str,
    target_portfolio: dict[str, Any],
    news_evidence_ref: dict[str, Any],
    news_summary: dict[str, Any],
    current_state: dict[str, Any],
) -> dict[str, Any]:
    targets = target_portfolio.get("targets", [])
    top_names = [item.get("name") or item.get("symbol") for item in targets[:3]]
    current_mode = (current_state.get("operating_mode") or {}).get("mode_id", "agent_daily_target_v1")
    return {
        "schema_version": "leaps.daily_judgment.v1",
        "sleeve_id": "LEaps",
        "judgment_id": f"LEaps-judgment-{target_date.isoformat()}",
        "decision_date": target_date.isoformat(),
        "decision_timezone": "Asia/Seoul",
        "decision_cutoff_at": decision_cutoff.isoformat(timespec="seconds"),
        "recorded_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "recording_mode": recording_mode,
        "backtest_eligibility": {
            "usable_for_engine_replay": True,
            "usable_as_true_premarket_agent_judgment": recording_mode.startswith("recorded_before"),
            "reason": (
                "Historical rows were reconstructed after the fact from proxy target artifacts. "
                "They enforce price/news cutoff discipline but are not original live decisions."
            ),
        },
        "runtime_contract": {
            "operating_mode": current_mode,
            "live_target_artifact_path": "data/operator-targets/LEaps/latest_target.json",
            "selection_model": "sleeves/LEaps/selections/agent_daily_target.py:AgentDailyTargetSelectionModel",
            "portfolio_model": "sleeves/LEaps/portfolios/agent_daily_target.py:AgentDailyTargetPortfolioModel",
            "risk_model": "sleeves/LEaps/risks/kospi_growth_us_hedge.py",
            "execution_model": "sleeves/LEaps/executions/leaps_immediate.py",
            "alpha_modules_active": [],
        },
        "decision_summary": {
            "target_id": target_portfolio.get("target_id"),
            "stance": "KRX momentum swing basket selected by daily agent/proxy target artifact",
            "gross_target": target_portfolio.get("gross_target"),
            "currency": "KRW",
            "thesis": (
                "Favor liquid KRX momentum leaders while keeping risk and execution engine-owned. "
                f"Top target names: {', '.join(str(name) for name in top_names)}."
            ),
            "news_context_tags": news_summary.get("topic_tags", []),
            "primary_risks": [
                "Past judgment is reconstructed, not a live premarket decision.",
                "Momentum targets can reverse after crowded breakouts.",
                "News evidence is timestamp-filtered by RSS pubDate/seendate but still depends on provider coverage quality.",
            ],
        },
        "target_portfolio": target_portfolio,
        "news_evidence_ref": news_evidence_ref,
    }


def _load_krx_name_map() -> dict[str, str]:
    try:
        import FinanceDataReader as fdr  # type: ignore

        listing = fdr.StockListing("KRX")
    except Exception:
        return {
            "005930": "삼성전자",
            "000660": "SK하이닉스",
            "005380": "현대차",
            "009150": "삼성전기",
            "066570": "LG전자",
            "011070": "LG이노텍",
            "036930": "주성엔지니어링",
            "353200": "대덕전자",
            "000500": "가온전선",
            "222800": "심텍",
            "095610": "테스",
        }
    if "Code" not in listing.columns or "Name" not in listing.columns:
        return {}
    return {str(row.Code).zfill(6): str(row.Name) for row in listing.itertuples(index=False)}


if __name__ == "__main__":
    raise SystemExit(main())
