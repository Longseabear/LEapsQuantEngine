import json

from leaps_quant_engine.cli import main


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
