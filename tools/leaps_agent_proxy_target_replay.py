from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from leaps_quant_engine.models import Bar, Symbol
from leaps_quant_engine.universe.loader import load_universe_definition


DEFAULT_WEIGHTS = (0.24, 0.24, 0.16, 0.12, 0.10, 0.09)


@dataclass(frozen=True, slots=True)
class CandidateScore:
    symbol: Symbol
    name: str
    sector: str
    score: float
    confidence: float
    ret_5d: float
    ret_20d: float
    ret_60d: float
    ma20_gap: float
    ma60_gap: float
    near_high20: float
    vol20: float
    adv20: float


def main() -> int:
    args = _parse_args()
    start = _parse_date(args.start)
    end = _parse_date(args.end)
    if end < start:
        raise SystemExit("--end must be greater than or equal to --start")
    output_root = Path(args.output_root or f"data/research/leaps-agent-proxy-{start:%Y%m%d}-{end:%Y%m%d}")
    target_dir = output_root / "targets"
    target_dir.mkdir(parents=True, exist_ok=True)

    universe = load_universe_definition(args.universe)
    symbols = _eligible_symbols(universe.symbols, universe.symbol_properties, include_etfs=args.include_etfs)
    history_by_key = _load_history(
        symbols,
        cache_root=Path(args.daily_cache_root),
        warmup_start=start - timedelta(days=int(args.warmup_calendar_days)),
        end=end,
    )
    weights = _rank_weights(
        top_k=int(args.top_k),
        gross=float(args.gross),
        max_position_pct=float(args.max_position_pct),
        explicit=_parse_weights(args.weights),
    )

    target_summaries: list[dict[str, Any]] = []
    for day in _weekdays(start, end):
        scores = _score_day(
            day,
            symbols=symbols,
            universe_properties=universe.symbol_properties,
            history_by_key=history_by_key,
            min_adv20=float(args.min_adv20),
        )
        selected = _select_ranked(scores, top_k=int(args.top_k), max_per_sector=int(args.max_per_sector))
        target_path = target_dir / f"{day.isoformat()}.json"
        payload = _target_payload(
            day,
            selected=selected,
            weights=weights,
            sleeve_id=str(args.sleeve_id),
            gross=float(args.gross),
            decision_time=args.decision_time,
        )
        target_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        target_summaries.append(
            {
                "date": day.isoformat(),
                "target_path": str(target_path.as_posix()),
                "target_count": len(payload["targets"]),
                "gross_target": sum(float(item["target_percent"]) for item in payload["targets"]),
                "symbols": [item["symbol"] for item in payload["targets"]],
            }
        )

    runtime_config_path = output_root / "runtime_config.json"
    runtime_payload = _research_runtime_config(
        config_path=Path(args.config),
        output_root=output_root,
        sleeve_id=str(args.sleeve_id),
        cash=float(args.cash),
        target_template=(target_dir / "{date}.json").as_posix(),
        gross=float(args.gross),
        max_position_pct=float(args.max_position_pct),
        decision_time=args.decision_time,
    )
    runtime_config_path.write_text(json.dumps(runtime_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    manifest = {
        "schema_version": "leaps_agent_proxy_replay.v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "description": (
            "Point-in-time proxy for the agent-authored LEaps daily target. "
            "Each target uses cached FinanceDataReader daily bars strictly before the target date."
        ),
        "sleeve_id": str(args.sleeve_id),
        "universe": str(Path(args.universe).as_posix()),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "target_dir": str(target_dir.as_posix()),
        "runtime_config": str(runtime_config_path.as_posix()),
        "daily_cache_root": str(Path(args.daily_cache_root).as_posix()),
        "eligible_symbol_count": len(symbols),
        "history_symbol_count": len(history_by_key),
        "missing_history_symbol_count": len(symbols) - len(history_by_key),
        "target_days": target_summaries,
        "runtime_backtest_minute_command": _minute_backtest_command(
            runtime_config_path,
            start=start,
            end=end,
            cash=float(args.cash),
            sleeve_id=str(args.sleeve_id),
            output_root=output_root,
        ),
    }
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "status": "ok",
        "runtime_config": str(runtime_config_path.as_posix()),
        "manifest": str(manifest_path.as_posix()),
        "target_count": len(target_summaries),
        "history_symbol_count": len(history_by_key),
        "missing_history_symbol_count": len(symbols) - len(history_by_key),
        "first_day": target_summaries[0] if target_summaries else None,
        "last_day": target_summaries[-1] if target_summaries else None,
        "runtime_backtest_minute_command": manifest["runtime_backtest_minute_command"],
    }, ensure_ascii=False, indent=2))
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build point-in-time LEaps agent proxy target artifacts for minute replay."
    )
    parser.add_argument("--config", default="configs/runtime/live_multi_sleeve.json")
    parser.add_argument("--universe", default="configs/universes/leaps_kr_research_200.json")
    parser.add_argument("--sleeve-id", default="LEaps")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output-root")
    parser.add_argument("--cash", type=float, default=8_429_010.0)
    parser.add_argument("--gross", type=float, default=0.95)
    parser.add_argument("--max-position-pct", type=float, default=0.24)
    parser.add_argument("--top-k", type=int, default=6)
    parser.add_argument("--weights", default=",".join(str(value) for value in DEFAULT_WEIGHTS))
    parser.add_argument("--max-per-sector", type=int, default=3)
    parser.add_argument("--min-adv20", type=float, default=300_000_000.0)
    parser.add_argument("--daily-cache-root", default="data/runtime/cache/finance-datareader/daily")
    parser.add_argument("--warmup-calendar-days", type=int, default=220)
    parser.add_argument("--decision-time", default="08:50")
    parser.add_argument("--include-etfs", action="store_true")
    return parser.parse_args()


def _eligible_symbols(
    symbols: Iterable[Symbol],
    symbol_properties: Mapping[str, Mapping[str, Any]],
    *,
    include_etfs: bool,
) -> tuple[Symbol, ...]:
    result: list[Symbol] = []
    for symbol in symbols:
        props = symbol_properties.get(symbol.key, {})
        asset_type = str(props.get("asset_type") or "").strip().lower()
        if not include_etfs and asset_type == "etf":
            continue
        result.append(symbol)
    return tuple(result)


def _load_history(
    symbols: tuple[Symbol, ...],
    *,
    cache_root: Path,
    warmup_start: date,
    end: date,
) -> dict[str, tuple[Bar, ...]]:
    history: dict[str, tuple[Bar, ...]] = {}
    for symbol in symbols:
        bars = _load_cached_symbol_history(cache_root, symbol, warmup_start=warmup_start, end=end)
        filtered = tuple(
            bar for bar in bars
            if warmup_start <= bar.time.date() <= end and bar.close > 0
        )
        if len(filtered) >= 70:
            history[symbol.key] = filtered
    return history


def _load_cached_symbol_history(
    cache_root: Path,
    symbol: Symbol,
    *,
    warmup_start: date,
    end: date,
) -> tuple[Bar, ...]:
    symbol_dir = cache_root / symbol.market.upper() / symbol.ticker.upper()
    if not symbol_dir.exists():
        return ()
    by_day: dict[date, Bar] = {}
    for path in _candidate_cache_files(symbol_dir, warmup_start=warmup_start, end=end):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("schema_version") != "finance_datareader_daily_history.v1":
            continue
        for row in payload.get("bars") or ():
            if not isinstance(row, Mapping):
                continue
            try:
                when = datetime.fromisoformat(str(row["time"]))
                bar = Bar(
                    symbol=symbol,
                    time=when,
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row.get("volume") or 0)),
                )
            except (KeyError, TypeError, ValueError):
                continue
            by_day[when.date()] = bar
    return tuple(by_day[day] for day in sorted(by_day))


def _candidate_cache_files(symbol_dir: Path, *, warmup_start: date, end: date) -> tuple[Path, ...]:
    parsed: list[tuple[Path, date, date]] = []
    for path in sorted(symbol_dir.glob("*.json")):
        try:
            start_text, end_text = path.stem.split("_", 1)
            cache_start = datetime.strptime(start_text, "%Y%m%d").date()
            cache_end = datetime.strptime(end_text, "%Y%m%d").date()
        except ValueError:
            continue
        parsed.append((path, cache_start, cache_end))
    covering = [
        (path, cache_start, cache_end)
        for path, cache_start, cache_end in parsed
        if cache_start <= warmup_start and cache_end >= end
    ]
    if covering:
        covering.sort(key=lambda item: ((item[2] - item[1]).days, item[2]), reverse=True)
        return (covering[0][0],)
    overlapping = [
        (path, cache_start, cache_end)
        for path, cache_start, cache_end in parsed
        if cache_start <= end and cache_end >= warmup_start
    ]
    overlapping.sort(key=lambda item: ((item[2] - item[1]).days, item[2]), reverse=True)
    return tuple(path for path, _, _ in overlapping[:2])


def _score_day(
    day: date,
    *,
    symbols: tuple[Symbol, ...],
    universe_properties: Mapping[str, Mapping[str, Any]],
    history_by_key: Mapping[str, tuple[Bar, ...]],
    min_adv20: float,
) -> tuple[CandidateScore, ...]:
    raw: list[dict[str, Any]] = []
    for symbol in symbols:
        prior = [bar for bar in history_by_key.get(symbol.key, ()) if bar.time.date() < day]
        if len(prior) < 61:
            continue
        closes = [bar.close for bar in prior]
        highs = [bar.high for bar in prior]
        volumes = [bar.volume for bar in prior]
        if min(closes[-61:]) <= 0:
            continue
        adv20 = _mean([close * volume for close, volume in zip(closes[-20:], volumes[-20:])])
        if adv20 < min_adv20:
            continue
        ret_5d = closes[-1] / closes[-6] - 1.0
        ret_20d = closes[-1] / closes[-21] - 1.0
        ret_60d = closes[-1] / closes[-61] - 1.0
        ma20 = _mean(closes[-20:])
        ma60 = _mean(closes[-60:])
        ma20_gap = closes[-1] / ma20 - 1.0
        ma60_gap = closes[-1] / ma60 - 1.0
        daily_returns = [closes[index] / closes[index - 1] - 1.0 for index in range(len(closes) - 19, len(closes))]
        vol20 = _std(daily_returns)
        near_high20 = closes[-1] / max(highs[-20:]) - 1.0
        if ret_5d <= 0 or ret_20d <= 0 or ma20_gap <= -0.02:
            continue
        raw.append(
            {
                "symbol": symbol,
                "name": str(universe_properties.get(symbol.key, {}).get("name") or symbol.ticker),
                "sector": str(universe_properties.get(symbol.key, {}).get("sector") or "unknown"),
                "ret_5d": ret_5d,
                "ret_20d": ret_20d,
                "ret_60d": ret_60d,
                "ma20_gap": ma20_gap,
                "ma60_gap": ma60_gap,
                "near_high20": near_high20,
                "vol20": vol20,
                "adv20": adv20,
            }
        )
    if not raw:
        return ()
    z = {name: _z_map(raw, name) for name in ("ret_5d", "ret_20d", "ret_60d", "ma20_gap", "ma60_gap", "near_high20", "vol20", "adv20")}
    scored: list[CandidateScore] = []
    for row in raw:
        key = row["symbol"].key
        score = (
            0.28 * z["ret_20d"][key]
            + 0.22 * z["ret_5d"][key]
            + 0.16 * z["ret_60d"][key]
            + 0.14 * z["ma20_gap"][key]
            + 0.08 * z["ma60_gap"][key]
            + 0.10 * z["near_high20"][key]
            + 0.08 * z["adv20"][key]
            - 0.18 * z["vol20"][key]
        )
        if row["ret_5d"] > 0.18:
            score -= 0.35
        confidence = max(0.35, min(0.95, 0.58 + 0.08 * score))
        scored.append(
            CandidateScore(
                symbol=row["symbol"],
                name=row["name"],
                sector=row["sector"],
                score=float(score),
                confidence=float(confidence),
                ret_5d=float(row["ret_5d"]),
                ret_20d=float(row["ret_20d"]),
                ret_60d=float(row["ret_60d"]),
                ma20_gap=float(row["ma20_gap"]),
                ma60_gap=float(row["ma60_gap"]),
                near_high20=float(row["near_high20"]),
                vol20=float(row["vol20"]),
                adv20=float(row["adv20"]),
            )
        )
    return tuple(sorted(scored, key=lambda item: item.score, reverse=True))


def _select_ranked(scores: tuple[CandidateScore, ...], *, top_k: int, max_per_sector: int) -> tuple[CandidateScore, ...]:
    selected: list[CandidateScore] = []
    sector_counts: dict[str, int] = {}
    for candidate in scores:
        sector = candidate.sector or "unknown"
        if sector_counts.get(sector, 0) >= max_per_sector:
            continue
        selected.append(candidate)
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
        if len(selected) >= top_k:
            return tuple(selected)
    for candidate in scores:
        if candidate in selected:
            continue
        selected.append(candidate)
        if len(selected) >= top_k:
            break
    return tuple(selected)


def _target_payload(
    day: date,
    *,
    selected: tuple[CandidateScore, ...],
    weights: tuple[float, ...],
    sleeve_id: str,
    gross: float,
    decision_time: str,
) -> dict[str, Any]:
    generated_at = datetime.combine(day, _parse_time(decision_time))
    expires_at = generated_at + timedelta(days=1)
    targets = []
    for index, candidate in enumerate(selected):
        if index >= len(weights):
            break
        weight = weights[index]
        targets.append(
            {
                "symbol": candidate.symbol.key,
                "name": candidate.name,
                "target_percent": round(weight, 6),
                "confidence": round(candidate.confidence, 4),
                "reason": (
                    f"agent_proxy_rank={index + 1}; score={candidate.score:.3f}; "
                    f"ret5={candidate.ret_5d:.2%}; ret20={candidate.ret_20d:.2%}; "
                    f"ret60={candidate.ret_60d:.2%}; ma20_gap={candidate.ma20_gap:.2%}; "
                    f"vol20={candidate.vol20:.2%}; adv20={candidate.adv20:,.0f}"
                ),
                "features": {
                    "score": round(candidate.score, 6),
                    "ret_5d": round(candidate.ret_5d, 6),
                    "ret_20d": round(candidate.ret_20d, 6),
                    "ret_60d": round(candidate.ret_60d, 6),
                    "ma20_gap": round(candidate.ma20_gap, 6),
                    "ma60_gap": round(candidate.ma60_gap, 6),
                    "near_high20": round(candidate.near_high20, 6),
                    "vol20": round(candidate.vol20, 6),
                    "adv20": round(candidate.adv20, 2),
                    "sector": candidate.sector,
                },
            }
        )
    return {
        "sleeve_id": sleeve_id,
        "target_id": f"leaps-agent-proxy-{day:%Y%m%d}",
        "generated_at": generated_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "max_gross_exposure": round(float(gross), 6),
        "flatten": False,
        "method": "cached_daily_point_in_time_proxy",
        "lookahead_guard": "features use daily bars with bar.date < target_date only",
        "targets": targets,
    }


def _research_runtime_config(
    *,
    config_path: Path,
    output_root: Path,
    sleeve_id: str,
    cash: float,
    target_template: str,
    gross: float,
    max_position_pct: float,
    decision_time: str,
) -> dict[str, Any]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["runtime_id"] = f"leaps_agent_proxy_backtest_{output_root.name}"
    payload["mode"] = "backtest"
    payload["journal_path"] = (output_root / "cycle_journal.jsonl").as_posix()
    sleeves = payload.get("sleeves") or []
    sleeve = next((item for item in sleeves if str(item.get("sleeve_id")) == sleeve_id), None)
    if sleeve is None:
        raise SystemExit(f"Sleeve not found in config: {sleeve_id}")
    payload["sleeves"] = [sleeve]
    sleeve["cash"] = cash
    sleeve["cash_by_currency"] = {"KRW": cash}
    active = sleeve.setdefault("universe", {}).setdefault("active", {})
    active["cadence"] = f"daily_at {decision_time} Asia/Seoul"
    portfolio = sleeve.setdefault("portfolio", {})
    parameters = dict(portfolio.get("parameters") or portfolio.get("params") or {})
    parameters.update(
        {
            "target_path": target_template,
            "max_gross_exposure": gross,
            "max_position_pct": max_position_pct,
            "max_target_age_hours": 48.0,
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


def _minute_backtest_command(
    runtime_config_path: Path,
    *,
    start: date,
    end: date,
    cash: float,
    sleeve_id: str,
    output_root: Path,
) -> list[str]:
    return [
        "$env:PYTHONPATH='src'",
        "py -3 -m leaps_quant_engine.cli runtime-backtest-minute",
        runtime_config_path.as_posix(),
        "--sleeve-id",
        sleeve_id,
        "--minute-cache-root",
        "data/replay/minute-cache",
        "--compiled-replay-cache",
        (output_root / "compiled_minute_replay.json.gz").as_posix(),
        "--daily-warmup-cache",
        (output_root / "daily_warmup.json.gz").as_posix(),
        "--start",
        f"{start.isoformat()}T09:00:00",
        "--end",
        f"{end.isoformat()}T15:30:00",
        "--warmup-start",
        (start - timedelta(days=220)).isoformat(),
        "--cash",
        str(int(cash)),
        "--currency",
        "KRW",
        "--daily-source",
        "finance-datareader",
        "--fee-model",
        "kis",
        "--slippage-bps",
        "5",
        "--summary-only",
    ]


def _rank_weights(
    *,
    top_k: int,
    gross: float,
    max_position_pct: float,
    explicit: tuple[float, ...],
) -> tuple[float, ...]:
    if top_k <= 0:
        return ()
    weights = list(explicit[:top_k])
    if len(weights) < top_k:
        tail = [math.exp(-0.35 * index) for index in range(top_k)]
        weights = tail
    weights = [min(max(0.0, value), max_position_pct) for value in weights]
    total = sum(weights)
    if total <= 0:
        return tuple(0.0 for _ in range(top_k))
    scale = min(1.0, gross / total)
    return tuple(round(value * scale, 6) for value in weights)


def _parse_weights(value: str) -> tuple[float, ...]:
    result = []
    for chunk in str(value or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        result.append(float(chunk))
    return tuple(result)


def _z_map(rows: list[dict[str, Any]], field_name: str) -> dict[str, float]:
    values = [math.log(row[field_name]) if field_name == "adv20" else float(row[field_name]) for row in rows]
    mean = _mean(values)
    std = _std(values)
    if std <= 1e-12:
        return {row["symbol"].key: 0.0 for row in rows}
    return {
        row["symbol"].key: ((math.log(row[field_name]) if field_name == "adv20" else float(row[field_name])) - mean) / std
        for row in rows
    }


def _mean(values: Iterable[float]) -> float:
    items = [float(value) for value in values]
    return sum(items) / len(items) if items else 0.0


def _std(values: Iterable[float]) -> float:
    items = [float(value) for value in values]
    if len(items) < 2:
        return 0.0
    mean = _mean(items)
    variance = sum((value - mean) ** 2 for value in items) / (len(items) - 1)
    return math.sqrt(max(variance, 0.0))


def _weekdays(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def _parse_date(value: str) -> date:
    return datetime.fromisoformat(value).date()


def _parse_time(value: str) -> time:
    hour, minute = str(value).split(":", 1)
    return time(hour=int(hour), minute=int(minute))


if __name__ == "__main__":
    raise SystemExit(main())
