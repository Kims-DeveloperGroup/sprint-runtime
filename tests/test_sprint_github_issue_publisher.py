from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from teams_runtime.core.orchestration import TeamService
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.workflows.sprints.github_issue_publisher import (
    GhResult,
    SprintIssuePublishError,
    collect_sprint_issue_documents,
    load_github_token_dotenv,
    publish_sprint_issue,
)
from teams_runtime.workflows.state.request_store import save_request

from orchestration_test_utils import FakeDiscordClient, scaffold_workspace


class SprintGithubIssuePublisherTests(unittest.TestCase):
    def test_load_github_token_dotenv_imports_gh_token_without_overriding_existing_env(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            scaffold_workspace(root)
            (root / ".env").write_text("GH_TOKEN=from-dotenv\nGITHUB_TOKEN=github-dotenv\n", encoding="utf-8")

            with patch.dict("os.environ", {}, clear=True):
                loaded = load_github_token_dotenv(RuntimePaths.from_root(root))
                self.assertEqual(loaded, (root / ".env").resolve())
                self.assertEqual(os.environ.get("GH_TOKEN"), "from-dotenv")
                self.assertEqual(os.environ.get("GITHUB_TOKEN"), "github-dotenv")

            with patch.dict("os.environ", {"GH_TOKEN": "existing"}, clear=True):
                load_github_token_dotenv(RuntimePaths.from_root(root))
                self.assertEqual(os.environ.get("GH_TOKEN"), "existing")

    def test_collect_sprint_issue_documents_orders_sprint_docs_and_excludes_shared_status_docs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            sprint_id = "260501-Sprint-12:00"
            folder_name = "260501-Sprint-12-00"
            sprint_dir = paths.sprint_artifact_dir(folder_name)
            (sprint_dir / "research").mkdir(parents=True)
            for filename in ("kickoff.md", "milestone.md", "plan.md", "spec.md", "iteration_log.md", "todo_backlog.md", "report.md"):
                (sprint_dir / filename).write_text(f"# {filename}\n", encoding="utf-8")
            (sprint_dir / "research" / "req-research.md").write_text("# research\n", encoding="utf-8")
            (sprint_dir / "attachments").mkdir()
            (sprint_dir / "attachments" / "role-note.md").write_text("# role artifact\n", encoding="utf-8")
            (sprint_dir / "attachments" / "state.json").write_text("{}", encoding="utf-8")
            role_doc = paths.shared_workspace_root / "role-result.md"
            role_doc.write_text("# result\n", encoding="utf-8")
            (paths.role_root("developer") / "history.md").write_text("# private\n", encoding="utf-8")
            save_request(
                paths,
                {
                    "request_id": "req-dev",
                    "sprint_id": sprint_id,
                    "artifacts": ["shared_workspace/role-result.md", "shared_workspace/backlog.md"],
                    "reference_artifacts": ["shared_workspace/current_sprint.md"],
                    "result": {
                        "artifacts": ["developer/history.md", "shared_workspace/completed_backlog.md"],
                    },
                },
                update_timestamp=False,
            )

            docs = collect_sprint_issue_documents(
                paths,
                {
                    "sprint_id": sprint_id,
                    "sprint_folder_name": folder_name,
                    "todos": [
                        {
                            "request_id": "req-dev",
                            "artifacts": [
                                "shared_workspace/role-result.md",
                                "shared_workspace/backlog.md",
                            ],
                        }
                    ],
                },
            )

            labels = [doc.label for doc in docs]
            self.assertEqual(labels[:2], ["sprint/todo_backlog.md", "sprint/report.md"])
            names = {doc.path.name for doc in docs}
            self.assertIn("kickoff.md", names)
            self.assertIn("report.md", names)
            self.assertIn("todo_backlog.md", names)
            self.assertNotIn("backlog.md", names)
            self.assertNotIn("completed_backlog.md", names)
            self.assertNotIn("current_sprint.md", names)
            self.assertIn("req-research.md", names)
            self.assertIn("role-note.md", names)
            self.assertIn("role-result.md", names)
            self.assertNotIn("state.json", names)
            self.assertNotIn("history.md", names)

    def test_publish_sprint_issue_creates_issue_and_document_comments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            sprint_id = "260501-Sprint-12:00"
            folder_name = "260501-Sprint-12-00"
            paths.sprint_artifact_dir(folder_name).mkdir(parents=True)
            (paths.sprint_artifact_dir(folder_name) / "report.md").write_text(
                "# final\n\n- **done**\n",
                encoding="utf-8",
            )
            calls: list[tuple[list[str], str | None]] = []

            def runner(args, stdin=None):
                calls.append((list(args), stdin))
                joined = " ".join(args)
                if args == ["repo", "view", "--json", "nameWithOwner"]:
                    return GhResult(0, json.dumps({"nameWithOwner": "owner/repo"}), "")
                if args[:2] == ["issue", "list"] and "teams-runtime:sprint-issue" in joined:
                    return GhResult(0, "[]", "")
                if args[:2] == ["issue", "list"]:
                    return GhResult(0, json.dumps([{"number": 7, "title": "Similar", "state": "OPEN"}]), "")
                if args[:2] == ["issue", "create"]:
                    return GhResult(0, "https://github.com/owner/repo/issues/42\n", "")
                if args == ["api", "repos/owner/repo/issues/42/comments"]:
                    return GhResult(0, "[]", "")
                return GhResult(0, "", "")

            issue_number = publish_sprint_issue(
                paths,
                {
                    "sprint_id": sprint_id,
                    "sprint_folder_name": folder_name,
                    "milestone_title": "Milestone docs",
                    "status": "completed",
                    "closeout_status": "verified",
                },
                runner=runner,
            )

            self.assertEqual(issue_number, 42)
            create_call = next(stdin for args, stdin in calls if args[:2] == ["issue", "create"])
            self.assertIn("teams-runtime:sprint-issue:260501-Sprint-12:00", create_call or "")
            comment_call = next(stdin for args, stdin in calls if args[:2] == ["issue", "comment"])
            self.assertIn("## sprint/report.md\n\n# final", comment_call or "")
            self.assertIn("- **done**", comment_call or "")
            self.assertNotIn("```text", comment_call or "")

    def test_publish_sprint_issue_updates_existing_issue_and_comment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            sprint_id = "260501-Sprint-12:00"
            folder_name = "260501-Sprint-12-00"
            sprint_dir = paths.sprint_artifact_dir(folder_name)
            sprint_dir.mkdir(parents=True)
            (sprint_dir / "report.md").write_text("# updated\n", encoding="utf-8")
            marker = "<!-- teams-runtime:sprint-doc:260501-Sprint-12:00:sprint/report.md:part-1 -->"
            calls: list[tuple[list[str], str | None]] = []

            def runner(args, stdin=None):
                calls.append((list(args), stdin))
                joined = " ".join(args)
                if args == ["repo", "view", "--json", "nameWithOwner"]:
                    return GhResult(0, json.dumps({"nameWithOwner": "owner/repo"}), "")
                if args[:2] == ["issue", "list"] and "teams-runtime:sprint-issue" in joined:
                    return GhResult(0, json.dumps([{"number": 42, "body": "<!-- teams-runtime:sprint-issue:260501-Sprint-12:00 -->"}]), "")
                if args[:2] == ["issue", "list"]:
                    return GhResult(0, "[]", "")
                if args == ["api", "repos/owner/repo/issues/42/comments"]:
                    return GhResult(0, json.dumps([{"id": 99, "body": marker}]), "")
                return GhResult(0, "", "")

            issue_number = publish_sprint_issue(
                paths,
                {"sprint_id": sprint_id, "sprint_folder_name": folder_name, "milestone_title": "Milestone docs"},
                runner=runner,
            )

            self.assertEqual(issue_number, 42)
            self.assertTrue(any(args[:2] == ["issue", "edit"] for args, _stdin in calls))
            self.assertTrue(any(args[:2] == ["api", "repos/owner/repo/issues/comments/99"] for args, _stdin in calls))

    def test_missing_token_reports_explicit_message(self):
        def runner(args, stdin=None):
            if args[:2] == ["auth", "status"]:
                return GhResult(1, "", "not logged in")
            return GhResult(0, json.dumps({"nameWithOwner": "owner/repo"}), "")

        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with self.assertRaises(SprintIssuePublishError) as raised:
                publish_sprint_issue(RuntimePaths.from_root(tmpdir), {"sprint_id": "sprint-1"}, runner=runner)

            self.assertEqual(raised.exception.stage, "auth")
            self.assertIn("GitHub token missing", str(raised.exception))

    def test_closeout_schedules_publisher_and_completion_still_succeeds_when_publisher_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(milestone_title="GitHub publisher", trigger="manual")
                service._prepare_and_archive_sprint_report = AsyncMock(return_value="report")
                service._send_terminal_sprint_reports = AsyncMock(return_value=None)
                service._finish_scheduler_after_sprint = lambda *_args, **_kwargs: None

                async def failing_publish(_state):
                    raise SprintIssuePublishError("auth", "GitHub token missing. Run gh auth login or set GH_TOKEN/GITHUB_TOKEN.", next_action="Run gh auth login or set GH_TOKEN/GITHUB_TOKEN.")

                service._publish_sprint_issue_best_effort = failing_publish

                result = asyncio.run(
                    service._complete_terminal_sprint(
                        sprint_state,
                        status="completed",
                        closeout_status="verified",
                        terminal_title="done",
                        message="done",
                    )
                )

                self.assertEqual(result["status"], "verified")
                service._send_terminal_sprint_reports.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
