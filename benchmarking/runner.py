from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from teams_runtime.benchmarking.metrics import (
    load_workspace_telemetry,
    reduce_telemetry,
    sanitize_invocation_record,
)
from teams_runtime.benchmarking.models import (
    ArmResult,
    BenchmarkOptions,
    BenchmarkResult,
    BenchmarkWorker,
    BenchmarkWorkerSafetyError,
    QualityEvidence,
    WorkerContext,
    WorkerOutcome,
    make_arm_schedule,
)
from teams_runtime.benchmarking.reporting import (
    build_report,
    render_markdown,
    utc_now_iso,
    write_json_atomic,
    write_run_artifacts,
    write_text_atomic,
)
from teams_runtime.benchmarking.scenario import (
    SCENARIO_MILESTONE,
    ScenarioWorkspace,
    create_scenario_workspace,
    inspect_scenario_workspace,
    load_runtime_settings,
)


_BENCHMARK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_RETAINED_BASELINE_FILES = (
    (".benchmark/scenario.json", ".benchmark/scenario.json"),
    (".benchmark/history_seed.json", ".benchmark/history_seed.json"),
    ("benchmark_app.py", "benchmark_app.baseline.py"),
    ("tests/__init__.py", "tests/__init__.py"),
    ("tests/test_benchmark_app.py", "tests/test_benchmark_app.py"),
    ("BENCHMARK_TASK.md", "BENCHMARK_TASK.md"),
    ("team_runtime.yaml", "team_runtime.yaml"),
)
_RETENTION_NOTICE = """# Sanitized Benchmark Snapshot

This directory is an allowlisted diagnostic snapshot, not the execution workspace.
Baseline files are captured before any model call. The mutable implementation is
represented only by a content hash and byte count. Runtime state, model sessions,
provider output, logs, Git metadata, and unrecognized files are intentionally excluded.
"""


class BenchmarkPreflightError(RuntimeError):
    pass


def _git(
    root: Path,
    *args: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("git", *args),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def _dirty_hash(porcelain: str) -> str:
    return hashlib.sha256(porcelain.encode("utf-8")).hexdigest() if porcelain else ""


def _source_revision(source_root: Path, *, allow_dirty: bool) -> dict[str, Any]:
    root = source_root.expanduser().resolve()
    head = _git(root, "rev-parse", "HEAD")
    if head.returncode:
        raise BenchmarkPreflightError(f"Source root is not a Git repository: {root}")
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status.returncode:
        raise BenchmarkPreflightError("Unable to inspect source Git status")
    dirty = bool(status.stdout.strip())
    if dirty and not allow_dirty:
        raise BenchmarkPreflightError(
            "Source worktree is dirty; commit changes or use allow_dirty_source explicitly"
        )
    describe = _git(root, "describe", "--always", "--dirty", "--tags")
    return {
        "commit_sha": head.stdout.strip(),
        "describe": describe.stdout.strip() if describe.returncode == 0 else head.stdout.strip()[:12],
        "dirty": dirty,
        "dirty_state_hash": _dirty_hash(status.stdout),
        "python": platform.python_version(),
        "platform": platform.platform(),
    }


def _benchmark_id(options: BenchmarkOptions) -> str:
    if options.benchmark_id:
        if not _BENCHMARK_ID_PATTERN.fullmatch(options.benchmark_id):
            raise ValueError(
                "benchmark_id must be 1-96 ASCII letters, numbers, dots, underscores, or hyphens"
            )
        return options.benchmark_id
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"sprint-ab-{timestamp}-{uuid.uuid4().hex[:8]}"


def _output_root(options: BenchmarkOptions, benchmark_id: str) -> Path:
    base = (
        options.output_dir.expanduser().resolve()
        if options.output_dir is not None
        else options.source_root.expanduser().resolve() / ".teams_runtime" / "benchmarks"
    )
    root = base / benchmark_id
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    root.chmod(0o700)
    return root


def _merge_quality(
    outcome: WorkerOutcome,
    scenario: ScenarioWorkspace,
) -> QualityEvidence:
    inspection = inspect_scenario_workspace(scenario)
    worker = outcome.quality
    sprint = outcome.sprint
    notes = tuple(dict.fromkeys((*worker.notes, *inspection.notes)))
    return QualityEvidence(
        behavior_oracle_passed=inspection.behavior_oracle_passed,
        sprint_terminal=worker.sprint_terminal,
        closeout_verified=worker.closeout_verified,
        protected_files_unchanged=inspection.protected_files_unchanged,
        git_clean=inspection.git_clean,
        commit_created=inspection.commit_created,
        no_git_remotes=inspection.no_git_remotes,
        blocked_todo_count=max(worker.blocked_todo_count, sprint.blocked_todo_count),
        failed_todo_count=max(worker.failed_todo_count, sprint.failed_todo_count),
        notes=notes,
    )


def _safe_worker_failure(
    started_at: str,
    started_monotonic: float,
    exc: BaseException,
) -> WorkerOutcome:
    return WorkerOutcome(
        status="failed",
        started_at=started_at,
        ended_at=utc_now_iso(),
        wall_duration_ms=max(int((time.monotonic() - started_monotonic) * 1000), 0),
        worker_duration_ms=max(int((time.monotonic() - started_monotonic) * 1000), 0),
        stop_reason="worker_exception",
        error_category=type(exc).__name__,
    )


def _is_safe_regular_file(source_root: Path, relative_name: str) -> bool:
    candidate = source_root
    for component in Path(relative_name).parts:
        candidate = candidate / component
        if candidate.is_symlink():
            return False
    if not candidate.is_file():
        return False
    try:
        candidate.resolve().relative_to(source_root.resolve())
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _capture_retention_baseline(source_root: Path) -> dict[str, bytes]:
    baseline: dict[str, bytes] = {}
    for source_name, retained_name in _RETAINED_BASELINE_FILES:
        if not _is_safe_regular_file(source_root, source_name):
            raise BenchmarkPreflightError(
                f"Benchmark baseline file is missing or unsafe: {source_name}"
            )
        baseline[retained_name] = (source_root / source_name).read_bytes()
    return baseline


def _implementation_result_summary(
    source_root: Path,
    baseline: Mapping[str, bytes],
) -> dict[str, Any]:
    source_name = "benchmark_app.py"
    baseline_content = baseline["benchmark_app.baseline.py"]
    baseline_hash = hashlib.sha256(baseline_content).hexdigest()
    if not _is_safe_regular_file(source_root, source_name):
        return {
            "schema_version": 1,
            "path": source_name,
            "status": "missing_or_unsafe",
            "baseline_sha256": baseline_hash,
            "sha256": None,
            "size_bytes": None,
            "changed_from_baseline": None,
        }
    source = source_root / source_name
    digest = hashlib.sha256()
    size_bytes = 0
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
            size_bytes += len(chunk)
    result_hash = digest.hexdigest()
    return {
        "schema_version": 1,
        "path": source_name,
        "status": "hashed",
        "baseline_sha256": baseline_hash,
        "sha256": result_hash,
        "size_bytes": size_bytes,
        "changed_from_baseline": result_hash != baseline_hash,
    }


def _retain_workspace_snapshot(
    source_root: Path,
    retained_root: Path,
    *,
    baseline: Mapping[str, bytes],
) -> None:
    retained_root.mkdir(parents=True, mode=0o700, exist_ok=False)
    retained_root.chmod(0o700)
    for relative_name, content in baseline.items():
        destination = retained_root / relative_name
        destination.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(0o600)
    write_json_atomic(
        retained_root / "benchmark_app.result.json",
        _implementation_result_summary(source_root, baseline),
    )
    notice_path = retained_root / "RETENTION_NOTICE.md"
    notice_path.write_text(
        _RETENTION_NOTICE,
        encoding="utf-8",
    )
    notice_path.chmod(0o600)


def _run_arm(
    *,
    benchmark_id: str,
    output_root: Path,
    temporary_root: Path,
    options: BenchmarkOptions,
    worker: BenchmarkWorker,
    settings: Any,
    arm: Any,
) -> ArmResult:
    workspace_parent = temporary_root / arm.run_id
    scenario = create_scenario_workspace(
        workspace_parent,
        benchmark_id=benchmark_id,
        run_id=arm.run_id,
        prompt_context_enabled=arm.prompt_context_enabled,
        settings=settings,
    )
    retention_baseline = _capture_retention_baseline(scenario.root)
    run_dir = output_root / "runs" / arm.run_id
    started_at = utc_now_iso()
    started_monotonic = time.monotonic()
    context = WorkerContext(
        benchmark_id=benchmark_id,
        arm=arm,
        workspace_root=scenario.root,
        run_output_dir=run_dir,
        milestone=SCENARIO_MILESTONE,
        history_seed=scenario.history_seed,
        max_invocations=options.max_invocations,
        call_timeout_seconds=options.call_timeout_seconds,
        run_timeout_seconds=options.run_timeout_seconds,
        live=options.live,
    )
    try:
        outcome = worker(context)
        if not isinstance(outcome, WorkerOutcome):
            raise TypeError("Benchmark worker must return WorkerOutcome")
    except BenchmarkWorkerSafetyError:
        raise
    except Exception as exc:
        outcome = _safe_worker_failure(started_at, started_monotonic, exc)
    measured_wall_ms = max(int((time.monotonic() - started_monotonic) * 1000), 0)
    if measured_wall_ms > int(options.run_timeout_seconds * 1000) and outcome.status == "completed":
        outcome = replace(
            outcome,
            status="timeout",
            stop_reason="run_timeout_exceeded",
        )
    raw_records = (
        tuple(sanitize_invocation_record(record) for record in outcome.telemetry_records)
        if outcome.telemetry_records
        else load_workspace_telemetry(scenario.root)
    )
    metrics = reduce_telemetry(raw_records)
    quality = _merge_quality(outcome, scenario)
    result = ArmResult(
        arm=arm,
        status=outcome.status,
        started_at=outcome.started_at or started_at,
        ended_at=outcome.ended_at or utc_now_iso(),
        wall_duration_ms=outcome.wall_duration_ms or measured_wall_ms,
        worker_duration_ms=outcome.worker_duration_ms or measured_wall_ms,
        stop_reason=outcome.stop_reason,
        error_category=outcome.error_category,
        config_hash=scenario.config_hash,
        comparable_config_hash=scenario.comparable_config_hash,
        metrics=metrics,
        quality=quality,
        sprint=outcome.sprint,
        invocation_records=raw_records,
    )
    keep = options.keep_workspaces == "all" or (
        options.keep_workspaces == "failures"
        and (result.status != "completed" or not result.quality.passed)
    )
    if keep:
        retained_root = output_root / "workspaces" / arm.run_id
        retained_root.parent.mkdir(parents=True, exist_ok=True)
        _retain_workspace_snapshot(
            scenario.root,
            retained_root,
            baseline=retention_baseline,
        )
        shutil.rmtree(scenario.root, ignore_errors=True)
        result = replace(
            result,
            retained_workspace=f"workspaces/{arm.run_id}",
        )
    else:
        shutil.rmtree(scenario.root, ignore_errors=True)
    write_run_artifacts(run_dir, result)
    return result


def run_sprint_ab_benchmark(
    options: BenchmarkOptions,
    *,
    worker: BenchmarkWorker,
) -> BenchmarkResult:
    """Run isolated sprint arms and write a privacy-safe A/B report.

    The injected worker owns TeamService orchestration and provider-process
    enforcement. This core owns fixtures, arm isolation, telemetry reduction,
    quality checks, comparison semantics, retention, and report persistence.
    """

    options.validate()
    source_revision = _source_revision(
        options.source_root,
        allow_dirty=options.allow_dirty_source,
    )
    settings = load_runtime_settings(
        options.runtime_config_path,
        rate_card_path=options.rate_card_path,
    )
    benchmark_id = _benchmark_id(options)
    output_root = _output_root(options, benchmark_id)
    started_at = utc_now_iso()
    schedule = make_arm_schedule(options.repetitions)
    runs: list[ArmResult] = []
    history_hash = ""
    with tempfile.TemporaryDirectory(prefix=f"{benchmark_id}-") as temp_directory:
        temporary_root = Path(temp_directory)
        temporary_root.chmod(0o700)
        for arm in schedule:
            result = _run_arm(
                benchmark_id=benchmark_id,
                output_root=output_root,
                temporary_root=temporary_root,
                options=options,
                worker=worker,
                settings=settings,
                arm=arm,
            )
            runs.append(result)
            if result.status == "preflight_failed":
                # The paired arm uses the same source, model, and safety controls.
                # Repeating a failed safety/configuration preflight cannot produce
                # a valid comparison and may obscure the original failure.
                break
            if not history_hash:
                scenario_file = (
                    output_root / result.retained_workspace / ".benchmark" / "scenario.json"
                    if result.retained_workspace
                    else None
                )
                if scenario_file is not None and scenario_file.is_file():
                    import json

                    history_hash = str(
                        (json.loads(scenario_file.read_text(encoding="utf-8")) or {}).get(
                            "history_hash"
                        )
                        or ""
                    )
        if not history_hash:
            from teams_runtime.benchmarking.scenario import build_history_seed, canonical_hash

            history_hash = canonical_hash(build_history_seed())

    ended_at = utc_now_iso()
    report = build_report(
        benchmark_id=benchmark_id,
        options=options,
        source_revision=source_revision,
        source_config_hash=settings.source_config_hash,
        runtime_model_map=settings.role_defaults,
        rate_cards=settings.rate_cards,
        history_hash=history_hash,
        runs=tuple(runs),
        started_at=started_at,
        ended_at=ended_at,
    )
    report_json = output_root / "report.json"
    report_markdown = output_root / "report.md"
    write_json_atomic(report_json, report)
    write_text_atomic(report_markdown, render_markdown(report))
    return BenchmarkResult(
        benchmark_id=benchmark_id,
        status=str(report["status"]),  # type: ignore[arg-type]
        classification=str(report["classification"]),  # type: ignore[arg-type]
        output_dir=output_root,
        report_json=report_json,
        report_markdown=report_markdown,
        runs=tuple(runs),
        report=report,
    )


__all__ = [
    "BenchmarkPreflightError",
    "run_sprint_ab_benchmark",
]
