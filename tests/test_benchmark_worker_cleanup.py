from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from teams_runtime.benchmarking import runner, worker
from teams_runtime.benchmarking.models import (
    ArmPlan,
    BenchmarkOptions,
    BenchmarkWorkerSafetyError,
    WorkerContext,
    WorkerOutcome,
    invocation_identity_digest,
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
    DEFAULT_HISTORY_SEED_COUNT,
    ScenarioWorkspace,
)
from teams_runtime.shared.prompt_context import PROMPT_EVENT_SELECTION_POLICY


def _snapshot(*entries: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "entries": list(entries),
    }


def _running_entry(pid: int, process_group_id: int) -> dict[str, object]:
    return {
        "pid": pid,
        "process_group_id": process_group_id,
        "state": "running",
    }


def _target_context(*, enabled: bool) -> dict[str, object]:
    return {
        "provider": "codex_cli",
        "invocation_id": "target-invocation",
        "operation_id": "target-operation",
        "logical_call_id": "target-logical-call",
        "attempt_index": 1,
        "attempt_kind": "primary",
        "runtime_identity": "role",
        "role": BENCHMARK_TARGET_ROLE,
        "purpose": BENCHMARK_TARGET_PURPOSE,
        "workflow_step": BENCHMARK_TARGET_WORKFLOW_STEP,
        "request_id": "target-request",
        "sprint_id": "target-sprint",
        "todo_id": "",
        "backlog_id": "",
        "goal_id": "",
        "prompt_context_enabled": enabled,
        "prompt_context_total_events": BENCHMARK_TARGET_TOTAL_EVENTS,
        "prompt_context_included_events": (
            BENCHMARK_TARGET_INCLUDED_EVENTS
            if enabled
            else BENCHMARK_TARGET_TOTAL_EVENTS
        ),
        "prompt_context_omitted_events": (
            BENCHMARK_TARGET_OMITTED_EVENTS if enabled else 0
        ),
        "prompt_context_recent_events": BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
        "prompt_context_max_events": BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
        "prompt_context_selection_policy": PROMPT_EVENT_SELECTION_POLICY,
    }


def _worker_context(
    root: Path,
    *,
    run_output_dir: Path | None = None,
) -> WorkerContext:
    workspace_root = root / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    output_dir = run_output_dir or root / "run-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return WorkerContext(
        benchmark_id="private-telemetry-test",
        arm=ArmPlan(
            pair_index=1,
            order_index=1,
            variant="before",
            run_id="pair-001-before",
            prompt_context_enabled=False,
        ),
        workspace_root=workspace_root,
        run_output_dir=output_dir,
        milestone="Repair the benchmark fixture.",
        history_seed=(),
        max_invocations=2,
        call_timeout_seconds=30,
        run_timeout_seconds=60,
        live=True,
    )


class _FakeProcess:
    def __init__(self, *wait_results: object):
        self.pid = 41001
        self._wait_results = list(wait_results)
        self.wait_calls: list[float] = []

    def wait(self, *, timeout: float) -> int:
        self.wait_calls.append(timeout)
        result = self._wait_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return int(result)

    def poll(self) -> int | None:
        return None

    def send_signal(self, _process_signal: signal.Signals) -> None:
        return None


class BenchmarkWorkerCleanupTests(unittest.TestCase):
    def test_worker_context_rejects_non_finite_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            context = _worker_context(Path(temporary_directory))
            (context.workspace_root / "team_runtime.yaml").write_text(
                "roles: {}\n",
                encoding="utf-8",
            )
            (context.workspace_root / ".git").mkdir()
            scenario_dir = context.workspace_root / ".benchmark"
            scenario_dir.mkdir()
            (scenario_dir / "scenario.json").write_text("{}\n", encoding="utf-8")
            context = replace(
                context,
                history_seed=tuple(
                    {"event": index}
                    for index in range(DEFAULT_HISTORY_SEED_COUNT)
                ),
            )

            with mock.patch.dict(
                os.environ,
                {worker.LIVE_BENCHMARK_ENV: "1"},
            ):
                for field_name in (
                    "call_timeout_seconds",
                    "run_timeout_seconds",
                ):
                    for value in (math.nan, math.inf, -math.inf):
                        with self.subTest(field_name=field_name, value=value):
                            invalid_context = replace(
                                context,
                                **{field_name: value},
                            )
                            with self.assertRaises(ValueError):
                                worker._validate_context(invalid_context)

    def test_execution_policy_routes_telemetry_outside_provider_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            context = _worker_context(Path(temporary_directory))
            budget = worker.InvocationBudget(context.max_invocations)

            with mock.patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "benchmark-test-key"},
            ), mock.patch.object(
                worker,
                "_resolve_benchmark_codex_executable",
                return_value=Path(sys.executable).resolve(),
            ):
                policy = worker._build_execution_policy(
                    context,
                    budget=budget,
                )

            expected = (
                context.run_output_dir.resolve()
                / ".private_model_invocations"
            )
            self.assertEqual(policy.telemetry_output_dir, expected)
            self.assertFalse(
                expected.is_relative_to(context.workspace_root.resolve())
            )
            with self.assertRaises(worker.ModelExecutionPolicyViolation):
                policy.assert_workspace_allowed(expected)

    def test_benchmark_path_excludes_untrusted_and_relative_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = _worker_context(root)
            workspace_bin = context.workspace_root / "bin"
            run_output_bin = context.run_output_dir / "bin"
            temporary_bin = root / "system-temp" / "bin"
            safe_bin = root / "safe-bin"
            for directory in (
                workspace_bin,
                run_output_bin,
                temporary_bin,
                safe_bin,
            ):
                directory.mkdir(parents=True)
            workspace_link = root / "workspace-bin-link"
            workspace_link.symlink_to(workspace_bin, target_is_directory=True)
            supplied_path = os.pathsep.join(
                (
                    "",
                    ".",
                    str(root / "missing-bin"),
                    str(workspace_bin),
                    str(workspace_link),
                    str(run_output_bin),
                    str(temporary_bin),
                    str(safe_bin),
                    str(safe_bin),
                )
            )

            with (
                mock.patch.dict(os.environ, {"PATH": supplied_path}, clear=False),
                mock.patch.object(
                    worker,
                    "_benchmark_unsafe_path_roots",
                    return_value=(
                        context.workspace_root.resolve(),
                        context.run_output_dir.resolve(),
                        (root / "system-temp").resolve(),
                    ),
                ),
            ):
                sanitized = worker._sanitized_benchmark_path(context)

            self.assertEqual(sanitized, str(safe_bin.resolve()))

            with (
                mock.patch.dict(
                    os.environ,
                    {"PATH": os.pathsep.join((".", str(workspace_link)))},
                    clear=False,
                ),
                mock.patch.object(
                    worker,
                    "_benchmark_unsafe_path_roots",
                    return_value=(context.workspace_root.resolve(),),
                ),
            ):
                with self.assertRaisesRegex(
                    worker.ModelExecutionPolicyViolation,
                    "no safe external executable directories",
                ):
                    worker._sanitized_benchmark_path(context)

    def test_codex_resolution_rejects_provider_writable_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            context = _worker_context(Path(temporary_directory))
            for label, executable in (
                ("workspace", context.workspace_root / "codex"),
                ("run_output", context.run_output_dir / "codex"),
            ):
                executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                executable.chmod(0o700)
                with self.subTest(label=label), mock.patch.object(
                    worker.shutil,
                    "which",
                    return_value=str(executable),
                ):
                    with self.assertRaisesRegex(
                        worker.ModelExecutionPolicyViolation,
                        "not a safe external executable",
                    ):
                        worker._resolve_benchmark_codex_executable(
                            context,
                            search_path=os.defpath,
                        )

    def test_private_telemetry_consumption_ignores_workspace_forgery_and_removes_raw_shards(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            context = _worker_context(Path(temporary_directory))
            private_dir = worker._initialize_private_telemetry(context)
            private_shard = private_dir / "2026-08-12" / "role.100.jsonl"
            private_shard.parent.mkdir(parents=True)
            private_shard.write_text(
                json.dumps(
                    {
                        "invocation_id": "trusted-private-invocation",
                        "input_tokens": 25,
                        "usage_source": "native",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            forged_shard = (
                context.workspace_root
                / ".teams_runtime"
                / "metrics"
                / "model_invocations"
                / "2026-08-12"
                / "role.200.jsonl"
            )
            forged_shard.parent.mkdir(parents=True)
            forged_shard.write_text(
                json.dumps(
                    {
                        "invocation_id": "forged-workspace-invocation",
                        "input_tokens": 9_999_999,
                        "usage_source": "native",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            records = worker._consume_private_telemetry(context)

            self.assertEqual(
                [record["invocation_id"] for record in records],
                ["trusted-private-invocation"],
            )
            self.assertEqual(records[0]["input_tokens"], 25)
            self.assertFalse(private_dir.exists())
            self.assertTrue(forged_shard.is_file())

    def test_private_telemetry_deletion_failure_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            context = _worker_context(Path(temporary_directory))
            private_dir = worker._initialize_private_telemetry(context)
            shard = private_dir / "2026-08-12" / "role.100.jsonl"
            shard.parent.mkdir(parents=True)
            shard.write_text(
                json.dumps({"invocation_id": "trusted-invocation"}) + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(
                worker.shutil,
                "rmtree",
                side_effect=OSError("permission denied"),
            ):
                with self.assertRaisesRegex(
                    worker._WorkerCleanupFailure,
                    "Failed to remove benchmark private telemetry shards",
                ):
                    worker._consume_private_telemetry(context)

            self.assertTrue(private_dir.is_dir())

    def test_worker_outcome_payload_cannot_transport_telemetry(self) -> None:
        outcome = WorkerOutcome(
            status="completed",
            telemetry_records=(
                {
                    "invocation_id": "child-supplied-invocation",
                    "input_tokens": 9_999_999,
                },
            ),
        )

        payload = worker._worker_outcome_payload(outcome)

        self.assertEqual(payload["telemetry_records"], [])
        payload["telemetry_records"] = [
            {
                "invocation_id": "forged-result-invocation",
                "input_tokens": 9_999_999,
            }
        ]
        restored = worker._worker_outcome_from_payload(payload)
        self.assertEqual(restored.telemetry_records, ())

    def test_worker_context_rejects_run_output_inside_provider_workspace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = _worker_context(
                root,
                run_output_dir=root / "workspace" / "run-output",
            )

            with mock.patch.dict(
                os.environ,
                {worker.LIVE_BENCHMARK_ENV: "1"},
            ):
                with self.assertRaisesRegex(
                    worker.ModelExecutionPolicyViolation,
                    "run output must be outside",
                ):
                    worker._validate_context(context)

    def test_timeout_cleanup_rereads_journal_before_kill(self) -> None:
        first_timeout = subprocess.TimeoutExpired(
            cmd=("worker",),
            timeout=worker._CHILD_TERMINATION_GRACE_SECONDS,
        )
        process = _FakeProcess(first_timeout, -signal.SIGKILL)
        initial_entry = _running_entry(42001, 42001)
        late_entry = _running_entry(42002, 42002)
        snapshots = (
            _snapshot(initial_entry),
            _snapshot(initial_entry, late_entry),
            _snapshot(initial_entry, late_entry),
        )
        log = mock.Mock()

        with (
            mock.patch.object(
                worker,
                "_read_call_journal_strict",
                side_effect=snapshots,
            ) as read_journal,
            mock.patch.object(
                worker,
                "_signal_provider_entries",
                return_value=0,
            ) as signal_providers,
            mock.patch.object(worker, "_signal_child_group") as signal_child,
            mock.patch.object(
                worker,
                "_wait_for_cleanup_confirmation",
            ) as confirm_cleanup,
            mock.patch.object(
                worker,
                "_write_private_json",
            ) as write_journal,
        ):
            final_snapshot = worker._terminate_worker_child(
                process,  # type: ignore[arg-type]
                journal_path=Path("/content-safe/call_journal.json"),
                worker_log=log,
                stop_reason="run_timeout_exceeded",
            )

        self.assertEqual(read_journal.call_count, 3)
        self.assertEqual(len(process.wait_calls), 2)
        self.assertEqual(signal_providers.call_args_list[0].args[1], signal.SIGTERM)
        pre_kill_entries = signal_providers.call_args_list[1].args[0]
        self.assertEqual(
            {
                (entry["pid"], entry["process_group_id"])
                for entry in pre_kill_entries
            },
            {(42001, 42001), (42002, 42002)},
        )
        self.assertEqual(
            [entry["state"] for entry in final_snapshot["entries"]],
            ["terminated", "terminated"],
        )
        self.assertTrue(
            all(
                entry["stop_reason"] == "run_timeout_exceeded"
                for entry in final_snapshot["entries"]
            )
        )
        write_journal.assert_called_once_with(
            Path("/content-safe/call_journal.json"),
            final_snapshot,
        )
        self.assertEqual(signal_providers.call_args_list[1].args[1], signal.SIGKILL)
        self.assertEqual(signal_child.call_args_list[0].args[1], signal.SIGTERM)
        self.assertEqual(signal_child.call_args_list[1].args[1], signal.SIGKILL)
        confirmed_entries = confirm_cleanup.call_args.kwargs["provider_entries"]
        self.assertEqual(
            {
                (entry["pid"], entry["process_group_id"])
                for entry in confirmed_entries
            },
            {(42001, 42001), (42002, 42002)},
        )

    def test_second_worker_wait_timeout_fails_closed(self) -> None:
        first_timeout = subprocess.TimeoutExpired(
            cmd=("worker",),
            timeout=worker._CHILD_TERMINATION_GRACE_SECONDS,
        )
        second_timeout = subprocess.TimeoutExpired(
            cmd=("worker",),
            timeout=worker._CHILD_TERMINATION_GRACE_SECONDS,
        )
        process = _FakeProcess(first_timeout, second_timeout)
        snapshots = (
            _snapshot(_running_entry(42001, 42001)),
            _snapshot(_running_entry(42001, 42001)),
            _snapshot(_running_entry(42001, 42001)),
        )

        with (
            mock.patch.object(
                worker,
                "_read_call_journal_strict",
                side_effect=snapshots,
            ),
            mock.patch.object(
                worker,
                "_signal_provider_entries",
                return_value=0,
            ),
            mock.patch.object(worker, "_signal_child_group"),
            mock.patch.object(
                worker,
                "_wait_for_cleanup_confirmation",
            ) as confirm_cleanup,
            mock.patch.object(
                worker,
                "_write_private_json",
            ) as write_journal,
        ):
            with self.assertRaises(worker._WorkerCleanupFailure):
                worker._terminate_worker_child(
                    process,  # type: ignore[arg-type]
                    journal_path=Path("/content-safe/call_journal.json"),
                    worker_log=mock.Mock(),
                )

        self.assertEqual(len(process.wait_calls), 2)
        confirm_cleanup.assert_called_once()
        write_journal.assert_called_once()

    def test_journal_read_failures_are_deferred_until_cleanup_is_confirmed(
        self,
    ) -> None:
        entries = (
            _running_entry(42001, 42001),
            _running_entry(42002, 42002),
            _running_entry(42003, 42003),
        )

        for failed_read_index in range(3):
            with self.subTest(failed_read_index=failed_read_index):
                process = _FakeProcess(0)
                side_effects: list[object] = [
                    _snapshot(entries[0]),
                    _snapshot(entries[1]),
                    _snapshot(entries[2]),
                ]
                side_effects[failed_read_index] = (
                    worker._WorkerCleanupFailure("journal unavailable")
                )

                with (
                    mock.patch.object(
                        worker,
                        "_read_call_journal_strict",
                        side_effect=side_effects,
                    ) as read_journal,
                    mock.patch.object(
                        worker,
                        "_signal_provider_entries",
                        return_value=0,
                    ) as signal_providers,
                    mock.patch.object(
                        worker,
                        "_signal_child_group",
                    ) as signal_child,
                    mock.patch.object(
                        worker,
                        "_wait_for_cleanup_confirmation",
                    ) as confirm_cleanup,
                    mock.patch.object(
                        worker,
                        "_write_private_json",
                    ) as write_journal,
                ):
                    with self.assertRaisesRegex(
                        worker._WorkerCleanupFailure,
                        "cleanup could not be verified",
                    ):
                        worker._terminate_worker_child(
                            process,  # type: ignore[arg-type]
                            journal_path=Path(
                                "/content-safe/call_journal.json"
                            ),
                            worker_log=mock.Mock(),
                        )

                self.assertEqual(read_journal.call_count, 3)
                self.assertTrue(
                    all(
                        call.kwargs["required"] is True
                        for call in read_journal.call_args_list
                    )
                )
                self.assertEqual(len(process.wait_calls), 1)
                child_signals = [
                    call.args[1]
                    for call in signal_child.call_args_list
                ]
                self.assertEqual(child_signals[0], signal.SIGTERM)
                self.assertIn(signal.SIGKILL, child_signals)
                confirm_cleanup.assert_called_once()
                confirmed_entries = confirm_cleanup.call_args.kwargs[
                    "provider_entries"
                ]
                self.assertEqual(
                    {
                        (entry["pid"], entry["process_group_id"])
                        for entry in confirmed_entries
                    },
                    {
                        (entry["pid"], entry["process_group_id"])
                        for index, entry in enumerate(entries)
                        if index != failed_read_index
                    },
                )
                final_provider_sweep = (
                    signal_providers.call_args_list[-1]
                )
                self.assertEqual(
                    final_provider_sweep.args[1],
                    signal.SIGKILL,
                )
                self.assertEqual(
                    {
                        (entry["pid"], entry["process_group_id"])
                        for entry in final_provider_sweep.args[0]
                    },
                    {
                        (entry["pid"], entry["process_group_id"])
                        for index, entry in enumerate(entries)
                        if index != failed_read_index
                    },
                )
                if failed_read_index == 2:
                    write_journal.assert_not_called()
                else:
                    write_journal.assert_called_once()

    def test_all_journal_reads_failing_still_cleans_up_worker_group(self) -> None:
        process = _FakeProcess(0)
        journal_failure = worker._WorkerCleanupFailure(
            "journal unavailable"
        )

        with (
            mock.patch.object(
                worker,
                "_read_call_journal_strict",
                side_effect=(
                    journal_failure,
                    journal_failure,
                    journal_failure,
                ),
            ) as read_journal,
            mock.patch.object(
                worker,
                "_signal_provider_entries",
                return_value=0,
            ),
            mock.patch.object(
                worker,
                "_signal_child_group",
            ) as signal_child,
            mock.patch.object(
                worker,
                "_wait_for_cleanup_confirmation",
            ) as confirm_cleanup,
            mock.patch.object(
                worker,
                "_write_private_json",
            ) as write_journal,
        ):
            with self.assertRaisesRegex(
                worker._WorkerCleanupFailure,
                "initial_journal,pre_kill_journal,final_journal",
            ):
                worker._terminate_worker_child(
                    process,  # type: ignore[arg-type]
                    journal_path=Path(
                        "/content-safe/call_journal.json"
                    ),
                    worker_log=mock.Mock(),
                )

        self.assertEqual(read_journal.call_count, 3)
        self.assertEqual(len(process.wait_calls), 1)
        self.assertEqual(
            [call.args[1] for call in signal_child.call_args_list],
            [signal.SIGTERM, signal.SIGKILL, signal.SIGKILL],
        )
        confirm_cleanup.assert_called_once()
        self.assertEqual(
            confirm_cleanup.call_args.kwargs["provider_entries"],
            [],
        )
        write_journal.assert_not_called()

    def test_reserved_launch_observed_during_cleanup_fails_closed(self) -> None:
        process = _FakeProcess(0)
        reserved_entry = {
            "reservation_id": "reservation-1",
            "state": "reserved",
        }
        snapshot = _snapshot(reserved_entry)

        with (
            mock.patch.object(
                worker,
                "_read_call_journal_strict",
                side_effect=(snapshot, snapshot, snapshot),
            ),
            mock.patch.object(
                worker,
                "_signal_provider_entries",
                return_value=0,
            ),
            mock.patch.object(
                worker,
                "_signal_child_group",
            ) as signal_child,
            mock.patch.object(
                worker,
                "_wait_for_cleanup_confirmation",
            ) as confirm_cleanup,
            mock.patch.object(
                worker,
                "_write_private_json",
            ) as write_journal,
        ):
            with self.assertRaisesRegex(
                worker._WorkerCleanupFailure,
                "reserved_attempt",
            ):
                worker._terminate_worker_child(
                    process,  # type: ignore[arg-type]
                    journal_path=Path(
                        "/content-safe/call_journal.json"
                    ),
                    worker_log=mock.Mock(),
                )

        self.assertEqual(
            [call.args[1] for call in signal_child.call_args_list],
            [signal.SIGTERM, signal.SIGKILL, signal.SIGKILL],
        )
        confirm_cleanup.assert_called_once()
        write_journal.assert_called_once()

    def test_existing_malformed_journal_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            journal_path = Path(temporary_directory) / "call_journal.json"
            journal_path.write_text("{not-json", encoding="utf-8")

            with self.assertRaisesRegex(
                worker._WorkerCleanupFailure,
                "unreadable or malformed",
            ):
                worker._read_call_journal_strict(journal_path)

            journal_path.unlink()
            self.assertEqual(
                worker._read_call_journal_strict(journal_path),
                {},
            )
            with self.assertRaisesRegex(
                worker._WorkerCleanupFailure,
                "Required benchmark call journal is missing",
            ):
                worker._read_call_journal_strict(
                    journal_path,
                    required=True,
                )

    def test_journal_reservation_ids_must_be_unique_and_present(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            journal_path = Path(temporary_directory) / "call_journal.json"
            cases = {
                "duplicate": [
                    {"reservation_id": "same", "state": "completed"},
                    {"reservation_id": "same", "state": "completed"},
                ],
                "missing": [
                    {"reservation_id": "first", "state": "completed"},
                    {"state": "completed"},
                ],
            }
            for label, entries in cases.items():
                with self.subTest(label=label):
                    journal_path.write_text(
                        json.dumps(
                            {
                                "schema_version": 2,
                                "max_invocations": 2,
                                "reserved_count": 2,
                                "remaining": 0,
                                "rejected_count": 0,
                                "entries": entries,
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        worker._WorkerCleanupFailure,
                        "reservation ids",
                    ):
                        worker._read_call_journal_strict(journal_path)

    def test_call_journal_summary_keeps_attempt_and_telemetry_counts_distinct(self) -> None:
        states = [
            *(["completed"] * 5),
            *(["timeout"] * 2),
            "terminated",
        ]
        summary = worker._summarize_call_journal(
            {
                "schema_version": 2,
                "max_invocations": 20,
                "reserved_count": 8,
                "remaining": 12,
                "rejected_count": 1,
                "entries": [
                    {
                        "state": state,
                        "invocation_id": f"invocation-{index}",
                    }
                    for index, state in enumerate(states)
                ],
            },
            telemetry_records=tuple(
                {"invocation_id": f"invocation-{index}"}
                for index in range(7)
            ),
        )

        self.assertEqual(summary["reserved_count"], 8)
        self.assertEqual(summary["telemetry_record_count"], 7)
        self.assertEqual(summary["unobserved_attempt_count"], 1)
        self.assertEqual(summary["telemetry_coverage_percent"], 87.5)
        self.assertEqual(summary["completed_count"], 5)
        self.assertEqual(summary["timeout_count"], 2)
        self.assertEqual(summary["terminated_count"], 1)
        self.assertEqual(summary["active_count"], 0)
        self.assertEqual(summary["rejected_count"], 1)
        self.assertTrue(summary["identity_reconciled"])
        self.assertEqual(
            summary["journal_invocation_id_unobserved_count"],
            1,
        )

    def test_parent_cleanup_ignores_terminal_provider_groups(self) -> None:
        entries = worker._merge_launched_process_entries(
            {
                "entries": [
                    {
                        "pid": 42001,
                        "process_group_id": 42001,
                        "state": "completed",
                    },
                    {
                        "pid": 42002,
                        "process_group_id": 42002,
                        "state": "failed",
                    },
                ]
            }
        )

        self.assertEqual(entries, [])

    def test_v3_journal_verifies_exact_target_projection_outside_workspace(self) -> None:
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                context = _target_context(enabled=enabled)
                entry = {
                    **context,
                    "reservation_id": "target-reservation",
                    "state": "completed",
                }
                telemetry = {**context, "status": "completed"}
                summary = worker._summarize_call_journal(
                    {
                        "schema_version": 3,
                        "max_invocations": 1,
                        "reserved_count": 1,
                        "remaining": 0,
                        "rejected_count": 0,
                        "entries": [entry],
                    },
                    telemetry_records=(telemetry,),
                )

                self.assertTrue(summary["identity_reconciled"])
                self.assertTrue(summary["context_reconciled"])
                self.assertEqual(
                    summary["journal_telemetry_context_mismatch_count"],
                    0,
                )
                self.assertEqual(
                    summary["verified_target_projection_count"],
                    1,
                )
                self.assertEqual(
                    summary["verified_target_invocation_ids_sha256"],
                    invocation_identity_digest(["target-invocation"]),
                )

    def test_v3_journal_rejects_tampered_or_unrelated_target_telemetry(self) -> None:
        context = _target_context(enabled=True)
        entry = {
            **context,
            "reservation_id": "target-reservation",
            "state": "completed",
        }
        tampered = {
            **context,
            "status": "completed",
            "prompt_context_included_events": BENCHMARK_TARGET_INCLUDED_EVENTS - 1,
            "prompt_context_omitted_events": BENCHMARK_TARGET_OMITTED_EVENTS + 1,
        }
        tampered_summary = worker._summarize_call_journal(
            {
                "schema_version": 3,
                "max_invocations": 1,
                "reserved_count": 1,
                "remaining": 0,
                "rejected_count": 0,
                "entries": [entry],
            },
            telemetry_records=(tampered,),
        )

        self.assertFalse(tampered_summary["context_reconciled"])
        self.assertEqual(
            tampered_summary["journal_telemetry_context_mismatch_count"],
            1,
        )
        self.assertEqual(
            tampered_summary["verified_target_projection_count"],
            0,
        )

        conflicting_representation = {
            **context,
            "status": "completed",
            "prompt_context": {
                "enabled": False,
                "total_events": BENCHMARK_TARGET_TOTAL_EVENTS,
                "included_events": BENCHMARK_TARGET_TOTAL_EVENTS,
                "omitted_events": 0,
                "recent_events": BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
                "max_events": BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
                "selection_policy": PROMPT_EVENT_SELECTION_POLICY,
            },
        }
        conflicting_summary = worker._summarize_call_journal(
            {
                "schema_version": 3,
                "max_invocations": 1,
                "reserved_count": 1,
                "remaining": 0,
                "rejected_count": 0,
                "entries": [entry],
            },
            telemetry_records=(conflicting_representation,),
        )

        self.assertFalse(conflicting_summary["context_reconciled"])
        self.assertEqual(
            conflicting_summary["journal_telemetry_context_mismatch_count"],
            1,
        )
        self.assertEqual(
            conflicting_summary["verified_target_projection_count"],
            0,
        )

        unrelated_context = {
            **context,
            "role": "developer",
            "purpose": "implement",
            "workflow_step": "todo_execution",
        }
        unrelated_summary = worker._summarize_call_journal(
            {
                "schema_version": 3,
                "max_invocations": 1,
                "reserved_count": 1,
                "remaining": 0,
                "rejected_count": 0,
                "entries": [
                    {
                        **unrelated_context,
                        "reservation_id": "unrelated-reservation",
                        "state": "completed",
                    }
                ],
            },
            telemetry_records=(
                {**unrelated_context, "status": "completed"},
            ),
        )

        self.assertTrue(unrelated_summary["context_reconciled"])
        self.assertEqual(
            unrelated_summary["verified_target_projection_count"],
            0,
        )

    def test_journal_finalization_failure_fails_closed(self) -> None:
        snapshot = {
            "schema_version": 2,
            "max_invocations": 1,
            "reserved_count": 1,
            "remaining": 0,
            "rejected_count": 0,
            "entries": [
                {
                    "reservation_id": "reservation-1",
                    "state": "running",
                }
            ],
        }

        with mock.patch.object(
            worker,
            "_write_private_json",
            side_effect=OSError("disk full"),
        ):
            with self.assertRaisesRegex(
                worker._WorkerCleanupFailure,
                "Failed to finalize",
            ):
                worker._finalize_call_journal_after_cleanup(
                    Path("/content-safe/call_journal.json"),
                    snapshot,
                    stop_reason="run_timeout_exceeded",
                )

    def test_cleanup_confirmation_raises_while_any_group_survives(self) -> None:
        process = _FakeProcess(0)
        provider_entry = _running_entry(42001, 42001)

        with (
            mock.patch.object(worker, "_worker_group_alive", return_value=False),
            mock.patch.object(worker, "_provider_entry_alive", return_value=True),
            mock.patch.object(worker, "_signal_provider_entries", return_value=1),
            mock.patch.object(worker.time, "monotonic", side_effect=(10.0, 10.0)),
        ):
            with self.assertRaises(worker._WorkerCleanupFailure):
                worker._wait_for_cleanup_confirmation(
                    process,  # type: ignore[arg-type]
                    provider_entries=[provider_entry],
                    worker_log=mock.Mock(),
                    timeout_seconds=0.0,
                )

    def test_runner_does_not_convert_worker_safety_failure_to_arm_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            scenario_root = root / "scenario"
            scenario_root.mkdir()
            scenario = ScenarioWorkspace(
                root=scenario_root,
                initial_commit="initial",
                initial_commit_count=1,
                protected_hashes={},
                config_hash="config",
                comparable_config_hash="comparable",
                history_hash="history",
                history_seed=(),
            )
            options = BenchmarkOptions(
                source_root=root,
                runtime_config_path=root / "runtime.yaml",
                output_dir=root / "output",
                live=True,
            )
            arm = ArmPlan(
                pair_index=1,
                order_index=1,
                variant="before",
                run_id="pair-001-before",
                prompt_context_enabled=False,
            )

            def unsafe_worker(_context: object) -> object:
                raise BenchmarkWorkerSafetyError("cleanup could not be confirmed")

            with (
                mock.patch.object(
                    runner,
                    "create_scenario_workspace",
                    return_value=scenario,
                ),
                mock.patch.object(
                    runner,
                    "_capture_retention_baseline",
                    return_value={},
                ),
                mock.patch.object(runner, "_safe_worker_failure") as safe_failure,
            ):
                with self.assertRaises(BenchmarkWorkerSafetyError):
                    runner._run_arm(
                        benchmark_id="safety-test",
                        output_root=root / "output",
                        temporary_root=root / "temporary",
                        options=options,
                        worker=unsafe_worker,  # type: ignore[arg-type]
                        settings=None,
                        arm=arm,
                    )

            safe_failure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
