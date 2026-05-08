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
from leaps_quant_engine.benchmark import run_daily_indicator_benchmark
from leaps_quant_engine.logging import configure_logging
from leaps_quant_engine.live_snapshot import run_live_indicator_snapshot
from leaps_quant_engine.models import OrderIntent
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.runtime import build_indicator_engine_from_file, run_once_from_file
from leaps_quant_engine.universe.loader import load_universe_definition


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

    live_indicators = subparsers.add_parser("live-indicators-once")
    live_indicators.add_argument("universe", type=Path)
    live_indicators.add_argument("--sleeve-id", required=True)
    live_indicators.add_argument("--source", default="market-data-engine", choices=("market-data-engine",))
    live_indicators.add_argument("--min-success", type=int)
    live_indicators.add_argument("--rate-limit-per-second", type=int)
    live_indicators.add_argument("--include-failures", action="store_true")

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


if __name__ == "__main__":
    raise SystemExit(main())
