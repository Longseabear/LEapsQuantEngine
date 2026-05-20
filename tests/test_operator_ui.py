import json

from leaps_quant_engine.cli import main
from leaps_quant_engine.operator_ui import build_operator_dashboard_snapshot


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
    assert payload["order_routes"][0]["sleeves"][0]["portfolio"]["cash"] == 1000
    assert account_store_path.exists() is False


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
