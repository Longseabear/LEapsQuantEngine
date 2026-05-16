from __future__ import annotations

from dataclasses import dataclass

from leaps_quant_engine.universe.selection import (
    UniverseSelectionCandidate,
    UniverseSelectionContext,
    build_universe_selection_result,
)


ROLE_PRIORITY = {
    "cash_like": 100.0,
    "benchmark": 90.0,
    "market_beta": 85.0,
    "inverse": 70.0,
    "sector_beta": 35.0,
}


@dataclass(frozen=True, slots=True)
class KrxEtfSafetySelectionModel:
    max_active_symbols: int = 12
    selection_id: str = "leaps-krx-etf-safety"

    def select(self, context: UniverseSelectionContext):
        candidates: dict[str, UniverseSelectionCandidate] = {}
        rejected: dict[str, tuple[str, ...]] = {}
        scored = []

        for symbol in context.universe.symbols:
            if symbol.market != "KRX":
                rejected[symbol.key] = ("not_krx",)
                continue
            properties = context.universe.properties_for(symbol.key)
            if not _is_etf(properties):
                rejected[symbol.key] = ("not_etf",)
                continue
            role = _role(properties)
            if not bool(properties.get("live_enabled", True)):
                rejected[symbol.key] = ("live_disabled",)
                continue
            liquidity = _first_value(context, symbol.key, ("rolling_dollar_volume_20", "dollar_volume_1", "volume"))
            liquidity_bonus = 0.0 if liquidity is None else min(float(liquidity) / 1_000_000_000_000.0, 5.0)
            score = ROLE_PRIORITY.get(role, 20.0) + liquidity_bonus
            scored.append((score, symbol, role, liquidity))

        selected = tuple(
            item[1]
            for item in sorted(scored, key=lambda item: (item[0], item[1].key), reverse=True)[
                : max(0, self.max_active_symbols)
            ]
        )
        selected_keys = {symbol.key for symbol in selected}
        for score, symbol, role, liquidity in scored:
            candidates[symbol.key] = UniverseSelectionCandidate(
                symbol=symbol,
                score=score,
                selected=symbol.key in selected_keys,
                forced=symbol.key in context.forced_symbol_keys,
                reasons=("krx_etf_safety_candidate",),
                metadata={
                    "role": role,
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


def _is_etf(properties: dict[str, object]) -> bool:
    asset_type = str(properties.get("asset_type") or properties.get("type") or "").strip().lower()
    if asset_type == "etf":
        return True
    value = properties.get("is_etf")
    return bool(value) if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "y"}


def _role(properties: dict[str, object]) -> str:
    role = str(properties.get("krw_safety_role") or properties.get("etf_role") or "").strip().lower()
    return role or "sector_beta"
