from __future__ import annotations

import tempfile
from pathlib import Path
import unittest
from typing import Any

from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.workflows.sprints.lifecycle import (
    INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
    INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
)
from teams_runtime.workflows.sprints.reporting import (
    archive_sprint_history,
    build_closeout_terminal_report_context,
    build_sprint_closeout_result,
    build_sprint_closeout_state_update,
    build_sprint_terminal_state_update,
    build_terminal_sprint_report_context,
    build_sprint_history_archive_payload,
    build_sprint_history_archive_update,
    build_sprint_report_archive_state,
    build_sprint_report_path_text,
    build_planner_closeout_artifacts,
    build_planner_closeout_context_payload,
    build_planner_closeout_envelope_payload,
    build_planner_closeout_request_context,
    build_planner_initial_phase_activity_report,
    build_planner_initial_phase_activity_sections,
    build_derived_closeout_result_from_sprint_state,
    build_generic_sprint_report_sections,
    build_sprint_delivered_change,
    build_machine_sprint_report_lines,
    build_sprint_report_snapshot,
    build_sprint_change_behavior_summary,
    build_sprint_change_how_lines,
    build_sprint_change_meaning,
    build_sprint_change_summary_lines,
    extract_sprint_change_subject,
    render_backlog_status_report,
    build_sprint_progress_log_summary,
    build_sprint_achievement_lines,
    build_sprint_agent_contribution_lines,
    build_sprint_artifact_lines,
    build_sprint_commit_lines,
    build_sprint_followup_lines,
    build_sprint_kickoff_preview_lines,
    build_sprint_kickoff_report_context,
    build_sprint_progress_report,
    collect_sprint_role_report_events,
    collect_sprint_report_artifacts,
    build_sprint_headline,
    build_sprint_issue_lines,
    build_sprint_kickoff_report_sections,
    build_sprint_overview_lines,
    build_sprint_planned_todo_lines,
    build_sprint_spec_todo_report_body,
    build_sprint_spec_todo_report_sections,
    build_sprint_timeline_lines,
    build_sprint_todo_list_report_body,
    build_sprint_todo_list_report_context,
    build_sprint_todo_list_report_sections,
    build_terminal_sprint_report_sections,
    format_backlog_report_line,
    format_sprint_duration,
    format_sprint_report_text,
    format_todo_report_line,
    limit_sprint_report_lines,
    planner_closeout_request_id,
    planner_initial_phase_next_action,
    planner_initial_phase_priority_lines,
    planner_initial_phase_report_key,
    planner_initial_phase_report_keys,
    planner_initial_phase_work_lines,
    preview_sprint_artifact_path,
    relative_workspace_path,
    resolve_sprint_change_behavior_text,
    resolve_sprint_change_title,
    should_refresh_sprint_history_archive,
    render_live_sprint_report_markdown,
    render_sprint_iteration_log_markdown,
    render_sprint_kickoff_markdown,
    render_sprint_kickoff_report_body,
    render_sprint_milestone_markdown,
    render_sprint_plan_markdown,
    render_sprint_status_report,
    render_sprint_report_body,
    render_sprint_completion_user_report,
    render_sprint_spec_markdown,
    render_sprint_todo_backlog_markdown,
    parse_sprint_report_fields,
    parse_sprint_report_int_field,
    parse_sprint_report_list_field,
    refresh_sprint_report_body,
    refresh_sprint_history_archive,
    sprint_artifact_paths,
    sprint_role_display_name,
    sprint_status_label,
)


def _format_text(value: Any, *, full_detail: bool = False, limit: int = 240) -> str:
    normalized = " ".join(str(value or "").split())
    if full_detail:
        return normalized
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(0, limit - 1)].rstrip() + "…"


def _role_label(role: str) -> str:
    return {
        "planner": "플래너",
        "developer": "개발자",
        "qa": "QA",
    }.get(str(role or "").strip().lower(), str(role or "").strip() or "기타")


def _preview_artifact(_sprint_state: dict[str, Any], raw_path: str) -> str:
    return str(raw_path or "").strip()


def _count_summary(counts: dict[str, int], ordered_keys: list[str] | tuple[str, ...]) -> str:
    parts: list[str] = []
    for key in ordered_keys:
        value = int(counts.get(key) or 0)
        if value > 0:
            parts.append(f"{key}:{value}")
    return ", ".join(parts) if parts else "N/A"


class TeamsRuntimeSprintReportingTests(unittest.TestCase):
    def test_format_sprint_text_and_duration_helpers_match_service_contract(self):
        self.assertEqual(format_sprint_report_text(" one   two "), "one two")
        self.assertEqual(format_sprint_report_text("x" * 245, limit=10), "xxxxxxxxx…")
        self.assertEqual(format_sprint_report_text("x" * 245, full_detail=True, limit=10), "x" * 245)
        self.assertEqual(
            format_sprint_duration(
                {
                    "started_at": "2026-04-20T09:00:00+00:00",
                    "ended_at": "2026-04-21T11:03:00+00:00",
                }
            ),
            "1일 2시간 3분",
        )
        self.assertEqual(format_sprint_duration({"started_at": ""}), "N/A")

    def test_archive_and_refresh_sprint_history_owns_write_side_effects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            sprint_state = {
                "sprint_id": "260421-Sprint-13:40",
                "status": "completed",
                "milestone_title": "history archive ownership",
                "started_at": "2026-04-21T13:00:00+00:00",
                "ended_at": "2026-04-21T13:40:00+00:00",
                "report_body": "# Sprint Report\n\nDone.",
                "report_path": "",
                "todos": [{"title": "archive helper", "status": "completed"}],
            }

            archived_path = archive_sprint_history(paths, sprint_state, sprint_state["report_body"])

            self.assertEqual(archived_path, str(paths.sprint_history_file("260421-Sprint-13:40")))
            self.assertIn("history archive ownership", paths.sprint_history_file("260421-Sprint-13:40").read_text())
            self.assertIn("260421-Sprint-13:40", paths.sprint_history_index_file.read_text())

            sprint_state["report_path"] = "old-report.md"
            self.assertTrue(refresh_sprint_history_archive(paths, sprint_state))
            self.assertEqual(sprint_state["report_path"], archived_path)
            self.assertFalse(refresh_sprint_history_archive(paths, sprint_state))

    def test_sprint_report_delivery_helpers_build_body_artifacts_and_progress_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            sprint_state = {
                "sprint_id": "260421-Sprint-14:10",
                "selected_items": [{"backlog_id": "backlog-1", "title": "delivery item"}],
                "todos": [
                    {
                        "todo_id": "todo-1",
                        "title": "report delivery split",
                        "owner_role": "planner",
                    }
                ],
            }
            todo_body = build_sprint_todo_list_report_body(
                sprint_state
            )
            kickoff_context = build_sprint_kickoff_report_context(sprint_state)
            todo_context = build_sprint_todo_list_report_context(sprint_state)
            artifacts = collect_sprint_report_artifacts(
                paths,
                active_sprint_id="260421-Sprint-14:10",
                related_artifacts=["shared_workspace/sprints/demo/report.md", ""],
            )
            report = build_sprint_progress_report(
                rendered_title="🚀 스프린트 시작",
                sprint_scope="현재 스프린트: 260421-Sprint-14:10",
                body="sprint started",
                report_artifacts=artifacts,
            )

            self.assertIn("sprint_id=260421-Sprint-14:10", todo_body)
            self.assertIn("- todo-1 | report delivery split | owner=planner", todo_body)
            self.assertEqual(kickoff_context["title"], "🚀 스프린트 시작")
            self.assertIn("report delivery split", kickoff_context["body"])
            self.assertEqual(todo_context["title"], "스프린트 TODO")
            self.assertIn("report delivery split", todo_context["body"])
            self.assertIn("shared_workspace/sprints/demo/report.md", artifacts)
            self.assertIn(str(paths.current_sprint_file), artifacts)
            self.assertIn(str(paths.sprint_events_file("260421-Sprint-14:10")), artifacts)
            self.assertIn("현재 스프린트: 260421-Sprint-14:10", report)
            self.assertIn("[상세]", report)
            self.assertIn("sprint started", report)

    def test_build_terminal_sprint_report_sections_preserves_section_order(self):
        sections = build_terminal_sprint_report_sections(
            {"sprint_id": "260421-Sprint-14:30"},
            {},
            build_overview_lines=lambda *_args, **_kwargs: ["overview"],
            build_change_summary_lines=lambda *_args, **_kwargs: ["change"],
            build_planned_todo_lines=lambda *_args, **_kwargs: ["planned"],
            build_commit_lines=lambda *_args, **_kwargs: ["commit"],
            build_followup_lines=lambda *_args, **_kwargs: ["followup"],
            build_timeline_lines=lambda *_args, **_kwargs: ["timeline"],
            build_agent_contribution_lines=lambda *_args, **_kwargs: ["agent"],
            build_issue_lines=lambda *_args, **_kwargs: ["issue"],
            build_achievement_lines=lambda *_args, **_kwargs: ["achievement"],
            build_artifact_lines=lambda *_args, **_kwargs: ["artifact"],
        )

        self.assertEqual(
            [section.title for section in sections],
            ["한눈에 보기", "변경 요약", "후속 조치", "Sprint A to Z", "에이전트 기여", "핵심 이슈", "성과", "참고 아티팩트"],
        )
        self.assertEqual(sections[0].lines, ("overview",))

    def test_sprint_report_utility_helpers_preserve_teamservice_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            artifact = workspace_root / "shared_workspace" / "sprints" / "sprint-a" / "nested" / "report.md"
            external = Path(tmpdir).parent / "external" / "secret" / "trace.log"

            self.assertEqual(sprint_status_label("running"), "진행중")
            self.assertEqual(sprint_status_label("custom"), "custom")
            self.assertEqual(
                limit_sprint_report_lines([" a ", "", "b", "c"], limit=2),
                ["a", "b", "- ... 외 1건"],
            )
            self.assertEqual(
                preview_sprint_artifact_path(
                    {"sprint_folder_name": "sprint-a"},
                    str(artifact),
                    workspace_root=workspace_root,
                ),
                "nested/report.md",
            )
            self.assertEqual(
                preview_sprint_artifact_path(
                    {},
                    "workspace/teams_generated/shared_workspace/report.md",
                    workspace_root=workspace_root,
                ),
                "shared_workspace/report.md",
            )
            self.assertEqual(
                preview_sprint_artifact_path(
                    {"sprint_folder_name": "sprint-a", "sprint_folder": str(workspace_root / "shared_workspace" / "sprints" / "sprint-a")},
                    str(artifact),
                    workspace_root=workspace_root,
                    full_detail=True,
                ),
                "nested/report.md",
            )
            self.assertEqual(
                preview_sprint_artifact_path({}, str(workspace_root / "libs" / "foo.py"), workspace_root=workspace_root),
                "libs/foo.py",
            )
            self.assertEqual(
                preview_sprint_artifact_path({}, str(external), workspace_root=workspace_root, full_detail=True),
                "secret/trace.log",
            )
            self.assertEqual(planner_closeout_request_id({"sprint_id": "Sprint A"}), "planner-closeout-report-sprint-a")
            self.assertEqual(relative_workspace_path(workspace_root / "shared_workspace" / "report.md", workspace_root), "shared_workspace/report.md")

    def test_final_report_planned_todos_commits_and_followups(self):
        sprint_state = {
            "todos": [
                {"title": "low", "status": "completed", "priority_rank": 20, "owner_role": "qa", "todo_id": "todo-2"},
                {
                    "title": "high",
                    "status": "blocked",
                    "priority_rank": 1,
                    "owner_role": "developer",
                    "todo_id": "todo-1",
                    "backlog_id": "backlog-1",
                    "request_id": "request-1",
                    "summary": "waiting on decision",
                    "carry_over_backlog_id": "backlog-carry",
                },
            ],
            "sprint_folder_name": "sprint-a",
        }
        snapshot = {
            "todos": sprint_state["todos"],
            "commits": [
                {"sha": "abcdef123456", "short_sha": "abcdef1", "subject": "add report", "sprint_tagged": True},
                {"sha": "123456abcdef", "short_sha": "123456a", "subject": "misc", "sprint_tagged": False},
            ],
            "commit_count": 2,
            "sprint_tagged_commit_count": 1,
            "uncommitted_paths": ["/outside/build/output.txt"],
        }

        planned = "\n".join(build_sprint_planned_todo_lines(sprint_state, snapshot, format_text=_format_text))
        commits = "\n".join(build_sprint_commit_lines(snapshot))
        followups = "\n".join(
            build_sprint_followup_lines(
                sprint_state,
                snapshot,
                format_text=_format_text,
                preview_artifact=lambda _state, path: Path(path).name,
            )
        )

        self.assertLess(planned.index("high"), planned.index("low"))
        self.assertIn("owner=developer", planned)
        self.assertIn("carry-over=backlog-carry", planned)
        self.assertIn("`abcdef1` add report", commits)
        self.assertIn("`123456a` misc | sprint_id 태그 없음", commits)
        self.assertIn("[blocked] high", followups)
        self.assertIn("[uncommitted_path] output.txt", followups)

    def test_planner_initial_phase_report_helpers_preserve_contract(self):
        request_record = {
            "request_id": "request-1",
            "artifacts": ["shared_workspace/backlog.md"],
            "params": {
                "_teams_kind": "sprint_internal",
                "initial_phase_step": INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
                "milestone_title": "Original milestone",
            },
            "planner_initial_phase_report_keys": ["already-sent", ""],
        }
        sprint_state = {
            "requested_milestone_title": "Original milestone",
            "milestone_title": "Refined milestone",
            "kickoff_requirements": ["Must define backlog"],
        }
        proposals = {
            "backlog_items": [
                {
                    "title": "Define backlog",
                    "summary": "Create sprint-specific backlog",
                }
            ],
            "revised_milestone_title": "Refined milestone",
        }
        semantic_context = {
            "what_summary": "Planner defined the backlog",
            "why_summary": "Backlog zero is invalid",
            "constraint_points": ["Needs owner confirmation"],
        }

        sections = build_planner_initial_phase_activity_sections(
            request_record,
            step=INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
            step_position=3,
            event_type="role_result",
            status="completed",
            summary="done",
            sprint_state=sprint_state,
            proposals=proposals,
            semantic_context=semantic_context,
            backlog_items=[],
            doc_refs=["shared_workspace/current_sprint.md"],
            error_text="",
            format_backlog_line=format_backlog_report_line,
            format_todo_line=lambda todo: format_todo_report_line(todo, include_artifacts=True),
        )
        report = build_planner_initial_phase_activity_report(
            request_record,
            event_type="role_result",
            status="completed",
            summary="done",
            semantic_context=semantic_context,
            sprint_scope="스프린트: demo",
            artifacts=["shared_workspace/backlog.md"],
            sections=sections,
        )
        work_lines = planner_initial_phase_work_lines(
            step=INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
            sprint_state=sprint_state,
            proposals=proposals,
            backlog_items=[],
            format_backlog_line=format_backlog_report_line,
            format_todo_line=lambda todo: format_todo_report_line(todo, include_artifacts=True),
        )
        priority_lines = planner_initial_phase_priority_lines(
            step=INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
            sprint_state=sprint_state,
            proposals=proposals,
            backlog_items=[],
            format_backlog_line=format_backlog_report_line,
            format_todo_line=lambda todo: format_todo_report_line(todo, include_artifacts=True),
        )

        self.assertEqual(planner_initial_phase_report_keys(request_record), ["already-sent"])
        self.assertIn(":backlog_definition:role_result:completed:", planner_initial_phase_report_key(request_record, event_type="role_result", status="completed", summary="done"))
        self.assertEqual(planner_initial_phase_next_action(request_record, "role_result", "completed"), "다음 단계: backlog 우선순위화")
        self.assertIn("- Define backlog | Create sprint-specific backlog", work_lines)
        self.assertIn("- Define backlog | Create sprint-specific backlog", priority_lines)
        self.assertEqual([section.title for section in sections][:3], ["핵심 결론", "마일스톤", "정의된 작업"])
        self.assertIn("planner initial 3/5 체크포인트", report)
        self.assertIn("Planner defined the backlog", report)
        self.assertIn("shared_workspace/backlog.md", report)

    def test_build_sprint_headline_prefers_draft_headline(self):
        headline = build_sprint_headline(
            {"milestone_title": "ignored"},
            {"status_label": "완료", "todo_status_counts": {}, "todos": []},
            draft={"headline": "draft headline wins"},
            format_text=_format_text,
        )

        self.assertEqual(headline, "draft headline wins")

    def test_build_sprint_overview_lines_includes_headline_commit_summary_and_artifacts(self):
        lines = build_sprint_overview_lines(
            {
                "sprint_id": "260419-Sprint-21:30",
                "sprint_name": "sprint-reporting-cut",
                "milestone_title": "report helper extraction",
            },
            {
                "status_label": "완료",
                "closeout_status": "verified",
                "duration": "32분",
                "todo_summary": "committed:2, blocked:1",
                "commit_count": 2,
                "commit_sha": "abcdef0123456789",
                "linked_artifacts": ["a.md", "b.md"],
            },
            headline="report helper extraction 스프린트를 완료했습니다.",
        )

        self.assertEqual(lines[0], "- TL;DR: report helper extraction 스프린트를 완료했습니다.")
        self.assertIn("- commit 요약: 2건 | 대표 abcdef0", lines)
        self.assertIn("- 주요 아티팩트: 2건", lines)

    def test_build_sprint_timeline_lines_prefers_draft_timeline(self):
        lines = build_sprint_timeline_lines(
            {"trigger": "manual_start"},
            {"todos": [], "events": []},
            draft={"timeline": ["- first milestone", "second milestone"]},
            format_text=_format_text,
            role_display_name=_role_label,
        )

        self.assertEqual(lines, ["- first milestone", "- second milestone"])

    def test_build_sprint_timeline_lines_summarizes_planning_execution_and_verification(self):
        lines = build_sprint_timeline_lines(
            {
                "trigger": "manual_start",
                "selected_items": [{"id": "1"}, {"id": "2"}],
                "planning_iterations": [{"summary": "todo를 구체화했습니다."}],
            },
            {
                "todos": [
                    {"owner_role": "planner"},
                    {"owner_role": "developer"},
                ],
                "events": [
                    {"type": "planning_sync", "summary": "milestone과 spec을 맞췄습니다."},
                    {"type": "role_result", "summary": "QA까지 결과를 회수했습니다."},
                ],
                "status_label": "완료",
                "closeout_status": "verified",
                "commit_count": 1,
                "linked_artifacts": ["artifact.md"],
            },
            draft={},
            format_text=_format_text,
            role_display_name=_role_label,
        )

        self.assertEqual(lines[0], "- 시작: `manual_start`로 스프린트를 열고 2건을 작업 범위에 올렸습니다.")
        self.assertIn("- 계획: planning sync 1회로 milestone과 spec을 맞췄습니다.", lines)
        self.assertIn("- 실행: 플래너, 개발자가 todo 2건을 처리했습니다.", lines)
        self.assertIn("- 검증: QA까지 결과를 회수했습니다.", lines)
        self.assertIn("- 마감: 상태 완료, closeout=verified, commit 1건, artifact 1건으로 정리했습니다.", lines)

    def test_build_sprint_agent_contribution_lines_summarizes_roles_and_version_controller(self):
        lines = build_sprint_agent_contribution_lines(
            {
                "version_control_status": "committed",
                "version_control_message": "closeout commit created",
                "version_control_paths": ["shared_workspace/report.md"],
            },
            {
                "todos": [
                    {"owner_role": "planner", "status": "completed", "title": "plan sync", "artifacts": ["plan.md"]},
                    {"owner_role": "developer", "status": "blocked", "title": "runtime fix", "artifacts": ["fix.py"]},
                ],
                "events": [
                    {"payload": {"role": "planner"}, "summary": "planner finalized scope"},
                    {"payload": {"role": "developer"}, "summary": "developer hit follow-up issue"},
                ],
                "closeout_message": "verified closeout",
            },
            draft={},
            format_text=_format_text,
            role_display_name=_role_label,
            preview_artifact=_preview_artifact,
            team_roles=("planner", "developer", "qa"),
        )

        normalized_lines = [line.replace("버전 컨트롤러", "version_controller") for line in lines]
        self.assertEqual(lines[0], "| 역할 | 활동 | 근거/요약 | 참고 산출물 |")
        self.assertEqual(lines[1], "| --- | --- | --- | --- |")
        self.assertIn("| 플래너 (planner) | todo 1건, 완료 1건 | planner finalized scope | plan.md |", lines)
        self.assertIn("| 개발자 (developer) | todo 1건, 이슈 1건 | developer hit follow-up issue | fix.py |", lines)
        self.assertIn(
            "| version_controller (version_controller) | 이벤트 1건 | closeout commit created | shared_workspace/report.md |",
            normalized_lines,
        )

    def test_build_sprint_agent_contribution_lines_renders_planner_draft_as_table(self):
        lines = build_sprint_agent_contribution_lines(
            {},
            {},
            draft={
                "agent_contributions": [
                    {
                        "role": "planner",
                        "summary": "draft summary with a | separator",
                        "artifacts": ["plan.md", "report.md"],
                    }
                ]
            },
            format_text=_format_text,
            role_display_name=_role_label,
            preview_artifact=_preview_artifact,
            team_roles=("planner", "developer", "qa"),
        )

        self.assertEqual(lines[0], "| 역할 | 활동 | 근거/요약 | 참고 산출물 |")
        self.assertIn(
            "| 플래너 (planner) | 기여 기록 | draft summary with a \\| separator | plan.md, report.md |",
            lines,
        )

    def test_build_sprint_issue_lines_surfaces_blocked_todos_uncommitted_paths_and_role_errors(self):
        lines = build_sprint_issue_lines(
            {"status": "running"},
            {
                "todos": [
                    {
                        "status": "blocked",
                        "title": "verification follow-up",
                        "summary": "문서 재확인이 필요합니다.",
                    }
                ],
                "uncommitted_paths": ["a.py", "b.py", "c.py", "d.py"],
                "events": [
                    {"payload": {"role": "qa", "error": "recheck required"}},
                ],
            },
            draft={},
            format_text=_format_text,
            role_display_name=_role_label,
            preview_artifact=_preview_artifact,
        )

        self.assertIn("- [blocked] verification follow-up: 문서 재확인이 필요합니다.", lines)
        self.assertIn("- 참고: 미커밋 경로 4건 | a.py, b.py, c.py 외 1건", lines)
        self.assertIn("- QA 이슈: recheck required", lines)

    def test_build_sprint_achievement_and_artifact_lines_prefer_completed_work_and_linked_artifacts(self):
        achievement_lines = build_sprint_achievement_lines(
            {"status": "completed"},
            {
                "todos": [{"status": "committed", "title": "report cleanup"}],
                "commit_count": 2,
                "commit_sha": "abcdef0123456789",
                "linked_artifacts": [{"status": "committed", "title": "report cleanup", "path": "shared_workspace/report.md"}],
                "closeout_message": "closeout verified",
            },
            draft={},
            format_text=_format_text,
        )
        artifact_lines = build_sprint_artifact_lines(
            {"version_control_paths": ["fallback.md"]},
            {
                "linked_artifacts": [{"status": "committed", "title": "report cleanup", "path": "shared_workspace/report.md"}],
            },
            draft={},
            format_text=_format_text,
            preview_artifact=_preview_artifact,
        )

        self.assertIn("- [committed] report cleanup", achievement_lines)
        self.assertIn("- closeout commit 2건을 남겼습니다. 대표 SHA=abcdef0", achievement_lines)
        self.assertIn("- 주요 산출물 1건을 report에 연결했습니다.", achievement_lines)
        self.assertIn("- closeout verified", achievement_lines)
        self.assertEqual(artifact_lines, ["- 참고: [committed] report cleanup -> shared_workspace/report.md"])

    def test_render_sprint_completion_user_report_renders_sections_and_report_link(self):
        report = render_sprint_completion_user_report(
            title="스프린트 완료",
            sprint_state={"sprint_id": "260419-Sprint-22:00"},
            snapshot={
                "status_label": "완료",
                "duration": "12분",
                "todo_summary": "committed:1",
                "commit_count": 1,
                "commit_sha": "abcdef0123456789",
                "linked_artifacts": [{"path": "shared_workspace/report.md"}],
            },
            report_path_text="shared_workspace/sprints/260419-Sprint-22:00/report.md",
            decorate_title=lambda title: f"✅ {title}",
            build_headline=lambda *_args: "headline",
            build_change_summary_lines=lambda *_args: ["- change"],
            build_planned_todo_lines=lambda *_args: ["- planned"],
            build_commit_lines=lambda *_args: ["- commit"],
            build_followup_lines=lambda *_args: ["- followup"],
            build_timeline_lines=lambda *_args: ["- timeline"],
            build_agent_contribution_lines=lambda *_args: ["- contribution"],
            build_issue_lines=lambda *_args: ["- issue"],
            build_achievement_lines=lambda *_args: ["- achievement"],
            build_artifact_lines=lambda *_args: ["- artifact"],
        )

        self.assertIn("## ✅ 스프린트 완료 사용자 요약", report)
        self.assertIn("**TL;DR** headline", report)
        self.assertIn("commits   : 1 (abcdef0)", report)
        self.assertIn("🔄 변경 요약", report)
        self.assertNotIn("📋 계획된 TODO", report)
        self.assertNotIn("🔖 커밋", report)
        self.assertIn("➡️ 후속 조치", report)
        self.assertIn("🧭 흐름", report)
        self.assertIn("🤖 에이전트 기여", report)
        self.assertIn("⚠️ 핵심 이슈", report)
        self.assertIn("🏁 성과", report)
        self.assertIn("📎 참고 아티팩트", report)
        self.assertIn("상세 보고: `shared_workspace/sprints/260419-Sprint-22:00/report.md`", report)

    def test_report_path_and_history_archive_helpers_cover_relative_and_terminal_states(self):
        relative_path = build_sprint_report_path_text(
            Path("/workspace/root/shared_workspace/sprints/demo/report.md"),
            Path("/workspace/root"),
        )
        absolute_path = build_sprint_report_path_text(
            Path("/tmp/demo/report.md"),
            Path("/workspace/root"),
        )

        self.assertEqual(relative_path, "shared_workspace/sprints/demo/report.md")
        self.assertEqual(absolute_path, "demo/report.md")
        self.assertTrue(
            should_refresh_sprint_history_archive(
                {
                    "sprint_id": "260419-Sprint-22:00",
                    "report_body": "# Sprint Report",
                    "status": "completed",
                    "ended_at": "",
                }
            )
        )
        self.assertTrue(
            should_refresh_sprint_history_archive(
                {
                    "sprint_id": "260419-Sprint-22:01",
                    "report_body": "# Sprint Report",
                    "status": "running",
                    "ended_at": "2026-04-19T12:00:00Z",
                }
            )
        )
        self.assertFalse(
            should_refresh_sprint_history_archive(
                {
                    "sprint_id": "260419-Sprint-22:02",
                    "report_body": "",
                    "status": "completed",
                    "ended_at": "2026-04-19T12:00:00Z",
                }
            )
        )

    def test_build_sprint_history_archive_payload_renders_markdown_and_index(self):
        payload = build_sprint_history_archive_payload(
            sprint_state={
                "sprint_id": "260419-Sprint-22:03",
                "status": "completed",
                "milestone_title": "archive helper extraction",
                "started_at": "2026-04-19T12:00:00Z",
                "ended_at": "2026-04-19T12:17:00Z",
                "commit_sha": "abcdef0123456789",
                "todos": [{"todo_id": "todo-1"}],
            },
            report_body="# Sprint Report\n\nbody",
            history_path=Path("/workspace/root/shared_workspace/sprint_history/260419-Sprint-22:03.md"),
            existing_index_rows=[
                {
                    "sprint_id": "260418-Sprint-21:00",
                    "status": "completed",
                    "milestone_title": "older sprint",
                    "started_at": "2026-04-18T12:00:00Z",
                    "ended_at": "2026-04-18T12:10:00Z",
                    "commit_sha": "older123",
                    "todo_count": 1,
                }
            ],
        )

        self.assertEqual(
            payload["archived_path"],
            "/workspace/root/shared_workspace/sprint_history/260419-Sprint-22:03.md",
        )
        self.assertIn("# Sprint History", payload["history_markdown"])
        self.assertIn("archive helper extraction", payload["history_markdown"])
        self.assertIn("# Sprint History Index", payload["history_index_markdown"])
        self.assertIn("260419-Sprint-22:03", payload["history_index_markdown"])
        self.assertIn("260418-Sprint-21:00", payload["history_index_markdown"])

    def test_build_sprint_history_archive_update_detects_report_path_change(self):
        changed = build_sprint_history_archive_update(
            current_report_path="shared_workspace/sprint_history/old.md",
            archived_path="shared_workspace/sprint_history/new.md",
        )
        unchanged = build_sprint_history_archive_update(
            current_report_path="shared_workspace/sprint_history/new.md",
            archived_path="shared_workspace/sprint_history/new.md",
        )

        self.assertEqual(
            changed,
            {
                "report_path": "shared_workspace/sprint_history/new.md",
                "changed": True,
            },
        )
        self.assertEqual(
            unchanged,
            {
                "report_path": "shared_workspace/sprint_history/new.md",
                "changed": False,
            },
        )

    def test_build_sprint_report_archive_state_normalizes_report_body_and_path(self):
        archive_state = build_sprint_report_archive_state(
            report_body=" # Sprint Report\n\nbody\n",
            report_path=" shared_workspace/sprint_history/demo.md ",
        )

        self.assertEqual(
            archive_state,
            {
                "report_body": "# Sprint Report\n\nbody",
                "report_path": "shared_workspace/sprint_history/demo.md",
            },
        )

    def test_build_sprint_terminal_state_update_normalizes_terminal_fields(self):
        terminal_state = build_sprint_terminal_state_update(
            status=" completed ",
            closeout_status=" no_selected_backlog ",
            ended_at=" 2026-04-19T09:00:00+09:00 ",
        )

        self.assertEqual(
            terminal_state,
            {
                "status": "completed",
                "closeout_status": "no_selected_backlog",
                "ended_at": "2026-04-19T09:00:00+09:00",
            },
        )

    def test_build_sprint_closeout_result_uses_state_defaults_and_override_fields(self):
        closeout_result = build_sprint_closeout_result(
            sprint_state={
                "commit_count": 3,
                "commit_shas": ["abc123", "", "def456"],
                "commit_sha": "abc123",
                "uncommitted_paths": ["left.py", "", "right.py"],
            },
            status="planning_incomplete",
            message="initial phase failed",
            representative_commit_sha="",
        )

        self.assertEqual(
            closeout_result,
            {
                "status": "planning_incomplete",
                "message": "initial phase failed",
                "commit_count": 3,
                "commit_shas": ["abc123", "def456"],
                "commits": [],
                "representative_commit_sha": "",
                "uncommitted_paths": ["left.py", "right.py"],
            },
        )

    def test_build_sprint_closeout_state_update_derives_terminal_completion_fields(self):
        state_update = build_sprint_closeout_state_update(
            closeout_result={
                "status": "warning_missing_sprint_tag",
                "representative_commit_sha": "warn123",
                "commit_count": 1,
                "commit_shas": ["warn123", ""],
                "uncommitted_paths": ["", "workspace/app.py"],
            },
            ended_at="2026-04-19T10:00:00+09:00",
        )

        self.assertEqual(
            state_update,
            {
                "commit_sha": "warn123",
                "commit_shas": ["warn123"],
                "commits": [],
                "commit_count": 1,
                "uncommitted_paths": ["workspace/app.py"],
                "status": "completed",
                "closeout_status": "warning_missing_sprint_tag",
                "ended_at": "2026-04-19T10:00:00+09:00",
            },
        )

    def test_build_closeout_terminal_report_context_derives_title_judgment_and_artifacts(self):
        context = build_closeout_terminal_report_context(
            sprint_state={
                "status": "completed",
                "report_path": "shared_workspace/sprint_history/demo.md",
                "version_control_status": "committed",
                "version_control_message": "reporting.py: improve terminal summary",
                "version_control_paths": ["shared_workspace/report.md", ""],
                "uncommitted_paths": ["tmp/debug.txt", "shared_workspace/report.md"],
            },
            closeout_result={
                "status": "warning_missing_sprint_tag",
                "message": "sprint tag warning",
            },
        )

        self.assertEqual(context["title"], "⚠️ 스프린트 완료(경고)")
        self.assertEqual(context["judgment"], "sprint tag warning")
        self.assertEqual(context["commit_message"], "reporting.py: improve terminal summary")
        self.assertEqual(
            context["related_artifacts"],
            [
                "shared_workspace/sprint_history/demo.md",
                "shared_workspace/report.md",
                "tmp/debug.txt",
            ],
        )

    def test_build_terminal_sprint_report_context_preserves_explicit_terminal_title(self):
        context = build_terminal_sprint_report_context(
            sprint_state={
                "status": "blocked",
                "report_path": "shared_workspace/sprint_history/start-failure.md",
                "version_control_status": "dirty",
                "version_control_message": "should not leak",
                "version_control_paths": ["shared_workspace/spec.md"],
                "uncommitted_paths": ["shared_workspace/spec.md", "shared_workspace/todo.md"],
            },
            closeout_result={
                "status": "planning_incomplete",
                "message": "initial phase planning failed",
            },
            title="⚠️ 스프린트 시작 실패",
        )

        self.assertEqual(context["title"], "⚠️ 스프린트 시작 실패")
        self.assertEqual(context["judgment"], "initial phase planning failed")
        self.assertEqual(context["commit_message"], "")
        self.assertEqual(
            context["related_artifacts"],
            [
                "shared_workspace/sprint_history/start-failure.md",
                "shared_workspace/spec.md",
                "shared_workspace/todo.md",
            ],
        )

    def test_render_live_sprint_report_markdown_orders_actionable_todos(self):
        report = render_live_sprint_report_markdown(
            {
                "sprint_id": "260419-Sprint-22:10",
                "milestone_title": "live report emphasis",
                "phase": "ongoing",
                "todos": [
                    {"status": "blocked", "title": "blocked todo", "priority_rank": 2, "request_id": "req-blocked", "artifacts": ["blocked.md"]},
                    {"status": "queued", "title": "queued todo", "priority_rank": 3, "request_id": "req-queued", "artifacts": []},
                    {"status": "running", "title": "running todo", "priority_rank": 1, "request_id": "req-running", "artifacts": []},
                    {"status": "committed", "title": "done todo", "priority_rank": 4, "request_id": "req-done", "artifacts": []},
                ],
            },
            todo_status_counts={"running": 1, "queued": 1, "blocked": 1, "committed": 1},
            linked_artifacts=[
                {"status": "blocked", "title": "blocked todo", "request_id": "req-blocked", "path": "artifact=blocked.md"}
            ],
            status_label="진행중",
            format_count_summary=_count_summary,
        )

        next_action_section = report.split("## 다음 액션", 1)[1].split("## Todo Summary", 1)[0]
        self.assertIn("- TL;DR: live report emphasis 스프린트가 진행중 상태입니다.", report)
        self.assertIn("- todo 요약: running:1, queued:1, committed:1, blocked:1", report)
        self.assertIn("- todo_summary: running:1, queued:1, committed:1, blocked:1", report)
        self.assertIn("- [running] running todo | request_id=req-running", next_action_section)
        self.assertIn("- [blocked] blocked todo | request_id=req-blocked", next_action_section)
        self.assertIn("- [queued] queued todo | request_id=req-queued", next_action_section)
        self.assertNotIn("done todo", next_action_section)
        self.assertIn("artifact=blocked.md", report)

    def test_build_sprint_progress_log_summary_prefers_issue_over_achievement(self):
        summary = build_sprint_progress_log_summary(
            {"sprint_id": "260419-Sprint-22:30"},
            {
                "todo_summary": "blocked:1",
                "commit_count": 0,
                "linked_artifacts": ["artifact.md"],
            },
            build_headline=lambda *_args: "headline",
            build_issue_lines=lambda *_args: ["- blocked issue"],
            build_achievement_lines=lambda *_args: ["- committed result"],
        )

        self.assertEqual(
            summary,
            "headline\ntodo=blocked:1\ncommit=0, artifact=1\nblocked issue",
        )

    def test_render_sprint_report_body_renders_all_sections_in_order(self):
        body = render_sprint_report_body(
            {"sprint_id": "260419-Sprint-22:40"},
            {},
            {},
            build_overview_lines=lambda *_args: ["- overview"],
            build_change_summary_lines=lambda *_args: ["- changes"],
            build_planned_todo_lines=lambda *_args: ["- planned"],
            build_commit_lines=lambda *_args: ["- commit"],
            build_followup_lines=lambda *_args: ["- followup"],
            build_timeline_lines=lambda *_args: ["- timeline"],
            build_agent_contribution_lines=lambda *_args: ["- contribution"],
            build_issue_lines=lambda *_args: ["- issue"],
            build_achievement_lines=lambda *_args: ["- achievement"],
            build_artifact_lines=lambda *_args: ["- artifact"],
            build_machine_report_lines=lambda *_args: ["machine=true"],
        )

        self.assertIn("# Sprint Report", body)
        self.assertLess(body.index("## 한눈에 보기"), body.index("## 변경 요약"))
        self.assertNotIn("## 계획된 TODO", body)
        self.assertNotIn("## 커밋", body)
        self.assertLess(body.index("## 변경 요약"), body.index("## 후속 조치"))
        self.assertLess(body.index("## 후속 조치"), body.index("## Sprint A to Z"))
        self.assertLess(body.index("## Sprint A to Z"), body.index("## 에이전트 기여"))
        self.assertLess(body.index("## 에이전트 기여"), body.index("## 핵심 이슈"))
        self.assertLess(body.index("## 핵심 이슈"), body.index("## 성과"))
        self.assertLess(body.index("## 성과"), body.index("## 참고 아티팩트"))
        self.assertLess(body.index("## 참고 아티팩트"), body.index("## 머신 요약"))
        self.assertIn("- overview", body)
        self.assertIn("- changes", body)
        self.assertNotIn("- planned", body)
        self.assertNotIn("- commit", body)
        self.assertIn("- timeline", body)
        self.assertIn("- contribution", body)
        self.assertIn("- issue", body)
        self.assertIn("- achievement", body)
        self.assertIn("- artifact", body)
        self.assertIn("machine=true", body)

    def test_render_backlog_status_report_orders_priority_items_and_summaries(self):
        report = render_backlog_status_report(
            active_items=[
                {
                    "status": "selected",
                    "title": "critical bug",
                    "backlog_id": "backlog-1",
                    "kind": "bug",
                    "source": "user",
                },
                {
                    "status": "pending",
                    "title": "follow-up enhancement",
                    "backlog_id": "backlog-2",
                    "kind": "enhancement",
                    "source": "sourcer",
                },
            ],
            counts={"pending": 1, "selected": 1, "blocked": 0, "total": 2},
            kind_counts={"bug": 1, "enhancement": 1},
            source_counts={"user": 1, "sourcer": 1},
            format_count_summary=_count_summary,
        )

        self.assertIn("## Backlog Summary", report)
        self.assertIn("- counts: pending=1, selected=1, blocked=0, total=2", report)
        self.assertIn("- kind_summary: bug:1, enhancement:1", report)
        self.assertIn("- source_summary: user:1, sourcer:1", report)
        self.assertIn("- [selected] critical bug | backlog_id=backlog-1 | kind=bug | source=user", report)
        self.assertIn(
            "- [pending] follow-up enhancement | backlog_id=backlog-2 | kind=enhancement | source=sourcer",
            report,
        )

    def test_render_sprint_status_report_prefers_todos_then_selected_items(self):
        todo_report = render_sprint_status_report(
            {
                "sprint_id": "260419-Sprint-23:10",
                "sprint_name": "status-reporting",
                "phase": "ongoing",
                "milestone_title": "status helper extraction",
                "status": "running",
                "trigger": "manual_start",
                "started_at": "2026-04-19T14:00:00Z",
                "closeout_status": "N/A",
                "commit_count": 1,
                "commit_sha": "abcdef0123456789",
                "todos": [
                    {
                        "status": "running",
                        "title": "extract helper",
                        "todo_id": "todo-1",
                        "backlog_id": "backlog-1",
                        "request_id": "req-1",
                    }
                ],
            },
            is_active=True,
            scheduler_state={"next_slot_at": "2026-04-19T15:00:00Z"},
            todo_status_counts={"running": 1},
            selected_kind_counts={"feature": 1},
            format_count_summary=_count_summary,
        )
        selected_report = render_sprint_status_report(
            {
                "sprint_id": "260419-Sprint-23:20",
                "sprint_display_name": "selected-only",
                "phase": "planning",
                "milestone_title": "selected fallback",
                "status": "queued",
                "selected_items": [
                    {
                        "title": "selected backlog item",
                        "backlog_id": "backlog-2",
                        "kind": "feature",
                    }
                ],
                "todos": [],
            },
            is_active=False,
            scheduler_state={},
            todo_status_counts={},
            selected_kind_counts={"feature": 1},
            format_count_summary=_count_summary,
        )

        self.assertIn("## Sprint Summary", todo_report)
        self.assertIn("- view: active", todo_report)
        self.assertIn("- todo_summary: running:1", todo_report)
        self.assertIn("- backlog_kind_summary: feature:1", todo_report)
        self.assertIn(
            "- [running] extract helper | todo_id=todo-1 | backlog_id=backlog-1 | request_id=req-1",
            todo_report,
        )
        self.assertIn("- view: latest", selected_report)
        self.assertIn("- sprint_name: selected-only", selected_report)
        self.assertIn("- [selected] selected backlog item | backlog_id=backlog-2 | kind=feature", selected_report)

    def test_build_machine_sprint_report_lines_includes_commits_todos_and_artifacts(self):
        lines = build_machine_sprint_report_lines(
            {
                "sprint_id": "260419-Sprint-23:40",
                "sprint_name": "machine-summary",
                "phase": "closeout",
                "milestone_title": "machine helper extraction",
                "sprint_folder": "shared_workspace/sprints/260419-Sprint-23:40",
                "status": "completed",
                "trigger": "manual_start",
                "closeout_status": "verified",
                "version_control_status": "committed",
                "version_control_sha": "deadbeef",
                "auto_commit_status": "not_needed",
                "commit_count": 2,
                "commit_sha": "abcdef0123456789",
                "commit_shas": ["abcdef0123456789"],
                "version_control_paths": ["shared_workspace/report.md"],
                "todos": [
                    {
                        "status": "committed",
                        "title": "extract machine helper",
                        "request_id": "req-machine",
                        "carry_over_backlog_id": "carry-1",
                    }
                ],
                "version_control_message": "commit message",
            },
            {
                "status": "verified",
                "commit_count": 2,
                "representative_commit_sha": "abcdef0123456789",
                "commit_shas": ["abcdef0123456789", "0123456789abcdef"],
                "sprint_tagged_commit_count": 1,
                "sprint_tagged_commit_shas": ["abcdef0123456789"],
                "uncommitted_paths": ["leftover.py"],
                "message": "closeout verified",
            },
            todo_status_counts={"committed": 1},
            linked_artifacts=[
                {
                    "status": "committed",
                    "title": "extract machine helper",
                    "request_id": "req-machine",
                    "path": "shared_workspace/report.md",
                }
            ],
            format_count_summary=_count_summary,
        )

        rendered = "\n".join(lines)
        self.assertIn("sprint_id=260419-Sprint-23:40", rendered)
        self.assertIn("commit_shas=abcdef0123456789, 0123456789abcdef", rendered)
        self.assertIn("sprint_tagged_commit_shas=abcdef0123456789", rendered)
        self.assertIn("uncommitted_paths=leftover.py", rendered)
        self.assertIn("todo_status_counts=committed:1", rendered)
        self.assertNotIn("todo_summary:", rendered)
        self.assertNotIn("linked_artifacts:", rendered)
        self.assertNotIn("- [committed] extract machine helper | request_id=req-machine | carry_over=carry-1", rendered)
        self.assertNotIn("artifact=shared_workspace/report.md", rendered)
        self.assertIn("closeout_message=closeout verified", rendered)
        self.assertIn("version_control_message=commit message", rendered)

    def test_build_sprint_change_helpers_cover_behavior_meaning_and_how(self):
        change = {
            "subject": "status report",
            "what_changed": "더 짧은 요약과 backlog 통계를 함께 보여줍니다.",
            "semantic_context": {},
            "insights": ["status rendering을 sprint_reporting.py로 이동했습니다."],
            "artifacts": ["teams_runtime/core/sprint_reporting.py"],
            "scope": "status report helper extraction",
        }

        behavior = build_sprint_change_behavior_summary(change)
        meaning = build_sprint_change_meaning(change)
        how_lines = build_sprint_change_how_lines(change, format_text=_format_text)

        self.assertEqual(behavior, "이제 status report는 더 짧은 요약과 backlog 통계를 함께 보여줍니다.")
        self.assertIn("출력과 설명이 더 읽기 쉽고 바로 이해되는 방향", meaning)
        self.assertIn("  - 핵심 로직: status rendering을 sprint_reporting.py로 이동했습니다.", how_lines)
        self.assertIn("  - 구현 근거 아티팩트: teams_runtime/core/sprint_reporting.py", how_lines)
        self.assertIn("  - 작업 범위: status report helper extraction", how_lines)

    def test_change_resolution_helpers_prefer_non_meta_semantics_and_subject_hints(self):
        semantic_context = {
            "what_summary": "prompt 구조를 정리했습니다.",
            "what_details": [
                "planner와 developer handoff 기준을 더 명확히 보여줍니다.",
                "문서 정리만 했습니다.",
            ],
        }

        what_changed = resolve_sprint_change_behavior_text(
            semantic_context,
            "task commit summary fallback",
            "plain summary fallback",
        )
        title = resolve_sprint_change_title(
            "프롬프트를 정리했습니다.",
            "developer routing handoff",
            semantic_context,
            what_changed,
        )
        subject = extract_sprint_change_subject(
            title,
            what_changed,
            "developer routing handoff",
        )

        self.assertEqual(what_changed, "planner와 developer handoff 기준을 더 명확히 보여줍니다.")
        self.assertEqual(title, "planner와 developer handoff 기준을 더 명확히 보여줍니다.")
        self.assertEqual(subject, "planner")

    def test_change_resolution_helpers_keep_domain_agent_subject_hints(self):
        subject = extract_sprint_change_subject(
            "김단타 진입 기준 재구성",
            "거래량과 프로그램 순매수가 동시에 붙는 구간에서만 재진입하도록 바뀌었습니다.",
            "김단타 진입 판단 기준 조정",
        )

        self.assertEqual(subject, "김단타")

    def test_change_resolution_helpers_fall_back_to_scope_and_what_changed(self):
        what_changed = resolve_sprint_change_behavior_text({}, "", "commit summary fallback")
        title = resolve_sprint_change_title(
            "문서 정리했습니다.",
            "runtime status rendering",
            {"what_summary": "프롬프트 정리했습니다.", "what_details": []},
            what_changed,
        )

        self.assertEqual(what_changed, "commit summary fallback")
        self.assertEqual(title, "runtime status rendering")

    def test_build_sprint_delivered_change_assembles_title_subject_artifacts_and_why(self):
        change = build_sprint_delivered_change(
            milestone="status/report cleanup",
            title="문서 정리했습니다.",
            scope="status report helper extraction",
            semantic_context={
                "what_summary": "프롬프트를 정리했습니다.",
                "what_details": [
                    "planner와 developer handoff 기준을 더 명확히 보여줍니다.",
                ],
                "why_summary": "report/status 경계를 더 쉽게 이해하도록 돕습니다.",
            },
            insights=["status/report helper 경계를 분리했습니다."],
            artifact_candidates=[
                "teams_runtime/core/sprint_reporting.py",
                "teams_runtime/core/sprint_reporting.py",
                "docs/implementation.md",
            ],
            preview_artifact=lambda raw_path: raw_path if raw_path != "docs/implementation.md" else "",
            what_changed_fallbacks=("commit summary fallback",),
        )

        self.assertEqual(change["title"], "planner와 developer handoff 기준을 더 명확히 보여줍니다.")
        self.assertEqual(change["subject"], "planner")
        self.assertEqual(change["what_changed"], "planner와 developer handoff 기준을 더 명확히 보여줍니다.")
        self.assertEqual(change["artifacts"], ["teams_runtime/core/sprint_reporting.py"])
        self.assertEqual(
            change["why"],
            "`status/report cleanup` 마일스톤을 위해 `status report helper extraction` 작업을 반영했습니다.",
        )
        self.assertEqual(change["semantic_context"]["why_summary"], "report/status 경계를 더 쉽게 이해하도록 돕습니다.")

    def test_build_sprint_report_snapshot_packages_report_fields(self):
        snapshot = build_sprint_report_snapshot(
            sprint_state={
                "commit_count": 1,
                "commit_sha": "fallback123",
                "closeout_status": "fallback_status",
                "uncommitted_paths": ["a.py", "", "b.py"],
            },
            closeout_result={
                "commit_count": 2,
                "representative_commit_sha": "abcdef0123456789",
                "status": "verified",
                "message": "closeout verified",
                "uncommitted_paths": ["main.py", "", "helper.py"],
            },
            todos=[{"id": "todo-1", "status": "committed"}],
            delivered_changes=[{"title": "status helper extraction"}],
            planner_report_draft={"headline": "draft headline"},
            linked_artifacts=[{"path": "shared_workspace/report.md"}],
            todo_status_counts={"committed": 1, "blocked": 1},
            events=[{"type": "planning_sync"}],
            duration="17분",
            status_label="완료",
            format_count_summary=_count_summary,
        )

        self.assertEqual(snapshot["todos"], [{"id": "todo-1", "status": "committed"}])
        self.assertEqual(snapshot["delivered_changes"], [{"title": "status helper extraction"}])
        self.assertEqual(snapshot["planner_report_draft"], {"headline": "draft headline"})
        self.assertEqual(snapshot["linked_artifacts"], [{"path": "shared_workspace/report.md"}])
        self.assertEqual(snapshot["todo_status_counts"], {"committed": 1, "blocked": 1})
        self.assertEqual(snapshot["todo_summary"], "committed:1, blocked:1")
        self.assertEqual(snapshot["commit_count"], 2)
        self.assertEqual(snapshot["commit_sha"], "abcdef0123456789")
        self.assertEqual(snapshot["closeout_status"], "verified")
        self.assertEqual(snapshot["closeout_message"], "closeout verified")
        self.assertEqual(snapshot["uncommitted_paths"], ["main.py", "helper.py"])
        self.assertEqual(snapshot["events"], [{"type": "planning_sync"}])
        self.assertEqual(snapshot["duration"], "17분")
        self.assertEqual(snapshot["status_label"], "완료")

    def test_parse_report_fields_and_refresh_report_body_reuse_machine_summary(self):
        report_body = "\n".join(
            [
                "sprint_id=sprint-1",
                "closeout_status=verified",
                "commit_count=2",
                "commit_sha=abcdef0",
                "commit_shas=abcdef0, 1234567",
                "sprint_tagged_commit_count=1",
                "sprint_tagged_commit_shas=fedcba0",
                "uncommitted_paths=main.py, helper.py",
                "closeout_message=verified cleanly",
            ]
        )
        sprint_state = {"report_body": report_body}

        self.assertEqual(parse_sprint_report_fields(report_body)["sprint_id"], "sprint-1")
        self.assertEqual(parse_sprint_report_list_field("a.py, b.py, "), ["a.py", "b.py"])
        self.assertEqual(parse_sprint_report_list_field("N/A"), [])
        self.assertEqual(parse_sprint_report_int_field("not-a-number"), 0)

        closeout_result = build_derived_closeout_result_from_sprint_state(sprint_state)

        self.assertEqual(closeout_result["status"], "verified")
        self.assertEqual(closeout_result["commit_count"], 2)
        self.assertEqual(closeout_result["commit_shas"], ["abcdef0", "1234567"])
        self.assertEqual(closeout_result["representative_commit_sha"], "abcdef0")
        self.assertEqual(closeout_result["sprint_tagged_commit_count"], 1)
        self.assertEqual(closeout_result["sprint_tagged_commit_shas"], ["fedcba0"])
        self.assertEqual(closeout_result["uncommitted_paths"], ["main.py", "helper.py"])
        self.assertEqual(closeout_result["message"], "verified cleanly")

        captured: dict[str, Any] = {}

        def build_report_body(state: dict[str, Any], derived: dict[str, Any]) -> str:
            captured["state"] = state
            captured["derived"] = derived
            return f"sprint_id=sprint-1\ncloseout_status={derived['status']}\ncommit_count={derived['commit_count']}"

        self.assertTrue(refresh_sprint_report_body(sprint_state, build_report_body=build_report_body))
        self.assertIs(captured["state"], sprint_state)
        self.assertEqual(captured["derived"], closeout_result)
        self.assertEqual(sprint_state["report_body"], "sprint_id=sprint-1\ncloseout_status=verified\ncommit_count=2")

    def test_sprint_artifact_paths_and_basic_markdown_renderers_are_stable(self):
        with self.subTest("paths"):
            with tempfile.TemporaryDirectory() as tmpdir:
                from teams_runtime.core.template import scaffold_workspace
                from teams_runtime.shared.paths import RuntimePaths

                scaffold_workspace(tmpdir)
                paths = RuntimePaths.from_root(tmpdir)
                artifact_paths = sprint_artifact_paths(paths, {"sprint_folder_name": "demo"})

                self.assertEqual(artifact_paths["root"], paths.sprint_artifact_dir("demo"))
                self.assertEqual(artifact_paths["kickoff"], paths.sprint_artifact_file("demo", "kickoff.md"))
                self.assertEqual(artifact_paths["todo_backlog"], paths.sprint_artifact_file("demo", "todo_backlog.md"))

        sprint_state = {
            "requested_milestone_title": "Original milestone",
            "milestone_title": "Runtime cleanup",
            "sprint_name": "runtime-cleanup",
            "phase": "planning",
            "started_at": "2026-04-21T12:00:00",
            "kickoff_source_request_id": "req-1",
            "kickoff_request_text": "Please clean up runtime.",
            "kickoff_brief": "Keep compatibility.",
            "kickoff_requirements": ["No CLI change"],
            "kickoff_reference_artifacts": ["shared_workspace/planning.md"],
            "initial_phase_ready_at": "2026-04-21T12:30:00",
            "planning_iterations": [
                {
                    "summary": "Planner framed the sprint.",
                }
            ],
            "selected_items": [
                {
                    "backlog_id": "backlog-1",
                    "title": "Extract renderer",
                    "status": "selected",
                    "priority_rank": 1,
                    "summary": "Move markdown rendering.",
                }
            ],
        }

        kickoff = render_sprint_kickoff_markdown(sprint_state, source_request_path=".teams_runtime/requests/req-1.json")
        milestone = render_sprint_milestone_markdown(sprint_state)
        plan = render_sprint_plan_markdown(sprint_state)
        todo_backlog = render_sprint_todo_backlog_markdown(sprint_state)

        self.assertIn("- kickoff_source_request: .teams_runtime/requests/req-1.json", kickoff)
        self.assertIn("- No CLI change", kickoff)
        self.assertIn("- Preserve the original kickoff brief in `kickoff.md`.", milestone)
        self.assertIn("Planner framed the sprint.", milestone)
        self.assertIn("- initial_phase_ready_at: 2026-04-21T12:30:00", plan)
        self.assertIn("### Extract renderer", todo_backlog)
        self.assertIn("- backlog_id: backlog-1", todo_backlog)

    def test_sprint_spec_and_iteration_renderers_promote_role_reports(self):
        sprint_state = {
            "sprint_name": "runtime-cleanup",
            "milestone_title": "Runtime cleanup",
            "requested_milestone_title": "Original milestone",
            "planning_iterations": [
                {
                    "created_at": "2026-04-21T12:00:00",
                    "phase": "planning",
                    "request_id": "planning-1",
                    "summary": "Planner framed the sprint.",
                    "insights": ["Preserve compatibility."],
                    "artifacts": ["shared_workspace/sprints/demo/spec.md"],
                    "phase_ready": True,
                }
            ],
        }
        request_record = {
            "request_id": "req-1",
            "status": "blocked",
            "scope": "Move sprint renderers.",
            "events": [
                {
                    "timestamp": "2026-04-21T12:05:00",
                    "type": "delegated",
                    "actor": "orchestrator",
                    "summary": "planner role selected.",
                    "payload": {"routing_context": {"selected_role": "planner", "reason": "planning owner"}},
                },
                {
                    "timestamp": "2026-04-21T12:10:00",
                    "type": "role_report",
                    "actor": "planner",
                    "summary": "Spec updated.",
                    "payload": {
                        "role": "planner",
                        "status": "completed",
                        "summary": "Spec updated.",
                        "insights": ["Spec now names the contract."],
                        "proposals": {
                            "planning_note": {"contract_points": ["session isolation"]},
                            "workflow_transition": {
                                "outcome": "advance",
                                "reason": "developer can continue",
                            },
                        },
                        "artifacts": ["./shared_workspace/sprints/demo/spec.md"],
                    },
                },
            ],
        }
        request_entries = [
            {
                "todo": {
                    "title": "Extract sprint reporting",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                },
                "request": request_record,
            }
        ]

        def transition_provider(payload: dict[str, Any]) -> dict[str, Any]:
            return dict((payload.get("proposals") or {}).get("workflow_transition") or {})

        spec = render_sprint_spec_markdown(
            sprint_state,
            request_entries=request_entries,
            workflow_transition_provider=transition_provider,
        )
        iteration_log = render_sprint_iteration_log_markdown(
            sprint_state,
            request_entries=request_entries,
            workflow_transition_provider=transition_provider,
        )

        self.assertEqual(sprint_role_display_name("planner"), "플래너")
        self.assertEqual(len(collect_sprint_role_report_events(request_record)), 1)
        self.assertIn("## Canonical Contract Body", spec)
        self.assertIn("#### 플래너", spec)
        self.assertIn("session isolation", spec)
        self.assertIn("developer can continue", spec)
        self.assertIn("## Workflow Validation Trace", iteration_log)
        self.assertIn("- selected_role: planner", iteration_log)
        self.assertIn("2026-04-21T12:10:00 | planner | role_report", iteration_log)

    def test_sprint_report_section_helpers_are_stable(self):
        sprint_state = {
            "sprint_id": "sprint-1",
            "trigger": "manual_start",
            "selected_items": [{"title": "Fallback backlog", "priority_rank": 2}],
            "todos": [
                {
                    "todo_id": "todo-1",
                    "title": "Implement report sections",
                    "status": "running",
                    "request_id": "req-1",
                    "priority_rank": 1,
                    "artifacts": ["shared_workspace/sprints/demo/report.md"],
                }
            ],
            "requested_milestone_title": "Original milestone",
            "milestone_title": "Runtime cleanup",
            "kickoff_requirements": ["Keep CLI stable"],
            "planning_iterations": [
                {
                    "summary": "Planner summary.",
                    "insights": ["Insight one"],
                }
            ],
        }

        generic_sections = build_generic_sprint_report_sections("line one\n\nline two")
        kickoff_sections = build_sprint_kickoff_report_sections(
            sprint_state,
            selected_lines=["- [rank 1] Implement report sections"],
        )
        todo_sections = build_sprint_todo_list_report_sections(sprint_state)
        spec_sections = build_sprint_spec_todo_report_sections(
            sprint_state,
            backlog_items=[
                {
                    "backlog_id": "backlog-1",
                    "title": "Extract sections",
                    "status": "selected",
                    "priority_rank": 1,
                }
            ],
            artifact_hints=["shared_workspace/sprints/demo/spec.md"],
            fallback_todo_lines=["- fallback"],
        )
        body = build_sprint_spec_todo_report_body(
            sprint_state,
            todo_lines=["- [rank 1] Implement report sections | request_id=req-1"],
        )
        kickoff_preview = build_sprint_kickoff_preview_lines(sprint_state)
        kickoff_body = render_sprint_kickoff_report_body(sprint_state)

        self.assertEqual(generic_sections[0].title, "상세")
        self.assertEqual(kickoff_sections[0].title, "킥오프")
        self.assertIn("Implement report sections", kickoff_preview[0])
        self.assertIn("📝 kickoff_items:", kickoff_body)
        self.assertIn("artifacts=1", todo_sections[1].lines[0])
        self.assertIn("shared_workspace/sprints/demo/spec.md", "\n".join(spec_sections[-1].lines))
        self.assertIn("[Spec]", body)
        self.assertIn("Insight one", body)
        self.assertIn("backlog_id=backlog-1", format_backlog_report_line({"backlog_id": "backlog-1"}))
        self.assertIn("request_id=req-1", format_todo_report_line(sprint_state["todos"][0]))

    def test_planner_closeout_helpers_build_context_payload_and_deduped_artifacts(self):
        payload = build_planner_closeout_context_payload(
            sprint_state={
                "sprint_id": "260419-Sprint-21:30",
                "sprint_name": "snapshot-cut",
                "milestone_title": "report cleanup",
                "status": "completed",
            },
            closeout_result={"status": "verified", "message": "closeout verified"},
            snapshot={
                "todo_summary": "committed:2",
                "commit_count": 2,
                "commit_sha": "abcdef0123456789",
                "linked_artifacts": [{"path": "shared_workspace/report.md"}],
            },
            request_files=[".teams_runtime/requests/a.json", "", ".teams_runtime/requests/b.json"],
        )
        artifacts = build_planner_closeout_artifacts(
            context_file="planner/context.json",
            sprint_artifact_files=["shared_workspace/sprints/report.md", "shared_workspace/sprints/report.md", ""],
            request_files=[".teams_runtime/requests/a.json", "", ".teams_runtime/requests/a.json", ".teams_runtime/requests/b.json"],
        )

        self.assertEqual(payload["sprint_id"], "260419-Sprint-21:30")
        self.assertEqual(payload["sprint_name"], "snapshot-cut")
        self.assertEqual(payload["milestone_title"], "report cleanup")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["closeout_result"]["status"], "verified")
        self.assertEqual(payload["todo_summary"], "committed:2")
        self.assertEqual(payload["commit_count"], 2)
        self.assertEqual(payload["commit_sha"], "abcdef0123456789")
        self.assertEqual(payload["request_files"], [".teams_runtime/requests/a.json", ".teams_runtime/requests/b.json"])
        self.assertEqual(
            artifacts,
            [
                "planner/context.json",
                "shared_workspace/sprints/report.md",
                ".teams_runtime/requests/a.json",
                ".teams_runtime/requests/b.json",
            ],
        )

    def test_planner_closeout_helpers_build_request_context_and_envelope_payload(self):
        request_context = build_planner_closeout_request_context(
            sprint_state={
                "sprint_id": "260419-Sprint-22:10",
                "milestone_title": "closeout report helper cut",
            },
            closeout_result={
                "status": "verified",
                "message": "closeout generated",
            },
            request_id="planner-closeout-report-260419-sprint-22-10",
            artifacts=["planner/context.json", "", "shared_workspace/sprints/report.md"],
            created_at="2026-04-19T12:00:00Z",
            updated_at="2026-04-19T12:00:00Z",
        )
        envelope_payload = build_planner_closeout_envelope_payload(
            request_id=request_context["request_id"],
            scope=request_context["scope"],
            artifacts=request_context["artifacts"],
        )

        self.assertEqual(request_context["scope"], "260419-Sprint-22:10 closeout report")
        self.assertEqual(
            request_context["body"],
            "Persisted sprint evidence를 읽고 canonical sprint final report용 의미 중심 요약을 작성합니다.",
        )
        self.assertEqual(request_context["params"]["_teams_kind"], "sprint_closeout_report")
        self.assertEqual(request_context["params"]["sprint_id"], "260419-Sprint-22:10")
        self.assertEqual(request_context["params"]["closeout_status"], "verified")
        self.assertEqual(request_context["params"]["closeout_message"], "closeout generated")
        self.assertEqual(request_context["artifacts"], ["planner/context.json", "shared_workspace/sprints/report.md"])
        self.assertEqual(request_context["visited_roles"], ["orchestrator"])
        self.assertEqual(envelope_payload["sender"], "orchestrator")
        self.assertEqual(envelope_payload["target"], "planner")
        self.assertEqual(envelope_payload["scope"], "260419-Sprint-22:10 closeout report")
        self.assertEqual(envelope_payload["params"], {"_teams_kind": "sprint_closeout_report"})
        self.assertIn("`proposals.sprint_report`", envelope_payload["body"])

    def test_build_sprint_change_summary_lines_prefers_draft_changes(self):
        lines = build_sprint_change_summary_lines(
            {"milestone_title": "draft change summary"},
            {"delivered_changes": []},
            draft={
                "changes": [
                    {
                        "title": "draft status helper",
                        "why": "status surface를 더 명확히 보여주기 위해",
                        "what_changed": "status 출력 조립을 helper로 옮겼습니다.",
                        "meaning": "유지보수 시 report/status 경계를 더 쉽게 파악할 수 있습니다.",
                        "how": "wrapper와 helper를 분리했습니다.",
                        "artifacts": ["teams_runtime/core/sprint_reporting.py"],
                    }
                ]
            },
            format_text=_format_text,
        )

        rendered = "\n".join(lines)
        self.assertIn("### draft status helper", rendered)
        self.assertIn("- 왜: status surface를 더 명확히 보여주기 위해", rendered)
        self.assertIn("- 무엇이 달라졌나: status 출력 조립을 helper로 옮겼습니다.", rendered)
        self.assertIn("teams_runtime/core/sprint_reporting.py", rendered)

    def test_build_sprint_change_summary_lines_renders_delivered_changes_and_closeout_fallback(self):
        delivered_lines = build_sprint_change_summary_lines(
            {"milestone_title": "delivered change summary"},
            {
                "delivered_changes": [
                    {
                        "title": "status report extraction",
                        "subject": "status report",
                        "why": "`status --sprint` 출력 조립을 helper로 모으기 위해",
                        "what_changed": "running/queued/blocked 통계를 한 곳에서 렌더링합니다.",
                        "semantic_context": {},
                        "insights": ["report-body와 status-report가 같은 formatting path를 재사용합니다."],
                        "artifacts": ["teams_runtime/core/sprint_reporting.py"],
                        "scope": "status report helper extraction",
                    }
                ],
                "closeout_message": "closeout verified",
            },
            draft={},
            format_text=_format_text,
        )
        fallback_lines = build_sprint_change_summary_lines(
            {"milestone_title": "empty summary fallback"},
            {"delivered_changes": [], "closeout_message": "closeout verified"},
            draft={},
            format_text=_format_text,
        )

        delivered_rendered = "\n".join(delivered_lines)
        fallback_rendered = "\n".join(fallback_lines)
        self.assertIn("### status report extraction", delivered_rendered)
        self.assertIn("- 무엇이 달라졌나: 이제 status report는 running/queued/blocked 통계를 한 곳에서 렌더링합니다.", delivered_rendered)
        self.assertIn("- 의미:", delivered_rendered)
        self.assertIn("  - 참고 아티팩트: teams_runtime/core/sprint_reporting.py", delivered_rendered)
        self.assertIn("delivered change는 없었습니다", fallback_rendered)
        self.assertIn("closeout verified", fallback_rendered)


if __name__ == "__main__":
    unittest.main()
