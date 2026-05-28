from dataclasses import dataclass

from leaps_quant_engine.universe.selection import (
    UniverseSelectionCandidate,
    UniverseSelectionContext,
    build_universe_selection_result,
)


@dataclass(frozen=True, slots=True)
class WatchlistSelectionModel:
    selection_id: str = "kr-domestic-4401-watchlist"
    max_symbols: int | None = None

    def select(self, context: UniverseSelectionContext):
        symbols = tuple(
            symbol
            for symbol in context.universe.symbols
            if symbol.key.startswith("KRX:")
        )
        if self.max_symbols is not None and self.max_symbols > 0:
            symbols = symbols[: self.max_symbols]
        selected_keys = {symbol.key for symbol in symbols}
        candidates = {
            symbol.key: UniverseSelectionCandidate(
                symbol=symbol,
                score=1.0,
                selected=symbol.key in selected_keys,
                forced=symbol.key in context.forced_symbol_keys,
                reasons=("watchlist",),
            )
            for symbol in symbols
        }
        rejected = {
            symbol.key: ("not_krx_watchlist_symbol",)
            for symbol in context.universe.symbols
            if symbol.key not in selected_keys
        }
        return build_universe_selection_result(
            context,
            symbols,
            selection_id=self.selection_id,
            candidates=candidates,
            rejected=rejected,
        )


def create_selection_model(params):
    max_symbols = params.get("max_symbols")
    return WatchlistSelectionModel(
        selection_id=str(params.get("selection_id", "kr-domestic-4401-watchlist")),
        max_symbols=int(max_symbols) if max_symbols not in (None, "") else None,
    )
