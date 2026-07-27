from __future__ import annotations

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
                "_read_json_mapping",
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
        ):
            worker._terminate_worker_child(
                process,  # type: ignore[arg-type]
                journal_path=Path("/content-safe/call_journal.json"),
                worker_log=log,
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
        )

        with (
            mock.patch.object(
                worker,
                "_read_json_mapping",
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
        ):
            with self.assertRaises(worker._WorkerCleanupFailure):
                worker._terminate_worker_child(
                    process,  # type: ignore[arg-type]
                    journal_path=Path("/content-safe/call_journal.json"),
                    worker_log=mock.Mock(),
                )

        self.assertEqual(len(process.wait_calls), 2)
        confirm_cleanup.assert_not_called()

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
