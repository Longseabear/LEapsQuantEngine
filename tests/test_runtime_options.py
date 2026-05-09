import json
from pathlib import Path

import pytest

from leaps_quant_engine.control import (
    RuntimeControlCommand,
    RuntimeControlCommandType,
    RuntimeControlQueue,
    RuntimeConfigController,
)
from leaps_quant_engine.runtime_config import (
    ConfigurationValidationError,
    ModuleReference,
    RuntimeConfigSnapshot,
    load_runtime_config_snapshot,
    parse_runtime_config,
)


def _runtime_payload():
    return {
        "runtime_id": "live-us-main",
        "mode": "live",
        "timezone": "Asia/Seoul",
        "market_data": {
            "provider": "market-data-engine",
            "history_provider": "kis-cache",
            "rate_limit_per_second": 20,
        },
        "sleeves": [
            {
                "sleeve_id": "us-live",
                "cash": 100_000,
                "universe": {
                    "coarse_path": "configs/universes/us_live_smoke.json",
                    "fine": {
                        "enabled": True,
                        "refresh_seconds": 300,
                        "max_age_seconds": 300,
                    },
                    "active": {
                        "max_symbols": 2,
                        "selection_model": "leaps_quant_engine.universe.selection:StaticUniverseSelectionModel",
                    },
                },
                "indicators": {
                    "warmup_enabled": True,
                    "extra_bars": 1,
                    "min_ready_ratio": 0.9,
                },
                "alpha": {
                    "modules": [
                        {
                            "ref": "examples/alpha/price_above_sma_alpha.py",
                            "enabled": True,
                        }
                    ]
                },
                "portfolio": {
                    "model": "examples/portfolio_models/equal_weight.py",
                    "parameters": {"max_portfolio_pct": 0.8},
                    "rebalance": {
                        "cash_reserve_pct": 0.1,
                        "min_order_notional": 1000,
                        "min_quantity_delta": 2,
                    },
                },
                "worker": {
                    "cycle_interval_seconds": 60,
                    "min_success": 2,
                },
            }
        ],
    }


def test_runtime_config_keeps_logic_as_module_references():
    config = parse_runtime_config(_runtime_payload())
    sleeve = config.sleeve("us-live")

    assert config.runtime_id == "live-us-main"
    assert config.market_data.provider == "market-data-engine"
    assert sleeve.cash == 100_000
    assert sleeve.universe.coarse_path == Path("configs/universes/us_live_smoke.json")
    assert sleeve.universe.fine.enabled is True
    assert sleeve.universe.active.max_symbols == 2
    assert sleeve.universe.active.selection_model == ModuleReference(
        "leaps_quant_engine.universe.selection:StaticUniverseSelectionModel"
    )
    assert sleeve.alpha.modules == (ModuleReference("examples/alpha/price_above_sma_alpha.py"),)
    assert sleeve.portfolio.model == ModuleReference("examples/portfolio_models/equal_weight.py")
    assert dict(sleeve.portfolio.parameters) == {"max_portfolio_pct": 0.8}
    assert sleeve.portfolio.rebalance.cash_reserve_pct == 0.1
    assert sleeve.portfolio.rebalance.min_order_notional == 1000
    assert sleeve.portfolio.rebalance.min_quantity_delta == 2
    assert sleeve.worker.cycle_interval_seconds == 60
    assert sleeve.indicators.min_ready_ratio == 0.9


def test_runtime_config_validation_rejects_invalid_operational_settings():
    payload = _runtime_payload()
    payload["market_data"]["rate_limit_per_second"] = 0

    with pytest.raises(ConfigurationValidationError):
        parse_runtime_config(payload)


def test_runtime_config_validation_rejects_negative_sleeve_cash():
    payload = _runtime_payload()
    payload["sleeves"][0]["cash"] = -1

    with pytest.raises(ConfigurationValidationError):
        parse_runtime_config(payload)


def test_runtime_config_validation_rejects_invalid_portfolio_rebalance():
    payload = _runtime_payload()
    payload["sleeves"][0]["portfolio"]["rebalance"]["cash_reserve_pct"] = 1.0

    with pytest.raises(ConfigurationValidationError):
        parse_runtime_config(payload)


def test_runtime_config_snapshot_hashes_loaded_file(tmp_path):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(json.dumps(_runtime_payload()), encoding="utf-8")

    snapshot = load_runtime_config_snapshot(config_path)

    assert snapshot.source_path == config_path
    assert snapshot.version.startswith("sha256:")
    assert snapshot.config.sleeve("us-live").worker.min_success == 2
    assert snapshot.to_dict()["config"]["runtime_id"] == "live-us-main"


def test_control_queue_drains_commands_in_order():
    queue = RuntimeControlQueue()
    first = queue.submit(RuntimeControlCommand.reload_config("configs/runtime/live_us.json"))
    second = queue.submit(RuntimeControlCommand.pause_worker())

    assert queue.drain() == (first, second)
    assert queue.drain() == ()


def test_runtime_config_controller_loads_only_on_reload_command(tmp_path):
    initial = RuntimeConfigSnapshot(
        config=parse_runtime_config(_runtime_payload()),
        source_path=tmp_path / "initial.json",
        version="sha256:initial",
        loaded_at="2026-05-09T09:00:00",
    )
    loaded = RuntimeConfigSnapshot(
        config=parse_runtime_config({**_runtime_payload(), "runtime_id": "live-us-updated"}),
        source_path=tmp_path / "updated.json",
        version="sha256:updated",
        loaded_at="2026-05-09T09:01:00",
    )
    calls = []

    def loader(path):
        calls.append(path)
        return loaded

    queue = RuntimeControlQueue()
    controller = RuntimeConfigController(snapshot=initial, queue=queue, loader=loader)

    idle_report = controller.apply_pending()
    assert idle_report.applied_commands == ()
    assert calls == []
    assert controller.snapshot is initial

    queue.submit(RuntimeControlCommand.reload_config(tmp_path / "updated.json"))
    report = controller.apply_pending()

    assert calls == [tmp_path / "updated.json"]
    assert report.applied_commands[0].command_type == RuntimeControlCommandType.RELOAD_CONFIG
    assert report.previous_version == "sha256:initial"
    assert report.current_version == "sha256:updated"
    assert controller.snapshot is loaded
