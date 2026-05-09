from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Sequence

from leaps_quant_engine.adapters.kis import (
    KISBrokerEngineMarketDataProvider,
    KISCachedMarketDataProvider,
    MarketDataEngineLiveQuoteProvider,
)
from leaps_quant_engine.alpha import AlphaRuntime, PythonAlphaLoader, SnapshotContext
from leaps_quant_engine.benchmark import run_daily_indicator_benchmark
from leaps_quant_engine.logging import configure_logging
from leaps_quant_engine.live_snapshot import run_live_indicator_snapshot
from leaps_quant_engine.models import OrderIntent
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.runtime import build_indicator_engine_from_file, run_once_from_file
from leaps_quant_engine.runtime_bootstrap import bootstrap_sleeve_runtime, resolve_runtime_path
from leaps_quant_engine.runtime_config import load_runtime_config_snapshot
from leaps_quant_engine.snapshot_worker import BackgroundSnapshotWorker
from leaps_quant_engine.universe.fine import FineUniverseRuntime
from leaps_quant_engine.universe.loader import load_universe_definition
from leaps_quant_engine.universe.runtime import UniverseSelectionRuntime
from leaps_quant_engine.universe.selection import (
    MomentumUniverseSelectionModel,
    StaticUniverseSelectionModel,
    UniverseSelectionContext,
)
from leaps_quant_engine.warmup import WarmupPolicy, run_daily_indicator_warmup


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leapsq")
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--log-json", action="store_true")
    parser.add_argument("--log-max-bytes", type=int, default=10_000_000)
    parser.add_argument("--log-backup-count", type=int, default=5)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_once = subparsers.add_parser("run-once")
    run_once.add_argument("config", type=Path)

    runtime_config_validate = subparsers.add_parser("runtime-config-validate")
    runtime_config_validate.add_argument("config", type=Path)

    runtime_run_once = subparsers.add_parser("runtime-run-once")
    runtime_run_once.add_argument("config", type=Path)
    runtime_run_once.add_argument("--sleeve-id")
    runtime_run_once.add_argument("--held", action="append", default=[])
    runtime_run_once.add_argument("--open-order", action="append", default=[])
    runtime_run_once.add_argument("--exit-watch", action="append", default=[])
    runtime_run_once.add_argument("--manual", action="append", default=[])
    runtime_run_once.add_argument("--skip-fine-refresh", action="store_true")
    runtime_run_once.add_argument("--skip-warmup", action="store_true")
    runtime_run_once.add_argument("--summary-only", action="store_true")

    subparsers.add_parser("kis-health")

    kis_quote = subparsers.add_parser("kis-quote")
    kis_quote.add_argument("symbol")
    kis_quote.add_argument("--market", default="KRX")

    indicators_kis = subparsers.add_parser("indicators-kis-once")
    indicators_kis.add_argument("config", type=Path)
    indicators_kis.add_argument("--sleeve-id", required=True)
    indicators_kis.add_argument("--warmup-start")
    indicators_kis.add_argument("--warmup-end")

    indicators_backtest = subparsers.add_parser("indicators-backtest-once")
    indicators_backtest.add_argument("config", type=Path)
    indicators_backtest.add_argument("--sleeve-id", required=True)

    benchmark_indicators = subparsers.add_parser("benchmark-indicators-daily")
    benchmark_indicators.add_argument("universe", type=Path)
    benchmark_indicators.add_argument("--sleeve-id", required=True)
    benchmark_indicators.add_argument("--start")
    benchmark_indicators.add_argument("--end")
    benchmark_indicators.add_argument("--source", default="kis-cache", choices=("kis-cache",))
    benchmark_indicators.add_argument("--refresh-history", action="store_true")
    benchmark_indicators.add_argument("--include-daily", action="store_true")

    warmup_indicators = subparsers.add_parser("warmup-indicators-daily")
    warmup_indicators.add_argument("universe", type=Path)
    warmup_indicators.add_argument("--sleeve-id", required=True)
    warmup_indicators.add_argument("--start")
    warmup_indicators.add_argument("--end")
    warmup_indicators.add_argument("--source", default="kis-cache", choices=("kis-cache",))
    warmup_indicators.add_argument("--refresh-history", action="store_true")
    warmup_indicators.add_argument("--extra-bars", type=int, default=0)
    warmup_indicators.add_argument("--min-ready-ratio", type=float, default=1.0)
    warmup_indicators.add_argument("--summary-only", action="store_true")

    select_universe = subparsers.add_parser("select-active-universe")
    select_universe.add_argument("universe", type=Path)
    select_universe.add_argument("--sleeve-id", required=True)
    select_universe.add_argument("--top-n", type=int, default=60)
    select_universe.add_argument("--start")
    select_universe.add_argument("--end")
    select_universe.add_argument("--source", default="kis-cache", choices=("kis-cache",))
    select_universe.add_argument("--refresh-history", action="store_true")
    select_universe.add_argument("--extra-bars", type=int, default=0)
    select_universe.add_argument("--min-ready-ratio", type=float, default=0.99)
    select_universe.add_argument("--price-indicator", default="identity_close")
    select_universe.add_argument("--moving-average-indicator", default="sma_5_close")
    select_universe.add_argument("--momentum-indicator", default="momentum_5_close")
    select_universe.add_argument("--liquidity-indicator", default="rolling_dollar_volume_20")
    select_universe.add_argument("--volatility-indicator", default="stddev_20_close")
    select_universe.add_argument("--require-price-above-average", action="store_true")
    select_universe.add_argument("--allow-negative-momentum", action="store_true")
    select_universe.add_argument("--min-liquidity", type=float)
    select_universe.add_argument("--max-volatility", type=float)
    select_universe.add_argument("--previous-live", action="append", default=[])
    select_universe.add_argument("--held", action="append", default=[])
    select_universe.add_argument("--open-order", action="append", default=[])
    select_universe.add_argument("--exit-watch", action="append", default=[])
    select_universe.add_argument("--manual", action="append", default=[])
    select_universe.add_argument("--summary-only", action="store_true")

    live_indicators = subparsers.add_parser("live-indicators-once")
    live_indicators.add_argument("universe", type=Path)
    live_indicators.add_argument("--sleeve-id", required=True)
    live_indicators.add_argument("--source", default="market-data-engine", choices=("market-data-engine",))
    live_indicators.add_argument("--min-success", type=int)
    live_indicators.add_argument("--rate-limit-per-second", type=int)
    live_indicators.add_argument("--include-failures", action="store_true")

    fine_refresh = subparsers.add_parser("fine-universe-refresh")
    fine_refresh.add_argument("universe", type=Path)
    fine_refresh.add_argument("--source", default="market-data-engine", choices=("market-data-engine",))
    fine_refresh.add_argument("--rate-limit-per-second", type=int)
    fine_refresh.add_argument("--max-symbols", type=int)
    fine_refresh.add_argument("--min-success", type=int)
    fine_refresh.add_argument("--max-age-seconds", type=float, default=300.0)
    fine_refresh.add_argument("--include-entries", action="store_true")

    snapshot_worker = subparsers.add_parser("snapshot-worker-run")
    snapshot_worker.add_argument("universe", type=Path)
    snapshot_worker.add_argument("--sleeve-id", required=True)
    snapshot_worker.add_argument("--source", default="market-data-engine", choices=("market-data-engine",))
    snapshot_worker.add_argument("--history-source", default="kis-cache", choices=("kis-cache",))
    snapshot_worker.add_argument("--cycles", type=int, default=1)
    snapshot_worker.add_argument("--interval-seconds", type=float, default=60.0)
    snapshot_worker.add_argument("--min-success", type=int)
    snapshot_worker.add_argument("--rate-limit-per-second", type=int)
    snapshot_worker.add_argument("--skip-warmup", action="store_true")
    snapshot_worker.add_argument("--warmup-start")
    snapshot_worker.add_argument("--warmup-end")
    snapshot_worker.add_argument("--refresh-history", action="store_true")
    snapshot_worker.add_argument("--extra-bars", type=int, default=0)
    snapshot_worker.add_argument("--min-ready-ratio", type=float, default=1.0)
    snapshot_worker.add_argument("--summary-only", action="store_true")

    active_snapshot_worker = subparsers.add_parser("active-snapshot-worker-run")
    active_snapshot_worker.add_argument("universe", type=Path)
    active_snapshot_worker.add_argument("--sleeve-id", required=True)
    active_snapshot_worker.add_argument("--selection", choices=("static", "momentum"), default="static")
    active_snapshot_worker.add_argument("--top-n", type=int, default=60)
    active_snapshot_worker.add_argument("--fine-refresh", action="store_true")
    active_snapshot_worker.add_argument("--fine-max-symbols", type=int)
    active_snapshot_worker.add_argument("--fine-min-success", type=int)
    active_snapshot_worker.add_argument("--fine-max-age-seconds", type=float, default=300.0)
    active_snapshot_worker.add_argument("--source", default="market-data-engine", choices=("market-data-engine",))
    active_snapshot_worker.add_argument("--history-source", default="kis-cache", choices=("kis-cache",))
    active_snapshot_worker.add_argument("--cycles", type=int, default=1)
    active_snapshot_worker.add_argument("--interval-seconds", type=float, default=60.0)
    active_snapshot_worker.add_argument("--min-success", type=int)
    active_snapshot_worker.add_argument("--rate-limit-per-second", type=int)
    active_snapshot_worker.add_argument("--skip-worker-warmup", action="store_true")
    active_snapshot_worker.add_argument("--start")
    active_snapshot_worker.add_argument("--end")
    active_snapshot_worker.add_argument("--refresh-history", action="store_true")
    active_snapshot_worker.add_argument("--extra-bars", type=int, default=0)
    active_snapshot_worker.add_argument("--min-ready-ratio", type=float, default=0.99)
    active_snapshot_worker.add_argument("--held", action="append", default=[])
    active_snapshot_worker.add_argument("--open-order", action="append", default=[])
    active_snapshot_worker.add_argument("--exit-watch", action="append", default=[])
    active_snapshot_worker.add_argument("--manual", action="append", default=[])
    active_snapshot_worker.add_argument("--summary-only", action="store_true")

    alpha_run = subparsers.add_parser("alpha-run-snapshot")
    alpha_run.add_argument("universe", type=Path)
    alpha_run.add_argument("alpha", type=Path)
    alpha_run.add_argument("--sleeve-id", required=True)
    alpha_run.add_argument("--source", default="market-data-engine", choices=("market-data-engine",))
    alpha_run.add_argument("--history-source", default="kis-cache", choices=("kis-cache",))
    alpha_run.add_argument("--min-success", type=int)
    alpha_run.add_argument("--rate-limit-per-second", type=int)
    alpha_run.add_argument("--skip-warmup", action="store_true")
    alpha_run.add_argument("--warmup-start")
    alpha_run.add_argument("--warmup-end")
    alpha_run.add_argument("--refresh-history", action="store_true")
    alpha_run.add_argument("--extra-bars", type=int, default=0)
    alpha_run.add_argument("--min-ready-ratio", type=float, default=1.0)
    alpha_run.add_argument("--summary-only", action="store_true")

    args = parser.parse_args(argv)
    configure_logging(
        level=args.log_level,
        log_file=args.log_file,
        json_logs=args.log_json,
        max_bytes=args.log_max_bytes,
        backup_count=args.log_backup_count,
    )
    if args.command == "run-once":
        orders = run_once_from_file(args.config, time=datetime(2026, 5, 7, 9, 0))
        print(json.dumps([_order_to_json(order) for order in orders], ensure_ascii=False, indent=2))
        return 0
    if args.command == "runtime-config-validate":
        snapshot = load_runtime_config_snapshot(args.config)
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "runtime-run-once":
        snapshot = load_runtime_config_snapshot(args.config)
        default_market = _default_market_from_runtime_snapshot(snapshot, args.sleeve_id)
        runtime = bootstrap_sleeve_runtime(
            snapshot,
            args.sleeve_id,
            refresh_fine=not args.skip_fine_refresh,
            held_symbols=_parse_symbol_refs(args.held, default_market),
            open_order_symbols=_parse_symbol_refs(args.open_order, default_market),
            exit_watch_symbols=_parse_symbol_refs(args.exit_watch, default_market),
            manual_symbols=_parse_symbol_refs(args.manual, default_market),
        )
        report = runtime.run_once(warmup=False if args.skip_warmup else None)
        print(
            json.dumps(
                report.to_dict(
                    include_candidates=not args.summary_only,
                    include_warmup_symbols=not args.summary_only,
                    include_failures=not args.summary_only,
                    include_framework_details=not args.summary_only,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "kis-health":
        provider = KISBrokerEngineMarketDataProvider.from_env()
        print(json.dumps(provider.health_check(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "kis-quote":
        provider = KISBrokerEngineMarketDataProvider.from_env()
        bar = provider.get_latest_bar(Symbol(args.symbol, args.market))
        print(
            json.dumps(
                {
                    "symbol": bar.symbol.ticker,
                    "market": bar.symbol.market,
                    "time": bar.time.isoformat(),
                    "close": bar.close,
                    "volume": bar.volume,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "indicators-kis-once":
        provider = KISBrokerEngineMarketDataProvider.from_env()
        indicator_engine = build_indicator_engine_from_file(args.config)
        if args.warmup_start or args.warmup_end:
            indicator_engine.warm_up_from_provider(
                args.sleeve_id,
                provider,
                start=_parse_cli_datetime(args.warmup_start),
                end=_parse_cli_datetime(args.warmup_end),
            )
        indicator_engine.update_from_provider(provider)
        print(json.dumps(_indicator_values_to_json(indicator_engine, args.sleeve_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "indicators-backtest-once":
        indicator_engine = build_indicator_engine_from_file(args.config)
        print(json.dumps(_indicator_values_to_json(indicator_engine, args.sleeve_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "benchmark-indicators-daily":
        provider = KISCachedMarketDataProvider.from_env()
        universe = load_universe_definition(args.universe)
        report = run_daily_indicator_benchmark(
            universe,
            provider,
            sleeve_id=args.sleeve_id,
            start=_parse_cli_datetime(args.start),
            end=_parse_cli_datetime(args.end),
            refresh_history=args.refresh_history,
            include_daily=args.include_daily,
            source=args.source,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "warmup-indicators-daily":
        provider = KISCachedMarketDataProvider.from_env()
        universe = load_universe_definition(args.universe)
        result = run_daily_indicator_warmup(
            universe,
            provider,
            sleeve_id=args.sleeve_id,
            start=_parse_cli_datetime(args.start),
            end=_parse_cli_datetime(args.end),
            refresh_history=args.refresh_history,
            source=args.source,
            policy=WarmupPolicy(extra_bars=args.extra_bars, min_ready_ratio=args.min_ready_ratio),
        )
        print(json.dumps(result.report.to_dict(include_symbols=not args.summary_only), ensure_ascii=False, indent=2))
        return 0
    if args.command == "select-active-universe":
        provider = KISCachedMarketDataProvider.from_env()
        universe = load_universe_definition(args.universe)
        warmup_result = run_daily_indicator_warmup(
            universe,
            provider,
            sleeve_id=args.sleeve_id,
            start=_parse_cli_datetime(args.start),
            end=_parse_cli_datetime(args.end),
            refresh_history=args.refresh_history,
            source=args.source,
            policy=WarmupPolicy(extra_bars=args.extra_bars, min_ready_ratio=args.min_ready_ratio),
        )
        indicator_snapshot = warmup_result.indicator_engine.snapshot(
            args.sleeve_id,
            universe_id=universe.id,
        )
        model = MomentumUniverseSelectionModel(
            max_active_symbols=args.top_n,
            price_indicator=args.price_indicator,
            moving_average_indicator=args.moving_average_indicator,
            momentum_indicator=args.momentum_indicator,
            liquidity_indicator=args.liquidity_indicator,
            volatility_indicator=args.volatility_indicator if args.volatility_indicator else None,
            require_positive_momentum=not args.allow_negative_momentum,
            require_price_above_average=args.require_price_above_average,
            min_liquidity=args.min_liquidity,
            max_volatility=args.max_volatility,
        )
        context = UniverseSelectionContext(
            sleeve_id=args.sleeve_id,
            universe=universe,
            indicator_snapshot=indicator_snapshot,
            previous_live_symbols=_parse_symbol_refs(args.previous_live, universe.market),
            held_symbols=_parse_symbol_refs(args.held, universe.market),
            open_order_symbols=_parse_symbol_refs(args.open_order, universe.market),
            exit_watch_symbols=_parse_symbol_refs(args.exit_watch, universe.market),
            manual_symbols=_parse_symbol_refs(args.manual, universe.market),
        )
        selection = model.select(context)
        report = {
            "warmup": warmup_result.report.to_dict(include_symbols=False),
            "selection": selection.to_dict(include_candidates=not args.summary_only),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "live-indicators-once":
        universe = load_universe_definition(args.universe)
        provider = MarketDataEngineLiveQuoteProvider.from_env(
            exchange_by_symbol=_exchange_map_from_universe(universe),
            rate_limit_per_second=args.rate_limit_per_second,
        )
        report = run_live_indicator_snapshot(
            universe,
            provider,
            sleeve_id=args.sleeve_id,
            source=args.source,
            min_success=args.min_success,
            include_failures=args.include_failures,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "fine-universe-refresh":
        universe = load_universe_definition(args.universe)
        provider = MarketDataEngineLiveQuoteProvider.from_env(
            exchange_by_symbol=_exchange_map_from_universe(universe),
            rate_limit_per_second=args.rate_limit_per_second,
        )
        runtime = FineUniverseRuntime(
            universe=universe,
            provider=provider,
            source=args.source,
            max_age_seconds=args.max_age_seconds,
        )
        report = runtime.refresh_once(max_symbols=args.max_symbols, min_success=args.min_success)
        output = {
            "refresh": report.to_dict(),
            "fine_universe": {
                "universe_id": runtime.fine_universe_definition().id,
                "symbol_count": len(runtime.fine_universe_definition().symbols),
                "symbols": [symbol.key for symbol in runtime.fine_universe_definition().symbols],
            },
        }
        if args.include_entries:
            output["entries"] = runtime.cache.to_dict(max_age_seconds=args.max_age_seconds)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    if args.command == "snapshot-worker-run":
        universe = load_universe_definition(args.universe)
        live_provider = MarketDataEngineLiveQuoteProvider.from_env(
            exchange_by_symbol=_exchange_map_from_universe(universe),
            rate_limit_per_second=args.rate_limit_per_second,
        )
        history_provider = KISCachedMarketDataProvider.from_env()
        worker = BackgroundSnapshotWorker(
            universe=universe,
            sleeve_id=args.sleeve_id,
            live_provider=live_provider,
            history_provider=history_provider,
            source=args.source,
            history_source=args.history_source,
            min_success=args.min_success,
            interval_seconds=args.interval_seconds,
            warmup_policy=WarmupPolicy(extra_bars=args.extra_bars, min_ready_ratio=args.min_ready_ratio),
        )
        report = worker.run(
            max_cycles=args.cycles,
            warmup=not args.skip_warmup,
            warmup_start=_parse_cli_datetime(args.warmup_start),
            warmup_end=_parse_cli_datetime(args.warmup_end),
            refresh_history=args.refresh_history,
        )
        print(
            json.dumps(
                report.to_dict(
                    include_warmup_symbols=not args.summary_only,
                    include_failures=not args.summary_only,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "active-snapshot-worker-run":
        coarse_universe = load_universe_definition(args.universe)
        history_provider = KISCachedMarketDataProvider.from_env()
        live_provider = MarketDataEngineLiveQuoteProvider.from_env(
            exchange_by_symbol=_exchange_map_from_universe(coarse_universe),
            rate_limit_per_second=args.rate_limit_per_second,
        )
        fine_refresh_report = None
        selection_base_universe = coarse_universe
        if args.fine_refresh:
            fine_runtime = FineUniverseRuntime(
                universe=coarse_universe,
                provider=live_provider,
                source=args.source,
                max_age_seconds=args.fine_max_age_seconds,
            )
            fine_refresh_report = fine_runtime.refresh_once(
                max_symbols=args.fine_max_symbols,
                min_success=args.fine_min_success,
            )
            selection_base_universe = fine_runtime.fine_universe_definition(
                universe_id=f"{coarse_universe.id}-fine",
            )
        selection_warmup_report = None
        indicator_snapshot = None
        if args.selection == "momentum":
            selection_warmup = run_daily_indicator_warmup(
                selection_base_universe,
                history_provider,
                sleeve_id=args.sleeve_id,
                start=_parse_cli_datetime(args.start),
                end=_parse_cli_datetime(args.end),
                refresh_history=args.refresh_history,
                source=args.history_source,
                policy=WarmupPolicy(extra_bars=args.extra_bars, min_ready_ratio=args.min_ready_ratio),
            )
            selection_warmup_report = selection_warmup.report
            indicator_snapshot = selection_warmup.indicator_engine.snapshot(
                args.sleeve_id,
                universe_id=selection_base_universe.id,
            )
            selection_model = MomentumUniverseSelectionModel(max_active_symbols=args.top_n)
        else:
            selection_model = StaticUniverseSelectionModel(max_active_symbols=args.top_n)
        selection_runtime = UniverseSelectionRuntime(
            coarse_universe=selection_base_universe,
            selection_model=selection_model,
        )
        active = selection_runtime.select_active(
            sleeve_id=args.sleeve_id,
            indicator_snapshot=indicator_snapshot,
            held_symbols=_parse_symbol_refs(args.held, coarse_universe.market),
            open_order_symbols=_parse_symbol_refs(args.open_order, coarse_universe.market),
            exit_watch_symbols=_parse_symbol_refs(args.exit_watch, coarse_universe.market),
            manual_symbols=_parse_symbol_refs(args.manual, coarse_universe.market),
            active_universe_id=f"{selection_base_universe.id}-active",
        )
        worker = BackgroundSnapshotWorker(
            universe=active.active_universe,
            sleeve_id=args.sleeve_id,
            live_provider=live_provider,
            history_provider=history_provider,
            source=args.source,
            history_source=args.history_source,
            min_success=args.min_success,
            interval_seconds=args.interval_seconds,
            warmup_policy=WarmupPolicy(extra_bars=args.extra_bars, min_ready_ratio=args.min_ready_ratio),
        )
        run_report = worker.run(
            max_cycles=args.cycles,
            warmup=not args.skip_worker_warmup,
            warmup_start=_parse_cli_datetime(args.start),
            warmup_end=_parse_cli_datetime(args.end),
            refresh_history=args.refresh_history,
        )
        report = {
            "coarse_universe_id": coarse_universe.id,
            "fine_universe_id": selection_base_universe.id if args.fine_refresh else None,
            "active_universe_id": active.active_universe.id,
            "fine_refresh": fine_refresh_report.to_dict() if fine_refresh_report is not None else None,
            "selection_warmup": (
                selection_warmup_report.to_dict(include_symbols=False)
                if selection_warmup_report is not None
                else None
            ),
            "selection": active.selection.to_dict(include_candidates=not args.summary_only),
            "worker": run_report.to_dict(
                include_warmup_symbols=not args.summary_only,
                include_failures=not args.summary_only,
            ),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "alpha-run-snapshot":
        universe = load_universe_definition(args.universe)
        live_provider = MarketDataEngineLiveQuoteProvider.from_env(
            exchange_by_symbol=_exchange_map_from_universe(universe),
            rate_limit_per_second=args.rate_limit_per_second,
        )
        history_provider = KISCachedMarketDataProvider.from_env()
        alpha_load = PythonAlphaLoader().load(args.alpha)
        alpha_runtime = AlphaRuntime()
        worker = BackgroundSnapshotWorker(
            universe=universe,
            sleeve_id=args.sleeve_id,
            live_provider=live_provider,
            history_provider=history_provider,
            source=args.source,
            history_source=args.history_source,
            min_success=args.min_success,
            interval_seconds=0.0,
            warmup_policy=WarmupPolicy(extra_bars=args.extra_bars, min_ready_ratio=args.min_ready_ratio),
        )
        if not args.skip_warmup:
            worker.warm_up(
                start=_parse_cli_datetime(args.warmup_start),
                end=_parse_cli_datetime(args.warmup_end),
                refresh_history=args.refresh_history,
            )
        latest_snapshot = worker.run_once()
        active_indicator_snapshot = worker.stores_by_sleeve[args.sleeve_id].active()
        if active_indicator_snapshot is None:
            raise RuntimeError("No active indicator snapshot was published.")
        context = SnapshotContext.from_indicator_snapshot(active_indicator_snapshot)
        alpha_runtime.stage([alpha_load.model], validation_context=context)
        insight_batch = alpha_runtime.run(context, activate_pending=True, publish_active=True)
        report = {
            "alpha": {
                "alpha_id": alpha_load.alpha_id,
                "version": alpha_load.version,
                "path": str(alpha_load.path),
                "content_hash": alpha_load.content_hash,
            },
            "cycle": latest_snapshot.to_dict(include_failures=not args.summary_only),
            "insights": insight_batch.to_dict(),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    return 1


def _order_to_json(order: OrderIntent) -> dict[str, object]:
    return {
        "sleeve_id": order.sleeve_id,
        "symbol": order.symbol.ticker,
        "market": order.symbol.market,
        "side": order.side.value,
        "quantity": order.quantity,
        "reference_price": order.reference_price,
        "notional": order.notional,
        "tag": order.tag,
    }


def _indicator_values_to_json(indicator_engine, sleeve_id: str) -> dict[str, object]:
    symbols = indicator_engine.symbols_for_sleeve(sleeve_id)
    return {
        "sleeve_id": sleeve_id,
        "symbols": [symbol.key for symbol in symbols],
        "values": indicator_engine.values_for(sleeve_id, symbols, ready_only=False),
    }


def _parse_cli_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d")
    return datetime.fromisoformat(text)


def _exchange_map_from_universe(universe) -> dict[str, str]:
    exchange_by_symbol: dict[str, str] = {}
    for symbol in universe.symbols:
        exchange = universe.properties_for(symbol).get("exchange")
        if exchange:
            exchange_by_symbol[symbol.key] = str(exchange).strip().upper()
            exchange_by_symbol[symbol.ticker] = str(exchange).strip().upper()
    return exchange_by_symbol


def _parse_symbol_refs(values: Sequence[str], default_market: str) -> tuple[Symbol, ...]:
    symbols: list[Symbol] = []
    for value in values:
        text = value.strip().upper()
        if not text:
            continue
        if ":" in text:
            market, ticker = text.split(":", 1)
            symbols.append(Symbol(ticker=ticker.strip(), market=market.strip()))
        else:
            symbols.append(Symbol(ticker=text, market=default_market))
    return tuple(symbols)


def _default_market_from_runtime_snapshot(snapshot, sleeve_id: str | None) -> str:
    sleeves = snapshot.config.sleeves
    if sleeve_id is None and len(sleeves) == 1:
        sleeve = sleeves[0]
    elif sleeve_id is not None:
        sleeve = snapshot.config.sleeve(sleeve_id)
    else:
        return "KRX"
    universe = load_universe_definition(resolve_runtime_path(snapshot, sleeve.universe.coarse_path))
    return universe.market


if __name__ == "__main__":
    raise SystemExit(main())
