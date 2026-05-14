from tools.leaps_portfolio_report import _format_report, _friendly_risk_reason


def test_leaps_portfolio_report_formats_korean_status_and_risk_reason():
    payload = {
        "engine_status": {
            "framework": {"active_insight_count": 2},
        },
        "worker": {
            "cycles": [
                {
                    "snapshot_quality": {
                        "status": "fresh",
                        "collected_symbol_count": 1,
                        "requested_symbol_count": 1,
                    }
                }
            ]
        },
        "portfolio_state": {
            "current": {
                "cash": 100_000,
                "equity": 1_100_000,
                "gross_exposure": 1_000_000,
                "gross_exposure_pct": 0.909,
                "holdings": [
                    {
                        "symbol": "KRX:005930",
                        "quantity": 10,
                        "average_price": 90_000,
                        "market_price": 100_000,
                        "market_value": 1_000_000,
                        "cost_basis": 900_000,
                        "unrealized_pnl": 100_000,
                        "unrealized_pnl_pct": 0.1111,
                    }
                ],
            }
        },
        "framework": {
            "order_sizing": {
                "plans": [
                    {
                        "symbol": "KRX:005930",
                        "target_quantity": 12,
                        "reason": "target",
                    }
                ]
            },
            "risk": {
                "decisions": [
                    {
                        "symbol": "KRX:005930",
                        "status": "clamped",
                        "reason": "max_position_pct",
                        "approved_quantity": 11,
                        "original_quantity": 12,
                    }
                ]
            },
            "execution": {"order_count": 1},
            "order_intents": [
                {
                    "symbol": "KRX:005930",
                    "side": "buy",
                    "quantity": 1,
                    "reference_price": 100_000,
                }
            ],
        },
    }

    message = _format_report(payload, sleeve_id="LEaps", realized_pnl=5_000)

    assert "[LEaps] 운용 현황" in message
    assert "KRX:005930 삼성전자" in message
    assert "```" in message
    assert "| 종목" in message
    assert "| KRX:005930 삼성전자 | 10주 | 11주 | +1 매수" in message
    assert "| KRX:005930 삼성전자 | 매수 |  1주 | 100,000 |" in message
    assert "risk clamped:max_position_pct" in message
    assert "Risk 조정/차단 1건" in message


def test_leaps_portfolio_report_explains_too_small_risk_reason():
    payload = {
        "engine_status": {"framework": {"active_insight_count": 1}},
        "worker": {
            "cycles": [
                {
                    "snapshot_quality": {
                        "status": "fresh",
                        "collected_symbol_count": 1,
                        "requested_symbol_count": 1,
                    }
                }
            ]
        },
        "portfolio_state": {
            "current": {
                "cash": 4_000_000,
                "equity": 10_000_000,
                "gross_exposure": 6_000_000,
                "gross_exposure_pct": 0.6,
                "cash_by_currency": {"KRW": 4_000_000},
                "holdings": [
                    {
                        "symbol": "KRX:005930",
                        "quantity": 3,
                        "average_price": 280_000,
                        "market_price": 279_000,
                        "market_value": 837_000,
                        "cost_basis": 840_000,
                        "unrealized_pnl": -3_000,
                        "unrealized_pnl_pct": -0.0036,
                    }
                ],
            }
        },
        "framework": {
            "risk": {
                "decisions": [
                    {
                        "symbol": "KRX:005930",
                        "status": "rejected",
                        "reason": "insufficient_cash_or_position_too_small",
                        "approved_quantity": None,
                        "original_quantity": 11,
                        "metadata": {"currency": "KRW", "available_cash": 4_105_625.4},
                    }
                ]
            },
            "execution": {"order_count": 0},
        },
    }

    message = _format_report(payload, sleeve_id="LEaps")

    assert "추가매수 불가(현금/노출한도; 가용 4,105,625)" in message


def test_leaps_portfolio_report_explains_exposure_limit_no_room():
    message = _friendly_risk_reason(
        "exposure_limit_no_room",
        {"max_total_exposure_pct": 0.60, "market_regime": {"name": "neutral"}},
    )

    assert message == "총 노출한도 꽉 참(한도 60.0%, regime neutral)"
