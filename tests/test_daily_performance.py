import json

import pytest

from leaps_quant_engine.cli import main
from leaps_quant_engine.performance import build_sleeve_daily_performance_report


def test_daily_performance_builds_sleeve_returns_from_eod_snapshots(tmp_path):
    root = tmp_path / "eod-snapshots"
    _write_snapshot(
        root,
        date="2026-05-13",
        label="krx-after-hours",
        target="domestic_LEaps",
        sleeve_id="LEaps",
        as_of="2026-05-13T18:05:00",
        equity=1_000_000,
        cash=100_000,
        holdings=[{"symbol": "KRX:005930", "quantity": 10, "market_value": 900_000}],
        transfers=[
            {
                "transfer_id": "t1",
                "from_sleeve_id": "default sleeve",
                "to_sleeve_id": "LEaps",
                "amount": 200_000,
                "occurred_at": "2026-05-13T09:00:00+09:00",
                "currency": "KRW",
            }
        ],
    )
    _write_snapshot(
        root,
        date="2026-05-14",
        label="krx-after-hours",
        target="domestic_LEaps",
        sleeve_id="LEaps",
        as_of="2026-05-14T18:05:00",
        equity=1_300_000,
        cash=200_000,
        holdings=[{"symbol": "KRX:005930", "quantity": 10, "market_value": 1_100_000}],
        transfers=[
            {
                "transfer_id": "t1",
                "from_sleeve_id": "default sleeve",
                "to_sleeve_id": "LEaps",
                "amount": 200_000,
                "occurred_at": "2026-05-13T09:00:00+09:00",
                "currency": "KRW",
            },
            {
                "transfer_id": "t2",
                "from_sleeve_id": "default sleeve",
                "to_sleeve_id": "LEaps",
                "amount": 100_000,
                "occurred_at": "2026-05-14T10:00:00+09:00",
                "currency": "KRW",
            },
            {
                "transfer_id": "future-same-day",
                "from_sleeve_id": "default sleeve",
                "to_sleeve_id": "LEaps",
                "amount": 999_000,
                "occurred_at": "2026-05-14T23:00:00+09:00",
                "currency": "KRW",
            },
        ],
    )

    report = build_sleeve_daily_performance_report(root, sleeve_ids=("LEaps",))

    payload = report.to_dict(include_holdings=True)
    assert payload["row_count"] == 2
    assert payload["summaries"][0]["period_pnl"] == 200_000
    assert payload["summaries"][0]["period_return"] == pytest.approx(0.2)
    first, second = payload["rows"]
    assert first["daily_return"] is None
    assert second["previous_equity"] == 1_000_000
    assert second["net_cash_flow"] == 100_000
    assert second["daily_pnl"] == 200_000
    assert second["daily_return"] == 0.2
    assert second["holdings"][0]["symbol"] == "KRX:005930"


def test_daily_performance_keeps_sleeve_and_currency_separate(tmp_path):
    root = tmp_path / "eod-snapshots"
    _write_snapshot(
        root,
        date="2026-05-14",
        label="us-after-hours",
        target="overseas_us_etf_rotation",
        sleeve_id="us_etf_rotation",
        as_of="2026-05-14T06:10:00",
        equity=2_510.0,
        cash=800.0,
        currency="USD",
        holdings=[{"symbol": "US:QQQ", "quantity": 3, "market_value": 1_710.0}],
    )
    _write_snapshot(
        root,
        date="2026-05-14",
        label="krx-after-hours",
        target="domestic_LEaps",
        sleeve_id="LEaps",
        as_of="2026-05-14T18:05:00",
        equity=11_000_000,
        cash=1_000_000,
        currency="KRW",
        holdings=[{"symbol": "KRX:005930", "quantity": 10, "market_value": 10_000_000}],
    )

    report = build_sleeve_daily_performance_report(root, currency="USD")

    payload = report.to_dict()
    assert payload["row_count"] == 1
    assert payload["summaries"][0]["sleeve_id"] == "us_etf_rotation"
    assert payload["summaries"][0]["currency"] == "USD"
    assert payload["rows"][0]["held_symbols"] == ["US:QQQ"]


def test_cli_sleeve_daily_performance_outputs_report(tmp_path, capsys):
    root = tmp_path / "eod-snapshots"
    _write_snapshot(
        root,
        date="2026-05-14",
        label="krx-after-hours",
        target="domestic_LEaps",
        sleeve_id="LEaps",
        as_of="2026-05-14T18:05:00",
        equity=1_000_000,
        cash=100_000,
        holdings=[{"symbol": "KRX:005930", "quantity": 10, "market_value": 900_000}],
    )

    exit_code = main(
        [
            "sleeve-daily-performance",
            "--snapshot-root",
            str(root),
            "--sleeve-id",
            "LEaps",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["row_count"] == 1
    assert "rows" not in payload
    assert payload["summaries"][0]["latest_held_symbols"] == ["KRX:005930"]


def test_daily_performance_reads_fast_report_shape_without_current_sleeve_id(tmp_path):
    root = tmp_path / "eod-snapshots"
    report_dir = root / "2026-05-22" / "us-after-hours" / "overseas_us_etf_rotation" / "portfolio-report"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "us_etf_rotation_runtime_20260522_061026.json").write_text(
        json.dumps(
            {
                "report_source": {"generated_at": "2026-05-22T06:10:26"},
                "portfolio_state": {
                    "current": {
                        "currency": "USD",
                        "cash": 100.0,
                        "cash_by_currency": {"USD": 100.0},
                        "equity": 1010.0,
                        "equity_by_currency": {"USD": 1010.0},
                        "gross_exposure": 910.0,
                        "holdings": [{"symbol": "US:QQQ", "quantity": 1, "market_value": 910.0}],
                    }
                },
                "framework": {"portfolio_target_batch": {"sleeve_id": "us_etf_rotation"}},
            }
        ),
        encoding="utf-8",
    )

    report = build_sleeve_daily_performance_report(root, sleeve_ids=("us_etf_rotation",))

    payload = report.to_dict()
    assert payload["warnings"] == []
    assert payload["row_count"] == 1
    assert payload["summaries"][0]["sleeve_id"] == "us_etf_rotation"
    assert payload["summaries"][0]["end_date"] == "2026-05-22"
    assert payload["summaries"][0]["latest_held_symbols"] == ["US:QQQ"]


def _write_snapshot(
    root,
    *,
    date,
    label,
    target,
    sleeve_id,
    as_of,
    equity,
    cash,
    holdings,
    currency="KRW",
    transfers=None,
):
    report_dir = root / date / label / target / "portfolio-report"
    report_dir.mkdir(parents=True, exist_ok=True)
    stores_dir = root / date / label / "stores"
    stores_dir.mkdir(parents=True, exist_ok=True)
    gross_exposure = sum(float(item["market_value"]) for item in holdings)
    current = {
        "sleeve_id": sleeve_id,
        "as_of": as_of,
        "cash": cash,
        "cash_by_currency": {currency: cash},
        "equity": equity,
        "equity_by_currency": {currency: equity},
        "gross_exposure": gross_exposure,
        "gross_exposure_pct": gross_exposure / equity if equity else None,
        "holdings": [
            {
                "symbol": item["symbol"],
                "quantity": item["quantity"],
                "average_price": item.get("average_price", 0),
                "market_price": item.get("market_price", 0),
                "market_value": item["market_value"],
                "unrealized_pnl": item.get("unrealized_pnl", 0),
                "unrealized_pnl_pct": item.get("unrealized_pnl_pct", 0),
            }
            for item in holdings
        ],
    }
    (report_dir / f"{sleeve_id}_runtime_{date.replace('-', '')}_180500.json").write_text(
        json.dumps({"sleeve_id": sleeve_id, "portfolio_state": {"current": current}}),
        encoding="utf-8",
    )
    transfer_payload = {
        item["transfer_id"]: item
        for item in transfers or []
    }
    (stores_dir / "account.json").write_text(
        json.dumps({"cash_transfers": transfer_payload}),
        encoding="utf-8",
    )
