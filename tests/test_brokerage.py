from datetime import datetime

from leaps_quant_engine.brokerage import (
    BrokerEngineExecutionGateway,
    BrokerExecutionService,
    PaperBrokerExecutionGateway,
)
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderIntent, OrderSide, OrderType, Symbol, TimeInForce
from leaps_quant_engine.orders import OrderCoordinator, OrderEventType, OrderTicketStatus
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


def _ticket():
    symbol = Symbol("005930", "KRX")
    batch = OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 9, 9, 30),
        order_intents=(OrderIntent("LEaps", symbol, OrderSide.BUY, 2, 70_000),),
        batch_id="batch-1",
    )
    return OrderCoordinator().coordinate((batch,), generated_at=datetime(2026, 5, 9, 9, 31)).tickets[0]


def test_paper_broker_gateway_submits_then_polls_fill_events():
    ticket = _ticket()
    service = BrokerExecutionService(PaperBrokerExecutionGateway())

    submitted = service.submit((ticket,), occurred_at=datetime(2026, 5, 9, 9, 32))

    submitted_ticket = submitted.tickets[0]
    assert submitted.events[0].event_type is OrderEventType.SUBMITTED
    assert submitted_ticket.status is OrderTicketStatus.SUBMITTED
    assert submitted_ticket.broker_order_id == f"paper:{ticket.ticket_id}"

    polled = service.poll(submitted.tickets, occurred_at=datetime(2026, 5, 9, 9, 33))

    filled_ticket = polled.tickets[0]
    assert polled.events[0].event_type is OrderEventType.FILLED
    assert filled_ticket.status is OrderTicketStatus.FILLED
    assert filled_ticket.filled_quantity == 2


class _FakeBrokerEngineQueueClient:
    def __init__(self):
        self.enqueued = []
        self.snapshots = {}

    def call_operation(self, operation, arguments=None):
        raise AssertionError("queue mode should not call operation directly")

    def enqueue_command(self, operation, *, arguments=None, metadata=None):
        self.enqueued.append(
            {
                "operation": operation,
                "arguments": dict(arguments or {}),
                "metadata": dict(metadata or {}),
            }
        )
        return {"command_id": "cmd-00000001", "sequence": 1, "status": "queued"}

    def get_snapshots(self, *, consumer_id, snapshot_type="", resource_id="", limit=200):
        return {
            "consumer_id": consumer_id,
            "snapshot_count": 1,
            "snapshots": [
                {
                    "snapshot_id": f"{snapshot_type}:{resource_id}",
                    "snapshot_type": snapshot_type,
                    "resource_id": resource_id,
                    "payload": {
                        "status": "completed",
                        "result": {
                            "branch_no": "001",
                            "order_no": "00012345",
                        },
                        "error": "",
                    },
                }
            ],
        }


def test_broker_engine_gateway_enqueues_domestic_order_with_stockprogram_dedupe_metadata():
    ticket = _ticket()
    client = _FakeBrokerEngineQueueClient()
    gateway = BrokerEngineExecutionGateway(client=client, consumer_id="test-consumer")

    event = gateway.submit(ticket, occurred_at=datetime(2026, 5, 9, 9, 32))

    assert event.event_type is OrderEventType.SUBMITTED
    assert event.broker_order_id == "cmd-00000001"
    command = client.enqueued[0]
    assert command["operation"] == "place_domestic_cash_order"
    assert command["arguments"] == {
        "side": "buy",
        "symbol": "005930",
        "quantity": 2,
        "price": 70_000,
        "order_division": "00",
        "exchange_scope": "KRX",
        "use_hashkey": False,
    }
    assert command["metadata"]["consumer_id"] == "test-consumer"
    assert command["metadata"]["desired_action"] == "submit"
    assert command["metadata"]["plan_id"] == "batch-1"
    assert command["metadata"]["chain_id"] == ticket.ticket_id
    assert command["metadata"]["strategy_leg_id"] == "LEaps"
    assert command["metadata"]["intent_id"] == ticket.order_intent_id

    submitted_ticket = ticket.apply_event(event)
    accepted = gateway.poll(submitted_ticket, occurred_at=datetime(2026, 5, 9, 9, 34))

    assert accepted[0].event_type is OrderEventType.ACCEPTED
    assert accepted[0].broker_order_id == "001:00012345"


def test_broker_engine_gateway_maps_market_ioc_ticket_to_domestic_order_division():
    symbol = Symbol("005930", "KRX")
    batch = OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 9, 9, 30),
        order_intents=(
            OrderIntent(
                "LEaps",
                symbol,
                OrderSide.BUY,
                2,
                70_000,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.IOC,
            ),
        ),
        batch_id="batch-market",
    )
    ticket = OrderCoordinator().coordinate((batch,), generated_at=datetime(2026, 5, 9, 9, 31)).tickets[0]
    client = _FakeBrokerEngineQueueClient()
    gateway = BrokerEngineExecutionGateway(client=client)

    gateway.submit(ticket, occurred_at=datetime(2026, 5, 9, 9, 32))

    assert client.enqueued[0]["arguments"]["order_division"] == "13"
    assert client.enqueued[0]["arguments"]["price"] == 0


def test_broker_engine_gateway_maps_domestic_after_hours_close_to_order_division():
    symbol = Symbol("005930", "KRX")
    batch = OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 13, 15, 41),
        order_intents=(
            OrderIntent(
                "LEaps",
                symbol,
                OrderSide.SELL,
                2,
                70_000,
                order_type=OrderType.LIMIT,
                limit_price=70_000,
                time_in_force=TimeInForce.DAY,
                metadata={"order_session": "after_hours_close"},
            ),
        ),
        batch_id="batch-after-hours-close",
    )
    ticket = OrderCoordinator().coordinate((batch,), generated_at=datetime(2026, 5, 13, 15, 42)).tickets[0]
    client = _FakeBrokerEngineQueueClient()
    gateway = BrokerEngineExecutionGateway(client=client)

    gateway.submit(ticket, occurred_at=datetime(2026, 5, 13, 15, 42))

    command = client.enqueued[0]
    assert command["arguments"]["order_division"] == "06"
    assert command["metadata"]["order_session"] == "after_hours_close"
    assert command["metadata"]["order_division"] == "06"


def test_broker_engine_gateway_maps_domestic_after_hours_single_price_to_order_division():
    symbol = Symbol("005930", "KRX")
    batch = OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 13, 16, 1),
        order_intents=(
            OrderIntent(
                "LEaps",
                symbol,
                OrderSide.BUY,
                2,
                70_000,
                order_type=OrderType.LIMIT,
                limit_price=70_000,
                time_in_force=TimeInForce.DAY,
                metadata={"market_session_phase": "after_hours_single_price"},
            ),
        ),
        batch_id="batch-after-hours-single-price",
    )
    ticket = OrderCoordinator().coordinate((batch,), generated_at=datetime(2026, 5, 13, 16, 2)).tickets[0]
    client = _FakeBrokerEngineQueueClient()
    gateway = BrokerEngineExecutionGateway(client=client)

    gateway.submit(ticket, occurred_at=datetime(2026, 5, 13, 16, 2))

    command = client.enqueued[0]
    assert command["arguments"]["order_division"] == "07"
    assert command["metadata"]["order_session"] == "after_hours_single_price"
    assert command["metadata"]["order_division"] == "07"


def test_broker_engine_gateway_uses_limit_price_for_domestic_limit_order():
    symbol = Symbol("005930", "KRX")
    batch = OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=datetime(2026, 5, 9, 9, 30),
        order_intents=(
            OrderIntent(
                "LEaps",
                symbol,
                OrderSide.BUY,
                2,
                70_000,
                order_type=OrderType.LIMIT,
                limit_price=70_150.4,
            ),
        ),
        batch_id="batch-limit",
    )
    ticket = OrderCoordinator().coordinate((batch,), generated_at=datetime(2026, 5, 9, 9, 31)).tickets[0]
    client = _FakeBrokerEngineQueueClient()
    gateway = BrokerEngineExecutionGateway(client=client)

    gateway.submit(ticket, occurred_at=datetime(2026, 5, 9, 9, 32))

    assert client.enqueued[0]["arguments"]["order_division"] == "00"
    assert client.enqueued[0]["arguments"]["price"] == 70_200


def test_broker_engine_gateway_enqueues_overseas_etf_order_with_kis_exchange():
    symbol = Symbol("SMH", "US")
    batch = OrderIntentBatch(
        sleeve_id="us_etf_rotation",
        generated_at=datetime(2026, 5, 11, 22, 30),
        order_intents=(
            OrderIntent(
                "us_etf_rotation",
                symbol,
                OrderSide.BUY,
                1,
                569.55,
                order_type=OrderType.LIMIT,
                limit_price=569.55,
            ),
        ),
        batch_id="batch-us",
    )
    ticket = OrderCoordinator().coordinate((batch,), generated_at=datetime(2026, 5, 11, 22, 31)).tickets[0]
    client = _FakeBrokerEngineQueueClient()
    gateway = BrokerEngineExecutionGateway(client=client)

    gateway.submit(ticket, occurred_at=datetime(2026, 5, 11, 22, 32))

    command = client.enqueued[0]
    assert command["operation"] == "place_overseas_stock_order"
    assert command["arguments"] == {
        "side": "buy",
        "exchange": "NASD",
        "symbol": "SMH",
        "quantity": 1,
        "price": 569.55,
        "order_division": "00",
        "use_hashkey": False,
    }


def test_broker_engine_gateway_rounds_us_buy_limit_price_up_to_kis_tick():
    symbol = Symbol("SMH", "US")
    batch = OrderIntentBatch(
        sleeve_id="us_etf_rotation",
        generated_at=datetime(2026, 5, 11, 22, 30),
        order_intents=(
            OrderIntent(
                "us_etf_rotation",
                symbol,
                OrderSide.BUY,
                1,
                570.885,
                order_type=OrderType.LIMIT,
                limit_price=570.885,
            ),
        ),
        batch_id="batch-us",
    )
    ticket = OrderCoordinator().coordinate((batch,), generated_at=datetime(2026, 5, 11, 22, 31)).tickets[0]
    client = _FakeBrokerEngineQueueClient()
    gateway = BrokerEngineExecutionGateway(client=client)

    gateway.submit(ticket, occurred_at=datetime(2026, 5, 11, 22, 32))

    assert client.enqueued[0]["arguments"]["price"] == 570.89


def test_broker_engine_gateway_rounds_us_sell_limit_price_down_to_kis_tick():
    symbol = Symbol("XLK", "US")
    batch = OrderIntentBatch(
        sleeve_id="us_etf_rotation",
        generated_at=datetime(2026, 5, 11, 22, 30),
        order_intents=(
            OrderIntent(
                "us_etf_rotation",
                symbol,
                OrderSide.SELL,
                3,
                176.795,
                order_type=OrderType.LIMIT,
                limit_price=176.795,
            ),
        ),
        batch_id="batch-us",
    )
    ticket = OrderCoordinator().coordinate((batch,), generated_at=datetime(2026, 5, 11, 22, 31)).tickets[0]
    client = _FakeBrokerEngineQueueClient()
    gateway = BrokerEngineExecutionGateway(client=client)

    gateway.submit(ticket, occurred_at=datetime(2026, 5, 11, 22, 32))

    assert client.enqueued[0]["arguments"]["price"] == 176.79


class _FakeBrokerEngineCallClient:
    def __init__(self):
        self.called = []

    def call_operation(self, operation, arguments=None):
        self.called.append({"operation": operation, "arguments": dict(arguments or {})})
        return {
            "branch_no": "001",
            "order_no": "00099999",
            "market": "domestic",
        }


def test_broker_engine_gateway_can_call_operation_directly_for_synchronous_submission():
    ticket = _ticket()
    client = _FakeBrokerEngineCallClient()
    gateway = BrokerEngineExecutionGateway(client=client, use_command_queue=False)

    event = gateway.submit(ticket, occurred_at=datetime(2026, 5, 9, 9, 32))

    assert event.event_type is OrderEventType.ACCEPTED
    assert event.broker_order_id == "001:00099999"
    assert client.called[0]["operation"] == "place_domestic_cash_order"


def test_virtual_account_registers_ticket_broker_alias_and_applies_fill_order_event(tmp_path):
    ticket = _ticket()
    store = VirtualSleeveAccountStore(tmp_path / "accounts.json", default_cash_by_sleeve={"LEaps": 1_000_000})

    store.register_order_ticket(ticket)
    accepted = ticket.event(
        OrderEventType.ACCEPTED,
        occurred_at=datetime(2026, 5, 9, 9, 32),
        broker_order_id="001:00012345",
    )
    store.apply_order_event(accepted)

    ownership = store.ownership_for_order(ticket.order_intent_id)
    assert ownership is not None
    assert ownership.broker_order_id == "001:00012345"

    fill = ticket.event(
        OrderEventType.FILLED,
        occurred_at=datetime(2026, 5, 9, 9, 35),
        quantity=2,
        fill_price=70_100,
        broker_order_id="00012345",
    )
    portfolio = store.apply_order_event(fill)

    assert portfolio.cash == 859_800
    assert portfolio.quantity(ticket.symbol) == 2
    assert portfolio.holdings[ticket.symbol.key].average_price == 70_100
