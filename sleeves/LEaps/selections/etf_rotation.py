from __future__ import annotations

from dataclasses import dataclass

from leaps_quant_engine.universe.selection import (
    UniverseSelectionCandidate,
    UniverseSelectionContext,
    build_universe_selection_result,
)


@dataclass(frozen=True, slots=True)
class EtfRotationSelectionModel:
    max_active_symbols: int = 20
    selection_id: str = "leaps-etf-rotation"

    def select(self, context: UniverseSelectionContext):
        if context.indicator_snapshot is None:
            return build_universe_selection_result(
                context,
                (),
                selection_id=self.selection_id,
                candidates={},
                rejected={symbol.key: ("missing_indicator_snapshot",) for symbol in context.universe.symbols},
            )

        candidates: dict[str, UniverseSelectionCandidate] = {}
        rejected: dict[str, tuple[str, ...]] = {}
        scored = []
        for symbol in context.universe.symbols:
            if not _is_etf(context, symbol.key):
                rejected[symbol.key] = ("not_etf",)
                continue
            momentum = _first_value(context, symbol.key, ("roc_20_close", "momentum_20_close", "momentum_5_close", "momentum_2_close"))
            volatility = _first_value(context, symbol.key, ("stddev_20_close", "volatility_20_close"))
            liquidity = _first_value(context, symbol.key, ("rolling_dollar_volume_20", "dollar_volume_2", "dollar_volume_1"))
            if momentum is None:
                rejected[symbol.key] = ("missing_momentum",)
                continue
            volatility_penalty = 0.0 if volatility is None else volatility * 0.1
            liquidity_bonus = 0.0 if liquidity is None else min(liquidity / 1_000_000_000.0, 0.05)
            score = momentum - volatility_penalty + liquidity_bonus
            scored.append((score, symbol, momentum, volatility, liquidity))

        selected = tuple(
            item[1]
            for item in sorted(scored, key=lambda item: (item[0], item[1].key), reverse=True)[: self.max_active_symbols]
        )
        selected_keys = {symbol.key for symbol in selected}
        for score, symbol, momentum, volatility, liquidity in scored:
            candidates[symbol.key] = UniverseSelectionCandidate(
                symbol=symbol,
                score=score,
                selected=symbol.key in selected_keys,
                forced=symbol.key in context.forced_symbol_keys,
                reasons=("etf_rotation_candidate",),
                metadata={
                    "momentum": momentum,
                    "volatility": volatility,
                    "liquidity": liquidity,
                },
            )
        return build_universe_selection_result(
            context,
            selected,
            selection_id=self.selection_id,
            candidates=candidates,
            rejected=rejected,
        )


def _first_value(context: UniverseSelectionContext, symbol_key: str, names: tuple[str, ...]) -> float | None:
    snapshot = context.indicator_snapshot
    if snapshot is None:
        return None
    for name in names:
        value = snapshot.value(symbol_key, name)
        if value is not None:
            return value
    return None


def _is_etf(context: UniverseSelectionContext, symbol_key: str) -> bool:
    properties = context.universe.properties_for(symbol_key)
    asset_type = str(properties.get("asset_type") or properties.get("type") or "").strip().lower()
    if asset_type == "etf":
        return True
    value = properties.get("is_etf")
    return bool(value) if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "y"}
