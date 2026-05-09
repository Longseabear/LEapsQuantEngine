from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from leaps_quant_engine.backtesting import FrameworkBacktestResult, run_framework_replay
from leaps_quant_engine.framework import FrameworkRunner
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.universe.definition import UniverseDefinition


@dataclass(frozen=True, slots=True)
class MarketReplaySession:
    root: Path
    session_id: str
    sleeve_id: str

    @property
    def session_dir(self) -> Path:
        return self.root / self.session_id / self.sleeve_id

    @property
    def data_slices_path(self) -> Path:
        return self.session_dir / "data_slices.jsonl"

    @property
    def engine_status_path(self) -> Path:
        return self.session_dir / "engine_status.jsonl"

    @property
    def order_intents_path(self) -> Path:
        return self.session_dir / "order_intents.jsonl"


@dataclass(frozen=True, slots=True)
class MarketReplayStore:
    root: Path = Path("data/replay/sessions")

    def session(self, session_id: str, sleeve_id: str) -> MarketReplaySession:
        return MarketReplaySession(root=self.root, session_id=session_id, sleeve_id=sleeve_id)

    def append_data_slice(self, session_id: str, sleeve_id: str, data: DataSlice) -> Path:
        session = self.session(session_id, sleeve_id)
        _append_jsonl(session.data_slices_path, _data_slice_to_dict(data))
        return session.data_slices_path

    def write_data_slices(self, session_id: str, sleeve_id: str, slices: Iterable[DataSlice]) -> Path:
        session = self.session(session_id, sleeve_id)
        session.data_slices_path.parent.mkdir(parents=True, exist_ok=True)
        with session.data_slices_path.open("w", encoding="utf-8") as handle:
            for data in slices:
                handle.write(json.dumps(_data_slice_to_dict(data), ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
        return session.data_slices_path

    def load_data_slices(self, session_id: str, sleeve_id: str) -> list[DataSlice]:
        session = self.session(session_id, sleeve_id)
        if not session.data_slices_path.exists():
            return []
        slices = [
            _data_slice_from_dict(json.loads(line))
            for line in session.data_slices_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return sorted(slices, key=lambda data: data.time)

    def append_engine_status(self, session_id: str, sleeve_id: str, status: dict[str, Any]) -> Path:
        session = self.session(session_id, sleeve_id)
        _append_jsonl(session.engine_status_path, status)
        return session.engine_status_path

    def run_framework_replay(
        self,
        session_id: str,
        sleeve_id: str,
        *,
        universe: UniverseDefinition,
        framework_runner: FrameworkRunner,
        portfolio: Portfolio,
        indicator_engine: IndicatorEngine | None = None,
    ) -> FrameworkBacktestResult:
        return run_framework_replay(
            self.load_data_slices(session_id, sleeve_id),
            universe,
            sleeve_id=sleeve_id,
            framework_runner=framework_runner,
            portfolio=portfolio,
            indicator_engine=indicator_engine,
        )


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        handle.write("\n")


def _data_slice_to_dict(data: DataSlice) -> dict[str, Any]:
    return {
        "time": data.time.isoformat(),
        "bars": [_bar_to_dict(bar) for bar in sorted(data.bars.values(), key=lambda item: item.symbol.key)],
    }


def _data_slice_from_dict(payload: dict[str, Any]) -> DataSlice:
    bars = [_bar_from_dict(item) for item in payload.get("bars", [])]
    return DataSlice(
        time=datetime.fromisoformat(str(payload["time"])),
        bars={bar.symbol.key: bar for bar in bars},
    )


def _bar_to_dict(bar: Bar) -> dict[str, Any]:
    return {
        "symbol": {
            "ticker": bar.symbol.ticker,
            "market": bar.symbol.market,
        },
        "time": bar.time.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _bar_from_dict(payload: dict[str, Any]) -> Bar:
    symbol_payload = payload["symbol"]
    symbol = Symbol(ticker=str(symbol_payload["ticker"]), market=str(symbol_payload["market"]))
    return Bar(
        symbol=symbol,
        time=datetime.fromisoformat(str(payload["time"])),
        open=float(payload["open"]),
        high=float(payload["high"]),
        low=float(payload["low"]),
        close=float(payload["close"]),
        volume=int(payload.get("volume", 0) or 0),
    )
