from teams_runtime.tests.orchestration_test_utils import *


def _no_subject_definition(rationale="The sprint handoff is repo-local and does not need external research."):
    return {
        "planning_decision": "",
        "knowledge_gap": "",
        "external_boundary": "",
        "planner_impact": "",
        "candidate_subject": "",
        "research_query": "",
        "source_requirements": [],
        "rejected_subjects": ["repo-local implementation context"],
        "no_subject_rationale": rationale,
    }


class TeamsRuntimeOrchestrationSprintExecutionTests(OrchestrationTestCase):
    def test_sprint_initial_planning_runs_research_prepass_once_before_planner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="source-backed planner kickoff",
                    trigger="test",
                )
                service._save_sprint_state(sprint_state)
                request_record = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step="milestone_refinement",
                )
                workflow = dict(request_record.get("params", {}).get("workflow") or {})
                self.assertEqual(request_record["next_role"], "research")
                self.assertEqual(workflow.get("phase_owner"), "research")
                self.assertEqual(workflow.get("step"), "research_initial")

                delegated_steps: list[tuple[str, str]] = []
                research_artifact = (
                    f"shared_workspace/sprints/{sprint_state['sprint_id']}/research/{request_record['request_id']}.md"
                )

                async def fake_delegate(delegated_request, next_role):
                    workflow = dict(delegated_request.get("params", {}).get("workflow") or {})
                    delegated_steps.append((next_role, str(workflow.get("step") or "")))
                    if next_role == "research":
                        result = {
                            "request_id": delegated_request["request_id"],
                            "role": "research",
                            "status": "completed",
                            "summary": "planner가 참고할 source-backed research prepass를 정리했습니다.",
                            "insights": [],
                            "proposals": {
                                "research_signal": {
                                    "needed": True,
                                    "subject": "Current source-backed sprint planning assumptions",
                                    "research_query": "Collect current sources that constrain sprint planning assumptions.",
                                    "reason_code": "needed_external_grounding",
                                },
                                "research_subject_definition": {
                                    "planning_decision": "sprint milestone refinement and todo traceability",
                                    "knowledge_gap": "whether workflow ordering needs explicit source traceability",
                                    "external_boundary": "workflow planning guidance outside local sprint docs",
                                    "planner_impact": "planner should refine the milestone around research-first evidence flow",
                                    "candidate_subject": "Current source-backed sprint planning assumptions",
                                    "research_query": "Collect current sources that constrain sprint planning assumptions.",
                                    "source_requirements": ["workflow planning sources"],
                                    "rejected_subjects": ["repo-only implementation details"],
                                    "no_subject_rationale": "",
                                },
                                "research_report": {
                                    "report_artifact": research_artifact,
                                    "headline": "외부 근거가 sprint planning assumptions에 영향을 줍니다.",
                                    "planner_guidance": "planner는 source-backed constraints를 milestone/spec 결정에 반영해야 합니다.",
                                    "research_subject_definition": {
                                        "planning_decision": "sprint milestone refinement and todo traceability",
                                        "knowledge_gap": "whether workflow ordering needs explicit source traceability",
                                        "external_boundary": "workflow planning guidance outside local sprint docs",
                                        "planner_impact": "planner should refine the milestone around research-first evidence flow",
                                        "candidate_subject": "Current source-backed sprint planning assumptions",
                                        "research_query": "Collect current sources that constrain sprint planning assumptions.",
                                        "source_requirements": ["workflow planning sources"],
                                        "rejected_subjects": ["repo-only implementation details"],
                                        "no_subject_rationale": "",
                                    },
                                    "milestone_refinement_hints": [
                                        "추상 kickoff를 source-backed workflow ordering contract로 구체화해야 합니다."
                                    ],
                                    "problem_framing_hints": [
                                        "planner가 먼저 정의해야 할 문제는 역할 순서와 evidence traceability입니다."
                                    ],
                                    "spec_implications": [
                                        "spec에는 research-first planning handoff contract가 드러나야 합니다."
                                    ],
                                    "todo_definition_hints": [
                                        "research persistence, planner context, validation을 분리된 slices로 정의합니다."
                                    ],
                                    "backing_reasoning": [
                                        "Runtime workflow source가 planner handoff 순서를 제약합니다."
                                    ],
                                    "backing_sources": [
                                        {
                                            "title": "Runtime Workflow Source",
                                            "url": "https://example.com/runtime-workflow",
                                            "published_at": "2026-04-25",
                                            "relevance": "Constrains planning assumptions.",
                                            "summary": "Explains workflow ordering.",
                                        }
                                    ],
                                    "open_questions": [],
                                    "effective_config": {},
                                },
                            },
                            "artifacts": [research_artifact],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                    elif next_role == "planner":
                        self.assertEqual(delegated_request["result"]["role"], "research")
                        result = {
                            "request_id": delegated_request["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "research prepass를 반영해 milestone refinement를 완료했습니다.",
                            "insights": [],
                            "proposals": {
                                "sprint_plan_update": {
                                    "revised_milestone_title": "source-backed sprint planning workflow contract",
                                    "refinement_rationale": "research source가 planner보다 먼저 확정돼야 하는 evidence traceability를 보여줍니다.",
                                    "problem_framing": "추상 kickoff를 research-first workflow contract와 planner traceability 문제로 발전시킵니다.",
                                    "research_refs": ["Runtime Workflow Source | https://example.com/runtime-workflow"],
                                    "summary": "source-backed planning assumptions를 반영했습니다.",
                                }
                            },
                            "artifacts": ["shared_workspace/current_sprint.md"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                    else:
                        raise AssertionError(f"unexpected delegation: {next_role}")
                    await service._apply_role_result(delegated_request, result, sender_role=next_role)
                    return True

                with patch.object(service, "_delegate_request", side_effect=fake_delegate):
                    result = asyncio.run(
                        service._run_internal_request_chain(
                            sprint_id=sprint_state["sprint_id"],
                            request_record=request_record,
                            initial_role="planner",
                        )
                    )

                self.assertEqual(result["role"], "planner")
                self.assertEqual(delegated_steps, [("research", "research_initial"), ("planner", "planner_draft")])
                updated_request = service._load_request(request_record["request_id"])
                self.assertEqual(updated_request["status"], "completed")
                self.assertEqual(updated_request["current_role"], "orchestrator")

                updated_sprint = service._load_sprint_state(sprint_state["sprint_id"])
                self.assertEqual(updated_sprint["research_prepass"]["request_id"], request_record["request_id"])
                self.assertEqual(updated_sprint["research_prepass"]["backing_sources"][0]["url"], "https://example.com/runtime-workflow")
                self.assertIn("milestone_refinement_hints", updated_sprint["research_prepass"])
                self.assertEqual(
                    updated_sprint["research_prepass"]["research_subject_definition"]["planning_decision"],
                    "sprint milestone refinement and todo traceability",
                )
                self.assertIn(research_artifact, updated_sprint["reference_artifacts"])

                next_planning_request = service._build_sprint_planning_request_record(
                    updated_sprint,
                    phase="initial",
                    iteration=1,
                    step="artifact_sync",
                )
                self.assertEqual(next_planning_request["next_role"], "planner")
                self.assertNotIn("workflow", next_planning_request["params"])
                self.assertIn("research_prepass:", next_planning_request["body"])
                self.assertIn("research_subject_definition:", next_planning_request["body"])
                self.assertIn(research_artifact, next_planning_request["artifacts"])

    def test_sprint_internal_todo_starts_at_planner_without_research_prepass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="internal todo implementation",
                    summary="regular sprint todo should not start with research.",
                    kind="enhancement",
                    source="planner",
                    scope="regular sprint todo",
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="developer")
                sprint_state = {
                    "sprint_id": "2026-Sprint-Regular-Todo",
                    "status": "running",
                    "trigger": "test",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                }

                request_record = service._create_internal_request_record(sprint_state, todo, backlog_item)
                workflow = dict(request_record.get("params", {}).get("workflow") or {})

                self.assertEqual(request_record["next_role"], "planner")
                self.assertEqual(workflow.get("phase_owner"), "planner")
                self.assertEqual(workflow.get("step"), "planner_draft")

    def test_execute_sprint_todo_marks_same_backlog_blocked_without_creating_new_item(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="신규 기획",
                    summary="기획 초안을 만듭니다.",
                    kind="enhancement",
                    source="user",
                    scope="신규 기획",
                )
                backlog_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(backlog_item.get("title") or ""),
                    scope=str(backlog_item.get("scope") or ""),
                    kind=str(backlog_item.get("kind") or ""),
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="planner")
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T000300Z",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-03-24T00:03:00+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                service._save_sprint_state(sprint_state)

                async def fake_run_internal_request_chain(**_kwargs):
                    return {
                        "request_id": "req-blocked-todo",
                        "role": "planner",
                        "status": "blocked",
                        "summary": "도메인과 목표 정보가 없어 계획 수립을 보류합니다.",
                        "insights": [],
                        "proposals": {
                            "required_inputs": ["도메인", "목표"],
                            "recommended_next_step": "오케스트레이터가 기본 정보를 수집한 뒤 planner로 재위임",
                        },
                        "artifacts": [],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "",
                    }

                async def fake_send_sprint_report(**_kwargs):
                    return None

                with (
                    patch.object(service, "_run_internal_request_chain", side_effect=fake_run_internal_request_chain),
                    patch.object(service, "_send_sprint_report", side_effect=fake_send_sprint_report),
                ):
                    asyncio.run(service._execute_sprint_todo(sprint_state, todo))

                updated_backlog = service._load_backlog_item(backlog_item["backlog_id"])
                self.assertEqual(todo["status"], "blocked")
                self.assertEqual(todo["carry_over_backlog_id"], backlog_item["backlog_id"])
                self.assertEqual(updated_backlog["status"], "blocked")
                self.assertEqual(updated_backlog["blocked_reason"], "도메인과 목표 정보가 없어 계획 수립을 보류합니다.")
                self.assertEqual(updated_backlog["required_inputs"], ["도메인", "목표"])
                self.assertEqual(
                    updated_backlog["recommended_next_step"],
                    "오케스트레이터가 기본 정보를 수집한 뒤 planner로 재위임",
                )

    def test_execute_sprint_todo_delegates_task_commit_to_version_controller(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="커밋 강제 보강",
                    summary="todo 완료 시 task 단위 자동 커밋을 보강합니다.",
                    kind="bug",
                    source="user",
                    scope="커밋 강제 보강",
                )
                backlog_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(backlog_item.get("title") or ""),
                    scope=str(backlog_item.get("scope") or ""),
                    kind=str(backlog_item.get("kind") or ""),
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="developer")
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T000320Z",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-03-24T00:03:20+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                service._save_sprint_state(sprint_state)

                async def fake_run_internal_request_chain(**_kwargs):
                    return {
                        "request_id": "req-completed-todo",
                        "role": "developer",
                        "status": "completed",
                        "summary": "task 단위 자동 커밋을 연결했습니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": ["./workspace/teams_runtime/core/orchestration.py"],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "",
                    }

                async def fake_send_sprint_report(**_kwargs):
                    return None

                with (
                    patch.object(service, "_run_internal_request_chain", side_effect=fake_run_internal_request_chain),
                    patch.object(service, "_send_sprint_report", side_effect=fake_send_sprint_report),
                    patch.object(
                        service,
                        "_inspect_task_version_control_state",
                        return_value={
                            "status": "pending_changes",
                            "repo_root": tmpdir,
                            "changed_paths": ["teams_runtime/core/orchestration.py"],
                            "message": "현재 task 소유 변경이 아직 commit되지 않았습니다.",
                        },
                    ),
                    patch.object(
                        service.version_controller_runtime,
                        "run_task",
                        return_value={
                            "request_id": "req-completed-todo",
                            "role": "version_controller",
                            "status": "completed",
                            "summary": "task 변경을 커밋했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["sources/req-completed-todo.task.version_control.json"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                            "commit_status": "committed",
                            "commit_sha": "taskcommit123",
                            "commit_paths": ["teams_runtime/core/orchestration.py"],
                            "commit_message": "[2026-Sprint-01-20260324T000320Z] todo-commit orchestration.py: connect task auto commit",
                            "change_detected": True,
                        },
                    ) as version_controller_mock,
                ):
                    asyncio.run(service._execute_sprint_todo(sprint_state, todo))

                updated_backlog = service._load_backlog_item(backlog_item["backlog_id"])
                request_record = service._load_request(todo["request_id"])
                self.assertEqual(todo["status"], "committed")
                self.assertEqual(request_record["status"], "committed")
                self.assertEqual(updated_backlog["status"], "done")
                self.assertEqual(sprint_state["selected_backlog_ids"], [])
                self.assertEqual(sprint_state["selected_items"][0]["status"], "done")
                self.assertEqual(request_record["version_control_status"], "committed")
                self.assertEqual(request_record["task_commit_status"], "committed")
                self.assertEqual(request_record["task_commit_sha"], "taskcommit123")
                self.assertEqual(request_record["task_commit_paths"], ["teams_runtime/core/orchestration.py"])
                self.assertIn("./workspace/teams_runtime/core/orchestration.py", request_record["artifacts"])
                self.assertIn("teams_runtime/core/orchestration.py", request_record["artifacts"])
                self.assertIn("teams_runtime/core/orchestration.py", todo["artifacts"])
                version_controller_mock.assert_called_once()
                version_context = version_controller_mock.call_args.args[1]
                self.assertEqual(version_context["version_control"]["title"], "커밋 강제 보강")
                self.assertEqual(version_context["version_control"]["summary"], "task 단위 자동 커밋을 연결했습니다.")
                payload_path = (
                    service.paths.internal_agent_root("version_controller")
                    / version_context["version_control"]["payload_file"]
                )
                payload = read_json(payload_path)
                self.assertEqual(payload["title"], "커밋 강제 보강")
                self.assertEqual(payload["summary"], "task 단위 자동 커밋을 연결했습니다.")
                self.assertEqual(request_record["task_commit_summary"], "task 단위 자동 커밋을 연결했습니다.")

    def test_load_sprint_state_repairs_committed_todo_and_selected_backlog_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_id = "2026-Sprint-01-20260324T000321Z"
                backlog_item = build_backlog_item(
                    title="선택 스냅샷 복구",
                    summary="committed todo 이후 sprint snapshot을 정합화합니다.",
                    kind="bug",
                    source="user",
                    scope="selected backlog sync",
                )
                backlog_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(backlog_item.get("title") or ""),
                    scope=str(backlog_item.get("scope") or ""),
                    kind=str(backlog_item.get("kind") or ""),
                )
                backlog_item["status"] = "selected"
                backlog_item["selected_in_sprint_id"] = sprint_id
                service._save_backlog_item(backlog_item)

                todo = build_todo_item(backlog_item, owner_role="developer")
                todo["status"] = "committed"
                todo["summary"] = "task 변경을 커밋했습니다."
                inconsistent_sprint_state = {
                    "sprint_id": sprint_id,
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-03-24T00:03:21+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                write_json(service.paths.sprint_file(sprint_id), inconsistent_sprint_state)

                repaired = service._load_sprint_state(sprint_id)
                repaired_backlog = service._load_backlog_item(backlog_item["backlog_id"])
                persisted = read_json(service.paths.sprint_file(sprint_id))
                todo_backlog_text = service.paths.sprint_artifact_file(
                    build_sprint_artifact_folder_name(sprint_id),
                    "todo_backlog.md",
                ).read_text(encoding="utf-8")

                self.assertEqual(repaired["selected_backlog_ids"], [])
                self.assertEqual(repaired["selected_items"][0]["status"], "done")
                self.assertEqual(repaired_backlog["status"], "done")
                self.assertEqual(repaired_backlog["completed_in_sprint_id"], sprint_id)
                self.assertEqual(persisted["selected_backlog_ids"], [])
                self.assertEqual(persisted["selected_items"][0]["status"], "done")
                self.assertIn("status: done", todo_backlog_text)
                self.assertNotIn("status: selected", todo_backlog_text)

    def test_sync_manual_sprint_queue_keeps_committed_and_uncommitted_todos(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_id = "260405-Sprint-16:34"

                queued_item = build_backlog_item(
                    title="queued todo",
                    summary="아직 선택 상태인 todo입니다.",
                    kind="enhancement",
                    source="user",
                    scope="queued todo scope",
                )
                queued_item["status"] = "selected"
                queued_item["selected_in_sprint_id"] = sprint_id
                service._save_backlog_item(queued_item)

                committed_item = build_backlog_item(
                    title="committed todo",
                    summary="이미 커밋된 todo입니다.",
                    kind="bug",
                    source="user",
                    scope="committed todo scope",
                )
                committed_item["status"] = "done"
                committed_item["selected_in_sprint_id"] = sprint_id
                committed_item["completed_in_sprint_id"] = sprint_id
                service._save_backlog_item(committed_item)

                uncommitted_item = build_backlog_item(
                    title="uncommitted todo",
                    summary="버전 컨트롤 정리가 남았습니다.",
                    kind="bug",
                    source="user",
                    scope="uncommitted todo scope",
                )
                uncommitted_item["status"] = "blocked"
                service._save_backlog_item(uncommitted_item)

                queued_todo = build_todo_item(queued_item, owner_role="planner")
                committed_todo = build_todo_item(committed_item, owner_role="developer")
                committed_todo["status"] = "committed"
                committed_todo["request_id"] = "req-committed"
                uncommitted_todo = build_todo_item(uncommitted_item, owner_role="developer")
                uncommitted_todo["status"] = "uncommitted"
                uncommitted_todo["request_id"] = "req-uncommitted"

                sprint_state = {
                    "sprint_id": sprint_id,
                    "status": "running",
                    "trigger": "manual_start",
                    "started_at": "2026-04-05T16:34:00+09:00",
                    "ended_at": "",
                    "milestone_title": "queue sync retention",
                    "selected_backlog_ids": [queued_item["backlog_id"]],
                    "selected_items": [dict(queued_item)],
                    "todos": [queued_todo, committed_todo, uncommitted_todo],
                    "commit_sha": "",
                    "report_path": "",
                }

                service._sync_manual_sprint_queue(sprint_state)

                todo_status_by_request = {
                    str(todo.get("request_id") or todo.get("title") or ""): str(todo.get("status") or "")
                    for todo in sprint_state["todos"]
                }
                self.assertEqual(todo_status_by_request["req-committed"], "committed")
                self.assertEqual(todo_status_by_request["req-uncommitted"], "uncommitted")
                self.assertIn(queued_item["backlog_id"], sprint_state["selected_backlog_ids"])
                self.assertEqual(len(sprint_state["todos"]), 3)

    def test_load_sprint_state_recovers_missing_committed_todos_and_refreshes_report_body(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_id = "260405-Sprint-16:34"
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="report sync recovery",
                    trigger="manual_start",
                )
                sprint_state["sprint_id"] = sprint_id
                sprint_state["sprint_folder_name"] = build_sprint_artifact_folder_name(sprint_id)
                sprint_state["sprint_folder"] = str(service.paths.sprint_artifact_dir(sprint_state["sprint_folder_name"]))
                sprint_state["phase"] = "wrap_up"
                sprint_state["status"] = "completed"
                sprint_state["closeout_status"] = "verified"
                sprint_state["commit_count"] = 3
                sprint_state["commit_sha"] = "commit-report-sync"
                sprint_state["commit_shas"] = ["commit-1", "commit-2", "commit-3"]
                sprint_state["ended_at"] = "2026-04-05T17:32:55.408877+09:00"

                backlog_items = []
                todo_artifact_paths = []
                for index, title in enumerate(
                    [
                        "uvloop 적용 후보 정리",
                        "asyncio 경로 재구성",
                        "orjson 전환 범위 정의",
                    ],
                    start=1,
                ):
                    backlog_item = build_backlog_item(
                        title=title,
                        summary=f"{title} summary",
                        kind="enhancement",
                        source="user",
                        scope=title,
                    )
                    backlog_item["status"] = "selected"
                    backlog_item["selected_in_sprint_id"] = sprint_id
                    service._save_backlog_item(backlog_item)
                    backlog_items.append(backlog_item)
                    todo_artifact_paths.append(
                        str(service.paths.sprint_artifact_dir(sprint_state["sprint_folder_name"]) / f"task_{index}.md")
                    )

                recovered_todos = []
                for index, backlog_item in enumerate(backlog_items, start=1):
                    todo = build_todo_item(backlog_item, owner_role="developer")
                    todo["todo_id"] = f"todo-report-sync-{index}"
                    todo["status"] = "committed"
                    todo["request_id"] = f"req-report-sync-{index}"
                    todo["summary"] = f"{backlog_item['title']} committed"
                    todo["artifacts"] = [todo_artifact_paths[index - 1]]
                    recovered_todos.append(todo)
                    service._save_request(
                        {
                            "request_id": todo["request_id"],
                            "status": "committed",
                            "intent": "route",
                            "urgency": "normal",
                            "scope": backlog_item["title"],
                            "body": backlog_item["summary"],
                            "artifacts": [todo_artifact_paths[index - 1]],
                            "params": {
                                "_teams_kind": "sprint_internal",
                                "sprint_id": sprint_id,
                                "backlog_id": backlog_item["backlog_id"],
                                "todo_id": todo["todo_id"],
                            },
                            "current_role": "developer",
                            "next_role": "",
                            "owner_role": "orchestrator",
                            "sprint_id": sprint_id,
                            "backlog_id": backlog_item["backlog_id"],
                            "todo_id": todo["todo_id"],
                            "created_at": f"2026-04-05T16:3{index}:00+09:00",
                            "updated_at": f"2026-04-05T16:4{index}:00+09:00",
                            "fingerprint": f"req-report-sync-{index}",
                            "reply_route": {},
                            "events": [],
                            "result": {
                                "request_id": todo["request_id"],
                                "role": "developer",
                                "status": "committed",
                                "summary": todo["summary"],
                                "insights": [],
                                "proposals": {},
                                "artifacts": [todo_artifact_paths[index - 1]],
                                "next_role": "",
                                "error": "",
                            },
                            "version_control_status": "committed",
                            "version_control_sha": f"taskcommit-{index}",
                            "version_control_paths": [f"workspace/task_{index}.py"],
                            "version_control_message": f"task commit {index}",
                            "version_control_error": "",
                            "task_commit_status": "committed",
                            "task_commit_sha": f"taskcommit-{index}",
                            "task_commit_paths": [f"workspace/task_{index}.py"],
                            "task_commit_message": f"task commit {index}",
                            "visited_roles": ["planner", "developer"],
                            "task_commit_summary": todo["summary"],
                        }
                    )

                stale_sprint_state = dict(sprint_state)
                stale_sprint_state["selected_backlog_ids"] = [item["backlog_id"] for item in backlog_items]
                stale_sprint_state["selected_items"] = [dict(item) for item in backlog_items]
                stale_sprint_state["todos"] = [dict(recovered_todos[1])]
                stale_report_snapshot = dict(stale_sprint_state)
                stale_report_snapshot["status"] = "reporting"
                stale_report_snapshot["report_body"] = service._build_sprint_report_body(
                    stale_report_snapshot,
                    {
                        "status": "verified",
                        "commit_count": 3,
                        "commit_shas": ["commit-1", "commit-2", "commit-3"],
                        "representative_commit_sha": "commit-report-sync",
                        "sprint_tagged_commit_count": 3,
                        "sprint_tagged_commit_shas": ["commit-1", "commit-2", "commit-3"],
                        "uncommitted_paths": [],
                        "message": "스프린트 closeout 검증을 완료했습니다.",
                    },
                )
                stale_sprint_state["report_body"] = stale_report_snapshot["report_body"]
                stale_sprint_state["report_path"] = service._archive_sprint_history(
                    stale_report_snapshot,
                    stale_report_snapshot["report_body"],
                )
                write_json(service.paths.sprint_file(sprint_id), stale_sprint_state)

                repaired = service._load_sprint_state(sprint_id)
                persisted = read_json(service.paths.sprint_file(sprint_id))
                report_text = service.paths.sprint_artifact_file(
                    build_sprint_artifact_folder_name(sprint_id),
                    "report.md",
                ).read_text(encoding="utf-8")
                history_text = service.paths.sprint_history_file(sprint_id).read_text(encoding="utf-8")
                history_index_text = service.paths.sprint_history_index_file.read_text(encoding="utf-8")

                self.assertEqual(len(repaired["todos"]), 3)
                self.assertEqual(
                    {str(todo.get("request_id") or "") for todo in repaired["todos"]},
                    {"req-report-sync-1", "req-report-sync-2", "req-report-sync-3"},
                )
                self.assertEqual(repaired["selected_backlog_ids"], [])
                self.assertEqual(
                    [str(item.get("status") or "") for item in repaired["selected_items"]],
                    ["done", "done", "done"],
                )
                self.assertIn("## 한눈에 보기", persisted["report_body"])
                self.assertIn("## 머신 요약", persisted["report_body"])
                self.assertIn("todo_status_counts=committed:3", persisted["report_body"])
                self.assertIn("## 에이전트 기여", report_text)
                self.assertIn("## 성과", report_text)
                self.assertIn("todo_status_counts=committed:3", report_text)
                self.assertIn("req-report-sync-1", report_text)
                self.assertIn("req-report-sync-2", report_text)
                self.assertIn("req-report-sync-3", report_text)
                self.assertIn("artifact=task_1.md", report_text)
                self.assertIn("artifact=task_2.md", report_text)
                self.assertIn("artifact=task_3.md", report_text)
                self.assertIn("- status: completed", history_text)
                self.assertIn("### uvloop 적용 후보 정리", history_text)
                self.assertIn("### asyncio 경로 재구성", history_text)
                self.assertIn("### orjson 전환 범위 정의", history_text)
                self.assertIn("## Sprint Report", history_text)
                self.assertIn("## 한눈에 보기", history_text)
                self.assertIn("todo_status_counts=committed:3", history_text)
                self.assertIn("| 260405-Sprint-16:34 | completed |", history_index_text)
                self.assertIn("| 3 | commit-report-sync |", history_index_text)

    def test_execute_sprint_todo_blocks_when_version_controller_commit_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="커밋 실패 보강",
                    summary="task auto commit 실패 시 차단합니다.",
                    kind="bug",
                    source="user",
                    scope="커밋 실패 보강",
                )
                backlog_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(backlog_item.get("title") or ""),
                    scope=str(backlog_item.get("scope") or ""),
                    kind=str(backlog_item.get("kind") or ""),
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="developer")
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T000340Z",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-03-24T00:03:40+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                service._save_sprint_state(sprint_state)

                async def fake_run_internal_request_chain(**_kwargs):
                    return {
                        "request_id": "req-commit-failed-todo",
                        "role": "developer",
                        "status": "completed",
                        "summary": "변경을 반영했습니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": ["./workspace/teams_runtime/core/orchestration.py"],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "",
                    }

                async def fake_send_sprint_report(**_kwargs):
                    return None

                with (
                    patch.object(service, "_run_internal_request_chain", side_effect=fake_run_internal_request_chain),
                    patch.object(service, "_send_sprint_report", side_effect=fake_send_sprint_report),
                    patch.object(
                        service,
                        "_inspect_task_version_control_state",
                        return_value={
                            "status": "pending_changes",
                            "repo_root": tmpdir,
                            "changed_paths": ["teams_runtime/core/orchestration.py"],
                            "message": "현재 task 소유 변경이 아직 commit되지 않았습니다.",
                        },
                    ),
                    patch.object(
                        service.version_controller_runtime,
                        "run_task",
                        return_value={
                            "request_id": "req-commit-failed-todo",
                            "role": "version_controller",
                            "status": "blocked",
                            "summary": "git commit failed",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["sources/req-commit-failed-todo.task.version_control.json"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "git commit failed",
                            "commit_status": "failed",
                            "commit_sha": "",
                            "commit_paths": ["teams_runtime/core/orchestration.py"],
                            "commit_message": "[2026-Sprint-01-20260324T000340Z] todo-commit orchestration.py: connect task auto commit",
                            "change_detected": True,
                        },
                    ),
                ):
                    asyncio.run(service._execute_sprint_todo(sprint_state, todo))

                updated_backlog = service._load_backlog_item(backlog_item["backlog_id"])
                request_record = service._load_request(todo["request_id"])
                self.assertEqual(todo["status"], "uncommitted")
                self.assertEqual(updated_backlog["status"], "blocked")
                self.assertEqual(request_record["status"], "uncommitted")
                self.assertEqual(request_record["version_control_status"], "failed")
                self.assertEqual(request_record["task_commit_status"], "failed")
                self.assertIn("version_controller 커밋 단계에 실패", request_record["result"]["summary"])
                self.assertIn("./workspace/teams_runtime/core/orchestration.py", request_record["artifacts"])
                self.assertIn("teams_runtime/core/orchestration.py", request_record["artifacts"])
                self.assertIn("teams_runtime/core/orchestration.py", todo["artifacts"])

    def test_execute_sprint_todo_does_not_persist_restart_policy_fields_when_no_changes_detected(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="일반 기능 수정",
                    summary="teams_runtime 외 변경입니다.",
                    kind="enhancement",
                    source="user",
                    scope="일반 기능 수정",
                )
                backlog_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(backlog_item.get("title") or ""),
                    scope=str(backlog_item.get("scope") or ""),
                    kind=str(backlog_item.get("kind") or ""),
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="developer")
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T000350Z",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-03-24T00:03:50+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                service._save_sprint_state(sprint_state)

                async def fake_run_internal_request_chain(**_kwargs):
                    return {
                        "request_id": "req-no-restart-todo",
                        "role": "developer",
                        "status": "completed",
                        "summary": "일반 기능 수정을 완료했습니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": ["./workspace/apps/sample/file.txt"],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "",
                    }

                async def fake_send_sprint_report(**_kwargs):
                    return None

                with (
                    patch.object(service, "_run_internal_request_chain", side_effect=fake_run_internal_request_chain),
                    patch.object(service, "_send_sprint_report", side_effect=fake_send_sprint_report),
                    patch.object(
                        service,
                        "_inspect_task_version_control_state",
                        return_value={
                            "status": "no_changes",
                            "repo_root": tmpdir,
                            "changed_paths": [],
                            "message": "현재 task 소유 변경 파일이 없습니다.",
                        },
                    ),
                    patch.object(
                        service.version_controller_runtime,
                        "run_task",
                        return_value={
                            "request_id": "req-no-restart-todo",
                            "role": "version_controller",
                            "status": "completed",
                            "summary": "task 소유 변경 파일이 없습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["sources/req-no-restart-todo.task.version_control.json"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                            "commit_status": "no_changes",
                            "commit_sha": "",
                            "commit_paths": [],
                            "commit_message": "",
                            "change_detected": False,
                        },
                    ) as version_controller_mock,
                ):
                    asyncio.run(service._execute_sprint_todo(sprint_state, todo))

                request_record = service._load_request(todo["request_id"])
                self.assertEqual(todo["status"], "completed")
                self.assertEqual(request_record["version_control_status"], "no_changes")
                self.assertNotIn("restart_policy_status", request_record)
                self.assertNotIn("restart_policy_status", request_record["result"])
                version_controller_mock.assert_not_called()

    def test_execute_sprint_todo_reports_commit_message_without_restart_policy_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="runtime 변경 반영",
                    summary="teams_runtime 변경을 반영합니다.",
                    kind="bug",
                    source="user",
                    scope="runtime 변경 반영",
                )
                backlog_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(backlog_item.get("title") or ""),
                    scope=str(backlog_item.get("scope") or ""),
                    kind=str(backlog_item.get("kind") or ""),
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="developer")
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T000360Z",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-03-24T00:03:60+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                service._save_sprint_state(sprint_state)

                async def fake_run_internal_request_chain(**_kwargs):
                    return {
                        "request_id": "req-restart-todo",
                        "role": "developer",
                        "status": "completed",
                        "summary": "runtime 변경을 완료했습니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": ["./workspace/teams_runtime/core/orchestration.py"],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "",
                    }

                send_sprint_report_mock = AsyncMock()

                with (
                    patch.object(service, "_run_internal_request_chain", side_effect=fake_run_internal_request_chain),
                    patch.object(service, "_send_sprint_report", send_sprint_report_mock),
                    patch.object(
                        service,
                        "_inspect_task_version_control_state",
                        return_value={
                            "status": "pending_changes",
                            "repo_root": tmpdir,
                            "changed_paths": ["teams_runtime/core/orchestration.py"],
                            "message": "현재 task 소유 변경이 아직 commit되지 않았습니다.",
                        },
                    ),
                    patch.object(
                        service.version_controller_runtime,
                        "run_task",
                        return_value={
                            "request_id": "req-restart-todo",
                            "role": "version_controller",
                            "status": "completed",
                            "summary": "task 변경을 커밋했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["sources/req-restart-todo.task.version_control.json"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                            "commit_status": "committed",
                            "commit_sha": "taskcommit999",
                            "commit_paths": ["teams_runtime/core/orchestration.py"],
                            "commit_message": "[2026-Sprint-01-20260324T000360Z] todo-runtime orchestration.py: complete runtime change",
                            "change_detected": True,
                        },
                    ),
                ):
                    asyncio.run(service._execute_sprint_todo(sprint_state, todo))

                request_record = service._load_request(todo["request_id"])
                self.assertEqual(todo["status"], "committed")
                self.assertNotIn("restart_policy_status", request_record)
                self.assertNotIn("restart_policy_paths", request_record)
                self.assertNotIn("restart_policy_command", request_record)
                self.assertEqual(
                    send_sprint_report_mock.await_args.kwargs["commit_message"],
                    "[2026-Sprint-01-20260324T000360Z] todo-runtime orchestration.py: complete runtime change",
                )

    def test_execute_sprint_todo_does_not_persist_restart_policy_error_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="runtime 재시작 실패 기록",
                    summary="재시작 실패를 기록합니다.",
                    kind="bug",
                    source="user",
                    scope="runtime 재시작 실패 기록",
                )
                backlog_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(backlog_item.get("title") or ""),
                    scope=str(backlog_item.get("scope") or ""),
                    kind=str(backlog_item.get("kind") or ""),
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="developer")
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T000370Z",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-03-24T00:03:70+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                service._save_sprint_state(sprint_state)

                async def fake_run_internal_request_chain(**_kwargs):
                    return {
                        "request_id": "req-restart-failed-todo",
                        "role": "developer",
                        "status": "completed",
                        "summary": "runtime 변경을 완료했습니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": ["./workspace/teams_runtime/core/template.py"],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "",
                    }

                async def fake_send_sprint_report(**_kwargs):
                    return None

                with (
                    patch.object(service, "_run_internal_request_chain", side_effect=fake_run_internal_request_chain),
                    patch.object(service, "_send_sprint_report", side_effect=fake_send_sprint_report),
                    patch.object(
                        service,
                        "_inspect_task_version_control_state",
                        return_value={
                            "status": "pending_changes",
                            "repo_root": tmpdir,
                            "changed_paths": ["teams_runtime/core/template.py"],
                            "message": "현재 task 소유 변경이 아직 commit되지 않았습니다.",
                        },
                    ),
                    patch.object(
                        service.version_controller_runtime,
                        "run_task",
                        return_value={
                            "request_id": "req-restart-failed-todo",
                            "role": "version_controller",
                            "status": "completed",
                            "summary": "task 변경을 커밋했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["sources/req-restart-failed-todo.task.version_control.json"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                            "commit_status": "committed",
                            "commit_sha": "taskcommit1000",
                            "commit_paths": ["teams_runtime/core/template.py"],
                            "commit_message": "[2026-Sprint-01-20260324T000370Z] todo-runtime template.py: complete runtime prompt change",
                            "change_detected": True,
                        },
                    ),
                ):
                    asyncio.run(service._execute_sprint_todo(sprint_state, todo))

                request_record = service._load_request(todo["request_id"])
                self.assertEqual(todo["status"], "committed")
                self.assertNotIn("restart_policy_status", request_record)
                self.assertNotIn("restart_policy_error", request_record)

    def test_execute_sprint_todo_resumes_uncommitted_version_control_without_rerunning_roles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="중단된 커밋 재개",
                    summary="uncommitted task를 재개합니다.",
                    kind="bug",
                    source="user",
                    scope="중단된 커밋 재개",
                )
                backlog_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(backlog_item.get("title") or ""),
                    scope=str(backlog_item.get("scope") or ""),
                    kind=str(backlog_item.get("kind") or ""),
                )
                backlog_item["status"] = "blocked"
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="developer")
                todo["status"] = "uncommitted"
                todo["summary"] = "변경을 반영했습니다."
                todo["request_id"] = "req-uncommitted-todo"
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T000371Z",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-03-24T00:03:71+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                service._save_sprint_state(sprint_state)
                service._save_request(
                    {
                        "request_id": "req-uncommitted-todo",
                        "status": "uncommitted",
                        "intent": "route",
                        "urgency": "normal",
                        "scope": "중단된 커밋 재개",
                        "body": "변경을 반영했습니다.",
                        "artifacts": ["./workspace/teams_runtime/core/orchestration.py"],
                        "params": {
                            "_teams_kind": "sprint_internal",
                            "sprint_id": sprint_state["sprint_id"],
                            "backlog_id": backlog_item["backlog_id"],
                            "todo_id": todo["todo_id"],
                        },
                        "current_role": "orchestrator",
                        "next_role": "",
                        "owner_role": "orchestrator",
                        "sprint_id": sprint_state["sprint_id"],
                        "backlog_id": backlog_item["backlog_id"],
                        "todo_id": todo["todo_id"],
                        "created_at": "2026-03-24T00:03:71+00:00",
                        "updated_at": "2026-03-24T00:03:71+00:00",
                        "fingerprint": "req-uncommitted-todo",
                        "reply_route": {},
                        "events": [],
                        "result": {
                            "request_id": "req-uncommitted-todo",
                            "role": "developer",
                            "status": "uncommitted",
                            "summary": "Task 완료 직전 version_controller 커밋 단계에 실패했습니다. git commit failed",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["./workspace/teams_runtime/core/orchestration.py"],
                            "next_role": "",
                            "error": "git commit failed",
                            "task_commit_summary": "변경을 반영했습니다.",
                        },
                        "git_baseline": {"repo_root": tmpdir, "head_sha": "base", "dirty_paths": []},
                        "version_control_status": "failed",
                        "version_control_sha": "",
                        "version_control_paths": ["teams_runtime/core/orchestration.py"],
                        "version_control_message": "",
                        "version_control_error": "git commit failed",
                        "task_commit_status": "failed",
                        "task_commit_sha": "",
                        "task_commit_paths": ["teams_runtime/core/orchestration.py"],
                        "task_commit_message": "",
                        "visited_roles": ["planner", "developer"],
                        "task_commit_summary": "변경을 반영했습니다.",
                    }
                )

                async def fake_send_sprint_report(**_kwargs):
                    return None

                with (
                    patch.object(service, "_send_sprint_report", side_effect=fake_send_sprint_report),
                    patch.object(
                        service,
                        "_inspect_task_version_control_state",
                        return_value={
                            "status": "pending_changes",
                            "repo_root": tmpdir,
                            "changed_paths": ["teams_runtime/core/orchestration.py"],
                            "message": "현재 task 소유 변경이 아직 commit되지 않았습니다.",
                        },
                    ),
                    patch.object(service, "_run_internal_request_chain", new=AsyncMock()) as internal_chain_mock,
                    patch.object(
                        service.version_controller_runtime,
                        "run_task",
                        return_value={
                            "request_id": "req-uncommitted-todo",
                            "role": "version_controller",
                            "status": "completed",
                            "summary": "task 변경을 커밋했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["sources/req-uncommitted-todo.task.version_control.json"],
                            "next_role": "",
                            "error": "",
                            "commit_status": "committed",
                            "commit_sha": "taskcommitresume1",
                            "commit_paths": ["teams_runtime/core/orchestration.py"],
                            "commit_message": "[2026-Sprint-01-20260324T000371Z] todo-resume orchestration.py: resume task commit",
                            "change_detected": True,
                        },
                    ),
                ):
                    asyncio.run(service._execute_sprint_todo(sprint_state, todo))

                updated_backlog = service._load_backlog_item(backlog_item["backlog_id"])
                request_record = service._load_request("req-uncommitted-todo")
                self.assertEqual(todo["status"], "committed")
                self.assertEqual(updated_backlog["status"], "done")
                self.assertEqual(sprint_state["selected_backlog_ids"], [])
                self.assertEqual(sprint_state["selected_items"][0]["status"], "done")
                self.assertEqual(request_record["status"], "committed")
                internal_chain_mock.assert_not_awaited()
                self.assertIn("./workspace/teams_runtime/core/orchestration.py", request_record["artifacts"])
                self.assertIn("teams_runtime/core/orchestration.py", request_record["artifacts"])
                self.assertIn("teams_runtime/core/orchestration.py", todo["artifacts"])

    def test_continue_sprint_manual_restart_retries_latest_blocked_todo_before_other_queued_work(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                queued_item = build_backlog_item(
                    title="queued work",
                    summary="다른 queued todo입니다.",
                    kind="enhancement",
                    source="user",
                    scope="queued work",
                )
                queued_item["status"] = "selected"
                queued_item["selected_in_sprint_id"] = "2026-Sprint-01-20260324T000380Z"
                queued_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(queued_item.get("title") or ""),
                    scope=str(queued_item.get("scope") or ""),
                    kind=str(queued_item.get("kind") or ""),
                )
                blocked_item = build_backlog_item(
                    title="blocked work",
                    summary="막힌 todo를 재시도해야 합니다.",
                    kind="bug",
                    source="user",
                    scope="blocked work",
                )
                blocked_item["status"] = "blocked"
                blocked_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(blocked_item.get("title") or ""),
                    scope=str(blocked_item.get("scope") or ""),
                    kind=str(blocked_item.get("kind") or ""),
                )
                service._save_backlog_item(queued_item)
                service._save_backlog_item(blocked_item)

                todo_queued = build_todo_item(queued_item, owner_role="planner")
                todo_blocked = build_todo_item(blocked_item, owner_role="planner")
                todo_blocked["status"] = "blocked"
                todo_blocked["request_id"] = "req-blocked-restart"
                todo_blocked["ended_at"] = "2026-03-24T00:04:00+09:00"
                todo_blocked["carry_over_backlog_id"] = blocked_item["backlog_id"]
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T000380Z",
                    "status": "running",
                    "trigger": "manual_restart",
                    "started_at": "2026-03-24T00:03:40+09:00",
                    "ended_at": "",
                    "selected_backlog_ids": [queued_item["backlog_id"], blocked_item["backlog_id"]],
                    "selected_items": [dict(queued_item), dict(blocked_item)],
                    "todos": [todo_queued, todo_blocked],
                    "commit_sha": "",
                    "report_path": "",
                    "git_baseline": {"repo_root": "", "head_sha": "", "dirty_paths": []},
                    "resume_from_checkpoint_requested_at": "2026-03-24T00:05:00+09:00",
                }
                service._save_sprint_state(sprint_state)
                service._save_request(
                    {
                        "request_id": "req-blocked-restart",
                        "status": "blocked",
                        "intent": "plan",
                        "urgency": "normal",
                        "scope": "blocked work",
                        "body": "blocked work",
                        "artifacts": [],
                        "params": {
                            "_teams_kind": "sprint_internal",
                            "sprint_id": sprint_state["sprint_id"],
                            "backlog_id": blocked_item["backlog_id"],
                            "todo_id": todo_blocked["todo_id"],
                        },
                        "current_role": "planner",
                        "next_role": "",
                        "owner_role": "orchestrator",
                        "sprint_id": sprint_state["sprint_id"],
                        "backlog_id": blocked_item["backlog_id"],
                        "todo_id": todo_blocked["todo_id"],
                        "created_at": "2026-03-24T00:04:00+09:00",
                        "updated_at": "2026-03-24T00:04:00+09:00",
                        "fingerprint": "req-blocked-restart",
                        "reply_route": {},
                        "events": [],
                        "result": {
                            "request_id": "req-blocked-restart",
                            "role": "planner",
                            "status": "blocked",
                            "summary": "입력 부족으로 planner가 중단했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": [],
                            "next_role": "",
                            "error": "missing input",
                        },
                    }
                )

                execution_order: list[tuple[str, str, str]] = []

                async def fake_execute(_sprint_state, todo):
                    execution_order.append(
                        (
                            str(todo.get("todo_id") or ""),
                            str(todo.get("status") or ""),
                            str(todo.get("request_id") or ""),
                        )
                    )
                    todo["status"] = "completed"

                with (
                    patch.object(service, "_execute_sprint_todo", side_effect=fake_execute),
                    patch.object(service, "_finalize_sprint", new=AsyncMock(return_value=None)),
                ):
                    asyncio.run(service._continue_sprint(sprint_state, announce=False))

                updated_blocked = service._load_backlog_item(blocked_item["backlog_id"])
                self.assertEqual(execution_order[0][0], todo_blocked["todo_id"])
                self.assertEqual(execution_order[0][1], "queued")
                self.assertEqual(execution_order[0][2], "")
                self.assertEqual(sprint_state["last_resume_checkpoint_todo_id"], todo_blocked["todo_id"])
                self.assertEqual(sprint_state["last_resume_checkpoint_status"], "blocked")
                self.assertEqual(str(sprint_state.get("resume_from_checkpoint_requested_at") or ""), "")
                self.assertEqual(updated_blocked["status"], "done")
                self.assertEqual(updated_blocked["selected_in_sprint_id"], sprint_state["sprint_id"])
                self.assertEqual(updated_blocked["completed_in_sprint_id"], sprint_state["sprint_id"])

    def test_manual_daily_restart_resumes_running_checkpoint_before_earlier_queued_todo(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('  start_mode: "auto"\n', '  start_mode: "manual_daily"\n', 1)
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                queued_item = build_backlog_item(
                    title="queued work",
                    summary="이 todo는 나중에 실행되어야 합니다.",
                    kind="enhancement",
                    source="user",
                    scope="queued work",
                )
                queued_item["status"] = "selected"
                queued_item["selected_in_sprint_id"] = "260324-Sprint-09:00"
                running_item = build_backlog_item(
                    title="running checkpoint",
                    summary="이 todo부터 다시 시작해야 합니다.",
                    kind="bug",
                    source="user",
                    scope="running checkpoint",
                )
                running_item["status"] = "selected"
                running_item["selected_in_sprint_id"] = "260324-Sprint-09:00"
                service._save_backlog_item(queued_item)
                service._save_backlog_item(running_item)

                sprint_state = service._build_manual_sprint_state(
                    milestone_title="restart checkpoint",
                    trigger="manual_start",
                )
                sprint_state["phase"] = "ongoing"
                sprint_state["status"] = "running"
                sprint_state["last_planner_review_at"] = datetime.now(timezone.utc).isoformat()
                sprint_state["resume_from_checkpoint_requested_at"] = datetime.now(timezone.utc).isoformat()
                sprint_state["selected_backlog_ids"] = [queued_item["backlog_id"], running_item["backlog_id"]]
                sprint_state["selected_items"] = [dict(queued_item), dict(running_item)]

                todo_queued = build_todo_item(queued_item, owner_role="planner")
                todo_running = build_todo_item(running_item, owner_role="developer")
                todo_running["status"] = "running"
                todo_running["request_id"] = "req-running-checkpoint"
                todo_running["started_at"] = "2026-03-24T09:10:00+09:00"
                sprint_state["todos"] = [todo_queued, todo_running]
                service._save_sprint_state(sprint_state)

                execution_order: list[str] = []

                async def fake_execute(_sprint_state, todo):
                    execution_order.append(str(todo.get("todo_id") or ""))
                    todo["status"] = "completed"

                with (
                    patch.object(service, "_run_ongoing_sprint_review", new=AsyncMock(return_value=None)),
                    patch.object(service, "_sync_manual_sprint_queue", return_value=None),
                    patch.object(service, "_execute_sprint_todo", side_effect=fake_execute),
                    patch.object(service, "_finalize_sprint", new=AsyncMock(return_value=None)),
                ):
                    asyncio.run(service._continue_sprint(sprint_state, announce=False))

                self.assertEqual(execution_order[0], todo_running["todo_id"])
                self.assertEqual(sprint_state["last_resume_checkpoint_todo_id"], todo_running["todo_id"])
                self.assertEqual(sprint_state["last_resume_checkpoint_status"], "running")
                self.assertEqual(str(sprint_state.get("resume_from_checkpoint_requested_at") or ""), "")

    def test_manual_daily_sprint_wraps_up_when_only_terminal_todos_remain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('  start_mode: "auto"\n', '  start_mode: "manual_daily"\n', 1)
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                completed_item = build_backlog_item(
                    title="completed work",
                    summary="이미 완료된 작업입니다.",
                    kind="enhancement",
                    source="user",
                    scope="completed work",
                )
                blocked_item = build_backlog_item(
                    title="blocked work",
                    summary="다시 막힌 작업입니다.",
                    kind="bug",
                    source="user",
                    scope="blocked work",
                )
                completed_item["status"] = "done"
                blocked_item["status"] = "blocked"
                service._save_backlog_item(completed_item)
                service._save_backlog_item(blocked_item)

                sprint_state = service._build_manual_sprint_state(
                    milestone_title="terminal todo wrap up",
                    trigger="manual_start",
                )
                sprint_state["phase"] = "ongoing"
                sprint_state["status"] = "running"
                sprint_state["last_planner_review_at"] = datetime.now(timezone.utc).isoformat()
                sprint_state["selected_backlog_ids"] = [completed_item["backlog_id"], blocked_item["backlog_id"]]
                sprint_state["selected_items"] = [dict(completed_item), dict(blocked_item)]

                todo_completed = build_todo_item(completed_item, owner_role="planner")
                todo_completed["status"] = "completed"
                todo_blocked = build_todo_item(blocked_item, owner_role="planner")
                todo_blocked["status"] = "blocked"
                sprint_state["todos"] = [todo_completed, todo_blocked]
                service._save_sprint_state(sprint_state)

                with (
                    patch.object(service, "_run_ongoing_sprint_review", new=AsyncMock(return_value=None)),
                    patch.object(service, "_sync_manual_sprint_queue", return_value=None),
                    patch.object(service, "_finalize_sprint", new=AsyncMock(return_value=None)) as finalize_mock,
                ):
                    asyncio.run(service._continue_manual_daily_sprint(sprint_state, announce=False))

                self.assertEqual(sprint_state["phase"], "wrap_up")
                finalize_mock.assert_awaited_once_with(sprint_state)

    def test_cancel_request_warns_when_task_is_uncommitted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "req-cancel-uncommitted",
                    "status": "uncommitted",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "미커밋 task",
                    "body": "변경은 끝났지만 아직 commit되지 않았습니다.",
                    "artifacts": [],
                    "params": {},
                    "current_role": "orchestrator",
                    "next_role": "",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-24T00:03:80+00:00",
                    "updated_at": "2026-03-24T00:03:80+00:00",
                    "fingerprint": "req-cancel-uncommitted",
                    "reply_route": {"channel_id": "channel-1", "author_id": "user-1", "is_dm": False},
                    "events": [],
                    "result": {},
                    "version_control_status": "failed",
                    "version_control_paths": ["teams_runtime/core/orchestration.py"],
                    "task_commit_paths": ["teams_runtime/core/orchestration.py"],
                }
                service._save_request(request_record)
                message = DiscordMessage(
                    message_id="msg-1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="user",
                    content="cancel request_id:req-cancel-uncommitted",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id="req-cancel-uncommitted",
                    sender="user",
                    target="orchestrator",
                    intent="cancel",
                    urgency="normal",
                    scope="",
                    artifacts=[],
                    params={},
                    body="cancel request_id:req-cancel-uncommitted",
                )

                asyncio.run(service._cancel_request(message, envelope))

                updated = service._load_request("req-cancel-uncommitted")
                self.assertEqual(updated["status"], "uncommitted")
                self.assertTrue(service.discord_client.sent_channels)
                self.assertIn("uncommitted 상태라 취소할 수 없습니다", service.discord_client.sent_channels[-1][1])

    def test_continue_sprint_prunes_legacy_insight_backlog_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                real_item = build_backlog_item(
                    title="실제 후속 작업",
                    summary="수정이 필요한 backlog입니다.",
                    kind="bug",
                    source="user",
                    scope="실제 후속 작업",
                )
                real_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(real_item.get("title") or ""),
                    scope=str(real_item.get("scope") or ""),
                    kind=str(real_item.get("kind") or ""),
                )
                insight_item = build_backlog_item(
                    title="planner insight follow-up",
                    summary="관찰 메모입니다.",
                    kind="enhancement",
                    source="discovery",
                    scope="관찰 메모입니다.",
                )
                insight_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(insight_item.get("title") or ""),
                    scope=str(insight_item.get("scope") or ""),
                    kind=str(insight_item.get("kind") or ""),
                )
                service._save_backlog_item(real_item)
                service._save_backlog_item(insight_item)

                sprint_state = {
                    "sprint_id": "sprint-1",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-03-24T00:00:00+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [
                        str(real_item.get("backlog_id") or ""),
                        str(insight_item.get("backlog_id") or ""),
                    ],
                    "selected_items": [dict(real_item), dict(insight_item)],
                    "todos": [
                        {
                            "todo_id": "todo-real",
                            "backlog_id": str(real_item.get("backlog_id") or ""),
                            "title": str(real_item.get("title") or ""),
                            "owner_role": "planner",
                            "status": "running",
                            "request_id": "req-real",
                            "artifacts": [],
                            "summary": "",
                            "acceptance_criteria": [],
                            "started_at": "",
                            "ended_at": "",
                            "carry_over_backlog_id": "",
                        },
                        {
                            "todo_id": "todo-insight",
                            "backlog_id": str(insight_item.get("backlog_id") or ""),
                            "title": str(insight_item.get("title") or ""),
                            "owner_role": "planner",
                            "status": "queued",
                            "request_id": "",
                            "artifacts": [],
                            "summary": "",
                            "acceptance_criteria": [],
                            "started_at": "",
                            "ended_at": "",
                            "carry_over_backlog_id": "",
                        },
                    ],
                }

                dropped_ids = service._drop_non_actionable_backlog_items()
                changed = service._prune_dropped_backlog_from_sprint(sprint_state, dropped_ids)
                service._save_sprint_state(sprint_state)
                service._refresh_backlog_markdown()

                self.assertTrue(changed)
                self.assertIn(str(insight_item.get("backlog_id") or ""), dropped_ids)
                self.assertEqual(
                    service._load_backlog_item(str(insight_item.get("backlog_id") or "")).get("status"),
                    "dropped",
                )
                self.assertEqual(len(sprint_state["todos"]), 1)
                self.assertEqual(
                    sprint_state["todos"][0]["backlog_id"],
                    str(real_item.get("backlog_id") or ""),
                )
                backlog_text = service.paths.shared_backlog_file.read_text(encoding="utf-8")
                self.assertIn("실제 후속 작업", backlog_text)
                self.assertNotIn("planner insight follow-up", backlog_text)

    def test_autonomous_sprint_archives_history_and_marks_backlog_done(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="intraday trading 개선",
                    summary="전략 개선안을 계획하고 구현합니다.",
                    kind="enhancement",
                    source="user",
                    scope="intraday trading 개선",
                )
                service._save_backlog_item(backlog_item)
                service.paths.current_sprint_file.write_text("# current sprint\n", encoding="utf-8")
                planner_draft = {
                    "title": "intraday trading 개선",
                    "what_changed": ["전략 개선안을 planning/implementation/qa 흐름으로 마무리했습니다."],
                    "why_it_mattered": ["QA까지 완료된 증적을 기준으로 sprint closeout report를 정리했습니다."],
                }

                async def fake_delegate(request_record, next_role):
                    workflow = dict(request_record.get("params", {}).get("workflow") or {})
                    workflow_step = str(workflow.get("step") or "")
                    if next_role == "planner":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "개선 계획을 정리했고 implementation guidance로 이어집니다.",
                            "insights": ["체결 강도 기준을 QA가 검증해야 합니다."],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "architect_guidance",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "architect guidance를 먼저 거칩니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": True,
                                }
                            },
                            "artifacts": ["./shared_workspace/current_sprint.md"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                    elif next_role == "architect" and workflow_step == "architect_guidance":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "architect",
                            "status": "completed",
                            "summary": "구현 전 technical guidance를 정리했습니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "developer_build",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "developer 구현 단계로 진행합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                    elif next_role == "developer" and workflow_step == "developer_build":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "developer",
                            "status": "completed",
                            "summary": "개선 구현을 마쳤습니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "architect_review",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "architect review를 진행합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": ["workspace/src/intraday.py"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                    elif next_role == "architect" and workflow_step == "architect_review":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "architect",
                            "status": "completed",
                            "summary": "구조 리뷰를 마쳤고 developer revision이 필요합니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "developer_revision",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "review findings를 developer가 반영합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                    elif next_role == "developer" and workflow_step == "developer_revision":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "developer",
                            "status": "completed",
                            "summary": "architect review 반영을 마쳤습니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "validation",
                                    "target_step": "qa_validation",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "QA 검증으로 넘깁니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": ["workspace/src/intraday.py"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                    else:
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "qa",
                            "status": "completed",
                            "summary": "QA 검증을 통과했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                    await service._handle_role_report(
                        DiscordMessage(
                            message_id=f"relay-{next_role}",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role(next_role).bot_id,
                            author_name=next_role,
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender=next_role,
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=str(request_record.get("scope") or ""),
                            params={"_teams_kind": "report", "result": result},
                        ),
                    )

                with (
                    patch.object(service, "_discover_backlog_candidates", return_value=[]),
                    patch.object(service, "_delegate_request", side_effect=fake_delegate),
                    patch.object(
                        service.version_controller_runtime,
                        "run_task",
                        return_value={
                            "request_id": "sprint-request",
                            "role": "version_controller",
                            "status": "completed",
                            "summary": "task 변경을 커밋했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["sources/sprint-request.task.version_control.json"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                            "commit_status": "no_changes",
                            "commit_sha": "",
                            "commit_paths": [],
                            "commit_message": "",
                            "change_detected": False,
                        },
                    ),
                    patch("teams_runtime.core.orchestration.build_active_sprint_id", return_value="2026-Sprint-01-20260323T000000Z"),
                    patch("teams_runtime.core.orchestration.capture_git_baseline", return_value={"repo_root": "", "head_sha": "", "dirty_paths": []}),
                    patch(
                        "teams_runtime.core.orchestration.inspect_sprint_closeout",
                        return_value={
                            "status": "verified",
                            "representative_commit_sha": "abc123",
                            "commit_count": 1,
                            "commit_shas": ["abc123"],
                            "uncommitted_paths": [],
                            "message": "closeout verified",
                        },
                    ),
                    patch.object(service, "_draft_sprint_report_via_planner", AsyncMock(return_value=planner_draft)),
                ):
                    asyncio.run(service._run_autonomous_sprint("backlog_ready"))

                sprint_state = json.loads(
                    service.paths.sprint_file("2026-Sprint-01-20260323T000000Z").read_text(encoding="utf-8")
                )
                self.assertEqual(sprint_state["status"], "completed")
                artifact_root = Path(sprint_state["sprint_folder"])
                self.assertEqual(artifact_root.name, build_sprint_artifact_folder_name(sprint_state["sprint_id"]))
                self.assertTrue((artifact_root / "index.md").exists())
                self.assertEqual(sprint_state["commit_sha"], "abc123")
                self.assertEqual(sprint_state["closeout_status"], "verified")
                self.assertEqual(sprint_state["commit_count"], 1)
                self.assertEqual(len(sprint_state["todos"]), 1)
                self.assertEqual(sprint_state["todos"][0]["status"], "completed")
                current_sprint_text = service.paths.current_sprint_file.read_text(encoding="utf-8")
                self.assertIn("active sprint 없음", current_sprint_text)
                history_text = service.paths.sprint_history_file("2026-Sprint-01-20260323T000000Z").read_text(encoding="utf-8")
                self.assertIn("intraday trading 개선", history_text)
                self.assertIn("QA 검증을 통과했습니다.", history_text)
                index_text = service.paths.sprint_history_index_file.read_text(encoding="utf-8")
                self.assertIn("2026-Sprint-01-20260323T000000Z", index_text)
                updated_backlog = service._load_backlog_item(backlog_item["backlog_id"])
                self.assertEqual(updated_backlog["status"], "done")
                active_backlog_text = service.paths.shared_backlog_file.read_text(encoding="utf-8")
                completed_backlog_text = service.paths.shared_completed_backlog_file.read_text(encoding="utf-8")
                self.assertNotIn("### intraday trading 개선", active_backlog_text)
                self.assertIn("### intraday trading 개선", completed_backlog_text)
                self.assertIn("- created_at:", completed_backlog_text)
                combined_reports = "\n".join(content for _channel_id, content in service.discord_client.sent_channels)
                self.assertIn("🚀 스프린트 시작", combined_reports)
                self.assertIn("✅ 스프린트 완료", combined_reports)

    def test_execute_sprint_todo_continues_same_todo_after_architect_review_rejection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="architect review revision loop",
                    summary="architect review rejection 이후 같은 todo에서 revision을 이어가야 합니다.",
                    kind="bug",
                    source="user",
                    scope="architect review revision loop",
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="planner")
                sprint_state = {
                    "sprint_id": "2026-Sprint-Review-Loop",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-03-24T00:03:00+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                service._save_sprint_state(sprint_state)

                def save_workflow_state(
                    request_id: str,
                    *,
                    status: str,
                    current_role: str,
                    next_role: str,
                    phase: str,
                    step: str,
                    phase_owner: str,
                    result: dict[str, object],
                ) -> dict[str, object]:
                    updated_request = service._load_request(request_id)
                    updated_request["status"] = status
                    updated_request["current_role"] = current_role
                    updated_request["next_role"] = next_role
                    updated_request["result"] = result
                    updated_request["artifacts"] = list(result.get("artifacts") or [])
                    updated_params = dict(updated_request.get("params") or {})
                    updated_workflow = dict(updated_params.get("workflow") or {})
                    updated_workflow["phase"] = phase
                    updated_workflow["step"] = step
                    updated_workflow["phase_owner"] = phase_owner
                    updated_workflow["phase_status"] = "active" if status == "delegated" else "completed"
                    updated_params["workflow"] = updated_workflow
                    updated_request["params"] = updated_params
                    service._save_request(updated_request)
                    return updated_request

                async def fake_delegate(request_record, next_role):
                    workflow = dict(request_record.get("params", {}).get("workflow") or {})
                    workflow_step = str(workflow.get("step") or "")
                    if next_role == "research":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "research",
                            "status": "completed",
                            "summary": "외부 research 없이 planner가 implementation plan을 이어갈 수 있다고 판단했습니다.",
                            "insights": [],
                            "proposals": {
                                "research_signal": {
                                    "needed": False,
                                    "subject": "",
                                    "research_query": "",
                                    "reason_code": "not_needed_no_subject",
                                },
                                "research_subject_definition": _no_subject_definition(),
                                "research_report": {
                                    "report_artifact": "",
                                    "headline": "외부 research 불필요",
                                    "planner_guidance": "planner가 local implementation context만으로 다음 단계를 정리할 수 있습니다.",
                                    "research_subject_definition": _no_subject_definition(),
                                    "backing_sources": [],
                                    "open_questions": [],
                                    "effective_config": {},
                                },
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = save_workflow_state(
                            request_record["request_id"],
                            status="delegated",
                            current_role="planner",
                            next_role="planner",
                            phase="planning",
                            step="planner_draft",
                            phase_owner="planner",
                            result=result,
                        )
                        await fake_delegate(updated_request, "planner")
                        return
                    if next_role == "planner":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "계획을 정리했고 architect guidance로 넘깁니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "architect_guidance",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "architect guidance를 먼저 거칩니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": True,
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = save_workflow_state(
                            request_record["request_id"],
                            status="delegated",
                            current_role="architect",
                            next_role="architect",
                            phase="implementation",
                            step="architect_guidance",
                            phase_owner="architect",
                            result=result,
                        )
                        await fake_delegate(updated_request, "architect")
                        return
                    elif next_role == "architect" and workflow_step == "architect_guidance":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "architect",
                            "status": "completed",
                            "summary": "구현 전 technical guidance를 정리했습니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "developer_build",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "developer 구현 단계로 진행합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = save_workflow_state(
                            request_record["request_id"],
                            status="delegated",
                            current_role="developer",
                            next_role="developer",
                            phase="implementation",
                            step="developer_build",
                            phase_owner="developer",
                            result=result,
                        )
                        await fake_delegate(updated_request, "developer")
                        return
                    elif next_role == "developer" and workflow_step == "developer_build":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "developer",
                            "status": "completed",
                            "summary": "초기 구현을 마쳤습니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "architect_review",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "architect review를 진행합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": ["workspace/src/intraday.py"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = save_workflow_state(
                            request_record["request_id"],
                            status="delegated",
                            current_role="architect",
                            next_role="architect",
                            phase="implementation",
                            step="architect_review",
                            phase_owner="architect",
                            result=result,
                        )
                        await fake_delegate(updated_request, "architect")
                        return
                    elif next_role == "architect" and workflow_step == "architect_review":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "architect",
                            "status": "blocked",
                            "summary": "구조 리뷰에서 수정이 필요해 developer revision으로 넘깁니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "developer_revision",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "review findings를 developer가 반영합니다.",
                                    "unresolved_items": ["구조 리뷰 반영"],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "review failed",
                        }
                        updated_request = save_workflow_state(
                            request_record["request_id"],
                            status="delegated",
                            current_role="developer",
                            next_role="developer",
                            phase="implementation",
                            step="developer_revision",
                            phase_owner="developer",
                            result=result,
                        )
                        await fake_delegate(updated_request, "developer")
                        return
                    elif next_role == "developer" and workflow_step == "developer_revision":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "developer",
                            "status": "completed",
                            "summary": "architect review 반영을 마쳤습니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "validation",
                                    "target_step": "qa_validation",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "QA 검증으로 넘깁니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": ["workspace/src/intraday.py"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = save_workflow_state(
                            request_record["request_id"],
                            status="delegated",
                            current_role="qa",
                            next_role="qa",
                            phase="validation",
                            step="qa_validation",
                            phase_owner="qa",
                            result=result,
                        )
                        await fake_delegate(updated_request, "qa")
                        return
                    else:
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "qa",
                            "status": "completed",
                            "summary": "QA 검증을 통과했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        save_workflow_state(
                            request_record["request_id"],
                            status="completed",
                            current_role="orchestrator",
                            next_role="",
                            phase="closeout",
                            step="closeout",
                            phase_owner="orchestrator",
                            result=result,
                        )
                        return

                async def fake_enforce_task_commit_for_completed_todo(**kwargs):
                    return dict(kwargs["result"])

                with (
                    patch.object(service, "_delegate_request", side_effect=fake_delegate),
                    patch.object(service, "_enforce_task_commit_for_completed_todo", side_effect=fake_enforce_task_commit_for_completed_todo),
                ):
                    asyncio.run(service._execute_sprint_todo(sprint_state, todo))

                updated_backlog = service._load_backlog_item(backlog_item["backlog_id"])
                self.assertEqual(todo["status"], "completed")
                self.assertEqual(todo["carry_over_backlog_id"], "")
                self.assertEqual(updated_backlog["status"], "done")
                self.assertEqual(updated_backlog["selected_in_sprint_id"], sprint_state["sprint_id"])
                self.assertEqual(updated_backlog["blocked_reason"], "")

    def test_execute_sprint_todo_can_skip_developer_revision_after_architect_pass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="architect review direct qa handoff",
                    summary="architect review pass 시 developer_revision 없이 QA로 넘어가야 합니다.",
                    kind="bug",
                    source="user",
                    scope="architect review direct qa handoff",
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="planner")
                sprint_state = {
                    "sprint_id": "2026-Sprint-Review-Pass",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-03-24T00:03:00+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                service._save_sprint_state(sprint_state)

                delegated_steps: list[tuple[str, str]] = []

                async def fake_delegate(request_record, next_role):
                    workflow = dict(request_record.get("params", {}).get("workflow") or {})
                    workflow_step = str(workflow.get("step") or "")
                    delegated_steps.append((next_role, workflow_step))
                    if next_role == "research":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "research",
                            "status": "completed",
                            "summary": "외부 research 없이 planner가 implementation plan을 이어갈 수 있다고 판단했습니다.",
                            "insights": [],
                            "proposals": {
                                "research_signal": {
                                    "needed": False,
                                    "subject": "",
                                    "research_query": "",
                                    "reason_code": "not_needed_no_subject",
                                },
                                "research_subject_definition": _no_subject_definition(),
                                "research_report": {
                                    "report_artifact": "",
                                    "headline": "외부 research 불필요",
                                    "planner_guidance": "planner가 local implementation context만으로 다음 단계를 정리할 수 있습니다.",
                                    "research_subject_definition": _no_subject_definition(),
                                    "backing_sources": [],
                                    "open_questions": [],
                                    "effective_config": {},
                                },
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = service._load_request(request_record["request_id"])
                        updated_request["status"] = "delegated"
                        updated_request["current_role"] = "planner"
                        updated_request["next_role"] = "planner"
                        updated_request["result"] = result
                        updated_request["artifacts"] = []
                        updated_params = dict(updated_request.get("params") or {})
                        updated_workflow = dict(updated_params.get("workflow") or {})
                        updated_workflow["step"] = "planner_draft"
                        updated_workflow["phase_owner"] = "planner"
                        updated_workflow["phase_status"] = "active"
                        updated_params["workflow"] = updated_workflow
                        updated_request["params"] = updated_params
                        service._save_request(updated_request)
                        await fake_delegate(updated_request, "planner")
                        return
                    if next_role == "planner":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "계획을 정리했고 architect guidance로 넘깁니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "architect_guidance",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "architect guidance를 먼저 거칩니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": True,
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = service._load_request(request_record["request_id"])
                        updated_request["status"] = "delegated"
                        updated_request["current_role"] = "architect"
                        updated_request["next_role"] = "architect"
                        updated_request["result"] = result
                        updated_request["artifacts"] = []
                        updated_params = dict(updated_request.get("params") or {})
                        updated_workflow = dict(updated_params.get("workflow") or {})
                        updated_workflow["phase"] = "implementation"
                        updated_workflow["step"] = "architect_guidance"
                        updated_workflow["phase_owner"] = "architect"
                        updated_workflow["phase_status"] = "active"
                        updated_params["workflow"] = updated_workflow
                        updated_request["params"] = updated_params
                        service._save_request(updated_request)
                        await fake_delegate(updated_request, "architect")
                        return
                    elif next_role == "architect" and workflow_step == "architect_guidance":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "architect",
                            "status": "completed",
                            "summary": "구현 전 technical guidance를 정리했습니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "developer_build",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "developer 구현 단계로 진행합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = service._load_request(request_record["request_id"])
                        updated_request["status"] = "delegated"
                        updated_request["current_role"] = "developer"
                        updated_request["next_role"] = "developer"
                        updated_request["result"] = result
                        updated_request["artifacts"] = []
                        updated_params = dict(updated_request.get("params") or {})
                        updated_workflow = dict(updated_params.get("workflow") or {})
                        updated_workflow["phase"] = "implementation"
                        updated_workflow["step"] = "developer_build"
                        updated_workflow["phase_owner"] = "developer"
                        updated_workflow["phase_status"] = "active"
                        updated_params["workflow"] = updated_workflow
                        updated_request["params"] = updated_params
                        service._save_request(updated_request)
                        await fake_delegate(updated_request, "developer")
                        return
                    elif next_role == "developer" and workflow_step == "developer_build":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "developer",
                            "status": "completed",
                            "summary": "초기 구현을 마쳤습니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "architect_review",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "architect review를 진행합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": ["workspace/src/intraday.py"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = service._load_request(request_record["request_id"])
                        updated_request["status"] = "delegated"
                        updated_request["current_role"] = "architect"
                        updated_request["next_role"] = "architect"
                        updated_request["result"] = result
                        updated_request["artifacts"] = list(result["artifacts"])
                        updated_params = dict(updated_request.get("params") or {})
                        updated_workflow = dict(updated_params.get("workflow") or {})
                        updated_workflow["phase"] = "implementation"
                        updated_workflow["step"] = "architect_review"
                        updated_workflow["phase_owner"] = "architect"
                        updated_workflow["phase_status"] = "active"
                        updated_params["workflow"] = updated_workflow
                        updated_request["params"] = updated_params
                        service._save_request(updated_request)
                        await fake_delegate(updated_request, "architect")
                        return
                    elif next_role == "architect" and workflow_step == "architect_review":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "architect",
                            "status": "completed",
                            "summary": "구조 리뷰를 통과해 QA 검증으로 넘깁니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "validation",
                                    "target_step": "qa_validation",
                                    "requested_role": "qa",
                                    "reopen_category": "",
                                    "reason": "추가 developer 수정 없이 QA가 회귀를 검증합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = service._load_request(request_record["request_id"])
                        updated_request["status"] = "delegated"
                        updated_request["current_role"] = "qa"
                        updated_request["next_role"] = "qa"
                        updated_request["result"] = result
                        updated_request["artifacts"] = []
                        updated_params = dict(updated_request.get("params") or {})
                        updated_workflow = dict(updated_params.get("workflow") or {})
                        updated_workflow["phase"] = "validation"
                        updated_workflow["step"] = "qa_validation"
                        updated_workflow["phase_owner"] = "qa"
                        updated_workflow["phase_status"] = "active"
                        updated_params["workflow"] = updated_workflow
                        updated_request["params"] = updated_params
                        service._save_request(updated_request)
                        await fake_delegate(updated_request, "qa")
                        return
                    elif next_role == "qa" and workflow_step == "qa_validation":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "qa",
                            "status": "completed",
                            "summary": "QA 검증을 통과했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = service._load_request(request_record["request_id"])
                        updated_request["status"] = "completed"
                        updated_request["current_role"] = "orchestrator"
                        updated_request["next_role"] = ""
                        updated_request["result"] = result
                        updated_request["artifacts"] = []
                        service._save_request(updated_request)
                        return
                    else:
                        raise AssertionError(f"unexpected delegation: {next_role=} {workflow_step=}")

                async def fake_enforce_task_commit_for_completed_todo(**kwargs):
                    return dict(kwargs["result"])

                with (
                    patch.object(service, "_delegate_request", side_effect=fake_delegate),
                    patch.object(service, "_enforce_task_commit_for_completed_todo", side_effect=fake_enforce_task_commit_for_completed_todo),
                ):
                    asyncio.run(service._execute_sprint_todo(sprint_state, todo))

                updated_backlog = service._load_backlog_item(backlog_item["backlog_id"])
                self.assertEqual(todo["status"], "completed")
                self.assertEqual(updated_backlog["status"], "done")
                self.assertNotIn(("developer", "developer_revision"), delegated_steps)
                self.assertEqual(
                    delegated_steps,
                    [
                        ("planner", "planner_draft"),
                        ("architect", "architect_guidance"),
                        ("developer", "developer_build"),
                        ("architect", "architect_review"),
                        ("qa", "qa_validation"),
                    ],
                )

    def test_execute_sprint_todo_closes_doc_only_planning_work_without_implementation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="designer advisory 누락 원인 고정",
                    summary="prior sprint root cause를 current sprint planning surface에 설명으로 고정합니다.",
                    kind="bug",
                    source="planner",
                    scope=(
                        "prior sprint가 왜 designer 없이 planner->architect->developer->qa로 닫혔는지와 "
                        "어떤 request/spec/workflow field가 그 판정을 만들었는지를 current sprint 기준 설명으로 고정한다."
                    ),
                    acceptance_criteria=[
                        "root cause와 current sprint remediation이 canonical planning surface에서 분리되어 설명된다.",
                        "todo_backlog는 compact queue 요약으로 남을 수 있다.",
                    ],
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="planner")
                sprint_state = {
                    "sprint_id": "2026-Sprint-Doc-Only",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-04-12T08:00:00+09:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                service._save_sprint_state(sprint_state)

                delegated_steps: list[tuple[str, str]] = []

                async def fake_delegate(request_record, next_role):
                    workflow = dict(request_record.get("params", {}).get("workflow") or {})
                    delegated_steps.append((next_role, str(workflow.get("step") or "")))
                    if next_role == "research":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "research",
                            "status": "completed",
                            "summary": "외부 research 없이 planner가 current sprint planning surface를 정리할 수 있다고 판단했습니다.",
                            "insights": [],
                            "proposals": {
                                "research_signal": {
                                    "needed": False,
                                    "subject": "",
                                    "research_query": "",
                                    "reason_code": "not_needed_no_subject",
                                },
                                "research_subject_definition": _no_subject_definition(),
                                "research_report": {
                                    "report_artifact": "",
                                    "headline": "외부 research 불필요",
                                    "planner_guidance": "repo/local sprint evidence만으로 planning을 이어갈 수 있습니다.",
                                    "research_subject_definition": _no_subject_definition(),
                                    "backing_sources": [],
                                    "open_questions": [],
                                    "effective_config": {},
                                },
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = service._load_request(request_record["request_id"])
                        updated_request["status"] = "delegated"
                        updated_request["current_role"] = "planner"
                        updated_request["next_role"] = "planner"
                        updated_request["result"] = result
                        updated_request["artifacts"] = list(result["artifacts"])
                        updated_params = dict(updated_request.get("params") or {})
                        updated_workflow = dict(updated_params.get("workflow") or {})
                        updated_workflow["step"] = "planner_draft"
                        updated_workflow["phase_owner"] = "planner"
                        updated_workflow["phase_status"] = "active"
                        updated_params["workflow"] = updated_workflow
                        updated_request["params"] = updated_params
                        service._save_request(updated_request)
                        await fake_delegate(updated_request, "planner")
                        return True
                    elif next_role == "planner":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "prior sprint root cause를 current sprint planning surface에 고정했습니다.",
                            "insights": [],
                            "proposals": {
                                "root_cause_contract": {
                                    "summary": "designer advisory 누락의 request/workflow contract를 정리했습니다.",
                                },
                                "todo_brief": {
                                    "summary": "todo_backlog는 compact queue로 유지합니다.",
                                },
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "architect_guidance",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "current sprint planning surface 반영이 끝나 요청을 닫을 수 있습니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": True,
                                },
                            },
                            "artifacts": [
                                "./.teams_runtime/requests/20260412-cab6628c.json",
                                f"./.teams_runtime/backlog/{backlog_item['backlog_id']}.json",
                                "./shared_workspace/sprint_history/260412-Sprint-16:05.md",
                                "./shared_workspace/current_sprint.md",
                                "./shared_workspace/sprints/260412-Sprint-17-00/spec.md",
                                "./shared_workspace/sprints/260412-Sprint-17-00/iteration_log.md",
                            ],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = service._load_request(request_record["request_id"])
                        updated_request["status"] = "completed"
                        updated_request["current_role"] = "orchestrator"
                        updated_request["next_role"] = ""
                        updated_request["result"] = result
                        updated_request["artifacts"] = list(result["artifacts"])
                        updated_params = dict(updated_request.get("params") or {})
                        updated_workflow = dict(updated_params.get("workflow") or {})
                        updated_workflow["phase"] = "closeout"
                        updated_workflow["step"] = "closeout"
                        updated_workflow["phase_owner"] = "orchestrator"
                        updated_workflow["phase_status"] = "completed"
                        updated_params["workflow"] = updated_workflow
                        updated_request["params"] = updated_params
                        service._save_request(updated_request)
                        return True
                    else:
                        raise AssertionError(f"unexpected delegation: {next_role=}")

                async def fake_enforce_task_commit_for_completed_todo(**kwargs):
                    return dict(kwargs["result"])

                with (
                    patch.object(service, "_delegate_request", side_effect=fake_delegate),
                    patch.object(service, "_enforce_task_commit_for_completed_todo", side_effect=fake_enforce_task_commit_for_completed_todo),
                ):
                    asyncio.run(service._execute_sprint_todo(sprint_state, todo))

                updated_request = service._load_request(str(todo.get("request_id") or ""))
                updated_workflow = dict(updated_request.get("params", {}).get("workflow") or {})
                updated_backlog = service._load_backlog_item(backlog_item["backlog_id"])
                self.assertEqual(delegated_steps, [("planner", "planner_draft")])
                self.assertEqual(todo["status"], "completed")
                self.assertEqual(updated_backlog["status"], "done")
                self.assertEqual(updated_request["status"], "completed")
                self.assertEqual(updated_request["current_role"], "orchestrator")
                self.assertEqual(updated_workflow["phase"], "closeout")
                self.assertEqual(updated_workflow["step"], "closeout")

    def test_save_sprint_state_refreshes_todo_projection_from_newer_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="current sprint projection refresh",
                    summary="더 최신 request 결과가 있으면 todo summary/artifacts를 다시 투영해야 합니다.",
                    kind="bug",
                    source="planner",
                    scope="current sprint projection refresh",
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="planner")
                todo["request_id"] = "request-projection-refresh-1"
                todo["status"] = "completed"
                todo["summary"] = "예전 planner advisory 요약"
                todo["artifacts"] = ["./shared_workspace/current_sprint.md"]
                todo["created_at"] = "2026-04-15T00:00:00+00:00"
                todo["updated_at"] = "2026-04-15T00:00:00+00:00"
                sprint_state = {
                    "sprint_id": "2026-Sprint-Projection-Refresh",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-04-15T00:00:00+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                service._save_sprint_state(sprint_state)

                request_record = {
                    "request_id": "request-projection-refresh-1",
                    "status": "blocked",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "current sprint projection refresh",
                    "body": "current sprint projection refresh",
                    "artifacts": ["./shared_workspace/current_sprint.md", "./workspace/formatters.py"],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "validation",
                            "step": "qa_validation",
                            "phase_owner": "qa",
                            "phase_status": "active",
                            "planning_pass_count": 0,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "qa",
                            "reopen_category": "verification",
                            "review_cycle_count": 0,
                            "review_cycle_limit": 3,
                        },
                    },
                    "current_role": "qa",
                    "next_role": "qa",
                    "owner_role": "orchestrator",
                    "sprint_id": sprint_state["sprint_id"],
                    "backlog_id": backlog_item["backlog_id"],
                    "todo_id": todo["todo_id"],
                    "created_at": "2026-04-15T00:00:00+00:00",
                    "updated_at": "2026-04-15T01:00:00+00:00",
                    "fingerprint": "projection-refresh",
                    "reply_route": {},
                    "events": [],
                    "result": {
                        "summary": "최신 구현/검증 결과를 반영한 요약",
                        "artifacts": ["./shared_workspace/current_sprint.md", "./workspace/formatters.py"],
                    },
                }
                recovered = service._build_recovered_sprint_todo_from_request(sprint_state, request_record)
                refreshed_todo = service._merge_recovered_sprint_todo(todo, recovered)
                sprint_state["todos"] = [refreshed_todo]
                service._save_sprint_state(sprint_state)
                current_sprint_text = service.paths.current_sprint_file.read_text(encoding="utf-8")

                self.assertEqual(refreshed_todo["status"], "blocked")
                self.assertEqual(refreshed_todo["summary"], "최신 구현/검증 결과를 반영한 요약")
                self.assertEqual(refreshed_todo["artifacts"], ["./workspace/formatters.py"])
                self.assertIn("summary: 최신 구현/검증 결과를 반영한 요약", current_sprint_text)
                self.assertIn("./workspace/formatters.py", current_sprint_text)
                self.assertNotIn("./shared_workspace/current_sprint.md", current_sprint_text)

    def test_execute_sprint_todo_blocks_after_architect_review_cycle_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="architect review limit block",
                    summary="architect review가 반복 실패하면 같은 todo를 blocked로 종료해야 합니다.",
                    kind="bug",
                    source="user",
                    scope="architect review limit block",
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="planner")
                sprint_state = {
                    "sprint_id": "2026-Sprint-Review-Limit",
                    "status": "running",
                    "trigger": "test",
                    "started_at": "2026-03-24T00:03:00+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                    "commit_sha": "",
                    "report_path": "",
                }
                service._save_sprint_state(sprint_state)

                architect_review_attempts = {"count": 0}
                developer_revision_attempts = {"count": 0}

                def save_workflow_state(
                    request_id: str,
                    *,
                    status: str,
                    current_role: str,
                    next_role: str,
                    phase: str,
                    step: str,
                    phase_owner: str,
                    result: dict[str, object],
                ) -> dict[str, object]:
                    updated_request = service._load_request(request_id)
                    updated_request["status"] = status
                    updated_request["current_role"] = current_role
                    updated_request["next_role"] = next_role
                    updated_request["result"] = result
                    updated_request["artifacts"] = list(result.get("artifacts") or [])
                    updated_params = dict(updated_request.get("params") or {})
                    updated_workflow = dict(updated_params.get("workflow") or {})
                    updated_workflow["phase"] = phase
                    updated_workflow["step"] = step
                    updated_workflow["phase_owner"] = phase_owner
                    updated_workflow["phase_status"] = "active" if status == "delegated" else "completed"
                    updated_params["workflow"] = updated_workflow
                    updated_request["params"] = updated_params
                    service._save_request(updated_request)
                    return updated_request

                async def fake_delegate(request_record, next_role):
                    workflow = dict(request_record.get("params", {}).get("workflow") or {})
                    workflow_step = str(workflow.get("step") or "")
                    if next_role == "research":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "research",
                            "status": "completed",
                            "summary": "외부 research 없이 planner가 implementation plan을 이어갈 수 있다고 판단했습니다.",
                            "insights": [],
                            "proposals": {
                                "research_signal": {
                                    "needed": False,
                                    "subject": "",
                                    "research_query": "",
                                    "reason_code": "not_needed_no_subject",
                                },
                                "research_subject_definition": _no_subject_definition(),
                                "research_report": {
                                    "report_artifact": "",
                                    "headline": "외부 research 불필요",
                                    "planner_guidance": "planner가 local implementation context만으로 다음 단계를 정리할 수 있습니다.",
                                    "research_subject_definition": _no_subject_definition(),
                                    "backing_sources": [],
                                    "open_questions": [],
                                    "effective_config": {},
                                },
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = save_workflow_state(
                            request_record["request_id"],
                            status="delegated",
                            current_role="planner",
                            next_role="planner",
                            phase="planning",
                            step="planner_draft",
                            phase_owner="planner",
                            result=result,
                        )
                        await fake_delegate(updated_request, "planner")
                        return
                    if next_role == "planner":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "계획을 정리했고 architect guidance로 넘깁니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "architect_guidance",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "architect guidance를 먼저 거칩니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": True,
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = save_workflow_state(
                            request_record["request_id"],
                            status="delegated",
                            current_role="architect",
                            next_role="architect",
                            phase="implementation",
                            step="architect_guidance",
                            phase_owner="architect",
                            result=result,
                        )
                        await fake_delegate(updated_request, "architect")
                        return
                    elif next_role == "architect" and workflow_step == "architect_guidance":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "architect",
                            "status": "completed",
                            "summary": "구현 전 technical guidance를 정리했습니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "developer_build",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "developer 구현 단계로 진행합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = save_workflow_state(
                            request_record["request_id"],
                            status="delegated",
                            current_role="developer",
                            next_role="developer",
                            phase="implementation",
                            step="developer_build",
                            phase_owner="developer",
                            result=result,
                        )
                        await fake_delegate(updated_request, "developer")
                        return
                    elif next_role == "developer" and workflow_step == "developer_build":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "developer",
                            "status": "completed",
                            "summary": "초기 구현을 마쳤습니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "architect_review",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "architect review를 진행합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": ["workspace/src/intraday.py"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = save_workflow_state(
                            request_record["request_id"],
                            status="delegated",
                            current_role="architect",
                            next_role="architect",
                            phase="implementation",
                            step="architect_review",
                            phase_owner="architect",
                            result=result,
                        )
                        await fake_delegate(updated_request, "architect")
                        return
                    elif next_role == "architect" and workflow_step == "architect_review":
                        architect_review_attempts["count"] += 1
                        if architect_review_attempts["count"] >= 3:
                            result = {
                                "request_id": request_record["request_id"],
                                "role": "architect",
                                "status": "blocked",
                                "summary": "architect review가 3회 연속 미통과하여 review cycle limit 3에 도달했습니다.",
                                "insights": [],
                                "proposals": {},
                                "artifacts": [],
                                "next_role": "",
                                "approval_needed": False,
                                "error": "review failed",
                            }
                            save_workflow_state(
                                request_record["request_id"],
                                status="blocked",
                                current_role="orchestrator",
                                next_role="",
                                phase="closeout",
                                step="closeout",
                                phase_owner="orchestrator",
                                result=result,
                            )
                            return
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "architect",
                            "status": "blocked",
                            "summary": f"{architect_review_attempts['count']}차 구조 리뷰에서도 수정이 필요합니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "developer_revision",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "review findings를 developer가 반영합니다.",
                                    "unresolved_items": [f"{architect_review_attempts['count']}차 구조 리뷰 반영"],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "review failed",
                        }
                        updated_request = save_workflow_state(
                            request_record["request_id"],
                            status="delegated",
                            current_role="developer",
                            next_role="developer",
                            phase="implementation",
                            step="developer_revision",
                            phase_owner="developer",
                            result=result,
                        )
                        await fake_delegate(updated_request, "developer")
                        return
                    elif next_role == "developer" and workflow_step == "developer_revision":
                        developer_revision_attempts["count"] += 1
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "developer",
                            "status": "completed",
                            "summary": f"{developer_revision_attempts['count']}차 architect review 반영을 마쳤습니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "architect_review",
                                    "requested_role": "",
                                    "reopen_category": "",
                                    "reason": "architect가 수정 반영을 다시 검토합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": ["workspace/src/intraday.py"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                        updated_request = save_workflow_state(
                            request_record["request_id"],
                            status="delegated",
                            current_role="architect",
                            next_role="architect",
                            phase="implementation",
                            step="architect_review",
                            phase_owner="architect",
                            result=result,
                        )
                        await fake_delegate(updated_request, "architect")
                        return
                    else:
                        raise AssertionError(f"unexpected delegation: {next_role=} {workflow_step=}")

                async def fake_enforce_task_commit_for_completed_todo(**kwargs):
                    return dict(kwargs["result"])

                with (
                    patch.object(service, "_delegate_request", side_effect=fake_delegate),
                    patch.object(service, "_enforce_task_commit_for_completed_todo", side_effect=fake_enforce_task_commit_for_completed_todo),
                ):
                    asyncio.run(service._execute_sprint_todo(sprint_state, todo))

                updated_backlog = service._load_backlog_item(backlog_item["backlog_id"])
                self.assertEqual(architect_review_attempts["count"], 3)
                self.assertEqual(developer_revision_attempts["count"], 2)
                self.assertEqual(todo["status"], "blocked")
                self.assertEqual(todo["carry_over_backlog_id"], backlog_item["backlog_id"])
                self.assertEqual(updated_backlog["status"], "blocked")
                self.assertEqual(updated_backlog["blocked_by_role"], "architect")
                self.assertIn("review cycle limit 3", updated_backlog["blocked_reason"])
                self.assertEqual(updated_backlog["selected_in_sprint_id"], "")

    def test_autonomous_sprint_continues_when_sprint_report_send_fails(self):
        class _FlakyDiscordClient(FakeDiscordClient):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.remaining_failures = 1

            async def send_channel_message(self, channel_id, content):
                if self.remaining_failures > 0:
                    self.remaining_failures -= 1
                    raise RuntimeError("temporary discord send failure")
                return await super().send_channel_message(channel_id, content)

        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", _FlakyDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="discord report resilience",
                    summary="스프린트 보고 실패에도 계속 진행되어야 합니다.",
                    kind="bug",
                    source="user",
                    scope="discord report resilience",
                )
                service._save_backlog_item(backlog_item)
                service.paths.current_sprint_file.write_text("# current sprint\n", encoding="utf-8")
                planner_draft = {
                    "title": "discord report resilience",
                    "what_changed": ["보고 전송 실패에도 sprint closeout은 완료까지 진행됩니다."],
                    "why_it_mattered": ["Discord 일시 실패가 sprint lifecycle을 막지 않음을 검증합니다."],
                }

                async def fake_delegate(request_record, next_role):
                    if next_role == "planner":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "계획을 세웠고 다음으로 실제 구현을 이어가야 합니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["./shared_workspace/current_sprint.md"],
                            "next_role": "developer",
                            "approval_needed": False,
                            "error": "",
                        }
                    elif next_role == "developer":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "developer",
                            "status": "completed",
                            "summary": "수정을 완료했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["workspace/src/runtime.py"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                    else:
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "qa",
                            "status": "completed",
                            "summary": "QA 검증을 통과했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        }
                    await service._handle_role_report(
                        DiscordMessage(
                            message_id=f"relay-{next_role}",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role(next_role).bot_id,
                            author_name=next_role,
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender=next_role,
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=str(request_record.get("scope") or ""),
                            params={"_teams_kind": "report", "result": result},
                        ),
                    )

                with (
                    patch.object(service, "_discover_backlog_candidates", return_value=[]),
                    patch.object(service, "_delegate_request", side_effect=fake_delegate),
                    patch.object(
                        service.version_controller_runtime,
                        "run_task",
                        return_value={
                            "request_id": "sprint-request",
                            "role": "version_controller",
                            "status": "completed",
                            "summary": "task 변경을 커밋했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["sources/sprint-request.task.version_control.json"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                            "commit_status": "no_changes",
                            "commit_sha": "",
                            "commit_paths": [],
                            "commit_message": "",
                            "change_detected": False,
                        },
                    ),
                    patch("teams_runtime.core.orchestration.build_active_sprint_id", return_value="2026-Sprint-01-20260324T000100Z"),
                    patch("teams_runtime.core.orchestration.capture_git_baseline", return_value={"repo_root": "", "head_sha": "", "dirty_paths": []}),
                    patch(
                        "teams_runtime.core.orchestration.inspect_sprint_closeout",
                        return_value={
                            "status": "verified",
                            "representative_commit_sha": "abc123",
                            "commit_count": 1,
                            "commit_shas": ["abc123"],
                            "uncommitted_paths": [],
                            "message": "closeout verified",
                        },
                    ),
                    patch.object(service, "_draft_sprint_report_via_planner", AsyncMock(return_value=planner_draft)),
                ):
                    asyncio.run(service._run_autonomous_sprint("backlog_ready"))

                sprint_state = json.loads(
                    service.paths.sprint_file("2026-Sprint-01-20260324T000100Z").read_text(encoding="utf-8")
                )
                scheduler_state = service._load_scheduler_state()
                self.assertEqual(sprint_state["status"], "completed")
                self.assertEqual(scheduler_state["active_sprint_id"], "")
                self.assertIn("✅ 스프린트 완료", "\n".join(content for _channel_id, content in service.discord_client.sent_channels))

    def test_load_sprint_state_repairs_cross_sprint_attachment_reference_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                staged_path = (
                    Path(tmpdir)
                    / "shared_workspace"
                    / "sprints"
                    / "260405-Sprint-21-17"
                    / "attachments"
                    / "1490324395179380827_KIS_API_.md"
                )
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path.write_text("# staged kickoff attachment\n", encoding="utf-8")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="김단타 OBI 스캘핑 전략 전환 및 NXT 확장장 대응",
                    trigger="manual_start",
                    started_at=datetime.fromisoformat("2026-04-05T21:18:38+09:00"),
                    kickoff_source_request_id="request-origin-1",
                    kickoff_reference_artifacts=[
                        "./shared_workspace/sprints/260405-Sprint-21-17/attachments/1490324395179380827_KIS_API_.md"
                    ],
                )
                write_json(service.paths.sprint_file("260405-Sprint-21:18"), sprint_state)
                service._save_scheduler_state(
                    {
                        "active_sprint_id": "260405-Sprint-21:18",
                        "last_started_at": "",
                        "last_completed_at": "",
                        "next_slot_at": "",
                        "deferred_slot_at": "",
                        "last_trigger": "manual_start",
                    }
                )

                repaired = service._load_sprint_state("260405-Sprint-21:18")

                relocated_path = (
                    Path(tmpdir)
                    / "shared_workspace"
                    / "sprints"
                    / "260405-Sprint-21-18"
                    / "attachments"
                    / "1490324395179380827_KIS_API_.md"
                )
                expected_hint = "./shared_workspace/sprints/260405-Sprint-21-18/attachments/1490324395179380827_KIS_API_.md"

                self.assertFalse(staged_path.exists())
                self.assertTrue(relocated_path.exists())
                self.assertEqual(repaired["kickoff_reference_artifacts"], [expected_hint])
                self.assertEqual(repaired["reference_artifacts"], [expected_hint])
                current_sprint_text = service.paths.current_sprint_file.read_text(encoding="utf-8")
                self.assertIn(expected_hint, current_sprint_text)

    def test_manual_daily_sprint_wraps_up_when_no_executable_todo_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('  start_mode: "auto"\n', '  start_mode: "manual_daily"\n', 1)
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                )
                sprint_state["phase"] = "ongoing"
                sprint_state["status"] = "running"
                sprint_state["last_planner_review_at"] = datetime.now(timezone.utc).isoformat()
                service._save_sprint_state(sprint_state)
                service._run_ongoing_sprint_review = AsyncMock(return_value=None)
                service._finalize_sprint = AsyncMock(return_value=None)

                asyncio.run(service._continue_sprint(sprint_state, announce=False))

                service._finalize_sprint.assert_awaited_once_with(sprint_state)
                self.assertEqual(sprint_state["phase"], "wrap_up")
                self.assertEqual(sprint_state["status"], "running")
