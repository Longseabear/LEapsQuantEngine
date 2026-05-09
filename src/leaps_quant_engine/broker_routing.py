from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, Symbol


DOMESTIC_MARKETS = frozenset({"KR", "KRX", "KOSPI", "KOSDAQ", "KONEX"})
MARKET_SCOPE_CURRENCIES = {
    "domestic": "KRW",
    "overseas": "USD",
}


@dataclass(frozen=True, slots=True)
class BrokerAccountRoute:
    account_id: str | None
    market_scope: str | None
    account_store_path: Path
    order_store_path: Path


@dataclass(frozen=True, slots=True)
class RoutedOrderIntentBatch:
    account_id: str | None
    market_scope: str | None
    batch: OrderIntentBatch


def market_scope_from_market(market: str) -> str:
    text = str(market or "").strip().upper()
    return "domestic" if text in DOMESTIC_MARKETS else "overseas"


def currency_for_market_scope(market_scope: str | None) -> str:
    return MARKET_SCOPE_CURRENCIES.get(str(market_scope or "").strip().lower(), "KRW")


def currency_for_market(market: str) -> str:
    return currency_for_market_scope(market_scope_from_market(market))


def currency_for_symbol(symbol: Symbol) -> str:
    return currency_for_market(symbol.market)


def market_scope_for_symbol(symbol: Symbol) -> str:
    return market_scope_from_market(symbol.market)


def account_id_for_sleeve_market_scope(sleeve_config: Any, market_scope: str) -> str | None:
    routes = dict(getattr(sleeve_config, "broker_account_routes", {}) or {})
    account_id = routes.get(market_scope)
    if account_id:
        return account_id
    return getattr(sleeve_config, "broker_account_id", None)


def configured_account_ids_for_sleeve(sleeve_config: Any) -> tuple[str, ...]:
    account_ids: list[str] = []
    default_account_id = getattr(sleeve_config, "broker_account_id", None)
    if default_account_id:
        account_ids.append(default_account_id)
    for account_id in dict(getattr(sleeve_config, "broker_account_routes", {}) or {}).values():
        if account_id:
            account_ids.append(str(account_id))
    return tuple(dict.fromkeys(account_ids))


def split_batches_by_account_route(
    *,
    config: Any,
    batches: tuple[OrderIntentBatch, ...],
    allowed_sleeve_ids: tuple[str, ...],
) -> tuple[RoutedOrderIntentBatch, ...]:
    allowed = set(allowed_sleeve_ids)
    grouped: dict[tuple[str | None, str | None], list[tuple[OrderIntentBatch, OrderIntent]]] = {}
    for batch in batches:
        if batch.sleeve_id not in allowed:
            key = (None, None)
            grouped.setdefault(key, []).extend((batch, order) for order in batch.order_intents)
            continue
        sleeve = config.sleeve(batch.sleeve_id)
        for order in batch.order_intents:
            market_scope = market_scope_for_symbol(order.symbol)
            account_id = account_id_for_sleeve_market_scope(sleeve, market_scope)
            grouped.setdefault((account_id, market_scope), []).append((batch, order))

    routed: list[RoutedOrderIntentBatch] = []
    for (account_id, market_scope), batch_orders in grouped.items():
        batches_by_id: dict[str, list[OrderIntent]] = {}
        batch_by_id: dict[str, OrderIntentBatch] = {}
        for batch, order in batch_orders:
            batches_by_id.setdefault(batch.batch_id, []).append(order)
            batch_by_id[batch.batch_id] = batch
        for batch_id, orders in batches_by_id.items():
            original = batch_by_id[batch_id]
            route_suffix = account_id or market_scope or "unrouted"
            routed.append(
                RoutedOrderIntentBatch(
                    account_id=account_id,
                    market_scope=market_scope,
                    batch=OrderIntentBatch(
                        sleeve_id=original.sleeve_id,
                        generated_at=original.generated_at,
                        order_intents=tuple(orders),
                        model_name=original.model_name,
                        reason=original.reason,
                        metadata={
                            **dict(original.metadata),
                            "source_batch_id": original.batch_id,
                            "broker_account_id": account_id,
                            "market_scope": market_scope,
                        },
                        batch_id=f"{original.batch_id}:{route_suffix}",
                    ),
                )
            )
    return tuple(routed)
