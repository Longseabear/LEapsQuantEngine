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
    exclusions = _parse_universe_exclusions(payload, market)
    symbols, symbol_properties = _parse_symbols(payload.get("symbols", []), market, exclusions=exclusions)
    return UniverseDefinition(
        id=str(payload["id"]).strip(),
        market=market,
        symbols=symbols,
        indicators=tuple(_parse_indicator_definition(item) for item in payload.get("indicators", [])),
        tags=tuple(str(tag).strip() for tag in payload.get("tags", []) if str(tag).strip()),
        symbol_properties=symbol_properties,
    )


def _parse_symbols(
    items: list[Any],
    default_market: str,
    *,
    exclusions: "_UniverseExclusions | None" = None,
) -> tuple[tuple[Symbol, ...], dict[str, dict[str, Any]]]:
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
            if exclusions is not None and exclusions.excludes(symbol, properties):
                continue
            if properties:
                properties_by_key[symbol.key] = properties
        else:
            symbol = Symbol(str(item).strip().upper(), default_market)
            if exclusions is not None and exclusions.excludes(symbol, {}):
                continue
        symbols.append(symbol)
    return tuple(symbols), properties_by_key


class _UniverseExclusions:
    def __init__(self, *, symbol_keys: set[str], name_rules: tuple[dict[str, tuple[str, ...]], ...]) -> None:
        self.symbol_keys = symbol_keys
        self.name_rules = name_rules

    def excludes(self, symbol: Symbol, properties: dict[str, Any]) -> bool:
        if symbol.key.upper() in self.symbol_keys or symbol.ticker.upper() in self.symbol_keys:
            return True
        text = " ".join(
            str(value)
            for key, value in properties.items()
            if key.lower() in {"name", "display_name", "korean_name", "english_name", "asset_name"}
        ).upper()
        if not text:
            return False
        for rule in self.name_rules:
            required = rule.get("all", ())
            optional = rule.get("any", ())
            if all(term in text for term in required) and (not optional or any(term in text for term in optional)):
                return True
        return False


def _parse_universe_exclusions(payload: dict[str, Any], default_market: str) -> _UniverseExclusions:
    metadata = payload.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    raw_symbols = (
        list(payload.get("operator_excluded_symbols") or [])
        + list(metadata.get("operator_excluded_symbols") or [])
        + list(payload.get("blacklisted_symbols") or [])
        + list(metadata.get("blacklisted_symbols") or [])
    )
    symbol_keys = {_normalize_excluded_symbol(value, default_market) for value in raw_symbols}
    symbol_keys.discard("")
    raw_rules = list(payload.get("operator_excluded_name_rules") or []) + list(
        metadata.get("operator_excluded_name_rules") or []
    )
    name_rules = tuple(_parse_name_rule(rule) for rule in raw_rules)
    return _UniverseExclusions(symbol_keys=symbol_keys, name_rules=name_rules)


def _normalize_excluded_symbol(value: Any, default_market: str) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if ":" in text:
        return text
    if text.isdigit():
        return Symbol(text, default_market).key.upper()
    return text


def _parse_name_rule(rule: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(rule, dict):
        raise ValueError("Universe operator_excluded_name_rules entries must be objects.")
    return {
        "all": tuple(_rule_terms(rule.get("all"))),
        "any": tuple(_rule_terms(rule.get("any"))),
    }


def _rule_terms(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip().upper(),) if value.strip() else ()
    return tuple(str(item).strip().upper() for item in value if str(item).strip())


def _parse_indicator_definition(payload: dict[str, Any]) -> IndicatorDefinition:
    return IndicatorDefinition(
        name=str(payload["name"]).strip(),
        type=str(payload["type"]).strip(),
        period=int(payload["period"]),
        field=str(payload.get("field", "close")).strip() or "close",
        resolution=str(payload.get("resolution", "daily")).strip().lower() or "daily",
        readiness=_parse_indicator_readiness(payload),
        parameters=dict(payload.get("parameters") or {}),
    )


def _parse_indicator_readiness(payload: dict[str, Any]) -> str:
    if "required_for_warmup" in payload:
        return "required" if _parse_bool(payload["required_for_warmup"]) else "optional"
    return str(payload.get("readiness", "required")).strip().lower() or "required"


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise ValueError(f"Expected a boolean required_for_warmup value, got {value!r}.")
