from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from leaps_quant_engine.market_data import MarketDataError, MarketDataProvider
from leaps_quant_engine.models import Bar, Symbol


@dataclass(slots=True)
class FinanceDataReaderMarketDataProvider(MarketDataProvider):
    """Daily historical provider for long-horizon backtests."""

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        history = self.get_history(symbol)
        if not history:
            raise MarketDataError(f"No FinanceDataReader bars for {symbol.key}")
        return history[-1]

    def get_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        fdr = _load_finance_datareader()
        df = fdr.DataReader(
            _reader_symbol(symbol),
            start.strftime("%Y-%m-%d") if start else None,
            end.strftime("%Y-%m-%d") if end else None,
        )
        bars: list[Bar] = []
        for index, row in df.iterrows():
            time = index.to_pydatetime() if hasattr(index, "to_pydatetime") else datetime.fromisoformat(str(index))
            bars.append(
                Bar(
                    symbol=symbol,
                    time=time.replace(tzinfo=None),
                    open=float(row.get("Open")),
                    high=float(row.get("High")),
                    low=float(row.get("Low")),
                    close=float(row.get("Close")),
                    volume=int(float(row.get("Volume") or 0)),
                )
            )
        return bars


def _load_finance_datareader() -> Any:
    try:
        import FinanceDataReader as fdr
    except ImportError as exc:
        raise MarketDataError(
            "FinanceDataReader is required for source='finance-datareader'. Install FinanceDataReader first."
        ) from exc
    return fdr


def _reader_symbol(symbol: Symbol) -> str:
    return symbol.ticker
