from __future__ import annotations

from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, Iterable, TypeVar

from leaps_quant_engine.models import Bar


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class IndicatorDataPoint:
    time: datetime
    value: float


class RollingWindow(Generic[T]):
    def __init__(self, size: int) -> None:
        if size <= 0:
            raise ValueError("RollingWindow size must be positive.")
        self.size = size
        self._values: deque[T] = deque(maxlen=size)

    def add(self, value: T) -> None:
        self._values.append(value)

    @property
    def is_ready(self) -> bool:
        return len(self._values) == self.size

    @property
    def count(self) -> int:
        return len(self._values)

    @property
    def values(self) -> tuple[T, ...]:
        return tuple(self._values)

    def __iter__(self) -> Iterable[T]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getitem__(self, index: int) -> T:
        return self.values[index]


class Indicator(ABC):
    def __init__(self, name: str, warmup_period: int) -> None:
        if warmup_period <= 0:
            raise ValueError("warmup_period must be positive.")
        self.name = name
        self.warmup_period = warmup_period
        self.samples = 0
        self.current: IndicatorDataPoint | None = None

    @property
    def is_ready(self) -> bool:
        return self.samples >= self.warmup_period and self.current is not None

    def update(self, bar: Bar) -> IndicatorDataPoint | None:
        self.samples += 1
        value = self.compute_next_value(bar)
        if value is None:
            self.current = None
            return None
        self.current = IndicatorDataPoint(time=bar.time, value=value)
        return self.current

    @abstractmethod
    def compute_next_value(self, bar: Bar) -> float | None:
        """Compute the next indicator value from a normalized bar."""
