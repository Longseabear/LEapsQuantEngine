from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from leaps_quant_engine.framework.portfolio_construction import (
    PortfolioAllocationTarget,
    PortfolioConstructionContext,
    PortfolioTargetBatch,
)


@dataclass(frozen=True, slots=True)
class PortfolioTargetResolutionPolicy:
    mode: str = "complete"
    zero_missing_tag: str = "portfolio_target_resolver:missing_target_zero"
    zero_missing_when_raw_empty: bool = False

    def __post_init__(self) -> None:
        mode = str(self.mode or "complete").strip().lower()
        if mode not in {"complete", "patch", "raw"}:
            raise ValueError("portfolio target resolution mode must be 'complete', 'patch', or 'raw'.")
        tag = str(self.zero_missing_tag or "portfolio_target_resolver:missing_target_zero").strip()
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "zero_missing_tag", tag)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "zero_missing_tag": self.zero_missing_tag,
            "zero_missing_when_raw_empty": self.zero_missing_when_raw_empty,
        }


@dataclass(frozen=True, slots=True)
class PortfolioTargetResolutionDecision:
    targets: tuple[PortfolioAllocationTarget, ...]
    reason: str
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PortfolioTargetResolver:
    policy: PortfolioTargetResolutionPolicy = field(default_factory=PortfolioTargetResolutionPolicy)

    def resolve(
        self,
        context: PortfolioConstructionContext,
        raw_batch: PortfolioTargetBatch,
        *,
        previous_batch: PortfolioTargetBatch | None = None,
    ) -> PortfolioTargetResolutionDecision:
        raw_targets = _target_map(raw_batch.targets)
        previous_targets = _target_map(previous_batch.targets if previous_batch is not None else ())
        resolved = dict(raw_targets.targets)
        resolved_order = list(raw_targets.order)
        carried_symbols: list[str] = []
        zeroed_symbols: list[str] = []

        if self.policy.mode == "raw":
            return self._decision(
                raw_batch,
                tuple(resolved[key] for key in resolved_order),
                status="raw",
                raw_count=len(raw_batch.targets),
                duplicate_raw_symbols=raw_targets.duplicate_symbols,
                carried_symbols=(),
                zeroed_symbols=(),
            )

        if self.policy.mode == "patch":
            for symbol_key in previous_targets.order:
                if symbol_key in resolved:
                    continue
                resolved[symbol_key] = previous_targets.targets[symbol_key]
                resolved_order.append(symbol_key)
                carried_symbols.append(symbol_key)
        else:
            if resolved or self.policy.zero_missing_when_raw_empty:
                for symbol_key in previous_targets.order:
                    if symbol_key in resolved:
                        continue
                    previous = previous_targets.targets[symbol_key]
                    if abs(previous.target_percent) <= 1e-12:
                        continue
                    resolved[symbol_key] = self._zero_target(previous)
                    resolved_order.append(symbol_key)
                    zeroed_symbols.append(symbol_key)
                for symbol in context.held_symbols:
                    if symbol.key in resolved:
                        continue
                    if context.portfolio.quantity(symbol) == 0:
                        continue
                    resolved[symbol.key] = PortfolioAllocationTarget(
                        symbol=symbol,
                        target_percent=0.0,
                        tag=self.policy.zero_missing_tag,
                    )
                    resolved_order.append(symbol.key)
                    zeroed_symbols.append(symbol.key)

        targets = tuple(resolved[key] for key in resolved_order)
        status = "empty_no_action" if self.policy.mode == "complete" and not raw_targets.targets and not targets else self.policy.mode
        return self._decision(
            raw_batch,
            targets,
            status=status,
            raw_count=len(raw_batch.targets),
            duplicate_raw_symbols=raw_targets.duplicate_symbols,
            carried_symbols=tuple(carried_symbols),
            zeroed_symbols=tuple(zeroed_symbols),
        )

    def _zero_target(self, target: PortfolioAllocationTarget) -> PortfolioAllocationTarget:
        return PortfolioAllocationTarget(
            symbol=target.symbol,
            target_percent=0.0,
            tag=self.policy.zero_missing_tag,
        )

    def _decision(
        self,
        raw_batch: PortfolioTargetBatch,
        targets: tuple[PortfolioAllocationTarget, ...],
        *,
        status: str,
        raw_count: int,
        duplicate_raw_symbols: tuple[str, ...],
        carried_symbols: tuple[str, ...],
        zeroed_symbols: tuple[str, ...],
    ) -> PortfolioTargetResolutionDecision:
        metadata = {
            "portfolio_target_resolution": {
                **self.policy.to_dict(),
                "status": status,
                "raw_batch_id": raw_batch.batch_id,
                "raw_target_count": raw_count,
                "resolved_target_count": len(targets),
                "duplicate_raw_symbols": list(duplicate_raw_symbols),
                "carried_symbols": list(carried_symbols),
                "zeroed_symbols": list(zeroed_symbols),
                "carried_count": len(carried_symbols),
                "zeroed_count": len(zeroed_symbols),
            }
        }
        reason = raw_batch.reason
        if carried_symbols or zeroed_symbols or duplicate_raw_symbols:
            reason = f"{reason}:target_resolved" if reason else "portfolio_target_resolved"
        return PortfolioTargetResolutionDecision(targets=targets, reason=reason, metadata=metadata)


@dataclass(frozen=True, slots=True)
class _TargetMap:
    targets: Mapping[str, PortfolioAllocationTarget]
    order: tuple[str, ...]
    duplicate_symbols: tuple[str, ...]


def _target_map(targets: tuple[PortfolioAllocationTarget, ...]) -> _TargetMap:
    mapped: dict[str, PortfolioAllocationTarget] = {}
    order: list[str] = []
    duplicates: list[str] = []
    for target in targets:
        symbol_key = target.symbol.key
        if symbol_key not in mapped:
            order.append(symbol_key)
        else:
            duplicates.append(symbol_key)
        mapped[symbol_key] = target
    return _TargetMap(
        targets=mapped,
        order=tuple(order),
        duplicate_symbols=tuple(dict.fromkeys(duplicates)),
    )
