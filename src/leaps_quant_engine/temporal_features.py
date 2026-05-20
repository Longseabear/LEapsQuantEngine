from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

import numpy as np

from leaps_quant_engine.history import get_daily_history
from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.models import Bar, DataResolution, DataSlice, Symbol
from leaps_quant_engine.rl.portfolio_constructor import (
    V2_TEMPORAL_FEATURE_SCHEMA,
    V2_TEMPORAL_RESIDUAL_FEATURE_SCHEMA,
    _asset_feature_arrays_at_index,
    _clip_feature,
    _effective_temporal_lookback,
    _feature_schema_name,
    _is_temporal_feature_schema,
    _observation_fields,
    _residual_asset_feature_arrays_at_index,
)
from leaps_quant_engine.snapshots import IndicatorSnapshot


@dataclass(frozen=True, slots=True)
class TemporalFeatureWindowConfig:
    feature_schema: str = V2_TEMPORAL_FEATURE_SCHEMA
    lookback_window: int = 64
    metadata_key: str = "rl_temporal_features"
    max_history_bars: int | None = None

    def __post_init__(self) -> None:
        schema = _feature_schema_name(self.feature_schema)
        if not _is_temporal_feature_schema(schema):
            raise ValueError(f"Temporal feature window requires a temporal feature_schema: {self.feature_schema!r}")
        if self.lookback_window < 1:
            raise ValueError("lookback_window must be positive.")
        if not str(self.metadata_key or "").strip():
            raise ValueError("metadata_key cannot be empty.")
        if self.max_history_bars is not None and self.max_history_bars < self.required_history_bars:
            raise ValueError("max_history_bars must be at least required_history_bars.")
        object.__setattr__(self, "feature_schema", schema)
        object.__setattr__(self, "metadata_key", str(self.metadata_key).strip())

    @property
    def effective_lookback(self) -> int:
        return _effective_temporal_lookback(self.feature_schema, self.lookback_window)

    @property
    def required_history_bars(self) -> int:
        return self.effective_lookback + _feature_warmup_bars(self.feature_schema)

    @property
    def history_keep_bars(self) -> int:
        return self.max_history_bars or max(self.required_history_bars + 20, self.effective_lookback * 3)


@dataclass(slots=True)
class TemporalFeatureWindowProvider:
    config: TemporalFeatureWindowConfig
    history_by_symbol: dict[str, list[Bar]] | None = None

    def __post_init__(self) -> None:
        self.history_by_symbol = {
            symbol_key: _dedupe_sort_daily_bars(bars, max_bars=self.config.history_keep_bars)
            for symbol_key, bars in dict(self.history_by_symbol or {}).items()
        }

    def update(self, data: DataSlice | Iterable[Bar]) -> None:
        if isinstance(data, DataSlice):
            bars = list(data.bars.values())
            slice_resolution = data.resolution
        else:
            bars = list(data)
            slice_resolution = DataResolution.ANY.value
        for bar in bars:
            if not _is_daily_bar(bar, slice_resolution=slice_resolution):
                continue
            self._append_bar(bar)

    def warm_up_from_provider(
        self,
        provider: MarketDataProvider,
        symbols: Iterable[Symbol],
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        refresh_history: bool = False,
    ) -> int:
        resolved_end = end or datetime.now()
        resolved_start = start or (resolved_end - timedelta(days=max(self.config.required_history_bars * 3, 240)))
        loaded = 0
        for symbol in symbols:
            try:
                bars = get_daily_history(
                    provider,
                    symbol,
                    start=resolved_start,
                    end=resolved_end,
                    refresh_history=refresh_history,
                )
            except Exception:  # noqa: BLE001 - feature windows fail closed per symbol.
                continue
            self.update(bars)
            loaded += len(bars)
        return loaded

    def metadata_for_snapshot(self, snapshot: IndicatorSnapshot) -> dict[str, dict[str, Any]]:
        windows = self.windows(
            snapshot.symbols,
            as_of=snapshot.as_of,
        )
        metadata: dict[str, dict[str, Any]] = {
            symbol_key: dict(snapshot.metadata(symbol_key))
            for symbol_key in snapshot.symbols
        }
        for symbol_key, rows in windows.items():
            if not rows:
                continue
            payload = metadata.setdefault(symbol_key, {})
            payload[self.config.metadata_key] = rows
            payload["rl_temporal_feature_schema"] = self.config.feature_schema
            payload["rl_temporal_feature_lookback"] = self.config.effective_lookback
            payload["rl_temporal_feature_as_of"] = snapshot.as_of.isoformat()
            payload["rl_temporal_feature_source"] = "daily_history_window"
        return metadata

    def windows(
        self,
        symbol_keys: Iterable[str],
        *,
        as_of: datetime,
    ) -> dict[str, list[dict[str, float]]]:
        keys = tuple(dict.fromkeys(str(symbol_key) for symbol_key in symbol_keys))
        if not keys:
            return {}
        if self.config.feature_schema == V2_TEMPORAL_RESIDUAL_FEATURE_SCHEMA:
            return self._residual_windows(keys, as_of=as_of)
        return {
            symbol_key: rows
            for symbol_key in keys
            if (rows := self._basic_window(symbol_key, as_of=as_of)) is not None
        }

    def _append_bar(self, bar: Bar) -> None:
        if bar.close <= 0:
            return
        history = self.history_by_symbol.setdefault(bar.symbol.key, [])
        history.append(replace(bar, resolution=DataResolution.DAILY.value))
        self.history_by_symbol[bar.symbol.key] = _dedupe_sort_daily_bars(
            history,
            max_bars=self.config.history_keep_bars,
        )

    def _basic_window(self, symbol_key: str, *, as_of: datetime) -> list[dict[str, float]] | None:
        bars = _bars_through(self.history_by_symbol.get(symbol_key, ()), as_of=as_of)
        if len(bars) < self.config.required_history_bars:
            return None
        prices = np.asarray([bar.close for bar in bars], dtype=np.float64)
        if not np.all(prices > 0):
            return None
        lookback = self.config.effective_lookback
        first_index = len(prices) - lookback
        if first_index < _feature_warmup_bars(self.config.feature_schema):
            return None
        rows: list[dict[str, float]] = []
        fields = _observation_fields(self.config.feature_schema)
        for day_index in range(first_index, len(prices)):
            momentum_20, realized_vol, return_5, return_1, drawdown, rank_score = _asset_feature_arrays_at_index(
                prices.reshape(-1, 1),
                day_index,
            )
            rows.append(
                _row_from_values(
                    fields,
                    {
                        "selected_flag": 1.0,
                        "momentum_20": float(momentum_20[0]),
                        "volatility_20": float(realized_vol[0]),
                        "return_5": float(return_5[0]),
                        "return_1": float(return_1[0]),
                        "drawdown_20": float(drawdown[0]),
                        "rank_score": float(rank_score[0]),
                    },
                )
            )
        return rows

    def _residual_windows(self, symbol_keys: tuple[str, ...], *, as_of: datetime) -> dict[str, list[dict[str, float]]]:
        eligible_keys = tuple(
            symbol_key
            for symbol_key in symbol_keys
            if len(_bars_through(self.history_by_symbol.get(symbol_key, ()), as_of=as_of))
            >= self.config.required_history_bars
        )
        if not eligible_keys:
            return {}
        dated_prices = _common_dated_price_matrix(
            {
                symbol_key: _bars_through(self.history_by_symbol.get(symbol_key, ()), as_of=as_of)
                for symbol_key in eligible_keys
            },
            symbol_keys=eligible_keys,
        )
        if dated_prices is None:
            return {}
        _dates, price_matrix = dated_prices
        if price_matrix.shape[0] < self.config.required_history_bars:
            return {}
        lookback = self.config.effective_lookback
        first_index = price_matrix.shape[0] - lookback
        if first_index < _feature_warmup_bars(self.config.feature_schema):
            return {}
        fields = _observation_fields(self.config.feature_schema)
        result = {symbol_key: [] for symbol_key in eligible_keys}
        for day_index in range(first_index, price_matrix.shape[0]):
            (
                momentum_20,
                residual_momentum_20,
                market_beta_60,
                realized_vol,
                return_5,
                return_1,
                drawdown,
                trend_quality_20,
                rank_score,
            ) = _residual_asset_feature_arrays_at_index(price_matrix, day_index)
            for column, symbol_key in enumerate(eligible_keys):
                result[symbol_key].append(
                    _row_from_values(
                        fields,
                        {
                            "selected_flag": 1.0,
                            "momentum_20": float(momentum_20[column]),
                            "residual_momentum_20": float(residual_momentum_20[column]),
                            "market_beta_60": float(market_beta_60[column]),
                            "volatility_20": float(realized_vol[column]),
                            "return_5": float(return_5[column]),
                            "return_1": float(return_1[column]),
                            "drawdown_20": float(drawdown[column]),
                            "trend_quality_20": float(trend_quality_20[column]),
                            "rank_score": float(rank_score[column]),
                        },
                    )
                )
        return result


def enrich_indicator_snapshot_with_temporal_features(
    snapshot: IndicatorSnapshot,
    provider: TemporalFeatureWindowProvider | None,
) -> IndicatorSnapshot:
    if provider is None:
        return snapshot
    return replace(snapshot, symbol_metadata=provider.metadata_for_snapshot(snapshot))


def temporal_feature_provider_from_portfolio_parameters(
    parameters: Mapping[str, Any],
) -> TemporalFeatureWindowProvider | None:
    schema_value = parameters.get("feature_schema")
    if schema_value is None:
        return None
    schema = _feature_schema_name(str(schema_value))
    if not _is_temporal_feature_schema(schema):
        return None
    return TemporalFeatureWindowProvider(
        TemporalFeatureWindowConfig(
            feature_schema=schema,
            lookback_window=int(parameters.get("lookback_window", 20)),
            metadata_key=str(parameters.get("temporal_feature_metadata_key", "rl_temporal_features")),
        )
    )


def _row_from_values(fields: tuple[str, ...], values: Mapping[str, float]) -> dict[str, float]:
    return {
        field: float(_clip_feature(values.get(field, 0.0)))
        for field in fields
    }


def _feature_warmup_bars(feature_schema: str) -> int:
    return 60 if _feature_schema_name(feature_schema) == V2_TEMPORAL_RESIDUAL_FEATURE_SCHEMA else 20


def _is_daily_bar(bar: Bar, *, slice_resolution: str) -> bool:
    resolution = str(bar.resolution or slice_resolution or DataResolution.ANY.value).strip().lower()
    if resolution in {DataResolution.MINUTE.value, DataResolution.LIVE.value, DataResolution.QUOTE.value}:
        return False
    return resolution in {
        DataResolution.ANY.value,
        DataResolution.DAILY.value,
        DataResolution.DAILY_CONFIRMED.value,
        "",
    }


def _bars_through(bars: Iterable[Bar], *, as_of: datetime) -> list[Bar]:
    cutoff = as_of.date()
    return [
        bar
        for bar in _dedupe_sort_daily_bars(bars)
        if bar.close > 0 and bar.time.date() <= cutoff
    ]


def _dedupe_sort_daily_bars(bars: Iterable[Bar], *, max_bars: int | None = None) -> list[Bar]:
    by_date: dict[Any, Bar] = {}
    for bar in bars:
        by_date[bar.time.date()] = bar
    ordered = [by_date[date] for date in sorted(by_date)]
    if max_bars is not None and len(ordered) > max_bars:
        return ordered[-max_bars:]
    return ordered


def _common_dated_price_matrix(
    history_by_symbol: Mapping[str, list[Bar]],
    *,
    symbol_keys: tuple[str, ...],
) -> tuple[list[Any], np.ndarray] | None:
    prices_by_symbol: dict[str, dict[Any, float]] = {}
    common_dates: set[Any] | None = None
    for symbol_key in symbol_keys:
        dated = {bar.time.date(): float(bar.close) for bar in history_by_symbol.get(symbol_key, []) if bar.close > 0}
        if not dated:
            return None
        prices_by_symbol[symbol_key] = dated
        dates = set(dated)
        common_dates = dates if common_dates is None else common_dates & dates
    if not common_dates:
        return None
    dates = sorted(common_dates)
    matrix = np.asarray(
        [
            [prices_by_symbol[symbol_key][date] for symbol_key in symbol_keys]
            for date in dates
        ],
        dtype=np.float64,
    )
    return dates, matrix
