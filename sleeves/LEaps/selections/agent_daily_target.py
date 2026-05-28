from __future__ import annotations

from dataclasses import dataclass

from leaps_quant_engine.agent_targets import load_agent_target_artifact
from leaps_quant_engine.universe.selection import (
    UniverseSelectionCandidate,
    UniverseSelectionContext,
    build_universe_selection_result,
)


DEFAULT_TARGET_PATH = "data/operator-targets/LEaps/latest_target.json"


@dataclass(frozen=True, slots=True)
class AgentDailyTargetSelectionModel:
    max_active_symbols: int = 60
    target_path: str = DEFAULT_TARGET_PATH
    selection_id: str = "leaps-agent-daily-target"
    default_market: str = "KRX"
    allowed_markets: tuple[str, ...] = ("KRX",)
    max_age_hours: float = 36.0
    require_sleeve_id: bool = True

    def select(self, context: UniverseSelectionContext):
        result = load_agent_target_artifact(
            self.target_path,
            as_of=context.generated_at,
            sleeve_id=context.sleeve_id,
            require_sleeve_id=self.require_sleeve_id,
            max_age_hours=self.max_age_hours,
            default_market=self.default_market,
            allowed_markets=self.allowed_markets,
            allow_short=False,
        )
        if not result.is_usable or result.artifact is None or result.artifact.flatten:
            return build_universe_selection_result(
                context,
                (),
                selection_id=self.selection_id,
                candidates={},
                rejected={symbol.key: (result.status,) for symbol in context.universe.symbols},
            )

        coarse_symbols = {symbol.key: symbol for symbol in context.universe.symbols}
        candidates: dict[str, UniverseSelectionCandidate] = {}
        rejected: dict[str, tuple[str, ...]] = {}
        selected = []
        for index, target in enumerate(result.artifact.targets):
            symbol = coarse_symbols.get(target.symbol.key)
            if symbol is None:
                rejected[target.symbol.key] = ("target_symbol_not_in_coarse_universe",)
                continue
            if len(selected) >= self.max_active_symbols:
                rejected[target.symbol.key] = ("max_active_symbols_reached",)
                continue
            selected.append(symbol)
            candidates[symbol.key] = UniverseSelectionCandidate(
                symbol=symbol,
                score=float(target.target_percent),
                selected=True,
                forced=symbol.key in context.forced_symbol_keys,
                reasons=("agent_daily_target",),
                metadata={
                    "target_percent": target.target_percent,
                    "target_rank": index + 1,
                    "target_name": target.name,
                    "target_reason": target.reason,
                    "target_confidence": target.confidence,
                    "artifact_id": result.artifact.target_id,
                },
            )

        return build_universe_selection_result(
            context,
            tuple(selected),
            selection_id=self.selection_id,
            candidates=candidates,
            rejected=rejected,
        )
