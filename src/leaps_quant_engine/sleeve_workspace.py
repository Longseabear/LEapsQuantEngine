from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from leaps_quant_engine.control import RuntimeControlCommand


class SleeveWorkspaceError(ValueError):
    """Raised when a sleeve workspace cannot be inspected or updated."""


def describe_sleeve_alpha_modules(config_path: str | Path, sleeve_id: str) -> dict[str, Any]:
    path = Path(config_path)
    payload = _load_payload(path)
    sleeve = _find_sleeve(payload, sleeve_id)
    workspace = _workspace_path(path, sleeve)
    return _alpha_status(path, sleeve_id, sleeve, workspace)


def enable_sleeve_alpha_module(config_path: str | Path, sleeve_id: str, alpha_ref: str) -> dict[str, Any]:
    path = Path(config_path)
    payload = _load_payload(path)
    sleeve = _find_sleeve(payload, sleeve_id)
    workspace = _workspace_path(path, sleeve)
    ref = _normalize_alpha_ref(alpha_ref)
    _assert_alpha_exists(workspace, ref)
    alpha = sleeve.setdefault("alpha", {})
    modules = _module_entries(alpha)
    if ref not in [_module_ref_any(item) for item in modules]:
        modules.append({"ref": ref, "enabled": True})
    else:
        modules = [
            {"ref": _module_ref_any(item), "enabled": True} if _module_ref_any(item) == ref else item
            for item in modules
        ]
    alpha["modules"] = modules
    _write_payload(path, payload)
    return _alpha_status(path, sleeve_id, sleeve, workspace)


def disable_sleeve_alpha_module(config_path: str | Path, sleeve_id: str, alpha_ref: str) -> dict[str, Any]:
    path = Path(config_path)
    payload = _load_payload(path)
    sleeve = _find_sleeve(payload, sleeve_id)
    workspace = _workspace_path(path, sleeve)
    ref = _normalize_alpha_ref(alpha_ref)
    alpha = sleeve.setdefault("alpha", {})
    alpha["modules"] = [
        item
        for item in _module_entries(alpha)
        if _module_ref_any(item) != ref
    ]
    _write_payload(path, payload)
    return _alpha_status(path, sleeve_id, sleeve, workspace)


def describe_sleeve_portfolio_model(config_path: str | Path, sleeve_id: str) -> dict[str, Any]:
    path = Path(config_path)
    payload = _load_payload(path)
    sleeve = _find_sleeve(payload, sleeve_id)
    workspace = _workspace_path(path, sleeve)
    return _portfolio_status(path, sleeve_id, sleeve, workspace)


def set_sleeve_portfolio_model(config_path: str | Path, sleeve_id: str, portfolio_ref: str) -> dict[str, Any]:
    path = Path(config_path)
    payload = _load_payload(path)
    sleeve = _find_sleeve(payload, sleeve_id)
    workspace = _workspace_path(path, sleeve)
    ref = _normalize_portfolio_ref(portfolio_ref)
    _assert_portfolio_exists(workspace, ref)
    portfolio = sleeve.setdefault("portfolio", {})
    portfolio["model"] = ref
    _write_payload(path, payload)
    return _portfolio_status(path, sleeve_id, sleeve, workspace)


def describe_sleeve_risk_model(config_path: str | Path, sleeve_id: str) -> dict[str, Any]:
    path = Path(config_path)
    payload = _load_payload(path)
    sleeve = _find_sleeve(payload, sleeve_id)
    workspace = _workspace_path(path, sleeve)
    return _risk_status(path, sleeve_id, sleeve, workspace)


def set_sleeve_risk_model(config_path: str | Path, sleeve_id: str, risk_ref: str) -> dict[str, Any]:
    path = Path(config_path)
    payload = _load_payload(path)
    sleeve = _find_sleeve(payload, sleeve_id)
    workspace = _workspace_path(path, sleeve)
    ref = _normalize_risk_ref(risk_ref)
    _assert_risk_exists(workspace, ref)
    risk = sleeve.setdefault("risk", {})
    risk["model"] = ref
    _write_payload(path, payload)
    return _risk_status(path, sleeve_id, sleeve, workspace)


def describe_sleeve_execution_model(config_path: str | Path, sleeve_id: str) -> dict[str, Any]:
    path = Path(config_path)
    payload = _load_payload(path)
    sleeve = _find_sleeve(payload, sleeve_id)
    workspace = _workspace_path(path, sleeve)
    return _execution_status(path, sleeve_id, sleeve, workspace)


def set_sleeve_execution_model(config_path: str | Path, sleeve_id: str, execution_ref: str) -> dict[str, Any]:
    path = Path(config_path)
    payload = _load_payload(path)
    sleeve = _find_sleeve(payload, sleeve_id)
    workspace = _workspace_path(path, sleeve)
    ref = _normalize_execution_ref(execution_ref)
    _assert_execution_exists(workspace, ref)
    execution = sleeve.setdefault("execution", {})
    execution["model"] = ref
    _write_payload(path, payload)
    return _execution_status(path, sleeve_id, sleeve, workspace)


def _alpha_status(config_path: Path, sleeve_id: str, sleeve: dict[str, Any], workspace: Path) -> dict[str, Any]:
    active = [
        ref
        for ref in (_module_ref(item) for item in _module_entries(sleeve.get("alpha", {})))
        if ref
    ]
    available = _available_alpha_refs(workspace)
    return {
        "sleeve_id": sleeve_id,
        "workspace_path": str(workspace),
        "available_alpha_modules": available,
        "active_alpha_modules": active,
        "inactive_alpha_modules": [ref for ref in available if ref not in active],
        "reload_command": RuntimeControlCommand.reload_sleeve(config_path, sleeve_id).to_dict(),
    }


def _portfolio_status(config_path: Path, sleeve_id: str, sleeve: dict[str, Any], workspace: Path) -> dict[str, Any]:
    active = _portfolio_model_ref(sleeve.get("portfolio", {}))
    available = _available_portfolio_refs(workspace)
    return {
        "sleeve_id": sleeve_id,
        "workspace_path": str(workspace),
        "available_portfolio_models": available,
        "active_portfolio_model": active,
        "inactive_portfolio_models": [ref for ref in available if ref != active],
        "reload_command": RuntimeControlCommand.reload_sleeve(config_path, sleeve_id).to_dict(),
    }


def _risk_status(config_path: Path, sleeve_id: str, sleeve: dict[str, Any], workspace: Path) -> dict[str, Any]:
    active = _risk_model_ref(sleeve.get("risk", {}))
    available = _available_risk_refs(workspace)
    return {
        "sleeve_id": sleeve_id,
        "workspace_path": str(workspace),
        "available_risk_models": available,
        "active_risk_model": active,
        "inactive_risk_models": [ref for ref in available if ref != active],
        "reload_command": RuntimeControlCommand.reload_sleeve(config_path, sleeve_id).to_dict(),
    }


def _execution_status(config_path: Path, sleeve_id: str, sleeve: dict[str, Any], workspace: Path) -> dict[str, Any]:
    active = _execution_model_ref(sleeve.get("execution", {}))
    available = _available_execution_refs(workspace)
    return {
        "sleeve_id": sleeve_id,
        "workspace_path": str(workspace),
        "available_execution_models": available,
        "active_execution_model": active,
        "inactive_execution_models": [ref for ref in available if ref != active],
        "reload_command": RuntimeControlCommand.reload_sleeve(config_path, sleeve_id).to_dict(),
    }


def _load_payload(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SleeveWorkspaceError("Runtime config root must be an object.")
    return payload


def _write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _find_sleeve(payload: dict[str, Any], sleeve_id: str) -> dict[str, Any]:
    sleeves = payload.get("sleeves")
    if not isinstance(sleeves, list):
        raise SleeveWorkspaceError("Runtime config must contain a sleeves list.")
    for item in sleeves:
        if isinstance(item, dict) and str(item.get("sleeve_id", item.get("id", ""))) == sleeve_id:
            return item
    raise SleeveWorkspaceError(f"Unknown sleeve_id: {sleeve_id}")


def _workspace_path(config_path: Path, sleeve: dict[str, Any]) -> Path:
    raw = sleeve.get("workspace_path", sleeve.get("workspace"))
    if not raw:
        raise SleeveWorkspaceError("Sleeve must define workspace_path before managing workspace alpha modules.")
    path = Path(str(raw))
    if path.is_absolute():
        return path
    return (config_path.parent / path).resolve() if (config_path.parent / path).exists() else path.resolve()


def _available_alpha_refs(workspace: Path) -> list[str]:
    alpha_dir = workspace / "alphas"
    if not alpha_dir.exists():
        return []
    return sorted(
        _relative_ref(path, workspace)
        for path in alpha_dir.glob("*.py")
        if path.name != "__init__.py"
    )


def _available_portfolio_refs(workspace: Path) -> list[str]:
    portfolio_dir = workspace / "portfolios"
    if not portfolio_dir.exists():
        return []
    return sorted(
        _relative_ref(path, workspace)
        for path in portfolio_dir.glob("*.py")
        if path.name != "__init__.py"
    )


def _available_risk_refs(workspace: Path) -> list[str]:
    risk_dir = workspace / "risks"
    if not risk_dir.exists():
        return []
    return sorted(
        _relative_ref(path, workspace)
        for path in risk_dir.glob("*.py")
        if path.name != "__init__.py"
    )


def _available_execution_refs(workspace: Path) -> list[str]:
    execution_dir = workspace / "executions"
    if not execution_dir.exists():
        return []
    return sorted(
        _relative_ref(path, workspace)
        for path in execution_dir.glob("*.py")
        if path.name != "__init__.py"
    )


def _relative_ref(path: Path, workspace: Path) -> str:
    return path.relative_to(workspace).as_posix()


def _normalize_alpha_ref(ref: str) -> str:
    text = ref.strip().replace("\\", "/")
    if not text:
        raise SleeveWorkspaceError("alpha_ref cannot be empty.")
    if not text.endswith(".py"):
        text = f"alphas/{text}.py" if "/" not in text else f"{text}.py"
    if "/" not in text:
        text = f"alphas/{text}"
    return text


def _normalize_portfolio_ref(ref: str) -> str:
    return _normalize_model_ref(ref, folder="portfolios", label="portfolio_ref")


def _normalize_risk_ref(ref: str) -> str:
    return _normalize_model_ref(ref, folder="risks", label="risk_ref")


def _normalize_execution_ref(ref: str) -> str:
    return _normalize_model_ref(ref, folder="executions", label="execution_ref")


def _normalize_model_ref(ref: str, *, folder: str, label: str) -> str:
    text = ref.strip().replace("\\", "/")
    if not text:
        raise SleeveWorkspaceError(f"{label} cannot be empty.")
    if ":" in text:
        module_ref, object_ref = text.rsplit(":", 1)
        if _looks_like_file_ref(module_ref):
            return f"{_normalize_model_file_ref(module_ref, folder=folder, label=label)}:{object_ref}"
        return text
    return _normalize_model_file_ref(text, folder=folder, label=label)


def _normalize_model_file_ref(ref: str, *, folder: str, label: str) -> str:
    text = ref.strip().replace("\\", "/")
    if not text:
        raise SleeveWorkspaceError(f"{label} cannot be empty.")
    if not text.endswith(".py"):
        text = f"{folder}/{text}.py" if "/" not in text else f"{text}.py"
    if "/" not in text:
        text = f"{folder}/{text}"
    return text


def _looks_like_file_ref(ref: str) -> bool:
    return ref.endswith(".py") or "/" in ref or "\\" in ref


def _assert_alpha_exists(workspace: Path, ref: str) -> None:
    path = workspace / ref
    if not path.exists():
        raise SleeveWorkspaceError(f"Alpha module does not exist in sleeve workspace: {ref}")


def _assert_portfolio_exists(workspace: Path, ref: str) -> None:
    path = workspace / _file_part(ref)
    if not path.exists():
        raise SleeveWorkspaceError(f"Portfolio model does not exist in sleeve workspace: {ref}")


def _assert_risk_exists(workspace: Path, ref: str) -> None:
    path = workspace / _file_part(ref)
    if not path.exists():
        raise SleeveWorkspaceError(f"Risk model does not exist in sleeve workspace: {ref}")


def _assert_execution_exists(workspace: Path, ref: str) -> None:
    path = workspace / _file_part(ref)
    if not path.exists():
        raise SleeveWorkspaceError(f"Execution model does not exist in sleeve workspace: {ref}")


def _file_part(ref: str) -> str:
    return ref.rsplit(":", 1)[0] if ":" in ref else ref


def _module_entries(alpha_payload: Any) -> list[Any]:
    if not isinstance(alpha_payload, dict):
        return []
    modules = alpha_payload.get("modules", [])
    if not isinstance(modules, list):
        raise SleeveWorkspaceError("alpha.modules must be a list.")
    return list(modules)


def _module_ref(item: Any) -> str:
    if isinstance(item, str):
        return _normalize_alpha_ref(item)
    if isinstance(item, dict):
        if item.get("enabled", True) is False:
            return ""
        return _module_ref_any(item)
    return ""


def _module_ref_any(item: Any) -> str:
    if isinstance(item, str):
        return _normalize_alpha_ref(item)
    if isinstance(item, dict):
        return _normalize_alpha_ref(str(item.get("ref", "")))
    return ""


def _portfolio_model_ref(portfolio_payload: Any) -> str:
    if not isinstance(portfolio_payload, dict):
        return ""
    item = portfolio_payload.get("model", portfolio_payload.get("module", ""))
    if isinstance(item, str):
        return _normalize_portfolio_ref(item)
    if isinstance(item, dict):
        return _normalize_portfolio_ref(str(item.get("ref", "")))
    return ""


def _risk_model_ref(risk_payload: Any) -> str:
    if not isinstance(risk_payload, dict):
        return ""
    item = risk_payload.get("model", risk_payload.get("module", ""))
    if isinstance(item, str):
        return _normalize_risk_ref(item)
    if isinstance(item, dict):
        return _normalize_risk_ref(str(item.get("ref", "")))
    return ""


def _execution_model_ref(execution_payload: Any) -> str:
    if not isinstance(execution_payload, dict):
        return ""
    item = execution_payload.get("model", execution_payload.get("module", ""))
    if isinstance(item, str):
        return _normalize_execution_ref(item)
    if isinstance(item, dict):
        return _normalize_execution_ref(str(item.get("ref", "")))
    return ""
