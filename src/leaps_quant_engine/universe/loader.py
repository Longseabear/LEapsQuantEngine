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
    return UniverseDefinition(
        id=str(payload["id"]).strip(),
        market=market,
        symbols=tuple(Symbol(str(ticker).strip().upper(), market) for ticker in payload["symbols"]),
        indicators=tuple(_parse_indicator_definition(item) for item in payload.get("indicators", [])),
        tags=tuple(str(tag).strip() for tag in payload.get("tags", []) if str(tag).strip()),
    )


def _parse_indicator_definition(payload: dict[str, Any]) -> IndicatorDefinition:
    return IndicatorDefinition(
        name=str(payload["name"]).strip(),
        type=str(payload["type"]).strip(),
        period=int(payload["period"]),
        field=str(payload.get("field", "close")).strip() or "close",
        parameters=dict(payload.get("parameters") or {}),
    )
