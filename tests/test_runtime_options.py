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
        "journal_path": "data/cycle-journal/live-us-main.jsonl",
        "sleeves": [
            {
                "sleeve_id": "us-live",
                "workspace_path": "sleeves/us-live",
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
                    "account_store_path": "data/virtual-accounts/live-us-main.json",
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
    assert config.journal_path == Path("data/cycle-journal/live-us-main.jsonl")
    assert config.market_data.provider == "market-data-engine"
    assert sleeve.workspace_path == Path("sleeves/us-live")
    assert sleeve.cash == 100_000
    assert sleeve.universe.coarse_path == Path("configs/universes/us_live_smoke.json")
    assert sleeve.universe.fine.enabled is True
    assert sleeve.universe.active.max_symbols == 2
    assert sleeve.universe.active.selection_model == ModuleReference(
        "leaps_quant_engine.universe.selection:StaticUniverseSelectionModel"
    )
    assert sleeve.universe.active.selection_models == ()
    assert sleeve.alpha.modules == (ModuleReference("examples/alpha/price_above_sma_alpha.py"),)
    assert dict(sleeve.alpha.input_selections) == {}
    assert sleeve.portfolio.model == ModuleReference("examples/portfolio_models/equal_weight.py")
    assert dict(sleeve.portfolio.parameters) == {"max_portfolio_pct": 0.8}
    assert sleeve.portfolio.account_store_path == Path("data/virtual-accounts/live-us-main.json")
    assert sleeve.portfolio.rebalance.cash_reserve_pct == 0.1
    assert sleeve.portfolio.rebalance.min_order_notional == 1000
    assert sleeve.portfolio.rebalance.min_quantity_delta == 2
    assert sleeve.worker.cycle_interval_seconds == 60
    assert sleeve.indicators.min_ready_ratio == 0.9


def test_runtime_config_can_wire_selection_results_to_alpha_inputs():
    payload = _runtime_payload()
    payload["sleeves"][0]["universe"]["active"]["selection_models"] = [
        "leaps_quant_engine.universe.selection:StaticUniverseSelectionModel",
        "leaps_quant_engine.universe.selection:MomentumUniverseSelectionModel",
    ]
    payload["sleeves"][0]["alpha"]["input_selections"] = {
        "momentum-alpha": "static-top-n",
        "etf-rotation": "momentum-active-selection",
    }

    config = parse_runtime_config(payload)
    sleeve = config.sleeve("us-live")

    assert sleeve.universe.active.selection_models == (
        ModuleReference("leaps_quant_engine.universe.selection:StaticUniverseSelectionModel"),
        ModuleReference("leaps_quant_engine.universe.selection:MomentumUniverseSelectionModel"),
    )
    assert dict(sleeve.alpha.input_selections) == {
        "momentum-alpha": "static-top-n",
        "etf-rotation": "momentum-active-selection",
    }
    assert config.to_dict()["sleeves"][0]["alpha"]["input_selections"]["momentum-alpha"] == "static-top-n"


def test_runtime_config_routes_sleeves_to_broker_account_profiles():
    payload = _runtime_payload()
    payload["broker_accounts"] = [
        {
            "account_id": "kis-domestic",
            "market_scope": "domestic",
            "account_store_path": "data/virtual-accounts/kis-domestic.json",
            "order_store_path": "data/order-runtime/kis-domestic.jsonl",
        },
        {
            "account_id": "kis-overseas",
            "market_scope": "overseas",
            "account_store_path": "data/virtual-accounts/kis-overseas.json",
            "order_store_path": "data/order-runtime/kis-overseas.jsonl",
            "broker_gateway": "paper",
            "metadata": {"account_kind": "us"},
        },
    ]
    payload["sleeves"][0]["broker_account_id"] = "kis-overseas"
    payload["sleeves"][0]["broker_account_routes"] = {
        "domestic": "kis-domestic",
        "overseas": "kis-overseas",
    }

    config = parse_runtime_config(payload)
    account = config.broker_account("kis-overseas")

    assert config.sleeve("us-live").broker_account_id == "kis-overseas"
    assert dict(config.sleeve("us-live").broker_account_routes) == {
        "domestic": "kis-domestic",
        "overseas": "kis-overseas",
    }
    assert account.market_scope == "overseas"
    assert account.currency == "USD"
    assert account.account_store_path == Path("data/virtual-accounts/kis-overseas.json")
    assert account.order_store_path == Path("data/order-runtime/kis-overseas.jsonl")
    assert account.broker_gateway == "paper"
    assert dict(account.metadata) == {"account_kind": "us"}
    assert config.to_dict()["broker_accounts"][1]["market_scope"] == "overseas"
    assert config.to_dict()["broker_accounts"][1]["currency"] == "USD"
    assert config.to_dict()["journal_path"] == "data/cycle-journal/live-us-main.jsonl"


def test_runtime_config_rejects_unknown_sleeve_broker_account():
    payload = _runtime_payload()
    payload["broker_accounts"] = [
        {
            "account_id": "kis-domestic",
            "market_scope": "domestic",
            "account_store_path": "data/virtual-accounts/kis-domestic.json",
        }
    ]
    payload["sleeves"][0]["broker_account_id"] = "kis-overseas"

    with pytest.raises(ConfigurationValidationError):
        parse_runtime_config(payload)


def test_runtime_config_rejects_unknown_market_route_broker_account():
    payload = _runtime_payload()
    payload["broker_accounts"] = [
        {
            "account_id": "kis-domestic",
            "market_scope": "domestic",
            "account_store_path": "data/virtual-accounts/kis-domestic.json",
        }
    ]
    payload["sleeves"][0]["broker_account_routes"] = {"overseas": "kis-overseas"}

    with pytest.raises(ConfigurationValidationError):
        parse_runtime_config(payload)


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


def test_us_etf_rotation_sample_config_is_usd_etf_only():
    root = Path(__file__).resolve().parents[1]
    snapshot = load_runtime_config_snapshot(root / "configs" / "runtime" / "us_etf_rotation_sleeve.json")
    sleeve = snapshot.config.sleeve("us_etf_rotation")
    universe_payload = json.loads((root / sleeve.universe.coarse_path).read_text(encoding="utf-8"))

    assert snapshot.config.runtime_id == "us_etf_rotation"
    assert sleeve.workspace_path == Path("sleeves/us_etf_rotation")
    assert sleeve.broker_account_id == "kis-overseas"
    assert dict(sleeve.cash_by_currency) == {}
    assert sleeve.universe.coarse_path == Path("configs/universes/us_etf_rotation_core.json")
    assert [module.ref for module in sleeve.alpha.modules] == [
        "alphas/etf_rotation.py",
        "alphas/volatility_trailing_stop.py",
    ]
    assert sleeve.portfolio.model == ModuleReference("portfolios/rl_ppo_constructor.py")
    assert dict(sleeve.portfolio.parameters)["allocation_mode"] == "rl_weights"
    assert dict(sleeve.portfolio.parameters)["policy_path"] is None
    assert dict(sleeve.portfolio.parameters)["top_k"] == 8
    assert dict(sleeve.alpha.input_selections)["us_etf_rotation"] == "us_etf_rotation"
    assert universe_payload["id"] == "us-etf-rotation-core"
    assert all(symbol["asset_type"] == "etf" and symbol["is_etf"] is True for symbol in universe_payload["symbols"])


def test_control_queue_drains_commands_in_order():
    queue = RuntimeControlQueue()
    first = queue.submit(RuntimeControlCommand.reload_config("configs/runtime/live_us.json"))
    second = queue.submit(RuntimeControlCommand.reload_sleeve("configs/runtime/live_us.json", "LEaps"))
    third = queue.submit(RuntimeControlCommand.pause_worker())

    assert queue.drain() == (first, second, third)
    assert queue.drain() == ()
    assert second.sleeve_id() == "LEaps"


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


def test_runtime_config_controller_loads_on_reload_sleeve_command(tmp_path):
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
    queue.submit(RuntimeControlCommand.reload_sleeve(tmp_path / "updated.json", "us-live"))
    report = controller.apply_pending()

    assert calls == [tmp_path / "updated.json"]
    assert report.applied_commands[0].command_type == RuntimeControlCommandType.RELOAD_SLEEVE
    assert report.current_version == "sha256:updated"
