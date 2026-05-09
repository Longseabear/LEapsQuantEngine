from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
import importlib.util
import inspect
from pathlib import Path
from types import ModuleType
from typing import Any, Mapping
from uuid import uuid4

from leaps_quant_engine.execution import ExecutionModel


class ExecutionModelLoadError(ValueError):
    """Raised when an execution model cannot be loaded."""


@dataclass(frozen=True, slots=True)
class ExecutionModelLoadResult:
    model: ExecutionModel
    ref: str
    parameters: Mapping[str, Any]
    model_name: str


@dataclass(frozen=True, slots=True)
class PythonExecutionModelLoader:
    def load(
        self,
        ref: str | Path,
        *,
        parameters: Mapping[str, Any] | None = None,
    ) -> ExecutionModelLoadResult:
        params = dict(parameters or {})
        ref_text = str(ref)
        model = _model_from_reference(ref_text, params)
        _validate_model(model)
        return ExecutionModelLoadResult(
            model=model,
            ref=ref_text,
            parameters=params,
            model_name=type(model).__name__,
        )


def _model_from_reference(ref: str, parameters: dict[str, Any]) -> ExecutionModel:
    path = Path(ref)
    if path.suffix == ".py" or path.exists():
        return _model_from_module(_load_module_from_path(path), parameters)
    if ":" in ref:
        module_name, object_name = ref.split(":", 1)
        value = _load_object(module_name, object_name)
        return _model_from_value(value, parameters)
    module = import_module(ref)
    return _model_from_module(module, parameters)


def _load_module_from_path(path: Path) -> ModuleType:
    resolved = path.resolve()
    module_name = f"leaps_execution_{resolved.stem}_{uuid4().hex[:12]}"
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise ExecutionModelLoadError(f"Cannot load execution model module from {resolved}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_object(module_name: str, object_name: str) -> Any:
    module = import_module(module_name)
    value: Any = module
    for part in object_name.split("."):
        value = getattr(value, part)
    return value


def _model_from_module(module: ModuleType, parameters: dict[str, Any]) -> ExecutionModel:
    for factory_name in ("create_execution_model", "create_model"):
        factory = getattr(module, factory_name, None)
        if callable(factory):
            return _call_factory(factory, parameters)
    model = getattr(module, "EXECUTION_MODEL", None)
    if model is not None:
        return _model_from_value(model, parameters)
    raise ExecutionModelLoadError(
        "Execution model module must expose create_execution_model(params), "
        "create_model(params), or EXECUTION_MODEL."
    )


def _model_from_value(value: Any, parameters: dict[str, Any]) -> ExecutionModel:
    if inspect.isclass(value):
        return _call_factory(value, parameters)
    if callable(getattr(value, "create_orders", None)):
        return value
    if callable(value):
        return _call_factory(value, parameters)
    raise ExecutionModelLoadError("Execution model reference did not resolve to a model or factory.")


def _call_factory(factory: Any, parameters: dict[str, Any]) -> ExecutionModel:
    signature = inspect.signature(factory)
    params = signature.parameters
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in params.values()):
        model = factory(**parameters)
    elif "params" in params:
        model = factory(parameters)
    elif "parameters" in params:
        model = factory(parameters)
    else:
        kwargs = {name: value for name, value in parameters.items() if name in params}
        model = factory(**kwargs)
    _validate_model(model)
    return model


def _validate_model(model: Any) -> ExecutionModel:
    if not callable(getattr(model, "create_orders", None)):
        raise ExecutionModelLoadError("Execution model must provide create_orders(...).")
    return model
