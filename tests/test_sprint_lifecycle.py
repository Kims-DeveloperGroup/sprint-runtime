from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.workflows.sprints.lifecycle import (
    INITIAL_PHASE_STEP_ARTIFACT_SYNC,
    INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
    INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
    INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
    INITIAL_PHASE_STEP_TODO_FINALIZATION,
    INITIAL_PHASE_STEPS,
    attachment_storage_relative_path,
    build_idle_current_sprint_markdown,
    build_manual_sprint_names,
    build_manual_sprint_state,
    build_recovered_sprint_todo_from_request,
    build_sprint_planning_request_record,
    collect_sprint_relevant_backlog_items,
    extract_sprint_folder_name,
    initial_phase_step,
    initial_phase_step_instruction,
    initial_phase_step_position,
    initial_phase_step_title,
    is_initial_phase_planner_request,
    is_manual_sprint_cutoff_reached,
    is_sprint_planning_request,
    merge_recovered_sprint_todo,
    normalize_trace_list,
    next_initial_phase_step,
    record_sprint_planning_iteration,
    recover_sprint_todos_from_recovered,
    sort_sprint_todos,
    sprint_todo_dependencies_satisfied,
    sprint_todo_dependency_waiting_on,
    sprint_research_prepass_body_lines,
    sprint_planning_phase_ready,
    sprint_attachment_filename,
    sprint_uses_manual_flow,
    todo_status_from_request_record,
    todo_status_rank,
    uses_manual_daily_sprint,
    validate_initial_phase_step_result,
)


class TeamsRuntimeSprintLifecycleHelperTests(unittest.TestCase):
    def test_manual_flow_detection_uses_runtime_mode_or_sprint_state(self) -> None:
        self.assertTrue(uses_manual_daily_sprint("manual_daily"))
        self.assertTrue(
            sprint_uses_manual_flow(
                sprint_start_mode="auto",
                sprint_state={"execution_mode": "manual"},
            )
        )
        self.assertTrue(
            sprint_uses_manual_flow(
                sprint_start_mode="auto",
                sprint_state={"trigger": "manual_start"},
            )
        )
        self.assertFalse(
            sprint_uses_manual_flow(
                sprint_start_mode="auto",
                sprint_state={"execution_mode": "auto", "trigger": "scheduler"},
            )
        )

    def test_idle_current_sprint_markdown_is_stable(self) -> None:
        self.assertEqual(build_idle_current_sprint_markdown(), "# Current Sprint\n\n- active sprint 없음\n")

    def test_sprint_folder_and_attachment_helpers_preserve_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            sprint_root = Path(tmpdir) / "shared_workspace" / "sprints"
            resolved = sprint_root / "260421-Sprint-20-00" / "attachments" / "brief.md"
            resolved.parent.mkdir(parents=True)
            resolved.write_text("brief", encoding="utf-8")

            self.assertEqual(
                extract_sprint_folder_name({"sprint_folder": "shared_workspace/sprints/custom-folder"}),
                "custom-folder",
            )
            self.assertEqual(
                extract_sprint_folder_name({"sprint_id": "260421-Sprint 20:00"}),
                "260421-Sprint-20-00",
            )
            self.assertEqual(attachment_storage_relative_path(Path("/tmp/reference.pdf")), Path("reference.pdf"))
            self.assertEqual(
                sprint_attachment_filename(
                    "shared_workspace/sprints/260421-Sprint-20-00/attachments/brief.md",
                ),
                "brief.md",
            )
            self.assertEqual(
                sprint_attachment_filename(
                    "",
                    resolved=resolved,
                    sprint_artifacts_root=sprint_root,
                ),
                "brief.md",
            )

    def test_manual_sprint_cutoff_policy_never_blocks_manual_flow(self) -> None:
        self.assertFalse(
            is_manual_sprint_cutoff_reached(
                sprint_start_mode="manual_daily",
                sprint_state={"execution_mode": "manual", "trigger": "manual_start"},
            )
        )

    def test_initial_phase_planning_request_detection(self) -> None:
        request_record = {
            "intent": "plan",
            "params": {
                "_teams_kind": "sprint_internal",
                "sprint_phase": "initial",
                "initial_phase_step": INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
            },
        }

        self.assertTrue(is_sprint_planning_request(request_record))
        self.assertTrue(is_initial_phase_planner_request(request_record))
        self.assertEqual(initial_phase_step(request_record), INITIAL_PHASE_STEP_BACKLOG_DEFINITION)

        non_initial = {"intent": "plan", "params": {"_teams_kind": "sprint_internal", "sprint_phase": "ongoing"}}
        self.assertTrue(is_sprint_planning_request(non_initial))
        self.assertFalse(is_initial_phase_planner_request(non_initial))
        self.assertEqual(initial_phase_step(non_initial), "")

    def test_build_sprint_planning_request_record_preserves_contract_shape(self) -> None:
        record = build_sprint_planning_request_record(
            {
                "sprint_id": "260421-Sprint-19:40",
                "requested_milestone_title": "original milestone",
                "milestone_title": "refined milestone",
                "sprint_name": "Refined Sprint",
                "sprint_folder": "shared_workspace/sprints/refined",
                "kickoff_brief": "keep this brief",
                "kickoff_requirements": ["requirement A"],
                "kickoff_reference_artifacts": ["docs/spec.md"],
            },
            phase="initial",
            iteration=2,
            step=INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
            request_id="request-1",
            artifacts=["shared_workspace/backlog.md", "docs/spec.md"],
            created_at="2026-04-21T19:40:00+09:00",
            updated_at="2026-04-21T19:40:01+09:00",
            git_baseline={"sha": "abc123"},
        )

        self.assertEqual(record["request_id"], "request-1")
        self.assertEqual(record["status"], "queued")
        self.assertEqual(record["next_role"], "planner")
        self.assertEqual(record["params"]["_teams_kind"], "sprint_internal")
        self.assertEqual(record["params"]["initial_phase_step"], INITIAL_PHASE_STEP_BACKLOG_DEFINITION)
        self.assertIn("sprint initial backlog_definition for refined milestone", record["scope"])
        self.assertIn("kickoff_brief:", record["body"])
        self.assertIn("Backlog zero is invalid", record["body"])
        self.assertEqual(record["artifacts"], ["shared_workspace/backlog.md", "docs/spec.md"])
        self.assertEqual(record["git_baseline"], {"sha": "abc123"})
        self.assertTrue(record["fingerprint"])

    def test_sprint_research_prepass_body_lines_include_planning_hints(self) -> None:
        lines = sprint_research_prepass_body_lines(
            {
                "research_prepass": {
                    "request_id": "request-research",
                    "status": "completed",
                    "reason_code": "needed_external_grounding",
                    "subject": "workflow planning evidence",
                    "research_query": "Find workflow planning evidence.",
                    "research_subject_definition": {
                        "planning_decision": "milestone refinement",
                        "knowledge_gap": "source-backed workflow ordering",
                        "external_boundary": "external planning guidance",
                        "planner_impact": "planner should refine milestone and trace todos",
                        "candidate_subject": "workflow planning evidence",
                        "research_query": "Find workflow planning evidence.",
                        "source_requirements": ["workflow planning sources"],
                        "rejected_subjects": ["repo-only implementation details"],
                        "no_subject_rationale": "",
                    },
                    "headline": "Research changes the planning frame.",
                    "planner_guidance": "planner는 evidence traceability를 반영해야 합니다.",
                    "milestone_refinement_hints": ["추상 milestone을 evidence traceability contract로 구체화합니다."],
                    "todo_definition_hints": ["research refs를 backlog origin에 남깁니다."],
                    "backing_reasoning": ["Source explains why planner cannot keep the abstract milestone."],
                    "backing_sources": [
                        {
                            "title": "Workflow Source",
                            "url": "https://example.com/workflow",
                        }
                    ],
                }
            }
        )

        body = "\n".join(lines)
        self.assertIn("milestone_refinement_hints", body)
        self.assertIn("research_subject_definition", body)
        self.assertIn("planning_decision: milestone refinement", body)
        self.assertIn("todo_definition_hints", body)
        self.assertIn("backing_reasoning", body)
        self.assertIn("Workflow Source | https://example.com/workflow", body)

    def test_initial_phase_step_metadata_is_stable(self) -> None:
        self.assertEqual(
            list(INITIAL_PHASE_STEPS),
            [
                INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
                INITIAL_PHASE_STEP_ARTIFACT_SYNC,
                INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
                INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
                INITIAL_PHASE_STEP_TODO_FINALIZATION,
            ],
        )
        self.assertEqual(initial_phase_step_title(INITIAL_PHASE_STEP_ARTIFACT_SYNC), "plan/spec 동기화")
        self.assertEqual(initial_phase_step_position(INITIAL_PHASE_STEP_TODO_FINALIZATION), 5)
        self.assertEqual(
            next_initial_phase_step(INITIAL_PHASE_STEP_BACKLOG_DEFINITION),
            INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
        )
        self.assertEqual(next_initial_phase_step(INITIAL_PHASE_STEP_TODO_FINALIZATION), "")
        self.assertIn(
            "Backlog zero is invalid",
            initial_phase_step_instruction(INITIAL_PHASE_STEP_BACKLOG_DEFINITION),
        )
        self.assertEqual(initial_phase_step_instruction("unknown"), "")

    def test_collect_sprint_relevant_backlog_items_filters_and_sorts(self) -> None:
        sprint_state = {
            "sprint_id": "sprint-1",
            "milestone_title": "Login cleanup",
        }
        backlog_items = [
            {"backlog_id": "done", "status": "done", "milestone_title": "Login cleanup"},
            {
                "backlog_id": "late",
                "status": "pending",
                "milestone_title": "Login cleanup",
                "priority_rank": 2,
                "created_at": "2026-04-20T02:00:00",
            },
            {
                "backlog_id": "selected",
                "status": "selected",
                "selected_in_sprint_id": "sprint-1",
                "priority_rank": 1,
                "created_at": "2026-04-20T03:00:00",
            },
            {
                "backlog_id": "origin-created",
                "status": "pending",
                "origin": {"sprint_id": "sprint-1"},
                "priority_rank": 0,
                "created_at": "2026-04-20T04:00:00",
            },
            {
                "backlog_id": "unrelated",
                "status": "pending",
                "milestone_title": "Other",
                "priority_rank": 0,
                "created_at": "2026-04-20T01:00:00",
            },
        ]

        relevant = collect_sprint_relevant_backlog_items(sprint_state, backlog_items)

        self.assertEqual([item["backlog_id"] for item in relevant], ["selected", "late", "origin-created"])

    def test_todo_status_rank_and_sorting_are_stable(self) -> None:
        self.assertEqual(todo_status_rank("queued"), 0)
        self.assertEqual(todo_status_rank("running"), 1)
        self.assertEqual(todo_status_rank("uncommitted"), 2)
        self.assertEqual(todo_status_rank("committed"), 4)
        self.assertEqual(todo_status_rank("unknown"), -1)

        todos = [
            {"todo_id": "low", "status": "queued", "priority_rank": 1, "created_at": "2026-04-20T01:00:00"},
            {"todo_id": "running", "status": "running", "priority_rank": 0, "created_at": "2026-04-20T03:00:00"},
            {"todo_id": "high", "status": "queued", "priority_rank": 5, "created_at": "2026-04-20T02:00:00"},
        ]

        self.assertEqual([todo["todo_id"] for todo in sort_sprint_todos(todos)], ["running", "low", "high"])

    def test_ranked_todo_dependencies_require_lower_ranks_to_complete(self) -> None:
        todos = [
            {"todo_id": "rank-1", "status": "queued", "priority_rank": 1},
            {"todo_id": "rank-2", "status": "queued", "priority_rank": 2},
            {"todo_id": "rank-3", "status": "queued", "priority_rank": 3},
        ]

        self.assertTrue(sprint_todo_dependencies_satisfied(todos[0], todos))
        self.assertFalse(sprint_todo_dependencies_satisfied(todos[1], todos))
        self.assertEqual(
            [todo["todo_id"] for todo in sprint_todo_dependency_waiting_on(todos[2], todos)],
            ["rank-1", "rank-2"],
        )

        todos[0]["status"] = "committed"
        self.assertTrue(sprint_todo_dependencies_satisfied(todos[1], todos))
        self.assertFalse(sprint_todo_dependencies_satisfied(todos[2], todos))

    def test_todo_status_from_request_record_prefers_result_status(self) -> None:
        self.assertEqual(
            todo_status_from_request_record({"status": "delegated", "result": {"status": "committed"}}),
            "committed",
        )
        self.assertEqual(todo_status_from_request_record({"status": "delegated", "result": {}}), "running")
        self.assertEqual(todo_status_from_request_record({"status": "unknown", "result": {}}), "queued")

    def test_merge_recovered_sprint_todo_prefers_newer_recovery_and_merges_artifacts(self) -> None:
        existing = {
            "todo_id": "todo-1",
            "status": "queued",
            "artifacts": ["a.md"],
            "version_control_paths": ["old.py"],
            "updated_at": "2026-04-21T01:00:00",
        }
        recovered = {
            "todo_id": "todo-1",
            "status": "completed",
            "summary": "Recovered result.",
            "artifacts": ["b.md"],
            "version_control_paths": ["new.py"],
            "version_control_status": "committed",
            "updated_at": "2026-04-21T02:00:00",
        }

        merged = merge_recovered_sprint_todo(existing, recovered)

        self.assertEqual(merged["status"], "completed")
        self.assertEqual(merged["summary"], "Recovered result.")
        self.assertEqual(merged["artifacts"], ["b.md"])
        self.assertEqual(merged["version_control_paths"], ["new.py"])
        self.assertEqual(merged["version_control_status"], "committed")

        older_recovered = {
            "status": "running",
            "artifacts": ["a.md", "c.md"],
            "version_control_paths": ["old.py", "extra.py"],
            "updated_at": "2026-04-21T00:30:00",
        }

        merged_with_older = merge_recovered_sprint_todo(existing, older_recovered)

        self.assertEqual(merged_with_older["status"], "running")
        self.assertEqual(merged_with_older["artifacts"], ["a.md", "c.md"])
        self.assertEqual(merged_with_older["version_control_paths"], ["old.py", "extra.py"])

    def test_build_recovered_sprint_todo_from_request_uses_backlog_and_request_metadata(self) -> None:
        sprint_state = {"milestone_title": "Runtime cleanup"}
        request_record = {
            "request_id": "req-1",
            "status": "delegated",
            "result": {"status": "completed", "summary": "Done."},
            "backlog_id": "backlog-1",
            "todo_id": "todo-1",
            "next_role": "developer",
            "created_at": "2026-04-21T01:00:00",
            "updated_at": "2026-04-21T02:00:00",
            "version_control_status": "committed",
            "version_control_paths": ["app.py"],
            "version_control_message": "commit ok",
        }
        backlog_item = {
            "backlog_id": "backlog-1",
            "title": "Implement runtime cleanup",
            "milestone_title": "Runtime cleanup",
            "priority_rank": 7,
            "acceptance_criteria": ["tests pass"],
        }

        todo = build_recovered_sprint_todo_from_request(
            sprint_state,
            request_record,
            backlog_item=backlog_item,
            artifacts=["shared_workspace/report.md"],
        )

        self.assertEqual(todo["todo_id"], "todo-1")
        self.assertEqual(todo["owner_role"], "developer")
        self.assertEqual(todo["status"], "completed")
        self.assertEqual(todo["summary"], "Done.")
        self.assertEqual(todo["priority_rank"], 7)
        self.assertEqual(todo["artifacts"], ["shared_workspace/report.md"])
        self.assertEqual(todo["started_at"], "2026-04-21T01:00:00")
        self.assertEqual(todo["ended_at"], "2026-04-21T02:00:00")

    def test_recover_sprint_todos_from_recovered_merges_appends_and_skips_retired_requests(self) -> None:
        sprint_state = {
            "todos": [
                {
                    "todo_id": "todo-existing",
                    "request_id": "req-existing",
                    "backlog_id": "backlog-existing",
                    "status": "queued",
                    "priority_rank": 2,
                    "artifacts": ["old.md"],
                    "updated_at": "2026-04-21T01:00:00",
                },
                {
                    "todo_id": "todo-retry",
                    "request_id": "retry-req",
                    "retry_of_request_id": "retired-req",
                    "status": "running",
                    "priority_rank": 1,
                    "created_at": "2026-04-21T02:00:00",
                },
            ]
        }
        recovered_todos = [
            {
                "todo_id": "todo-existing",
                "request_id": "req-existing",
                "backlog_id": "backlog-existing",
                "status": "completed",
                "summary": "Recovered existing work.",
                "priority_rank": 2,
                "artifacts": ["new.md"],
                "updated_at": "2026-04-21T03:00:00",
            },
            {
                "todo_id": "todo-new",
                "request_id": "req-new",
                "backlog_id": "backlog-new",
                "status": "running",
                "priority_rank": 3,
                "created_at": "2026-04-21T04:00:00",
            },
            {
                "todo_id": "todo-retired",
                "request_id": "retired-req",
                "backlog_id": "backlog-retired",
                "status": "completed",
                "priority_rank": 9,
            },
        ]

        self.assertTrue(recover_sprint_todos_from_recovered(sprint_state, recovered_todos))

        todos_by_id = {todo["todo_id"]: todo for todo in sprint_state["todos"]}
        self.assertEqual([todo["todo_id"] for todo in sprint_state["todos"]], ["todo-retry", "todo-new", "todo-existing"])
        self.assertEqual(todos_by_id["todo-existing"]["status"], "completed")
        self.assertEqual(todos_by_id["todo-existing"]["summary"], "Recovered existing work.")
        self.assertEqual(todos_by_id["todo-existing"]["artifacts"], ["new.md"])
        self.assertNotIn("todo-retired", todos_by_id)

    def test_recover_sprint_todos_from_recovered_returns_false_without_changes(self) -> None:
        existing = {
            "todo_id": "todo-existing",
            "request_id": "req-existing",
            "backlog_id": "backlog-existing",
            "status": "completed",
            "priority_rank": 2,
            "artifacts": ["new.md"],
            "updated_at": "2026-04-21T03:00:00",
        }
        sprint_state = {"todos": [dict(existing)]}

        self.assertFalse(recover_sprint_todos_from_recovered(sprint_state, [dict(existing)]))
        self.assertEqual(sprint_state["todos"], [existing])

    def test_validate_initial_phase_step_result_enforces_backlog_trace_contract(self) -> None:
        sprint_state = {"kickoff_requirements": ["requirement-a"]}
        request_record = {
            "intent": "plan",
            "params": {
                "_teams_kind": "sprint_internal",
                "sprint_phase": "initial",
                "initial_phase_step": INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
            },
        }
        traced_item = {
            "title": "KIS websocket alert contract",
            "milestone_title": "workflow initial",
            "acceptance_criteria": ["신규 호가 이벤트가 알림과 히스토리에 반영된다."],
            "origin": {
                "milestone_ref": "workflow initial",
                "requirement_refs": ["requirement-a"],
                "spec_refs": ["./shared_workspace/sprints/current/spec.md#kis-alert"],
            },
        }

        self.assertIn(
            "sprint-relevant backlog가 0건",
            validate_initial_phase_step_result(
                sprint_state,
                request_record=request_record,
                sync_summary={"planner_persisted_backlog": False},
                relevant_items=[],
            ),
        )
        self.assertIn(
            "planner가 sprint-relevant backlog를 실제로 persist하지 않았습니다",
            validate_initial_phase_step_result(
                sprint_state,
                request_record=request_record,
                sync_summary={"planner_persisted_backlog": False},
                relevant_items=[traced_item],
            ),
        )
        self.assertIn(
            "origin.spec_refs 없음",
            validate_initial_phase_step_result(
                sprint_state,
                request_record=request_record,
                sync_summary={"planner_persisted_backlog": True},
                relevant_items=[{**traced_item, "origin": {"milestone_ref": "workflow initial"}}],
            ),
        )
        self.assertEqual(
            validate_initial_phase_step_result(
                sprint_state,
                request_record=request_record,
                sync_summary={"planner_persisted_backlog": True},
                relevant_items=[traced_item],
            ),
            "",
        )

    def test_validate_initial_phase_step_result_enforces_prioritization_and_todo_completion(self) -> None:
        sprint_state = {
            "sprint_id": "Sprint-01",
            "selected_items": [],
            "selected_backlog_ids": [],
            "todos": [],
        }
        prioritization_request = {
            "intent": "plan",
            "params": {
                "_teams_kind": "sprint_internal",
                "sprint_phase": "initial",
                "initial_phase_step": INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
            },
        }
        todo_request = {
            "intent": "plan",
            "params": {
                "_teams_kind": "sprint_internal",
                "sprint_phase": "initial",
                "initial_phase_step": INITIAL_PHASE_STEP_TODO_FINALIZATION,
            },
        }

        self.assertIn(
            "priority_rank 없음",
            validate_initial_phase_step_result(
                sprint_state,
                request_record=prioritization_request,
                sync_summary={},
                relevant_items=[{"title": "priority missing", "milestone_title": "workflow initial"}],
            ),
        )
        self.assertEqual(
            validate_initial_phase_step_result(
                sprint_state,
                request_record=prioritization_request,
                sync_summary={},
                relevant_items=[{"title": "priority set", "milestone_title": "workflow initial", "priority_rank": 1}],
            ),
            "",
        )
        self.assertIn(
            "selected backlog 또는 sprint todo",
            validate_initial_phase_step_result(
                sprint_state,
                request_record=todo_request,
                sync_summary={},
                relevant_items=[{"title": "ready", "milestone_title": "workflow initial", "priority_rank": 1}],
            ),
        )

        sprint_state.update(
            {
                "selected_items": [
                    {
                        "backlog_id": "backlog-1",
                        "title": "ready",
                        "planned_in_sprint_id": "Sprint-01",
                    }
                ],
                "selected_backlog_ids": ["backlog-1"],
                "todos": [{"todo_id": "todo-1", "backlog_id": "backlog-1"}],
            }
        )
        self.assertEqual(
            validate_initial_phase_step_result(
                sprint_state,
                request_record=todo_request,
                sync_summary={},
                relevant_items=[{"title": "ready", "milestone_title": "workflow initial", "priority_rank": 1}],
            ),
            "",
        )

    def test_validate_initial_phase_step_result_enforces_research_backlog_trace(self) -> None:
        sprint_state = {
            "kickoff_requirements": ["requirement-a"],
            "research_prepass": {
                "status": "completed",
                "backing_sources": [{"title": "Workflow Source", "url": "https://example.com/workflow"}],
            },
        }
        request_record = {
            "intent": "plan",
            "params": {
                "_teams_kind": "sprint_internal",
                "sprint_phase": "initial",
                "initial_phase_step": INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
            },
        }
        item = {
            "title": "Research-traced workflow contract",
            "milestone_title": "workflow initial",
            "acceptance_criteria": ["workflow가 research trace를 보존한다."],
            "origin": {
                "milestone_ref": "workflow initial",
                "requirement_refs": ["requirement-a"],
                "spec_refs": ["./shared_workspace/sprints/current/spec.md#workflow"],
            },
        }

        self.assertIn(
            "origin.research_refs 없음",
            validate_initial_phase_step_result(
                sprint_state,
                request_record=request_record,
                sync_summary={"planner_persisted_backlog": True},
                relevant_items=[item],
            ),
        )
        self.assertEqual(
            validate_initial_phase_step_result(
                sprint_state,
                request_record=request_record,
                sync_summary={"planner_persisted_backlog": True},
                relevant_items=[
                    {
                        **item,
                        "origin": {
                            **item["origin"],
                            "research_refs": ["Workflow Source | https://example.com/workflow"],
                        },
                    }
                ],
            ),
            "",
        )

    def test_validate_initial_phase_step_result_blocks_copy_through_researched_milestone(self) -> None:
        sprint_state = {
            "requested_milestone_title": "Improve runtime planning",
            "research_prepass": {
                "status": "completed",
                "backing_sources": [{"title": "Workflow Source", "url": "https://example.com/workflow"}],
            },
        }
        request_record = {
            "intent": "plan",
            "params": {
                "_teams_kind": "sprint_internal",
                "sprint_phase": "initial",
                "initial_phase_step": INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
            },
            "result": {
                "proposals": {
                    "sprint_plan_update": {
                        "revised_milestone_title": "Improve runtime planning",
                        "refinement_rationale": "Research indicates planner needs stronger traceability.",
                    }
                }
            },
        }

        self.assertIn(
            "user 요청 milestone을 그대로 채택했습니다",
            validate_initial_phase_step_result(
                sprint_state,
                request_record=request_record,
                sync_summary={},
                relevant_items=[],
            ),
        )

        request_record["result"]["proposals"]["sprint_plan_update"].update(
            {
                "problem_framing": "추상 milestone을 source-backed planning traceability 문제로 재구성합니다.",
                "research_refs": ["Workflow Source | https://example.com/workflow"],
            }
        )
        self.assertEqual(
            validate_initial_phase_step_result(
                sprint_state,
                request_record=request_record,
                sync_summary={},
                relevant_items=[],
            ),
            "",
        )

    def test_normalize_trace_list_matches_orchestration_compatibility(self) -> None:
        self.assertEqual(normalize_trace_list([" a ", "", 3]), ["a", "3"])
        self.assertEqual(normalize_trace_list(" one item "), ["one item"])
        self.assertEqual(normalize_trace_list({"not": "supported"}), [])

    def test_sprint_planning_phase_ready_only_allows_initial_ready_at_todo_finalization(self) -> None:
        sprint_state = {"selected_items": [{"backlog_id": "B-1"}]}

        self.assertFalse(
            sprint_planning_phase_ready(
                sprint_state,
                phase="initial",
                step=INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
            )
        )
        self.assertTrue(
            sprint_planning_phase_ready(
                sprint_state,
                phase="initial",
                step=INITIAL_PHASE_STEP_TODO_FINALIZATION,
            )
        )
        self.assertTrue(
            sprint_planning_phase_ready(
                sprint_state,
                phase="ongoing",
                step="",
            )
        )

    def test_record_sprint_planning_iteration_replaces_matching_request_and_phase(self) -> None:
        sprint_state = {
            "planning_iterations": [
                {
                    "created_at": "2026-04-20T09:00:00+09:00",
                    "phase": "initial",
                    "step": INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
                    "request_id": "req-1",
                    "summary": "old summary",
                    "insights": [],
                    "artifacts": [],
                    "phase_ready": False,
                }
            ]
        }
        request_record = {"request_id": "req-1"}
        result = {
            "summary": "updated summary ",
            "insights": [" insight ", ""],
            "artifacts": [" spec.md ", ""],
        }

        record_sprint_planning_iteration(
            sprint_state,
            created_at="2026-04-20T10:00:00+09:00",
            phase="initial",
            step=INITIAL_PHASE_STEP_TODO_FINALIZATION,
            request_record=request_record,
            result=result,
            phase_ready=True,
        )

        self.assertEqual(len(sprint_state["planning_iterations"]), 1)
        entry = sprint_state["planning_iterations"][0]
        self.assertEqual(entry["created_at"], "2026-04-20T09:00:00+09:00")
        self.assertEqual(entry["summary"], "updated summary")
        self.assertEqual(entry["insights"], ["insight"])
        self.assertEqual(entry["artifacts"], ["spec.md"])
        self.assertTrue(entry["phase_ready"])

    def test_build_manual_sprint_names_returns_display_and_folder_names(self) -> None:
        display_name, folder_name = build_manual_sprint_names(
            sprint_id="260420-Sprint-21:30",
            milestone_title="Login cleanup",
        )

        self.assertTrue(display_name.endswith("-Login cleanup"))
        self.assertEqual(folder_name, "260420-Sprint-21-30")

    def test_build_manual_sprint_state_normalizes_kickoff_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            started_at = datetime(2026, 4, 20, 21, 30, tzinfo=ZoneInfo("Asia/Seoul"))

            sprint_state = build_manual_sprint_state(
                milestone_title=" Login cleanup ",
                trigger="manual_start",
                sprint_cutoff_time="22:00",
                sprint_artifacts_root=paths.sprint_artifacts_root,
                git_baseline={"head": "abc123", "dirty": False},
                started_at=started_at,
                kickoff_brief="Keep scope  \n",
                kickoff_requirements=["Draft plan", "Draft plan", "", "Review"],
                kickoff_request_text="start sprint  \nrequirements",
                kickoff_source_request_id=" request-1 ",
                kickoff_reference_artifacts=["a.md", "a.md", "b.md"],
            )

            self.assertEqual(sprint_state["sprint_id"], "260420-Sprint-21:30")
            self.assertEqual(sprint_state["sprint_folder_name"], "260420-Sprint-21-30")
            self.assertEqual(
                Path(sprint_state["sprint_folder"]),
                paths.sprint_artifacts_root / "260420-Sprint-21-30",
            )
            self.assertEqual(sprint_state["milestone_title"], "Login cleanup")
            self.assertEqual(sprint_state["kickoff_brief"], "Keep scope")
            self.assertEqual(sprint_state["kickoff_requirements"], ["Draft plan", "Review"])
            self.assertEqual(sprint_state["kickoff_request_text"], "start sprint\nrequirements")
            self.assertEqual(sprint_state["kickoff_source_request_id"], "request-1")
            self.assertEqual(sprint_state["kickoff_reference_artifacts"], ["a.md", "b.md"])
            self.assertEqual(sprint_state["reference_artifacts"], ["a.md", "b.md"])
            self.assertEqual(sprint_state["git_baseline"], {"head": "abc123", "dirty": False})
            self.assertEqual(sprint_state["phase"], "initial")
            self.assertEqual(sprint_state["status"], "planning")
            self.assertEqual(sprint_state["execution_mode"], "manual")
            self.assertEqual(sprint_state["cutoff_at"], "2026-04-20T22:00:00+09:00")
            self.assertEqual(sprint_state["selected_items"], [])
            self.assertEqual(sprint_state["todos"], [])


if __name__ == "__main__":
    unittest.main()
