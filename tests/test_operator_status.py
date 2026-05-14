import json
from datetime import datetime

from leaps_quant_engine.cli import main
from leaps_quant_engine.operator_status import (
    CashAvailabilityRouteInput,
    build_cash_availability_report,
    build_eod_snapshot_status,
)
from leaps_quant_engine.virtual_account import VirtualSleeveAccountStore


def test_cash_availability_reports_residual_cash_by_route(tmp_path):
    account_store_path = tmp_path / "accounts.json"
    store = VirtualSleeveAccountStore(account_store_path, default_cash_by_sleeve={"LEaps": 200_000})
    store.current_portfolio("LEaps")
    store.sync_account_cash(
        {"cash_balance": 1_000_000, "currency": "KRW"},
        account_id="kis-domestic",
        currency="KRW",
        residual_sleeve_id="default sleeve",
        synced_at=datetime(2026, 5, 14, 9, 0),
    )

    report = build_cash_availability_report(
        runtime_id="test",
        sleeve_ids=("LEaps",),
        routes=(
            CashAvailabilityRouteInput(
                account_id="kis-domestic",
                market_scope="domestic",
                currency="KRW",
                account_store_path=account_store_path,
                default_cash_by_sleeve={"LEaps": 200_000},
            ),
        ),
    )

    assert report["available_cash_by_currency"] == {"KRW": 800_000}
    assert report["routes"][0]["available_cash"] == 800_000
    assert report["routes"][0]["sleeve_cash"] == {"LEaps": 200_000}
    assert report["needs_attention"] is False


def test_eod_snapshot_status_reports_today_marker_and_scheduled_future(tmp_path):
    root = tmp_path / "eod-snapshots"
    state = tmp_path / "runtime" / "eod-snapshots"
    manifest_dir = root / "2026-05-14" / "krx-after-hours"
    manifest_dir.mkdir(parents=True)
    state.mkdir(parents=True)
    (manifest_dir / "manifest_20260514_180500.json").write_text(
        json.dumps(
            {
                "snapshot_date": "2026-05-14",
                "label": "krx-after-hours",
                "generated_at": "2026-05-14T18:05:00+09:00",
                "targets": [{}],
            }
        ),
        encoding="utf-8",
    )
    (state / "2026-05-14_krx-after-hours.done").write_text(
        json.dumps(
            {
                "date": "2026-05-14",
                "label": "krx-after-hours",
                "attempted_at": "2026-05-14T18:05:30+09:00",
                "exit_code": 0,
            }
        ),
        encoding="utf-8",
    )

    report = build_eod_snapshot_status(
        snapshot_root=root,
        state_dir=state,
        schedules=("18:05|krx-after-hours", "23:00|late-check"),
        now=datetime(2026, 5, 14, 19, 0),
    )

    labels = {item["label"]: item for item in report["labels"]}
    assert labels["krx-after-hours"]["status"] == "ok_today"
    assert labels["late-check"]["status"] == "scheduled"
    assert report["needs_attention"] is False


def test_cli_sleeve_cash_availability_outputs_report(tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "broker_accounts": [
                    {
                        "account_id": "kis-domestic",
                        "market_scope": "domestic",
                        "currency": "KRW",
                        "account_store_path": "accounts/kis.json",
                        "order_store_path": "orders/kis.jsonl",
                    }
                ],
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "broker_account_id": "kis-domestic",
                        "cash": 200_000,
                        "universe": {"coarse_path": "universe.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    account_store = VirtualSleeveAccountStore(tmp_path / "accounts" / "kis.json")
    account_store.initialize_sleeve("LEaps", cash=200_000)
    account_store.sync_account_cash(
        {"cash_balance": 1_000_000, "currency": "KRW"},
        account_id="kis-domestic",
        currency="KRW",
    )

    exit_code = main(
        [
            "sleeve-cash-availability",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["available_cash_by_currency"] == {"KRW": 800_000}
    assert payload["routes"][0]["available_cash"] == 800_000


def test_cli_eod_snapshot_status_outputs_report(tmp_path, capsys):
    exit_code = main(
        [
            "eod-snapshot-status",
            "--snapshot-root",
            str(tmp_path / "snapshots"),
            "--state-dir",
            str(tmp_path / "state"),
            "--schedule",
            "23:59|late-check",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["label_count"] == 1
    assert payload["labels"][0]["label"] == "late-check"
