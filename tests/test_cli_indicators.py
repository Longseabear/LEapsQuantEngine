import json

from leaps_quant_engine import cli
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.universe.loader import parse_universe_definition


def test_cli_indicators_backtest_once_outputs_configured_symbols(monkeypatch, capsys):
    engine = IndicatorEngine()
    engine.register_universe(
        "swing-kor",
        parse_universe_definition(
            {
                "id": "test",
                "market": "KRX",
                "symbols": ["005930"],
                "indicators": [{"name": "sma_2_close", "type": "sma", "period": 2}],
            }
        ),
    )
    monkeypatch.setattr(cli, "build_indicator_engine_from_file", lambda path: engine)

    exit_code = cli.main(["indicators-backtest-once", "sample_swing_kor_pipeline.json", "--sleeve-id", "swing-kor"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "sleeve_id": "swing-kor",
        "symbols": ["KRX:005930"],
        "values": {"KRX:005930": {"sma_2_close": None}},
    }


def test_cli_indicators_kis_once_updates_from_provider(monkeypatch, capsys):
    class FakeProvider:
        @classmethod
        def from_env(cls):
            return cls()

    called = {"updated": False}

    class FakeIndicatorEngine:
        def warm_up_from_provider(self, sleeve_id, provider, start=None, end=None):
            called["warmup"] = (sleeve_id, start, end)

        def update_from_provider(self, provider):
            called["updated"] = True

        def symbols_for_sleeve(self, sleeve_id):
            return [type("S", (), {"key": "KRX:005930"})()]

        def values_for(self, sleeve_id, symbols, ready_only=False):
            return {"KRX:005930": {}}

    monkeypatch.setattr(cli, "KISBrokerEngineMarketDataProvider", FakeProvider)
    monkeypatch.setattr(cli, "build_indicator_engine_from_file", lambda path: FakeIndicatorEngine())

    exit_code = cli.main(
        [
            "indicators-kis-once",
            "sample_swing_kor_pipeline.json",
            "--sleeve-id",
            "swing-kor",
            "--warmup-start",
            "2026-05-01",
            "--warmup-end",
            "2026-05-07",
        ]
    )

    assert exit_code == 0
    assert called["updated"]
    assert called["warmup"][0] == "swing-kor"
    assert json.loads(capsys.readouterr().out)["symbols"] == ["KRX:005930"]
