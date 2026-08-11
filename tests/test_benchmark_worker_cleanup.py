from __future__ import annotations

import json
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from teams_runtime.benchmarking import runner, worker
from teams_runtime.benchmarking.models import (
    ArmPlan,
    BenchmarkOptions,
    BenchmarkWorkerSafetyError,
)
from teams_runtime.benchmarking.scenario import ScenarioWorkspace


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
