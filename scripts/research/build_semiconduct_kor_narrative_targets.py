from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import time as time_module
from typing import Any
from xml.etree import ElementTree
import urllib.parse
from zoneinfo import ZoneInfo

import pandas as pd


KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc
SLEEVE_ID = "semiconduct-kor"
EXPERIMENT_ID = "narrative_relay_v1"
NEWS_QUERIES = (
    "KOSPI semiconductor Samsung Electronics SK Hynix HBM",
    "Korea stocks chipmaker rally KOSPI semiconductor",
    "Korea cybersecurity stocks security authentication",
    "Korea nuclear fusion nuclear power stocks",
    "Korea shipbuilding defense power grid stocks",
)


@dataclass(frozen=True)
class SymbolProfile:
    symbol: str
    name: str
    bucket: str
    keywords: tuple[str, ...]
    base: float


PROFILES: tuple[SymbolProfile, ...] = (
    SymbolProfile("KRX:069500", "KODEX 200", "broad_market", ("코스피", "kospi", "증시", "외국인", "기관", "상승장"), 1.0),
    SymbolProfile("KRX:229200", "KODEX KOSDAQ150", "growth_beta", ("코스닥", "kosdaq", "성장주", "중소형"), 0.85),
    SymbolProfile("KRX:091160", "KODEX Semiconductor", "semiconductor", ("반도체", "삼성전자", "하이닉스", "hbm", "memory", "메모리", "ai칩"), 1.25),
    SymbolProfile("KRX:000660", "SK hynix", "semiconductor", ("하이닉스", "sk하이닉스", "hbm", "메모리", "dram"), 1.2),
    SymbolProfile("KRX:005930", "Samsung Electronics", "semiconductor", ("삼성전자", "삼전", "파운드리", "반도체", "hbm"), 1.1),
    SymbolProfile("KRX:091230", "TIGER Semiconductor", "semiconductor", ("반도체", "장비", "소부장", "hbm"), 1.0),
    SymbolProfile("KRX:009150", "Samsung Electro-Mechanics", "ai_hardware", ("삼성전기", "mlcc", "전자부품", "ai", "서버"), 0.95),
    SymbolProfile("KRX:036930", "Jusung Engineering", "semi_equipment", ("주성엔지니어링", "반도체 장비", "장비", "증착"), 0.9),
    SymbolProfile("KRX:466920", "SOL Shipbuilding TOP3 Plus", "shipbuilding", ("조선", "수주", "lng", "선박", "hd현대"), 1.0),
    SymbolProfile("KRX:329180", "HD Hyundai Heavy Industries", "shipbuilding", ("hd현대중공업", "조선", "수주", "방산"), 1.0),
    SymbolProfile("KRX:012450", "Hanwha Aerospace", "defense", ("한화에어로", "방산", "수출", "국방", "항공"), 1.0),
    SymbolProfile("KRX:034020", "Doosan Enerbility", "energy", ("두산에너빌리티", "원전", "전력", "에너지"), 0.85),
    SymbolProfile("KRX:091180", "KODEX Auto", "auto", ("자동차", "현대차", "기아", "전기차", "수출"), 0.9),
    SymbolProfile("KRX:005380", "Hyundai Motor", "auto", ("현대차", "자동차", "수출", "전기차"), 0.95),
    SymbolProfile("KRX:000270", "Kia", "auto", ("기아", "자동차", "수출", "전기차"), 0.9),
    SymbolProfile("KRX:091170", "KODEX Banks", "bank", ("은행", "금융지주", "금리", "배당", "밸류업"), 0.85),
    SymbolProfile("KRX:105560", "KB Financial", "bank", ("kb금융", "은행", "배당", "밸류업"), 0.85),
    SymbolProfile("KRX:055550", "Shinhan Financial", "bank", ("신한지주", "은행", "배당", "밸류업"), 0.8),
    SymbolProfile("KRX:305720", "KODEX Secondary Battery Industry", "battery", ("2차전지", "배터리", "전기차", "양극재"), 0.75),
    SymbolProfile("KRX:373220", "LG Energy Solution", "battery", ("lg에너지솔루션", "배터리", "전기차"), 0.75),
    SymbolProfile("KRX:006400", "Samsung SDI", "battery", ("삼성sdi", "배터리", "전기차"), 0.75),
    SymbolProfile("KRX:364970", "TIGER Bio TOP10", "bio", ("바이오", "제약", "임상", "헬스케어"), 0.7),
    SymbolProfile("KRX:035420", "NAVER", "internet_ai", ("네이버", "naver", "ai", "플랫폼", "웹툰"), 0.75),
    SymbolProfile("KRX:035720", "Kakao", "internet_ai", ("카카오", "플랫폼", "ai", "톡비즈"), 0.65),
)

# Keep the live candidate board explicit and ASCII-stable. The first PROFILES
# assignment above may preserve legacy Korean keyword text from older files, but
# this override is the source of truth for the agent target builder.
PROFILES = (
    SymbolProfile("KRX:069500", "KODEX 200", "broad_market", ("kospi", "market", "foreign", "institution", "rally"), 1.0),
    SymbolProfile("KRX:102110", "TIGER 200", "broad_market", ("kospi", "market", "large cap", "rally"), 0.95),
    SymbolProfile("KRX:229200", "KODEX KOSDAQ150", "growth_beta", ("kosdaq", "growth", "small cap", "risk on"), 0.85),
    SymbolProfile("KRX:091160", "KODEX Semiconductor", "semiconductor", ("semiconductor", "samsung", "hynix", "hbm", "memory", "ai chip"), 1.25),
    SymbolProfile("KRX:091230", "TIGER Semiconductor", "semiconductor", ("semiconductor", "equipment", "materials", "hbm"), 1.0),
    SymbolProfile("KRX:005930", "Samsung Electronics", "semiconductor", ("samsung electronics", "samsung", "foundry", "semiconductor", "hbm"), 1.1),
    SymbolProfile("KRX:000660", "SK hynix", "semiconductor", ("sk hynix", "hynix", "hbm", "memory", "dram"), 1.2),
    SymbolProfile("KRX:009150", "Samsung Electro-Mechanics", "ai_hardware", ("samsung electro", "mlcc", "component", "ai", "server"), 0.95),
    SymbolProfile("KRX:036930", "Jusung Engineering", "semi_equipment", ("jusung", "semiconductor equipment", "deposition", "equipment"), 0.9),
    SymbolProfile("KRX:011070", "LG Innotek", "ai_hardware", ("lg innotek", "camera module", "ai hardware", "apple", "component"), 0.9),
    SymbolProfile("KRX:039030", "EO Technics", "semi_equipment", ("eo technics", "laser", "semiconductor equipment", "hbm"), 0.85),
    SymbolProfile("KRX:240810", "Wonik IPS", "semi_equipment", ("wonik", "ips", "semiconductor equipment", "deposition"), 0.85),
    SymbolProfile("KRX:058470", "Leeno Industrial", "semi_equipment", ("leeno", "test socket", "probe", "semiconductor"), 0.82),
    SymbolProfile("KRX:095610", "TES", "semi_equipment", ("tes", "semiconductor equipment", "deposition"), 0.8),
    SymbolProfile("KRX:222800", "Simtech", "ai_hardware", ("simtech", "pcb", "package substrate", "memory"), 0.78),
    SymbolProfile("KRX:353200", "Daeduck Electronics", "ai_hardware", ("daeduck", "pcb", "substrate", "ai hardware"), 0.78),
    SymbolProfile("KRX:403870", "HPSP", "semi_equipment", ("hpsp", "semiconductor equipment", "anneal", "hbm"), 0.82),
    SymbolProfile("KRX:005290", "Dongjin Semichem", "semi_materials", ("dongjin", "photoresist", "semiconductor materials"), 0.78),
    SymbolProfile("KRX:095340", "ISC", "semi_equipment", ("isc", "test socket", "semiconductor", "hbm"), 0.78),
    SymbolProfile("KRX:466920", "SOL Shipbuilding TOP3 Plus", "shipbuilding", ("shipbuilding", "lng", "order", "export"), 1.0),
    SymbolProfile("KRX:329180", "HD Hyundai Heavy Industries", "shipbuilding", ("hd hyundai heavy", "shipbuilding", "order", "defense"), 1.0),
    SymbolProfile("KRX:009540", "HD Korea Shipbuilding", "shipbuilding", ("hd korea shipbuilding", "shipbuilding", "lng", "order"), 0.95),
    SymbolProfile("KRX:042660", "Hanwha Ocean", "shipbuilding", ("hanwha ocean", "shipbuilding", "lng", "defense"), 0.9),
    SymbolProfile("KRX:010140", "Samsung Heavy Industries", "shipbuilding", ("samsung heavy", "shipbuilding", "lng", "order"), 0.85),
    SymbolProfile("KRX:012450", "Hanwha Aerospace", "defense", ("hanwha aerospace", "defense", "export", "aerospace"), 1.0),
    SymbolProfile("KRX:047810", "Korea Aerospace", "defense", ("kai", "aerospace", "defense", "export"), 0.85),
    SymbolProfile("KRX:079550", "LIG Nex1", "defense", ("lig nex1", "defense", "missile", "export"), 0.85),
    SymbolProfile("KRX:034020", "Doosan Enerbility", "energy", ("doosan enerbility", "nuclear", "power", "energy"), 0.85),
    SymbolProfile("KRX:267260", "HD Hyundai Electric", "power_grid", ("hd hyundai electric", "transformer", "power grid", "electricity"), 0.95),
    SymbolProfile("KRX:010120", "LS ELECTRIC", "power_grid", ("ls electric", "power grid", "transformer", "electricity"), 0.9),
    SymbolProfile("KRX:001440", "Taihan Cable", "power_grid", ("taihan cable", "cable", "power grid", "electricity"), 0.82),
    SymbolProfile("KRX:052690", "KEPCO Engineering", "fusion_nuclear", ("kepco engineering", "nuclear", "fusion", "reactor", "power plant"), 0.82),
    SymbolProfile("KRX:083650", "BHI", "fusion_nuclear", ("bhi", "nuclear", "fusion", "boiler", "power plant"), 0.76),
    SymbolProfile("KRX:042370", "Vitzro Tech", "fusion_nuclear", ("vitzro tech", "fusion", "plasma", "accelerator", "power device"), 0.74),
    SymbolProfile("KRX:250060", "Mobis", "fusion_nuclear", ("mobis", "fusion", "plasma", "control system"), 0.72),
    SymbolProfile("KRX:068240", "Dawonsys", "fusion_nuclear", ("dawonsys", "fusion", "accelerator", "power supply"), 0.7),
    SymbolProfile("KRX:094820", "Iljin Power", "fusion_nuclear", ("iljin power", "nuclear", "fusion", "maintenance", "power"), 0.68),
    SymbolProfile("KRX:091180", "KODEX Auto", "auto", ("auto", "hyundai", "kia", "ev", "export"), 0.9),
    SymbolProfile("KRX:005380", "Hyundai Motor", "auto", ("hyundai motor", "auto", "export", "ev"), 0.95),
    SymbolProfile("KRX:000270", "Kia", "auto", ("kia", "auto", "export", "ev"), 0.9),
    SymbolProfile("KRX:012330", "Hyundai Mobis", "auto", ("hyundai mobis", "auto parts", "ev", "export"), 0.82),
    SymbolProfile("KRX:064350", "Hyundai Rotem", "defense", ("hyundai rotem", "defense", "rail", "export"), 0.82),
    SymbolProfile("KRX:091170", "KODEX Banks", "bank", ("bank", "financial", "rate", "dividend", "value up"), 0.85),
    SymbolProfile("KRX:105560", "KB Financial", "bank", ("kb financial", "bank", "dividend", "value up"), 0.85),
    SymbolProfile("KRX:055550", "Shinhan Financial", "bank", ("shinhan", "bank", "dividend", "value up"), 0.8),
    SymbolProfile("KRX:086790", "Hana Financial", "bank", ("hana financial", "bank", "dividend", "value up"), 0.8),
    SymbolProfile("KRX:316140", "Woori Financial", "bank", ("woori financial", "bank", "dividend", "value up"), 0.78),
    SymbolProfile("KRX:203650", "Dream Security", "cybersecurity", ("dream security", "cybersecurity", "security", "authentication", "encryption"), 0.78),
    SymbolProfile("KRX:042510", "RaonSecure", "cybersecurity", ("raonsecure", "cybersecurity", "security", "authentication", "mobile security"), 0.76),
    SymbolProfile("KRX:158430", "ATON", "cybersecurity", ("aton", "authentication", "fintech security", "mobile security"), 0.74),
    SymbolProfile("KRX:053800", "AhnLab", "cybersecurity", ("ahnlab", "cybersecurity", "security software", "malware"), 0.82),
    SymbolProfile("KRX:136540", "Wins", "cybersecurity", ("wins", "network security", "intrusion prevention", "cybersecurity"), 0.72),
    SymbolProfile("KRX:053300", "Korea Electronic Certification", "cybersecurity", ("electronic certification", "authentication", "certificate", "security"), 0.68),
    SymbolProfile("KRX:305720", "KODEX Secondary Battery Industry", "battery", ("battery", "secondary battery", "ev", "cathode"), 0.75),
    SymbolProfile("KRX:373220", "LG Energy Solution", "battery", ("lg energy solution", "battery", "ev"), 0.75),
    SymbolProfile("KRX:006400", "Samsung SDI", "battery", ("samsung sdi", "battery", "ev"), 0.75),
    SymbolProfile("KRX:051910", "LG Chem", "battery", ("lg chem", "battery", "chemical", "ev"), 0.72),
    SymbolProfile("KRX:247540", "Ecopro BM", "battery", ("ecopro bm", "battery", "cathode", "ev"), 0.72),
    SymbolProfile("KRX:086520", "Ecopro", "battery", ("ecopro", "battery", "materials", "ev"), 0.7),
    SymbolProfile("KRX:096770", "SK Innovation", "battery", ("sk innovation", "battery", "energy", "ev"), 0.7),
    SymbolProfile("KRX:364970", "TIGER Bio TOP10", "bio", ("bio", "pharma", "clinical", "healthcare"), 0.7),
    SymbolProfile("KRX:068270", "Celltrion", "bio", ("celltrion", "bio", "biosimilar", "healthcare"), 0.75),
    SymbolProfile("KRX:207940", "Samsung Biologics", "bio", ("samsung biologics", "bio", "cmo", "healthcare"), 0.75),
    SymbolProfile("KRX:128940", "Hanmi Pharm", "bio", ("hanmi pharm", "pharma", "bio", "clinical"), 0.68),
    SymbolProfile("KRX:035420", "NAVER", "internet_ai", ("naver", "ai", "platform", "webtoon", "cloud"), 0.75),
    SymbolProfile("KRX:035720", "Kakao", "internet_ai", ("kakao", "platform", "ai", "commerce"), 0.65),
)

RISK_KEYWORDS = (
    "급락",
    "하락",
    "폭락",
    "관세",
    "전쟁",
    "침체",
    "위기",
    "환율 급등",
    "금리 급등",
    "매도",
    "불확실",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-04-21")
    parser.add_argument("--end", default="2026-05-22")
    parser.add_argument("--decision-date", default="")
    parser.add_argument("--cash", type=float, default=5_000_000)
    parser.add_argument("--daily-bars-dir", default="data/research/market_data/daily_bars")
    parser.add_argument("--news-evidence-dir", default="data/research/news_evidence/krx")
    parser.add_argument("--output-dir", default=f"sleeves/{SLEEVE_ID}/agent_state/pseudo_portfolios/{EXPERIMENT_ID}/targets")
    parser.add_argument("--latest-target-path", default="")
    parser.add_argument("--current-state-path", default="")
    parser.add_argument("--daily-judgments-dir", default="")
    parser.add_argument("--strategy-doc-path", default="")
    parser.add_argument("--refresh-news", action="store_true")
    parser.add_argument("--max-news-articles", type=int, default=18)
    parser.add_argument("--request-timeout-seconds", type=float, default=12.0)
    parser.add_argument("--sleep-seconds", type=float, default=0.2)
    args = parser.parse_args()

    bars = _load_bars(Path(args.daily_bars_dir))
    if args.decision_date:
        trading_days = [_date(args.decision_date)]
    else:
        trading_days = _trading_days(bars, _date(args.start), _date(args.end))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest_entries = []
    for decision_day in trading_days:
        if args.refresh_news:
            _refresh_news_evidence(
                news_evidence_dir=Path(args.news_evidence_dir),
                decision_day=decision_day,
                max_news_articles=int(args.max_news_articles),
                request_timeout_seconds=float(args.request_timeout_seconds),
                sleep_seconds=float(args.sleep_seconds),
            )
        artifact = build_target_artifact(
            decision_day=decision_day,
            bars=bars,
            news_evidence_dir=Path(args.news_evidence_dir),
            cash=args.cash,
        )
        path = output_dir / f"{decision_day.isoformat()}.json"
        path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.latest_target_path:
            latest_path = Path(args.latest_target_path)
            latest_path.parent.mkdir(parents=True, exist_ok=True)
            latest_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.daily_judgments_dir or args.current_state_path:
            judgment = build_daily_judgment(
                target_artifact=artifact,
                target_path=Path(args.latest_target_path) if args.latest_target_path else path,
                generated_from_target_path=path,
            )
            if args.daily_judgments_dir:
                judgment_dir = Path(args.daily_judgments_dir)
                judgment_dir.mkdir(parents=True, exist_ok=True)
                judgment_path = judgment_dir / f"{decision_day.isoformat()}.json"
                judgment_path.write_text(json.dumps(judgment, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                _write_daily_judgment_manifest(judgment_dir, judgment_path, judgment, args)
            if args.current_state_path:
                _write_current_state(Path(args.current_state_path), artifact, judgment)
            if args.strategy_doc_path:
                _update_strategy_recent_judgment(Path(args.strategy_doc_path), artifact)
        manifest_entries.append(
            {
                "decision_date": decision_day.isoformat(),
                "target_path": str(path).replace("\\", "/"),
                "target_count": len(artifact["targets"]),
                "gross_target": round(sum(abs(float(item["target_percent"])) for item in artifact["targets"]), 6),
                "news_article_count": artifact["metadata"]["news_article_count"],
                "price_data_cutoff_date": artifact["metadata"]["price_data_cutoff_date"],
            }
        )

    manifest = {
        "schema_version": "leaps.pseudo_portfolio_manifest.v1",
        "sleeve_id": SLEEVE_ID,
        "experiment_id": EXPERIMENT_ID,
        "recording_mode": "point_in_time_reconstructed_preopen_news_and_prior_daily_bars",
        "decision_cutoff_time": "08:00 Asia/Seoul",
        "entries": manifest_entries,
    }
    manifest_path = output_dir.parent / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    latest_note = f" and latest target to {args.latest_target_path}" if args.latest_target_path else ""
    print(f"wrote {len(manifest_entries)} targets to {output_dir}{latest_note}")
    return 0


def build_daily_judgment(
    *,
    target_artifact: dict[str, Any],
    target_path: Path,
    generated_from_target_path: Path,
) -> dict[str, Any]:
    decision_date = str(target_artifact.get("decision_date") or "")
    gross = _gross_from_targets(target_artifact)
    metadata = target_artifact.get("metadata") or {}
    targets = list(target_artifact.get("targets") or [])
    top_names = [str(item.get("name") or item.get("symbol")) for item in targets[:3]]
    risk_score = float(metadata.get("risk_score") or 0.0)
    risk_terms = list(metadata.get("risk_terms") or [])
    news_count = int(metadata.get("news_article_count") or 0)
    risk_regime = _risk_regime(risk_score, risk_terms)
    return {
        "schema_version": "leaps.daily_judgment.v1",
        "sleeve_id": SLEEVE_ID,
        "strategy_name": "KR Rally Relay",
        "judgment_id": f"{SLEEVE_ID}-judgment-{decision_date}",
        "decision_date": decision_date,
        "decision_timezone": "Asia/Seoul",
        "decision_cutoff_at": target_artifact.get("decision_cutoff_at"),
        "recorded_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "recording_mode": target_artifact.get("recording_mode", "live_or_reconstructed_daily_target_artifact"),
        "backtest_eligibility": {
            "usable_for_engine_replay": True,
            "usable_as_true_premarket_agent_judgment": False,
            "reason": (
                "This file records the target artifact and supporting evidence. "
                "Historical rows remain reconstructed unless produced by the live 08:00 automation before market application."
            ),
        },
        "runtime_contract": {
            "operating_mode": "kr_rally_relay_agent_daily_target_v1",
            "live_target_artifact_path": "data/operator-targets/semiconduct-kor/latest_target.json",
            "selection_model": "sleeves/semiconduct-kor/selections/agent_narrative_target.py:AgentNarrativeTargetSelectionModel",
            "portfolio_model": "sleeves/semiconduct-kor/portfolios/agent_narrative_target.py:AgentNarrativeTargetPortfolioModel",
            "risk_model": "sleeves/semiconduct-kor/risks/basic_long_only.py",
            "execution_model": "sleeves/semiconduct-kor/executions/immediate.py",
            "alpha_modules_active": [],
        },
        "decision_summary": {
            "target_id": target_artifact.get("target_id"),
            "stance": _stance(targets, risk_regime),
            "gross_target": gross,
            "max_gross_exposure": target_artifact.get("max_gross_exposure"),
            "currency": (target_artifact.get("cash_assumption") or {}).get("currency", "KRW"),
            "thesis": _target_thesis(targets, gross, risk_regime),
            "risk_regime": risk_regime,
            "news_article_count": news_count,
            "cash_posture": _cash_posture(gross),
            "primary_risks": _primary_risks(metadata),
            "wait_state": _wait_state(gross, news_count, risk_regime),
        },
        "price_evidence": {
            "source": "data/research/market_data/daily_bars",
            "price_data_cutoff_date": metadata.get("price_data_cutoff_date"),
            "notes": "The target builder uses daily bars strictly before the decision date.",
        },
        "target_portfolio": {
            "source_artifact": str(target_path).replace("\\", "/"),
            "generated_from_target_path": str(generated_from_target_path).replace("\\", "/"),
            "target_id": target_artifact.get("target_id"),
            "generated_at": target_artifact.get("generated_at"),
            "expires_at": target_artifact.get("expires_at"),
            "max_gross_exposure": target_artifact.get("max_gross_exposure"),
            "gross_target": gross,
            "flatten": bool(target_artifact.get("flatten", False)),
            "targets": targets,
        },
        "news_evidence_ref": {
            "schema_version": "leaps.news_evidence.v1",
            "paths": list(target_artifact.get("source_news_evidence_paths") or []),
            "market": "KRX",
            "article_count": news_count,
            "score_notes": metadata.get("score_notes", {}),
        },
    }


def build_target_artifact(
    *,
    decision_day: date,
    bars: pd.DataFrame,
    news_evidence_dir: Path,
    cash: float,
) -> dict[str, Any]:
    cutoff = datetime.combine(decision_day, time(8, 0), tzinfo=KST)
    window_start = cutoff - timedelta(days=1)
    articles = _preopen_articles(news_evidence_dir, decision_day, window_start, cutoff)
    prior = bars[bars["date_value"] < decision_day].copy()
    price_cutoff = str(prior["date_value"].max()) if not prior.empty else ""
    scores, score_notes = _score_symbols(prior, articles)
    risk_score, risk_terms = _risk_score(prior, articles)
    gross_target = _gross_target(risk_score, bool(articles))
    selected = _select_targets(scores, gross_target)

    target_id = f"{SLEEVE_ID}-{EXPERIMENT_ID}-{decision_day.strftime('%Y%m%d')}"
    return {
        "schema_version": "leaps.agent_target.v1",
        "sleeve_id": SLEEVE_ID,
        "target_id": target_id,
        "generated_at": cutoff.isoformat(),
        "expires_at": (cutoff + timedelta(days=1)).isoformat(),
        "decision_date": decision_day.isoformat(),
        "decision_cutoff_at": cutoff.isoformat(),
        "recording_mode": "point_in_time_reconstructed_preopen_news_and_prior_daily_bars",
        "max_gross_exposure": gross_target,
        "flatten": False,
        "cash_assumption": {"amount": cash, "currency": "KRW"},
        "targets": [
            {
                "symbol": item["symbol"],
                "name": item["name"],
                "target_percent": item["target_percent"],
                "confidence": item["confidence"],
                "reason": item["reason"],
                "bucket": item["bucket"],
            }
            for item in selected
        ],
        "source_news_evidence_paths": _evidence_paths(news_evidence_dir, decision_day),
        "metadata": {
            "news_window_start_at": window_start.isoformat(),
            "decision_cutoff_at": cutoff.isoformat(),
            "news_article_count": len(articles),
            "price_data_cutoff_date": price_cutoff,
            "risk_score": round(risk_score, 4),
            "risk_terms": risk_terms,
            "score_notes": score_notes,
        },
    }


def _refresh_news_evidence(
    *,
    news_evidence_dir: Path,
    decision_day: date,
    max_news_articles: int,
    request_timeout_seconds: float,
    sleep_seconds: float,
) -> None:
    news_evidence_dir.mkdir(parents=True, exist_ok=True)
    cutoff = datetime.combine(decision_day, time(8, 0), tzinfo=KST)
    window_start = cutoff - timedelta(days=1)
    articles_by_url: dict[str, dict[str, Any]] = {}
    queries_used: list[str] = []
    existing_articles = _existing_news_articles(news_evidence_dir / f"{decision_day.isoformat()}.json")
    for article in existing_articles:
        url = str(article.get("url") or article.get("link") or "").strip()
        if url:
            articles_by_url[url] = article
    for query in NEWS_QUERIES:
        queries_used.append(query)
        for article in _query_google_news_rss(
            query=query,
            start=window_start,
            end=cutoff,
            max_records=max_news_articles,
            timeout_seconds=request_timeout_seconds,
        ):
            if article.get("error"):
                url = str(article.get("query_url") or f"provider-error:{query}")
                articles_by_url[url] = article
                continue
            normalized = _normalize_rss_article(article, window_start, cutoff)
            if not normalized:
                continue
            articles_by_url.setdefault(str(normalized["url"]), normalized)
        if sleep_seconds > 0:
            time_module.sleep(sleep_seconds)
    articles = sorted(
        articles_by_url.values(),
        key=lambda item: str(item.get("published_at_kst") or item.get("seen_at_utc") or ""),
        reverse=True,
    )[:max_news_articles]
    output_path = news_evidence_dir / f"{decision_day.isoformat()}.json"
    payload = {
        "schema_version": "leaps.news_evidence.v1",
        "evidence_id": f"krx-news-{decision_day:%Y%m%d}",
        "market": "KRX",
        "decision_date": decision_day.isoformat(),
        "decision_timezone": "Asia/Seoul",
        "decision_cutoff_at": cutoff.isoformat(timespec="seconds"),
        "recorded_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "recording_mode": "live_preopen_or_operator_refreshed_google_news_rss_context",
        "news_evidence": {
            "provider": "Google News RSS",
            "provider_url": "https://news.google.com/rss/search",
            "decision_news_cutoff_at": cutoff.isoformat(timespec="seconds"),
            "window_start_at": window_start.isoformat(timespec="seconds"),
            "queries": queries_used,
            "articles": articles,
            "summary": {
                "article_count": len([item for item in articles if not item.get("error")]),
                "topic_tags": _news_topic_tags(articles),
                "interpretation": "Used to give KR Rally Relay fresh pre-open context before target scoring.",
            },
        },
        "provenance": {
            "writer": "scripts/research/build_semiconduct_kor_narrative_targets.py",
            "purpose": "Sleeve-specific KR Rally Relay news context for daily target and judgment artifacts.",
        },
    }
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _existing_news_articles(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    articles = (payload.get("news_evidence") or {}).get("articles") or []
    return [dict(item) for item in articles if isinstance(item, dict)]


def _query_google_news_rss(
    *,
    query: str,
    start: datetime,
    end: datetime,
    max_records: int,
    timeout_seconds: float,
) -> list[dict[str, Any]]:
    before = (end.astimezone(UTC).date() + timedelta(days=1)).isoformat()
    after = (start.astimezone(UTC).date() - timedelta(days=1)).isoformat()
    rss_query = f"{query} after:{after} before:{before}"
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": rss_query, "hl": "ko", "gl": "KR", "ceid": "KR:ko"}
    )
    try:
        import requests  # type: ignore

        response = requests.get(
            url,
            timeout=max(1.0, timeout_seconds),
            headers={"User-Agent": "LEapsQuantEngine/1.0 semiconduct-kor-target-builder"},
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
                "published_at_utc": pub_date.isoformat(timespec="seconds") if pub_date else None,
                "domain": source.attrib.get("url") if source is not None else None,
                "source_name": source.text if source is not None else None,
                "query_url": url,
            }
        )
        if len(articles) >= max_records:
            break
    return articles


def _parse_rss_pubdate(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_rss_article(article: dict[str, Any], window_start: datetime, cutoff: datetime) -> dict[str, Any] | None:
    url = str(article.get("url") or "").strip()
    if not url:
        return None
    published = _parse_datetime_maybe(article.get("published_at_utc"))
    if published is None:
        return None
    published_kst = published.astimezone(KST)
    if not (window_start <= published_kst < cutoff):
        return None
    title = str(article.get("title") or "").strip()
    return {
        "title": title,
        "url": url,
        "domain": article.get("domain"),
        "source_name": article.get("source_name"),
        "published_at_kst": published_kst.isoformat(timespec="seconds"),
        "seen_at_utc": published.astimezone(UTC).isoformat(timespec="seconds"),
        "symbols": _symbols_from_title(title),
        "used_as": "semiconduct_kor_preopen_news_context",
    }


def _parse_datetime_maybe(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def _symbols_from_title(title: str) -> list[str]:
    lowered = title.lower()
    symbols = []
    for profile in PROFILES:
        ticker = profile.symbol.split(":", 1)[1]
        if ticker in lowered or any(keyword.lower() in lowered for keyword in profile.keywords):
            symbols.append(ticker)
    return sorted(set(symbols))


def _news_topic_tags(articles: list[dict[str, Any]]) -> list[str]:
    titles = " ".join(str(article.get("title") or "") for article in articles).lower()
    tags = []
    for label, keywords in {
        "semiconductor": ("semiconductor", "chip", "hbm", "memory", "samsung", "hynix"),
        "cybersecurity": ("cyber", "security", "authentication", "hacking"),
        "fusion_nuclear": ("fusion", "nuclear", "reactor", "power plant"),
        "shipbuilding_defense": ("shipbuilding", "defense", "aerospace"),
        "market_beta": ("kospi", "kosdaq", "rally", "foreign"),
        "risk": ("plunge", "selloff", "tariff", "war", "crisis", "rate"),
    }.items():
        if any(keyword in titles for keyword in keywords):
            tags.append(label)
    return tags


def _load_bars(root: Path) -> pd.DataFrame:
    frames = []
    for path in sorted(root.glob("krx_2026_*.parquet")):
        frames.append(pd.read_parquet(path))
    if not frames:
        raise FileNotFoundError(f"no krx parquet daily bars under {root}")
    bars = pd.concat(frames, ignore_index=True)
    bars["date_value"] = pd.to_datetime(bars["date"]).dt.date
    keep = {profile.symbol for profile in PROFILES}
    return bars[bars["symbol"].isin(keep)].sort_values(["symbol", "date_value"])


def _trading_days(bars: pd.DataFrame, start: date, end: date) -> list[date]:
    days = sorted(day for day in bars["date_value"].unique() if start <= day <= end)
    return [day for day in days if (bars["date_value"] == day).any()]


def _preopen_articles(news_dir: Path, decision_day: date, window_start: datetime, cutoff: datetime) -> list[dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    for day in (decision_day - timedelta(days=1), decision_day):
        path = news_dir / f"{day.isoformat()}.json"
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        for article in (payload.get("news_evidence") or {}).get("articles") or []:
            published = _article_time(article)
            if published is None or not (window_start <= published < cutoff):
                continue
            articles.append(article)
    return articles


def _article_time(article: dict[str, Any]) -> datetime | None:
    for key in ("published_at_kst", "seen_at_kst", "published_at", "seen_at_utc"):
        value = str(article.get(key) or "").strip()
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=KST)
        return parsed.astimezone(KST)
    return None


def _score_symbols(prior: pd.DataFrame, articles: list[dict[str, Any]]) -> tuple[dict[str, float], dict[str, Any]]:
    news_counts: Counter[str] = Counter()
    title_examples: dict[str, list[str]] = defaultdict(list)
    for article in articles:
        title = str(article.get("title") or "").lower()
        article_symbols = set(str(symbol).upper() for symbol in article.get("symbols") or [])
        for profile in PROFILES:
            ticker = profile.symbol.split(":", 1)[1]
            matched = ticker in article_symbols or any(keyword.lower() in title for keyword in profile.keywords)
            if not matched:
                continue
            news_counts[profile.symbol] += 1
            if len(title_examples[profile.symbol]) < 3:
                title_examples[profile.symbol].append(str(article.get("title") or "")[:120])

    scores: dict[str, float] = {}
    for profile in PROFILES:
        sub = prior[prior["symbol"] == profile.symbol]
        momentum = _momentum_score(sub)
        liquidity = _liquidity_score(sub)
        news = min(1.5, news_counts[profile.symbol] * 0.18)
        scores[profile.symbol] = profile.base + momentum + liquidity + news
    notes = {
        symbol: {
            "news_hits": int(news_counts[symbol]),
            "examples": examples,
            "score": round(scores[symbol], 4),
        }
        for symbol, examples in title_examples.items()
    }
    return scores, notes


def _momentum_score(sub: pd.DataFrame) -> float:
    if len(sub) < 6:
        return 0.0
    closes = sub["close"].astype(float).to_list()
    ret_5 = closes[-1] / closes[-6] - 1.0
    ret_20 = closes[-1] / closes[-21] - 1.0 if len(closes) >= 21 else 0.0
    return max(-0.35, min(0.55, ret_5 * 3.0 + ret_20 * 1.2))


def _liquidity_score(sub: pd.DataFrame) -> float:
    if sub.empty:
        return -0.25
    rank = float(sub.iloc[-1].get("liquidity_rank") or 500)
    return max(0.0, (120.0 - min(rank, 120.0)) / 120.0) * 0.25


def _risk_score(prior: pd.DataFrame, articles: list[dict[str, Any]]) -> tuple[float, list[str]]:
    terms: Counter[str] = Counter()
    for article in articles:
        title = str(article.get("title") or "").lower()
        for keyword in RISK_KEYWORDS:
            if keyword.lower() in title:
                terms[keyword] += 1
    risk = min(4.0, sum(terms.values()) * 0.25)
    kospi = prior[prior["symbol"] == "KRX:069500"]
    if len(kospi) >= 4:
        closes = kospi["close"].astype(float).to_list()
        ret_3 = closes[-1] / closes[-4] - 1.0
        if ret_3 < -0.02:
            risk += min(2.0, abs(ret_3) * 25.0)
            terms["kospi_3d_down"] += 1
    return risk, [f"{term}:{count}" for term, count in terms.most_common(8)]


def _gross_target(risk_score: float, has_news: bool) -> float:
    # Normal mornings should put roughly 95% of sleeve cash to work. Missing
    # news alone is not a defensive signal; exposure only steps down when the
    # pre-open risk score is explicit enough to matter.
    base = 0.95
    risk_drag = max(0.0, risk_score - 1.0) * 0.035
    return round(max(0.85, min(0.95, base - min(0.10, risk_drag))), 4)


def _gross_from_targets(target_artifact: dict[str, Any]) -> float:
    return round(
        sum(abs(float(item.get("target_percent", 0.0))) for item in target_artifact.get("targets", [])),
        6,
    )


def _risk_regime(risk_score: float, risk_terms: list[Any]) -> str:
    if risk_score >= 2.0:
        return "risk_off"
    if risk_score >= 1.0 or risk_terms:
        return "watchful_risk_on"
    return "risk_on"


def _cash_posture(gross: float) -> str:
    if gross >= 0.93:
        return "cash_working_with_5pct_risk_buffer"
    if gross >= 0.85:
        return "partially_deployed_due_to_risk_regime"
    return "defensive_cash_retention"


def _stance(targets: list[dict[str, Any]], risk_regime: str) -> str:
    buckets = [str(item.get("bucket") or "") for item in targets]
    if risk_regime == "risk_off":
        return "Risk-reduced KR rally relay target."
    if "semiconductor" in buckets and "broad_market" in buckets:
        return "Semiconductor plus broad-market rally relay target."
    if buckets:
        return f"{buckets[0]} rally relay target."
    return "No active target selected."


def _target_thesis(targets: list[dict[str, Any]], gross: float, risk_regime: str) -> str:
    top_names = [str(item.get("name") or item.get("symbol")) for item in targets[:3]]
    names = ", ".join(top_names) if top_names else "no active names"
    return (
        f"{risk_regime} regime with gross {gross:.2%}; selected leaders are {names}. "
        "The sleeve rotates capital to the strongest KRX rally themes while leaving execution and order sizing to the engine."
    )


def _primary_risks(metadata: dict[str, Any]) -> list[str]:
    risks: list[str] = []
    risk_terms = list(metadata.get("risk_terms") or [])
    if risk_terms:
        risks.append("Explicit risk terms were detected: " + ", ".join(str(term) for term in risk_terms[:5]))
    news_count = int(metadata.get("news_article_count") or 0)
    if news_count <= 1:
        risks.append("News evidence coverage is thin; price momentum and base theme priors carry more of the decision.")
    price_cutoff = str(metadata.get("price_data_cutoff_date") or "")
    if not price_cutoff:
        risks.append("No local daily price cutoff was available for the selected universe.")
    if not risks:
        risks.append("Crowded rally themes can reverse quickly after a strong pre-open setup.")
    return risks


def _wait_state(gross: float, news_count: int, risk_regime: str) -> str:
    if risk_regime == "risk_off":
        return "Wait for risk regime to improve before restoring normal 95% exposure."
    if gross >= 0.93:
        return "No strategic cash wait; residual cash should mostly come from risk buffer, rounding, and fills."
    if news_count == 0:
        return "Reduced confidence until fresh news or market context improves."
    return "Partial exposure while explicit risk evidence remains active."


def _write_daily_judgment_manifest(
    judgment_dir: Path,
    judgment_path: Path,
    judgment: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    existing = _read_json_file(judgment_dir / "manifest.json", default={})
    records = {
        str(item.get("decision_date")): item
        for item in existing.get("judgments", [])
        if isinstance(item, dict) and item.get("decision_date")
    }
    decision_date = str(judgment.get("decision_date"))
    records[decision_date] = {
        "decision_date": decision_date,
        "output_path": str(judgment_path).replace("\\", "/"),
        "target_id": judgment.get("decision_summary", {}).get("target_id"),
        "gross_target": judgment.get("decision_summary", {}).get("gross_target"),
        "risk_regime": judgment.get("decision_summary", {}).get("risk_regime"),
        "recording_mode": judgment.get("recording_mode"),
        "updated_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
    }
    manifest = {
        "schema_version": "leaps.daily_judgment_manifest.v1",
        "sleeve_id": SLEEVE_ID,
        "strategy_name": "KR Rally Relay",
        "updated_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "output_dir": str(judgment_dir).replace("\\", "/"),
        "source_targets": {
            "pseudo_target_dir": str(Path(args.output_dir)).replace("\\", "/"),
            "live_target_path": str(Path(args.latest_target_path)).replace("\\", "/") if args.latest_target_path else "",
        },
        "judgments": [records[key] for key in sorted(records)],
    }
    (judgment_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_current_state(path: Path, target_artifact: dict[str, Any], judgment: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = _read_json_file(path, default={})
    targets = list(target_artifact.get("targets") or [])
    decision_summary = judgment.get("decision_summary") or {}
    decision_log = [
        item for item in previous.get("decision_log", []) if isinstance(item, dict)
    ][-19:]
    decision_log.append(
        {
            "decision_id": f"{SLEEVE_ID}-{target_artifact.get('decision_date')}-{target_artifact.get('target_id')}",
            "decided_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
            "decision": "Updated KR Rally Relay daily target, daily judgment, and current state.",
            "rationale": [
                str(decision_summary.get("thesis") or ""),
                str(decision_summary.get("cash_posture") or ""),
                str(decision_summary.get("wait_state") or ""),
            ],
            "evidence": [
                f"target_id={target_artifact.get('target_id')}",
                f"risk_regime={decision_summary.get('risk_regime')}",
                f"news_article_count={decision_summary.get('news_article_count')}",
                f"price_data_cutoff_date={(target_artifact.get('metadata') or {}).get('price_data_cutoff_date')}",
            ],
        }
    )
    state = {
        "schema_version": "leaps.agent_state.v1",
        "sleeve_id": SLEEVE_ID,
        "strategy_name": "KR Rally Relay",
        "state_owner": "Codex semiconduct-kor sleeve agent",
        "updated_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
        "operating_mode": {
            "mode_id": "kr_rally_relay_agent_daily_target_v1",
            "description": "Alpha-less daily agent target artifact mode. The agent writes one KRX rally target portfolio per trading day; portfolio construction reads percentages from the target artifact.",
            "target_artifact_path": "data/operator-targets/semiconduct-kor/latest_target.json",
            "selection_model": "sleeves/semiconduct-kor/selections/agent_narrative_target.py:AgentNarrativeTargetSelectionModel",
            "portfolio_model": "sleeves/semiconduct-kor/portfolios/agent_narrative_target.py:AgentNarrativeTargetPortfolioModel",
            "risk_model": "sleeves/semiconduct-kor/risks/basic_long_only.py",
            "execution_model": "sleeves/semiconduct-kor/executions/immediate.py",
            "universe_refresh_cadence": "daily_at 08:00 Asia/Seoul",
            "portfolio_refresh_cadence": "daily_at 09:05 Asia/Seoul",
            "alpha_modules_active": [],
        },
        "operator_intent": {
            "primary_goal": "Keep sleeve cash working in KRX rally themes while using agent evidence to avoid obvious risk-off mornings.",
            "portfolio_goal": "Produce percent-only target portfolios; execution owns quantities, rounding, tickets, and fills.",
            "constraints": [
                "Do not treat agent_state as a live trading input.",
                "Do not create orders or broker tickets from this file.",
                "Update current_state, daily_judgments, latest_target, and STRATEGY rationale together when the target changes.",
            ],
        },
        "runtime_status": {
            "config_path": "configs/runtime/live_multi_sleeve.json",
            "sleeve_live_status": "active",
            "live_target_artifact_path": "data/operator-targets/semiconduct-kor/latest_target.json",
            "last_target_id": target_artifact.get("target_id"),
            "last_generated_at": target_artifact.get("generated_at"),
            "last_expires_at": target_artifact.get("expires_at"),
        },
        "current_target_portfolio": {
            "status": "prepared_daily_agent_target",
            "target_artifact_path": "data/operator-targets/semiconduct-kor/latest_target.json",
            "artifact_exists": True,
            "artifact_id": target_artifact.get("target_id"),
            "generated_at": target_artifact.get("generated_at"),
            "expires_at": target_artifact.get("expires_at"),
            "gross_target": decision_summary.get("gross_target"),
            "max_gross_exposure": target_artifact.get("max_gross_exposure"),
            "currency": (target_artifact.get("cash_assumption") or {}).get("currency", "KRW"),
            "risk_regime": decision_summary.get("risk_regime"),
            "cash_posture": decision_summary.get("cash_posture"),
            "wait_state": decision_summary.get("wait_state"),
            "targets": targets,
            "reason": decision_summary.get("thesis"),
            "live_apply": {
                "applied": False,
                "why_not": "This script writes target artifacts only; live loop, risk, execution, and order runtime apply them later.",
            },
        },
        "latest_daily_judgment": {
            "path": f"sleeves/semiconduct-kor/agent_state/daily_judgments/{target_artifact.get('decision_date')}.json",
            "judgment_id": judgment.get("judgment_id"),
            "recording_mode": judgment.get("recording_mode"),
            "risk_regime": decision_summary.get("risk_regime"),
        },
        "validation_status": {
            "last_builder_status": "ok",
            "last_builder_checked_at": datetime.now(tz=KST).isoformat(timespec="seconds"),
            "pending_runtime_validation": True,
            "notes": "Run runtime-config-validate or preflight separately after material config changes. Target-only refresh does not submit orders.",
        },
        "decision_log": decision_log,
    }
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _update_strategy_recent_judgment(path: Path, target_artifact: dict[str, Any]) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    metadata = target_artifact.get("metadata") or {}
    targets = list(target_artifact.get("targets") or [])
    names = ", ".join(f"`{item.get('name') or item.get('symbol')}`" for item in targets)
    gross = _gross_from_targets(target_artifact)
    risk_score = float(metadata.get("risk_score") or 0.0)
    risk_regime = _risk_regime(risk_score, list(metadata.get("risk_terms") or []))
    rationale = (
        f"최신 target artifact 기준 판단일은 {target_artifact.get('decision_date')}이며, "
        f"{risk_regime} 판단 아래 {names}로 {gross:.0%} gross target을 구성했다. "
        f"artifact상 risk_score는 {risk_score:.1f}이고 뉴스 근거 수는 {int(metadata.get('news_article_count') or 0)}건이다. "
        f"gross가 95%에 가까우면 현금 잔류는 전략적 대기라기보다 5% 리스크 버퍼와 실행단 반올림/체결 상태로 해석한다. "
        "다음 08:00 자동화가 새 뉴스, 웹 정보, 공개 주가 맥락을 반영해 target을 바꾸면 이 섹션도 함께 갱신해야 한다."
    )
    updated = _replace_markdown_section(text, "Recent Judgment Rationale", rationale)
    path.write_text(updated, encoding="utf-8")


def _replace_markdown_section(text: str, heading: str, body: str) -> str:
    marker = f"## {heading}"
    start = text.find(marker)
    if start < 0:
        return text.rstrip() + f"\n\n{marker}\n\n{body}\n"
    body_start = start + len(marker)
    next_start = text.find("\n## ", body_start)
    replacement = f"{marker}\n\n{body}\n"
    if next_start < 0:
        return text[:start].rstrip() + "\n\n" + replacement
    return text[:start].rstrip() + "\n\n" + replacement + text[next_start:]


def _read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _select_targets(scores: dict[str, float], gross_target: float) -> list[dict[str, Any]]:
    profile_by_symbol = {profile.symbol: profile for profile in PROFILES}
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    selected: list[tuple[str, float]] = []
    buckets: set[str] = set()
    for symbol, score in ranked:
        profile = profile_by_symbol[symbol]
        if profile.bucket in buckets and profile.bucket not in {"semiconductor", "broad_market"}:
            continue
        selected.append((symbol, max(0.1, score)))
        buckets.add(profile.bucket)
        if len(selected) >= 5:
            break
    if "KRX:069500" not in {symbol for symbol, _ in selected}:
        selected.append(("KRX:069500", max(0.8, scores.get("KRX:069500", 0.8))))

    selected = selected[:6]
    total_score = sum(score for _, score in selected)
    weights = [(symbol, score / total_score * gross_target) for symbol, score in selected]
    weights = _cap_and_redistribute(weights, cap=0.30, gross_target=gross_target)
    result = []
    for symbol, weight in weights:
        profile = profile_by_symbol[symbol]
        result.append(
            {
                "symbol": symbol,
                "name": profile.name,
                "bucket": profile.bucket,
                "target_percent": round(weight, 6),
                "confidence": round(max(0.45, min(0.88, 0.50 + scores[symbol] * 0.08)), 4),
                "reason": f"preopen_news_prior_bars:{profile.bucket}:score={scores[symbol]:.3f}",
            }
        )
    return result


def _cap_and_redistribute(weights: list[tuple[str, float]], *, cap: float, gross_target: float) -> list[tuple[str, float]]:
    capped = {symbol: min(weight, cap) for symbol, weight in weights}
    for _ in range(8):
        spare = gross_target - sum(capped.values())
        if spare <= 1e-9:
            break
        uncapped = [(symbol, weight) for symbol, weight in weights if capped[symbol] < cap - 1e-9]
        if not uncapped:
            break
        base = sum(weight for _, weight in uncapped)
        for symbol, weight in uncapped:
            capped[symbol] = min(cap, capped[symbol] + spare * weight / base)
    return [(symbol, capped[symbol]) for symbol, _ in weights if capped[symbol] > 0.005]


def _evidence_paths(news_dir: Path, decision_day: date) -> list[str]:
    return [
        str(news_dir / f"{day.isoformat()}.json").replace("\\", "/")
        for day in (decision_day - timedelta(days=1), decision_day)
        if (news_dir / f"{day.isoformat()}.json").exists()
    ]


def _date(value: str) -> date:
    return datetime.fromisoformat(value).date()


if __name__ == "__main__":
    raise SystemExit(main())
