from __future__ import annotations

from datetime import datetime

from leaps_quant_engine.models import Bar, DataSlice, Symbol


def single_bar_slice(time: datetime, prices: dict[Symbol, float]) -> DataSlice:
    bars = {
        symbol.key: Bar(
            symbol=symbol,
            time=time,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=0,
        )
        for symbol, price in prices.items()
    }
    return DataSlice(time=time, bars=bars)
