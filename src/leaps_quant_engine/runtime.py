from __future__ import annotations

from importlib import import_module
from datetime import datetime
from pathlib import Path
from typing import Any

from leaps_quant_engine.algorithm import Algorithm
from leaps_quant_engine.config import PipelineConfig, SleeveConfig, load_pipeline_config
from leaps_quant_engine.data import single_bar_slice
from leaps_quant_engine.engine import Engine
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.models import OrderIntent, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.sleeve import Sleeve, SleevePolicy
from leaps_quant_engine.universe.loader import load_universe_definition


def build_engine_from_config(config: PipelineConfig) -> Engine:
    sleeves = [build_sleeve(item) for item in config.sleeves]
    return Engine(sleeves=sleeves)


def build_engine_from_file(path: str | Path) -> Engine:
    return build_engine_from_config(load_pipeline_config(path))


def build_indicator_engine_from_config(config: PipelineConfig) -> IndicatorEngine:
    indicator_engine = IndicatorEngine()
    for sleeve in config.sleeves:
        if sleeve.universe:
            indicator_engine.register_universe(sleeve.id, load_universe_definition(sleeve.universe))
    return indicator_engine


def build_indicator_engine_from_file(path: str | Path) -> IndicatorEngine:
    return build_indicator_engine_from_config(load_pipeline_config(path))


def run_once_from_config(config: PipelineConfig, time: datetime | None = None) -> list[OrderIntent]:
    if config.data is None or not config.data.sample_prices:
        raise ValueError("run_once requires data.sample_prices in the pipeline config")
    engine = build_engine_from_config(config)
    prices: dict[Symbol, float] = {}
    for sleeve in config.sleeves:
        for ticker in sleeve.symbols:
            if ticker in config.data.sample_prices:
                prices[Symbol(ticker, sleeve.market)] = config.data.sample_prices[ticker]
    if not prices:
        raise ValueError("data.sample_prices did not match any configured sleeve symbols")
    feed = [single_bar_slice(time or datetime.now(), prices)]
    return engine.run(feed).orders


def run_once_from_file(path: str | Path, time: datetime | None = None) -> list[OrderIntent]:
    return run_once_from_config(load_pipeline_config(path), time=time)


def build_sleeve(config: SleeveConfig) -> Sleeve:
    algorithm = load_algorithm(config)
    return Sleeve(
        id=config.id,
        algorithm=algorithm,
        portfolio=Portfolio(cash=config.cash),
        policy=SleevePolicy(max_position_pct=config.max_position_pct),
    )


def load_algorithm(config: SleeveConfig) -> Algorithm:
    module_name, class_name = _split_import_path(config.algorithm)
    module = import_module(_normalize_module_name(module_name))
    algorithm_cls = getattr(module, class_name)
    kwargs = dict(config.parameters or {})
    if "symbols" not in kwargs and config.symbols:
        kwargs["symbols"] = [Symbol(ticker, config.market) for ticker in config.symbols]
    instance = algorithm_cls(**kwargs)
    if not isinstance(instance, Algorithm):
        raise TypeError(f"{config.algorithm} did not create an Algorithm instance")
    return instance


def _split_import_path(value: str) -> tuple[str, str]:
    if ":" not in value:
        raise ValueError(f"Algorithm path must use module:Class format: {value}")
    module_name, class_name = value.split(":", 1)
    return module_name, class_name


def _normalize_module_name(module_name: str) -> str:
    if module_name.startswith("leaps_quant_engine."):
        return module_name
    return f"leaps_quant_engine.{module_name}"
