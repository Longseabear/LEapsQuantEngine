from __future__ import annotations

from leaps_quant_engine.indicators.core import Indicator, RollingWindow
from leaps_quant_engine.models import Bar


class Volume(Indicator):
    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name or "volume", warmup_period=1)

    def compute_next_value(self, bar: Bar) -> float | None:
        return float(bar.volume)


class RollingVolume(Indicator):
    def __init__(self, period: int, *, name: str | None = None) -> None:
        super().__init__(name or f"volume_{period}", warmup_period=period)
        self.period = period
        self.window: RollingWindow[float] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        self.window.add(float(bar.volume))
        if not self.window.is_ready:
            return None
        return sum(self.window.values) / self.period


class RollingDollarVolume(Indicator):
    def __init__(self, period: int, *, name: str | None = None) -> None:
        super().__init__(name or f"dollar_volume_{period}", warmup_period=period)
        self.period = period
        self.window: RollingWindow[float] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        self.window.add(float(bar.close) * float(bar.volume))
        if not self.window.is_ready:
            return None
        return sum(self.window.values) / self.period


class VolumeMomentum(Indicator):
    def __init__(self, period: int, *, name: str | None = None) -> None:
        super().__init__(name or f"volume_momentum_{period}", warmup_period=period + 1)
        self.window: RollingWindow[float] = RollingWindow(period + 1)

    def compute_next_value(self, bar: Bar) -> float | None:
        self.window.add(float(bar.volume))
        if not self.window.is_ready:
            return None
        first = self.window[0]
        if first == 0:
            return None
        return (self.window[-1] / first) - 1.0


class VolumeRatio(Indicator):
    def __init__(self, period: int, *, name: str | None = None) -> None:
        super().__init__(name or f"volume_ratio_{period}", warmup_period=period)
        self.window: RollingWindow[float] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        current = float(bar.volume)
        self.window.add(current)
        if not self.window.is_ready:
            return None
        average = sum(self.window.values) / len(self.window)
        return None if average == 0 else current / average


class VolumeWeightedAveragePrice(Indicator):
    def __init__(self, period: int, *, name: str | None = None) -> None:
        super().__init__(name or f"vwap_{period}", warmup_period=period)
        self.window: RollingWindow[tuple[float, float]] = RollingWindow(period)

    def compute_next_value(self, bar: Bar) -> float | None:
        self.window.add((float(bar.close), float(bar.volume)))
        if not self.window.is_ready:
            return None
        total_volume = sum(volume for _, volume in self.window.values)
        if total_volume == 0:
            return None
        return sum(price * volume for price, volume in self.window.values) / total_volume


class OnBalanceVolume(Indicator):
    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name or "obv", warmup_period=2)
        self.previous_close: float | None = None
        self.obv = 0.0

    def compute_next_value(self, bar: Bar) -> float | None:
        if self.previous_close is None:
            self.previous_close = bar.close
            return None
        if bar.close > self.previous_close:
            self.obv += bar.volume
        elif bar.close < self.previous_close:
            self.obv -= bar.volume
        self.previous_close = bar.close
        return self.obv


class PriceVolumeTrend(Indicator):
    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name or "pvt", warmup_period=2)
        self.previous_close: float | None = None
        self.value = 0.0

    def compute_next_value(self, bar: Bar) -> float | None:
        if self.previous_close in (None, 0):
            self.previous_close = bar.close
            return None
        self.value += ((bar.close - self.previous_close) / self.previous_close) * bar.volume
        self.previous_close = bar.close
        return self.value


class AccumulationDistribution(Indicator):
    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name or "accumulation_distribution", warmup_period=1)
        self.value = 0.0

    def compute_next_value(self, bar: Bar) -> float | None:
        spread = bar.high - bar.low
        multiplier = 0.0 if spread == 0 else ((bar.close - bar.low) - (bar.high - bar.close)) / spread
        self.value += multiplier * bar.volume
        return self.value


class MoneyFlowVolume(Indicator):
    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name or "money_flow_volume", warmup_period=1)

    def compute_next_value(self, bar: Bar) -> float | None:
        spread = bar.high - bar.low
        multiplier = 0.0 if spread == 0 else ((bar.close - bar.low) - (bar.high - bar.close)) / spread
        return multiplier * bar.volume
