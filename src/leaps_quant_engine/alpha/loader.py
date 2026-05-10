from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType

from leaps_quant_engine.alpha.domain import AlphaModel, Insight, SnapshotContext


@dataclass(frozen=True, slots=True)
class PythonAlphaLoadResult:
    model: AlphaModel
    path: Path
    content_hash: str
    alpha_id: str
    version: str


@dataclass(slots=True)
class FunctionAlphaModel:
    alpha_id: str
    version: str
    generate_fn: Callable[[SnapshotContext], list[Insight] | tuple[Insight, ...]]
    evaluation_cadence: str = "every_cycle"
    input_resolution: str = "any"

    def generate(self, context: SnapshotContext) -> list[Insight] | tuple[Insight, ...]:
        return self.generate_fn(context)


@dataclass(frozen=True, slots=True)
class PythonAlphaLoader:
    def load(self, path: str | Path) -> PythonAlphaLoadResult:
        resolved_path = Path(path).resolve()
        content_hash = hashlib.sha256(resolved_path.read_bytes()).hexdigest()
        module = _load_module(resolved_path, content_hash)
        model = _model_from_module(module)
        _validate_model(model)
        return PythonAlphaLoadResult(
            model=model,
            path=resolved_path,
            content_hash=content_hash,
            alpha_id=str(getattr(model, "alpha_id")),
            version=str(getattr(model, "version")),
        )


def _load_module(path: Path, content_hash: str) -> ModuleType:
    module_name = f"leaps_alpha_{path.stem}_{content_hash[:12]}_{int(datetime.now().timestamp() * 1000)}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load alpha module from {path}.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _model_from_module(module: ModuleType) -> AlphaModel:
    create_alpha_model = getattr(module, "create_alpha_model", None)
    if callable(create_alpha_model):
        return create_alpha_model()
    alpha_model = getattr(module, "ALPHA_MODEL", None)
    if alpha_model is not None:
        return alpha_model
    generate = getattr(module, "generate", None)
    if callable(generate):
        alpha_id = str(getattr(module, "ALPHA_ID", module.__name__))
        version = str(getattr(module, "VERSION", "0.1.0"))
        evaluation_cadence = str(getattr(module, "EVALUATION_CADENCE", "every_cycle"))
        input_resolution = str(getattr(module, "INPUT_RESOLUTION", "any"))
        return FunctionAlphaModel(
            alpha_id=alpha_id,
            version=version,
            generate_fn=generate,
            evaluation_cadence=evaluation_cadence,
            input_resolution=input_resolution,
        )
    raise ValueError("Python alpha module must expose create_alpha_model(), ALPHA_MODEL, or generate(context).")


def _validate_model(model: AlphaModel) -> None:
    if not callable(getattr(model, "generate", None)):
        raise TypeError("Alpha model must provide generate(context).")
    if not getattr(model, "alpha_id", None):
        raise ValueError("Alpha model must provide alpha_id.")
    if not getattr(model, "version", None):
        raise ValueError("Alpha model must provide version.")
