from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence


BenchmarkVariant = Literal["before", "after"]
KeepWorkspaces = Literal["none", "failures", "all"]
RunStatus = Literal[
    "completed",
    "failed",
    "timeout",
    "call_budget_exhausted",
    "preflight_failed",
]


class BenchmarkWorkerSafetyError(RuntimeError):
    """Raised when benchmark worker isolation cannot be proven safe."""


@dataclass(slots=True, frozen=True)
class BenchmarkOptions:
    """Operator-selected controls for a sprint A/B benchmark."""

    source_root: Path
    runtime_config_path: Path
    output_dir: Path | None = None
    rate_card_path: Path | None = None
    repetitions: int = 1
    max_invocations: int = 20
    call_timeout_seconds: float = 300.0
    run_timeout_seconds: float = 1800.0
    keep_workspaces: KeepWorkspaces = "failures"
    allow_dirty_source: bool = False
    live: bool = False
    benchmark_id: str = ""

    def validate(self) -> None:
        if self.repetitions <= 0:
            raise ValueError("repetitions must be a positive integer")
        if self.max_invocations <= 0:
            raise ValueError("max_invocations must be a positive integer")
        if self.call_timeout_seconds <= 0:
            raise ValueError("call_timeout_seconds must be positive")
        if self.run_timeout_seconds <= 0:
            raise ValueError("run_timeout_seconds must be positive")
        if self.keep_workspaces not in {"none", "failures", "all"}:
            raise ValueError("keep_workspaces must be none, failures, or all")
        if not self.source_root.expanduser().is_dir():
            raise FileNotFoundError(f"Source root does not exist: {self.source_root}")
        config_path = self.runtime_config_path.expanduser()
        if not config_path.exists():
            raise FileNotFoundError(f"Runtime config does not exist: {config_path}")
        if self.rate_card_path is not None and not self.rate_card_path.expanduser().is_file():
            raise FileNotFoundError(f"Rate card does not exist: {self.rate_card_path}")


@dataclass(slots=True, frozen=True)
class ArmPlan:
    pair_index: int
    order_index: int
    variant: BenchmarkVariant
    run_id: str
    prompt_context_enabled: bool


@dataclass(slots=True, frozen=True)
class SprintEvidence:
    sprint_id: str = ""
    status: str = ""
    closeout_status: str = ""
    todo_count: int = 0
    completed_todo_count: int = 0
    blocked_todo_count: int = 0
    failed_todo_count: int = 0
    commit_sha: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "sprint_id": self.sprint_id,
            "status": self.status,
            "closeout_status": self.closeout_status,
            "todo_count": self.todo_count,
            "completed_todo_count": self.completed_todo_count,
            "blocked_todo_count": self.blocked_todo_count,
            "failed_todo_count": self.failed_todo_count,
            "commit_sha": self.commit_sha,
        }


@dataclass(slots=True, frozen=True)
class QualityEvidence:
    behavior_oracle_passed: bool = False
    sprint_terminal: bool = False
    closeout_verified: bool = False
    protected_files_unchanged: bool = False
    git_clean: bool = False
    commit_created: bool = False
    no_git_remotes: bool = False
    blocked_todo_count: int = 0
    failed_todo_count: int = 0
    notes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            self.behavior_oracle_passed
            and self.sprint_terminal
            and self.closeout_verified
            and self.protected_files_unchanged
            and self.git_clean
            and self.commit_created
            and self.no_git_remotes
            and self.blocked_todo_count == 0
            and self.failed_todo_count == 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "behavior_oracle_passed": self.behavior_oracle_passed,
            "sprint_terminal": self.sprint_terminal,
            "closeout_verified": self.closeout_verified,
            "protected_files_unchanged": self.protected_files_unchanged,
            "git_clean": self.git_clean,
            "commit_created": self.commit_created,
            "no_git_remotes": self.no_git_remotes,
            "blocked_todo_count": self.blocked_todo_count,
            "failed_todo_count": self.failed_todo_count,
            "notes": list(self.notes),
        }


@dataclass(slots=True, frozen=True)
class WorkerContext:
    """Everything an orchestration worker needs for one isolated arm."""

    benchmark_id: str
    arm: ArmPlan
    workspace_root: Path
    run_output_dir: Path
    milestone: str
    history_seed: tuple[Mapping[str, Any], ...]
    max_invocations: int
    call_timeout_seconds: float
    run_timeout_seconds: float
    live: bool


@dataclass(slots=True, frozen=True)
class WorkerOutcome:
    """Privacy-safe result returned by an injected sprint worker."""

    status: RunStatus
    sprint: SprintEvidence = field(default_factory=SprintEvidence)
    quality: QualityEvidence = field(default_factory=QualityEvidence)
    telemetry_records: tuple[Mapping[str, Any], ...] = ()
    started_at: str = ""
    ended_at: str = ""
    wall_duration_ms: int = 0
    worker_duration_ms: int = 0
    stop_reason: str = ""
    error_category: str = ""


class BenchmarkWorker(Protocol):
    def __call__(self, context: WorkerContext) -> WorkerOutcome:
        """Run one complete sprint arm and return privacy-safe evidence."""


@dataclass(slots=True, frozen=True)
class ArmResult:
    arm: ArmPlan
    status: RunStatus
    started_at: str
    ended_at: str
    wall_duration_ms: int
    worker_duration_ms: int
    stop_reason: str
    error_category: str
    config_hash: str
    comparable_config_hash: str
    metrics: Mapping[str, Any]
    quality: QualityEvidence
    sprint: SprintEvidence
    invocation_records: tuple[Mapping[str, Any], ...] = ()
    retained_workspace: str = ""

    def to_dict(self, *, include_records: bool = False) -> dict[str, Any]:
        result = {
            "run_id": self.arm.run_id,
            "pair_index": self.arm.pair_index,
            "order_index": self.arm.order_index,
            "variant": self.arm.variant,
            "prompt_context_enabled": self.arm.prompt_context_enabled,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "wall_duration_ms": self.wall_duration_ms,
            "worker_duration_ms": self.worker_duration_ms,
            "stop_reason": self.stop_reason,
            "error_category": self.error_category,
            "config_hash": self.config_hash,
            "comparable_config_hash": self.comparable_config_hash,
            "metrics": dict(self.metrics),
            "quality": self.quality.to_dict(),
            "sprint": self.sprint.to_dict(),
            "retained_workspace": self.retained_workspace,
        }
        if include_records:
            result["invocations"] = [dict(record) for record in self.invocation_records]
        return result


@dataclass(slots=True, frozen=True)
class BenchmarkResult:
    benchmark_id: str
    status: Literal["comparable", "inconclusive"]
    classification: Literal["preliminary_smoke", "repeated_experiment"]
    output_dir: Path
    report_json: Path
    report_markdown: Path
    runs: tuple[ArmResult, ...]
    report: Mapping[str, Any]

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "comparable" else 1


def make_arm_schedule(repetitions: int) -> tuple[ArmPlan, ...]:
    if repetitions <= 0:
        raise ValueError("repetitions must be a positive integer")
    schedule: list[ArmPlan] = []
    order_index = 0
    for pair_index in range(1, repetitions + 1):
        variants: Sequence[BenchmarkVariant] = (
            ("before", "after") if pair_index % 2 else ("after", "before")
        )
        for variant in variants:
            order_index += 1
            schedule.append(
                ArmPlan(
                    pair_index=pair_index,
                    order_index=order_index,
                    variant=variant,
                    run_id=f"pair-{pair_index:03d}-{variant}",
                    prompt_context_enabled=variant == "after",
                )
            )
    return tuple(schedule)


__all__ = [
    "ArmPlan",
    "ArmResult",
    "BenchmarkOptions",
    "BenchmarkResult",
    "BenchmarkWorker",
    "BenchmarkWorkerSafetyError",
    "QualityEvidence",
    "SprintEvidence",
    "WorkerContext",
    "WorkerOutcome",
    "make_arm_schedule",
]
