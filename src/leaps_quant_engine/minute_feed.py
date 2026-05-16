from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
import csv
import gzip
import json
import time
from typing import Any, Mapping, Protocol
from zoneinfo import ZoneInfo

from leaps_quant_engine.market_rules import synthetic_domestic_market_session, synthetic_us_market_session
from leaps_quant_engine.models import Bar, DataResolution, Symbol
from leaps_quant_engine.universe.definition import UniverseDefinition


MINUTE_FEED_COLUMNS = ("symbol", "time", "open", "high", "low", "close", "volume")
MINUTE_FEED_SESSION_COLUMNS = (
    "market_session_scope",
    "market_session_phase",
    "is_regular_market_open",
    "is_orderable_session",
    "is_extended_market_hours",
    "session_source",
)


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


@dataclass(frozen=True, slots=True)
class MinuteFeedCacheBuildReport:
    status: str
    provider: str
    cache_root: str
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
    day_files: tuple[str, ...]
    empty_symbols: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "provider": self.provider,
            "cache_root": self.cache_root,
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
            "day_files": list(self.day_files),
            "empty_symbols": list(self.empty_symbols),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class MinuteFeedCacheExportReport:
    status: str
    cache_root: str
    output_path: str
    universe_id: str
    market: str
    requested_symbol_count: int
    exported_symbol_count: int
    row_count: int
    start: datetime
    end: datetime
    symbols: tuple[str, ...]
    source_files: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "cache_root": self.cache_root,
            "output_path": self.output_path,
            "universe_id": self.universe_id,
            "market": self.market,
            "requested_symbol_count": self.requested_symbol_count,
            "exported_symbol_count": self.exported_symbol_count,
            "row_count": self.row_count,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "symbols": list(self.symbols),
            "source_files": list(self.source_files),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class MinuteFeedCacheLoadReport:
    status: str
    cache_root: str
    universe_id: str
    market: str
    requested_symbol_count: int
    loaded_symbol_count: int
    row_count: int
    start: datetime
    end: datetime
    symbols: tuple[str, ...]
    source_files: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "cache_root": self.cache_root,
            "universe_id": self.universe_id,
            "market": self.market,
            "requested_symbol_count": self.requested_symbol_count,
            "loaded_symbol_count": self.loaded_symbol_count,
            "row_count": self.row_count,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "symbols": list(self.symbols),
            "source_files": list(self.source_files),
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
    annotate_sessions: bool = True
    sleep_seconds: float = 0.0
    max_request_days: int = 6
    yfinance_symbol_by_key: Mapping[str, str] = field(default_factory=dict)
    output_market_by_key: Mapping[str, str] = field(default_factory=dict)
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

        request_ticker = self.yfinance_symbol_by_key.get(symbol.key, symbol.ticker)
        output_symbol = Symbol(
            symbol.ticker,
            self.output_market_by_key.get(symbol.key, symbol.market),
        )
        bars_by_time: dict[datetime, Bar] = {}
        for chunk_start, chunk_end in _chunk_time_range(start, end, max_days=self.max_request_days):
            if self.sleep_seconds > 0:
                time.sleep(self.sleep_seconds)
            frame = self._download_frame(yf, request_ticker, start=chunk_start, end=chunk_end, interval=interval)
            if frame is None or getattr(frame, "empty", True):
                if chunk_start.date() != chunk_end.date():
                    for retry_start, retry_end in _chunk_time_range(chunk_start, chunk_end, max_days=1):
                        if self.sleep_seconds > 0:
                            time.sleep(self.sleep_seconds)
                        retry_frame = self._download_frame(
                            yf,
                            request_ticker,
                            start=retry_start,
                            end=retry_end,
                            interval=interval,
                        )
                        if retry_frame is None or getattr(retry_frame, "empty", True):
                            continue
                        for bar in _bars_from_yfinance_frame(
                            retry_frame,
                            symbol=output_symbol,
                            start=start,
                            end=end,
                            timezone=self.timezone,
                            series_ticker=request_ticker,
                            annotate_sessions=self.annotate_sessions,
                        ):
                            bars_by_time[bar.time] = bar
                continue
            for bar in _bars_from_yfinance_frame(
                frame,
                symbol=output_symbol,
                start=start,
                end=end,
                timezone=self.timezone,
                series_ticker=request_ticker,
                annotate_sessions=self.annotate_sessions,
            ):
                bars_by_time[bar.time] = bar
        return [bars_by_time[time_key] for time_key in sorted(bars_by_time)]

    def _download_frame(self, yf_module, ticker: str, *, start: datetime, end: datetime, interval: str):
        request_start = start.date().isoformat()
        request_end = (end.date() + timedelta(days=1)).isoformat()
        return yf_module.download(
            ticker,
            start=request_start,
            end=request_end,
            interval=interval,
            auto_adjust=False,
            prepost=self.include_prepost,
            progress=False,
            threads=False,
        )


@dataclass(slots=True)
class KISCachedMinuteBarProvider:
    """Minute provider backed by the engine's cache-first KIS adapter."""

    provider: Any
    refresh: bool = False
    daily_start_time: str | None = None
    daily_end_time: str | None = None
    provider_name: str = "kis-cache"

    def download(
        self,
        symbol: Symbol,
        *,
        start: datetime,
        end: datetime,
        interval: str,
    ) -> list[Bar]:
        interval_minutes = _interval_to_minutes(interval)
        bars: list[Bar] = []
        for day in _date_range(start, end):
            day_start = datetime.fromisoformat(day)
            day_end = day_start + timedelta(days=1) - timedelta(microseconds=1)
            if self.daily_start_time:
                day_start = _replace_date_time(day_start, self.daily_start_time)
            if self.daily_end_time:
                day_end = _replace_date_time(day_end, self.daily_end_time)
            request_start = max(start, day_start)
            request_end = min(end, day_end)
            if request_end < request_start:
                continue
            bars.extend(
                self.provider.get_cached_minute_history(
                    symbol,
                    trade_date=day_start,
                    start_time=request_start.strftime("%H:%M:%S"),
                    end_time=request_end.strftime("%H:%M:%S"),
                    interval_minutes=interval_minutes,
                    refresh=self.refresh,
                )
            )
        return [bar for bar in bars if start <= bar.time <= end]


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
    include_session_metadata: bool = False,
) -> MinuteFeedDownloadReport:
    return download_minute_feed(
        universe,
        provider=provider,
        output_path=output_path,
        start=start,
        end=end,
        interval=interval,
        timezone=timezone,
        symbols=symbols,
        overwrite=overwrite,
        include_session_metadata=include_session_metadata,
        allowed_markets=("US", "NAS", "NYS", "AMS"),
        request_symbol_market="US",
    )


def download_minute_feed(
    universe: UniverseDefinition,
    *,
    provider: MinuteBarProvider,
    output_path: str | Path,
    start: datetime,
    end: datetime,
    interval: str = "1m",
    timezone: str,
    symbols: tuple[str, ...] = (),
    overwrite: bool = False,
    include_session_metadata: bool = False,
    allowed_markets: tuple[str, ...] | None = None,
    request_symbol_market: str | None = None,
) -> MinuteFeedDownloadReport:
    destination = Path(output_path)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Minute feed already exists: {destination}. Pass --overwrite to replace it.")

    selected_symbols = _select_symbols(universe, symbols)
    all_bars: list[Bar] = []
    empty_symbols: list[str] = []
    warnings: list[str] = []

    for symbol in selected_symbols:
        if allowed_markets is not None and symbol.market.upper() not in {market.upper() for market in allowed_markets}:
            warnings.append(f"skip_non_matching_market:{symbol.key}")
            continue
        request_symbol = Symbol(symbol.ticker, request_symbol_market) if request_symbol_market else symbol
        try:
            bars = provider.download(request_symbol, start=start, end=end, interval=interval)
        except Exception as exc:
            warnings.append(f"download_failed:{symbol.key}:{exc}")
            empty_symbols.append(symbol.key)
            continue
        if not bars:
            empty_symbols.append(symbol.key)
            continue
        all_bars.extend(_annotate_bars_if_requested(bars, include_session_metadata=include_session_metadata, timezone=timezone))

    written_count = write_minute_feed_csv(destination, all_bars, include_session_metadata=include_session_metadata)
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


def build_minute_feed_cache(
    universe: UniverseDefinition,
    *,
    provider: MinuteBarProvider,
    cache_root: str | Path,
    start: datetime,
    end: datetime,
    interval: str = "1m",
    timezone: str,
    symbols: tuple[str, ...] = (),
    overwrite: bool = False,
    compress: bool = True,
    include_session_metadata: bool = False,
) -> MinuteFeedCacheBuildReport:
    cache_dir = _minute_cache_universe_dir(cache_root, universe.id)
    selected_symbols = _select_symbols(universe, symbols)
    bars_by_day: dict[str, list[Bar]] = {}
    empty_symbols: list[str] = []
    warnings: list[str] = []

    for symbol in selected_symbols:
        try:
            bars = provider.download(symbol, start=start, end=end, interval=interval)
        except Exception as exc:
            warnings.append(f"download_failed:{symbol.key}:{exc}")
            empty_symbols.append(symbol.key)
            continue
        if not bars:
            empty_symbols.append(symbol.key)
            continue
        for bar in _annotate_bars_if_requested(bars, include_session_metadata=include_session_metadata, timezone=timezone):
            if start <= bar.time <= end:
                bars_by_day.setdefault(bar.time.date().isoformat(), []).append(bar)

    for day in _date_range(start, end):
        if datetime.fromisoformat(day).weekday() < 5 and day not in bars_by_day:
            warnings.append(f"missing_weekday_cache_day:{day}")

    day_files: list[str] = []
    row_count = 0
    for day, bars in sorted(bars_by_day.items()):
        suffix = ".csv.gz" if compress else ".csv"
        path = cache_dir / f"{day}{suffix}"
        if path.exists() and not overwrite:
            raise FileExistsError(f"Minute cache day already exists: {path}. Pass --overwrite to replace it.")
        written = write_minute_feed_csv(path, bars, include_session_metadata=include_session_metadata)
        row_count += written
        day_files.append(str(path))
        _write_json(
            cache_dir / f"{day}.manifest.json",
            {
                "schema_version": "leaps_minute_cache_day.v1",
                "universe_id": universe.id,
                "market": universe.market,
                "provider": provider.provider_name,
                "date": day,
                "row_count": written,
                "symbol_count": len({bar.symbol.key for bar in bars}),
                "file": str(path),
                "interval": interval,
                "timezone": timezone,
                "include_session_metadata": include_session_metadata,
                "generated_at": datetime.now().isoformat(),
            },
        )

    downloaded_keys = tuple(sorted({bar.symbol.key for bars in bars_by_day.values() for bar in bars}))
    status = "ok" if row_count else "empty"
    if (warnings or empty_symbols) and row_count:
        status = "partial"
    report = MinuteFeedCacheBuildReport(
        status=status,
        provider=provider.provider_name,
        cache_root=str(cache_dir),
        universe_id=universe.id,
        market=universe.market,
        requested_symbol_count=len(selected_symbols),
        downloaded_symbol_count=len(downloaded_keys),
        row_count=row_count,
        start=start,
        end=end,
        interval=interval,
        timezone=timezone,
        symbols=downloaded_keys,
        day_files=tuple(day_files),
        empty_symbols=tuple(sorted(set(empty_symbols))),
        warnings=tuple(warnings),
    )
    _write_json(
        cache_dir / "manifest.json",
        {
            "schema_version": "leaps_minute_cache.v1",
            **report.to_dict(),
            "updated_at": datetime.now().isoformat(),
        },
    )
    return report


def export_minute_feed_cache(
    universe: UniverseDefinition,
    *,
    cache_root: str | Path,
    output_path: str | Path,
    start: datetime,
    end: datetime,
    symbols: tuple[str, ...] = (),
    overwrite: bool = False,
    include_session_metadata: bool = False,
) -> MinuteFeedCacheExportReport:
    destination = Path(output_path)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"Minute feed already exists: {destination}. Pass --overwrite to replace it.")
    bars, load_report = load_minute_feed_cache_bars(
        universe,
        cache_root=cache_root,
        start=start,
        end=end,
        symbols=symbols,
        include_session_metadata=include_session_metadata,
    )
    written = write_minute_feed_csv(destination, bars, include_session_metadata=include_session_metadata)
    status = "ok" if written else "empty"
    if load_report.warnings and written:
        status = "partial"
    return MinuteFeedCacheExportReport(
        status=status,
        cache_root=load_report.cache_root,
        output_path=str(destination),
        universe_id=load_report.universe_id,
        market=load_report.market,
        requested_symbol_count=load_report.requested_symbol_count,
        exported_symbol_count=load_report.loaded_symbol_count,
        row_count=written,
        start=start,
        end=end,
        symbols=load_report.symbols,
        source_files=load_report.source_files,
        warnings=load_report.warnings,
    )


def load_minute_feed_cache_bars(
    universe: UniverseDefinition,
    *,
    cache_root: str | Path,
    start: datetime,
    end: datetime,
    symbols: tuple[str, ...] = (),
    include_session_metadata: bool = False,
) -> tuple[list[Bar], MinuteFeedCacheLoadReport]:
    cache_dir = _minute_cache_universe_dir(cache_root, universe.id)
    selected_symbols = _select_symbols(universe, symbols)
    selected_keys = {symbol.key for symbol in selected_symbols}
    bars: list[Bar] = []
    source_files: list[str] = []
    warnings: list[str] = []

    for day in _date_range(start, end):
        source = _minute_cache_day_path(cache_dir, day)
        if source is None:
            if datetime.fromisoformat(day).weekday() < 5:
                warnings.append(f"missing_weekday_cache_day:{day}")
            continue
        source_files.append(str(source))
        for row in _read_minute_feed_rows(source):
            bar = _minute_cache_row_to_bar(row, default_market=universe.market)
            if bar.symbol.key not in selected_keys:
                continue
            if start <= bar.time <= end:
                bars.append(
                    annotate_minute_bar_session(bar, timezone=_default_timezone_for_market(bar.symbol.market))
                    if include_session_metadata
                    else bar
                )

    exported_keys = tuple(sorted({bar.symbol.key for bar in bars}))
    bars = sorted(bars, key=lambda bar: (bar.time, bar.symbol.key))
    status = "ok" if bars else "empty"
    if warnings and bars:
        status = "partial"
    return bars, MinuteFeedCacheLoadReport(
        status=status,
        cache_root=str(cache_dir),
        universe_id=universe.id,
        market=universe.market,
        requested_symbol_count=len(selected_symbols),
        loaded_symbol_count=len(exported_keys),
        row_count=len(bars),
        start=start,
        end=end,
        symbols=exported_keys,
        source_files=tuple(source_files),
        warnings=tuple(warnings),
    )


def write_minute_feed_csv(path: str | Path, bars: list[Bar], *, include_session_metadata: bool = False) -> int:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(bars, key=lambda bar: (bar.time, bar.symbol.key))
    columns = MINUTE_FEED_COLUMNS + (MINUTE_FEED_SESSION_COLUMNS if include_session_metadata else ())
    with _open_text_for_write(destination) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for bar in rows:
            row = {
                "symbol": bar.symbol.key,
                "time": bar.time.replace(tzinfo=None).isoformat(),
                "open": _format_number(bar.open),
                "high": _format_number(bar.high),
                "low": _format_number(bar.low),
                "close": _format_number(bar.close),
                "volume": str(int(bar.volume)),
            }
            if include_session_metadata:
                row.update(_session_columns_for_bar(bar))
            writer.writerow(row)
    return len(rows)


def annotate_minute_bar_session(bar: Bar, *, timezone: str | None = None) -> Bar:
    metadata = dict(bar.metadata)
    metadata.update(
        _minute_bar_session_metadata(
            symbol=bar.symbol,
            bar_time=bar.time,
            timezone=timezone or _default_timezone_for_market(bar.symbol.market),
        )
    )
    return replace(bar, metadata=metadata)


def yfinance_symbol_map_for_universe(universe: UniverseDefinition) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for symbol in universe.symbols:
        if symbol.market.upper() == "KRX":
            mapping[symbol.key] = f"{symbol.ticker}{_krx_yfinance_suffix(universe.properties_for(symbol))}"
    return mapping


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


def _minute_cache_universe_dir(cache_root: str | Path, universe_id: str) -> Path:
    safe_id = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in universe_id)
    return Path(cache_root) / safe_id


def _minute_cache_day_path(cache_dir: Path, day: str) -> Path | None:
    for suffix in (".csv.gz", ".csv"):
        candidate = cache_dir / f"{day}{suffix}"
        if candidate.exists():
            return candidate
    return None


def _date_range(start: datetime, end: datetime) -> tuple[str, ...]:
    days: list[str] = []
    cursor = start.date()
    final = end.date()
    while cursor <= final:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return tuple(days)


def _open_text_for_write(path: Path):
    if path.name.lower().endswith(".gz"):
        return gzip.open(path, "wt", encoding="utf-8", newline="")
    return path.open("w", encoding="utf-8", newline="")


def _open_text_for_read(path: Path):
    if path.name.lower().endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8-sig", newline="")
    return path.open("r", encoding="utf-8-sig", newline="")


def _read_minute_feed_rows(path: Path) -> list[Mapping[str, Any]]:
    with _open_text_for_read(path) as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _minute_cache_row_to_bar(row: Mapping[str, Any], *, default_market: str) -> Bar:
    symbol = _normalize_symbol_ref(str(row.get("symbol") or row.get("symbol_key") or row.get("ticker") or ""), default_market=default_market)
    bar_time = datetime.fromisoformat(str(row["time"]))
    return Bar(
        symbol=symbol,
        time=bar_time,
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=int(float(row.get("volume") or 0)),
        resolution=DataResolution.MINUTE.value,
        metadata=_minute_row_session_metadata(row, symbol=symbol, bar_time=bar_time),
    )


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _krx_yfinance_suffix(properties: Mapping[str, Any]) -> str:
    segment = str(properties.get("market_segment") or "").strip().upper()
    market_id = str(properties.get("market_id") or "").strip().upper()
    if segment == "KOSDAQ" or market_id == "KSQ":
        return ".KQ"
    return ".KS"


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
    series_ticker: str | None = None,
    annotate_sessions: bool = True,
) -> list[Bar]:
    ticker = series_ticker or symbol.ticker
    open_values = _series_for_field(frame, "Open", ticker)
    high_values = _series_for_field(frame, "High", ticker)
    low_values = _series_for_field(frame, "Low", ticker)
    close_values = _series_for_field(frame, "Close", ticker)
    volume_values = _series_for_field(frame, "Volume", ticker)

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
                metadata=_minute_bar_session_metadata(symbol=symbol, bar_time=bar_time, timezone=timezone)
                if annotate_sessions
                else {},
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


def _annotate_bars_if_requested(
    bars: list[Bar],
    *,
    include_session_metadata: bool,
    timezone: str,
) -> list[Bar]:
    if not include_session_metadata:
        return bars
    return [annotate_minute_bar_session(bar, timezone=timezone) for bar in bars]


def _session_columns_for_bar(bar: Bar) -> dict[str, str]:
    metadata = dict(bar.metadata)
    if not metadata.get("market_session_phase"):
        metadata.update(
            _minute_bar_session_metadata(
                symbol=bar.symbol,
                bar_time=bar.time,
                timezone=_default_timezone_for_market(bar.symbol.market),
            )
        )
    return {
        "market_session_scope": str(metadata.get("market_session_scope") or ""),
        "market_session_phase": str(metadata.get("market_session_phase") or ""),
        "is_regular_market_open": _bool_to_text(metadata.get("is_regular_market_open")),
        "is_orderable_session": _bool_to_text(metadata.get("is_orderable_session")),
        "is_extended_market_hours": _bool_to_text(metadata.get("is_extended_market_hours")),
        "session_source": str(metadata.get("session_source") or ""),
    }


def _minute_bar_session_metadata(*, symbol: Symbol, bar_time: datetime, timezone: str) -> dict[str, Any]:
    scope = _market_scope_for_symbol(symbol)
    if scope == "overseas":
        local_time = bar_time
        if local_time.tzinfo is None:
            local_time = local_time.replace(tzinfo=ZoneInfo(timezone or "America/New_York"))
        session = synthetic_us_market_session(local_time)
    else:
        session = synthetic_domestic_market_session(bar_time)
    return {
        "market_session_scope": session.market_scope,
        "market_session_phase": session.session_phase,
        "is_regular_market_open": session.is_regular_market_open,
        "is_orderable_session": session.is_orderable,
        "is_extended_market_hours": session.is_orderable and not session.is_regular_market_open,
        "session_source": session.source,
    }


def _minute_row_session_metadata(
    row: Mapping[str, Any],
    *,
    symbol: Symbol,
    bar_time: datetime,
) -> dict[str, Any]:
    phase = str(row.get("market_session_phase") or row.get("session_phase") or row.get("session") or "").strip()
    scope = str(row.get("market_session_scope") or row.get("market_scope") or "").strip()
    if not phase and not scope:
        return {}
    metadata: dict[str, Any] = {
        "market_session_phase": phase,
        "market_session_scope": scope or _market_scope_for_symbol(symbol),
    }
    for source_key, target_key in (
        ("is_regular_market_open", "is_regular_market_open"),
        ("is_orderable_session", "is_orderable_session"),
        ("is_extended_market_hours", "is_extended_market_hours"),
    ):
        if source_key in row and row[source_key] not in (None, ""):
            metadata[target_key] = _text_to_bool(row[source_key])
    source = str(row.get("session_source") or "").strip()
    if source:
        metadata["session_source"] = source
    if "is_regular_market_open" not in metadata or "is_orderable_session" not in metadata:
        inferred = _minute_bar_session_metadata(
            symbol=symbol,
            bar_time=bar_time,
            timezone=_default_timezone_for_market(symbol.market),
        )
        inferred.update(metadata)
        return inferred
    return metadata


def _market_scope_for_symbol(symbol: Symbol) -> str:
    market = symbol.market.strip().upper()
    if market in {"US", "NAS", "NYS", "NYSE", "NASDAQ", "AMEX", "AMS"}:
        return "overseas"
    return "domestic"


def _default_timezone_for_market(market: str) -> str:
    return "America/New_York" if market.strip().upper() in {"US", "NAS", "NYS", "NYSE", "NASDAQ", "AMEX", "AMS"} else "Asia/Seoul"


def _interval_to_minutes(interval: str) -> int:
    text = str(interval or "1m").strip().lower()
    if text.endswith("m"):
        text = text[:-1]
    try:
        value = int(text)
    except ValueError as exc:
        raise ValueError(f"Minute interval must be like '1m' or '5m': {interval!r}") from exc
    if value <= 0:
        raise ValueError("Minute interval must be positive.")
    return value


def _replace_date_time(day: datetime, value: str) -> datetime:
    text = str(value or "").strip().replace(":", "")
    if len(text) == 4:
        text += "00"
    if len(text) != 6 or not text.isdigit():
        raise ValueError(f"Invalid minute cache time bound: {value!r}")
    return day.replace(hour=int(text[0:2]), minute=int(text[2:4]), second=int(text[4:6]), microsecond=0)


def _bool_to_text(value: Any) -> str:
    return "true" if bool(value) else "false"


def _text_to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _format_number(value: float) -> str:
    return f"{float(value):.10g}"
