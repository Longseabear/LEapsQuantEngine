from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from threading import Lock
import time
from typing import Any, Mapping

import requests

from leaps_quant_engine.settings import KISSettings


class KISDirectClientError(RuntimeError):
    """Raised when the in-process KIS adapter cannot complete an operation."""


@dataclass(frozen=True, slots=True)
class KISAccessToken:
    token: str
    expires_at: datetime

    def is_expired(self, *, safety_margin_seconds: int = 30) -> bool:
        return datetime.now(timezone.utc) >= self.expires_at - timedelta(seconds=safety_margin_seconds)


@dataclass(slots=True)
class KISDirectClient:
    """In-process KIS operation boundary used instead of legacy HTTP servers."""

    settings: KISSettings
    session: requests.Session = field(default_factory=requests.Session)
    cache_dir: Path = Path("data/kis-cache")
    rate_limit_per_second: int = 10
    _lock: Lock = field(default_factory=Lock)
    _last_request_at: float = 0.0

    @classmethod
    def from_settings(cls, settings: KISSettings) -> "KISDirectClient":
        return cls(
            settings=settings,
            rate_limit_per_second=min(max(settings.rate_limit_per_second, 1), 20),
        )

    def health_check(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "transport": "in_process_kis",
            "mock": self.settings.mock,
            "base_url": self.settings.base_url,
            "cache_dir": str(self.cache_dir),
            "supported_operations": sorted(_SUPPORTED_OPERATIONS),
        }

    def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.call_operation(tool, arguments)

    def call_operation(self, operation: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        args = dict(arguments or {})
        if operation in {"get_stock_price", "get_latest_quote"}:
            return self._get_stock_price(args)
        if operation == "get_domestic_orderbook":
            return self._get_domestic_orderbook(_required_text(args, "symbol"))
        if operation == "get_daily_ohlcv":
            return self._get_daily_ohlcv(args)
        if operation == "get_or_cache_daily_ohlcv":
            return self._get_or_cache_daily_ohlcv(args)
        if operation == "get_or_cache_domestic_minute_bars":
            return self._get_or_cache_domestic_minute_bars(args)
        if operation == "get_intraday_bars":
            return self._get_domestic_intraday_bars(args)
        if operation == "get_account_balance_summary":
            return self._get_account_balance_summary(args)
        if operation == "get_account_holdings":
            return self._get_account_holdings(args)
        if operation == "get_account_execution_history":
            return self._get_account_execution_history(args)
        if operation == "get_overseas_account_buying_power":
            return self._get_overseas_account_buying_power(args)
        if operation == "place_domestic_cash_order":
            return self._place_domestic_cash_order(args)
        if operation == "revise_or_cancel_domestic_order":
            return self._revise_or_cancel_domestic_order(args)
        if operation == "place_overseas_stock_order":
            return self._place_overseas_stock_order(args)
        if operation == "revise_or_cancel_overseas_stock_order":
            return self._revise_or_cancel_overseas_stock_order(args)
        if operation == "request_hashkey":
            payload = args.get("payload")
            if not isinstance(payload, dict):
                raise KISDirectClientError("request_hashkey requires a payload object.")
            return {"hashkey": self._request_hashkey(payload)}
        raise KISDirectClientError(f"Unsupported in-process KIS operation: {operation}")

    def _get_stock_price(self, args: Mapping[str, Any]) -> dict[str, Any]:
        market = _normalize_market(args.get("market"))
        symbol = _required_text(args, "symbol")
        if market == "domestic":
            return self._get_domestic_price(symbol)
        return self._get_overseas_price(symbol, _required_exchange(args))

    def _get_daily_ohlcv(self, args: Mapping[str, Any]) -> dict[str, Any]:
        market = _normalize_market(args.get("market"))
        symbol = _required_text(args, "symbol")
        period_code = str(args.get("period_code") or "D").strip().upper()
        adjusted_price = bool(args.get("adjusted_price", True))
        start_date = _optional_date(args.get("start_date"))
        end_date = _optional_date(args.get("end_date"))
        if market == "domestic":
            return self._get_domestic_daily_ohlcv(
                symbol,
                period_code=period_code,
                adjusted_price=adjusted_price,
                start_date=start_date,
                end_date=end_date,
            )
        return self._get_overseas_daily_ohlcv(
            symbol,
            _required_exchange(args),
            period_code=period_code,
            adjusted_price=adjusted_price,
            start_date=start_date,
            end_date=end_date,
        )

    def _get_or_cache_daily_ohlcv(self, args: Mapping[str, Any]) -> dict[str, Any]:
        refresh = bool(args.get("refresh", False))
        market = _normalize_market(args.get("market"))
        symbol = _required_text(args, "symbol")
        period_code = str(args.get("period_code") or "D").strip().upper()
        adjusted_price = bool(args.get("adjusted_price", True))
        exchange = _optional_exchange(args)
        cache_path = self._daily_cache_path(
            market=market,
            symbol=symbol,
            exchange=exchange,
            period_code=period_code,
            adjusted_price=adjusted_price,
        )
        if not refresh and cache_path.exists():
            payload = _read_json(cache_path)
        else:
            payload = self._get_daily_ohlcv(args)
            _write_json(cache_path, payload)
        filtered = dict(payload)
        rows = _history_rows(filtered)
        filtered["candles"] = _filter_rows_by_date(
            rows,
            start_date=_optional_date(args.get("start_date")),
            end_date=_optional_date(args.get("end_date")),
        )
        filtered["cache"] = {"path": str(cache_path), "refresh": refresh}
        return filtered

    def _get_or_cache_domestic_minute_bars(self, args: Mapping[str, Any]) -> dict[str, Any]:
        symbol = _required_text(args, "symbol")
        trade_date = _required_date(args, "trade_date")
        start_time = _optional_time(args.get("start_time")) or "090000"
        end_time = _optional_time(args.get("end_time")) or "153000"
        interval_minutes = int(args.get("interval_minutes") or 1)
        refresh = bool(args.get("refresh", False))
        cache_path = self.cache_dir / "minute" / "domestic" / trade_date / f"{symbol}_{interval_minutes}m.json"
        if not refresh and cache_path.exists():
            payload = _read_json(cache_path)
        else:
            payload = self._get_domestic_intraday_bars(
                {
                    "symbol": symbol,
                    "start_time": start_time,
                    "end_time": end_time,
                    "include_previous_data": args.get("include_previous_data", True),
                }
            )
            _write_json(cache_path, payload)
        filtered = dict(payload)
        rows = _history_rows(filtered)
        filtered["candles"] = [
            row
            for row in rows
            if _row_date(row) == trade_date and start_time <= _row_time(row) <= end_time
        ]
        filtered["cache"] = {"path": str(cache_path), "refresh": refresh}
        return filtered

    def _get_domestic_price(self, symbol: str) -> dict[str, Any]:
        normalized_symbol = symbol.strip()
        payload = self._get_json(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            tr_id="FHKST01010100",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": normalized_symbol,
            },
            label=f"domestic quote {symbol}",
        )
        output = _required_mapping(payload, "output")
        last_price = _to_int(output.get("stck_prpr"), "stck_prpr")
        open_price = _to_int(output.get("stck_oprc"), "stck_oprc")
        high_price = _to_int(output.get("stck_hgpr"), "stck_hgpr")
        low_price = _to_int(output.get("stck_lwpr"), "stck_lwpr")
        volume = _to_int(output.get("acml_vol"), "acml_vol")
        result = {
            "symbol": normalized_symbol,
            "name": str(output.get("hts_kor_isnm", "")).strip(),
            "market_code": str(output.get("rprs_mrkt_kor_name", "")).strip(),
            "last_price": last_price,
            "change": _to_int(output.get("prdy_vrss"), "prdy_vrss"),
            "change_rate_percent": _to_float(output.get("prdy_ctrt"), "prdy_ctrt"),
            "open_price": open_price,
            "high_price": high_price,
            "low_price": low_price,
            "volume": volume,
            "live_price_usable": True,
            "price_source": "inquire-price",
            "raw_output": output,
        }
        if _looks_like_domestic_reference_price(output, last_price, open_price, high_price, low_price):
            try:
                orderbook = self._get_domestic_orderbook(normalized_symbol)
            except KISDirectClientError as exc:
                result["live_price_usable"] = False
                result["price_quality_reason"] = f"reference_price_without_orderbook: {exc}"
                return result
            orderbook_price = int(orderbook.get("reference_price") or 0)
            if orderbook_price > 0 and orderbook_price != last_price:
                result.update(
                    {
                        "last_price": orderbook_price,
                        "open_price": orderbook_price if open_price <= 0 else open_price,
                        "high_price": max(high_price, orderbook_price),
                        "low_price": orderbook_price if low_price <= 0 else min(low_price, orderbook_price),
                        "price_source": str(orderbook.get("reference_price_source") or "orderbook"),
                        "orderbook": {
                            "quote_time": orderbook.get("quote_time"),
                            "best_ask": orderbook.get("best_ask"),
                            "best_bid": orderbook.get("best_bid"),
                            "total_ask_size": orderbook.get("total_ask_size"),
                            "total_bid_size": orderbook.get("total_bid_size"),
                        },
                    }
                )
            else:
                result["live_price_usable"] = False
                result["price_quality_reason"] = "reference_price_without_distinct_orderbook_price"
                result["orderbook"] = {
                    "quote_time": orderbook.get("quote_time"),
                    "best_ask": orderbook.get("best_ask"),
                    "best_bid": orderbook.get("best_bid"),
                    "total_ask_size": orderbook.get("total_ask_size"),
                    "total_bid_size": orderbook.get("total_bid_size"),
                }
        return result

    def _get_domestic_orderbook(self, symbol: str) -> dict[str, Any]:
        normalized_symbol = symbol.strip()
        payload = self._get_json(
            "/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
            tr_id="FHKST01010200",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": normalized_symbol,
            },
            label=f"domestic orderbook {symbol}",
        )
        output = _required_mapping(payload, "output1")
        levels: list[dict[str, int]] = []
        for level in range(1, 11):
            levels.append(
                {
                    "level": level,
                    "ask_price": _to_int(output.get(f"askp{level}"), f"askp{level}"),
                    "ask_size": _to_int(output.get(f"askp_rsqn{level}"), f"askp_rsqn{level}"),
                    "bid_price": _to_int(output.get(f"bidp{level}"), f"bidp{level}"),
                    "bid_size": _to_int(output.get(f"bidp_rsqn{level}"), f"bidp_rsqn{level}"),
                }
            )
        best_ask = levels[0]["ask_price"]
        best_bid = levels[0]["bid_price"]
        raw_last_price = _optional_int(output.get("stck_prpr"), default=0)
        reference_price = raw_last_price
        reference_price_source = "orderbook_last_price"
        if reference_price <= 0:
            positive_prices = [price for price in (best_ask, best_bid) if price > 0]
            reference_price = int(sum(positive_prices) / len(positive_prices)) if positive_prices else 0
            reference_price_source = "orderbook_best_bid_ask_mid"
        return {
            "symbol": normalized_symbol,
            "market": "domestic",
            "quote_time": str(output.get("aspr_acpt_hour", "")).strip(),
            "last_price": raw_last_price,
            "reference_price": reference_price,
            "reference_price_source": reference_price_source,
            "best_ask": best_ask,
            "best_bid": best_bid,
            "total_ask_size": _to_int(output.get("total_askp_rsqn"), "total_askp_rsqn"),
            "total_bid_size": _to_int(output.get("total_bidp_rsqn"), "total_bidp_rsqn"),
            "levels": levels,
            "raw_output": output,
        }

    def _get_overseas_price(self, symbol: str, exchange: str) -> dict[str, Any]:
        normalized_symbol = symbol.strip().upper()
        normalized_exchange = _normalize_exchange(exchange)
        payload = self._get_json(
            "/uapi/overseas-price/v1/quotations/price",
            tr_id="HHDFS00000300",
            params={"AUTH": "", "EXCD": normalized_exchange, "SYMB": normalized_symbol},
            label=f"overseas quote {normalized_exchange}:{normalized_symbol}",
        )
        output = _required_mapping(payload, "output")
        last_price = _to_float(output.get("last"), "last")
        return {
            "symbol": normalized_symbol,
            "exchange": normalized_exchange,
            "name": str(output.get("name", "")).strip(),
            "last_price": last_price,
            "change": _to_float(output.get("diff"), "diff"),
            "change_rate_percent": _to_float(output.get("rate"), "rate"),
            "open_price": _optional_float(output.get("open"), default=last_price),
            "high_price": _optional_float(output.get("high"), default=last_price),
            "low_price": _optional_float(output.get("low"), default=last_price),
            "volume": _to_int(output.get("tvol"), "tvol"),
            "raw_output": output,
        }

    def _get_domestic_daily_ohlcv(
        self,
        symbol: str,
        *,
        period_code: str,
        adjusted_price: bool,
        start_date: str | None,
        end_date: str | None,
    ) -> dict[str, Any]:
        normalized_period = _normalize_period(period_code, {"D", "W", "M"})
        payload = self._get_json(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
            tr_id="FHKST01010400",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol.strip(),
                "FID_PERIOD_DIV_CODE": normalized_period,
                "FID_ORG_ADJ_PRC": "1" if adjusted_price else "0",
            },
            label=f"domestic daily {symbol}",
        )
        rows = _required_sequence(payload, "output")
        candles = [
            {
                "date": str(row.get("stck_bsop_date", "")).strip(),
                "open_price": _to_int(row.get("stck_oprc"), "stck_oprc"),
                "high_price": _to_int(row.get("stck_hgpr"), "stck_hgpr"),
                "low_price": _to_int(row.get("stck_lwpr"), "stck_lwpr"),
                "close_price": _to_int(row.get("stck_clpr"), "stck_clpr"),
                "volume": _to_int(row.get("acml_vol"), "acml_vol"),
            }
            for row in rows
        ]
        return {
            "symbol": symbol.strip(),
            "market": "domestic",
            "period_code": normalized_period,
            "adjusted_price": adjusted_price,
            "candles": _filter_rows_by_date(candles, start_date=start_date, end_date=end_date),
            **({"start_date": start_date} if start_date else {}),
            **({"end_date": end_date} if end_date else {}),
        }

    def _get_overseas_daily_ohlcv(
        self,
        symbol: str,
        exchange: str,
        *,
        period_code: str,
        adjusted_price: bool,
        start_date: str | None,
        end_date: str | None,
    ) -> dict[str, Any]:
        normalized_period = _normalize_period(period_code, {"D", "W", "M", "Y"})
        normalized_symbol = symbol.strip().upper()
        normalized_exchange = _normalize_exchange(exchange)
        payload = self._get_json(
            "/uapi/overseas-price/v1/quotations/dailyprice",
            tr_id="HHDFS76240000",
            params={
                "AUTH": "",
                "EXCD": normalized_exchange,
                "SYMB": normalized_symbol,
                "GUBN": normalized_period,
                "BYMD": start_date or "",
                "MODP": "1" if adjusted_price else "0",
            },
            label=f"overseas daily {normalized_exchange}:{normalized_symbol}",
        )
        rows = _required_sequence(payload, "output2")
        candles = [
            {
                "date": str(row.get("xymd", "")).strip(),
                "open_price": _to_float(row.get("open"), "open"),
                "high_price": _to_float(row.get("high"), "high"),
                "low_price": _to_float(row.get("low"), "low"),
                "close_price": _to_float(row.get("clos"), "clos"),
                "volume": _to_int(row.get("tvol"), "tvol"),
            }
            for row in rows
        ]
        return {
            "symbol": normalized_symbol,
            "market": "overseas",
            "exchange": normalized_exchange,
            "period_code": normalized_period,
            "adjusted_price": adjusted_price,
            "candles": _filter_rows_by_date(candles, start_date=start_date, end_date=end_date),
            **({"start_date": start_date} if start_date else {}),
            **({"end_date": end_date} if end_date else {}),
        }

    def _get_domestic_intraday_bars(self, args: Mapping[str, Any]) -> dict[str, Any]:
        symbol = _required_text(args, "symbol")
        start_time = _optional_time(args.get("start_time")) or "090000"
        end_time = _optional_time(args.get("end_time")) or "153000"
        include_previous_data = bool(args.get("include_previous_data", True))
        payload = self._get_json(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            tr_id="FHKST03010200",
            params={
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol.strip(),
                "FID_INPUT_HOUR_1": end_time,
                "FID_PW_DATA_INCU_YN": "Y" if include_previous_data else "N",
                "FID_ETC_CLS_CODE": str(args.get("etc_cls_code") or "").strip(),
            },
            label=f"domestic intraday {symbol}",
        )
        rows = _required_sequence(payload, "output2")
        candles: list[dict[str, Any]] = []
        trade_date = ""
        for row in rows:
            row_date = str(row.get("stck_bsop_date", "")).strip()
            row_time = _optional_time(row.get("stck_cntg_hour")) or ""
            if not row_date or not row_time or row_time < start_time or row_time > end_time:
                continue
            trade_date = trade_date or row_date
            candles.append(
                {
                    "date": row_date,
                    "time": row_time,
                    "open_price": _to_int(row.get("stck_oprc"), "stck_oprc"),
                    "high_price": _to_int(row.get("stck_hgpr"), "stck_hgpr"),
                    "low_price": _to_int(row.get("stck_lwpr"), "stck_lwpr"),
                    "close_price": _to_int(row.get("stck_prpr"), "stck_prpr"),
                    "volume": _to_int(row.get("cntg_vol"), "cntg_vol"),
                }
            )
        candles.sort(key=lambda row: (row["date"], row["time"]))
        return {
            "symbol": symbol.strip(),
            "market": "domestic",
            "trade_date": trade_date,
            "start_time": start_time,
            "end_time": end_time,
            "candle_count": len(candles),
            "candles": candles,
        }

    def _get_account_balance_summary(self, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        market = _normalize_market((args or {}).get("market"))
        if market == "overseas":
            return self._get_overseas_account_balance_summary(args or {})
        payload = self._get_account_balance_raw()
        holdings = _list_or_empty(payload.get("output1"))
        summary = _first_mapping(payload.get("output2"), "output2")
        return {
            "account_type": "domestic_stock",
            "cash_balance": _to_int(summary.get("prvs_rcdl_excc_amt"), "prvs_rcdl_excc_amt"),
            "deposit_total_amount": _to_int(summary.get("dnca_tot_amt"), "dnca_tot_amt"),
            "previous_settlement_amount": _to_int(summary.get("prvs_rcdl_excc_amt"), "prvs_rcdl_excc_amt"),
            "next_day_settlement_amount": _to_int(summary.get("nxdy_excc_amt"), "nxdy_excc_amt"),
            "securities_evaluation_amount": _to_int(summary.get("scts_evlu_amt"), "scts_evlu_amt"),
            "total_evaluation_amount": _to_int(summary.get("tot_evlu_amt"), "tot_evlu_amt"),
            "net_asset_amount": _to_int(summary.get("nass_amt"), "nass_amt"),
            "purchase_amount_total": _to_int(summary.get("pchs_amt_smtl_amt"), "pchs_amt_smtl_amt"),
            "evaluation_profit_loss_total": _to_int(summary.get("evlu_pfls_smtl_amt"), "evlu_pfls_smtl_amt"),
            "holdings_count": len(holdings),
        }

    def _get_account_holdings(self, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        market = _normalize_market((args or {}).get("market"))
        if market == "overseas":
            return self._get_overseas_account_holdings(args or {})
        payload = self._get_account_balance_raw()
        holdings: list[dict[str, Any]] = []
        for row in _list_or_empty(payload.get("output1")):
            symbol = str(row.get("pdno", "")).strip()
            if not symbol:
                continue
            holdings.append(
                {
                    "symbol": symbol,
                    "name": str(row.get("prdt_name", "")).strip(),
                    "holding_quantity": _to_int(row.get("hldg_qty", "0"), "hldg_qty"),
                    "orderable_quantity": _to_int(row.get("ord_psbl_qty", "0"), "ord_psbl_qty"),
                    "average_purchase_price": _to_int(row.get("pchs_avg_pric", "0"), "pchs_avg_pric"),
                    "purchase_amount": _to_int(row.get("pchs_amt", "0"), "pchs_amt"),
                    "current_price": _to_int(row.get("prpr", "0"), "prpr"),
                    "evaluation_amount": _to_int(row.get("evlu_amt", "0"), "evlu_amt"),
                    "evaluation_profit_loss_amount": _to_int(row.get("evlu_pfls_amt", "0"), "evlu_pfls_amt"),
                    "evaluation_profit_loss_rate": _optional_float(row.get("evlu_pfls_rt"), default=0.0),
                }
            )
        return {"account_type": "domestic_stock", "holdings": holdings, "holdings_count": len(holdings)}

    def _get_overseas_account_balance_summary(self, args: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._get_overseas_account_balance_raw(args)
        holdings = _list_or_empty(payload.get("output1"))
        summary = _first_mapping(payload.get("output3"), "output3")
        present_cash_balance = _to_float(
            summary.get("wdrw_psbl_tot_amt", summary.get("nxdy_frcr_drwg_psbl_amt", "0")),
            "wdrw_psbl_tot_amt",
        )
        buying_power = self._get_overseas_account_buying_power(args)
        cash_balance = float(buying_power.get("orderable_foreign_amount") or 0.0)
        return {
            "account_type": "overseas_stock",
            "currency": str(buying_power.get("currency") or _overseas_cash_currency(args)),
            "cash_balance": cash_balance,
            "present_cash_balance": present_cash_balance,
            "deposit_total_amount": _to_float(summary.get("tot_dncl_amt", summary.get("dncl_amt", "0")), "tot_dncl_amt"),
            "previous_settlement_amount": cash_balance,
            "next_day_settlement_amount": _to_float(
                summary.get("nxdy_frcr_drwg_psbl_amt", summary.get("wdrw_psbl_tot_amt", "0")),
                "nxdy_frcr_drwg_psbl_amt",
            ),
            "securities_evaluation_amount": _to_float(
                summary.get("evlu_amt_smtl_amt", summary.get("evlu_amt_smtl", "0")),
                "evlu_amt_smtl_amt",
            ),
            "total_evaluation_amount": _to_float(summary.get("tot_asst_amt", "0"), "tot_asst_amt"),
            "net_asset_amount": _to_float(summary.get("tot_asst_amt", "0"), "tot_asst_amt"),
            "purchase_amount_total": _to_float(
                summary.get("pchs_amt_smtl_amt", summary.get("pchs_amt_smtl", "0")),
                "pchs_amt_smtl_amt",
            ),
            "evaluation_profit_loss_total": _to_float(summary.get("tot_evlu_pfls_amt", "0"), "tot_evlu_pfls_amt"),
            "holdings_count": len(holdings),
            "buying_power": buying_power,
        }

    def _get_overseas_account_holdings(self, args: Mapping[str, Any]) -> dict[str, Any]:
        payload = self._get_overseas_account_balance_raw(args)
        holdings: list[dict[str, Any]] = []
        for row in _list_or_empty(payload.get("output1")):
            symbol = str(row.get("pdno") or row.get("ovrs_pdno") or row.get("std_pdno") or "").strip()
            if not symbol:
                continue
            holdings.append(
                {
                    "symbol": symbol,
                    "market": _canonical_market_from_overseas_exchange(str(row.get("ovrs_excg_cd", "")).strip()),
                    "name": str(row.get("prdt_name", "")).strip(),
                    "exchange": str(row.get("ovrs_excg_cd", "")).strip(),
                    "currency": str(row.get("crcy_cd") or row.get("buy_crcy_cd") or row.get("tr_crcy_cd") or "").strip(),
                    "holding_quantity": _to_int(row.get("cblc_qty13", row.get("ovrs_cblc_qty", "0")), "cblc_qty13"),
                    "orderable_quantity": _to_int(row.get("ord_psbl_qty1", row.get("ord_psbl_qty", "0")), "ord_psbl_qty1"),
                    "average_purchase_price": _to_float(row.get("avg_unpr3", row.get("pchs_avg_pric", "0")), "avg_unpr3"),
                    "purchase_amount": _to_float(
                        row.get("pchs_rmnd_wcrc_amt", row.get("frcr_pchs_amt", row.get("frcr_pchs_amt1", "0"))),
                        "pchs_rmnd_wcrc_amt",
                    ),
                    "current_price": _to_float(row.get("ovrs_now_pric1", row.get("now_pric2", "0")), "ovrs_now_pric1"),
                    "evaluation_amount": _to_float(row.get("frcr_evlu_amt2", row.get("ovrs_stck_evlu_amt", "0")), "frcr_evlu_amt2"),
                    "evaluation_profit_loss_amount": _to_float(
                        row.get("evlu_pfls_amt2", row.get("frcr_evlu_pfls_amt", "0")),
                        "evlu_pfls_amt2",
                    ),
                    "evaluation_profit_loss_rate": _to_float(row.get("evlu_pfls_rt1", row.get("evlu_pfls_rt", "0")), "evlu_pfls_rt1"),
                }
            )
        return {"account_type": "overseas_stock", "holdings": holdings, "holdings_count": len(holdings)}

    def _get_account_execution_history(self, args: Mapping[str, Any]) -> dict[str, Any]:
        market = _normalize_market(args.get("market"))
        if market == "overseas":
            return self._get_overseas_account_execution_history(args)
        start_date = _required_date(args, "start_date")
        end_date = _required_date(args, "end_date")
        side_filter = _normalize_side_filter(str(args.get("side") or "all"))
        symbol = str(args.get("symbol") or "").strip()
        payload = self._get_json(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            tr_id="VTTC0081R" if self.settings.mock else "TTTC0081R",
            params={
                **self._account_params(),
                "INQR_STRT_DT": start_date,
                "INQR_END_DT": end_date,
                "SLL_BUY_DVSN_CD": side_filter,
                "INQR_DVSN": "00",
                "PDNO": symbol,
                "CCLD_DVSN": "01",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_3": "00",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
                "EXCG_ID_DVSN_CD": "ALL",
            },
            label="domestic account execution history",
        )
        executions = []
        for row in _list_or_empty(payload.get("output1")):
            filled_quantity = _to_int(row.get("tot_ccld_qty", "0"), "tot_ccld_qty")
            if filled_quantity <= 0:
                continue
            side = _normalize_history_side(row)
            executions.append(
                {
                    "order_id": str(row.get("odno", "")).strip(),
                    "symbol": str(row.get("pdno", "")).strip(),
                    "name": str(row.get("prdt_name", "")).strip(),
                    "side": side,
                    "execution_date": str(row.get("ord_dt", "")).strip(),
                    "execution_time": str(row.get("ord_tmd", "")).strip(),
                    "execution_timestamp": _join_kis_timestamp(row.get("ord_dt"), row.get("ord_tmd")),
                    "execution_quantity": filled_quantity,
                    "execution_price": _to_int(row.get("avg_prvs", "0"), "avg_prvs"),
                    "execution_amount": _to_int(row.get("tot_ccld_amt", "0"), "tot_ccld_amt"),
                    "source_granularity": "order_execution_summary",
                }
            )
        return {
            "account_type": "domestic_stock",
            "executions": executions,
            "execution_count": len(executions),
            "start_date": start_date,
            "end_date": end_date,
            "side_filter": str(args.get("side") or "all").strip().lower(),
            "symbol_filter": symbol,
            "exchange_scope_filter": "ALL",
            "source_note": "Rows come from KIS daily order/execution inquiry and may aggregate fills.",
        }

    def _get_overseas_account_execution_history(self, args: Mapping[str, Any]) -> dict[str, Any]:
        start_date = _required_date(args, "start_date")
        end_date = _required_date(args, "end_date")
        side_filter = _normalize_side_filter(str(args.get("side") or "all"))
        symbol = str(args.get("symbol") or "").strip().upper()
        exchange = str(args.get("exchange") or "%%").strip().upper() or "%%"
        payload = self._get_json(
            "/uapi/overseas-stock/v1/trading/inquire-ccnl",
            tr_id="VTTS3035R" if self.settings.mock else "TTTS3035R",
            params={
                **self._account_params(),
                "PDNO": symbol or "%",
                "ORD_STRT_DT": start_date,
                "ORD_END_DT": end_date,
                "SLL_BUY_DVSN": side_filter,
                "CCLD_NCCS_DVSN": "01",
                "OVRS_EXCG_CD": exchange,
                "SORT_SQN": "DS",
                "ORD_DT": "",
                "ORD_GNO_BRNO": str(args.get("branch_no") or "").strip(),
                "ODNO": str(args.get("order_no") or "").strip(),
                "CTX_AREA_NK200": "",
                "CTX_AREA_FK200": "",
            },
            label="overseas account execution history",
        )
        executions = []
        for row in _list_or_empty(payload.get("output")):
            filled_quantity = _to_int(row.get("ft_ccld_qty", "0"), "ft_ccld_qty")
            if filled_quantity <= 0:
                continue
            exchange_code = str(row.get("ovrs_excg_cd") or "").strip().upper()
            executions.append(
                {
                    "order_id": str(row.get("odno", "")).strip(),
                    "symbol": str(row.get("pdno", "")).strip().upper(),
                    "name": str(row.get("prdt_name", "")).strip(),
                    "side": _normalize_history_side(row),
                    "execution_date": str(row.get("ord_dt", "")).strip(),
                    "execution_time": str(row.get("ord_tmd", "")).strip(),
                    "execution_timestamp": _join_kis_timestamp(row.get("ord_dt"), row.get("ord_tmd")),
                    "execution_quantity": filled_quantity,
                    "execution_price": _to_float(row.get("ft_ccld_unpr3", "0"), "ft_ccld_unpr3"),
                    "execution_amount": _to_float(row.get("ft_ccld_amt3", "0"), "ft_ccld_amt3"),
                    "order_quantity": _to_int(row.get("ft_ord_qty", "0"), "ft_ord_qty"),
                    "order_price": _to_float(row.get("ft_ord_unpr3", "0"), "ft_ord_unpr3"),
                    "unfilled_quantity": _to_int(row.get("nccs_qty", "0"), "nccs_qty"),
                    "exchange": exchange_code,
                    "market": _canonical_market_from_overseas_exchange(exchange_code),
                    "currency": str(row.get("tr_crcy_cd") or "").strip().upper(),
                    "source_granularity": "order_execution_summary",
                }
            )
        return {
            "account_type": "overseas_stock",
            "executions": executions,
            "execution_count": len(executions),
            "start_date": start_date,
            "end_date": end_date,
            "side_filter": str(args.get("side") or "all").strip().lower(),
            "symbol_filter": symbol,
            "exchange_scope_filter": exchange,
            "source_note": "Rows come from KIS overseas order/execution inquiry and may aggregate fills.",
        }

    def _place_domestic_cash_order(self, args: Mapping[str, Any]) -> dict[str, Any]:
        side = _normalize_order_side(args.get("side"))
        symbol = _required_text(args, "symbol")
        order_division = _normalize_order_division(args.get("order_division") or "00")
        quantity = _positive_int(args.get("quantity"), "quantity")
        price = _normalize_order_price(args.get("price"), order_division=order_division)
        body = {
            **self._account_params(),
            "PDNO": symbol,
            "ORD_DVSN": order_division,
            "ORD_QTY": str(quantity),
            "ORD_UNPR": str(price),
            "EXCG_ID_DVSN_CD": _normalize_exchange_scope(args.get("exchange_scope") or "KRX"),
            "SLL_TYPE": str(args.get("sell_type") or "").strip(),
            "CNDT_PRIC": str(args.get("conditional_price") or "").strip(),
        }
        tr_id = {
            ("buy", False): "TTTC0012U",
            ("sell", False): "TTTC0011U",
            ("buy", True): "VTTC0012U",
            ("sell", True): "VTTC0011U",
        }[(side, self.settings.mock)]
        payload = self._post_json(
            "/uapi/domestic-stock/v1/trading/order-cash",
            tr_id=tr_id,
            body=body,
            label="domestic stock order",
            use_hashkey=bool(args.get("use_hashkey", False)),
        )
        output = _required_mapping(payload, "output")
        return {
            "rt_cd": str(payload.get("rt_cd", "")).strip(),
            "msg_cd": str(payload.get("msg_cd", "")).strip(),
            "msg1": str(payload.get("msg1", "")).strip(),
            "branch_no": str(output.get("KRX_FWDG_ORD_ORGNO", "")).strip(),
            "order_no": str(output.get("ODNO", "")).strip(),
            "order_time": str(output.get("ORD_TMD", "")).strip(),
            "market": "domestic",
            "side": side,
            "symbol": symbol,
            "quantity": quantity,
            "price": price,
            "order_division": order_division,
            "exchange_scope": body["EXCG_ID_DVSN_CD"],
            "raw_output": output,
        }

    def _revise_or_cancel_domestic_order(self, args: Mapping[str, Any]) -> dict[str, Any]:
        body = {
            **self._account_params(),
            "KRX_FWDG_ORD_ORGNO": _required_text(args, "original_branch_no"),
            "ORGN_ODNO": _required_text(args, "original_order_no"),
            "ORD_DVSN": _normalize_order_division(args.get("order_division") or "00"),
            "RVSE_CNCL_DVSN_CD": _normalize_cancel_type(args.get("rvse_cncl_dvsn_cd") or "02"),
            "ORD_QTY": str(_non_negative_int(args.get("quantity", 0), "quantity")),
            "ORD_UNPR": str(_non_negative_int(args.get("price", 0), "price")),
            "QTY_ALL_ORD_YN": _normalize_flag(args.get("qty_all_ord_yn") or "Y"),
            "EXCG_ID_DVSN_CD": _normalize_exchange_scope(args.get("exchange_scope") or "KRX"),
        }
        payload = self._post_json(
            "/uapi/domestic-stock/v1/trading/order-rvsecncl",
            tr_id="VTTC0013U" if self.settings.mock else "TTTC0013U",
            body=body,
            label="domestic revise/cancel order",
            use_hashkey=bool(args.get("use_hashkey", False)),
        )
        output = _required_mapping(payload, "output")
        return {
            "rt_cd": str(payload.get("rt_cd", "")).strip(),
            "msg_cd": str(payload.get("msg_cd", "")).strip(),
            "msg1": str(payload.get("msg1", "")).strip(),
            "branch_no": str(output.get("KRX_FWDG_ORD_ORGNO", "")).strip(),
            "order_no": str(output.get("ODNO", "")).strip(),
            "order_time": str(output.get("ORD_TMD", "")).strip(),
            "market": "domestic",
            "original_order_no": body["ORGN_ODNO"],
            "raw_output": output,
        }

    def _place_overseas_stock_order(self, args: Mapping[str, Any]) -> dict[str, Any]:
        side = _normalize_order_side(args.get("side"))
        exchange = _normalize_overseas_order_exchange(args.get("exchange"))
        symbol = _required_text(args, "symbol").upper()
        order_division = _normalize_order_division(args.get("order_division") or "00")
        quantity = _positive_int(args.get("quantity"), "quantity")
        price = _normalize_overseas_order_price(args.get("price"), order_division=order_division)
        tr_id = _overseas_order_tr_id(side=side, exchange=exchange, mock=self.settings.mock)
        body = {
            **self._account_params(),
            "OVRS_EXCG_CD": exchange,
            "PDNO": symbol,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": price,
            "CTAC_TLNO": str(args.get("contact_phone") or "").strip(),
            "MGCO_APTM_ODNO": str(args.get("auto_order_no") or "").strip(),
            "SLL_TYPE": "" if side == "buy" else str(args.get("sell_type") or "00").strip(),
            "ORD_SVR_DVSN_CD": str(args.get("order_server_division_code") or "0").strip(),
            "ORD_DVSN": order_division,
        }
        payload = self._post_json(
            "/uapi/overseas-stock/v1/trading/order",
            tr_id=tr_id,
            body=body,
            label="overseas stock order",
            use_hashkey=bool(args.get("use_hashkey", False)),
        )
        output = _required_mapping(payload, "output")
        return {
            "rt_cd": str(payload.get("rt_cd", "")).strip(),
            "msg_cd": str(payload.get("msg_cd", "")).strip(),
            "msg1": str(payload.get("msg1", "")).strip(),
            "branch_no": str(output.get("KRX_FWDG_ORD_ORGNO", "")).strip(),
            "order_no": str(output.get("ODNO", "")).strip(),
            "order_time": str(output.get("ORD_TMD", "")).strip(),
            "market": "overseas",
            "side": side,
            "exchange": exchange,
            "symbol": symbol,
            "quantity": quantity,
            "price": float(price),
            "order_division": order_division,
            "raw_output": output,
        }

    def _revise_or_cancel_overseas_stock_order(self, args: Mapping[str, Any]) -> dict[str, Any]:
        exchange = _normalize_overseas_order_exchange(args.get("exchange"))
        symbol = _required_text(args, "symbol").upper()
        order_division = _normalize_order_division(args.get("order_division") or "00")
        cancel_type = _normalize_cancel_type(args.get("rvse_cncl_dvsn_cd") or "02")
        quantity = _positive_int(args.get("quantity"), "quantity")
        price = _normalize_overseas_order_price(
            args.get("price"),
            order_division=order_division,
            allow_zero=True,
        )
        body = {
            **self._account_params(),
            "OVRS_EXCG_CD": exchange,
            "PDNO": symbol,
            "ORGN_ODNO": _required_text(args, "original_order_no"),
            "RVSE_CNCL_DVSN_CD": cancel_type,
            "ORD_QTY": str(quantity),
            "OVRS_ORD_UNPR": price,
            "MGCO_APTM_ODNO": str(args.get("auto_order_no") or "").strip(),
            "ORD_SVR_DVSN_CD": str(args.get("order_server_division_code") or "0").strip(),
        }
        payload = self._post_json(
            "/uapi/overseas-stock/v1/trading/order-rvsecncl",
            tr_id=_overseas_revise_cancel_tr_id(exchange=exchange, mock=self.settings.mock),
            body=body,
            label="overseas revise/cancel order",
            use_hashkey=bool(args.get("use_hashkey", False)),
        )
        output = _required_mapping(payload, "output")
        return {
            "rt_cd": str(payload.get("rt_cd", "")).strip(),
            "msg_cd": str(payload.get("msg_cd", "")).strip(),
            "msg1": str(payload.get("msg1", "")).strip(),
            "branch_no": str(output.get("KRX_FWDG_ORD_ORGNO", "")).strip(),
            "order_no": str(output.get("ODNO", "")).strip(),
            "order_time": str(output.get("ORD_TMD", "")).strip(),
            "market": "overseas",
            "exchange": exchange,
            "symbol": symbol,
            "original_order_no": body["ORGN_ODNO"],
            "rvse_cncl_dvsn_cd": cancel_type,
            "quantity": quantity,
            "price": float(price),
            "raw_output": output,
        }

    def _get_account_balance_raw(self) -> dict[str, Any]:
        return self._get_json(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id="VTTC8434R" if self.settings.mock else "TTTC8434R",
            params={
                **self._account_params(),
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
            label="domestic account balance",
        )

    def _get_overseas_account_balance_raw(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return self._get_json(
            "/uapi/overseas-stock/v1/trading/inquire-present-balance",
            tr_id="VTRP6504R" if self.settings.mock else "CTRP6504R",
            params={
                **self._account_params(),
                "WCRC_FRCR_DVSN_CD": _normalize_currency_basis(args.get("currency_basis") or "02"),
                "NATN_CD": str(args.get("nation_code") or "000").strip() or "000",
                "TR_MKET_CD": str(args.get("market_code") or "00").strip() or "00",
                "INQR_DVSN_CD": str(args.get("inquiry_division") or "00").strip() or "00",
            },
            label="overseas account balance",
        )

    def _get_overseas_account_buying_power(self, args: Mapping[str, Any]) -> dict[str, Any]:
        symbol = str(args.get("symbol") or self.settings.default_overseas_symbol or "SMH").strip().upper()
        exchange = _normalize_overseas_order_exchange(args.get("exchange") or self.settings.default_overseas_exchange or "NAS")
        price = str(args.get("price") or "").strip()
        payload = self._get_json(
            "/uapi/overseas-stock/v1/trading/inquire-psamount",
            tr_id="VTTS3007R" if self.settings.mock else "TTTS3007R",
            params={
                **self._account_params(),
                "OVRS_EXCG_CD": exchange,
                "OVRS_ORD_UNPR": price,
                "ITEM_CD": symbol,
            },
            label="overseas buying power",
        )
        output = _required_mapping(payload, "output")
        return {
            "account_type": "overseas_stock",
            "currency": str(output.get("tr_crcy_cd") or "").strip() or "USD",
            "orderable_foreign_amount": _to_float(output.get("ord_psbl_frcr_amt", "0"), "ord_psbl_frcr_amt"),
            "overseas_orderable_amount": _to_float(output.get("ovrs_ord_psbl_amt", "0"), "ovrs_ord_psbl_amt"),
            "foreign_orderable_amount_detail": _to_float(output.get("frcr_ord_psbl_amt1", "0"), "frcr_ord_psbl_amt1"),
            "sell_reusable_amount": _to_float(output.get("sll_ruse_psbl_amt", "0"), "sll_ruse_psbl_amt"),
            "exchange_after_orderable_amount": _to_float(output.get("echm_af_ord_psbl_amt", "0"), "echm_af_ord_psbl_amt"),
            "exchange_after_orderable_quantity": _to_int(output.get("echm_af_ord_psbl_qty", "0"), "echm_af_ord_psbl_qty"),
            "max_orderable_quantity": _to_int(output.get("max_ord_psbl_qty", output.get("ovrs_max_ord_psbl_qty", "0")), "max_ord_psbl_qty"),
            "orderable_quantity": _to_int(output.get("ord_psbl_qty", "0"), "ord_psbl_qty"),
            "exchange_rate": _to_float(output.get("exrt", "0"), "exrt"),
            "exchange": exchange,
            "symbol": symbol,
        }

    def _account_params(self) -> dict[str, str]:
        if not self.settings.cano:
            raise KISDirectClientError("KIS_CANO is required for account operations.")
        if not self.settings.account_product_code:
            raise KISDirectClientError("KIS_ACNT_PRDT_CD is required for account operations.")
        return {"CANO": self.settings.cano, "ACNT_PRDT_CD": self.settings.account_product_code}

    def _get_json(
        self,
        path: str,
        *,
        tr_id: str,
        params: Mapping[str, Any],
        label: str,
    ) -> dict[str, Any]:
        self._wait_for_turn()
        headers = self._headers(tr_id=tr_id)
        try:
            response = self.session.get(
                f"{self.settings.base_url.rstrip('/')}{path}",
                headers=headers,
                params=dict(params),
                timeout=10,
            )
        except requests.RequestException as exc:
            raise KISDirectClientError(f"Failed to request {label} due to a network error.") from exc
        return self._checked_payload(response, label=label)

    def _post_json(
        self,
        path: str,
        *,
        tr_id: str,
        body: Mapping[str, Any],
        label: str,
        use_hashkey: bool,
    ) -> dict[str, Any]:
        self._wait_for_turn()
        body_dict = dict(body)
        headers = self._headers(tr_id=tr_id, content_type=True)
        if use_hashkey:
            headers["hashkey"] = self._request_hashkey(body_dict)
        try:
            response = self.session.post(
                f"{self.settings.base_url.rstrip('/')}{path}",
                headers=headers,
                json=body_dict,
                timeout=10,
            )
        except requests.RequestException as exc:
            raise KISDirectClientError(f"Failed to request {label} due to a network error.") from exc
        return self._checked_payload(response, label=label)

    def _request_hashkey(self, payload: Mapping[str, Any]) -> str:
        self._wait_for_turn()
        try:
            response = self.session.post(
                f"{self.settings.base_url.rstrip('/')}/uapi/hashkey",
                headers={
                    "content-type": "application/json; charset=UTF-8",
                    "appkey": self.settings.app_key,
                    "appsecret": self.settings.app_secret,
                },
                json=dict(payload),
                timeout=10,
            )
        except requests.RequestException as exc:
            raise KISDirectClientError("Failed to request KIS hashkey due to a network error.") from exc
        body = self._checked_payload(response, label="KIS hashkey", check_rt_cd=False)
        hashkey = str(body.get("HASH") or "").strip()
        if not hashkey:
            raise KISDirectClientError("KIS hashkey response did not include HASH.")
        return hashkey

    def _headers(self, *, tr_id: str, content_type: bool = False) -> dict[str, str]:
        headers = {
            "authorization": f"Bearer {self._access_token()}",
            "appkey": self.settings.app_key,
            "appsecret": self.settings.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if content_type:
            headers["content-type"] = "application/json; charset=UTF-8"
        return headers

    def _access_token(self) -> str:
        cache_key = f"{self.settings.base_url}|{self.settings.app_key}|{int(self.settings.mock)}"
        cached = _TOKEN_CACHE.get(cache_key)
        if cached is not None and not cached.is_expired():
            return cached.token
        cached = self._read_token_cache(cache_key)
        if cached is not None:
            _TOKEN_CACHE[cache_key] = cached
            return cached.token
        token = self._request_access_token()
        _TOKEN_CACHE[cache_key] = token
        self._write_token_cache(cache_key, token)
        return token.token

    def _request_access_token(self) -> KISAccessToken:
        self._wait_for_turn()
        try:
            response = self.session.post(
                f"{self.settings.base_url.rstrip('/')}/oauth2/tokenP",
                json={
                    "grant_type": "client_credentials",
                    "appkey": self.settings.app_key,
                    "appsecret": self.settings.app_secret,
                },
                headers={"content-type": "application/json; charset=UTF-8"},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise KISDirectClientError("Failed to request KIS access token due to a network error.") from exc
        payload = self._checked_payload(response, label="KIS token", check_rt_cd=False)
        access_token = str(payload.get("access_token") or "").strip()
        if not access_token:
            raise KISDirectClientError("KIS token response did not include access_token.")
        try:
            expires_in = int(payload.get("expires_in") or 0)
        except (TypeError, ValueError) as exc:
            raise KISDirectClientError("KIS token response did not include a valid expires_in.") from exc
        return KISAccessToken(
            token=access_token,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        )

    def _read_token_cache(self, cache_key: str) -> KISAccessToken | None:
        path = self._token_cache_path(cache_key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            token = KISAccessToken(
                token=str(payload["token"]).strip(),
                expires_at=datetime.fromisoformat(str(payload["expires_at"])).astimezone(timezone.utc),
            )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
        if not token.token or token.is_expired():
            return None
        return token

    def _write_token_cache(self, cache_key: str, token: KISAccessToken) -> None:
        _write_json(
            self._token_cache_path(cache_key),
            {"token": token.token, "expires_at": token.expires_at.isoformat()},
        )

    def _token_cache_path(self, cache_key: str) -> Path:
        digest = sha256(cache_key.encode("utf-8")).hexdigest()
        return self.cache_dir / "tokens" / f"{digest}.json"

    def _daily_cache_path(
        self,
        *,
        market: str,
        symbol: str,
        exchange: str | None,
        period_code: str,
        adjusted_price: bool,
    ) -> Path:
        route = exchange or market
        adjusted = "adjusted" if adjusted_price else "raw"
        return self.cache_dir / "daily" / market / f"{route}_{symbol}_{period_code}_{adjusted}.json"

    def _checked_payload(self, response: requests.Response, *, label: str, check_rt_cd: bool = True) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise KISDirectClientError(f"{label} response was not valid JSON (HTTP {response.status_code}).") from exc
        if not isinstance(payload, dict):
            raise KISDirectClientError(f"{label} response was not a JSON object.")
        if response.status_code >= 400 or (check_rt_cd and str(payload.get("rt_cd", "0")) != "0"):
            message = payload.get("msg1") or payload.get("message") or f"Unknown {label} error."
            code = payload.get("msg_cd") or payload.get("rt_cd") or payload.get("code") or "UNKNOWN"
            raise KISDirectClientError(f"{label} request failed ({code}): {message}")
        return payload

    def _wait_for_turn(self) -> None:
        min_interval = 1.0 / max(self.rate_limit_per_second, 1)
        with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            self._last_request_at = time.monotonic()


_TOKEN_CACHE: dict[str, KISAccessToken] = {}

_SUPPORTED_OPERATIONS = {
    "get_stock_price",
    "get_latest_quote",
    "get_domestic_orderbook",
    "get_daily_ohlcv",
    "get_or_cache_daily_ohlcv",
    "get_or_cache_domestic_minute_bars",
    "get_intraday_bars",
    "get_account_balance_summary",
    "get_account_holdings",
    "get_account_execution_history",
    "get_overseas_account_buying_power",
    "place_domestic_cash_order",
    "revise_or_cancel_domestic_order",
    "place_overseas_stock_order",
    "revise_or_cancel_overseas_stock_order",
    "request_hashkey",
}

_SUPPORTED_OVERSEAS_EXCHANGES = {"NAS", "NYS", "AMS", "HKS", "TSE", "SHS", "SZS"}
_SUPPORTED_OVERSEAS_ORDER_EXCHANGES = {"NASD", "NYSE", "AMEX", "SEHK", "SHAA", "SZAA", "TKSE", "HASE", "VNSE"}
_OVERSEAS_BUY_TR_IDS = {
    "NASD": "TTTT1002U",
    "NYSE": "TTTT1002U",
    "AMEX": "TTTT1002U",
    "SEHK": "TTTS1002U",
    "SHAA": "TTTS0202U",
    "SZAA": "TTTS0305U",
    "TKSE": "TTTS0308U",
    "HASE": "TTTS0311U",
    "VNSE": "TTTS0311U",
}
_OVERSEAS_SELL_TR_IDS = {
    "NASD": "TTTT1006U",
    "NYSE": "TTTT1006U",
    "AMEX": "TTTT1006U",
    "SEHK": "TTTS1001U",
    "SHAA": "TTTS1005U",
    "SZAA": "TTTS0304U",
    "TKSE": "TTTS0307U",
    "HASE": "TTTS0310U",
    "VNSE": "TTTS0310U",
}
_OVERSEAS_REVISE_CANCEL_TR_IDS = {
    "NASD": "TTTT1004U",
    "NYSE": "TTTT1004U",
    "AMEX": "TTTT1004U",
    "SEHK": "TTTS1003U",
    "SHAA": "TTTS0302U",
    "SZAA": "TTTS0306U",
    "TKSE": "TTTS0309U",
    "HASE": "TTTS0312U",
    "VNSE": "TTTS0312U",
}
_MARKET_PRICE_ORDER_DIVISIONS = {"01", "13", "14"}
_LIMIT_PRICE_ORDER_DIVISIONS = {"00", "05", "06", "07", "11", "12"}


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    text = str(payload.get(key) or "").strip()
    if not text:
        raise KISDirectClientError(f"{key} is required.")
    return text


def _normalize_market(value: Any) -> str:
    text = str(value or "domestic").strip().upper()
    if text in {"KR", "KRX", "KOR", "DOMESTIC"}:
        return "domestic"
    return "overseas"


def _optional_exchange(payload: Mapping[str, Any]) -> str | None:
    raw = payload.get("exchange")
    if raw not in (None, ""):
        return _normalize_exchange(str(raw))
    market = str(payload.get("market") or "").strip().upper()
    if market in _SUPPORTED_OVERSEAS_EXCHANGES:
        return market
    return None


def _required_exchange(payload: Mapping[str, Any]) -> str:
    exchange = _optional_exchange(payload)
    if not exchange:
        raise KISDirectClientError("exchange is required for overseas KIS operations.")
    return exchange


def _normalize_exchange(value: str) -> str:
    text = str(value or "").strip().upper()
    if text == "NYSE":
        text = "NYS"
    if text == "NASDAQ":
        text = "NAS"
    if text == "AMEX":
        text = "AMS"
    if text not in _SUPPORTED_OVERSEAS_EXCHANGES:
        raise KISDirectClientError(f"Unsupported overseas exchange: {value}")
    return text


def _normalize_currency_basis(value: Any) -> str:
    text = str(value or "02").strip()
    if text not in {"01", "02"}:
        raise KISDirectClientError("currency_basis must be '01' (KRW) or '02' (foreign).")
    return text


def _overseas_cash_currency(args: Mapping[str, Any]) -> str:
    currency = str(args.get("currency") or "").strip().upper()
    if currency:
        return currency
    return "KRW" if _normalize_currency_basis(args.get("currency_basis") or "02") == "01" else "USD"


def _normalize_overseas_order_exchange(value: Any) -> str:
    text = str(value or "").strip().upper()
    aliases = {
        "NAS": "NASD",
        "NASDAQ": "NASD",
        "NYS": "NYSE",
        "AMS": "AMEX",
        "HKS": "SEHK",
        "SHS": "SHAA",
        "SZS": "SZAA",
    }
    text = aliases.get(text, text)
    if text not in _SUPPORTED_OVERSEAS_ORDER_EXCHANGES:
        supported = ", ".join(sorted(_SUPPORTED_OVERSEAS_ORDER_EXCHANGES))
        raise KISDirectClientError(f"Unsupported overseas order exchange: {value}. Supported: {supported}.")
    return text


def _canonical_market_from_overseas_exchange(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"NAS", "NASD", "NASDAQ", "NYS", "NYSE", "AMS", "AMEX"}:
        return "US"
    return text or "overseas"


def _optional_date(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return _normalize_date(value)


def _required_date(payload: Mapping[str, Any], key: str) -> str:
    return _normalize_date(_required_text(payload, key))


def _normalize_date(value: Any) -> str:
    text = str(value).strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise KISDirectClientError(f"Date must be YYYY-MM-DD or YYYYMMDD: {value}")
    return text


def _optional_time(value: Any) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip().replace(":", "")
    if len(text) == 4 and text.isdigit():
        text = f"{text}00"
    if len(text) == 5 and text.isdigit():
        text = f"0{text}"
    if len(text) != 6 or not text.isdigit():
        raise KISDirectClientError(f"Time must be HHMMSS or HH:MM:SS: {value}")
    return text


def _normalize_period(value: str, allowed: set[str]) -> str:
    text = str(value or "D").strip().upper()
    if text not in allowed:
        raise KISDirectClientError(f"Unsupported period_code: {value}")
    return text


def _to_int(value: Any, field_name: str) -> int:
    try:
        text = str(value).replace(",", "").strip()
        if text == "":
            text = "0"
        return int(Decimal(text))
    except (AttributeError, TypeError, ValueError, InvalidOperation) as exc:
        raise KISDirectClientError(f"Field '{field_name}' could not be parsed as int.") from exc


def _optional_int(value: Any, *, default: int) -> int:
    if value in (None, ""):
        return default
    try:
        text = str(value).replace(",", "").strip()
        if text == "":
            return default
        return int(Decimal(text))
    except (AttributeError, TypeError, ValueError, InvalidOperation):
        return default


def _to_float(value: Any, field_name: str) -> float:
    try:
        text = str(value).replace(",", "").strip()
        if text == "":
            text = "0"
        return float(text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise KISDirectClientError(f"Field '{field_name}' could not be parsed as float.") from exc


def _optional_float(value: Any, *, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        return float(str(value).replace(",", "").strip())
    except (AttributeError, TypeError, ValueError):
        return default


def _looks_like_domestic_reference_price(
    output: Mapping[str, Any],
    last_price: int,
    open_price: int,
    high_price: int,
    low_price: int,
) -> bool:
    standard_price = _optional_int(output.get("stck_sdpr"), default=0)
    change = _optional_int(output.get("prdy_vrss"), default=0)
    change_rate = _optional_float(output.get("prdy_ctrt"), default=0.0)
    return (
        last_price > 0
        and standard_price > 0
        and last_price == standard_price
        and open_price <= 0
        and high_price <= 0
        and low_price <= 0
        and change == 0
        and abs(change_rate) < 0.000001
    )


def _required_mapping(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise KISDirectClientError(f"KIS payload is missing object field '{key}'.")
    return dict(value)


def _required_sequence(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key)
    if isinstance(value, list):
        return [dict(row) for row in value if isinstance(row, dict)]
    raise KISDirectClientError(f"KIS payload is missing list field '{key}'.")


def _first_mapping(value: Any, label: str) -> dict[str, Any]:
    if isinstance(value, list):
        if not value or not isinstance(value[0], dict):
            raise KISDirectClientError(f"KIS payload is missing object field '{label}'.")
        return dict(value[0])
    if isinstance(value, dict):
        return dict(value)
    raise KISDirectClientError(f"KIS payload is missing object field '{label}'.")


def _list_or_empty(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, dict):
        return [dict(value)]
    if isinstance(value, list):
        return [dict(item) for item in value if isinstance(item, dict)]
    return []


def _history_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    for key in ("candles", "bars", "rows", "output", "output2"):
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(row) for row in value if isinstance(row, dict)]
    return []


def _filter_rows_by_date(
    rows: list[dict[str, Any]],
    *,
    start_date: str | None,
    end_date: str | None,
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if (start_date is None or _row_date(row) >= start_date)
        and (end_date is None or _row_date(row) <= end_date)
    ]


def _row_date(row: Mapping[str, Any]) -> str:
    for key in ("date", "trade_date", "stck_bsop_date", "xymd"):
        text = str(row.get(key) or "").strip().replace("-", "")
        if text:
            return text
    return ""


def _row_time(row: Mapping[str, Any]) -> str:
    for key in ("time", "stck_cntg_hour", "hour", "hhmmss"):
        value = row.get(key)
        if value not in (None, ""):
            return _optional_time(value) or ""
    return ""


def _normalize_side_filter(value: str) -> str:
    mapping = {"all": "00", "buy": "02", "sell": "01"}
    text = value.strip().lower()
    if text not in mapping:
        raise KISDirectClientError("side must be one of: all, buy, sell.")
    return mapping[text]


def _normalize_history_side(row: Mapping[str, Any]) -> str:
    code = str(row.get("sll_buy_dvsn_cd") or "").strip()
    if code == "02":
        return "buy"
    if code == "01":
        return "sell"
    name = str(row.get("sll_buy_dvsn_cd_name") or "").strip().lower()
    if "buy" in name:
        return "buy"
    if "sell" in name:
        return "sell"
    return name


def _join_kis_timestamp(date_value: Any, time_value: Any) -> str:
    date_text = str(date_value or "").strip()
    time_text = str(time_value or "").strip()
    if date_text and time_text:
        return f"{date_text}T{time_text}"
    return date_text or time_text


def _normalize_order_side(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text not in {"buy", "sell"}:
        raise KISDirectClientError("side must be buy or sell.")
    return text


def _positive_int(value: Any, field_name: str) -> int:
    normalized = _non_negative_int(value, field_name)
    if normalized <= 0:
        raise KISDirectClientError(f"{field_name} must be greater than zero.")
    return normalized


def _non_negative_int(value: Any, field_name: str) -> int:
    try:
        normalized = int(str(value).replace(",", "").strip() or "0")
    except (TypeError, ValueError) as exc:
        raise KISDirectClientError(f"{field_name} must be an integer.") from exc
    if normalized < 0:
        raise KISDirectClientError(f"{field_name} must be greater than or equal to zero.")
    return normalized


def _normalize_order_division(value: Any) -> str:
    text = str(value or "00").strip()
    if len(text) != 2 or not text.isdigit():
        raise KISDirectClientError("order_division must be a two-digit KIS order code.")
    return text


def _normalize_order_price(value: Any, *, order_division: str) -> int:
    price = _non_negative_int(value, "price")
    if price == 0 and order_division in _LIMIT_PRICE_ORDER_DIVISIONS:
        raise KISDirectClientError("limit order price must be greater than zero.")
    if order_division in _MARKET_PRICE_ORDER_DIVISIONS:
        return 0
    return price


def _normalize_overseas_order_price(value: Any, *, order_division: str, allow_zero: bool = False) -> str:
    try:
        price = Decimal(str(value).replace(",", "").strip() or "0")
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise KISDirectClientError("price must be numeric.") from exc
    if price < 0:
        raise KISDirectClientError("price must be greater than or equal to zero.")
    if price == 0 and order_division in _LIMIT_PRICE_ORDER_DIVISIONS and not allow_zero:
        raise KISDirectClientError("limit order price must be greater than zero.")
    if order_division in _MARKET_PRICE_ORDER_DIVISIONS:
        return "0"
    return format(price, "f")


def _mock_tr_id(real_tr_id: str) -> str:
    return f"V{real_tr_id[1:]}"


def _overseas_order_tr_id(*, side: str, exchange: str, mock: bool) -> str:
    lookup = _OVERSEAS_BUY_TR_IDS if side == "buy" else _OVERSEAS_SELL_TR_IDS
    tr_id = lookup[exchange]
    return _mock_tr_id(tr_id) if mock else tr_id


def _overseas_revise_cancel_tr_id(*, exchange: str, mock: bool) -> str:
    tr_id = _OVERSEAS_REVISE_CANCEL_TR_IDS[exchange]
    return _mock_tr_id(tr_id) if mock else tr_id


def _normalize_exchange_scope(value: Any) -> str:
    text = str(value or "KRX").strip().upper()
    if text not in {"KRX", "NXT", "SOR"}:
        raise KISDirectClientError("exchange_scope must be one of KRX, NXT, or SOR.")
    return text


def _normalize_cancel_type(value: Any) -> str:
    text = str(value or "").strip()
    if text not in {"01", "02"}:
        raise KISDirectClientError("rvse_cncl_dvsn_cd must be 01 or 02.")
    return text


def _normalize_flag(value: Any) -> str:
    text = str(value or "").strip().upper() or "Y"
    if text not in {"Y", "N"}:
        raise KISDirectClientError("flag must be Y or N.")
    return text


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KISDirectClientError(f"Failed to read KIS cache file: {path}") from exc
    if not isinstance(payload, dict):
        raise KISDirectClientError(f"KIS cache file is not a JSON object: {path}")
    return payload


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
