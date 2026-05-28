from __future__ import annotations

import math

from leaps_quant_engine.indicators.core import Indicator, RollingWindow
from leaps_quant_engine.models import Bar


class Identity(Indicator):
    def __init__(self, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(name or f"{field}", warmup_period=1)
        self.field = field

    def compute_next_value(self, bar: Bar) -> float | None:
        return _bar_value(bar, self.field)


class SimpleMovingAverage(Indicator):
    def __init__(self, period: int, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(name or f"sma_{period}_{field}", warmup_period=period)
        self.period = period
        self.field = field
        self.window: RollingWindow[float] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        self.window.add(_bar_value(bar, self.field))
        if not self.window.is_ready:
            return None
        return sum(self.window.values) / self.period


class Momentum(Indicator):
    def __init__(self, period: int, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(name or f"momentum_{period}_{field}", warmup_period=period + 1)
        self.period = period
        self.field = field
        self.window: RollingWindow[float] = RollingWindow(period + 1)

    def compute_next_value(self, bar: Bar) -> float | None:
        self.window.add(_bar_value(bar, self.field))
        if not self.window.is_ready:
            return None
        first = self.window[0]
        if first == 0:
            return None
        return (self.window[-1] / first) - 1.0


class ExponentialMovingAverage(Indicator):
    def __init__(self, period: int, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(name or f"ema_{period}_{field}", warmup_period=period)
        self.period = period
        self.field = field
        self.multiplier = 2.0 / (period + 1.0)
        self._ema: float | None = None

    def compute_next_value(self, bar: Bar) -> float | None:
        value = _bar_value(bar, self.field)
        self._ema = value if self._ema is None else (value - self._ema) * self.multiplier + self._ema
        if self.samples < self.warmup_period:
            return None
        return self._ema


class RollingMinimum(Indicator):
    def __init__(self, period: int, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(name or f"min_{period}_{field}", warmup_period=period)
        self.field = field
        self.window: RollingWindow[float] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        self.window.add(_bar_value(bar, self.field))
        return min(self.window.values) if self.window.is_ready else None


class RollingMaximum(Indicator):
    def __init__(self, period: int, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(name or f"max_{period}_{field}", warmup_period=period)
        self.field = field
        self.window: RollingWindow[float] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        self.window.add(_bar_value(bar, self.field))
        return max(self.window.values) if self.window.is_ready else None


class RollingRange(Indicator):
    def __init__(self, period: int, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(name or f"range_{period}_{field}", warmup_period=period)
        self.field = field
        self.window: RollingWindow[float] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        self.window.add(_bar_value(bar, self.field))
        if not self.window.is_ready:
            return None
        return max(self.window.values) - min(self.window.values)


class RollingVariance(Indicator):
    def __init__(self, period: int, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(name or f"variance_{period}_{field}", warmup_period=period)
        self.field = field
        self.window: RollingWindow[float] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        self.window.add(_bar_value(bar, self.field))
        if not self.window.is_ready:
            return None
        mean = sum(self.window.values) / len(self.window)
        return sum((value - mean) ** 2 for value in self.window.values) / len(self.window)


class RollingStandardDeviation(RollingVariance):
    def __init__(self, period: int, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(period, field=field, name=name or f"std_{period}_{field}")

    def compute_next_value(self, bar: Bar) -> float | None:
        variance = super().compute_next_value(bar)
        return math.sqrt(variance) if variance is not None else None


class RollingReturnStandardDeviation(Indicator):
    def __init__(self, period: int, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(name or f"return_stddev_{period}_{field}", warmup_period=period + 1)
        self.period = period
        self.field = field
        self.previous: float | None = None
        self.window: RollingWindow[float] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        value = _bar_value(bar, self.field)
        previous = self.previous
        self.previous = value
        if previous in (None, 0):
            return None
        self.window.add((value / previous) - 1.0)
        if not self.window.is_ready:
            return None
        mean = sum(self.window.values) / len(self.window)
        variance = sum((item - mean) ** 2 for item in self.window.values) / len(self.window)
        return math.sqrt(variance)


class ZScore(Indicator):
    def __init__(self, period: int, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(name or f"zscore_{period}_{field}", warmup_period=period)
        self.field = field
        self.window: RollingWindow[float] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        value = _bar_value(bar, self.field)
        self.window.add(value)
        if not self.window.is_ready:
            return None
        mean = sum(self.window.values) / len(self.window)
        variance = sum((item - mean) ** 2 for item in self.window.values) / len(self.window)
        std = math.sqrt(variance)
        return 0.0 if std == 0 else (value - mean) / std


class TypicalPrice(Indicator):
    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name or "typical_price", warmup_period=1)

    def compute_next_value(self, bar: Bar) -> float | None:
        return (bar.high + bar.low + bar.close) / 3.0


class MedianPrice(Indicator):
    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name or "median_price", warmup_period=1)

    def compute_next_value(self, bar: Bar) -> float | None:
        return (bar.high + bar.low) / 2.0


class WeightedClose(Indicator):
    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name or "weighted_close", warmup_period=1)

    def compute_next_value(self, bar: Bar) -> float | None:
        return (bar.high + bar.low + (2 * bar.close)) / 4.0


class TrueRange(Indicator):
    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name or "true_range", warmup_period=1)
        self.previous_close: float | None = None

    def compute_next_value(self, bar: Bar) -> float | None:
        candidates = [bar.high - bar.low]
        if self.previous_close is not None:
            candidates.extend([abs(bar.high - self.previous_close), abs(bar.low - self.previous_close)])
        self.previous_close = bar.close
        return max(candidates)


class AverageTrueRange(Indicator):
    def __init__(self, period: int, *, name: str | None = None) -> None:
        super().__init__(name or f"atr_{period}", warmup_period=period)
        self.true_range = TrueRange()
        self.window: RollingWindow[float] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        point = self.true_range.update(bar)
        if point is None:
            return None
        self.window.add(point.value)
        return sum(self.window.values) / len(self.window) if self.window.is_ready else None


class GapPercent(Indicator):
    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name or "gap_percent", warmup_period=2)
        self.previous_close: float | None = None

    def compute_next_value(self, bar: Bar) -> float | None:
        previous_close = self.previous_close
        self.previous_close = bar.close
        if previous_close in (None, 0):
            return None
        return (bar.open / previous_close) - 1.0


class BarReturn(Indicator):
    def __init__(self, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(name or f"return_1_{field}", warmup_period=2)
        self.field = field
        self.previous: float | None = None

    def compute_next_value(self, bar: Bar) -> float | None:
        value = _bar_value(bar, self.field)
        previous = self.previous
        self.previous = value
        if previous in (None, 0):
            return None
        return (value / previous) - 1.0


class HighLowRangePercent(Indicator):
    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name or "high_low_range_percent", warmup_period=1)

    def compute_next_value(self, bar: Bar) -> float | None:
        return 0.0 if bar.close == 0 else (bar.high - bar.low) / bar.close


class CloseLocationValue(Indicator):
    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name or "close_location_value", warmup_period=1)

    def compute_next_value(self, bar: Bar) -> float | None:
        spread = bar.high - bar.low
        if spread == 0:
            return 0.0
        return ((bar.close - bar.low) - (bar.high - bar.close)) / spread


class Drawdown(Indicator):
    def __init__(self, period: int, *, field: str = "close", name: str | None = None) -> None:
        super().__init__(name or f"drawdown_{period}_{field}", warmup_period=period)
        self.field = field
        self.window: RollingWindow[float] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        value = _bar_value(bar, self.field)
        self.window.add(value)
        if not self.window.is_ready:
            return None
        peak = max(self.window.values)
        return 0.0 if peak == 0 else (value / peak) - 1.0


def _bar_value(bar: Bar, field: str) -> float:
    try:
        return float(getattr(bar, field))
    except AttributeError as exc:
        raise ValueError(f"Bar has no field '{field}'.") from exc
