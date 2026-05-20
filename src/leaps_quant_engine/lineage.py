from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from leaps_quant_engine.models import OrderIntent, Symbol
from leaps_quant_engine.orders import OrderEvent, OrderTicket
from leaps_quant_engine.virtual_account import PortfolioMutationRecord


@dataclass(frozen=True, slots=True)
class SymbolLineageSummary:
    sleeve_id: str
    symbol: Symbol
    insight_ids: tuple[str, ...] = ()
    portfolio_target_batch_id: str = ""
    target_quantity: int | None = None
    risk_statuses: tuple[str, ...] = ()
    order_intent_ids: tuple[str, ...] = ()
    ticket_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    mutation_fill_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sleeve_id": self.sleeve_id,
            "symbol": self.symbol.key,
            "insight_ids": list(self.insight_ids),
            "portfolio_target_batch_id": self.portfolio_target_batch_id,
            "target_quantity": self.target_quantity,
            "risk_statuses": list(self.risk_statuses),
            "order_intent_ids": list(self.order_intent_ids),
            "ticket_ids": list(self.ticket_ids),
            "event_ids": list(self.event_ids),
            "mutation_fill_ids": list(self.mutation_fill_ids),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CycleLineageSummary:
    sleeve_id: str
    symbol_count: int
    symbols: tuple[SymbolLineageSummary, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sleeve_id": self.sleeve_id,
            "symbol_count": self.symbol_count,
            "symbols": [symbol.to_dict() for symbol in self.symbols],
        }


def build_cycle_lineage_summary(
    cycle: Any,
    *,
    order_tickets: Iterable[OrderTicket] = (),
    order_events: Iterable[OrderEvent] = (),
    portfolio_mutations: Iterable[PortfolioMutationRecord] = (),
) -> CycleLineageSummary:
    sleeve_id = str(getattr(cycle, "sleeve_id", ""))
    portfolio_batch = getattr(cycle, "portfolio_target_batch", None)
    execution_batch = getattr(cycle, "execution_batch", None)
    symbol_keys: set[str] = set()

    insights_by_symbol: dict[str, list[str]] = {}
    for insight in tuple(getattr(cycle, "active_insights", ()) or ()) + tuple(getattr(getattr(cycle, "new_insight_batch", None), "insights", ()) or ()):
        symbol = getattr(insight, "symbol", None)
        if symbol is None:
            continue
        symbol_keys.add(symbol.key)
        insight_id = str(getattr(insight, "insight_id", "") or "")
        if insight_id:
            insights_by_symbol.setdefault(symbol.key, []).append(insight_id)

    target_quantity_by_symbol: dict[str, int] = {}
    for target in tuple(getattr(cycle, "portfolio_targets", ()) or ()):
        symbol = getattr(target, "symbol", None)
        if symbol is None:
            continue
        symbol_keys.add(symbol.key)
        target_quantity_by_symbol[symbol.key] = int(getattr(target, "quantity", 0) or 0)

    risk_by_symbol: dict[str, list[str]] = {}
    for decision in tuple(getattr(getattr(cycle, "risk_decisions", None), "decisions", ()) or ()):
        target = getattr(decision, "original_target", None)
        symbol = getattr(target, "symbol", None)
        if symbol is None:
            continue
        symbol_keys.add(symbol.key)
        status = getattr(getattr(decision, "status", None), "value", getattr(decision, "status", ""))
        reason = str(getattr(decision, "reason", "") or "")
        risk_by_symbol.setdefault(symbol.key, []).append(":".join(part for part in (str(status), reason) if part))

    order_ids_by_symbol: dict[str, list[str]] = {}
    for index, order in enumerate(tuple(getattr(execution_batch, "order_intents", ()) or ()), start=1):
        if not isinstance(order, OrderIntent):
            continue
        symbol_keys.add(order.symbol.key)
        batch_id = str(getattr(execution_batch, "batch_id", "") or "")
        order_ids_by_symbol.setdefault(order.symbol.key, []).append(f"{batch_id}:{index}" if batch_id else "")

    ticket_ids_by_symbol: dict[str, list[str]] = {}
    for ticket in order_tickets:
        symbol_keys.add(ticket.symbol.key)
        ticket_ids_by_symbol.setdefault(ticket.symbol.key, []).append(ticket.ticket_id)
        order_ids_by_symbol.setdefault(ticket.symbol.key, []).append(ticket.order_intent_id)

    event_ids_by_symbol: dict[str, list[str]] = {}
    for event in order_events:
        symbol_keys.add(event.symbol.key)
        event_ids_by_symbol.setdefault(event.symbol.key, []).append(event.event_id)
        ticket_ids_by_symbol.setdefault(event.symbol.key, []).append(event.ticket_id)
        order_ids_by_symbol.setdefault(event.symbol.key, []).append(event.order_intent_id)

    mutation_ids_by_symbol: dict[str, list[str]] = {}
    symbol_by_key: dict[str, Symbol] = {}
    for mutation in portfolio_mutations:
        symbol_keys.add(mutation.symbol.key)
        symbol_by_key[mutation.symbol.key] = mutation.symbol
        mutation_ids_by_symbol.setdefault(mutation.symbol.key, []).append(mutation.fill_id)
        if mutation.order_intent_id:
            order_ids_by_symbol.setdefault(mutation.symbol.key, []).append(mutation.order_intent_id)
        if mutation.ticket_id:
            ticket_ids_by_symbol.setdefault(mutation.symbol.key, []).append(mutation.ticket_id)
        if mutation.event_id:
            event_ids_by_symbol.setdefault(mutation.symbol.key, []).append(mutation.event_id)

    for key in symbol_keys:
        symbol_by_key.setdefault(key, _symbol_from_key(key))
    summaries = tuple(
        SymbolLineageSummary(
            sleeve_id=sleeve_id,
            symbol=symbol_by_key[key],
            insight_ids=_unique(insights_by_symbol.get(key, ())),
            portfolio_target_batch_id=str(getattr(portfolio_batch, "batch_id", "") or ""),
            target_quantity=target_quantity_by_symbol.get(key),
            risk_statuses=_unique(risk_by_symbol.get(key, ())),
            order_intent_ids=_unique(item for item in order_ids_by_symbol.get(key, ()) if item),
            ticket_ids=_unique(ticket_ids_by_symbol.get(key, ())),
            event_ids=_unique(event_ids_by_symbol.get(key, ())),
            mutation_fill_ids=_unique(mutation_ids_by_symbol.get(key, ())),
        )
        for key in sorted(symbol_keys)
    )
    return CycleLineageSummary(sleeve_id=sleeve_id, symbol_count=len(summaries), symbols=summaries)


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(value) for value in values if str(value)))


def _symbol_from_key(key: str) -> Symbol:
    if ":" in key:
        market, ticker = key.split(":", 1)
        return Symbol(ticker=ticker, market=market)
    return Symbol(ticker=key)
