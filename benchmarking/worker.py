from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from teams_runtime.benchmarking.metrics import load_workspace_telemetry
from teams_runtime.benchmarking.models import (
    ArmPlan,
    BenchmarkWorkerSafetyError,
    QualityEvidence,
    SprintEvidence,
    WorkerContext,
    WorkerOutcome,
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
)
from teams_runtime.shared.models import TEAM_ROLES
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.workflows.orchestration.team_service import TeamService
from teams_runtime.workflows.sprints.lifecycle import apply_initial_plan_confirmation
from teams_runtime.workflows.state.sprint_store import iter_sprint_states


LIVE_BENCHMARK_ENV = "TEAMS_RUNTIME_LIVE_BENCHMARK"
_TERMINAL_SPRINT_STATUSES = frozenset({"completed", "failed", "blocked"})
_COMPLETED_TODO_STATUSES = frozenset({"completed", "committed"})
_MAX_RESUME_PASSES = 16
_RELAY_POLL_SECONDS = 0.02
_CHILD_TERMINATION_GRACE_SECONDS = 5.0
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
        **kwargs: Any,
    ):
        self._benchmark_context = benchmark_context
        self._benchmark_history_seeded = False
        super().__init__(*args, **kwargs)

    def _create_internal_request_record(
        self,
        sprint_state: dict[str, Any],
        todo: dict[str, Any],
        backlog_item: dict[str, Any],
    ) -> dict[str, Any]:
        request_record = super()._create_internal_request_record(
            sprint_state,
            todo,
            backlog_item,
        )
        if self._benchmark_history_seeded:
            return request_record

        seed = _copy_history_seed(self._benchmark_context.history_seed)
        request_record["events"] = [
            *seed,
            *[
                dict(event)
                for event in (request_record.get("events") or [])
                if isinstance(event, dict)
            ],
        ]
        params = (
            dict(request_record.get("params") or {})
            if isinstance(request_record.get("params"), dict)
            else {}
        )
        params["_benchmark_history_seed"] = {
            "event_count": len(seed),
            "sha256": canonical_hash(seed),
        }
        request_record["params"] = params
        self._save_request(request_record)
        self._benchmark_history_seeded = True
        return request_record

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
    if context.call_timeout_seconds <= 0:
        raise ValueError("Benchmark call timeout must be positive")
    if context.run_timeout_seconds <= 0:
        raise ValueError("Benchmark run timeout must be positive")


def _source_import_root() -> Path:
    # teams_runtime/benchmarking/worker.py -> import parent containing teams_runtime.
    return Path(__file__).resolve().parents[2]


def _build_execution_policy(
    context: WorkerContext,
    *,
    budget: InvocationBudget,
) -> ModelExecutionPolicy:
    return ModelExecutionPolicy.for_benchmark(
        allowed_workspace_root=context.workspace_root,
        invocation_budget=budget,
        call_timeout_seconds=context.call_timeout_seconds,
        shell_environment={
            "LANG": "C",
            "LC_ALL": "C",
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
    services = {
        role: _BenchmarkTeamService(
            context.workspace_root,
            role,
            enable_discord_client=False,
            relay_transport="internal",
            model_execution_policy=policy,
            allow_external_research=False,
            benchmark_context=context,
        )
        for role in TEAM_ROLES
    }
    configured_models = {
        str(config.model or "").strip()
        for service in services.values()
        for config in service.runtime_config.role_defaults.values()
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


def _process_exists(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
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
            signaled += 1
            continue
        try:
            os.kill(pid, process_signal)
        except ProcessLookupError:
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
    telemetry_records = load_workspace_telemetry(context.workspace_root)
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
        "telemetry_records": [
            dict(record) for record in outcome.telemetry_records
        ],
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
    telemetry_records = tuple(
        dict(record)
        for record in (payload.get("telemetry_records") or [])
        if isinstance(record, dict)
    )
    return WorkerOutcome(
        status=status,  # type: ignore[arg-type]
        sprint=sprint,
        quality=quality,
        telemetry_records=telemetry_records,
        started_at=str(payload.get("started_at") or ""),
        ended_at=str(payload.get("ended_at") or ""),
        wall_duration_ms=max(int(payload.get("wall_duration_ms") or 0), 0),
        worker_duration_ms=max(int(payload.get("worker_duration_ms") or 0), 0),
        stop_reason=str(payload.get("stop_reason") or ""),
        error_category=str(payload.get("error_category") or ""),
    )


def _child_environment() -> dict[str, str]:
    environment = {
        key: value
        for key in _CHILD_ENVIRONMENT_KEYS
        if (value := os.environ.get(key)) is not None
    }
    environment["PATH"] = environment.get("PATH") or os.defpath
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
    except ProcessLookupError:
        pass


def _worker_group_alive(process: subprocess.Popen[Any]) -> bool:
    if hasattr(os, "killpg"):
        return _process_group_exists(process.pid)
    return process.poll() is None


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
            worker_log.append(
                "worker_cleanup_confirmed",
                provider_group_count=len(provider_entries),
            )
            return
        _signal_provider_entries(surviving_providers, signal.SIGKILL)
        if worker_alive:
            _signal_child_group(process, signal.SIGKILL)
        if time.monotonic() >= deadline:
            worker_log.append(
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
) -> None:
    initial_snapshot = _read_json_mapping(journal_path)
    provider_entries = _merge_active_process_entries(initial_snapshot)
    provider_count = _signal_provider_entries(
        provider_entries,
        signal.SIGTERM,
    )
    worker_log.append(
        "parent_provider_termination_requested",
        process_count=provider_count,
    )
    _signal_child_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=_CHILD_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        # The worker may reserve and launch a provider between the first journal
        # read and delivery of SIGTERM. Re-read before either kill signal.
        pre_kill_snapshot = _read_json_mapping(journal_path)
        provider_entries = _merge_active_process_entries(
            initial_snapshot,
            pre_kill_snapshot,
        )
        _signal_provider_entries(provider_entries, signal.SIGKILL)
        _signal_child_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=_CHILD_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired as exc:
            worker_log.append(
                "worker_process_reap_failed",
                provider_group_count=len(provider_entries),
            )
            raise _WorkerCleanupFailure(
                "Benchmark worker did not exit after SIGKILL"
            ) from exc
    else:
        # Even a worker that exits during its TERM grace window may have written
        # a new provider reservation after the initial snapshot.
        pre_kill_snapshot = _read_json_mapping(journal_path)
        provider_entries = _merge_active_process_entries(
            initial_snapshot,
            pre_kill_snapshot,
        )
        _signal_provider_entries(provider_entries, signal.SIGKILL)
        _signal_child_group(process, signal.SIGKILL)

    # Once the worker is reaped it cannot create another call. A final journal
    # read closes the remaining write/read race before liveness verification.
    final_snapshot = _read_json_mapping(journal_path)
    provider_entries = _merge_active_process_entries(
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


def _partial_outcome(
    context: WorkerContext,
    *,
    status: str,
    started_at: str,
    started_monotonic: float,
    stop_reason: str,
    error_category: str,
) -> WorkerOutcome:
    sprint_state = _latest_sprint_state(context.workspace_root)
    sprint = _sprint_evidence(sprint_state)
    quality = _quality_evidence(sprint_state, sprint)
    duration_ms = max(int((time.monotonic() - started_monotonic) * 1000), 0)
    return WorkerOutcome(
        status=status,  # type: ignore[arg-type]
        sprint=sprint,
        quality=quality,
        telemetry_records=load_workspace_telemetry(context.workspace_root),
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
    _write_private_json(
        manifest_path,
        _manifest_payload(context, result_path=result_path),
    )
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
            env=_child_environment(),
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
        )

    timed_out = False
    try:
        process.wait(timeout=context.run_timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        worker_log.append("worker_child_timeout")
        _terminate_worker_child(
            process,
            journal_path=journal_path,
            worker_log=worker_log,
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            manifest_path.unlink()

    if not timed_out:
        # The child's own asyncio deadline can fire just before the parent's.
        # Verify cleanup even when process.wait() observed a normal child exit.
        _terminate_worker_child(
            process,
            journal_path=journal_path,
            worker_log=worker_log,
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
    if process.returncode != 0 or not result_payload:
        outcome = _partial_outcome(
            context,
            status="failed",
            started_at=started_at,
            started_monotonic=started_monotonic,
            stop_reason="worker_child_failed",
            error_category="worker_child_failed",
        )
    else:
        try:
            outcome = _worker_outcome_from_payload(result_payload)
        except (TypeError, ValueError):
            outcome = _partial_outcome(
                context,
                status="failed",
                started_at=started_at,
                started_monotonic=started_monotonic,
                stop_reason="worker_result_invalid",
                error_category="worker_result_invalid",
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
