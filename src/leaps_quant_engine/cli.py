from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Sequence

from leaps_quant_engine.adapters.kis import KISBrokerEngineMarketDataProvider
from leaps_quant_engine.models import OrderIntent
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.runtime import build_indicator_engine_from_file, run_once_from_file


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leapsq")
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

    args = parser.parse_args(argv)
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


if __name__ == "__main__":
    raise SystemExit(main())
