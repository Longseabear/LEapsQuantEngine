from datetime import datetime
import json

import pandas as pd

from leaps_quant_engine import cli
from leaps_quant_engine.adapters import finance_datareader
from leaps_quant_engine.backtesting import VirtualMarketDataProvider
from leaps_quant_engine.fundamentals import FileFundamentalArtifactStore, FundamentalArtifact
from leaps_quant_engine.models import Bar
from leaps_quant_engine.models import Symbol


class FakeFinanceDataReader:
    def __init__(self, frame):
        self.frame = frame

    def StockListing(self, market):
        return self.frame


def test_fundamental_artifact_store_writes_reads_and_rebuilds_pit_store(tmp_path):
    as_of = datetime(2026, 5, 8)
    artifact = FundamentalArtifact.from_values(
        market="KRX",
        as_of=as_of,
        source="test",
        values={Symbol("005930", "KRX"): {"per": 9.5, "market_cap": 430_000_000_000_000}},
    )
    store = FileFundamentalArtifactStore(tmp_path)

    path = store.write(artifact)
    record = store.read(market="KRX", as_of=as_of)
    pit_store = record.artifact.to_store()

    assert path == tmp_path / "krx" / "2026-05-08.json"
    assert record.artifact.symbol_count == 1
    assert record.artifact.value_count == 2
    assert pit_store.latest(Symbol("005930", "KRX"), "per", as_of=as_of).value == 9.5
    assert pit_store.latest(Symbol("005930", "KRX"), "per", as_of=datetime(2026, 5, 7)) is None
    assert store.status()["latest_by_market"]["KRX"]["path"] == str(path)


def test_fundamental_artifact_store_loads_records_as_point_in_time_store(tmp_path):
    store = FileFundamentalArtifactStore(tmp_path)
    store.write(
        FundamentalArtifact.from_values(
            market="KRX",
            as_of=datetime(2024, 1, 1),
            source="test",
            values={Symbol("005930", "KRX"): {"per": 15.0, "pbr": 1.1}},
        )
    )
    store.write(
        FundamentalArtifact.from_values(
            market="KRX",
            as_of=datetime(2024, 1, 3),
            source="test",
            values={Symbol("005930", "KRX"): {"per": 8.0, "pbr": 1.0}},
        )
    )

    pit_store, records = store.load_to_store(
        market="KRX",
        end=datetime(2024, 1, 3),
        names=("per",),
    )

    assert len(records) == 2
    assert pit_store.latest(Symbol("005930", "KRX"), "per", as_of=datetime(2024, 1, 2)).value == 15.0
    assert pit_store.latest(Symbol("005930", "KRX"), "per", as_of=datetime(2024, 1, 4)).value == 8.0
    assert pit_store.latest(Symbol("005930", "KRX"), "pbr", as_of=datetime(2024, 1, 4)) is None


def test_fundamental_artifact_store_blocks_accidental_overwrite(tmp_path):
    as_of = datetime(2026, 5, 8)
    artifact = FundamentalArtifact.from_values(
        market="KRX",
        as_of=as_of,
        source="test",
        values={Symbol("005930", "KRX"): {"per": 9.5}},
    )
    store = FileFundamentalArtifactStore(tmp_path)

    store.write(artifact)

    try:
        store.write(artifact)
    except FileExistsError:
        pass
    else:
        raise AssertionError("expected duplicate artifact write to fail")


def test_fundamentals_import_fdr_and_status_cli(tmp_path, monkeypatch, capsys):
    frame = pd.DataFrame(
        [
            {
                "Code": "005930",
                "Close": 72000,
                "Marcap": 430_000_000_000_000,
                "Stocks": 5_969_782_550,
                "PER": 9.5,
            },
            {
                "Code": "000660",
                "Close": 180000,
                "Marcap": 131_000_000_000_000,
                "Stocks": 728_002_365,
                "PER": 12.1,
            },
        ]
    )
    monkeypatch.setattr(finance_datareader, "_load_finance_datareader", lambda: FakeFinanceDataReader(frame))

    import_result = cli.main(
        [
            "fundamentals-import-fdr",
            "--root",
            str(tmp_path),
            "--market",
            "KRX",
            "--as-of",
            "2026-05-08",
            "--symbol",
            "005930",
            "--name",
            "per",
            "--name",
            "market_cap",
            "--summary-only",
        ]
    )
    import_payload = json.loads(capsys.readouterr().out)

    assert import_result == 0
    assert import_payload["status"] == "written"
    assert import_payload["artifact"]["symbol_count"] == 1
    assert import_payload["artifact"]["value_count"] == 2

    status_result = cli.main(
        [
            "fundamentals-status",
            "--root",
            str(tmp_path),
            "--market",
            "KRX",
            "--summary-only",
        ]
    )
    status_payload = json.loads(capsys.readouterr().out)

    assert status_result == 0
    assert status_payload["artifact_count"] == 1
    assert status_payload["latest_by_market"]["KRX"]["names"] == ["market_cap", "per"]


def test_fundamentals_import_fdr_can_use_universe_symbols(tmp_path, monkeypatch, capsys):
    frame = pd.DataFrame(
        [
            {"Code": "005930", "PER": 9.5, "Marcap": 430_000_000_000_000},
            {"Code": "000660", "PER": 12.1, "Marcap": 131_000_000_000_000},
            {"Code": "035420", "PER": 18.0, "Marcap": 40_000_000_000_000},
        ]
    )
    universe_path = tmp_path / "universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "id": "value-universe",
                "market": "KRX",
                "symbols": ["005930", "000660"],
                "indicators": [{"name": "close", "type": "close", "period": 1}],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(finance_datareader, "_load_finance_datareader", lambda: FakeFinanceDataReader(frame))

    result = cli.main(
        [
            "fundamentals-import-fdr",
            "--root",
            str(tmp_path / "fundamentals"),
            "--universe",
            str(universe_path),
            "--as-of",
            "2026-05-08",
            "--name",
            "per",
            "--summary-only",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["artifact"]["market"] == "KRX"
    assert payload["artifact"]["symbol_count"] == 2
    assert payload["artifact"]["value_count"] == 2


def test_fundamentals_status_can_read_specific_artifact(tmp_path, capsys):
    as_of = datetime(2026, 5, 8)
    artifact = FundamentalArtifact.from_values(
        market="KRX",
        as_of=as_of,
        source="test",
        values={Symbol("005930", "KRX"): {"per": 9.5}},
    )
    FileFundamentalArtifactStore(tmp_path).write(artifact)

    result = cli.main(
        [
            "fundamentals-status",
            "--root",
            str(tmp_path),
            "--market",
            "KRX",
            "--as-of",
            "2026-05-08",
            "--summary-only",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["artifact"]["as_of"] == "2026-05-08T00:00:00"
    assert payload["artifact"]["symbol_count"] == 1


def test_framework_backtest_cli_loads_fundamental_artifacts(tmp_path, monkeypatch, capsys):
    symbol = Symbol("005930", "KRX")
    universe_path = tmp_path / "universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "id": "value-universe",
                "market": "KRX",
                "symbols": ["005930"],
                "indicators": [{"name": "close", "type": "close", "period": 1}],
            }
        ),
        encoding="utf-8",
    )
    alpha_path = tmp_path / "value_alpha.py"
    alpha_path.write_text(
        """
from leaps_quant_engine.alpha import Insight, InsightDirection

ALPHA_ID = "artifact-value-alpha"
VERSION = "1.0"

def generate(context):
    insights = []
    for symbol_key in context.symbol_keys:
        per = context.fundamental(symbol_key, "per")
        if per is not None and per < 10:
            insights.append(
                Insight(
                    sleeve_id=context.sleeve_id,
                    symbol=context.symbol(symbol_key),
                    direction=InsightDirection.UP,
                    generated_at=context.as_of,
                    source_snapshot_id=context.source_snapshot_id,
                    alpha_id=ALPHA_ID,
                    alpha_version=VERSION,
                    confidence=0.7,
                    reason="low_per_from_artifact",
                )
            )
    return insights
""".strip()
        + "\n",
        encoding="utf-8",
    )
    fundamentals_root = tmp_path / "fundamentals"
    FileFundamentalArtifactStore(fundamentals_root).write(
        FundamentalArtifact.from_values(
            market="KRX",
            as_of=datetime(2024, 1, 3),
            source="test",
            values={symbol: {"per": 8.0}},
        )
    )
    provider = VirtualMarketDataProvider.from_bars(
        [
            Bar(symbol, datetime(2024, 1, 2), 100.0, 100.0, 100.0, 100.0, 1000),
            Bar(symbol, datetime(2024, 1, 4), 100.0, 100.0, 100.0, 100.0, 1000),
        ]
    )
    monkeypatch.setattr(cli, "_daily_backtest_provider", lambda source: provider)

    result = cli.main(
        [
            "framework-backtest-daily",
            str(universe_path),
            str(alpha_path),
            "--sleeve-id",
            "LEaps",
            "--cash",
            "1000",
            "--fundamentals-root",
            str(fundamentals_root),
            "--fundamental-name",
            "per",
            "--summary-only",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["insight_count"] == 1
    assert payload["order_count"] == 1
    assert payload["fundamentals"]["artifact_count"] == 1
    assert payload["fundamentals"]["requested_names"] == ["per"]
