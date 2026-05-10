from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from leaps_quant_engine.market_data import MarketDataProvider
from leaps_quant_engine.models import Bar, Symbol


def get_daily_history(
    provider: MarketDataProvider,
    symbol: Symbol,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    refresh_history: bool = False,
) -> list[Bar]:
    get_cached_daily_history = getattr(provider, "get_cached_daily_history", None)
    if get_cached_daily_history is not None:
        return _as_daily_bars(
            get_cached_daily_history(
                symbol,
                start=start,
                end=end,
                refresh=refresh_history,
            )
        )
    if refresh_history:
        raise ValueError("refresh_history requires a provider with get_cached_daily_history().")
    return _as_daily_bars(provider.get_history(symbol, start=start, end=end))


def load_daily_history(
    provider: MarketDataProvider,
    symbols: list[Symbol],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    refresh_history: bool = False,
) -> dict[str, list[Bar]]:
    return {
        symbol.key: get_daily_history(
            provider,
            symbol,
            start=start,
            end=end,
            refresh_history=refresh_history,
        )
        for symbol in symbols
    }


def _as_daily_bars(bars: list[Bar] | tuple[Bar, ...]) -> list[Bar]:
    return [replace(bar, resolution="daily") if bar.resolution != "daily" else bar for bar in bars]
