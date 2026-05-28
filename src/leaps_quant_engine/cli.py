from __future__ import annotations

import argparse
import json
import sys
import time as perf_time
from dataclasses import dataclass, replace
from datetime import datetime, time as clock_time, timedelta, timezone
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo

from leaps_quant_engine.adapters.kis import (
    KISBrokerEngineMarketDataProvider,
    KISCachedMarketDataProvider,
    MarketDataEngineLiveQuoteProvider,
)
from leaps_quant_engine.adapters.finance_datareader import (
    FinanceDataReaderFundamentalProvider,
    FinanceDataReaderMarketDataProvider,
)
from leaps_quant_engine.adapters.parquet_daily import ParquetDailyBarProvider
from leaps_quant_engine.account_sync import KISVirtualAccountSync
from leaps_quant_engine.account_sync import KISAccountClient
from leaps_quant_engine.alpha import AlphaRuntime, PythonAlphaLoader, SnapshotContext
from leaps_quant_engine.backtesting import (
    build_minute_replay_feed_from_bars,
    load_compiled_minute_replay_cache,
    load_daily_warmup_bars_for_backtest,
    load_daily_warmup_cache,
    load_minute_replay_feed,
    minute_replay_source_signature,
    run_framework_backtest,
    run_framework_replay,
    simulated_fill_model_for_costs,
    universe_with_default_indicator_resolution,
    warm_up_daily_indicators_for_backtest,
    write_compiled_minute_replay_cache,
    write_daily_warmup_cache,
)
from leaps_quant_engine.benchmark import run_daily_indicator_benchmark
from leaps_quant_engine.broker_routing import (
    configured_account_ids_for_sleeve,
    currency_for_market,
    currency_for_market_scope,
    market_scope_from_market,
    split_batches_by_account_route,
)
from leaps_quant_engine.brokerage import BrokerEngineExecutionGateway, BrokerExecutionService, PaperBrokerExecutionGateway
from leaps_quant_engine.cycle_journal import CycleJournalEntry, FileCycleJournalStore
from leaps_quant_engine.control import FileRuntimeControlQueue, RuntimeControlCommand
from leaps_quant_engine.framework import FileFrameworkRunnerStateStore, FrameworkRunner
from leaps_quant_engine.fundamentals import (
    DEFAULT_FUNDAMENTAL_ARTIFACT_ROOT,
    FileFundamentalArtifactStore,
    FundamentalArtifact,
)
from leaps_quant_engine.logging import configure_logging
from leaps_quant_engine.indicators import IndicatorEngine
from leaps_quant_engine.kis_gateway import (
    DEFAULT_KIS_GATEWAY_HOST,
    DEFAULT_KIS_GATEWAY_PORT,
    KISGatewayService,
    fetch_kis_gateway_health,
    run_kis_gateway_http_server,
)
from leaps_quant_engine.live_snapshot import run_live_indicator_snapshot
from leaps_quant_engine.market_rules import synthetic_domestic_market_session, synthetic_us_market_session
from leaps_quant_engine.minute_feed import (
    KISCachedMinuteBarProvider,
    YFinanceMinuteBarProvider,
    build_minute_feed_cache,
    download_us_minute_feed,
    export_minute_feed_cache,
    load_minute_feed_cache_bars,
    yfinance_symbol_map_for_universe,
)
from leaps_quant_engine.models import OrderIntent
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.model_state_seed import (
    DEFAULT_TRAILING_STOP_MODEL_ID,
    DEFAULT_TRAILING_STOP_NAMESPACE,
    seed_trailing_stop_state_from_positions,
)
from leaps_quant_engine.notifications import (
    NotificationService,
    notify_order_submit_report,
    notify_order_supervisor_report,
)
from leaps_quant_engine.order_orchestrator import MultiSleeveOrderOrchestrator
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_smoke import OrderRuntimePaperSmokeRunner
from leaps_quant_engine.order_status import build_order_runtime_status
from leaps_quant_engine.order_submit import OrderRuntimeSubmitter, load_order_intent_batches, write_order_intent_batches
from leaps_quant_engine.order_supervisor import OrderMaintenancePolicy, OrderRuntimeSupervisor
from leaps_quant_engine.order_worker import ExecutionHistoryReconcileWorker, OpenTicketPollWorker
from leaps_quant_engine.operator_status import (
    DEFAULT_EOD_SCHEDULES,
    CashAvailabilityRouteInput,
    build_cash_availability_report,
    build_eod_snapshot_status,
)
from leaps_quant_engine.performance import build_sleeve_daily_performance_report
from leaps_quant_engine.portfolio import Portfolio, StaticPortfolioProvider
from leaps_quant_engine.runtime import build_indicator_engine_from_file, run_once_from_file
from leaps_quant_engine.runtime_bootstrap import RuntimeBootstrapDependencies, bootstrap_sleeve_runtime, resolve_runtime_path
from leaps_quant_engine.runtime_config import load_runtime_config_snapshot
from leaps_quant_engine.runtime_health import build_runtime_health_report
from leaps_quant_engine.runtime_integrity import build_runtime_code_identity
from leaps_quant_engine.runtime_multi import run_multi_sleeve_once
from leaps_quant_engine.runtime_preflight import build_runtime_preflight_report
from leaps_quant_engine.runtime_recovery import build_recovery_account_report, build_recovery_report
from leaps_quant_engine.runtime_state import InMemoryRuntimeStateStore, SQLiteRuntimeStateStore, fork_sqlite_runtime_state
from leaps_quant_engine.rl import train_ppo_portfolio_constructor
from leaps_quant_engine.security import SecurityCatalog, SymbolProperties, symbol_properties_from_metadata
from leaps_quant_engine.snapshot_worker import BackgroundSnapshotWorker
from leaps_quant_engine.temporal_features import temporal_feature_provider_from_portfolio_parameters
from leaps_quant_engine.sleeve_workspace import (
    describe_sleeve_alpha_modules,
    describe_sleeve_execution_model,
    describe_sleeve_portfolio_model,
    describe_sleeve_risk_model,
    disable_sleeve_alpha_module,
    enable_sleeve_alpha_module,
    set_sleeve_execution_model,
    set_sleeve_portfolio_model,
    set_sleeve_risk_model,
)
from leaps_quant_engine.universe.fine import FineUniverseRuntime
from leaps_quant_engine.universe.loader import load_universe_definition
from leaps_quant_engine.universe.runtime import UniverseSelectionRuntime
from leaps_quant_engine.universe.selection import (
    MomentumUniverseSelectionModel,
    StaticUniverseSelectionModel,
    UniverseSelectionContext,
)
from leaps_quant_engine.virtual_account import FillAllocation, VirtualSleeveAccountStore
from leaps_quant_engine.warmup import WarmupPolicy, run_daily_indicator_warmup


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leapsq")
    parser.add_argument("--log-level", default="WARNING")
    parser.add_argument("--log-file", type=Path)
    parser.add_argument("--log-json", action="store_true")
    parser.add_argument("--log-max-bytes", type=int, default=10_000_000)
    parser.add_argument("--log-backup-count", type=int, default=5)
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_once = subparsers.add_parser("run-once")
    run_once.add_argument("config", type=Path)

    runtime_config_validate = subparsers.add_parser("runtime-config-validate")
    runtime_config_validate.add_argument("config", type=Path)

    runtime_run_once = subparsers.add_parser("runtime-run-once")
    runtime_run_once.add_argument("config", type=Path)
    runtime_run_once.add_argument("--sleeve-id")
    runtime_run_once.add_argument("--held", action="append", default=[])
    runtime_run_once.add_argument("--open-order", action="append", default=[])
    runtime_run_once.add_argument("--exit-watch", action="append", default=[])
    runtime_run_once.add_argument("--manual", action="append", default=[])
    runtime_run_once.add_argument("--skip-fine-refresh", action="store_true")
    runtime_run_once.add_argument("--skip-warmup", action="store_true")
    runtime_run_once.add_argument("--order-batch-output", type=Path)
    runtime_run_once.add_argument("--journal", type=Path)
    runtime_run_once.add_argument("--framework-state", type=Path)
    runtime_run_once.add_argument("--framework-state-read-only", action="store_true")
    runtime_run_once.add_argument("--runtime-state", type=Path)
    runtime_run_once.add_argument("--runtime-state-read-only", action="store_true")
    runtime_run_once.add_argument("--summary-only", action="store_true")

    runtime_run_multi_once = subparsers.add_parser("runtime-run-multi-once")
    runtime_run_multi_once.add_argument("config", type=Path)
    runtime_run_multi_once.add_argument("--sleeve-id", action="append", default=[])
    runtime_run_multi_once.add_argument("--skip-fine-refresh", action="store_true")
    runtime_run_multi_once.add_argument("--skip-warmup", action="store_true")
    runtime_run_multi_once.add_argument("--order-batch-output", type=Path)
    runtime_run_multi_once.add_argument("--journal", type=Path)
    runtime_run_multi_once.add_argument("--framework-state-dir", type=Path)
    runtime_run_multi_once.add_argument("--framework-state-read-only", action="store_true")
    runtime_run_multi_once.add_argument("--runtime-state", type=Path)
    runtime_run_multi_once.add_argument("--runtime-state-read-only", action="store_true")
    runtime_run_multi_once.add_argument("--summary-only", action="store_true")

    runtime_state_seed = subparsers.add_parser("runtime-state-seed-trailing-stop")
    runtime_state_seed.add_argument("config", type=Path)
    runtime_state_seed.add_argument("--sleeve-id", required=True)
    runtime_state_seed.add_argument("--account-store", type=Path)
    runtime_state_seed.add_argument("--runtime-state", type=Path, required=True)
    runtime_state_seed.add_argument("--model-id", default=DEFAULT_TRAILING_STOP_MODEL_ID)
    runtime_state_seed.add_argument("--namespace", default=DEFAULT_TRAILING_STOP_NAMESPACE)
    runtime_state_seed.add_argument("--summary-only", action="store_true")

    runtime_state_fork = subparsers.add_parser("runtime-state-fork")
    runtime_state_fork.add_argument("--source", type=Path, required=True)
    runtime_state_fork.add_argument("--target", type=Path, required=True)
    runtime_state_fork.add_argument("--overwrite", action="store_true")

    runtime_control_submit = subparsers.add_parser("runtime-control-submit")
    runtime_control_submit.add_argument("--queue", type=Path, required=True)
    runtime_control_submit.add_argument(
        "--command",
        dest="control_command",
        choices=[
            "reload-config",
            "reload-sleeve",
            "activate-sleeve",
            "deactivate-sleeve",
            "suspend-sleeve",
            "resume-sleeve",
            "pause-worker",
            "resume-worker",
            "run-once",
            "shutdown",
        ],
        required=True,
    )
    runtime_control_submit.add_argument("--config", type=Path)
    runtime_control_submit.add_argument("--sleeve-id")
    runtime_control_submit.add_argument("--reason")

    runtime_control_drain = subparsers.add_parser("runtime-control-drain")
    runtime_control_drain.add_argument("--queue", type=Path, required=True)

    sleeve_alpha_list = subparsers.add_parser("sleeve-alpha-list")
    sleeve_alpha_list.add_argument("config", type=Path)
    sleeve_alpha_list.add_argument("--sleeve-id", required=True)

    sleeve_alpha_enable = subparsers.add_parser("sleeve-alpha-enable")
    sleeve_alpha_enable.add_argument("config", type=Path)
    sleeve_alpha_enable.add_argument("alpha_ref")
    sleeve_alpha_enable.add_argument("--sleeve-id", required=True)

    sleeve_alpha_disable = subparsers.add_parser("sleeve-alpha-disable")
    sleeve_alpha_disable.add_argument("config", type=Path)
    sleeve_alpha_disable.add_argument("alpha_ref")
    sleeve_alpha_disable.add_argument("--sleeve-id", required=True)

    sleeve_portfolio_list = subparsers.add_parser("sleeve-portfolio-list")
    sleeve_portfolio_list.add_argument("config", type=Path)
    sleeve_portfolio_list.add_argument("--sleeve-id", required=True)

    sleeve_portfolio_set = subparsers.add_parser("sleeve-portfolio-set")
    sleeve_portfolio_set.add_argument("config", type=Path)
    sleeve_portfolio_set.add_argument("portfolio_ref")
    sleeve_portfolio_set.add_argument("--sleeve-id", required=True)

    sleeve_risk_list = subparsers.add_parser("sleeve-risk-list")
    sleeve_risk_list.add_argument("config", type=Path)
    sleeve_risk_list.add_argument("--sleeve-id", required=True)

    sleeve_risk_set = subparsers.add_parser("sleeve-risk-set")
    sleeve_risk_set.add_argument("config", type=Path)
    sleeve_risk_set.add_argument("risk_ref")
    sleeve_risk_set.add_argument("--sleeve-id", required=True)

    sleeve_execution_list = subparsers.add_parser("sleeve-execution-list")
    sleeve_execution_list.add_argument("config", type=Path)
    sleeve_execution_list.add_argument("--sleeve-id", required=True)

    sleeve_execution_set = subparsers.add_parser("sleeve-execution-set")
    sleeve_execution_set.add_argument("config", type=Path)
    sleeve_execution_set.add_argument("execution_ref")
    sleeve_execution_set.add_argument("--sleeve-id", required=True)

    subparsers.add_parser("kis-health")

    kis_gateway_serve = subparsers.add_parser("kis-gateway-serve")
    kis_gateway_serve.add_argument("--host", default=DEFAULT_KIS_GATEWAY_HOST)
    kis_gateway_serve.add_argument("--port", type=int, default=DEFAULT_KIS_GATEWAY_PORT)
    kis_gateway_serve.add_argument("--cache-dir", type=Path)

    kis_gateway_health = subparsers.add_parser("kis-gateway-health")
    kis_gateway_health.add_argument("--base-url", default=f"http://{DEFAULT_KIS_GATEWAY_HOST}:{DEFAULT_KIS_GATEWAY_PORT}")
    kis_gateway_health.add_argument("--timeout-seconds", type=float, default=5.0)

    kis_quote = subparsers.add_parser("kis-quote")
    kis_quote.add_argument("symbol")
    kis_quote.add_argument("--market", default="KRX")

    kis_account_sync = subparsers.add_parser("kis-account-sync")
    kis_account_sync.add_argument("config", type=Path)
    kis_account_sync.add_argument("--sleeve-id", required=True)
    kis_account_sync.add_argument("--start-date", required=True)
    kis_account_sync.add_argument("--end-date", required=True)
    kis_account_sync.add_argument("--market", default="domestic", choices=("domestic", "overseas"))
    kis_account_sync.add_argument("--side", default="all", choices=("all", "buy", "sell"))
    kis_account_sync.add_argument("--symbol", default="")
    kis_account_sync.add_argument("--assign-unknown-to-sleeve", action="store_true")
    kis_account_sync.add_argument("--sync-cash", action="store_true")
    kis_account_sync.add_argument("--residual-sleeve-id", default="default sleeve")

    virtual_account_allocate = subparsers.add_parser("virtual-account-allocate-fill")
    virtual_account_allocate.add_argument("config", type=Path)
    virtual_account_allocate.add_argument("--sleeve-id", required=True)
    virtual_account_allocate.add_argument("--fill-id", required=True)
    virtual_account_allocate.add_argument("--allocation", action="append", required=True)
    virtual_account_allocate.add_argument("--reason", default="")

    virtual_account_ignore = subparsers.add_parser("virtual-account-ignore-fill")
    virtual_account_ignore.add_argument("config", type=Path)
    virtual_account_ignore.add_argument("--sleeve-id", required=True)
    virtual_account_ignore.add_argument("--market", default="domestic", choices=("domestic", "overseas"))
    virtual_account_ignore.add_argument("--fill-id", required=True)
    virtual_account_ignore.add_argument("--reason", required=True)
    virtual_account_ignore.add_argument("--ignored-by", default="operator")

    virtual_account_reconcile = subparsers.add_parser("virtual-account-reconcile")
    virtual_account_reconcile.add_argument("config", type=Path)
    virtual_account_reconcile.add_argument("--sleeve-id", required=True)
    virtual_account_reconcile.add_argument("--market", default="domestic", choices=("domestic", "overseas"))
    virtual_account_reconcile.add_argument("--summary-only", action="store_true")

    virtual_account_cash_sync = subparsers.add_parser("virtual-account-sync-cash")
    virtual_account_cash_sync.add_argument("config", type=Path)
    virtual_account_cash_sync.add_argument("--sleeve-id", required=True)
    virtual_account_cash_sync.add_argument("--currency")
    virtual_account_cash_sync.add_argument("--residual-sleeve-id", default="default sleeve")

    virtual_account_cash_transfer = subparsers.add_parser("virtual-account-transfer-cash")
    virtual_account_cash_transfer.add_argument("config", type=Path)
    virtual_account_cash_transfer.add_argument("--sleeve-id", required=True)
    virtual_account_cash_transfer.add_argument("--from-sleeve-id", required=True)
    virtual_account_cash_transfer.add_argument("--to-sleeve-id", required=True)
    virtual_account_cash_transfer.add_argument("--amount", type=float, required=True)
    virtual_account_cash_transfer.add_argument("--currency", default="KRW")
    virtual_account_cash_transfer.add_argument("--reason", default="")

    order_runtime_status = subparsers.add_parser("order-runtime-status")
    order_runtime_status.add_argument("config", type=Path)
    order_runtime_status.add_argument("--sleeve-id", action="append", default=[])
    order_runtime_status.add_argument("--account-id")
    order_runtime_status.add_argument("--account-store", type=Path)
    order_runtime_status.add_argument("--order-store", type=Path)
    order_runtime_status.add_argument("--recent-events", type=int, default=10)
    order_runtime_status.add_argument("--summary-only", action="store_true")

    order_runtime_submit = subparsers.add_parser("order-runtime-submit")
    order_runtime_submit.add_argument("config", type=Path)
    order_runtime_submit.add_argument("batch_file", type=Path)
    order_runtime_submit.add_argument("--sleeve-id", action="append", default=[])
    order_runtime_submit.add_argument("--account-id")
    order_runtime_submit.add_argument("--account-store", type=Path)
    order_runtime_submit.add_argument("--order-store", type=Path)
    order_runtime_submit.add_argument("--broker", default="paper", choices=("paper", "broker-engine"))
    order_runtime_submit.add_argument("--commit", action="store_true")
    order_runtime_submit.add_argument("--confirm-live-submit", action="store_true")
    order_runtime_submit.add_argument("--poll-after-submit", action="store_true")
    order_runtime_submit.add_argument("--paper-no-fill", action="store_true")
    order_runtime_submit.add_argument("--max-submit-notional", type=float)
    order_runtime_submit.add_argument("--max-submit-notional-by-account", action="append", default=[])
    order_runtime_submit.add_argument("--allow-symbol", action="append", default=[])
    order_runtime_submit.add_argument("--recent-events", type=int, default=10)
    order_runtime_submit.add_argument("--journal", type=Path)
    order_runtime_submit.add_argument("--notify", action="store_true")
    order_runtime_submit.add_argument("--notification-root", type=Path)
    order_runtime_submit.add_argument("--notify-chat-id")
    order_runtime_submit.add_argument("--notify-disable-notification", action="store_true")
    order_runtime_submit.add_argument("--summary-only", action="store_true")

    order_runtime_paper_smoke = subparsers.add_parser("order-runtime-paper-smoke")
    order_runtime_paper_smoke.add_argument("config", type=Path)
    order_runtime_paper_smoke.add_argument("batch_file", type=Path)
    order_runtime_paper_smoke.add_argument("--sleeve-id", action="append", default=[])
    order_runtime_paper_smoke.add_argument("--account-id")
    order_runtime_paper_smoke.add_argument("--account-store", type=Path)
    order_runtime_paper_smoke.add_argument("--order-store", type=Path)
    order_runtime_paper_smoke.add_argument("--paper-no-fill", action="store_true")
    order_runtime_paper_smoke.add_argument("--max-submit-notional", type=float)
    order_runtime_paper_smoke.add_argument("--allow-symbol", action="append", default=[])
    order_runtime_paper_smoke.add_argument("--recent-events", type=int, default=10)
    order_runtime_paper_smoke.add_argument("--journal", type=Path)
    order_runtime_paper_smoke.add_argument("--summary-only", action="store_true")

    order_runtime_supervise = subparsers.add_parser("order-runtime-supervise")
    order_runtime_supervise.add_argument("config", type=Path)
    order_runtime_supervise.add_argument("--sleeve-id", action="append", default=[])
    order_runtime_supervise.add_argument("--account-id")
    order_runtime_supervise.add_argument("--account-store", type=Path)
    order_runtime_supervise.add_argument("--order-store", type=Path)
    order_runtime_supervise.add_argument("--broker", default="broker-engine", choices=("broker-engine", "paper"))
    order_runtime_supervise.add_argument("--paper-no-fill", action="store_true")
    order_runtime_supervise.add_argument("--skip-poll", action="store_true")
    order_runtime_supervise.add_argument("--skip-reconcile", action="store_true")
    order_runtime_supervise.add_argument("--start-date")
    order_runtime_supervise.add_argument("--end-date")
    order_runtime_supervise.add_argument("--market", choices=("domestic", "overseas"))
    order_runtime_supervise.add_argument("--side", default="all", choices=("all", "buy", "sell"))
    order_runtime_supervise.add_argument("--symbol", default="")
    order_runtime_supervise.add_argument("--assign-unknown-to-sleeve-id")
    order_runtime_supervise.add_argument("--drop-unknown-fills", action="store_true")
    order_runtime_supervise.add_argument("--max-executions", type=int, default=500)
    order_runtime_supervise.add_argument("--skip-holdings-reconcile", action="store_true")
    order_runtime_supervise.add_argument("--stale-after-seconds", type=float, default=0.0)
    order_runtime_supervise.add_argument("--cancel-stale-open-tickets", action="store_true")
    order_runtime_supervise.add_argument("--keep-partially-filled-stale", action="store_true")
    order_runtime_supervise.add_argument("--expire-day-open-tickets", action="store_true")
    order_runtime_supervise.add_argument("--recent-events", type=int, default=10)
    order_runtime_supervise.add_argument("--journal", type=Path)
    order_runtime_supervise.add_argument("--notify", action="store_true")
    order_runtime_supervise.add_argument("--notification-root", type=Path)
    order_runtime_supervise.add_argument("--notify-chat-id")
    order_runtime_supervise.add_argument("--notify-disable-notification", action="store_true")
    order_runtime_supervise.add_argument("--summary-only", action="store_true")

    notification_status = subparsers.add_parser("notification-status")
    notification_status.add_argument("--root", type=Path)

    notification_fetch = subparsers.add_parser("notification-fetch-telegram-updates")
    notification_fetch.add_argument("--root", type=Path)
    notification_fetch.add_argument("--offset", type=int)
    notification_fetch.add_argument("--limit", type=int, default=20)
    notification_fetch.add_argument("--summary-only", action="store_true")

    notify_user_message = subparsers.add_parser("notify-user-message")
    notify_user_message.add_argument("--category", default="status")
    notify_user_message.add_argument("--title", required=True)
    notify_user_message.add_argument("--message")
    notify_user_message.add_argument("--message-file", type=Path)
    notify_user_message.add_argument("--message-stdin", action="store_true")
    notify_user_message.add_argument("--root", type=Path)
    notify_user_message.add_argument("--chat-id")
    notify_user_message.add_argument("--parse-mode", choices=("Markdown", "MarkdownV2", "HTML"))
    notify_user_message.add_argument("--disable-notification", action="store_true")
    notify_user_message.add_argument("--summary-only", action="store_true")

    runtime_recovery = subparsers.add_parser("runtime-recovery-status")
    runtime_recovery.add_argument("config", type=Path)
    runtime_recovery.add_argument("--sleeve-id", action="append", default=[])
    runtime_recovery.add_argument("--account-id")
    runtime_recovery.add_argument("--account-store", type=Path)
    runtime_recovery.add_argument("--order-store", type=Path)
    runtime_recovery.add_argument("--journal", type=Path)
    runtime_recovery.add_argument("--recent-events", type=int, default=10)
    runtime_recovery.add_argument("--summary-only", action="store_true")

    runtime_health = subparsers.add_parser("runtime-health")
    runtime_health.add_argument("config", type=Path)
    runtime_health.add_argument("--sleeve-id", action="append", default=[])
    runtime_health.add_argument("--account-id")
    runtime_health.add_argument("--account-store", type=Path)
    runtime_health.add_argument("--order-store", type=Path)
    runtime_health.add_argument("--journal", type=Path)
    runtime_health.add_argument("--runtime-state", type=Path)
    runtime_health.add_argument("--broker", default="paper", choices=("broker-engine", "paper"))
    runtime_health.add_argument("--max-cycle-age-seconds", type=float, default=300.0)
    runtime_health.add_argument("--max-open-ticket-age-seconds", type=float, default=600.0)
    runtime_health.add_argument("--heartbeat", type=Path)
    runtime_health.add_argument("--heartbeat-component")
    runtime_health.add_argument("--max-heartbeat-age-seconds", type=float, default=120.0)
    runtime_health.add_argument("--recent-events", type=int, default=10)
    runtime_health.add_argument("--summary-only", action="store_true")

    runtime_artifact_status = subparsers.add_parser("runtime-artifact-status")
    runtime_artifact_status.add_argument("config", type=Path)
    runtime_artifact_status.add_argument("--sleeve-id", action="append", default=[])
    runtime_artifact_status.add_argument("--active-only", action="store_true")
    runtime_artifact_status.add_argument(
        "--active-sleeves-path",
        type=Path,
        default=Path("data/runtime/live-order-loop/multi_sleeve_active_sleeves.json"),
    )
    runtime_artifact_status.add_argument("--control-queue", type=Path, default=Path("data/runtime/control/live.jsonl"))
    runtime_artifact_status.add_argument(
        "--framework-state-dir",
        type=Path,
        default=Path("data/runtime/framework-state/multi-sleeve"),
    )
    runtime_artifact_status.add_argument(
        "--order-batch",
        type=Path,
        default=Path("data/runtime/live-order-loop/multi_sleeve_candidate_orders.json"),
    )
    runtime_artifact_status.add_argument(
        "--live-loop-log",
        type=Path,
        default=Path("data/runtime/live-order-loop/multi_sleeve.log"),
    )
    runtime_artifact_status.add_argument(
        "--live-loop-heartbeat",
        type=Path,
        default=Path("data/runtime/live-order-loop/multi_sleeve_heartbeat.json"),
    )
    runtime_artifact_status.add_argument("--runtime-state", type=Path)
    runtime_artifact_status.add_argument(
        "--submit-state",
        type=Path,
        default=Path("data/runtime/live-order-loop/multi_sleeve_submit_state.json"),
    )
    runtime_artifact_status.add_argument(
        "--report-dir",
        type=Path,
        default=Path("data/runtime/portfolio-reports"),
    )
    runtime_artifact_status.add_argument("--eod-snapshot-root", type=Path, default=Path("data/eod-snapshots"))
    runtime_artifact_status.add_argument("--eod-state-dir", type=Path, default=Path("data/runtime/eod-snapshots"))
    runtime_artifact_status.add_argument(
        "--startup-status",
        type=Path,
        default=Path("data/runtime/startup/leaps_safe_start_live_stack_status.json"),
    )
    runtime_artifact_status.add_argument("--summary-only", action="store_true")

    operator_ui = subparsers.add_parser("operator-ui")
    operator_ui.add_argument("config", type=Path)
    operator_ui.add_argument("--sleeve-id", action="append", default=[])
    operator_ui.add_argument("--account-id")
    operator_ui.add_argument("--account-store", type=Path)
    operator_ui.add_argument("--order-store", type=Path)
    operator_ui.add_argument("--journal", type=Path)
    operator_ui.add_argument("--recent-events", type=int, default=10)
    operator_ui.add_argument("--host", default="127.0.0.1")
    operator_ui.add_argument("--port", type=int, default=8765)
    operator_ui.add_argument("--snapshot-only", action="store_true")

    runtime_preflight = subparsers.add_parser("runtime-preflight")
    runtime_preflight.add_argument("config", type=Path)
    runtime_preflight.add_argument("--sleeve-id", action="append", default=[])
    runtime_preflight.add_argument("--account-id")
    runtime_preflight.add_argument("--account-store", type=Path)
    runtime_preflight.add_argument("--order-store", type=Path)
    runtime_preflight.add_argument("--journal", type=Path)
    runtime_preflight.add_argument("--include-order-status", action="store_true")
    runtime_preflight.add_argument("--recent-events", type=int, default=10)
    runtime_preflight.add_argument("--strict-live", action="store_true")
    runtime_preflight.add_argument("--skip-bootstrap", action="store_true")
    runtime_preflight.add_argument("--summary-only", action="store_true")

    sleeve_cash_availability = subparsers.add_parser("sleeve-cash-availability")
    sleeve_cash_availability.add_argument("config", type=Path)
    sleeve_cash_availability.add_argument("--sleeve-id", action="append", default=[])
    sleeve_cash_availability.add_argument("--account-id")
    sleeve_cash_availability.add_argument("--account-store", type=Path)
    sleeve_cash_availability.add_argument("--order-store", type=Path)
    sleeve_cash_availability.add_argument("--residual-sleeve-id", default="default sleeve")
    sleeve_cash_availability.add_argument("--summary-only", action="store_true")

    eod_snapshot_status = subparsers.add_parser("eod-snapshot-status")
    eod_snapshot_status.add_argument("--snapshot-root", type=Path, default=Path("data/eod-snapshots"))
    eod_snapshot_status.add_argument("--state-dir", type=Path, default=Path("data/runtime/eod-snapshots"))
    eod_snapshot_status.add_argument("--schedule", action="append", default=[])
    eod_snapshot_status.add_argument("--summary-only", action="store_true")

    sleeve_daily_performance = subparsers.add_parser("sleeve-daily-performance")
    sleeve_daily_performance.add_argument("--snapshot-root", type=Path, default=Path("data/eod-snapshots"))
    sleeve_daily_performance.add_argument("--sleeve-id", action="append", default=[])
    sleeve_daily_performance.add_argument("--currency")
    sleeve_daily_performance.add_argument("--from-date")
    sleeve_daily_performance.add_argument("--to-date")
    sleeve_daily_performance.add_argument("--include-holdings", action="store_true")
    sleeve_daily_performance.add_argument("--summary-only", action="store_true")

    indicators_kis = subparsers.add_parser("indicators-kis-once")
    indicators_kis.add_argument("config", type=Path)
    indicators_kis.add_argument("--sleeve-id", required=True)
    indicators_kis.add_argument("--warmup-start")
    indicators_kis.add_argument("--warmup-end")

    indicators_backtest = subparsers.add_parser("indicators-backtest-once")
    indicators_backtest.add_argument("config", type=Path)
    indicators_backtest.add_argument("--sleeve-id", required=True)

    benchmark_indicators = subparsers.add_parser("benchmark-indicators-daily")
    benchmark_indicators.add_argument("universe", type=Path)
    benchmark_indicators.add_argument("--sleeve-id", required=True)
    benchmark_indicators.add_argument("--start")
    benchmark_indicators.add_argument("--end")
    benchmark_indicators.add_argument("--source", default="kis-cache", choices=("kis-cache",))
    benchmark_indicators.add_argument("--refresh-history", action="store_true")
    benchmark_indicators.add_argument("--include-daily", action="store_true")

    framework_backtest = subparsers.add_parser("framework-backtest-daily")
    framework_backtest.add_argument("universe", type=Path)
    framework_backtest.add_argument("alpha", type=Path)
    framework_backtest.add_argument("--sleeve-id", required=True)
    framework_backtest.add_argument("--start")
    framework_backtest.add_argument("--end")
    framework_backtest.add_argument("--warmup-start")
    framework_backtest.add_argument("--daily-bar-time")
    framework_backtest.add_argument("--cash", type=float, default=100_000.0)
    framework_backtest.add_argument("--slippage-bps", type=float, default=0.0)
    framework_backtest.add_argument("--fee-model", choices=("none", "kis"), default="none")
    framework_backtest.add_argument("--source", default="finance-datareader", choices=("finance-datareader", "kis-cache", "parquet-daily"))
    framework_backtest.add_argument("--refresh-history", action="store_true")
    framework_backtest.add_argument("--fundamentals-root", type=Path)
    framework_backtest.add_argument("--fundamentals-market")
    framework_backtest.add_argument("--fundamental-name", action="append", default=[])
    framework_backtest.add_argument("--journal", type=Path)
    framework_backtest.add_argument("--journal-mode", choices=("auto", "full", "light"), default="auto")
    framework_backtest.add_argument("--include-insights", action="store_true")
    framework_backtest.add_argument("--summary-only", action="store_true")

    runtime_backtest = subparsers.add_parser("runtime-backtest-daily")
    runtime_backtest.add_argument("config", type=Path)
    runtime_backtest.add_argument("--sleeve-id", required=True)
    runtime_backtest.add_argument("--start")
    runtime_backtest.add_argument("--end")
    runtime_backtest.add_argument("--warmup-start")
    runtime_backtest.add_argument("--daily-bar-time")
    runtime_backtest.add_argument("--cash", type=float)
    runtime_backtest.add_argument("--currency")
    runtime_backtest.add_argument("--slippage-bps", type=float, default=0.0)
    runtime_backtest.add_argument("--fee-model", choices=("none", "kis"), default="none")
    runtime_backtest.add_argument("--source", default="finance-datareader", choices=("finance-datareader", "kis-cache", "parquet-daily"))
    runtime_backtest.add_argument("--refresh-history", action="store_true")
    runtime_backtest.add_argument("--fundamentals-root", type=Path)
    runtime_backtest.add_argument("--fundamentals-market")
    runtime_backtest.add_argument("--fundamental-name", action="append", default=[])
    runtime_backtest.add_argument("--journal", type=Path)
    runtime_backtest.add_argument("--journal-mode", choices=("auto", "full", "light"), default="auto")
    runtime_backtest.add_argument("--include-insights", action="store_true")
    runtime_backtest.add_argument("--summary-only", action="store_true")

    runtime_minute_backtest = subparsers.add_parser("runtime-backtest-minute")
    runtime_minute_backtest.add_argument("config", type=Path)
    runtime_minute_backtest.add_argument("--sleeve-id", required=True)
    runtime_minute_backtest.add_argument("--minute-feed", type=Path)
    runtime_minute_backtest.add_argument("--minute-cache-root", type=Path)
    runtime_minute_backtest.add_argument("--compiled-replay-cache", type=Path)
    runtime_minute_backtest.add_argument("--refresh-compiled-replay-cache", action="store_true")
    runtime_minute_backtest.add_argument("--start")
    runtime_minute_backtest.add_argument("--end")
    runtime_minute_backtest.add_argument("--warmup-start")
    runtime_minute_backtest.add_argument("--cash", type=float)
    runtime_minute_backtest.add_argument("--currency")
    runtime_minute_backtest.add_argument("--slippage-bps", type=float, default=0.0)
    runtime_minute_backtest.add_argument("--fee-model", choices=("none", "kis"), default="none")
    runtime_minute_backtest.add_argument("--daily-source", default="finance-datareader", choices=("finance-datareader", "kis-cache", "parquet-daily"))
    runtime_minute_backtest.add_argument("--refresh-history", action="store_true")
    runtime_minute_backtest.add_argument("--daily-warmup-cache", type=Path)
    runtime_minute_backtest.add_argument("--refresh-daily-warmup-cache", action="store_true")
    runtime_minute_backtest.add_argument("--fundamentals-root", type=Path)
    runtime_minute_backtest.add_argument("--fundamentals-market")
    runtime_minute_backtest.add_argument("--fundamental-name", action="append", default=[])
    runtime_minute_backtest.add_argument("--journal", type=Path)
    runtime_minute_backtest.add_argument("--journal-mode", choices=("auto", "full", "light"), default="auto")
    runtime_minute_backtest.add_argument("--include-insights", action="store_true")
    runtime_minute_backtest.add_argument("--summary-only", action="store_true")

    us_minute_feed = subparsers.add_parser("download-us-minute-feed")
    us_minute_feed.add_argument("source", type=Path)
    us_minute_feed.add_argument("--sleeve-id")
    us_minute_feed.add_argument("--output", type=Path, required=True)
    us_minute_feed.add_argument("--start", required=True)
    us_minute_feed.add_argument("--end", required=True)
    us_minute_feed.add_argument("--interval", default="1m")
    us_minute_feed.add_argument("--provider", choices=("yfinance", "kis-cache"), default="yfinance")
    us_minute_feed.add_argument("--timezone", default="America/New_York")
    us_minute_feed.add_argument("--include-prepost", action="store_true")
    us_minute_feed.add_argument("--include-session-metadata", action="store_true")
    us_minute_feed.add_argument("--symbol", action="append", default=[])
    us_minute_feed.add_argument("--max-symbols", type=int)
    us_minute_feed.add_argument("--sleep-seconds", type=float, default=0.0)
    us_minute_feed.add_argument("--overwrite", action="store_true")
    us_minute_feed.add_argument("--summary-only", action="store_true")

    minute_cache_build = subparsers.add_parser("minute-cache-build")
    minute_cache_build.add_argument("source", type=Path)
    minute_cache_build.add_argument("--sleeve-id")
    minute_cache_build.add_argument("--cache-root", type=Path, default=Path("data/replay/minute-cache"))
    minute_cache_build.add_argument("--start", required=True)
    minute_cache_build.add_argument("--end", required=True)
    minute_cache_build.add_argument("--interval", default="1m")
    minute_cache_build.add_argument("--provider", choices=("yfinance", "kis-cache"), default="yfinance")
    minute_cache_build.add_argument("--timezone")
    minute_cache_build.add_argument("--include-prepost", action="store_true")
    minute_cache_build.add_argument("--include-extended-hours", action="store_true")
    minute_cache_build.add_argument("--include-session-metadata", action="store_true")
    minute_cache_build.add_argument("--refresh-provider-cache", action="store_true")
    minute_cache_build.add_argument("--symbol", action="append", default=[])
    minute_cache_build.add_argument("--max-symbols", type=int)
    minute_cache_build.add_argument("--sleep-seconds", type=float, default=0.0)
    minute_cache_build.add_argument("--overwrite", action="store_true")
    minute_cache_build.add_argument("--uncompressed", action="store_true")
    minute_cache_build.add_argument("--summary-only", action="store_true")

    minute_cache_export = subparsers.add_parser("minute-cache-export")
    minute_cache_export.add_argument("source", type=Path)
    minute_cache_export.add_argument("--sleeve-id")
    minute_cache_export.add_argument("--cache-root", type=Path, default=Path("data/replay/minute-cache"))
    minute_cache_export.add_argument("--output", type=Path, required=True)
    minute_cache_export.add_argument("--start", required=True)
    minute_cache_export.add_argument("--end", required=True)
    minute_cache_export.add_argument("--symbol", action="append", default=[])
    minute_cache_export.add_argument("--max-symbols", type=int)
    minute_cache_export.add_argument("--include-session-metadata", action="store_true")
    minute_cache_export.add_argument("--overwrite", action="store_true")
    minute_cache_export.add_argument("--summary-only", action="store_true")

    train_rl_constructor = subparsers.add_parser("train-rl-portfolio-constructor")
    train_rl_constructor.add_argument("config", type=Path)
    train_rl_constructor.add_argument("--sleeve-id", required=True)
    train_rl_constructor.add_argument("--start")
    train_rl_constructor.add_argument("--end")
    train_rl_constructor.add_argument("--source", default="finance-datareader", choices=("finance-datareader", "kis-cache", "parquet-daily"))
    train_rl_constructor.add_argument("--timesteps", type=int, default=10_000)
    train_rl_constructor.add_argument("--seed", type=int, default=7)
    train_rl_constructor.add_argument("--ensemble-seed", type=int, action="append", default=[])
    train_rl_constructor.add_argument("--output-dir", type=Path, default=Path("data/rl"))
    train_rl_constructor.add_argument("--training-cash", type=float, default=5_000_000.0)
    train_rl_constructor.add_argument("--turnover-penalty", type=float)
    train_rl_constructor.add_argument("--downside-penalty", type=float)
    train_rl_constructor.add_argument("--volatility-penalty", type=float)
    train_rl_constructor.add_argument("--drawdown-penalty", type=float)
    train_rl_constructor.add_argument("--underwater-penalty", type=float)
    train_rl_constructor.add_argument("--missed-upside-penalty", type=float)
    train_rl_constructor.add_argument("--concentration-penalty", type=float)
    train_rl_constructor.add_argument("--summary-only", action="store_true")

    fundamentals_import = subparsers.add_parser("fundamentals-import-fdr")
    fundamentals_import.add_argument("--root", type=Path, default=DEFAULT_FUNDAMENTAL_ARTIFACT_ROOT)
    fundamentals_import.add_argument("--market")
    fundamentals_import.add_argument("--universe", type=Path)
    fundamentals_import.add_argument("--as-of")
    fundamentals_import.add_argument("--symbol", action="append", default=[])
    fundamentals_import.add_argument("--name", action="append", default=[])
    fundamentals_import.add_argument("--include-naver-valuation", action="store_true")
    fundamentals_import.add_argument("--overwrite", action="store_true")
    fundamentals_import.add_argument("--summary-only", action="store_true")

    fundamentals_status = subparsers.add_parser("fundamentals-status")
    fundamentals_status.add_argument("--root", type=Path, default=DEFAULT_FUNDAMENTAL_ARTIFACT_ROOT)
    fundamentals_status.add_argument("--market")
    fundamentals_status.add_argument("--as-of")
    fundamentals_status.add_argument("--include-artifacts", action="store_true")
    fundamentals_status.add_argument("--summary-only", action="store_true")

    warmup_indicators = subparsers.add_parser("warmup-indicators-daily")
    warmup_indicators.add_argument("universe", type=Path)
    warmup_indicators.add_argument("--sleeve-id", required=True)
    warmup_indicators.add_argument("--start")
    warmup_indicators.add_argument("--end")
    warmup_indicators.add_argument("--source", default="kis-cache", choices=("kis-cache",))
    warmup_indicators.add_argument("--refresh-history", action="store_true")
    warmup_indicators.add_argument("--extra-bars", type=int, default=0)
    warmup_indicators.add_argument("--min-ready-ratio", type=float, default=1.0)
    warmup_indicators.add_argument("--summary-only", action="store_true")

    select_universe = subparsers.add_parser("select-active-universe")
    select_universe.add_argument("universe", type=Path)
    select_universe.add_argument("--sleeve-id", required=True)
    select_universe.add_argument("--top-n", type=int, default=60)
    select_universe.add_argument("--start")
    select_universe.add_argument("--end")
    select_universe.add_argument("--source", default="kis-cache", choices=("kis-cache",))
    select_universe.add_argument("--refresh-history", action="store_true")
    select_universe.add_argument("--extra-bars", type=int, default=0)
    select_universe.add_argument("--min-ready-ratio", type=float, default=0.99)
    select_universe.add_argument("--price-indicator", default="identity_close")
    select_universe.add_argument("--moving-average-indicator", default="sma_5_close")
    select_universe.add_argument("--momentum-indicator", default="momentum_5_close")
    select_universe.add_argument("--liquidity-indicator", default="rolling_dollar_volume_20")
    select_universe.add_argument("--volatility-indicator", default="stddev_20_close")
    select_universe.add_argument("--require-price-above-average", action="store_true")
    select_universe.add_argument("--allow-negative-momentum", action="store_true")
    select_universe.add_argument("--min-liquidity", type=float)
    select_universe.add_argument("--max-volatility", type=float)
    select_universe.add_argument("--previous-live", action="append", default=[])
    select_universe.add_argument("--held", action="append", default=[])
    select_universe.add_argument("--open-order", action="append", default=[])
    select_universe.add_argument("--exit-watch", action="append", default=[])
    select_universe.add_argument("--manual", action="append", default=[])
    select_universe.add_argument("--summary-only", action="store_true")

    live_indicators = subparsers.add_parser("live-indicators-once")
    live_indicators.add_argument("universe", type=Path)
    live_indicators.add_argument("--sleeve-id", required=True)
    live_indicators.add_argument("--source", default="market-data-engine", choices=("market-data-engine",))
    live_indicators.add_argument("--min-success", type=int)
    live_indicators.add_argument("--rate-limit-per-second", type=int)
    live_indicators.add_argument("--include-failures", action="store_true")

    fine_refresh = subparsers.add_parser("fine-universe-refresh")
    fine_refresh.add_argument("universe", type=Path)
    fine_refresh.add_argument("--source", default="market-data-engine", choices=("market-data-engine",))
    fine_refresh.add_argument("--rate-limit-per-second", type=int)
    fine_refresh.add_argument("--max-symbols", type=int)
    fine_refresh.add_argument("--min-success", type=int)
    fine_refresh.add_argument("--max-age-seconds", type=float, default=300.0)
    fine_refresh.add_argument("--include-entries", action="store_true")

    snapshot_worker = subparsers.add_parser("snapshot-worker-run")
    snapshot_worker.add_argument("universe", type=Path)
    snapshot_worker.add_argument("--sleeve-id", required=True)
    snapshot_worker.add_argument("--source", default="market-data-engine", choices=("market-data-engine",))
    snapshot_worker.add_argument("--history-source", default="kis-cache", choices=("kis-cache",))
    snapshot_worker.add_argument("--cycles", type=int, default=1)
    snapshot_worker.add_argument("--interval-seconds", type=float, default=60.0)
    snapshot_worker.add_argument("--min-success", type=int)
    snapshot_worker.add_argument("--rate-limit-per-second", type=int)
    snapshot_worker.add_argument("--skip-warmup", action="store_true")
    snapshot_worker.add_argument("--warmup-start")
    snapshot_worker.add_argument("--warmup-end")
    snapshot_worker.add_argument("--refresh-history", action="store_true")
    snapshot_worker.add_argument("--extra-bars", type=int, default=0)
    snapshot_worker.add_argument("--min-ready-ratio", type=float, default=1.0)
    snapshot_worker.add_argument("--summary-only", action="store_true")

    active_snapshot_worker = subparsers.add_parser("active-snapshot-worker-run")
    active_snapshot_worker.add_argument("universe", type=Path)
    active_snapshot_worker.add_argument("--sleeve-id", required=True)
    active_snapshot_worker.add_argument("--selection", choices=("static", "momentum"), default="static")
    active_snapshot_worker.add_argument("--top-n", type=int, default=60)
    active_snapshot_worker.add_argument("--fine-refresh", action="store_true")
    active_snapshot_worker.add_argument("--fine-max-symbols", type=int)
    active_snapshot_worker.add_argument("--fine-min-success", type=int)
    active_snapshot_worker.add_argument("--fine-max-age-seconds", type=float, default=300.0)
    active_snapshot_worker.add_argument("--source", default="market-data-engine", choices=("market-data-engine",))
    active_snapshot_worker.add_argument("--history-source", default="kis-cache", choices=("kis-cache",))
    active_snapshot_worker.add_argument("--cycles", type=int, default=1)
    active_snapshot_worker.add_argument("--interval-seconds", type=float, default=60.0)
    active_snapshot_worker.add_argument("--min-success", type=int)
    active_snapshot_worker.add_argument("--rate-limit-per-second", type=int)
    active_snapshot_worker.add_argument("--skip-worker-warmup", action="store_true")
    active_snapshot_worker.add_argument("--start")
    active_snapshot_worker.add_argument("--end")
    active_snapshot_worker.add_argument("--refresh-history", action="store_true")
    active_snapshot_worker.add_argument("--extra-bars", type=int, default=0)
    active_snapshot_worker.add_argument("--min-ready-ratio", type=float, default=0.99)
    active_snapshot_worker.add_argument("--held", action="append", default=[])
    active_snapshot_worker.add_argument("--open-order", action="append", default=[])
    active_snapshot_worker.add_argument("--exit-watch", action="append", default=[])
    active_snapshot_worker.add_argument("--manual", action="append", default=[])
    active_snapshot_worker.add_argument("--summary-only", action="store_true")

    alpha_run = subparsers.add_parser("alpha-run-snapshot")
    alpha_run.add_argument("universe", type=Path)
    alpha_run.add_argument("alpha", type=Path)
    alpha_run.add_argument("--sleeve-id", required=True)
    alpha_run.add_argument("--source", default="market-data-engine", choices=("market-data-engine",))
    alpha_run.add_argument("--history-source", default="kis-cache", choices=("kis-cache",))
    alpha_run.add_argument("--min-success", type=int)
    alpha_run.add_argument("--rate-limit-per-second", type=int)
    alpha_run.add_argument("--skip-warmup", action="store_true")
    alpha_run.add_argument("--warmup-start")
    alpha_run.add_argument("--warmup-end")
    alpha_run.add_argument("--refresh-history", action="store_true")
    alpha_run.add_argument("--extra-bars", type=int, default=0)
    alpha_run.add_argument("--min-ready-ratio", type=float, default=1.0)
    alpha_run.add_argument("--summary-only", action="store_true")

    args = parser.parse_args(argv)
    configure_logging(
        level=args.log_level,
        log_file=args.log_file,
        json_logs=args.log_json,
        max_bytes=args.log_max_bytes,
        backup_count=args.log_backup_count,
    )
    if args.command == "run-once":
        orders = run_once_from_file(args.config, time=datetime(2026, 5, 7, 9, 0))
        print(json.dumps([_order_to_json(order) for order in orders], ensure_ascii=False, indent=2))
        return 0
    if args.command == "runtime-config-validate":
        snapshot = load_runtime_config_snapshot(args.config)
        print(json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "runtime-run-once":
        snapshot = load_runtime_config_snapshot(args.config)
        default_market = _default_market_from_runtime_snapshot(snapshot, args.sleeve_id)
        runtime_state_store, runtime_state_summary = _runtime_state_store_from_args(
            snapshot,
            args.runtime_state,
            read_only=bool(args.runtime_state_read_only),
        )
        runtime = bootstrap_sleeve_runtime(
            snapshot,
            args.sleeve_id,
            dependencies=RuntimeBootstrapDependencies(
                runtime_state_store=runtime_state_store,
                runtime_state_commit_enabled=not bool(args.runtime_state_read_only),
            ),
            refresh_fine=not args.skip_fine_refresh,
            held_symbols=_parse_symbol_refs(args.held, default_market),
            open_order_symbols=_parse_symbol_refs(args.open_order, default_market),
            exit_watch_symbols=_parse_symbol_refs(args.exit_watch, default_market),
            manual_symbols=_parse_symbol_refs(args.manual, default_market),
            preselect_warmup=False if args.skip_warmup else None,
        )
        framework_state_store = None
        framework_state_summary = None
        if args.framework_state is not None:
            framework_state_path = resolve_runtime_path(snapshot, args.framework_state)
            framework_state_store = FileFrameworkRunnerStateStore(framework_state_path)
            restored_state = framework_state_store.load()
            runtime.framework_runner.restore_state(restored_state)
            framework_state_summary = {
                "path": str(framework_state_path.resolve()),
                "restored": restored_state is not None,
                "read_only": bool(args.framework_state_read_only),
            }
        report = runtime.run_once(warmup=False if args.skip_warmup else None)
        if framework_state_store is not None and not args.framework_state_read_only:
            state_as_of = report.framework.new_insight_batch.generated_at if report.framework is not None else datetime.now()
            framework_state_store.save(runtime.framework_runner.export_state(as_of=state_as_of))
            if framework_state_summary is not None:
                framework_state_summary["saved"] = True
        journal_summary = None
        journal_path = _runtime_journal_path(snapshot, args.journal)
        if journal_path is not None:
            market_scope = market_scope_from_market(default_market)
            account_id = runtime.sleeve_config.broker_account_routes.get(
                market_scope,
                runtime.sleeve_config.broker_account_id,
            )
            entry = CycleJournalEntry.from_runtime_run_once_report(
                report,
                account_id=account_id,
                route_id=account_id,
                market_scope=market_scope,
            )
            entry = _with_runtime_code_identity_metadata(snapshot, entry, (report.sleeve_id,))
            FileCycleJournalStore(journal_path).append(entry)
            journal_summary = {"path": str(journal_path.resolve()), "entry_id": entry.entry_id}
        order_batch_artifact = None
        if args.order_batch_output is not None:
            batches = (report.framework.execution_batch,) if report.framework is not None else ()
            order_batch_artifact = write_order_intent_batches(
                resolve_runtime_path(snapshot, args.order_batch_output),
                batches,
                runtime_id=report.runtime_id,
                config_version=report.config_version,
                source="runtime-run-once",
            )
            order_batch_artifact["path"] = str(Path(order_batch_artifact["path"]).resolve())
            order_batch_artifact["submit_command"] = [
                "order-runtime-submit",
                str(args.config),
                order_batch_artifact["path"],
            ]
        print_payload = report.to_dict(
            include_candidates=not args.summary_only,
            include_warmup_symbols=not args.summary_only,
            include_failures=not args.summary_only,
            include_framework_details=not args.summary_only,
        )
        if order_batch_artifact is not None:
            print_payload["order_batch_artifact"] = order_batch_artifact
        if journal_summary is not None:
            print_payload["cycle_journal"] = journal_summary
        if framework_state_summary is not None:
            print_payload["framework_state"] = framework_state_summary
        if runtime_state_summary is not None:
            print_payload["runtime_state"] = runtime_state_summary
        print(
            json.dumps(
                print_payload,
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "runtime-run-multi-once":
        if not args.sleeve_id:
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": "runtime-run-multi-once requires at least one --sleeve-id.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_ids = _status_sleeve_ids(snapshot, args.sleeve_id)
        runtime_state_store, runtime_state_summary = _runtime_state_store_from_args(
            snapshot,
            args.runtime_state,
            read_only=bool(args.runtime_state_read_only),
        )
        framework_state_dir = (
            resolve_runtime_path(snapshot, args.framework_state_dir).resolve()
            if args.framework_state_dir is not None
            else None
        )
        report = run_multi_sleeve_once(
            snapshot,
            sleeve_ids,
            refresh_fine=not args.skip_fine_refresh,
            warmup=False if args.skip_warmup else None,
            framework_state_dir=framework_state_dir,
            framework_state_read_only=args.framework_state_read_only,
            dependencies=RuntimeBootstrapDependencies(
                runtime_state_store=runtime_state_store,
                runtime_state_commit_enabled=not bool(args.runtime_state_read_only),
            ),
        )
        journal_summary = None
        journal_path = _runtime_journal_path(snapshot, args.journal)
        if journal_path is not None:
            store = FileCycleJournalStore(journal_path)
            entries = []
            for sleeve_report in report.reports:
                runtime_sleeve = snapshot.config.sleeve(sleeve_report.sleeve_id)
                market_scope = market_scope_from_market(_default_market_from_runtime_snapshot(snapshot, sleeve_report.sleeve_id))
                account_id = runtime_sleeve.broker_account_routes.get(
                    market_scope,
                    runtime_sleeve.broker_account_id,
                )
                entry = CycleJournalEntry.from_runtime_run_once_report(
                    sleeve_report,
                    account_id=account_id,
                    route_id=account_id,
                    market_scope=market_scope,
                    source="runtime-run-multi-once",
                )
                entry = _with_runtime_code_identity_metadata(snapshot, entry, (sleeve_report.sleeve_id,))
                store.append(entry)
                entries.append(entry.entry_id)
            journal_summary = {"path": str(journal_path.resolve()), "entry_ids": entries}
        order_batch_artifact = None
        if args.order_batch_output is not None:
            order_batch_artifact = write_order_intent_batches(
                resolve_runtime_path(snapshot, args.order_batch_output),
                report.execution_batches(),
                runtime_id=report.runtime_id,
                config_version=report.config_version,
                source="runtime-run-multi-once",
            )
            order_batch_artifact["path"] = str(Path(order_batch_artifact["path"]).resolve())
            order_batch_artifact["submit_command"] = [
                "order-runtime-submit",
                str(args.config),
                order_batch_artifact["path"],
            ]
            for sleeve_id in sleeve_ids:
                order_batch_artifact["submit_command"].extend(["--sleeve-id", sleeve_id])
        print_payload = report.to_dict(
            include_candidates=not args.summary_only,
            include_warmup_symbols=not args.summary_only,
            include_failures=not args.summary_only,
            include_framework_details=not args.summary_only,
        )
        if order_batch_artifact is not None:
            print_payload["order_batch_artifact"] = order_batch_artifact
        if journal_summary is not None:
            print_payload["cycle_journal"] = journal_summary
        if runtime_state_summary is not None:
            print_payload["runtime_state"] = runtime_state_summary
        print(json.dumps(print_payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "runtime-state-seed-trailing-stop":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        account_store_path = (
            resolve_runtime_path(snapshot, args.account_store).resolve()
            if args.account_store is not None
            else _resolve_sleeve_account_store_path(snapshot, sleeve_config)
        )
        runtime_state_path = resolve_runtime_path(snapshot, args.runtime_state).resolve()
        account_store = VirtualSleeveAccountStore(account_store_path)
        runtime_state_store = SQLiteRuntimeStateStore(runtime_state_path)
        report = seed_trailing_stop_state_from_positions(
            account_store.position_states(args.sleeve_id),
            runtime_state_store,
            sleeve_id=args.sleeve_id,
            model_id=args.model_id,
            namespace=args.namespace,
        )
        payload = report.to_dict(include_rows=not args.summary_only)
        payload["account_store_path"] = str(account_store_path)
        payload["runtime_state_path"] = str(runtime_state_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "runtime-state-fork":
        report = fork_sqlite_runtime_state(args.source, args.target, overwrite=args.overwrite)
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "runtime-control-submit":
        command = _build_runtime_control_command(args)
        FileRuntimeControlQueue(args.queue).submit(command)
        print(json.dumps(command.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "runtime-control-drain":
        commands = FileRuntimeControlQueue(args.queue).drain()
        print(
            json.dumps(
                {"queue": str(args.queue), "command_count": len(commands), "commands": [command.to_dict() for command in commands]},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "sleeve-alpha-list":
        print(
            json.dumps(
                describe_sleeve_alpha_modules(args.config, args.sleeve_id),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "sleeve-alpha-enable":
        print(
            json.dumps(
                enable_sleeve_alpha_module(args.config, args.sleeve_id, args.alpha_ref),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "sleeve-alpha-disable":
        print(
            json.dumps(
                disable_sleeve_alpha_module(args.config, args.sleeve_id, args.alpha_ref),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "sleeve-portfolio-list":
        print(
            json.dumps(
                describe_sleeve_portfolio_model(args.config, args.sleeve_id),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "sleeve-portfolio-set":
        print(
            json.dumps(
                set_sleeve_portfolio_model(args.config, args.sleeve_id, args.portfolio_ref),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "sleeve-risk-list":
        print(
            json.dumps(
                describe_sleeve_risk_model(args.config, args.sleeve_id),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "sleeve-risk-set":
        print(
            json.dumps(
                set_sleeve_risk_model(args.config, args.sleeve_id, args.risk_ref),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "sleeve-execution-list":
        print(
            json.dumps(
                describe_sleeve_execution_model(args.config, args.sleeve_id),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "sleeve-execution-set":
        print(
            json.dumps(
                set_sleeve_execution_model(args.config, args.sleeve_id, args.execution_ref),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "kis-health":
        provider = KISBrokerEngineMarketDataProvider.from_env()
        print(json.dumps(provider.health_check(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "kis-gateway-serve":
        service = KISGatewayService.from_env(cache_dir=args.cache_dir)
        run_kis_gateway_http_server(service, host=args.host, port=args.port)
        return 0
    if args.command == "kis-gateway-health":
        print(
            json.dumps(
                fetch_kis_gateway_health(args.base_url, timeout_seconds=args.timeout_seconds),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "kis-quote":
        provider = KISBrokerEngineMarketDataProvider.from_env()
        bar = provider.get_latest_bar(Symbol(args.symbol, args.market))
        print(
            json.dumps(
                {
                    "symbol": bar.symbol.ticker,
                    "market": bar.symbol.market,
                    "time": bar.time.isoformat(),
                    "close": bar.close,
                    "volume": bar.volume,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "kis-account-sync":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        account = _broker_account_for_sleeve_market(snapshot, sleeve_config, args.market)
        currency = account.currency if account is not None else currency_for_market_scope(args.market)
        default_cash_by_sleeve = _default_cash_by_sleeve(
            snapshot,
            currency,
            account.account_id if account is not None else getattr(sleeve_config, "broker_account_id", None),
        )
        store = VirtualSleeveAccountStore(
            _resolve_sleeve_account_store_path(snapshot, sleeve_config, market_scope=args.market),
            default_cash_by_sleeve=default_cash_by_sleeve,
            default_currency=currency,
        )
        account_metadata = dict(account.metadata) if account is not None else {}
        try:
            sync = KISVirtualAccountSync.from_env(
                account.account_id if account is not None else None,
                metadata=account_metadata,
            )
        except TypeError:
            sync = KISVirtualAccountSync.from_env()
        report = sync.sync(
            store,
            start_date=args.start_date,
            end_date=args.end_date,
            market=args.market,
            side=args.side,
            symbol=args.symbol,
            assign_unknown_to_sleeve_id=args.sleeve_id if args.assign_unknown_to_sleeve else None,
            sync_cash=args.sync_cash,
            residual_sleeve_id=args.residual_sleeve_id,
            report_sleeve_ids=(args.sleeve_id,),
        )
        payload = report.to_dict()
        payload["account_store_path"] = str(store.path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "virtual-account-sync-cash":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        account = _broker_account_for_order_runtime(snapshot, None, (args.sleeve_id,))
        account_id = account.account_id if account is not None else getattr(sleeve_config, "broker_account_id", None)
        market = account.market_scope if account is not None else "domestic"
        currency = args.currency or (account.currency if account is not None else _sleeve_default_currency(snapshot, sleeve_config))
        account_store_path = (
            resolve_runtime_path(snapshot, account.account_store_path).resolve()
            if account is not None
            else _resolve_sleeve_account_store_path(snapshot, sleeve_config, market_scope=market)
        )
        account_metadata = dict(account.metadata) if account is not None else {}
        default_cash_by_sleeve = _default_cash_by_sleeve(snapshot, currency, account_id)
        store = VirtualSleeveAccountStore(
            account_store_path,
            default_cash_by_sleeve=default_cash_by_sleeve,
            default_currency=currency,
        )
        try:
            sync = KISVirtualAccountSync.from_env(account_id, metadata=account_metadata)
        except TypeError:
            sync = KISVirtualAccountSync.from_env()
        try:
            balance = sync.account_client.get_balance_summary(market=market)
        except TypeError:
            balance = sync.account_client.get_balance_summary()
        report = store.sync_account_cash(
            balance,
            account_id=account_id or "default",
            currency=currency,
            residual_sleeve_id=args.residual_sleeve_id,
        )
        payload = report.to_dict()
        payload["account_store_path"] = str(store.path)
        payload["market"] = market
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "virtual-account-transfer-cash":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        default_cash_by_sleeve = _default_cash_by_sleeve(snapshot, args.currency)
        store = VirtualSleeveAccountStore(
            _resolve_sleeve_account_store_path(snapshot, sleeve_config),
            default_cash_by_sleeve=default_cash_by_sleeve,
            default_currency=args.currency,
        )
        event = store.transfer_cash(
            from_sleeve_id=args.from_sleeve_id,
            to_sleeve_id=args.to_sleeve_id,
            amount=args.amount,
            currency=args.currency,
            reason=args.reason,
        )
        print(
            json.dumps(
                {
                    "transfer": event.to_dict(),
                    "account_store_path": str(store.path),
                    "from_portfolio": _portfolio_to_json(store.current_portfolio(args.from_sleeve_id)),
                    "to_portfolio": _portfolio_to_json(store.current_portfolio(args.to_sleeve_id)),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "order-runtime-status":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_ids = _status_sleeve_ids(snapshot, args.sleeve_id)
        routes = _resolve_order_runtime_routes(snapshot, args.account_id, args.account_store, args.order_store, sleeve_ids)
        reports = tuple(
            _build_order_runtime_status_for_route(
                snapshot,
                route,
                sleeve_ids,
                recent_events=args.recent_events,
            )
            for route in routes
        )
        if len(reports) == 1:
            print(json.dumps(reports[0].to_dict(include_details=not args.summary_only), ensure_ascii=False, indent=2))
        else:
            print(
                json.dumps(
                    _multi_route_payload(snapshot, reports, include_details=not args.summary_only),
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    if args.command == "order-runtime-submit":
        if args.confirm_live_submit and args.broker != "broker-engine":
            print(
                json.dumps(
                    {
                        "status": "error",
                        "error": "--confirm-live-submit requires --broker broker-engine.",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                file=sys.stderr,
            )
            return 2
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_ids = _status_sleeve_ids(snapshot, args.sleeve_id)
        batches = load_order_intent_batches(resolve_runtime_path(snapshot, args.batch_file))
        journal_store = _runtime_journal_store(snapshot, args.journal)
        routed_batches = _routed_batches_for_submit(
            snapshot,
            batches,
            sleeve_ids,
            args.account_id,
            args.account_store,
            args.order_store,
        )
        if len(routed_batches) > 1:
            reports = []
            notifications = []
            for route, route_batches in routed_batches:
                report = _run_order_runtime_submit_for_route(
                    snapshot,
                    args,
                    route,
                    sleeve_ids,
                    route_batches,
                )
                _append_order_report_journal(
                    journal_store,
                    snapshot=snapshot,
                    sleeve_ids=sleeve_ids,
                    route=route,
                    source="order-runtime-submit",
                    generated_at=report.generated_at,
                    status=report.status,
                    counts={
                        "batch_count": report.batch_count,
                        "order_count": report.order_count,
                        "ticket_count": len(report.coordination.tickets),
                        "event_count": len(report.coordination.events),
                    },
                    errors=report.errors,
                    warnings=report.warnings,
                )
                notification = _maybe_notify_order_submit(args, report)
                if notification is not None:
                    notifications.append(notification)
                reports.append(report)
            payload = _multi_submit_payload(snapshot, reports, include_details=not args.summary_only)
            if notifications:
                payload["notifications"] = notifications
            print(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            route, route_batches = routed_batches[0]
            report = _run_order_runtime_submit_for_route(
                snapshot,
                args,
                route,
                sleeve_ids,
                route_batches,
            )
            _append_order_report_journal(
                journal_store,
                snapshot=snapshot,
                sleeve_ids=sleeve_ids,
                route=route,
                source="order-runtime-submit",
                generated_at=report.generated_at,
                status=report.status,
                counts={
                    "batch_count": report.batch_count,
                    "order_count": report.order_count,
                    "ticket_count": len(report.coordination.tickets),
                    "event_count": len(report.coordination.events),
                },
                errors=report.errors,
                warnings=report.warnings,
            )
            payload = report.to_dict(include_details=not args.summary_only)
            notification = _maybe_notify_order_submit(args, report)
            if notification is not None:
                payload["notification"] = notification
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "order-runtime-paper-smoke":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_ids = _status_sleeve_ids(snapshot, args.sleeve_id)
        batches = load_order_intent_batches(resolve_runtime_path(snapshot, args.batch_file))
        journal_store = _runtime_journal_store(snapshot, args.journal)
        routed_batches = _routed_batches_for_submit(
            snapshot,
            batches,
            sleeve_ids,
            args.account_id,
            args.account_store,
            args.order_store,
        )
        reports = []
        for route, route_batches in routed_batches:
            report = _run_order_runtime_paper_smoke_for_route(snapshot, args, route, sleeve_ids, route_batches)
            _append_order_report_journal(
                journal_store,
                snapshot=snapshot,
                sleeve_ids=sleeve_ids,
                route=route,
                source="order-runtime-paper-smoke",
                generated_at=report.started_at,
                status=report.status,
                counts={
                    "batch_count": report.submit.batch_count,
                    "order_count": report.submit.order_count,
                    "submit_event_count": len(report.submit.coordination.events),
                    "supervisor_poll_event_count": report.supervisor.poll_event_count if report.supervisor else 0,
                },
                errors=report.submit.errors,
                warnings=(),
            )
            reports.append(report)
        if len(reports) == 1:
            print(json.dumps(reports[0].to_dict(include_details=not args.summary_only), ensure_ascii=False, indent=2))
        else:
            print(
                json.dumps(
                    {
                        "status": "blocked" if any(report.status == "blocked" for report in reports) else "ok",
                        "runtime_id": snapshot.config.runtime_id,
                        "route_count": len(reports),
                        "routes": [report.to_dict(include_details=not args.summary_only) for report in reports],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    if args.command == "order-runtime-supervise":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_ids = _status_sleeve_ids(snapshot, args.sleeve_id)
        routes = _resolve_order_runtime_routes(snapshot, args.account_id, args.account_store, args.order_store, sleeve_ids)
        journal_store = _runtime_journal_store(snapshot, args.journal)
        reports = []
        notifications = []
        for route in routes:
            report = _run_order_runtime_supervisor_for_route(snapshot, args, route, sleeve_ids)
            _append_order_report_journal(
                journal_store,
                snapshot=snapshot,
                sleeve_ids=sleeve_ids,
                route=route,
                source="order-runtime-supervise",
                generated_at=report.finished_at,
                status=report.status,
                counts={
                    "poll_report_count": len(report.poll_reports),
                    "poll_event_count": report.poll_event_count,
                    "poll_fill_event_count": report.poll_fill_event_count,
                    "open_ticket_count": len(report.final_status.order_snapshot.open_tickets),
                    "unallocated_fill_count": report.final_status.unallocated_fill_count,
                },
                errors=report.errors,
                warnings=(),
            )
            notification = _maybe_notify_order_supervisor(args, report)
            if notification is not None:
                notifications.append(notification)
            reports.append(report)
        if len(reports) == 1:
            payload = reports[0].to_dict(include_details=not args.summary_only)
            if notifications:
                payload["notification"] = notifications[0]
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            payload = {
                "status": "warnings" if any(report.status != "ok" for report in reports) else "ok",
                "runtime_id": snapshot.config.runtime_id,
                "route_count": len(reports),
                "routes": [report.to_dict(include_details=not args.summary_only) for report in reports],
            }
            if notifications:
                payload["notifications"] = notifications
            print(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                )
        )
        return 0
    if args.command == "notification-status":
        service = NotificationService.from_env(root=args.root)
        print(json.dumps(service.health_check(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "notification-fetch-telegram-updates":
        service = NotificationService.from_env(root=args.root)
        result = service.fetch_telegram_updates(offset=args.offset, limit=args.limit)
        if args.summary_only:
            result = {
                "status": result["status"],
                "delivery_mode": result["delivery_mode"],
                "fetched_count": result["fetched_count"],
                "stored_count": result["stored_count"],
                "root": result["root"],
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "notify-user-message":
        service = NotificationService.from_env(root=args.root)
        message = _notification_message_from_args(args)
        result = service.notify_user_message(
            category=args.category,
            title=args.title,
            message=message,
            chat_id=args.chat_id,
            parse_mode=args.parse_mode,
            disable_notification=args.disable_notification,
        )
        if args.summary_only:
            result = {
                "record_id": result["record_id"],
                "delivery_mode": result["delivery_mode"],
                "delivery_status": result["delivery_status"],
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == "runtime-recovery-status":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_ids = _status_sleeve_ids(snapshot, args.sleeve_id)
        routes = _resolve_order_runtime_routes(snapshot, args.account_id, args.account_store, args.order_store, sleeve_ids)
        journal_store = _runtime_journal_store(snapshot, args.journal)
        accounts = []
        for route in routes:
            status_report = _build_order_runtime_status_for_route(
                snapshot,
                route,
                sleeve_ids,
                recent_events=args.recent_events,
            )
            accounts.append(
                build_recovery_account_report(
                    order_status=status_report,
                    journal_store=journal_store,
                    sleeve_ids=sleeve_ids,
                )
            )
        report = build_recovery_report(
            runtime_id=snapshot.config.runtime_id,
            config_version=snapshot.version,
            sleeve_ids=sleeve_ids,
            accounts=tuple(accounts),
        )
        print(json.dumps(report.to_dict(include_details=not args.summary_only), ensure_ascii=False, indent=2))
        return 0
    if args.command == "runtime-health":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_ids = _status_sleeve_ids(snapshot, args.sleeve_id)
        routes = _resolve_order_runtime_routes(snapshot, args.account_id, args.account_store, args.order_store, sleeve_ids)
        journal_path = _runtime_journal_path(snapshot, args.journal)
        journal_store = FileCycleJournalStore(journal_path) if journal_path is not None else None
        runtime_state_store, _runtime_state_summary = _runtime_state_store_from_args(
            snapshot,
            args.runtime_state,
            read_only=True,
        )
        reports = []
        for route in routes:
            status_report = _build_order_runtime_status_for_route(
                snapshot,
                route,
                sleeve_ids,
                recent_events=args.recent_events,
            )
            reports.append(
                build_runtime_health_report(
                    runtime_id=snapshot.config.runtime_id,
                    sleeve_ids=sleeve_ids,
                    journal_store=journal_store,
                    order_status=status_report,
                    runtime_state_store=runtime_state_store,
                    journal_path=journal_path,
                    broker=args.broker,
                    max_cycle_age_seconds=args.max_cycle_age_seconds,
                    max_open_ticket_age_seconds=args.max_open_ticket_age_seconds,
                    heartbeat_path=_cwd_path(args.heartbeat) if args.heartbeat is not None else None,
                    heartbeat_component=args.heartbeat_component,
                    max_heartbeat_age_seconds=args.max_heartbeat_age_seconds,
                    kis_gateway_base_url=(
                        snapshot.config.market_data.gateway_base_url
                        if snapshot.config.market_data.provider == "kis-gateway"
                        else None
                    ),
                )
            )
        if len(reports) == 1:
            print(json.dumps(reports[0].to_dict(), ensure_ascii=False, indent=2))
        else:
            statuses = {report.status for report in reports}
            aggregate_status = "critical" if "critical" in statuses else "needs_attention" if "needs_attention" in statuses else "ok"
            print(
                json.dumps(
                    {
                        "status": aggregate_status,
                        "runtime_id": snapshot.config.runtime_id,
                        "route_count": len(reports),
                        "routes": [report.to_dict() for report in reports],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        return 0
    if args.command == "runtime-artifact-status":
        snapshot = load_runtime_config_snapshot(args.config)
        active_sleeve_payload = _read_runtime_json(_cwd_path(args.active_sleeves_path))
        requested_sleeve_ids = tuple(args.sleeve_id)
        active_sleeve_ids = _active_sleeve_ids_from_payload(active_sleeve_payload)
        if requested_sleeve_ids:
            sleeve_ids = requested_sleeve_ids
        elif args.active_only and active_sleeve_ids:
            sleeve_ids = active_sleeve_ids
        else:
            sleeve_ids = tuple(sleeve.sleeve_id for sleeve in snapshot.config.sleeves)
        report = _build_runtime_artifact_status_report(
            snapshot,
            sleeve_ids=sleeve_ids,
            active_sleeve_ids=active_sleeve_ids,
            active_sleeves_path=_cwd_path(args.active_sleeves_path),
            active_sleeves_payload=active_sleeve_payload,
            control_queue_path=_cwd_path(args.control_queue),
            framework_state_dir=_cwd_path(args.framework_state_dir),
            order_batch_path=_cwd_path(args.order_batch),
            live_loop_log_path=_cwd_path(args.live_loop_log),
            live_loop_heartbeat_path=_cwd_path(args.live_loop_heartbeat),
            runtime_state_path=_cwd_path(args.runtime_state)
            if args.runtime_state is not None
            else _cwd_path(Path("data/runtime/runtime-state") / f"{snapshot.config.runtime_id}.sqlite"),
            submit_state_path=_cwd_path(args.submit_state),
            report_dir=_cwd_path(args.report_dir),
            eod_snapshot_root=_cwd_path(args.eod_snapshot_root),
            eod_state_dir=_cwd_path(args.eod_state_dir),
            startup_status_path=_cwd_path(args.startup_status),
            include_details=not args.summary_only,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "operator-ui":
        from leaps_quant_engine.operator_ui import build_operator_dashboard_snapshot, serve_operator_ui

        if args.snapshot_only:
            report = build_operator_dashboard_snapshot(
                args.config,
                sleeve_ids=tuple(args.sleeve_id),
                account_id=args.account_id,
                account_store_path=args.account_store,
                order_store_path=args.order_store,
                journal_path=args.journal,
                recent_events=args.recent_events,
                include_details=False,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        serve_operator_ui(
            args.config,
            host=args.host,
            port=args.port,
            sleeve_ids=tuple(args.sleeve_id),
            account_id=args.account_id,
            account_store_path=args.account_store,
            order_store_path=args.order_store,
            journal_path=args.journal,
            recent_events=args.recent_events,
        )
        return 0
    if args.command == "runtime-preflight":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_ids = _status_sleeve_ids(snapshot, args.sleeve_id)
        journal_path = _runtime_journal_path(snapshot, args.journal)
        journal_store = FileCycleJournalStore(journal_path) if journal_path is not None else None
        order_statuses = []
        if args.include_order_status:
            for route in _resolve_order_runtime_routes(snapshot, args.account_id, args.account_store, args.order_store, sleeve_ids):
                order_statuses.append(
                    _build_order_runtime_status_for_route(
                        snapshot,
                        route,
                        sleeve_ids,
                        recent_events=args.recent_events,
                    )
                )
        report = build_runtime_preflight_report(
            snapshot=snapshot,
            sleeve_ids=sleeve_ids,
            journal_store=journal_store,
            journal_path=journal_path,
            order_statuses=tuple(order_statuses),
            strict_live=args.strict_live,
            check_bootstrap=not args.skip_bootstrap,
        )
        print(json.dumps(report.to_dict(include_details=not args.summary_only), ensure_ascii=False, indent=2))
        return 0
    if args.command == "sleeve-cash-availability":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_ids = _status_sleeve_ids(snapshot, args.sleeve_id)
        routes = _resolve_order_runtime_routes(snapshot, args.account_id, args.account_store, args.order_store, sleeve_ids)
        report = build_cash_availability_report(
            runtime_id=snapshot.config.runtime_id,
            sleeve_ids=sleeve_ids,
            residual_sleeve_id=args.residual_sleeve_id,
            routes=tuple(
                CashAvailabilityRouteInput(
                    account_id=route.account_id,
                    market_scope=route.market_scope,
                    currency=route.currency,
                    account_store_path=route.account_store_path,
                    default_cash_by_sleeve=_default_cash_by_sleeve(snapshot, route.currency, route.account_id),
                )
                for route in routes
            ),
        )
        if args.summary_only:
            report = {
                key: value
                for key, value in report.items()
                if key not in {"routes"}
            } | {
                "routes": [
                    {
                        key: value
                        for key, value in route.items()
                        if key not in {"all_sleeve_cash"}
                    }
                    for route in report["routes"]
                ]
            }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "eod-snapshot-status":
        report = build_eod_snapshot_status(
            snapshot_root=args.snapshot_root,
            state_dir=args.state_dir,
            schedules=tuple(args.schedule) if args.schedule else DEFAULT_EOD_SCHEDULES,
        )
        if args.summary_only:
            report = {
                **report,
                "labels": [
                    {
                        "label": item["label"],
                        "schedule_time": item["schedule_time"],
                        "status": item["status"],
                        "today_marker": item["today_marker"],
                        "latest_marker": item["latest_marker"],
                        "latest_manifest": item["latest_manifest"],
                    }
                    for item in report["labels"]
                ],
            }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "sleeve-daily-performance":
        report = build_sleeve_daily_performance_report(
            args.snapshot_root,
            sleeve_ids=tuple(args.sleeve_id),
            currency=args.currency,
            from_date=args.from_date,
            to_date=args.to_date,
        )
        print(
            json.dumps(
                report.to_dict(
                    include_rows=not args.summary_only,
                    include_holdings=args.include_holdings and not args.summary_only,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "virtual-account-allocate-fill":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        default_cash_by_sleeve = _default_cash_by_sleeve(snapshot, _sleeve_default_currency(snapshot, sleeve_config))
        store = VirtualSleeveAccountStore(
            _resolve_sleeve_account_store_path(snapshot, sleeve_config),
            default_cash_by_sleeve=default_cash_by_sleeve,
            default_currency=_sleeve_default_currency(snapshot, sleeve_config),
        )
        fill = store.broker_fill(args.fill_id)
        if fill is None:
            raise RuntimeError(f"broker fill not found: {args.fill_id}")
        allocations = tuple(
            FillAllocation(
                fill_id=fill.fill_id,
                sleeve_id=sleeve_id,
                quantity=quantity,
                allocation_id=f"{fill.fill_id}:{index}:{sleeve_id}",
                reason=args.reason,
            )
            for index, (sleeve_id, quantity) in enumerate(_parse_fill_allocations(args.allocation), start=1)
        )
        portfolios = store.apply_fill_allocations(fill, allocations)
        print(
            json.dumps(
                {
                    "fill_id": fill.fill_id,
                    "account_store_path": str(store.path),
                    "allocations": [allocation.to_dict() for allocation in allocations],
                    "synced_sleeves": {
                        sleeve_id: _portfolio_to_json(portfolio)
                        for sleeve_id, portfolio in sorted(portfolios.items())
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "virtual-account-ignore-fill":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        account = _broker_account_for_sleeve_market(snapshot, sleeve_config, args.market)
        default_currency = account.currency if account is not None else _sleeve_default_currency(snapshot, sleeve_config)
        store = VirtualSleeveAccountStore(
            _resolve_sleeve_account_store_path(snapshot, sleeve_config, market_scope=args.market),
            default_cash_by_sleeve=_default_cash_by_sleeve(
                snapshot,
                default_currency,
                account.account_id if account is not None else getattr(sleeve_config, "broker_account_id", None),
            ),
            default_currency=default_currency,
        )
        fill = store.broker_fill(args.fill_id)
        if fill is None:
            raise RuntimeError(f"broker fill not found: {args.fill_id}")
        ignored = store.ignore_broker_fill(
            args.fill_id,
            reason=args.reason,
            ignored_by=args.ignored_by,
        )
        report = store.reconciliation_report([], include_fills=True)
        print(
            json.dumps(
                {
                    "fill_id": fill.fill_id,
                    "account_store_path": str(store.path),
                    "ignored": ignored.to_dict(),
                    "allocation_status": next(
                        (
                            status.to_dict()
                            for status in report.allocation_statuses
                            if status.fill.fill_id == fill.fill_id
                        ),
                        None,
                    ),
                    "unallocated_fill_count": report.unallocated_fill_count,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "virtual-account-reconcile":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        account = _broker_account_for_sleeve_market(snapshot, sleeve_config, args.market)
        currency = account.currency if account is not None else _sleeve_default_currency(snapshot, sleeve_config)
        default_cash_by_sleeve = _default_cash_by_sleeve(
            snapshot,
            currency,
            account.account_id if account is not None else getattr(sleeve_config, "broker_account_id", None),
        )
        store = VirtualSleeveAccountStore(
            _resolve_sleeve_account_store_path(snapshot, sleeve_config, market_scope=args.market),
            default_cash_by_sleeve=default_cash_by_sleeve,
            default_currency=currency,
        )
        account_metadata = dict(account.metadata) if account is not None else {}
        try:
            sync = KISVirtualAccountSync.from_env(
                account.account_id if account is not None else None,
                metadata=account_metadata,
            )
        except TypeError:
            sync = KISVirtualAccountSync.from_env()
        holdings = sync.account_client.get_holdings(market=args.market)
        report = store.reconciliation_report(holdings, include_fills=True)
        payload = report.to_dict(include_fills=not args.summary_only)
        payload["account_store_path"] = str(store.path)
        order_store_path = (
            resolve_runtime_path(snapshot, account.order_store_path).resolve()
            if account is not None and account.order_store_path is not None
            else _resolve_status_order_store_path(snapshot, None, store.path)
        )
        payload["order_store_path"] = str(order_store_path)
        payload["order_runtime_filled_positions"] = _order_runtime_filled_positions(
            order_store_path,
            sleeve_id=args.sleeve_id,
        )
        payload["broker_holdings_count"] = holdings.get("holdings_count", len(holdings.get("holdings", [])))
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "indicators-kis-once":
        provider = KISBrokerEngineMarketDataProvider.from_env()
        indicator_engine = build_indicator_engine_from_file(args.config)
        if args.warmup_start or args.warmup_end:
            indicator_engine.warm_up_from_provider(
                args.sleeve_id,
                provider,
                start=_parse_cli_datetime(args.warmup_start),
                end=_parse_cli_datetime(args.warmup_end),
            )
        indicator_engine.update_from_provider(provider)
        print(json.dumps(_indicator_values_to_json(indicator_engine, args.sleeve_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "indicators-backtest-once":
        indicator_engine = build_indicator_engine_from_file(args.config)
        print(json.dumps(_indicator_values_to_json(indicator_engine, args.sleeve_id), ensure_ascii=False, indent=2))
        return 0
    if args.command == "benchmark-indicators-daily":
        universe = load_universe_definition(args.universe)
        provider = KISCachedMarketDataProvider.from_env()
        _attach_exchange_map(provider, universe)
        report = run_daily_indicator_benchmark(
            universe,
            provider,
            sleeve_id=args.sleeve_id,
            start=_parse_cli_datetime(args.start),
            end=_parse_cli_datetime(args.end),
            refresh_history=args.refresh_history,
            include_daily=args.include_daily,
            source=args.source,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "framework-backtest-daily":
        universe = load_universe_definition(args.universe)
        provider = _daily_backtest_provider(args.source)
        _attach_exchange_map(provider, universe)
        alpha_load = PythonAlphaLoader().load(args.alpha)
        journal_store = FileCycleJournalStore(args.journal) if args.journal is not None else None
        journal_mode = _resolve_backtest_journal_mode(args)
        universe_market = getattr(universe, "market", None)
        fundamentals_store = None
        fundamentals_report = None
        fundamental_names = tuple(args.fundamental_name) if args.fundamental_name else None
        if args.fundamentals_root is not None:
            fundamentals_store, fundamentals_report = _load_fundamentals_for_backtest(
                args.fundamentals_root,
                market=args.fundamentals_market or universe_market,
                end=_parse_cli_datetime(args.end),
                names=fundamental_names,
            )
        result = run_framework_backtest(
            universe,
            provider,
            sleeve_id=args.sleeve_id,
            framework_runner=FrameworkRunner(
                sleeve_id=args.sleeve_id,
                alpha_runtime=AlphaRuntime(active_models=(alpha_load.model,)),
                runtime_state_store=InMemoryRuntimeStateStore(),
            ),
            portfolio=Portfolio(cash=args.cash),
            start=_parse_cli_datetime(args.start),
            end=_parse_cli_datetime(args.end),
            warmup_start=_parse_cli_datetime(args.warmup_start),
            refresh_history=args.refresh_history,
            cycle_journal_store=journal_store,
            runtime_id=f"framework-backtest:{args.sleeve_id}",
            market_scope=market_scope_from_market(universe_market) if universe_market else None,
            fundamental_store=fundamentals_store,
            fundamental_names=fundamental_names,
            fill_model=simulated_fill_model_for_costs(slippage_bps=args.slippage_bps, fee_model=args.fee_model),
            cycle_journal_include_lineage=journal_mode == "full",
            daily_bar_time=_parse_cli_time(args.daily_bar_time),
        )
        report = result.to_report(
            include_orders=not args.summary_only,
            include_insights=args.include_insights,
            include_selection_details=(not args.summary_only or args.include_insights),
        )
        report["source"] = args.source
        if args.daily_bar_time:
            report["daily_bar_time"] = args.daily_bar_time
        report["fill_model"] = {"slippage_bps": float(args.slippage_bps or 0.0), "fee_model": args.fee_model}
        if fundamentals_report is not None:
            report["fundamentals"] = fundamentals_report
        if args.journal is not None:
            report["cycle_journal"] = {"path": str(args.journal.resolve()), "mode": journal_mode}
        report["alpha"] = {
            "alpha_id": alpha_load.alpha_id,
            "version": alpha_load.version,
            "path": str(alpha_load.path),
            "content_hash": alpha_load.content_hash,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "runtime-backtest-daily":
        command_started = perf_time.perf_counter()
        timings: dict[str, float] = {}
        setup_started = perf_time.perf_counter()
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        provider = _daily_backtest_provider(args.source)
        currency = str(args.currency or currency_for_market(_runtime_sleeve_universe_market(snapshot, sleeve_config))).strip().upper()
        portfolio = _backtest_portfolio_from_sleeve(sleeve_config, cash=args.cash, currency=currency)
        initial_cash = portfolio.cash
        initial_cash_by_currency = dict(portfolio.cash_by_currency)
        dependencies = RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit_per_second: provider,
            history_provider_factory=lambda: provider,
            portfolio_provider=StaticPortfolioProvider(portfolios={args.sleeve_id: portfolio}),
            runtime_state_store=InMemoryRuntimeStateStore(),
        )
        runtime = bootstrap_sleeve_runtime(
            snapshot,
            args.sleeve_id,
            dependencies=dependencies,
            refresh_fine=False,
        )
        universe = runtime.coarse_universe
        temporal_feature_provider = temporal_feature_provider_from_portfolio_parameters(sleeve_config.portfolio.parameters)
        _attach_exchange_map(provider, universe)
        universe_market = getattr(universe, "market", None)
        journal_store = FileCycleJournalStore(args.journal) if args.journal is not None else None
        journal_mode = _resolve_backtest_journal_mode(args)
        fundamentals_store = None
        fundamentals_report = None
        fundamental_names = tuple(args.fundamental_name) if args.fundamental_name else None
        if args.fundamentals_root is not None:
            fundamentals_store, fundamentals_report = _load_fundamentals_for_backtest(
                args.fundamentals_root,
                market=args.fundamentals_market or universe_market,
                end=_parse_cli_datetime(args.end),
                names=fundamental_names,
            )
        timings["config_bootstrap_ms"] = _perf_elapsed_ms(setup_started)
        backtest_started = perf_time.perf_counter()
        result = run_framework_backtest(
            universe,
            provider,
            sleeve_id=args.sleeve_id,
            framework_runner=runtime.framework_runner,
            portfolio=portfolio,
            start=_parse_cli_datetime(args.start),
            end=_parse_cli_datetime(args.end),
            warmup_start=_runtime_backtest_warmup_start(args, sleeve_config),
            refresh_history=args.refresh_history,
            cycle_journal_store=journal_store,
            runtime_id=f"{snapshot.config.runtime_id}:backtest:{args.sleeve_id}",
            config_version=snapshot.version,
            account_id=sleeve_config.broker_account_id,
            market_scope=market_scope_from_market(universe_market) if universe_market else None,
            fundamental_store=fundamentals_store,
            fundamental_names=fundamental_names,
            selection_models=runtime.selection_models,
            alpha_input_selections=sleeve_config.alpha.input_selections,
            fill_model=simulated_fill_model_for_costs(slippage_bps=args.slippage_bps, fee_model=args.fee_model),
            temporal_feature_provider=temporal_feature_provider,
            cycle_journal_include_lineage=journal_mode == "full",
            daily_bar_time=_parse_cli_time(args.daily_bar_time),
        )
        timings["framework_backtest_ms"] = _perf_elapsed_ms(backtest_started)
        report_started = perf_time.perf_counter()
        report = result.to_report(
            include_orders=not args.summary_only,
            include_insights=args.include_insights,
            include_selection_details=(not args.summary_only or args.include_insights),
        )
        report["source"] = args.source
        if args.daily_bar_time:
            report["daily_bar_time"] = args.daily_bar_time
        report["fill_model"] = {"slippage_bps": float(args.slippage_bps or 0.0), "fee_model": args.fee_model}
        report["runtime"] = {
            "runtime_id": snapshot.config.runtime_id,
            "config_version": snapshot.version,
            "config_path": str(args.config),
            "sleeve_id": args.sleeve_id,
            "configured_sleeves": [
                {
                    "sleeve_id": sleeve.sleeve_id,
                    "cash": sleeve.cash,
                    "cash_by_currency": dict(sleeve.cash_by_currency),
                    "alpha_module_count": len(sleeve.alpha.modules),
                    "selection_model_count": len(sleeve.universe.active.selection_models)
                    if sleeve.universe.active.selection_models
                    else 1,
                }
                for sleeve in snapshot.config.sleeves
            ],
        }
        report["alpha"] = {
            "alpha_ids": list(runtime.alpha_runtime.active_alpha_ids()),
            "input_selections": dict(sleeve_config.alpha.input_selections),
        }
        report["portfolio"] = {
            "initial_cash": initial_cash,
            "initial_cash_by_currency": initial_cash_by_currency,
            "model": sleeve_config.portfolio.model.to_dict(),
        }
        report["risk"] = {"model": sleeve_config.risk.model.to_dict()}
        report["execution"] = {"model": sleeve_config.execution.model.to_dict()}
        if fundamentals_report is not None:
            report["fundamentals"] = fundamentals_report
        if args.journal is not None:
            report["cycle_journal"] = {"path": str(args.journal.resolve()), "mode": journal_mode}
        timings["report_generation_ms"] = _perf_elapsed_ms(report_started)
        timings["total_ms"] = _perf_elapsed_ms(command_started)
        report["timings"] = _merge_backtest_timings(timings, result)
        history_cache = _history_cache_report(provider)
        if history_cache:
            report["history_cache"] = history_cache
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "runtime-backtest-minute":
        command_started = perf_time.perf_counter()
        timings: dict[str, float] = {}
        setup_started = perf_time.perf_counter()
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        daily_provider = _daily_backtest_provider(args.daily_source)
        currency = str(args.currency or currency_for_market(_runtime_sleeve_universe_market(snapshot, sleeve_config))).strip().upper()
        portfolio = _backtest_portfolio_from_sleeve(sleeve_config, cash=args.cash, currency=currency)
        initial_cash = portfolio.cash
        initial_cash_by_currency = dict(portfolio.cash_by_currency)
        dependencies = RuntimeBootstrapDependencies(
            live_provider_factory=lambda universe, rate_limit_per_second: daily_provider,
            history_provider_factory=lambda: daily_provider,
            portfolio_provider=StaticPortfolioProvider(portfolios={args.sleeve_id: portfolio}),
            runtime_state_store=InMemoryRuntimeStateStore(),
        )
        runtime = bootstrap_sleeve_runtime(
            snapshot,
            args.sleeve_id,
            dependencies=dependencies,
            refresh_fine=False,
            preselect_warmup=False,
        )
        universe = universe_with_default_indicator_resolution(runtime.coarse_universe, default_resolution="daily")
        temporal_feature_provider = temporal_feature_provider_from_portfolio_parameters(sleeve_config.portfolio.parameters)
        _attach_exchange_map(daily_provider, universe)
        start = _parse_cli_datetime(args.start)
        end = _parse_cli_datetime(args.end)
        timings["config_bootstrap_ms"] = _perf_elapsed_ms(setup_started)
        feed_started = perf_time.perf_counter()
        minute_cache_report = None
        compiled_replay_cache_report = None
        minute_feed_path = args.minute_feed
        feed_source = "minute-cache" if args.minute_cache_root is not None else "minute-feed"
        compiled_cache_path = args.compiled_replay_cache
        if (
            compiled_cache_path is not None
            and compiled_cache_path.exists()
            and not args.refresh_compiled_replay_cache
        ):
            feed, compiled_replay_cache_report = load_compiled_minute_replay_cache(compiled_cache_path)
            feed = _slice_replay_feed(feed, start=start, end=end)
            feed_source = "compiled-replay-cache"
        else:
            source_signature = None
            if args.minute_cache_root is not None:
                if start is None or end is None:
                    raise RuntimeError("--start and --end are required when using --minute-cache-root.")
                cache_bars, minute_cache_report = load_minute_feed_cache_bars(
                    universe,
                    cache_root=args.minute_cache_root,
                    start=start,
                    end=end,
                )
                feed = build_minute_replay_feed_from_bars(cache_bars, start=start, end=end)
                source_signature = minute_replay_source_signature(args.minute_cache_root)
            elif minute_feed_path is not None:
                feed = load_minute_replay_feed(minute_feed_path, universe=universe, start=start, end=end)
                source_signature = minute_replay_source_signature(minute_feed_path)
            else:
                raise RuntimeError(
                    "runtime-backtest-minute requires --minute-feed, --minute-cache-root, "
                    "or an existing --compiled-replay-cache."
                )
            if compiled_cache_path is not None:
                compiled_replay_cache_report = write_compiled_minute_replay_cache(
                    compiled_cache_path,
                    feed,
                    source=feed_source,
                    source_signature=source_signature,
                    start=start,
                    end=end,
                )
        timings["feed_load_ms"] = _perf_elapsed_ms(feed_started)
        indicator_engine = IndicatorEngine()
        indicator_engine.register_universe(args.sleeve_id, universe)
        warmup_start = _runtime_backtest_warmup_start(args, sleeve_config)
        warmup_end = _minute_backtest_warmup_end(feed, start=start)
        daily_warmup_bar_count = 0
        daily_warmup_cache_report = None
        warmup_started = perf_time.perf_counter()
        if warmup_start is not None:
            warmup_cache_path = args.daily_warmup_cache
            if (
                warmup_cache_path is not None
                and warmup_cache_path.exists()
                and not args.refresh_daily_warmup_cache
            ):
                daily_warmup_bars, daily_warmup_cache_report = load_daily_warmup_cache(warmup_cache_path)
                _validate_daily_warmup_cache_report(
                    daily_warmup_cache_report,
                    expected_start=warmup_start,
                    expected_end=warmup_end,
                )
            else:
                daily_warmup_bars = load_daily_warmup_bars_for_backtest(
                    daily_provider,
                    universe,
                    start=warmup_start,
                    end=warmup_end,
                    refresh_history=args.refresh_history,
                )
                if warmup_cache_path is not None:
                    daily_warmup_cache_report = write_daily_warmup_cache(
                        warmup_cache_path,
                        daily_warmup_bars,
                        source=args.daily_source,
                        source_signature=f"{snapshot.version}:{universe.id}:{warmup_start.isoformat()}:{warmup_end.isoformat() if warmup_end else ''}",
                        start=warmup_start,
                        end=warmup_end,
                    )
            indicator_engine.warm_up(args.sleeve_id, daily_warmup_bars)
            daily_warmup_bar_count = len(daily_warmup_bars)
            if temporal_feature_provider is not None:
                temporal_feature_provider.update(daily_warmup_bars)
        timings["daily_warmup_ms"] = _perf_elapsed_ms(warmup_started)
        daily_replay_bars: list[Bar] = []
        daily_replay_started = perf_time.perf_counter()
        if feed:
            daily_replay_start_at = (warmup_end + timedelta(days=1)) if warmup_end is not None else feed[0].time
            daily_replay_start = datetime.combine(daily_replay_start_at.date(), clock_time.min)
            daily_replay_end = end or feed[-1].time
            daily_replay_bars = load_daily_warmup_bars_for_backtest(
                daily_provider,
                universe,
                start=daily_replay_start,
                end=daily_replay_end,
                refresh_history=args.refresh_history,
            )
        timings["daily_replay_load_ms"] = _perf_elapsed_ms(daily_replay_started)
        universe_market = getattr(universe, "market", None)
        journal_store = FileCycleJournalStore(args.journal) if args.journal is not None else None
        journal_mode = _resolve_backtest_journal_mode(args)
        fundamentals_store = None
        fundamentals_report = None
        fundamental_names = tuple(args.fundamental_name) if args.fundamental_name else None
        if args.fundamentals_root is not None:
            fundamentals_store, fundamentals_report = _load_fundamentals_for_backtest(
                args.fundamentals_root,
                market=args.fundamentals_market or universe_market,
                end=end,
                names=fundamental_names,
            )
        replay_started = perf_time.perf_counter()
        result = run_framework_replay(
            feed,
            universe,
            sleeve_id=args.sleeve_id,
            framework_runner=runtime.framework_runner,
            portfolio=portfolio,
            indicator_engine=indicator_engine,
            fundamental_store=fundamentals_store,
            fundamental_names=fundamental_names,
            selection_models=runtime.selection_models,
            alpha_input_selections=sleeve_config.alpha.input_selections,
            fill_model=simulated_fill_model_for_costs(slippage_bps=args.slippage_bps, fee_model=args.fee_model),
            cycle_journal_store=journal_store,
            runtime_id=f"{snapshot.config.runtime_id}:minute-backtest:{args.sleeve_id}",
            config_version=snapshot.version,
            account_id=sleeve_config.broker_account_id,
            market_scope=market_scope_from_market(universe_market) if universe_market else None,
            warmup_data_slice_count=daily_warmup_bar_count,
            temporal_feature_provider=temporal_feature_provider,
            cycle_journal_include_lineage=journal_mode == "full",
            daily_indicator_bars=daily_replay_bars,
        )
        timings["framework_replay_ms"] = _perf_elapsed_ms(replay_started)
        report_started = perf_time.perf_counter()
        report = result.to_report(
            include_orders=not args.summary_only,
            include_insights=args.include_insights,
            include_selection_details=(not args.summary_only or args.include_insights),
        )
        report["source"] = feed_source
        report["daily_source"] = args.daily_source
        report["fill_model"] = {"slippage_bps": float(args.slippage_bps or 0.0), "fee_model": args.fee_model}
        report["runtime"] = {
            "runtime_id": snapshot.config.runtime_id,
            "config_version": snapshot.version,
            "config_path": str(args.config),
            "sleeve_id": args.sleeve_id,
            "configured_sleeves": [
                {
                    "sleeve_id": sleeve.sleeve_id,
                    "cash": sleeve.cash,
                    "cash_by_currency": dict(sleeve.cash_by_currency),
                    "alpha_module_count": len(sleeve.alpha.modules),
                    "selection_model_count": len(sleeve.universe.active.selection_models)
                    if sleeve.universe.active.selection_models
                    else 1,
                }
                for sleeve in snapshot.config.sleeves
            ],
        }
        report["minute_backtest"] = {
            "minute_feed": str(minute_feed_path) if minute_feed_path is not None else None,
            "minute_data_slice_count": len(feed),
            "requested_start": start.isoformat() if start is not None else None,
            "requested_end": end.isoformat() if end is not None else None,
            "effective_start": _feed_start(feed),
            "effective_end": _feed_end(feed),
            "daily_warmup_bar_count": daily_warmup_bar_count,
            "daily_warmup_start": warmup_start.isoformat() if warmup_start else None,
            "daily_warmup_end": warmup_end.isoformat() if warmup_end else None,
            "daily_replay_bar_count": len(daily_replay_bars),
            "indicator_default_resolution": "daily",
        }
        if minute_cache_report is not None:
            report["minute_cache"] = minute_cache_report.to_dict()
        if compiled_replay_cache_report is not None:
            report["compiled_replay_cache"] = compiled_replay_cache_report.to_dict()
        if daily_warmup_cache_report is not None:
            report["daily_warmup_cache"] = daily_warmup_cache_report.to_dict()
        report["alpha"] = {
            "alpha_ids": list(runtime.alpha_runtime.active_alpha_ids()),
            "input_selections": dict(sleeve_config.alpha.input_selections),
        }
        report["portfolio"] = {
            "initial_cash": initial_cash,
            "initial_cash_by_currency": initial_cash_by_currency,
            "model": sleeve_config.portfolio.model.to_dict(),
        }
        report["risk"] = {"model": sleeve_config.risk.model.to_dict()}
        report["execution"] = {"model": sleeve_config.execution.model.to_dict()}
        if fundamentals_report is not None:
            report["fundamentals"] = fundamentals_report
        if args.journal is not None:
            report["cycle_journal"] = {"path": str(args.journal.resolve()), "mode": journal_mode}
        timings["report_generation_ms"] = _perf_elapsed_ms(report_started)
        timings["total_ms"] = _perf_elapsed_ms(command_started)
        report["timings"] = _merge_backtest_timings(timings, result)
        history_cache = _history_cache_report(daily_provider)
        if history_cache:
            report["daily_history_cache"] = history_cache
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "download-us-minute-feed":
        universe, source_info = _load_minute_feed_source_universe(args.source, args.sleeve_id)
        start = _parse_cli_minute_start(args.start)
        end = _parse_cli_minute_end(args.end)
        if end < start:
            raise RuntimeError("--end must be greater than or equal to --start.")
        if args.provider not in {"yfinance", "kis-cache"}:
            raise RuntimeError(f"Unsupported US minute feed provider: {args.provider}")
        requested_symbols = tuple(args.symbol or ())
        if args.max_symbols is not None and not requested_symbols:
            requested_symbols = tuple(symbol.key for symbol in universe.symbols[: max(int(args.max_symbols), 0)])
        if args.provider == "kis-cache":
            provider = KISCachedMinuteBarProvider(
                provider=KISCachedMarketDataProvider.from_env(
                    exchange_by_symbol=_kis_exchange_map_for_universe(universe)
                ),
                refresh=False,
                daily_start_time=None,
                daily_end_time=None,
            )
        else:
            provider = YFinanceMinuteBarProvider(
                timezone=args.timezone,
                include_prepost=args.include_prepost,
                annotate_sessions=args.include_session_metadata,
                sleep_seconds=float(args.sleep_seconds or 0.0),
            )
        report_obj = download_us_minute_feed(
            universe,
            provider=provider,
            output_path=args.output,
            start=start,
            end=end,
            interval=args.interval,
            timezone=args.timezone,
            symbols=requested_symbols,
            overwrite=args.overwrite,
            include_session_metadata=args.include_session_metadata,
        )
        report = report_obj.to_dict()
        report["source"] = source_info
        report["runtime_backtest_minute_command"] = _runtime_minute_backtest_command_for_feed(
            source_info,
            output=args.output,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        if args.summary_only:
            report = {
                key: report[key]
                for key in (
                    "status",
                    "provider",
                    "output_path",
                    "requested_symbol_count",
                    "downloaded_symbol_count",
                    "row_count",
                    "empty_symbols",
                    "warnings",
                    "runtime_backtest_minute_command",
                )
                if key in report
            }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "minute-cache-build":
        universe, source_info = _load_minute_feed_source_universe(args.source, args.sleeve_id)
        start = _parse_cli_minute_start(args.start)
        end = _parse_cli_minute_end(args.end)
        start, end = _normalize_minute_cache_session_range(
            start,
            end,
            market=universe.market,
            start_text=args.start,
            end_text=args.end,
            include_extended_hours=args.include_extended_hours,
        )
        if end < start:
            raise RuntimeError("--end must be greater than or equal to --start.")
        requested_symbols = tuple(args.symbol or ())
        if args.max_symbols is not None and not requested_symbols:
            requested_symbols = tuple(symbol.key for symbol in universe.symbols[: max(int(args.max_symbols), 0)])
        timezone_name = args.timezone or _default_minute_timezone_for_market(universe.market)
        include_session_metadata = bool(args.include_session_metadata or args.include_extended_hours)
        if args.provider == "kis-cache":
            daily_start_time, daily_end_time = _minute_cache_session_time_bounds(
                universe.market,
                include_extended_hours=args.include_extended_hours,
            )
            provider = KISCachedMinuteBarProvider(
                provider=KISCachedMarketDataProvider.from_env(
                    exchange_by_symbol=_kis_exchange_map_for_universe(universe)
                ),
                refresh=args.refresh_provider_cache,
                daily_start_time=daily_start_time,
                daily_end_time=daily_end_time,
            )
        else:
            provider = YFinanceMinuteBarProvider(
                timezone=timezone_name,
                include_prepost=bool(args.include_prepost or args.include_extended_hours),
                annotate_sessions=include_session_metadata,
                sleep_seconds=float(args.sleep_seconds or 0.0),
                yfinance_symbol_by_key=yfinance_symbol_map_for_universe(universe),
                output_market_by_key={symbol.key: symbol.market for symbol in universe.symbols},
            )
        report_obj = build_minute_feed_cache(
            universe,
            provider=provider,
            cache_root=args.cache_root,
            start=start,
            end=end,
            interval=args.interval,
            timezone=timezone_name,
            symbols=requested_symbols,
            overwrite=args.overwrite,
            compress=not args.uncompressed,
            include_session_metadata=include_session_metadata,
        )
        report = report_obj.to_dict()
        report["source"] = source_info
        report["include_extended_hours"] = bool(args.include_extended_hours)
        report["include_session_metadata"] = include_session_metadata
        if args.provider == "kis-cache":
            report["provider_cache_refresh"] = bool(args.refresh_provider_cache)
        report["minute_cache_export_command"] = _minute_cache_export_command_for_source(
            source_info,
            cache_root=args.cache_root,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        report["runtime_backtest_minute_command"] = _runtime_minute_backtest_command_for_cache(
            source_info,
            cache_root=args.cache_root,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        if args.summary_only:
            report = {
                key: report[key]
                for key in (
                    "status",
                    "provider",
                    "cache_root",
                    "requested_symbol_count",
                    "downloaded_symbol_count",
                    "row_count",
                    "empty_symbols",
                    "warnings",
                    "include_extended_hours",
                    "include_session_metadata",
                    "provider_cache_refresh",
                    "minute_cache_export_command",
                    "runtime_backtest_minute_command",
                )
                if key in report
            }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "minute-cache-export":
        universe, source_info = _load_minute_feed_source_universe(args.source, args.sleeve_id)
        start = _parse_cli_minute_start(args.start)
        end = _parse_cli_minute_end(args.end)
        if end < start:
            raise RuntimeError("--end must be greater than or equal to --start.")
        requested_symbols = tuple(args.symbol or ())
        if args.max_symbols is not None and not requested_symbols:
            requested_symbols = tuple(symbol.key for symbol in universe.symbols[: max(int(args.max_symbols), 0)])
        report_obj = export_minute_feed_cache(
            universe,
            cache_root=args.cache_root,
            output_path=args.output,
            start=start,
            end=end,
            symbols=requested_symbols,
            overwrite=args.overwrite,
            include_session_metadata=args.include_session_metadata,
        )
        report = report_obj.to_dict()
        report["source"] = source_info
        report["include_session_metadata"] = bool(args.include_session_metadata)
        report["runtime_backtest_minute_command"] = _runtime_minute_backtest_command_for_feed(
            source_info,
            output=args.output,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        if args.summary_only:
            report = {
                key: report[key]
                for key in (
                    "status",
                    "cache_root",
                    "output_path",
                    "requested_symbol_count",
                    "exported_symbol_count",
                    "row_count",
                    "warnings",
                    "include_session_metadata",
                    "runtime_backtest_minute_command",
                )
                if key in report
            }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "train-rl-portfolio-constructor":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        universe = load_universe_definition(resolve_runtime_path(snapshot, sleeve_config.universe.coarse_path))
        provider = _daily_backtest_provider(args.source)
        _attach_exchange_map(provider, universe)
        portfolio_parameters = dict(sleeve_config.portfolio.parameters)
        result = train_ppo_portfolio_constructor(
            universe,
            provider,
            output_dir=args.output_dir,
            start=_parse_cli_datetime(args.start),
            end=_parse_cli_datetime(args.end),
            timesteps=args.timesteps,
            seed=args.seed,
            seeds=tuple(args.ensemble_seed) if args.ensemble_seed else None,
            exposure_levels=tuple(
                float(value)
                for value in portfolio_parameters.get(
                    "exposure_levels",
                    (0.0, 0.10, 0.20, 0.35, 0.50),
                )
            ),
            top_k=int(portfolio_parameters.get("top_k", 32)),
            turnover_penalty=float(
                args.turnover_penalty
                if args.turnover_penalty is not None
                else portfolio_parameters.get("turnover_penalty", 0.002)
            ),
            downside_penalty=float(
                args.downside_penalty
                if args.downside_penalty is not None
                else portfolio_parameters.get("downside_penalty", 0.25)
            ),
            volatility_penalty=float(
                args.volatility_penalty
                if args.volatility_penalty is not None
                else portfolio_parameters.get("volatility_penalty", 0.05)
            ),
            drawdown_penalty=float(
                args.drawdown_penalty
                if args.drawdown_penalty is not None
                else portfolio_parameters.get("drawdown_penalty", 0.05)
            ),
            underwater_penalty=float(
                args.underwater_penalty
                if args.underwater_penalty is not None
                else portfolio_parameters.get("underwater_penalty", 0.01)
            ),
            missed_upside_penalty=float(
                args.missed_upside_penalty
                if args.missed_upside_penalty is not None
                else portfolio_parameters.get("missed_upside_penalty", 0.05)
            ),
            concentration_penalty=float(
                args.concentration_penalty
                if args.concentration_penalty is not None
                else portfolio_parameters.get("concentration_penalty", 0.0)
            ),
            allocation_mode=str(portfolio_parameters.get("allocation_mode", "exposure")),
            initial_cash=args.training_cash,
            feature_schema=str(portfolio_parameters.get("feature_schema", "legacy")),
            lookback_window=int(portfolio_parameters.get("lookback_window", 20)),
            rollout_length=(
                int(portfolio_parameters["rollout_length"])
                if portfolio_parameters.get("rollout_length") is not None
                else None
            ),
            random_rollout=bool(portfolio_parameters.get("random_rollout", False)),
            max_target_turnover_pct=(
                float(portfolio_parameters["max_target_turnover_pct"])
                if portfolio_parameters.get("max_target_turnover_pct") is not None
                else None
            ),
            attention_features_dim=int(portfolio_parameters.get("attention_features_dim", 64)),
            attention_embed_dim=int(portfolio_parameters.get("attention_embed_dim", 32)),
            attention_num_heads=int(portfolio_parameters.get("attention_num_heads", 4)),
            attention_num_layers=(
                int(portfolio_parameters["attention_num_layers"])
                if portfolio_parameters.get("attention_num_layers") is not None
                else None
            ),
        )
        payload = {
            "status": "trained",
            "source": args.source,
            "runtime": {
                "runtime_id": snapshot.config.runtime_id,
                "config_version": snapshot.version,
                "config_path": str(args.config),
                "sleeve_id": args.sleeve_id,
            },
            "rl": result.to_dict(),
            "portfolio_model": {
                "ref": sleeve_config.portfolio.model.to_dict(),
                "parameters": dict(sleeve_config.portfolio.parameters),
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "fundamentals-import-fdr":
        as_of = _parse_cli_datetime(args.as_of) or datetime.now()
        universe = load_universe_definition(args.universe) if args.universe is not None else None
        market = args.market or (universe.market if universe is not None else "KRX")
        provider = FinanceDataReaderFundamentalProvider(
            market=market,
            include_naver_valuation=args.include_naver_valuation,
        )
        symbols = _fundamental_import_symbols(universe, args.symbol, market)
        names = tuple(args.name) if args.name else None
        values = provider.current_values(
            symbols=symbols,
            as_of=as_of,
            names=names,
            include_naver_valuation=args.include_naver_valuation,
        )
        source = provider.source_name(args.include_naver_valuation)
        artifact = FundamentalArtifact.from_values(
            market=market,
            as_of=as_of,
            source=source,
            values=values,
            metadata={
                "provider": "FinanceDataReader",
                "include_naver_valuation": args.include_naver_valuation,
                "requested_symbols": [symbol.key for symbol in symbols] if symbols is not None else [],
                "requested_names": list(names or []),
                "universe_id": universe.id if universe is not None else None,
                "universe_path": str(args.universe) if args.universe is not None else None,
            },
        )
        store = FileFundamentalArtifactStore(args.root)
        path = store.write(artifact, overwrite=args.overwrite)
        payload: dict[str, object] = {
            "status": "written",
            "root": str(store.root),
            "artifact": artifact.summary(path=path),
        }
        if not args.summary_only:
            payload["values"] = artifact.to_dict()["values"]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "fundamentals-status":
        store = FileFundamentalArtifactStore(args.root)
        as_of = _parse_cli_datetime(args.as_of)
        if as_of is not None:
            if not args.market:
                raise RuntimeError("--market is required when --as-of is provided.")
            record = store.read(market=args.market, as_of=as_of)
            payload = {
                "status": "ok",
                "root": str(store.root),
                "artifact": record.summary(),
            }
            if not args.summary_only:
                payload["values"] = record.artifact.to_dict()["values"]
        else:
            payload = store.status(
                market=args.market,
                include_artifacts=args.include_artifacts and not args.summary_only,
            )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "warmup-indicators-daily":
        universe = load_universe_definition(args.universe)
        provider = KISCachedMarketDataProvider.from_env()
        _attach_exchange_map(provider, universe)
        result = run_daily_indicator_warmup(
            universe,
            provider,
            sleeve_id=args.sleeve_id,
            start=_parse_cli_datetime(args.start),
            end=_parse_cli_datetime(args.end),
            refresh_history=args.refresh_history,
            source=args.source,
            policy=WarmupPolicy(extra_bars=args.extra_bars, min_ready_ratio=args.min_ready_ratio),
        )
        print(json.dumps(result.report.to_dict(include_symbols=not args.summary_only), ensure_ascii=False, indent=2))
        return 0
    if args.command == "select-active-universe":
        universe = load_universe_definition(args.universe)
        provider = KISCachedMarketDataProvider.from_env()
        _attach_exchange_map(provider, universe)
        warmup_result = run_daily_indicator_warmup(
            universe,
            provider,
            sleeve_id=args.sleeve_id,
            start=_parse_cli_datetime(args.start),
            end=_parse_cli_datetime(args.end),
            refresh_history=args.refresh_history,
            source=args.source,
            policy=WarmupPolicy(extra_bars=args.extra_bars, min_ready_ratio=args.min_ready_ratio),
        )
        indicator_snapshot = warmup_result.indicator_engine.snapshot(
            args.sleeve_id,
            universe_id=universe.id,
        )
        model = MomentumUniverseSelectionModel(
            max_active_symbols=args.top_n,
            price_indicator=args.price_indicator,
            moving_average_indicator=args.moving_average_indicator,
            momentum_indicator=args.momentum_indicator,
            liquidity_indicator=args.liquidity_indicator,
            volatility_indicator=args.volatility_indicator if args.volatility_indicator else None,
            require_positive_momentum=not args.allow_negative_momentum,
            require_price_above_average=args.require_price_above_average,
            min_liquidity=args.min_liquidity,
            max_volatility=args.max_volatility,
        )
        context = UniverseSelectionContext(
            sleeve_id=args.sleeve_id,
            universe=universe,
            indicator_snapshot=indicator_snapshot,
            previous_live_symbols=_parse_symbol_refs(args.previous_live, universe.market),
            held_symbols=_parse_symbol_refs(args.held, universe.market),
            open_order_symbols=_parse_symbol_refs(args.open_order, universe.market),
            exit_watch_symbols=_parse_symbol_refs(args.exit_watch, universe.market),
            manual_symbols=_parse_symbol_refs(args.manual, universe.market),
        )
        selection = model.select(context)
        report = {
            "warmup": warmup_result.report.to_dict(include_symbols=False),
            "selection": selection.to_dict(include_candidates=not args.summary_only),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "live-indicators-once":
        universe = load_universe_definition(args.universe)
        provider = MarketDataEngineLiveQuoteProvider.from_env(
            exchange_by_symbol=_exchange_map_from_universe(universe),
            rate_limit_per_second=args.rate_limit_per_second,
        )
        report = run_live_indicator_snapshot(
            universe,
            provider,
            sleeve_id=args.sleeve_id,
            source=args.source,
            min_success=args.min_success,
            include_failures=args.include_failures,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "fine-universe-refresh":
        universe = load_universe_definition(args.universe)
        provider = MarketDataEngineLiveQuoteProvider.from_env(
            exchange_by_symbol=_exchange_map_from_universe(universe),
            rate_limit_per_second=args.rate_limit_per_second,
        )
        runtime = FineUniverseRuntime(
            universe=universe,
            provider=provider,
            source=args.source,
            max_age_seconds=args.max_age_seconds,
        )
        report = runtime.refresh_once(max_symbols=args.max_symbols, min_success=args.min_success)
        output = {
            "refresh": report.to_dict(),
            "fine_universe": {
                "universe_id": runtime.fine_universe_definition().id,
                "symbol_count": len(runtime.fine_universe_definition().symbols),
                "symbols": [symbol.key for symbol in runtime.fine_universe_definition().symbols],
            },
        }
        if args.include_entries:
            output["entries"] = runtime.cache.to_dict(max_age_seconds=args.max_age_seconds)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    if args.command == "snapshot-worker-run":
        universe = load_universe_definition(args.universe)
        live_provider = MarketDataEngineLiveQuoteProvider.from_env(
            exchange_by_symbol=_exchange_map_from_universe(universe),
            rate_limit_per_second=args.rate_limit_per_second,
        )
        history_provider = KISCachedMarketDataProvider.from_env()
        _attach_exchange_map(history_provider, universe)
        worker = BackgroundSnapshotWorker(
            universe=universe,
            sleeve_id=args.sleeve_id,
            live_provider=live_provider,
            history_provider=history_provider,
            source=args.source,
            history_source=args.history_source,
            min_success=args.min_success,
            interval_seconds=args.interval_seconds,
            warmup_policy=WarmupPolicy(extra_bars=args.extra_bars, min_ready_ratio=args.min_ready_ratio),
        )
        report = worker.run(
            max_cycles=args.cycles,
            warmup=not args.skip_warmup,
            warmup_start=_parse_cli_datetime(args.warmup_start),
            warmup_end=_parse_cli_datetime(args.warmup_end),
            refresh_history=args.refresh_history,
        )
        print(
            json.dumps(
                report.to_dict(
                    include_warmup_symbols=not args.summary_only,
                    include_failures=not args.summary_only,
                ),
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "active-snapshot-worker-run":
        coarse_universe = load_universe_definition(args.universe)
        history_provider = KISCachedMarketDataProvider.from_env()
        _attach_exchange_map(history_provider, coarse_universe)
        live_provider = MarketDataEngineLiveQuoteProvider.from_env(
            exchange_by_symbol=_exchange_map_from_universe(coarse_universe),
            rate_limit_per_second=args.rate_limit_per_second,
        )
        fine_refresh_report = None
        selection_base_universe = coarse_universe
        if args.fine_refresh:
            fine_runtime = FineUniverseRuntime(
                universe=coarse_universe,
                provider=live_provider,
                source=args.source,
                max_age_seconds=args.fine_max_age_seconds,
            )
            fine_refresh_report = fine_runtime.refresh_once(
                max_symbols=args.fine_max_symbols,
                min_success=args.fine_min_success,
            )
            selection_base_universe = fine_runtime.fine_universe_definition(
                universe_id=f"{coarse_universe.id}-fine",
            )
        selection_warmup_report = None
        indicator_snapshot = None
        if args.selection == "momentum":
            selection_warmup = run_daily_indicator_warmup(
                selection_base_universe,
                history_provider,
                sleeve_id=args.sleeve_id,
                start=_parse_cli_datetime(args.start),
                end=_parse_cli_datetime(args.end),
                refresh_history=args.refresh_history,
                source=args.history_source,
                policy=WarmupPolicy(extra_bars=args.extra_bars, min_ready_ratio=args.min_ready_ratio),
            )
            selection_warmup_report = selection_warmup.report
            indicator_snapshot = selection_warmup.indicator_engine.snapshot(
                args.sleeve_id,
                universe_id=selection_base_universe.id,
            )
            selection_model = MomentumUniverseSelectionModel(max_active_symbols=args.top_n)
        else:
            selection_model = StaticUniverseSelectionModel(max_active_symbols=args.top_n)
        selection_runtime = UniverseSelectionRuntime(
            coarse_universe=selection_base_universe,
            selection_model=selection_model,
        )
        active = selection_runtime.select_active(
            sleeve_id=args.sleeve_id,
            indicator_snapshot=indicator_snapshot,
            held_symbols=_parse_symbol_refs(args.held, coarse_universe.market),
            open_order_symbols=_parse_symbol_refs(args.open_order, coarse_universe.market),
            exit_watch_symbols=_parse_symbol_refs(args.exit_watch, coarse_universe.market),
            manual_symbols=_parse_symbol_refs(args.manual, coarse_universe.market),
            active_universe_id=f"{selection_base_universe.id}-active",
        )
        worker = BackgroundSnapshotWorker(
            universe=active.active_universe,
            sleeve_id=args.sleeve_id,
            live_provider=live_provider,
            history_provider=history_provider,
            source=args.source,
            history_source=args.history_source,
            min_success=args.min_success,
            interval_seconds=args.interval_seconds,
            warmup_policy=WarmupPolicy(extra_bars=args.extra_bars, min_ready_ratio=args.min_ready_ratio),
        )
        run_report = worker.run(
            max_cycles=args.cycles,
            warmup=not args.skip_worker_warmup,
            warmup_start=_parse_cli_datetime(args.start),
            warmup_end=_parse_cli_datetime(args.end),
            refresh_history=args.refresh_history,
        )
        report = {
            "coarse_universe_id": coarse_universe.id,
            "fine_universe_id": selection_base_universe.id if args.fine_refresh else None,
            "active_universe_id": active.active_universe.id,
            "fine_refresh": fine_refresh_report.to_dict() if fine_refresh_report is not None else None,
            "selection_warmup": (
                selection_warmup_report.to_dict(include_symbols=False)
                if selection_warmup_report is not None
                else None
            ),
            "selection": active.selection.to_dict(include_candidates=not args.summary_only),
            "worker": run_report.to_dict(
                include_warmup_symbols=not args.summary_only,
                include_failures=not args.summary_only,
            ),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "alpha-run-snapshot":
        universe = load_universe_definition(args.universe)
        live_provider = MarketDataEngineLiveQuoteProvider.from_env(
            exchange_by_symbol=_exchange_map_from_universe(universe),
            rate_limit_per_second=args.rate_limit_per_second,
        )
        history_provider = KISCachedMarketDataProvider.from_env()
        _attach_exchange_map(history_provider, universe)
        alpha_load = PythonAlphaLoader().load(args.alpha)
        alpha_runtime = AlphaRuntime()
        worker = BackgroundSnapshotWorker(
            universe=universe,
            sleeve_id=args.sleeve_id,
            live_provider=live_provider,
            history_provider=history_provider,
            source=args.source,
            history_source=args.history_source,
            min_success=args.min_success,
            interval_seconds=0.0,
            warmup_policy=WarmupPolicy(extra_bars=args.extra_bars, min_ready_ratio=args.min_ready_ratio),
        )
        if not args.skip_warmup:
            worker.warm_up(
                start=_parse_cli_datetime(args.warmup_start),
                end=_parse_cli_datetime(args.warmup_end),
                refresh_history=args.refresh_history,
            )
        latest_snapshot = worker.run_once()
        active_indicator_snapshot = worker.stores_by_sleeve[args.sleeve_id].active()
        if active_indicator_snapshot is None:
            raise RuntimeError("No active indicator snapshot was published.")
        context = SnapshotContext.from_indicator_snapshot(active_indicator_snapshot)
        alpha_runtime.stage([alpha_load.model], validation_context=context)
        insight_batch = alpha_runtime.run(context, activate_pending=True, publish_active=True)
        report = {
            "alpha": {
                "alpha_id": alpha_load.alpha_id,
                "version": alpha_load.version,
                "path": str(alpha_load.path),
                "content_hash": alpha_load.content_hash,
            },
            "cycle": latest_snapshot.to_dict(include_failures=not args.summary_only),
            "insights": insight_batch.to_dict(),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    return 1


def _order_to_json(order: OrderIntent) -> dict[str, object]:
    return {
        "sleeve_id": order.sleeve_id,
        "symbol": order.symbol.ticker,
        "market": order.symbol.market,
        "side": order.side.value,
        "quantity": order.quantity,
        "reference_price": order.reference_price,
        "notional": order.notional,
        "tag": order.tag,
    }


def _indicator_values_to_json(indicator_engine, sleeve_id: str) -> dict[str, object]:
    symbols = indicator_engine.symbols_for_sleeve(sleeve_id)
    return {
        "sleeve_id": sleeve_id,
        "symbols": [symbol.key for symbol in symbols],
        "values": indicator_engine.values_for(sleeve_id, symbols, ready_only=False),
    }


def _parse_cli_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if len(text) == 8 and text.isdigit():
        return datetime.strptime(text, "%Y%m%d")
    return datetime.fromisoformat(text)


def _parse_cli_time(value: str | None) -> clock_time | None:
    if not value:
        return None
    text = value.strip()
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, pattern).time()
        except ValueError:
            continue
    raise RuntimeError(f"Time must be HH:MM or HH:MM:SS: {value}")


def _parse_cli_minute_start(value: str) -> datetime:
    parsed = _parse_cli_datetime(value)
    if parsed is None:
        raise RuntimeError("--start is required.")
    return parsed


def _notification_message_from_args(args) -> str:
    sources = [
        bool(getattr(args, "message", None)),
        getattr(args, "message_file", None) is not None,
        bool(getattr(args, "message_stdin", False)),
    ]
    if sum(1 for enabled in sources if enabled) != 1:
        raise RuntimeError("Pass exactly one of --message, --message-file, or --message-stdin.")
    if getattr(args, "message_file", None) is not None:
        return args.message_file.read_text(encoding="utf-8-sig")
    if getattr(args, "message_stdin", False):
        return sys.stdin.read()
    return str(args.message)


def _parse_cli_minute_end(value: str) -> datetime:
    parsed = _parse_cli_datetime(value)
    if parsed is None:
        raise RuntimeError("--end is required.")
    text = value.strip()
    if (len(text) == 8 and text.isdigit()) or (len(text) == 10 and "T" not in text and ":" not in text):
        return parsed + timedelta(days=1) - timedelta(microseconds=1)
    return parsed


def _normalize_minute_cache_session_range(
    start: datetime,
    end: datetime,
    *,
    market: str,
    start_text: str,
    end_text: str,
    include_extended_hours: bool,
) -> tuple[datetime, datetime]:
    scope = market_scope_from_market(market)
    if scope == "overseas":
        regular_start = clock_time(9, 30)
        regular_end = clock_time(16, 0)
        extended_start = clock_time(4, 0)
        extended_end = clock_time(20, 0)
    else:
        regular_start = clock_time(9, 0)
        regular_end = clock_time(15, 30)
        extended_start = clock_time(8, 30)
        extended_end = clock_time(18, 0)
    start_clock = extended_start if include_extended_hours else regular_start
    end_clock = extended_end if include_extended_hours else regular_end
    if _is_cli_date_only(start_text):
        start = start.replace(hour=start_clock.hour, minute=start_clock.minute, second=0, microsecond=0)
    if _is_cli_date_only(end_text):
        end = end.replace(hour=end_clock.hour, minute=end_clock.minute, second=0, microsecond=0)
    return start, end


def _minute_cache_session_time_bounds(market: str, *, include_extended_hours: bool) -> tuple[str, str]:
    scope = market_scope_from_market(market)
    if scope == "overseas":
        return ("04:00:00", "20:00:00") if include_extended_hours else ("09:30:00", "16:00:00")
    return ("08:30:00", "18:00:00") if include_extended_hours else ("09:00:00", "15:30:00")


def _is_cli_date_only(value: str) -> bool:
    text = str(value or "").strip()
    return (len(text) == 8 and text.isdigit()) or (len(text) == 10 and "T" not in text and ":" not in text)


def _load_minute_feed_source_universe(source: Path, sleeve_id: str | None):
    if sleeve_id:
        snapshot = load_runtime_config_snapshot(source)
        sleeve_config = snapshot.config.sleeve(sleeve_id)
        universe_path = resolve_runtime_path(snapshot, sleeve_config.universe.coarse_path)
        return load_universe_definition(universe_path), {
            "source_type": "runtime_config",
            "config_path": str(source),
            "runtime_id": snapshot.config.runtime_id,
            "config_version": snapshot.version,
            "sleeve_id": sleeve_id,
            "universe_path": str(universe_path),
        }
    universe_path = source
    return load_universe_definition(universe_path), {
        "source_type": "universe",
        "universe_path": str(universe_path),
    }


def _runtime_minute_backtest_command_for_feed(
    source_info: dict[str, object],
    *,
    output: Path,
    start: str,
    end: str,
) -> list[str]:
    if source_info.get("source_type") != "runtime_config":
        return []
    config_path = str(source_info.get("config_path") or "")
    sleeve_id = str(source_info.get("sleeve_id") or "")
    if not config_path or not sleeve_id:
        return []
    return [
        "runtime-backtest-minute",
        config_path,
        "--sleeve-id",
        sleeve_id,
        "--minute-feed",
        str(output),
        "--start",
        start,
        "--end",
        end,
    ]


def _runtime_minute_backtest_command_for_cache(
    source_info: dict[str, object],
    *,
    cache_root: Path,
    start: str,
    end: str,
) -> list[str]:
    if source_info.get("source_type") != "runtime_config":
        return []
    config_path = str(source_info.get("config_path") or "")
    sleeve_id = str(source_info.get("sleeve_id") or "")
    if not config_path or not sleeve_id:
        return []
    return [
        "runtime-backtest-minute",
        config_path,
        "--sleeve-id",
        sleeve_id,
        "--minute-cache-root",
        str(cache_root),
        "--start",
        start,
        "--end",
        end,
    ]


def _minute_cache_export_command_for_source(
    source_info: dict[str, object],
    *,
    cache_root: Path,
    start: str,
    end: str,
) -> list[str]:
    command = ["minute-cache-export"]
    if source_info.get("source_type") == "runtime_config":
        command.extend([str(source_info.get("config_path") or "")])
        sleeve_id = str(source_info.get("sleeve_id") or "")
        if sleeve_id:
            command.extend(["--sleeve-id", sleeve_id])
    else:
        command.extend([str(source_info.get("universe_path") or "")])
    command.extend(
        [
            "--cache-root",
            str(cache_root),
            "--output",
            "data/replay/exported_minute_feed.csv",
            "--start",
            start,
            "--end",
            end,
        ]
    )
    return command


def _default_minute_timezone_for_market(market: str) -> str:
    normalized = str(market or "").strip().upper()
    if normalized in {"KR", "KRX", "KOSPI", "KOSDAQ"}:
        return "Asia/Seoul"
    if normalized in {"US", "NAS", "NYS", "AMS"}:
        return "America/New_York"
    return "UTC"


def _kis_exchange_map_for_universe(universe) -> dict[str, str]:
    exchange_by_symbol: dict[str, str] = {}
    for symbol in universe.symbols:
        market = symbol.market.strip().upper()
        exchange = universe.properties_for(symbol).get("exchange")
        if not exchange and market in {"NAS", "NYS", "AMS", "HKS", "TSE", "SHS", "SZS"}:
            exchange = market
        if exchange:
            normalized = str(exchange).strip().upper()
            exchange_by_symbol[symbol.key] = normalized
            exchange_by_symbol[symbol.ticker] = normalized
    return exchange_by_symbol


def _runtime_backtest_warmup_start(args, sleeve_config) -> datetime | None:
    explicit = _parse_cli_datetime(getattr(args, "warmup_start", None))
    if explicit is not None:
        return explicit
    evaluation_start = _parse_cli_datetime(getattr(args, "start", None))
    if evaluation_start is None:
        return None
    indicators = getattr(sleeve_config, "indicators", None)
    if indicators is None or not getattr(indicators, "warmup_enabled", False):
        return None
    extra_bars = int(getattr(indicators, "extra_bars", 0) or 0)
    if extra_bars <= 0:
        return None
    return evaluation_start - timedelta(days=max(extra_bars * 3, extra_bars + 7))


def _minute_backtest_warmup_end(feed, *, start: datetime | None) -> datetime | None:
    evaluation_start = start
    if evaluation_start is None and feed:
        evaluation_start = feed[0].time
    if evaluation_start is None:
        return None
    return evaluation_start - timedelta(days=1)


def _slice_replay_feed(feed, *, start: datetime | None, end: datetime | None):
    if start is None and end is None:
        return feed
    return [
        data
        for data in feed
        if (start is None or data.time >= start) and (end is None or data.time <= end)
    ]


def _feed_start(feed) -> str | None:
    if not feed:
        return None
    return min(data.time for data in feed).isoformat()


def _feed_end(feed) -> str | None:
    if not feed:
        return None
    return max(data.time for data in feed).isoformat()


def _daily_backtest_provider(source: str):
    if source == "finance-datareader":
        return FinanceDataReaderMarketDataProvider()
    if source == "kis-cache":
        return KISCachedMarketDataProvider.from_env()
    if source == "parquet-daily":
        return ParquetDailyBarProvider()
    raise ValueError(f"Unsupported daily backtest source: {source}")


def _perf_elapsed_ms(started: float) -> float:
    return round((perf_time.perf_counter() - started) * 1000.0, 3)


def _merge_backtest_timings(timings: dict[str, float], result) -> dict[str, float]:
    merged = dict(getattr(result, "timings", {}) or {})
    merged.update(timings)
    merged["framework_model_ms"] = round(float(getattr(result, "framework_total_ms", 0.0) or 0.0), 3)
    return {key: round(float(value), 3) for key, value in merged.items()}


def _resolve_backtest_journal_mode(args) -> str:
    mode = str(getattr(args, "journal_mode", "auto") or "auto").strip().lower()
    if mode == "auto":
        return "light" if bool(getattr(args, "summary_only", False)) else "full"
    return mode


def _validate_daily_warmup_cache_report(report, *, expected_start: datetime, expected_end: datetime | None) -> None:
    if report.start is not None and report.start != expected_start:
        raise RuntimeError(
            "--daily-warmup-cache start does not match requested warmup window; "
            "pass --refresh-daily-warmup-cache to rebuild it."
        )
    if report.end is not None and expected_end is not None and report.end != expected_end:
        raise RuntimeError(
            "--daily-warmup-cache end does not match requested warmup window; "
            "pass --refresh-daily-warmup-cache to rebuild it."
        )


def _history_cache_report(provider) -> dict[str, object]:
    cache_root = getattr(provider, "cache_root", None)
    if cache_root is None:
        return {}
    return {
        "provider": type(provider).__name__,
        "enabled": bool(getattr(provider, "cache_enabled", True)),
        "cache_root": str(cache_root),
    }


def _runtime_sleeve_universe_market(snapshot, sleeve_config) -> str:
    universe = load_universe_definition(resolve_runtime_path(snapshot, sleeve_config.universe.coarse_path))
    return universe.market


def _backtest_portfolio_from_sleeve(sleeve_config, *, cash: float | None, currency: str) -> Portfolio:
    code = str(currency or "KRW").strip().upper()
    if cash is not None:
        return Portfolio(cash=float(cash), cash_by_currency={code: float(cash)})
    cash_by_currency = {
        str(key).strip().upper(): float(value)
        for key, value in dict(getattr(sleeve_config, "cash_by_currency", {}) or {}).items()
        if float(value) != 0.0
    }
    if cash_by_currency:
        return Portfolio(cash=sum(cash_by_currency.values()), cash_by_currency=cash_by_currency)
    configured_cash = float(getattr(sleeve_config, "cash", 0.0) or 0.0)
    return Portfolio(cash=configured_cash, cash_by_currency={code: configured_cash} if configured_cash else {})


def _load_fundamentals_for_backtest(
    root: Path,
    *,
    market: str | None,
    end: datetime | None,
    names: tuple[str, ...] | None,
):
    if not market:
        raise RuntimeError("--fundamentals-market is required when the universe has no market.")
    artifact_store = FileFundamentalArtifactStore(root)
    pit_store, records = artifact_store.load_to_store(
        market=market,
        end=end,
        names=names,
    )
    if not records:
        raise RuntimeError(f"No fundamental artifacts found for market={market!r} under {root}.")
    return pit_store, {
        "root": str(root),
        "market": str(market).strip().upper(),
        "artifact_count": len(records),
        "requested_names": list(names or []),
        "artifacts": [record.summary() for record in records],
    }


def _exchange_map_from_universe(universe) -> dict[str, str]:
    exchange_by_symbol: dict[str, str] = {}
    for symbol in universe.symbols:
        exchange = universe.properties_for(symbol).get("exchange")
        if exchange:
            exchange_by_symbol[symbol.key] = str(exchange).strip().upper()
            exchange_by_symbol[symbol.ticker] = str(exchange).strip().upper()
    return exchange_by_symbol


def _attach_exchange_map(provider, universe) -> None:
    if hasattr(provider, "exchange_by_symbol"):
        provider.exchange_by_symbol = _exchange_map_from_universe(universe)


def _parse_symbol_refs(values: Sequence[str], default_market: str) -> tuple[Symbol, ...]:
    symbols: list[Symbol] = []
    for value in values:
        text = value.strip().upper()
        if not text:
            continue
        if ":" in text:
            market, ticker = text.split(":", 1)
            symbols.append(Symbol(ticker=ticker.strip(), market=market.strip()))
        else:
            symbols.append(Symbol(ticker=text, market=default_market))
    return tuple(symbols)


def _fundamental_import_symbols(universe, symbol_refs: Sequence[str], default_market: str) -> tuple[Symbol, ...] | None:
    symbols: dict[str, Symbol] = {}
    if universe is not None:
        for symbol in universe.symbols:
            symbols[symbol.key] = symbol
    for symbol in _parse_symbol_refs(symbol_refs, default_market):
        symbols[symbol.key] = symbol
    return tuple(symbols.values()) if symbols else None


def _parse_fill_allocations(values: Sequence[str]) -> tuple[tuple[str, int], ...]:
    allocations: list[tuple[str, int]] = []
    for value in values:
        text = value.strip()
        if "=" not in text:
            raise ValueError("allocation must use sleeve_id=quantity format.")
        sleeve_id, quantity_text = text.split("=", 1)
        sleeve_id = sleeve_id.strip()
        quantity = int(quantity_text.strip())
        if not sleeve_id or quantity <= 0:
            raise ValueError("allocation sleeve_id and positive quantity are required.")
        allocations.append((sleeve_id, quantity))
    return tuple(allocations)


def _portfolio_to_json(portfolio: Portfolio) -> dict[str, object]:
    return {
        "cash": portfolio.cash,
        "cash_by_currency": dict(portfolio.cash_by_currency),
        "holding_count": len(portfolio.holdings),
        "holdings": [
            {
                "symbol": holding.symbol.ticker,
                "market": holding.symbol.market,
                "quantity": holding.quantity,
                "average_price": holding.average_price,
            }
            for holding in portfolio.holdings.values()
        ],
    }


@dataclass(frozen=True, slots=True)
class _OrderRuntimeRoute:
    account_id: str | None
    market_scope: str | None
    currency: str
    account_store_path: Path
    order_store_path: Path


def _runtime_journal_path(snapshot, explicit_path: Path | None) -> Path | None:
    if explicit_path is not None:
        return resolve_runtime_path(snapshot, explicit_path).resolve()
    journal_path = getattr(snapshot.config, "journal_path", None)
    if journal_path is None:
        return None
    return resolve_runtime_path(snapshot, journal_path).resolve()


def _runtime_journal_store(snapshot, explicit_path: Path | None):
    journal_path = _runtime_journal_path(snapshot, explicit_path)
    return FileCycleJournalStore(journal_path) if journal_path is not None else None


def _build_runtime_artifact_status_report(
    snapshot,
    *,
    sleeve_ids: tuple[str, ...],
    active_sleeve_ids: tuple[str, ...],
    active_sleeves_path: Path,
    active_sleeves_payload: object,
    control_queue_path: Path,
    framework_state_dir: Path,
    order_batch_path: Path,
    live_loop_log_path: Path,
    live_loop_heartbeat_path: Path,
    runtime_state_path: Path,
    submit_state_path: Path,
    report_dir: Path,
    eod_snapshot_root: Path,
    eod_state_dir: Path,
    startup_status_path: Path,
    include_details: bool = True,
) -> dict[str, object]:
    selected_sleeves = tuple(snapshot.config.sleeve(sleeve_id) for sleeve_id in sleeve_ids)
    journal_path = _runtime_journal_path(snapshot, None)
    routes = _resolve_order_runtime_routes(snapshot, None, None, None, sleeve_ids)
    warnings: list[str] = []

    route_payloads = []
    for route in routes:
        route_sleeve_ids = tuple(
            sleeve.sleeve_id
            for sleeve in selected_sleeves
            if route.account_id is None
            or route.account_id in configured_account_ids_for_sleeve(sleeve)
        )
        if not route.account_store_path.exists():
            warnings.append(f"missing_account_store:{route.account_id or 'default'}:{route.account_store_path}")
        if not route.order_store_path.exists():
            warnings.append(f"missing_order_store:{route.account_id or 'default'}:{route.order_store_path}")
        route_payloads.append(
            {
                "account_id": route.account_id,
                "market_scope": route.market_scope,
                "currency": route.currency,
                "sleeve_ids": list(route_sleeve_ids),
                "account_store": _path_status(route.account_store_path),
                "order_store": _path_status(route.order_store_path),
            }
        )

    sleeve_payloads = [
        _runtime_artifact_status_for_sleeve(
            snapshot,
            sleeve,
            active_sleeve_ids=active_sleeve_ids,
            framework_state_dir=framework_state_dir,
            report_dir=report_dir,
        )
        for sleeve in selected_sleeves
    ]
    for sleeve_payload in sleeve_payloads:
        framework_state = sleeve_payload.get("framework_state")
        if isinstance(framework_state, Mapping) and not framework_state.get("exists"):
            warnings.append(f"missing_framework_state:{sleeve_payload.get('sleeve_id')}:{framework_state.get('path')}")

    if not active_sleeves_path.exists():
        warnings.append(f"missing_active_sleeves_file:{active_sleeves_path}")
    if journal_path is not None and not journal_path.exists():
        warnings.append(f"missing_cycle_journal:{journal_path}")

    active_payload: dict[str, object] = {
        "path": _path_status(active_sleeves_path),
        "active_sleeve_ids": list(active_sleeve_ids),
    }
    if isinstance(active_sleeves_payload, Mapping):
        active_payload.update(
            {
                "updated_at": active_sleeves_payload.get("updated_at"),
                "source": active_sleeves_payload.get("source"),
                "config": active_sleeves_payload.get("config"),
                "hot_reload": active_sleeves_payload.get("hot_reload"),
            }
        )

    market_snapshot_store = (
        _path_status(resolve_runtime_path(snapshot, snapshot.config.market_data.snapshot_store_path).resolve())
        if snapshot.config.market_data.snapshot_store_path is not None
        else None
    )
    report: dict[str, object] = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "read_only": True,
        "runtime_id": snapshot.config.runtime_id,
        "mode": snapshot.config.mode,
        "config": {
            "path": _path_status(snapshot.source_path.resolve()),
            "version": snapshot.version,
            "loaded_at": snapshot.loaded_at,
        },
        "selected_sleeve_ids": list(sleeve_ids),
        "active_sleeves": active_payload,
        "market_data": {
            "provider": snapshot.config.market_data.provider,
            "history_provider": snapshot.config.market_data.history_provider,
            "rate_limit_per_second": snapshot.config.market_data.rate_limit_per_second,
            "gateway_base_url": snapshot.config.market_data.gateway_base_url,
            "snapshot_store": market_snapshot_store,
        },
        "journal": _path_status(journal_path) if journal_path is not None else None,
        "runtime_control": {
            "control_queue": _path_status(control_queue_path),
        },
        "live_order_loop": {
            "log": _path_status(live_loop_log_path),
            "heartbeat": {
                "path": _path_status(live_loop_heartbeat_path),
                "payload": _read_runtime_json(live_loop_heartbeat_path),
            },
            "order_batch": _path_status(order_batch_path),
            "runtime_run_latest": _path_status(live_loop_log_path.parent / "multi_sleeve_runtime_run_latest.json"),
            "submit_latest": _path_status(live_loop_log_path.parent / "multi_sleeve_submit_latest.json"),
            "submit_state": _path_status(submit_state_path),
            "runtime_state": _path_status(runtime_state_path),
            "framework_state_dir": _path_status(framework_state_dir),
            "start_stdout": _path_status(live_loop_log_path.parent / "multi_sleeve_start_stdout.log"),
            "start_stderr": _path_status(live_loop_log_path.parent / "multi_sleeve_start_stderr.log"),
        },
        "portfolio_reports": {
            "directory": _path_status(report_dir),
        },
        "eod_snapshots": {
            "snapshot_root": _path_status(eod_snapshot_root),
            "state_dir": _path_status(eod_state_dir),
        },
        "startup": {
            "safe_start_status": _path_status(startup_status_path),
        },
        "routes": route_payloads,
        "sleeves": sleeve_payloads,
        "summary": {
            "selected_sleeve_count": len(sleeve_ids),
            "active_selected_sleeve_count": sum(1 for sleeve_id in sleeve_ids if sleeve_id in active_sleeve_ids),
            "route_count": len(route_payloads),
            "warning_count": len(warnings),
            "warnings": warnings,
        },
    }
    if include_details:
        report["operator_rules"] = {
            "strategy_workspace": "sleeves/<sleeve_id> contains strategy code and sleeve docs only.",
            "runtime_config": "configs/runtime/*.json owns live wiring and account routes.",
            "runtime_artifacts": "data/runtime, data/order-runtime, data/virtual-accounts, data/cycle-journal, and data/eod-snapshots are runtime state/read models.",
            "safety": "This command is read-only and does not sync KIS, run models, submit orders, or mutate virtual accounts.",
        }
    return report


def _runtime_artifact_status_for_sleeve(
    snapshot,
    sleeve,
    *,
    active_sleeve_ids: tuple[str, ...],
    framework_state_dir: Path,
    report_dir: Path,
) -> dict[str, object]:
    workspace = resolve_runtime_path(snapshot, sleeve.workspace_path).resolve()
    safe_id = _safe_sleeve_filename(sleeve.sleeve_id)
    latest_report = _latest_matching_path(report_dir, f"{sleeve.sleeve_id}_runtime_*.json")
    report_log_name = _portfolio_report_log_name(sleeve.sleeve_id)
    routes = []
    route_map = dict(sleeve.broker_account_routes)
    if not route_map and sleeve.broker_account_id:
        route_map = {"default": sleeve.broker_account_id}
    for market_scope, account_id in route_map.items():
        try:
            account = snapshot.config.broker_account(account_id)
            routes.append(
                {
                    "market_scope": market_scope,
                    "account_id": account.account_id,
                    "currency": account.currency,
                    "broker_gateway": account.broker_gateway,
                    "account_store": _path_status(resolve_runtime_path(snapshot, account.account_store_path).resolve()),
                    "order_store": _path_status(resolve_runtime_path(snapshot, account.order_store_path).resolve())
                    if account.order_store_path is not None
                    else None,
                }
            )
        except KeyError:
            routes.append({"market_scope": market_scope, "account_id": account_id, "error": "unknown_broker_account"})
    return {
        "sleeve_id": sleeve.sleeve_id,
        "active": sleeve.sleeve_id in active_sleeve_ids,
        "workspace": _path_status(workspace),
        "strategy_doc": _path_status(workspace / "STRATEGY.md"),
        "agents_doc": _path_status(workspace / "AGENTS.md"),
        "readme": _path_status(workspace / "README.md"),
        "framework_state": _path_status(framework_state_dir / f"{safe_id}.json"),
        "latest_portfolio_report": _path_status(latest_report) if latest_report is not None else None,
        "report_loop": {
            "log": _path_status(report_dir / f"{report_log_name}.log"),
            "state": _path_status(report_dir / f"{report_log_name}.state.json"),
            "start_stdout": _path_status(report_dir / f"{report_log_name}_start_stdout.log"),
            "start_stderr": _path_status(report_dir / f"{report_log_name}_start_stderr.log"),
        },
        "routes": routes,
    }


def _path_status(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    try:
        stat = resolved.stat()
    except OSError:
        return {"path": str(resolved), "exists": False}
    return {
        "path": str(resolved),
        "exists": True,
        "kind": "directory" if resolved.is_dir() else "file",
        "size_bytes": stat.st_size if resolved.is_file() else None,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
    }


def _cwd_path(path: Path | None) -> Path | None:
    if path is None:
        return None
    candidate = Path(path)
    return candidate if candidate.is_absolute() else (Path.cwd() / candidate).resolve()


def _read_runtime_json(path: Path | None) -> object:
    if path is None:
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}


def _active_sleeve_ids_from_payload(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, Mapping):
        return ()
    raw = payload.get("active_sleeve_ids")
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if str(item).strip())


def _latest_matching_path(root: Path, pattern: str) -> Path | None:
    if not root.exists():
        return None
    matches = [path for path in root.glob(pattern) if path.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _safe_sleeve_filename(sleeve_id: str) -> str:
    return "".join(char if char.isalnum() or char in "._-" else "_" for char in sleeve_id)


def _portfolio_report_log_name(sleeve_id: str) -> str:
    known = {
        "LEaps": "LEaps_portfolio_report_loop",
        "kr-lowvol-defensive": "kr_lowvol_defensive_portfolio_report_loop",
        "us_etf_rotation": "us_etf_rotation_portfolio_report_loop",
    }
    if sleeve_id in known:
        return known[sleeve_id]
    return f"{_safe_sleeve_filename(sleeve_id).replace('-', '_')}_portfolio_report_loop"


def _runtime_state_store_from_args(snapshot, explicit_path: Path | None, *, read_only: bool):
    if explicit_path is None:
        return None, None
    path = resolve_runtime_path(snapshot, explicit_path).resolve()
    return SQLiteRuntimeStateStore(path), {
        "path": str(path),
        "store": "sqlite",
        "read_only": bool(read_only),
    }


def _status_sleeve_ids(snapshot, requested_sleeve_ids: Sequence[str]) -> tuple[str, ...]:
    if requested_sleeve_ids:
        for sleeve_id in requested_sleeve_ids:
            snapshot.config.sleeve(sleeve_id)
        return tuple(dict.fromkeys(requested_sleeve_ids))
    return tuple(sleeve.sleeve_id for sleeve in snapshot.config.sleeves)


def _default_cash_by_sleeve(
    snapshot,
    currency: str | None = None,
    account_id: str | None = None,
) -> dict[str, float]:
    result: dict[str, float] = {}
    code = str(currency or "").strip().upper()
    for sleeve in snapshot.config.sleeves:
        cash_by_currency = dict(getattr(sleeve, "cash_by_currency", {}) or {})
        if code and cash_by_currency:
            result[sleeve.sleeve_id] = float(cash_by_currency.get(code, 0.0) or 0.0)
            continue
        if code and dict(getattr(sleeve, "broker_account_routes", {}) or {}) and account_id != getattr(sleeve, "broker_account_id", None):
            result[sleeve.sleeve_id] = 0.0
            continue
        result[sleeve.sleeve_id] = sleeve.cash
    return result


def _sleeve_default_currency(snapshot, sleeve_config) -> str:
    account_id = getattr(sleeve_config, "broker_account_id", None)
    if account_id:
        try:
            return snapshot.config.broker_account(account_id).currency
        except KeyError:
            return currency_for_market_scope(None)
    return currency_for_market_scope(None)


def _broker_account_for_sleeve_market(snapshot, sleeve_config, market_scope: str | None = None):
    routes = dict(getattr(sleeve_config, "broker_account_routes", {}) or {})
    account_id = routes.get(str(market_scope or "").strip().lower()) or getattr(sleeve_config, "broker_account_id", None)
    if account_id is None and len(set(routes.values())) == 1:
        account_id = next(iter(routes.values()))
    if account_id is None:
        return None
    try:
        return snapshot.config.broker_account(account_id)
    except KeyError as exc:
        raise RuntimeError(f"Sleeve '{sleeve_config.sleeve_id}' references unknown broker_account_id: {account_id}") from exc


def _resolve_sleeve_account_store_path(snapshot, sleeve_config, market_scope: str | None = None) -> Path:
    account = _broker_account_for_sleeve_market(snapshot, sleeve_config, market_scope)
    if account is not None:
        return resolve_runtime_path(snapshot, account.account_store_path).resolve()
    if sleeve_config.portfolio.account_store_path is not None:
        return resolve_runtime_path(snapshot, sleeve_config.portfolio.account_store_path).resolve()
    raise RuntimeError("broker_accounts.account_store_path or portfolio.account_store_path is required.")


def _build_order_runtime_status_for_route(snapshot, route: _OrderRuntimeRoute, sleeve_ids: tuple[str, ...], *, recent_events: int):
    return build_order_runtime_status(
        runtime_id=snapshot.config.runtime_id,
        sleeve_ids=sleeve_ids,
        order_state_store=FileOrderRuntimeStateStore(route.order_store_path),
        account_store=VirtualSleeveAccountStore(
            route.account_store_path,
            default_cash_by_sleeve=_default_cash_by_sleeve(snapshot, route.currency, route.account_id),
            default_currency=route.currency,
        ),
        order_store_path=route.order_store_path,
        account_store_path=route.account_store_path,
        broker_account_id=route.account_id,
        market_scope=route.market_scope,
        currency=route.currency,
        recent_events=recent_events,
    )


def _order_runtime_filled_positions(order_store_path: Path, *, sleeve_id: str | None = None) -> list[dict[str, object]]:
    snapshot = FileOrderRuntimeStateStore(order_store_path).snapshot()
    quantities: dict[str, int] = {}
    symbols: dict[str, Symbol] = {}
    for event in snapshot.fill_events:
        if sleeve_id is not None and event.sleeve_id != sleeve_id:
            continue
        quantity = int(event.quantity or 0)
        if quantity <= 0:
            continue
        signed_quantity = quantity if event.side.value == "buy" else -quantity
        symbols[event.symbol.key] = event.symbol
        quantities[event.symbol.key] = quantities.get(event.symbol.key, 0) + signed_quantity
    return [
        {
            "symbol": symbols[symbol_key].ticker,
            "market": symbols[symbol_key].market,
            "quantity": quantity,
        }
        for symbol_key, quantity in sorted(quantities.items())
        if quantity != 0
    ]


def _resolve_order_runtime_routes(
    snapshot,
    account_id: str | None,
    explicit_account_store: Path | None,
    explicit_order_store: Path | None,
    sleeve_ids: Sequence[str],
) -> tuple[_OrderRuntimeRoute, ...]:
    if account_id or explicit_account_store is not None:
        return (
            _resolve_order_runtime_route(
                snapshot,
                account_id,
                explicit_account_store,
                explicit_order_store,
                sleeve_ids,
            ),
        )
    account_ids: list[str] = []
    for sleeve_id in sleeve_ids:
        account_ids.extend(configured_account_ids_for_sleeve(snapshot.config.sleeve(sleeve_id)))
    unique_account_ids = tuple(dict.fromkeys(account_ids))
    if not unique_account_ids:
        return (
            _resolve_order_runtime_route(
                snapshot,
                account_id,
                explicit_account_store,
                explicit_order_store,
                sleeve_ids,
            ),
        )
    return tuple(
        _resolve_order_runtime_route(
            snapshot,
            route_account_id,
            None,
            explicit_order_store,
            sleeve_ids,
            strict_account_scope=False,
        )
        for route_account_id in unique_account_ids
    )


def _resolve_order_runtime_route(
    snapshot,
    account_id: str | None,
    explicit_account_store: Path | None,
    explicit_order_store: Path | None,
    sleeve_ids: Sequence[str],
    strict_account_scope: bool = True,
) -> _OrderRuntimeRoute:
    account = _broker_account_for_order_runtime(snapshot, account_id, sleeve_ids, strict_account_scope=strict_account_scope)
    if explicit_account_store is not None:
        account_store_path = resolve_runtime_path(snapshot, explicit_account_store).resolve()
        order_store_path = _resolve_status_order_store_path(snapshot, explicit_order_store, account_store_path)
        return _OrderRuntimeRoute(
            account_id=account.account_id if account is not None else account_id,
            market_scope=account.market_scope if account is not None else None,
            currency=account.currency if account is not None else currency_for_market_scope(None),
            account_store_path=account_store_path,
            order_store_path=order_store_path,
        )
    if account is not None:
        account_store_path = resolve_runtime_path(snapshot, account.account_store_path).resolve()
        order_store_path = (
            resolve_runtime_path(snapshot, account.order_store_path).resolve()
            if explicit_order_store is None and account.order_store_path is not None
            else _resolve_status_order_store_path(snapshot, explicit_order_store, account_store_path)
        )
        return _OrderRuntimeRoute(
            account_id=account.account_id,
            market_scope=account.market_scope,
            currency=account.currency,
            account_store_path=account_store_path,
            order_store_path=order_store_path,
        )
    account_store_path = _resolve_status_account_store_path(snapshot, explicit_account_store, sleeve_ids)
    return _OrderRuntimeRoute(
        account_id=account_id,
        market_scope=None,
        currency=currency_for_market_scope(None),
        account_store_path=account_store_path,
        order_store_path=_resolve_status_order_store_path(snapshot, explicit_order_store, account_store_path),
    )


def _broker_account_for_order_runtime(
    snapshot,
    account_id: str | None,
    sleeve_ids: Sequence[str],
    *,
    strict_account_scope: bool = True,
):
    if account_id:
        try:
            account = snapshot.config.broker_account(account_id)
        except KeyError as exc:
            raise RuntimeError(f"Unknown broker account_id: {account_id}") from exc
        valid_account_ids_by_sleeve = {
            sleeve_id: set(configured_account_ids_for_sleeve(snapshot.config.sleeve(sleeve_id)))
            for sleeve_id in sleeve_ids
        }
        if not strict_account_scope:
            allowed_account_ids = set().union(*valid_account_ids_by_sleeve.values()) if valid_account_ids_by_sleeve else set()
            if allowed_account_ids and account.account_id not in allowed_account_ids:
                raise RuntimeError(
                    f"Selected sleeves route to broker_account_id values {sorted(allowed_account_ids)}, not '{account.account_id}'."
                )
            return account
        for sleeve_id, valid_account_ids in valid_account_ids_by_sleeve.items():
            if valid_account_ids and account.account_id not in valid_account_ids:
                raise RuntimeError(
                    f"Sleeve '{sleeve_id}' routes to broker_account_id values {sorted(valid_account_ids)}, not '{account.account_id}'."
                )
        return account
    routed_account_ids = tuple(
        dict.fromkeys(
            account_id
            for sleeve in (snapshot.config.sleeve(sleeve_id) for sleeve_id in sleeve_ids)
            for account_id in configured_account_ids_for_sleeve(sleeve)
        )
    )
    if not routed_account_ids:
        return None
    if len(routed_account_ids) > 1:
        raise RuntimeError(
            "Selected sleeves route to multiple broker accounts; pass --account-id and run one account at a time."
        )
    return snapshot.config.broker_account(routed_account_ids[0])


def _routed_batches_for_submit(
    snapshot,
    batches,
    sleeve_ids: tuple[str, ...],
    account_id: str | None,
    explicit_account_store: Path | None,
    explicit_order_store: Path | None,
) -> tuple[tuple[_OrderRuntimeRoute, tuple], ...]:
    if account_id or explicit_account_store is not None:
        route = _resolve_order_runtime_route(
            snapshot,
            account_id,
            explicit_account_store,
            explicit_order_store,
            sleeve_ids,
        )
        return ((route, tuple(batches)),)
    routed = split_batches_by_account_route(
        config=snapshot.config,
        batches=tuple(batches),
        allowed_sleeve_ids=sleeve_ids,
    )
    if not routed:
        route = _resolve_order_runtime_route(snapshot, account_id, explicit_account_store, explicit_order_store, sleeve_ids)
        return ((route, tuple(batches)),)
    grouped: dict[str | None, list] = {}
    market_scope_by_account: dict[str | None, str | None] = {}
    for routed_batch in routed:
        grouped.setdefault(routed_batch.account_id, []).append(routed_batch.batch)
        market_scope_by_account[routed_batch.account_id] = routed_batch.market_scope
    result = []
    for route_account_id, route_batches in grouped.items():
        route = _resolve_order_runtime_route(
            snapshot,
            route_account_id,
            explicit_account_store,
            explicit_order_store,
            sleeve_ids,
            strict_account_scope=False,
        )
        if route.market_scope is None and market_scope_by_account.get(route_account_id) is not None:
            route = _OrderRuntimeRoute(
                account_id=route.account_id,
                market_scope=market_scope_by_account[route_account_id],
                currency=currency_for_market_scope(market_scope_by_account[route_account_id]),
                account_store_path=route.account_store_path,
                order_store_path=route.order_store_path,
            )
        result.append((route, tuple(route_batches)))
    return tuple(result)


def _run_order_runtime_submit_for_route(snapshot, args, route: _OrderRuntimeRoute, sleeve_ids: tuple[str, ...], batches) -> object:
    batches_tuple = tuple(batches)
    account_store = VirtualSleeveAccountStore(
        route.account_store_path,
        default_cash_by_sleeve=_default_cash_by_sleeve(snapshot, route.currency, route.account_id),
        default_currency=route.currency,
    )
    order_state_store = FileOrderRuntimeStateStore(route.order_store_path)
    orchestrator = None
    setup_errors: list[str] = []
    if args.commit and (args.broker != "broker-engine" or args.confirm_live_submit):
        try:
            orchestrator = MultiSleeveOrderOrchestrator(
                broker=_order_supervisor_broker(
                    args.broker,
                    args.paper_no_fill,
                    None,
                    account_id=route.account_id,
                    account_metadata=_broker_account_metadata(snapshot, route.account_id),
                ),
                account_store=account_store,
                order_state_store=order_state_store,
                poll_after_submit=args.poll_after_submit,
            )
        except Exception as exc:  # noqa: BLE001
            setup_errors.append(f"orchestrator_setup_failed: {exc}")
    return OrderRuntimeSubmitter(
        runtime_id=snapshot.config.runtime_id,
        order_state_store=order_state_store,
        account_store=account_store,
        orchestrator=orchestrator,
        order_store_path=route.order_store_path,
        account_store_path=route.account_store_path,
        broker_account_id=route.account_id,
        market_scope=route.market_scope,
        currency=route.currency,
        market_session=_market_session_for_route(route),
        security_catalog=_security_catalog_for_sleeves(snapshot, _sleeve_ids_for_batches(batches_tuple) or sleeve_ids),
    ).submit_batches(
        batches_tuple,
        allowed_sleeve_ids=sleeve_ids,
        broker=args.broker,
        commit=args.commit,
        confirm_live_submit=args.confirm_live_submit,
        poll_after_submit=args.poll_after_submit,
        max_submit_notional=_max_submit_notional_for_route(args, route),
        allowed_symbols=tuple(args.allow_symbol),
        recent_events=args.recent_events,
        initial_errors=tuple(setup_errors),
    )


def _max_submit_notional_for_route(args, route: _OrderRuntimeRoute) -> float | None:
    overrides = getattr(args, "max_submit_notional_by_account", ()) or ()
    for item in overrides:
        key, separator, value = str(item).partition("=")
        if not separator:
            raise RuntimeError("--max-submit-notional-by-account must use account_id=value or market_scope=value.")
        route_keys = {
            str(route.account_id or "").strip().lower(),
            str(route.market_scope or "").strip().lower(),
            str(route.currency or "").strip().lower(),
        }
        if key.strip().lower() in route_keys:
            return float(value)
    return args.max_submit_notional


def _security_catalog_for_sleeves(snapshot, sleeve_ids: Sequence[str]) -> SecurityCatalog | None:
    properties: dict[str, SymbolProperties] = {}
    for sleeve_id in sleeve_ids:
        sleeve = snapshot.config.sleeve(sleeve_id)
        try:
            universe = load_universe_definition(resolve_runtime_path(snapshot, sleeve.universe.coarse_path))
        except FileNotFoundError:
            continue
        for symbol in universe.symbols:
            properties.setdefault(symbol.key, symbol_properties_from_metadata(symbol, universe.properties_for(symbol)))
    if not properties:
        return None
    return SecurityCatalog(properties)


def _sleeve_ids_for_batches(batches) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(batch.sleeve_id) for batch in batches if str(batch.sleeve_id)))


def _market_session_for_route(route: _OrderRuntimeRoute):
    if route.market_scope == "domestic":
        return synthetic_domestic_market_session(datetime.now(timezone(timedelta(hours=9))))
    if route.market_scope == "overseas":
        return synthetic_us_market_session(datetime.now(timezone.utc))
    return None


def _run_order_runtime_paper_smoke_for_route(snapshot, args, route: _OrderRuntimeRoute, sleeve_ids: tuple[str, ...], batches):
    account_store = VirtualSleeveAccountStore(
        route.account_store_path,
        default_cash_by_sleeve=_default_cash_by_sleeve(snapshot, route.currency, route.account_id),
        default_currency=route.currency,
    )
    order_state_store = FileOrderRuntimeStateStore(route.order_store_path)
    return OrderRuntimePaperSmokeRunner(
        runtime_id=snapshot.config.runtime_id,
        sleeve_ids=sleeve_ids,
        order_state_store=order_state_store,
        account_store=account_store,
        order_store_path=route.order_store_path,
        account_store_path=route.account_store_path,
        broker_account_id=route.account_id,
        market_scope=route.market_scope,
        currency=route.currency,
    ).run_batches(
        batches,
        max_submit_notional=args.max_submit_notional,
        allowed_symbols=tuple(args.allow_symbol),
        paper_no_fill=args.paper_no_fill,
        recent_events=args.recent_events,
    )


def _run_order_runtime_supervisor_for_route(snapshot, args, route: _OrderRuntimeRoute, sleeve_ids: tuple[str, ...]):
    account_store = VirtualSleeveAccountStore(
        route.account_store_path,
        default_cash_by_sleeve=_default_cash_by_sleeve(snapshot, route.currency, route.account_id),
        default_currency=route.currency,
    )
    order_state_store = FileOrderRuntimeStateStore(route.order_store_path)
    setup_errors: list[str] = []
    needs_account_client = (
        not args.skip_reconcile
        or (not args.skip_poll and args.broker == "broker-engine")
    )
    account_client = None
    if needs_account_client:
        try:
            try:
                account_client = KISAccountClient.from_env(
                    route.account_id,
                    metadata=_broker_account_metadata(snapshot, route.account_id),
                )
            except TypeError:
                account_client = KISAccountClient.from_env()
        except Exception as exc:  # noqa: BLE001
            setup_errors.append(f"account_client_setup_failed: {exc}")
    poll_worker = None
    if not args.skip_poll:
        try:
            poll_worker = OpenTicketPollWorker(
                broker=_order_supervisor_broker(args.broker, args.paper_no_fill, account_client),
                order_state_store=order_state_store,
                account_store=account_store,
            )
        except Exception as exc:  # noqa: BLE001
            setup_errors.append(f"poll_worker_setup_failed: {exc}")
    reconcile_worker = None
    if not args.skip_reconcile and account_client is not None:
        reconcile_worker = ExecutionHistoryReconcileWorker(
            account_client=account_client,
            account_store=account_store,
            order_state_store=order_state_store,
            default_max_executions=args.max_executions,
        )
    default_reconcile_date = _default_reconcile_date(route.market_scope)
    return OrderRuntimeSupervisor(
        runtime_id=snapshot.config.runtime_id,
        sleeve_ids=sleeve_ids,
        order_state_store=order_state_store,
        account_store=account_store,
        poll_worker=poll_worker,
        reconcile_worker=reconcile_worker,
        order_store_path=route.order_store_path,
        account_store_path=route.account_store_path,
        broker_account_id=route.account_id,
        market_scope=route.market_scope,
        currency=route.currency,
        maintenance_policy=OrderMaintenancePolicy(
            stale_after_seconds=args.stale_after_seconds,
            cancel_stale=args.cancel_stale_open_tickets,
            cancel_partially_filled=not args.keep_partially_filled_stale,
            expire_day_orders=args.expire_day_open_tickets,
        ),
    ).run_once(
        poll=not args.skip_poll,
        reconcile=not args.skip_reconcile,
        start_date=args.start_date or default_reconcile_date,
        end_date=args.end_date or default_reconcile_date,
        market=args.market or route.market_scope or "domestic",
        side=args.side,
        symbol=args.symbol,
        assign_unknown_to_sleeve_id=args.assign_unknown_to_sleeve_id,
        record_unknown_fills=not args.drop_unknown_fills,
        max_executions=args.max_executions,
        reconcile_holdings=not args.skip_holdings_reconcile,
        recent_events=args.recent_events,
        initial_errors=tuple(setup_errors),
    )


def _default_reconcile_date(market_scope: str | None, *, now: datetime | None = None) -> str:
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    if market_scope == "overseas":
        return instant.astimezone(ZoneInfo("America/New_York")).strftime("%Y%m%d")
    if market_scope == "domestic":
        return instant.astimezone(timezone(timedelta(hours=9))).strftime("%Y%m%d")
    return instant.strftime("%Y%m%d")


def _maybe_notify_order_submit(args, report) -> dict[str, object] | None:
    if not getattr(args, "notify", False):
        return None
    service = NotificationService.from_env(root=getattr(args, "notification_root", None))
    return notify_order_submit_report(
        service,
        report,
        chat_id=getattr(args, "notify_chat_id", None),
        disable_notification=getattr(args, "notify_disable_notification", False),
    )


def _maybe_notify_order_supervisor(args, report) -> dict[str, object] | None:
    if not getattr(args, "notify", False):
        return None
    service = NotificationService.from_env(root=getattr(args, "notification_root", None))
    return notify_order_supervisor_report(
        service,
        report,
        chat_id=getattr(args, "notify_chat_id", None),
        disable_notification=getattr(args, "notify_disable_notification", False),
    )


def _multi_route_payload(snapshot, reports, *, include_details: bool) -> dict[str, object]:
    return {
        "runtime_id": snapshot.config.runtime_id,
        "route_count": len(reports),
        "needs_attention": any(report.needs_attention for report in reports),
        "routes": [report.to_dict(include_details=include_details) for report in reports],
    }


def _multi_submit_payload(snapshot, reports, *, include_details: bool) -> dict[str, object]:
    statuses = {report.status for report in reports}
    if "blocked" in statuses:
        status = "blocked"
    elif "submitted_with_warnings" in statuses:
        status = "submitted_with_warnings"
    elif "submitted" in statuses:
        status = "submitted"
    else:
        status = "dry_run"
    return {
        "status": status,
        "runtime_id": snapshot.config.runtime_id,
        "route_count": len(reports),
        "batch_count": sum(report.batch_count for report in reports),
        "order_count": sum(report.order_count for report in reports),
        "total_notional": sum(report.total_notional for report in reports),
        "routes": [
            {
                "broker_account_id": report.final_status.broker_account_id,
                "market_scope": report.final_status.market_scope,
                "currency": report.final_status.currency,
                **report.to_dict(include_details=include_details),
            }
            for report in reports
        ],
    }


def _append_order_report_journal(
    journal_store,
    *,
    snapshot,
    sleeve_ids: tuple[str, ...],
    route: _OrderRuntimeRoute,
    source: str,
    generated_at: datetime,
    status: str,
    counts: dict[str, int | float],
    errors: tuple[str, ...],
    warnings: tuple[str, ...],
) -> None:
    if journal_store is None:
        return
    for sleeve_id in sleeve_ids:
        metadata = _runtime_code_identity_metadata(snapshot, (sleeve_id,))
        journal_store.append(
            CycleJournalEntry(
                runtime_id=snapshot.config.runtime_id,
                config_version=snapshot.version,
                sleeve_id=sleeve_id,
                account_id=route.account_id,
                route_id=route.account_id,
                market_scope=route.market_scope,
                generated_at=generated_at,
                recorded_at=datetime.now(),
                source=source,
                status=status,
                counts=counts,
                warnings=warnings,
                errors=errors,
                metadata=metadata,
            )
        )


def _with_runtime_code_identity_metadata(snapshot, entry: CycleJournalEntry, sleeve_ids: tuple[str, ...]) -> CycleJournalEntry:
    metadata = dict(entry.metadata)
    metadata.update(_runtime_code_identity_metadata(snapshot, sleeve_ids))
    return replace(entry, metadata=metadata)


def _runtime_code_identity_metadata(snapshot, sleeve_ids: tuple[str, ...]) -> dict[str, object]:
    try:
        return build_runtime_code_identity(snapshot, sleeve_ids=sleeve_ids).journal_metadata()
    except Exception as exc:  # noqa: BLE001 - journal writes should not fail the runtime cycle.
        return {"runtime_code_identity_error": str(exc)}


def _resolve_status_account_store_path(snapshot, explicit_path: Path | None, sleeve_ids: Sequence[str]) -> Path:
    if explicit_path is not None:
        return resolve_runtime_path(snapshot, explicit_path).resolve()
    resolved_paths: list[Path] = []
    for sleeve_id in sleeve_ids:
        sleeve = snapshot.config.sleeve(sleeve_id)
        if sleeve.portfolio.account_store_path is None:
            continue
        resolved_paths.append(resolve_runtime_path(snapshot, sleeve.portfolio.account_store_path).resolve())
    unique_paths = tuple(dict.fromkeys(resolved_paths))
    if not unique_paths:
        raise RuntimeError("portfolio.account_store_path or --account-store is required for order runtime status.")
    if len(unique_paths) > 1:
        raise RuntimeError("Multiple account_store_path values are configured; pass --account-store explicitly.")
    return unique_paths[0]


def _resolve_status_order_store_path(snapshot, explicit_path: Path | None, account_store_path: Path) -> Path:
    if explicit_path is not None:
        return resolve_runtime_path(snapshot, explicit_path).resolve()
    return (account_store_path.parent.parent / "order-runtime" / f"{account_store_path.stem}.jsonl").resolve()


def _order_supervisor_broker(
    broker: str,
    paper_no_fill: bool,
    account_client: KISAccountClient | None,
    *,
    account_id: str | None = None,
    account_metadata: dict | None = None,
) -> BrokerExecutionService:
    if broker == "paper":
        return BrokerExecutionService(PaperBrokerExecutionGateway(fill_on_poll=not paper_no_fill))
    if broker == "broker-engine":
        if account_client is None:
            account_client = KISAccountClient.from_env(account_id, metadata=account_metadata)
        return BrokerExecutionService(BrokerEngineExecutionGateway(client=account_client.broker))
    raise ValueError(f"Unsupported order supervisor broker: {broker}")


def _broker_account_metadata(snapshot, account_id: str | None) -> dict:
    if not account_id:
        return {}
    try:
        return dict(snapshot.config.broker_account(account_id).metadata)
    except KeyError:
        return {}


def _default_market_from_runtime_snapshot(snapshot, sleeve_id: str | None) -> str:
    sleeves = snapshot.config.sleeves
    if sleeve_id is None and len(sleeves) == 1:
        sleeve = sleeves[0]
    elif sleeve_id is not None:
        sleeve = snapshot.config.sleeve(sleeve_id)
    else:
        return "KRX"
    universe = load_universe_definition(resolve_runtime_path(snapshot, sleeve.universe.coarse_path))
    return universe.market


def _build_runtime_control_command(args) -> RuntimeControlCommand:
    if args.control_command == "reload-config":
        if args.config is None:
            raise SystemExit("runtime-control-submit --command reload-config requires --config.")
        return RuntimeControlCommand.reload_config(args.config, reason=args.reason)
    if args.control_command == "reload-sleeve":
        if args.config is None or not args.sleeve_id:
            raise SystemExit("runtime-control-submit --command reload-sleeve requires --config and --sleeve-id.")
        return RuntimeControlCommand.reload_sleeve(args.config, args.sleeve_id, reason=args.reason)
    if args.control_command == "activate-sleeve":
        if args.config is None or not args.sleeve_id:
            raise SystemExit("runtime-control-submit --command activate-sleeve requires --config and --sleeve-id.")
        return RuntimeControlCommand.activate_sleeve(args.config, args.sleeve_id, reason=args.reason)
    if args.control_command == "deactivate-sleeve":
        if args.config is None or not args.sleeve_id:
            raise SystemExit("runtime-control-submit --command deactivate-sleeve requires --config and --sleeve-id.")
        return RuntimeControlCommand.deactivate_sleeve(args.config, args.sleeve_id, reason=args.reason)
    if args.control_command == "suspend-sleeve":
        if args.config is None or not args.sleeve_id:
            raise SystemExit("runtime-control-submit --command suspend-sleeve requires --config and --sleeve-id.")
        return RuntimeControlCommand.suspend_sleeve(args.config, args.sleeve_id, reason=args.reason)
    if args.control_command == "resume-sleeve":
        if args.config is None or not args.sleeve_id:
            raise SystemExit("runtime-control-submit --command resume-sleeve requires --config and --sleeve-id.")
        return RuntimeControlCommand.resume_sleeve(args.config, args.sleeve_id, reason=args.reason)
    if args.control_command == "pause-worker":
        return RuntimeControlCommand.pause_worker(reason=args.reason)
    if args.control_command == "resume-worker":
        return RuntimeControlCommand.resume_worker(reason=args.reason)
    if args.control_command == "run-once":
        return RuntimeControlCommand.run_once(reason=args.reason)
    if args.control_command == "shutdown":
        return RuntimeControlCommand.shutdown(reason=args.reason)
    raise SystemExit(f"Unsupported runtime control command: {args.control_command}")


if __name__ == "__main__":
    raise SystemExit(main())
