from __future__ import annotations

from datetime import datetime, timedelta
import importlib.util
import json
from pathlib import Path
import sys

from leaps_quant_engine.agent_targets import load_agent_target_artifact, resolve_agent_target_path
from leaps_quant_engine.framework import PortfolioConstructionContext
from leaps_quant_engine.models import Bar, DataSlice, Symbol
from leaps_quant_engine.portfolio import Holding, Portfolio
from leaps_quant_engine.runtime_state import RuntimeModelStateView
from leaps_quant_engine.universe.definition import UniverseDefinition
from leaps_quant_engine.universe.selection import UniverseSelectionContext


ROOT = Path(__file__).resolve().parents[1]


def test_agent_target_artifact_loader_validates_sleeve_expiry_and_market(tmp_path):
    now = datetime(2026, 5, 22, 8, 50)
    path = tmp_path / "target.json"
    _write_target(
        path,
        now=now,
        targets=[
            {"symbol": "KRX:005930", "target_percent": 0.30},
            {"symbol": "NAS:AAPL", "target_percent": 0.30},
            {"symbol": "KRX:000660", "target_percent": 30},
        ],
    )

    result = load_agent_target_artifact(path, as_of=now, sleeve_id="LEaps")

    assert result.status == "loaded"
    assert result.artifact is not None
    assert [target.symbol.key for target in result.artifact.targets] == ["KRX:005930", "KRX:000660"]
    assert [target.target_percent for target in result.artifact.targets] == [0.30, 0.30]

    stale = load_agent_target_artifact(path, as_of=now + timedelta(days=3), sleeve_id="LEaps")
    assert stale.status == "target_artifact_expired"


def test_agent_target_artifact_loader_resolves_point_in_time_template(tmp_path):
    now = datetime(2026, 5, 15, 8, 50)
    target_dir = tmp_path / "targets"
    target_dir.mkdir()
    expected = target_dir / "2026-05-15.json"
    _write_target(expected, now=now, targets=[{"symbol": "KRX:005930", "target_percent": 0.40}])

    resolved = resolve_agent_target_path(target_dir / "{date}.json", as_of=now)
    result = load_agent_target_artifact(target_dir / "{date}.json", as_of=now, sleeve_id="LEaps")

    assert resolved == expected
    assert result.status == "loaded"
    assert result.artifact is not None
    assert result.artifact.targets[0].symbol.key == "KRX:005930"


def test_agent_target_artifact_loader_resolves_directory_by_replay_date(tmp_path):
    now = datetime(2026, 5, 15, 8, 50)
    _write_target(tmp_path / "20260515.json", now=now, targets=[{"symbol": "KRX:000660", "target_percent": 0.40}])

    result = load_agent_target_artifact(tmp_path, as_of=now, sleeve_id="LEaps")

    assert result.status == "loaded"
    assert result.artifact is not None
    assert result.artifact.path.name == "20260515.json"


def test_agent_daily_target_selection_reads_daily_artifact(tmp_path):
    module = _load("sleeves/LEaps/selections/agent_daily_target.py")
    now = datetime(2026, 5, 22, 8, 45)
    path = tmp_path / "target.json"
    _write_target(
        path,
        now=now,
        targets=[
            {"symbol": "KRX:005930", "target_percent": 0.40, "name": "Samsung", "reason": "leader"},
            {"symbol": "KRX:999999", "target_percent": 0.10, "reason": "not in coarse"},
        ],
    )
    samsung = Symbol("005930", "KRX")
    hynix = Symbol("000660", "KRX")
    universe = UniverseDefinition(id="test", market="KRX", symbols=(samsung, hynix), indicators=())
    context = UniverseSelectionContext(sleeve_id="LEaps", universe=universe, as_of=now)

    result = module.AgentDailyTargetSelectionModel(target_path=str(path)).select(context)

    assert [symbol.key for symbol in result.selected_symbols] == ["KRX:005930"]
    assert result.candidates["KRX:005930"].metadata["target_reason"] == "leader"
    assert result.rejected["KRX:999999"] == ("target_symbol_not_in_coarse_universe",)


def test_agent_daily_target_portfolio_emits_targets_and_missing_held_zero(tmp_path):
    module = _load("sleeves/LEaps/portfolios/agent_daily_target.py")
    now = datetime(2026, 5, 22, 8, 50)
    path = tmp_path / "target.json"
    _write_target(
        path,
        now=now,
        max_gross_exposure=0.50,
        targets=[
            {"symbol": "KRX:005930", "target_percent": 0.40, "reason": "core", "confidence": 0.8},
            {"symbol": "KRX:000660", "target_percent": 0.40, "reason": "leader"},
        ],
    )
    samsung = Symbol("005930", "KRX")
    hynix = Symbol("000660", "KRX")
    old = Symbol("011070", "KRX")
    context = _portfolio_context(now, (samsung, hynix, old), held_symbol=old)

    model = module.AgentDailyTargetPortfolioModel(
        target_path=str(path),
        max_gross_exposure=0.50,
        max_position_pct=0.35,
    )
    targets = model.create_targets(context)
    by_key = {target.symbol.key: target for target in targets}

    assert round(by_key["KRX:005930"].target_percent, 4) == 0.25
    assert round(by_key["KRX:000660"].target_percent, 4) == 0.25
    assert by_key["KRX:011070"].target_percent == 0.0
    patches = model.state_patches(context, targets)
    assert patches[0].key.model_id == "leaps-agent-daily-target-portfolio"
    assert patches[0].value["status"] == "loaded"
    assert patches[0].value["emitted_target_count"] == 3


def test_agent_daily_target_portfolio_stale_file_fails_closed(tmp_path):
    module = _load("sleeves/LEaps/portfolios/agent_daily_target.py")
    now = datetime(2026, 5, 22, 8, 50)
    path = tmp_path / "target.json"
    _write_target(path, now=now - timedelta(days=3), targets=[{"symbol": "KRX:005930", "target_percent": 0.40}])
    context = _portfolio_context(now, (Symbol("005930", "KRX"),))

    model = module.AgentDailyTargetPortfolioModel(target_path=str(path), max_age_hours=24.0)

    assert model.create_targets(context) == ()


def _write_target(
    path: Path,
    *,
    now: datetime,
    targets: list[dict],
    max_gross_exposure: float | None = None,
) -> None:
    payload = {
        "sleeve_id": "LEaps",
        "target_id": "test-target",
        "generated_at": now.isoformat(),
        "expires_at": (now + timedelta(days=1)).isoformat(),
        "targets": targets,
    }
    if max_gross_exposure is not None:
        payload["max_gross_exposure"] = max_gross_exposure
    path.write_text(json.dumps(payload), encoding="utf-8")


def _portfolio_context(
    now: datetime,
    symbols: tuple[Symbol, ...],
    *,
    held_symbol: Symbol | None = None,
) -> PortfolioConstructionContext:
    holdings = {}
    if held_symbol is not None:
        holdings[held_symbol.key] = Holding(symbol=held_symbol, quantity=2, average_price=100_000)
    data = DataSlice(
        time=now,
        bars={
            symbol.key: Bar(symbol=symbol, time=now, open=100_000, high=100_000, low=100_000, close=100_000)
            for symbol in symbols
        },
    )
    return PortfolioConstructionContext(
        sleeve_id="LEaps",
        data=data,
        portfolio=Portfolio(cash=1_000_000, cash_by_currency={"KRW": 1_000_000}, holdings=holdings),
        active_insights=(),
        managed_symbols=symbols,
        model_state=RuntimeModelStateView(),
    )


def _load(relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
