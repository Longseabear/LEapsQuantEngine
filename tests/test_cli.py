import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import leaps_quant_engine.cli as cli
from leaps_quant_engine.cli import main
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import OrderSide
from leaps_quant_engine.models import OrderIntent
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.orders import OrderCoordinator, OrderEventType
from leaps_quant_engine.virtual_account import VirtualFillEvent, VirtualSleeveAccountStore


def test_cli_run_once_outputs_order_intents(capsys):
    exit_code = main(["run-once", "sample_swing_kor_pipeline.json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {
            "sleeve_id": "swing-kor",
            "symbol": "005930",
            "market": "KRX",
            "side": "buy",
            "quantity": 10,
            "reference_price": 70000.0,
            "notional": 700000.0,
            "tag": "buy-and-hold",
        }
    ]


def test_cli_runtime_config_validate_outputs_snapshot(monkeypatch, capsys):
    captured = {}

    def fake_load_runtime_config_snapshot(path):
        captured["path"] = path
        return SimpleNamespace(
            to_dict=lambda: {
                "version": "sha256:test",
                "config": {"runtime_id": "live-us-main"},
            }
        )

    monkeypatch.setattr(cli, "load_runtime_config_snapshot", fake_load_runtime_config_snapshot)

    exit_code = main(["runtime-config-validate", "configs/runtime/live_us_smoke.json"])

    assert exit_code == 0
    assert captured["path"] == Path("configs/runtime/live_us_smoke.json")
    assert json.loads(capsys.readouterr().out) == {
        "version": "sha256:test",
        "config": {"runtime_id": "live-us-main"},
    }


def test_cli_daily_backtest_provider_prefers_finance_datareader(monkeypatch):
    class FakeFinanceProvider:
        pass

    class FakeKISProvider:
        @classmethod
        def from_env(cls):
            return cls()

    monkeypatch.setattr(cli, "FinanceDataReaderMarketDataProvider", FakeFinanceProvider)
    monkeypatch.setattr(cli, "KISCachedMarketDataProvider", FakeKISProvider)

    assert isinstance(cli._daily_backtest_provider("finance-datareader"), FakeFinanceProvider)
    assert isinstance(cli._daily_backtest_provider("kis-cache"), FakeKISProvider)


def test_cli_runtime_run_once_bootstraps_configured_runtime(monkeypatch, capsys):
    captured = {}
    snapshot = SimpleNamespace(
        source_path=Path("configs/runtime/live_us_smoke.json"),
        config=SimpleNamespace(
            sleeves=(SimpleNamespace(universe=SimpleNamespace(coarse_path="universe.json")),),
            sleeve=lambda sleeve_id: SimpleNamespace(universe=SimpleNamespace(coarse_path="universe.json")),
        )
    )

    class FakeRuntime:
        def run_once(self, warmup=None):
            captured["warmup"] = warmup
            return SimpleNamespace(
                to_dict=lambda include_candidates=True,
                include_warmup_symbols=True,
                include_failures=True,
                include_framework_details=True: {
                    "runtime_id": "live-us-main",
                    "include_candidates": include_candidates,
                    "include_warmup_symbols": include_warmup_symbols,
                    "include_failures": include_failures,
                    "include_framework_details": include_framework_details,
                }
            )

    monkeypatch.setattr(cli, "load_runtime_config_snapshot", lambda path: snapshot)
    monkeypatch.setattr(
        cli,
        "load_universe_definition",
        lambda path: SimpleNamespace(market="US"),
    )

    def fake_bootstrap_sleeve_runtime(snapshot_arg, sleeve_id=None, **kwargs):
        captured["snapshot"] = snapshot_arg
        captured["sleeve_id"] = sleeve_id
        captured["kwargs"] = kwargs
        return FakeRuntime()

    monkeypatch.setattr(cli, "bootstrap_sleeve_runtime", fake_bootstrap_sleeve_runtime)

    exit_code = main(
        [
            "runtime-run-once",
            "configs/runtime/live_us_smoke.json",
            "--sleeve-id",
            "us-live",
            "--held",
            "IBM",
            "--skip-fine-refresh",
            "--skip-warmup",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    assert captured["snapshot"] is snapshot
    assert captured["sleeve_id"] == "us-live"
    assert captured["kwargs"]["refresh_fine"] is False
    assert captured["kwargs"]["held_symbols"] == (Symbol("IBM", "US"),)
    assert captured["warmup"] is False
    assert json.loads(capsys.readouterr().out) == {
        "runtime_id": "live-us-main",
        "include_candidates": False,
        "include_warmup_symbols": False,
        "include_failures": False,
        "include_framework_details": False,
    }


def test_cli_runtime_run_once_can_write_order_batch_artifact(monkeypatch, tmp_path, capsys):
    snapshot = SimpleNamespace(
        source_path=tmp_path / "runtime.json",
        config=SimpleNamespace(
            sleeves=(SimpleNamespace(universe=SimpleNamespace(coarse_path="universe.json")),),
            sleeve=lambda sleeve_id: SimpleNamespace(universe=SimpleNamespace(coarse_path="universe.json")),
        )
    )
    batch = OrderIntentBatch(
        sleeve_id="LEaps",
        generated_at=cli.datetime(2026, 5, 10, 9, 30),
        order_intents=(
            OrderIntent(
                sleeve_id="LEaps",
                symbol=Symbol("005930", "KRX"),
                side=OrderSide.BUY,
                quantity=2,
                reference_price=100,
                tag="artifact",
            ),
        ),
        batch_id="batch-1",
    )

    class FakeRuntime:
        def run_once(self, warmup=None):
            return SimpleNamespace(
                runtime_id="live-us-main",
                config_version="sha256:test",
                framework=SimpleNamespace(execution_batch=batch),
                to_dict=lambda **kwargs: {"runtime_id": "live-us-main"},
            )

    monkeypatch.setattr(cli, "load_runtime_config_snapshot", lambda path: snapshot)
    monkeypatch.setattr(cli, "load_universe_definition", lambda path: SimpleNamespace(market="KRX"))
    monkeypatch.setattr(cli, "bootstrap_sleeve_runtime", lambda *args, **kwargs: FakeRuntime())

    artifact_path = tmp_path / "order_batches.json"
    exit_code = main(
        [
            "runtime-run-once",
            str(snapshot.source_path),
            "--order-batch-output",
            str(artifact_path),
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["order_batch_artifact"]["path"] == str(artifact_path)
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["schema_version"] == "order_intent_batches.v1"
    assert artifact["batches"][0]["batch_id"] == "batch-1"
    assert artifact["batches"][0]["orders"][0]["symbol"] == "KRX:005930"


def test_cli_manages_sleeve_alpha_modules(tmp_path, capsys):
    workspace = tmp_path / "sleeves" / "LEaps"
    alpha_dir = workspace / "alphas"
    alpha_dir.mkdir(parents=True)
    (alpha_dir / "momentum.py").write_text("# alpha\n", encoding="utf-8")
    (alpha_dir / "etf_rotation.py").write_text("# alpha\n", encoding="utf-8")
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "workspace_path": str(workspace),
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "alpha": {"modules": [{"ref": "alphas/momentum.py", "enabled": True}]},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert main(["sleeve-alpha-list", str(config_path), "--sleeve-id", "LEaps"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["available_alpha_modules"] == ["alphas/etf_rotation.py", "alphas/momentum.py"]
    assert listed["active_alpha_modules"] == ["alphas/momentum.py"]
    assert listed["reload_command"]["command_type"] == "reload_sleeve"
    assert listed["reload_command"]["payload"]["sleeve_id"] == "LEaps"

    assert main(["sleeve-alpha-enable", str(config_path), "etf_rotation", "--sleeve-id", "LEaps"]) == 0
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["active_alpha_modules"] == ["alphas/momentum.py", "alphas/etf_rotation.py"]

    assert main(["sleeve-alpha-disable", str(config_path), "momentum", "--sleeve-id", "LEaps"]) == 0
    disabled = json.loads(capsys.readouterr().out)
    assert disabled["active_alpha_modules"] == ["alphas/etf_rotation.py"]


def test_cli_manages_sleeve_portfolio_model(tmp_path, capsys):
    workspace = tmp_path / "sleeves" / "LEaps"
    portfolio_dir = workspace / "portfolios"
    portfolio_dir.mkdir(parents=True)
    (portfolio_dir / "equal_weight.py").write_text("# portfolio\n", encoding="utf-8")
    (portfolio_dir / "confidence_weight.py").write_text("# portfolio\n", encoding="utf-8")
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "workspace_path": str(workspace),
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"model": "portfolios/equal_weight.py"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert main(["sleeve-portfolio-list", str(config_path), "--sleeve-id", "LEaps"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["available_portfolio_models"] == [
        "portfolios/confidence_weight.py",
        "portfolios/equal_weight.py",
    ]
    assert listed["active_portfolio_model"] == "portfolios/equal_weight.py"
    assert listed["reload_command"]["command_type"] == "reload_sleeve"

    assert main(["sleeve-portfolio-set", str(config_path), "confidence_weight", "--sleeve-id", "LEaps"]) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["active_portfolio_model"] == "portfolios/confidence_weight.py"
    assert updated["inactive_portfolio_models"] == ["portfolios/equal_weight.py"]


def test_cli_kis_account_sync_uses_configured_virtual_account_store(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class FakeSync:
        @classmethod
        def from_env(cls):
            return cls()

        def sync(self, store, **kwargs):
            assert store.path == tmp_path / "accounts" / "leaps.json"
            assert kwargs["assign_unknown_to_sleeve_id"] == "LEaps"
            return SimpleNamespace(
                to_dict=lambda: {
                    "imported_fill_count": 1,
                    "synced_sleeves": {"LEaps": {"cash": 1000}},
                }
            )

    monkeypatch.setattr(cli, "KISVirtualAccountSync", FakeSync)

    exit_code = main(
        [
            "kis-account-sync",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--start-date",
            "20260508",
            "--end-date",
            "20260508",
            "--assign-unknown-to-sleeve",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["imported_fill_count"] == 1
    assert payload["account_store_path"] == str(tmp_path / "accounts" / "leaps.json")


def test_cli_virtual_account_allocate_fill_updates_sleeve_projection(tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = VirtualSleeveAccountStore(tmp_path / "accounts" / "leaps.json", default_cash_by_sleeve={"LEaps": 1000})
    store.record_broker_fill(
        VirtualFillEvent(
            fill_id="fill-1",
            order_id="order-1",
            symbol=Symbol("005930", "KRX"),
            side=OrderSide.BUY,
            quantity=2,
            fill_price=100,
            filled_at=cli.datetime(2026, 5, 9, 9, 1),
        )
    )

    exit_code = main(
        [
            "virtual-account-allocate-fill",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--fill-id",
            "fill-1",
            "--allocation",
            "LEaps=2",
            "--reason",
            "operator-test",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["allocations"][0]["sleeve_id"] == "LEaps"
    assert payload["synced_sleeves"]["LEaps"]["holdings"][0]["quantity"] == 2


def test_cli_virtual_account_reconcile_reports_broker_vs_virtual(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = VirtualSleeveAccountStore(tmp_path / "accounts" / "leaps.json", default_cash_by_sleeve={"LEaps": 1000})
    store.apply_fill(
        VirtualFillEvent(
            fill_id="fill-1",
            order_id="order-1",
            symbol=Symbol("005930", "KRX"),
            side=OrderSide.BUY,
            quantity=2,
            fill_price=100,
            sleeve_id="LEaps",
            filled_at=cli.datetime(2026, 5, 9, 9, 1),
        )
    )

    class FakeAccountClient:
        def get_holdings(self, *, market="domestic"):
            return {"holdings": [{"symbol": "005930", "holding_quantity": 3}]}

    class FakeSync:
        @classmethod
        def from_env(cls):
            return SimpleNamespace(account_client=FakeAccountClient())

    monkeypatch.setattr(cli, "KISVirtualAccountSync", FakeSync)

    exit_code = main(
        [
            "virtual-account-reconcile",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "needs_reconciliation"
    assert payload["rows"][0]["broker_quantity"] == 3
    assert payload["rows"][0]["virtual_quantity"] == 2


def test_cli_virtual_account_transfer_cash_moves_between_sleeves(tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 0,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    },
                    {
                        "sleeve_id": "default sleeve",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts" / "leaps.json",
        default_cash_by_sleeve={"LEaps": 0, "default sleeve": 1000},
    )
    store.current_portfolio("default sleeve")

    exit_code = main(
        [
            "virtual-account-transfer-cash",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--from-sleeve-id",
            "default sleeve",
            "--to-sleeve-id",
            "LEaps",
            "--amount",
            "250",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["from_portfolio"]["cash"] == 750
    assert payload["to_portfolio"]["cash"] == 250


def test_cli_virtual_account_sync_cash_uses_kis_balance(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 100,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    },
                    {
                        "sleeve_id": "default sleeve",
                        "cash": 0,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    store = VirtualSleeveAccountStore(
        tmp_path / "accounts" / "leaps.json",
        default_cash_by_sleeve={"LEaps": 100, "default sleeve": 0},
    )
    store.current_portfolio("LEaps")

    class FakeAccountClient:
        def get_balance_summary(self):
            return {"cash_balance": 1000}

    class FakeSync:
        @classmethod
        def from_env(cls):
            return SimpleNamespace(account_client=FakeAccountClient())

    monkeypatch.setattr(cli, "KISVirtualAccountSync", FakeSync)

    exit_code = main(
        [
            "virtual-account-sync-cash",
            str(config_path),
            "--sleeve-id",
            "LEaps",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "matched"
    assert payload["residual_cash"] == 900


def test_cli_order_runtime_status_reports_configured_stores(tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    account_store = VirtualSleeveAccountStore(
        tmp_path / "accounts" / "leaps.json",
        default_cash_by_sleeve={"LEaps": 1000},
    )
    account_store.current_portfolio("LEaps")
    order_store = FileOrderRuntimeStateStore(tmp_path / "order-runtime" / "leaps.jsonl")
    coordination = OrderCoordinator().coordinate(
        (
            OrderIntentBatch(
                sleeve_id="LEaps",
                generated_at=cli.datetime(2026, 5, 9, 9, 30),
                order_intents=(
                    OrderIntent(
                        sleeve_id="LEaps",
                        symbol=Symbol("005930", "KRX"),
                        side=OrderSide.SELL,
                        quantity=1,
                        reference_price=70_000,
                        tag="cli-status",
                    ),
                ),
                batch_id="batch-1",
            ),
        ),
        generated_at=cli.datetime(2026, 5, 9, 9, 31),
    )
    accepted = coordination.tickets[0].event(
        OrderEventType.ACCEPTED,
        occurred_at=cli.datetime(2026, 5, 9, 9, 32),
        broker_order_id="broker-1",
    )
    order_store.record_tickets(coordination.tickets)
    order_store.record_events(coordination.events)
    order_store.record_event(accepted)

    exit_code = main(
        [
            "order-runtime-status",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime_id"] == "test"
    assert payload["order_store_path"] == str(tmp_path / "order-runtime" / "leaps.jsonl")
    assert payload["account_store_path"] == str(tmp_path / "accounts" / "leaps.json")
    assert payload["order_runtime"]["open_ticket_count"] == 1
    assert payload["order_runtime"]["ticket_status_counts"] == {"accepted": 1}
    assert payload["sleeves"][0]["pending_sell_quantities"] == {"KRX:005930": 1}


def test_cli_order_runtime_status_uses_sleeve_broker_account_route(tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "broker_accounts": [
                    {
                        "account_id": "kis-domestic",
                        "market_scope": "domestic",
                        "account_store_path": "accounts/kr.json",
                        "order_store_path": "orders/kr.jsonl",
                    },
                    {
                        "account_id": "kis-overseas",
                        "market_scope": "overseas",
                        "account_store_path": "accounts/us.json",
                        "order_store_path": "orders/us.jsonl",
                    },
                ],
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "broker_account_id": "kis-overseas",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/legacy.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    VirtualSleeveAccountStore(
        tmp_path / "accounts" / "us.json",
        default_cash_by_sleeve={"LEaps": 1000},
    ).current_portfolio("LEaps")

    exit_code = main(
        [
            "order-runtime-status",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["broker_account_id"] == "kis-overseas"
    assert payload["market_scope"] == "overseas"
    assert payload["account_store_path"] == str(tmp_path / "accounts" / "us.json")
    assert payload["order_store_path"] == str(tmp_path / "orders" / "us.jsonl")


def test_cli_order_runtime_status_rejects_mismatched_explicit_broker_account(tmp_path):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "broker_accounts": [
                    {
                        "account_id": "kis-domestic",
                        "market_scope": "domestic",
                        "account_store_path": "accounts/kr.json",
                    },
                    {
                        "account_id": "kis-overseas",
                        "market_scope": "overseas",
                        "account_store_path": "accounts/us.json",
                    },
                ],
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "broker_account_id": "kis-overseas",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="routes to broker_account_id"):
        main(
            [
                "order-runtime-status",
                str(config_path),
                "--sleeve-id",
                "LEaps",
                "--account-id",
                "kis-domestic",
                "--summary-only",
            ]
        )


def test_cli_order_runtime_supervise_blocks_overseas_broker_engine_side_effects(monkeypatch, tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "broker_accounts": [
                    {
                        "account_id": "kis-overseas",
                        "market_scope": "overseas",
                        "account_store_path": "accounts/us.json",
                        "order_store_path": "orders/us.jsonl",
                    }
                ],
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "broker_account_id": "kis-overseas",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    VirtualSleeveAccountStore(
        tmp_path / "accounts" / "us.json",
        default_cash_by_sleeve={"LEaps": 1000},
    ).current_portfolio("LEaps")

    class FailIfCalledAccountClient:
        @classmethod
        def from_env(cls):
            raise AssertionError("domestic KIS account client must not be created for overseas account routes")

    monkeypatch.setattr(cli, "KISAccountClient", FailIfCalledAccountClient)

    exit_code = main(
        [
            "order-runtime-supervise",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--broker",
            "broker-engine",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "warnings"
    assert "broker_engine_overseas_poll_not_supported" in payload["errors"]
    assert "broker_engine_overseas_reconcile_not_supported" in payload["errors"]
    assert payload["final_status"]["broker_account_id"] == "kis-overseas"
    assert payload["final_status"]["market_scope"] == "overseas"


def test_cli_order_runtime_supervise_polls_open_tickets_with_paper_broker(tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    VirtualSleeveAccountStore(
        tmp_path / "accounts" / "leaps.json",
        default_cash_by_sleeve={"LEaps": 1000},
    ).current_portfolio("LEaps")
    order_store = FileOrderRuntimeStateStore(tmp_path / "order-runtime" / "leaps.jsonl")
    coordination = OrderCoordinator().coordinate(
        (
            OrderIntentBatch(
                sleeve_id="LEaps",
                generated_at=cli.datetime(2026, 5, 10, 9, 30),
                order_intents=(
                    OrderIntent(
                        sleeve_id="LEaps",
                        symbol=Symbol("005930", "KRX"),
                        side=OrderSide.BUY,
                        quantity=1,
                        reference_price=100,
                        tag="cli-supervise",
                    ),
                ),
                batch_id="batch-1",
            ),
        ),
        generated_at=cli.datetime(2026, 5, 10, 9, 31),
    )
    submitted = coordination.tickets[0].event(
        OrderEventType.SUBMITTED,
        occurred_at=cli.datetime(2026, 5, 10, 9, 32),
        broker_order_id="paper:ticket",
    )
    order_store.record_tickets(coordination.tickets)
    order_store.record_events(coordination.events)
    order_store.record_event(submitted)

    exit_code = main(
        [
            "order-runtime-supervise",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--broker",
            "paper",
            "--skip-reconcile",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["poll_fill_event_count"] == 1
    assert payload["final_status"]["order_runtime"]["open_ticket_count"] == 0
    assert payload["final_status"]["sleeves"][0]["portfolio"]["holdings"][0]["quantity"] == 1
    assert payload["final_status"]["sleeves"][0]["portfolio"]["cash"] == 900


def test_cli_order_runtime_submit_commits_paper_order_batches(tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    batch_path = tmp_path / "order_batches.json"
    batch_path.write_text(
        json.dumps(
            {
                "batch_id": "batch-1",
                "sleeve_id": "LEaps",
                "generated_at": "2026-05-10T09:30:00",
                "orders": [
                    {
                        "symbol": "KRX:005930",
                        "side": "buy",
                        "quantity": 2,
                        "reference_price": 100,
                        "tag": "cli-submit",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "order-runtime-submit",
            str(config_path),
            str(batch_path),
            "--sleeve-id",
            "LEaps",
            "--commit",
            "--broker",
            "paper",
            "--poll-after-submit",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "submitted"
    assert payload["order_count"] == 1
    assert payload["orchestration"]["fill_event_count"] == 1
    assert payload["final_status"]["order_runtime"]["terminal_ticket_count"] == 1
    assert payload["final_status"]["sleeves"][0]["portfolio"]["cash"] == 800
    assert payload["final_status"]["sleeves"][0]["portfolio"]["holdings"][0]["quantity"] == 2


def test_cli_order_runtime_submit_splits_same_sleeve_orders_by_market_route(tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "broker_accounts": [
                    {
                        "account_id": "kis-domestic",
                        "market_scope": "domestic",
                        "account_store_path": "accounts/kr.json",
                        "order_store_path": "orders/kr.jsonl",
                    },
                    {
                        "account_id": "kis-overseas",
                        "market_scope": "overseas",
                        "account_store_path": "accounts/us.json",
                        "order_store_path": "orders/us.jsonl",
                    },
                ],
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "broker_account_id": "kis-overseas",
                        "broker_account_routes": {
                            "domestic": "kis-domestic",
                            "overseas": "kis-overseas",
                        },
                        "cash": 1000,
                        "cash_by_currency": {"KRW": 1000, "USD": 1000},
                        "universe": {"coarse_path": "universe.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    batch_path = tmp_path / "order_batches.json"
    batch_path.write_text(
        json.dumps(
            {
                "batch_id": "batch-1",
                "sleeve_id": "LEaps",
                "generated_at": "2026-05-10T09:30:00",
                "orders": [
                    {
                        "symbol": "KRX:005930",
                        "side": "buy",
                        "quantity": 1,
                        "reference_price": 100,
                    },
                    {
                        "symbol": "NAS:NVDA",
                        "side": "buy",
                        "quantity": 1,
                        "reference_price": 200,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "order-runtime-submit",
            str(config_path),
            str(batch_path),
            "--sleeve-id",
            "LEaps",
            "--commit",
            "--broker",
            "paper",
            "--poll-after-submit",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "submitted"
    assert payload["route_count"] == 2
    by_account = {route["broker_account_id"]: route for route in payload["routes"]}
    assert by_account["kis-domestic"]["market_scope"] == "domestic"
    assert by_account["kis-domestic"]["currency"] == "KRW"
    assert by_account["kis-domestic"]["order_count"] == 1
    assert by_account["kis-overseas"]["market_scope"] == "overseas"
    assert by_account["kis-overseas"]["currency"] == "USD"
    assert by_account["kis-overseas"]["order_count"] == 1
    assert (tmp_path / "accounts" / "kr.json").exists()
    assert (tmp_path / "accounts" / "us.json").exists()


def test_cli_order_runtime_paper_smoke_submits_and_supervises(tmp_path, capsys):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "test",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "universe": {"coarse_path": "universe.json"},
                        "portfolio": {"account_store_path": "accounts/leaps.json"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    batch_path = tmp_path / "order_batches.json"
    batch_path.write_text(
        json.dumps(
            {
                "batch_id": "batch-1",
                "sleeve_id": "LEaps",
                "orders": [
                    {
                        "symbol": "KRX:005930",
                        "side": "buy",
                        "quantity": 1,
                        "reference_price": 100,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "order-runtime-paper-smoke",
            str(config_path),
            str(batch_path),
            "--sleeve-id",
            "LEaps",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["submit"]["orchestration"]["fill_event_count"] == 0
    assert payload["supervisor"]["poll_fill_event_count"] == 1
    assert payload["final_status"]["order_runtime"]["terminal_ticket_count"] == 1
    assert payload["final_status"]["sleeves"][0]["portfolio"]["cash"] == 900
    assert payload["final_status"]["sleeves"][0]["portfolio"]["holdings"][0]["quantity"] == 1
