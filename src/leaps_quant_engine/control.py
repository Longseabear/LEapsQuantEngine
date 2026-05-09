from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Any, Callable
from uuid import uuid4

from leaps_quant_engine.runtime_config import RuntimeConfigSnapshot, load_runtime_config_snapshot


class RuntimeControlCommandType(str, Enum):
    RELOAD_CONFIG = "reload_config"
    RELOAD_SLEEVE = "reload_sleeve"
    PAUSE_WORKER = "pause_worker"
    RESUME_WORKER = "resume_worker"
    RUN_ONCE = "run_once"
    SHUTDOWN = "shutdown"


@dataclass(frozen=True, slots=True)
class RuntimeControlCommand:
    command_type: RuntimeControlCommandType
    command_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None

    @classmethod
    def reload_config(cls, config_path: str | Path, *, reason: str | None = None) -> "RuntimeControlCommand":
        return cls(
            command_type=RuntimeControlCommandType.RELOAD_CONFIG,
            payload={"config_path": str(config_path)},
            reason=reason,
        )

    @classmethod
    def reload_sleeve(
        cls,
        config_path: str | Path,
        sleeve_id: str,
        *,
        reason: str | None = None,
    ) -> "RuntimeControlCommand":
        return cls(
            command_type=RuntimeControlCommandType.RELOAD_SLEEVE,
            payload={"config_path": str(config_path), "sleeve_id": sleeve_id},
            reason=reason,
        )

    @classmethod
    def pause_worker(cls, *, reason: str | None = None) -> "RuntimeControlCommand":
        return cls(command_type=RuntimeControlCommandType.PAUSE_WORKER, reason=reason)

    @classmethod
    def resume_worker(cls, *, reason: str | None = None) -> "RuntimeControlCommand":
        return cls(command_type=RuntimeControlCommandType.RESUME_WORKER, reason=reason)

    @classmethod
    def run_once(cls, *, reason: str | None = None) -> "RuntimeControlCommand":
        return cls(command_type=RuntimeControlCommandType.RUN_ONCE, reason=reason)

    @classmethod
    def shutdown(cls, *, reason: str | None = None) -> "RuntimeControlCommand":
        return cls(command_type=RuntimeControlCommandType.SHUTDOWN, reason=reason)

    def config_path(self) -> Path:
        value = self.payload.get("config_path")
        if not value:
            raise ValueError("reload_config command requires payload.config_path.")
        return Path(str(value))

    def sleeve_id(self) -> str:
        value = self.payload.get("sleeve_id")
        if not value:
            raise ValueError("sleeve reload command requires payload.sleeve_id.")
        return str(value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_type": self.command_type.value,
            "command_id": self.command_id,
            "created_at": self.created_at,
            "payload": dict(self.payload),
            "reason": self.reason,
        }


@dataclass(slots=True)
class RuntimeControlQueue:
    _commands: deque[RuntimeControlCommand] = field(default_factory=deque)
    _lock: Lock = field(default_factory=Lock)

    def submit(self, command: RuntimeControlCommand) -> RuntimeControlCommand:
        with self._lock:
            self._commands.append(command)
        return command

    def drain(self) -> tuple[RuntimeControlCommand, ...]:
        with self._lock:
            commands = tuple(self._commands)
            self._commands.clear()
        return commands

    def __len__(self) -> int:
        with self._lock:
            return len(self._commands)


@dataclass(frozen=True, slots=True)
class RuntimeControlApplyReport:
    applied_commands: tuple[RuntimeControlCommand, ...]
    previous_version: str
    current_version: str
    paused: bool
    run_once_requested: bool = False
    shutdown_requested: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied_commands": [command.to_dict() for command in self.applied_commands],
            "previous_version": self.previous_version,
            "current_version": self.current_version,
            "paused": self.paused,
            "run_once_requested": self.run_once_requested,
            "shutdown_requested": self.shutdown_requested,
        }


@dataclass(slots=True)
class RuntimeConfigController:
    snapshot: RuntimeConfigSnapshot
    queue: RuntimeControlQueue = field(default_factory=RuntimeControlQueue)
    loader: Callable[[Path], RuntimeConfigSnapshot] = load_runtime_config_snapshot
    paused: bool = False

    def apply_pending(self) -> RuntimeControlApplyReport:
        previous_version = self.snapshot.version
        applied: list[RuntimeControlCommand] = []
        run_once_requested = False
        shutdown_requested = False
        for command in self.queue.drain():
            if command.command_type in {RuntimeControlCommandType.RELOAD_CONFIG, RuntimeControlCommandType.RELOAD_SLEEVE}:
                self.snapshot = self.loader(command.config_path())
            elif command.command_type == RuntimeControlCommandType.PAUSE_WORKER:
                self.paused = True
            elif command.command_type == RuntimeControlCommandType.RESUME_WORKER:
                self.paused = False
            elif command.command_type == RuntimeControlCommandType.RUN_ONCE:
                run_once_requested = True
            elif command.command_type == RuntimeControlCommandType.SHUTDOWN:
                shutdown_requested = True
            else:
                raise ValueError(f"Unsupported runtime control command: {command.command_type}")
            applied.append(command)
        return RuntimeControlApplyReport(
            applied_commands=tuple(applied),
            previous_version=previous_version,
            current_version=self.snapshot.version,
            paused=self.paused,
            run_once_requested=run_once_requested,
            shutdown_requested=shutdown_requested,
        )
