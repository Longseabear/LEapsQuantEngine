from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4

from dotenv import load_dotenv

from leaps_quant_engine.telegram import TelegramClient, TelegramConfigError, normalize_agent_text

DEFAULT_NOTIFICATION_ROOT = Path("data/notification-engine")
DEFAULT_AUDIT_LOG_PATH = Path("logs/notification_engine_audit.jsonl")


class TelegramClientProtocol(Protocol):
    def send_message(
        self,
        *,
        text: str,
        chat_id: str | None = None,
        disable_notification: bool = False,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        """Send one Telegram message and return the provider response."""

    def get_updates(self, *, offset: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Fetch pending Telegram updates."""


@dataclass(frozen=True, slots=True)
class NotificationPaths:
    root: Path = DEFAULT_NOTIFICATION_ROOT
    audit_log_path: Path = DEFAULT_AUDIT_LOG_PATH

    @property
    def outbox_root(self) -> Path:
        return self.root / "outbox"

    @property
    def inbox_root(self) -> Path:
        return self.root / "inbox"

    @property
    def pending_root(self) -> Path:
        return self.root / "pending-requests"

    @property
    def history_root(self) -> Path:
        return self.root / "history"

    @property
    def state_root(self) -> Path:
        return self.root / "state"


@dataclass(slots=True)
class NotificationService:
    """Local-first notification service with optional Telegram delivery."""

    paths: NotificationPaths | None = None
    telegram_client: TelegramClientProtocol | None = None
    category_telegram_clients: Mapping[str, TelegramClientProtocol] | None = None

    def __post_init__(self) -> None:
        self.paths = self.paths or NotificationPaths()
        self.category_telegram_clients = dict(self.category_telegram_clients or {})

    @classmethod
    def from_env(
        cls,
        *,
        root: Path | None = None,
        audit_log_path: Path | None = None,
    ) -> "NotificationService":
        env_path = Path(".env")
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=False)
        token = _env_first("LEAPS_TELEGRAM_BOT_TOKEN", "STOCKPROGRAM_TELEGRAM_BOT_TOKEN")
        chat_id = _env_first("LEAPS_TELEGRAM_CHAT_ID", "STOCKPROGRAM_TELEGRAM_CHAT_ID")
        client = TelegramClient(bot_token=token, default_chat_id=chat_id or None) if token else None
        order_token = _env_first("LEAPS_ORDER_TELEGRAM_BOT_TOKEN", "STOCKPROGRAM_TELEGRAM_BOT_TOKEN")
        order_chat_id = _env_first("LEAPS_ORDER_TELEGRAM_CHAT_ID", "STOCKPROGRAM_TELEGRAM_CHAT_ID")
        category_clients: dict[str, TelegramClientProtocol] = {}
        if order_token:
            order_client = TelegramClient(bot_token=order_token, default_chat_id=order_chat_id or None)
            category_clients["order"] = order_client
        return cls(
            paths=NotificationPaths(
                root=root or DEFAULT_NOTIFICATION_ROOT,
                audit_log_path=audit_log_path or DEFAULT_AUDIT_LOG_PATH,
            ),
            telegram_client=client,
            category_telegram_clients=category_clients,
        )

    @property
    def delivery_mode(self) -> str:
        if self.telegram_client is not None or self.category_telegram_clients:
            return "telegram"
        return "dry-run"

    def health_check(self) -> dict[str, Any]:
        paths = _paths(self.paths)
        return {
            "status": "ok",
            "engine": "leaps-notification",
            "delivery_mode": self.delivery_mode,
            "outbox_count": len(_list_json_files(paths.outbox_root)),
            "history_count": len(_list_json_files(paths.history_root)),
            "pending_request_count": len(_list_json_files(paths.pending_root)),
            "root": str(paths.root),
        }

    def notify_user_message(
        self,
        *,
        category: str,
        title: str,
        message: str,
        disable_notification: bool = False,
        chat_id: str | None = None,
        parse_mode: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        paths = _paths(self.paths)
        record_id = _make_record_id("outbound")
        category_value = _sanitize_line(category, limit=40) or "status"
        payload: dict[str, Any] = {
            "record_id": record_id,
            "category": category_value,
            "title": _sanitize_line(normalize_agent_text(title), limit=120),
            "message": _sanitize_message_body(normalize_agent_text(message), limit=1500),
            "created_at": _utc_timestamp(),
            "delivery_mode": self.delivery_mode,
            "delivery_status": "queued",
            "metadata": dict(metadata or {}),
        }
        if parse_mode:
            payload["parse_mode"] = _sanitize_line(parse_mode, limit=40)
        _write_json_file(paths.outbox_root / f"{record_id}.json", payload)

        try:
            telegram_client = self._telegram_client_for_category(category_value)
            if telegram_client is None:
                payload["delivery_status"] = "saved_only"
            else:
                payload["delivery_route"] = _notification_route_for_category(category_value)
                send_kwargs: dict[str, Any] = {
                    "text": _format_message(payload["title"], payload["message"], category=payload["category"]),
                    "chat_id": chat_id,
                    "disable_notification": disable_notification,
                }
                if payload.get("parse_mode"):
                    send_kwargs["parse_mode"] = payload["parse_mode"]
                response = telegram_client.send_message(**send_kwargs)
                payload["delivery_status"] = "sent"
                payload["telegram_message_id"] = _telegram_message_id(response)
        except Exception as exc:  # noqa: BLE001
            payload["delivery_status"] = "failed"
            payload["error"] = str(exc)
            _append_audit_event(paths.audit_log_path, "notification_failed", payload)
        finally:
            payload["finalized_at"] = _utc_timestamp()
            _write_json_file(paths.history_root / f"{record_id}.json", payload)

        _append_audit_event(
            paths.audit_log_path,
            "notification_created",
            {
                "record_id": record_id,
                "category": payload["category"],
                "delivery_mode": payload["delivery_mode"],
                "delivery_status": payload["delivery_status"],
            },
        )
        return payload

    def _telegram_client_for_category(self, category: str) -> TelegramClientProtocol | None:
        category_key = _sanitize_line(category, limit=40).lower()
        if self.category_telegram_clients and category_key in self.category_telegram_clients:
            return self.category_telegram_clients[category_key]
        return self.telegram_client

    def fetch_telegram_updates(self, *, offset: int | None = None, limit: int = 20) -> dict[str, Any]:
        paths = _paths(self.paths)
        if self.telegram_client is None:
            return {
                "status": "saved_only",
                "delivery_mode": self.delivery_mode,
                "fetched_count": 0,
                "stored_count": 0,
                "updates": [],
                "root": str(paths.root),
            }
        updates = self.telegram_client.get_updates(offset=offset, limit=limit)
        stored = []
        for update in updates:
            if not isinstance(update, Mapping):
                continue
            record = _normalize_telegram_update(update)
            _write_json_file(paths.inbox_root / f"{record['record_id']}.json", record)
            stored.append(record)
        _append_audit_event(
            paths.audit_log_path,
            "telegram_updates_fetched",
            {"fetched_count": len(updates), "stored_count": len(stored)},
        )
        return {
            "status": "ok",
            "delivery_mode": self.delivery_mode,
            "fetched_count": len(updates),
            "stored_count": len(stored),
            "updates": stored,
            "root": str(paths.root),
        }


def notify_order_submit_report(
    service: NotificationService,
    report: Any,
    *,
    chat_id: str | None = None,
    disable_notification: bool = False,
) -> dict[str, Any]:
    final_status = getattr(report, "final_status", None)
    return service.notify_user_message(
        category="order",
        title=f"Order submit {getattr(report, 'status', 'unknown')}",
        message="\n".join(
            [
                f"runtime: {getattr(report, 'runtime_id', '')}",
                f"broker: {getattr(report, 'broker', '')}",
                f"account: {getattr(final_status, 'broker_account_id', '') if final_status else ''}",
                f"market_scope: {getattr(final_status, 'market_scope', '') if final_status else ''}",
                f"commit: {getattr(report, 'commit', False)}",
                f"batches: {getattr(report, 'batch_count', 0)}",
                f"orders: {getattr(report, 'order_count', 0)}",
                f"tickets: {len(getattr(getattr(report, 'coordination', None), 'tickets', ()))}",
                f"errors: {', '.join(getattr(report, 'errors', ()))}",
                f"warnings: {', '.join(getattr(report, 'warnings', ()))}",
            ]
        ),
        chat_id=chat_id,
        disable_notification=disable_notification,
        metadata={
            "source": "order-runtime-submit",
            "runtime_id": getattr(report, "runtime_id", ""),
            "status": getattr(report, "status", ""),
        },
    )


def notify_order_supervisor_report(
    service: NotificationService,
    report: Any,
    *,
    chat_id: str | None = None,
    disable_notification: bool = False,
) -> dict[str, Any]:
    final_status = getattr(report, "final_status", None)
    order_snapshot = getattr(final_status, "order_snapshot", None)
    return service.notify_user_message(
        category="order",
        title=f"Order supervisor {getattr(report, 'status', 'unknown')}",
        message="\n".join(
            [
                f"runtime: {getattr(report, 'runtime_id', '')}",
                f"account: {getattr(final_status, 'broker_account_id', '') if final_status else ''}",
                f"market_scope: {getattr(final_status, 'market_scope', '') if final_status else ''}",
                f"poll_events: {getattr(report, 'poll_event_count', 0)}",
                f"poll_fills: {getattr(report, 'poll_fill_event_count', 0)}",
                f"open_tickets: {len(getattr(order_snapshot, 'open_tickets', ())) if order_snapshot else 0}",
                f"unallocated_fills: {getattr(final_status, 'unallocated_fill_count', 0) if final_status else 0}",
                f"errors: {', '.join(getattr(report, 'errors', ()))}",
            ]
        ),
        chat_id=chat_id,
        disable_notification=disable_notification,
        metadata={
            "source": "order-runtime-supervise",
            "runtime_id": getattr(report, "runtime_id", ""),
            "status": getattr(report, "status", ""),
        },
    )


def _paths(paths: NotificationPaths | None) -> NotificationPaths:
    return paths or NotificationPaths()


def _env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _sanitize_line(text: str, *, limit: int = 1000) -> str:
    compact = " ".join(str(text).split())
    return compact[:limit]


def _sanitize_message_body(text: str, *, limit: int = 1500) -> str:
    raw_lines = str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    compact_lines: list[str] = []
    previous_blank = False
    in_code_block = False
    for raw_line in raw_lines:
        if raw_line.strip().startswith("```"):
            compact_lines.append(raw_line.rstrip())
            in_code_block = not in_code_block
            previous_blank = False
            continue
        if in_code_block:
            compact_lines.append(raw_line.rstrip())
            previous_blank = False
            continue
        line = " ".join(raw_line.split())
        if not line:
            if previous_blank:
                continue
            compact_lines.append("")
            previous_blank = True
            continue
        compact_lines.append(line)
        previous_blank = False
    return "\n".join(compact_lines).strip()[:limit]


def _format_message(title: str, body: str, *, category: str) -> str:
    return "\n".join([f"[{category.upper()}] {title}", "", body]).strip()


def _notification_route_for_category(category: str) -> str:
    category_key = _sanitize_line(category, limit=40).lower()
    if category_key == "order":
        return "order"
    return "default"


def _telegram_message_id(response: Mapping[str, Any]) -> Any:
    result = response.get("result")
    if isinstance(result, Mapping):
        return result.get("message_id")
    return None


def _normalize_telegram_update(update: Mapping[str, Any]) -> dict[str, Any]:
    update_id = update.get("update_id")
    message = update.get("message") if isinstance(update.get("message"), Mapping) else {}
    text = normalize_agent_text(str(message.get("text") or "")) if isinstance(message, Mapping) else ""
    chat = message.get("chat") if isinstance(message, Mapping) and isinstance(message.get("chat"), Mapping) else {}
    record_id = f"telegram-update-{update_id}" if update_id is not None else _make_record_id("telegram-update")
    return {
        "record_id": record_id,
        "source": "telegram",
        "update_id": update_id,
        "received_at": _utc_timestamp(),
        "chat_id": str(chat.get("id") or "") if isinstance(chat, Mapping) else "",
        "text": text,
        "raw_update": dict(update),
    }


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_record_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"


def _write_json_file(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _list_json_files(root: Path) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    return tuple(sorted(root.glob("*.json"), reverse=True))


def _append_audit_event(path: Path, event_type: str, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "event_type": event_type,
        "recorded_at": _utc_timestamp(),
        "payload": dict(payload),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
