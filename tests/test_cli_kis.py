import json

from leaps_quant_engine import cli


class FakeKISProvider:
    @classmethod
    def from_env(cls):
        return cls()

    def health_check(self):
        return {"status": "ok"}


def test_cli_kis_health_outputs_bridge_health(monkeypatch, capsys):
    monkeypatch.setattr(cli, "KISBrokerEngineMarketDataProvider", FakeKISProvider)

    exit_code = cli.main(["kis-health"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "ok"}
