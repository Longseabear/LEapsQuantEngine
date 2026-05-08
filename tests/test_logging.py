import json
import logging

from leaps_quant_engine.logging import JsonLogFormatter, configure_logging


def test_json_log_formatter_includes_extra_fields():
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="leaps.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="snapshot.complete",
        args=(),
        exc_info=None,
    )
    record.snapshot_id = "snapshot-1"
    record.elapsed_ms = 12.34

    payload = json.loads(formatter.format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "leaps.test"
    assert payload["message"] == "snapshot.complete"
    assert payload["snapshot_id"] == "snapshot-1"
    assert payload["elapsed_ms"] == 12.34


def test_configure_logging_writes_json_lines_to_file(tmp_path):
    log_file = tmp_path / "leapsq.jsonl"

    configure_logging(level="INFO", log_file=log_file, json_logs=True)
    logging.getLogger("leaps.test").info("hello", extra={"symbol": "US:NVDA"})

    payload = json.loads(log_file.read_text(encoding="utf-8").strip().splitlines()[0])
    assert payload["message"] == "hello"
    assert payload["symbol"] == "US:NVDA"


def test_configure_logging_rotates_file(tmp_path):
    log_file = tmp_path / "leapsq.jsonl"

    configure_logging(level="INFO", log_file=log_file, json_logs=True, max_bytes=120, backup_count=1)
    logger = logging.getLogger("leaps.test")
    for idx in range(20):
        logger.info("large-log-line", extra={"idx": idx, "payload": "x" * 80})

    assert log_file.exists()
    assert (tmp_path / "leapsq.jsonl.1").exists()
