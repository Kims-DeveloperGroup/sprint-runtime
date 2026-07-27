from __future__ import annotations

import json
import math
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


_ENVIRONMENT_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_ENVIRONMENT_NAME_PATTERN = re.compile(
    r"(?:^|_)(?:API_?KEY|AUTH|CREDENTIALS?|PASSWORD|SECRET|TOKEN)(?:$|_)",
    re.IGNORECASE,
)
_TERMINAL_STATES = {
    "completed",
    "failed",
    "timeout",
    "launch_failed",
    "terminated",
}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _positive_finite_number(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive finite number.")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number.") from exc
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be a positive finite number.")
    return normalized


def _non_negative_finite_number(value: Any, *, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a non-negative finite number.")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative finite number.") from exc
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(f"{name} must be a non-negative finite number.")
    return normalized


class InvocationBudgetExceeded(RuntimeError):
    def __init__(self, max_invocations: int, reserved_count: int):
        self.max_invocations = max_invocations
        self.reserved_count = reserved_count
        super().__init__(
            f"Model invocation budget exhausted: {reserved_count}/{max_invocations} calls are already reserved."
        )


class ModelExecutionPolicyViolation(RuntimeError):
    """Raised before launch when a benchmark request violates its safety policy."""


class ModelInvocationTimeout(RuntimeError, TimeoutError):
    def __init__(
        self,
        timeout_seconds: float,
        *,
        completed_process: Any = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.completed_process = completed_process
        super().__init__(f"Model invocation exceeded the {timeout_seconds:g}-second timeout.")


class InvocationReservation:
    __slots__ = ("_budget", "reservation_id")

    def __init__(self, budget: "InvocationBudget", reservation_id: str):
        self._budget = budget
        self.reservation_id = reservation_id

    def mark_started(self, *, pid: int, process_group_id: int | None) -> None:
        self._budget._mark_started(  # noqa: SLF001 - reservation is the budget's public mutation handle
            self.reservation_id,
            pid=pid,
            process_group_id=process_group_id,
        )

    def complete(
        self,
        *,
        state: str,
        exit_code: int | None,
        stop_reason: str,
    ) -> None:
        self._budget._complete(  # noqa: SLF001 - reservation is the budget's public mutation handle
            self.reservation_id,
            state=state,
            exit_code=exit_code,
            stop_reason=stop_reason,
        )


class InvocationBudget:
    """Thread-safe physical-call budget with an atomic, privacy-safe journal."""

    def __init__(
        self,
        max_invocations: int,
        *,
        journal_path: str | os.PathLike[str] | None = None,
    ):
        if isinstance(max_invocations, bool) or not isinstance(max_invocations, int) or max_invocations <= 0:
            raise ValueError("max_invocations must be a positive integer.")
        self.max_invocations = max_invocations
        self.journal_path = (
            Path(journal_path).expanduser().resolve()
            if journal_path is not None
            else None
        )
        self._lock = threading.RLock()
        self._entries: list[dict[str, Any]] = []
        self._entries_by_id: dict[str, dict[str, Any]] = {}
        self._rejected_count = 0

    @property
    def reserved_count(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(self.max_invocations - len(self._entries), 0)

    @property
    def rejected_count(self) -> int:
        with self._lock:
            return self._rejected_count

    def reserve(
        self,
        invocation_context: Any = None,
        *,
        provider: str,
        role: str = "",
    ) -> InvocationReservation:
        with self._lock:
            if len(self._entries) >= self.max_invocations:
                self._rejected_count += 1
                self._persist_locked()
                raise InvocationBudgetExceeded(self.max_invocations, len(self._entries))

            reservation_id = uuid.uuid4().hex
            entry = {
                "reservation_id": reservation_id,
                "provider": str(provider or "").strip(),
                "invocation_id": str(getattr(invocation_context, "invocation_id", "") or "").strip(),
                "operation_id": str(getattr(invocation_context, "operation_id", "") or "").strip(),
                "logical_call_id": str(getattr(invocation_context, "logical_call_id", "") or "").strip(),
                "attempt_index": getattr(invocation_context, "attempt_index", None),
                "attempt_kind": str(getattr(invocation_context, "attempt_kind", "") or "").strip(),
                "runtime_identity": str(
                    getattr(invocation_context, "runtime_identity", "") or ""
                ).strip(),
                "role": str(getattr(invocation_context, "role", "") or role or "").strip(),
                "purpose": str(getattr(invocation_context, "purpose", "") or "").strip(),
                "workflow_step": str(
                    getattr(invocation_context, "workflow_step", "") or ""
                ).strip(),
                "request_id": str(getattr(invocation_context, "request_id", "") or "").strip(),
                "sprint_id": str(getattr(invocation_context, "sprint_id", "") or "").strip(),
                "todo_id": str(getattr(invocation_context, "todo_id", "") or "").strip(),
                "backlog_id": str(getattr(invocation_context, "backlog_id", "") or "").strip(),
                "goal_id": str(getattr(invocation_context, "goal_id", "") or "").strip(),
                "state": "reserved",
                "reserved_at": _utc_timestamp(),
                "started_at": "",
                "completed_at": "",
                "pid": None,
                "process_group_id": None,
                "exit_code": None,
                "stop_reason": "",
            }
            self._entries.append(entry)
            self._entries_by_id[reservation_id] = entry
            self._persist_locked()
            return InvocationReservation(self, reservation_id)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "max_invocations": self.max_invocations,
            "reserved_count": len(self._entries),
            "remaining": max(self.max_invocations - len(self._entries), 0),
            "rejected_count": self._rejected_count,
            "entries": [dict(entry) for entry in self._entries],
        }

    def _mark_started(
        self,
        reservation_id: str,
        *,
        pid: int,
        process_group_id: int | None,
    ) -> None:
        with self._lock:
            entry = self._entries_by_id[reservation_id]
            if entry["state"] != "reserved":
                raise RuntimeError(f"Invocation reservation {reservation_id} is already started.")
            entry.update(
                {
                    "state": "running",
                    "started_at": _utc_timestamp(),
                    "pid": int(pid),
                    "process_group_id": (
                        int(process_group_id)
                        if process_group_id is not None
                        else None
                    ),
                }
            )
            self._persist_locked()

    def _complete(
        self,
        reservation_id: str,
        *,
        state: str,
        exit_code: int | None,
        stop_reason: str,
    ) -> None:
        normalized_state = str(state or "").strip()
        if normalized_state not in _TERMINAL_STATES:
            raise ValueError(f"Unsupported invocation terminal state: {state}")
        with self._lock:
            entry = self._entries_by_id[reservation_id]
            if entry["state"] in _TERMINAL_STATES:
                return
            entry.update(
                {
                    "state": normalized_state,
                    "completed_at": _utc_timestamp(),
                    "exit_code": int(exit_code) if exit_code is not None else None,
                    "stop_reason": str(stop_reason or "").strip(),
                }
            )
            self._persist_locked()

    def _persist_locked(self) -> None:
        if self.journal_path is None:
            return
        journal_path = self.journal_path
        journal_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            journal_path.parent.chmod(0o700)
        except OSError:
            pass
        descriptor, temporary_name = tempfile.mkstemp(
            dir=str(journal_path.parent),
            prefix=f".{journal_path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            try:
                os.fchmod(descriptor, 0o600)
            except OSError:
                pass
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    self._snapshot_locked(),
                    handle,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, journal_path)
            try:
                journal_path.chmod(0o600)
            except OSError:
                pass
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass
            raise


def _normalize_shell_environment(values: Mapping[str, str]) -> Mapping[str, str]:
    normalized: dict[str, str] = {}
    for raw_name, raw_value in values.items():
        name = str(raw_name or "").strip()
        if not _ENVIRONMENT_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid shell environment variable name: {raw_name!r}")
        if _SENSITIVE_ENVIRONMENT_NAME_PATTERN.search(name):
            raise ValueError(
                f"Benchmark shell environment must not expose secret-bearing variable {name}."
            )
        if not isinstance(raw_value, str):
            raise ValueError(f"Benchmark shell environment value for {name} must be a string.")
        if "\x00" in raw_value or "\n" in raw_value or "\r" in raw_value:
            raise ValueError(f"Benchmark shell environment value for {name} contains control characters.")
        normalized[name] = raw_value
    return MappingProxyType(dict(sorted(normalized.items())))


@dataclass(slots=True, frozen=True)
class ModelExecutionPolicy:
    """Immutable opt-in controls for benchmark model execution."""

    benchmark_mode: bool = False
    call_timeout_seconds: float | None = None
    kill_grace_seconds: float = 5.0
    invocation_budget: InvocationBudget | None = field(default=None, compare=False)
    allowed_workspace_root: Path | None = None
    shell_environment: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}),
        hash=False,
    )

    def __post_init__(self) -> None:
        normalized_environment = _normalize_shell_environment(self.shell_environment)
        object.__setattr__(self, "shell_environment", normalized_environment)
        object.__setattr__(
            self,
            "kill_grace_seconds",
            _non_negative_finite_number(
                self.kill_grace_seconds,
                name="kill_grace_seconds",
            ),
        )
        if not self.benchmark_mode:
            if (
                self.call_timeout_seconds is not None
                or self.invocation_budget is not None
                or self.allowed_workspace_root is not None
                or normalized_environment
            ):
                raise ValueError(
                    "Bounded execution controls require benchmark_mode=True; use "
                    "ModelExecutionPolicy.for_benchmark()."
                )
            return

        object.__setattr__(
            self,
            "call_timeout_seconds",
            _positive_finite_number(
                self.call_timeout_seconds,
                name="call_timeout_seconds",
            ),
        )
        if self.invocation_budget is None:
            raise ValueError("Benchmark execution requires an invocation budget.")
        if self.allowed_workspace_root is None:
            raise ValueError("Benchmark execution requires an allowed workspace root.")
        allowed_root = Path(self.allowed_workspace_root).expanduser().resolve()
        object.__setattr__(self, "allowed_workspace_root", allowed_root)

    @classmethod
    def for_benchmark(
        cls,
        *,
        allowed_workspace_root: str | os.PathLike[str],
        invocation_budget: InvocationBudget,
        call_timeout_seconds: float,
        kill_grace_seconds: float = 5.0,
        shell_environment: Mapping[str, str] | None = None,
    ) -> "ModelExecutionPolicy":
        allowed_root = Path(allowed_workspace_root).expanduser().resolve()
        environment = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(allowed_root),
            "PATH": os.environ.get("PATH") or os.defpath,
            "TMPDIR": str(allowed_root / ".tmp"),
        }
        environment.update(shell_environment or {})
        return cls(
            benchmark_mode=True,
            call_timeout_seconds=call_timeout_seconds,
            kill_grace_seconds=kill_grace_seconds,
            invocation_budget=invocation_budget,
            allowed_workspace_root=allowed_root,
            shell_environment=environment,
        )

    def assert_workspace_allowed(self, workspace: Path) -> None:
        if not self.benchmark_mode:
            return
        allowed_root = self.allowed_workspace_root
        try:
            resolved_workspace = Path(workspace).expanduser().resolve()
        except (OSError, RuntimeError) as exc:
            raise ModelExecutionPolicyViolation(
                f"Benchmark workspace {workspace} could not be resolved safely."
            ) from exc
        if allowed_root is None or not resolved_workspace.is_relative_to(allowed_root):
            raise ModelExecutionPolicyViolation(
                f"Benchmark workspace {resolved_workspace} is outside the allowed root {allowed_root}."
            )


DEFAULT_MODEL_EXECUTION_POLICY = ModelExecutionPolicy()


__all__ = [
    "DEFAULT_MODEL_EXECUTION_POLICY",
    "InvocationBudget",
    "InvocationBudgetExceeded",
    "InvocationReservation",
    "ModelExecutionPolicy",
    "ModelExecutionPolicyViolation",
    "ModelInvocationTimeout",
]
