from tools.leaps_portfolio_report import _format_report


def test_leaps_portfolio_report_includes_portfolio_blend_status():
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
                "cash": 1_000_000,
                "equity": 1_000_000,
                "gross_exposure": 0,
                "gross_exposure_pct": 0,
                "holdings": [],
            }
        },
        "framework": {
            "portfolio_target_batch": {
                "metadata": {
                    "portfolio_blend": {
                        "status": "advancing",
                        "progress": 0.5,
                        "elapsed_minutes": 150,
                        "duration_minutes": 300,
                        "target_drift": 0.12,
                        "transition_id": "portfolio-blend-1",
                        "bypassed_symbols": ["KRX:005930"],
                    }
                }
            },
            "risk": {"decisions": []},
            "execution": {"order_count": 0},
        },
    }

    message = _format_report(payload, sleeve_id="LEaps")

    assert "Portfolio Blend" in message
    assert "status advancing / progress 50.0%" in message
    assert "elapsed 150/300 minutes" in message
    assert "target drift 12.0%" in message
    assert "bypassed KRX:005930" in message
