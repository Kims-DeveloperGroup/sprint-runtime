from orchestration_test_utils import *


class TeamsRuntimeOrchestrationCloseoutReportingTests(OrchestrationTestCase):
    def test_apply_sprint_planning_result_revises_milestone_and_builds_prioritized_queue(self):
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
                service._save_sprint_state(sprint_state)

                request_record = {
                    "request_id": "planning-1",
                    "intent": "plan",
                    "scope": "initial sprint planning",
                    "body": "workflow initial",
                }
                result = {
                    "role": "planner",
                    "status": "completed",
                    "summary": "초기 phase용 plan/spec과 prioritized todo를 정리했습니다.",
                    "insights": ["phase 진입 조건을 분리합니다.", "folder 기반 문서를 유지합니다."],
                    "artifacts": [],
                    "proposals": {
                        "revised_milestone_title": "workflow refined",
                        "backlog_items": [
                            {
                                "title": "manual sprint start gate",
                                "summary": "milestone 없이는 sprint를 시작하지 않도록 정리",
                                "kind": "feature",
                                "scope": "manual sprint start gate",
                                "priority_rank": 2,
                                "milestone_title": "conflicting milestone",
                            },
                            {
                                "title": "sprint folder artifact rendering",
                                "summary": "sprint folder living docs를 렌더링",
                                "kind": "enhancement",
                                "scope": "sprint folder artifact rendering",
                                "priority_rank": 1,
                                "milestone_title": "other milestone",
                            },
                        ],
                    },
                }
                merge_backlog_payload(
                    workspace_root=tmpdir,
                    payload={
                        "backlog_items": [
                            {
                                "title": "manual sprint start gate",
                                "summary": "milestone 없이는 sprint를 시작하지 않도록 정리",
                                "kind": "feature",
                                "scope": "manual sprint start gate",
                                "priority_rank": 2,
                                "milestone_title": "workflow refined",
                                "planned_in_sprint_id": sprint_state["sprint_id"],
                            },
                            {
                                "title": "sprint folder artifact rendering",
                                "summary": "sprint folder living docs를 렌더링",
                                "kind": "enhancement",
                                "scope": "sprint folder artifact rendering",
                                "priority_rank": 1,
                                "milestone_title": "workflow refined",
                                "planned_in_sprint_id": sprint_state["sprint_id"],
                            },
                        ]
                    },
                    default_source="planner",
                    source_request_id="planning-1",
                )

                ready = service._apply_sprint_planning_result(
                    sprint_state,
                    phase="initial",
                    request_record=request_record,
                    result=result,
                )
                service._save_sprint_state(sprint_state)

                self.assertTrue(ready)
                self.assertEqual(sprint_state["milestone_title"], "workflow refined")
                self.assertEqual(
                    [item["title"] for item in sprint_state["selected_items"]],
                    ["sprint folder artifact rendering", "manual sprint start gate"],
                )
                self.assertEqual(
                    {item["milestone_title"] for item in sprint_state["selected_items"]},
                    {"workflow refined"},
                )
                self.assertEqual(
                    [todo["priority_rank"] for todo in sprint_state["todos"][:2]],
                    [1, 2],
                )
                self.assertEqual(
                    {todo["milestone_title"] for todo in sprint_state["todos"][:2]},
                    {"workflow refined"},
                )
                self.assertEqual(
                    Path(sprint_state["sprint_folder"]).name,
                    build_sprint_artifact_folder_name(sprint_state["sprint_id"]),
                )
                todo_backlog_text = service._sprint_artifact_paths(sprint_state)["todo_backlog"].read_text(encoding="utf-8")
                self.assertIn("priority_rank: 2", todo_backlog_text)
                self.assertIn("manual sprint start gate", todo_backlog_text)
                persisted_backlog = service._iter_backlog_items()
                self.assertEqual(
                    {str(item.get("milestone_title") or "") for item in persisted_backlog},
                    {"workflow refined"},
                )

    def test_sprint_spec_markdown_promotes_role_reports_into_canonical_sections(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="canonical spec",
                    trigger="manual_start",
                )
                sprint_state["planning_iterations"] = [
                    {
                        "created_at": "2026-04-07T00:00:00+09:00",
                        "phase": "ongoing_review",
                        "request_id": "planning-sync-1",
                        "summary": "planner summary",
                        "insights": ["latest planner insight"],
                        "artifacts": [],
                        "phase_ready": True,
                    }
                ]
                sprint_state["todos"] = [
                    {
                        "todo_id": "todo-1",
                        "backlog_id": "backlog-1",
                        "title": "전략 2 데이터 계약",
                        "status": "blocked",
                        "request_id": "req-canonical-1",
                    }
                ]
                service._save_request(
                    {
                        "request_id": "req-canonical-1",
                        "status": "blocked",
                        "scope": "전략 2 bootstrap·stream·fill 데이터 계약 정의",
                        "events": [
                            {
                                "timestamp": "2026-04-07T00:10:00+09:00",
                                "type": "role_report",
                                "actor": "planner",
                                "summary": "shared spec에 데이터 계약 초안을 반영했습니다.",
                                "payload": {
                                    "request_id": "req-canonical-1",
                                    "role": "planner",
                                    "status": "completed",
                                    "summary": "shared spec에 데이터 계약 초안을 반영했습니다.",
                                    "insights": ["planner insight"],
                                    "proposals": {
                                        "planning_note": {
                                            "summary": "bootstrap/tick/fill contract를 문서에 반영",
                                            "contract_points": [
                                                "session_venue required",
                                                "trade_side optional",
                                            ],
                                        },
                                        "workflow_transition": {
                                            "outcome": "advance",
                                            "reason": "developer 구현으로 진행",
                                            "unresolved_items": ["reducer ownership split 확인"],
                                        },
                                    },
                                    "artifacts": ["./shared_workspace/sprints/demo/spec.md"],
                                    "error": "",
                                },
                            },
                            {
                                "timestamp": "2026-04-07T00:20:00+09:00",
                                "type": "role_report",
                                "actor": "qa",
                                "summary": "문서-우선 완료로 보기 어렵습니다.",
                                "payload": {
                                    "request_id": "req-canonical-1",
                                    "role": "qa",
                                    "status": "blocked",
                                    "summary": "문서-우선 완료로 보기 어렵습니다.",
                                    "insights": ["shared spec 본문이 최신 계약을 닫지 못했습니다."],
                                    "proposals": {
                                        "workflow_transition": {
                                            "outcome": "reopen",
                                            "reopen_category": "verification",
                                            "reason": "shared spec canonical 본문 보강 필요",
                                            "unresolved_items": [
                                                "spec.md에 session_venue/trade_side 정책 반영",
                                                "iteration_log.md에 QA 검증 흐름 반영",
                                            ],
                                        }
                                    },
                                    "artifacts": ["./shared_workspace/sprints/demo/iteration_log.md"],
                                    "error": "",
                                },
                            },
                        ],
                    }
                )

                rendered = service._render_sprint_spec_markdown(sprint_state)

                self.assertIn("## Canonical Contract Body", rendered)
                self.assertIn("전략 2 데이터 계약", rendered)
                self.assertIn("session_venue required", rendered)
                self.assertIn("trade_side optional", rendered)
                self.assertIn("shared spec canonical 본문 보강 필요", rendered)
                self.assertIn("iteration_log.md에 QA 검증 흐름 반영", rendered)

    def test_sprint_iteration_log_includes_workflow_validation_trace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="validation trace",
                    trigger="manual_start",
                )
                sprint_state["planning_iterations"] = [
                    {
                        "created_at": "2026-04-07T00:00:00+09:00",
                        "phase": "initial",
                        "request_id": "planning-sync-1",
                        "summary": "planner sync",
                        "insights": ["plan/spec synced"],
                        "artifacts": ["./shared_workspace/sprints/demo/spec.md"],
                        "phase_ready": False,
                    }
                ]
                sprint_state["todos"] = [
                    {
                        "todo_id": "todo-2",
                        "backlog_id": "backlog-2",
                        "title": "runtime guardrail",
                        "status": "blocked",
                        "request_id": "req-trace-1",
                    }
                ]
                service._save_request(
                    {
                        "request_id": "req-trace-1",
                        "status": "blocked",
                        "scope": "runtime guardrail 규칙 정의",
                        "events": [
                            {
                                "timestamp": "2026-04-07T01:00:00+09:00",
                                "type": "delegated",
                                "actor": "orchestrator",
                                "summary": "planner 역할로 위임했습니다.",
                                "payload": {"routing_context": {"selected_role": "planner", "reason": "planning owner"}},
                            },
                            {
                                "timestamp": "2026-04-07T01:10:00+09:00",
                                "type": "role_report",
                                "actor": "developer",
                                "summary": "runtime guardrail 구현을 완료했습니다.",
                                "payload": {
                                    "request_id": "req-trace-1",
                                    "role": "developer",
                                    "status": "completed",
                                    "summary": "runtime guardrail 구현을 완료했습니다.",
                                    "insights": ["execution truth와 degraded-entry를 코드로 고정했습니다."],
                                    "proposals": {
                                        "validation": {
                                            "passed": ["python -m unittest tests.test_runtime_guardrails"],
                                            "follow_up": ["runtime wiring 검증"],
                                        },
                                        "workflow_transition": {
                                            "outcome": "advance",
                                            "target_step": "architect_review",
                                            "reason": "architect 재검토 필요",
                                            "unresolved_items": ["QPS 상수 검토"],
                                        },
                                    },
                                    "artifacts": ["./workspace/tests/test_runtime_guardrails.py"],
                                    "error": "",
                                },
                            },
                            {
                                "timestamp": "2026-04-07T01:20:00+09:00",
                                "type": "role_report",
                                "actor": "qa",
                                "summary": "shared spec 본문 보강이 필요합니다.",
                                "payload": {
                                    "request_id": "req-trace-1",
                                    "role": "qa",
                                    "status": "blocked",
                                    "summary": "shared spec 본문 보강이 필요합니다.",
                                    "insights": ["iteration artifact가 검증 흐름을 닫지 못했습니다."],
                                    "proposals": {
                                        "workflow_transition": {
                                            "outcome": "reopen",
                                            "reopen_category": "verification",
                                            "reason": "shared iteration artifact 보강 필요",
                                            "unresolved_items": ["developer 구현과 architect 통과 흐름 반영"],
                                        }
                                    },
                                    "artifacts": ["./shared_workspace/sprints/demo/iteration_log.md"],
                                    "error": "",
                                },
                            },
                        ],
                    }
                )

                rendered = service._render_sprint_iteration_log_markdown(sprint_state)

                self.assertIn("## Workflow Validation Trace", rendered)
                self.assertIn("planner 역할로 위임했습니다.", rendered)
                self.assertIn("2026-04-07T01:10:00+09:00 | developer | role_report", rendered)
                self.assertIn("python -m unittest tests.test_runtime_guardrails", rendered)
                self.assertIn("shared iteration artifact 보강 필요", rendered)
                self.assertIn("developer 구현과 architect 통과 흐름 반영", rendered)

    def test_finalize_sprint_blocks_when_canonical_doc_sections_are_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="doc closeout guard",
                    trigger="manual_start",
                )
                sprint_state["git_baseline"] = {"repo_root": "/repo", "head_sha": "base123", "dirty_paths": []}

                with (
                    patch("teams_runtime.core.orchestration.inspect_sprint_closeout") as inspect_mock,
                    patch.object(service, "_draft_sprint_report_via_planner", AsyncMock(return_value={})),
                ):
                    asyncio.run(service._finalize_sprint(sprint_state))

                updated = read_json(service.paths.sprint_file(sprint_state["sprint_id"]))
                self.assertEqual(updated["status"], "failed")
                self.assertEqual(updated["closeout_status"], "planning_incomplete")
                self.assertIn("canonical 계약 본문", updated["report_body"])
                inspect_mock.assert_not_called()

    def test_active_sprint_artifact_entrypoints_link_todo_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('  start_mode: "auto"\n', '  start_mode: "manual_daily"\n', 1)
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="artifact linkage",
                    trigger="manual_start",
                )
                sprint_state["phase"] = "ongoing"
                sprint_state["status"] = "running"
                todo = {
                    "todo_id": "todo-artifact-linkage",
                    "backlog_id": "backlog-artifact-linkage",
                    "title": "KIS 활용률 검증 및 fallback 축소 기준 정리",
                    "milestone_title": sprint_state["milestone_title"],
                    "priority_rank": 1,
                    "owner_role": "planner",
                    "status": "blocked",
                    "request_id": "20260403-artifact-linkage",
                    "summary": "verification 문서를 sprint entrypoint에서 다시 찾을 수 있어야 합니다.",
                    "artifacts": [
                        f"./shared_workspace/sprints/{sprint_state['sprint_folder_name']}/kis_adoption_verification.md"
                    ],
                    "started_at": "",
                    "ended_at": "",
                    "carry_over_backlog_id": "backlog-artifact-linkage",
                }
                sprint_state["todos"] = [todo]

                service._save_sprint_state(sprint_state)

                artifact_paths = service._sprint_artifact_paths(sprint_state)
                index_text = artifact_paths["index"].read_text(encoding="utf-8")
                report_text = artifact_paths["report"].read_text(encoding="utf-8")

                self.assertIn("- kis_adoption_verification.md", index_text)
                self.assertIn("## Linked Todo Artifacts", index_text)
                self.assertIn("artifact=kis_adoption_verification.md", index_text)
                self.assertNotIn("report not generated yet", report_text)
                self.assertIn("## Linked Todo Artifacts", report_text)
                self.assertIn("artifact=kis_adoption_verification.md", report_text)

    def test_closeout_sprint_report_body_links_todo_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('  start_mode: "auto"\n', '  start_mode: "manual_daily"\n', 1)
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="artifact linkage closeout",
                    trigger="manual_start",
                )
                sprint_state["phase"] = "wrap_up"
                sprint_state["status"] = "completed"
                sprint_state["todos"] = [
                    {
                        "todo_id": "todo-closeout-artifact-linkage",
                        "backlog_id": "backlog-closeout-artifact-linkage",
                        "title": "KIS 활용률 검증 및 fallback 축소 기준 정리",
                        "milestone_title": sprint_state["milestone_title"],
                        "priority_rank": 1,
                        "owner_role": "planner",
                        "status": "blocked",
                        "request_id": "20260403-closeout-artifact-linkage",
                        "summary": "verification 문서를 closeout report에서도 다시 찾을 수 있어야 합니다.",
                        "artifacts": [
                            str(service._sprint_artifact_paths(sprint_state)["root"] / "kis_adoption_verification.md")
                        ],
                        "started_at": "",
                        "ended_at": "",
                        "carry_over_backlog_id": "backlog-closeout-artifact-linkage",
                    }
                ]

                report_body = service._build_sprint_report_body(
                    sprint_state,
                    {
                        "status": "warning_missing_sprint_tag",
                        "message": "closeout generated",
                    },
                )

                self.assertIn("## 한눈에 보기", report_body)
                self.assertIn(f"- sprint_id: {sprint_state['sprint_id']}", report_body)
                self.assertIn("## 변경 요약", report_body)
                self.assertIn("## Sprint A to Z", report_body)
                self.assertIn("## 에이전트 기여", report_body)
                self.assertIn("## 핵심 이슈", report_body)
                self.assertIn("## 성과", report_body)
                self.assertIn("## 참고 아티팩트", report_body)
                self.assertIn("## 머신 요약", report_body)
                self.assertIn("실제로 완료/커밋된 delivered change는 없었습니다.", report_body)
                self.assertIn("- 어떻게:\n  - closeout 정리: closeout generated", report_body)
                self.assertIn("linked_artifacts:", report_body)
                self.assertIn("artifact=kis_adoption_verification.md", report_body)
                self.assertIn("closeout_message=closeout generated", report_body)

    def test_live_sprint_report_body_surfaces_next_actions_before_full_todo_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('  start_mode: "auto"\n', '  start_mode: "manual_daily"\n', 1)
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="live report emphasis",
                    trigger="manual_start",
                )
                sprint_state["phase"] = "ongoing"
                sprint_state["status"] = "running"
                sprint_state["todos"] = [
                    {
                        "todo_id": "todo-live-blocked",
                        "backlog_id": "backlog-live-blocked",
                        "title": "KIS 활용률 검증 및 fallback 축소 기준 정리",
                        "milestone_title": sprint_state["milestone_title"],
                        "priority_rank": 2,
                        "owner_role": "planner",
                        "status": "blocked",
                        "request_id": "req-live-blocked",
                        "summary": "verification 문서를 sprint entrypoint에서 다시 찾을 수 있어야 합니다.",
                        "artifacts": [
                            f"./shared_workspace/sprints/{sprint_state['sprint_folder_name']}/kis_adoption_verification.md"
                        ],
                        "started_at": "",
                        "ended_at": "",
                        "carry_over_backlog_id": "backlog-live-blocked",
                    },
                    {
                        "todo_id": "todo-live-queued",
                        "backlog_id": "backlog-live-queued",
                        "title": "데일리 요약 카드 재배치",
                        "milestone_title": sprint_state["milestone_title"],
                        "priority_rank": 3,
                        "owner_role": "designer",
                        "status": "queued",
                        "request_id": "req-live-queued",
                        "summary": "핵심 정보 카드 위치를 정리합니다.",
                        "artifacts": [],
                        "started_at": "",
                        "ended_at": "",
                        "carry_over_backlog_id": "",
                    },
                    {
                        "todo_id": "todo-live-running",
                        "backlog_id": "backlog-live-running",
                        "title": "실시간 상태 반영 순서 재정리",
                        "milestone_title": sprint_state["milestone_title"],
                        "priority_rank": 1,
                        "owner_role": "developer",
                        "status": "running",
                        "request_id": "req-live-running",
                        "summary": "상단 요약 블록을 먼저 노출합니다.",
                        "artifacts": [],
                        "started_at": "",
                        "ended_at": "",
                        "carry_over_backlog_id": "",
                    },
                    {
                        "todo_id": "todo-live-committed",
                        "backlog_id": "backlog-live-committed",
                        "title": "완료된 기록 보강",
                        "milestone_title": sprint_state["milestone_title"],
                        "priority_rank": 4,
                        "owner_role": "developer",
                        "status": "committed",
                        "request_id": "req-live-committed",
                        "summary": "이미 반영된 기록입니다.",
                        "artifacts": [],
                        "started_at": "",
                        "ended_at": "",
                        "carry_over_backlog_id": "",
                    },
                ]

                report_body = service._render_live_sprint_report_markdown(sprint_state)
                next_action_section = report_body.split("## 다음 액션", 1)[1].split("## Todo Summary", 1)[0]

                self.assertIn("## 한눈에 보기", report_body)
                self.assertIn("## 다음 액션", report_body)
                self.assertIn("## Todo Summary", report_body)
                self.assertLess(report_body.index("## 한눈에 보기"), report_body.index("## 다음 액션"))
                self.assertLess(report_body.index("## 다음 액션"), report_body.index("## Todo Summary"))
                self.assertIn("- TL;DR: live report emphasis 스프린트가 진행중 상태입니다.", report_body)
                self.assertIn("- todo 요약: running:1, queued:1, committed:1, blocked:1", report_body)
                self.assertIn("- todo_summary: running:1, queued:1, committed:1, blocked:1", report_body)
                self.assertIn("- [running] 실시간 상태 반영 순서 재정리 | request_id=req-live-running", next_action_section)
                self.assertIn("- [queued] 데일리 요약 카드 재배치 | request_id=req-live-queued", next_action_section)
                self.assertIn("- [blocked] KIS 활용률 검증 및 fallback 축소 기준 정리 | request_id=req-live-blocked", next_action_section)
                self.assertNotIn("완료된 기록 보강", next_action_section)
                self.assertLess(
                    next_action_section.index("- [running] 실시간 상태 반영 순서 재정리 | request_id=req-live-running"),
                    next_action_section.index("- [queued] 데일리 요약 카드 재배치 | request_id=req-live-queued"),
                )
                self.assertLess(
                    next_action_section.index("- [queued] 데일리 요약 카드 재배치 | request_id=req-live-queued"),
                    next_action_section.index("- [blocked] KIS 활용률 검증 및 fallback 축소 기준 정리 | request_id=req-live-blocked"),
                )
                self.assertIn("artifact=kis_adoption_verification.md", report_body)

    def test_closeout_sprint_report_body_links_workspace_relative_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workspace artifact linkage closeout",
                    trigger="manual_start",
                )
                sprint_state["phase"] = "wrap_up"
                sprint_state["status"] = "completed"
                sprint_state["todos"] = [
                    {
                        "todo_id": "todo-workspace-artifact-linkage-1",
                        "backlog_id": "backlog-workspace-artifact-linkage-1",
                        "title": "김단타 진입 기준 재구성",
                        "milestone_title": sprint_state["milestone_title"],
                        "priority_rank": 2,
                        "owner_role": "developer",
                        "status": "committed",
                        "request_id": "20260403-workspace-artifact-linkage-1",
                        "summary": "김단타 진입 기준을 다시 정리했습니다.",
                        "artifacts": [
                            "workspace/libs/kis/domestic_stock_ws.py",
                            "libs/kis/_official_domestic_stock_ws.py",
                        ],
                        "started_at": "",
                        "ended_at": "",
                        "carry_over_backlog_id": "",
                    },
                    {
                        "todo_id": "todo-workspace-artifact-linkage-2",
                        "backlog_id": "backlog-workspace-artifact-linkage-2",
                        "title": "김단타 보고 근거 재구성",
                        "milestone_title": sprint_state["milestone_title"],
                        "priority_rank": 1,
                        "owner_role": "developer",
                        "status": "committed",
                        "request_id": "20260403-workspace-artifact-linkage-2",
                        "summary": "김단타 보고 근거를 정리했습니다.",
                        "artifacts": [
                            "workspace/libs/kis/domestic_stock_ws.py",
                            "tests/test_kis_client.py",
                        ],
                        "started_at": "",
                        "ended_at": "",
                        "carry_over_backlog_id": "",
                    },
                ]
                service._save_request(
                    {
                        "request_id": "20260403-workspace-artifact-linkage-1",
                        "status": "committed",
                        "intent": "route",
                        "urgency": "normal",
                        "scope": "김단타 진입 판단 기준 조정",
                        "body": "김단타의 재진입 판단 기준을 조정합니다.",
                        "artifacts": [
                            "workspace/libs/kis/domestic_stock_ws.py",
                            "libs/kis/_official_domestic_stock_ws.py",
                        ],
                        "params": {},
                        "current_role": "developer",
                        "next_role": "",
                        "owner_role": "developer",
                        "sprint_id": sprint_state["sprint_id"],
                        "created_at": "2026-04-03T15:20:00+09:00",
                        "updated_at": "2026-04-03T15:24:00+09:00",
                        "fingerprint": "20260403-workspace-artifact-linkage-1",
                        "reply_route": {},
                        "events": [],
                        "result": {
                            "request_id": "20260403-workspace-artifact-linkage-1",
                            "role": "developer",
                            "status": "committed",
                            "summary": "거래량과 프로그램 순매수가 동시에 붙는 구간에서만 재진입하도록 바뀌었습니다.",
                            "insights": [
                                "김단타는 급등 추격보다 거래대금이 붙는 구간을 우선 확인합니다.",
                                "entry rule은 실시간 순매수와 캔들 맥락을 함께 본 뒤 판단합니다.",
                            ],
                            "proposals": {},
                            "artifacts": [
                                "workspace/libs/kis/domestic_stock_ws.py",
                                "libs/kis/_official_domestic_stock_ws.py",
                            ],
                            "next_role": "",
                            "error": "",
                        },
                        "version_control_status": "committed",
                        "version_control_sha": "kimdanta-entry-1",
                        "version_control_paths": [
                            "workspace/libs/kis/domestic_stock_ws.py",
                            "libs/kis/_official_domestic_stock_ws.py",
                        ],
                        "version_control_message": "entry_signal_policy_v2.py: tighten kimdanta re-entry rule",
                        "version_control_error": "",
                        "task_commit_status": "committed",
                        "task_commit_sha": "kimdanta-entry-1",
                        "task_commit_paths": [
                            "workspace/libs/kis/domestic_stock_ws.py",
                            "libs/kis/_official_domestic_stock_ws.py",
                        ],
                        "task_commit_message": "entry_signal_policy_v2.py: tighten kimdanta re-entry rule",
                        "visited_roles": ["planner", "developer"],
                        "task_commit_summary": "거래량과 프로그램 순매수가 동시에 붙는 구간에서만 재진입하도록 바뀌었습니다.",
                    }
                )
                service._save_request(
                    {
                        "request_id": "20260403-workspace-artifact-linkage-2",
                        "status": "committed",
                        "intent": "route",
                        "urgency": "normal",
                        "scope": "김단타 보고 메시지 재구성",
                        "body": "김단타의 보고 메시지에 판단 근거와 의미를 함께 보여 줍니다.",
                        "artifacts": [
                            "workspace/libs/kis/domestic_stock_ws.py",
                            "tests/test_kis_client.py",
                        ],
                        "params": {},
                        "current_role": "developer",
                        "next_role": "",
                        "owner_role": "developer",
                        "sprint_id": sprint_state["sprint_id"],
                        "created_at": "2026-04-03T15:26:00+09:00",
                        "updated_at": "2026-04-03T15:31:00+09:00",
                        "fingerprint": "20260403-workspace-artifact-linkage-2",
                        "reply_route": {},
                        "events": [],
                        "result": {
                            "request_id": "20260403-workspace-artifact-linkage-2",
                            "role": "developer",
                            "status": "committed",
                            "summary": "김단타 보고 메시지가 이제 판단 근거와 변화 의미를 함께 설명하도록 바뀌었습니다.",
                            "insights": [
                                "사용자는 왜 HOLD 또는 BUY가 나왔는지 보고서만 보고 바로 이해할 수 있습니다."
                            ],
                            "proposals": {},
                            "artifacts": [
                                "workspace/libs/kis/domestic_stock_ws.py",
                                "tests/test_kis_client.py",
                            ],
                            "next_role": "",
                            "error": "",
                        },
                        "version_control_status": "committed",
                        "version_control_sha": "kimdanta-report-2",
                        "version_control_paths": [
                            "workspace/libs/kis/domestic_stock_ws.py",
                            "tests/test_kis_client.py",
                        ],
                        "version_control_message": "domestic_stock_ws.py: explain kimdanta report reasoning",
                        "version_control_error": "",
                        "task_commit_status": "committed",
                        "task_commit_sha": "kimdanta-report-2",
                        "task_commit_paths": [
                            "workspace/libs/kis/domestic_stock_ws.py",
                            "tests/test_kis_client.py",
                        ],
                        "task_commit_message": "domestic_stock_ws.py: explain kimdanta report reasoning",
                        "visited_roles": ["planner", "developer"],
                        "task_commit_summary": "김단타 보고 메시지가 이제 판단 근거와 변화 의미를 함께 설명하도록 바뀌었습니다.",
                    }
                )

                report_body = service._build_sprint_report_body(
                    sprint_state,
                    {
                        "status": "verified",
                        "message": "workspace artifact coverage",
                    },
                )

                self.assertIn("## 변경 요약", report_body)
                self.assertIn("### 김단타 진입 기준 재구성", report_body)
                self.assertIn(
                    "- 무엇이 달라졌나: 이제 김단타는 거래량과 프로그램 순매수가 동시에 붙는 구간에서만 재진입하도록 바뀌었습니다.",
                    report_body,
                )
                self.assertIn(
                    "- 의미: 사용자 입장에서는 이제 김단타가 언제 어떤 판단을 내리는지 기준이 더 분명해진다는 의미입니다.",
                    report_body,
                )
                self.assertIn("- 어떻게:", report_body)
                self.assertIn(
                    "  - 핵심 로직: 김단타는 급등 추격보다 거래대금이 붙는 구간을 우선 확인합니다. / entry rule은 실시간 순매수와 캔들 맥락을 함께 본 뒤 판단합니다.",
                    report_body,
                )
                self.assertIn(
                    "  - 구현 근거 아티팩트: libs/kis/domestic_stock_ws.py, libs/kis/_official_domestic_stock_ws.py",
                    report_body,
                )
                self.assertIn("  - 작업 범위: 김단타 진입 판단 기준 조정", report_body)
                self.assertIn(
                    "  - 참고 아티팩트: libs/kis/domestic_stock_ws.py, libs/kis/_official_domestic_stock_ws.py",
                    report_body,
                )
                self.assertNotIn("- 어떻게: 핵심 로직은", report_body)
                self.assertIn("- 개발자 (developer): todo 2건, 완료 2건.", report_body)
                self.assertIn(
                    "  - 근거 하이라이트: 김단타 진입 기준 재구성, 김단타 보고 근거 재구성 작업을 담당했습니다.",
                    report_body,
                )
                self.assertIn(
                    "  - 참고 산출물: libs/kis/domestic_stock_ws.py, libs/kis/_official_domestic_stock_ws.py, tests/test_kis_client.py",
                    report_body,
                )
                self.assertIn("## 참고 아티팩트", report_body)
                self.assertIn(
                    "- 참고: [committed] 김단타 진입 기준 재구성 -> libs/kis/domestic_stock_ws.py",
                    report_body,
                )
                self.assertIn(
                    "libs/kis/domestic_stock_ws.py",
                    report_body,
                )
                self.assertIn(
                    "artifact=libs/kis/domestic_stock_ws.py",
                    report_body,
                )
                self.assertIn("artifact=libs/kis/_official_domestic_stock_ws.py", report_body)
                self.assertIn("artifact=libs/kis/domestic_stock_ws.py", report_body)
                self.assertIn("artifact=tests/test_kis_client.py", report_body)
                self.assertNotIn("... 외", report_body)
                self.assertNotIn("외 1건", report_body)

    def test_closeout_sprint_report_body_prefers_semantic_change_over_meta_commit_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="designer advisory contract",
                    trigger="manual_start",
                )
                sprint_state["phase"] = "wrap_up"
                sprint_state["status"] = "completed"
                sprint_state["todos"] = [
                    {
                        "todo_id": "todo-designer-advisory-contract",
                        "backlog_id": "backlog-designer-advisory-contract",
                        "title": "designer advisory 계약 정리",
                        "milestone_title": sprint_state["milestone_title"],
                        "priority_rank": 1,
                        "owner_role": "planner",
                        "status": "committed",
                        "request_id": "20260411-designer-advisory-contract",
                        "summary": "planner finalization 전에 designer advisory contract를 고정합니다.",
                        "artifacts": ["shared_workspace/sprints/demo/spec.md"],
                        "started_at": "",
                        "ended_at": "",
                        "carry_over_backlog_id": "",
                    }
                ]
                service._save_request(
                    {
                        "request_id": "20260411-designer-advisory-contract",
                        "status": "committed",
                        "intent": "plan",
                        "urgency": "normal",
                        "scope": "designer advisory contract",
                        "body": "designer advisory contract",
                        "artifacts": ["shared_workspace/sprints/demo/spec.md"],
                        "params": {},
                        "current_role": "planner",
                        "next_role": "",
                        "owner_role": "planner",
                        "sprint_id": sprint_state["sprint_id"],
                        "created_at": "2026-04-11T12:00:00+09:00",
                        "updated_at": "2026-04-11T12:05:00+09:00",
                        "fingerprint": "20260411-designer-advisory-contract",
                        "reply_route": {},
                        "events": [
                            {
                                "timestamp": "2026-04-11T12:01:00+09:00",
                                "type": "role_report",
                                "actor": "designer",
                                "summary": "designer advisory contract를 구체화했습니다.",
                                "payload": {
                                    "request_id": "20260411-designer-advisory-contract",
                                    "role": "designer",
                                    "status": "completed",
                                    "summary": "designer advisory contract를 구체화했습니다.",
                                    "insights": [],
                                    "proposals": {
                                        "design_feedback": {
                                            "rules": [
                                                "designer는 planning advisory만 수행하고 직접 execution을 열지 않습니다."
                                            ]
                                        },
                                        "workflow_transition": {
                                            "outcome": "advance",
                                            "target_phase": "planning",
                                            "target_step": "planner_finalize",
                                            "reason": "planner가 designer advisory를 반영한 뒤 planning finalization으로 닫을 수 있습니다.",
                                            "unresolved_items": [],
                                            "finalize_phase": False,
                                        },
                                    },
                                    "artifacts": ["shared_workspace/sprints/demo/spec.md"],
                                    "error": "",
                                },
                            }
                        ],
                        "result": {
                            "request_id": "20260411-designer-advisory-contract",
                            "role": "planner",
                            "status": "committed",
                            "summary": "designer advisory 계약이 prompt·문서·라우팅·회귀 테스트에 일관되게 반영된 것을 확인했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["shared_workspace/sprints/demo/spec.md"],
                            "next_role": "",
                            "error": "",
                        },
                        "version_control_status": "committed",
                        "version_control_sha": "designer-advisory-1",
                        "version_control_paths": ["shared_workspace/sprints/demo/spec.md"],
                        "version_control_message": "spec.md: restrict designer advisory flow",
                        "version_control_error": "",
                        "task_commit_status": "committed",
                        "task_commit_sha": "designer-advisory-1",
                        "task_commit_paths": ["shared_workspace/sprints/demo/spec.md"],
                        "task_commit_message": "spec.md: restrict designer advisory flow",
                        "visited_roles": ["planner", "designer", "planner"],
                        "task_commit_summary": "designer advisory 계약이 prompt·문서·라우팅·회귀 테스트에 일관되게 반영된 것을 확인했습니다.",
                    }
                )

                report_body = service._build_sprint_report_body(
                    sprint_state,
                    {
                        "status": "verified",
                        "message": "closeout generated",
                    },
                )

                self.assertIn(
                    "- 무엇이 달라졌나: designer는 planning advisory만 수행하고 직접 execution을 열지 않습니다.",
                    report_body,
                )
                self.assertIn(
                    "- 의미: planner가 designer advisory를 반영한 뒤 planning finalization으로 닫을 수 있습니다.",
                    report_body,
                )
                self.assertNotIn("prompt·문서·라우팅·회귀 테스트", report_body)

    def test_prepare_sprint_report_body_uses_planner_report_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="closeout planner draft",
                    trigger="manual_start",
                )
                sprint_state["phase"] = "wrap_up"
                sprint_state["status"] = "completed"
                closeout_result = {
                    "status": "verified",
                    "message": "closeout generated",
                    "commit_count": 0,
                    "commit_shas": [],
                    "representative_commit_sha": "",
                    "uncommitted_paths": [],
                }
                planner_draft = {
                    "headline": "designer advisory 계약 변경으로 planning closeout 기준이 더 분명해졌습니다.",
                    "changes": [
                        {
                            "title": "designer advisory를 planner finalization 전용으로 제한",
                            "why": "planning ownership을 planner로 고정하기 위한 스프린트였습니다.",
                            "what_changed": "designer는 planning advisory만 수행하고 직접 execution을 열지 않도록 바뀌었습니다.",
                            "meaning": "이번 스프린트 기준으로 planner가 advisory 반영 후 planning을 닫는 흐름이 더 엄격해졌습니다.",
                            "how": "workflow contract, role prompt, 회귀 테스트를 함께 검토해 closeout 의미를 정리했습니다.",
                            "artifacts": ["shared_workspace/sprints/demo/spec.md"],
                        }
                    ],
                    "timeline": ["manual_start로 스프린트를 열고 closeout evidence를 검토했습니다."],
                    "agent_contributions": [{"role": "planner", "summary": "persisted sprint evidence를 읽고 closeout draft를 작성했습니다."}],
                    "issues": ["핵심 blocker 없이 closeout을 마쳤습니다."],
                    "achievements": ["planner draft가 canonical report 형식에 맞춰 반영됐습니다."],
                    "highlight_artifacts": ["shared_workspace/sprints/demo/spec.md"],
                }

                with patch.object(service, "_draft_sprint_report_via_planner", AsyncMock(return_value=planner_draft)):
                    report_body = asyncio.run(service._prepare_sprint_report_body(sprint_state, closeout_result))

                self.assertEqual(sprint_state["planner_report_draft"], planner_draft)
                self.assertIn(planner_draft["headline"], report_body)
                self.assertIn("### designer advisory를 planner finalization 전용으로 제한", report_body)
                self.assertIn(
                    "- 무엇이 달라졌나: designer는 planning advisory만 수행하고 직접 execution을 열지 않도록 바뀌었습니다.",
                    report_body,
                )
                self.assertIn(
                    "- 의미: 이번 스프린트 기준으로 planner가 advisory 반영 후 planning을 닫는 흐름이 더 엄격해졌습니다.",
                    report_body,
                )

    def test_planner_closeout_request_id_slugifies_sprint_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="closeout planner draft",
                    trigger="manual_start",
                )
                sprint_state["sprint_id"] = "260411-Sprint-23:44"

                self.assertEqual(
                    service._planner_closeout_request_id(sprint_state),
                    "planner-closeout-report-260411-sprint-23-44",
                )

    def test_draft_sprint_report_via_planner_builds_closeout_request_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="closeout planner draft",
                    trigger="manual_start",
                )
                sprint_state["sprint_id"] = "260411-Sprint-23:44"
                sprint_state["phase"] = "wrap_up"
                sprint_state["status"] = "completed"
                todo_request_id = "20260411-closeout-todo"
                sprint_state["todos"] = [
                    {
                        "todo_id": "todo-closeout-001",
                        "request_id": todo_request_id,
                    }
                ]
                closeout_result = {
                    "status": "verified",
                    "message": "closeout generated",
                    "commit_count": 0,
                    "commit_shas": [],
                    "representative_commit_sha": "",
                    "uncommitted_paths": [],
                }
                expected_request_id = "planner-closeout-report-260411-sprint-23-44"
                planner_draft = {
                    "headline": "closeout report가 생성됐습니다.",
                    "changes": [],
                    "timeline": ["closeout draft를 생성했습니다."],
                    "agent_contributions": [],
                    "issues": [],
                    "achievements": [],
                    "highlight_artifacts": ["shared_workspace/sprints/260411-Sprint-23-44/milestone.md"],
                }

                class FakePlannerRuntime:
                    def __init__(self):
                        self.calls: list[tuple[object, dict]] = []

                    def run_task(self, envelope, request_record):
                        self.calls.append((envelope, request_record))
                        return {
                            "request_id": request_record["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "planner closeout draft를 생성했습니다.",
                            "insights": [],
                            "proposals": {"sprint_report": planner_draft},
                            "artifacts": list(request_record.get("artifacts") or []),
                            "next_role": "",
                            "error": "",
                        }

                planner_runtime = FakePlannerRuntime()

                with patch.object(service, "_runtime_for_role", return_value=planner_runtime):
                    draft = asyncio.run(
                        service._draft_sprint_report_via_planner(
                            sprint_state,
                            closeout_result,
                        )
                    )

                self.assertEqual(draft, planner_draft)
                self.assertEqual(len(planner_runtime.calls), 1)
                _envelope, request_record = planner_runtime.calls[0]
                self.assertEqual(request_record["request_id"], expected_request_id)
                self.assertEqual(request_record["params"]["_teams_kind"], "sprint_closeout_report")
                self.assertEqual(request_record["params"]["sprint_id"], sprint_state["sprint_id"])
                self.assertEqual(request_record["params"]["closeout_status"], closeout_result["status"])
                self.assertEqual(request_record["params"]["closeout_message"], closeout_result["message"])
                self.assertEqual(request_record["scope"], f"{sprint_state['sprint_id']} closeout report")
                context_file = planner_runtime.calls[0][1]["artifacts"][0]
                expected_context_file = service._relative_workspace_path(
                    service.paths.role_sources_dir("planner") / f"{expected_request_id}.closeout_report.json"
                )
                self.assertEqual(context_file, expected_context_file)
                context_path = service.paths.role_sources_dir("planner") / f"{expected_request_id}.closeout_report.json"
                self.assertTrue(context_path.exists())
                payload = json.loads(context_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["sprint_id"], sprint_state["sprint_id"])
                self.assertEqual(payload["closeout_result"]["status"], closeout_result["status"])
                self.assertEqual(payload["closeout_result"]["message"], closeout_result["message"])
                expected_todo_request_file = service._relative_workspace_path(service.paths.request_file(todo_request_id))
                self.assertIn(expected_todo_request_file, payload["request_files"])
                self.assertIn(context_file, request_record["artifacts"])
                self.assertIn(expected_todo_request_file, request_record["artifacts"])

    def test_planner_backlog_merge_keeps_selected_fields_from_proposals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                )
                sprint_state["sprint_id"] = "260403-Sprint-13:36"
                service._save_sprint_state(sprint_state)

                backlog_id = "backlog-20260403-duplicate"
                proposal_item = {
                    "backlog_id": backlog_id,
                    "title": "KIS 우선 전환 대상 경로 식별 및 기준선 정리",
                    "summary": "제안 payload는 rationale only입니다.",
                    "kind": "chore",
                    "scope": "prioritized backlog",
                    "acceptance_criteria": [],
                    "milestone_title": sprint_state["milestone_title"],
                    "priority_rank": 1,
                    "source": "planner",
                    "status": "pending",
                    "planned_in_sprint_id": "",
                    "selected_in_sprint_id": "",
                }

                artifact_path = service.paths.backlog_file(backlog_id)
                artifact_data = {
                    "backlog_id": backlog_id,
                    "title": proposal_item["title"],
                    "summary": proposal_item["summary"],
                    "kind": proposal_item["kind"],
                    "scope": proposal_item["scope"],
                    "acceptance_criteria": proposal_item["acceptance_criteria"],
                    "milestone_title": proposal_item["milestone_title"],
                    "priority_rank": proposal_item["priority_rank"],
                    "source": "planner",
                    "status": "selected",
                    "planned_in_sprint_id": sprint_state["sprint_id"],
                    "selected_in_sprint_id": sprint_state["sprint_id"],
                }
                write_json(artifact_path, artifact_data)

                request_record = {
                    "request_id": "20260403-e4757963",
                    "status": "completed",
                    "intent": "plan",
                    "urgency": "normal",
                    "scope": "김단타 스프린트 초기 실행",
                    "body": "initial phase todo_finalization",
                    "artifacts": [str(artifact_path)],
                    "params": {
                        "sprint_id": sprint_state["sprint_id"],
                        "initial_phase_step": orchestration_module.INITIAL_PHASE_STEP_TODO_FINALIZATION,
                    },
                }
                result = {
                    "proposals": {
                        "backlog_items": [proposal_item],
                        "backlog_writes": [
                            {
                                "status": "updated",
                                "backlog_id": backlog_id,
                                "artifact_path": str(artifact_path),
                                "changed_fields": [
                                    "milestone_title",
                                    "priority_rank",
                                    "planned_in_sprint_id",
                                    "selected_in_sprint_id",
                                ],
                            }
                        ],
                    },
                    "artifacts": [],
                }

                sync_summary = service._sync_planner_backlog_from_report(request_record, result)
                persisted = {
                    str(item.get("backlog_id")): item for item in service._iter_backlog_items()
                }.get(backlog_id, {})

                self.assertEqual(sync_summary["proposal_items"], 1)
                self.assertEqual(sync_summary["receipt_items"], 1)
                self.assertEqual(sync_summary["verified_backlog_items"], 1)
                self.assertTrue(sync_summary["planner_persisted_backlog"])
                self.assertEqual(persisted.get("backlog_id"), backlog_id)
                self.assertEqual(persisted.get("status"), "selected")
                self.assertEqual(persisted.get("planned_in_sprint_id"), sprint_state["sprint_id"])
                self.assertEqual(persisted.get("selected_in_sprint_id"), sprint_state["sprint_id"])

    def test_sync_sprint_planning_state_only_marks_initial_phase_ready_at_todo_finalization(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                )
                backlog_item = build_backlog_item(
                    title="priority only",
                    summary="우선순위만 정리합니다.",
                    kind="feature",
                    source="planner",
                    scope="priority only",
                    milestone_title=sprint_state["milestone_title"],
                    priority_rank=3,
                )
                service._save_backlog_item(backlog_item)

                prioritization_request = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
                )
                prioritization_result = {
                    "request_id": prioritization_request["request_id"],
                    "role": "planner",
                    "status": "completed",
                    "summary": "backlog 우선순위를 정리했습니다.",
                    "insights": [],
                    "proposals": {},
                    "artifacts": [],
                    "error": "",
                }
                self.assertFalse(
                    service._apply_sprint_planning_result(
                        sprint_state,
                        phase="initial",
                        request_record=prioritization_request,
                        result=prioritization_result,
                    )
                )

                backlog_item["planned_in_sprint_id"] = sprint_state["sprint_id"]
                service._save_backlog_item(backlog_item)
                todo_request = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_TODO_FINALIZATION,
                )
                todo_result = {
                    "request_id": todo_request["request_id"],
                    "role": "planner",
                    "status": "completed",
                    "summary": "실행 todo를 확정했습니다.",
                    "insights": [],
                    "proposals": {},
                    "artifacts": [],
                    "error": "",
                }
                self.assertTrue(
                    service._apply_sprint_planning_result(
                        sprint_state,
                        phase="initial",
                        request_record=todo_request,
                        result=todo_result,
                    )
                )
                self.assertEqual(
                    [item["title"] for item in sprint_state["selected_items"]],
                    ["priority only"],
                )

    def test_validate_initial_phase_backlog_definition_requires_persisted_backlog_with_trace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                    kickoff_requirements=["requirement-a"],
                )
                request_record = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
                )

                error = service._validate_initial_phase_step_result(
                    sprint_state,
                    request_record=request_record,
                    sync_summary={"planner_persisted_backlog": False},
                )

                self.assertIn("sprint-relevant backlog가 0건", error)

                traced_backlog = build_backlog_item(
                    title="KIS websocket alert contract",
                    summary="KIS websocket 신규 호가 알림 계약을 정의합니다.",
                    kind="feature",
                    source="planner",
                    scope="monitoring alert",
                    acceptance_criteria=["신규 호가 이벤트가 알림과 히스토리에 반영된다."],
                    origin={
                        "milestone_ref": sprint_state["milestone_title"],
                        "requirement_refs": ["requirement-a"],
                    },
                    milestone_title=sprint_state["milestone_title"],
                )
                service._save_backlog_item(traced_backlog)

                missing_trace_error = service._validate_initial_phase_step_result(
                    sprint_state,
                    request_record=request_record,
                    sync_summary={"planner_persisted_backlog": True},
                )

                self.assertIn("origin.spec_refs 없음", missing_trace_error)

    def test_validate_initial_phase_backlog_definition_accepts_traced_backlog(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                    kickoff_requirements=["requirement-a", "requirement-b"],
                )
                traced_backlog = build_backlog_item(
                    title="KIS websocket alert contract",
                    summary="KIS websocket 신규 호가 알림 계약을 정의합니다.",
                    kind="feature",
                    source="planner",
                    scope="monitoring alert",
                    acceptance_criteria=[
                        "신규 호가 이벤트가 알림과 히스토리에 반영된다.",
                        "채널 id가 kickoff 요구와 일치한다.",
                    ],
                    origin={
                        "milestone_ref": sprint_state["milestone_title"],
                        "requirement_refs": ["requirement-a", "requirement-b"],
                        "spec_refs": ["./shared_workspace/sprints/current/spec.md#kis-alert"],
                    },
                    milestone_title=sprint_state["milestone_title"],
                )
                service._save_backlog_item(traced_backlog)
                request_record = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
                )

                error = service._validate_initial_phase_step_result(
                    sprint_state,
                    request_record=request_record,
                    sync_summary={"planner_persisted_backlog": True},
                )

                self.assertEqual(error, "")

    def test_apply_sprint_planning_result_marks_backlog_definition_invalid_when_zero_backlog(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                    kickoff_requirements=["requirement-a"],
                )
                request_record = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
                )
                result = {
                    "request_id": request_record["request_id"],
                    "role": "planner",
                    "status": "completed",
                    "summary": "backlog 정의를 완료했습니다.",
                    "insights": [],
                    "proposals": {},
                    "artifacts": [],
                    "error": "",
                }

                phase_ready = service._apply_sprint_planning_result(
                    sprint_state,
                    phase="initial",
                    request_record=request_record,
                    result=result,
                )

                persisted_request = service._load_request(request_record["request_id"])
                self.assertFalse(phase_ready)
                self.assertIn(
                    "backlog 0건 상태는 허용되지 않습니다",
                    str(persisted_request.get("initial_phase_validation_error") or ""),
                )

    def test_apply_sprint_planning_result_verifies_backlog_artifact_without_receipt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                    kickoff_requirements=["requirement-a"],
                )
                service._save_sprint_state(sprint_state)
                traced_backlog = build_backlog_item(
                    title="KIS websocket alert contract",
                    summary="KIS websocket 신규 호가 알림 계약을 정의합니다.",
                    kind="feature",
                    source="planner",
                    scope="monitoring alert",
                    acceptance_criteria=["신규 호가 이벤트가 알림과 히스토리에 반영된다."],
                    origin={
                        "sprint_id": sprint_state["sprint_id"],
                        "milestone_ref": sprint_state["milestone_title"],
                        "requirement_refs": ["requirement-a"],
                        "spec_refs": ["./shared_workspace/sprints/current/spec.md#kis-alert"],
                    },
                )
                service._save_backlog_item(traced_backlog)
                request_record = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
                )
                artifact_path = f"./.teams_runtime/backlog/{traced_backlog['backlog_id']}.json"
                result = {
                    "request_id": request_record["request_id"],
                    "role": "planner",
                    "status": "completed",
                    "summary": "backlog 정의를 완료했습니다.",
                    "insights": [],
                    "proposals": {
                        "backlog_items": [
                            {
                                "backlog_id": traced_backlog["backlog_id"],
                                "title": traced_backlog["title"],
                            }
                        ],
                    },
                    "artifacts": [artifact_path],
                    "error": "",
                }
                sync_summary = service._sync_planner_backlog_from_report(request_record, result)
                request_record["planning_sync_summary"] = sync_summary

                phase_ready = service._apply_sprint_planning_result(
                    sprint_state,
                    phase="initial",
                    request_record=request_record,
                    result=result,
                )

                persisted_request = service._load_request(request_record["request_id"])
                self.assertFalse(phase_ready)
                self.assertTrue(sync_summary["planner_persisted_backlog"])
                self.assertEqual(sync_summary["receipt_items"], 0)
                self.assertEqual(sync_summary["persisted_backlog_items"], 1)
                self.assertIn("planner backlog_writes receipt missing", sync_summary["missing_backlog_receipts"])
                self.assertEqual(persisted_request["initial_phase_validation_error"], "")

    def test_run_initial_sprint_phase_uses_fixed_five_step_sequence(self):
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
                seen_steps: list[str] = []

                async def fake_run_internal_request_chain(*, sprint_id, request_record, initial_role):
                    self.assertEqual(sprint_id, sprint_state["sprint_id"])
                    self.assertEqual(initial_role, "planner")
                    seen_steps.append(str(request_record["params"].get("initial_phase_step") or ""))
                    persisted = service._load_request(request_record["request_id"])
                    persisted["status"] = "completed"
                    persisted["result"] = {
                        "request_id": request_record["request_id"],
                        "role": "planner",
                        "status": "completed",
                        "summary": f"{request_record['params'].get('initial_phase_step')} 완료",
                        "insights": [],
                        "proposals": {},
                        "artifacts": [],
                        "error": "",
                    }
                    service._save_request(persisted)
                    return dict(persisted["result"])

                def fake_apply_sprint_planning_result(sprint_state_arg, *, phase, request_record, result):
                    self.assertEqual(phase, "initial")
                    return (
                        str(request_record["params"].get("initial_phase_step") or "")
                        == orchestration_module.INITIAL_PHASE_STEP_TODO_FINALIZATION
                    )

                with (
                    patch.object(service, "_run_internal_request_chain", side_effect=fake_run_internal_request_chain),
                    patch.object(service, "_apply_sprint_planning_result", side_effect=fake_apply_sprint_planning_result),
                    patch.object(service, "_validate_initial_phase_step_result", return_value=""),
                ):
                    ready = asyncio.run(service._run_initial_sprint_phase(sprint_state))

                self.assertTrue(ready)
                self.assertEqual(
                    seen_steps,
                    list(orchestration_module.INITIAL_PHASE_STEPS),
                )
                self.assertEqual(sprint_state["phase"], "ongoing")
                self.assertEqual(sprint_state["status"], "running")

    def test_run_initial_sprint_phase_emits_spec_todo_preflight_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('  start_mode: "auto"\n', '  start_mode: "manual_daily"\n', 1)
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="spec preflight",
                    trigger="manual_start",
                )
                seen_titles: list[str] = []

                async def fake_run_internal_request_chain(*, sprint_id, request_record, initial_role):
                    self.assertEqual(sprint_id, sprint_state["sprint_id"])
                    self.assertEqual(initial_role, "planner")
                    persisted = service._load_request(request_record["request_id"])
                    persisted["status"] = "completed"
                    persisted["result"] = {
                        "request_id": request_record["request_id"],
                        "role": "planner",
                        "status": "completed",
                        "summary": f"{request_record['params'].get('initial_phase_step')} 완료",
                        "insights": ["canonical spec/todo synced"],
                        "proposals": {},
                        "artifacts": [],
                        "error": "",
                    }
                    service._save_request(persisted)
                    return dict(persisted["result"])

                def fake_apply_sprint_planning_result(sprint_state_arg, *, phase, request_record, result):
                    self.assertEqual(phase, "initial")
                    return (
                        str(request_record["params"].get("initial_phase_step") or "")
                        == orchestration_module.INITIAL_PHASE_STEP_TODO_FINALIZATION
                    )

                async def fake_send_sprint_report(*, title, **_kwargs):
                    seen_titles.append(title)
                    return None

                with (
                    patch.object(service, "_run_internal_request_chain", side_effect=fake_run_internal_request_chain),
                    patch.object(service, "_apply_sprint_planning_result", side_effect=fake_apply_sprint_planning_result),
                    patch.object(service, "_validate_initial_phase_step_result", return_value=""),
                    patch.object(service, "_send_sprint_report", side_effect=fake_send_sprint_report),
                ):
                    ready = asyncio.run(service._run_initial_sprint_phase(sprint_state))

                self.assertTrue(ready)
                self.assertIn("📐 스프린트 Spec/TODO", seen_titles)

    def test_build_sprint_spec_todo_report_body_uses_sectioned_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="sectioned spec report",
                    trigger="manual_start",
                )
                sprint_state["planning_iterations"] = [
                    {
                        "created_at": "2026-04-13T09:00:00+09:00",
                        "phase": "initial",
                        "step": "todo_finalization",
                        "request_id": "req-sectioned-spec-report",
                        "summary": "canonical spec과 todo를 정리했습니다.",
                        "insights": ["KIS websocket constraint 유지", "QA reopen 시 planner가 spec을 다시 닫음"],
                        "artifacts": [],
                        "phase_ready": True,
                    }
                ]
                sprint_state["todos"] = [
                    {
                        "todo_id": "todo-1",
                        "title": "KIS websocket adapter 구현",
                        "owner_role": "planner",
                    }
                ]

                rendered = service._build_sprint_spec_todo_report_body(sprint_state)

                self.assertIn("[Milestone]", rendered)
                self.assertIn("[Spec]", rendered)
                self.assertIn("[TODO]", rendered)
                self.assertIn("- selected_count: 1", rendered)
                self.assertIn("KIS websocket constraint 유지", rendered)

    def test_run_initial_sprint_phase_clears_active_sprint_after_planning_incomplete(self):
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
                service._save_sprint_state(sprint_state)
                service._save_scheduler_state(
                    {
                        "active_sprint_id": sprint_state["sprint_id"],
                        "last_started_at": sprint_state["started_at"],
                        "last_completed_at": "",
                        "next_slot_at": "",
                        "deferred_slot_at": "",
                        "last_trigger": "manual_start",
                    }
                )

                async def fake_run_internal_request_chain(*, sprint_id, request_record, initial_role):
                    self.assertEqual(sprint_id, sprint_state["sprint_id"])
                    self.assertEqual(initial_role, "planner")
                    persisted = service._load_request(request_record["request_id"])
                    persisted["status"] = "completed"
                    persisted["result"] = {
                        "request_id": request_record["request_id"],
                        "role": "planner",
                        "status": "completed",
                        "summary": f"{request_record['params'].get('initial_phase_step')} 완료",
                        "insights": [],
                        "proposals": {},
                        "artifacts": [],
                        "error": "",
                    }
                    service._save_request(persisted)
                    return dict(persisted["result"])

                with (
                    patch.object(service, "_run_internal_request_chain", side_effect=fake_run_internal_request_chain),
                    patch.object(service, "_apply_sprint_planning_result", return_value=False),
                    patch.object(service, "_send_terminal_sprint_reports", AsyncMock()),
                    patch.object(service, "_draft_sprint_report_via_planner", AsyncMock(return_value={})),
                    patch.object(orchestration_module, "SPRINT_INITIAL_PHASE_MAX_ITERATIONS", 1),
                ):
                    ready = asyncio.run(service._run_initial_sprint_phase(sprint_state))

                self.assertFalse(ready)
                updated_sprint = service._load_sprint_state(sprint_state["sprint_id"])
                scheduler_state = service._load_scheduler_state()
                self.assertEqual(updated_sprint["status"], "blocked")
                self.assertEqual(updated_sprint["closeout_status"], "planning_incomplete")
                self.assertTrue(updated_sprint["ended_at"])
                self.assertEqual(scheduler_state["active_sprint_id"], "")

    def test_build_sprint_planning_request_record_reuses_open_initial_phase_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                )
                first_request = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
                )
                first_request["status"] = "delegated"
                first_request["current_role"] = "planner"
                first_request["next_role"] = "planner"
                service._save_request(first_request)

                reused_request = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
                )
                next_step_request = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_ARTIFACT_SYNC,
                )

                self.assertEqual(reused_request["request_id"], first_request["request_id"])
                self.assertNotEqual(next_step_request["request_id"], first_request["request_id"])
                self.assertEqual(len(list(service.paths.requests_dir.glob("*.json"))), 2)

    def test_resume_active_sprint_clears_legacy_reload_meta_and_continues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T001600Z",
                    "sprint_name": "2026-Sprint-01-20260324T001600Z",
                    "sprint_display_name": "2026-Sprint-01-20260324T001600Z",
                    "sprint_folder": str(service.paths.sprint_artifact_dir("2026-Sprint-01-20260324T001600Z")),
                    "sprint_folder_name": "2026-Sprint-01-20260324T001600Z",
                    "status": "blocked",
                    "closeout_status": "restart_required",
                    "trigger": "manual_restart",
                    "phase": "ongoing",
                    "started_at": "2026-03-24T00:16:00+09:00",
                    "ended_at": "2026-03-24T00:17:00+09:00",
                    "selected_backlog_ids": [],
                    "selected_items": [],
                    "todos": [],
                    "commit_sha": "",
                    "commit_shas": [],
                    "commit_count": 0,
                    "uncommitted_paths": [],
                    "version_control_status": "",
                    "version_control_sha": "",
                    "version_control_paths": [],
                    "version_control_message": "",
                    "version_control_error": "",
                    "auto_commit_status": "",
                    "auto_commit_sha": "",
                    "auto_commit_paths": [],
                    "auto_commit_message": "",
                    "reload_required": True,
                    "reload_paths": ["teams_runtime/core/orchestration.py"],
                    "reload_message": "runtime updated",
                    "reload_restart_command": "python -m teams_runtime restart",
                    "report_path": "",
                    "git_baseline": {"repo_root": "", "head_sha": "", "dirty_paths": []},
                    "resume_from_checkpoint_requested_at": "",
                    "last_resume_checkpoint_todo_id": "",
                    "last_resume_checkpoint_status": "",
                }
                service._save_sprint_state(sprint_state)
                service._save_scheduler_state({"active_sprint_id": sprint_state["sprint_id"]})

                with patch.object(service, "_continue_sprint", AsyncMock(return_value=None)) as continue_mock:
                    asyncio.run(service._resume_active_sprint(sprint_state["sprint_id"]))

                updated = service._load_sprint_state(sprint_state["sprint_id"])
                self.assertEqual(updated["status"], "blocked")
                self.assertEqual(updated["ended_at"], "")
                self.assertNotIn("reload_required", updated)
                self.assertNotIn("reload_paths", updated)
                self.assertNotIn("reload_message", updated)
                self.assertNotIn("reload_restart_command", updated)
                continue_mock.assert_awaited_once()

    def test_resumable_blocked_sprint_allows_planning_incomplete_and_legacy_initial_block(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = {
                    "sprint_id": "2026-Sprint-legacy-20260403",
                    "status": "blocked",
                    "phase": "initial",
                    "closeout_status": "planning_incomplete",
                    "report_body": "initial phase에서 실행 가능한 prioritized todo를 만들지 못해 sprint를 중단했습니다.",
                }
                self.assertTrue(service._is_resumable_blocked_sprint(sprint_state))

                sprint_state["closeout_status"] = "restart_required"
                self.assertTrue(service._is_resumable_blocked_sprint(sprint_state))

                sprint_state["closeout_status"] = ""
                sprint_state["report_body"] = "임의의 블록 사유"
                self.assertFalse(service._is_resumable_blocked_sprint(sprint_state))

    def test_sprint_planning_request_record_requires_milestone_relevant_tasks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                )

                request_record = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                )

                self.assertIn("Preserve the original kickoff brief", request_record["body"])
                self.assertIn("single milestone", request_record["body"])
                self.assertIn("mandatory backlog definition", request_record["body"])
                self.assertIn("Only include backlog items and sprint todos that directly advance", request_record["body"])
                self.assertIn("Do not promote unrelated maintenance or side quests", request_record["body"])
                self.assertIn("create or reopen sprint-relevant backlog before prioritization", request_record["body"])
                self.assertEqual(request_record["params"]["milestone_title"], "workflow initial")
                self.assertIn(
                    service._workspace_artifact_hint(service.paths.shared_backlog_file),
                    request_record["artifacts"],
                )
                self.assertIn(
                    service._workspace_artifact_hint(service.paths.shared_completed_backlog_file),
                    request_record["artifacts"],
                )
                self.assertIn(
                    service._workspace_artifact_hint(service.paths.current_sprint_file),
                    request_record["artifacts"],
                )
                self.assertIn(
                    service._workspace_artifact_hint(service._sprint_artifact_paths(sprint_state)["kickoff"]),
                    request_record["artifacts"],
                )

    def test_sprint_planning_request_record_includes_preserved_kickoff_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                    kickoff_brief="keep the original scope detail",
                    kickoff_requirements=["preserve kickoff", "derive refined milestone separately"],
                    kickoff_request_text="start sprint\nmilestone: workflow initial\nbrief: keep the original scope detail",
                    kickoff_source_request_id="request-origin-1",
                    kickoff_reference_artifacts=["./shared_workspace/sprints/260404-Sprint-09-00/attachments/att-1_scope.md"],
                )

                request_record = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
                )

                self.assertEqual(request_record["params"]["requested_milestone_title"], "workflow initial")
                self.assertEqual(request_record["params"]["kickoff_brief"], "keep the original scope detail")
                self.assertEqual(
                    request_record["params"]["kickoff_requirements"],
                    ["preserve kickoff", "derive refined milestone separately"],
                )
                self.assertEqual(request_record["params"]["kickoff_source_request_id"], "request-origin-1")
                self.assertIn("kickoff_brief:", request_record["body"])
                self.assertIn("kickoff_requirements:", request_record["body"])
                self.assertIn("request-origin-1", request_record["body"])
                self.assertIn(
                    "./shared_workspace/sprints/260404-Sprint-09-00/attachments/att-1_scope.md",
                    request_record["artifacts"],
                )

    def test_planner_initial_phase_reports_start_and_checkpoint_to_report_channel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "discord_agents_config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace(
                'report_channel_id: "111111111111111111"',
                'report_channel_id: "1486503058765779066"',
                1,
            )
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                )
                service._save_sprint_state(sprint_state)
                request_record = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
                )
                request_record["status"] = "delegated"
                request_record["current_role"] = "planner"
                request_record["next_role"] = "planner"
                service._save_request(request_record)
                envelope = MessageEnvelope(
                    request_id=request_record["request_id"],
                    sender="orchestrator",
                    target="planner",
                    intent="plan",
                    urgency="normal",
                    scope=request_record["scope"],
                    artifacts=list(request_record["artifacts"]),
                    params={"_teams_kind": "delegate"},
                    body=request_record["body"],
                )

                with (
                    patch.object(
                        service.role_runtime,
                        "run_task",
                        return_value={
                            "request_id": request_record["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "milestone title과 framing을 정리했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": [str(service._sprint_artifact_paths(sprint_state)["milestone"])],
                            "error": "",
                        },
                    ),
                    patch.object(service, "_send_relay", AsyncMock(return_value=None)),
                ):
                    asyncio.run(service._process_delegated_request(envelope, request_record))

                self.assertEqual(
                    [channel for channel, _ in service.discord_client.sent_channels],
                    ["1486503058765779066", "1486503058765779066"],
                )
                self.assertIn("planner initial 1/5 시작", service.discord_client.sent_channels[0][1])
                self.assertIn("milestone 정리", service.discord_client.sent_channels[0][1])
                self.assertIn("planner initial 1/5 체크포인트", service.discord_client.sent_channels[1][1])
                self.assertIn("milestone title과 framing을 정리했습니다.", service.discord_client.sent_channels[1][1])

    def test_planner_initial_phase_checkpoint_report_includes_concrete_planning_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "discord_agents_config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace(
                'report_channel_id: "111111111111111111"',
                'report_channel_id: "1486503058765779066"',
                1,
            )
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                )
                request_record = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_TODO_FINALIZATION,
                )

                payload = {
                    "request_id": request_record["request_id"],
                    "role": "planner",
                    "status": "completed",
                    "summary": "실행 todo를 확정했습니다.",
                    "proposals": {
                        "revised_milestone_title": "workflow refined",
                        "backlog_items": [
                            {
                                "title": "manual sprint start gate",
                                "summary": "milestone 없이는 sprint를 시작하지 않도록 정리",
                            },
                            {
                                "title": "sprint folder artifact rendering",
                                "summary": "sprint folder living docs를 렌더링",
                            },
                        ],
                    },
                    "artifacts": [],
                    "error": "",
                }

                report = service._build_planner_initial_phase_activity_report(
                    request_record,
                    event_type="role_completed",
                    status="completed",
                    summary=str(payload["summary"]),
                    payload=payload,
                )

                self.assertIn("마일스톤을 workflow refined로 정리하고 backlog/todo 2건을 확정했습니다.", report)
                self.assertIn("manual sprint start gate", report)
                self.assertIn("sprint folder artifact rendering", report)
                self.assertIn("[우선순위/확정]", report)
                self.assertIn("- manual sprint start gate | milestone 없이는 sprint를 시작하지 않도록 정리", report)
                self.assertIn("- sprint folder artifact rendering | sprint folder living docs를 렌더링", report)

    def test_planner_initial_phase_activity_report_dedupes_same_event(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "discord_agents_config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace(
                'report_channel_id: "111111111111111111"',
                'report_channel_id: "1486503058765779066"',
                1,
            )
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                )
                request_record = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                    step=orchestration_module.INITIAL_PHASE_STEP_ARTIFACT_SYNC,
                )

                asyncio.run(
                    service._maybe_report_planner_initial_phase_activity(
                        request_record,
                        event_type="role_started",
                        status="running",
                        summary="plan/spec 동기화를 시작했습니다.",
                    )
                )
                asyncio.run(
                    service._maybe_report_planner_initial_phase_activity(
                        request_record,
                        event_type="role_started",
                        status="running",
                        summary="plan/spec 동기화를 시작했습니다.",
                    )
                )

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                persisted_request = service._load_request(request_record["request_id"])
                self.assertEqual(len(persisted_request.get("planner_initial_phase_report_keys") or []), 1)

    def test_scheduler_resumes_stuck_active_sprint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="stuck sprint recovery",
                    summary="중단된 sprint를 재개합니다.",
                    kind="bug",
                    source="discovery",
                    scope="stuck sprint recovery",
                    backlog_id="backlog-20260324-stuck1234",
                )
                backlog_item["status"] = "selected"
                backlog_item["selected_in_sprint_id"] = "2026-Sprint-01-20260324T000200Z"
                service._save_backlog_item(backlog_item)
                service.paths.current_sprint_file.write_text("# current sprint\n", encoding="utf-8")
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T000200Z",
                    "status": "running",
                    "trigger": "scheduled_slot",
                    "started_at": "2026-03-24T00:02:00+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [backlog_item["backlog_id"]],
                    "selected_items": [dict(backlog_item)],
                    "todos": [build_todo_item(backlog_item, owner_role="planner")],
                    "commit_sha": "",
                    "report_path": "",
                    "git_baseline": {"repo_root": "", "head_sha": "", "dirty_paths": []},
                }
                service._save_sprint_state(sprint_state)
                service._save_scheduler_state(
                    {
                        "active_sprint_id": "2026-Sprint-01-20260324T000200Z",
                        "last_started_at": "2026-03-24T00:02:00+00:00",
                        "last_completed_at": "",
                        "next_slot_at": "2026-03-24T03:00:00+00:00",
                        "deferred_slot_at": "",
                        "last_trigger": "scheduled_slot",
                    }
                )

                async def fake_delegate(request_record, next_role):
                    result = {
                        "request_id": request_record["request_id"],
                        "role": next_role,
                        "status": "completed",
                        "summary": f"{next_role} resumed the sprint.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": ["./shared_workspace/current_sprint.md"] if next_role == "planner" else [],
                        "next_role": "" if next_role != "planner" else "developer",
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
                    patch.object(service, "_delegate_request", side_effect=fake_delegate),
                    patch(
                        "teams_runtime.core.orchestration.inspect_sprint_closeout",
                        return_value={
                            "status": "no_new_commits",
                            "representative_commit_sha": "",
                            "commit_count": 0,
                            "commit_shas": [],
                            "uncommitted_paths": [],
                            "message": "baseline 이후 새 커밋은 없지만 미커밋 sprint-owned 변경도 없습니다.",
                        },
                    ),
                    patch.object(
                        service.version_controller_runtime,
                        "run_task",
                        return_value={
                            "request_id": "20260326-55a3c491",
                            "role": "version_controller",
                            "status": "completed",
                            "summary": "task 소유 변경 파일이 없습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["sources/20260326-55a3c491.task.version_control.json"],
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
                    patch.object(service, "_draft_sprint_report_via_planner", AsyncMock(return_value={})),
                ):
                    asyncio.run(service._poll_scheduler_once())

                resumed_state = json.loads(
                    service.paths.sprint_file("2026-Sprint-01-20260324T000200Z").read_text(encoding="utf-8")
                )
                scheduler_state = service._load_scheduler_state()
                self.assertEqual(resumed_state["status"], "completed")
                self.assertEqual(resumed_state["todos"][0]["status"], "completed")
                self.assertEqual(scheduler_state["active_sprint_id"], "")
                self.assertEqual(service._load_backlog_item(backlog_item["backlog_id"])["status"], "done")
                version_controller_mock.assert_called_once()

    def test_finalize_sprint_delegates_pending_sprint_owned_changes_to_version_controller(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T001400Z",
                    "status": "running",
                    "trigger": "backlog_ready",
                    "started_at": "2026-03-24T00:14:00+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [],
                    "selected_items": [],
                    "todos": [],
                    "commit_sha": "",
                    "commit_shas": [],
                    "commit_count": 0,
                    "closeout_status": "",
                    "uncommitted_paths": [],
                    "auto_commit_status": "",
                    "auto_commit_sha": "",
                    "auto_commit_paths": [],
                    "auto_commit_message": "",
                    "report_path": "",
                    "git_baseline": {"repo_root": "/repo", "head_sha": "base123", "dirty_paths": []},
                }
                service._save_sprint_state(sprint_state)

                with (
                    patch.object(
                        service,
                        "_inspect_sprint_documentation_closeout",
                        return_value={"status": "verified", "message": "doc verified"},
                    ),
                    patch(
                        "teams_runtime.core.orchestration.inspect_sprint_closeout",
                        side_effect=[
                            {
                                "status": "pending_changes",
                                "representative_commit_sha": "",
                                "commit_count": 0,
                                "commit_shas": [],
                                "uncommitted_paths": ["workspace/app.py"],
                                "message": "스프린트 소유 변경 파일 중 아직 커밋되지 않은 항목이 있습니다.",
                            },
                            {
                                "status": "verified",
                                "representative_commit_sha": "commit789",
                                "commit_count": 1,
                                "commit_shas": ["commit789"],
                                "uncommitted_paths": [],
                                "message": "스프린트 closeout 검증을 완료했습니다.",
                            },
                        ],
                    ),
                    patch.object(
                        service.version_controller_runtime,
                        "run_task",
                        return_value={
                            "request_id": "2026-Sprint-01-20260324T001400Z:closeout",
                            "role": "version_controller",
                            "status": "completed",
                            "summary": "leftover sprint 변경을 커밋했습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["sources/2026-Sprint-01-20260324T001400Z.closeout.version_control.json"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                            "commit_status": "committed",
                            "commit_sha": "commit789",
                            "commit_paths": ["workspace/app.py"],
                            "commit_message": "[2026-Sprint-01-20260324T001400Z] chore: sprint closeout",
                            "change_detected": True,
                        },
                    ) as version_controller_mock,
                    patch.object(service, "_draft_sprint_report_via_planner", AsyncMock(return_value={})),
                ):
                    asyncio.run(service._finalize_sprint(sprint_state))

                updated = json.loads(
                    service.paths.sprint_file("2026-Sprint-01-20260324T001400Z").read_text(encoding="utf-8")
                )
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(updated["closeout_status"], "verified")
                self.assertEqual(updated["commit_sha"], "commit789")
                self.assertEqual(updated["version_control_status"], "committed")
                self.assertEqual(updated["auto_commit_status"], "committed")
                self.assertEqual(updated["auto_commit_sha"], "commit789")
                self.assertEqual(updated["auto_commit_paths"], ["workspace/app.py"])
                self.assertEqual(version_controller_mock.call_args.args[0].params["version_control_mode"], "closeout")
                history_text = service.paths.sprint_history_file("2026-Sprint-01-20260324T001400Z").read_text(encoding="utf-8")
                self.assertIn("version_control_status=committed", history_text)
                self.assertIn("auto_commit_status=committed", history_text)
                self.assertIn("auto_commit_paths=workspace/app.py", history_text)

    def test_finalize_sprint_completes_with_warning_when_commit_lacks_sprint_tag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T001450Z",
                    "status": "running",
                    "trigger": "backlog_ready",
                    "started_at": "2026-03-24T00:14:50+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [],
                    "selected_items": [],
                    "todos": [],
                    "commit_sha": "",
                    "commit_shas": [],
                    "commit_count": 0,
                    "closeout_status": "",
                    "uncommitted_paths": [],
                    "auto_commit_status": "",
                    "auto_commit_sha": "",
                    "auto_commit_paths": [],
                    "auto_commit_message": "",
                    "report_path": "",
                    "git_baseline": {"repo_root": "/repo", "head_sha": "base123", "dirty_paths": []},
                }
                service._save_sprint_state(sprint_state)

                with (
                    patch.object(
                        service,
                        "_inspect_sprint_documentation_closeout",
                        return_value={"status": "verified", "message": "doc verified"},
                    ),
                    patch(
                        "teams_runtime.core.orchestration.inspect_sprint_closeout",
                        return_value={
                            "status": "warning_missing_sprint_tag",
                            "representative_commit_sha": "warn123",
                            "commit_count": 1,
                            "commit_shas": ["warn123"],
                            "sprint_tagged_commit_count": 0,
                            "sprint_tagged_commit_shas": [],
                            "uncommitted_paths": [],
                            "message": "baseline 이후 새 커밋은 확인되었고 미커밋 sprint-owned 변경도 없습니다. sprint_id 태그 커밋은 없어 권장사항 경고만 남깁니다.",
                        },
                    ),
                    patch.object(service, "_draft_sprint_report_via_planner", AsyncMock(return_value={})),
                ):
                    asyncio.run(service._finalize_sprint(sprint_state))

                updated = json.loads(
                    service.paths.sprint_file("2026-Sprint-01-20260324T001450Z").read_text(encoding="utf-8")
                )
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(updated["closeout_status"], "warning_missing_sprint_tag")
                self.assertEqual(updated["commit_sha"], "warn123")
                history_text = service.paths.sprint_history_file("2026-Sprint-01-20260324T001450Z").read_text(encoding="utf-8")
                self.assertIn("sprint_tagged_commit_count=0", history_text)
                self.assertIn("closeout_status=warning_missing_sprint_tag", history_text)
                combined_reports = "\n".join(content for _channel_id, content in service.discord_client.sent_channels)
                self.assertIn("⚠️ 스프린트 완료(경고)", combined_reports)

    def test_finalize_sprint_fails_when_version_controller_closeout_commit_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T001500Z",
                    "status": "running",
                    "trigger": "backlog_ready",
                    "started_at": "2026-03-24T00:15:00+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [],
                    "selected_items": [],
                    "todos": [],
                    "commit_sha": "",
                    "commit_shas": [],
                    "commit_count": 0,
                    "closeout_status": "",
                    "uncommitted_paths": [],
                    "auto_commit_status": "",
                    "auto_commit_sha": "",
                    "auto_commit_paths": [],
                    "auto_commit_message": "",
                    "report_path": "",
                    "git_baseline": {"repo_root": "/repo", "head_sha": "base123", "dirty_paths": []},
                }
                service._save_sprint_state(sprint_state)

                with (
                    patch.object(
                        service,
                        "_inspect_sprint_documentation_closeout",
                        return_value={"status": "verified", "message": "doc verified"},
                    ),
                    patch(
                        "teams_runtime.core.orchestration.inspect_sprint_closeout",
                        return_value={
                            "status": "pending_changes",
                            "representative_commit_sha": "abc123",
                            "commit_count": 1,
                            "commit_shas": ["abc123"],
                            "uncommitted_paths": ["workspace/app.py"],
                            "message": "스프린트 소유 변경 파일 중 아직 커밋되지 않은 항목이 있습니다.",
                        },
                    ),
                    patch.object(
                        service.version_controller_runtime,
                        "run_task",
                        return_value={
                            "request_id": "2026-Sprint-01-20260324T001500Z:closeout",
                            "role": "version_controller",
                            "status": "failed",
                            "summary": "git commit failed",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["sources/2026-Sprint-01-20260324T001500Z.closeout.version_control.json"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "git commit failed",
                            "commit_status": "failed",
                            "commit_sha": "",
                            "commit_paths": ["workspace/app.py"],
                            "commit_message": "[2026-Sprint-01-20260324T001500Z] chore: sprint closeout",
                            "change_detected": True,
                        },
                    ),
                    patch.object(service, "_draft_sprint_report_via_planner", AsyncMock(return_value={})),
                ):
                    asyncio.run(service._finalize_sprint(sprint_state))

                updated = json.loads(
                    service.paths.sprint_file("2026-Sprint-01-20260324T001500Z").read_text(encoding="utf-8")
                )
                self.assertEqual(updated["status"], "failed")
                self.assertEqual(updated["closeout_status"], "version_control_failed")
                self.assertEqual(updated["version_control_status"], "failed")
                self.assertEqual(updated["uncommitted_paths"], ["workspace/app.py"])
                self.assertEqual(updated["auto_commit_status"], "failed")
                self.assertEqual(updated["auto_commit_paths"], ["workspace/app.py"])
                history_text = service.paths.sprint_history_file("2026-Sprint-01-20260324T001500Z").read_text(encoding="utf-8")
                self.assertIn("closeout_status=version_control_failed", history_text)
                self.assertIn("version_control_status=failed", history_text)
                self.assertIn("uncommitted_paths=workspace/app.py", history_text)
                self.assertIn("version_control_message=[2026-Sprint-01-20260324T001500Z] chore: sprint closeout", history_text)

    def test_finalize_sprint_continues_when_runtime_files_changed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            runtime_file = Path(tmpdir) / "teams_runtime" / "core" / "orchestration.py"
            runtime_file.parent.mkdir(parents=True, exist_ok=True)
            runtime_file.write_text("value = 1\n", encoding="utf-8")
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                runtime_file.write_text("value = 2\n", encoding="utf-8")
                sprint_state = {
                    "sprint_id": "2026-Sprint-01-20260324T001530Z",
                    "status": "running",
                    "trigger": "backlog_ready",
                    "started_at": "2026-03-24T00:15:30+00:00",
                    "ended_at": "",
                    "selected_backlog_ids": [],
                    "selected_items": [],
                    "todos": [],
                    "commit_sha": "",
                    "commit_shas": [],
                    "commit_count": 0,
                    "closeout_status": "",
                    "uncommitted_paths": [],
                    "auto_commit_status": "",
                    "auto_commit_sha": "",
                    "auto_commit_paths": [],
                    "auto_commit_message": "",
                    "reload_required": False,
                    "reload_paths": [],
                    "reload_message": "",
                    "reload_restart_command": "",
                    "report_path": "",
                    "git_baseline": {"repo_root": "/repo", "head_sha": "base123", "dirty_paths": []},
                }
                service._save_scheduler_state({"active_sprint_id": sprint_state["sprint_id"]})
                service._save_sprint_state(sprint_state)

                with (
                    patch.object(
                        service,
                        "_inspect_sprint_documentation_closeout",
                        return_value={"status": "verified", "message": "doc verified"},
                    ),
                    patch(
                        "teams_runtime.core.orchestration.inspect_sprint_closeout",
                        return_value={
                            "status": "warning_missing_sprint_tag",
                            "representative_commit_sha": "warn123",
                            "commit_count": 1,
                            "commit_shas": ["warn123"],
                            "sprint_tagged_commit_count": 0,
                            "sprint_tagged_commit_shas": [],
                            "uncommitted_paths": [],
                            "message": "runtime 파일이 바뀌어도 closeout은 계속 진행합니다.",
                        },
                    ),
                    patch.object(service, "_draft_sprint_report_via_planner", AsyncMock(return_value={})),
                ):
                    asyncio.run(service._finalize_sprint(sprint_state))

                updated = json.loads(
                    service.paths.sprint_file("2026-Sprint-01-20260324T001530Z").read_text(encoding="utf-8")
                )
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(updated["closeout_status"], "warning_missing_sprint_tag")
                self.assertFalse(updated["reload_required"])
                self.assertEqual(updated["reload_paths"], [])
                self.assertEqual(updated["reload_restart_command"], "")
                scheduler_state = service._load_scheduler_state()
                self.assertEqual(str(scheduler_state.get("active_sprint_id") or ""), "")
                self.assertNotEqual(str(scheduler_state.get("last_skip_reason") or ""), "restart_required")
                history_text = service.paths.sprint_history_file("2026-Sprint-01-20260324T001530Z").read_text(encoding="utf-8")
                self.assertIn("closeout_status=warning_missing_sprint_tag", history_text)
                combined_reports = "\n".join(content for _channel_id, content in service.discord_client.sent_channels)
                self.assertIn("⚠️ 스프린트 완료(경고)", combined_reports)
