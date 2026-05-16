from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
import io
import json
import math
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Mapping

from leaps_quant_engine.fundamentals import FundamentalSnapshot, PointInTimeFundamentalStore
from leaps_quant_engine.market_data import MarketDataError, MarketDataProvider
from leaps_quant_engine.models import Bar, Symbol


FDR_FUNDAMENTAL_FIELD_MAP: Mapping[str, str] = {
    "Close": "last_price",
    "Open": "day_open",
    "High": "day_high",
    "Low": "day_low",
    "Volume": "volume",
    "Amount": "turnover_krw",
    "Marcap": "market_cap",
    "MarCap": "market_cap",
    "Stocks": "listed_shares",
    "PER": "per",
    "PBR": "pbr",
    "EPS": "eps",
    "BPS": "bps",
    "DPS": "dps",
    "DIV": "dividend_yield",
    "ChangeRatio": "change_pct",
    "ChagesRatio": "change_pct",
    "market_cap": "market_cap",
    "sales": "sales",
    "operating_profit": "operating_profit",
    "net_income": "net_income",
    "per": "per",
    "pbr": "pbr",
    "eps": "eps",
    "bps": "bps",
    "dps": "dps",
    "dividend_yield": "dividend_yield",
    "roe": "roe",
    "roa": "roa",
    "foreign_ownership_pct": "foreign_ownership_pct",
}

NAVER_MARKET_SUM_URL = "https://finance.naver.com/sise/sise_market_sum.nhn"
NAVER_MARKET_SUM_FIELD_SETS: Mapping[str, str] = {
    "profitability": "12|06108810",
    "roe": "12|01882048",
    "roa": "12|00441424",
    "valuation": "12|00234202",
    "dividend": "12|00000181",
}
NAVER_MARKET_PAGE_LIMITS: Mapping[int, int] = {0: 32, 1: 29}
NAVER_MARKET_SUM_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "profitability": (
        "rank",
        "name",
        "last_price",
        "change_text",
        "change_pct",
        "par_value",
        "volume",
        "turnover_mkrw",
        "bid_price",
        "market_cap_100m",
        "operating_profit_100m",
        "per",
        "discussion",
    ),
    "roe": (
        "rank",
        "name",
        "last_price",
        "change_text",
        "change_pct",
        "par_value",
        "previous_volume",
        "open_price",
        "ask_price",
        "assets_100m",
        "operating_profit_growth_pct",
        "roe",
        "discussion",
    ),
    "roa": (
        "rank",
        "name",
        "last_price",
        "change_text",
        "change_pct",
        "par_value",
        "day_high",
        "bid_total",
        "liabilities_100m",
        "net_income_100m",
        "foreign_ownership_pct",
        "roa",
        "discussion",
    ),
    "valuation": (
        "rank",
        "name",
        "last_price",
        "change_text",
        "change_pct",
        "par_value",
        "day_low",
        "ask_total",
        "listed_shares_1000",
        "sales_100m",
        "eps",
        "pbr",
        "discussion",
    ),
    "dividend": (
        "rank",
        "name",
        "last_price",
        "change_text",
        "change_pct",
        "par_value",
        "dps",
        "sales_growth_pct",
        "retention_ratio",
        "discussion",
        "extra_1",
        "extra_2",
        "extra_3",
    ),
}


@dataclass(slots=True)
class FinanceDataReaderMarketDataProvider(MarketDataProvider):
    """Daily historical provider for long-horizon backtests."""

    cache_root: str | Path | None = Path("data/runtime/cache/finance-datareader/daily")
    cache_enabled: bool = True

    def get_latest_bar(self, symbol: Symbol) -> Bar:
        history = self.get_history(symbol)
        if not history:
            raise MarketDataError(f"No FinanceDataReader bars for {symbol.key}")
        return history[-1]

    def get_cached_daily_history(
        self,
        symbol: Symbol,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        refresh: bool = False,
    ) -> list[Bar]:
        if not self.cache_enabled or self.cache_root is None:
            return self.get_history(symbol, start=start, end=end)
        cache_path = _history_cache_path(self.cache_root, symbol, start=start, end=end)
        if not refresh:
            cached = _read_history_cache(cache_path, symbol)
            if cached is not None:
                return cached
        bars = self.get_history(symbol, start=start, end=end)
        _write_history_cache(cache_path, symbol, bars=bars, start=start, end=end)
        return bars

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


def _history_cache_path(
    root: str | Path,
    symbol: Symbol,
    *,
    start: datetime | None,
    end: datetime | None,
) -> Path:
    safe_market = _safe_path_token(symbol.market.upper())
    safe_ticker = _safe_path_token(symbol.ticker.upper())
    start_key = _date_key(start)
    end_key = _date_key(end)
    return Path(root) / safe_market / safe_ticker / f"{start_key}_{end_key}.json"


def _read_history_cache(path: Path, symbol: Symbol) -> list[Bar] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    if payload.get("schema_version") != "finance_datareader_daily_history.v1":
        return None
    bars_payload = payload.get("bars")
    if not isinstance(bars_payload, list):
        return None
    bars: list[Bar] = []
    try:
        for row in bars_payload:
            if not isinstance(row, Mapping):
                return None
            bars.append(
                Bar(
                    symbol=symbol,
                    time=datetime.fromisoformat(str(row["time"])),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=int(float(row.get("volume") or 0)),
                    resolution="daily",
                )
            )
    except (KeyError, TypeError, ValueError):
        return None
    return bars


def _write_history_cache(
    path: Path,
    symbol: Symbol,
    *,
    bars: list[Bar],
    start: datetime | None,
    end: datetime | None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "finance_datareader_daily_history.v1",
        "provider": "FinanceDataReader",
        "symbol": symbol.key,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "generated_at": datetime.now().isoformat(),
        "bar_count": len(bars),
        "bars": [
            {
                "time": bar.time.replace(tzinfo=None).isoformat(),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": int(bar.volume),
            }
            for bar in bars
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def _date_key(value: datetime | None) -> str:
    return value.strftime("%Y%m%d") if value is not None else "none"


def _safe_path_token(value: str) -> str:
    return re.sub(r"[^A-Z0-9._-]+", "_", str(value or "").upper()).strip("_") or "unknown"


@dataclass(slots=True)
class FinanceDataReaderFundamentalProvider:
    """Load FDR listing/fundamental snapshot values into the point-in-time store.

    FinanceDataReader's KRX listing is a current snapshot source, not a historical
    point-in-time fundamentals database. Pass the snapshot date explicitly so
    backtests do not accidentally see values before they were imported.
    """

    market: str = "KRX"
    include_naver_valuation: bool = False
    valuation_loader: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None
    field_map: Mapping[str, str] = field(default_factory=lambda: dict(FDR_FUNDAMENTAL_FIELD_MAP))

    def load_to_store(
        self,
        store: PointInTimeFundamentalStore | None = None,
        *,
        symbols: Iterable[Symbol | str] | None = None,
        as_of: datetime | None = None,
        names: Iterable[str] | None = None,
        include_naver_valuation: bool | None = None,
    ) -> PointInTimeFundamentalStore:
        resolved_store = store or PointInTimeFundamentalStore()
        resolved_as_of = as_of or datetime.now()
        allowed_names = _normalize_name_set(names)
        for symbol, values in self.current_values(
            symbols=symbols,
            as_of=resolved_as_of,
            names=allowed_names,
            include_naver_valuation=include_naver_valuation,
        ).items():
            for name, value in values.items():
                resolved_store.add(
                    symbol,
                    name,
                    value,
                    as_of=resolved_as_of,
                    reported_at=resolved_as_of,
                    effective_at=resolved_as_of,
                    source=self._source(include_naver_valuation),
                    metadata={"provider": "FinanceDataReader", "market": self.market},
                )
        return resolved_store

    def snapshot(
        self,
        *,
        sleeve_id: str,
        universe_id: str | None,
        symbols: tuple[Symbol, ...] | list[Symbol],
        as_of: datetime | None = None,
        names: Iterable[str] | None = None,
        include_naver_valuation: bool | None = None,
    ) -> FundamentalSnapshot:
        resolved_as_of = as_of or datetime.now()
        resolved_names = tuple(names) if names is not None else None
        store = self.load_to_store(
            symbols=symbols,
            as_of=resolved_as_of,
            names=resolved_names,
            include_naver_valuation=include_naver_valuation,
        )
        return store.snapshot(
            sleeve_id=sleeve_id,
            universe_id=universe_id,
            symbols=symbols,
            as_of=resolved_as_of,
            names=resolved_names,
            source_snapshot_id=self._source(include_naver_valuation),
            created_at=resolved_as_of,
        )

    def current_values(
        self,
        *,
        symbols: Iterable[Symbol | str] | None = None,
        as_of: datetime | None = None,
        names: Iterable[str] | None = None,
        include_naver_valuation: bool | None = None,
    ) -> dict[Symbol, dict[str, float]]:
        _ = as_of or datetime.now()
        allowed_symbols = _normalize_symbol_filter(symbols)
        allowed_names = _normalize_name_set(names)
        records = self._listing_records(include_naver_valuation=include_naver_valuation)
        values_by_symbol: dict[Symbol, dict[str, float]] = {}
        for row in records:
            ticker = _normalize_ticker(row.get("Code") or row.get("Symbol") or row.get("symbol"))
            if not ticker:
                continue
            symbol = Symbol(ticker, self.market)
            if allowed_symbols is not None and symbol.key.upper() not in allowed_symbols and ticker.upper() not in allowed_symbols:
                continue
            values = self._fundamental_values_from_row(row, allowed_names=allowed_names)
            if values:
                values_by_symbol[symbol] = values
        return values_by_symbol

    def _listing_records(self, *, include_naver_valuation: bool | None) -> list[dict[str, Any]]:
        fdr = _load_finance_datareader()
        frame = fdr.StockListing(self.market)
        records = _frame_to_records(frame)
        if not self._should_include_naver(include_naver_valuation):
            return records
        valuation_payload = dict(self.valuation_loader() if self.valuation_loader is not None else fetch_naver_market_sum_valuation())
        enriched: list[dict[str, Any]] = []
        for row in records:
            ticker = _normalize_ticker(row.get("Code") or row.get("Symbol") or row.get("symbol"))
            payload = dict(row)
            if ticker and ticker in valuation_payload:
                payload.update(dict(valuation_payload[ticker]))
            enriched.append(payload)
        return enriched

    def _fundamental_values_from_row(
        self,
        row: Mapping[str, Any],
        *,
        allowed_names: set[str] | None,
    ) -> dict[str, float]:
        values: dict[str, float] = {}
        for provider_field, fundamental_name in self.field_map.items():
            normalized_name = _normalize_name(fundamental_name)
            if allowed_names is not None and normalized_name not in allowed_names:
                continue
            if provider_field not in row:
                continue
            value = _safe_float(row.get(provider_field))
            if value is None:
                continue
            values[normalized_name] = value
        return values

    def _should_include_naver(self, include_naver_valuation: bool | None) -> bool:
        return self.include_naver_valuation if include_naver_valuation is None else include_naver_valuation

    def source_name(self, include_naver_valuation: bool | None = None) -> str:
        return self._source(include_naver_valuation)

    def _source(self, include_naver_valuation: bool | None) -> str:
        source = f"FinanceDataReader:StockListing({self.market})"
        if self._should_include_naver(include_naver_valuation):
            return f"{source}+Naver:MarketSum"
        return source


@dataclass(slots=True)
class _ValuationRow:
    symbol: str
    current_price: float | None = None
    market_cap: int | None = None
    sales: int | None = None
    operating_profit: int | None = None
    net_income: int | None = None
    per: float | None = None
    pbr: float | None = None
    eps: float | None = None
    bps: float | None = None
    dps: float | None = None
    dividend_yield: float | None = None
    roe: float | None = None
    roa: float | None = None
    foreign_ownership_pct: float | None = None


def fetch_naver_market_sum_valuation(*, max_workers: int = 8) -> dict[str, dict[str, Any]]:
    """Return StockProgram-style KRX valuation fields from Naver market-sum pages."""

    merged: dict[str, _ValuationRow] = {}
    tasks = [
        (market_code, page, field_key, field_list)
        for market_code, page_limit in NAVER_MARKET_PAGE_LIMITS.items()
        for page in range(1, page_limit + 1)
        for field_key, field_list in NAVER_MARKET_SUM_FIELD_SETS.items()
    ]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _load_naver_market_sum_page,
                market_code=market_code,
                page=page,
                field_key=field_key,
                field_list=field_list,
            ): field_key
            for market_code, page, field_key, field_list in tasks
        }
        for future in as_completed(futures):
            _merge_valuation_rows(merged, future.result(), futures[future])
    return _valuation_payload(merged)


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


def _frame_to_records(frame: Any) -> list[dict[str, Any]]:
    if hasattr(frame, "to_dict"):
        return list(frame.to_dict(orient="records"))
    return [dict(row) for row in frame]


def _load_naver_market_sum_page(
    *,
    market_code: int,
    page: int,
    field_key: str,
    field_list: str,
) -> list[dict[str, Any]]:
    try:
        import pandas as pd
        import requests
    except ImportError as exc:
        raise MarketDataError("pandas and requests are required for Naver market-sum valuation enrichment.") from exc
    response = requests.get(
        NAVER_MARKET_SUM_URL,
        params={"sosok": market_code, "page": page},
        cookies={"field_list": field_list},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=20,
    )
    response.raise_for_status()
    tables = pd.read_html(io.StringIO(response.text))
    if len(tables) < 2:
        return []
    table = tables[1].copy()
    columns = NAVER_MARKET_SUM_COLUMNS[field_key]
    if len(table.columns) < len(columns):
        return []
    table = table.iloc[:, : len(columns)].copy()
    table.columns = columns
    table = table.dropna(subset=["name"]).copy()
    table = table[table["name"].astype(str).str.strip() != "종목명"]
    codes = _extract_codes_from_market_page(response.text)
    if not codes:
        return []
    if len(codes) != len(table):
        table = table.iloc[: min(len(codes), len(table))].copy()
        codes = codes[: len(table)]
    table.insert(0, "symbol", codes)
    return _frame_to_records(table)


def _extract_codes_from_market_page(html_text: str) -> list[str]:
    return [match.zfill(6) for match in re.findall(r"code=(\d+)", html_text)]


def _merge_valuation_rows(target: dict[str, _ValuationRow], records: list[dict[str, Any]], field_key: str) -> None:
    for row in records:
        symbol = _normalize_ticker(row.get("symbol"))
        if not symbol:
            continue
        existing = target.setdefault(symbol, _ValuationRow(symbol=symbol))
        if field_key == "profitability":
            current_price = _safe_float(row.get("last_price"))
            if current_price is not None:
                existing.current_price = current_price
            raw_market_cap = _safe_float(row.get("market_cap_100m"))
            if raw_market_cap is not None:
                existing.market_cap = int(raw_market_cap * 100_000_000)
            operating_profit = _safe_float(row.get("operating_profit_100m"))
            if operating_profit is not None:
                existing.operating_profit = int(operating_profit * 100_000_000)
            per = _safe_float(row.get("per"))
            if per is not None:
                existing.per = per
        elif field_key == "valuation":
            sales = _safe_float(row.get("sales_100m"))
            if sales is not None:
                existing.sales = int(sales * 100_000_000)
            current_price = _safe_float(row.get("last_price"))
            if current_price is not None:
                existing.current_price = current_price
            eps = _safe_float(row.get("eps"))
            if eps is not None:
                existing.eps = eps
            pbr = _safe_float(row.get("pbr"))
            if pbr is not None:
                existing.pbr = pbr
        elif field_key == "dividend":
            dps = _safe_float(row.get("dps"))
            if dps is not None:
                existing.dps = dps
        elif field_key == "roe":
            roe = _safe_float(row.get("roe"))
            if roe is not None:
                existing.roe = roe
        elif field_key == "roa":
            net_income = _safe_float(row.get("net_income_100m"))
            if net_income is not None:
                existing.net_income = int(net_income * 100_000_000)
            roa = _safe_float(row.get("roa"))
            if roa is not None:
                existing.roa = roa
            foreign_ownership_pct = _safe_float(row.get("foreign_ownership_pct"))
            if foreign_ownership_pct is not None:
                existing.foreign_ownership_pct = foreign_ownership_pct


def _valuation_payload(rows: Mapping[str, _ValuationRow]) -> dict[str, dict[str, Any]]:
    payload: dict[str, dict[str, Any]] = {}
    for symbol, row in rows.items():
        dividend_yield = None
        if row.dps is not None and row.current_price and row.current_price > 0:
            dividend_yield = round((row.dps / row.current_price) * 100.0, 4)
        bps = row.bps
        if bps is None and row.current_price is not None and row.pbr is not None and row.pbr > 0:
            bps = round(row.current_price / row.pbr, 4)
        payload[symbol] = {
            "market_cap": row.market_cap,
            "sales": row.sales,
            "operating_profit": row.operating_profit,
            "net_income": row.net_income,
            "per": row.per,
            "pbr": row.pbr,
            "eps": row.eps,
            "bps": bps,
            "dps": row.dps,
            "dividend_yield": dividend_yield,
            "roe": row.roe,
            "roa": row.roa,
            "foreign_ownership_pct": row.foreign_ownership_pct,
        }
    return payload


def _normalize_symbol_filter(symbols: Iterable[Symbol | str] | None) -> set[str] | None:
    if symbols is None:
        return None
    allowed: set[str] = set()
    for symbol in symbols:
        if isinstance(symbol, Symbol):
            allowed.add(symbol.key.upper())
            allowed.add(symbol.ticker.upper())
        else:
            text = str(symbol or "").strip().upper()
            if text:
                allowed.add(text)
                if ":" in text:
                    allowed.add(text.split(":", 1)[1])
    return allowed


def _normalize_name_set(names: Iterable[str] | None) -> set[str] | None:
    if names is None:
        return None
    return {_normalize_name(name) for name in names}


def _normalize_name(name: str) -> str:
    return str(name or "").strip().lower()


def _normalize_ticker(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return text.zfill(6) if text.isdigit() else text


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str) and value.strip() in ("", "N/A", "-"):
        return None
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, str):
            normalized = value.replace(",", "").replace("%", "").strip()
            if not normalized:
                return None
            result = float(normalized)
        else:
            result = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(result) else result
