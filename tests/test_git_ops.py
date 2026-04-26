from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from teams_runtime.workflows.repository_ops import (
    _parse_status_paths,
    auto_commit_task_changes,
    build_version_control_helper_command,
    build_task_commit_message,
    build_sprint_commit_message,
    capture_git_baseline,
    commit_sprint_changes,
    inspect_sprint_closeout,
    run_version_control_payload,
)


def _run_git(repo_root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
        text=True,
    )


class GitOpsCloseoutTests(unittest.TestCase):
    def test_build_version_control_helper_command_uses_payload_file(self):
        command = build_version_control_helper_command("sources/request-1.task.version_control.json")

        self.assertEqual(
            command,
            'python -m teams_runtime.core.git_ops apply-version-control --payload-file "sources/request-1.task.version_control.json"',
        )

    def test_build_task_commit_message_uses_task_format(self):
        message = build_task_commit_message(
            sprint_id="2026-Sprint-01-20260326T164900Z",
            todo_id="todo-164900-abc123",
            backlog_id="backlog-ignored",
            changed_paths=["teams_runtime/core/orchestration.py", "tests/test_orchestration.py"],
            summary="record task-level auto commit diagnostics",
        )

        self.assertEqual(
            message,
            "[2026-Sprint-01-20260326T164900Z] todo-164900-abc123 orchestration.py: record task-level auto commit diagnostics",
        )

    def test_build_task_commit_message_prefers_title_over_verbose_summary(self):
        message = build_task_commit_message(
            sprint_id="2026-Sprint-01-20260326T164900Z",
            todo_id="todo-164900-abc123",
            backlog_id="backlog-ignored",
            changed_paths=["libs/gemini/workflows/intraday/cli.py"],
            summary="기본 cadence 60초 전환과 3분 고정 설명 제거가 코드·문서·회귀 테스트에 일관되게 반영됐고, QA 재실행한 대상 검증도 통과했습니다.",
            title="김단타 1분 이하 사이클 재설계",
        )

        self.assertEqual(
            message,
            "[2026-Sprint-01-20260326T164900Z] todo-164900-abc123 cli.py: 김단타 1분 이하 사이클 재설계",
        )

    def test_build_task_commit_message_prefers_functional_title_when_title_is_meta(self):
        message = build_task_commit_message(
            sprint_id="2026-Sprint-01-20260326T164900Z",
            todo_id="todo-164900-abc123",
            backlog_id="backlog-ignored",
            changed_paths=["teams_runtime/core/orchestration.py"],
            summary="designer advisory를 planner finalization 전용으로 제한합니다.",
            title="designer advisory 계약 정리",
            functional_title="designer advisory를 planner finalization 전용으로 제한",
        )

        self.assertEqual(
            message,
            "[2026-Sprint-01-20260326T164900Z] todo-164900-abc123 orchestration.py: designer advisory를 planner finalization 전용으로 제한",
        )

    def test_parse_status_paths_decodes_git_quoted_non_ascii_paths(self):
        status_output = (
            ' M "apps/\\352\\271\\200\\353\\213\\250\\355\\203\\200/AGENTS.md"\n'
            ' M "apps/\\352\\271\\200\\353\\213\\250\\355\\203\\200/GEMINI.md"\n'
            " M libs/gemini/workflows/intraday/cli.py\n"
        )

        self.assertEqual(
            _parse_status_paths(status_output),
            {
                "apps/김단타/AGENTS.md",
                "apps/김단타/GEMINI.md",
                "libs/gemini/workflows/intraday/cli.py",
            },
        )

    def test_build_task_commit_message_uses_decoded_non_ascii_target_name(self):
        message = build_task_commit_message(
            sprint_id="2026-Sprint-01-20260326T164900Z",
            todo_id="todo-164900-abc123",
            backlog_id="backlog-ignored",
            changed_paths=['"apps/\\352\\271\\200\\353\\213\\250\\355\\203\\200/AGENTS.md"'],
            summary="sync cadence contract",
        )

        self.assertEqual(
            message,
            "[2026-Sprint-01-20260326T164900Z] todo-164900-abc123 AGENTS.md: sync cadence contract",
        )

    def test_build_task_commit_message_prefers_code_target_over_docs_and_tests(self):
        message = build_task_commit_message(
            sprint_id="2026-Sprint-01-20260326T164900Z",
            todo_id="todo-164900-abc123",
            backlog_id="backlog-ignored",
            changed_paths=[
                "apps/김단타/AGENTS.md",
                "apps/김단타/GEMINI.md",
                "libs/gemini/workflows/intraday/cli.py",
                "tests/test_intraday_trader_mode.py",
            ],
            summary="verbose summary",
            title="김단타 1분 이하 사이클 재설계",
        )

        self.assertEqual(
            message,
            "[2026-Sprint-01-20260326T164900Z] todo-164900-abc123 cli.py: 김단타 1분 이하 사이클 재설계",
        )

    def test_build_task_commit_message_prefers_runtime_config_target_over_tests(self):
        message = build_task_commit_message(
            sprint_id="2026-Sprint-01-20260326T164900Z",
            todo_id="todo-164900-abc123",
            backlog_id="backlog-ignored",
            changed_paths=[
                "config/runtime.yaml",
                "tests/test_intraday_trader_mode.py",
            ],
            summary="verbose summary",
            title="런타임 설정 정리",
        )

        self.assertEqual(
            message,
            "[2026-Sprint-01-20260326T164900Z] todo-164900-abc123 runtime.yaml: 런타임 설정 정리",
        )

    def test_build_task_commit_message_uses_docs_target_when_only_docs_changed(self):
        message = build_task_commit_message(
            sprint_id="2026-Sprint-01-20260326T164900Z",
            todo_id="todo-164900-abc123",
            backlog_id="backlog-ignored",
            changed_paths=[
                "apps/김단타/AGENTS.md",
                "apps/김단타/GEMINI.md",
            ],
            summary="verbose summary",
            title="프롬프트 계약 정리",
        )

        self.assertEqual(
            message,
            "[2026-Sprint-01-20260326T164900Z] todo-164900-abc123 AGENTS.md: 프롬프트 계약 정리",
        )

    def test_commit_sprint_changes_creates_sprint_tagged_commit(self):
        sprint_id = "2026-Sprint-01-20260324T123330Z"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _run_git(repo_root, "init")
            _run_git(repo_root, "config", "user.name", "Teams Runtime")
            _run_git(repo_root, "config", "user.email", "teams-runtime@example.com")
            (repo_root / "base.txt").write_text("base\n", encoding="utf-8")
            _run_git(repo_root, "add", "base.txt")
            _run_git(repo_root, "commit", "-m", "base commit")

            baseline = capture_git_baseline(repo_root)

            (repo_root / "app.py").write_text("print('hello')\n", encoding="utf-8")
            commit_result = commit_sprint_changes(repo_root, baseline, build_sprint_commit_message(sprint_id))
            inspect_result = inspect_sprint_closeout(repo_root, baseline, sprint_id)

            self.assertEqual(commit_result["status"], "committed")
            self.assertEqual(commit_result["changed_paths"], ["app.py"])
            self.assertTrue(commit_result["commit_sha"])
            self.assertEqual(inspect_result["status"], "verified")
            self.assertEqual(inspect_result["commit_count"], 1)

    def test_commit_sprint_changes_returns_no_changes_when_nothing_new(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _run_git(repo_root, "init")
            _run_git(repo_root, "config", "user.name", "Teams Runtime")
            _run_git(repo_root, "config", "user.email", "teams-runtime@example.com")
            (repo_root / "base.txt").write_text("base\n", encoding="utf-8")
            _run_git(repo_root, "add", "base.txt")
            _run_git(repo_root, "commit", "-m", "base commit")

            baseline = capture_git_baseline(repo_root)
            result = commit_sprint_changes(repo_root, baseline, build_sprint_commit_message("2026-Sprint-01"))

            self.assertEqual(result["status"], "no_changes")
            self.assertEqual(result["changed_paths"], [])

    def test_inspect_sprint_closeout_warns_when_new_commit_lacks_sprint_id(self):
        sprint_id = "2026-Sprint-01-20260324T123330Z"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _run_git(repo_root, "init")
            _run_git(repo_root, "config", "user.name", "Teams Runtime")
            _run_git(repo_root, "config", "user.email", "teams-runtime@example.com")
            (repo_root / "base.txt").write_text("base\n", encoding="utf-8")
            _run_git(repo_root, "add", "base.txt")
            _run_git(repo_root, "commit", "-m", "base commit")

            baseline = capture_git_baseline(repo_root)

            (repo_root / "other.txt").write_text("other\n", encoding="utf-8")
            _run_git(repo_root, "add", "other.txt")
            _run_git(repo_root, "commit", "-m", "unrelated change")

            result = inspect_sprint_closeout(repo_root, baseline, sprint_id)

            self.assertEqual(result["status"], "warning_missing_sprint_tag")
            self.assertEqual(result["commit_count"], 1)
            self.assertEqual(len(result["commit_shas"]), 1)
            self.assertEqual(result["sprint_tagged_commit_count"], 0)
            self.assertEqual(result["sprint_tagged_commit_shas"], [])

    def test_inspect_sprint_closeout_accepts_sprint_tagged_commit(self):
        sprint_id = "2026-Sprint-01-20260324T123330Z"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _run_git(repo_root, "init")
            _run_git(repo_root, "config", "user.name", "Teams Runtime")
            _run_git(repo_root, "config", "user.email", "teams-runtime@example.com")
            (repo_root / "base.txt").write_text("base\n", encoding="utf-8")
            _run_git(repo_root, "add", "base.txt")
            _run_git(repo_root, "commit", "-m", "base commit")

            baseline = capture_git_baseline(repo_root)

            (repo_root / "app.py").write_text("print('hello')\n", encoding="utf-8")
            _run_git(repo_root, "add", "app.py")
            _run_git(repo_root, "commit", "-m", f"[{sprint_id}] developer: add app")

            result = inspect_sprint_closeout(repo_root, baseline, sprint_id)

            self.assertEqual(result["status"], "verified")
            self.assertEqual(result["commit_count"], 1)
            self.assertEqual(len(result["commit_shas"]), 1)
            self.assertEqual(result["representative_commit_sha"], result["commit_shas"][0])

    def test_auto_commit_task_changes_creates_task_scoped_commit(self):
        sprint_id = "2026-Sprint-01-20260324T123330Z"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _run_git(repo_root, "init")
            _run_git(repo_root, "config", "user.name", "Teams Runtime")
            _run_git(repo_root, "config", "user.email", "teams-runtime@example.com")
            (repo_root / "base.txt").write_text("base\n", encoding="utf-8")
            _run_git(repo_root, "add", "base.txt")
            _run_git(repo_root, "commit", "-m", "base commit")

            baseline = capture_git_baseline(repo_root)

            runtime_file = repo_root / "teams_runtime" / "core" / "orchestration.py"
            runtime_file.parent.mkdir(parents=True, exist_ok=True)
            runtime_file.write_text("value = 1\n", encoding="utf-8")

            result = auto_commit_task_changes(
                repo_root,
                baseline,
                sprint_id=sprint_id,
                todo_id="todo-123330-task001",
                backlog_id="backlog-ignored",
                summary="record task completion auto commit",
            )

            self.assertEqual(result["status"], "committed")
            self.assertEqual(result["changed_paths"], ["teams_runtime/core/orchestration.py"])
            self.assertEqual(
                result["commit_message"],
                f"[{sprint_id}] todo-123330-task001 orchestration.py: record task completion auto commit",
            )

    def test_run_version_control_payload_task_mode_creates_task_commit(self):
        sprint_id = "2026-Sprint-01-20260324T123330Z"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _run_git(repo_root, "init")
            _run_git(repo_root, "config", "user.name", "Teams Runtime")
            _run_git(repo_root, "config", "user.email", "teams-runtime@example.com")
            (repo_root / "base.txt").write_text("base\n", encoding="utf-8")
            _run_git(repo_root, "add", "base.txt")
            _run_git(repo_root, "commit", "-m", "base commit")

            baseline = capture_git_baseline(repo_root)
            runtime_file = repo_root / "teams_runtime" / "core" / "template.py"
            runtime_file.parent.mkdir(parents=True, exist_ok=True)
            runtime_file.write_text("value = 1\n", encoding="utf-8")

            result = run_version_control_payload(
                {
                    "mode": "task",
                    "project_root": str(repo_root),
                    "baseline": baseline,
                    "sprint_id": sprint_id,
                    "todo_id": "todo-123330-task002",
                    "backlog_id": "backlog-ignored",
                    "summary": "delegate task commit to version controller",
                }
            )

            self.assertEqual(result["commit_status"], "committed")
            self.assertTrue(result["change_detected"])
            self.assertEqual(result["commit_message"], f"[{sprint_id}] todo-123330-task002 template.py: delegate task commit to version controller")

    def test_run_version_control_payload_task_mode_prefers_title_and_code_target(self):
        sprint_id = "2026-Sprint-01-20260324T123330Z"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _run_git(repo_root, "init")
            _run_git(repo_root, "config", "user.name", "Teams Runtime")
            _run_git(repo_root, "config", "user.email", "teams-runtime@example.com")
            (repo_root / "base.txt").write_text("base\n", encoding="utf-8")
            _run_git(repo_root, "add", "base.txt")
            _run_git(repo_root, "commit", "-m", "base commit")

            baseline = capture_git_baseline(repo_root)
            (repo_root / "apps" / "김단타" / "AGENTS.md").parent.mkdir(parents=True, exist_ok=True)
            (repo_root / "apps" / "김단타" / "AGENTS.md").write_text("prompt\n", encoding="utf-8")
            (repo_root / "apps" / "김단타" / "GEMINI.md").write_text("prompt\n", encoding="utf-8")
            (repo_root / "libs" / "gemini" / "workflows" / "intraday" / "cli.py").parent.mkdir(parents=True, exist_ok=True)
            (repo_root / "libs" / "gemini" / "workflows" / "intraday" / "cli.py").write_text("value = 1\n", encoding="utf-8")
            (repo_root / "tests" / "test_intraday_trader_mode.py").parent.mkdir(parents=True, exist_ok=True)
            (repo_root / "tests" / "test_intraday_trader_mode.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")

            result = run_version_control_payload(
                {
                    "mode": "task",
                    "project_root": str(repo_root),
                    "baseline": baseline,
                    "sprint_id": sprint_id,
                    "todo_id": "todo-123330-task004",
                    "backlog_id": "backlog-ignored",
                    "title": "김단타 1분 이하 사이클 재설계",
                    "summary": "기본 cadence 60초 전환과 3분 고정 설명 제거가 코드·문서·회귀 테스트에 일관되게 반영됐고, QA 재실행한 대상 검증도 통과했습니다.",
                }
            )

            self.assertEqual(result["commit_status"], "committed")
            self.assertEqual(
                result["commit_message"],
                f"[{sprint_id}] todo-123330-task004 cli.py: 김단타 1분 이하 사이클 재설계",
            )

    def test_auto_commit_task_changes_handles_non_ascii_paths_without_quoted_pathspec_failure(self):
        sprint_id = "2026-Sprint-01-20260324T123330Z"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _run_git(repo_root, "init")
            _run_git(repo_root, "config", "user.name", "Teams Runtime")
            _run_git(repo_root, "config", "user.email", "teams-runtime@example.com")
            (repo_root / "base.txt").write_text("base\n", encoding="utf-8")
            _run_git(repo_root, "add", "base.txt")
            _run_git(repo_root, "commit", "-m", "base commit")

            baseline = capture_git_baseline(repo_root)
            prompt_file = repo_root / "apps" / "김단타" / "AGENTS.md"
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text("cadence\n", encoding="utf-8")

            result = auto_commit_task_changes(
                repo_root,
                baseline,
                sprint_id=sprint_id,
                todo_id="todo-123330-task003",
                backlog_id="backlog-ignored",
                summary="sync cadence prompt",
            )

            self.assertEqual(result["status"], "committed")
            self.assertEqual(result["changed_paths"], ["apps/김단타/AGENTS.md"])
            self.assertEqual(
                result["commit_message"],
                f"[{sprint_id}] todo-123330-task003 AGENTS.md: sync cadence prompt",
            )

    def test_run_version_control_payload_closeout_mode_creates_closeout_commit(self):
        sprint_id = "2026-Sprint-01-20260324T123330Z"
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            _run_git(repo_root, "init")
            _run_git(repo_root, "config", "user.name", "Teams Runtime")
            _run_git(repo_root, "config", "user.email", "teams-runtime@example.com")
            (repo_root / "base.txt").write_text("base\n", encoding="utf-8")
            _run_git(repo_root, "add", "base.txt")
            _run_git(repo_root, "commit", "-m", "base commit")

            baseline = capture_git_baseline(repo_root)
            (repo_root / "workspace.py").write_text("print('closeout')\n", encoding="utf-8")

            result = run_version_control_payload(
                {
                    "mode": "closeout",
                    "project_root": str(repo_root),
                    "baseline": baseline,
                    "sprint_id": sprint_id,
                }
            )

            self.assertEqual(result["commit_status"], "committed")
            self.assertEqual(result["commit_message"], f"[{sprint_id}] chore: sprint closeout")
