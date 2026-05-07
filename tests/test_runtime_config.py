from datetime import datetime

from leaps_quant_engine.data import single_bar_slice
from leaps_quant_engine.models import OrderSide, Symbol
from leaps_quant_engine.runtime import build_engine_from_config
from leaps_quant_engine.config import parse_pipeline_config


def test_runtime_builds_multi_sleeve_engine_from_pipeline_config():
    config = parse_pipeline_config(
        {
            "engine": {"mode": "backtest", "timezone": "Asia/Seoul"},
            "sleeves": [
                {
                    "id": "swing-kor",
                    "cash": 1_000_000,
                    "algorithm": "examples.buy_and_hold:BuyAndHoldAlgorithm",
                    "market": "KRX",
                    "symbols": ["005930"],
                    "parameters": {"quantity": 3},
                },
                {
                    "id": "micro-kor",
                    "cash": 500_000,
                    "algorithm": "examples.buy_and_hold:BuyAndHoldAlgorithm",
                    "market": "KRX",
                    "symbols": ["000660"],
                    "parameters": {"quantity": 2},
                },
            ],
        }
    )
    engine = build_engine_from_config(config)
    feed = [
        single_bar_slice(
            datetime(2026, 5, 7, 9, 0),
            {
                Symbol("005930", "KRX"): 70_000,
                Symbol("000660", "KRX"): 180_000,
            },
        )
    ]

    result = engine.run(feed)

    assert [(order.sleeve_id, order.symbol.ticker, order.side, order.quantity) for order in result.orders] == [
        ("swing-kor", "005930", OrderSide.BUY, 3),
        ("micro-kor", "000660", OrderSide.BUY, 2),
    ]
