from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
import time
from typing import Any, Mapping

import requests


DEFAULT_KIS_GATEWAY_HOST = "127.0.0.1"
DEFAULT_KIS_GATEWAY_PORT = 8766
DEFAULT_KIS_GATEWAY_BASE_URL = f"http://{DEFAULT_KIS_GATEWAY_HOST}:{DEFAULT_KIS_GATEWAY_PORT}"


class KISGatewayClientError(RuntimeError):
    """Raised when the local KIS gateway cannot serve a request."""


@dataclass(slots=True)
class KISGatewayClient:
    """HTTP client for the local KIS gateway process.

    Runtime code uses this client when it should share the AppKey lane with
    other live components instead of creating another in-process KIS client.
    """

    base_url: str = DEFAULT_KIS_GATEWAY_BASE_URL
    session: requests.Session = field(default_factory=requests.Session)
    rate_limit_per_second: int = 18
    cache_dir: Path = Path("data/kis-cache")
    timeout_seconds: float = 60.0
    _lock: Lock = field(default_factory=Lock)
    _last_request_at: float = 0.0

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    def health_check(self) -> dict[str, Any]:
        try:
            response = self.session.get(f"{self.base_url}/health", timeout=5)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise KISGatewayClientError(f"KIS gateway health failed at {self.base_url}.") from exc
        except ValueError as exc:
            raise KISGatewayClientError("KIS gateway health returned non-JSON.") from exc
        if not isinstance(payload, dict):
            raise KISGatewayClientError("KIS gateway health returned a non-object payload.")
        return payload

    def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.call_operation(tool, arguments)

    def call_operation(self, operation: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._wait_for_turn()
        try:
            response = self.session.post(
                f"{self.base_url}/call",
                json={"operation": operation, "arguments": dict(arguments or {})},
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise KISGatewayClientError(f"Failed to call KIS gateway operation '{operation}'.") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise KISGatewayClientError(
                f"KIS gateway operation '{operation}' returned non-JSON (HTTP {response.status_code})."
            ) from exc
        if response.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else payload
            raise KISGatewayClientError(f"KIS gateway operation '{operation}' failed: {detail}")
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise KISGatewayClientError(f"KIS gateway operation '{operation}' returned an unexpected payload.")
        return result

    def _wait_for_turn(self) -> None:
        min_interval = 1.0 / max(int(self.rate_limit_per_second or 1), 1)
        with self._lock:
            elapsed = time.monotonic() - self._last_request_at
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            self._last_request_at = time.monotonic()
