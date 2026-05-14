from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


class TelegramConfigError(RuntimeError):
    """Raised when Telegram delivery is requested without usable credentials."""


@dataclass(frozen=True, slots=True)
class TelegramClient:
    """Small Telegram Bot API client used by LEaps notification surfaces."""

    bot_token: str
    default_chat_id: str | None = None
    timeout_seconds: float = 20.0

    @property
    def base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}"

    def send_message(
        self,
        *,
        text: str,
        chat_id: str | None = None,
        disable_notification: bool = False,
        parse_mode: str | None = None,
    ) -> dict[str, Any]:
        target_chat_id = chat_id or self.default_chat_id
        if not target_chat_id:
            raise TelegramConfigError("Telegram chat id is not configured.")
        payload: dict[str, Any] = {
            "chat_id": target_chat_id,
            "text": normalize_agent_text(text),
            "disable_notification": disable_notification,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        response = requests.post(
            f"{self.base_url}/sendMessage",
            json=payload,
            timeout=self.timeout_seconds,
        )
        _raise_for_telegram_error(response, method="sendMessage")
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("Telegram response must be a JSON object.")
        return body

    def get_updates(self, *, offset: int | None = None, limit: int = 20) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"limit": int(limit), "timeout": 1}
        if offset is not None:
            payload["offset"] = int(offset)
        response = requests.get(f"{self.base_url}/getUpdates", params=payload, timeout=self.timeout_seconds)
        _raise_for_telegram_error(response, method="getUpdates")
        body = response.json()
        if not isinstance(body, dict):
            raise RuntimeError("Telegram response must be a JSON object.")
        result = body.get("result", [])
        return result if isinstance(result, list) else []


def _raise_for_telegram_error(response: requests.Response, *, method: str) -> None:
    """Raise a sanitized Telegram error without echoing the bot token URL."""

    if response.status_code < 400:
        return
    description = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        description = str(body.get("description") or "")
    if not description:
        description = response.reason or "request failed"
    raise RuntimeError(f"Telegram {method} failed ({response.status_code}): {description}")


def normalize_agent_text(text: str) -> str:
    """Normalize text from agent/CLI surfaces and repair common Windows mojibake."""

    normalized = str(text).replace("\r\n", "\n").replace("\r", "\n")
    return _repair_utf8_read_as_cp949(normalized)


def _repair_utf8_read_as_cp949(text: str) -> str:
    """Repair strings such as Korean UTF-8 bytes decoded through CP949."""

    try:
        candidate = text.encode("cp949").decode("utf-8")
    except UnicodeError:
        return text
    if _text_quality_score(candidate) > _text_quality_score(text):
        return candidate
    return text


def _text_quality_score(text: str) -> int:
    hangul = sum(1 for char in text if "\uac00" <= char <= "\ud7a3")
    replacement = text.count("\ufffd") + text.count("?")
    suspicious = sum(1 for char in text if char in _SUSPICIOUS_MOJIBAKE_CHARS)
    return hangul * 3 - suspicious * 2 - replacement


_SUSPICIOUS_MOJIBAKE_CHARS = set(
    "臾蹂怨援湲遺留吏紐諛寃곕씪븘쟾쓣쓽씠꽌몄꽭뜽⑤쟻"
)
