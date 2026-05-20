from datetime import datetime
from types import SimpleNamespace

from leaps_quant_engine.alpha import Insight, InsightBatch, InsightDirection
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.framework.risk import RiskDecision, RiskDecisionBatch, RiskDecisionStatus
from leaps_quant_engine.lineage import build_cycle_lineage_summary
from leaps_quant_engine.models import OrderIntent, OrderSide, PortfolioTarget, Symbol
from leaps_quant_engine.orders import OrderCoordinator, OrderEventType
from leaps_quant_engine.virtual_account import PortfolioMutationRecord


def test_build_cycle_lineage_summary_links_symbol_through_order_and_fill():
    symbol = Symbol("005930", "KRX")
    now = datetime(2026, 5, 9, 9, 0)
    insight = Insight(
        sleeve_id="LEaps",
        symbol=symbol,
        direction=InsightDirection.UP,
        generated_at=now,
        source_snapshot_id="snapshot-1",
        alpha_id="alpha-a",
        alpha_version="1",
        insight_id="insight-1",
    )
    target = PortfolioTarget(symbol, 2, tag="target-from-alpha")
    order = OrderIntent("LEaps", symbol, OrderSide.BUY, 2, 70_000)
    batch = OrderIntentBatch("LEaps", now, (order,), batch_id="orders-1")
    ticket = OrderCoordinator().coordinate((batch,), generated_at=now).tickets[0]
    event = ticket.event(
        OrderEventType.FILLED,
        occurred_at=now,
        quantity=2,
        fill_price=70_100,
        broker_order_id="001:0001",
    )
    mutation = PortfolioMutationRecord(
        sleeve_id="LEaps",
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=2,
        fill_price=70_100,
        fee=100,
        realized_pnl_estimate=0,
        before_quantity=0,
        after_quantity=2,
        before_average_price=0,
        after_average_price=70_100,
        before_cash=1_000_000,
        after_cash=859_700,
        currency="KRW",
        fill_id="order-event:event-1",
        order_intent_id=ticket.order_intent_id,
        ticket_id=ticket.ticket_id,
        event_id=event.event_id,
        broker_order_id="001:0001",
        applied_at=now,
    )
    cycle = SimpleNamespace(
        sleeve_id="LEaps",
        active_insights=(insight,),
        new_insight_batch=InsightBatch("LEaps", "u", "snapshot-1", now, ("alpha-a",), (insight,)),
        portfolio_target_batch=SimpleNamespace(batch_id="targets-1"),
        portfolio_targets=(target,),
        risk_decisions=RiskDecisionBatch(
            "LEaps",
            (
                RiskDecision(
                    original_target=target,
                    approved_target=target,
                    status=RiskDecisionStatus.APPROVED,
                    reason="approved",
                ),
            ),
        ),
        execution_batch=batch,
    )

    summary = build_cycle_lineage_summary(
        cycle,
        order_tickets=(ticket,),
        order_events=(event,),
        portfolio_mutations=(mutation,),
    )

    assert summary.symbol_count == 1
    row = summary.symbols[0].to_dict()
    assert row["symbol"] == "KRX:005930"
    assert row["insight_ids"] == ["insight-1"]
    assert row["target_quantity"] == 2
    assert row["order_intent_ids"] == [ticket.order_intent_id]
    assert row["ticket_ids"] == [ticket.ticket_id]
    assert row["event_ids"] == [event.event_id]
    assert row["mutation_fill_ids"] == ["order-event:event-1"]
