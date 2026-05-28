from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
BASE_CONFIG = ROOT / "configs" / "runtime" / "kr_lowvol_defensive_sleeve.json"
BASE_SLEEVE = ROOT / "sleeves" / "kr-lowvol-defensive"
ARTIFACT_ROOT = ROOT / "artifacts" / "tuning" / "kr-lowvol-defensive"


@dataclass(frozen=True, slots=True)
class Variant:
    variant_id: str
    note: str
    alpha: dict[str, float] = field(default_factory=dict)
    selection: dict[str, float] = field(default_factory=dict)
    portfolio: dict[str, float | int | bool] = field(default_factory=dict)
    risk: dict[str, float | int | bool] = field(default_factory=dict)
    active_max_symbols: int | None = None


VARIANTS: tuple[Variant, ...] = (
    Variant("v00_base", "current v2.1 defaults"),
    Variant(
        "v01_tight_vol",
        "stricter volatility gate",
        alpha={"MAX_NORMALIZED_VOLATILITY": 0.105, "HARD_MAX_NORMALIZED_VOLATILITY": 0.140},
        selection={"MAX_NORMALIZED_VOLATILITY": 0.105, "HARD_MAX_NORMALIZED_VOLATILITY": 0.140},
    ),
    Variant(
        "v02_loose_vol",
        "looser volatility gate",
        alpha={"MAX_NORMALIZED_VOLATILITY": 0.135, "HARD_MAX_NORMALIZED_VOLATILITY": 0.175},
        selection={"MAX_NORMALIZED_VOLATILITY": 0.135, "HARD_MAX_NORMALIZED_VOLATILITY": 0.175},
    ),
    Variant(
        "v03_high_quality_bar",
        "higher score threshold and lower exposure",
        alpha={"MIN_SCORE": 0.35},
        portfolio={"core_gross_exposure": 0.78, "neutral_gross_exposure": 0.56, "defensive_gross_exposure": 0.34},
    ),
    Variant(
        "v04_more_names",
        "more diversification, smaller caps per name",
        alpha={"MIN_SCORE": 0.27, "MAX_SELECTED": 16},
        portfolio={"top_k": 16, "max_position_pct": 0.075, "min_position_pct": 0.010},
        active_max_symbols=52,
    ),
    Variant(
        "v05_concentrated",
        "fewer names, more conviction per name",
        alpha={"MIN_SCORE": 0.34, "MAX_SELECTED": 8},
        portfolio={"top_k": 8, "max_position_pct": 0.12, "min_position_pct": 0.020},
        active_max_symbols=32,
    ),
    Variant(
        "v06_tight_crowding",
        "stricter turnover and lottery rejection",
        alpha={
            "HARD_VOLUME_RATIO": 3.80,
            "HARD_UPSIDE_SPIKE": 0.080,
            "HARD_INTRADAY_RANGE": 0.105,
            "LOTTERY_REJECT": 0.70,
            "CROWDING_REJECT": 0.78,
        },
        selection={"HARD_VOLUME_RATIO": 3.80, "HARD_UPSIDE_SPIKE": 0.080, "HARD_INTRADAY_RANGE": 0.105},
    ),
    Variant(
        "v07_loose_crowding",
        "looser turnover and lottery rejection",
        alpha={
            "HARD_VOLUME_RATIO": 5.20,
            "HARD_UPSIDE_SPIKE": 0.110,
            "HARD_INTRADAY_RANGE": 0.135,
            "LOTTERY_REJECT": 0.84,
            "CROWDING_REJECT": 0.90,
        },
        selection={"HARD_VOLUME_RATIO": 5.20, "HARD_UPSIDE_SPIKE": 0.110, "HARD_INTRADAY_RANGE": 0.135},
    ),
    Variant(
        "v08_tight_falling_knife",
        "stricter weak momentum and drawdown defense",
        alpha={"MOMENTUM_20_FALLING": -0.035, "DRAWDOWN_20_FALLING": -0.075, "MOMENTUM_60_MIN": -0.055},
        selection={"MIN_MOMENTUM_60": -0.055, "MAX_DRAWDOWN_60": 0.180},
    ),
    Variant(
        "v09_loose_falling_knife",
        "looser weak momentum and drawdown defense",
        alpha={"MOMENTUM_20_FALLING": -0.080, "DRAWDOWN_20_FALLING": -0.130, "MOMENTUM_60_MIN": -0.125},
        selection={"MIN_MOMENTUM_60": -0.125, "MAX_DRAWDOWN_60": 0.270},
    ),
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=("all", "daily", "hour", "minute"), default="all")
    parser.add_argument("--daily-top", type=int, default=5)
    parser.add_argument("--hour-top", type=int, default=3)
    parser.add_argument("--minute-top", type=int, default=2)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()

    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    prepared = {variant.variant_id: prepare_variant(variant, refresh=args.refresh) for variant in VARIANTS}

    results: dict[str, dict[str, Any]] = {}
    daily_ids = tuple(variant.variant_id for variant in VARIANTS)
    if args.stage in {"all", "daily"}:
        for variant_id in daily_ids:
            if results.get(variant_id, {}).get("daily") and not args.refresh:
                continue
            results.setdefault(variant_id, {})["daily"] = run_daily(prepared[variant_id])
        write_results(results)
    else:
        results = read_results()

    daily_ranked = rank_variants(results, "daily")
    hour_ids = tuple(item["variant_id"] for item in daily_ranked[: args.daily_top])
    if args.stage in {"all", "hour"}:
        for variant_id in hour_ids:
            if results.get(variant_id, {}).get("hour") and not args.refresh:
                continue
            results.setdefault(variant_id, {})["hour"] = run_hour(prepared[variant_id])
        write_results(results)
    else:
        results = read_results()

    hour_ranked = rank_variants(results, "hour")
    minute_ids = tuple(item["variant_id"] for item in hour_ranked[: args.hour_top])
    if args.stage in {"all", "minute"}:
        for variant_id in minute_ids[: args.minute_top]:
            if results.get(variant_id, {}).get("minute") and not args.refresh:
                continue
            results.setdefault(variant_id, {})["minute"] = run_minute(prepared[variant_id])
        write_results(results)

    summary = summarize(results)
    summary_path = ARTIFACT_ROOT / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def prepare_variant(variant: Variant, *, refresh: bool) -> Path:
    variant_root = ARTIFACT_ROOT / variant.variant_id
    workspace = variant_root / "workspace"
    if refresh and variant_root.exists():
        _safe_rmtree(variant_root)
    if not workspace.exists():
        shutil.copytree(BASE_SLEEVE, workspace)
    patch_alpha(workspace / "alphas" / "lowvol_defensive.py", variant.alpha)
    patch_selection(workspace / "selections" / "lowvol_rank.py", variant.selection)

    config = json.loads(BASE_CONFIG.read_text(encoding="utf-8"))
    config["runtime_id"] = f"kr-lowvol-defensive-tune-{variant.variant_id}"
    config["journal_path"] = f"artifacts/tuning/kr-lowvol-defensive/{variant.variant_id}/journal.jsonl"
    sleeve = config["sleeves"][0]
    sleeve["workspace_path"] = f"artifacts/tuning/kr-lowvol-defensive/{variant.variant_id}/workspace"
    if variant.active_max_symbols is not None:
        sleeve["universe"]["active"]["max_symbols"] = variant.active_max_symbols
    sleeve["portfolio"]["parameters"].update(variant.portfolio)
    sleeve["risk"]["parameters"].update(variant.risk)
    config["metadata"]["tuning_variant"] = variant.variant_id
    config["metadata"]["tuning_note"] = variant.note
    config_path = variant_root / "runtime.json"
    variant_root.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return config_path


def patch_alpha(path: Path, params: dict[str, float]) -> None:
    text = path.read_text(encoding="utf-8")
    assignments = {
        "MAX_SELECTED": params.get("MAX_SELECTED"),
        "MIN_SCORE": params.get("MIN_SCORE"),
        "MIN_LIQUIDITY": params.get("MIN_LIQUIDITY"),
        "MAX_NORMALIZED_VOLATILITY": params.get("MAX_NORMALIZED_VOLATILITY"),
        "HARD_MAX_NORMALIZED_VOLATILITY": params.get("HARD_MAX_NORMALIZED_VOLATILITY"),
    }
    for name, value in assignments.items():
        if value is not None:
            text = _replace_assignment(text, name, value)
    replacements = {
        r'item\["momentum_20"\] < -0\.06 and item\["drawdown_20"\] < -0\.10':
            f'item["momentum_20"] < {params.get("MOMENTUM_20_FALLING", -0.06):.6g} '
            f'and item["drawdown_20"] < {params.get("DRAWDOWN_20_FALLING", -0.10):.6g}',
        r'item\["momentum_60"\] < -0\.10':
            f'item["momentum_60"] < {params.get("MOMENTUM_60_MIN", -0.10):.6g}',
        r'item\["drawdown_60"\] < -0\.24':
            f'item["drawdown_60"] < {-abs(params.get("DRAWDOWN_60_MIN", 0.24)):.6g}',
        r'item\["gap"\] > 0\.09 or item\["high_low_range"\] > 0\.12':
            f'item["gap"] > {params.get("HARD_GAP", 0.09):.6g} '
            f'or item["high_low_range"] > {params.get("HARD_INTRADAY_RANGE", 0.12):.6g}',
        r'item\["volume_ratio_20"\] >= 4\.50':
            f'item["volume_ratio_20"] >= {params.get("HARD_VOLUME_RATIO", 4.50):.6g}',
        r'item\["bar_return"\] > 0\.095':
            f'item["bar_return"] > {params.get("HARD_UPSIDE_SPIKE", 0.095):.6g}',
        r'item\["lottery_penalty"\] >= 0\.78 or item\["crowding_penalty"\] >= 0\.85':
            f'item["lottery_penalty"] >= {params.get("LOTTERY_REJECT", 0.78):.6g} '
            f'or item["crowding_penalty"] >= {params.get("CROWDING_REJECT", 0.85):.6g}',
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text)
    path.write_text(text, encoding="utf-8")


def patch_selection(path: Path, params: dict[str, float]) -> None:
    text = path.read_text(encoding="utf-8")
    for name in (
        "MIN_LIQUIDITY",
        "MAX_NORMALIZED_VOLATILITY",
        "HARD_MAX_NORMALIZED_VOLATILITY",
        "MAX_DRAWDOWN_60",
        "MIN_MOMENTUM_60",
        "HARD_VOLUME_RATIO",
        "HARD_INTRADAY_RANGE",
        "HARD_UPSIDE_SPIKE",
    ):
        if name in params:
            text = _replace_assignment(text, name, params[name])
    path.write_text(text, encoding="utf-8")


def run_daily(config_path: Path) -> dict[str, Any]:
    return run_cli(
        [
            "runtime-backtest-daily",
            str(config_path),
            "--sleeve-id",
            "kr-lowvol-defensive",
            "--start",
            "2024-05-23",
            "--end",
            "2026-05-20",
            "--warmup-start",
            "2023-06-01",
            "--daily-bar-time",
            "09:05",
            "--cash",
            "10000000",
            "--currency",
            "KRW",
            "--source",
            "finance-datareader",
            "--fee-model",
            "kis",
            "--slippage-bps",
            "5",
            "--summary-only",
        ]
    )


def run_hour(config_path: Path) -> dict[str, Any]:
    return run_cli(
        [
            "runtime-backtest-minute",
            str(config_path),
            "--sleeve-id",
            "kr-lowvol-defensive",
            "--compiled-replay-cache",
            "data/replay/compiled/kr_lowvol_defensive_20240523_20260520_60m.json.gz",
            "--start",
            "2024-05-23T09:00:00",
            "--end",
            "2026-05-20T15:30:00",
            "--warmup-start",
            "2023-06-01",
            "--cash",
            "10000000",
            "--currency",
            "KRW",
            "--daily-source",
            "finance-datareader",
            "--daily-warmup-cache",
            "data/replay/warmup/kr_lowvol_defensive_20230601_20240522_daily.json.gz",
            "--fee-model",
            "kis",
            "--slippage-bps",
            "5",
            "--summary-only",
        ]
    )


def run_minute(config_path: Path) -> dict[str, Any]:
    return run_cli(
        [
            "runtime-backtest-minute",
            str(config_path),
            "--sleeve-id",
            "kr-lowvol-defensive",
            "--minute-feed",
            "data/replay/leaps_krx_20260421_20260515_stock200_minute.csv",
            "--start",
            "2026-04-21T09:00:00",
            "--end",
            "2026-05-15T15:30:00",
            "--warmup-start",
            "2025-06-02",
            "--cash",
            "10000000",
            "--currency",
            "KRW",
            "--daily-source",
            "finance-datareader",
            "--daily-warmup-cache",
            "data/replay/warmup/kr_lowvol_defensive_tuning_20250602_20260420_daily.json.gz",
            "--fee-model",
            "kis",
            "--slippage-bps",
            "5",
            "--summary-only",
        ]
    )


def run_cli(args: list[str]) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    command = [sys.executable, "-m", "leaps_quant_engine.cli", *args]
    completed = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
    if completed.returncode != 0:
        return {
            "status": "error",
            "returncode": completed.returncode,
            "command": command,
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {
            "status": "parse_error",
            "error": str(exc),
            "stdout": completed.stdout[-4000:],
            "stderr": completed.stderr[-4000:],
        }
    payload["status"] = "ok"
    return payload


def score_result(result: dict[str, Any]) -> float:
    if result.get("status") != "ok":
        return -999.0
    metrics = result.get("metrics", {})
    total_return = float(metrics.get("total_return") or 0.0)
    sharpe = float(metrics.get("sharpe") or 0.0)
    mdd = float(metrics.get("mdd") or 0.0)
    turnover = float(metrics.get("turnover") or 0.0)
    exposure = float(metrics.get("avg_exposure") or 0.0)
    return total_return * 1.0 + sharpe * 0.15 - mdd * 1.6 - turnover * 0.01 + exposure * 0.05


def rank_variants(results: dict[str, dict[str, Any]], stage: str) -> list[dict[str, Any]]:
    ranked = []
    for variant in VARIANTS:
        result = results.get(variant.variant_id, {}).get(stage)
        if not result:
            continue
        ranked.append(
            {
                "variant_id": variant.variant_id,
                "stage": stage,
                "score": score_result(result),
                "metrics": _metric_summary(result),
                "note": variant.note,
            }
        )
    return sorted(ranked, key=lambda item: float(item["score"]), reverse=True)


def summarize(results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {
        "variant_notes": {variant.variant_id: variant.note for variant in VARIANTS},
        "rankings": {
            "daily": rank_variants(results, "daily"),
            "hour": rank_variants(results, "hour"),
            "minute": rank_variants(results, "minute"),
        },
        "results_path": str(results_path().relative_to(ROOT)),
    }


def _metric_summary(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") != "ok":
        return {"status": result.get("status"), "stderr": result.get("stderr", "")[-800:]}
    metrics = result.get("metrics", {})
    return {
        "total_return": metrics.get("total_return"),
        "sharpe": metrics.get("sharpe"),
        "mdd": metrics.get("mdd"),
        "turnover": metrics.get("turnover"),
        "avg_exposure": metrics.get("avg_exposure"),
        "order_count": metrics.get("order_count"),
        "final_equity": metrics.get("final_equity"),
    }


def write_results(results: dict[str, dict[str, Any]]) -> None:
    results_path().write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_results() -> dict[str, dict[str, Any]]:
    path = results_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def results_path() -> Path:
    return ARTIFACT_ROOT / "results.json"


def _replace_assignment(text: str, name: str, value: float | int) -> str:
    literal = _literal(value)
    return re.sub(rf"^{name} = .*$", f"{name} = {literal}", text, flags=re.MULTILINE)


def _literal(value: float | int) -> str:
    if isinstance(value, int):
        return str(value)
    if abs(value) >= 1000:
        return f"{value:.1f}"
    return f"{value:.6g}"


def _safe_rmtree(path: Path) -> None:
    resolved = path.resolve()
    artifact = ARTIFACT_ROOT.resolve()
    if artifact not in resolved.parents and resolved != artifact:
        raise RuntimeError(f"Refusing to remove path outside artifact root: {resolved}")
    shutil.rmtree(resolved)


if __name__ == "__main__":
    raise SystemExit(main())
