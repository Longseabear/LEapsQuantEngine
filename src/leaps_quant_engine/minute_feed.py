from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
import csv
import time
from typing import Protocol
from zoneinfo import ZoneInfo

from leaps_quant_engine.models import Bar, DataResolution, Symbol
from leaps_quant_engine.universe.definition import UniverseDefinition


MINUTE_FEED_COLUMNS = ("symbol", "time", "open", "high", "low", "close", "volume")


class MinuteBarProvider(Protocol):
    provider_name: str

    def download(
        self,
        symbol: Symbol,
        *,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> list[Bar]:
        ...


@dataclass(frozen=True, slots=True)
class MinuteFeedDownloadReport:
    status: str
    provider: str
    output_path: str
    universe_id: str
    market: str
    requested_symbol_count: int
    downloaded_symbol_count: int
    row_count: int
    start: datetime
    end: datetime
    interval: str
    timezone: str
    symbols: tuple[str, ...]
    empty_symbols: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "provider": self.provider,
            "output_path": self.output_path,
            "universe_id": self.universe_id,
            "market": self.market,
            "requested_symbol_count": self.requested_symbol_count,
            "downloaded_symbol_count": self.downloaded_symbol_count,
            "row_count": self.row_count,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "interval": self.interval,
            "timezone": self.timezone,
            "symbols": list(self.symbols),
            "empty_symbols": list(self.empty_symbols),
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class StaticMinuteBarProvider:
    """Test/helper provider that returns preloaded minute bars."""

    bars_by_symbol: dict[str, list[Bar]]
    provider_name: str = "static"

    def download(
        self,
        symbol: Symbol,
        *,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> list[Bar]:
        return [
            bar
            for bar in self.bars_by_symbol.get(symbol.key, ())
            if start <= bar.time <= end
        ]


@dataclass(slots=True)
class YFinanceMinuteBarProvider:
    timezone: str = "America/New_York"
    include_prepost: bool = False
    sleep_seconds: float = 0.0
    max_request_days: int = 6
    provider_name: str = "yfinance"

    def download(
        self,
        symbol: Symbol,
        *,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> list[Bar]:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise RuntimeError(
                "yfinance is required for --provider yfinance. Install the optional market data dependency first."
            ) from exc

        bars_by_time: dict[datetime, Bar] = {}
        for chunk_start, chunk_end in _chunk_time_range(start, end, max_days=self.max_request_days):
            if self.sleep_seconds > 0:
                time.sleep(self.sleep_seconds)
            request_start = chunk_start.date().isoformat()
            request_end = (chunk_end.date() + timedelta(days=1)).isoformat()
            frame = yf.download(
                symbol.ticker,
                start=request_start,
                end=request_end,
                interval=interval,
                auto_adjust=False,
                prepost=self.include_prepost,
                progress=False,
                threads=False,
            )
            if frame is None or getattr(frame, "empty", True):
                continue
            for bar in _bars_from_yfinance_frame(
                frame,
                symbol=Symbol(symbol.ticker, "US"),
                start=start,
                end=end,
                timezone=self.timezone,
            ):
                bars_by_time[bar.time] = bar
        return [bars_by_time[time_key] for time_key in sorted(bars_by_time)]


def download_us_minute_feed(
    universe: UniverseDefinition,
    *,
    provider: MinuteBarProvider,
    output_path: str | Path,
    start: datetime,
    end: datetime,
    interval: str = "1m",
    timezone: str = "America/New_York",
    symbols: tuple[str, ...] = (),
    overwrite: bool = False,
) -> MinuteFeedDownloadReport:
    destination = Path(output_path)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Minute feed already exists: {destination}. Pass --overwrite to replace it.")

    selected_symbols = _select_symbols(universe, symbols)
    all_bars: list[Bar] = []
    empty_symbols: list[str] = []
    warnings: list[str] = []

    for symbol in selected_symbols:
        if symbol.market.upper() not in {"US", "NAS", "NYS", "AMS"}:
            warnings.append(f"skip_non_us_symbol:{symbol.key}")
            continue
        try:
            bars = provider.download(Symbol(symbol.ticker, "US"), start=start, end=end, interval=interval)
        except Exception as exc:
            warnings.append(f"download_failed:{symbol.key}:{exc}")
            empty_symbols.append(Symbol(symbol.ticker, "US").key)
            continue
        if not bars:
            empty_symbols.append(Symbol(symbol.ticker, "US").key)
            continue
        all_bars.extend(bars)

    written_count = write_minute_feed_csv(destination, all_bars)
    downloaded_keys = tuple(sorted({bar.symbol.key for bar in all_bars}))
    status = "ok" if written_count else "empty"
    if (warnings or empty_symbols) and written_count:
        status = "partial"
    return MinuteFeedDownloadReport(
        status=status,
        provider=provider.provider_name,
        output_path=str(destination),
        universe_id=universe.id,
        market=universe.market,
        requested_symbol_count=len(selected_symbols),
        downloaded_symbol_count=len(downloaded_keys),
        row_count=written_count,
        start=start,
        end=end,
        interval=interval,
        timezone=timezone,
        symbols=downloaded_keys,
        empty_symbols=tuple(sorted(set(empty_symbols))),
        warnings=tuple(warnings),
    )


def write_minute_feed_csv(path: str | Path, bars: list[Bar]) -> int:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(bars, key=lambda bar: (bar.time, bar.symbol.key))
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MINUTE_FEED_COLUMNS)
        writer.writeheader()
        for bar in rows:
            writer.writerow(
                {
                    "symbol": bar.symbol.key,
                    "time": bar.time.replace(tzinfo=None).isoformat(),
                    "open": _format_number(bar.open),
                    "high": _format_number(bar.high),
                    "low": _format_number(bar.low),
                    "close": _format_number(bar.close),
                    "volume": str(int(bar.volume)),
                }
            )
    return len(rows)


def _select_symbols(universe: UniverseDefinition, requested: tuple[str, ...]) -> tuple[Symbol, ...]:
    if not requested:
        return universe.symbols
    requested_keys = {_normalize_symbol_ref(value, default_market=universe.market).key for value in requested}
    selected = tuple(symbol for symbol in universe.symbols if Symbol(symbol.ticker, "US").key in requested_keys or symbol.key in requested_keys)
    missing = sorted(requested_keys - {Symbol(symbol.ticker, "US").key for symbol in selected} - {symbol.key for symbol in selected})
    if missing:
        raise ValueError(f"Requested symbols are not in universe {universe.id}: {', '.join(missing)}")
    return selected


def _normalize_symbol_ref(value: str, *, default_market: str) -> Symbol:
    text = str(value).strip().upper()
    if ":" in text:
        market, ticker = text.split(":", 1)
        return Symbol(ticker=ticker.strip(), market=market.strip())
    return Symbol(ticker=text, market=default_market.strip().upper() or "US")


def _normalize_timestamp(value, *, timezone: str) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        value = datetime.fromisoformat(str(value))
    if value.tzinfo is not None:
        return value.astimezone(ZoneInfo(timezone)).replace(tzinfo=None)
    return value.replace(tzinfo=None)


def _chunk_time_range(start: datetime, end: datetime, *, max_days: int):
    if end < start:
        return
    chunk_days = max(int(max_days), 1)
    step = timedelta(days=chunk_days)
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + step - timedelta(microseconds=1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(microseconds=1)


def _bars_from_yfinance_frame(
    frame,
    *,
    symbol: Symbol,
    start: datetime,
    end: datetime,
    timezone: str,
) -> list[Bar]:
    open_values = _series_for_field(frame, "Open", symbol.ticker)
    high_values = _series_for_field(frame, "High", symbol.ticker)
    low_values = _series_for_field(frame, "Low", symbol.ticker)
    close_values = _series_for_field(frame, "Close", symbol.ticker)
    volume_values = _series_for_field(frame, "Volume", symbol.ticker)

    bars: list[Bar] = []
    for raw_time, close in close_values.items():
        if _is_missing(close):
            continue
        bar_time = _normalize_timestamp(raw_time, timezone=timezone)
        if bar_time < start or bar_time > end:
            continue
        bars.append(
            Bar(
                symbol=symbol,
                time=bar_time,
                open=float(open_values.get(raw_time, close)),
                high=float(high_values.get(raw_time, close)),
                low=float(low_values.get(raw_time, close)),
                close=float(close),
                volume=int(float(volume_values.get(raw_time, 0) or 0)),
                resolution=DataResolution.MINUTE.value,
            )
        )
    return bars


def _series_for_field(frame, field: str, ticker: str):
    columns = getattr(frame, "columns", None)
    if columns is not None and hasattr(columns, "nlevels") and columns.nlevels > 1:
        for key in ((ticker, field), (field, ticker)):
            if key in columns:
                return frame[key]
        for level in range(columns.nlevels):
            try:
                subset = frame.xs(field, axis=1, level=level)
            except (KeyError, ValueError):
                continue
            if hasattr(subset, "columns"):
                if ticker in subset.columns:
                    return subset[ticker]
                if len(subset.columns) == 1:
                    return subset.iloc[:, 0]
            return subset
    if field in frame:
        return frame[field]
    raise ValueError(f"Downloaded minute data is missing {field!r} column.")


def _is_missing(value) -> bool:
    return value is None or value != value


def _format_number(value: float) -> str:
    return f"{float(value):.10g}"
