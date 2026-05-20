import json
from datetime import datetime

from leaps_quant_engine.cli import main
from leaps_quant_engine.cycle_journal import CycleJournalEntry, FileCycleJournalStore
from leaps_quant_engine.runtime_config import load_runtime_config_snapshot
from leaps_quant_engine.runtime_preflight import build_runtime_preflight_report


def test_runtime_preflight_reports_bootstrap_and_code_identity(tmp_path, capsys):
    config_path = _write_preflight_runtime(tmp_path)
    journal_path = tmp_path / "journal.jsonl"

    exit_code = main(
        [
            "runtime-preflight",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--journal",
            str(journal_path),
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime_id"] == "preflight-test"
    assert payload["status"] == "needs_attention"
    assert payload["code_identity"]["runtime_fingerprint"].startswith("sha256:")
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["bootstrap_sleeve"]["status"] == "ok"
    assert checks["runtime_file_fingerprints"]["status"] == "ok"
    assert checks["journal_missing"]["status"] == "warning"


def test_runtime_preflight_detects_code_changed_since_last_cycle(tmp_path):
    config_path = _write_preflight_runtime(tmp_path)
    snapshot = load_runtime_config_snapshot(config_path)
    journal_path = tmp_path / "journal.jsonl"
    journal_store = FileCycleJournalStore(journal_path)
    journal_store.append(
        CycleJournalEntry(
            runtime_id="preflight-test",
            config_version=snapshot.version,
            sleeve_id="LEaps",
            generated_at=datetime(2026, 5, 10, 9, 0),
            recorded_at=datetime(2026, 5, 10, 9, 0),
            source="runtime-run-once",
            status="ok",
            metadata={"engine_source_hash": "sha256:old"},
        )
    )

    report = build_runtime_preflight_report(
        snapshot=snapshot,
        sleeve_ids=("LEaps",),
        journal_store=journal_store,
        journal_path=journal_path,
        check_bootstrap=False,
    )

    checks = [check for check in report.checks if check.name == "engine_code_changed_since_last_cycle"]
    assert checks
    assert checks[0].status == "warning"
    assert "stage_reload_and_run_runtime_once" in report.recommended_next_actions


def test_runtime_preflight_checks_kis_gateway_liveness(tmp_path, monkeypatch):
    config_path = _write_preflight_runtime(tmp_path, provider="kis-gateway")
    snapshot = load_runtime_config_snapshot(config_path)

    monkeypatch.setattr(
        "leaps_quant_engine.runtime_preflight.fetch_kis_gateway_health",
        lambda base_url, timeout_seconds: {"status": "ok", "server": "leaps-kis-gateway", "lane": {"mock": False}},
    )

    report = build_runtime_preflight_report(
        snapshot=snapshot,
        sleeve_ids=("LEaps",),
        check_bootstrap=False,
        strict_live=True,
    )

    checks = {check.name: check for check in report.checks}
    assert checks["kis_gateway_liveness"].status == "ok"
    assert checks["kis_gateway_liveness"].metadata["base_url"] == "http://127.0.0.1:8766"


def _write_preflight_runtime(tmp_path, *, provider="market-data-engine"):
    workspace = tmp_path / "sleeves" / "LEaps"
    (workspace / "alphas").mkdir(parents=True)
    (tmp_path / "state" / "accounts").mkdir(parents=True)
    (tmp_path / "state" / "order-runtime").mkdir(parents=True)
    universe_path = tmp_path / "universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "id": "preflight-universe",
                "market": "KRX",
                "symbols": ["005930"],
                "indicators": [{"name": "close", "type": "close", "period": 1}],
            }
        ),
        encoding="utf-8",
    )
    (workspace / "alphas" / "noop.py").write_text(
        """
ALPHA_ID = "noop-alpha"
VERSION = "1.0"

def generate(context):
    return []
""",
        encoding="utf-8",
    )
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "preflight-test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "market_data": {
                    "provider": provider,
                    "history_provider": "kis-cache",
                    **({"gateway_base_url": "http://127.0.0.1:8766"} if provider == "kis-gateway" else {}),
                },
                "journal_path": "journal.jsonl",
                "broker_accounts": [
                    {
                        "account_id": "kis-domestic",
                        "market_scope": "domestic",
                        "currency": "KRW",
                        "account_store_path": "state/accounts/kis-domestic.json",
                        "order_store_path": "state/order-runtime/kis-domestic.jsonl",
                    }
                ],
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "workspace_path": str(workspace),
                        "broker_account_routes": {"domestic": "kis-domestic"},
                        "cash_by_currency": {"KRW": 1000000},
                        "universe": {"coarse_path": str(universe_path)},
                        "indicators": {"warmup_enabled": False},
                        "alpha": {"modules": [{"ref": "alphas/noop.py"}]},
                        "portfolio": {"model": "leaps_quant_engine.framework:EqualWeightPortfolioConstructionModel"},
                        "risk": {"model": "leaps_quant_engine.framework:PassThroughRiskManagementModel"},
                        "execution": {"model": "leaps_quant_engine.execution:ImmediateExecutionModel"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return config_path
