import json
from datetime import datetime

import pytest

from leaps_quant_engine.cli import main
from leaps_quant_engine.operator_ui import (
    APP_JS,
    INDEX_HTML,
    OPERATOR_UI_ASSET_VERSION,
    STYLES_CSS,
    _cash_flows_by_sleeve_currency,
    _pnl_since_eod_by_currency,
    _return_since_eod_by_currency,
    build_operator_dashboard_snapshot,
)


def test_operator_dashboard_snapshot_does_not_create_missing_account_store(tmp_path):
    config_path = tmp_path / "runtime.json"
    account_store_path = tmp_path / "accounts" / "leaps.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "operator-ui-test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "journal_path": "runtime/cycles.jsonl",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "display_name": "LEaps Display",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = build_operator_dashboard_snapshot(config_path, sleeve_ids=("LEaps",), include_details=False)

    assert payload["schema_version"] == "operator_dashboard_snapshot.v1"
    assert payload["source"] == {
        "snapshot_only": True,
        "kis_api": "not_called",
        "market_data_provider": "not_called",
        "writes_runtime_state": False,
    }
    assert payload["runtime"]["runtime_id"] == "operator-ui-test"
    assert payload["summary"]["sleeve_count"] == 1
    assert payload["sleeve_display_names"] == {"LEaps": "LEaps Display"}
    assert payload["order_routes"][0]["sleeves"][0]["display_name"] == "LEaps Display"
    assert payload["order_routes"][0]["sleeves"][0]["portfolio"]["cash"] == 1000
    allocation = payload["order_routes"][0]["sleeves"][0]["allocation"]
    assert allocation["basis"] == "book_cost"
    assert allocation["total_value"] == 1000
    assert allocation["segments"][0]["label"] == "Cash"
    assert payload["current_estimates"]["source"]["status"] == "unavailable"
    assert payload["order_routes"][0]["sleeves"][0]["current_estimate"]["status"] == "unavailable"
    assert payload["daily_performance"]["summary_count"] == 0
    assert payload["cash_availability"]["available_cash_by_currency"] == {"KRW": 0.0}
    assert any("missing_cash_snapshot" in warning for warning in payload["warnings"])
    assert account_store_path.exists() is False


def test_operator_dashboard_snapshot_includes_sleeve_strategy_doc(tmp_path):
    config_path = tmp_path / "runtime.json"
    workspace = tmp_path / "sleeves" / "LEaps"
    workspace.mkdir(parents=True)
    (workspace / "STRATEGY.md").write_text(
        "\n".join(
            [
                "---",
                "schema_version: leaps_sleeve_strategy.v1",
                "sleeve_id: LEaps",
                "---",
                "# Test Strategy",
                "",
                "## ABSTRACT",
                "",
                "This abstract stays visible by default.",
                "",
                "## Recent Judgment Rationale",
                "",
                "Today uses the latest operator judgment.",
                "",
                "## Cadence",
                "",
                "- runtime cycle: daily",
                "",
                "Full details stay behind the toggle.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "operator-ui-strategy-doc-test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "workspace_path": str(workspace),
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = build_operator_dashboard_snapshot(config_path, sleeve_ids=("LEaps",), include_details=False)

    doc = payload["strategy_docs"]["LEaps"]
    assert doc["exists"] is True
    assert doc["title"] == "Test Strategy"
    assert doc["line_count"] >= 10
    assert doc["char_count"] > 0
    assert doc["abstract"] == "This abstract stays visible by default."
    assert doc["recent_judgment_rationale"] == "Today uses the latest operator judgment."
    assert "Full details stay behind the toggle." in doc["content"]
    assert doc["warnings"] == []


def test_operator_dashboard_snapshot_includes_book_allocation_for_holdings(tmp_path):
    config_path = tmp_path / "runtime.json"
    account_store_path = tmp_path / "accounts" / "leaps.json"
    account_store_path.parent.mkdir()
    account_store_path.write_text(
        json.dumps(
            {
                "sleeves": {
                    "LEaps": {
                        "cash": 1000,
                        "cash_by_currency": {"KRW": 1000},
                        "holdings": {
                            "KRX:005930": {
                                "symbol": {"ticker": "005930", "market": "KRX"},
                                "quantity": 2,
                                "average_price": 500,
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "operator-ui-allocation-test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = build_operator_dashboard_snapshot(config_path, sleeve_ids=("LEaps",), include_details=False)

    allocation = payload["order_routes"][0]["sleeves"][0]["allocation"]
    assert allocation["total_value"] == 2000
    assert [segment["label"] for segment in allocation["segments"]] == ["Cash", "삼성전자 (005930)"]
    assert [segment.get("symbol") for segment in allocation["segments"]] == [None, "KRX:005930"]
    assert [segment["weight"] for segment in allocation["segments"]] == [0.5, 0.5]


def test_operator_dashboard_current_estimate_allows_first_eod_when_cash_flow_baseline_is_present():
    daily_performance = {
        "latest_by_sleeve_currency": {
            "kr-domestic-4401:KRW": {
                "equity": 14_310_580,
                "cumulative_cash_flow": 13_721_557,
                "previous_equity": None,
                "daily_pnl": None,
            }
        }
    }

    pnl = _pnl_since_eod_by_currency(
        "kr-domestic-4401",
        equity_by_currency={"KRW": 14_310_580},
        cash_flow_by_currency={"KRW": 13_721_557},
        daily_performance=daily_performance,
    )

    assert pnl == {"KRW": 0.0}


def test_operator_dashboard_current_estimate_suppresses_today_for_missing_cash_flow_baseline():
    daily_performance = {
        "latest_by_sleeve_currency": {
            "kr-domestic-4401:KRW": {
                "equity": 14_310_580,
                "cumulative_cash_flow": 0,
                "previous_equity": None,
                "daily_pnl": None,
            }
        }
    }

    pnl = _pnl_since_eod_by_currency(
        "kr-domestic-4401",
        equity_by_currency={"KRW": 14_310_580},
        cash_flow_by_currency={"KRW": 13_721_557},
        daily_performance=daily_performance,
    )

    assert pnl == {}


def test_operator_dashboard_today_return_uses_cash_flow_adjusted_denominator():
    daily_performance = {
        "latest_by_sleeve_currency": {
            "semiconduct-kor:KRW": {
                "equity": 5_324_750,
                "cumulative_cash_flow": 6_450_500,
                "previous_equity": None,
                "daily_pnl": None,
            }
        }
    }

    returns = _return_since_eod_by_currency(
        "semiconduct-kor",
        equity_by_currency={"KRW": 15_577_600},
        cash_flow_by_currency={"KRW": 16_450_225},
        daily_performance=daily_performance,
    )

    assert returns["KRW"] == pytest.approx(253_125 / 15_324_475)


def test_operator_dashboard_cash_flows_dedupe_across_account_ledgers(tmp_path):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    payload = {
        "cash_transfers": {
            "transfer-1": {
                "transfer_id": "transfer-1",
                "from_sleeve_id": "default sleeve",
                "to_sleeve_id": "semiconduct-kor",
                "amount": 1_000,
                "currency": "KRW",
            }
        }
    }
    first.write_text(json.dumps(payload), encoding="utf-8")
    second.write_text(json.dumps(payload), encoding="utf-8")

    flows = _cash_flows_by_sleeve_currency(
        (
            {"account_store_path": str(first), "currency": "KRW"},
            {"account_store_path": str(second), "currency": "KRW"},
        )
    )

    assert flows["semiconduct-kor"]["KRW"] == 1_000


def test_operator_dashboard_snapshot_keeps_sleeves_on_configured_routes(tmp_path):
    config_path = tmp_path / "runtime.json"
    domestic_store = tmp_path / "accounts" / "domestic.json"
    domestic_4401_store = tmp_path / "accounts" / "domestic_4401.json"
    domestic_store.parent.mkdir()
    domestic_store.write_text(
        json.dumps({"sleeves": {"LEaps": {"cash": 1000, "cash_by_currency": {"KRW": 1000}, "holdings": {}}}}),
        encoding="utf-8",
    )
    domestic_4401_store.write_text(
        json.dumps(
            {
                "sleeves": {
                    "kr-domestic-4401": {
                        "cash": 2000,
                        "cash_by_currency": {"KRW": 2000},
                        "holdings": {},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "operator-ui-route-scope-test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "broker_accounts": [
                    {
                        "account_id": "kis-domestic",
                        "market_scope": "domestic",
                        "currency": "KRW",
                        "account_store_path": str(domestic_store),
                        "order_store_path": "orders/domestic.jsonl",
                    },
                    {
                        "account_id": "kis-domestic-4401",
                        "market_scope": "domestic",
                        "currency": "KRW",
                        "account_store_path": str(domestic_4401_store),
                        "order_store_path": "orders/domestic_4401.jsonl",
                    },
                ],
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "broker_account_id": "kis-domestic",
                        "broker_account_routes": {"domestic": "kis-domestic"},
                        "universe": {"coarse_path": "universe.json"},
                    },
                    {
                        "sleeve_id": "kr-domestic-4401",
                        "broker_account_id": "kis-domestic-4401",
                        "broker_account_routes": {"domestic": "kis-domestic-4401"},
                        "universe": {"coarse_path": "universe.json"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = build_operator_dashboard_snapshot(
        config_path,
        sleeve_ids=("LEaps", "kr-domestic-4401"),
        include_details=False,
    )

    sleeves_by_route = {
        route["broker_account_id"]: [sleeve["sleeve_id"] for sleeve in route["sleeves"]]
        for route in payload["order_routes"]
    }
    assert sleeves_by_route == {
        "kis-domestic": ["LEaps"],
        "kis-domestic-4401": ["kr-domestic-4401"],
    }


def test_operator_dashboard_snapshot_uses_universe_symbol_names(tmp_path):
    config_path = tmp_path / "runtime.json"
    account_store_path = tmp_path / "accounts" / "leaps.json"
    universe_path = tmp_path / "universe.json"
    account_store_path.parent.mkdir()
    account_store_path.write_text(
        json.dumps(
            {
                "sleeves": {
                    "LEaps": {
                        "cash": 0,
                        "cash_by_currency": {"KRW": 0},
                        "holdings": {
                            "KRX:123456": {
                                "symbol": {"ticker": "123456", "market": "KRX"},
                                "quantity": 2,
                                "average_price": 500,
                            }
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    universe_path.write_text(
        json.dumps(
            {
                "id": "named-universe",
                "market": "KRX",
                "symbols": [{"ticker": "123456", "market": "KRX", "name": "테스트전자"}],
                "indicators": [],
            }
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "operator-ui-symbol-name-test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = build_operator_dashboard_snapshot(config_path, sleeve_ids=("LEaps",), include_details=False)

    allocation = payload["order_routes"][0]["sleeves"][0]["allocation"]
    assert payload["symbol_names"]["KRX:123456"] == "테스트전자"
    assert allocation["segments"][0]["label"] == "테스트전자 (123456)"


def test_operator_dashboard_js_renders_all_allocation_segments():
    assert "segments.map((segment, index)" in APP_JS
    assert "segments.slice(0, 5)" not in APP_JS


def test_operator_dashboard_js_groups_route_views_into_sleeve_sections():
    assert "function sleeveSections(snapshot)" in APP_JS
    assert "display_name: sleeve.display_name || sleeveId" in APP_JS
    assert "section.routes.map(renderRoutePanel)" in APP_JS
    assert "function renderSleeveTab(section, isSelected)" in APP_JS
    assert "const displayName = section.display_name || section.sleeve_id || 'unknown'" in APP_JS
    assert "selectedSleeveSection(sections)" in APP_JS
    assert "role=\"tablist\"" in APP_JS
    assert "role=\"tab\"" in APP_JS
    assert "data-sleeve-id" in APP_JS
    assert ".sleeve-tabs" in STYLES_CSS
    assert 'id="sleeves-panel"' in INDEX_HTML
    assert "function sleeveSlideButton(direction, disabled, label, title)" in APP_JS
    assert "function selectAdjacentSleeve(direction)" in APP_JS
    assert "data-sleeve-slide" in APP_JS
    assert "const sleeveSwipeMinDelta = 60" in APP_JS
    assert "function beginSleeveSwipe(event)" in APP_JS
    assert "function finishSleeveSwipe(event)" in APP_JS
    assert "document.addEventListener('pointerdown', beginSleeveSwipe)" in APP_JS
    assert ".sleeve-detail-nav" in STYLES_CSS
    assert ".sleeve-slide-button" in STYLES_CSS
    assert "touch-action: pan-y" in STYLES_CSS
    assert "route views" not in APP_JS


def test_operator_dashboard_js_supports_local_sleeve_ordering():
    assert "const sleeveOrderStorageKey = 'leaps.operatorUi.sleeveOrder.v1'" in APP_JS
    assert "function orderedSleeveSections(sections)" in APP_JS
    assert "function moveSleeveOrder(sleeveId, direction)" in APP_JS
    assert "function sleeveOrderControls(section, index, total)" in APP_JS
    assert "function sleeveOrderButton(section, direction, disabled, label, title)" in APP_JS
    assert "data-sleeve-order" in APP_JS
    assert "window.localStorage.getItem(sleeveOrderStorageKey)" in APP_JS
    assert "window.localStorage.setItem(sleeveOrderStorageKey" in APP_JS
    assert ".summary-card" in STYLES_CSS
    assert ".sleeve-order-button" in STYLES_CSS
    assert ".sleeve-tab-item" in STYLES_CSS


def test_operator_dashboard_js_marks_missing_eod_performance():
    assert "daily_performance" in APP_JS
    assert "history_by_sleeve_currency" in APP_JS
    assert "function performanceHistoryForSleeve(snapshot, sleeveId, currencies)" in APP_JS
    assert "function dailyReturnChart(section)" in APP_JS
    assert "Daily return" in APP_JS
    assert ".daily-return-panel" in STYLES_CSS
    assert ".daily-return-line" in STYLES_CSS
    assert "sectionMetric('EOD return'" not in APP_JS
    assert "miniValue('EOD return'" not in APP_JS


def test_operator_dashboard_js_renders_sleeve_summary_jump_cards():
    assert "function renderSleeveOverview(snapshot)" in APP_JS
    assert 'id="portfolio-overview"' in INDEX_HTML
    assert "renderPortfolioOverview(snapshot, sections)" in APP_JS
    assert "summarySectionsByReturn(sections)" in APP_JS
    assert "function sectionReturnSortValue(section)" in APP_JS
    assert "function aggregateMoneyField(sections, field, missingLabel = 'Unavailable')" in APP_JS
    assert "function aggregateTodayField(sections, missingLabel = 'No EOD')" in APP_JS
    assert "function cashAvailabilityField(report)" in APP_JS
    assert "portfolioSummaryItem('Cash check', cashAvailabilityField(snapshot.cash_availability)" in APP_JS
    assert "moneyMapText(report.available_cash_by_currency || {}, '-')" in APP_JS
    assert "portfolioSummaryItem('Today', aggregateTodayField(sections)" in APP_JS
    assert "currentEstimateCurrencyRawValue(estimate, 'return_since_eod_by_currency', currency)" in APP_JS
    assert "function aggregateTone(sections, field)" in APP_JS
    assert ".portfolio-overview" in STYLES_CSS
    assert ".portfolio-summary-item" in STYLES_CSS
    assert ".portfolio-summary-item .value.warning" in STYLES_CSS
    assert "summary-link" in APP_JS
    assert "href=\"#${escapeHtml(sectionDomId(section))}\"" in APP_JS
    assert "data-sleeve-id=\"${escapeHtml(section.sleeve_id || 'unknown')}\"" in APP_JS
    assert "summary-return-label" in APP_JS
    assert "const summaryReturnLabel = 'Total return';" in APP_JS
    assert "const summaryPnl = currentMoneyField(section.routes, 'total_pnl_by_currency')" in APP_JS
    assert "const summaryEquity = currentEquityField(section.routes)" in APP_JS
    assert "summary-return-money" in APP_JS
    assert "summary-return-equity" in APP_JS
    assert "const todayPnl = currentMoneyField(section.routes, 'pnl_since_eod_by_currency', 'No EOD')" in APP_JS
    assert "const todayReturn = currentReturnField(section.routes, 'return_since_eod_by_currency', 'No EOD')" in APP_JS
    assert "summary-today-row" in APP_JS
    assert "summary-today-label" in APP_JS
    assert "summaryMini('Today +/-'" not in APP_JS
    assert "summaryMini('Today %'" not in APP_JS
    assert ".summary-return-money" in STYLES_CSS
    assert ".summary-return-equity" in STYLES_CSS
    assert ".summary-today-row" in STYLES_CSS
    assert "sleeve-tab-pnl" in APP_JS
    assert "summaryVisuals(section)" in APP_JS
    assert OPERATOR_UI_ASSET_VERSION in INDEX_HTML
    assert "operator-ui-summary-visuals" in OPERATOR_UI_ASSET_VERSION


def test_operator_dashboard_js_distinguishes_eod_equity_from_cost_basis():
    assert "const hasCurrentReturn = currentReturnAvailable(section.routes, 'total_return_by_currency')" in APP_JS
    assert "currentReturnField(section.routes, 'total_return_by_currency')" in APP_JS
    assert "amount.toLocaleString(undefined, { maximumFractionDigits: 0 })" in APP_JS
    assert "function summaryPnlChart(section)" in APP_JS
    assert "function summaryAssetChart(section)" in APP_JS
    assert "asset-donut-layout" in APP_JS
    assert "function summaryAssetSegments(section, currency, stockValue, cashValue, total)" in APP_JS
    assert "function compactAssetSegments(segments, total)" in APP_JS
    assert "function assetHoldingRow(segment, index, currency)" in APP_JS
    assert "function assetDonutBackground(segments)" in APP_JS
    assert "asset-holding-list" in APP_JS
    assert ".asset-holding-row" in STYLES_CSS
    assert ".asset-donut-label" in STYLES_CSS
    assert "function chartRow(label, datum, max)" in APP_JS
    assert "summaryMini('Current equity'" not in APP_JS
    assert "summaryMini('Stock value'" in APP_JS
    assert "summaryMini('Cash'" in APP_JS
    assert "summaryMini('EOD equity'" not in APP_JS
    assert "summaryMini('EOD P&L'" not in APP_JS
    assert "summaryMini('Cost basis'" not in APP_JS
    assert "sectionMetric('Total return'" in APP_JS
    assert "sectionMetric('Total P&L'" in APP_JS
    assert "sectionMetric('Today +/-', currentMoneyField(section.routes, 'pnl_since_eod_by_currency', 'No EOD')" in APP_JS
    assert "sectionMetric('Today %', currentReturnField(section.routes, 'return_since_eod_by_currency', 'No EOD')" in APP_JS
    assert "sectionMetric('Realized P&L'" in APP_JS
    assert "sectionMetric('Unrealized P&L'" in APP_JS
    assert "sectionMetric('Since EOD'" not in APP_JS
    assert "miniValue('Cost basis'" in APP_JS
    assert "miniValue('EOD equity'" not in APP_JS
    assert "function currentEstimatePanel(sleeve)" in APP_JS
    assert "function positionTargetList(sleeve)" in APP_JS
    assert "function signedPercent(value)" in APP_JS
    assert "function toneForNumber(value)" in APP_JS
    assert "position.total_pnl_pct" in APP_JS
    assert "position.today_pnl_pct" in APP_JS
    assert "position-pnl-rate" in APP_JS
    assert "position-pnl-badge" not in APP_JS
    assert "position-pnl-cell" in APP_JS
    assert "filter(isVisiblePortfolioPosition)" in APP_JS
    assert "function isVisiblePortfolioPosition(position)" in APP_JS
    assert "currentPercent >= 0.00005" in APP_JS
    assert "function currentReturnField(routes, field, missingLabel = 'Unavailable')" in APP_JS
    assert "function currentMoneyField(routes, field, missingLabel = 'Unavailable')" in APP_JS
    assert "function currentEstimateMoneyField(estimate, field, currency, missingLabel = 'Unavailable')" in APP_JS
    assert "function currentEstimatePercentField(estimate, field, currency, missingLabel = 'Unavailable')" in APP_JS
    assert "currentEstimateMoneyField(estimate, 'pnl_since_eod_by_currency', currency, 'No EOD')" in APP_JS
    assert "currentEstimatePercentField(estimate, 'return_since_eod_by_currency', currency, 'No EOD')" in APP_JS
    assert "currentEstimateCurrencyValue(estimate, 'pnl_since_eod_by_currency'" not in APP_JS
    assert "currentEstimateCurrencyValue(estimate, 'return_since_eod_by_currency'" not in APP_JS
    assert "function currentEquityField(routes)" in APP_JS
    assert "function currentMoneyDatum(routes, field)" in APP_JS
    assert "function isPnlMoneyField(field)" in APP_JS
    assert "Est. equity" in APP_JS
    assert "Target %" in APP_JS
    assert "Today %" in APP_JS
    assert "P&L %" in APP_JS
    assert "Total return" in APP_JS
    assert "Realized P&L" in APP_JS
    assert "summaryMini('Book'" not in APP_JS
    assert "<span>Book</span>" not in APP_JS


def test_operator_dashboard_js_auto_refreshes_snapshot_safely():
    assert "const autoRefreshMs = 30000" in APP_JS
    assert "selectedSleeveId: null" in APP_JS
    assert "function startAutoRefresh()" in APP_JS
    assert "window.setInterval" in APP_JS
    assert "document.hidden" in APP_JS
    assert "refreshInFlight" in APP_JS


def test_operator_dashboard_js_renders_strategy_doc_toggle():
    assert "function strategyDocPanel(section)" in APP_JS
    assert "snapshot.strategy_docs" in APP_JS
    assert "strategy-abstract" in APP_JS
    assert "recent_judgment_rationale" in APP_JS
    assert "Recent Judgment Rationale" in APP_JS
    assert "strategy-rationale" in APP_JS
    assert ".strategy-rationale" in STYLES_CSS
    assert ".position-pnl-rate" in STYLES_CSS
    assert ".position-pnl-badge" not in STYLES_CSS
    assert "<details class=\"strategy-details\">" in APP_JS
    assert "Full STRATEGY.md" in APP_JS
    assert "STRATEGY.md not found in sleeve workspace" in APP_JS


def test_operator_dashboard_snapshot_includes_sleeve_daily_performance(tmp_path):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "operator-ui-performance-test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    for date, equity in (("2026-05-20", 1000), ("2026-05-21", 1100)):
        report_dir = tmp_path / "data" / "eod-snapshots" / date / "eod" / "domestic_LEaps" / "portfolio-report"
        report_dir.mkdir(parents=True)
        (report_dir / f"LEaps_runtime_{date.replace('-', '')}_160000.json").write_text(
            json.dumps(
                {
                    "generated_at": f"{date}T16:00:00+09:00",
                    "portfolio_state": {
                        "current": {
                            "sleeve_id": "LEaps",
                            "cash_by_currency": {"KRW": equity},
                            "equity_by_currency": {"KRW": equity},
                            "holdings": [],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

    payload = build_operator_dashboard_snapshot(config_path, sleeve_ids=("LEaps",), include_details=False)

    latest = payload["daily_performance"]["latest_by_sleeve_currency"]["LEaps:KRW"]
    assert latest["date"] == "2026-05-21"
    assert latest["daily_pnl"] == 100
    assert latest["daily_return"] == 0.1
    history = payload["daily_performance"]["history_by_sleeve_currency"]["LEaps:KRW"]
    assert [row["date"] for row in history] == ["2026-05-20", "2026-05-21"]
    assert history[-1]["daily_return"] == 0.1


def test_operator_dashboard_snapshot_includes_snapshot_only_current_estimate(tmp_path):
    config_path = tmp_path / "runtime.json"
    runtime_latest = tmp_path / "data" / "runtime" / "live-order-loop" / "multi_sleeve_runtime_run_latest.json"
    runtime_latest.parent.mkdir(parents=True)
    runtime_latest.write_text(
        json.dumps(
            {
                "completed_at": "2026-05-22T09:02:35",
                "reports": [
                    {
                        "sleeve_id": "LEaps",
                        "engine_status": {
                            "portfolio_engine_state": {
                                "current": {
                                    "sleeve_id": "LEaps",
                                    "as_of": "2026-05-22T09:02:30",
                                    "cash": 1100,
                                    "cash_by_currency": {"KRW": 1100},
                                    "equity": 2300,
                                    "equity_by_currency": {"KRW": 2300},
                                    "holdings": [
                                        {
                                            "symbol": "KRX:005930",
                                            "quantity": 2,
                                            "average_price": 500,
                                            "market_price": 600,
                                            "market_value": 1200,
                                            "cost_basis": 1000,
                                            "unrealized_pnl": 200,
                                            "unrealized_pnl_pct": 0.2,
                                        }
                                    ],
                                }
                            }
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "operator-ui-current-estimate-test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "broker_accounts": [
                    {
                        "account_id": "kis-domestic",
                        "market_scope": "domestic",
                        "account_store_path": "accounts/domestic.json",
                        "order_store_path": "orders/domestic.jsonl",
                    },
                    {
                        "account_id": "kis-overseas",
                        "market_scope": "overseas",
                        "account_store_path": "accounts/overseas.json",
                        "order_store_path": "orders/overseas.jsonl",
                    },
                ],
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "broker_account_id": "kis-domestic",
                        "broker_account_routes": {
                            "domestic": "kis-domestic",
                            "overseas": "kis-overseas",
                        },
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = build_operator_dashboard_snapshot(
        config_path,
        sleeve_ids=("LEaps",),
        include_details=False,
        generated_at=datetime.fromisoformat("2026-05-22T09:03:00"),
    )

    source = payload["current_estimates"]["source"]
    assert source["status"] == "fresh"
    assert source["age_seconds"] == 25
    estimate = payload["current_estimates"]["latest_by_sleeve"]["LEaps"]
    assert estimate["status"] == "fresh"
    assert estimate["age_seconds"] == 30
    assert estimate["cash_by_currency"] == {"KRW": 1100}
    assert estimate["stock_market_value_by_currency"] == {"KRW": 1200}
    assert estimate["equity_by_currency"] == {"KRW": 2300}
    assert estimate["cost_basis_by_currency"] == {"KRW": 1000}
    assert estimate["unrealized_pnl_by_currency"] == {"KRW": 200}
    assert estimate["holdings"][0]["label"] == "삼성전자 (005930)"
    routes_by_scope = {route["market_scope"]: route for route in payload["order_routes"]}
    routed = routes_by_scope["domestic"]["sleeves"][0]["current_estimate"]
    assert routed["source_path"].endswith("multi_sleeve_runtime_run_latest.json")
    assert routed["equity_by_currency"] == {"KRW": 2300}
    overseas_routed = routes_by_scope["overseas"]["sleeves"][0]["current_estimate"]
    assert overseas_routed["status"] == "unavailable"
    assert overseas_routed["reason"] == "current_estimate_currency_unavailable:USD"


def test_operator_dashboard_snapshot_prefers_latest_by_sleeve_current_estimates(tmp_path):
    config_path = tmp_path / "runtime.json"
    live_dir = tmp_path / "data" / "runtime" / "live-order-loop"
    live_dir.mkdir(parents=True)
    (live_dir / "multi_sleeve_runtime_run_latest.json").write_text(
        json.dumps(
            {
                "completed_at": "2026-05-22T09:05:00",
                "reports": [{"sleeve_id": "kr-domestic-4401", "engine_status": {}}],
            }
        ),
        encoding="utf-8",
    )
    (live_dir / "multi_sleeve_runtime_run_latest_by_sleeve.json").write_text(
        json.dumps(
            {
                "schema_version": "multi_sleeve_runtime_latest_by_sleeve.v1",
                "generated_at": "2026-05-22T09:06:00",
                "latest_by_sleeve": {
                    "LEaps": {
                        "sleeve_id": "LEaps",
                        "updated_at": "2026-05-22T09:04:30",
                        "source_run_completed_at": "2026-05-22T09:04:30",
                        "report": {
                            "sleeve_id": "LEaps",
                            "engine_status": {
                                "portfolio_engine_state": {
                                    "current": {
                                        "sleeve_id": "LEaps",
                                        "as_of": "2026-05-22T09:04:20",
                                        "cash_by_currency": {"KRW": 100},
                                        "equity_by_currency": {"KRW": 700},
                                        "holdings": [
                                            {
                                                "symbol": "KRX:005930",
                                                "quantity": 1,
                                                "average_price": 500,
                                                "market_price": 600,
                                                "market_value": 600,
                                                "cost_basis": 500,
                                                "unrealized_pnl": 100,
                                            }
                                        ],
                                    }
                                }
                            },
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "operator-ui-latest-by-sleeve-test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = build_operator_dashboard_snapshot(
        config_path,
        sleeve_ids=("LEaps",),
        include_details=False,
        generated_at=datetime.fromisoformat("2026-05-22T09:05:00"),
    )

    assert payload["current_estimates"]["source"]["name"] == "multi_sleeve_runtime_run_latest_by_sleeve"
    estimate = payload["current_estimates"]["latest_by_sleeve"]["LEaps"]
    assert estimate["status"] == "fresh"
    assert estimate["equity_by_currency"] == {"KRW": 700}
    assert estimate["source_path"].endswith("multi_sleeve_runtime_run_latest_by_sleeve.json")


def test_operator_dashboard_snapshot_marks_to_market_from_quote_store_and_targets(tmp_path):
    config_path = tmp_path / "runtime.json"
    account_store_path = tmp_path / "accounts" / "leaps.json"
    snapshot_store_path = tmp_path / "data" / "market-data-snapshots" / "live.jsonl"
    framework_state_path = tmp_path / "data" / "runtime" / "framework-state" / "multi-sleeve" / "LEaps.json"
    account_store_path.parent.mkdir(parents=True)
    snapshot_store_path.parent.mkdir(parents=True)
    framework_state_path.parent.mkdir(parents=True)
    for date, equity, cash, holdings in (
        ("2026-05-20", 10000, 10000, []),
        (
            "2026-05-21",
            5000,
            4000,
            [{"symbol": "KRX:005930", "quantity": 2, "average_price": 500, "market_price": 500, "market_value": 1000}],
        ),
    ):
        report_dir = tmp_path / "data" / "eod-snapshots" / date / "eod" / "domestic_LEaps" / "portfolio-report"
        report_dir.mkdir(parents=True)
        (report_dir / f"LEaps_runtime_{date.replace('-', '')}_180000.json").write_text(
            json.dumps(
                {
                    "generated_at": f"{date}T18:00:00+09:00",
                    "portfolio_state": {
                        "current": {
                            "sleeve_id": "LEaps",
                            "cash_by_currency": {"KRW": cash},
                            "equity_by_currency": {"KRW": equity},
                            "holdings": holdings,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
    account_store_path.write_text(
        json.dumps(
            {
                "sleeves": {
                    "LEaps": {
                        "cash": 500,
                        "cash_by_currency": {"KRW": 500},
                        "holdings": {
                            "KRX:005930": {
                                "symbol": {"ticker": "005930", "market": "KRX"},
                                "quantity": 2,
                                "average_price": 500,
                            }
                        },
                    }
                },
                "cash_transfers": {
                    "cash:test:KRW:default:LEaps:1": {
                        "transfer_id": "cash:test:KRW:default:LEaps:1",
                        "from_sleeve_id": "default sleeve",
                        "to_sleeve_id": "LEaps",
                        "amount": 1500,
                        "currency": "KRW",
                        "occurred_at": "2026-05-22T08:00:00",
                    }
                },
                "fills": {
                    "buy-closed": {
                        "fill_id": "buy-closed",
                        "order_id": "buy-closed",
                        "symbol": {"ticker": "000660", "market": "KRX"},
                        "side": "buy",
                        "quantity": 1,
                        "fill_price": 1000,
                        "filled_at": "2026-05-22T08:30:00",
                        "sleeve_id": "LEaps",
                        "fee": 0,
                    },
                    "sell-closed": {
                        "fill_id": "sell-closed",
                        "order_id": "sell-closed",
                        "symbol": {"ticker": "000660", "market": "KRX"},
                        "side": "sell",
                        "quantity": 1,
                        "fill_price": 1200,
                        "filled_at": "2026-05-22T08:40:00",
                        "sleeve_id": "LEaps",
                        "fee": 10,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    framework_state_path.write_text(
        json.dumps(
            {
                "sleeve_id": "LEaps",
                "updated_at": "2026-05-22T09:04:00",
                "last_portfolio_target_batch": {
                    "plans": [
                        {
                            "symbol": "KRX:005930",
                            "target_percent": 0.5,
                            "desired_value": 850,
                            "current_price": 600,
                            "reason": "target",
                        },
                        {
                            "symbol": "KRX:000660",
                            "target_percent": 0.25,
                            "desired_value": 425,
                            "current_price": 1700,
                            "reason": "target",
                        },
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    snapshot_records = [
        {
            "snapshot": {
                "snapshot_id": "quote-older-sleeve",
                "time": "2026-05-22T09:04:50",
                "source": "test-quote-store",
                "lane": "quote",
                "bars": [
                    {
                        "symbol": {"ticker": "000660", "market": "KRX"},
                        "time": "2026-05-22T09:04:45",
                        "open": 1700,
                        "high": 1700,
                        "low": 1700,
                        "close": 1700,
                        "volume": 10,
                        "resolution": "live",
                        "metadata": {},
                    }
                ],
            },
            "quality_by_sleeve": {"other-sleeve": {"status": "fresh"}},
            "metadata": {},
        },
        {
            "snapshot": {
                "snapshot_id": "quote-latest-sleeve",
                "time": "2026-05-22T09:05:00",
                "source": "test-quote-store",
                "lane": "quote",
                "bars": [
                    {
                        "symbol": {"ticker": "005930", "market": "KRX"},
                        "time": "2026-05-22T09:04:55",
                        "open": 600,
                        "high": 600,
                        "low": 600,
                        "close": 600,
                        "volume": 10,
                        "resolution": "live",
                        "metadata": {},
                    }
                ],
            },
            "quality_by_sleeve": {"LEaps": {"status": "fresh"}},
            "metadata": {},
        },
    ]
    snapshot_store_path.write_text(
        "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in snapshot_records),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "operator-ui-quote-estimate-test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "market_data": {"snapshot_store_path": str(snapshot_store_path)},
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 0,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": str(account_store_path)},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = build_operator_dashboard_snapshot(
        config_path,
        sleeve_ids=("LEaps",),
        include_details=False,
        generated_at=datetime.fromisoformat("2026-05-22T09:05:30"),
    )

    assert payload["current_estimates"]["source"]["name"] == "market_data_snapshot_store_quote"
    assert payload["current_estimates"]["source"]["merged_record_count"] == 2
    estimate = payload["current_estimates"]["latest_by_sleeve"]["LEaps"]
    assert estimate["status"] == "fresh"
    assert estimate["stock_market_value_by_currency"] == {"KRW": 1200}
    assert estimate["equity_by_currency"] == {"KRW": 1700}
    assert estimate["cost_basis_by_currency"] == {"KRW": 1000}
    assert estimate["unrealized_pnl_by_currency"] == {"KRW": 200}
    assert estimate["realized_pnl_by_currency"] == {"KRW": 190}
    assert estimate["total_pnl_by_currency"] == {"KRW": 390}
    assert estimate["realized_cost_basis_by_currency"] == {"KRW": 1000}
    assert estimate["total_pnl_cost_basis_by_currency"] == {"KRW": 1500}
    assert estimate["net_cash_flow_by_currency"] == {"KRW": 1500}
    assert estimate["pnl_since_eod_by_currency"] == {"KRW": -4800}
    assert estimate["return_since_eod_by_currency"]["KRW"] == pytest.approx(-4800 / 6500)
    assert estimate["total_return_by_currency"]["KRW"] == pytest.approx(390 / 1500)
    assert estimate["total_return_basis_by_currency"] == {"KRW": "realized_plus_unrealized_book_value"}
    positions = {position["symbol"]: position for position in estimate["positions"]}
    assert positions["KRX:005930"]["current_percent"] == pytest.approx(1200 / 1700)
    assert positions["KRX:005930"]["target_percent"] == 0.5
    assert positions["KRX:005930"]["total_pnl_pct"] == pytest.approx(0.2)
    assert positions["KRX:005930"]["today_pnl"] == pytest.approx(200)
    assert positions["KRX:005930"]["today_pnl_pct"] == pytest.approx(0.2)
    assert positions["KRX:000660"]["quantity"] == 0
    assert positions["KRX:000660"]["target_percent"] == 0.25
    assert positions["KRX:000660"]["realized_pnl"] == 190
    assert positions["KRX:000660"]["total_pnl"] == 190
    assert positions["KRX:000660"]["total_pnl_pct"] == pytest.approx(0.19)

    stale_payload = build_operator_dashboard_snapshot(
        config_path,
        sleeve_ids=("LEaps",),
        include_details=False,
        generated_at=datetime.fromisoformat("2026-05-22T09:15:30"),
    )

    stale_estimate = stale_payload["current_estimates"]["latest_by_sleeve"]["LEaps"]
    assert stale_estimate["status"] == "stale"
    assert stale_estimate["stock_market_value_by_currency"] == {"KRW": 1200}
    assert stale_estimate["equity_by_currency"] == {"KRW": 1700}


def test_cli_operator_ui_snapshot_only_outputs_dashboard_payload(tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "operator-ui-cli-test",
                "mode": "paper",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 500,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(["operator-ui", str(config_path), "--sleeve-id", "LEaps", "--snapshot-only"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"]["snapshot_only"] is True
    assert payload["source"]["kis_api"] == "not_called"
    assert payload["runtime"]["runtime_id"] == "operator-ui-cli-test"
    assert payload["order_routes"][0]["sleeves"][0]["portfolio"]["cash"] == 500
