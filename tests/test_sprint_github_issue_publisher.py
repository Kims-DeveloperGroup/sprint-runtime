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
    SprintIssuePublishMetadata,
    SprintIssuePublishError,
    collect_sprint_issue_documents,
    load_github_token_dotenv,
    publish_sprint_issue,
    publish_sprint_issue_metadata,
)
from teams_runtime.workflows.state.request_store import save_request

from teams_runtime.tests.orchestration_test_utils import FakeDiscordClient, scaffold_workspace


class RichFakeDiscordClient(FakeDiscordClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.sent_rich: list[dict[str, object]] = []

    async def send_channel_rich_message(
        self,
        channel_id,
        *,
        content="",
        embed=None,
        files=None,
        allowed_mentions=None,
    ):
        self.sent_rich.append(
            {
                "channel_id": str(channel_id),
                "content": str(content or ""),
                "embed": embed,
                "files": list(files or []),
                "allowed_mentions": allowed_mentions,
            }
        )
        return None


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
            (paths.role_sources_dir("planner") / "req-dev.planner_draft.md").write_text(
                "# planner draft\n",
                encoding="utf-8",
            )
            (paths.role_sources_dir("planner") / "req-dev.request.md").write_text(
                "# runtime request snapshot\n",
                encoding="utf-8",
            )
            (paths.role_sources_dir("architect") / "req-dev.architect_review.md").write_text(
                "# architect review\n",
                encoding="utf-8",
            )
            (paths.role_sources_dir("architect") / "req-dev.architect_guidance.md").write_text(
                "# architect guidance\n",
                encoding="utf-8",
            )
            (paths.role_sources_dir("developer") / "req-dev.developer_build.md").write_text(
                "# developer build\n",
                encoding="utf-8",
            )
            (paths.role_sources_dir("qa") / "req-dev.qa_validation.md").write_text(
                "# qa validation\n",
                encoding="utf-8",
            )
            (paths.role_sources_dir("designer") / "other-request.designer_advisory.md").write_text(
                "# unrelated advisory\n",
                encoding="utf-8",
            )
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
            request_labels = [label for label in labels if label.startswith("request/req-dev/")]
            self.assertEqual(
                request_labels,
                [
                    "request/req-dev/planner/sources/req-dev.planner_draft.md",
                    "request/req-dev/architect/sources/req-dev.architect_guidance.md",
                    "request/req-dev/developer/sources/req-dev.developer_build.md",
                    "request/req-dev/architect/sources/req-dev.architect_review.md",
                    "request/req-dev/qa/sources/req-dev.qa_validation.md",
                ],
            )
            self.assertNotIn("request/req-dev/planner/sources/req-dev.request.md", labels)
            self.assertNotIn("request/other-request/designer/sources/other-request.designer_advisory.md", labels)
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
            self.assertIn("req-dev.planner_draft.md", names)
            self.assertIn("req-dev.architect_guidance.md", names)
            self.assertNotIn("req-dev.request.md", names)
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
            (paths.sprint_artifact_dir(folder_name) / "spec.md").write_text(
                "# Sprint Spec\n\n"
                "- sprint_name: 260501-Sprint-12:00\n\n"
                "## Canonical Contract Body\n\n"
                "### First logical unit\n"
                "- request_id: req-one\n"
                "#### 플래너\n"
                "- first planner evidence\n\n"
                "### Second logical unit\n"
                "- request_id: req-two\n"
                "#### QA\n"
                "- second qa evidence\n",
                encoding="utf-8",
            )
            (paths.sprint_artifact_dir(folder_name) / "attachments").mkdir()
            (paths.sprint_artifact_dir(folder_name) / "attachments" / "no-heading.md").write_text(
                "fallback body without a heading\n",
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
                    comments = []
                    for index, (_call_args, body) in enumerate(
                        [(call_args, body) for call_args, body in calls if call_args[:2] == ["issue", "comment"]],
                        start=101,
                    ):
                        comment = {"id": index, "body": body or ""}
                        if "sprint/attachments/no-heading.md" not in (body or ""):
                            comment["html_url"] = f"https://github.com/owner/repo/issues/42#issuecomment-{index}"
                        comments.append(comment)
                    return GhResult(0, json.dumps(comments), "")
                return GhResult(0, "", "")

            metadata = publish_sprint_issue_metadata(
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

            self.assertEqual(metadata.issue_number, 42)
            self.assertEqual(metadata.issue_url, "https://github.com/owner/repo/issues/42")
            self.assertEqual(metadata.repo, "owner/repo")
            create_call = next(stdin for args, stdin in calls if args[:2] == ["issue", "create"])
            self.assertIn("teams-runtime:sprint-issue:260501-Sprint-12:00", create_call or "")
            comment_call = next(stdin for args, stdin in calls if args[:2] == ["issue", "comment"])
            self.assertIn("## sprint/report.md\n\n# final", comment_call or "")
            self.assertIn("- **done**", comment_call or "")
            self.assertNotIn("```text", comment_call or "")
            comment_bodies = [stdin or "" for args, stdin in calls if args[:2] == ["issue", "comment"]]
            report_index = next(index for index, body in enumerate(comment_bodies) if "## sprint/report.md" in body)
            overview_index = next(index for index, body in enumerate(comment_bodies) if "## sprint/spec.md - Overview" in body)
            first_spec_index = next(
                index
                for index, body in enumerate(comment_bodies)
                if "## sprint/spec.md - req-one - First logical unit" in body
            )
            second_spec_index = next(
                index
                for index, body in enumerate(comment_bodies)
                if "## sprint/spec.md - req-two - Second logical unit" in body
            )
            self.assertLess(report_index, overview_index)
            self.assertLess(overview_index, first_spec_index)
            self.assertLess(first_spec_index, second_spec_index)
            self.assertIn("#### 플래너", comment_bodies[first_spec_index])
            self.assertIn("#### QA", comment_bodies[second_spec_index])
            self.assertFalse(any("## sprint/spec.md\n" in body for body in comment_bodies))
            final_issue_body = [stdin or "" for args, stdin in calls if args[:2] == ["issue", "edit"]][-1]
            self.assertIn("## Artifact Index", final_issue_body)
            self.assertIn(
                "1. [final](<https://github.com/owner/repo/issues/42#issuecomment-101>) - sprint/report.md",
                final_issue_body,
            )
            self.assertIn(
                "2. [Sprint Spec](<https://github.com/owner/repo/issues/42#issuecomment-102>) - sprint/spec.md / Overview",
                final_issue_body,
            )
            self.assertIn(
                "3. [First logical unit](<https://github.com/owner/repo/issues/42#issuecomment-103>) - sprint/spec.md / req-one",
                final_issue_body,
            )
            self.assertIn(
                "4. [Second logical unit](<https://github.com/owner/repo/issues/42#issuecomment-104>) - sprint/spec.md / req-two",
                final_issue_body,
            )
            self.assertIn(
                "5. sprint/attachments/no-heading.md - sprint/attachments/no-heading.md",
                final_issue_body,
            )
            self.assertNotIn("[sprint/spec.md - req-one - First logical unit]", final_issue_body)
            self.assertNotIn("[sprint/attachments/no-heading.md]", final_issue_body)

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
                    return GhResult(
                        0,
                        json.dumps(
                            [
                                {
                                    "id": 99,
                                    "body": marker,
                                    "html_url": "https://github.com/owner/repo/issues/42#issuecomment-99",
                                }
                            ]
                        ),
                        "",
                    )
                return GhResult(0, "", "")

            issue_number = publish_sprint_issue(
                paths,
                {"sprint_id": sprint_id, "sprint_folder_name": folder_name, "milestone_title": "Milestone docs"},
                runner=runner,
            )

            self.assertEqual(issue_number, 42)
            self.assertTrue(any(args[:2] == ["issue", "edit"] for args, _stdin in calls))
            self.assertTrue(any(args[:2] == ["api", "repos/owner/repo/issues/comments/99"] for args, _stdin in calls))
            edit_bodies = [stdin or "" for args, stdin in calls if args[:2] == ["issue", "edit"]]
            self.assertGreaterEqual(len(edit_bodies), 2)
            self.assertIn("## Artifact Index", edit_bodies[-1])
            self.assertIn(
                "1. [updated](<https://github.com/owner/repo/issues/42#issuecomment-99>) - sprint/report.md",
                edit_bodies[-1],
            )

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

    def test_publish_sprint_issue_best_effort_persists_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(milestone_title="GitHub metadata", trigger="manual")
                metadata = SprintIssuePublishMetadata(
                    issue_number=42,
                    issue_url="https://github.com/owner/repo/issues/42",
                    repo="owner/repo",
                )

                with patch(
                    "teams_runtime.workflows.orchestration.team_service.publish_sprint_issue_metadata_async",
                    AsyncMock(return_value=metadata),
                ):
                    result = asyncio.run(service._publish_sprint_issue_best_effort(sprint_state))

                self.assertEqual(result, metadata)
                self.assertEqual(sprint_state["github_issue_number"], 42)
                self.assertEqual(sprint_state["github_issue_url"], "https://github.com/owner/repo/issues/42")
                self.assertEqual(sprint_state["github_issue_publish_status"], "published")
                self.assertTrue(sprint_state["github_issue_published_at"])
                updated = service._load_sprint_state(sprint_state["sprint_id"])
                self.assertEqual(updated["github_issue_number"], 42)
                self.assertEqual(updated["github_issue_url"], "https://github.com/owner/repo/issues/42")

    def test_closeout_publishes_issue_before_terminal_discord_reports_and_sends_link_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            issue_url = "https://github.com/owner/repo/issues/42"
            with patch("teams_runtime.core.orchestration.DiscordClient", RichFakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(milestone_title="GitHub publisher", trigger="manual")
                service._finish_scheduler_after_sprint = lambda *_args, **_kwargs: None
                events: list[str] = []

                async def fake_prepare(state, closeout_result):
                    report_body = f"sprint_id={state['sprint_id']}\ncloseout_status={closeout_result['status']}"
                    state["report_body"] = report_body
                    state["report_path"] = service._archive_sprint_history(state, report_body)
                    return report_body

                async def fake_publish(state):
                    events.append("publish")
                    state["github_issue_number"] = 42
                    state["github_issue_url"] = issue_url
                    state["github_issue_publish_status"] = "published"
                    state["github_issue_published_at"] = "2026-05-01T12:00:00+09:00"
                    state["github_issue_publish_updated_at"] = "2026-05-01T12:00:00+09:00"
                    service._save_sprint_state(state)
                    return SprintIssuePublishMetadata(issue_number=42, issue_url=issue_url, repo="owner/repo")

                original_send_terminal = service._send_terminal_sprint_reports

                async def recording_send_terminal(**kwargs):
                    events.append(f"send:{kwargs['sprint_state'].get('github_issue_url')}")
                    return await original_send_terminal(**kwargs)

                service._publish_sprint_issue_best_effort = fake_publish
                service._prepare_and_archive_sprint_report = fake_prepare
                service._send_terminal_sprint_reports = recording_send_terminal

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
                self.assertEqual(events[:2], ["publish", f"send:{issue_url}"])
                self.assertTrue(service.discord_client.sent_rich)
                self.assertEqual(service.discord_client.sent_rich[0]["content"], f"GitHub issue: {issue_url}")
                fields = service.discord_client.sent_rich[0]["embed"].get("fields", [])
                field_names = [field["name"] for field in fields]
                self.assertIn("GitHub Issue", field_names)
                self.assertFalse(any(str(name).startswith("변경 요약") for name in field_names))
                history_text = service.paths.sprint_history_file(sprint_state["sprint_id"]).read_text(encoding="utf-8")
                index_text = service.paths.sprint_history_index_file.read_text(encoding="utf-8")
                self.assertIn(f"- github_issue_url: <{issue_url}>", history_text)
                self.assertIn(f"[#42]({issue_url})", index_text)

    def test_closeout_completion_still_succeeds_when_publisher_fails(self):
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
                self.assertEqual(sprint_state["github_issue_publish_status"], "failed")
                self.assertEqual(sprint_state["github_issue_url"], "")
                service._send_terminal_sprint_reports.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
