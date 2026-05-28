from datetime import datetime, timedelta, timezone
import json

from leaps_quant_engine.runtime_heartbeat import (
    RuntimeHeartbeat,
    evaluate_runtime_heartbeat,
    read_runtime_heartbeat,
    write_runtime_heartbeat,
)
from leaps_quant_engine.runtime_health import build_runtime_health_report


def test_runtime_heartbeat_write_read_and_evaluate_fresh(tmp_path):
    path = tmp_path / "heartbeat.json"
    now = datetime(2026, 5, 24, 9, 0, tzinfo=timezone.utc)
    heartbeat = RuntimeHeartbeat(
        runtime_id="live_multi_sleeve",
        component="multi_sleeve_live_order_loop",
        status="running",
        updated_at=now,
        config_path="configs/runtime/live_multi_sleeve.json",
        sleeve_ids=("LEaps", "us_etf_rotation"),
        cycle_index=12,
        process_id=1234,
        metadata={"phase": "cycle_end"},
    )

    write_runtime_heartbeat(path, heartbeat)

    loaded = read_runtime_heartbeat(path)
    assert loaded == heartbeat
    evaluation = evaluate_runtime_heartbeat(
        path,
        runtime_id="live_multi_sleeve",
        component="multi_sleeve_live_order_loop",
        max_age_seconds=120,
        now=now + timedelta(seconds=10),
    )

    assert evaluation.status == "ok"
    assert evaluation.metadata["age_seconds"] == 10
    assert evaluation.metadata["process_id_liveness_checked"] is False


def test_runtime_heartbeat_read_accepts_utf8_bom(tmp_path):
    path = tmp_path / "heartbeat.json"
    now = datetime(2026, 5, 24, 9, 0, tzinfo=timezone.utc)
    heartbeat = RuntimeHeartbeat(
        runtime_id="live_multi_sleeve",
        component="multi_sleeve_live_order_loop",
        status="running",
        updated_at=now,
    )
    path.write_text(
        "\ufeff" + json.dumps(heartbeat.to_dict()),
        encoding="utf-8",
    )

    loaded = read_runtime_heartbeat(path)

    assert loaded == heartbeat


def test_runtime_heartbeat_evaluation_reports_stale_without_pid_scan(tmp_path):
    path = tmp_path / "heartbeat.json"
    now = datetime(2026, 5, 24, 9, 0)
    write_runtime_heartbeat(
        path,
        RuntimeHeartbeat(
            runtime_id="live_multi_sleeve",
            component="multi_sleeve_live_order_loop",
            status="running",
            updated_at=now,
            process_id=4321,
        ),
    )

    evaluation = evaluate_runtime_heartbeat(
        path,
        runtime_id="live_multi_sleeve",
        component="multi_sleeve_live_order_loop",
        max_age_seconds=30,
        now=now + timedelta(seconds=31),
    )

    assert evaluation.status == "warning"
    assert evaluation.reason == "heartbeat_stale"
    assert evaluation.metadata["process_id_liveness_checked"] is False


def test_runtime_health_includes_heartbeat_check(tmp_path):
    path = tmp_path / "heartbeat.json"
    now = datetime(2026, 5, 24, 9, 0)
    write_runtime_heartbeat(
        path,
        RuntimeHeartbeat(
            runtime_id="live_multi_sleeve",
            component="multi_sleeve_live_order_loop",
            status="running",
            updated_at=now,
        ),
    )

    report = build_runtime_health_report(
        runtime_id="live_multi_sleeve",
        sleeve_ids=("LEaps",),
        journal_store=None,
        heartbeat_path=path,
        heartbeat_component="multi_sleeve_live_order_loop",
        max_heartbeat_age_seconds=120,
        generated_at=now,
    )

    checks = {check.name: check for check in report.checks}
    assert checks["runtime_heartbeat"].status == "ok"
    assert checks["runtime_heartbeat"].metadata["process_id_liveness_checked"] is False
