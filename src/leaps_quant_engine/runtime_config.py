from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


class ConfigurationValidationError(ValueError):
    """Raised when a runtime option file is syntactically valid but unsafe to run."""


def _path_to_config_str(path: Path | None) -> str | None:
    if path is None:
        return None
    return path.as_posix()


def _default_currency_for_market_scope(market_scope: str) -> str:
    return "KRW" if market_scope == "domestic" else "USD"


@dataclass(frozen=True, slots=True)
class ModuleReference:
    ref: str

    def __post_init__(self) -> None:
        value = self.ref.strip()
        if not value:
            raise ConfigurationValidationError("Module reference cannot be empty.")
        object.__setattr__(self, "ref", value)

    def to_dict(self) -> dict[str, str]:
        return {"ref": self.ref}


@dataclass(frozen=True, slots=True)
class MarketDataRuntimeConfig:
    provider: str = "market-data-engine"
    history_provider: str = "kis-cache"
    source: str = "market-data-engine"
    history_source: str = "kis-cache"
    rate_limit_per_second: int | None = None

    def __post_init__(self) -> None:
        if self.provider != "market-data-engine":
            raise ConfigurationValidationError(f"Unsupported market data provider: {self.provider}")
        if self.history_provider != "kis-cache":
            raise ConfigurationValidationError(f"Unsupported history provider: {self.history_provider}")
        if self.rate_limit_per_second is not None and self.rate_limit_per_second <= 0:
            raise ConfigurationValidationError("market_data.rate_limit_per_second must be positive.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "history_provider": self.history_provider,
            "source": self.source,
            "history_source": self.history_source,
            "rate_limit_per_second": self.rate_limit_per_second,
        }


@dataclass(frozen=True, slots=True)
class BrokerAccountRuntimeConfig:
    account_id: str
    market_scope: str
    account_store_path: Path
    order_store_path: Path | None = None
    currency: str = ""
    broker_gateway: str = "broker-engine"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            raise ConfigurationValidationError("broker_accounts.account_id is required.")
        if self.market_scope not in {"domestic", "overseas"}:
            raise ConfigurationValidationError(f"Unsupported broker account market_scope: {self.market_scope}")
        if not str(self.account_store_path).strip():
            raise ConfigurationValidationError("broker_accounts.account_store_path is required.")
        currency = str(self.currency or _default_currency_for_market_scope(self.market_scope)).strip().upper()
        if not currency:
            raise ConfigurationValidationError("broker_accounts.currency is required.")
        if self.broker_gateway not in {"broker-engine", "paper"}:
            raise ConfigurationValidationError(f"Unsupported broker account gateway: {self.broker_gateway}")
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "market_scope": self.market_scope,
            "account_store_path": _path_to_config_str(self.account_store_path),
            "order_store_path": _path_to_config_str(self.order_store_path),
            "currency": self.currency,
            "broker_gateway": self.broker_gateway,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class FineUniverseRuntimeConfig:
    enabled: bool = False
    refresh_seconds: float = 300.0
    max_symbols: int | None = None
    min_success: int | None = None
    max_age_seconds: float = 300.0

    def __post_init__(self) -> None:
        _validate_non_negative("universe.fine.refresh_seconds", self.refresh_seconds)
        _validate_non_negative("universe.fine.max_age_seconds", self.max_age_seconds)
        _validate_optional_positive_int("universe.fine.max_symbols", self.max_symbols)
        _validate_optional_positive_int("universe.fine.min_success", self.min_success)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "refresh_seconds": self.refresh_seconds,
            "max_symbols": self.max_symbols,
            "min_success": self.min_success,
            "max_age_seconds": self.max_age_seconds,
        }


@dataclass(frozen=True, slots=True)
class ActiveUniverseRuntimeConfig:
    max_symbols: int = 60
    selection_model: ModuleReference = field(
        default_factory=lambda: ModuleReference("leaps_quant_engine.universe.selection:StaticUniverseSelectionModel")
    )

    def __post_init__(self) -> None:
        if self.max_symbols < 0:
            raise ConfigurationValidationError("universe.active.max_symbols must be non-negative.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_symbols": self.max_symbols,
            "selection_model": self.selection_model.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class UniverseRuntimeConfig:
    coarse_path: Path
    fine: FineUniverseRuntimeConfig = field(default_factory=FineUniverseRuntimeConfig)
    active: ActiveUniverseRuntimeConfig = field(default_factory=ActiveUniverseRuntimeConfig)

    def to_dict(self) -> dict[str, Any]:
        return {
            "coarse_path": _path_to_config_str(self.coarse_path),
            "fine": self.fine.to_dict(),
            "active": self.active.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class IndicatorRuntimeConfig:
    warmup_enabled: bool = True
    extra_bars: int = 0
    min_ready_ratio: float = 1.0
    refresh_history: bool = False

    def __post_init__(self) -> None:
        if self.extra_bars < 0:
            raise ConfigurationValidationError("indicators.extra_bars must be non-negative.")
        if not 0 <= self.min_ready_ratio <= 1:
            raise ConfigurationValidationError("indicators.min_ready_ratio must be between 0 and 1.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "warmup_enabled": self.warmup_enabled,
            "extra_bars": self.extra_bars,
            "min_ready_ratio": self.min_ready_ratio,
            "refresh_history": self.refresh_history,
        }


@dataclass(frozen=True, slots=True)
class AlphaRuntimeConfig:
    modules: tuple[ModuleReference, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"modules": [module.to_dict() for module in self.modules]}


@dataclass(frozen=True, slots=True)
class RebalancePolicyRuntimeConfig:
    cash_reserve_pct: float = 0.0
    min_order_notional: float = 0.0
    min_quantity_delta: int = 1
    allow_exit_below_min_notional: bool = True

    def __post_init__(self) -> None:
        if not 0.0 <= self.cash_reserve_pct < 1.0:
            raise ConfigurationValidationError("portfolio.rebalance.cash_reserve_pct must be between 0 inclusive and 1 exclusive.")
        _validate_non_negative("portfolio.rebalance.min_order_notional", self.min_order_notional)
        if self.min_quantity_delta < 0:
            raise ConfigurationValidationError("portfolio.rebalance.min_quantity_delta must be non-negative.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cash_reserve_pct": self.cash_reserve_pct,
            "min_order_notional": self.min_order_notional,
            "min_quantity_delta": self.min_quantity_delta,
            "allow_exit_below_min_notional": self.allow_exit_below_min_notional,
        }


@dataclass(frozen=True, slots=True)
class PortfolioRuntimeConfig:
    model: ModuleReference = field(
        default_factory=lambda: ModuleReference("leaps_quant_engine.framework:EqualWeightPortfolioConstructionModel")
    )
    parameters: Mapping[str, Any] = field(default_factory=dict)
    rebalance: RebalancePolicyRuntimeConfig = field(default_factory=RebalancePolicyRuntimeConfig)
    account_store_path: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.to_dict(),
            "parameters": dict(self.parameters),
            "rebalance": self.rebalance.to_dict(),
            "account_store_path": _path_to_config_str(self.account_store_path),
        }


@dataclass(frozen=True, slots=True)
class RiskRuntimeConfig:
    model: ModuleReference = field(
        default_factory=lambda: ModuleReference("leaps_quant_engine.framework:BasicRiskManagementModel")
    )
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.to_dict(),
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True, slots=True)
class ExecutionRuntimeConfig:
    model: ModuleReference = field(
        default_factory=lambda: ModuleReference("leaps_quant_engine.execution:ImmediateExecutionModel")
    )
    parameters: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model.to_dict(),
            "parameters": dict(self.parameters),
        }


@dataclass(frozen=True, slots=True)
class WorkerRuntimeConfig:
    cycle_interval_seconds: float = 60.0
    min_success: int | None = None

    def __post_init__(self) -> None:
        _validate_non_negative("worker.cycle_interval_seconds", self.cycle_interval_seconds)
        _validate_optional_positive_int("worker.min_success", self.min_success)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_interval_seconds": self.cycle_interval_seconds,
            "min_success": self.min_success,
        }


@dataclass(frozen=True, slots=True)
class SleeveRuntimeConfig:
    sleeve_id: str
    universe: UniverseRuntimeConfig
    workspace_path: Path | None = None
    broker_account_id: str | None = None
    broker_account_routes: Mapping[str, str] = field(default_factory=dict)
    cash: float = 100_000.0
    cash_by_currency: Mapping[str, float] = field(default_factory=dict)
    indicators: IndicatorRuntimeConfig = field(default_factory=IndicatorRuntimeConfig)
    alpha: AlphaRuntimeConfig = field(default_factory=AlphaRuntimeConfig)
    portfolio: PortfolioRuntimeConfig = field(default_factory=PortfolioRuntimeConfig)
    risk: RiskRuntimeConfig = field(default_factory=RiskRuntimeConfig)
    execution: ExecutionRuntimeConfig = field(default_factory=ExecutionRuntimeConfig)
    worker: WorkerRuntimeConfig = field(default_factory=WorkerRuntimeConfig)

    def __post_init__(self) -> None:
        if not self.sleeve_id.strip():
            raise ConfigurationValidationError("sleeve_id is required.")
        _validate_non_negative("sleeve.cash", self.cash)
        routes = {}
        for market_scope, account_id in dict(self.broker_account_routes).items():
            market_scope = str(market_scope).strip()
            account_id = str(account_id).strip()
            if market_scope not in {"domestic", "overseas"}:
                raise ConfigurationValidationError(f"Unsupported sleeve broker_account_routes market_scope: {market_scope}")
            if not account_id:
                raise ConfigurationValidationError("sleeve broker_account_routes account_id cannot be empty.")
            routes[market_scope] = account_id
        object.__setattr__(self, "broker_account_routes", MappingProxyType(routes))
        cash_by_currency = {}
        for currency, amount in dict(self.cash_by_currency).items():
            code = str(currency).strip().upper()
            if not code:
                raise ConfigurationValidationError("sleeve.cash_by_currency currency cannot be empty.")
            value = float(amount)
            if value < 0:
                raise ConfigurationValidationError("sleeve.cash_by_currency amounts must be non-negative.")
            if value > 0:
                cash_by_currency[code] = value
        object.__setattr__(self, "cash_by_currency", MappingProxyType(cash_by_currency))

    def to_dict(self) -> dict[str, Any]:
        return {
            "sleeve_id": self.sleeve_id,
            "workspace_path": _path_to_config_str(self.workspace_path),
            "broker_account_id": self.broker_account_id,
            "broker_account_routes": dict(self.broker_account_routes),
            "cash": self.cash,
            "cash_by_currency": dict(self.cash_by_currency),
            "universe": self.universe.to_dict(),
            "indicators": self.indicators.to_dict(),
            "alpha": self.alpha.to_dict(),
            "portfolio": self.portfolio.to_dict(),
            "risk": self.risk.to_dict(),
            "execution": self.execution.to_dict(),
            "worker": self.worker.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    runtime_id: str
    mode: str
    timezone: str
    market_data: MarketDataRuntimeConfig
    sleeves: tuple[SleeveRuntimeConfig, ...]
    broker_accounts: tuple[BrokerAccountRuntimeConfig, ...] = ()
    journal_path: Path | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.runtime_id.strip():
            raise ConfigurationValidationError("runtime_id is required.")
        if self.mode not in {"live", "paper", "backtest", "research"}:
            raise ConfigurationValidationError(f"Unsupported runtime mode: {self.mode}")
        if not self.timezone.strip():
            raise ConfigurationValidationError("timezone is required.")
        if not self.sleeves:
            raise ConfigurationValidationError("At least one sleeve is required.")
        seen: set[str] = set()
        for sleeve in self.sleeves:
            if sleeve.sleeve_id in seen:
                raise ConfigurationValidationError(f"Duplicate sleeve_id: {sleeve.sleeve_id}")
            seen.add(sleeve.sleeve_id)
        account_ids: set[str] = set()
        for account in self.broker_accounts:
            if account.account_id in account_ids:
                raise ConfigurationValidationError(f"Duplicate broker account_id: {account.account_id}")
            account_ids.add(account.account_id)
        for sleeve in self.sleeves:
            if sleeve.broker_account_id and sleeve.broker_account_id not in account_ids:
                raise ConfigurationValidationError(
                    f"Sleeve '{sleeve.sleeve_id}' references unknown broker_account_id: {sleeve.broker_account_id}"
                )
            for market_scope, broker_account_id in sleeve.broker_account_routes.items():
                if broker_account_id not in account_ids:
                    raise ConfigurationValidationError(
                        f"Sleeve '{sleeve.sleeve_id}' {market_scope} route references unknown broker_account_id: {broker_account_id}"
                    )
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def sleeve(self, sleeve_id: str) -> SleeveRuntimeConfig:
        for sleeve in self.sleeves:
            if sleeve.sleeve_id == sleeve_id:
                return sleeve
        raise KeyError(f"Unknown sleeve_id: {sleeve_id}")

    def broker_account(self, account_id: str) -> BrokerAccountRuntimeConfig:
        for account in self.broker_accounts:
            if account.account_id == account_id:
                return account
        raise KeyError(f"Unknown broker account_id: {account_id}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_id": self.runtime_id,
            "mode": self.mode,
            "timezone": self.timezone,
            "market_data": self.market_data.to_dict(),
            "broker_accounts": [account.to_dict() for account in self.broker_accounts],
            "journal_path": _path_to_config_str(self.journal_path),
            "sleeves": [sleeve.to_dict() for sleeve in self.sleeves],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class RuntimeConfigSnapshot:
    config: RuntimeConfig
    source_path: Path
    version: str
    loaded_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path),
            "version": self.version,
            "loaded_at": self.loaded_at,
            "config": self.config.to_dict(),
        }


def load_runtime_config_snapshot(path: str | Path) -> RuntimeConfigSnapshot:
    source_path = Path(path)
    raw = source_path.read_bytes()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ConfigurationValidationError("Runtime config root must be an object.")
    return RuntimeConfigSnapshot(
        config=parse_runtime_config(payload),
        source_path=source_path,
        version=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        loaded_at=datetime.now().isoformat(),
    )


def parse_runtime_config(payload: Mapping[str, Any]) -> RuntimeConfig:
    market_data_payload = _object(payload.get("market_data"), default={})
    return RuntimeConfig(
        runtime_id=str(payload.get("runtime_id", "")).strip(),
        mode=str(payload.get("mode", "live")).strip(),
        timezone=str(payload.get("timezone", "Asia/Seoul")).strip(),
        market_data=_parse_market_data_runtime_config(market_data_payload),
        broker_accounts=tuple(_parse_broker_account_runtime_config(item) for item in _list(payload.get("broker_accounts"), default=[])),
        journal_path=_optional_path(payload.get("journal_path", payload.get("cycle_journal_path"))),
        sleeves=tuple(_parse_sleeve_runtime_config(item) for item in _list(payload.get("sleeves"))),
        metadata=dict(_object(payload.get("metadata"), default={})),
    )


def _parse_market_data_runtime_config(payload: Mapping[str, Any]) -> MarketDataRuntimeConfig:
    return MarketDataRuntimeConfig(
        provider=str(payload.get("provider", "market-data-engine")).strip(),
        history_provider=str(payload.get("history_provider", "kis-cache")).strip(),
        source=str(payload.get("source", payload.get("provider", "market-data-engine"))).strip(),
        history_source=str(payload.get("history_source", payload.get("history_provider", "kis-cache"))).strip(),
        rate_limit_per_second=_optional_int(payload.get("rate_limit_per_second")),
    )


def _parse_broker_account_runtime_config(payload: Any) -> BrokerAccountRuntimeConfig:
    data = _object(payload)
    account_store_path = _optional_path(data.get("account_store_path", data.get("virtual_account_path")))
    if account_store_path is None:
        raise ConfigurationValidationError("broker_accounts.account_store_path is required.")
    return BrokerAccountRuntimeConfig(
        account_id=str(data.get("account_id", data.get("id", ""))).strip(),
        market_scope=str(data.get("market_scope", data.get("market", ""))).strip() or "domestic",
        account_store_path=account_store_path,
        order_store_path=_optional_path(data.get("order_store_path", data.get("order_runtime_path"))),
        currency=str(data.get("currency", "")).strip(),
        broker_gateway=str(data.get("broker_gateway", data.get("gateway", "broker-engine"))).strip(),
        metadata=dict(_object(data.get("metadata"), default={})),
    )


def _parse_sleeve_runtime_config(payload: Any) -> SleeveRuntimeConfig:
    data = _object(payload)
    return SleeveRuntimeConfig(
        sleeve_id=str(data.get("sleeve_id", data.get("id", ""))).strip(),
        universe=_parse_universe_runtime_config(_object(data.get("universe"))),
        workspace_path=_optional_path(data.get("workspace_path", data.get("workspace"))),
        broker_account_id=_optional_text(data.get("broker_account_id", data.get("account_id"))),
        broker_account_routes=_parse_broker_account_routes(data.get("broker_account_routes", data.get("account_routes"))),
        cash=float(data.get("cash", data.get("portfolio_cash", 100_000.0))),
        cash_by_currency=dict(_object(data.get("cash_by_currency"), default={})),
        indicators=_parse_indicator_runtime_config(_object(data.get("indicators"), default={})),
        alpha=_parse_alpha_runtime_config(_object(data.get("alpha"), default={})),
        portfolio=_parse_portfolio_runtime_config(_object(data.get("portfolio"), default={})),
        risk=_parse_risk_runtime_config(_object(data.get("risk"), default={})),
        execution=_parse_execution_runtime_config(_object(data.get("execution"), default={})),
        worker=_parse_worker_runtime_config(_object(data.get("worker"), default={})),
    )


def _parse_broker_account_routes(payload: Any) -> dict[str, str]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise ConfigurationValidationError("sleeve.broker_account_routes must be an object.")
    return {
        str(market_scope).strip(): str(account_id).strip()
        for market_scope, account_id in payload.items()
        if str(market_scope).strip() and str(account_id).strip()
    }


def _parse_universe_runtime_config(payload: Mapping[str, Any]) -> UniverseRuntimeConfig:
    coarse_path = str(payload.get("coarse_path", "")).strip()
    if not coarse_path:
        raise ConfigurationValidationError("universe.coarse_path is required.")
    return UniverseRuntimeConfig(
        coarse_path=Path(coarse_path),
        fine=_parse_fine_universe_runtime_config(_object(payload.get("fine"), default={})),
        active=_parse_active_universe_runtime_config(_object(payload.get("active"), default={})),
    )


def _parse_fine_universe_runtime_config(payload: Mapping[str, Any]) -> FineUniverseRuntimeConfig:
    return FineUniverseRuntimeConfig(
        enabled=bool(payload.get("enabled", False)),
        refresh_seconds=float(payload.get("refresh_seconds", 300.0)),
        max_symbols=_optional_int(payload.get("max_symbols")),
        min_success=_optional_int(payload.get("min_success")),
        max_age_seconds=float(payload.get("max_age_seconds", 300.0)),
    )


def _parse_active_universe_runtime_config(payload: Mapping[str, Any]) -> ActiveUniverseRuntimeConfig:
    return ActiveUniverseRuntimeConfig(
        max_symbols=int(payload.get("max_symbols", 60)),
        selection_model=_parse_module_reference(
            payload.get("selection_model", "leaps_quant_engine.universe.selection:StaticUniverseSelectionModel")
        ),
    )


def _parse_indicator_runtime_config(payload: Mapping[str, Any]) -> IndicatorRuntimeConfig:
    return IndicatorRuntimeConfig(
        warmup_enabled=bool(payload.get("warmup_enabled", True)),
        extra_bars=int(payload.get("extra_bars", 0)),
        min_ready_ratio=float(payload.get("min_ready_ratio", 1.0)),
        refresh_history=bool(payload.get("refresh_history", False)),
    )


def _parse_alpha_runtime_config(payload: Mapping[str, Any]) -> AlphaRuntimeConfig:
    return AlphaRuntimeConfig(
        modules=tuple(
            module
            for module in (_parse_optional_module_reference(item) for item in _list(payload.get("modules"), default=[]))
            if module is not None
        )
    )


def _parse_portfolio_runtime_config(payload: Mapping[str, Any]) -> PortfolioRuntimeConfig:
    raw_model = payload.get("model", payload.get("module", "leaps_quant_engine.framework:EqualWeightPortfolioConstructionModel"))
    return PortfolioRuntimeConfig(
        model=_parse_module_reference(raw_model),
        parameters=dict(_object(payload.get("parameters", payload.get("params")), default={})),
        rebalance=_parse_rebalance_policy_runtime_config(_object(payload.get("rebalance"), default={})),
        account_store_path=_optional_path(payload.get("account_store_path", payload.get("virtual_account_path"))),
    )


def _parse_risk_runtime_config(payload: Mapping[str, Any]) -> RiskRuntimeConfig:
    raw_model = payload.get("model", payload.get("module", "leaps_quant_engine.framework:BasicRiskManagementModel"))
    return RiskRuntimeConfig(
        model=_parse_module_reference(raw_model),
        parameters=dict(_object(payload.get("parameters", payload.get("params")), default={})),
    )


def _parse_execution_runtime_config(payload: Mapping[str, Any]) -> ExecutionRuntimeConfig:
    raw_model = payload.get("model", payload.get("module", "leaps_quant_engine.execution:ImmediateExecutionModel"))
    return ExecutionRuntimeConfig(
        model=_parse_module_reference(raw_model),
        parameters=dict(_object(payload.get("parameters", payload.get("params")), default={})),
    )


def _parse_rebalance_policy_runtime_config(payload: Mapping[str, Any]) -> RebalancePolicyRuntimeConfig:
    return RebalancePolicyRuntimeConfig(
        cash_reserve_pct=float(payload.get("cash_reserve_pct", 0.0)),
        min_order_notional=float(payload.get("min_order_notional", 0.0)),
        min_quantity_delta=int(payload.get("min_quantity_delta", 1)),
        allow_exit_below_min_notional=bool(payload.get("allow_exit_below_min_notional", True)),
    )


def _parse_worker_runtime_config(payload: Mapping[str, Any]) -> WorkerRuntimeConfig:
    return WorkerRuntimeConfig(
        cycle_interval_seconds=float(payload.get("cycle_interval_seconds", 60.0)),
        min_success=_optional_int(payload.get("min_success")),
    )


def _parse_optional_module_reference(payload: Any) -> ModuleReference | None:
    if isinstance(payload, Mapping) and not bool(payload.get("enabled", True)):
        return None
    return _parse_module_reference(payload)


def _parse_module_reference(payload: Any) -> ModuleReference:
    if isinstance(payload, str):
        return ModuleReference(payload)
    if isinstance(payload, Mapping):
        return ModuleReference(str(payload.get("ref", "")).strip())
    raise ConfigurationValidationError(f"Unsupported module reference payload: {payload!r}")


def _object(value: Any, *, default: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    if value is None and default is not None:
        return default
    if not isinstance(value, Mapping):
        raise ConfigurationValidationError(f"Expected object payload, got {type(value).__name__}.")
    return value


def _list(value: Any, *, default: list[Any] | None = None) -> list[Any]:
    if value is None and default is not None:
        return default
    if not isinstance(value, list):
        raise ConfigurationValidationError(f"Expected list payload, got {type(value).__name__}.")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_path(value: Any) -> Path | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return Path(text)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _validate_non_negative(name: str, value: float) -> None:
    if value < 0:
        raise ConfigurationValidationError(f"{name} must be non-negative.")


def _validate_optional_positive_int(name: str, value: int | None) -> None:
    if value is not None and value <= 0:
        raise ConfigurationValidationError(f"{name} must be positive when set.")
