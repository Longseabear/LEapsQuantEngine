from __future__ import annotations

from collections.abc import Callable

from leaps_quant_engine.indicators.core import Indicator
from leaps_quant_engine.indicators.price import (
    AverageTrueRange,
    BarReturn,
    CloseLocationValue,
    Drawdown,
    ExponentialMovingAverage,
    GapPercent,
    HighLowRangePercent,
    Identity,
    MedianPrice,
    Momentum,
    RollingMaximum,
    RollingMinimum,
    RollingRange,
    RollingReturnStandardDeviation,
    RollingStandardDeviation,
    RollingVariance,
    SimpleMovingAverage,
    TrueRange,
    TypicalPrice,
    WeightedClose,
    ZScore,
)
from leaps_quant_engine.indicators.volume import (
    AccumulationDistribution,
    MoneyFlowVolume,
    OnBalanceVolume,
    PriceVolumeTrend,
    RollingDollarVolume,
    RollingVolume,
    Volume,
    VolumeMomentum,
    VolumeRatio,
    VolumeWeightedAveragePrice,
)
from leaps_quant_engine.universe.definition import IndicatorDefinition


IndicatorBuilder = Callable[[IndicatorDefinition], Indicator]


def supported_indicator_types() -> tuple[str, ...]:
    return tuple(sorted(_BUILDERS))


def create_indicator(definition: IndicatorDefinition) -> Indicator:
    indicator_type = definition.type.strip().lower()
    builder = _BUILDERS.get(indicator_type)
    if builder is not None:
        return builder(definition)
    raise ValueError(f"Unsupported indicator type: {definition.type}")


def _period(definition: IndicatorDefinition) -> int:
    return definition.period


def _field(definition: IndicatorDefinition) -> str:
    return definition.field


def _name(definition: IndicatorDefinition) -> str:
    return definition.name


_BUILDERS: dict[str, IndicatorBuilder] = {
    "identity": lambda d: Identity(field=_field(d), name=_name(d)),
    "price": lambda d: Identity(field=_field(d), name=_name(d)),
    "open": lambda d: Identity(field="open", name=_name(d)),
    "high": lambda d: Identity(field="high", name=_name(d)),
    "low": lambda d: Identity(field="low", name=_name(d)),
    "close": lambda d: Identity(field="close", name=_name(d)),
    "sma": lambda d: SimpleMovingAverage(_period(d), field=_field(d), name=_name(d)),
    "simple_moving_average": lambda d: SimpleMovingAverage(_period(d), field=_field(d), name=_name(d)),
    "ema": lambda d: ExponentialMovingAverage(_period(d), field=_field(d), name=_name(d)),
    "exponential_moving_average": lambda d: ExponentialMovingAverage(_period(d), field=_field(d), name=_name(d)),
    "momentum": lambda d: Momentum(_period(d), field=_field(d), name=_name(d)),
    "roc": lambda d: Momentum(_period(d), field=_field(d), name=_name(d)),
    "rate_of_change": lambda d: Momentum(_period(d), field=_field(d), name=_name(d)),
    "rolling_min": lambda d: RollingMinimum(_period(d), field=_field(d), name=_name(d)),
    "min": lambda d: RollingMinimum(_period(d), field=_field(d), name=_name(d)),
    "rolling_max": lambda d: RollingMaximum(_period(d), field=_field(d), name=_name(d)),
    "max": lambda d: RollingMaximum(_period(d), field=_field(d), name=_name(d)),
    "rolling_range": lambda d: RollingRange(_period(d), field=_field(d), name=_name(d)),
    "range": lambda d: RollingRange(_period(d), field=_field(d), name=_name(d)),
    "variance": lambda d: RollingVariance(_period(d), field=_field(d), name=_name(d)),
    "rolling_variance": lambda d: RollingVariance(_period(d), field=_field(d), name=_name(d)),
    "std": lambda d: RollingStandardDeviation(_period(d), field=_field(d), name=_name(d)),
    "stddev": lambda d: RollingStandardDeviation(_period(d), field=_field(d), name=_name(d)),
    "standard_deviation": lambda d: RollingStandardDeviation(_period(d), field=_field(d), name=_name(d)),
    "return_stddev": lambda d: RollingReturnStandardDeviation(_period(d), field=_field(d), name=_name(d)),
    "rolling_return_stddev": lambda d: RollingReturnStandardDeviation(_period(d), field=_field(d), name=_name(d)),
    "return_volatility": lambda d: RollingReturnStandardDeviation(_period(d), field=_field(d), name=_name(d)),
    "zscore": lambda d: ZScore(_period(d), field=_field(d), name=_name(d)),
    "typical_price": lambda d: TypicalPrice(name=_name(d)),
    "median_price": lambda d: MedianPrice(name=_name(d)),
    "weighted_close": lambda d: WeightedClose(name=_name(d)),
    "true_range": lambda d: TrueRange(name=_name(d)),
    "atr": lambda d: AverageTrueRange(_period(d), name=_name(d)),
    "average_true_range": lambda d: AverageTrueRange(_period(d), name=_name(d)),
    "gap_percent": lambda d: GapPercent(name=_name(d)),
    "bar_return": lambda d: BarReturn(field=_field(d), name=_name(d)),
    "return": lambda d: BarReturn(field=_field(d), name=_name(d)),
    "high_low_range_percent": lambda d: HighLowRangePercent(name=_name(d)),
    "close_location_value": lambda d: CloseLocationValue(name=_name(d)),
    "clv": lambda d: CloseLocationValue(name=_name(d)),
    "drawdown": lambda d: Drawdown(_period(d), field=_field(d), name=_name(d)),
    "volume": lambda d: Volume(name=_name(d)),
    "rolling_volume": lambda d: RollingVolume(_period(d), name=_name(d)),
    "volume_sma": lambda d: RollingVolume(_period(d), name=_name(d)),
    "rolling_dollar_volume": lambda d: RollingDollarVolume(_period(d), name=_name(d)),
    "dollar_volume": lambda d: RollingDollarVolume(_period(d), name=_name(d)),
    "volume_momentum": lambda d: VolumeMomentum(_period(d), name=_name(d)),
    "volume_roc": lambda d: VolumeMomentum(_period(d), name=_name(d)),
    "volume_ratio": lambda d: VolumeRatio(_period(d), name=_name(d)),
    "vwap": lambda d: VolumeWeightedAveragePrice(_period(d), name=_name(d)),
    "obv": lambda d: OnBalanceVolume(name=_name(d)),
    "on_balance_volume": lambda d: OnBalanceVolume(name=_name(d)),
    "pvt": lambda d: PriceVolumeTrend(name=_name(d)),
    "price_volume_trend": lambda d: PriceVolumeTrend(name=_name(d)),
    "accumulation_distribution": lambda d: AccumulationDistribution(name=_name(d)),
    "ad": lambda d: AccumulationDistribution(name=_name(d)),
    "money_flow_volume": lambda d: MoneyFlowVolume(name=_name(d)),
}
