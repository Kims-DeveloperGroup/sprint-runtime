from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
                "OPENAI_API_KEY": "provider-secret",
                "GH_TOKEN": "github-secret",
                "DISCORD_TOKEN": "discord-secret",
                "PATH": os.defpath,
            }

            with mock.patch.dict(os.environ, source_environment, clear=True):
                environment = runner._provider_environment()

            self.assertEqual(environment["OPENAI_API_KEY"], "provider-secret")
            self.assertNotIn("GH_TOKEN", environment)
            self.assertNotIn("DISCORD_TOKEN", environment)

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
            self.assertEqual(persisted["entries"][0]["state"], "timeout")


if __name__ == "__main__":
    unittest.main()
