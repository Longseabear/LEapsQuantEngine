import json
from pathlib import Path
from types import SimpleNamespace

import leaps_quant_engine.cli as cli
from leaps_quant_engine.cli import main
from leaps_quant_engine.models import Symbol


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
