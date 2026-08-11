from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from teams_runtime.runtime import benchmark_launcher
from teams_runtime.runtime.codex_runner import CodexRunner
from teams_runtime.runtime.execution_policy import (
    InvocationBudget,
    ModelExecutionPolicy,
    ModelExecutionPolicyViolation,
    ModelInvocationTimeout,
)
from teams_runtime.shared.models import RoleRuntimeConfig


class BenchmarkExecutionPolicyTests(unittest.TestCase):
    def _policy(
        self,
        root: Path,
        *,
        max_invocations: int = 2,
        timeout_seconds: float = 1.0,
    ) -> tuple[ModelExecutionPolicy, InvocationBudget]:
        budget = InvocationBudget(
            max_invocations,
            journal_path=root / "call_journal.json",
        )
        policy = ModelExecutionPolicy.for_benchmark(
            allowed_workspace_root=root,
            invocation_budget=budget,
            call_timeout_seconds=timeout_seconds,
            kill_grace_seconds=0.1,
            shell_environment={"PYTHONPATH": str(root)},
        )
        return policy, budget

    def test_benchmark_codex_command_is_sandboxed_without_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            workspace = root / "role"
            workspace.mkdir()
            policy, _budget = self._policy(root)
            runner = CodexRunner(
                RoleRuntimeConfig(model="gpt-benchmark", reasoning="high"),
                role="developer",
                execution_policy=policy,
            )

            command, stdin_input = runner._build_command(
                workspace=workspace,
                prompt="content-safe test prompt",
                session_id=None,
                output_file=workspace / "output.txt",
                bypass_sandbox=False,
            )

            self.assertEqual(stdin_input, "content-safe test prompt")
            self.assertIn("--sandbox", command)
            self.assertIn("workspace-write", command)
            self.assertIn("--ignore-user-config", command)
            self.assertIn("--ignore-rules", command)
            self.assertIn("mcp_servers={}", command)
            self.assertNotIn(
                "--dangerously-bypass-approvals-and-sandbox",
                command,
            )
            self.assertNotIn("--full-auto", command)

    def test_provider_environment_excludes_github_and_discord_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            policy, _budget = self._policy(root)
            runner = CodexRunner(
                RoleRuntimeConfig(model="gpt-benchmark"),
                execution_policy=policy,
            )
            source_environment = {
                "CODEX_HOME": "/operator/codex-home",
                "OPENAI_API_KEY": "provider-secret",
                "GH_TOKEN": "github-secret",
                "DISCORD_TOKEN": "discord-secret",
                "HOME": "/operator/home",
                "PATH": os.defpath,
                "TEMP": "/operator/temp",
                "TMP": "/operator/tmp",
                "TMPDIR": "/operator/tmpdir",
            }

            with mock.patch.dict(os.environ, source_environment, clear=True):
                environment = runner._provider_environment()

            self.assertEqual(environment["OPENAI_API_KEY"], "provider-secret")
            self.assertNotIn("GH_TOKEN", environment)
            self.assertNotIn("DISCORD_TOKEN", environment)
            provider_state = root / ".teams_runtime" / "benchmark_provider"
            provider_tmp = str(provider_state / "tmp")
            self.assertEqual(environment["HOME"], str(provider_state / "home"))
            self.assertEqual(
                environment["CODEX_HOME"],
                str(provider_state / "codex_home"),
            )
            self.assertEqual(environment["TMPDIR"], provider_tmp)
            self.assertEqual(environment["TMP"], provider_tmp)
            self.assertEqual(environment["TEMP"], provider_tmp)
            self.assertEqual(environment["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(environment["PYTHONPATH"], str(root))

    def test_benchmark_rejects_provider_state_directory_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            workspace = root / "role"
            workspace.mkdir()
            outside = root.parent / "outside-codex-home"
            budget = InvocationBudget(1)
            policy = ModelExecutionPolicy.for_benchmark(
                allowed_workspace_root=root,
                invocation_budget=budget,
                call_timeout_seconds=1,
                shell_environment={"CODEX_HOME": str(outside)},
            )
            runner = CodexRunner(
                RoleRuntimeConfig(model="gpt-benchmark"),
                execution_policy=policy,
            )

            with self.assertRaises(ModelExecutionPolicyViolation):
                runner.run(workspace, "prompt", None)

            self.assertEqual(budget.reserved_count, 0)

    def test_benchmark_rejects_bypass_and_gemini_before_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            workspace = root / "role"
            workspace.mkdir()
            policy, budget = self._policy(root)
            codex_runner = CodexRunner(
                RoleRuntimeConfig(model="gpt-benchmark"),
                execution_policy=policy,
            )
            with self.assertRaises(ModelExecutionPolicyViolation):
                codex_runner.run(
                    workspace,
                    "prompt",
                    None,
                    bypass_sandbox=True,
                )
            self.assertEqual(budget.reserved_count, 0)

            gemini_runner = CodexRunner(
                RoleRuntimeConfig(model="gemini-benchmark"),
                execution_policy=policy,
            )
            with self.assertRaises(ModelExecutionPolicyViolation):
                gemini_runner.run(workspace, "prompt", None)
            self.assertEqual(budget.reserved_count, 0)

    def test_real_timeout_marks_journal_and_terminates_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            workspace = root / "role"
            workspace.mkdir()
            policy, budget = self._policy(
                root,
                max_invocations=1,
                timeout_seconds=0.1,
            )
            runner = CodexRunner(
                RoleRuntimeConfig(model="gpt-benchmark"),
                role="developer",
                execution_policy=policy,
            )
            sleeping_command = [
                sys.executable,
                "-c",
                "import time; time.sleep(30)",
            ]

            with mock.patch.object(
                runner,
                "_build_command",
                return_value=(sleeping_command, None),
            ):
                with self.assertRaises(ModelInvocationTimeout):
                    runner.run(workspace, "prompt", None)

            snapshot = budget.snapshot()
            self.assertEqual(snapshot["reserved_count"], 1)
            self.assertEqual(snapshot["entries"][0]["state"], "timeout")
            self.assertEqual(snapshot["entries"][0]["stop_reason"], "timeout")
            persisted = json.loads(
                (root / "call_journal.json").read_text(encoding="utf-8")
            )
            self.assertEqual(persisted["schema_version"], 2)
            self.assertEqual(persisted["entries"][0]["state"], "timeout")

    def test_provider_launch_waits_for_durable_pid_registration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            policy, _budget = self._policy(root)
            runner = CodexRunner(
                RoleRuntimeConfig(model="gpt-benchmark"),
                execution_policy=policy,
            )
            process = mock.Mock(pid=43210, returncode=0)
            process.communicate.return_value = ("provider output", "")
            events: list[str] = []
            reservation = mock.Mock()
            reservation.mark_started.side_effect = (
                lambda **_kwargs: events.append("registered")
            )

            def release_provider(
                file_descriptor: int,
                payload: bytes,
            ) -> int:
                self.assertEqual(file_descriptor, 12)
                self.assertEqual(payload, b"\x01")
                events.append("released")
                return len(payload)

            command = [sys.executable, "-c", "print('provider')"]
            with (
                mock.patch(
                    "teams_runtime.runtime.codex_runner.os.pipe",
                    return_value=(11, 12),
                ),
                mock.patch(
                    "teams_runtime.runtime.codex_runner.os.close",
                ) as close_fd,
                mock.patch(
                    "teams_runtime.runtime.codex_runner.os.write",
                    side_effect=release_provider,
                ),
                mock.patch(
                    "teams_runtime.runtime.codex_runner.subprocess.Popen",
                    return_value=process,
                ) as popen,
            ):
                completed = runner._run_benchmark_process(
                    command,
                    cwd=root,
                    stdin_input=None,
                    env={"PATH": os.defpath},
                    reservation=reservation,
                )

            self.assertEqual(events, ["registered", "released"])
            reservation.mark_started.assert_called_once_with(
                pid=43210,
                process_group_id=43210,
            )
            launch_command = popen.call_args.args[0]
            self.assertEqual(
                launch_command[-len(command):],
                command,
            )
            self.assertEqual(
                popen.call_args.kwargs["pass_fds"],
                (11,),
            )
            self.assertEqual(close_fd.call_args_list[0].args, (11,))
            self.assertEqual(close_fd.call_args_list[-1].args, (12,))
            self.assertEqual(completed.returncode, 0)

    def test_registration_failure_never_releases_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            policy, _budget = self._policy(root)
            runner = CodexRunner(
                RoleRuntimeConfig(model="gpt-benchmark"),
                execution_policy=policy,
            )
            process = mock.Mock(pid=43210, returncode=None)
            reservation = mock.Mock()
            reservation.mark_started.side_effect = OSError(
                "journal unavailable"
            )

            with (
                mock.patch(
                    "teams_runtime.runtime.codex_runner.os.pipe",
                    return_value=(11, 12),
                ),
                mock.patch(
                    "teams_runtime.runtime.codex_runner.os.close",
                ) as close_fd,
                mock.patch(
                    "teams_runtime.runtime.codex_runner.os.write",
                ) as release_provider,
                mock.patch(
                    "teams_runtime.runtime.codex_runner.subprocess.Popen",
                    return_value=process,
                ),
                mock.patch.object(
                    runner,
                    "_terminate_process_group",
                    return_value=("", ""),
                ) as terminate_process,
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "journal unavailable",
                ):
                    runner._run_benchmark_process(
                        [sys.executable, "-c", "print('provider')"],
                        cwd=root,
                        stdin_input=None,
                        env={"PATH": os.defpath},
                        reservation=reservation,
                    )

            release_provider.assert_not_called()
            self.assertIn(mock.call(11), close_fd.call_args_list)
            self.assertIn(mock.call(12), close_fd.call_args_list)
            terminate_process.assert_called_once()

    def test_launcher_exits_without_exec_when_parent_closes_gate(self) -> None:
        ready_read_fd, ready_write_fd = os.pipe()
        os.close(ready_write_fd)

        with mock.patch.object(
            benchmark_launcher.os,
            "execvpe",
        ) as execute_provider:
            exit_code = benchmark_launcher.main(
                [
                    "--ready-fd",
                    str(ready_read_fd),
                    "--",
                    sys.executable,
                    "-c",
                    "print('must not run')",
                ]
            )

        self.assertEqual(exit_code, 70)
        execute_provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
