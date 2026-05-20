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


def test_cli_kis_gateway_health_outputs_gateway_health(monkeypatch, capsys):
    def fake_health(base_url, *, timeout_seconds):
        return {"status": "ok", "base_url": base_url, "timeout_seconds": timeout_seconds}

    monkeypatch.setattr(cli, "fetch_kis_gateway_health", fake_health)

    exit_code = cli.main(["kis-gateway-health", "--base-url", "http://127.0.0.1:9999", "--timeout-seconds", "1.25"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "ok",
        "base_url": "http://127.0.0.1:9999",
        "timeout_seconds": 1.25,
    }
