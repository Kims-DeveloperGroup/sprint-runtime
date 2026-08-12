from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from teams_runtime.runtime import benchmark_launcher
from teams_runtime.runtime.codex_runner import CodexRunner
from teams_runtime.runtime.execution_policy import (
    InvocationBudget,
    ModelExecutionPolicy,
    ModelExecutionPolicyViolation,
    ModelInvocationTimeout,
    quarantine_unsafe_workspace_entries,
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
            codex_executable=sys.executable,
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
            self.assertIn(
                "sandbox_workspace_write.exclude_slash_tmp=true",
                command,
            )
            self.assertIn(
                "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                command,
            )
            self.assertIn("mcp_servers={}", command)
            self.assertNotIn(
                "--dangerously-bypass-approvals-and-sandbox",
                command,
            )
            self.assertNotIn("--full-auto", command)

    def test_benchmark_codex_commands_use_pinned_executable_without_output_path(self) -> None:
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
            workspace_output = workspace / ".teams_runtime_codex_output.txt"

            for label, session_id in (
                ("fresh", None),
                ("resume", "session-123"),
            ):
                with self.subTest(label=label):
                    command, stdin_input = runner._build_command(
                        workspace=workspace,
                        prompt="content-safe test prompt",
                        session_id=session_id,
                        output_file=workspace_output,
                        bypass_sandbox=False,
                    )

                    self.assertEqual(command[0], str(policy.codex_executable))
                    self.assertTrue(Path(command[0]).is_absolute())
                    self.assertEqual(
                        Path(command[0]),
                        Path(sys.executable).resolve(),
                    )
                    self.assertNotIn("-o", command)
                    self.assertNotIn("--output-last-message", command)
                    self.assertNotIn(str(workspace_output), command)
                    self.assertEqual(stdin_input, "content-safe test prompt")

    def test_benchmark_run_returns_jsonl_message_without_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            workspace = root / "role"
            workspace.mkdir()
            policy, budget = self._policy(root, max_invocations=1)
            runner = CodexRunner(
                RoleRuntimeConfig(model="gpt-benchmark", reasoning="high"),
                role="developer",
                execution_policy=policy,
            )
            completed = subprocess.CompletedProcess(
                [str(policy.codex_executable)],
                0,
                (
                    '{"type":"item.completed","item":{"type":"agent_message",'
                    '"text":"result-from-jsonl"}}\n'
                ),
                "",
            )

            with mock.patch.object(
                runner,
                "_run_benchmark_process",
                return_value=completed,
            ):
                output, _session_id = runner.run(
                    workspace,
                    "content-safe test prompt",
                    None,
                )

            self.assertEqual(output, "result-from-jsonl")
            self.assertFalse(
                (workspace / ".teams_runtime_codex_output.txt").exists()
            )
            self.assertEqual(budget.snapshot()["entries"][0]["state"], "completed")

    def test_cli_version_uses_pinned_benchmark_executable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            policy, _budget = self._policy(root)
            telemetry = mock.Mock(enabled=True)
            runner = CodexRunner(
                RoleRuntimeConfig(model="gpt-benchmark"),
                execution_policy=policy,
                telemetry_recorder=telemetry,
            )
            executable = str(policy.codex_executable)
            CodexRunner._version_cache.pop(executable, None)

            with mock.patch(
                "teams_runtime.runtime.codex_runner.subprocess.run",
                return_value=subprocess.CompletedProcess(
                    [executable, "--version"],
                    0,
                    "codex-cli benchmark-test\n",
                    "",
                ),
            ) as run_version:
                version = runner._cli_version("codex")

            self.assertEqual(version, "codex-cli benchmark-test")
            self.assertEqual(run_version.call_args.args[0], [executable, "--version"])
            self.assertIn("env", run_version.call_args.kwargs)

    def test_benchmark_codex_executable_is_canonicalized_and_validated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            trusted_executable = root / "trusted-bin" / "codex"
            trusted_executable.parent.mkdir()
            trusted_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            trusted_executable.chmod(0o700)
            executable_symlink = root / "codex-link"
            executable_symlink.symlink_to(trusted_executable)

            policy = ModelExecutionPolicy.for_benchmark(
                allowed_workspace_root=workspace,
                invocation_budget=InvocationBudget(1),
                call_timeout_seconds=1,
                codex_executable=executable_symlink,
            )

            self.assertEqual(
                policy.codex_executable,
                trusted_executable.resolve(),
            )
            self.assertTrue(policy.codex_executable.is_absolute())

            missing = root / "missing-codex"
            non_executable = root / "non-executable-codex"
            non_executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            workspace_executable = workspace / "codex"
            workspace_executable.write_text(
                "#!/bin/sh\nexit 0\n",
                encoding="utf-8",
            )
            workspace_executable.chmod(0o700)
            cases = (
                (
                    "missing",
                    missing,
                    "could not be resolved safely",
                ),
                (
                    "not_executable",
                    non_executable,
                    "regular executable file",
                ),
                (
                    "workspace_contained",
                    workspace_executable,
                    "outside the provider-writable workspace",
                ),
            )
            for label, executable, expected_message in cases:
                with self.subTest(label=label):
                    with self.assertRaisesRegex(ValueError, expected_message):
                        ModelExecutionPolicy.for_benchmark(
                            allowed_workspace_root=workspace,
                            invocation_budget=InvocationBudget(1),
                            call_timeout_seconds=1,
                            codex_executable=executable,
                        )

    def test_workspace_quarantine_removes_cross_boundary_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory).resolve()
            workspace = temporary_root / "workspace"
            outside = temporary_root / "outside"
            workspace.mkdir()
            outside.mkdir()
            internal_target = workspace / "internal.txt"
            internal_target.write_text("internal\n", encoding="utf-8")
            internal_link = workspace / "internal-link"
            internal_link.symlink_to(internal_target)
            outside_target = outside / "host-state.txt"
            outside_target.write_text("unchanged\n", encoding="utf-8")
            outward_link = workspace / "outward-link"
            outward_link.symlink_to(outside_target)
            hard_link = workspace / "hard-link"
            os.link(outside_target, hard_link)
            special_file = workspace / "provider-pipe"
            if hasattr(os, "mkfifo"):
                os.mkfifo(special_file)

            removed = quarantine_unsafe_workspace_entries(workspace)

            self.assertTrue(internal_link.is_symlink())
            self.assertEqual(internal_link.resolve(), internal_target)
            self.assertFalse(outward_link.exists())
            self.assertFalse(outward_link.is_symlink())
            self.assertFalse(hard_link.exists())
            if hasattr(os, "mkfifo"):
                self.assertFalse(special_file.exists())
                self.assertIn("special_file", removed)
            self.assertIn("outward_symlink", removed)
            self.assertIn("hard_link", removed)
            self.assertEqual(
                outside_target.read_text(encoding="utf-8"),
                "unchanged\n",
            )

    def test_runner_quarantines_outward_symlink_before_returning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory).resolve()
            allowed_root = temporary_root / "allowed"
            outside_root = temporary_root / "outside"
            workspace = allowed_root / "role"
            workspace.mkdir(parents=True)
            outside_root.mkdir()
            outside_target = outside_root / "host-state.txt"
            outside_target.write_text("unchanged\n", encoding="utf-8")
            policy, budget = self._policy(allowed_root, max_invocations=1)
            runner = CodexRunner(
                RoleRuntimeConfig(model="gpt-benchmark", reasoning="high"),
                role="developer",
                execution_policy=policy,
            )

            def completed_provider(*_args: object, **_kwargs: object) -> object:
                (workspace / "history.md").symlink_to(outside_target)
                return subprocess.CompletedProcess(
                    [str(policy.codex_executable)],
                    0,
                    '{"type":"item.completed","item":{"type":"agent_message","text":"{}"}}\n',
                    "",
                )

            with mock.patch.object(
                runner,
                "_run_benchmark_process",
                side_effect=completed_provider,
            ):
                with self.assertRaisesRegex(
                    ModelExecutionPolicyViolation,
                    "unsafe filesystem entries",
                ):
                    runner.run(workspace, "content-safe prompt", None)

            self.assertFalse((workspace / "history.md").is_symlink())
            self.assertEqual(
                outside_target.read_text(encoding="utf-8"),
                "unchanged\n",
            )
            entry = budget.snapshot()["entries"][0]
            self.assertEqual(entry["state"], "failed")

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
                codex_executable=sys.executable,
                shell_environment={"CODEX_HOME": str(outside)},
            )
            runner = CodexRunner(
                RoleRuntimeConfig(model="gpt-benchmark"),
                execution_policy=policy,
            )

            with self.assertRaises(ModelExecutionPolicyViolation):
                runner.run(workspace, "prompt", None)

            self.assertEqual(budget.reserved_count, 0)

    def test_benchmark_telemetry_output_must_be_outside_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            budget = InvocationBudget(1)

            for output_dir in (workspace, workspace / ".teams_runtime" / "metrics"):
                with self.subTest(output_dir=output_dir):
                    with self.assertRaisesRegex(
                        ValueError,
                        "outside the provider-writable workspace",
                    ):
                        ModelExecutionPolicy.for_benchmark(
                            allowed_workspace_root=workspace,
                            invocation_budget=budget,
                            call_timeout_seconds=1,
                            codex_executable=sys.executable,
                            telemetry_output_dir=output_dir,
                        )

            external_output = root / "private-telemetry"
            policy = ModelExecutionPolicy.for_benchmark(
                allowed_workspace_root=workspace,
                invocation_budget=budget,
                call_timeout_seconds=1,
                codex_executable=sys.executable,
                telemetry_output_dir=external_output,
            )

            self.assertEqual(
                policy.telemetry_output_dir,
                external_output.resolve(),
            )

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
            self.assertEqual(persisted["schema_version"], 3)
            self.assertEqual(persisted["entries"][0]["state"], "timeout")
            self.assertIn(
                "prompt_context_enabled",
                persisted["entries"][0],
            )

    def test_reservation_journals_content_free_prompt_projection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            budget = InvocationBudget(
                1,
                journal_path=root / "call_journal.json",
            )
            context = SimpleNamespace(
                invocation_id="invocation-1",
                operation_id="operation-1",
                logical_call_id="logical-1",
                attempt_index=1,
                attempt_kind="primary",
                runtime_identity="role",
                role="research",
                purpose="research_decision",
                workflow_step="research_initial",
                request_id="request-1",
                sprint_id="sprint-1",
                todo_id="",
                backlog_id="",
                goal_id="",
                prompt_context_enabled=True,
                prompt_context_total_events=50,
                prompt_context_included_events=16,
                prompt_context_omitted_events=34,
                prompt_context_recent_events=8,
                prompt_context_max_events=16,
                prompt_context_selection_policy=(
                    "recent_tail_plus_latest_role_evidence"
                ),
            )

            budget.reserve(
                context,
                provider="codex_cli",
                role="research",
            )
            entry = budget.snapshot()["entries"][0]

            self.assertTrue(entry["prompt_context_enabled"])
            self.assertEqual(entry["prompt_context_total_events"], 50)
            self.assertEqual(entry["prompt_context_included_events"], 16)
            self.assertEqual(entry["prompt_context_omitted_events"], 34)
            self.assertNotIn("prompt", entry)
            self.assertNotIn("response", entry)

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
