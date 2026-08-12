from __future__ import annotations

import asyncio
import json
import io
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

from teams_runtime.benchmarking import scenario as benchmark_scenario
from teams_runtime.benchmarking.metrics import (
    SAFE_INVOCATION_FIELDS,
    compare_metrics,
    reduce_telemetry,
    sanitize_invocation_record,
)
from teams_runtime.benchmarking.models import (
    ArmPlan,
    ArmResult,
    BenchmarkOptions,
    BenchmarkWorkerSafetyError,
    QualityEvidence,
    SprintEvidence,
    WorkerContext,
    WorkerOutcome,
    invocation_identity_digest,
    make_arm_schedule,
)
from teams_runtime.benchmarking.reporting import build_report
from teams_runtime.benchmarking.runner import (
    _journal_coverage_available,
    run_sprint_ab_benchmark,
)
from teams_runtime.benchmarking.scenario import (
    BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
    BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
    BENCHMARK_TARGET_INCLUDED_EVENTS,
    BENCHMARK_TARGET_OMITTED_EVENTS,
    BENCHMARK_TARGET_PURPOSE,
    BENCHMARK_TARGET_ROLE,
    BENCHMARK_TARGET_TOTAL_EVENTS,
    BENCHMARK_TARGET_WORKFLOW_STEP,
    PROTECTED_PATHS,
    RuntimeSettings,
    SCENARIO_ID,
    SCENARIO_MILESTONE,
    build_history_seed,
    canonical_hash,
    create_scenario_workspace,
    load_runtime_settings,
)
from teams_runtime.benchmarking.worker import (
    LIVE_BENCHMARK_ENV,
    _BenchmarkHistorySeedState,
    _BenchmarkTeamService,
    _build_execution_policy,
    _history_seed_hash,
    _summarize_call_journal,
    run_live_sprint_arm,
)
from teams_runtime.cli import build_parser, cmd_benchmark_sprint_ab
from teams_runtime.runtime.execution_policy import (
    InvocationBudget,
    InvocationBudgetExceeded,
    ModelExecutionPolicy,
    ModelExecutionPolicyViolation,
)
from teams_runtime.shared.models import (
    INTERNAL_TEAM_AGENTS,
    PromptContextRuntimeConfig,
    TEAM_ROLES,
)
from teams_runtime.shared.prompt_context import (
    PROMPT_EVENT_SELECTION_POLICY,
    project_request_record_for_prompt,
)
from teams_runtime.workflows.orchestration.team_service import TeamService
from teams_runtime.workflows.sprints.lifecycle import apply_initial_plan_confirmation


_MISSING = object()
_HISTORY_SEED_HASH = "13024f26fb93918509533bfc5797e4fb3512944ae885234fd6da1393af72a365"


def _role_defaults() -> dict[str, dict[str, str]]:
    return {
        role: {
            "model": "gpt-benchmark-test",
            "reasoning": "medium",
        }
        for role in TEAM_ROLES
    }


def _internal_agent_defaults() -> dict[str, dict[str, str]]:
    return {
        agent: {
            "model": "gpt-benchmark-helper",
            "reasoning": "low",
        }
        for agent in INTERNAL_TEAM_AGENTS
    }


def _settings() -> RuntimeSettings:
    role_defaults = _role_defaults()
    internal_agent_defaults = _internal_agent_defaults()
    return RuntimeSettings(
        role_defaults=role_defaults,
        internal_agent_defaults=internal_agent_defaults,
        rate_cards={},
        source_config_hash=canonical_hash(
            {
                "role_defaults": role_defaults,
                "internal_agent_defaults": internal_agent_defaults,
                "rate_cards": {},
            }
        ),
    )


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ.get("PATH", ""),
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(
        ("git", *args),
        cwd=root,
        text=True,
        capture_output=True,
        check=True,
        env=environment,
    )


def _initialize_source_repository(root: Path) -> None:
    root.mkdir(parents=True)
    _git(root, "init")
    _git(root, "config", "--local", "user.name", "benchmark-test")
    _git(root, "config", "--local", "user.email", "benchmark-test@invalid.local")
    _git(root, "config", "--local", "commit.gpgsign", "false")
    (root / "source-marker.txt").write_text("benchmark source\n", encoding="utf-8")
    _git(root, "add", "source-marker.txt")
    _git(root, "commit", "-m", "seed benchmark source")


def _write_runtime_config(path: Path) -> None:
    path.write_text(
        yaml.safe_dump({"role_defaults": _role_defaults()}, sort_keys=False),
        encoding="utf-8",
    )


def _telemetry_record(
    *,
    variant: str,
    occurrence: int = 1,
    native_usage: bool = True,
    estimated_cost: float | object = 0.001,
    compacted: bool | None = None,
) -> dict[str, Any]:
    is_after = variant == "after"
    if compacted is None:
        compacted = is_after
    input_tokens = 240 if is_after else 480
    cached_tokens = 40 if is_after else 80
    output_tokens = 30
    included_events = (
        BENCHMARK_TARGET_INCLUDED_EVENTS
        if compacted
        else BENCHMARK_TARGET_TOTAL_EVENTS
    )
    omitted_events = BENCHMARK_TARGET_OMITTED_EVENTS if compacted else 0
    record: dict[str, Any] = {
        "schema_version": 1,
        "invocation_id": f"{variant}-invocation-{occurrence}",
        "operation_id": f"{variant}-operation-{occurrence}",
        "logical_call_id": f"{variant}-logical-{occurrence}",
        "attempt_index": 1,
        "attempt_kind": "primary",
        "started_at": f"2026-07-27T00:00:0{occurrence}+00:00",
        "ended_at": f"2026-07-27T00:00:1{occurrence}+00:00",
        "duration_ms": 400 if is_after else 700,
        "runtime_identity": "role",
        "role": BENCHMARK_TARGET_ROLE if occurrence == 1 else "developer",
        "purpose": BENCHMARK_TARGET_PURPOSE if occurrence == 1 else "implement",
        "workflow_step": (
            BENCHMARK_TARGET_WORKFLOW_STEP
            if occurrence == 1
            else "todo_execution"
        ),
        "request_id": f"request-{occurrence}",
        "sprint_id": "benchmark-sprint",
        "todo_id": "todo-1",
        "provider": "codex_cli",
        "model": "gpt-benchmark-test",
        "reasoning": "medium",
        "status": "completed",
        "exit_code": 0,
        "prompt_chars": 1800 if is_after else 4200,
        "output_chars": 300,
        "tool_calls": 2,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 10,
        "total_tokens": input_tokens + output_tokens,
        "usage_source": "native" if native_usage else "",
        "prompt_context_enabled": is_after,
        "prompt_context_total_events": BENCHMARK_TARGET_TOTAL_EVENTS,
        "prompt_context_included_events": included_events,
        "prompt_context_omitted_events": omitted_events,
        "prompt_context_recent_events": BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
        "prompt_context_max_events": BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
        "prompt_context_selection_policy": PROMPT_EVENT_SELECTION_POLICY,
        "prompt_context": {
            "enabled": is_after,
            "compacted": bool(compacted),
            "total_events": BENCHMARK_TARGET_TOTAL_EVENTS,
            "included_events": included_events,
            "omitted_events": omitted_events,
            "recent_events": BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
            "max_events": BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
            "selection_policy": PROMPT_EVENT_SELECTION_POLICY,
            "raw_history": "SENSITIVE_HISTORY_SHOULD_NOT_PERSIST",
        },
        "prompt": "SENSITIVE_PROMPT_SHOULD_NOT_PERSIST",
        "response": "SENSITIVE_RESPONSE_SHOULD_NOT_PERSIST",
        "api_key": "SENSITIVE_API_KEY_SHOULD_NOT_PERSIST",
        "session_id": "SENSITIVE_SESSION_ID_SHOULD_NOT_PERSIST",
    }
    if estimated_cost is not _MISSING:
        record["estimated_cost_usd"] = estimated_cost
    return record


def _passing_quality() -> QualityEvidence:
    return QualityEvidence(
        behavior_oracle_passed=True,
        sprint_terminal=True,
        closeout_verified=True,
        protected_files_unchanged=True,
        git_clean=True,
        commit_created=True,
        no_git_remotes=True,
    )


def _arm_result(
    variant: str,
    records: tuple[dict[str, Any], ...],
    *,
    pair_index: int = 1,
    order_index: int | None = None,
    comparable_config_hash: str = "same-non-feature-config",
) -> ArmResult:
    invocation_count = len(records)
    completed_count = sum(
        str(record.get("status") or "") == "completed"
        for record in records
    )
    invocation_ids = [
        str(record.get("invocation_id") or "")
        for record in records
    ]
    candidate_metrics = reduce_telemetry(records)
    verified_target_projection_count = int(
        candidate_metrics["compaction"]["target_projection_candidate_count"]
    )
    verified_target_invocation_ids_sha256 = str(
        candidate_metrics["compaction"][
            "target_projection_invocation_ids_sha256"
        ]
    )
    return ArmResult(
        arm=ArmPlan(
            pair_index=pair_index,
            order_index=order_index or (1 if variant == "before" else 2),
            variant=variant,  # type: ignore[arg-type]
            run_id=f"pair-{pair_index:03d}-{variant}",
            prompt_context_enabled=variant == "after",
        ),
        status="completed",
        started_at="2026-07-27T00:00:00+00:00",
        ended_at="2026-07-27T00:01:00+00:00",
        wall_duration_ms=1_000 if variant == "before" else 700,
        worker_duration_ms=900 if variant == "before" else 600,
        stop_reason="",
        error_category="",
        config_hash=f"{variant}-config",
        comparable_config_hash=comparable_config_hash,
        metrics=reduce_telemetry(
            records,
            verified_target_projection_count=verified_target_projection_count,
            verified_target_invocation_ids_sha256=(
                verified_target_invocation_ids_sha256
            ),
        ),
        quality=_passing_quality(),
        sprint=SprintEvidence(
            sprint_id="benchmark-sprint",
            status="completed",
            closeout_status="verified",
            todo_count=1,
            completed_todo_count=1,
            commit_sha=f"{variant}-commit",
        ),
        invocation_attempts={
            "schema_version": 1,
            "journal_available": True,
            "journal_schema_version": 3,
            "reconciled": True,
            "identity_reconciled": True,
            "context_reconciled": True,
            "max_invocations": 20,
            "reserved_count": invocation_count,
            "entry_count": invocation_count,
            "telemetry_record_count": invocation_count,
            "unobserved_attempt_count": 0,
            "telemetry_overage_count": 0,
            "completed_count": completed_count,
            "failed_count": invocation_count - completed_count,
            "timeout_count": 0,
            "launch_failed_count": 0,
            "terminated_count": 0,
            "active_count": 0,
            "unknown_state_count": 0,
            "malformed_entry_count": 0,
            "unaccounted_count": 0,
            "overaccounted_count": 0,
            "rejected_count": 0,
            "remaining_budget": 20 - invocation_count,
            "journal_invocation_ids_sha256": (
                invocation_identity_digest(invocation_ids)
            ),
            "journal_invocation_id_missing_count": 0,
            "journal_invocation_id_duplicate_count": 0,
            "telemetry_invocation_id_missing_count": 0,
            "telemetry_invocation_id_duplicate_count": 0,
            "telemetry_invocation_id_unmatched_count": 0,
            "journal_invocation_id_unobserved_count": 0,
            "journal_telemetry_context_mismatch_count": 0,
            "verified_target_projection_count": verified_target_projection_count,
            "verified_target_invocation_ids_sha256": (
                verified_target_invocation_ids_sha256
            ),
        },
        invocation_records=records,
    )


def _report_for_runs(
    runs: tuple[ArmResult, ...],
    *,
    repetitions: int = 1,
) -> dict[str, Any]:
    options = BenchmarkOptions(
        source_root=Path("/unused/source"),
        runtime_config_path=Path("/unused/team_runtime.yaml"),
        repetitions=repetitions,
    )
    return build_report(
        benchmark_id="deterministic-report",
        options=options,
        source_revision={"commit_sha": "source-sha", "dirty": False},
        source_config_hash="source-config-hash",
        runtime_model_map={**_role_defaults(), **_internal_agent_defaults()},
        rate_cards={},
        history_hash=_HISTORY_SEED_HASH,
        runs=runs,
        started_at="2026-07-27T00:00:00+00:00",
        ended_at="2026-07-27T00:02:00+00:00",
    )


class SprintBenchmarkScenarioTests(unittest.TestCase):
    def test_runtime_settings_hash_and_record_effective_internal_agent_tiers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = root / "team_runtime.yaml"
            config_path.write_text(
                yaml.safe_dump({"role_defaults": _role_defaults()}, sort_keys=False),
                encoding="utf-8",
            )

            inherited = load_runtime_settings(config_path)
            self.assertEqual(
                inherited.internal_agent_defaults["parser"],
                inherited.role_defaults["orchestrator"],
            )

            config_path.write_text(
                yaml.safe_dump(
                    {
                        "role_defaults": _role_defaults(),
                        "internal_agent_defaults": {
                            "parser": {"model": "gpt-benchmark-helper"},
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            partial = load_runtime_settings(config_path)
            self.assertEqual(
                partial.internal_agent_defaults["parser"],
                {
                    "model": "gpt-benchmark-helper",
                    "reasoning": _role_defaults()["orchestrator"]["reasoning"],
                },
            )
            self.assertEqual(
                partial.internal_agent_defaults["sourcer"],
                partial.role_defaults["orchestrator"],
            )

            explicit_payload = {
                "role_defaults": _role_defaults(),
                "internal_agent_defaults": _internal_agent_defaults(),
            }
            config_path.write_text(
                yaml.safe_dump(explicit_payload, sort_keys=False),
                encoding="utf-8",
            )
            explicit = load_runtime_settings(config_path)

            self.assertEqual(
                explicit.internal_agent_defaults["sourcer"]["model"],
                "gpt-benchmark-helper",
            )
            self.assertNotEqual(
                inherited.source_config_hash,
                explicit.source_config_hash,
            )

    def test_schedule_alternates_pair_order_and_only_after_enables_compaction(self) -> None:
        schedule = make_arm_schedule(3)

        self.assertEqual(
            [
                (
                    arm.pair_index,
                    arm.order_index,
                    arm.variant,
                    arm.run_id,
                    arm.prompt_context_enabled,
                )
                for arm in schedule
            ],
            [
                (1, 1, "before", "pair-001-before", False),
                (1, 2, "after", "pair-001-after", True),
                (2, 3, "after", "pair-002-after", True),
                (2, 4, "before", "pair-002-before", False),
                (3, 5, "before", "pair-003-before", False),
                (3, 6, "after", "pair-003-after", True),
            ],
        )
        with self.assertRaisesRegex(ValueError, "positive integer"):
            make_arm_schedule(0)

    def test_history_seed_is_deterministic_balanced_and_has_a_golden_hash(self) -> None:
        first = build_history_seed()
        second = build_history_seed()
        role_reports = [event for event in first if event["type"] == "role_report"]

        self.assertEqual(first, second)
        self.assertEqual(len(first), 48)
        self.assertEqual(canonical_hash(first), _HISTORY_SEED_HASH)
        self.assertEqual(
            [event["actor"] for event in role_reports],
            [
                "research",
                "planner",
                "designer",
                "architect",
                "developer",
                "qa",
                "version_controller",
                "orchestrator",
            ],
        )
        self.assertEqual(first[0]["created_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(first[-1]["created_at"], "2026-01-01T00:47:00+00:00")
        with self.assertRaisesRegex(ValueError, "at least 24"):
            build_history_seed(23)

    def test_fixture_reproduces_defect_and_config_fingerprints_only_feature_toggle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            before = create_scenario_workspace(
                root / "before",
                benchmark_id="fixture-fingerprint",
                run_id="pair-001-before",
                prompt_context_enabled=False,
                settings=_settings(),
            )
            after = create_scenario_workspace(
                root / "after",
                benchmark_id="fixture-fingerprint",
                run_id="pair-001-after",
                prompt_context_enabled=True,
                settings=_settings(),
            )

            before_config = yaml.safe_load(
                (before.root / "team_runtime.yaml").read_text(encoding="utf-8")
            )
            after_config = yaml.safe_load(
                (after.root / "team_runtime.yaml").read_text(encoding="utf-8")
            )
            self.assertNotEqual(before.config_hash, after.config_hash)
            self.assertEqual(before.comparable_config_hash, after.comparable_config_hash)
            self.assertEqual(before.history_hash, after.history_hash)
            self.assertEqual(before.history_hash, _HISTORY_SEED_HASH)
            scenario = json.loads(
                (before.root / ".benchmark" / "scenario.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(scenario["scenario_id"], "sum-positive-full-sprint-v2")
            accepted_return = (
                "return sum(value for value in values if value > 0)"
            )
            self.assertIn(accepted_return, scenario["milestone"])
            self.assertIn(
                accepted_return,
                (before.root / "BENCHMARK_TASK.md").read_text(encoding="utf-8"),
            )
            self.assertFalse(before_config["prompt_context"]["enabled"])
            self.assertTrue(after_config["prompt_context"]["enabled"])
            self.assertEqual(
                before_config["internal_agent_defaults"],
                _internal_agent_defaults(),
            )
            before_config["prompt_context"].pop("enabled")
            after_config["prompt_context"].pop("enabled")
            self.assertEqual(before_config, after_config)
            self.assertEqual(before.initial_commit_count, 1)
            self.assertEqual(set(before.protected_hashes), set(PROTECTED_PATHS))
            self.assertIn(
                "return sum(values)",
                (before.root / "benchmark_app.py").read_text(encoding="utf-8"),
            )
            baseline = subprocess.run(
                ("python", "-m", "unittest", "discover", "-s", "tests"),
                cwd=before.root,
                text=True,
                capture_output=True,
                check=False,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONPATH": str(before.root),
                    "LC_ALL": "C",
                },
            )
            self.assertNotEqual(baseline.returncode, 0)
            self.assertEqual(_git(before.root, "remote").stdout.strip(), "")

    def test_constrained_ast_oracle_accepts_only_the_documented_repair(self) -> None:
        accepted = (
            '"""Optional module docstring."""\n\n'
            "def sum_positive(values):\n"
            '    """Optional function docstring."""\n'
            "    return sum(value for value in values if value > 0)\n"
        )
        rejected = {
            "top_level_statement": (
                "sentinel = 'would execute under an importing oracle'\n" + accepted
            ),
            "annotation": (
                "def sum_positive(values: list[int]):\n"
                "    return sum(value for value in values if value > 0)\n"
            ),
            "default": (
                "def sum_positive(values=()):\n"
                "    return sum(value for value in values if value > 0)\n"
            ),
            "list_comprehension": (
                "def sum_positive(values):\n"
                "    return sum([value for value in values if value > 0])\n"
            ),
            "renamed_operand": (
                "def sum_positive(values):\n"
                "    return sum(item for item in values if item > 0)\n"
            ),
            "extra_statement": (
                "def sum_positive(values):\n"
                "    positives = (value for value in values if value > 0)\n"
                "    return sum(positives)\n"
            ),
        }

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            target = root / "benchmark_app.py"
            target.write_text(accepted, encoding="utf-8")
            self.assertTrue(benchmark_scenario._sum_positive_ast_oracle(root))

            for label, source in rejected.items():
                with self.subTest(label=label):
                    target.write_text(source, encoding="utf-8")
                    self.assertFalse(
                        benchmark_scenario._sum_positive_ast_oracle(root)
                    )

    def test_final_oracle_never_executes_model_modified_python(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scenario = create_scenario_workspace(
                root / "workspace",
                benchmark_id="non-executing-oracle",
                run_id="pair-001-before",
                prompt_context_enabled=False,
                settings=_settings(),
            )
            marker = root / "model-code-executed"
            (scenario.root / "benchmark_app.py").write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('unsafe')\n\n"
                "def sum_positive(values):\n"
                "    return sum(value for value in values if value > 0)\n",
                encoding="utf-8",
            )

            def fake_git(
                _root: Path,
                *args: str,
                **_kwargs: object,
            ) -> subprocess.CompletedProcess[str]:
                stdout = ""
                if args[:2] == ("rev-parse", "HEAD"):
                    stdout = "new-commit\n"
                return subprocess.CompletedProcess(args, 0, stdout, "")

            with (
                mock.patch.object(
                    benchmark_scenario,
                    "_run_git",
                    side_effect=fake_git,
                ),
                mock.patch.object(
                    benchmark_scenario.subprocess,
                    "run",
                    side_effect=AssertionError("workspace code must not be executed"),
                ),
            ):
                inspection = benchmark_scenario.inspect_scenario_workspace(scenario)

            self.assertFalse(marker.exists())
            self.assertFalse(inspection.behavior_oracle_passed)
            self.assertIn("behavior_oracle_failed", inspection.notes)

    def test_protected_hashing_rejects_symlinked_components(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            external = root / "external"
            workspace.mkdir()
            external.mkdir()
            (external / "scenario.json").write_text("same bytes\n", encoding="utf-8")
            (workspace / ".benchmark").symlink_to(external, target_is_directory=True)

            with self.assertRaises(OSError):
                benchmark_scenario._protected_file_hash(
                    workspace,
                    ".benchmark/scenario.json",
                )

    def test_git_inspection_pins_binary_and_overrides_executable_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scenario = create_scenario_workspace(
                root / "workspace",
                benchmark_id="safe-git-inspection",
                run_id="pair-001-after",
                prompt_context_enabled=True,
                settings=_settings(),
            )
            (scenario.root / "benchmark_app.py").write_text(
                "def sum_positive(values):\n"
                "    return sum(value for value in values if value > 0)\n",
                encoding="utf-8",
            )
            (scenario.root / ".gitattributes").write_text(
                "benchmark_app.py filter=model-filter\n",
                encoding="utf-8",
            )
            _git(scenario.root, "add", "benchmark_app.py", ".gitattributes")
            _git(scenario.root, "commit", "-m", "repair fixture")

            config_marker = root / "config-command-executed"
            config_command = root / "model-config-command"
            config_command.write_text(
                "#!/bin/sh\n"
                f": > {str(config_marker)!r}\n"
                "cat\n",
                encoding="utf-8",
            )
            config_command.chmod(0o700)
            for key in (
                "core.fsmonitor",
                "core.hooksPath",
                "diff.external",
                "core.pager",
                "filter.model-filter.clean",
            ):
                _git(scenario.root, "config", "--local", key, str(config_command))
            # Force status to inspect content rather than accepting the cached stat.
            target = scenario.root / "benchmark_app.py"
            target.write_bytes(target.read_bytes())

            fake_bin = root / "fake-bin"
            fake_bin.mkdir()
            fake_git_marker = root / "fake-git-executed"
            fake_git = fake_bin / "git"
            fake_git.write_text(
                "#!/bin/sh\n"
                f": > {str(fake_git_marker)!r}\n"
                "exit 0\n",
                encoding="utf-8",
            )
            fake_git.chmod(0o700)

            with mock.patch.dict(os.environ, {"PATH": str(fake_bin)}):
                inspection = benchmark_scenario.inspect_scenario_workspace(scenario)

            self.assertTrue(inspection.behavior_oracle_passed)
            self.assertTrue(inspection.protected_files_unchanged)
            self.assertTrue(inspection.git_clean)
            self.assertTrue(inspection.commit_created)
            self.assertTrue(inspection.no_git_remotes)
            self.assertFalse(config_marker.exists())
            self.assertFalse(fake_git_marker.exists())

            (scenario.root / ".git" / "info" / "attributes").write_text(
                "benchmark_app.py filter=model-filter\n",
                encoding="utf-8",
            )
            tampered_attributes = benchmark_scenario.inspect_scenario_workspace(
                scenario
            )
            self.assertFalse(tampered_attributes.git_clean)
            self.assertIn("git_attributes_changed", tampered_attributes.notes)
            self.assertFalse(config_marker.exists())

    def test_git_command_uses_non_executing_inspection_overrides(self) -> None:
        completed = subprocess.CompletedProcess(("git", "status"), 0, "", "")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with mock.patch.object(
                benchmark_scenario.subprocess,
                "run",
                return_value=completed,
            ) as run:
                benchmark_scenario._run_git(
                    root,
                    "status",
                    git_executable=Path(sys.executable).resolve(),
                )

        command = run.call_args.args[0]
        environment = run.call_args.kwargs["env"]
        self.assertEqual(command[0], str(Path(sys.executable).resolve()))
        self.assertIn("--no-pager", command)
        self.assertIn(f"core.hooksPath={os.devnull}", command)
        self.assertIn("core.fsmonitor=false", command)
        self.assertIn(f"core.attributesFile={os.devnull}", command)
        self.assertIn("diff.external=", command)
        self.assertIn("core.pager=", command)
        self.assertEqual(environment["GIT_EXTERNAL_DIFF"], "")
        self.assertEqual(environment["GIT_PAGER"], "")
        self.assertEqual(environment["PATH"], os.defpath)
        self.assertEqual(run.call_args.kwargs["timeout"], 10.0)


class SprintBenchmarkBackfillTests(unittest.TestCase):
    @staticmethod
    def _service(
        state: _BenchmarkHistorySeedState | None = None,
    ) -> _BenchmarkTeamService:
        service = object.__new__(_BenchmarkTeamService)
        service._benchmark_context = mock.Mock(history_seed=build_history_seed())
        service._benchmark_history_state = state or _BenchmarkHistorySeedState()
        service._benchmark_history_seeded = False
        return service

    def test_first_sprint_request_is_seeded_before_persistence(self) -> None:
        service = self._service()
        persisted: list[dict[str, Any]] = []
        request_record = {
            "request_id": "planning-request",
            "params": {
                "_teams_kind": "sprint_internal",
                "sprint_phase": "initial",
                "initial_phase_step": "milestone_refinement",
            },
            "events": [{"type": "created", "actor": "sprint_runner"}],
        }
        second_request = {
            "request_id": "todo-request",
            "params": {"_teams_kind": "sprint_internal"},
            "events": [{"type": "created", "actor": "sprint_runner"}],
        }

        def capture(_service: TeamService, record: dict[str, Any]) -> None:
            persisted.append(json.loads(json.dumps(record)))

        with mock.patch.object(
            TeamService,
            "_save_request",
            autospec=True,
            side_effect=capture,
        ):
            service._save_request(request_record)
            service._save_request(second_request)

        seed = build_history_seed()
        self.assertEqual(len(persisted), 2)
        self.assertEqual(persisted[0]["events"][: len(seed)], list(seed))
        self.assertEqual(
            canonical_hash(persisted[0]["events"][: len(seed)]),
            _HISTORY_SEED_HASH,
        )
        self.assertEqual(
            persisted[0]["params"]["_benchmark_history_seed"],
            {
                "event_count": len(seed),
                "sha256": _HISTORY_SEED_HASH,
            },
        )
        self.assertEqual(
            persisted[0]["events"][-1],
            {"type": "created", "actor": "sprint_runner"},
        )
        self.assertNotIn("_benchmark_history_seed", persisted[1]["params"])
        self.assertEqual(len(persisted[1]["events"]), 1)

    def test_persisted_seed_is_idempotent_across_role_services(self) -> None:
        shared_state = _BenchmarkHistorySeedState()
        first_service = self._service(shared_state)
        relay_service = self._service(shared_state)
        later_service = self._service(shared_state)
        seed = build_history_seed()
        request_record = {
            "request_id": "planning-request",
            "params": {
                "_teams_kind": "sprint_internal",
                "sprint_phase": "initial",
                "initial_phase_step": "milestone_refinement",
            },
            "events": [{"type": "created", "actor": "sprint_runner"}],
        }

        with mock.patch.object(TeamService, "_save_request", autospec=True):
            first_service._save_request(request_record)
            first_event_count = len(request_record["events"])
            relay_service._save_request(request_record)
            later_request = {
                "request_id": "later-planning-request",
                "params": {
                    "_teams_kind": "sprint_internal",
                    "sprint_phase": "initial",
                    "initial_phase_step": "milestone_refinement",
                },
                "events": [{"type": "created", "actor": "sprint_runner"}],
            }
            later_service._save_request(later_request)

        self.assertEqual(first_event_count, len(seed) + 1)
        self.assertEqual(len(request_record["events"]), first_event_count)
        self.assertTrue(relay_service._benchmark_history_seeded)
        self.assertEqual(len(later_request["events"]), 1)
        self.assertNotIn(
            "_benchmark_history_seed",
            later_request["params"],
        )

    def test_non_initial_sprint_request_cannot_consume_backfill(self) -> None:
        service = self._service()
        request_record = {
            "request_id": "todo-request",
            "params": {"_teams_kind": "sprint_internal"},
            "events": [{"type": "created", "actor": "sprint_runner"}],
        }

        with mock.patch.object(TeamService, "_save_request", autospec=True):
            service._save_request(request_record)

        self.assertEqual(len(request_record["events"]), 1)
        self.assertNotIn(
            "_benchmark_history_seed",
            request_record["params"],
        )
        self.assertFalse(service._benchmark_history_seeded)

    def test_seed_marker_conflicts_fail_before_persistence(self) -> None:
        service = self._service()
        request_record = {
            "request_id": "planning-request",
            "params": {
                "_teams_kind": "sprint_internal",
                "sprint_phase": "initial",
                "initial_phase_step": "milestone_refinement",
                "_benchmark_history_seed": {
                    "event_count": 48,
                    "sha256": "wrong-hash",
                },
            },
            "events": list(build_history_seed()),
        }

        with mock.patch.object(
            TeamService,
            "_save_request",
            autospec=True,
        ) as save_request:
            with self.assertRaisesRegex(ValueError, "invalid history seed marker"):
                service._save_request(request_record)

        save_request.assert_not_called()
        self.assertFalse(service._benchmark_history_seeded)

    def test_failed_persistence_does_not_mark_seed_as_complete(self) -> None:
        service = self._service()
        request_record = {
            "request_id": "planning-request",
            "params": {
                "_teams_kind": "sprint_internal",
                "sprint_phase": "initial",
                "initial_phase_step": "milestone_refinement",
            },
            "events": [{"type": "created", "actor": "sprint_runner"}],
        }

        with mock.patch.object(
            TeamService,
            "_save_request",
            autospec=True,
            side_effect=OSError("write failed"),
        ):
            with self.assertRaisesRegex(OSError, "write failed"):
                service._save_request(request_record)

        self.assertFalse(service._benchmark_history_seeded)

    def test_real_planning_lifecycle_produces_exact_v2_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scenario = create_scenario_workspace(
                root / "arm",
                benchmark_id="planning-seed-boundary",
                run_id="pair-001-before",
                prompt_context_enabled=False,
                settings=_settings(),
            )
            context = WorkerContext(
                benchmark_id="planning-seed-boundary",
                arm=ArmPlan(
                    pair_index=1,
                    order_index=1,
                    variant="before",
                    run_id="pair-001-before",
                    prompt_context_enabled=False,
                ),
                workspace_root=scenario.root,
                run_output_dir=root / "run",
                milestone="Verify deterministic Backfill persistence.",
                history_seed=scenario.history_seed,
                max_invocations=1,
                call_timeout_seconds=1,
                run_timeout_seconds=1,
                live=True,
            )
            service = _BenchmarkTeamService(
                scenario.root,
                "orchestrator",
                enable_discord_client=False,
                relay_transport="internal",
                allow_external_research=False,
                benchmark_context=context,
            )
            sprint_state = service._build_manual_sprint_state(
                milestone_title="Verify deterministic Backfill persistence.",
                trigger="benchmark",
            )

            request_record = service._build_sprint_planning_request_record(
                sprint_state,
                phase="initial",
                iteration=1,
                step="milestone_refinement",
            )
            persisted = service._load_request(request_record["request_id"])
            relay_service = _BenchmarkTeamService(
                scenario.root,
                "research",
                enable_discord_client=False,
                relay_transport="internal",
                allow_external_research=False,
                benchmark_context=context,
                benchmark_history_state=service._benchmark_history_state,
            )
            relay_service._save_request(persisted)
            persisted = relay_service._load_request(request_record["request_id"])

            with (
                mock.patch.object(
                    service,
                    "_delegate_request",
                    new=mock.AsyncMock(return_value=True),
                ),
                mock.patch.object(
                    service,
                    "_wait_for_internal_request_result",
                    new=mock.AsyncMock(return_value={"status": "completed"}),
                ),
                mock.patch.object(service, "_append_role_history"),
                mock.patch.object(service, "_record_internal_sprint_activity"),
            ):
                asyncio.run(
                    service._run_internal_request_chain(
                        sprint_id=str(sprint_state["sprint_id"]),
                        request_record=persisted,
                        initial_role="research",
                    )
                )
            delegated = service._load_request(request_record["request_id"])

        seed_count = len(build_history_seed())
        persisted_prefix = list(delegated["events"][:seed_count])
        self.assertEqual(len(delegated["events"]), BENCHMARK_TARGET_TOTAL_EVENTS)
        self.assertEqual(_history_seed_hash(persisted_prefix), _HISTORY_SEED_HASH)
        self.assertEqual(
            delegated["params"]["_benchmark_history_seed"],
            {
                "event_count": seed_count,
                "sha256": _HISTORY_SEED_HASH,
            },
        )
        self.assertEqual(
            [event["type"] for event in delegated["events"][-2:]],
            ["created", "delegated"],
        )
        before_projection = project_request_record_for_prompt(
            delegated,
            PromptContextRuntimeConfig(
                enabled=False,
                recent_events=BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
                max_events=BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
            ),
        )
        after_projection = project_request_record_for_prompt(
            delegated,
            PromptContextRuntimeConfig(
                enabled=True,
                recent_events=BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
                max_events=BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
            ),
        )
        self.assertEqual(
            (
                before_projection.total_events,
                before_projection.included_events,
                before_projection.omitted_events,
            ),
            (BENCHMARK_TARGET_TOTAL_EVENTS, BENCHMARK_TARGET_TOTAL_EVENTS, 0),
        )
        self.assertEqual(
            (
                after_projection.total_events,
                after_projection.included_events,
                after_projection.omitted_events,
            ),
            (
                BENCHMARK_TARGET_TOTAL_EVENTS,
                BENCHMARK_TARGET_INCLUDED_EVENTS,
                BENCHMARK_TARGET_OMITTED_EVENTS,
            ),
        )


class SprintBenchmarkCliTests(unittest.TestCase):
    def test_options_reject_non_finite_timeouts(self) -> None:
        options = BenchmarkOptions(
            source_root=Path.cwd(),
            runtime_config_path=Path(__file__),
        )

        for field_name in ("call_timeout_seconds", "run_timeout_seconds"):
            for value in (math.nan, math.inf, -math.inf):
                with self.subTest(field_name=field_name, value=value):
                    with self.assertRaises(ValueError):
                        replace(options, **{field_name: value}).validate()

    def test_parser_exposes_bounded_sprint_ab_defaults(self) -> None:
        args = build_parser().parse_args(
            [
                "benchmark",
                "sprint-ab",
                "--runtime-config",
                "deployed/team_runtime.yaml",
            ]
        )

        self.assertEqual(args.command, "benchmark")
        self.assertEqual(args.benchmark_command, "sprint-ab")
        self.assertFalse(args.live)
        self.assertEqual(args.repetitions, 1)
        self.assertEqual(args.max_invocations, 20)
        self.assertEqual(args.call_timeout_seconds, 300.0)
        self.assertEqual(args.run_timeout_seconds, 1800.0)
        self.assertEqual(args.keep_workspaces, "failures")

    def test_cli_requires_both_live_opt_ins_before_calling_runner(self) -> None:
        cases = (
            (False, {"TEAMS_RUNTIME_LIVE_BENCHMARK": "1"}),
            (True, {}),
        )
        for live, environment in cases:
            with self.subTest(live=live, environment=environment):
                with (
                    mock.patch.dict(os.environ, environment, clear=True),
                    mock.patch(
                        "teams_runtime.benchmarking.runner.run_sprint_ab_benchmark"
                    ) as runner,
                    redirect_stdout(io.StringIO()),
                ):
                    exit_code = cmd_benchmark_sprint_ab(
                        live=live,
                        runtime_config="unused.yaml",
                        repetitions=1,
                        max_invocations=20,
                        call_timeout_seconds=300,
                        run_timeout_seconds=1800,
                        keep_workspaces="failures",
                    )
                self.assertEqual(exit_code, 2)
                runner.assert_not_called()

    def test_cli_renders_successful_result_as_json(self) -> None:
        result = mock.Mock(
            benchmark_id="json-success",
            status="comparable",
            classification="preliminary_smoke",
            output_dir=Path("/tmp/json-success"),
            report_json=Path("/tmp/json-success/report.json"),
            report_markdown=Path("/tmp/json-success/report.md"),
            exit_code=0,
        )
        output = io.StringIO()

        with (
            mock.patch.dict(
                os.environ,
                {"TEAMS_RUNTIME_LIVE_BENCHMARK": "1"},
                clear=True,
            ),
            mock.patch(
                "teams_runtime.benchmarking.runner.run_sprint_ab_benchmark",
                return_value=result,
            ) as runner,
            redirect_stdout(output),
        ):
            exit_code = cmd_benchmark_sprint_ab(
                live=True,
                runtime_config="unused.yaml",
                repetitions=1,
                max_invocations=20,
                call_timeout_seconds=300,
                run_timeout_seconds=1800,
                keep_workspaces="failures",
                as_json=True,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "benchmark_id": "json-success",
                "status": "comparable",
                "classification": "preliminary_smoke",
                "output_dir": "/tmp/json-success",
                "report_json": "/tmp/json-success/report.json",
                "report_markdown": "/tmp/json-success/report.md",
                "exit_code": 0,
            },
        )
        runner.assert_called_once()

    def test_cli_returns_two_for_fatal_worker_safety_abort(self) -> None:
        output = io.StringIO()

        with (
            mock.patch.dict(
                os.environ,
                {"TEAMS_RUNTIME_LIVE_BENCHMARK": "1"},
                clear=True,
            ),
            mock.patch(
                "teams_runtime.benchmarking.runner.run_sprint_ab_benchmark",
                side_effect=BenchmarkWorkerSafetyError(
                    "provider cleanup could not be confirmed"
                ),
            ),
            redirect_stdout(output),
        ):
            exit_code = cmd_benchmark_sprint_ab(
                live=True,
                runtime_config="unused.yaml",
                repetitions=1,
                max_invocations=20,
                call_timeout_seconds=300,
                run_timeout_seconds=1800,
                keep_workspaces="failures",
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("Benchmark safety abort", output.getvalue())


class SprintBenchmarkReportTests(unittest.TestCase):
    def test_fake_worker_generates_comparable_private_full_report(self) -> None:
        seen_contexts: list[dict[str, Any]] = []

        def fake_worker(context: WorkerContext) -> WorkerOutcome:
            config = yaml.safe_load(
                (context.workspace_root / "team_runtime.yaml").read_text(encoding="utf-8")
            )
            seen_contexts.append(
                {
                    "run_id": context.arm.run_id,
                    "variant": context.arm.variant,
                    "prompt_context_enabled": config["prompt_context"]["enabled"],
                    "history_hash": canonical_hash(context.history_seed),
                    "max_invocations": context.max_invocations,
                    "call_timeout_seconds": context.call_timeout_seconds,
                    "run_timeout_seconds": context.run_timeout_seconds,
                    "live": context.live,
                }
            )
            target = context.workspace_root / "benchmark_app.py"
            self.assertIn("return sum(values)", target.read_text(encoding="utf-8"))
            target.write_text(
                '"""Small benchmark target with a repaired implementation."""\n\n'
                "\n"
                "def sum_positive(values):\n"
                '    """Return the sum of positive numeric values."""\n'
                "    return sum(value for value in values if value > 0)\n",
                encoding="utf-8",
            )
            _git(context.workspace_root, "add", "benchmark_app.py")
            _git(
                context.workspace_root,
                "commit",
                "-m",
                f"repair fixture for {context.arm.variant}",
            )
            commit_sha = _git(context.workspace_root, "rev-parse", "HEAD").stdout.strip()
            records = tuple(
                _telemetry_record(
                    variant=context.arm.variant,
                    occurrence=occurrence,
                    estimated_cost=0.0005 if context.arm.variant == "after" else 0.001,
                )
                for occurrence in (1, 2)
            )
            return WorkerOutcome(
                status="completed",
                sprint=SprintEvidence(
                    sprint_id="benchmark-sprint",
                    status="completed",
                    closeout_status="verified",
                    todo_count=1,
                    completed_todo_count=1,
                    commit_sha=commit_sha,
                ),
                quality=QualityEvidence(
                    sprint_terminal=True,
                    closeout_verified=True,
                ),
                telemetry_records=records,
                invocation_attempts={
                    "schema_version": 1,
                    "journal_available": True,
                    "journal_schema_version": 3,
                    "reconciled": True,
                    "identity_reconciled": True,
                    "context_reconciled": True,
                    "max_invocations": 20,
                    "reserved_count": 2,
                    "entry_count": 2,
                    "telemetry_record_count": 2,
                    "completed_count": 2,
                    "failed_count": 0,
                    "timeout_count": 0,
                    "launch_failed_count": 0,
                    "terminated_count": 0,
                    "active_count": 0,
                    "unknown_state_count": 0,
                    "malformed_entry_count": 0,
                    "unaccounted_count": 0,
                    "overaccounted_count": 0,
                    "rejected_count": 0,
                    "remaining_budget": 18,
                    "journal_invocation_ids_sha256": (
                        invocation_identity_digest(
                            record["invocation_id"]
                            for record in records
                        )
                    ),
                    "journal_invocation_id_missing_count": 0,
                    "journal_invocation_id_duplicate_count": 0,
                    "telemetry_invocation_id_missing_count": 0,
                    "telemetry_invocation_id_duplicate_count": 0,
                    "telemetry_invocation_id_unmatched_count": 0,
                    "journal_invocation_id_unobserved_count": 0,
                    "journal_telemetry_context_mismatch_count": 0,
                    "verified_target_projection_count": 1,
                    "verified_target_invocation_ids_sha256": (
                        invocation_identity_digest(
                            (records[0]["invocation_id"],)
                        )
                    ),
                    "prompt": "SENSITIVE_ATTEMPT_SUMMARY_SHOULD_NOT_PERSIST",
                },
                started_at="2026-07-27T00:00:00+00:00",
                ended_at="2026-07-27T00:00:02+00:00",
                wall_duration_ms=1_200 if context.arm.variant == "before" else 800,
                worker_duration_ms=1_100 if context.arm.variant == "before" else 700,
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_root = root / "source"
            runtime_config = root / "runtime.yaml"
            output_root = root / "reports"
            _initialize_source_repository(source_root)
            _write_runtime_config(runtime_config)
            options = BenchmarkOptions(
                source_root=source_root,
                runtime_config_path=runtime_config,
                output_dir=output_root,
                repetitions=1,
                max_invocations=20,
                call_timeout_seconds=300,
                run_timeout_seconds=1_800,
                keep_workspaces="none",
                live=False,
                benchmark_id="fake-worker-full-report",
            )

            result = run_sprint_ab_benchmark(options, worker=fake_worker)

            self.assertEqual(result.status, "comparable")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.classification, "preliminary_smoke")
            self.assertTrue(result.report_json.is_file())
            self.assertTrue(result.report_markdown.is_file())
            self.assertEqual(
                [(item["variant"], item["prompt_context_enabled"]) for item in seen_contexts],
                [("before", False), ("after", True)],
            )
            self.assertEqual(
                {item["history_hash"] for item in seen_contexts},
                {_HISTORY_SEED_HASH},
            )
            self.assertTrue(
                all(
                    item["max_invocations"] == 20
                    and item["call_timeout_seconds"] == 300
                    and item["run_timeout_seconds"] == 1_800
                    and item["live"] is False
                    for item in seen_contexts
                )
            )
            self.assertNotEqual(result.runs[0].config_hash, result.runs[1].config_hash)
            self.assertEqual(
                result.runs[0].comparable_config_hash,
                result.runs[1].comparable_config_hash,
            )
            self.assertTrue(all(run.quality.passed for run in result.runs))
            self.assertTrue(all(not run.retained_workspace for run in result.runs))

            report = json.loads(result.report_json.read_text(encoding="utf-8"))
            self.assertEqual(report["schema_version"], 3)
            self.assertEqual(report["provenance"]["scenario_id"], SCENARIO_ID)
            self.assertEqual(
                report["controls"]["target_invocation"],
                {
                    "attempt_kind": "primary",
                    "role": BENCHMARK_TARGET_ROLE,
                    "purpose": BENCHMARK_TARGET_PURPOSE,
                    "workflow_step": BENCHMARK_TARGET_WORKFLOW_STEP,
                },
            )
            self.assertEqual(
                report["controls"]["a_b_definition"],
                {
                    "before": {
                        "prompt_context_enabled": False,
                        "target_total_events": BENCHMARK_TARGET_TOTAL_EVENTS,
                        "target_included_events": BENCHMARK_TARGET_TOTAL_EVENTS,
                        "target_omitted_events": 0,
                    },
                    "after": {
                        "prompt_context_enabled": True,
                        "recent_events": BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
                        "max_events": BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
                        "target_total_events": BENCHMARK_TARGET_TOTAL_EVENTS,
                        "target_included_events": BENCHMARK_TARGET_INCLUDED_EVENTS,
                        "target_omitted_events": BENCHMARK_TARGET_OMITTED_EVENTS,
                    },
                },
            )
            self.assertEqual(
                report["runs"][0]["invocation_attempts"]["reserved_count"],
                2,
            )
            self.assertTrue(
                report["runs"][0]["invocation_attempts"][
                    "context_reconciled"
                ]
            )
            self.assertEqual(
                report["runs"][0]["invocation_attempts"][
                    "verified_target_projection_count"
                ],
                1,
            )
            self.assertNotIn(
                "prompt",
                report["runs"][0]["invocation_attempts"],
            )
            pair = report["pairs"][0]
            self.assertTrue(pair["comparable"])
            self.assertEqual(pair["execution_order"], ["before", "after"])
            self.assertEqual(pair["inconclusive_reasons"], [])
            self.assertEqual(
                pair["comparison"]["end_to_end"]["input_tokens"]["reduction"],
                480,
            )
            self.assertEqual(pair["comparison"]["matched_primary_count"], 2)
            self.assertEqual(
                report["aggregate_reductions"]["input_tokens"]["pair_count"],
                1,
            )
            self.assertIsNone(
                report["aggregate_reductions"]["input_tokens"][
                    "sample_standard_deviation"
                ]
            )
            self.assertFalse(
                report["interpretation"]["statistical_significance_claimed"]
            )
            self.assertIn("preliminary", report["interpretation"]["note"].lower())
            markdown = result.report_markdown.read_text(encoding="utf-8")
            self.assertIn("| Reserved | Telemetry | Completed |", markdown)
            self.assertIn(f"- Scenario: `{SCENARIO_ID}`", markdown)
            self.assertEqual(markdown.count("| 1 | 1200 | pass |"), 1)
            self.assertEqual(markdown.count("| 1 | 800 | pass |"), 1)

            for run in result.runs:
                run_dir = result.output_dir / "runs" / run.arm.run_id
                self.assertTrue((run_dir / "run.json").is_file())
                self.assertTrue((run_dir / "metrics.json").is_file())
                self.assertTrue((run_dir / "model_invocations.jsonl").is_file())
                persisted_metrics = json.loads(
                    (run_dir / "metrics.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    persisted_metrics["compaction"]["target_projection_count"],
                    1,
                )
                invocation_lines = (
                    run_dir / "model_invocations.jsonl"
                ).read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(invocation_lines), 2)
                persisted = json.loads(invocation_lines[0])
                self.assertLessEqual(set(persisted), SAFE_INVOCATION_FIELDS)
                self.assertNotIn("raw_history", persisted["prompt_context"])
                self.assertNotIn(
                    "invocations",
                    json.loads((run_dir / "run.json").read_text(encoding="utf-8")),
                )

            for artifact in result.output_dir.rglob("*"):
                if artifact.is_file():
                    content = artifact.read_text(encoding="utf-8")
                    self.assertNotIn("SENSITIVE_", content, artifact)

    def test_runner_does_not_fall_back_to_workspace_telemetry(self) -> None:
        def fake_worker(context: WorkerContext) -> WorkerOutcome:
            metrics_root = (
                context.workspace_root
                / ".teams_runtime"
                / "metrics"
                / "model_invocations"
            )
            metrics_root.mkdir(parents=True, exist_ok=True)
            forged_record = _telemetry_record(variant=context.arm.variant)
            (metrics_root / "forged.jsonl").write_text(
                json.dumps(forged_record, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return WorkerOutcome(status="completed", telemetry_records=())

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_root = root / "source"
            runtime_config = root / "runtime.yaml"
            _initialize_source_repository(source_root)
            _write_runtime_config(runtime_config)
            result = run_sprint_ab_benchmark(
                BenchmarkOptions(
                    source_root=source_root,
                    runtime_config_path=runtime_config,
                    output_dir=root / "reports",
                    repetitions=1,
                    max_invocations=20,
                    call_timeout_seconds=300,
                    run_timeout_seconds=1_800,
                    keep_workspaces="none",
                    live=False,
                    benchmark_id="workspace-telemetry-isolation",
                ),
                worker=fake_worker,
            )

        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(len(result.runs), 2)
        for run in result.runs:
            self.assertEqual(run.invocation_records, ())
            self.assertEqual(run.metrics["totals"]["invocation_count"], 0)
            self.assertEqual(
                run.metrics["compaction"]["observed_invocation_count"],
                0,
            )

    def test_untrusted_journal_never_persists_coverage_or_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_root = root / "source"
            runtime_config = root / "runtime.yaml"
            output_root = root / "reports"
            _initialize_source_repository(source_root)
            _write_runtime_config(runtime_config)

            for case_name in (
                "missing",
                "unsupported_schema",
                "unreconciled",
                "telemetry_overage",
                "duplicate_telemetry",
                "mismatched_telemetry",
            ):
                with self.subTest(case_name=case_name):

                    def fake_worker(context: WorkerContext) -> WorkerOutcome:
                        occurrences = (
                            (1, 2)
                            if case_name
                            in {
                                "telemetry_overage",
                                "duplicate_telemetry",
                            }
                            else (1,)
                        )
                        records = tuple(
                            _telemetry_record(
                                variant=context.arm.variant,
                                occurrence=occurrence,
                                estimated_cost=0.002,
                            )
                            for occurrence in occurrences
                        )
                        journal_invocation_ids = [
                            str(record["invocation_id"])
                            for record in records
                        ]
                        if case_name == "duplicate_telemetry":
                            records[1]["invocation_id"] = records[0][
                                "invocation_id"
                            ]
                        elif case_name == "mismatched_telemetry":
                            records[0]["invocation_id"] = (
                                "unmatched-telemetry-invocation"
                            )
                        reserved_count = (
                            2
                            if case_name == "duplicate_telemetry"
                            else 1
                        )
                        attempts: dict[str, Any] = {
                            "schema_version": 1,
                            "journal_available": True,
                            "journal_schema_version": 2,
                            "reconciled": True,
                            "identity_reconciled": True,
                            "max_invocations": 20,
                            "reserved_count": reserved_count,
                            "entry_count": reserved_count,
                            "telemetry_record_count": len(records),
                            "unobserved_attempt_count": 0,
                            "telemetry_overage_count": 0,
                            "completed_count": reserved_count,
                            "failed_count": 0,
                            "timeout_count": 0,
                            "launch_failed_count": 0,
                            "terminated_count": 0,
                            "active_count": 0,
                            "unknown_state_count": 0,
                            "malformed_entry_count": 0,
                            "unaccounted_count": 0,
                            "overaccounted_count": 0,
                            "rejected_count": 0,
                            "remaining_budget": 20 - reserved_count,
                            "journal_invocation_ids_sha256": (
                                invocation_identity_digest(
                                    journal_invocation_ids[
                                        :reserved_count
                                    ]
                                )
                            ),
                            "journal_invocation_id_missing_count": 0,
                            "journal_invocation_id_duplicate_count": 0,
                            "telemetry_invocation_id_missing_count": 0,
                            "telemetry_invocation_id_duplicate_count": 0,
                            "telemetry_invocation_id_unmatched_count": 0,
                            "journal_invocation_id_unobserved_count": 0,
                        }
                        if case_name == "missing":
                            attempts = {}
                        elif case_name == "unsupported_schema":
                            attempts["journal_schema_version"] = 99
                        elif case_name == "unreconciled":
                            attempts["reconciled"] = False
                        return WorkerOutcome(
                            status="completed",
                            telemetry_records=records,
                            invocation_attempts=attempts,
                        )

                    benchmark_id = f"untrusted-journal-{case_name}"
                    result = run_sprint_ab_benchmark(
                        BenchmarkOptions(
                            source_root=source_root,
                            runtime_config_path=runtime_config,
                            output_dir=output_root,
                            repetitions=1,
                            max_invocations=20,
                            call_timeout_seconds=300,
                            run_timeout_seconds=1_800,
                            keep_workspaces="none",
                            live=False,
                            benchmark_id=benchmark_id,
                        ),
                        worker=fake_worker,
                    )
                    report = json.loads(
                        result.report_json.read_text(encoding="utf-8")
                    )
                    report_runs = {
                        str(run["run_id"]): run
                        for run in report["runs"]
                    }

                    for run in result.runs:
                        persisted_metrics = json.loads(
                            (
                                result.output_dir
                                / "runs"
                                / run.arm.run_id
                                / "metrics.json"
                            ).read_text(encoding="utf-8")
                        )
                        for candidate in (
                            run.metrics,
                            persisted_metrics,
                            report_runs[run.arm.run_id]["metrics"],
                        ):
                            totals = candidate["totals"]
                            self.assertEqual(
                                totals["coverage_basis"],
                                "unavailable_untrusted_call_journal",
                            )
                            for field_name in (
                                "expected_invocation_count",
                                "token_coverage_percent",
                                "tool_call_coverage_percent",
                                "pricing_coverage_percent",
                                "estimated_cost_usd",
                            ):
                                self.assertIsNone(totals[field_name])
                            self.assertTrue(
                                all(
                                    group["estimated_cost_usd"] is None
                                    for group in candidate["groups"]
                                )
                            )
                        self.assertNotIn(
                            "telemetry_coverage_percent",
                            run.invocation_attempts,
                        )
                        self.assertNotIn(
                            "telemetry_coverage_percent",
                            report_runs[run.arm.run_id][
                                "invocation_attempts"
                            ],
                        )

    def test_missing_usage_is_inconclusive_but_missing_price_is_explicitly_unpriced(self) -> None:
        before_records = (
            _telemetry_record(variant="before", estimated_cost=0.001),
        )
        after_unpriced_records = (
            _telemetry_record(variant="after", estimated_cost=_MISSING),
        )
        unpriced_report = _report_for_runs(
            (
                _arm_result("before", before_records),
                _arm_result("after", after_unpriced_records),
            )
        )

        self.assertEqual(unpriced_report["status"], "comparable")
        self.assertEqual(
            unpriced_report["runs"][1]["metrics"]["totals"]["pricing_coverage_percent"],
            0.0,
        )
        self.assertIsNone(
            unpriced_report["runs"][1]["metrics"]["totals"]["estimated_cost_usd"]
        )
        cost_delta = unpriced_report["pairs"][0]["comparison"]["end_to_end"][
            "estimated_cost_usd"
        ]
        self.assertIsNone(cost_delta["delta"])
        self.assertIsNone(cost_delta["reduction_percent"])

        after_missing_usage = (
            _telemetry_record(
                variant="after",
                native_usage=False,
                estimated_cost=_MISSING,
            ),
        )
        missing_usage_report = _report_for_runs(
            (
                _arm_result("before", before_records),
                _arm_result("after", after_missing_usage),
            )
        )
        self.assertEqual(missing_usage_report["status"], "inconclusive")
        self.assertFalse(missing_usage_report["pairs"][0]["comparable"])
        self.assertIn(
            "after_native_token_coverage_incomplete",
            missing_usage_report["pairs"][0]["inconclusive_reasons"],
        )

    def test_reducer_reports_partial_coverage_without_inventing_cost_or_usage(self) -> None:
        native_priced = _telemetry_record(
            variant="before",
            occurrence=1,
            estimated_cost=0.002,
        )
        missing = _telemetry_record(
            variant="before",
            occurrence=2,
            native_usage=False,
            estimated_cost=_MISSING,
        )
        for field in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        ):
            missing.pop(field)

        metrics = reduce_telemetry((native_priced, missing))

        self.assertEqual(metrics["totals"]["invocation_count"], 2)
        self.assertEqual(metrics["totals"]["token_coverage_percent"], 50.0)
        self.assertEqual(metrics["totals"]["pricing_coverage_percent"], 50.0)
        self.assertIsNone(metrics["totals"]["estimated_cost_usd"])
        self.assertEqual(metrics["tokens"]["input"], native_priced["input_tokens"])
        self.assertEqual(
            {group["estimated_cost_usd"] for group in metrics["groups"]},
            {None, 0.002},
        )

    def test_reserved_attempt_without_telemetry_keeps_cost_and_usage_incomplete(
        self,
    ) -> None:
        observed = _telemetry_record(
            variant="after",
            estimated_cost=0.002,
        )

        metrics = reduce_telemetry(
            (observed,),
            expected_invocation_count=2,
        )

        self.assertEqual(metrics["totals"]["invocation_count"], 1)
        self.assertEqual(metrics["totals"]["expected_invocation_count"], 2)
        self.assertEqual(metrics["totals"]["unobserved_invocation_count"], 1)
        self.assertEqual(metrics["totals"]["token_coverage_percent"], 50.0)
        self.assertEqual(metrics["totals"]["pricing_coverage_percent"], 50.0)
        self.assertIsNone(metrics["totals"]["estimated_cost_usd"])
        self.assertTrue(
            all(
                group["estimated_cost_usd"] is None
                for group in metrics["groups"]
            )
        )
        self.assertEqual(
            metrics["compaction"]["unobserved_invocation_count"],
            1,
        )

    def test_missing_call_journal_keeps_coverage_and_cost_unknown(self) -> None:
        observed = _telemetry_record(
            variant="after",
            estimated_cost=0.002,
        )

        metrics = reduce_telemetry(
            (observed,),
            coverage_available=False,
        )

        totals = metrics["totals"]
        self.assertEqual(
            totals["coverage_basis"],
            "unavailable_untrusted_call_journal",
        )
        self.assertIsNone(totals["expected_invocation_count"])
        self.assertIsNone(totals["unobserved_invocation_count"])
        self.assertIsNone(totals["token_coverage_percent"])
        self.assertIsNone(totals["tool_call_coverage_percent"])
        self.assertIsNone(totals["pricing_coverage_percent"])
        self.assertIsNone(totals["estimated_cost_usd"])
        self.assertIsNone(
            metrics["compaction"]["unobserved_invocation_count"]
        )
        self.assertTrue(
            all(
                group["estimated_cost_usd"] is None
                for group in metrics["groups"]
            )
        )

    def test_coverage_requires_a_supported_reconciled_call_journal(self) -> None:
        trusted = {
            "schema_version": 1,
            "journal_available": True,
            "journal_schema_version": 2,
            "reconciled": True,
            "identity_reconciled": True,
            "max_invocations": 20,
            "reserved_count": 2,
            "entry_count": 2,
            "telemetry_record_count": 1,
            "unobserved_attempt_count": 1,
            "telemetry_overage_count": 0,
            "completed_count": 1,
            "failed_count": 0,
            "timeout_count": 0,
            "launch_failed_count": 0,
            "terminated_count": 1,
            "active_count": 0,
            "unknown_state_count": 0,
            "malformed_entry_count": 0,
            "unaccounted_count": 0,
            "overaccounted_count": 0,
            "rejected_count": 0,
            "remaining_budget": 18,
            "journal_invocation_ids_sha256": (
                invocation_identity_digest(("invocation-1", "invocation-2"))
            ),
            "journal_invocation_id_missing_count": 0,
            "journal_invocation_id_duplicate_count": 0,
            "telemetry_invocation_id_missing_count": 0,
            "telemetry_invocation_id_duplicate_count": 0,
            "telemetry_invocation_id_unmatched_count": 0,
            "journal_invocation_id_unobserved_count": 1,
        }

        self.assertTrue(
            _journal_coverage_available(
                trusted,
                expected_max_invocations=20,
            )
        )
        invalid_cases = {
            "missing": {},
            "unsupported_summary_schema": {
                **trusted,
                "schema_version": 2,
            },
            "unsupported_journal_schema": {
                **trusted,
                "journal_schema_version": 99,
            },
            "unreconciled": {
                **trusted,
                "reconciled": False,
            },
            "identity_unreconciled": {
                **trusted,
                "identity_reconciled": False,
            },
            "missing_count": {
                key: value
                for key, value in trusted.items()
                if key != "remaining_budget"
            },
            "maximum_mismatch": {
                **trusted,
                "max_invocations": 19,
                "remaining_budget": 17,
            },
            "state_count_mismatch": {
                **trusted,
                "completed_count": 0,
            },
            "telemetry_overage": {
                **trusted,
                "telemetry_record_count": 3,
                "unobserved_attempt_count": 0,
                "telemetry_overage_count": 1,
            },
            "duplicate_telemetry_identity": {
                **trusted,
                "telemetry_invocation_id_duplicate_count": 1,
            },
            "unmatched_telemetry_identity": {
                **trusted,
                "telemetry_invocation_id_unmatched_count": 1,
            },
            "unobserved_identity_mismatch": {
                **trusted,
                "journal_invocation_id_unobserved_count": 0,
            },
        }
        for label, attempts in invalid_cases.items():
            with self.subTest(label=label):
                self.assertFalse(
                    _journal_coverage_available(
                        attempts,
                        expected_max_invocations=20,
                    )
                )

        trusted_v3 = {
            **trusted,
            "journal_schema_version": 3,
            "context_reconciled": True,
            "journal_telemetry_context_mismatch_count": 0,
            "verified_target_projection_count": 1,
            "verified_target_invocation_ids_sha256": (
                invocation_identity_digest(("invocation-1",))
            ),
        }
        self.assertTrue(
            _journal_coverage_available(
                trusted_v3,
                expected_max_invocations=20,
            )
        )
        self.assertFalse(
            _journal_coverage_available(
                {**trusted_v3, "context_reconciled": False},
                expected_max_invocations=20,
            )
        )

    def test_unobserved_terminated_attempt_makes_pair_inconclusive(self) -> None:
        before = _arm_result(
            "before",
            (_telemetry_record(variant="before"),),
        )
        after = replace(
            _arm_result(
                "after",
                (_telemetry_record(variant="after"),),
            ),
            invocation_attempts={
                "journal_available": True,
                "journal_schema_version": 2,
                "reconciled": True,
                "reserved_count": 2,
                "entry_count": 2,
                "telemetry_record_count": 1,
                "unobserved_attempt_count": 1,
                "completed_count": 1,
                "terminated_count": 1,
                "active_count": 0,
                "unknown_state_count": 0,
                "malformed_entry_count": 0,
                "unaccounted_count": 0,
                "overaccounted_count": 0,
            },
        )

        report = _report_for_runs((before, after))

        self.assertEqual(report["status"], "inconclusive")
        reasons = report["pairs"][0]["inconclusive_reasons"]
        self.assertIn("after_unobserved_attempts", reasons)
        self.assertIn("after_terminated_attempts", reasons)

    def test_missing_call_journal_makes_pair_inconclusive(self) -> None:
        before = _arm_result(
            "before",
            (_telemetry_record(variant="before"),),
        )
        after = replace(
            _arm_result(
                "after",
                (_telemetry_record(variant="after"),),
            ),
            invocation_attempts={},
        )

        report = _report_for_runs((before, after))

        self.assertEqual(report["status"], "inconclusive")
        self.assertIn(
            "after_call_journal_missing",
            report["pairs"][0]["inconclusive_reasons"],
        )

    def test_native_usage_requires_complete_consistent_provider_counts(self) -> None:
        complete = _telemetry_record(variant="before")
        missing_output = _telemetry_record(variant="before")
        missing_output.pop("output_tokens")
        inconsistent_total = _telemetry_record(variant="before")
        inconsistent_total["total_tokens"] = (
            inconsistent_total["input_tokens"]
            + inconsistent_total["output_tokens"]
            - 1
        )
        derived_total = _telemetry_record(variant="before")
        derived_total.pop("total_tokens")

        cases = (
            ("complete", complete, 100.0),
            ("missing_output", missing_output, 0.0),
            ("inconsistent_total", inconsistent_total, 0.0),
            ("derived_total", derived_total, 100.0),
        )
        for label, record, expected_coverage in cases:
            with self.subTest(label=label):
                metrics = reduce_telemetry((record,))
                self.assertEqual(
                    metrics["totals"]["token_coverage_percent"],
                    expected_coverage,
                )
        self.assertEqual(
            reduce_telemetry((derived_total,))["tokens"]["total"],
            derived_total["input_tokens"] + derived_total["output_tokens"],
        )

    def test_v2_target_projection_requires_a_completed_primary_attempt(self) -> None:
        repair = _telemetry_record(variant="after", occurrence=1)
        repair["attempt_kind"] = "contract_repair"
        failed_primary = _telemetry_record(variant="after", occurrence=1)
        failed_primary["invocation_id"] = "after-failed-target"
        failed_primary["status"] = "failed"
        unrelated_primary = _telemetry_record(variant="after", occurrence=2)

        metrics = reduce_telemetry(
            (repair, failed_primary, unrelated_primary),
            verified_target_projection_count=1,
        )

        self.assertEqual(metrics["compaction"]["observed_invocation_count"], 3)
        self.assertEqual(metrics["compaction"]["compacted_invocation_count"], 3)
        self.assertEqual(
            metrics["compaction"]["target_projection_candidate_count"],
            0,
        )
        self.assertEqual(metrics["compaction"]["target_projection_count"], 0)
        self.assertEqual(
            metrics["compaction"][
                "target_projection_verification_mismatch_count"
            ],
            1,
        )

    def test_v2_target_projection_requires_verified_identity_digest(self) -> None:
        record = _telemetry_record(variant="after")
        cases = {
            "missing": "",
            "wrong": invocation_identity_digest(("different-invocation",)),
        }

        for label, digest in cases.items():
            with self.subTest(label=label):
                metrics = reduce_telemetry(
                    (record,),
                    verified_target_projection_count=1,
                    verified_target_invocation_ids_sha256=digest,
                )
                compaction = metrics["compaction"]
                self.assertEqual(
                    compaction["target_projection_candidate_count"],
                    1,
                )
                self.assertEqual(
                    compaction[
                        "target_projection_verification_mismatch_count"
                    ],
                    0,
                )
                self.assertFalse(
                    compaction["target_projection_identity_reconciled"]
                )
                self.assertEqual(compaction["target_projection_count"], 0)

    def test_v2_target_projection_identity_digest_is_order_independent(self) -> None:
        first = _telemetry_record(variant="after", occurrence=1)
        second = _telemetry_record(variant="after", occurrence=2)
        second.update(
            {
                "role": BENCHMARK_TARGET_ROLE,
                "purpose": BENCHMARK_TARGET_PURPOSE,
                "workflow_step": BENCHMARK_TARGET_WORKFLOW_STEP,
            }
        )
        reversed_ids = (
            second["invocation_id"],
            first["invocation_id"],
        )

        metrics = reduce_telemetry(
            (first, second),
            verified_target_projection_count=2,
            verified_target_invocation_ids_sha256=(
                invocation_identity_digest(reversed_ids)
            ),
        )

        compaction = metrics["compaction"]
        self.assertTrue(compaction["target_projection_identity_reconciled"])
        self.assertEqual(compaction["target_projection_count"], 2)

    def test_v2_target_projection_rejects_cross_invocation_substitution(self) -> None:
        telemetry_target = _telemetry_record(variant="after")
        telemetry_target["invocation_id"] = "telemetry-target"

        metrics = reduce_telemetry(
            (telemetry_target,),
            verified_target_projection_count=1,
            verified_target_invocation_ids_sha256=(
                invocation_identity_digest(("journal-verified-target",))
            ),
        )

        compaction = metrics["compaction"]
        self.assertEqual(compaction["target_projection_candidate_count"], 1)
        self.assertEqual(
            compaction["target_projection_verification_mismatch_count"],
            0,
        )
        self.assertFalse(compaction["target_projection_identity_reconciled"])
        self.assertEqual(compaction["target_projection_count"], 0)

    def test_exact_prompt_context_counts_require_json_integers(self) -> None:
        for label, invalid_value in (
            ("fractional", BENCHMARK_TARGET_TOTAL_EVENTS + 0.9),
            ("numeric_string", str(BENCHMARK_TARGET_TOTAL_EVENTS)),
        ):
            with self.subTest(label=label):
                journal_entry = _telemetry_record(variant="after")
                journal_entry["state"] = "completed"
                telemetry_record = _telemetry_record(variant="after")
                telemetry_record["prompt_context_total_events"] = invalid_value
                telemetry_record["prompt_context"][
                    "total_events"
                ] = invalid_value
                snapshot = {
                    "schema_version": 3,
                    "max_invocations": 1,
                    "reserved_count": 1,
                    "remaining": 0,
                    "entries": [journal_entry],
                }

                sanitized = sanitize_invocation_record(telemetry_record)
                summary = _summarize_call_journal(
                    snapshot,
                    telemetry_records=(telemetry_record,),
                )
                metrics = reduce_telemetry(
                    (telemetry_record,),
                    verified_target_projection_count=1,
                    verified_target_invocation_ids_sha256=(
                        invocation_identity_digest(
                            (telemetry_record["invocation_id"],)
                        )
                    ),
                )

                self.assertIsNone(
                    sanitized["prompt_context_total_events"]
                )
                self.assertNotIn(
                    "total_events",
                    sanitized["prompt_context"],
                )
                self.assertFalse(summary["context_reconciled"])
                self.assertEqual(
                    summary["journal_telemetry_context_mismatch_count"],
                    1,
                )
                self.assertEqual(
                    summary["verified_target_projection_count"],
                    0,
                )
                self.assertEqual(
                    metrics["compaction"]["invalid_projection_count"],
                    1,
                )
                self.assertEqual(
                    metrics["compaction"][
                        "target_projection_candidate_count"
                    ],
                    0,
                )
                self.assertEqual(
                    metrics["compaction"]["target_projection_count"],
                    0,
                )

    def test_pair_requires_exact_v2_target_projection_in_each_arm(self) -> None:
        wrong_before = _telemetry_record(variant="before")
        wrong_before["prompt_context"].update(
            {
                "total_events": BENCHMARK_TARGET_TOTAL_EVENTS - 1,
                "included_events": BENCHMARK_TARGET_TOTAL_EVENTS - 1,
                "omitted_events": 0,
            }
        )
        wrong_before.update(
            {
                "prompt_context_total_events": BENCHMARK_TARGET_TOTAL_EVENTS - 1,
                "prompt_context_included_events": (
                    BENCHMARK_TARGET_TOTAL_EVENTS - 1
                ),
                "prompt_context_omitted_events": 0,
            }
        )
        wrong_after = _telemetry_record(variant="after")
        wrong_after["prompt_context"].update(
            {
                "total_events": BENCHMARK_TARGET_TOTAL_EVENTS + 1,
                "included_events": BENCHMARK_TARGET_INCLUDED_EVENTS,
                "omitted_events": BENCHMARK_TARGET_OMITTED_EVENTS + 1,
            }
        )
        wrong_after.update(
            {
                "prompt_context_total_events": BENCHMARK_TARGET_TOTAL_EVENTS + 1,
                "prompt_context_included_events": BENCHMARK_TARGET_INCLUDED_EVENTS,
                "prompt_context_omitted_events": BENCHMARK_TARGET_OMITTED_EVENTS + 1,
            }
        )
        cases = (
            (
                _arm_result("before", (wrong_before,)),
                _arm_result("after", (_telemetry_record(variant="after"),)),
                "before_v2_target_projection_not_observed",
            ),
            (
                _arm_result("before", (_telemetry_record(variant="before"),)),
                _arm_result("after", (wrong_after,)),
                "after_v2_target_projection_not_observed",
            ),
        )

        for before, after, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                report = _report_for_runs((before, after))

                self.assertEqual(report["status"], "inconclusive")
                reasons = report["pairs"][0]["inconclusive_reasons"]
                self.assertIn(expected_reason, reasons)
                self.assertNotIn("after_compaction_not_observed", reasons)

    def test_invalid_compaction_projection_cannot_make_a_pair_comparable(self) -> None:
        invalid_cases: dict[str, dict[str, Any]] = {}
        inconsistent_total = _telemetry_record(variant="after")
        inconsistent_total["prompt_context"]["total_events"] = 49
        invalid_cases["inconsistent_total"] = inconsistent_total
        below_recent_tail = _telemetry_record(variant="after")
        below_recent_tail["prompt_context"].update(
            {"included_events": 7, "omitted_events": 43}
        )
        invalid_cases["below_recent_tail"] = below_recent_tail
        wrong_recent_limit = _telemetry_record(variant="after")
        wrong_recent_limit["prompt_context"]["recent_events"] = 7
        invalid_cases["wrong_recent_limit"] = wrong_recent_limit
        wrong_max_limit = _telemetry_record(variant="after")
        wrong_max_limit["prompt_context"]["max_events"] = 17
        invalid_cases["wrong_max_limit"] = wrong_max_limit

        for label, invalid_after in invalid_cases.items():
            with self.subTest(label=label):
                compaction = reduce_telemetry((invalid_after,))["compaction"]
                self.assertEqual(compaction["observed_invocation_count"], 1)
                self.assertEqual(compaction["invalid_projection_count"], 1)
                self.assertEqual(compaction["compacted_invocation_count"], 0)
                self.assertEqual(compaction["omitted_events"], 0)

        report = _report_for_runs(
            (
                _arm_result(
                    "before",
                    (_telemetry_record(variant="before"),),
                ),
                _arm_result("after", (inconsistent_total,)),
            )
        )
        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["pairs"][0]["comparable"])
        self.assertIn(
            "after_compaction_not_observed",
            report["pairs"][0]["inconclusive_reasons"],
        )

    def test_after_arm_rejects_mixed_enabled_and_disabled_projections(self) -> None:
        enabled = _telemetry_record(variant="after", occurrence=1)
        disabled_short_history = _telemetry_record(
            variant="after",
            occurrence=2,
            compacted=False,
        )
        disabled_short_history["prompt_context"].update(
            {
                "enabled": False,
                "total_events": 4,
                "included_events": 4,
                "omitted_events": 0,
            }
        )
        disabled_short_history.update(
            {
                "prompt_context_enabled": False,
                "prompt_context_total_events": 4,
                "prompt_context_included_events": 4,
                "prompt_context_omitted_events": 0,
            }
        )

        report = _report_for_runs(
            (
                _arm_result(
                    "before",
                    (_telemetry_record(variant="before"),),
                ),
                _arm_result(
                    "after",
                    (enabled, disabled_short_history),
                ),
            )
        )

        self.assertEqual(report["status"], "inconclusive")
        reasons = report["pairs"][0]["inconclusive_reasons"]
        self.assertIn(
            "after_prompt_projection_not_uniformly_enabled",
            reasons,
        )
        self.assertNotIn("after_disabled_projection_observed", reasons)

    def test_repeated_primary_groups_are_left_unmatched_as_ambiguous(self) -> None:
        before_records = [
            _telemetry_record(variant="before", occurrence=index)
            for index in (1, 2)
        ]
        after_records = [
            _telemetry_record(variant="after", occurrence=index)
            for index in (1, 2)
        ]
        for record in (*before_records, *after_records):
            record.update(
                {
                    "role": "developer",
                    "purpose": "implement",
                    "workflow_step": "todo_execution",
                }
            )

        comparison = compare_metrics(
            reduce_telemetry(before_records),
            reduce_telemetry(after_records),
            before_wall_duration_ms=1_000,
            after_wall_duration_ms=900,
            before_records=before_records,
            after_records=after_records,
        )

        self.assertEqual(comparison["matched_primary_count"], 0)
        self.assertEqual(comparison["unmatched_before_primary_count"], 2)
        self.assertEqual(comparison["unmatched_after_primary_count"], 2)
        self.assertEqual(comparison["ambiguous_primary_group_count"], 1)

    def test_sanitizer_drops_prompt_response_secrets_and_nested_history(self) -> None:
        sanitized = sanitize_invocation_record(_telemetry_record(variant="after"))

        self.assertLessEqual(set(sanitized), SAFE_INVOCATION_FIELDS)
        self.assertNotIn("prompt", sanitized)
        self.assertNotIn("response", sanitized)
        self.assertNotIn("api_key", sanitized)
        self.assertNotIn("session_id", sanitized)
        self.assertNotIn("raw_history", sanitized["prompt_context"])
        self.assertEqual(
            sanitized["prompt_context"]["omitted_events"],
            BENCHMARK_TARGET_OMITTED_EVENTS,
        )

    def test_divergent_flat_and_nested_prompt_context_fails_closed(self) -> None:
        record = _telemetry_record(variant="before")
        record.update(
            {
                "prompt_context_enabled": True,
                "prompt_context_total_events": BENCHMARK_TARGET_TOTAL_EVENTS,
                "prompt_context_included_events": BENCHMARK_TARGET_INCLUDED_EVENTS,
                "prompt_context_omitted_events": BENCHMARK_TARGET_OMITTED_EVENTS,
                "prompt_context_recent_events": BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
                "prompt_context_max_events": BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
                "prompt_context_selection_policy": PROMPT_EVENT_SELECTION_POLICY,
            }
        )

        sanitized = sanitize_invocation_record(record)
        metrics = reduce_telemetry(
            (record,),
            verified_target_projection_count=1,
        )

        self.assertTrue(
            sanitized["prompt_context_representation_conflict"]
        )
        self.assertEqual(metrics["compaction"]["invalid_projection_count"], 1)
        self.assertEqual(
            metrics["compaction"]["target_projection_candidate_count"],
            0,
        )
        self.assertEqual(metrics["compaction"]["target_projection_count"], 0)
        self.assertEqual(
            metrics["compaction"][
                "target_projection_verification_mismatch_count"
            ],
            1,
        )
        report = _report_for_runs(
            (
                replace(
                    _arm_result("before", (record,)),
                    metrics=metrics,
                ),
                _arm_result(
                    "after",
                    (_telemetry_record(variant="after"),),
                ),
            )
        )

        self.assertEqual(report["status"], "inconclusive")
        self.assertIn(
            "before_v2_target_projection_not_reconciled",
            report["pairs"][0]["inconclusive_reasons"],
        )

    def test_sanitizer_allowlists_nested_rate_card_fields(self) -> None:
        record = _telemetry_record(variant="after")
        record["rate_card"] = {
            "input_per_million_usd": 1.25,
            "cached_input_per_million_usd": 0.25,
            "output_per_million_usd": 2.5,
            "per_invocation_usd": None,
            "api_key": "SENSITIVE_RATE_CARD_SECRET",
            "metadata": {"authorization": "SENSITIVE_NESTED_SECRET"},
        }

        sanitized = sanitize_invocation_record(record)

        self.assertEqual(
            sanitized["rate_card"],
            {
                "input_per_million_usd": 1.25,
                "cached_input_per_million_usd": 0.25,
                "output_per_million_usd": 2.5,
                "per_invocation_usd": None,
            },
        )
        self.assertNotIn(
            "SENSITIVE_",
            json.dumps(sanitized["rate_card"], sort_keys=True),
        )


class SprintBenchmarkExecutionSafetyTests(unittest.TestCase):
    def test_missing_auth_live_worker_preflight_reserves_no_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            scenario = create_scenario_workspace(
                root / "workspace",
                benchmark_id="missing-auth-boundary",
                run_id="pair-001-before",
                prompt_context_enabled=False,
                settings=_settings(),
            )
            run_output = root / "run-output"
            run_output.mkdir()
            context = WorkerContext(
                benchmark_id="missing-auth-boundary",
                arm=ArmPlan(
                    pair_index=1,
                    order_index=1,
                    variant="before",
                    run_id="pair-001-before",
                    prompt_context_enabled=False,
                ),
                workspace_root=scenario.root,
                run_output_dir=run_output,
                milestone=SCENARIO_MILESTONE,
                history_seed=scenario.history_seed,
                max_invocations=2,
                call_timeout_seconds=5,
                run_timeout_seconds=10,
                live=True,
            )

            with mock.patch.dict(
                os.environ,
                {LIVE_BENCHMARK_ENV: "1", "PATH": os.defpath},
                clear=True,
            ):
                outcome = run_live_sprint_arm(context)

            journal = json.loads(
                (run_output / "call_journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual(outcome.status, "preflight_failed")
            self.assertEqual(journal["schema_version"], 3)
            self.assertEqual(journal["reserved_count"], 0)
            self.assertEqual(journal["entries"], [])
            self.assertEqual(outcome.invocation_attempts["reserved_count"], 0)
            self.assertEqual(outcome.telemetry_records, ())
            self.assertFalse(
                (run_output / ".private_model_invocations").exists()
            )

    def test_live_policy_requires_provider_only_auth_before_reserving_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            context = mock.Mock(
                workspace_root=root / "workspace",
                run_output_dir=root / "run-output",
                call_timeout_seconds=30,
            )
            budget = InvocationBudget(1)

            with mock.patch.dict(os.environ, {"PATH": os.defpath}, clear=True):
                with self.assertRaisesRegex(
                    ModelExecutionPolicyViolation,
                    "CODEX_API_KEY or OPENAI_API_KEY",
                ):
                    _build_execution_policy(context, budget=budget)

            self.assertEqual(budget.reserved_count, 0)

            with mock.patch.dict(
                os.environ,
                {
                    "CODEX_API_KEY": "provider-only-secret",
                    "PATH": str(Path(sys.executable).resolve().parent),
                },
                clear=True,
            ), mock.patch.object(
                shutil,
                "which",
                return_value=sys.executable,
            ):
                policy = _build_execution_policy(context, budget=budget)

            self.assertNotIn("CODEX_API_KEY", policy.shell_environment)
            self.assertEqual(
                policy.codex_executable,
                Path(sys.executable).resolve(),
            )

    def test_invocation_budget_rejects_the_twenty_first_call_and_journals_no_content(self) -> None:
        class InvocationContext:
            invocation_id = "safe-invocation-id"
            operation_id = "safe-operation-id"
            logical_call_id = "safe-logical-id"
            attempt_index = 1
            attempt_kind = "primary"
            role = "developer"
            purpose = "implementation"
            workflow_step = "todo_execution"
            prompt = "SENSITIVE_BUDGET_PROMPT"

        with tempfile.TemporaryDirectory() as temporary_directory:
            journal = Path(temporary_directory) / "budget" / "journal.json"
            budget = InvocationBudget(20, journal_path=journal)
            reservations = [
                budget.reserve(InvocationContext(), provider="codex_cli")
                for _ in range(20)
            ]

            self.assertEqual(len({item.reservation_id for item in reservations}), 20)
            self.assertEqual(budget.reserved_count, 20)
            self.assertEqual(budget.remaining, 0)
            with self.assertRaises(InvocationBudgetExceeded) as raised:
                budget.reserve(InvocationContext(), provider="codex_cli")
            self.assertEqual(raised.exception.max_invocations, 20)
            self.assertEqual(raised.exception.reserved_count, 20)
            self.assertEqual(budget.rejected_count, 1)

            persisted_text = journal.read_text(encoding="utf-8")
            persisted = json.loads(persisted_text)
            self.assertEqual(persisted["reserved_count"], 20)
            self.assertEqual(persisted["rejected_count"], 1)
            self.assertEqual(len(persisted["entries"]), 20)
            self.assertNotIn("SENSITIVE_BUDGET_PROMPT", persisted_text)
            if os.name == "posix":
                self.assertEqual(
                    stat.S_IMODE(journal.stat().st_mode),
                    0o600,
                )

    def test_execution_policy_rejects_secret_environment_and_workspace_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            allowed = root / "allowed"
            outside = root / "outside"
            allowed.mkdir()
            outside.mkdir()
            budget = InvocationBudget(20)

            for name in ("OPENAI_API_KEY", "GH_TOKEN", "DATABASE_PASSWORD"):
                with self.subTest(name=name):
                    with self.assertRaisesRegex(ValueError, "secret-bearing"):
                        ModelExecutionPolicy.for_benchmark(
                            allowed_workspace_root=allowed,
                            invocation_budget=budget,
                            call_timeout_seconds=30,
                            codex_executable=sys.executable,
                            shell_environment={name: "must-not-leak"},
                        )

            policy = ModelExecutionPolicy.for_benchmark(
                allowed_workspace_root=allowed,
                invocation_budget=budget,
                call_timeout_seconds=30,
                codex_executable=sys.executable,
                shell_environment={"PYTHONPATH": str(allowed)},
            )
            nested = allowed / "nested"
            nested.mkdir()
            policy.assert_workspace_allowed(nested)
            with self.assertRaises(ModelExecutionPolicyViolation):
                policy.assert_workspace_allowed(outside)
            with self.assertRaises(ModelExecutionPolicyViolation):
                policy.assert_workspace_allowed(allowed / ".." / "outside")
            if hasattr(os, "symlink"):
                escape_link = allowed / "escape-link"
                escape_link.symlink_to(outside, target_is_directory=True)
                with self.assertRaises(ModelExecutionPolicyViolation):
                    policy.assert_workspace_allowed(escape_link)

    def test_retained_workspaces_are_allowlisted_sanitized_snapshots(self) -> None:
        sensitive_marker = "SENSITIVE_RAW_PROVIDER_AND_SESSION_STATE"
        expected_files = {
            ".benchmark/history_seed.json",
            ".benchmark/scenario.json",
            "BENCHMARK_TASK.md",
            "RETENTION_NOTICE.md",
            "benchmark_app.baseline.py",
            "benchmark_app.result.json",
            "team_runtime.yaml",
            "tests/__init__.py",
            "tests/test_benchmark_app.py",
        }

        def failing_worker(context: WorkerContext) -> WorkerOutcome:
            (context.workspace_root / ".teams_runtime_codex_output.txt").write_text(
                sensitive_marker,
                encoding="utf-8",
            )
            session_file = (
                context.workspace_root
                / ".teams_runtime"
                / "role_sessions"
                / "developer.json"
            )
            session_file.parent.mkdir(parents=True, exist_ok=True)
            session_file.write_text(
                json.dumps({"session_id": sensitive_marker}),
                encoding="utf-8",
            )
            log_file = context.workspace_root / "logs" / "provider.log"
            log_file.parent.mkdir(parents=True, exist_ok=True)
            log_file.write_text(sensitive_marker, encoding="utf-8")
            (context.workspace_root / "unknown-model-note.txt").write_text(
                sensitive_marker,
                encoding="utf-8",
            )
            for relative_name in (
                ".benchmark/history_seed.json",
                ".benchmark/scenario.json",
                "BENCHMARK_TASK.md",
                "team_runtime.yaml",
            ):
                (context.workspace_root / relative_name).write_text(
                    sensitive_marker,
                    encoding="utf-8",
                )
            shutil.rmtree(context.workspace_root / "tests")
            redirected_tests = (
                context.workspace_root / ".teams_runtime" / "redirected-tests"
            )
            redirected_tests.mkdir(parents=True)
            for filename in ("__init__.py", "test_benchmark_app.py"):
                (redirected_tests / filename).write_text(
                    f"# {sensitive_marker}\n",
                    encoding="utf-8",
                )
            (context.workspace_root / "tests").symlink_to(
                redirected_tests,
                target_is_directory=True,
            )
            (context.workspace_root / "benchmark_app.py").unlink()
            (context.workspace_root / "benchmark_app.py").symlink_to(
                session_file,
            )
            return WorkerOutcome(
                status="failed",
                stop_reason="fixture_failure",
                error_category="FixtureFailure",
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_root = root / "source"
            runtime_config = root / "runtime.yaml"
            _initialize_source_repository(source_root)
            _write_runtime_config(runtime_config)
            result = run_sprint_ab_benchmark(
                BenchmarkOptions(
                    source_root=source_root,
                    runtime_config_path=runtime_config,
                    output_dir=root / "reports",
                    repetitions=1,
                    keep_workspaces="failures",
                    benchmark_id="sanitized-retention",
                ),
                worker=failing_worker,
            )

            self.assertEqual(len(result.runs), 2)
            for run in result.runs:
                retained_root = result.output_dir / run.retained_workspace
                retained_files = {
                    path.relative_to(retained_root).as_posix()
                    for path in retained_root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(retained_files, expected_files)
                self.assertFalse((retained_root / ".git").exists())
                self.assertFalse((retained_root / ".teams_runtime").exists())
                self.assertFalse((retained_root / "logs").exists())
                self.assertIn(
                    "return sum(values)",
                    (retained_root / "benchmark_app.baseline.py").read_text(
                        encoding="utf-8"
                    ),
                )
                self.assertEqual(
                    json.loads(
                        (retained_root / "benchmark_app.result.json").read_text(
                            encoding="utf-8"
                        )
                    )["status"],
                    "missing_or_unsafe",
                )
                self.assertIn(
                    "allowlisted diagnostic snapshot",
                    (retained_root / "RETENTION_NOTICE.md").read_text(
                        encoding="utf-8"
                    ),
                )

            for artifact in result.output_dir.rglob("*"):
                if artifact.is_file():
                    self.assertNotIn(
                        sensitive_marker,
                        artifact.read_text(encoding="utf-8"),
                        artifact,
                    )

    def test_preflight_failure_stops_all_subsequent_arms(self) -> None:
        seen_variants: list[str] = []

        def preflight_failure(context: WorkerContext) -> WorkerOutcome:
            seen_variants.append(context.arm.variant)
            return WorkerOutcome(
                status="preflight_failed",
                stop_reason="provider_preflight_failed",
                error_category="BenchmarkPreflightError",
            )

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_root = root / "source"
            runtime_config = root / "runtime.yaml"
            _initialize_source_repository(source_root)
            _write_runtime_config(runtime_config)
            result = run_sprint_ab_benchmark(
                BenchmarkOptions(
                    source_root=source_root,
                    runtime_config_path=runtime_config,
                    output_dir=root / "reports",
                    repetitions=3,
                    keep_workspaces="none",
                    benchmark_id="preflight-short-circuit",
                ),
                worker=preflight_failure,
            )
            report_json_exists = result.report_json.is_file()
            report_markdown_exists = result.report_markdown.is_file()

        self.assertEqual(seen_variants, ["before"])
        self.assertEqual(len(result.runs), 1)
        self.assertEqual(result.runs[0].status, "preflight_failed")
        self.assertEqual(result.status, "inconclusive")
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(report_json_exists)
        self.assertTrue(report_markdown_exists)
        self.assertEqual(
            result.report["pairs"][0]["inconclusive_reasons"],
            ["missing_arm"],
        )


class SprintBenchmarkLifecycleTests(unittest.TestCase):
    def test_initial_plan_auto_confirmation_preserves_draft_and_records_actor(self) -> None:
        draft = {
            "revision": 3,
            "milestone_title": "Fix deterministic fixture",
            "plan_actions": [{"plan_action_id": "PLAN-001", "title": "Repair defect"}],
        }
        state = {
            "initial_plan_confirmation": {
                "status": "pending",
                "revision": 3,
                "draft_proposal": draft,
                "plan_artifact": "shared_workspace/sprints/test/implementation_plan.md",
                "created_at": "2026-07-27T00:00:00+00:00",
            }
        }
        actor = {
            "type": "benchmark_auto_approval",
            "id": "sprint-ab-harness",
            "name": "Sprint A/B harness",
        }

        confirmation = apply_initial_plan_confirmation(
            state,
            confirmed_by=actor,
            message_id="benchmark-auto-confirm",
            parser_reason="isolated benchmark policy",
            parser_confidence="high",
            confirmed_at="2026-07-27T00:01:00+00:00",
        )

        self.assertIs(confirmation, state["initial_plan_confirmation"])
        self.assertEqual(confirmation["status"], "confirmed")
        self.assertEqual(confirmation["confirmed_at"], "2026-07-27T00:01:00+00:00")
        self.assertEqual(confirmation["updated_at"], "2026-07-27T00:01:00+00:00")
        self.assertEqual(confirmation["confirmed_by"], actor)
        self.assertEqual(confirmation["confirmed_message_id"], "benchmark-auto-confirm")
        self.assertEqual(confirmation["parser_reason"], "isolated benchmark policy")
        self.assertEqual(confirmation["parser_confidence"], "high")
        self.assertEqual(confirmation["draft_proposal"], draft)
        with self.assertRaisesRegex(ValueError, "not awaiting confirmation"):
            apply_initial_plan_confirmation(state, confirmed_by=actor)


if __name__ == "__main__":
    unittest.main()
