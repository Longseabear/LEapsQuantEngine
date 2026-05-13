from __future__ import annotations

from dataclasses import dataclass

from leaps_quant_engine.universe.selection import (
    UniverseSelectionCandidate,
    UniverseSelectionContext,
    build_universe_selection_result,
)


@dataclass(frozen=True, slots=True)
class EtfRotationSelectionModel:
    max_active_symbols: int = 8
    selection_id: str = "us_etf_rotation"
    defensive_tickers: tuple[str, ...] = ("TLT", "IEF", "GLD", "USMV", "XLP", "XLU")

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
        risk_on = _market_risk_on(context)
        for symbol in context.universe.symbols:
            if not _is_etf(context, symbol.key):
                rejected[symbol.key] = ("not_etf",)
                continue
            close = _first_value(context, symbol.key, ("identity_close", "close"))
            trend_average = _first_value(context, symbol.key, ("sma_200_close", "sma_100_close", "sma_20_close"))
            momentum_3m = _first_value(context, symbol.key, ("roc_63_close", "roc_60_close", "roc_20_close"))
            momentum_6m = _first_value(context, symbol.key, ("roc_126_close", "roc_120_close", "roc_63_close"))
            momentum_12m = _first_value(context, symbol.key, ("roc_252_close", "roc_240_close", "roc_126_close"))
            volatility = _first_value(context, symbol.key, ("stddev_63_close", "stddev_20_close", "volatility_20_close"))
            liquidity = _first_value(context, symbol.key, ("rolling_dollar_volume_20", "dollar_volume_2", "dollar_volume_1"))
            if close is None or trend_average is None or momentum_3m is None or momentum_6m is None:
                rejected[symbol.key] = ("missing_momentum",)
                continue
            defensive = _is_defensive_symbol(symbol.key, self.defensive_tickers)
            if close <= trend_average:
                rejected[symbol.key] = ("below_trend_filter",)
                continue
            if not defensive and not risk_on:
                rejected[symbol.key] = ("market_risk_off",)
                continue
            composite_momentum = (0.45 * momentum_6m) + (0.35 * momentum_3m) + (0.20 * (momentum_12m or momentum_6m))
            if composite_momentum <= 0 and not defensive:
                rejected[symbol.key] = ("negative_absolute_momentum",)
                continue
            normalized_volatility = 0.0 if volatility is None or close <= 0 else volatility / close
            volatility_penalty = min(normalized_volatility, 0.30) * (0.55 if defensive else 0.75)
            liquidity_bonus = 0.0 if liquidity is None else min(liquidity / 1_000_000_000.0, 0.05)
            defensive_bonus = 0.05 if defensive and not risk_on else 0.0
            score = composite_momentum - volatility_penalty + liquidity_bonus + defensive_bonus
            scored.append((score, symbol, composite_momentum, normalized_volatility, liquidity, risk_on, defensive))

        ranked_selected = tuple(
            item[1]
            for item in sorted(scored, key=lambda item: (item[0], item[1].key), reverse=True)[: self.max_active_symbols]
        )
        forced_etfs = tuple(
            symbol
            for symbol in context.forced_symbols
            if _is_etf(context, symbol.key)
        )
        selected = _dedupe_symbols((*ranked_selected, *forced_etfs))
        selected_keys = {symbol.key for symbol in selected}
        for score, symbol, momentum, volatility, liquidity, risk_on, defensive in scored:
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
                    "risk_on": risk_on,
                    "defensive": defensive,
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


def _market_risk_on(context: UniverseSelectionContext) -> bool:
    close = _first_value(context, "US:SPY", ("identity_close", "close"))
    trend = _first_value(context, "US:SPY", ("sma_200_close", "sma_100_close", "sma_20_close"))
    momentum = _first_value(context, "US:SPY", ("roc_126_close", "roc_63_close", "roc_20_close"))
    if close is None or trend is None or momentum is None:
        return False
    return close > trend and momentum > 0


def _is_defensive_symbol(symbol_key: str, defensive_tickers: tuple[str, ...]) -> bool:
    ticker = symbol_key.split(":", 1)[-1].upper()
    return ticker in set(defensive_tickers)


def _is_etf(context: UniverseSelectionContext, symbol_key: str) -> bool:
    properties = context.universe.properties_for(symbol_key)
    asset_type = str(properties.get("asset_type") or properties.get("type") or "").strip().lower()
    if asset_type == "etf":
        return True
    value = properties.get("is_etf")
    return bool(value) if isinstance(value, bool) else str(value).strip().lower() in {"1", "true", "yes", "y"}


def _dedupe_symbols(symbols):
    result = []
    seen = set()
    for symbol in symbols:
        if symbol.key in seen:
            continue
        seen.add(symbol.key)
        result.append(symbol)
    return tuple(result)
