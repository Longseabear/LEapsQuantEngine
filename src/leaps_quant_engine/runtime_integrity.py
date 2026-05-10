from __future__ import annotations

from dataclasses import dataclass
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from leaps_quant_engine.runtime_config import RuntimeConfigSnapshot, SleeveRuntimeConfig


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    root: Path
    digest: str
    file_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "digest": self.digest,
            "file_count": self.file_count,
        }


@dataclass(frozen=True, slots=True)
class RuntimeFileFingerprint:
    label: str
    ref: str
    path: Path
    exists: bool
    digest: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "ref": self.ref,
            "path": str(self.path),
            "exists": self.exists,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class RuntimeCodeIdentity:
    config_version: str
    engine_source: SourceFingerprint
    file_fingerprints: tuple[RuntimeFileFingerprint, ...]
    runtime_fingerprint: str

    @property
    def missing_files(self) -> tuple[RuntimeFileFingerprint, ...]:
        return tuple(item for item in self.file_fingerprints if not item.exists)

    def journal_metadata(self) -> dict[str, Any]:
        return {
            "engine_source_hash": self.engine_source.digest,
            "runtime_fingerprint": self.runtime_fingerprint,
            "runtime_file_fingerprints": [
                item.to_dict()
                for item in self.file_fingerprints
                if item.exists
            ],
        }

    def to_dict(self, *, include_files: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "config_version": self.config_version,
            "engine_source": self.engine_source.to_dict(),
            "runtime_fingerprint": self.runtime_fingerprint,
            "missing_file_count": len(self.missing_files),
        }
        if include_files:
            payload["files"] = [item.to_dict() for item in self.file_fingerprints]
        return payload


def current_engine_source_fingerprint(package_root: Path | None = None) -> SourceFingerprint:
    root = (package_root or Path(__file__).resolve().parent).resolve()
    return _cached_source_fingerprint(str(root))


def build_runtime_code_identity(
    snapshot: RuntimeConfigSnapshot,
    *,
    sleeve_ids: Iterable[str] | None = None,
) -> RuntimeCodeIdentity:
    selected_sleeve_ids = tuple(dict.fromkeys(sleeve_ids or (sleeve.sleeve_id for sleeve in snapshot.config.sleeves)))
    selected_sleeves = tuple(snapshot.config.sleeve(sleeve_id) for sleeve_id in selected_sleeve_ids)
    engine_source = current_engine_source_fingerprint()
    file_fingerprints = tuple(_runtime_file_fingerprints(snapshot, selected_sleeves))
    digest = hashlib.sha256()
    digest.update(snapshot.version.encode("utf-8"))
    digest.update(b"\n")
    digest.update(engine_source.digest.encode("utf-8"))
    digest.update(b"\n")
    for item in file_fingerprints:
        digest.update(item.label.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.path).encode("utf-8"))
        digest.update(b"\0")
        digest.update((item.digest or "missing").encode("utf-8"))
        digest.update(b"\n")
    return RuntimeCodeIdentity(
        config_version=snapshot.version,
        engine_source=engine_source,
        file_fingerprints=file_fingerprints,
        runtime_fingerprint=f"sha256:{digest.hexdigest()}",
    )


@lru_cache(maxsize=8)
def _cached_source_fingerprint(root_text: str) -> SourceFingerprint:
    root = Path(root_text).resolve()
    digest = hashlib.sha256()
    file_count = 0
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\n")
        file_count += 1
    return SourceFingerprint(root=root, digest=f"sha256:{digest.hexdigest()}", file_count=file_count)


def _runtime_file_fingerprints(
    snapshot: RuntimeConfigSnapshot,
    sleeves: tuple[SleeveRuntimeConfig, ...],
) -> tuple[RuntimeFileFingerprint, ...]:
    refs: list[tuple[str, str, Path]] = []
    for sleeve in sleeves:
        refs.append((f"{sleeve.sleeve_id}:universe", str(sleeve.universe.coarse_path), _resolve_runtime_path(snapshot, sleeve.universe.coarse_path)))
        for index, module in enumerate(sleeve.alpha.modules):
            path = _file_path_from_ref(snapshot, sleeve, module.ref)
            if path is not None:
                refs.append((f"{sleeve.sleeve_id}:alpha:{index}", module.ref, path))
        for label, ref in (
            ("selection", sleeve.universe.active.selection_model.ref),
            ("portfolio", sleeve.portfolio.model.ref),
            ("risk", sleeve.risk.model.ref),
            ("execution", sleeve.execution.model.ref),
        ):
            path = _file_path_from_ref(snapshot, sleeve, ref)
            if path is not None:
                refs.append((f"{sleeve.sleeve_id}:{label}", ref, path))
        for index, selection_ref in enumerate(sleeve.universe.active.selection_models):
            path = _file_path_from_ref(snapshot, sleeve, selection_ref.ref)
            if path is not None:
                refs.append((f"{sleeve.sleeve_id}:selection:{index}", selection_ref.ref, path))

    fingerprints: list[RuntimeFileFingerprint] = []
    seen: set[tuple[str, Path]] = set()
    for label, ref, path in refs:
        resolved = path.resolve()
        key = (label, resolved)
        if key in seen:
            continue
        seen.add(key)
        exists = resolved.exists()
        fingerprints.append(
            RuntimeFileFingerprint(
                label=label,
                ref=ref,
                path=resolved,
                exists=exists,
                digest=f"sha256:{hashlib.sha256(resolved.read_bytes()).hexdigest()}" if exists and resolved.is_file() else None,
            )
        )
    return tuple(fingerprints)


def _file_path_from_ref(
    snapshot: RuntimeConfigSnapshot,
    sleeve: SleeveRuntimeConfig,
    ref: str,
) -> Path | None:
    file_ref = _extract_file_ref(ref)
    if file_ref is None:
        return None
    path = Path(file_ref)
    if path.is_absolute():
        return path
    if sleeve.workspace_path is not None:
        return _resolve_runtime_path(snapshot, sleeve.workspace_path) / path
    return _resolve_runtime_path(snapshot, path)


def _extract_file_ref(ref: str) -> str | None:
    text = str(ref).strip()
    if not text:
        return None
    marker = ".py:"
    if marker in text:
        return text[: text.index(marker) + len(".py")]
    path = Path(text)
    if path.suffix == ".py" or path.exists():
        return text
    return None


def _resolve_runtime_path(snapshot: RuntimeConfigSnapshot, path: str | Path) -> Path:
    candidate = Path(path)
    if candidate.is_absolute() or candidate.exists():
        return candidate
    return snapshot.source_path.parent / candidate
