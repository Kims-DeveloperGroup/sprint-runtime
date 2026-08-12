from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from teams_runtime.benchmarking.metrics import (
    is_v2_target_projection,
    load_telemetry_directory,
    sanitize_invocation_record,
)
from teams_runtime.benchmarking.models import (
    ArmPlan,
    BenchmarkWorkerSafetyError,
    QualityEvidence,
    SprintEvidence,
    WorkerContext,
    WorkerOutcome,
    invocation_identity_digest,
    sanitize_invocation_attempts,
)
from teams_runtime.benchmarking.scenario import (
    DEFAULT_HISTORY_SEED_COUNT,
    canonical_hash,
)
from teams_runtime.runtime.execution_policy import (
    InvocationBudget,
    InvocationBudgetExceeded,
    ModelExecutionPolicy,
    ModelExecutionPolicyViolation,
    ModelInvocationTimeout,
    quarantine_unsafe_workspace_entries,
)
from teams_runtime.shared.models import TEAM_ROLES
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.workflows.orchestration.team_service import TeamService
from teams_runtime.workflows.sprints.lifecycle import (
    INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
    apply_initial_plan_confirmation,
)
from teams_runtime.workflows.state.sprint_store import iter_sprint_states


LIVE_BENCHMARK_ENV = "TEAMS_RUNTIME_LIVE_BENCHMARK"
_TERMINAL_SPRINT_STATUSES = frozenset({"completed", "failed", "blocked"})
_COMPLETED_TODO_STATUSES = frozenset({"completed", "committed"})
_MAX_RESUME_PASSES = 16
_RELAY_POLL_SECONDS = 0.02
_CHILD_TERMINATION_GRACE_SECONDS = 5.0
_CALL_JOURNAL_STATES = (
    "reserved",
    "running",
    "completed",
    "failed",
    "timeout",
    "launch_failed",
    "terminated",
)
_JOURNAL_CONTEXT_STRING_FIELDS = (
    "provider",
    "operation_id",
    "logical_call_id",
    "attempt_kind",
    "runtime_identity",
    "role",
    "purpose",
    "workflow_step",
    "request_id",
    "sprint_id",
    "todo_id",
    "backlog_id",
    "goal_id",
    "prompt_context_selection_policy",
)
_JOURNAL_CONTEXT_INTEGER_FIELDS = (
    "attempt_index",
    "prompt_context_total_events",
    "prompt_context_included_events",
    "prompt_context_omitted_events",
    "prompt_context_recent_events",
    "prompt_context_max_events",
)
_JOURNAL_CONTEXT_FIELDS = (
    *_JOURNAL_CONTEXT_STRING_FIELDS,
    *_JOURNAL_CONTEXT_INTEGER_FIELDS,
    "prompt_context_enabled",
)
_PROVIDER_AUTH_ENVIRONMENT_KEYS = (
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
)
_CHILD_ENVIRONMENT_KEYS = (
    "CODEX_API_KEY",
    "CODEX_HOME",
    "CURL_CA_BUNDLE",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TEMP",
    "TMP",
    "TMPDIR",
)


class _BenchmarkRunTimeout(TimeoutError):
    pass


class _SprintDidNotTerminate(RuntimeError):
    pass


class _InitialPlanNotReady(RuntimeError):
    pass


class _WorkerCleanupFailure(BenchmarkWorkerSafetyError):
    pass


class _BenchmarkHistorySeedState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.request_id = ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class _WorkerLog:
    """Append-only, content-free worker diagnostics."""

    def __init__(self, path: Path, *, reset: bool = False):
        self.path = path
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        if reset or not self.path.exists():
            self.path.write_text("", encoding="utf-8")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def append(self, event: str, **fields: Any) -> None:
        safe_fields = " ".join(
            f"{key}={str(value).replace(chr(10), ' ').replace(chr(13), ' ')}"
            for key, value in sorted(fields.items())
        )
        line = f"{_utc_now_iso()} event={event}"
        if safe_fields:
            line += f" {safe_fields}"
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class _BenchmarkTeamService(TeamService):
    """Production TeamService with benchmark-only outbound and history seams."""

    def __init__(
        self,
        *args: Any,
        benchmark_context: WorkerContext,
        benchmark_history_state: _BenchmarkHistorySeedState | None = None,
        **kwargs: Any,
    ):
        self._benchmark_context = benchmark_context
        self._benchmark_history_state = (
            benchmark_history_state or _BenchmarkHistorySeedState()
        )
        self._benchmark_history_seeded = False
        super().__init__(*args, **kwargs)

    def _prepare_benchmark_history(self, request_record: dict[str, Any]) -> bool:
        params = (
            dict(request_record.get("params") or {})
            if isinstance(request_record.get("params"), dict)
            else {}
        )
        if (
            str(params.get("_teams_kind") or "").strip() != "sprint_internal"
            or str(params.get("sprint_phase") or "").strip() != "initial"
            or str(params.get("initial_phase_step") or "").strip()
            != INITIAL_PHASE_STEP_MILESTONE_REFINEMENT
        ):
            return False

        request_id = str(request_record.get("request_id") or "").strip()
        if not request_id:
            raise ValueError("Benchmark history target request must have an id")
        seed = _copy_history_seed(self._benchmark_context.history_seed)
        expected_marker = {
            "event_count": len(seed),
            "sha256": _history_seed_hash(seed),
        }
        existing_marker = params.get("_benchmark_history_seed")
        if existing_marker is not None:
            events = [
                dict(event)
                for event in (request_record.get("events") or [])
                if isinstance(event, dict)
            ]
            if (
                existing_marker != expected_marker
                or _history_seed_hash(events[: len(seed)])
                != expected_marker["sha256"]
            ):
                raise ValueError("Benchmark request contains an invalid history seed marker")
            seeded_request_id = self._benchmark_history_state.request_id
            if seeded_request_id and seeded_request_id != request_id:
                raise ValueError(
                    "Benchmark history seed marker appears on multiple requests"
                )
            return True
        if self._benchmark_history_state.request_id:
            return False

        request_record["events"] = [
            *seed,
            *[
                dict(event)
                for event in (request_record.get("events") or [])
                if isinstance(event, dict)
            ],
        ]
        params["_benchmark_history_seed"] = expected_marker
        request_record["params"] = params
        return True

    def _save_request(self, request_record: dict[str, Any]) -> None:
        with self._benchmark_history_state.lock:
            history_prepared = self._prepare_benchmark_history(request_record)
            super()._save_request(request_record)
            if history_prepared:
                self._benchmark_history_state.request_id = str(
                    request_record.get("request_id") or ""
                ).strip()
            self._benchmark_history_seeded = bool(
                self._benchmark_history_state.request_id
            )

    def _mark_github_publish_skipped(self, sprint_state: dict[str, Any]) -> None:
        sprint_state["github_issue_number"] = ""
        sprint_state["github_issue_url"] = ""
        sprint_state["github_issue_publish_status"] = "skipped_benchmark"
        sprint_state["github_issue_publish_updated_at"] = _utc_now_iso()
        sprint_state.pop("github_issue_publish_error", None)
        self._save_sprint_state(sprint_state)

    def _schedule_sprint_issue_publish(self, sprint_state: dict[str, Any]) -> None:
        self._mark_github_publish_skipped(sprint_state)

    async def _publish_sprint_issue_best_effort(
        self,
        sprint_state: dict[str, Any],
    ) -> None:
        self._mark_github_publish_skipped(sprint_state)
        return None

    async def _publish_sprint_issue_before_terminal_reports(
        self,
        sprint_state: dict[str, Any],
    ) -> None:
        self._mark_github_publish_skipped(sprint_state)


def _copy_history_seed(
    history_seed: tuple[Mapping[str, Any], ...],
) -> list[dict[str, Any]]:
    payload = json.loads(
        json.dumps(
            list(history_seed),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("Benchmark history seed must contain JSON object events")
    return [dict(item) for item in payload]


def _history_seed_hash(history_seed: list[dict[str, Any]]) -> str:
    normalized: list[dict[str, Any]] = []
    for raw_event in history_seed:
        event = dict(raw_event)
        created_at = str(event.get("created_at") or "").strip()
        if created_at:
            parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Benchmark history timestamps must include a timezone")
            event["created_at"] = parsed.astimezone(timezone.utc).isoformat()
        normalized.append(event)
    return canonical_hash(normalized)


def _validate_context(context: WorkerContext) -> None:
    if not context.live:
        raise ModelExecutionPolicyViolation(
            "Live benchmark worker requires WorkerContext.live=True"
        )
    if os.environ.get(LIVE_BENCHMARK_ENV) != "1":
        raise ModelExecutionPolicyViolation(
            f"Live benchmark worker requires {LIVE_BENCHMARK_ENV}=1"
        )
    workspace_root = context.workspace_root.expanduser().resolve()
    run_output_dir = context.run_output_dir.expanduser().resolve()
    if run_output_dir.is_relative_to(workspace_root):
        raise ModelExecutionPolicyViolation(
            "Benchmark run output must be outside the provider-writable workspace"
        )
    if not workspace_root.is_dir():
        raise FileNotFoundError(f"Benchmark workspace does not exist: {workspace_root}")
    for relative_path in ("team_runtime.yaml", ".git", ".benchmark/scenario.json"):
        if not (workspace_root / relative_path).exists():
            raise FileNotFoundError(
                f"Benchmark workspace is missing required path: {relative_path}"
            )
    if len(context.history_seed) != DEFAULT_HISTORY_SEED_COUNT:
        raise ValueError(
            "Live sprint benchmark requires the fixed "
            f"{DEFAULT_HISTORY_SEED_COUNT}-event history seed"
        )
    if not str(context.milestone or "").strip():
        raise ValueError("Benchmark milestone must not be empty")
    if context.max_invocations <= 0:
        raise ValueError("Benchmark invocation budget must be positive")
    if (
        not math.isfinite(context.call_timeout_seconds)
        or context.call_timeout_seconds <= 0
    ):
        raise ValueError("Benchmark call timeout must be positive and finite")
    if (
        not math.isfinite(context.run_timeout_seconds)
        or context.run_timeout_seconds <= 0
    ):
        raise ValueError("Benchmark run timeout must be positive and finite")


def _source_import_root() -> Path:
    # teams_runtime/benchmarking/worker.py -> import parent containing teams_runtime.
    return Path(__file__).resolve().parents[2]


def _private_telemetry_dir(context: WorkerContext) -> Path:
    return (
        context.run_output_dir.expanduser().resolve()
        / ".private_model_invocations"
    )


def _benchmark_unsafe_path_roots(context: WorkerContext) -> tuple[Path, ...]:
    roots = {
        context.workspace_root.expanduser().resolve(),
        context.run_output_dir.expanduser().resolve(),
        Path("/tmp").resolve(),
        Path(tempfile.gettempdir()).expanduser().resolve(),
    }
    for name in ("TEMP", "TMP", "TMPDIR"):
        raw_value = str(os.environ.get(name) or "").strip()
        if raw_value:
            roots.add(Path(raw_value).expanduser().resolve())
    return tuple(sorted(roots, key=str))


def _sanitized_benchmark_path(context: WorkerContext) -> str:
    safe_directories: list[str] = []
    seen: set[str] = set()
    unsafe_roots = _benchmark_unsafe_path_roots(context)
    for raw_entry in (os.environ.get("PATH") or os.defpath).split(os.pathsep):
        if not raw_entry:
            continue
        entry = Path(raw_entry).expanduser()
        if not entry.is_absolute():
            continue
        try:
            resolved = entry.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved.is_dir() or any(
            resolved.is_relative_to(root) for root in unsafe_roots
        ):
            continue
        resolved_text = str(resolved)
        if resolved_text not in seen:
            seen.add(resolved_text)
            safe_directories.append(resolved_text)
    if not safe_directories:
        raise ModelExecutionPolicyViolation(
            "Benchmark PATH contains no safe external executable directories"
        )
    return os.pathsep.join(safe_directories)


def _resolve_benchmark_codex_executable(
    context: WorkerContext,
    *,
    search_path: str,
) -> Path:
    located = shutil.which("codex", path=search_path)
    if not located:
        raise ModelExecutionPolicyViolation(
            "Live sprint benchmark requires the Codex CLI on a safe PATH"
        )
    try:
        executable = Path(located).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ModelExecutionPolicyViolation(
            "Benchmark Codex executable could not be resolved safely"
        ) from exc
    if (
        not executable.is_file()
        or not os.access(executable, os.X_OK)
        or any(
            executable.is_relative_to(root)
            for root in _benchmark_unsafe_path_roots(context)
        )
    ):
        raise ModelExecutionPolicyViolation(
            "Benchmark Codex executable is not a safe external executable"
        )
    return executable


def _initialize_private_telemetry(context: WorkerContext) -> Path:
    telemetry_dir = _private_telemetry_dir(context)
    if telemetry_dir.exists() or telemetry_dir.is_symlink():
        raise _WorkerCleanupFailure(
            "Benchmark private telemetry directory already exists"
        )
    try:
        telemetry_dir.mkdir(parents=True, mode=0o700)
        telemetry_dir.chmod(0o700)
    except OSError as exc:
        raise _WorkerCleanupFailure(
            "Failed to initialize benchmark private telemetry directory"
        ) from exc
    return telemetry_dir


def _load_private_telemetry(
    context: WorkerContext,
) -> tuple[dict[str, Any], ...]:
    return load_telemetry_directory(_private_telemetry_dir(context))


def _consume_private_telemetry(
    context: WorkerContext,
) -> tuple[dict[str, Any], ...]:
    telemetry_dir = _private_telemetry_dir(context)
    records = load_telemetry_directory(telemetry_dir)
    if not telemetry_dir.exists() and not telemetry_dir.is_symlink():
        return records
    try:
        shutil.rmtree(telemetry_dir)
    except OSError as exc:
        raise _WorkerCleanupFailure(
            "Failed to remove benchmark private telemetry shards"
        ) from exc
    if telemetry_dir.exists() or telemetry_dir.is_symlink():
        raise _WorkerCleanupFailure(
            "Benchmark private telemetry shards remain after cleanup"
        )
    return records


def _build_execution_policy(
    context: WorkerContext,
    *,
    budget: InvocationBudget,
) -> ModelExecutionPolicy:
    if not any(
        str(os.environ.get(name) or "").strip()
        for name in _PROVIDER_AUTH_ENVIRONMENT_KEYS
    ):
        raise ModelExecutionPolicyViolation(
            "Live sprint benchmark requires provider-only authentication through "
            "CODEX_API_KEY or OPENAI_API_KEY; operator Codex home credentials are isolated."
        )
    safe_path = _sanitized_benchmark_path(context)
    codex_executable = _resolve_benchmark_codex_executable(
        context,
        search_path=safe_path,
    )
    return ModelExecutionPolicy.for_benchmark(
        allowed_workspace_root=context.workspace_root,
        invocation_budget=budget,
        call_timeout_seconds=context.call_timeout_seconds,
        codex_executable=codex_executable,
        telemetry_output_dir=_private_telemetry_dir(context),
        shell_environment={
            "LANG": "C",
            "LC_ALL": "C",
            "PATH": safe_path,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(_source_import_root()),
            "PYTHONUNBUFFERED": "1",
        },
    )


def _build_services(
    context: WorkerContext,
    *,
    policy: ModelExecutionPolicy,
) -> dict[str, _BenchmarkTeamService]:
    history_state = _BenchmarkHistorySeedState()
    services = {
        role: _BenchmarkTeamService(
            context.workspace_root,
            role,
            enable_discord_client=False,
            relay_transport="internal",
            model_execution_policy=policy,
            allow_external_research=False,
            benchmark_context=context,
            benchmark_history_state=history_state,
        )
        for role in TEAM_ROLES
    }
    configured_models = {
        str(config.model or "").strip()
        for service in services.values()
        for config in (
            *service.runtime_config.role_defaults.values(),
            *service.runtime_config.internal_agent_defaults.values(),
        )
    }
    unsupported_models = sorted(
        model for model in configured_models if not model or "gemini" in model.lower()
    )
    if unsupported_models:
        raise ModelExecutionPolicyViolation(
            "Live sprint benchmark requires Codex-compatible role models"
        )
    return services


async def _relay_pump(
    services: Mapping[str, _BenchmarkTeamService],
    *,
    worker_log: _WorkerLog,
) -> None:
    worker_log.append("relay_pump_started", role_count=len(services))
    while True:
        for role in TEAM_ROLES:
            await services[role]._consume_internal_relay_once()
        await asyncio.sleep(_RELAY_POLL_SECONDS)


def _active_process_entries(
    budget_or_snapshot: InvocationBudget | Mapping[str, Any],
) -> list[dict[str, Any]]:
    snapshot = (
        budget_or_snapshot.snapshot()
        if isinstance(budget_or_snapshot, InvocationBudget)
        else budget_or_snapshot
    )
    return [
        dict(entry)
        for entry in (snapshot.get("entries") or [])
        if isinstance(entry, dict)
        and str(entry.get("state") or "") == "running"
        and isinstance(entry.get("pid"), int)
    ]


def _provider_entry_key(entry: Mapping[str, Any]) -> tuple[int, int | None]:
    raw_pid = entry.get("pid")
    raw_process_group_id = entry.get("process_group_id")
    pid = int(raw_pid) if isinstance(raw_pid, int) and not isinstance(raw_pid, bool) else 0
    process_group_id = (
        int(raw_process_group_id)
        if isinstance(raw_process_group_id, int)
        and not isinstance(raw_process_group_id, bool)
        else None
    )
    return pid, process_group_id


def _merge_active_process_entries(
    *snapshots: Mapping[str, Any],
) -> list[dict[str, Any]]:
    merged: dict[tuple[int, int | None], dict[str, Any]] = {}
    for snapshot in snapshots:
        for entry in _active_process_entries(snapshot):
            key = _provider_entry_key(entry)
            if key[0] > 1:
                merged[key] = entry
    return list(merged.values())


def _merge_launched_process_entries(
    *snapshots: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Retain only unresolved provider groups for parent cleanup."""

    merged: dict[tuple[int, int | None], dict[str, Any]] = {}
    for snapshot in snapshots:
        for raw_entry in (snapshot.get("entries") or []):
            if not isinstance(raw_entry, dict):
                continue
            entry = dict(raw_entry)
            if str(entry.get("state") or "").strip() not in {"reserved", "running"}:
                continue
            key = _provider_entry_key(entry)
            if key[0] > 1:
                merged[key] = entry
    return list(merged.values())


def _journal_non_negative_int(value: Any, *, default: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return value if value >= 0 else default


def _journal_optional_non_negative_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if value >= 0 else None


def _journal_context_matches(
    entry: Mapping[str, Any],
    record: Mapping[str, Any],
) -> bool:
    if record.get("prompt_context_representation_conflict") is True:
        return False
    for field_name in _JOURNAL_CONTEXT_STRING_FIELDS:
        if str(entry.get(field_name) or "").strip() != str(
            record.get(field_name) or ""
        ).strip():
            return False
    for field_name in _JOURNAL_CONTEXT_INTEGER_FIELDS:
        if _journal_optional_non_negative_int(
            entry.get(field_name)
        ) != _journal_optional_non_negative_int(record.get(field_name)):
            return False
    journal_enabled = entry.get("prompt_context_enabled")
    telemetry_enabled = record.get("prompt_context_enabled")
    if journal_enabled is not telemetry_enabled:
        return False
    expected_status = (
        "completed"
        if str(entry.get("state") or "").strip() == "completed"
        else "failed"
    )
    return str(record.get("status") or "").strip() == expected_status


def _summarize_call_journal(
    snapshot: Mapping[str, Any],
    *,
    telemetry_records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    raw_entries = snapshot.get("entries")
    raw_entry_list = list(raw_entries) if isinstance(raw_entries, list) else []
    entries = [dict(entry) for entry in raw_entry_list if isinstance(entry, dict)]
    malformed_entry_count = len(raw_entry_list) - len(entries)
    state_counts = {state: 0 for state in _CALL_JOURNAL_STATES}
    unknown_count = 0
    for entry in entries:
        state = str(entry.get("state") or "").strip()
        if state in state_counts:
            state_counts[state] += 1
        else:
            unknown_count += 1

    reserved_count = _journal_non_negative_int(snapshot.get("reserved_count"))
    entry_count = len(raw_entry_list)
    telemetry_record_list = tuple(
        sanitize_invocation_record(record)
        for record in telemetry_records
    )
    observed_count = len(telemetry_record_list)
    journal_invocation_ids = [
        str(entry.get("invocation_id") or "").strip()
        for entry in entries
    ]
    telemetry_invocation_ids = [
        str(record.get("invocation_id") or "").strip()
        for record in telemetry_record_list
    ]
    journal_nonempty_ids = [
        invocation_id
        for invocation_id in journal_invocation_ids
        if invocation_id
    ]
    telemetry_nonempty_ids = [
        invocation_id
        for invocation_id in telemetry_invocation_ids
        if invocation_id
    ]
    journal_missing_id_count = (
        len(journal_invocation_ids) - len(journal_nonempty_ids)
    )
    telemetry_missing_id_count = (
        len(telemetry_invocation_ids) - len(telemetry_nonempty_ids)
    )
    journal_duplicate_id_count = (
        len(journal_nonempty_ids) - len(set(journal_nonempty_ids))
    )
    telemetry_duplicate_id_count = (
        len(telemetry_nonempty_ids) - len(set(telemetry_nonempty_ids))
    )
    journal_id_set = set(journal_nonempty_ids)
    telemetry_id_set = set(telemetry_nonempty_ids)
    telemetry_unmatched_id_count = len(
        telemetry_id_set - journal_id_set
    )
    journal_unobserved_id_count = len(
        journal_id_set - telemetry_id_set
    )
    identity_reconciled = (
        journal_missing_id_count == 0
        and telemetry_missing_id_count == 0
        and journal_duplicate_id_count == 0
        and telemetry_duplicate_id_count == 0
        and telemetry_unmatched_id_count == 0
    )
    journal_entries_by_invocation_id = {
        str(entry.get("invocation_id") or "").strip(): entry
        for entry in entries
        if str(entry.get("invocation_id") or "").strip()
    }
    context_mismatch_count = 0
    verified_target_invocation_ids: list[str] = []
    for record in telemetry_record_list:
        invocation_id = str(record.get("invocation_id") or "").strip()
        entry = journal_entries_by_invocation_id.get(invocation_id)
        if entry is None:
            continue
        if not _journal_context_matches(entry, record):
            context_mismatch_count += 1
            continue
        if is_v2_target_projection(
            entry,
            state_field="state",
        ) and is_v2_target_projection(
            record,
            state_field="status",
        ):
            verified_target_invocation_ids.append(invocation_id)
    journal_schema_version = _journal_non_negative_int(
        snapshot.get("schema_version")
    )
    context_reconciled = (
        journal_schema_version == 3
        and identity_reconciled
        and context_mismatch_count == 0
    )
    unaccounted_count = max(reserved_count - entry_count, 0)
    overaccounted_count = max(entry_count - reserved_count, 0)
    return {
        "schema_version": 1,
        "journal_available": bool(snapshot),
        "journal_schema_version": _journal_non_negative_int(
            snapshot.get("schema_version")
        ),
        "max_invocations": _journal_non_negative_int(
            snapshot.get("max_invocations")
        ),
        "reserved_count": reserved_count,
        "entry_count": entry_count,
        "telemetry_record_count": observed_count,
        "unobserved_attempt_count": max(reserved_count - observed_count, 0),
        "telemetry_overage_count": max(observed_count - reserved_count, 0),
        "telemetry_coverage_percent": (
            round(min(observed_count * 100 / reserved_count, 100.0), 2)
            if reserved_count
            else 0.0
        ),
        "identity_reconciled": identity_reconciled,
        "context_reconciled": context_reconciled,
        "journal_telemetry_context_mismatch_count": context_mismatch_count,
        "verified_target_projection_count": len(verified_target_invocation_ids),
        "verified_target_invocation_ids_sha256": invocation_identity_digest(
            verified_target_invocation_ids
        ),
        "journal_invocation_ids_sha256": invocation_identity_digest(
            journal_invocation_ids
        ),
        "journal_invocation_id_missing_count": journal_missing_id_count,
        "journal_invocation_id_duplicate_count": journal_duplicate_id_count,
        "telemetry_invocation_id_missing_count": telemetry_missing_id_count,
        "telemetry_invocation_id_duplicate_count": telemetry_duplicate_id_count,
        "telemetry_invocation_id_unmatched_count": (
            telemetry_unmatched_id_count
        ),
        "journal_invocation_id_unobserved_count": (
            journal_unobserved_id_count
        ),
        "completed_count": state_counts["completed"],
        "failed_count": state_counts["failed"],
        "timeout_count": state_counts["timeout"],
        "launch_failed_count": state_counts["launch_failed"],
        "terminated_count": state_counts["terminated"],
        "active_count": state_counts["reserved"] + state_counts["running"],
        "unknown_state_count": unknown_count,
        "malformed_entry_count": malformed_entry_count,
        "unaccounted_count": unaccounted_count,
        "overaccounted_count": overaccounted_count,
        "reconciled": (
            reserved_count == entry_count
            and malformed_entry_count == 0
            and unknown_count == 0
        ),
        "rejected_count": _journal_non_negative_int(
            snapshot.get("rejected_count")
        ),
        "remaining_budget": _journal_non_negative_int(snapshot.get("remaining")),
    }


def _finalize_call_journal_after_cleanup(
    journal_path: Path,
    snapshot: Mapping[str, Any],
    *,
    stop_reason: str,
) -> dict[str, Any]:
    if not snapshot:
        return {}
    normalized = dict(snapshot)
    entries: list[dict[str, Any]] = []
    changed = False
    completed_at = _utc_now_iso()
    for raw_entry in (snapshot.get("entries") or []):
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        if str(entry.get("state") or "").strip() in {"reserved", "running"}:
            entry.update(
                {
                    "state": "terminated",
                    "completed_at": completed_at,
                    "exit_code": None,
                    "stop_reason": str(stop_reason or "worker_cleanup").strip(),
                }
            )
            changed = True
        entries.append(entry)
    normalized["entries"] = entries
    normalized["reserved_count"] = max(
        _journal_non_negative_int(snapshot.get("reserved_count")),
        len(entries),
    )
    if changed:
        normalized["schema_version"] = _journal_non_negative_int(
            snapshot.get("schema_version"),
            default=1,
        )
        try:
            _write_private_json(journal_path, normalized)
        except (OSError, TypeError, ValueError) as exc:
            raise _WorkerCleanupFailure(
                "Failed to finalize benchmark call journal after cleanup"
            ) from exc
    return normalized


def _process_exists(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _process_group_exists(process_group_id: int) -> bool:
    if process_group_id <= 1 or not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _provider_entry_alive(entry: Mapping[str, Any]) -> bool:
    pid, process_group_id = _provider_entry_key(entry)
    if process_group_id is not None and hasattr(os, "killpg"):
        return _process_group_exists(process_group_id)
    return _process_exists(pid)


def _signal_provider_entries(
    entries: list[dict[str, Any]],
    process_signal: signal.Signals,
) -> int:
    signaled = 0
    own_process_group = os.getpgrp() if hasattr(os, "getpgrp") else None
    for entry in entries:
        pid, process_group_id = _provider_entry_key(entry)
        if pid <= 1:
            continue
        if process_group_id is not None and hasattr(os, "killpg"):
            if process_group_id == own_process_group:
                continue
            try:
                os.killpg(process_group_id, process_signal)
            except ProcessLookupError:
                continue
            except OSError:
                # Cleanup confirmation will fail closed if the group survives.
                continue
            signaled += 1
            continue
        try:
            os.kill(pid, process_signal)
        except ProcessLookupError:
            continue
        except OSError:
            # Continue so one inaccessible process cannot hide later entries.
            continue
        signaled += 1
    return signaled


def _signal_active_provider_processes(
    budget_or_snapshot: InvocationBudget | Mapping[str, Any],
    process_signal: signal.Signals,
) -> int:
    return _signal_provider_entries(
        _active_process_entries(budget_or_snapshot),
        process_signal,
    )


async def _terminate_active_provider_processes(
    budget: InvocationBudget,
    *,
    grace_seconds: float,
    worker_log: _WorkerLog,
) -> None:
    terminated = _signal_active_provider_processes(budget, signal.SIGTERM)
    worker_log.append("provider_termination_requested", process_count=terminated)
    if not terminated:
        return
    await asyncio.sleep(max(min(grace_seconds, 5.0), 0.0))
    killed = _signal_active_provider_processes(budget, signal.SIGKILL)
    if killed:
        worker_log.append("provider_kill_requested", process_count=killed)


def _latest_sprint_state(workspace_root: Path) -> dict[str, Any]:
    paths = RuntimePaths.from_root(workspace_root)
    states = [
        dict(state)
        for state in iter_sprint_states(paths)
        if isinstance(state, dict) and str(state.get("sprint_id") or "").strip()
    ]
    if not states:
        return {}
    return max(
        states,
        key=lambda state: (
            str(state.get("started_at") or ""),
            str(state.get("sprint_id") or ""),
        ),
    )


def _sprint_evidence(sprint_state: Mapping[str, Any]) -> SprintEvidence:
    todos = [
        dict(todo)
        for todo in (sprint_state.get("todos") or [])
        if isinstance(todo, dict)
    ]
    statuses = [
        str(todo.get("status") or "").strip().lower()
        for todo in todos
    ]
    return SprintEvidence(
        sprint_id=str(sprint_state.get("sprint_id") or "").strip(),
        status=str(sprint_state.get("status") or "").strip(),
        closeout_status=str(sprint_state.get("closeout_status") or "").strip(),
        todo_count=len(todos),
        completed_todo_count=sum(
            status in _COMPLETED_TODO_STATUSES for status in statuses
        ),
        blocked_todo_count=statuses.count("blocked"),
        failed_todo_count=statuses.count("failed"),
        commit_sha=str(
            sprint_state.get("commit_sha")
            or sprint_state.get("version_control_sha")
            or sprint_state.get("auto_commit_sha")
            or ""
        ).strip(),
    )


def _quality_evidence(
    sprint_state: Mapping[str, Any],
    sprint: SprintEvidence,
) -> QualityEvidence:
    notes: list[str] = []
    if not sprint.sprint_id:
        notes.append("sprint_state_missing")
    if sprint.status != "completed":
        notes.append("sprint_not_completed")
    if sprint.closeout_status != "verified":
        notes.append("closeout_not_verified")
    if any(
        str(todo.get("status") or "").strip().lower() == "uncommitted"
        for todo in (sprint_state.get("todos") or [])
        if isinstance(todo, dict)
    ):
        notes.append("uncommitted_todo_present")
    return QualityEvidence(
        sprint_terminal=sprint.status == "completed",
        closeout_verified=sprint.closeout_status == "verified",
        blocked_todo_count=sprint.blocked_todo_count,
        failed_todo_count=sprint.failed_todo_count,
        notes=tuple(notes),
    )


def _confirmation_is_pending(sprint_state: Mapping[str, Any]) -> bool:
    confirmation = sprint_state.get("initial_plan_confirmation")
    return (
        isinstance(confirmation, dict)
        and str(confirmation.get("status") or "").strip().lower() == "pending"
    )


def _confirm_initial_plan(
    orchestrator: _BenchmarkTeamService,
    sprint_state: dict[str, Any],
    *,
    worker_log: _WorkerLog,
) -> None:
    confirmation = apply_initial_plan_confirmation(
        sprint_state,
        confirmed_by={
            "type": "benchmark_harness",
            "author_id": "benchmark-harness",
            "author_name": "benchmark-harness",
        },
        message_id="benchmark-auto-confirm",
        parser_reason="isolated benchmark auto-confirm policy",
        parser_confidence="high",
        confirmed_at=_utc_now_iso(),
    )
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    orchestrator._save_sprint_state(sprint_state)
    orchestrator._append_sprint_event(
        sprint_id,
        event_type="initial_plan_confirmed",
        summary="Benchmark harness auto-confirmed the initial implementation plan.",
        payload={
            "revision": int(confirmation.get("revision") or 0),
            "confirmation_source": "benchmark_harness",
        },
    )
    worker_log.append(
        "initial_plan_auto_confirmed",
        revision=int(confirmation.get("revision") or 0),
    )


async def _drive_sprint(
    context: WorkerContext,
    orchestrator: _BenchmarkTeamService,
    *,
    worker_log: _WorkerLog,
) -> None:
    await orchestrator.start_sprint_lifecycle(
        context.milestone,
        trigger="benchmark",
        resume_mode="await",
        kickoff_brief=(
            "Execute the deterministic local benchmark task. Use only files in the "
            "workspace, preserve protected benchmark inputs, run the stated unittest "
            "command, and commit the implementation."
        ),
        kickoff_requirements=[context.milestone],
        kickoff_request_text=context.milestone,
        kickoff_reference_artifacts=[
            "./BENCHMARK_TASK.md",
            "./.benchmark/scenario.json",
            "./tests/test_benchmark_app.py",
        ],
        kickoff_requester_route={
            "type": "benchmark_harness",
            "author_id": "benchmark-harness",
            "author_name": "benchmark-harness",
        },
    )
    sprint_state = orchestrator._load_active_sprint_state()
    if not sprint_state:
        raise _InitialPlanNotReady("Sprint state was not created")

    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    worker_log.append("sprint_created")
    confirmation_performed = False
    for _resume_pass in range(_MAX_RESUME_PASSES):
        sprint_state = orchestrator._load_sprint_state(sprint_id)
        status = str(sprint_state.get("status") or "").strip().lower()
        if status in _TERMINAL_SPRINT_STATUSES:
            worker_log.append(
                "sprint_terminal",
                closeout_status=str(sprint_state.get("closeout_status") or ""),
                status=status,
            )
            return
        if _confirmation_is_pending(sprint_state):
            if confirmation_performed:
                raise _InitialPlanNotReady(
                    "Initial implementation plan returned to pending state"
                )
            _confirm_initial_plan(
                orchestrator,
                sprint_state,
                worker_log=worker_log,
            )
            confirmation_performed = True
        elif not confirmation_performed:
            raise _InitialPlanNotReady(
                "Initial implementation plan did not reach pending confirmation"
            )
        await orchestrator._resume_active_sprint(sprint_id)

    raise _SprintDidNotTerminate(
        f"Sprint did not terminate after {_MAX_RESUME_PASSES} resume passes"
    )


async def _execute_live_arm(
    context: WorkerContext,
    *,
    budget: InvocationBudget,
    policy: ModelExecutionPolicy,
    worker_log: _WorkerLog,
) -> None:
    services = _build_services(context, policy=policy)
    worker_log.append(
        "services_ready",
        discord="disabled",
        external_research="disabled",
        relay="internal",
        role_count=len(services),
    )
    pump_task = asyncio.create_task(
        _relay_pump(services, worker_log=worker_log),
        name=f"benchmark-relay-{context.arm.run_id}",
    )
    sprint_task = asyncio.create_task(
        _drive_sprint(
            context,
            services["orchestrator"],
            worker_log=worker_log,
        ),
        name=f"benchmark-sprint-{context.arm.run_id}",
    )
    try:
        await asyncio.wait_for(
            sprint_task,
            timeout=context.run_timeout_seconds,
        )
    except ModelInvocationTimeout:
        raise
    except TimeoutError as exc:
        await _terminate_active_provider_processes(
            budget,
            grace_seconds=policy.kill_grace_seconds,
            worker_log=worker_log,
        )
        raise _BenchmarkRunTimeout(
            f"Sprint arm exceeded {context.run_timeout_seconds:g} seconds"
        ) from exc
    finally:
        if not sprint_task.done():
            sprint_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sprint_task
        pump_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pump_task
        for service in services.values():
            with contextlib.suppress(Exception):
                await service.discord_client.close()


def _classify_status(
    *,
    forced_status: str,
    sprint: SprintEvidence,
    budget_snapshot: Mapping[str, Any],
) -> tuple[str, str, str]:
    if int(budget_snapshot.get("rejected_count") or 0) > 0:
        return (
            "call_budget_exhausted",
            "invocation_budget_exhausted",
            "invocation_budget_exceeded",
        )
    entries = [
        entry
        for entry in (budget_snapshot.get("entries") or [])
        if isinstance(entry, dict)
    ]
    if forced_status == "timeout" or any(
        str(entry.get("state") or "") == "timeout" for entry in entries
    ):
        return ("timeout", "timeout", "model_or_run_timeout")
    if forced_status:
        return (
            forced_status,
            "worker_exception",
            "worker_exception",
        )
    if sprint.status == "completed":
        return ("completed", "sprint_completed", "")
    return (
        "failed",
        "sprint_not_completed",
        "sprint_not_completed",
    )


def _preflight_failure_outcome(
    *,
    started_at: str,
    started_monotonic: float,
    worker_log: _WorkerLog,
    exc: BaseException,
) -> WorkerOutcome:
    worker_log.append("preflight_failed", error_category=type(exc).__name__)
    duration_ms = max(int((time.monotonic() - started_monotonic) * 1000), 0)
    return WorkerOutcome(
        status="preflight_failed",
        started_at=started_at,
        ended_at=_utc_now_iso(),
        wall_duration_ms=duration_ms,
        worker_duration_ms=duration_ms,
        stop_reason="preflight_failed",
        error_category=type(exc).__name__,
    )


def _run_live_sprint_arm_in_child(context: WorkerContext) -> WorkerOutcome:
    started_at = _utc_now_iso()
    started_monotonic = time.monotonic()
    run_output_dir = context.run_output_dir.expanduser().resolve()
    worker_log = _WorkerLog(run_output_dir / "worker.log")
    worker_log.append("child_worker_started", run_id=context.arm.run_id)
    try:
        _validate_context(context)
    except Exception as exc:
        return _preflight_failure_outcome(
            started_at=started_at,
            started_monotonic=started_monotonic,
            worker_log=worker_log,
            exc=exc,
        )

    budget = InvocationBudget(
        context.max_invocations,
        journal_path=run_output_dir / "call_journal.json",
    )
    try:
        policy = _build_execution_policy(context, budget=budget)
    except Exception as exc:
        return _preflight_failure_outcome(
            started_at=started_at,
            started_monotonic=started_monotonic,
            worker_log=worker_log,
            exc=exc,
        )

    forced_status = ""
    forced_error_category = ""
    try:
        asyncio.run(
            _execute_live_arm(
                context,
                budget=budget,
                policy=policy,
                worker_log=worker_log,
            )
        )
    except _BenchmarkRunTimeout:
        forced_status = "timeout"
        forced_error_category = "run_timeout"
    except ModelInvocationTimeout:
        forced_status = "timeout"
        forced_error_category = "model_invocation_timeout"
    except InvocationBudgetExceeded:
        forced_status = "call_budget_exhausted"
        forced_error_category = "invocation_budget_exceeded"
    except ModelExecutionPolicyViolation:
        forced_status = "preflight_failed"
        forced_error_category = "execution_policy_violation"
    except Exception as exc:
        forced_status = "failed"
        forced_error_category = type(exc).__name__

    sprint_state = _latest_sprint_state(context.workspace_root)
    sprint = _sprint_evidence(sprint_state)
    quality = _quality_evidence(sprint_state, sprint)
    budget_snapshot = budget.snapshot()
    status, stop_reason, classified_error = _classify_status(
        forced_status=forced_status,
        sprint=sprint,
        budget_snapshot=budget_snapshot,
    )
    error_category = forced_error_category or classified_error
    telemetry_records = _load_private_telemetry(context)
    duration_ms = max(int((time.monotonic() - started_monotonic) * 1000), 0)
    worker_log.append(
        "worker_finished",
        error_category=error_category or "none",
        invocation_count=len(telemetry_records),
        status=status,
    )
    return WorkerOutcome(
        status=status,  # type: ignore[arg-type]
        sprint=sprint,
        quality=quality,
        telemetry_records=telemetry_records,
        invocation_attempts=_summarize_call_journal(
            budget_snapshot,
            telemetry_records=telemetry_records,
        ),
        started_at=started_at,
        ended_at=_utc_now_iso(),
        wall_duration_ms=duration_ms,
        worker_duration_ms=duration_ms,
        stop_reason=stop_reason,
        error_category=error_category,
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary_path = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary_path.unlink()


def _read_json_mapping(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def _read_call_journal_strict(
    path: Path,
    *,
    required: bool = False,
) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise _WorkerCleanupFailure(
                "Required benchmark call journal is missing"
            )
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        raise _WorkerCleanupFailure(
            "Existing benchmark call journal is unreadable or malformed"
        ) from exc
    if not isinstance(payload, dict):
        raise _WorkerCleanupFailure("Benchmark call journal must be a JSON object")
    snapshot = dict(payload)
    journal_schema_version = _journal_non_negative_int(
        snapshot.get("schema_version")
    )
    if journal_schema_version not in {1, 2, 3}:
        raise _WorkerCleanupFailure("Benchmark call journal schema is invalid")
    entries = snapshot.get("entries")
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) for entry in entries
    ):
        raise _WorkerCleanupFailure("Benchmark call journal entries are invalid")
    for field_name in (
        "max_invocations",
        "reserved_count",
        "remaining",
        "rejected_count",
    ):
        raw_value = snapshot.get(field_name)
        if (
            raw_value is None
            or isinstance(raw_value, bool)
            or not isinstance(raw_value, int)
            or raw_value < 0
        ):
            raise _WorkerCleanupFailure(
                f"Benchmark call journal {field_name} is invalid"
            )
    max_invocations = int(snapshot["max_invocations"])
    reserved_count = int(snapshot["reserved_count"])
    if (
        max_invocations <= 0
        or reserved_count != len(entries)
        or reserved_count > max_invocations
        or int(snapshot["remaining"]) != max(max_invocations - reserved_count, 0)
    ):
        raise _WorkerCleanupFailure("Benchmark call journal counts do not reconcile")
    reservation_ids: set[str] = set()
    for entry in entries:
        reservation_id = str(entry.get("reservation_id") or "").strip()
        if not reservation_id or reservation_id in reservation_ids:
            raise _WorkerCleanupFailure(
                "Benchmark call journal reservation ids are missing or duplicated"
            )
        reservation_ids.add(reservation_id)
        state = str(entry.get("state") or "").strip()
        if state not in _CALL_JOURNAL_STATES:
            raise _WorkerCleanupFailure(
                "Benchmark call journal contains an invalid invocation state"
            )
        if state == "running":
            pid = entry.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
                raise _WorkerCleanupFailure(
                    "Running benchmark journal entry has an invalid process id"
                )
            process_group_id = entry.get("process_group_id")
            if (
                not isinstance(process_group_id, int)
                or isinstance(process_group_id, bool)
                or process_group_id <= 1
                or process_group_id != pid
            ):
                raise _WorkerCleanupFailure(
                    "Running benchmark journal entry has an invalid process group id"
                )
        elif entry.get("process_group_id") is not None:
            process_group_id = entry.get("process_group_id")
            if (
                not isinstance(process_group_id, int)
                or isinstance(process_group_id, bool)
                or process_group_id <= 1
            ):
                raise _WorkerCleanupFailure(
                    "Benchmark journal entry has an invalid process group id"
                )
        if journal_schema_version == 3:
            if not all(field_name in entry for field_name in _JOURNAL_CONTEXT_FIELDS):
                raise _WorkerCleanupFailure(
                    "Benchmark call journal context fields are incomplete"
                )
            prompt_context_enabled = entry.get("prompt_context_enabled")
            if prompt_context_enabled is not None and not isinstance(
                prompt_context_enabled,
                bool,
            ):
                raise _WorkerCleanupFailure(
                    "Benchmark call journal prompt context flag is invalid"
                )
            for field_name in _JOURNAL_CONTEXT_INTEGER_FIELDS:
                raw_value = entry.get(field_name)
                if raw_value is not None and (
                    isinstance(raw_value, bool)
                    or not isinstance(raw_value, int)
                    or raw_value < 0
                ):
                    raise _WorkerCleanupFailure(
                        "Benchmark call journal context count is invalid"
                    )
            for field_name in _JOURNAL_CONTEXT_STRING_FIELDS:
                if not isinstance(entry.get(field_name), str):
                    raise _WorkerCleanupFailure(
                        "Benchmark call journal context identity is invalid"
                    )
    return snapshot


def _manifest_payload(
    context: WorkerContext,
    *,
    result_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "benchmark_id": context.benchmark_id,
        "arm": {
            "pair_index": context.arm.pair_index,
            "order_index": context.arm.order_index,
            "variant": context.arm.variant,
            "run_id": context.arm.run_id,
            "prompt_context_enabled": context.arm.prompt_context_enabled,
        },
        "workspace_root": str(context.workspace_root.expanduser().resolve()),
        "run_output_dir": str(context.run_output_dir.expanduser().resolve()),
        "result_path": str(result_path),
        "controls": {
            "max_invocations": context.max_invocations,
            "call_timeout_seconds": context.call_timeout_seconds,
            "run_timeout_seconds": context.run_timeout_seconds,
            "live": context.live,
        },
        "fixture_evidence": {
            "milestone_sha256": _sha256_text(context.milestone),
            "history_sha256": canonical_hash(context.history_seed),
            "history_event_count": len(context.history_seed),
        },
    }


def _child_context_from_manifest(payload: Mapping[str, Any]) -> tuple[WorkerContext, Path]:
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError("Unsupported benchmark worker manifest schema")
    workspace_root = Path(str(payload.get("workspace_root") or "")).expanduser().resolve()
    run_output_dir = Path(str(payload.get("run_output_dir") or "")).expanduser().resolve()
    result_path = Path(str(payload.get("result_path") or "")).expanduser().resolve()
    if result_path.parent != run_output_dir:
        raise ValueError("Child result path must be inside the run output directory")

    scenario_payload = _read_json_mapping(workspace_root / ".benchmark" / "scenario.json")
    milestone = str(scenario_payload.get("milestone") or "").strip()
    try:
        raw_history = json.loads(
            (workspace_root / ".benchmark" / "history_seed.json").read_text(
                encoding="utf-8"
            )
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        raise ValueError("Unable to load benchmark history fixture") from exc
    if not isinstance(raw_history, list) or not all(
        isinstance(item, dict) for item in raw_history
    ):
        raise ValueError("Benchmark history fixture must be a JSON object array")
    history_seed = tuple(dict(item) for item in raw_history)

    expected_fixture = (
        dict(payload.get("fixture_evidence") or {})
        if isinstance(payload.get("fixture_evidence"), dict)
        else {}
    )
    if _sha256_text(milestone) != str(
        expected_fixture.get("milestone_sha256") or ""
    ):
        raise ValueError("Benchmark milestone fixture hash differs from the parent context")
    if canonical_hash(history_seed) != str(
        expected_fixture.get("history_sha256") or ""
    ):
        raise ValueError("Benchmark history fixture hash differs from the parent context")
    if len(history_seed) != int(
        expected_fixture.get("history_event_count") or 0
    ):
        raise ValueError("Benchmark history fixture count differs from the parent context")

    raw_arm = (
        dict(payload.get("arm") or {})
        if isinstance(payload.get("arm"), dict)
        else {}
    )
    variant = str(raw_arm.get("variant") or "")
    if variant not in {"before", "after"}:
        raise ValueError("Benchmark worker manifest has an invalid arm variant")
    arm = ArmPlan(
        pair_index=int(raw_arm.get("pair_index") or 0),
        order_index=int(raw_arm.get("order_index") or 0),
        variant=variant,  # type: ignore[arg-type]
        run_id=str(raw_arm.get("run_id") or "").strip(),
        prompt_context_enabled=bool(raw_arm.get("prompt_context_enabled")),
    )
    controls = (
        dict(payload.get("controls") or {})
        if isinstance(payload.get("controls"), dict)
        else {}
    )
    return (
        WorkerContext(
            benchmark_id=str(payload.get("benchmark_id") or "").strip(),
            arm=arm,
            workspace_root=workspace_root,
            run_output_dir=run_output_dir,
            milestone=milestone,
            history_seed=history_seed,
            max_invocations=int(controls.get("max_invocations") or 0),
            call_timeout_seconds=float(controls.get("call_timeout_seconds") or 0),
            run_timeout_seconds=float(controls.get("run_timeout_seconds") or 0),
            live=bool(controls.get("live")),
        ),
        result_path,
    )


def _worker_outcome_payload(outcome: WorkerOutcome) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": outcome.status,
        "sprint": outcome.sprint.to_dict(),
        "quality": outcome.quality.to_dict(),
        # The parent reloads telemetry from its private directory after cleanup.
        "telemetry_records": [],
        "invocation_attempts": sanitize_invocation_attempts(
            outcome.invocation_attempts
        ),
        "started_at": outcome.started_at,
        "ended_at": outcome.ended_at,
        "wall_duration_ms": outcome.wall_duration_ms,
        "worker_duration_ms": outcome.worker_duration_ms,
        "stop_reason": outcome.stop_reason,
        "error_category": outcome.error_category,
    }


def _worker_outcome_from_payload(payload: Mapping[str, Any]) -> WorkerOutcome:
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError("Unsupported benchmark worker result schema")
    status = str(payload.get("status") or "")
    if status not in {
        "completed",
        "failed",
        "timeout",
        "call_budget_exhausted",
        "preflight_failed",
    }:
        raise ValueError("Benchmark worker result has an invalid status")
    raw_sprint = (
        dict(payload.get("sprint") or {})
        if isinstance(payload.get("sprint"), dict)
        else {}
    )
    sprint = SprintEvidence(
        sprint_id=str(raw_sprint.get("sprint_id") or ""),
        status=str(raw_sprint.get("status") or ""),
        closeout_status=str(raw_sprint.get("closeout_status") or ""),
        todo_count=int(raw_sprint.get("todo_count") or 0),
        completed_todo_count=int(raw_sprint.get("completed_todo_count") or 0),
        blocked_todo_count=int(raw_sprint.get("blocked_todo_count") or 0),
        failed_todo_count=int(raw_sprint.get("failed_todo_count") or 0),
        commit_sha=str(raw_sprint.get("commit_sha") or ""),
    )
    raw_quality = (
        dict(payload.get("quality") or {})
        if isinstance(payload.get("quality"), dict)
        else {}
    )
    quality = QualityEvidence(
        behavior_oracle_passed=bool(raw_quality.get("behavior_oracle_passed")),
        sprint_terminal=bool(raw_quality.get("sprint_terminal")),
        closeout_verified=bool(raw_quality.get("closeout_verified")),
        protected_files_unchanged=bool(
            raw_quality.get("protected_files_unchanged")
        ),
        git_clean=bool(raw_quality.get("git_clean")),
        commit_created=bool(raw_quality.get("commit_created")),
        no_git_remotes=bool(raw_quality.get("no_git_remotes")),
        blocked_todo_count=int(raw_quality.get("blocked_todo_count") or 0),
        failed_todo_count=int(raw_quality.get("failed_todo_count") or 0),
        notes=tuple(
            str(note)
            for note in (raw_quality.get("notes") or [])
            if str(note).strip()
        ),
    )
    invocation_attempts = sanitize_invocation_attempts(
        payload.get("invocation_attempts")
    )
    return WorkerOutcome(
        status=status,  # type: ignore[arg-type]
        sprint=sprint,
        quality=quality,
        # Child result files are not an evidence channel. The parent replaces
        # this with records read from its private recorder directory.
        telemetry_records=(),
        invocation_attempts=invocation_attempts,
        started_at=str(payload.get("started_at") or ""),
        ended_at=str(payload.get("ended_at") or ""),
        wall_duration_ms=max(int(payload.get("wall_duration_ms") or 0), 0),
        worker_duration_ms=max(int(payload.get("worker_duration_ms") or 0), 0),
        stop_reason=str(payload.get("stop_reason") or ""),
        error_category=str(payload.get("error_category") or ""),
    )


def _child_environment(context: WorkerContext) -> dict[str, str]:
    environment = {
        key: value
        for key in _CHILD_ENVIRONMENT_KEYS
        if (value := os.environ.get(key)) is not None
    }
    environment["PATH"] = _sanitized_benchmark_path(context)
    environment["PYTHONPATH"] = str(_source_import_root())
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    environment["NO_COLOR"] = "1"
    environment[LIVE_BENCHMARK_ENV] = "1"
    return environment


def _signal_child_group(
    process: subprocess.Popen[Any],
    process_signal: signal.Signals,
) -> None:
    try:
        if hasattr(os, "killpg"):
            os.killpg(process.pid, process_signal)
        elif process.poll() is None:
            process.send_signal(process_signal)
    except OSError:
        pass


def _worker_group_alive(process: subprocess.Popen[Any]) -> bool:
    if hasattr(os, "killpg"):
        try:
            process.poll()
        except OSError:
            return True
        return _process_group_exists(process.pid)
    return process.poll() is None


def _append_cleanup_log(
    worker_log: _WorkerLog,
    event: str,
    **fields: Any,
) -> None:
    try:
        worker_log.append(event, **fields)
    except OSError:
        # Diagnostics must not prevent process termination.
        pass


def _wait_for_cleanup_confirmation(
    process: subprocess.Popen[Any],
    *,
    provider_entries: list[dict[str, Any]],
    worker_log: _WorkerLog,
    timeout_seconds: float,
) -> None:
    deadline = time.monotonic() + max(timeout_seconds, 0.0)
    while True:
        worker_alive = _worker_group_alive(process)
        surviving_providers = [
            entry for entry in provider_entries if _provider_entry_alive(entry)
        ]
        if not worker_alive and not surviving_providers:
            _append_cleanup_log(
                worker_log,
                "worker_cleanup_confirmed",
                provider_group_count=len(provider_entries),
            )
            return
        _signal_provider_entries(surviving_providers, signal.SIGKILL)
        if worker_alive:
            _signal_child_group(process, signal.SIGKILL)
        if time.monotonic() >= deadline:
            _append_cleanup_log(
                worker_log,
                "worker_cleanup_failed",
                provider_group_count=len(surviving_providers),
                worker_group_alive=worker_alive,
            )
            raise _WorkerCleanupFailure(
                "Timed out confirming benchmark worker and provider process termination"
            )
        time.sleep(0.05)


def _terminate_worker_child(
    process: subprocess.Popen[Any],
    *,
    journal_path: Path,
    worker_log: _WorkerLog,
    stop_reason: str = "worker_cleanup",
) -> dict[str, Any]:
    cleanup_failures: list[tuple[str, _WorkerCleanupFailure]] = []

    def defer_failure(stage: str, failure: _WorkerCleanupFailure) -> None:
        cleanup_failures.append((stage, failure))
        _append_cleanup_log(
            worker_log,
            "worker_cleanup_failure_deferred",
            stage=stage,
            error_category=type(failure).__name__,
        )

    def read_journal(stage: str) -> dict[str, Any]:
        try:
            snapshot = _read_call_journal_strict(
                journal_path,
                required=True,
            )
        except _WorkerCleanupFailure as exc:
            defer_failure(stage, exc)
            return {}
        if any(
            str(entry.get("state") or "").strip() == "reserved"
            for entry in (snapshot.get("entries") or [])
            if isinstance(entry, dict)
        ):
            defer_failure(
                f"{stage}_reserved_attempt",
                _WorkerCleanupFailure(
                    "Benchmark provider launch registration was incomplete"
                ),
            )
        return snapshot

    initial_snapshot = read_journal("initial_journal")
    provider_entries = _merge_launched_process_entries(initial_snapshot)
    provider_count = _signal_provider_entries(
        provider_entries,
        signal.SIGTERM,
    )
    _append_cleanup_log(
        worker_log,
        "parent_provider_termination_requested",
        process_count=provider_count,
    )
    _signal_child_group(process, signal.SIGTERM)
    term_timed_out = False
    try:
        process.wait(timeout=_CHILD_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        term_timed_out = True
    except OSError:
        term_timed_out = True
        defer_failure(
            "worker_term_wait",
            _WorkerCleanupFailure(
                "Unable to reap benchmark worker after SIGTERM"
            ),
        )

    # The worker may reserve and launch a provider between the first journal
    # read and delivery of SIGTERM. This read is required regardless of whether
    # the worker exited during its grace window.
    pre_kill_snapshot = read_journal("pre_kill_journal")
    provider_entries = _merge_launched_process_entries(
        initial_snapshot,
        pre_kill_snapshot,
    )
    _signal_provider_entries(provider_entries, signal.SIGKILL)
    _signal_child_group(process, signal.SIGKILL)
    if term_timed_out:
        try:
            process.wait(timeout=_CHILD_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            _append_cleanup_log(
                worker_log,
                "worker_process_reap_failed",
                provider_group_count=len(provider_entries),
            )
            defer_failure(
                "worker_kill_wait",
                _WorkerCleanupFailure(
                    "Benchmark worker did not exit after SIGKILL"
                ),
            )
        except OSError:
            defer_failure(
                "worker_kill_wait",
                _WorkerCleanupFailure(
                    "Unable to reap benchmark worker after SIGKILL"
                ),
            )

    # Once the worker has received SIGKILL it cannot intentionally launch
    # another call. A final read closes the remaining write/read race before
    # liveness verification.
    final_snapshot = read_journal("final_journal")
    provider_entries = _merge_launched_process_entries(
        initial_snapshot,
        pre_kill_snapshot,
        final_snapshot,
    )
    _signal_provider_entries(provider_entries, signal.SIGKILL)
    _signal_child_group(process, signal.SIGKILL)
    _wait_for_cleanup_confirmation(
        process,
        provider_entries=provider_entries,
        worker_log=worker_log,
        timeout_seconds=_CHILD_TERMINATION_GRACE_SECONDS,
    )

    finalized_snapshot: dict[str, Any] = {}
    if final_snapshot:
        try:
            finalized_snapshot = _finalize_call_journal_after_cleanup(
                journal_path,
                final_snapshot,
                stop_reason=stop_reason,
            )
        except _WorkerCleanupFailure as exc:
            defer_failure("journal_finalization", exc)

    if cleanup_failures:
        stages = ",".join(stage for stage, _failure in cleanup_failures)
        _append_cleanup_log(
            worker_log,
            "worker_cleanup_failed_closed",
            failure_count=len(cleanup_failures),
            stages=stages,
        )
        raise _WorkerCleanupFailure(
            f"Benchmark worker cleanup could not be verified: {stages}"
        ) from cleanup_failures[0][1]
    return finalized_snapshot


def _partial_outcome(
    context: WorkerContext,
    *,
    status: str,
    started_at: str,
    started_monotonic: float,
    stop_reason: str,
    error_category: str,
    telemetry_records: tuple[Mapping[str, Any], ...],
) -> WorkerOutcome:
    sprint_state = _latest_sprint_state(context.workspace_root)
    sprint = _sprint_evidence(sprint_state)
    quality = _quality_evidence(sprint_state, sprint)
    duration_ms = max(int((time.monotonic() - started_monotonic) * 1000), 0)
    journal_snapshot = _read_call_journal_strict(
        context.run_output_dir.expanduser().resolve() / "call_journal.json",
        required=True,
    )
    return WorkerOutcome(
        status=status,  # type: ignore[arg-type]
        sprint=sprint,
        quality=quality,
        telemetry_records=telemetry_records,
        invocation_attempts=_summarize_call_journal(
            journal_snapshot,
            telemetry_records=telemetry_records,
        ),
        started_at=started_at,
        ended_at=_utc_now_iso(),
        wall_duration_ms=duration_ms,
        worker_duration_ms=duration_ms,
        stop_reason=stop_reason,
        error_category=error_category,
    )


def run_live_sprint_arm(context: WorkerContext) -> WorkerOutcome:
    """Run one benchmark arm behind a hard child-process timeout boundary.

    The child executes the production sprint. The parent stores only bounded
    controls in its temporary manifest and recovers privacy-safe partial evidence
    if the child or one of its provider process groups must be terminated.
    """

    started_at = _utc_now_iso()
    started_monotonic = time.monotonic()
    run_output_dir = context.run_output_dir.expanduser().resolve()
    worker_log = _WorkerLog(run_output_dir / "worker.log", reset=True)
    worker_log.append("worker_parent_started", run_id=context.arm.run_id)
    try:
        _validate_context(context)
    except Exception as exc:
        return _preflight_failure_outcome(
            started_at=started_at,
            started_monotonic=started_monotonic,
            worker_log=worker_log,
            exc=exc,
        )

    nonce = uuid.uuid4().hex
    manifest_path = run_output_dir / f".worker-manifest-{nonce}.json"
    result_path = run_output_dir / f".worker-result-{nonce}.json"
    journal_path = run_output_dir / "call_journal.json"
    with contextlib.suppress(FileNotFoundError):
        journal_path.unlink()
    _initialize_private_telemetry(context)
    try:
        _write_private_json(
            journal_path,
            {
                "schema_version": 3,
                "max_invocations": context.max_invocations,
                "reserved_count": 0,
                "remaining": context.max_invocations,
                "rejected_count": 0,
                "entries": [],
            },
        )
    except (OSError, TypeError, ValueError) as exc:
        _consume_private_telemetry(context)
        raise _WorkerCleanupFailure(
            "Failed to initialize benchmark call journal"
        ) from exc
    try:
        _write_private_json(
            manifest_path,
            _manifest_payload(context, result_path=result_path),
        )
    except (OSError, TypeError, ValueError) as exc:
        _consume_private_telemetry(context)
        raise _WorkerCleanupFailure(
            "Failed to initialize benchmark worker manifest"
        ) from exc
    with contextlib.suppress(FileNotFoundError):
        result_path.unlink()
    command = (
        sys.executable,
        "-m",
        "teams_runtime.benchmarking.worker",
        "--child-manifest",
        str(manifest_path),
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=str(context.workspace_root.expanduser().resolve()),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_child_environment(context),
            start_new_session=True,
        )
    except Exception as exc:
        with contextlib.suppress(FileNotFoundError):
            manifest_path.unlink()
        worker_log.append("worker_child_launch_failed", error_category=type(exc).__name__)
        return _partial_outcome(
            context,
            status="preflight_failed",
            started_at=started_at,
            started_monotonic=started_monotonic,
            stop_reason="worker_child_launch_failed",
            error_category=type(exc).__name__,
            telemetry_records=_consume_private_telemetry(context),
        )

    timed_out = False
    final_journal_snapshot: dict[str, Any] = {}
    try:
        process.wait(timeout=context.run_timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        worker_log.append("worker_child_timeout")
        final_journal_snapshot = _terminate_worker_child(
            process,
            journal_path=journal_path,
            worker_log=worker_log,
            stop_reason="run_timeout_exceeded",
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            manifest_path.unlink()

    if not timed_out:
        # The child's own asyncio deadline can fire just before the parent's.
        # Verify cleanup even when process.wait() observed a normal child exit.
        final_journal_snapshot = _terminate_worker_child(
            process,
            journal_path=journal_path,
            worker_log=worker_log,
            stop_reason="worker_exit_cleanup",
        )

    try:
        quarantined_entries = quarantine_unsafe_workspace_entries(
            context.workspace_root,
        )
    except (ModelExecutionPolicyViolation, OSError, ValueError) as exc:
        raise _WorkerCleanupFailure(
            "Benchmark workspace integrity could not be verified after child cleanup"
        ) from exc
    if quarantined_entries:
        worker_log.append(
            "unsafe_workspace_entries_quarantined_by_parent",
            entry_count=len(quarantined_entries),
        )
        raise _WorkerCleanupFailure(
            "Benchmark child left unsafe filesystem entries after provider cleanup"
        )

    if timed_out:
        with contextlib.suppress(FileNotFoundError):
            result_path.unlink()
        outcome = _partial_outcome(
            context,
            status="timeout",
            started_at=started_at,
            started_monotonic=started_monotonic,
            stop_reason="run_timeout_exceeded",
            error_category="run_timeout",
            telemetry_records=_consume_private_telemetry(context),
        )
        worker_log.append(
            "worker_parent_finished",
            invocation_count=len(outcome.telemetry_records),
            status=outcome.status,
        )
        return outcome

    result_payload = _read_json_mapping(result_path)
    with contextlib.suppress(FileNotFoundError):
        result_path.unlink()
    telemetry_records = _consume_private_telemetry(context)
    if process.returncode != 0 or not result_payload:
        outcome = _partial_outcome(
            context,
            status="failed",
            started_at=started_at,
            started_monotonic=started_monotonic,
            stop_reason="worker_child_failed",
            error_category="worker_child_failed",
            telemetry_records=telemetry_records,
        )
    else:
        try:
            outcome = replace(
                _worker_outcome_from_payload(result_payload),
                telemetry_records=telemetry_records,
            )
        except (TypeError, ValueError):
            outcome = _partial_outcome(
                context,
                status="failed",
                started_at=started_at,
                started_monotonic=started_monotonic,
                stop_reason="worker_result_invalid",
                error_category="worker_result_invalid",
                telemetry_records=telemetry_records,
            )

    parent_duration_ms = max(
        int((time.monotonic() - started_monotonic) * 1000),
        0,
    )
    outcome = replace(
        outcome,
        started_at=started_at,
        ended_at=_utc_now_iso(),
        wall_duration_ms=parent_duration_ms,
        invocation_attempts=(
            _summarize_call_journal(
                final_journal_snapshot,
                telemetry_records=outcome.telemetry_records,
            )
            if final_journal_snapshot
            else outcome.invocation_attempts
        ),
    )
    worker_log.append(
        "worker_parent_finished",
        invocation_count=len(outcome.telemetry_records),
        status=outcome.status,
    )
    return outcome


def _child_main(manifest_path: Path) -> int:
    payload = _read_json_mapping(manifest_path.expanduser().resolve())
    if not payload:
        return 2
    try:
        context, result_path = _child_context_from_manifest(payload)
    except (TypeError, ValueError):
        return 2
    outcome = _run_live_sprint_arm_in_child(context)
    _write_private_json(result_path, _worker_outcome_payload(outcome))
    return 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--child-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    return _child_main(args.child_manifest)


__all__ = [
    "LIVE_BENCHMARK_ENV",
    "run_live_sprint_arm",
]


if __name__ == "__main__":
    raise SystemExit(_main())
