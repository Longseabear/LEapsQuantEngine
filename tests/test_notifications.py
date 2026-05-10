import json

from leaps_quant_engine.notifications import NotificationPaths, NotificationService


class FakeTelegramClient:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.sent = []

    def send_message(self, *, text, chat_id=None, disable_notification=False):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(
            {
                "text": text,
                "chat_id": chat_id,
                "disable_notification": disable_notification,
            }
        )
        return {"ok": True, "result": {"message_id": 123}}


def test_notification_service_saves_only_when_telegram_is_not_configured(tmp_path):
    service = NotificationService(paths=NotificationPaths(root=tmp_path / "notification-engine"))

    result = service.notify_user_message(
        category="order",
        title="  Test   Alert  ",
        message="hello\n\n\n  world  ",
    )

    assert result["delivery_mode"] == "dry-run"
    assert result["delivery_status"] == "saved_only"
    assert result["title"] == "Test Alert"
    assert result["message"] == "hello\n\nworld"
    assert (tmp_path / "notification-engine" / "outbox" / f"{result['record_id']}.json").exists()
    history = json.loads(
        (tmp_path / "notification-engine" / "history" / f"{result['record_id']}.json").read_text(encoding="utf-8")
    )
    assert history["delivery_status"] == "saved_only"


def test_notification_service_sends_telegram_and_records_history(tmp_path):
    fake = FakeTelegramClient()
    service = NotificationService(
        paths=NotificationPaths(root=tmp_path / "notification-engine"),
        telegram_client=fake,
    )

    result = service.notify_user_message(
        category="order",
        title="Submitted",
        message="LEaps KRX:005930 buy 1",
        chat_id="chat-1",
        disable_notification=True,
    )

    assert result["delivery_mode"] == "telegram"
    assert result["delivery_status"] == "sent"
    assert result["telegram_message_id"] == 123
    assert fake.sent == [
        {
            "text": "[ORDER] Submitted\n\nLEaps KRX:005930 buy 1",
            "chat_id": "chat-1",
            "disable_notification": True,
        }
    ]


def test_notification_service_failure_is_persisted_not_raised(tmp_path):
    service = NotificationService(
        paths=NotificationPaths(root=tmp_path / "notification-engine", audit_log_path=tmp_path / "audit.jsonl"),
        telegram_client=FakeTelegramClient(fail=True),
    )

    result = service.notify_user_message(
        category="order",
        title="Submitted",
        message="will fail",
    )

    assert result["delivery_status"] == "failed"
    assert "telegram down" in result["error"]
    assert (tmp_path / "audit.jsonl").exists()
