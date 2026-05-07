from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import csv

from leaps_quant_engine.engine import Engine
from leaps_quant_engine.market_data import MarketDataError, MarketDataProvider
from leaps_quant_engine.models import Bar, DataSlice, OrderIntent, Symbol


@dataclass(slots=True)
class VirtualMarketDataProvider(MarketDataProvider):
    """In-memory market data provider for deterministic backtests."""

    history: dict[str, list[Bar]] = field(default_factory=dict)

    @classmethod
    def from_bars(cls, bars: list[Bar]) -> "VirtualMarketDataProvider":
        provider = cls()
        for bar in bars:
            provider.add_bar(bar)
        return provider

    @classmethod
    def from_csv(
        cls,
        path: str | Path,
        *,
        symbol: Symbol,
        time_column: str = "time",
    ) -> "VirtualMarketDataProvider":
        bars: list[Bar] = []
        with Path(path).open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                bars.append(
                    Bar(
                        symbol=symbol,
                        time=_parse_datetime(row[time_column]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=int(float(row.get("volume") or 0)),
                    )
                )
        return cls.from_bars(bars)

    def add_bar(self, bar: Bar) -> None:
        bars = self.history.setdefault(bar.symbol.key, [])
        bars.append(bar)
        bars.sort(key=lambda item: item.time)

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        bars = self.history.get(symbol.key) or []
        if not bars:
            raise MarketDataError(f"No virtual bars for {symbol.key}")
        return bars[-1]

    def get_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        bars = self.history.get(symbol.key) or []
        return [
            bar
            for bar in bars
            if (start is None or bar.time >= start) and (end is None or bar.time <= end)
        ]


@dataclass(frozen=True, slots=True)
class BacktestResult:
    orders: list[OrderIntent]
    final_cash_by_sleeve: dict[str, float]
    final_quantity_by_sleeve: dict[str, dict[str, int]]


def build_replay_feed(
    provider: MarketDataProvider,
    symbols: list[Symbol],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[DataSlice]:
    bars_by_symbol = {symbol.key: provider.get_history(symbol, start=start, end=end) for symbol in symbols}
    times = sorted({bar.time for bars in bars_by_symbol.values() for bar in bars})
    feed: list[DataSlice] = []
    for time in times:
        bars = {
            symbol_key: bar
            for symbol_key, series in bars_by_symbol.items()
            for bar in series
            if bar.time == time
        }
        if bars:
            feed.append(DataSlice(time=time, bars=bars))
    return feed


def run_backtest(
    engine: Engine,
    provider: MarketDataProvider,
    symbols: list[Symbol],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> BacktestResult:
    feed = build_replay_feed(provider, symbols, start=start, end=end)
    result = engine.run(feed, fill_immediately=True)
    return BacktestResult(
        orders=result.orders,
        final_cash_by_sleeve={sleeve.id: sleeve.portfolio.cash for sleeve in engine.sleeves},
        final_quantity_by_sleeve={
            sleeve.id: {
                key: holding.quantity
                for key, holding in sleeve.portfolio.holdings.items()
            }
            for sleeve in engine.sleeves
        },
    )


def _parse_datetime(value: str) -> datetime:
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d")
    return datetime.fromisoformat(text)
