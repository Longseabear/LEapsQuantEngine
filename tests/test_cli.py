import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import leaps_quant_engine.cli as cli
from leaps_quant_engine.backtesting import VirtualMarketDataProvider
from leaps_quant_engine.cli import main
from leaps_quant_engine.execution import OrderIntentBatch
from leaps_quant_engine.models import Bar
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


def test_cli_notify_user_message_saves_local_record(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEAPS_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("STOCKPROGRAM_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("LEAPS_TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("STOCKPROGRAM_TELEGRAM_CHAT_ID", "")

    exit_code = main(
        [
            "notify-user-message",
            "--root",
            str(tmp_path / "notification-engine"),
            "--category",
            "order",
            "--title",
            "Order queued",
            "--message",
            "LEaps KRX:005930 buy 1",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["delivery_mode"] == "dry-run"
    assert payload["delivery_status"] == "saved_only"
    assert (tmp_path / "notification-engine" / "history" / f"{payload['record_id']}.json").exists()


def test_cli_notify_user_message_reads_utf8_message_file(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEAPS_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("STOCKPROGRAM_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("LEAPS_TELEGRAM_CHAT_ID", "")
    monkeypatch.setenv("STOCKPROGRAM_TELEGRAM_CHAT_ID", "")
    message_file = tmp_path / "message.txt"
    message_file.write_text("장 시작 점검\n삼성전자 후보", encoding="utf-8")

    exit_code = main(
        [
            "notify-user-message",
            "--root",
            str(tmp_path / "notification-engine"),
            "--category",
            "status",
            "--title",
            "한글 알림",
            "--message-file",
            str(message_file),
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    history = json.loads(
        (tmp_path / "notification-engine" / "history" / f"{payload['record_id']}.json").read_text(encoding="utf-8")
    )
    assert history["title"] == "한글 알림"
    assert history["message"] == "장 시작 점검\n삼성전자 후보"


def test_cli_notification_status_reports_local_counts(tmp_path, capsys):
    root = tmp_path / "notification-engine"
    (root / "history").mkdir(parents=True)
    (root / "history" / "item.json").write_text("{}", encoding="utf-8")

    exit_code = main(["notification-status", "--root", str(root)])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["engine"] == "leaps-notification"
    assert payload["history_count"] == 1


def test_cli_notification_fetch_updates_saves_only_without_telegram(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEAPS_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("STOCKPROGRAM_TELEGRAM_BOT_TOKEN", "")

    exit_code = main(
        [
            "notification-fetch-telegram-updates",
            "--root",
            str(tmp_path / "notification-engine"),
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "saved_only"
    assert payload["fetched_count"] == 0
    assert payload["stored_count"] == 0


def test_cli_runtime_backtest_daily_uses_config_selection_wiring(monkeypatch, tmp_path, capsys):
    universe_path = tmp_path / "universe.json"
    workspace = tmp_path / "sleeves" / "LEaps"
    (workspace / "alphas").mkdir(parents=True)
    (workspace / "selections").mkdir(parents=True)
    universe_path.write_text(
        json.dumps(
            {
                "id": "runtime-backtest-universe",
                "market": "KRX",
                "symbols": ["005930", "000660"],
                "indicators": [{"name": "close", "type": "close", "period": 1}],
            }
        ),
        encoding="utf-8",
    )
    (workspace / "alphas" / "selected.py").write_text(
        """
from datetime import timedelta
from leaps_quant_engine.alpha import Insight, InsightDirection

ALPHA_ID = "selected-alpha"
VERSION = "1.0"

def generate(context):
    return [
        Insight(
            sleeve_id=context.sleeve_id,
            symbol=context.symbol(symbol_key),
            direction=InsightDirection.UP,
            generated_at=context.as_of,
            expires_at=context.as_of + timedelta(days=1),
            source_snapshot_id=context.source_snapshot_id,
            alpha_id=ALPHA_ID,
            alpha_version=VERSION,
            weight=1.0,
            reason="selected_input",
            metadata={"opening_gap_pct": context.metadata_value(symbol_key, "opening_gap_pct")},
        )
        for symbol_key in context.symbol_keys
    ]
""",
        encoding="utf-8",
    )
    (workspace / "selections" / "second.py").write_text(
        """
from leaps_quant_engine.universe.selection import build_universe_selection_result

class SecondSymbolSelectionModel:
    selection_id = "second-only"

    def select(self, context):
        return build_universe_selection_result(
            context,
            (context.universe.symbols[1],),
            selection_id=self.selection_id,
            candidates={},
            rejected={},
        )
""",
        encoding="utf-8",
    )
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "runtime-backtest-test",
                "mode": "backtest",
                "timezone": "Asia/Seoul",
                "market_data": {"provider": "market-data-engine", "history_provider": "kis-cache"},
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "workspace_path": str(workspace),
                        "cash": 1000,
                        "universe": {
                            "coarse_path": str(universe_path),
                            "active": {
                                "max_symbols": 1,
                                "selection_models": ["selections/second.py:SecondSymbolSelectionModel"],
                            },
                        },
                        "indicators": {"warmup_enabled": False},
                        "alpha": {
                            "modules": [{"ref": "alphas/selected.py"}],
                            "input_selections": {"selected-alpha": "second-only"},
                        },
                        "portfolio": {"model": "leaps_quant_engine.framework:EqualWeightPortfolioConstructionModel"},
                        "risk": {"model": "leaps_quant_engine.framework:PassThroughRiskManagementModel"},
                        "execution": {"model": "leaps_quant_engine.execution:ImmediateExecutionModel"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    first = Symbol("005930", "KRX")
    second = Symbol("000660", "KRX")
    provider = VirtualMarketDataProvider.from_bars(
        [
            Bar(first, cli.datetime(2026, 1, 1), 100, 100, 100, 100, 1000),
            Bar(second, cli.datetime(2026, 1, 1), 200, 200, 200, 200, 1000),
            Bar(first, cli.datetime(2026, 1, 2), 100, 100, 100, 100, 1000),
            Bar(second, cli.datetime(2026, 1, 2), 200, 200, 200, 200, 1000),
        ]
    )
    monkeypatch.setattr(cli, "_daily_backtest_provider", lambda source: provider)

    exit_code = main(
        [
            "runtime-backtest-daily",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--cash",
            "1000",
            "--currency",
            "KRW",
            "--summary-only",
            "--include-insights",
            "--daily-bar-time",
            "09:00",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["runtime"]["runtime_id"] == "runtime-backtest-test"
    assert {"config_bootstrap_ms", "framework_backtest_ms", "report_generation_ms", "total_ms"} <= set(payload["timings"])
    assert "history_feed_build_ms" in payload["timings"]
    assert payload["alpha"]["input_selections"] == {"selected-alpha": "second-only"}
    assert "orders" not in payload
    assert payload["daily_bar_time"] == "09:00"
    assert payload["start"] == "2026-01-01T09:00:00"
    assert payload["insights"]["cycle_count"] == 2
    assert payload["insights"]["cycles"][0]["generated_at"] == "2026-01-01T09:00:00"
    assert payload["insights"]["cycles"][0]["new_insights"][0]["symbol"] == "KRX:000660"
    assert payload["insights"]["cycles"][1]["new_insights"][0]["metadata"]["opening_gap_pct"] == 0.0
    assert "cycles" in payload["selection"]
    assert payload["selection"]["last_live_symbols"] == ["KRX:000660"]
    assert payload["final_quantity"] == {"KRX:000660": 5}


def test_cli_runtime_backtest_minute_uses_runtime_config_and_local_feed(monkeypatch, tmp_path, capsys):
    universe_path = tmp_path / "universe.json"
    workspace = tmp_path / "sleeves" / "LEaps"
    (workspace / "alphas").mkdir(parents=True)
    universe_path.write_text(
        json.dumps(
            {
                "id": "runtime-minute-universe",
                "market": "KRX",
                "symbols": ["005930"],
                "indicators": [{"name": "close", "type": "close", "period": 1}],
            }
        ),
        encoding="utf-8",
    )
    (workspace / "alphas" / "daily.py").write_text(
        """
from datetime import timedelta
from leaps_quant_engine.alpha import Insight, InsightDirection

ALPHA_ID = "daily-alpha"
VERSION = "1.0"
EVALUATION_CADENCE = "once_per_day"
INPUT_RESOLUTION = "daily"

def generate(context):
    value = context.value(context.symbol_keys[0], "close")
    return [
        Insight(
            sleeve_id=context.sleeve_id,
            symbol=context.symbol(context.symbol_keys[0]),
            direction=InsightDirection.UP,
            generated_at=context.as_of,
            expires_at=context.as_of + timedelta(days=1),
            source_snapshot_id=context.source_snapshot_id,
            alpha_id=ALPHA_ID,
            alpha_version=VERSION,
            weight=1.0,
            reason="daily_alpha",
            metadata={"daily_close": value},
        )
    ]
""",
        encoding="utf-8",
    )
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "runtime-minute-test",
                "mode": "backtest",
                "timezone": "Asia/Seoul",
                "market_data": {"provider": "market-data-engine", "history_provider": "kis-cache"},
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "workspace_path": str(workspace),
                        "cash": 1000,
                        "universe": {"coarse_path": str(universe_path), "active": {"max_symbols": 1}},
                        "indicators": {"warmup_enabled": True, "extra_bars": 5, "refresh_history": False},
                        "alpha": {"modules": [{"ref": "alphas/daily.py"}]},
                        "portfolio": {"model": "leaps_quant_engine.framework:EqualWeightPortfolioConstructionModel"},
                        "risk": {"model": "leaps_quant_engine.framework:PassThroughRiskManagementModel"},
                        "execution": {"model": "leaps_quant_engine.execution:ImmediateExecutionModel"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    symbol = Symbol("005930", "KRX")
    provider = VirtualMarketDataProvider.from_bars(
        [Bar(symbol, cli.datetime(2026, 4, 30), 100, 100, 100, 100, 1000, resolution="daily")]
    )
    monkeypatch.setattr(cli, "_daily_backtest_provider", lambda source: provider)
    minute_feed = tmp_path / "minute.csv"
    minute_feed.write_text(
        "\n".join(
            [
                "symbol,time,open,high,low,close,volume",
                "KRX:005930,2026-05-01T09:00:00,50,50,50,50,100",
                "KRX:005930,2026-05-01T09:01:00,50,50,50,50,100",
            ]
        ),
        encoding="utf-8",
    )
    journal_path = tmp_path / "minute-journal.jsonl"

    exit_code = main(
        [
            "runtime-backtest-minute",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--minute-feed",
            str(minute_feed),
            "--start",
            "2026-05-01T09:00:00",
            "--end",
            "2026-05-01T09:01:00",
            "--warmup-start",
            "2026-04-01",
            "--cash",
            "1000",
            "--currency",
            "KRW",
            "--journal",
            str(journal_path),
            "--summary-only",
            "--include-insights",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "minute-feed"
    assert {"config_bootstrap_ms", "feed_load_ms", "daily_warmup_ms", "framework_replay_ms", "report_generation_ms", "total_ms"} <= set(payload["timings"])
    assert {
        "replay_indicator_update_ms",
        "replay_indicator_snapshot_ms",
        "replay_framework_runner_ms",
        "replay_journal_append_ms",
    } <= set(payload["timings"])
    assert payload["cycle_journal"]["mode"] == "light"
    assert payload["minute_backtest"]["minute_data_slice_count"] == 2
    assert payload["minute_backtest"]["daily_warmup_bar_count"] == 1
    assert payload["insights"]["cycle_count"] == 2
    assert payload["insights"]["cycles"][0]["new_insights"][0]["metadata"]["daily_close"] == 100.0
    assert payload["final_quantity"] == {"KRX:005930": 20}
    journal_rows = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
    assert len(journal_rows) == 2
    assert journal_rows[0]["metadata"]["lineage_omitted"] == "journal_mode_light"
    assert "lineage" not in journal_rows[0]["metadata"]

    compiled_cache = tmp_path / "compiled-minute-replay.json.gz"
    exit_code = main(
        [
            "runtime-backtest-minute",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--minute-feed",
            str(minute_feed),
            "--compiled-replay-cache",
            str(compiled_cache),
            "--start",
            "2026-05-01T09:00:00",
            "--end",
            "2026-05-01T09:01:00",
            "--warmup-start",
            "2026-04-01",
            "--cash",
            "1000",
            "--currency",
            "KRW",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    compiled_write_payload = json.loads(capsys.readouterr().out)
    assert compiled_write_payload["source"] == "minute-feed"
    assert compiled_write_payload["compiled_replay_cache"]["status"] == "written"
    assert compiled_write_payload["compiled_replay_cache"]["slice_count"] == 2
    assert compiled_cache.exists()

    exit_code = main(
        [
            "runtime-backtest-minute",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--compiled-replay-cache",
            str(compiled_cache),
            "--warmup-start",
            "2026-04-01",
            "--cash",
            "1000",
            "--currency",
            "KRW",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    compiled_hit_payload = json.loads(capsys.readouterr().out)
    assert compiled_hit_payload["source"] == "compiled-replay-cache"
    assert compiled_hit_payload["compiled_replay_cache"]["status"] == "hit"
    assert compiled_hit_payload["minute_backtest"]["minute_data_slice_count"] == 2
    assert compiled_hit_payload["final_quantity"] == {"KRX:005930": 20}

    daily_warmup_cache = tmp_path / "daily-warmup.json.gz"
    exit_code = main(
        [
            "runtime-backtest-minute",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--compiled-replay-cache",
            str(compiled_cache),
            "--warmup-start",
            "2026-04-01",
            "--daily-warmup-cache",
            str(daily_warmup_cache),
            "--cash",
            "1000",
            "--currency",
            "KRW",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    warmup_write_payload = json.loads(capsys.readouterr().out)
    assert warmup_write_payload["daily_warmup_cache"]["status"] == "written"
    assert warmup_write_payload["daily_warmup_cache"]["row_count"] == 1

    exit_code = main(
        [
            "runtime-backtest-minute",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--compiled-replay-cache",
            str(compiled_cache),
            "--warmup-start",
            "2026-04-01",
            "--daily-warmup-cache",
            str(daily_warmup_cache),
            "--cash",
            "1000",
            "--currency",
            "KRW",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    warmup_hit_payload = json.loads(capsys.readouterr().out)
    assert warmup_hit_payload["daily_warmup_cache"]["status"] == "hit"
    assert warmup_hit_payload["final_quantity"] == {"KRX:005930": 20}

    cache_dir = tmp_path / "minute-cache" / "runtime-minute-universe"
    cache_dir.mkdir(parents=True)
    (cache_dir / "2026-05-01.csv").write_text(minute_feed.read_text(encoding="utf-8"), encoding="utf-8")
    exit_code = main(
        [
            "runtime-backtest-minute",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--minute-cache-root",
            str(tmp_path / "minute-cache"),
            "--start",
            "2026-05-01T09:00:00",
            "--end",
            "2026-05-01T09:01:00",
            "--warmup-start",
            "2026-04-01",
            "--cash",
            "1000",
            "--currency",
            "KRW",
            "--summary-only",
            "--include-insights",
        ]
    )

    assert exit_code == 0
    cache_payload = json.loads(capsys.readouterr().out)
    assert cache_payload["source"] == "minute-cache"
    assert cache_payload["minute_cache"]["row_count"] == 2
    assert cache_payload["minute_backtest"]["minute_feed"] is None
    assert cache_payload["minute_backtest"]["minute_data_slice_count"] == 2
    assert cache_payload["final_quantity"] == {"KRX:005930": 20}


def test_cli_download_us_minute_feed_from_runtime_universe(monkeypatch, tmp_path, capsys):
    universe_path = tmp_path / "us_universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "id": "us-minute-universe",
                "market": "US",
                "symbols": ["SPY", "QQQ"],
                "indicators": [{"name": "close", "type": "close", "period": 1}],
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "us-minute-runtime",
                "mode": "backtest",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "us_etf_rotation",
                        "cash": 1000,
                        "universe": {"coarse_path": str(universe_path), "active": {"max_symbols": 2}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class FakeProvider:
        provider_name = "yfinance"

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def download(self, symbol, *, start, end, interval):
            return [
                Bar(
                    Symbol(symbol.ticker, "US"),
                    cli.datetime(2026, 5, 1, 9, 30),
                    100,
                    101,
                    99,
                    100.5,
                    1000,
                    resolution="minute",
                )
            ]

    monkeypatch.setattr(cli, "YFinanceMinuteBarProvider", FakeProvider)
    output = tmp_path / "minute" / "us.csv"

    exit_code = main(
        [
            "download-us-minute-feed",
            str(config_path),
            "--sleeve-id",
            "us_etf_rotation",
            "--output",
            str(output),
            "--start",
            "2026-05-01",
            "--end",
            "2026-05-01",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["row_count"] == 2
    assert payload["downloaded_symbol_count"] == 2
    assert payload["runtime_backtest_minute_command"][:4] == [
        "runtime-backtest-minute",
        str(config_path),
        "--sleeve-id",
        "us_etf_rotation",
    ]
    rows = output.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "symbol,time,open,high,low,close,volume"
    assert "US:SPY,2026-05-01T09:30:00,100,101,99,100.5,1000" in rows
    assert "US:QQQ,2026-05-01T09:30:00,100,101,99,100.5,1000" in rows


def test_cli_minute_cache_build_and_export_for_krx_runtime_universe(monkeypatch, tmp_path, capsys):
    universe_path = tmp_path / "krx_universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "id": "krx-minute-universe",
                "market": "KRX",
                "symbols": [
                    {"ticker": "005930", "market": "KRX", "market_segment": "KOSPI", "market_id": "STK"},
                    {"ticker": "050890", "market": "KRX", "market_segment": "KOSDAQ", "market_id": "KSQ"},
                ],
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "krx-minute-runtime",
                "mode": "backtest",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "universe": {"coarse_path": str(universe_path), "active": {"max_symbols": 2}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class FakeProvider:
        provider_name = "yfinance"

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def download(self, symbol, *, start, end, interval):
            return [
                Bar(
                    Symbol(symbol.ticker, symbol.market),
                    cli.datetime(2026, 5, 15, 9, 0),
                    100,
                    101,
                    99,
                    100.5,
                    1000,
                    resolution="minute",
                )
            ]

    monkeypatch.setattr(cli, "YFinanceMinuteBarProvider", FakeProvider)
    cache_root = tmp_path / "minute-cache"

    exit_code = main(
        [
            "minute-cache-build",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--cache-root",
            str(cache_root),
            "--start",
            "2026-05-15",
            "--end",
            "2026-05-15",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    build_payload = json.loads(capsys.readouterr().out)
    assert build_payload["status"] == "ok"
    assert build_payload["row_count"] == 2
    assert build_payload["runtime_backtest_minute_command"][:4] == [
        "runtime-backtest-minute",
        str(config_path),
        "--sleeve-id",
        "LEaps",
    ]

    output = tmp_path / "export.csv"
    exit_code = main(
        [
            "minute-cache-export",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--cache-root",
            str(cache_root),
            "--output",
            str(output),
            "--start",
            "2026-05-15T09:00:00",
            "--end",
            "2026-05-15T09:00:00",
            "--symbol",
            "KRX:005930",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    export_payload = json.loads(capsys.readouterr().out)
    assert export_payload["status"] == "ok"
    assert export_payload["row_count"] == 1
    assert output.read_text(encoding="utf-8").splitlines()[1].startswith("KRX:005930,2026-05-15T09:00:00")


def test_cli_minute_cache_build_supports_kis_extended_session_metadata(monkeypatch, tmp_path, capsys):
    universe_path = tmp_path / "krx_universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "id": "krx-opening-universe",
                "market": "KRX",
                "symbols": [{"ticker": "005930", "market": "KRX"}],
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "krx-opening-runtime",
                "mode": "backtest",
                "timezone": "Asia/Seoul",
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "cash": 1000,
                        "universe": {"coarse_path": str(universe_path), "active": {"max_symbols": 1}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class FakeKISProvider:
        last_instance = None

        def __init__(self, *, exchange_by_symbol=None):
            self.calls = []
            self.exchange_by_symbol = dict(exchange_by_symbol or {})
            FakeKISProvider.last_instance = self

        @classmethod
        def from_env(cls, exchange_by_symbol=None):
            return cls(exchange_by_symbol=exchange_by_symbol)

        def get_cached_minute_history(
            self,
            symbol,
            *,
            trade_date,
            start_time,
            end_time,
            interval_minutes,
            refresh,
        ):
            self.calls.append((start_time, end_time, interval_minutes, refresh))
            return [
                Bar(
                    symbol,
                    cli.datetime(2026, 5, 15, 8, 50),
                    100,
                    101,
                    99,
                    100,
                    1000,
                    resolution="minute",
                )
            ]

    monkeypatch.setattr(cli, "KISCachedMarketDataProvider", FakeKISProvider)

    exit_code = main(
        [
            "minute-cache-build",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--cache-root",
            str(tmp_path / "minute-cache"),
            "--start",
            "2026-05-15",
            "--end",
            "2026-05-15",
            "--provider",
            "kis-cache",
            "--include-extended-hours",
            "--refresh-provider-cache",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "kis-cache"
    assert payload["include_extended_hours"] is True
    assert payload["include_session_metadata"] is True
    assert FakeKISProvider.last_instance.calls == [("08:30:00", "18:00:00", 1, True)]
    cache_file = tmp_path / "minute-cache" / "krx-opening-universe" / "2026-05-15.csv.gz"
    import gzip

    with gzip.open(cache_file, "rt", encoding="utf-8") as handle:
        rows = handle.read().splitlines()
    assert "market_session_phase" in rows[0]
    assert "regular_open_auction" in rows[1]


def test_cli_minute_cache_build_supports_kis_overseas_universe(monkeypatch, tmp_path, capsys):
    universe_path = tmp_path / "us_universe.json"
    universe_path.write_text(
        json.dumps(
            {
                "id": "us-minute-universe",
                "market": "US",
                "symbols": [{"ticker": "SMH", "market": "US", "exchange": "NAS"}],
            }
        ),
        encoding="utf-8",
    )
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "us-minute-runtime",
                "mode": "backtest",
                "timezone": "America/New_York",
                "sleeves": [
                    {
                        "sleeve_id": "us_etf_rotation",
                        "cash": 1000,
                        "universe": {"coarse_path": str(universe_path), "active": {"max_symbols": 1}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    class FakeKISProvider:
        last_instance = None

        def __init__(self, *, exchange_by_symbol=None):
            self.exchange_by_symbol = dict(exchange_by_symbol or {})
            self.calls = []
            FakeKISProvider.last_instance = self

        @classmethod
        def from_env(cls, exchange_by_symbol=None):
            return cls(exchange_by_symbol=exchange_by_symbol)

        def get_cached_minute_history(
            self,
            symbol,
            *,
            trade_date,
            start_time,
            end_time,
            interval_minutes,
            refresh,
        ):
            self.calls.append((symbol.key, trade_date, start_time, end_time, interval_minutes, refresh))
            return [
                Bar(
                    symbol,
                    cli.datetime(2026, 5, 15, 9, 30),
                    570.10,
                    571.00,
                    569.90,
                    570.44,
                    1200,
                    resolution="minute",
                )
            ]

    monkeypatch.setattr(cli, "KISCachedMarketDataProvider", FakeKISProvider)

    exit_code = main(
        [
            "minute-cache-build",
            str(config_path),
            "--sleeve-id",
            "us_etf_rotation",
            "--cache-root",
            str(tmp_path / "minute-cache"),
            "--start",
            "2026-05-15",
            "--end",
            "2026-05-15",
            "--provider",
            "kis-cache",
            "--refresh-provider-cache",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["provider"] == "kis-cache"
    assert payload["row_count"] == 1
    assert FakeKISProvider.last_instance.exchange_by_symbol == {"US:SMH": "NAS", "SMH": "NAS"}
    assert FakeKISProvider.last_instance.calls == [("US:SMH", cli.datetime(2026, 5, 15, 9, 30), "09:30:00", "16:00:00", 1, True)]


def test_cli_leaps_runtime_backtest_uses_kr_research_universe_and_krw_cash(monkeypatch, capsys):
    class SyntheticDailyProvider:
        def get_history(self, symbol, *, start=None, end=None):
            bars = []
            base = 80.0 + (sum(ord(char) for char in symbol.ticker) % 70)
            for index in range(32):
                time = cli.datetime(2026, 1, 1) + timedelta(days=index)
                if start is not None and time < start:
                    continue
                if end is not None and time > end:
                    continue
                close = base * (1.0 + 0.004 * index)
                bars.append(
                    Bar(
                        symbol,
                        time,
                        close * 0.99,
                        close * 1.01,
                        close * 0.98,
                        close,
                        1_000_000 + (index * 1000),
                    )
                )
            return bars

    monkeypatch.setattr(cli, "_daily_backtest_provider", lambda source: SyntheticDailyProvider())

    exit_code = main(
        [
            "runtime-backtest-daily",
            "configs/runtime/leaps_workspace_smoke.json",
            "--sleeve-id",
            "LEaps",
            "--start",
            "2026-01-01",
            "--end",
            "2026-02-01",
            "--cash",
            "2000000",
            "--source",
            "finance-datareader",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["universe_id"] == "leaps-kr-research-core"
    assert payload["portfolio"]["initial_cash_by_currency"] == {"KRW": 2000000.0}
    assert payload["alpha"]["input_selections"] == {
        "leaps-kospi-conviction": "leaps-stock-momentum",
        "leaps-kospi-pullback-reversion": "leaps-stock-momentum",
        "leaps-volatility-trailing-stop": "leaps-operational-symbols",
    }
    assert set(payload["selection"]["selection_ids"]) == {
        "leaps-stock-momentum",
        "leaps-operational-symbols",
    }
    assert payload["order_count"] > 0
    assert {order["sleeve_id"] for order in payload.get("orders", [])} <= {"LEaps"}
    configured = {sleeve["sleeve_id"]: sleeve for sleeve in payload["runtime"]["configured_sleeves"]}
    assert configured["default sleeve"]["cash_by_currency"] == {}
    assert configured["default sleeve"]["alpha_module_count"] == 0


def test_cli_default_sleeve_runtime_backtest_stays_inactive(monkeypatch, capsys):
    class SyntheticDailyProvider:
        def get_history(self, symbol, *, start=None, end=None):
            bars = []
            for index in range(25):
                time = cli.datetime(2026, 1, 1) + timedelta(days=index)
                close = 100.0 + index
                bars.append(Bar(symbol, time, close, close, close, close, 1000))
            return bars

    monkeypatch.setattr(cli, "_daily_backtest_provider", lambda source: SyntheticDailyProvider())

    exit_code = main(
        [
            "runtime-backtest-daily",
            "configs/runtime/leaps_workspace_smoke.json",
            "--sleeve-id",
            "default sleeve",
            "--start",
            "2026-01-01",
            "--end",
            "2026-01-25",
            "--source",
            "finance-datareader",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sleeve_id"] == "default sleeve"
    assert payload["portfolio"]["initial_cash"] == 0
    assert payload["portfolio"]["initial_cash_by_currency"] == {}
    assert payload["insight_count"] == 0
    assert payload["order_count"] == 0
    assert payload["final_quantity"] == {}


def test_cli_train_rl_portfolio_constructor_uses_runtime_universe(monkeypatch, tmp_path, capsys):
    captured = {}

    def fake_train(universe, provider, **kwargs):
        captured["universe_id"] = universe.id
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            to_dict=lambda: {
                "model_path": str(tmp_path / "model.zip"),
                "metadata_path": str(tmp_path / "model.json"),
                "timesteps": kwargs["timesteps"],
                "algorithm": "PPO",
                "universe_id": universe.id,
                "start": kwargs["start"].isoformat(),
                "end": kwargs["end"].isoformat(),
                "symbol_count": len(universe.symbols),
                "episode_length": 10,
            }
        )

    monkeypatch.setattr(cli, "_daily_backtest_provider", lambda source: object())
    monkeypatch.setattr(cli, "train_ppo_portfolio_constructor", fake_train)

    exit_code = main(
        [
            "train-rl-portfolio-constructor",
            "configs/runtime/leaps_workspace_smoke.json",
            "--sleeve-id",
            "LEaps",
            "--start",
            "2024-01-01",
            "--end",
            "2024-12-31",
            "--timesteps",
            "123",
            "--ensemble-seed",
            "7",
            "--ensemble-seed",
            "17",
            "--output-dir",
            str(tmp_path),
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured["universe_id"] == "leaps-kr-research-core"
    assert captured["kwargs"]["output_dir"] == tmp_path
    assert captured["kwargs"]["seeds"] == (7, 17)
    assert captured["kwargs"]["top_k"] == 8
    assert captured["kwargs"]["exposure_levels"] == (0.0, 0.25, 0.5, 0.75, 0.95)
    assert captured["kwargs"]["allocation_mode"] == "rl_weights"
    assert captured["kwargs"]["turnover_penalty"] == 0.12
    assert captured["kwargs"]["downside_penalty"] == 1.1
    assert captured["kwargs"]["volatility_penalty"] == 0.45
    assert captured["kwargs"]["drawdown_penalty"] == 0.95
    assert captured["kwargs"]["underwater_penalty"] == 0.35
    assert captured["kwargs"]["missed_upside_penalty"] == 0.08
    assert captured["kwargs"]["concentration_penalty"] == 0.35
    assert payload["status"] == "trained"
    assert payload["rl"]["algorithm"] == "PPO"
    assert payload["portfolio_model"]["ref"]["ref"] == "portfolios/rl_ppo_constructor.py"


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


def test_cli_runtime_run_multi_once_requires_explicit_sleeves(capsys):
    exit_code = main(["runtime-run-multi-once", "configs/runtime/live_multi_sleeve.json"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert json.loads(captured.err)["error"] == "runtime-run-multi-once requires at least one --sleeve-id."


def test_cli_route_specific_submit_notional_override(tmp_path):
    route = cli._OrderRuntimeRoute(
        account_id="kis-domestic",
        market_scope="domestic",
        currency="KRW",
        account_store_path=tmp_path / "account.json",
        order_store_path=tmp_path / "orders.jsonl",
    )
    args = SimpleNamespace(
        max_submit_notional=1000.0,
        max_submit_notional_by_account=["kis-domestic=7000000", "overseas=2500"],
    )

    assert cli._max_submit_notional_for_route(args, route) == 7_000_000.0


def test_cli_resolves_multi_sleeve_order_routes_without_requiring_every_sleeve_on_each_account(tmp_path):
    config_path = tmp_path / "runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "runtime_id": "multi-route",
                "mode": "live",
                "timezone": "Asia/Seoul",
                "broker_accounts": [
                    {
                        "account_id": "kis-domestic",
                        "market_scope": "domestic",
                        "currency": "KRW",
                        "account_store_path": "domestic.json",
                    },
                    {
                        "account_id": "kis-overseas",
                        "market_scope": "overseas",
                        "currency": "USD",
                        "account_store_path": "overseas.json",
                    },
                ],
                "sleeves": [
                    {
                        "sleeve_id": "LEaps",
                        "broker_account_id": "kis-domestic",
                        "broker_account_routes": {"domestic": "kis-domestic"},
                        "universe": {"coarse_path": "kr.json"},
                    },
                    {
                        "sleeve_id": "us_etf_rotation",
                        "broker_account_id": "kis-overseas",
                        "broker_account_routes": {"overseas": "kis-overseas"},
                        "universe": {"coarse_path": "us.json"},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    snapshot = cli.load_runtime_config_snapshot(config_path)

    routes = cli._resolve_order_runtime_routes(
        snapshot,
        None,
        None,
        None,
        ("LEaps", "us_etf_rotation"),
    )

    assert [route.account_id for route in routes] == ["kis-domestic", "kis-overseas"]
    with pytest.raises(RuntimeError, match="us_etf_rotation"):
        cli._resolve_order_runtime_route(snapshot, "kis-domestic", None, None, ("LEaps", "us_etf_rotation"))


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


def test_cli_manages_sleeve_risk_and_execution_models(tmp_path, capsys):
    workspace = tmp_path / "sleeves" / "LEaps"
    risk_dir = workspace / "risks"
    execution_dir = workspace / "executions"
    risk_dir.mkdir(parents=True)
    execution_dir.mkdir(parents=True)
    (risk_dir / "basic.py").write_text("# risk\n", encoding="utf-8")
    (risk_dir / "strict.py").write_text("# risk\n", encoding="utf-8")
    (execution_dir / "immediate.py").write_text("# execution\n", encoding="utf-8")
    (execution_dir / "sliced.py").write_text("# execution\n", encoding="utf-8")
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
                        "risk": {"model": "risks/basic.py", "parameters": {"long_only": True}},
                        "execution": {"model": "executions/immediate.py", "parameters": {"order_type": "limit"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert main(["sleeve-risk-list", str(config_path), "--sleeve-id", "LEaps"]) == 0
    listed_risk = json.loads(capsys.readouterr().out)
    assert listed_risk["available_risk_models"] == ["risks/basic.py", "risks/strict.py"]
    assert listed_risk["active_risk_model"] == "risks/basic.py"
    assert listed_risk["reload_command"]["command_type"] == "reload_sleeve"

    assert main(["sleeve-risk-set", str(config_path), "strict", "--sleeve-id", "LEaps"]) == 0
    updated_risk = json.loads(capsys.readouterr().out)
    assert updated_risk["active_risk_model"] == "risks/strict.py"

    assert main(["sleeve-execution-list", str(config_path), "--sleeve-id", "LEaps"]) == 0
    listed_execution = json.loads(capsys.readouterr().out)
    assert listed_execution["available_execution_models"] == [
        "executions/immediate.py",
        "executions/sliced.py",
    ]
    assert listed_execution["active_execution_model"] == "executions/immediate.py"
    assert listed_execution["reload_command"]["payload"]["sleeve_id"] == "LEaps"

    assert main(["sleeve-execution-set", str(config_path), "sliced", "--sleeve-id", "LEaps"]) == 0
    updated_execution = json.loads(capsys.readouterr().out)
    assert updated_execution["active_execution_model"] == "executions/sliced.py"

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    sleeve = payload["sleeves"][0]
    assert sleeve["risk"]["model"] == "risks/strict.py"
    assert sleeve["risk"]["parameters"] == {"long_only": True}
    assert sleeve["execution"]["model"] == "executions/sliced.py"
    assert sleeve["execution"]["parameters"] == {"order_type": "limit"}


def test_cli_keeps_import_model_references_readable(tmp_path, capsys):
    workspace = tmp_path / "sleeves" / "LEaps"
    workspace.mkdir(parents=True)
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
                        "risk": {"model": "leaps_quant_engine.framework:BasicRiskManagementModel"},
                        "execution": {"model": "leaps_quant_engine.execution:ImmediateExecutionModel"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert main(["sleeve-risk-list", str(config_path), "--sleeve-id", "LEaps"]) == 0
    risk = json.loads(capsys.readouterr().out)
    assert risk["active_risk_model"] == "leaps_quant_engine.framework:BasicRiskManagementModel"

    assert main(["sleeve-execution-list", str(config_path), "--sleeve-id", "LEaps"]) == 0
    execution = json.loads(capsys.readouterr().out)
    assert execution["active_execution_model"] == "leaps_quant_engine.execution:ImmediateExecutionModel"


def test_cli_persists_runtime_control_commands(tmp_path, capsys):
    queue_path = tmp_path / "runtime-control.jsonl"

    assert main(
        [
            "runtime-control-submit",
            "--queue",
            str(queue_path),
            "--command",
            "reload-sleeve",
            "--config",
            "configs/runtime/leaps_workspace_smoke.json",
            "--sleeve-id",
            "LEaps",
            "--reason",
            "test reload",
        ]
    ) == 0
    submitted = json.loads(capsys.readouterr().out)
    assert submitted["command_type"] == "reload_sleeve"

    assert main(["runtime-control-drain", "--queue", str(queue_path)]) == 0
    drained = json.loads(capsys.readouterr().out)
    assert drained["command_count"] == 1
    assert drained["commands"][0]["payload"]["sleeve_id"] == "LEaps"


def test_cli_persists_activate_and_deactivate_sleeve_control_commands(tmp_path, capsys):
    queue_path = tmp_path / "runtime-control.jsonl"

    assert main(
        [
            "runtime-control-submit",
            "--queue",
            str(queue_path),
            "--command",
            "activate-sleeve",
            "--config",
            "configs/runtime/live_multi_sleeve.json",
            "--sleeve-id",
            "us_etf_rotation",
        ]
    ) == 0
    activated = json.loads(capsys.readouterr().out)
    assert activated["command_type"] == "activate_sleeve"

    assert main(
        [
            "runtime-control-submit",
            "--queue",
            str(queue_path),
            "--command",
            "deactivate-sleeve",
            "--config",
            "configs/runtime/live_multi_sleeve.json",
            "--sleeve-id",
            "us_etf_rotation",
        ]
    ) == 0
    deactivated = json.loads(capsys.readouterr().out)
    assert deactivated["command_type"] == "deactivate_sleeve"

    assert main(["runtime-control-drain", "--queue", str(queue_path)]) == 0
    drained = json.loads(capsys.readouterr().out)
    assert [command["command_type"] for command in drained["commands"]] == [
        "activate_sleeve",
        "deactivate_sleeve",
    ]


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
    order_store = FileOrderRuntimeStateStore(tmp_path / "order-runtime" / "leaps.jsonl")
    coordination = OrderCoordinator().coordinate(
        (
            OrderIntentBatch(
                sleeve_id="LEaps",
                generated_at=cli.datetime(2026, 5, 9, 9, 0),
                order_intents=(
                    OrderIntent(
                        sleeve_id="LEaps",
                        symbol=Symbol("005930", "KRX"),
                        side=OrderSide.BUY,
                        quantity=2,
                        reference_price=100,
                    ),
                ),
                batch_id="batch-1",
            ),
        ),
        generated_at=cli.datetime(2026, 5, 9, 9, 0),
    )
    order_store.record_tickets(coordination.tickets)
    order_store.record_event(
        coordination.tickets[0].event(
            OrderEventType.FILLED,
            occurred_at=cli.datetime(2026, 5, 9, 9, 1),
            quantity=2,
            fill_price=100,
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
    assert payload["order_runtime_filled_positions"] == [{"symbol": "005930", "market": "KRX", "quantity": 2}]


def test_cli_virtual_account_ignore_fill_marks_fill_non_actionable(tmp_path, capsys):
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
            order_id="manual-order",
            symbol=Symbol("005930", "KRX"),
            side=OrderSide.SELL,
            quantity=1,
            fill_price=100,
            filled_at=cli.datetime(2026, 5, 14, 15, 10),
        )
    )

    exit_code = main(
        [
            "virtual-account-ignore-fill",
            str(config_path),
            "--sleeve-id",
            "LEaps",
            "--fill-id",
            "fill-1",
            "--reason",
            "manual position outside engine",
            "--ignored-by",
            "test-operator",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ignored"]["fill_id"] == "fill-1"
    assert payload["ignored"]["ignored_by"] == "test-operator"
    assert payload["allocation_status"]["status"] == "ignored"
    assert payload["unallocated_fill_count"] == 0


def test_default_reconcile_date_uses_market_local_date_for_overseas():
    now = cli.datetime(2026, 5, 13, 2, 30, tzinfo=cli.timezone(cli.timedelta(hours=9)))

    assert cli._default_reconcile_date("overseas", now=now) == "20260512"
    assert cli._default_reconcile_date("domestic", now=now) == "20260513"


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


def test_cli_order_runtime_supervise_allows_overseas_broker_engine_poll_and_reconcile_setup(monkeypatch, tmp_path, capsys):
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

    class FakeBroker:
        def get_snapshots(self, *, consumer_id, snapshot_type="", resource_id="", limit=200):
            return {"snapshots": []}

    class FakeAccountClient:
        broker = FakeBroker()

        @classmethod
        def from_env(cls, *args, **kwargs):
            return cls()

        def get_execution_history(self, **kwargs):
            return {"executions": []}

        def get_holdings(self, **kwargs):
            return {"holdings": []}

    monkeypatch.setattr(cli, "KISAccountClient", FakeAccountClient)

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
    assert payload["status"] in {"ok", "needs_attention"}
    assert "broker_engine_overseas_poll_not_supported" not in payload["errors"]
    assert "broker_engine_overseas_reconcile_not_supported" not in payload["errors"]
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


def test_cli_order_runtime_supervise_cancels_stale_open_tickets_with_paper_broker(tmp_path, capsys):
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
                        tag="cli-supervise-cancel",
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
            "--paper-no-fill",
            "--skip-reconcile",
            "--stale-after-seconds",
            "0.01",
            "--cancel-stale-open-tickets",
            "--summary-only",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["maintenance_report"]["stale_ticket_count"] == 1
    assert payload["maintenance_report"]["cancel_event_count"] == 1
    assert payload["final_status"]["order_runtime"]["open_ticket_count"] == 0


def test_cli_order_runtime_submit_commits_paper_order_batches(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("LEAPS_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("STOCKPROGRAM_TELEGRAM_BOT_TOKEN", "")
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
            "--notify",
            "--notification-root",
            str(tmp_path / "notifications"),
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
    assert payload["notification"]["delivery_status"] == "saved_only"
    assert (tmp_path / "notifications" / "history" / f"{payload['notification']['record_id']}.json").exists()


def test_cli_order_runtime_submit_rejects_live_confirm_with_paper_broker(tmp_path, capsys):
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
            "--confirm-live-submit",
            "--summary-only",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["status"] == "error"
    assert "broker-engine" in payload["error"]


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
