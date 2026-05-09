import json
from pathlib import Path
from types import SimpleNamespace

import leaps_quant_engine.cli as cli
from leaps_quant_engine.cli import main
from leaps_quant_engine.models import OrderSide
from leaps_quant_engine.models import Symbol
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
