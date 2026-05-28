from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from leaps_quant_engine.market_data import MarketDataError, MarketDataProvider
from leaps_quant_engine.models import Bar, DataResolution, Symbol


DEFAULT_PARQUET_DAILY_ROOT = Path("data/research/market_data/daily_bars")


@dataclass(slots=True)
class ParquetDailyBarProvider(MarketDataProvider):
    """Normalized daily bar provider backed by monthly Parquet files."""

    root: str | Path = DEFAULT_PARQUET_DAILY_ROOT

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        history = self.get_history(symbol)
        if not history:
            raise MarketDataError(f"No Parquet daily bars for {symbol.key}")
        return history[-1]

    def get_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        paths = tuple(self._candidate_paths(symbol, start=start, end=end))
        if not paths:
            raise MarketDataError(f"No Parquet daily files found for {symbol.key} under {self.root}")
        records: list[Mapping[str, Any]] = []
        for path in paths:
            records.extend(_read_parquet_records(path))
        bars = [
            _row_to_bar(symbol, row)
            for row in records
            if _row_matches_symbol(symbol, row)
        ]
        filtered = [
            bar
            for bar in bars
            if (start is None or bar.time >= start) and (end is None or bar.time <= end)
        ]
        return sorted(filtered, key=lambda bar: bar.time)

    def _candidate_paths(
        self,
        symbol: Symbol,
        *,
        start: datetime | None,
        end: datetime | None,
    ) -> Iterable[Path]:
        root = Path(self.root)
        market = _market_token(symbol.market)
        if start is None or end is None:
            yield from sorted(root.glob(f"{market}_*.parquet"))
            yield from sorted((root / f"market={market.upper()}").glob("year=*/month=*/*.parquet"))
            yield from sorted((root / f"market={market}").glob("year=*/month=*/*.parquet"))
            return
        seen: set[Path] = set()
        for year, month in _month_span(start.date(), end.date()):
            candidates = [
                root / f"{market}_{year:04d}_{month:02d}.parquet",
                root / f"{market.upper()}_{year:04d}_{month:02d}.parquet",
                root / f"market={market.upper()}" / f"year={year:04d}" / f"month={month:02d}" / "part-000.parquet",
                root / f"market={market}" / f"year={year:04d}" / f"month={month:02d}" / "part-000.parquet",
            ]
            for candidate in candidates:
                if candidate.exists() and candidate not in seen:
                    seen.add(candidate)
                    yield candidate
            for pattern_root in (
                root / f"market={market.upper()}" / f"year={year:04d}" / f"month={month:02d}",
                root / f"market={market}" / f"year={year:04d}" / f"month={month:02d}",
            ):
                for candidate in sorted(pattern_root.glob("*.parquet")):
                    if candidate.exists() and candidate not in seen:
                        seen.add(candidate)
                        yield candidate


def _read_parquet_records(path: Path) -> list[Mapping[str, Any]]:
    pandas = _load_pandas()
    try:
        frame = pandas.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 - preserve provider error context.
        raise MarketDataError(f"Failed to read Parquet daily bars from {path}: {exc}") from exc
    if hasattr(frame, "to_dict"):
        records = frame.to_dict(orient="records")
        return [row for row in records if isinstance(row, Mapping)]
    return [row for row in frame if isinstance(row, Mapping)]


def _row_matches_symbol(symbol: Symbol, row: Mapping[str, Any]) -> bool:
    row_symbol = str(row.get("symbol") or row.get("symbol_key") or "").strip().upper()
    if row_symbol in {symbol.key.upper(), symbol.ticker.upper()}:
        return True
    row_ticker = str(row.get("ticker") or "").strip().upper()
    row_market = str(row.get("market") or symbol.market).strip().upper()
    return row_ticker == symbol.ticker.upper() and row_market == symbol.market.upper()


def _row_to_bar(symbol: Symbol, row: Mapping[str, Any]) -> Bar:
    metadata = _metadata(row)
    return Bar(
        symbol=symbol,
        time=_row_time(row),
        open=_float_field(row, "open"),
        high=_float_field(row, "high"),
        low=_float_field(row, "low"),
        close=_float_field(row, "close"),
        volume=_int_field(row, "volume", default=0),
        resolution=str(row.get("resolution") or DataResolution.DAILY.value),
        metadata=metadata,
    )


def _row_time(row: Mapping[str, Any]) -> datetime:
    for key in ("time", "datetime", "timestamp"):
        value = row.get(key)
        if value not in (None, ""):
            return _parse_datetime(value)
    value = row.get("date")
    if value in (None, ""):
        raise MarketDataError("Parquet daily bar row requires date or time.")
    parsed_date = _parse_date(value)
    return datetime(parsed_date.year, parsed_date.month, parsed_date.day)


def _metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata_value = row.get("metadata")
    if isinstance(metadata_value, Mapping):
        metadata = dict(metadata_value)
    else:
        metadata_json = row.get("metadata_json")
        if metadata_json in (None, ""):
            metadata = {}
        else:
            try:
                loaded = json.loads(str(metadata_json))
            except json.JSONDecodeError:
                loaded = {}
            metadata = dict(loaded) if isinstance(loaded, Mapping) else {}
    for key in ("source", "adjusted", "collected_at"):
        if key in row and row.get(key) not in (None, ""):
            metadata.setdefault(key, row.get(key))
    return metadata


def _month_span(start: date, end: date) -> Iterable[tuple[int, int]]:
    current = date(start.year, start.month, 1)
    final = date(end.year, end.month, 1)
    while current <= final:
        yield current.year, current.month
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)


def _parse_datetime(value: Any) -> datetime:
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().replace(tzinfo=None)
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = str(value).strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d")
    return datetime.fromisoformat(text).replace(tzinfo=None)


def _parse_date(value: Any) -> date:
    return _parse_datetime(value).date()


def _float_field(row: Mapping[str, Any], key: str) -> float:
    try:
        return float(row[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise MarketDataError(f"Parquet daily bar row requires numeric {key}.") from exc


def _int_field(row: Mapping[str, Any], key: str, *, default: int) -> int:
    value = row.get(key, default)
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return default


def _market_token(market: str) -> str:
    return str(market or "KRX").strip().lower()


def _load_pandas() -> Any:
    try:
        import pandas as pd
    except ImportError as exc:
        raise MarketDataError(
            "pandas with a Parquet engine such as pyarrow is required for source='parquet-daily'."
        ) from exc
    return pd
