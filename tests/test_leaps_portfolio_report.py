import json
import sys

import tools.leaps_portfolio_report as report_module
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
    assert "삼성전자 (005930)" in message
    assert "```" not in message
    assert "수량 10주 -> 11주 (+1 매수)" in message
    assert "- 삼성전자 (005930) 매수 1주 @ 100,000" in message
    assert "리스크 clamped:max_position_pct" in message
    assert "Risk 조정/차단 1건" in message
    assert "누적 실현 추정: +5,000 KRW" in message

    table_message = _format_report(payload, sleeve_id="LEaps", realized_pnl=5_000, layout="table")
    assert "```" in table_message
    assert "| 종목" in table_message
    assert "| KRX:005930 삼성전자 | 10주 | 11주 | +1 매수" in table_message


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


def test_leaps_portfolio_report_latest_target_does_not_recompute(tmp_path, monkeypatch, capsys):
    config_path, account_path, framework_state_path, order_batch_path, order_status_path = _write_fast_report_fixtures(tmp_path)

    def fail_runtime_once(**kwargs):
        raise AssertionError("runtime-run-once must not be called in latest-target mode")

    monkeypatch.setattr(report_module, "_run_runtime_once", fail_runtime_once)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "leaps_portfolio_report.py",
            "--config",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--account-store",
            str(account_path),
            "--framework-state",
            str(framework_state_path),
            "--order-batch",
            str(order_batch_path),
            "--order-status-json",
            str(order_status_path),
            "--out-dir",
            str(tmp_path / "reports"),
            "--mode",
            "latest-target",
        ],
    )

    assert report_module.main() == 0
    output = capsys.readouterr().out

    assert "소스: latest live-cycle target" in output
    assert "수량 1주 -> 3주 (+2 매수)" in output
    assert "- 주문 후보: 1건" in output
    assert "- 미체결 티켓: 1건" in output
    assert "삼성전자 (005930) 매수 2주" in output


def test_leaps_portfolio_report_fast_current_hides_latest_target(tmp_path, monkeypatch, capsys):
    config_path, account_path, framework_state_path, order_batch_path, order_status_path = _write_fast_report_fixtures(tmp_path)

    def fail_runtime_once(**kwargs):
        raise AssertionError("runtime-run-once must not be called in fast-current mode")

    monkeypatch.setattr(report_module, "_run_runtime_once", fail_runtime_once)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "leaps_portfolio_report.py",
            "--config",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--account-store",
            str(account_path),
            "--framework-state",
            str(framework_state_path),
            "--order-batch",
            str(order_batch_path),
            "--order-status-json",
            str(order_status_path),
            "--out-dir",
            str(tmp_path / "reports"),
            "--mode",
            "fast-current",
        ],
    )

    assert report_module.main() == 0
    output = capsys.readouterr().out

    assert "소스: fast current account/order state" in output
    assert "수량 1주 -> 1주 (유지)" in output
    assert "- 주문 후보: 0건" in output
    assert "- 미체결 티켓: 1건" in output


def _write_fast_report_fixtures(tmp_path):
    config_path = tmp_path / "runtime.json"
    account_path = tmp_path / "account.json"
    framework_state_path = tmp_path / "framework-state.json"
    order_batch_path = tmp_path / "orders.json"
    order_status_path = tmp_path / "order-status.json"

    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "broker_accounts": [
                    {
                        "account_id": "kis-domestic",
                        "market_scope": "domestic",
                        "currency": "KRW",
                        "account_store_path": str(account_path),
                    }
                ],
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "broker_account_id": "kis-domestic",
                        "broker_account_routes": {"domestic": "kis-domestic"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    account_path.write_text(
        json.dumps(
            {
                "sleeves": {
                    "LEaps": {
                        "cash": 1_000_000,
                        "cash_by_currency": {"KRW": 1_000_000},
                        "holdings": {
                            "KRX:005930": {
                                "symbol": {"market": "KRX", "ticker": "005930"},
                                "quantity": 1,
                                "average_price": 90_000,
                            }
                        },
                    }
                },
                "fills": {},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    framework_state_path.write_text(
        json.dumps(
            {
                "sleeve_id": "LEaps",
                "updated_at": "2026-05-15T09:01:00",
                "active_insights": [{"insight_id": "i1"}],
                "last_portfolio_target_batch": {
                    "generated_at": "2026-05-15T09:01:00",
                    "metadata": {
                        "portfolio_blend": {
                            "status": "advancing",
                            "progress": 0.5,
                            "duration_minutes": 60,
                            "elapsed_minutes": 30,
                        }
                    },
                    "plans": [
                        {
                            "symbol": "KRX:005930",
                            "current_quantity": 1,
                            "current_price": 100_000,
                            "desired_value": 300_000,
                            "target_percent": 0.3,
                        }
                    ],
                    "targets": [
                        {
                            "symbol": "KRX:005930",
                            "target_percent": 0.3,
                            "tag": "fixture",
                        }
                    ],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    order_batch_path.write_text(
        json.dumps(
            {
                "schema_version": "order_intent_batches.v1",
                "generated_at": "2026-05-15T09:01:00",
                "batch_count": 1,
                "order_count": 1,
                "batches": [
                    {
                        "sleeve_id": "LEaps",
                        "orders": [
                            {
                                "symbol": "KRX:005930",
                                "side": "buy",
                                "quantity": 2,
                                "reference_price": 100_000,
                                "limit_price": 100_100,
                                "metadata": {"current_quantity": 1, "target_quantity": 3},
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    order_status_path.write_text(
        json.dumps(
            {
                "needs_attention": False,
                "routes": [
                    {
                        "broker_account_id": "kis-domestic",
                        "market_scope": "domestic",
                        "currency": "KRW",
                        "order_runtime": {
                            "open_tickets": [
                                {
                                    "sleeve_id": "LEaps",
                                    "symbol": "KRX:005930",
                                    "side": "buy",
                                    "quantity": 2,
                                    "remaining_quantity": 2,
                                    "limit_price": 100_100,
                                    "status": "accepted",
                                    "broker_order_id": "91255:0000000001",
                                }
                            ]
                        },
                        "sleeves": [
                            {
                                "sleeve_id": "LEaps",
                                "open_ticket_count": 1,
                                "pending_buy_notional": 200_200,
                                "pending_sell_quantities": {},
                                "portfolio": {
                                    "cash": 1_000_000,
                                    "cash_by_currency": {"KRW": 1_000_000},
                                    "holdings": [
                                        {
                                            "symbol": "005930",
                                            "market": "KRX",
                                            "quantity": 1,
                                            "average_price": 90_000,
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return config_path, account_path, framework_state_path, order_batch_path, order_status_path
