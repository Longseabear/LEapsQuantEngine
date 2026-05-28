from __future__ import annotations

from typing import Any, Mapping

from leaps_quant_engine.agent_targets import (
    AgentTargetArtifact,
    AgentTargetItem,
    clamp_pct,
    compact_tag,
    load_agent_target_artifact,
)
from leaps_quant_engine.framework import PortfolioAllocationTarget, PortfolioConstructionContext
from leaps_quant_engine.runtime_state import StatePatch


DEFAULT_TARGET_PATH = "data/operator-targets/LEaps/latest_target.json"
MODEL_ID = "leaps-agent-daily-target-portfolio"
STATE_NAMESPACE = "target_artifact"
PORTFOLIO_STATE_SYMBOL = "__portfolio__"


class AgentDailyTargetPortfolioModel:
    """Emit target percentages from a daily agent-authored target artifact."""

    def __init__(
        self,
        *,
        target_path: str = DEFAULT_TARGET_PATH,
        model_id: str = MODEL_ID,
        state_namespace: str = STATE_NAMESPACE,
        default_market: str = "KRX",
        allowed_markets: tuple[str, ...] = ("KRX",),
        max_gross_exposure: float = 0.98,
        max_position_pct: float = 0.35,
        max_age_hours: float = 36.0,
        require_sleeve_id: bool = True,
        scale_to_max_gross: bool = True,
        allow_short: bool = False,
        flatten_flag: str = "flatten",
        emit_zero_for_missing_held_targets: bool = True,
    ) -> None:
        self.target_path = target_path
        self.model_id = model_id
        self.state_namespace = state_namespace
        self.default_market = default_market
        self.allowed_markets = allowed_markets
        self.max_gross_exposure = max_gross_exposure
        self.max_position_pct = max_position_pct
        self.max_age_hours = max_age_hours
        self.require_sleeve_id = require_sleeve_id
        self.scale_to_max_gross = scale_to_max_gross
        self.allow_short = allow_short
        self.flatten_flag = flatten_flag
        self.emit_zero_for_missing_held_targets = emit_zero_for_missing_held_targets
        self.max_gross_exposure = clamp_pct(self.max_gross_exposure)
        self.max_position_pct = clamp_pct(self.max_position_pct)
        self.max_age_hours = max(0.0, float(self.max_age_hours))
        self.allowed_markets = tuple(str(market).strip().upper() for market in self.allowed_markets if str(market).strip())
        if not str(self.target_path).strip():
            raise ValueError("target_path cannot be empty.")
        self._last_status = {}

    def create_targets(self, context: PortfolioConstructionContext) -> tuple[PortfolioAllocationTarget, ...]:
        result = load_agent_target_artifact(
            self.target_path,
            as_of=context.data.time,
            sleeve_id=context.sleeve_id,
            require_sleeve_id=self.require_sleeve_id,
            max_age_hours=self.max_age_hours,
            default_market=self.default_market,
            allowed_markets=self.allowed_markets,
            allow_short=self.allow_short,
            flatten_flag=self.flatten_flag,
        )
        if not result.is_usable or result.artifact is None:
            self._last_status = self._status(context, status=result.status, target_count=0)
            return ()

        artifact = result.artifact
        if artifact.flatten:
            targets = tuple(
                PortfolioAllocationTarget(
                    symbol=symbol,
                    target_percent=0.0,
                    tag="agent_daily_target:flatten",
                )
                for symbol in context.held_symbols
                if context.portfolio.quantity(symbol) != 0 and self._market_allowed(symbol.market)
            )
            self._last_status = self._status(
                context,
                status="flatten",
                target_count=len(targets),
                gross_target=0.0,
                artifact=artifact,
            )
            return targets

        scaled_items = self._scaled_items(artifact)
        if not scaled_items:
            self._last_status = self._status(context, status="empty_no_action", target_count=0, artifact=artifact)
            return ()

        targets = [
            PortfolioAllocationTarget(
                symbol=item.symbol,
                target_percent=target_percent,
                tag=_target_tag(item, artifact, target_percent),
            )
            for item, target_percent in scaled_items
            if abs(target_percent) > 1e-12
        ]
        if self.emit_zero_for_missing_held_targets:
            target_keys = {target.symbol.key for target in targets}
            for symbol in context.held_symbols:
                if symbol.key in target_keys or not self._market_allowed(symbol.market):
                    continue
                if context.portfolio.quantity(symbol) == 0:
                    continue
                targets.append(
                    PortfolioAllocationTarget(
                        symbol=symbol,
                        target_percent=0.0,
                        tag="agent_daily_target:missing_from_daily_artifact",
                    )
                )

        gross = sum(abs(target.target_percent) for target in targets)
        self._last_status = self._status(
            context,
            status="loaded",
            target_count=len(targets),
            gross_target=gross,
            artifact=artifact,
            scaled=artifact.gross_exposure > self._gross_cap(artifact),
            scale=self._scale(artifact),
        )
        return tuple(targets)

    def state_patches(
        self,
        context: PortfolioConstructionContext,
        targets: tuple[PortfolioAllocationTarget, ...],
    ) -> tuple[StatePatch, ...]:
        value = dict(self._last_status or self._status(context, status="unknown", target_count=len(targets)))
        value["emitted_target_count"] = len(targets)
        value["emitted_gross_target"] = sum(abs(target.target_percent) for target in targets)
        return (
            StatePatch(
                key=context.model_state.key(
                    sleeve_id=context.sleeve_id,
                    model_id=self.model_id,
                    namespace=self.state_namespace,
                    symbol_key=PORTFOLIO_STATE_SYMBOL,
                ),
                value=value,
                reason="agent_daily_target_artifact_read",
                generated_at=context.data.time,
            ),
        )

    def _scaled_items(self, artifact: AgentTargetArtifact) -> tuple[tuple[AgentTargetItem, float], ...]:
        scale = self._scale(artifact)
        result: list[tuple[AgentTargetItem, float]] = []
        for item in artifact.targets:
            target_percent = item.target_percent * scale
            target_percent = max(-self.max_position_pct, min(self.max_position_pct, target_percent))
            if not self.allow_short:
                target_percent = max(0.0, target_percent)
            result.append((item, target_percent))
        return tuple(result)

    def _gross_cap(self, artifact: AgentTargetArtifact) -> float:
        artifact_cap = artifact.max_gross_exposure if artifact.max_gross_exposure is not None else self.max_gross_exposure
        return min(clamp_pct(float(artifact_cap)), self.max_gross_exposure)

    def _scale(self, artifact: AgentTargetArtifact) -> float:
        gross_cap = self._gross_cap(artifact)
        if not self.scale_to_max_gross or artifact.gross_exposure <= gross_cap or gross_cap <= 0:
            return 1.0
        return gross_cap / artifact.gross_exposure

    def _market_allowed(self, market: str) -> bool:
        return not self.allowed_markets or str(market).strip().upper() in self.allowed_markets

    def _status(
        self,
        context: PortfolioConstructionContext,
        *,
        status: str,
        target_count: int,
        gross_target: float = 0.0,
        artifact: AgentTargetArtifact | None = None,
        scaled: bool = False,
        scale: float = 1.0,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "target_path": self.target_path,
            "as_of": context.data.time.isoformat(),
            "artifact_generated_at": artifact.generated_at.isoformat() if artifact and artifact.generated_at else "",
            "artifact_expires_at": artifact.expires_at.isoformat() if artifact and artifact.expires_at else "",
            "artifact_id": artifact.target_id if artifact else "",
            "target_count": int(target_count),
            "gross_target": float(gross_target),
            "scaled_to_gross_cap": bool(scaled),
            "scale": float(scale),
        }


def create_portfolio_model(params: Mapping[str, Any] | None = None) -> AgentDailyTargetPortfolioModel:
    values = dict(params or {})
    return AgentDailyTargetPortfolioModel(
        target_path=str(values.get("target_path", DEFAULT_TARGET_PATH)),
        model_id=str(values.get("model_id", MODEL_ID)),
        state_namespace=str(values.get("state_namespace", STATE_NAMESPACE)),
        default_market=str(values.get("default_market", "KRX")),
        allowed_markets=_tuple_param(values.get("allowed_markets"), ("KRX",)),
        max_gross_exposure=float(values.get("max_gross_exposure", 0.98)),
        max_position_pct=float(values.get("max_position_pct", 0.35)),
        max_age_hours=float(values.get("max_age_hours", values.get("max_target_age_hours", 36.0))),
        require_sleeve_id=bool(values.get("require_sleeve_id", True)),
        scale_to_max_gross=bool(values.get("scale_to_max_gross", True)),
        allow_short=bool(values.get("allow_short", False)),
        flatten_flag=str(values.get("flatten_flag", "flatten")),
        emit_zero_for_missing_held_targets=bool(values.get("emit_zero_for_missing_held_targets", True)),
    )


def _target_tag(item: AgentTargetItem, artifact: AgentTargetArtifact, target_percent: float) -> str:
    source = artifact.target_id or "daily"
    reason = item.reason or "agent_target"
    parts = ["agent_daily_target", f"id={source}", f"w={target_percent:.3f}"]
    if item.confidence is not None:
        parts.append(f"confidence={item.confidence:.3f}")
    if reason:
        parts.append(f"reason={compact_tag(reason)}")
    return ":".join(parts)


def _tuple_param(value: Any, default: tuple[str, ...]) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        return (value,)
    return tuple(str(item) for item in value)
