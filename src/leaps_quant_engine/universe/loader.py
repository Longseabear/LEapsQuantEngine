from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leaps_quant_engine.models import Symbol
from leaps_quant_engine.universe.definition import IndicatorDefinition, UniverseDefinition


def load_universe_definition(path: str | Path) -> UniverseDefinition:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_universe_definition(payload)


def parse_universe_definition(payload: dict[str, Any]) -> UniverseDefinition:
    market = str(payload["market"]).strip().upper()
    symbols, symbol_properties = _parse_symbols(payload.get("symbols", []), market)
    return UniverseDefinition(
        id=str(payload["id"]).strip(),
        market=market,
        symbols=symbols,
        indicators=tuple(_parse_indicator_definition(item) for item in payload.get("indicators", [])),
        tags=tuple(str(tag).strip() for tag in payload.get("tags", []) if str(tag).strip()),
        symbol_properties=symbol_properties,
    )


def _parse_symbols(items: list[Any], default_market: str) -> tuple[tuple[Symbol, ...], dict[str, dict[str, Any]]]:
    symbols: list[Symbol] = []
    properties_by_key: dict[str, dict[str, Any]] = {}
    for item in items:
        if isinstance(item, dict):
            ticker = str(item.get("ticker") or item.get("symbol") or "").strip().upper()
            if not ticker:
                raise ValueError("Universe symbol objects require 'ticker' or 'symbol'.")
            market = str(item.get("market") or default_market).strip().upper()
            symbol = Symbol(ticker, market)
            properties = {str(key): value for key, value in item.items() if key not in {"ticker", "symbol", "market"}}
            if properties:
                properties_by_key[symbol.key] = properties
        else:
            symbol = Symbol(str(item).strip().upper(), default_market)
        symbols.append(symbol)
    return tuple(symbols), properties_by_key


def _parse_indicator_definition(payload: dict[str, Any]) -> IndicatorDefinition:
    return IndicatorDefinition(
        name=str(payload["name"]).strip(),
        type=str(payload["type"]).strip(),
        period=int(payload["period"]),
        field=str(payload.get("field", "close")).strip() or "close",
        parameters=dict(payload.get("parameters") or {}),
    )
