from __future__ import annotations

import json
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest import mock

import yaml

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
    QualityEvidence,
    SprintEvidence,
    WorkerContext,
    WorkerOutcome,
    make_arm_schedule,
)
from teams_runtime.benchmarking.reporting import build_report
from teams_runtime.benchmarking.runner import run_sprint_ab_benchmark
from teams_runtime.benchmarking.scenario import (
    PROTECTED_PATHS,
    RuntimeSettings,
    build_history_seed,
    canonical_hash,
    create_scenario_workspace,
)
from teams_runtime.cli import build_parser, cmd_benchmark_sprint_ab
from teams_runtime.runtime.execution_policy import (
    InvocationBudget,
    InvocationBudgetExceeded,
    ModelExecutionPolicy,
    ModelExecutionPolicyViolation,
)
from teams_runtime.shared.models import TEAM_ROLES
from teams_runtime.shared.prompt_context import PROMPT_EVENT_SELECTION_POLICY
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


def _settings() -> RuntimeSettings:
    role_defaults = _role_defaults()
    return RuntimeSettings(
        role_defaults=role_defaults,
        rate_cards={},
        source_config_hash=canonical_hash({"role_defaults": role_defaults, "rate_cards": {}}),
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
        "role": "developer" if occurrence == 1 else "qa",
        "purpose": "implement" if occurrence == 1 else "validate",
        "workflow_step": "todo_execution" if occurrence == 1 else "quality_gate",
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
        "prompt_context": {
            "enabled": is_after,
            "compacted": bool(compacted),
            "total_events": 48,
            "included_events": 16 if compacted else 48,
            "omitted_events": 32 if compacted else 0,
            "recent_events": 8,
            "max_events": 16,
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
        metrics=reduce_telemetry(records),
        quality=_passing_quality(),
        sprint=SprintEvidence(
            sprint_id="benchmark-sprint",
            status="completed",
            closeout_status="verified",
            todo_count=1,
            completed_todo_count=1,
            commit_sha=f"{variant}-commit",
        ),
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
        runtime_model_map=_role_defaults(),
        rate_cards={},
        history_hash=_HISTORY_SEED_HASH,
        runs=runs,
        started_at="2026-07-27T00:00:00+00:00",
        ended_at="2026-07-27T00:02:00+00:00",
    )


class SprintBenchmarkScenarioTests(unittest.TestCase):
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
            self.assertFalse(before_config["prompt_context"]["enabled"])
            self.assertTrue(after_config["prompt_context"]["enabled"])
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


class SprintBenchmarkCliTests(unittest.TestCase):
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

            for run in result.runs:
                run_dir = result.output_dir / "runs" / run.arm.run_id
                self.assertTrue((run_dir / "run.json").is_file())
                self.assertTrue((run_dir / "metrics.json").is_file())
                self.assertTrue((run_dir / "model_invocations.jsonl").is_file())
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

    def test_invalid_compaction_projection_cannot_make_a_pair_comparable(self) -> None:
        invalid_cases: dict[str, dict[str, Any]] = {}
        inconsistent_total = _telemetry_record(variant="after")
        inconsistent_total["prompt_context"]["total_events"] = 49
        invalid_cases["inconsistent_total"] = inconsistent_total
        below_recent_tail = _telemetry_record(variant="after")
        below_recent_tail["prompt_context"].update(
            {"included_events": 7, "omitted_events": 41}
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
        self.assertEqual(sanitized["prompt_context"]["omitted_events"], 32)

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
                            shell_environment={name: "must-not-leak"},
                        )

            policy = ModelExecutionPolicy.for_benchmark(
                allowed_workspace_root=allowed,
                invocation_budget=budget,
                call_timeout_seconds=30,
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

        self.assertEqual(seen_variants, ["before"])
        self.assertEqual(len(result.runs), 1)
        self.assertEqual(result.runs[0].status, "preflight_failed")
        self.assertEqual(result.status, "inconclusive")
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
