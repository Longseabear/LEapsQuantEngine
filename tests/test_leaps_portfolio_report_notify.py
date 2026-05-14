import json
import subprocess
from pathlib import Path

import pytest

from tools.leaps_portfolio_report import _send_notification


def test_leaps_portfolio_report_notify_uses_plain_text_and_rejects_failed_delivery(monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(
                {
                    "record_id": "outbound-test",
                    "delivery_mode": "telegram",
                    "delivery_status": "failed",
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="delivery failed"):
        _send_notification(title="US_ETF - US regular", message_path=Path("message.txt"))

    assert "--parse-mode" not in calls[0]
