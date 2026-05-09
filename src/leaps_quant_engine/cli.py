from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Sequence

from leaps_quant_engine.adapters.kis import (
    KISBrokerEngineMarketDataProvider,
    KISCachedMarketDataProvider,
    MarketDataEngineLiveQuoteProvider,
)
from leaps_quant_engine.adapters.finance_datareader import FinanceDataReaderMarketDataProvider
from leaps_quant_engine.account_sync import KISVirtualAccountSync
from leaps_quant_engine.account_sync import KISAccountClient
from leaps_quant_engine.alpha import AlphaRuntime, PythonAlphaLoader, SnapshotContext
from leaps_quant_engine.backtesting import run_framework_backtest
from leaps_quant_engine.benchmark import run_daily_indicator_benchmark
from leaps_quant_engine.broker_routing import (
    configured_account_ids_for_sleeve,
    currency_for_market_scope,
    market_scope_from_market,
    split_batches_by_account_route,
)
from leaps_quant_engine.brokerage import BrokerEngineExecutionGateway, BrokerExecutionService, PaperBrokerExecutionGateway
from leaps_quant_engine.cycle_journal import CycleJournalEntry, FileCycleJournalStore
from leaps_quant_engine.framework import FrameworkRunner
from leaps_quant_engine.logging import configure_logging
from leaps_quant_engine.live_snapshot import run_live_indicator_snapshot
from leaps_quant_engine.models import OrderIntent
from leaps_quant_engine.models import Symbol
from leaps_quant_engine.order_orchestrator import MultiSleeveOrderOrchestrator
from leaps_quant_engine.order_state import FileOrderRuntimeStateStore
from leaps_quant_engine.order_smoke import OrderRuntimePaperSmokeRunner
from leaps_quant_engine.order_status import build_order_runtime_status
from leaps_quant_engine.order_submit import OrderRuntimeSubmitter, load_order_intent_batches, write_order_intent_batches
from leaps_quant_engine.order_supervisor import OrderRuntimeSupervisor
from leaps_quant_engine.order_worker import ExecutionHistoryReconcileWorker, OpenTicketPollWorker
from leaps_quant_engine.portfolio import Portfolio
from leaps_quant_engine.runtime import build_indicator_engine_from_file, run_once_from_file
from leaps_quant_engine.runtime_bootstrap import bootstrap_sleeve_runtime, resolve_runtime_path
from leaps_quant_engine.runtime_config import load_runtime_config_snapshot
from leaps_quant_engine.runtime_health import build_runtime_health_report
from leaps_quant_engine.runtime_recovery import build_recovery_account_report, build_recovery_report
from leaps_quant_engine.snapshot_worker import BackgroundSnapshotWorker
from leaps_quant_engine.sleeve_workspace import (
    describe_sleeve_alpha_modules,
    describe_sleeve_portfolio_model,
    disable_sleeve_alpha_module,
    enable_sleeve_alpha_module,
    set_sleeve_portfolio_model,
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
    runtime_run_once.add_argument("--summary-only", action="store_true")

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

    subparsers.add_parser("kis-health")

    kis_quote = subparsers.add_parser("kis-quote")
    kis_quote.add_argument("symbol")
    kis_quote.add_argument("--market", default="KRX")

    kis_account_sync = subparsers.add_parser("kis-account-sync")
    kis_account_sync.add_argument("config", type=Path)
    kis_account_sync.add_argument("--sleeve-id", required=True)
    kis_account_sync.add_argument("--start-date", required=True)
    kis_account_sync.add_argument("--end-date", required=True)
    kis_account_sync.add_argument("--market", default="domestic", choices=("domestic",))
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

    virtual_account_reconcile = subparsers.add_parser("virtual-account-reconcile")
    virtual_account_reconcile.add_argument("config", type=Path)
    virtual_account_reconcile.add_argument("--sleeve-id", required=True)
    virtual_account_reconcile.add_argument("--market", default="domestic", choices=("domestic",))
    virtual_account_reconcile.add_argument("--summary-only", action="store_true")

    virtual_account_cash_sync = subparsers.add_parser("virtual-account-sync-cash")
    virtual_account_cash_sync.add_argument("config", type=Path)
    virtual_account_cash_sync.add_argument("--sleeve-id", required=True)
    virtual_account_cash_sync.add_argument("--currency", default="KRW")
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
    order_runtime_submit.add_argument("--allow-symbol", action="append", default=[])
    order_runtime_submit.add_argument("--recent-events", type=int, default=10)
    order_runtime_submit.add_argument("--journal", type=Path)
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
    order_runtime_supervise.add_argument("--market", default="domestic", choices=("domestic",))
    order_runtime_supervise.add_argument("--side", default="all", choices=("all", "buy", "sell"))
    order_runtime_supervise.add_argument("--symbol", default="")
    order_runtime_supervise.add_argument("--assign-unknown-to-sleeve-id")
    order_runtime_supervise.add_argument("--drop-unknown-fills", action="store_true")
    order_runtime_supervise.add_argument("--max-executions", type=int, default=500)
    order_runtime_supervise.add_argument("--skip-holdings-reconcile", action="store_true")
    order_runtime_supervise.add_argument("--recent-events", type=int, default=10)
    order_runtime_supervise.add_argument("--journal", type=Path)
    order_runtime_supervise.add_argument("--summary-only", action="store_true")

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
    runtime_health.add_argument("--broker", default="paper", choices=("broker-engine", "paper"))
    runtime_health.add_argument("--max-cycle-age-seconds", type=float, default=300.0)
    runtime_health.add_argument("--max-open-ticket-age-seconds", type=float, default=600.0)
    runtime_health.add_argument("--recent-events", type=int, default=10)
    runtime_health.add_argument("--summary-only", action="store_true")

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
    framework_backtest.add_argument("--cash", type=float, default=100_000.0)
    framework_backtest.add_argument("--source", default="finance-datareader", choices=("finance-datareader", "kis-cache"))
    framework_backtest.add_argument("--refresh-history", action="store_true")
    framework_backtest.add_argument("--journal", type=Path)
    framework_backtest.add_argument("--summary-only", action="store_true")

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
        runtime = bootstrap_sleeve_runtime(
            snapshot,
            args.sleeve_id,
            refresh_fine=not args.skip_fine_refresh,
            held_symbols=_parse_symbol_refs(args.held, default_market),
            open_order_symbols=_parse_symbol_refs(args.open_order, default_market),
            exit_watch_symbols=_parse_symbol_refs(args.exit_watch, default_market),
            manual_symbols=_parse_symbol_refs(args.manual, default_market),
        )
        report = runtime.run_once(warmup=False if args.skip_warmup else None)
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
        print(
            json.dumps(
                print_payload,
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
    if args.command == "kis-health":
        provider = KISBrokerEngineMarketDataProvider.from_env()
        print(json.dumps(provider.health_check(), ensure_ascii=False, indent=2))
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
        if sleeve_config.portfolio.account_store_path is None:
            raise RuntimeError("portfolio.account_store_path is required for KIS account sync.")
        default_cash_by_sleeve = _default_cash_by_sleeve(snapshot, currency_for_market_scope(args.market))
        store = VirtualSleeveAccountStore(
            resolve_runtime_path(snapshot, sleeve_config.portfolio.account_store_path),
            default_cash_by_sleeve=default_cash_by_sleeve,
            default_currency=currency_for_market_scope(args.market),
        )
        report = KISVirtualAccountSync.from_env().sync(
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
        if sleeve_config.portfolio.account_store_path is None:
            raise RuntimeError("portfolio.account_store_path is required for virtual account cash sync.")
        default_cash_by_sleeve = _default_cash_by_sleeve(snapshot, args.currency)
        store = VirtualSleeveAccountStore(
            resolve_runtime_path(snapshot, sleeve_config.portfolio.account_store_path),
            default_cash_by_sleeve=default_cash_by_sleeve,
            default_currency=args.currency,
        )
        balance = KISVirtualAccountSync.from_env().account_client.get_balance_summary()
        report = store.sync_account_cash(balance, currency=args.currency, residual_sleeve_id=args.residual_sleeve_id)
        payload = report.to_dict()
        payload["account_store_path"] = str(store.path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.command == "virtual-account-transfer-cash":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        if sleeve_config.portfolio.account_store_path is None:
            raise RuntimeError("portfolio.account_store_path is required for virtual account cash transfer.")
        default_cash_by_sleeve = _default_cash_by_sleeve(snapshot, args.currency)
        store = VirtualSleeveAccountStore(
            resolve_runtime_path(snapshot, sleeve_config.portfolio.account_store_path),
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
                reports.append(report)
            print(
                json.dumps(
                    _multi_submit_payload(snapshot, reports, include_details=not args.summary_only),
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
            print(json.dumps(report.to_dict(include_details=not args.summary_only), ensure_ascii=False, indent=2))
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
            reports.append(report)
        if len(reports) == 1:
            print(json.dumps(reports[0].to_dict(include_details=not args.summary_only), ensure_ascii=False, indent=2))
        else:
            print(
                json.dumps(
                    {
                        "status": "warnings" if any(report.status != "ok" for report in reports) else "ok",
                        "runtime_id": snapshot.config.runtime_id,
                        "route_count": len(reports),
                        "routes": [report.to_dict(include_details=not args.summary_only) for report in reports],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
        )
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
                    journal_path=journal_path,
                    broker=args.broker,
                    max_cycle_age_seconds=args.max_cycle_age_seconds,
                    max_open_ticket_age_seconds=args.max_open_ticket_age_seconds,
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
    if args.command == "virtual-account-allocate-fill":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        if sleeve_config.portfolio.account_store_path is None:
            raise RuntimeError("portfolio.account_store_path is required for virtual account allocation.")
        default_cash_by_sleeve = _default_cash_by_sleeve(snapshot, _sleeve_default_currency(snapshot, sleeve_config))
        store = VirtualSleeveAccountStore(
            resolve_runtime_path(snapshot, sleeve_config.portfolio.account_store_path),
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
    if args.command == "virtual-account-reconcile":
        snapshot = load_runtime_config_snapshot(args.config)
        sleeve_config = snapshot.config.sleeve(args.sleeve_id)
        if sleeve_config.portfolio.account_store_path is None:
            raise RuntimeError("portfolio.account_store_path is required for virtual account reconciliation.")
        default_cash_by_sleeve = _default_cash_by_sleeve(snapshot, _sleeve_default_currency(snapshot, sleeve_config))
        store = VirtualSleeveAccountStore(
            resolve_runtime_path(snapshot, sleeve_config.portfolio.account_store_path),
            default_cash_by_sleeve=default_cash_by_sleeve,
            default_currency=_sleeve_default_currency(snapshot, sleeve_config),
        )
        holdings = KISVirtualAccountSync.from_env().account_client.get_holdings(market=args.market)
        report = store.reconciliation_report(holdings, include_fills=True)
        payload = report.to_dict(include_fills=not args.summary_only)
        payload["account_store_path"] = str(store.path)
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
        provider = KISCachedMarketDataProvider.from_env()
        universe = load_universe_definition(args.universe)
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
        provider = _daily_backtest_provider(args.source)
        universe = load_universe_definition(args.universe)
        alpha_load = PythonAlphaLoader().load(args.alpha)
        journal_store = FileCycleJournalStore(args.journal) if args.journal is not None else None
        universe_market = getattr(universe, "market", None)
        result = run_framework_backtest(
            universe,
            provider,
            sleeve_id=args.sleeve_id,
            framework_runner=FrameworkRunner(
                sleeve_id=args.sleeve_id,
                alpha_runtime=AlphaRuntime(active_models=(alpha_load.model,)),
            ),
            portfolio=Portfolio(cash=args.cash),
            start=_parse_cli_datetime(args.start),
            end=_parse_cli_datetime(args.end),
            refresh_history=args.refresh_history,
            cycle_journal_store=journal_store,
            runtime_id=f"framework-backtest:{args.sleeve_id}",
            market_scope=market_scope_from_market(universe_market) if universe_market else None,
        )
        report = result.to_report(include_orders=not args.summary_only)
        report["source"] = args.source
        if args.journal is not None:
            report["cycle_journal"] = {"path": str(args.journal.resolve())}
        report["alpha"] = {
            "alpha_id": alpha_load.alpha_id,
            "version": alpha_load.version,
            "path": str(alpha_load.path),
            "content_hash": alpha_load.content_hash,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if args.command == "warmup-indicators-daily":
        provider = KISCachedMarketDataProvider.from_env()
        universe = load_universe_definition(args.universe)
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
        provider = KISCachedMarketDataProvider.from_env()
        universe = load_universe_definition(args.universe)
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


def _daily_backtest_provider(source: str):
    if source == "finance-datareader":
        return FinanceDataReaderMarketDataProvider()
    if source == "kis-cache":
        return KISCachedMarketDataProvider.from_env()
    raise ValueError(f"Unsupported daily backtest source: {source}")


def _exchange_map_from_universe(universe) -> dict[str, str]:
    exchange_by_symbol: dict[str, str] = {}
    for symbol in universe.symbols:
        exchange = universe.properties_for(symbol).get("exchange")
        if exchange:
            exchange_by_symbol[symbol.key] = str(exchange).strip().upper()
            exchange_by_symbol[symbol.ticker] = str(exchange).strip().upper()
    return exchange_by_symbol


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
        )
        for route_account_id in unique_account_ids
    )


def _resolve_order_runtime_route(
    snapshot,
    account_id: str | None,
    explicit_account_store: Path | None,
    explicit_order_store: Path | None,
    sleeve_ids: Sequence[str],
) -> _OrderRuntimeRoute:
    account = _broker_account_for_order_runtime(snapshot, account_id, sleeve_ids)
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


def _broker_account_for_order_runtime(snapshot, account_id: str | None, sleeve_ids: Sequence[str]):
    if account_id:
        try:
            account = snapshot.config.broker_account(account_id)
        except KeyError as exc:
            raise RuntimeError(f"Unknown broker account_id: {account_id}") from exc
        for sleeve_id in sleeve_ids:
            sleeve = snapshot.config.sleeve(sleeve_id)
            valid_account_ids = set(configured_account_ids_for_sleeve(sleeve))
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
    account_store = VirtualSleeveAccountStore(
        route.account_store_path,
        default_cash_by_sleeve=_default_cash_by_sleeve(snapshot, route.currency, route.account_id),
        default_currency=route.currency,
    )
    order_state_store = FileOrderRuntimeStateStore(route.order_store_path)
    setup_errors: list[str] = []
    orchestrator = None
    if args.commit and args.broker == "broker-engine" and route.market_scope == "overseas":
        setup_errors.append("broker_engine_overseas_submit_not_supported")
    elif args.commit and (args.broker != "broker-engine" or args.confirm_live_submit):
        try:
            orchestrator = MultiSleeveOrderOrchestrator(
                broker=_order_supervisor_broker(args.broker, args.paper_no_fill, None),
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
    ).submit_batches(
        batches,
        allowed_sleeve_ids=sleeve_ids,
        broker=args.broker,
        commit=args.commit,
        confirm_live_submit=args.confirm_live_submit,
        poll_after_submit=args.poll_after_submit,
        max_submit_notional=args.max_submit_notional,
        allowed_symbols=tuple(args.allow_symbol),
        recent_events=args.recent_events,
        initial_errors=tuple(setup_errors),
    )


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
    overseas_route = route.market_scope == "overseas"
    if overseas_route and args.broker == "broker-engine" and not args.skip_poll:
        setup_errors.append("broker_engine_overseas_poll_not_supported")
    if overseas_route and not args.skip_reconcile:
        setup_errors.append("broker_engine_overseas_reconcile_not_supported")
    needs_account_client = (
        (not args.skip_reconcile and not overseas_route)
        or (not args.skip_poll and args.broker == "broker-engine" and not overseas_route)
    )
    account_client = None
    if needs_account_client:
        try:
            account_client = KISAccountClient.from_env()
        except Exception as exc:  # noqa: BLE001
            setup_errors.append(f"account_client_setup_failed: {exc}")
    poll_worker = None
    if not args.skip_poll and not (overseas_route and args.broker == "broker-engine"):
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
    today = datetime.now().strftime("%Y%m%d")
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
    ).run_once(
        poll=not args.skip_poll,
        reconcile=not args.skip_reconcile,
        start_date=args.start_date or today,
        end_date=args.end_date or today,
        market=args.market,
        side=args.side,
        symbol=args.symbol,
        assign_unknown_to_sleeve_id=args.assign_unknown_to_sleeve_id,
        record_unknown_fills=not args.drop_unknown_fills,
        max_executions=args.max_executions,
        reconcile_holdings=not args.skip_holdings_reconcile,
        recent_events=args.recent_events,
        initial_errors=tuple(setup_errors),
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
            )
        )


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
) -> BrokerExecutionService:
    if broker == "paper":
        return BrokerExecutionService(PaperBrokerExecutionGateway(fill_on_poll=not paper_no_fill))
    if broker == "broker-engine":
        if account_client is None:
            account_client = KISAccountClient.from_env()
        return BrokerExecutionService(BrokerEngineExecutionGateway(client=account_client.broker))
    raise ValueError(f"Unsupported order supervisor broker: {broker}")


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


if __name__ == "__main__":
    raise SystemExit(main())
