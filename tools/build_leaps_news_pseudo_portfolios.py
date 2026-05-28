from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


POSITIVE_KEYWORDS = (
    "rally",
    "surge",
    "jump",
    "gain",
    "gains",
    "record",
    "highest",
    "breaks",
    "strong",
    "buy-side",
    "sidecar",
    "급등",
    "상승",
    "강세",
    "랠리",
    "최고",
    "돌파",
    "질주",
    "순매수",
    "반등",
)
NEGATIVE_KEYWORDS = (
    "lower",
    "decline",
    "falls",
    "fell",
    "selloff",
    "sell-off",
    "risk",
    "burden",
    "retreat",
    "weak",
    "하락",
    "약세",
    "매도",
    "이탈",
    "부담",
    "쉬어",
    "조정",
)
SEMICONDUCTOR_KEYWORDS = (
    "semiconductor",
    "chip",
    "chips",
    "hbm",
    "ai",
    "반도체",
    "삼성전자",
    "하이닉스",
)
RATE_MACRO_KEYWORDS = ("rate", "fed", "yield", "금리", "연준", "환율")
SYMBOL_ALIASES = {
    "005930": ("삼성전자", "삼전", "samsung electronics"),
    "000660": ("sk하이닉스", "하이닉스", "sk hynix"),
    "009150": ("삼성전기", "samsung electro-mechanics"),
    "011070": ("lg이노텍", "lg innotek"),
    "036930": ("주성엔지니어링", "jusung"),
    "222800": ("심텍", "simmtech"),
    "353200": ("대덕전자", "daeduck"),
    "095610": ("테스", "tes"),
    "066570": ("lg전자", "lg electronics"),
    "000500": ("가온전선",),
    "005380": ("현대차", "hyundai motor"),
}


def main() -> int:
    args = _parse_args()
    start = _parse_date(args.start)
    end = _parse_date(args.end)
    input_dir = Path(args.input_dir)
    news_root = Path(args.news_root)
    output_root = Path(args.output_root)
    target_dir = output_root / "targets"
    target_dir.mkdir(parents=True, exist_ok=True)
    name_map = _load_krx_name_map()

    target_summaries: list[dict[str, Any]] = []
    for path in sorted(input_dir.glob("2026-*.json")):
        day = _parse_date(path.stem)
        if day < start or day > end:
            continue
        judgment = _read_json(path)
        news_evidence = _load_news_evidence(judgment, day=day, news_root=news_root)
        pseudo = _pseudo_target_from_judgment(judgment, name_map=name_map, news_evidence=news_evidence)
        out_path = target_dir / f"{day.isoformat()}.json"
        out_path.write_text(json.dumps(pseudo, ensure_ascii=False, indent=2), encoding="utf-8")
        target_summaries.append(
            {
                "date": day.isoformat(),
                "path": str(out_path.as_posix()),
                "target_count": len(pseudo.get("targets", [])),
                "gross_target": sum(float(item.get("target_percent", 0.0)) for item in pseudo.get("targets", [])),
                "market_news_score": pseudo.get("news_overlay", {}).get("market_news_score"),
                "clean_backtest_eligible": pseudo.get("clean_backtest_eligible"),
            }
        )

    runtime_config = _research_runtime_config(
        config_path=Path(args.config),
        output_root=output_root,
        target_template=(target_dir / "{date}.json").as_posix(),
        cash=float(args.cash),
        sleeve_id=str(args.sleeve_id),
        decision_time=str(args.decision_time),
    )
    runtime_path = output_root / "runtime_config.json"
    runtime_path.write_text(json.dumps(runtime_config, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "schema_version": "leaps.news_pseudo_portfolio_manifest.v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "model_id": "news_pseudo_v1",
        "description": (
            "Pseudo LEaps daily target set generated from daily judgments plus common news evidence. "
            "The base candidate set comes from point-in-time daily judgments; shared news changes gross exposure and relative weights."
        ),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "input_dir": str(input_dir.as_posix()),
        "input_news_dir": str(news_root.as_posix()),
        "target_dir": str(target_dir.as_posix()),
        "runtime_config": str(runtime_path.as_posix()),
        "cash": float(args.cash),
        "rules": {
            "gross": "0.92 + positive news tilt - negative news tilt, clipped to 0.72..0.98.",
            "weights": "Base target percent is reweighted by market/news score, symbol mentions, semiconductor news, confidence, and volatility penalty.",
            "caps": "Single-name cap is 24%; weights are renormalized to the daily news-adjusted gross target.",
            "lookahead": "Only common news evidence files timestamped before each decision_cutoff_at are used.",
        },
        "targets": target_summaries,
    }
    (output_root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"created": len(target_summaries), "manifest": str((output_root / "manifest.json").as_posix()), "runtime_config": str(runtime_path.as_posix())}, ensure_ascii=False))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build news-overlay pseudo LEaps target portfolios from daily judgments.")
    parser.add_argument("--input-dir", default="sleeves/LEaps/agent_state/daily_judgments")
    parser.add_argument("--news-root", default="data/research/news_evidence/krx")
    parser.add_argument("--output-root", default="sleeves/LEaps/agent_state/pseudo_portfolios/news_pseudo_v1")
    parser.add_argument("--config", default="configs/runtime/live_multi_sleeve.json")
    parser.add_argument("--sleeve-id", default="LEaps")
    parser.add_argument("--start", default="2026-04-22")
    parser.add_argument("--end", default="2026-05-22")
    parser.add_argument("--cash", type=float, default=8_429_010)
    parser.add_argument("--decision-time", default="08:50")
    return parser.parse_args()


def _pseudo_target_from_judgment(
    judgment: dict[str, Any],
    *,
    name_map: dict[str, str],
    news_evidence: dict[str, Any],
) -> dict[str, Any]:
    day = _parse_date(str(judgment["decision_date"]))
    target_portfolio = judgment.get("target_portfolio") or {}
    articles = list((news_evidence or {}).get("articles") or [])
    news_score = _market_news_score(articles)
    topics = tuple((news_evidence or {}).get("summary", {}).get("topic_tags") or ())
    gross = _gross_from_news(news_score, topics, articles)
    base_targets = list(target_portfolio.get("targets") or [])
    raw_scores: list[float] = []
    enriched: list[dict[str, Any]] = []
    for target in base_targets:
        raw_score, overlay = _target_raw_score(target, articles=articles, market_news_score=news_score, topics=topics)
        raw_scores.append(raw_score)
        copied = dict(target)
        ticker = str(copied.get("symbol") or "").split(":")[-1]
        if ticker in name_map:
            copied["name"] = name_map[ticker]
        copied["news_overlay"] = overlay
        enriched.append(copied)
    weights = _cap_and_normalize(raw_scores, gross=gross, cap=0.24)
    ordered = sorted(zip(enriched, weights), key=lambda item: item[1], reverse=True)
    targets = []
    for rank, (target, weight) in enumerate(ordered, start=1):
        if weight <= 0:
            continue
        overlay = dict(target.get("news_overlay") or {})
        base_reason = str(target.get("reason") or "")
        targets.append(
            {
                "symbol": target.get("symbol"),
                "name": target.get("name"),
                "target_percent": round(weight, 6),
                "confidence": round(_confidence(target, overlay, news_score), 4),
                "reason": f"news_pseudo_rank={rank}; {base_reason}; news_multiplier={overlay.get('multiplier', 1.0):.3f}",
                "features": target.get("features") or {},
                "news_overlay": overlay,
            }
        )
    clean_backtest_eligible = not str(judgment.get("recording_mode") or "").startswith("posthoc_")
    generated_at = f"{day.isoformat()}T08:50:00"
    expires_at = (day + timedelta(days=1)).isoformat() + "T08:50:00"
    return {
        "sleeve_id": "LEaps",
        "target_id": f"leaps-news-pseudo-v1-{day:%Y%m%d}",
        "generated_at": generated_at,
        "expires_at": expires_at,
        "max_gross_exposure": round(gross, 6),
        "flatten": False,
        "method": "news_pseudo_v1_from_daily_judgments",
        "pseudo_portfolio": True,
        "clean_backtest_eligible": clean_backtest_eligible,
        "source_judgment_id": judgment.get("judgment_id"),
        "source_recording_mode": judgment.get("recording_mode"),
        "lookahead_guard": "uses only common news evidence articles with pubDate/seendate <= source decision_cutoff_at",
        "source_news_evidence_path": _news_evidence_path(judgment, day),
        "news_overlay": {
            "market_news_score": round(news_score, 6),
            "topic_tags": list(topics),
            "article_count": len(articles),
            "gross_target_from_news": round(gross, 6),
        },
        "targets": targets,
    }


def _target_raw_score(target: dict[str, Any], *, articles: list[dict[str, Any]], market_news_score: float, topics: tuple[str, ...]) -> tuple[float, dict[str, Any]]:
    base_weight = max(0.0, float(target.get("target_percent") or 0.0))
    confidence = float(target.get("confidence") or 0.60)
    features = target.get("features") or {}
    ticker = str(target.get("symbol") or "").split(":")[-1]
    symbol_score = _symbol_news_score(ticker, str(target.get("name") or ""), articles)
    semiconductor_score = _semiconductor_score(articles)
    is_chip_related = _is_chip_related(target, ticker)
    sector_boost = 0.0
    if is_chip_related and "semiconductor" in topics:
        sector_boost += 0.10 * max(0.0, semiconductor_score)
    if is_chip_related and "ai" in topics:
        sector_boost += 0.03
    vol20 = _safe_float(features.get("vol20"), 0.0)
    vol_penalty = min(max((vol20 - 0.06) / 0.14, 0.0), 1.0) * 0.10
    multiplier = 1.0 + 0.12 * market_news_score + 0.20 * symbol_score + sector_boost - vol_penalty
    multiplier = max(0.45, min(1.45, multiplier))
    raw = max(0.0, base_weight * multiplier * (0.85 + 0.25 * confidence))
    overlay = {
        "market_news_score": round(market_news_score, 6),
        "symbol_news_score": round(symbol_score, 6),
        "semiconductor_news_score": round(semiconductor_score, 6),
        "sector_boost": round(sector_boost, 6),
        "volatility_penalty": round(vol_penalty, 6),
        "multiplier": round(multiplier, 6),
        "raw_score": round(raw, 8),
    }
    return raw, overlay


def _market_news_score(articles: Iterable[dict[str, Any]]) -> float:
    scores = [_title_score(str(article.get("title") or "")) for article in articles]
    if not scores:
        return 0.0
    return max(-1.0, min(1.0, sum(scores) / max(1, len(scores))))


def _semiconductor_score(articles: Iterable[dict[str, Any]]) -> float:
    relevant = []
    for article in articles:
        title = _normalize(str(article.get("title") or ""))
        if any(keyword in title for keyword in SEMICONDUCTOR_KEYWORDS):
            relevant.append(_title_score(title))
    if not relevant:
        return 0.0
    return max(-1.0, min(1.0, sum(relevant) / len(relevant)))


def _symbol_news_score(ticker: str, name: str, articles: Iterable[dict[str, Any]]) -> float:
    aliases = set(SYMBOL_ALIASES.get(ticker, ()))
    if name:
        aliases.add(name)
    aliases = {_normalize(alias) for alias in aliases if str(alias).strip()}
    if not aliases:
        return 0.0
    hits = []
    for article in articles:
        title = _normalize(str(article.get("title") or ""))
        if any(alias and alias in title for alias in aliases):
            hits.append(_title_score(title))
    if not hits:
        return 0.0
    return max(-1.0, min(1.0, sum(hits) / len(hits)))


def _title_score(title: str) -> float:
    text = _normalize(title)
    positive = sum(1 for keyword in POSITIVE_KEYWORDS if keyword in text)
    negative = sum(1 for keyword in NEGATIVE_KEYWORDS if keyword in text)
    if positive == 0 and negative == 0:
        return 0.0
    return max(-1.0, min(1.0, (positive - negative) / max(1, positive + negative)))


def _gross_from_news(score: float, topics: tuple[str, ...], articles: Iterable[dict[str, Any]]) -> float:
    gross = 0.92 + 0.06 * max(0.0, score) - 0.14 * max(0.0, -score)
    if "semiconductor" in topics and _semiconductor_score(articles) > 0:
        gross += 0.02
    if "rate_macro" in topics and _macro_score(articles) < 0:
        gross -= 0.03
    return max(0.72, min(0.98, gross))


def _macro_score(articles: Iterable[dict[str, Any]]) -> float:
    relevant = []
    for article in articles:
        title = _normalize(str(article.get("title") or ""))
        if any(keyword in title for keyword in RATE_MACRO_KEYWORDS):
            relevant.append(_title_score(title))
    if not relevant:
        return 0.0
    return sum(relevant) / len(relevant)


def _is_chip_related(target: dict[str, Any], ticker: str) -> bool:
    name = _normalize(str(target.get("name") or ""))
    sector = _normalize(str((target.get("features") or {}).get("sector") or ""))
    if sector == "technology":
        return True
    if ticker in {"005930", "000660", "009150", "011070", "036930", "222800", "353200", "095610", "066570"}:
        return True
    return any(keyword in name for keyword in ("전자", "전기", "이노텍", "심텍", "테스", "하이닉스", "반도체"))


def _cap_and_normalize(raw_scores: list[float], *, gross: float, cap: float) -> list[float]:
    if not raw_scores:
        return []
    total = sum(max(0.0, value) for value in raw_scores)
    if total <= 1e-12:
        raw_scores = [1.0 for _ in raw_scores]
        total = float(len(raw_scores))
    weights = [max(0.0, value) / total * gross for value in raw_scores]
    capped = [False for _ in weights]
    for _ in range(len(weights) + 2):
        changed = False
        for idx, weight in enumerate(weights):
            if weight > cap:
                weights[idx] = cap
                capped[idx] = True
                changed = True
        remaining = gross - sum(weights)
        if remaining <= 1e-9 or not changed:
            break
        free = [idx for idx, is_capped in enumerate(capped) if not is_capped]
        free_total = sum(max(0.0, raw_scores[idx]) for idx in free)
        if not free or free_total <= 1e-12:
            break
        for idx in free:
            weights[idx] += remaining * max(0.0, raw_scores[idx]) / free_total
    return [round(max(0.0, weight), 8) for weight in weights]


def _confidence(target: dict[str, Any], overlay: dict[str, Any], market_news_score: float) -> float:
    base = _safe_float(target.get("confidence"), 0.60)
    value = base + 0.05 * market_news_score + 0.06 * _safe_float(overlay.get("symbol_news_score"), 0.0)
    value -= 0.04 * (_safe_float(overlay.get("volatility_penalty"), 0.0) / 0.10 if overlay.get("volatility_penalty") else 0.0)
    return max(0.05, min(0.95, value))


def _research_runtime_config(
    *,
    config_path: Path,
    output_root: Path,
    target_template: str,
    cash: float,
    sleeve_id: str,
    decision_time: str,
) -> dict[str, Any]:
    payload = _read_json(config_path)
    payload["runtime_id"] = f"leaps_news_pseudo_v1_{output_root.name}"
    payload["mode"] = "backtest"
    payload["journal_path"] = (output_root / "cycle_journal.jsonl").as_posix()
    sleeves = payload.get("sleeves") or []
    sleeve = next((item for item in sleeves if str(item.get("sleeve_id")) == sleeve_id), None)
    if sleeve is None:
        raise SystemExit(f"Sleeve not found in config: {sleeve_id}")
    payload["sleeves"] = [sleeve]
    sleeve["cash"] = float(cash)
    sleeve["cash_by_currency"] = {"KRW": float(cash)}
    sleeve.setdefault("universe", {}).setdefault("active", {})["cadence"] = f"daily_at {decision_time} Asia/Seoul"
    portfolio = sleeve.setdefault("portfolio", {})
    parameters = dict(portfolio.get("parameters") or portfolio.get("params") or {})
    parameters.update(
        {
            "target_path": target_template,
            "max_gross_exposure": 0.98,
            "max_position_pct": 0.24,
            "max_target_age_hours": 72.0,
            "require_sleeve_id": True,
            "scale_to_max_gross": True,
            "emit_zero_for_missing_held_targets": True,
        }
    )
    portfolio["parameters"] = parameters
    portfolio.pop("params", None)
    rebalance = portfolio.setdefault("rebalance", {})
    rebalance["cadence"] = f"daily_at {decision_time} Asia/Seoul"
    rebalance["min_order_notional"] = min(float(rebalance.get("min_order_notional", 100_000.0)), 100_000.0)
    payload["broker_accounts"] = [
        {
            **account,
            "account_store_path": (output_root / f"{account.get('account_id', 'account')}_virtual_account.json").as_posix(),
            "order_store_path": (output_root / f"{account.get('account_id', 'account')}_orders.jsonl").as_posix(),
        }
        for account in payload.get("broker_accounts", [])
    ]
    return payload


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", str(value).strip().lower())


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_news_evidence(judgment: dict[str, Any], *, day: date, news_root: Path) -> dict[str, Any]:
    embedded = judgment.get("news_evidence")
    if isinstance(embedded, dict) and embedded:
        return embedded
    candidates: list[Path] = []
    ref = judgment.get("news_evidence_ref")
    if isinstance(ref, dict):
        ref_path = str(ref.get("path") or "").strip()
        if ref_path:
            candidates.append(Path(ref_path))
    candidates.append(news_root / f"{day.isoformat()}.json")
    for path in candidates:
        if not path.exists():
            continue
        payload = _read_json(path)
        if isinstance(payload, dict) and isinstance(payload.get("news_evidence"), dict):
            return payload["news_evidence"]
        if isinstance(payload, dict):
            return payload
    raise FileNotFoundError(f"No news evidence found for {day.isoformat()} in {news_root}")


def _news_evidence_path(judgment: dict[str, Any], day: date) -> str:
    ref = judgment.get("news_evidence_ref")
    if isinstance(ref, dict) and ref.get("path"):
        return str(ref["path"])
    return f"data/research/news_evidence/krx/{day.isoformat()}.json"


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


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(value).date()


if __name__ == "__main__":
    raise SystemExit(main())
