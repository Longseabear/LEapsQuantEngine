from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException
import requests
import uvicorn

from leaps_quant_engine.adapters.kis_direct import KISDirectClient, KISDirectClientError
from leaps_quant_engine.kis_gateway_client import (
    DEFAULT_KIS_GATEWAY_BASE_URL,
    DEFAULT_KIS_GATEWAY_HOST,
    DEFAULT_KIS_GATEWAY_PORT,
    KISGatewayClient,
    KISGatewayClientError,
)
from leaps_quant_engine.settings import KISSettings, load_kis_settings


@dataclass(slots=True)
class KISGatewayService:
    """Local AppKey-lane KIS gateway for shared health, pacing, and calls."""

    client: KISDirectClient
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    _lock: Lock = field(default_factory=Lock)
    _total_calls: int = 0
    _total_failures: int = 0
    _last_call_at: str | None = None
    _last_error: str | None = None
    _calls_by_operation: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls, *, cache_dir: Path | None = None) -> "KISGatewayService":
        settings = load_kis_settings()
        return cls.from_settings(settings, cache_dir=cache_dir)

    @classmethod
    def from_settings(cls, settings: KISSettings, *, cache_dir: Path | None = None) -> "KISGatewayService":
        client = KISDirectClient.from_settings(settings)
        if cache_dir is not None:
            client.cache_dir = cache_dir
        return cls(client)

    def health_check(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        client_health = self.client.health_check()
        with self._lock:
            counters = {
                "total_calls": self._total_calls,
                "total_failures": self._total_failures,
                "last_call_at": self._last_call_at,
                "last_error": self._last_error,
                "calls_by_operation": dict(sorted(self._calls_by_operation.items())),
            }
        return {
            "status": "ok",
            "server": "leaps-kis-gateway",
            "transport": "http",
            "started_at": self.started_at.isoformat(),
            "uptime_seconds": max((now - self.started_at).total_seconds(), 0.0),
            "lane": {
                "base_url": self.client.settings.base_url,
                "app_key_fingerprint": _fingerprint(self.client.settings.app_key),
                "mock": self.client.settings.mock,
                "query_rate_limit_per_second": client_health.get("query_rate_limit_per_second"),
                "request_rate_limit_per_second": client_health.get("request_rate_limit_per_second"),
                "quota_key": _quota_key(self.client.settings),
            },
            "kis": client_health,
            "counters": counters,
        }

    def call_operation(self, operation: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._total_calls += 1
            self._last_call_at = datetime.now(timezone.utc).isoformat()
            self._calls_by_operation[operation] = self._calls_by_operation.get(operation, 0) + 1
        try:
            return self.client.call_operation(operation, dict(arguments or {}))
        except Exception as exc:
            with self._lock:
                self._total_failures += 1
                self._last_error = str(exc)
            raise


def create_kis_gateway_app(service: KISGatewayService) -> FastAPI:
    app = FastAPI(
        title="LEaps KIS Gateway",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
    )
    app.state.kis_gateway_service = service

    @app.get("/health")
    def health() -> dict[str, Any]:
        return service.health_check()

    @app.post("/call")
    def call(payload: dict[str, Any]) -> dict[str, Any]:
        operation = str(payload.get("operation") or "").strip()
        if not operation:
            raise HTTPException(status_code=400, detail="operation is required")
        arguments = payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
        try:
            result = service.call_operation(operation, arguments)
        except KISDirectClientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"status": "ok", "operation": operation, "result": result}

    return app


def run_kis_gateway_http_server(
    service: KISGatewayService,
    *,
    host: str = DEFAULT_KIS_GATEWAY_HOST,
    port: int = DEFAULT_KIS_GATEWAY_PORT,
) -> None:
    app = create_kis_gateway_app(service)
    uvicorn.run(app, host=host, port=port, log_level="info")


def fetch_kis_gateway_health(base_url: str, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
    response = requests.get(f"{base_url.rstrip('/')}/health", timeout=timeout_seconds)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("KIS gateway health returned a non-object payload.")
    return payload


def _fingerprint(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:12]


def _quota_key(settings: KISSettings) -> str:
    mode = "mock" if settings.mock else "real"
    return f"{settings.base_url}|{_fingerprint(settings.app_key)}|{mode}"
