from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from leaps_quant_engine.alpha import Insight, InsightDirection, InsightType
from leaps_quant_engine.framework.portfolio_construction import (
    PortfolioAllocationTarget,
    PortfolioTargetBatch,
    PortfolioTargetPlan,
)
from leaps_quant_engine.models import Symbol


@dataclass(frozen=True, slots=True)
class FrameworkRunnerState:
    sleeve_id: str
    updated_at: datetime
    active_insights: tuple[Insight, ...] = ()
    alpha_last_run_by_alpha_id: Mapping[str, datetime] = field(default_factory=dict)
    last_portfolio_run_at: datetime | None = None
    last_portfolio_target_batch: PortfolioTargetBatch | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "sleeve_id": self.sleeve_id,
            "updated_at": self.updated_at.isoformat(),
            "active_insights": [insight.to_dict() for insight in self.active_insights],
            "alpha_last_run_by_alpha_id": {
                alpha_id: ran_at.isoformat()
                for alpha_id, ran_at in self.alpha_last_run_by_alpha_id.items()
            },
            "last_portfolio_run_at": self.last_portfolio_run_at.isoformat()
            if self.last_portfolio_run_at is not None
            else None,
            "last_portfolio_target_batch": self.last_portfolio_target_batch.to_dict()
            if self.last_portfolio_target_batch is not None
            else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FrameworkRunnerState":
        return cls(
            sleeve_id=str(payload.get("sleeve_id") or ""),
            updated_at=_parse_datetime(payload.get("updated_at")) or datetime.now(),
            active_insights=tuple(
                _insight_from_dict(item)
                for item in payload.get("active_insights", []) or []
                if isinstance(item, Mapping)
            ),
            alpha_last_run_by_alpha_id={
                str(alpha_id): parsed
                for alpha_id, value in dict(payload.get("alpha_last_run_by_alpha_id") or {}).items()
                if (parsed := _parse_datetime(value)) is not None
            },
            last_portfolio_run_at=_parse_datetime(payload.get("last_portfolio_run_at")),
            last_portfolio_target_batch=_portfolio_target_batch_from_dict(
                payload.get("last_portfolio_target_batch")
            ),
        )


@dataclass(frozen=True, slots=True)
class FileFrameworkRunnerStateStore:
    path: Path

    def load(self) -> FrameworkRunnerState | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        return FrameworkRunnerState.from_dict(payload)

    def save(self, state: FrameworkRunnerState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(f"{self.path.suffix}.tmp")
        tmp_path.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self.path)


def _portfolio_target_batch_from_dict(payload: Any) -> PortfolioTargetBatch | None:
    if not isinstance(payload, Mapping):
        return None
    return PortfolioTargetBatch(
        sleeve_id=str(payload.get("sleeve_id") or ""),
        generated_at=_parse_datetime(payload.get("generated_at")) or datetime.now(),
        targets=tuple(
            _allocation_target_from_dict(item)
            for item in payload.get("targets", []) or []
            if isinstance(item, Mapping)
        ),
        plans=tuple(
            _target_plan_from_dict(item)
            for item in payload.get("plans", []) or []
            if isinstance(item, Mapping)
        ),
        source_insight_ids=tuple(str(item) for item in payload.get("source_insight_ids", []) or []),
        model_name=str(payload.get("model_name") or ""),
        reason=str(payload.get("reason") or ""),
        metadata=dict(payload.get("metadata") or {}),
        batch_id=str(payload.get("batch_id") or ""),
    )


def _target_plan_from_dict(payload: Mapping[str, Any]) -> PortfolioTargetPlan:
    target = _allocation_target_from_dict(payload)
    return PortfolioTargetPlan(
        target=target,
        current_quantity=int(payload.get("current_quantity") or 0),
        current_price=_optional_float(payload.get("current_price")),
        current_value=float(payload.get("current_value") or 0.0),
        target_percent=float(payload.get("target_percent") or 0.0),
        desired_value=float(payload.get("desired_value") or 0.0),
        source_insight_ids=tuple(str(item) for item in payload.get("source_insight_ids", []) or []),
        reason=str(payload.get("reason") or ""),
    )


def _allocation_target_from_dict(payload: Mapping[str, Any]) -> PortfolioAllocationTarget:
    return PortfolioAllocationTarget(
        symbol=_symbol_from_key(str(payload.get("symbol") or "")),
        target_percent=float(payload.get("target_percent") or 0.0),
        tag=str(payload.get("tag") or ""),
    )


def _insight_from_dict(payload: Mapping[str, Any]) -> Insight:
    return Insight(
        sleeve_id=str(payload.get("sleeve_id") or ""),
        symbol=_symbol_from_key(str(payload.get("symbol") or "")),
        direction=InsightDirection(str(payload.get("direction") or "flat")),
        generated_at=_parse_datetime(payload.get("generated_at")) or datetime.now(),
        source_snapshot_id=_optional_str(payload.get("source_snapshot_id")),
        alpha_id=str(payload.get("alpha_id") or ""),
        alpha_version=str(payload.get("alpha_version") or ""),
        insight_type=InsightType(str(payload.get("type") or "price")),
        expires_at=_parse_datetime(payload.get("expires_at")),
        magnitude=_optional_float(payload.get("magnitude")),
        confidence=float(payload.get("confidence") if payload.get("confidence") is not None else 1.0),
        weight=_optional_float(payload.get("weight")),
        score=_optional_float(payload.get("score")),
        group_id=_optional_str(payload.get("group_id")),
        reason=str(payload.get("reason") or ""),
        metadata=dict(payload.get("metadata") or {}),
        insight_id=str(payload.get("insight_id") or ""),
    )


def _symbol_from_key(symbol_key: str) -> Symbol:
    market, _, ticker = symbol_key.partition(":")
    if not ticker:
        return Symbol(ticker=symbol_key, market="")
    return Symbol(ticker=ticker, market=market)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None
