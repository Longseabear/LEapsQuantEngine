from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class EngineConfig:
    mode: str
    timezone: str


@dataclass(frozen=True, slots=True)
class SleeveConfig:
    id: str
    cash: float
    algorithm: str
    max_position_pct: float = 1.0
    symbols: tuple[str, ...] = ()
    universe: str | None = None
    market: str = "KRX"
    parameters: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DataConfig:
    sample_prices: dict[str, float]


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    engine: EngineConfig
    sleeves: tuple[SleeveConfig, ...]
    data: DataConfig | None = None


def load_pipeline_config(path: str | Path) -> PipelineConfig:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return parse_pipeline_config(payload)


def parse_pipeline_config(payload: dict[str, Any]) -> PipelineConfig:
    engine_payload = payload["engine"]
    sleeves_payload = payload["sleeves"]
    return PipelineConfig(
        engine=EngineConfig(
            mode=engine_payload["mode"],
            timezone=engine_payload["timezone"],
        ),
        sleeves=tuple(_parse_sleeve_config(item) for item in sleeves_payload),
        data=_parse_data_config(payload.get("data")),
    )


def _parse_sleeve_config(payload: dict[str, Any]) -> SleeveConfig:
    return SleeveConfig(
        id=payload["id"],
        cash=float(payload["cash"]),
        algorithm=payload["algorithm"],
        max_position_pct=float(payload.get("max_position_pct", 1.0)),
        symbols=tuple(payload.get("symbols", ())),
        universe=payload.get("universe"),
        market=payload.get("market", "KRX"),
        parameters=payload.get("parameters"),
    )


def _parse_data_config(payload: dict[str, Any] | None) -> DataConfig | None:
    if payload is None:
        return None
    return DataConfig(
        sample_prices={ticker: float(price) for ticker, price in payload.get("sample_prices", {}).items()}
    )
