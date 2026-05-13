import json

from leaps_quant_engine.notifications import NotificationPaths, NotificationService
from leaps_quant_engine.telegram import TelegramClient, normalize_agent_text


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

    def get_updates(self, *, offset=None, limit=20):
        return [
            {
                "update_id": 77,
                "message": {
                    "chat": {"id": "chat-1"},
                    "text": "문서 모델".encode("utf-8").decode("cp949"),
                },
            }
        ][:limit]


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


def test_notification_service_preserves_korean_utf8_text(tmp_path):
    fake = FakeTelegramClient()
    service = NotificationService(
        paths=NotificationPaths(root=tmp_path / "notification-engine"),
        telegram_client=fake,
    )

    result = service.notify_user_message(
        category="status",
        title="장 시작 점검",
        message="삼성전자 매수 후보\n현금 5,000,000원",
    )

    assert result["title"] == "장 시작 점검"
    assert result["message"] == "삼성전자 매수 후보\n현금 5,000,000원"
    assert fake.sent[0]["text"] == "[STATUS] 장 시작 점검\n\n삼성전자 매수 후보\n현금 5,000,000원"
    history = json.loads(
        (tmp_path / "notification-engine" / "history" / f"{result['record_id']}.json").read_text(encoding="utf-8")
    )
    assert history["message"] == "삼성전자 매수 후보\n현금 5,000,000원"


def test_normalize_agent_text_repairs_utf8_decoded_as_cp949():
    broken = "문서 모델".encode("utf-8").decode("cp949")

    assert normalize_agent_text(broken) == "문서 모델"


def test_telegram_client_normalizes_text_before_post(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True, "result": {"message_id": 10}}

    def fake_post(url, *, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("leaps_quant_engine.telegram.requests.post", fake_post)
    broken = "문서 모델".encode("utf-8").decode("cp949")

    client = TelegramClient(bot_token="token", default_chat_id="chat")
    response = client.send_message(text=broken)

    assert response["ok"] is True
    assert captured["json"]["text"] == "문서 모델"


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


def test_notification_service_fetches_telegram_updates_to_inbox(tmp_path):
    service = NotificationService(
        paths=NotificationPaths(root=tmp_path / "notification-engine", audit_log_path=tmp_path / "audit.jsonl"),
        telegram_client=FakeTelegramClient(),
    )

    result = service.fetch_telegram_updates(limit=10)

    assert result["status"] == "ok"
    assert result["fetched_count"] == 1
    assert result["stored_count"] == 1
    inbox = tmp_path / "notification-engine" / "inbox" / "telegram-update-77.json"
    assert inbox.exists()
    payload = json.loads(inbox.read_text(encoding="utf-8"))
    assert payload["chat_id"] == "chat-1"
    assert payload["text"] == "문서 모델"
