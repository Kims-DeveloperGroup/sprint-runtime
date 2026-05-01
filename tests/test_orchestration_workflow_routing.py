from orchestration_test_utils import *


class TeamsRuntimeOrchestrationWorkflowRoutingTests(OrchestrationTestCase):
    def test_workflow_transition_matrix_routes_expected_next_role_and_step(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                cases = [
                    {
                        "name": "research_initial_always_routes_to_planner_draft",
                        "request_step": "research_initial",
                        "result": self._make_workflow_result(
                            role="research",
                            summary="외부 research 필요 여부와 planner guidance를 정리했습니다.",
                            outcome="continue",
                        ),
                        "expected_next_role": "planner",
                        "expected_phase": "planning",
                        "expected_step": "planner_draft",
                        "expected_phase_owner": "planner",
                    },
                    {
                        "name": "planner_draft_opens_designer_advisory",
                        "request_step": "planner_draft",
                        "result": self._make_workflow_result(
                            role="planner",
                            summary="designer advisory가 필요합니다.",
                            outcome="continue",
                            target_phase="planning",
                            target_step="designer_advisory",
                        ),
                        "expected_next_role": "designer",
                        "expected_phase": "planning",
                        "expected_step": "designer_advisory",
                        "expected_phase_owner": "designer",
                        "expected_planning_pass_count": 1,
                    },
                    {
                        "name": "planner_draft_with_planning_artifacts_can_still_handoff_to_implementation",
                        "request_step": "planner_draft",
                        "result": self._make_workflow_result(
                            role="planner",
                            summary="spec/iteration 정리를 마쳐 implementation guidance로 넘깁니다.",
                            outcome="advance",
                            target_phase="implementation",
                            target_step="execution_ready",
                            finalize_phase=True,
                            artifacts=[
                                "./shared_workspace/sprints/demo/spec.md",
                                "./shared_workspace/sprints/demo/iteration_log.md",
                            ],
                        ),
                        "expected_next_role": "architect",
                        "expected_phase": "implementation",
                        "expected_step": "architect_guidance",
                        "expected_phase_owner": "architect",
                    },
                    {
                        "name": "planner_finalize_scope_reopen_returns_to_planner_finalize",
                        "request_step": "planner_finalize",
                        "request_kwargs": {"planning_pass_count": 1},
                        "result": self._make_workflow_result(
                            role="planner",
                            summary="scope 재정의가 필요해 planner finalize로 되돌립니다.",
                            outcome="reopen",
                            target_phase="planning",
                            target_step="planner_finalize",
                            reopen_category="scope",
                        ),
                        "expected_next_role": "planner",
                        "expected_phase": "planning",
                        "expected_step": "planner_finalize",
                        "expected_phase_owner": "planner",
                        "expected_reopen_category": "scope",
                    },
                    {
                        "name": "architect_guidance_advances_to_developer_build",
                        "request_step": "architect_guidance",
                        "result": self._make_workflow_result(
                            role="architect",
                            summary="developer 구현을 시작합니다.",
                            outcome="advance",
                            target_phase="implementation",
                            target_step="developer_build",
                        ),
                        "expected_next_role": "developer",
                        "expected_phase": "implementation",
                        "expected_step": "developer_build",
                        "expected_phase_owner": "developer",
                    },
                    {
                        "name": "architect_guidance_architecture_reopen_stays_with_architect_guidance",
                        "request_step": "architect_guidance",
                        "result": self._make_workflow_result(
                            role="architect",
                            summary="architecture contract를 다시 정리합니다.",
                            outcome="reopen",
                            target_phase="planning",
                            target_step="planner_finalize",
                            reopen_category="architecture",
                        ),
                        "expected_next_role": "architect",
                        "expected_phase": "implementation",
                        "expected_step": "architect_guidance",
                        "expected_phase_owner": "architect",
                        "expected_reopen_category": "architecture",
                    },
                    {
                        "name": "developer_build_advances_to_architect_review",
                        "request_step": "developer_build",
                        "result": self._make_workflow_result(
                            role="developer",
                            summary="architect review로 넘깁니다.",
                            outcome="advance",
                            target_phase="implementation",
                            target_step="architect_review",
                        ),
                        "expected_next_role": "architect",
                        "expected_phase": "implementation",
                        "expected_step": "architect_review",
                        "expected_phase_owner": "architect",
                        "expected_review_cycle_count": 1,
                    },
                    {
                        "name": "developer_build_scope_reopen_returns_to_planner_finalize",
                        "request_step": "developer_build",
                        "result": self._make_workflow_result(
                            role="developer",
                            summary="scope mismatch라 planner realignment가 필요합니다.",
                            outcome="reopen",
                            target_phase="planning",
                            target_step="planner_finalize",
                            reopen_category="scope",
                        ),
                        "expected_next_role": "planner",
                        "expected_phase": "planning",
                        "expected_step": "planner_finalize",
                        "expected_phase_owner": "planner",
                        "expected_reopen_category": "scope",
                    },
                    {
                        "name": "architect_review_defaults_to_developer_revision",
                        "request_step": "architect_review",
                        "request_kwargs": {"review_cycle_count": 1},
                        "result": self._make_workflow_result(
                            role="architect",
                            summary="review findings를 developer가 반영해야 합니다.",
                            outcome="advance",
                            target_phase="implementation",
                            target_step="developer_revision",
                        ),
                        "expected_next_role": "developer",
                        "expected_phase": "implementation",
                        "expected_step": "developer_revision",
                        "expected_phase_owner": "developer",
                        "expected_review_cycle_count": 1,
                    },
                    {
                        "name": "architect_review_can_handoff_directly_to_qa",
                        "request_step": "architect_review",
                        "request_kwargs": {"review_cycle_count": 1},
                        "result": self._make_workflow_result(
                            role="architect",
                            summary="QA 검증으로 넘깁니다.",
                            outcome="advance",
                            target_phase="validation",
                            target_step="qa_validation",
                        ),
                        "expected_next_role": "qa",
                        "expected_phase": "validation",
                        "expected_step": "qa_validation",
                        "expected_phase_owner": "qa",
                        "expected_review_cycle_count": 1,
                    },
                    {
                        "name": "architect_review_scope_reopen_returns_to_planner_finalize",
                        "request_step": "architect_review",
                        "request_kwargs": {"review_cycle_count": 1},
                        "result": self._make_workflow_result(
                            role="architect",
                            summary="scope contract를 planner가 다시 정리해야 합니다.",
                            outcome="reopen",
                            target_phase="planning",
                            target_step="planner_finalize",
                            reopen_category="scope",
                        ),
                        "expected_next_role": "planner",
                        "expected_phase": "planning",
                        "expected_step": "planner_finalize",
                        "expected_phase_owner": "planner",
                        "expected_reopen_category": "scope",
                    },
                    {
                        "name": "architect_review_implementation_reopen_returns_to_developer_revision",
                        "request_step": "architect_review",
                        "request_kwargs": {"review_cycle_count": 1},
                        "result": self._make_workflow_result(
                            role="architect",
                            summary="implementation 수정이 더 필요합니다.",
                            outcome="reopen",
                            target_phase="implementation",
                            target_step="developer_revision",
                            reopen_category="implementation",
                        ),
                        "expected_next_role": "developer",
                        "expected_phase": "implementation",
                        "expected_step": "developer_revision",
                        "expected_phase_owner": "developer",
                        "expected_reopen_category": "implementation",
                    },
                    {
                        "name": "developer_revision_can_request_architect_rereview",
                        "request_step": "developer_revision",
                        "request_kwargs": {"review_cycle_count": 1},
                        "result": self._make_workflow_result(
                            role="developer",
                            summary="architect re-review를 요청합니다.",
                            outcome="advance",
                            target_phase="implementation",
                            target_step="architect_review",
                        ),
                        "expected_next_role": "architect",
                        "expected_phase": "implementation",
                        "expected_step": "architect_review",
                        "expected_phase_owner": "architect",
                        "expected_review_cycle_count": 2,
                    },
                    {
                        "name": "developer_revision_defaults_to_qa_validation",
                        "request_step": "developer_revision",
                        "request_kwargs": {"review_cycle_count": 1},
                        "result": self._make_workflow_result(
                            role="developer",
                            summary="developer revision이 끝나 QA로 넘깁니다.",
                            outcome="advance",
                            target_phase="validation",
                        ),
                        "expected_next_role": "qa",
                        "expected_phase": "validation",
                        "expected_step": "qa_validation",
                        "expected_phase_owner": "qa",
                        "expected_review_cycle_count": 1,
                    },
                    {
                        "name": "qa_validation_ux_reopen_opens_designer_advisory",
                        "request_step": "qa_validation",
                        "result": self._make_workflow_result(
                            role="qa",
                            summary="UX spec mismatch가 있어 designer advisory가 필요합니다.",
                            outcome="reopen",
                            target_phase="planning",
                            target_step="designer_advisory",
                            reopen_category="ux",
                        ),
                        "expected_next_role": "designer",
                        "expected_phase": "planning",
                        "expected_step": "designer_advisory",
                        "expected_phase_owner": "designer",
                        "expected_reopen_category": "ux",
                        "expected_planning_pass_count": 1,
                    },
                    {
                        "name": "qa_validation_verification_reopen_returns_to_developer_revision",
                        "request_step": "qa_validation",
                        "result": self._make_workflow_result(
                            role="qa",
                            summary="verification mismatch를 developer가 수정해야 합니다.",
                            outcome="reopen",
                            target_phase="implementation",
                            target_step="developer_revision",
                            reopen_category="verification",
                        ),
                        "expected_next_role": "developer",
                        "expected_phase": "implementation",
                        "expected_step": "developer_revision",
                        "expected_phase_owner": "developer",
                        "expected_reopen_category": "verification",
                    },
                    {
                        "name": "qa_validation_scope_reopen_returns_to_planner_finalize",
                        "request_step": "qa_validation",
                        "result": self._make_workflow_result(
                            role="qa",
                            summary="spec/todo scope가 달라 planner realignment가 필요합니다.",
                            outcome="reopen",
                            target_phase="planning",
                            target_step="planner_finalize",
                            reopen_category="scope",
                        ),
                        "expected_next_role": "planner",
                        "expected_phase": "planning",
                        "expected_step": "planner_finalize",
                        "expected_phase_owner": "planner",
                        "expected_reopen_category": "scope",
                    },
                ]

                for case in cases:
                    with self.subTest(case=case["name"]):
                        request_record = self._make_workflow_request_record(
                            step=case["request_step"],
                            **dict(case.get("request_kwargs") or {}),
                        )
                        decision = service._derive_workflow_routing_decision(
                            request_record,
                            case["result"],
                            sender_role=str(case["result"]["role"]),
                        )

                        self.assertIsNotNone(decision)
                        self.assertEqual(decision.get("next_role"), case["expected_next_role"])
                        self.assertEqual(str(decision.get("terminal_status") or ""), "")

                        workflow_state = dict(decision.get("workflow_state") or {})
                        self.assertEqual(workflow_state["phase"], case["expected_phase"])
                        self.assertEqual(workflow_state["step"], case["expected_step"])
                        self.assertEqual(workflow_state["phase_owner"], case["expected_phase_owner"])

                        if "expected_reopen_category" in case:
                            self.assertEqual(workflow_state["reopen_category"], case["expected_reopen_category"])
                        if "expected_planning_pass_count" in case:
                            self.assertEqual(
                                workflow_state["planning_pass_count"],
                                case["expected_planning_pass_count"],
                            )
                        if "expected_review_cycle_count" in case:
                            self.assertEqual(
                                workflow_state["review_cycle_count"],
                                case["expected_review_cycle_count"],
                            )

    def test_workflow_transition_matrix_preserves_terminal_closeout_and_limit_blocks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                cases = [
                    {
                        "name": "planner_draft_complete_with_doc_only_contract_closes_in_planning",
                        "request": self._make_workflow_request_record(step="planner_draft"),
                        "result": self._make_workflow_result(
                            role="planner",
                            summary="planner 문서 계약만 정리하고 planning에서 닫습니다.",
                            outcome="complete",
                            finalize_phase=True,
                            artifacts=[
                                "./shared_workspace/current_sprint.md",
                                "./shared_workspace/sprints/demo/spec.md",
                            ],
                            extra_proposals={"planning_note": {}},
                        ),
                        "expected_phase": "closeout",
                        "expected_step": "closeout",
                        "expected_phase_owner": "version_controller",
                        "expected_phase_status": "completed",
                        "expected_terminal_status": "",
                    },
                    {
                        "name": "architect_review_explicit_continuation_blocks_at_review_limit",
                        "request": self._make_workflow_request_record(
                            step="architect_review",
                            review_cycle_count=3,
                            review_cycle_limit=3,
                        ),
                        "result": self._make_workflow_result(
                            role="architect",
                            summary="review cycle limit에 도달해 더 이상 revision loop를 열 수 없습니다.",
                            outcome="advance",
                            target_phase="implementation",
                            target_step="developer_revision",
                        ),
                        "expected_phase": "implementation",
                        "expected_step": "architect_review",
                        "expected_phase_owner": "architect",
                        "expected_phase_status": "blocked",
                        "expected_reopen_category": "implementation",
                        "expected_terminal_status": "blocked",
                    },
                    {
                        "name": "qa_validation_complete_closes_to_closeout",
                        "request": self._make_workflow_request_record(step="qa_validation"),
                        "result": self._make_workflow_result(
                            role="qa",
                            summary="QA 검증이 끝나 closeout으로 진행합니다.",
                            outcome="complete",
                            target_phase="validation",
                            target_step="qa_validation",
                        ),
                        "expected_phase": "closeout",
                        "expected_step": "closeout",
                        "expected_phase_owner": "version_controller",
                        "expected_phase_status": "completed",
                        "expected_terminal_status": "",
                    },
                ]

                for case in cases:
                    with self.subTest(case=case["name"]):
                        decision = service._derive_workflow_routing_decision(
                            case["request"],
                            case["result"],
                            sender_role=str(case["result"]["role"]),
                        )

                        self.assertIsNotNone(decision)
                        self.assertEqual(
                            str(decision.get("terminal_status") or ""),
                            case["expected_terminal_status"],
                        )
                        self.assertEqual(decision.get("next_role", ""), "")

                        workflow_state = dict(decision.get("workflow_state") or {})
                        self.assertEqual(workflow_state["phase"], case["expected_phase"])
                        self.assertEqual(workflow_state["step"], case["expected_step"])
                        self.assertEqual(workflow_state["phase_owner"], case["expected_phase_owner"])
                        self.assertEqual(workflow_state["phase_status"], case["expected_phase_status"])

                        if "expected_reopen_category" in case:
                            self.assertEqual(workflow_state["reopen_category"], case["expected_reopen_category"])

    def test_internal_sprint_planner_finalization_routes_to_architect_guidance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-planner-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow planner finalize",
                    "body": "workflow planner finalize",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "planning",
                            "step": "planner_finalize",
                            "phase_owner": "planner",
                            "phase_status": "finalizing",
                            "planning_pass_count": 1,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 0,
                        },
                    },
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-planner-finalize",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "architect", "planner"],
                }
                service._save_request(request_record)
                service.paths.current_sprint_file.write_text("# current sprint\n", encoding="utf-8")

                message = DiscordMessage(
                    message_id="relay-workflow-planner-1",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id=service.discord_config.get_role("planner").bot_id,
                    author_name="planner",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=request_record["request_id"],
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope=request_record["scope"],
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": request_record["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "planning을 마쳐 implementation guidance로 넘깁니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "architect_guidance",
                                    "reopen_category": "",
                                    "reason": "implementation guidance가 필요합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": True,
                                }
                            },
                            "artifacts": ["./shared_workspace/current_sprint.md"],
                            "next_role": "",
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "architect")
                self.assertEqual(updated["next_role"], "architect")
                self.assertEqual(updated["params"]["workflow"]["phase"], "implementation")
                self.assertEqual(updated["params"]["workflow"]["step"], "architect_guidance")

    def test_internal_sprint_planner_can_request_designer_advisory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-planner-designer-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow planner designer advisory",
                    "body": "workflow planner designer advisory",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "planning",
                            "step": "planner_draft",
                            "phase_owner": "planner",
                            "phase_status": "active",
                            "planning_pass_count": 0,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 0,
                        },
                    },
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-planner-designer-advisory",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner"],
                }
                service._save_request(request_record)
                service.paths.current_sprint_file.write_text("# current sprint\n", encoding="utf-8")

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-workflow-planner-designer-1",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role("planner").bot_id,
                            author_name="planner",
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender="planner",
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=request_record["scope"],
                            params={
                                "_teams_kind": "report",
                                "result": {
                                    "request_id": request_record["request_id"],
                                    "role": "planner",
                                    "status": "completed",
                                    "summary": "message readability 판단이 필요해 designer advisory를 엽니다.",
                                    "insights": [],
                                    "proposals": {
                                        "workflow_transition": {
                                            "outcome": "continue",
                                            "target_phase": "planning",
                                            "target_step": "designer_advisory",
                                            "reopen_category": "",
                                            "reason": "사용자 노출 메시지의 정보 우선순위를 designer가 점검해야 합니다.",
                                            "unresolved_items": ["알림 메시지 readability"],
                                            "finalize_phase": False,
                                        }
                                    },
                                    "artifacts": ["./shared_workspace/current_sprint.md"],
                                    "next_role": "",
                                    "error": "",
                                },
                            },
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "designer")
                self.assertEqual(updated["next_role"], "designer")
                self.assertEqual(updated["params"]["workflow"]["phase"], "planning")
                self.assertEqual(updated["params"]["workflow"]["step"], "designer_advisory")
                self.assertEqual(updated["params"]["workflow"]["planning_pass_count"], 1)

    def test_internal_sprint_designer_advisory_routes_back_to_planner_finalize(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-designer-finalize-1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "workflow designer finalize",
                    "body": "workflow designer finalize",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "planning",
                            "step": "designer_advisory",
                            "phase_owner": "designer",
                            "phase_status": "active",
                            "planning_pass_count": 1,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 0,
                        },
                    },
                    "current_role": "designer",
                    "next_role": "designer",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-designer-finalize",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "designer"],
                }
                service._save_request(request_record)

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-workflow-designer-finalize-1",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role("designer").bot_id,
                            author_name="designer",
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender="designer",
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=request_record["scope"],
                            params={
                                "_teams_kind": "report",
                                "result": {
                                    "request_id": request_record["request_id"],
                                    "role": "designer",
                                    "status": "completed",
                                    "summary": "message readability와 정보 우선순위 advisory를 정리했습니다.",
                                    "insights": [],
                                    "proposals": {
                                        "design_feedback": {
                                            "entry_point": "message_readability",
                                            "user_judgment": [
                                                "요청 배경보다 현재 상태와 다음 액션을 먼저 보여줘야 합니다.",
                                                "상태 보고는 한 줄 결론 뒤에 근거를 붙이는 편이 읽기 쉽습니다.",
                                            ],
                                            "message_priority": {
                                                "lead": "현재 상태와 다음 액션",
                                                "defer": "세부 로그와 참고 근거",
                                            },
                                            "routing_rationale": "planner가 최종 spec에 정보 우선순위를 흡수하면 implementation message contract가 안정됩니다.",
                                        },
                                        "workflow_transition": {
                                            "outcome": "advance",
                                            "target_phase": "planning",
                                            "target_step": "planner_finalize",
                                            "reopen_category": "",
                                            "reason": "designer advisory를 planner가 반영해 planning을 마무리합니다.",
                                            "unresolved_items": [],
                                            "finalize_phase": False,
                                        },
                                    },
                                    "artifacts": [],
                                    "next_role": "",
                                    "error": "",
                                },
                            },
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "planner")
                self.assertEqual(updated["next_role"], "planner")
                self.assertEqual(updated["params"]["workflow"]["phase"], "planning")
                self.assertEqual(updated["params"]["workflow"]["step"], "planner_finalize")

    def test_internal_sprint_developer_build_routes_to_architect_review_with_workflow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-dev-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow developer build",
                    "body": "workflow developer build",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "implementation",
                            "step": "developer_build",
                            "phase_owner": "developer",
                            "phase_status": "active",
                            "planning_pass_count": 1,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 0,
                        },
                    },
                    "current_role": "developer",
                    "next_role": "developer",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-dev-build",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "architect", "developer"],
                }
                service._save_request(request_record)

                message = DiscordMessage(
                    message_id="relay-workflow-dev-1",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id=service.discord_config.get_role("developer").bot_id,
                    author_name="developer",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=request_record["request_id"],
                    sender="developer",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope=request_record["scope"],
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": request_record["request_id"],
                            "role": "developer",
                            "status": "completed",
                            "summary": "구현을 마쳤고 architect review가 필요합니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "architect_review",
                                    "reopen_category": "",
                                    "reason": "architect review로 넘깁니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": ["workspace/src/example.py"],
                            "next_role": "",
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "architect")
                self.assertEqual(updated["params"]["workflow"]["step"], "architect_review")

    def test_internal_sprint_architect_review_routes_to_developer_revision_with_workflow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-review-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow architect review",
                    "body": "workflow architect review",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "implementation",
                            "step": "architect_review",
                            "phase_owner": "architect",
                            "phase_status": "active",
                            "planning_pass_count": 1,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 1,
                        },
                    },
                    "current_role": "architect",
                    "next_role": "architect",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-architect-review",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "architect", "developer", "architect"],
                }
                service._save_request(request_record)

                message = DiscordMessage(
                    message_id="relay-workflow-review-1",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id=service.discord_config.get_role("architect").bot_id,
                    author_name="architect",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=request_record["request_id"],
                    sender="architect",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope=request_record["scope"],
                    params={
                        "_teams_kind": "report",
                        "result": {
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
                                    "reopen_category": "",
                                    "reason": "review findings를 developer가 반영해야 합니다.",
                                    "unresolved_items": ["구조 리뷰 반영"],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "developer")
                self.assertEqual(updated["params"]["workflow"]["step"], "developer_revision")

    def test_internal_sprint_architect_review_blocked_status_with_transition_stays_in_workflow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-review-blocked-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow architect review blocked",
                    "body": "workflow architect review blocked",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "implementation",
                            "step": "architect_review",
                            "phase_owner": "architect",
                            "phase_status": "active",
                            "planning_pass_count": 1,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 1,
                        },
                    },
                    "current_role": "architect",
                    "next_role": "architect",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-architect-review-blocked",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "architect", "developer", "architect"],
                }
                service._save_request(request_record)

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-workflow-review-blocked-1",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role("architect").bot_id,
                            author_name="architect",
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender="architect",
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=request_record["scope"],
                            params={
                                "_teams_kind": "report",
                                "result": {
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
                                            "reopen_category": "",
                                            "reason": "review findings를 developer가 반영해야 합니다.",
                                            "unresolved_items": ["구조 리뷰 반영"],
                                            "finalize_phase": False,
                                        }
                                    },
                                    "artifacts": [],
                                    "next_role": "",
                                    "error": "review failed",
                                },
                            },
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "developer")
                self.assertEqual(updated["params"]["workflow"]["step"], "developer_revision")
                self.assertEqual(updated["result"]["status"], "completed")
                self.assertEqual(updated["result"]["error"], "")

    def test_internal_sprint_architect_review_reopen_without_category_routes_to_developer_revision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-review-reopen-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow architect review reopen",
                    "body": "workflow architect review reopen",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "implementation",
                            "step": "architect_review",
                            "phase_owner": "architect",
                            "phase_status": "active",
                            "planning_pass_count": 1,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 1,
                        },
                    },
                    "current_role": "architect",
                    "next_role": "architect",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-architect-review-reopen",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "architect", "developer", "architect"],
                }
                service._save_request(request_record)

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-workflow-review-reopen-1",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role("architect").bot_id,
                            author_name="architect",
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender="architect",
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=request_record["scope"],
                            params={
                                "_teams_kind": "report",
                                "result": {
                                    "request_id": request_record["request_id"],
                                    "role": "architect",
                                    "status": "blocked",
                                    "summary": "developer가 review findings를 반영해야 합니다.",
                                    "insights": [],
                                    "proposals": {
                                        "workflow_transition": {
                                            "outcome": "reopen",
                                            "target_phase": "implementation",
                                            "target_step": "developer_revision",
                                            "reopen_category": "",
                                            "reason": "review findings를 반영하도록 developer revision으로 되돌립니다.",
                                            "unresolved_items": ["구조 리뷰 반영"],
                                            "finalize_phase": False,
                                        }
                                    },
                                    "artifacts": [],
                                    "next_role": "",
                                    "error": "review failed",
                                },
                            },
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "developer")
                self.assertEqual(updated["params"]["workflow"]["step"], "developer_revision")

    def test_internal_sprint_architect_review_can_route_directly_to_qa(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-review-pass-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow architect review pass",
                    "body": "workflow architect review pass",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "implementation",
                            "step": "architect_review",
                            "phase_owner": "architect",
                            "phase_status": "active",
                            "planning_pass_count": 1,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 1,
                        },
                    },
                    "current_role": "architect",
                    "next_role": "architect",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-architect-review-pass",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "architect", "developer", "architect"],
                }
                service._save_request(request_record)

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-workflow-review-pass-1",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role("architect").bot_id,
                            author_name="architect",
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender="architect",
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=request_record["scope"],
                            params={
                                "_teams_kind": "report",
                                "result": {
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
                                            "reopen_category": "",
                                            "reason": "추가 developer 수정 없이 QA가 회귀를 검증합니다.",
                                            "unresolved_items": [],
                                            "finalize_phase": False,
                                        }
                                    },
                                    "artifacts": [],
                                    "next_role": "",
                                    "error": "",
                                },
                            },
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "qa")
                self.assertEqual(updated["params"]["workflow"]["phase"], "validation")
                self.assertEqual(updated["params"]["workflow"]["step"], "qa_validation")

    def test_internal_sprint_architect_review_routes_to_qa_even_at_review_cycle_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-review-pass-limit-qa-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow architect review qa handoff at limit",
                    "body": "workflow architect review qa handoff at limit",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "implementation",
                            "step": "architect_review",
                            "phase_owner": "architect",
                            "phase_status": "active",
                            "planning_pass_count": 1,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 3,
                            "review_cycle_limit": 3,
                        },
                    },
                    "current_role": "architect",
                    "next_role": "architect",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-architect-review-pass-limit-qa",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "architect", "developer", "architect", "developer", "architect"],
                }
                service._save_request(request_record)

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-workflow-review-pass-limit-qa-1",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role("architect").bot_id,
                            author_name="architect",
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender="architect",
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=request_record["scope"],
                            params={
                                "_teams_kind": "report",
                                "result": {
                                    "request_id": request_record["request_id"],
                                    "role": "architect",
                                    "status": "completed",
                                    "summary": "review limit 직전이지만 QA 검증으로 넘깁니다.",
                                    "insights": [],
                                    "proposals": {
                                        "workflow_transition": {
                                            "outcome": "advance",
                                            "target_phase": "validation",
                                            "target_step": "qa_validation",
                                            "reopen_category": "",
                                            "reason": "추가 developer 수정 없이 QA가 최종 검증합니다.",
                                            "unresolved_items": [],
                                            "finalize_phase": False,
                                        }
                                    },
                                    "artifacts": [],
                                    "next_role": "",
                                    "error": "",
                                },
                            },
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "qa")
                self.assertEqual(updated["params"]["workflow"]["phase"], "validation")
                self.assertEqual(updated["params"]["workflow"]["step"], "qa_validation")
                self.assertEqual(updated["params"]["workflow"]["review_cycle_count"], 3)

    def test_internal_sprint_qa_reopen_ux_routes_to_designer_advisory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-qa-ux-reopen-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow qa ux reopen",
                    "body": "workflow qa ux reopen",
                    "artifacts": [],
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
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 0,
                        },
                    },
                    "current_role": "qa",
                    "next_role": "qa",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-qa-ux-reopen",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "architect", "developer", "architect", "developer", "qa"],
                }
                service._save_request(request_record)

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-workflow-qa-ux-reopen-1",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role("qa").bot_id,
                            author_name="qa",
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender="qa",
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=request_record["scope"],
                            params={
                                "_teams_kind": "report",
                                "result": {
                                    "request_id": request_record["request_id"],
                                    "role": "qa",
                                    "status": "blocked",
                                    "summary": "사용자 노출 상태 메시지 구조가 어색해 UX reopen이 필요합니다.",
                                    "insights": [],
                                    "proposals": {
                                        "workflow_transition": {
                                            "outcome": "reopen",
                                            "target_phase": "planning",
                                            "target_step": "designer_advisory",
                                            "reopen_category": "ux",
                                            "reason": "status message readability를 designer가 다시 점검해야 합니다.",
                                            "unresolved_items": ["상태 보고 정보 우선순위 조정"],
                                            "finalize_phase": False,
                                        }
                                    },
                                    "artifacts": [],
                                    "next_role": "",
                                    "error": "ux validation failed",
                                },
                            },
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "designer")
                self.assertEqual(updated["next_role"], "designer")
                self.assertEqual(updated["params"]["workflow"]["phase"], "planning")
                self.assertEqual(updated["params"]["workflow"]["step"], "designer_advisory")
                self.assertEqual(updated["params"]["workflow"]["reopen_category"], "ux")

    def test_internal_sprint_qa_spec_mismatch_routes_to_planner_finalize(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-qa-spec-reopen-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow qa spec reopen",
                    "body": "workflow qa spec reopen",
                    "artifacts": [
                        "shared_workspace/current_sprint.md",
                        "shared_workspace/sprints/demo/spec.md",
                        "shared_workspace/sprints/demo/todo_backlog.md",
                        "shared_workspace/sprints/demo/iteration_log.md",
                    ],
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
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 0,
                            "review_cycle_limit": 3,
                        },
                    },
                    "current_role": "qa",
                    "next_role": "qa",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-qa-spec-reopen",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)

                with (
                    patch.object(service, "_delegate_request", new=AsyncMock(return_value=True)) as delegate_mock,
                    patch.object(service, "_reply_to_requester", new=AsyncMock(return_value=None)),
                ):
                    asyncio.run(
                        service._handle_role_report(
                            DiscordMessage(
                                message_id="relay-workflow-qa-spec-reopen-1",
                                channel_id="111111111111111111",
                                guild_id="guild-1",
                                author_id=service.discord_config.get_role("qa").bot_id,
                                author_name="qa",
                                content="relay",
                                is_dm=False,
                                mentions_bot=True,
                                created_at=datetime.now(timezone.utc),
                            ),
                            MessageEnvelope(
                                request_id=request_record["request_id"],
                                sender="qa",
                                target="orchestrator",
                                intent="report",
                                urgency="normal",
                                scope=request_record["scope"],
                                params={
                                    "_teams_kind": "report",
                                    "result": {
                                        "request_id": request_record["request_id"],
                                        "role": "qa",
                                        "status": "completed",
                                        "summary": "spec.md 기준 acceptance와 실제 결과가 어긋납니다.",
                                        "insights": ["todo_backlog와 canonical spec이 같은 정책을 가리키지 않습니다."],
                                        "proposals": {
                                            "workflow_transition": {
                                                "outcome": "reopen",
                                                "target_phase": "validation",
                                                "target_step": "",
                                                "reopen_category": "verification",
                                                "reason": "spec.md와 todo_backlog.md를 planner가 다시 정렬해야 합니다.",
                                                "unresolved_items": ["spec.md contract drift"],
                                                "finalize_phase": False,
                                            }
                                        },
                                        "artifacts": ["./shared_workspace/sprints/demo/spec.md"],
                                        "next_role": "",
                                        "approval_needed": False,
                                        "error": "",
                                    },
                                },
                            ),
                        )
                    )

                updated = service._load_request(request_record["request_id"])
                updated_workflow = dict(updated.get("params", {}).get("workflow") or {})

                delegate_mock.assert_awaited_once()
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "planner")
                self.assertEqual(updated["next_role"], "planner")
                self.assertEqual(updated_workflow["phase"], "planning")
                self.assertEqual(updated_workflow["step"], "planner_finalize")
                self.assertEqual(updated_workflow["reopen_category"], "scope")

    def test_internal_sprint_qa_current_sprint_drift_closes_out_with_runtime_sync(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-qa-current-sprint-drift-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow qa current sprint drift",
                    "body": "workflow qa current sprint drift",
                    "artifacts": [
                        "shared_workspace/current_sprint.md",
                        "shared_workspace/sprints/demo/spec.md",
                        "shared_workspace/sprints/demo/todo_backlog.md",
                    ],
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
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 0,
                            "review_cycle_limit": 3,
                        },
                    },
                    "current_role": "qa",
                    "next_role": "qa",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-qa-current-sprint-drift",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)

                with (
                    patch.object(service, "_delegate_request", new=AsyncMock(return_value=True)) as delegate_mock,
                    patch.object(service, "_reply_to_requester", new=AsyncMock(return_value=None)),
                ):
                    asyncio.run(
                        service._handle_role_report(
                            DiscordMessage(
                                message_id="relay-workflow-qa-current-sprint-drift-1",
                                channel_id="111111111111111111",
                                guild_id="guild-1",
                                author_id=service.discord_config.get_role("qa").bot_id,
                                author_name="qa",
                                content="relay",
                                is_dm=False,
                                mentions_bot=True,
                                created_at=datetime.now(timezone.utc),
                            ),
                            MessageEnvelope(
                                request_id=request_record["request_id"],
                                sender="qa",
                                target="orchestrator",
                                intent="report",
                                urgency="normal",
                                scope=request_record["scope"],
                                params={
                                    "_teams_kind": "report",
                                    "result": {
                                        "request_id": request_record["request_id"],
                                        "role": "qa",
                                        "status": "blocked",
                                        "summary": "formatters.py와 테스트는 통과했지만 current_sprint.md summary가 최신 결과와 어긋납니다.",
                                        "insights": ["current_sprint.md todo summary와 artifacts를 runtime이 다시 동기화해야 합니다."],
                                        "proposals": {
                                            "workflow_transition": {
                                                "outcome": "reopen",
                                                "target_phase": "validation",
                                                "target_step": "",
                                                "reopen_category": "verification",
                                                "reason": "planner-owned 상태 문서 sync가 필요합니다.",
                                                "unresolved_items": ["current_sprint.md sync drift"],
                                                "finalize_phase": False,
                                            }
                                        },
                                        "artifacts": ["./shared_workspace/current_sprint.md"],
                                        "next_role": "",
                                        "approval_needed": False,
                                        "error": "current_sprint.md sync needed",
                                    },
                                },
                            ),
                        )
                    )

                updated = service._load_request(request_record["request_id"])
                updated_workflow = dict(updated.get("params", {}).get("workflow") or {})

                delegate_mock.assert_not_awaited()
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(updated["current_role"], "orchestrator")
                self.assertEqual(updated["next_role"], "")
                self.assertEqual(updated_workflow["phase"], "closeout")
                self.assertEqual(updated_workflow["step"], "closeout")
                self.assertIn("runtime이 canonical request/todo state로 다시 동기화합니다", updated["result"]["summary"])

    def test_internal_sprint_architect_review_reopen_implementation_routes_to_developer_revision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-review-reopen-implementation-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow architect review reopen implementation",
                    "body": "workflow architect review reopen implementation",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "implementation",
                            "step": "architect_review",
                            "phase_owner": "architect",
                            "phase_status": "active",
                            "planning_pass_count": 1,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 1,
                        },
                    },
                    "current_role": "architect",
                    "next_role": "architect",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-architect-review-reopen-implementation",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "architect", "developer", "architect"],
                }
                service._save_request(request_record)

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-workflow-review-reopen-implementation-1",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role("architect").bot_id,
                            author_name="architect",
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender="architect",
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=request_record["scope"],
                            params={
                                "_teams_kind": "report",
                                "result": {
                                    "request_id": request_record["request_id"],
                                    "role": "architect",
                                    "status": "completed",
                                    "summary": "implementation 관점 수정이 더 필요합니다.",
                                    "insights": [],
                                    "proposals": {
                                        "workflow_transition": {
                                            "outcome": "reopen",
                                            "target_phase": "implementation",
                                            "target_step": "developer_revision",
                                            "reopen_category": "implementation",
                                            "reason": "implementation 수정은 developer revision에서 이어갑니다.",
                                            "unresolved_items": ["구조 리뷰 반영"],
                                            "finalize_phase": False,
                                        }
                                    },
                                    "artifacts": [],
                                    "next_role": "",
                                    "error": "",
                                },
                            },
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "developer")
                self.assertEqual(updated["params"]["workflow"]["step"], "developer_revision")

    def test_internal_sprint_developer_revision_can_request_architect_rereview(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-developer-rereview-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow developer revision rereview",
                    "body": "workflow developer revision rereview",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "implementation",
                            "step": "developer_revision",
                            "phase_owner": "developer",
                            "phase_status": "active",
                            "planning_pass_count": 1,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 1,
                            "review_cycle_limit": 3,
                        },
                    },
                    "current_role": "developer",
                    "next_role": "developer",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-developer-rereview",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "architect", "developer", "architect", "developer"],
                }
                service._save_request(request_record)

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-workflow-developer-rereview-1",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role("developer").bot_id,
                            author_name="developer",
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender="developer",
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=request_record["scope"],
                            params={
                                "_teams_kind": "report",
                                "result": {
                                    "request_id": request_record["request_id"],
                                    "role": "developer",
                                    "status": "completed",
                                    "summary": "수정을 마쳤고 architect 재검토가 필요합니다.",
                                    "insights": [],
                                    "proposals": {
                                        "workflow_transition": {
                                            "outcome": "advance",
                                            "target_phase": "implementation",
                                            "target_step": "architect_review",
                                            "reopen_category": "",
                                            "reason": "architect가 수정 반영을 다시 검토해야 합니다.",
                                            "unresolved_items": [],
                                            "finalize_phase": False,
                                        }
                                    },
                                    "artifacts": ["workspace/src/example.py"],
                                    "next_role": "",
                                    "error": "",
                                },
                            },
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "architect")
                self.assertEqual(updated["params"]["workflow"]["step"], "architect_review")
                self.assertEqual(updated["params"]["workflow"]["review_cycle_count"], 2)

    def test_internal_sprint_architect_review_blocks_when_review_cycle_limit_is_reached(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-review-limit-1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "workflow architect review limit",
                    "body": "workflow architect review limit",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "implementation",
                            "step": "architect_review",
                            "phase_owner": "architect",
                            "phase_status": "active",
                            "planning_pass_count": 1,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 3,
                            "review_cycle_limit": 3,
                        },
                    },
                    "current_role": "architect",
                    "next_role": "architect",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-architect-review-limit",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "architect", "developer", "architect", "developer", "architect"],
                }
                service._save_request(request_record)

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-workflow-review-limit-1",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role("architect").bot_id,
                            author_name="architect",
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender="architect",
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=request_record["scope"],
                            params={
                                "_teams_kind": "report",
                                "result": {
                                    "request_id": request_record["request_id"],
                                    "role": "architect",
                                    "status": "blocked",
                                    "summary": "세 번째 review에서도 수정이 더 필요합니다.",
                                    "insights": [],
                                    "proposals": {
                                        "workflow_transition": {
                                            "outcome": "advance",
                                            "target_phase": "implementation",
                                            "target_step": "developer_revision",
                                            "reopen_category": "",
                                            "reason": "review findings를 developer가 추가 반영해야 합니다.",
                                            "unresolved_items": ["추가 구조 수정"],
                                            "finalize_phase": False,
                                        }
                                    },
                                    "artifacts": [],
                                    "next_role": "",
                                    "error": "review failed",
                                },
                            },
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "blocked")
                self.assertEqual(updated["current_role"], "architect")
                self.assertEqual(updated["params"]["workflow"]["phase_status"], "blocked")
                self.assertEqual(updated["params"]["workflow"]["reopen_category"], "implementation")
                self.assertIn("review cycle limit 3", updated["result"]["summary"])
                self.assertEqual(updated["result"]["status"], "blocked")

    def test_internal_sprint_planning_advisory_pass_limit_blocks_extra_specialist_loop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-workflow-passlimit-1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "workflow pass limit",
                    "body": "workflow pass limit",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "planning",
                            "step": "planner_finalize",
                            "phase_owner": "planner",
                            "phase_status": "finalizing",
                            "planning_pass_count": 2,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 0,
                        },
                    },
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "workflow-pass-limit",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner", "designer", "planner", "architect", "planner"],
                }
                service._save_request(request_record)
                service.paths.current_sprint_file.write_text("# current sprint\n", encoding="utf-8")

                message = DiscordMessage(
                    message_id="relay-workflow-passlimit-1",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id=service.discord_config.get_role("planner").bot_id,
                    author_name="planner",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=request_record["request_id"],
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope=request_record["scope"],
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": request_record["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "추가 architect advisory가 더 필요합니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "continue",
                                    "target_phase": "planning",
                                    "target_step": "architect_advisory",
                                    "reopen_category": "",
                                    "reason": "추가 technical advisory가 필요합니다.",
                                    "unresolved_items": ["technical detail"],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": ["./shared_workspace/current_sprint.md"],
                            "next_role": "",
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "blocked")
                self.assertEqual(updated["params"]["workflow"]["phase_status"], "blocked")
                self.assertIn("pass 한도", updated["result"]["summary"])

    def test_internal_sprint_legacy_planner_loop_is_migrated_and_blocked_at_pass_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260401-legacy-loop-1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "legacy loop",
                    "body": "legacy loop",
                    "artifacts": [],
                    "params": {"_teams_kind": "sprint_internal"},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-Workflow",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-01T00:00:00+00:00",
                    "updated_at": "2026-04-01T00:00:00+00:00",
                    "fingerprint": "legacy-loop",
                    "reply_route": {},
                    "events": [
                        {"event_type": "role_report", "actor": "architect"},
                        {"event_type": "role_report", "actor": "architect"},
                    ],
                    "result": {},
                    "visited_roles": ["planner", "architect"],
                }
                service._save_request(request_record)
                service.paths.current_sprint_file.write_text("# current sprint\n", encoding="utf-8")

                message = DiscordMessage(
                    message_id="relay-legacy-loop-1",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id=service.discord_config.get_role("planner").bot_id,
                    author_name="planner",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=request_record["request_id"],
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope=request_record["scope"],
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": request_record["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "legacy architect pass를 한 번 더 요청합니다.",
                            "insights": [],
                            "proposals": {
                                "workflow_transition": {
                                    "outcome": "continue",
                                    "target_phase": "planning",
                                    "target_step": "planner_advisory",
                                    "requested_role": "architect",
                                    "reopen_category": "",
                                    "reason": "legacy loop를 재요청합니다.",
                                    "unresolved_items": [],
                                    "finalize_phase": False,
                                }
                            },
                            "artifacts": ["./shared_workspace/current_sprint.md"],
                            "next_role": "",
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "blocked")
                self.assertEqual(updated["params"]["workflow"]["planning_pass_count"], 2)
                self.assertEqual(updated["params"]["workflow"]["phase_status"], "blocked")

    def test_planner_finalize_closes_doc_only_execution_request_when_finalize_phase_true(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="legacy failure carry-over contract",
                    summary="suite-level 실패 4건을 후속 검증 대상으로 분리 고정합니다.",
                    kind="bug",
                    source="planner",
                    scope="current sprint planning surface에 follow-up contract만 남깁니다.",
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="planner")
                sprint_state = {
                    "sprint_id": "2026-Sprint-Planning-Closeout",
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

                request_record = service._create_internal_request_record(sprint_state, todo, backlog_item)
                request_record["intent"] = "execute"
                request_record["status"] = "delegated"
                request_record["current_role"] = "planner"
                request_record["next_role"] = "planner"
                params = dict(request_record.get("params") or {})
                params["workflow"] = {
                    "contract_version": 1,
                    "phase": "planning",
                    "step": "planner_finalize",
                    "phase_owner": "planner",
                    "phase_status": "finalizing",
                    "planning_pass_count": 1,
                    "planning_pass_limit": 2,
                    "planning_final_owner": "planner",
                    "reopen_source_role": "architect",
                    "reopen_category": "scope",
                    "review_cycle_count": 0,
                    "review_cycle_limit": 3,
                }
                request_record["params"] = params
                service._save_request(request_record)

                result = {
                    "request_id": request_record["request_id"],
                    "role": "planner",
                    "status": "completed",
                    "summary": "planner-owned current_sprint closeout을 다시 고정했습니다.",
                    "insights": [],
                    "proposals": {
                        "workflow_transition": {
                            "outcome": "complete",
                            "target_phase": "",
                            "target_step": "",
                            "reopen_category": "",
                            "reason": "planner finalize에서 planning closeout으로 닫습니다.",
                            "unresolved_items": [
                                "legacy prompt/card/image contract 실제 복구 여부는 별도 implementation decision으로 남깁니다.",
                            ],
                            "finalize_phase": True,
                        }
                    },
                    "artifacts": [
                        "./shared_workspace/current_sprint.md",
                        "./shared_workspace/sprints/2026-Sprint-Planning-Closeout/plan.md",
                    ],
                    "next_role": "",
                    "approval_needed": False,
                    "error": "",
                }

                with (
                    patch.object(service, "_delegate_request", new=AsyncMock(return_value=True)) as delegate_mock,
                    patch.object(service, "_reply_to_requester", new=AsyncMock(return_value=None)),
                ):
                    asyncio.run(
                        service._handle_role_report(
                            DiscordMessage(
                                message_id="relay-planner-finalize-closeout",
                                channel_id="111111111111111111",
                                guild_id="guild-1",
                                author_id=service.discord_config.get_role("planner").bot_id,
                                author_name="planner",
                                content="relay",
                                is_dm=False,
                                mentions_bot=True,
                                created_at=datetime.now(timezone.utc),
                            ),
                            MessageEnvelope(
                                request_id=request_record["request_id"],
                                sender="planner",
                                target="orchestrator",
                                intent="report",
                                urgency="normal",
                                scope=str(request_record.get("scope") or ""),
                                params={"_teams_kind": "report", "result": result},
                            ),
                        )
                    )

                updated = service._load_request(request_record["request_id"])
                updated_workflow = dict(updated.get("params", {}).get("workflow") or {})

                delegate_mock.assert_not_awaited()
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(updated["current_role"], "orchestrator")
                self.assertEqual(updated["next_role"], "")
                self.assertEqual(updated_workflow["phase"], "closeout")
                self.assertEqual(updated_workflow["step"], "closeout")

    def test_planner_finalize_execution_request_with_non_planning_artifact_still_routes_to_architect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="implementation follow-up still needed",
                    summary="execution 성격 요청에서 implementation artifact가 남아 있으면 architect guidance로 이어져야 합니다.",
                    kind="bug",
                    source="planner",
                    scope="planner finalize 이후에도 implementation artifact가 남아 있는지 확인합니다.",
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="planner")
                sprint_state = {
                    "sprint_id": "2026-Sprint-Planning-Continue",
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

                request_record = service._create_internal_request_record(sprint_state, todo, backlog_item)
                request_record["intent"] = "execute"
                request_record["status"] = "delegated"
                request_record["current_role"] = "planner"
                request_record["next_role"] = "planner"
                params = dict(request_record.get("params") or {})
                params["workflow"] = {
                    "contract_version": 1,
                    "phase": "planning",
                    "step": "planner_finalize",
                    "phase_owner": "planner",
                    "phase_status": "finalizing",
                    "planning_pass_count": 1,
                    "planning_pass_limit": 2,
                    "planning_final_owner": "planner",
                    "reopen_source_role": "",
                    "reopen_category": "",
                    "review_cycle_count": 0,
                    "review_cycle_limit": 3,
                }
                request_record["params"] = params
                service._save_request(request_record)

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-planner-finalize-continue",
                            channel_id="111111111111111111",
                            guild_id="guild-1",
                            author_id=service.discord_config.get_role("planner").bot_id,
                            author_name="planner",
                            content="relay",
                            is_dm=False,
                            mentions_bot=True,
                            created_at=datetime.now(timezone.utc),
                        ),
                        MessageEnvelope(
                            request_id=request_record["request_id"],
                            sender="planner",
                            target="orchestrator",
                            intent="report",
                            urgency="normal",
                            scope=str(request_record.get("scope") or ""),
                            params={
                                "_teams_kind": "report",
                                "result": {
                                    "request_id": request_record["request_id"],
                                    "role": "planner",
                                    "status": "completed",
                                    "summary": "planning 정리는 끝났지만 implementation artifact가 남아 있습니다.",
                                    "insights": [],
                                    "proposals": {
                                        "workflow_transition": {
                                            "outcome": "complete",
                                            "target_phase": "",
                                            "target_step": "",
                                            "reopen_category": "",
                                            "reason": "planner finalize를 마쳤습니다.",
                                            "unresolved_items": [],
                                            "finalize_phase": True,
                                        }
                                    },
                                    "artifacts": [
                                        "./shared_workspace/current_sprint.md",
                                        "./teams_runtime/core/orchestration.py",
                                    ],
                                    "next_role": "",
                                    "approval_needed": False,
                                    "error": "",
                                },
                            },
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                updated_workflow = dict(updated.get("params", {}).get("workflow") or {})

                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "architect")
                self.assertEqual(updated["next_role"], "architect")
                self.assertEqual(updated_workflow["phase"], "implementation")
                self.assertEqual(updated_workflow["step"], "architect_guidance")

    def test_planner_draft_with_planning_artifacts_and_implementation_transition_routes_to_architect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="implementation handoff after planner draft",
                    summary="planner draft가 spec/iteration 문서만 남겨도 implementation handoff를 요청하면 architect guidance로 이어져야 합니다.",
                    kind="feature",
                    source="planner",
                    scope="planning artifacts만 보고한 planner draft의 implementation handoff를 검증합니다.",
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="planner")
                sprint_state = {
                    "sprint_id": "2026-Sprint-Planning-Handoff",
                    "sprint_folder_name": "2026-Sprint-Planning-Handoff",
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
                artifact_paths = service._sprint_artifact_paths(sprint_state)
                artifact_paths["root"].mkdir(parents=True, exist_ok=True)
                artifact_paths["spec"].write_text("# spec\n", encoding="utf-8")
                artifact_paths["iteration_log"].write_text("# iteration\n", encoding="utf-8")

                request_record = service._create_internal_request_record(sprint_state, todo, backlog_item)
                request_record["intent"] = "execute"
                request_record["status"] = "delegated"
                request_record["current_role"] = "planner"
                request_record["next_role"] = "planner"
                params = dict(request_record.get("params") or {})
                params["workflow"] = {
                    "contract_version": 1,
                    "phase": "planning",
                    "step": "planner_draft",
                    "phase_owner": "planner",
                    "phase_status": "active",
                    "planning_pass_count": 0,
                    "planning_pass_limit": 2,
                    "planning_final_owner": "planner",
                    "reopen_source_role": "",
                    "reopen_category": "",
                    "review_cycle_count": 0,
                    "review_cycle_limit": 3,
                }
                request_record["params"] = params
                service._save_request(request_record)

                result = {
                    "request_id": request_record["request_id"],
                    "role": "planner",
                    "status": "completed",
                    "summary": "spec/iteration contract를 정리했고 implementation으로 넘길 준비를 마쳤습니다.",
                    "insights": [],
                    "proposals": {
                        "workflow_transition": {
                            "outcome": "advance",
                            "target_phase": "implementation",
                            "target_step": "execution_ready",
                            "reopen_category": "",
                            "reason": "architect guidance를 시작합니다.",
                            "unresolved_items": [],
                            "finalize_phase": True,
                        }
                    },
                    "artifacts": [
                        "./shared_workspace/sprints/2026-Sprint-Planning-Handoff/spec.md",
                        "./shared_workspace/sprints/2026-Sprint-Planning-Handoff/iteration_log.md",
                    ],
                    "next_role": "",
                    "approval_needed": False,
                    "error": "",
                }

                with (
                    patch.object(service, "_delegate_request", new=AsyncMock(return_value=True)) as delegate_mock,
                    patch.object(service, "_reply_to_requester", new=AsyncMock(return_value=None)),
                ):
                    asyncio.run(
                        service._handle_role_report(
                            DiscordMessage(
                                message_id="relay-planner-draft-implementation-handoff",
                                channel_id="111111111111111111",
                                guild_id="guild-1",
                                author_id=service.discord_config.get_role("planner").bot_id,
                                author_name="planner",
                                content="relay",
                                is_dm=False,
                                mentions_bot=True,
                                created_at=datetime.now(timezone.utc),
                            ),
                            MessageEnvelope(
                                request_id=request_record["request_id"],
                                sender="planner",
                                target="orchestrator",
                                intent="report",
                                urgency="normal",
                                scope=str(request_record.get("scope") or ""),
                                params={"_teams_kind": "report", "result": result},
                            ),
                        )
                    )

                updated = service._load_request(request_record["request_id"])
                updated_workflow = dict(updated.get("params", {}).get("workflow") or {})

                delegate_mock.assert_awaited_once()
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "architect")
                self.assertEqual(updated["next_role"], "architect")
                self.assertEqual(updated_workflow["phase"], "implementation")
                self.assertEqual(updated_workflow["step"], "architect_guidance")

    def test_planner_finalize_requires_spec_todo_iteration_docs_after_qa_reopen(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="qa reopen planner docs",
                    summary="QA reopen 이후 planner는 spec/todo/iteration/current_sprint 문서를 다시 닫아야 합니다.",
                    kind="bug",
                    source="planner",
                    scope="qa reopen planner docs",
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="planner")
                sprint_state = {
                    "sprint_id": "2026-Sprint-Planning-QA-Reopen",
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

                request_record = service._create_internal_request_record(sprint_state, todo, backlog_item)
                request_record["intent"] = "execute"
                request_record["status"] = "delegated"
                request_record["current_role"] = "planner"
                request_record["next_role"] = "planner"
                request_record["artifacts"] = [
                    "shared_workspace/current_sprint.md",
                    "shared_workspace/sprints/2026-Sprint-Planning-QA-Reopen/spec.md",
                    "shared_workspace/sprints/2026-Sprint-Planning-QA-Reopen/todo_backlog.md",
                    "shared_workspace/sprints/2026-Sprint-Planning-QA-Reopen/iteration_log.md",
                ]
                params = dict(request_record.get("params") or {})
                params["workflow"] = {
                    "contract_version": 1,
                    "phase": "planning",
                    "step": "planner_finalize",
                    "phase_owner": "planner",
                    "phase_status": "finalizing",
                    "planning_pass_count": 1,
                    "planning_pass_limit": 2,
                    "planning_final_owner": "planner",
                    "reopen_source_role": "qa",
                    "reopen_category": "scope",
                    "review_cycle_count": 0,
                    "review_cycle_limit": 3,
                }
                request_record["params"] = params
                service._save_request(request_record)

                result = {
                    "request_id": request_record["request_id"],
                    "role": "planner",
                    "status": "completed",
                    "summary": "planner가 spec만 다시 정리했습니다.",
                    "insights": [],
                    "proposals": {
                        "workflow_transition": {
                            "outcome": "advance",
                            "target_phase": "implementation",
                            "target_step": "architect_guidance",
                            "reopen_category": "",
                            "reason": "implementation으로 다시 진행합니다.",
                            "unresolved_items": [],
                            "finalize_phase": False,
                        }
                    },
                    "artifacts": [
                        "./shared_workspace/sprints/2026-Sprint-Planning-QA-Reopen/spec.md",
                    ],
                    "next_role": "",
                    "approval_needed": False,
                    "error": "",
                }

                with (
                    patch.object(service, "_delegate_request", new=AsyncMock(return_value=True)) as delegate_mock,
                    patch.object(service, "_reply_to_requester", new=AsyncMock(return_value=None)),
                ):
                    asyncio.run(
                        service._handle_role_report(
                            DiscordMessage(
                                message_id="relay-planner-finalize-qa-docs",
                                channel_id="111111111111111111",
                                guild_id="guild-1",
                                author_id=service.discord_config.get_role("planner").bot_id,
                                author_name="planner",
                                content="relay",
                                is_dm=False,
                                mentions_bot=True,
                                created_at=datetime.now(timezone.utc),
                            ),
                            MessageEnvelope(
                                request_id=request_record["request_id"],
                                sender="planner",
                                target="orchestrator",
                                intent="report",
                                urgency="normal",
                                scope=str(request_record.get("scope") or ""),
                                params={"_teams_kind": "report", "result": result},
                            ),
                        )
                    )

                updated = service._load_request(request_record["request_id"])
                updated_workflow = dict(updated.get("params", {}).get("workflow") or {})
                role_report_events = [
                    event
                    for event in (updated.get("events") or [])
                    if str(event.get("type") or "").strip() == "role_report"
                ]

                delegate_mock.assert_awaited_once()
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "planner")
                self.assertEqual(updated["next_role"], "planner")
                self.assertEqual(updated_workflow["phase"], "planning")
                self.assertEqual(updated_workflow["step"], "planner_finalize")
                self.assertTrue(role_report_events)
                self.assertEqual(role_report_events[-1]["payload"]["status"], "blocked")
                self.assertIn("planner 문서 계약", role_report_events[-1]["payload"]["summary"])

    def test_planner_finalize_accepts_prefixed_required_docs_after_qa_reopen(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="qa reopen planner docs normalized",
                    summary="QA reopen 이후 planner가 `./shared_workspace/...` 경로로 문서를 보고해도 closeout으로 닫혀야 합니다.",
                    kind="bug",
                    source="planner",
                    scope="qa reopen planner docs normalized",
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="planner")
                sprint_state = {
                    "sprint_id": "2026-Sprint-Planning-QA-Reopen-Normalized",
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
                artifact_paths = service._sprint_artifact_paths(sprint_state)
                artifact_paths["root"].mkdir(parents=True, exist_ok=True)
                artifact_paths["spec"].write_text("# spec\n", encoding="utf-8")
                artifact_paths["todo_backlog"].write_text("# todo\n", encoding="utf-8")
                artifact_paths["iteration_log"].write_text("# iteration\n", encoding="utf-8")
                service.paths.current_sprint_file.write_text("# current\n", encoding="utf-8")

                request_record = service._create_internal_request_record(sprint_state, todo, backlog_item)
                request_record["intent"] = "execute"
                request_record["status"] = "delegated"
                request_record["current_role"] = "planner"
                request_record["next_role"] = "planner"
                request_record["artifacts"] = [
                    "shared_workspace/current_sprint.md",
                    "shared_workspace/sprints/2026-Sprint-Planning-QA-Reopen-Normalized/spec.md",
                    "shared_workspace/sprints/2026-Sprint-Planning-QA-Reopen-Normalized/todo_backlog.md",
                    "shared_workspace/sprints/2026-Sprint-Planning-QA-Reopen-Normalized/iteration_log.md",
                ]
                params = dict(request_record.get("params") or {})
                params["workflow"] = {
                    "contract_version": 1,
                    "phase": "planning",
                    "step": "planner_finalize",
                    "phase_owner": "planner",
                    "phase_status": "finalizing",
                    "planning_pass_count": 1,
                    "planning_pass_limit": 2,
                    "planning_final_owner": "planner",
                    "reopen_source_role": "qa",
                    "reopen_category": "scope",
                    "review_cycle_count": 0,
                    "review_cycle_limit": 3,
                }
                request_record["params"] = params
                service._save_request(request_record)

                result = {
                    "request_id": request_record["request_id"],
                    "role": "planner",
                    "status": "completed",
                    "summary": "planner가 QA reopen 문서를 모두 다시 정리했습니다.",
                    "insights": [],
                    "proposals": {
                        "workflow_transition": {
                            "outcome": "complete",
                            "target_phase": "",
                            "target_step": "",
                            "reopen_category": "",
                            "reason": "planner finalize를 마쳤습니다.",
                            "unresolved_items": [],
                            "finalize_phase": True,
                        }
                    },
                    "artifacts": [
                        "./shared_workspace/current_sprint.md",
                        "./shared_workspace/sprints/2026-Sprint-Planning-QA-Reopen-Normalized/spec.md",
                        "./shared_workspace/sprints/2026-Sprint-Planning-QA-Reopen-Normalized/todo_backlog.md",
                        "./shared_workspace/sprints/2026-Sprint-Planning-QA-Reopen-Normalized/iteration_log.md",
                    ],
                    "next_role": "",
                    "approval_needed": False,
                    "error": "",
                }

                with (
                    patch.object(service, "_delegate_request", new=AsyncMock(return_value=True)) as delegate_mock,
                    patch.object(service, "_reply_to_requester", new=AsyncMock(return_value=None)),
                ):
                    asyncio.run(
                        service._handle_role_report(
                            DiscordMessage(
                                message_id="relay-planner-finalize-qa-docs-normalized",
                                channel_id="111111111111111111",
                                guild_id="guild-1",
                                author_id=service.discord_config.get_role("planner").bot_id,
                                author_name="planner",
                                content="relay",
                                is_dm=False,
                                mentions_bot=True,
                                created_at=datetime.now(timezone.utc),
                            ),
                            MessageEnvelope(
                                request_id=request_record["request_id"],
                                sender="planner",
                                target="orchestrator",
                                intent="report",
                                urgency="normal",
                                scope=str(request_record.get("scope") or ""),
                                params={"_teams_kind": "report", "result": result},
                            ),
                        )
                    )

                updated = service._load_request(request_record["request_id"])
                updated_workflow = dict(updated.get("params", {}).get("workflow") or {})

                delegate_mock.assert_not_awaited()
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(updated["current_role"], "orchestrator")
                self.assertEqual(updated["next_role"], "")
                self.assertEqual(updated_workflow["phase"], "closeout")
                self.assertEqual(updated_workflow["step"], "closeout")

    def test_workflow_sanitizes_planner_owned_docs_from_implementation_artifacts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="planner-owned doc sanitize guard",
                    summary="implementation 역할이 planner-owned 문서를 claim해도 runtime이 artifact를 정리해야 합니다.",
                    kind="bug",
                    source="planner",
                    scope="planner-owned sprint docs를 implementation artifact에서 제외한다.",
                )
                service._save_backlog_item(backlog_item)
                todo = build_todo_item(backlog_item, owner_role="planner")
                sprint_state = {
                    "sprint_id": "2026-Sprint-Guardrail",
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

                request_record = service._create_internal_request_record(sprint_state, todo, backlog_item)
                request_record["status"] = "delegated"
                request_record["current_role"] = "developer"
                request_record["next_role"] = "developer"
                params = dict(request_record.get("params") or {})
                params["workflow"] = {
                    "contract_version": 1,
                    "phase": "implementation",
                    "step": "developer_build",
                    "phase_owner": "developer",
                    "phase_status": "active",
                    "planning_pass_count": 0,
                    "planning_pass_limit": 2,
                    "planning_final_owner": "planner",
                    "reopen_source_role": "",
                    "reopen_category": "",
                    "review_cycle_count": 0,
                    "review_cycle_limit": 3,
                }
                request_record["params"] = params
                service._save_request(request_record)

                result = {
                    "request_id": request_record["request_id"],
                    "role": "developer",
                    "status": "completed",
                    "summary": "todo_backlog와 iteration_log를 반영해 구현을 마쳤습니다.",
                    "insights": [],
                    "proposals": {
                        "workflow_transition": {
                            "outcome": "advance",
                            "target_phase": "implementation",
                            "target_step": "architect_review",
                            "reopen_category": "",
                            "reason": "architect review를 진행합니다.",
                            "unresolved_items": [],
                            "finalize_phase": False,
                        }
                    },
                    "artifacts": [
                        "./shared_workspace/sprints/demo/todo_backlog.md",
                        "./shared_workspace/sprints/demo/iteration_log.md",
                    ],
                    "next_role": "",
                    "approval_needed": False,
                    "error": "",
                }

                with (
                    patch.object(service, "_delegate_request", new=AsyncMock(return_value=True)) as delegate_mock,
                    patch.object(service, "_reply_to_requester", new=AsyncMock(return_value=None)),
                ):
                    asyncio.run(
                        service._handle_role_report(
                            DiscordMessage(
                                message_id="relay-dev-guard",
                                channel_id="111111111111111111",
                                guild_id="guild-1",
                                author_id=service.discord_config.get_role("developer").bot_id,
                                author_name="developer",
                                content="relay",
                                is_dm=False,
                                mentions_bot=True,
                                created_at=datetime.now(timezone.utc),
                            ),
                            MessageEnvelope(
                                request_id=request_record["request_id"],
                                sender="developer",
                                target="orchestrator",
                                intent="report",
                                urgency="normal",
                                scope=str(request_record.get("scope") or ""),
                                params={"_teams_kind": "report", "result": result},
                            ),
                        )
                    )

                updated = service._load_request(request_record["request_id"])
                updated_workflow = dict(updated.get("params", {}).get("workflow") or {})
                role_report_events = [
                    event
                    for event in (updated.get("events") or [])
                    if str(event.get("type") or "").strip() == "role_report"
                ]

                delegate_mock.assert_awaited_once()
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "architect")
                self.assertEqual(updated["next_role"], "architect")
                self.assertEqual(updated_workflow["phase"], "implementation")
                self.assertEqual(updated_workflow["step"], "architect_review")
                self.assertEqual(updated["result"].get("artifacts") or [], [])
                self.assertIn(
                    "runtime이 implementation artifact에서 planner-owned 문서를 제외했습니다",
                    " ".join(str(item) for item in (updated["result"].get("insights") or [])),
                )
                self.assertTrue(role_report_events)
                self.assertEqual(role_report_events[-1]["payload"]["status"], "completed")

    def test_internal_sprint_planner_role_report_syncs_sprint_artifacts_before_chain_completion(self):
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

                backlog_item = build_backlog_item(
                    title="planner synced todo",
                    summary="planner 결과 직후 sprint artifacts를 동기화합니다.",
                    kind="feature",
                    source="planner",
                    scope="planner synced todo",
                    milestone_title=sprint_state["milestone_title"],
                    priority_rank=2,
                )
                backlog_item["status"] = "selected"
                backlog_item["selected_in_sprint_id"] = sprint_state["sprint_id"]
                service._save_backlog_item(backlog_item)

                request_record = service._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=1,
                )
                request_record["status"] = "delegated"
                request_record["current_role"] = "planner"
                request_record["next_role"] = "planner"
                service._save_request(request_record)

                planner_result = {
                    "request_id": request_record["request_id"],
                    "role": "planner",
                    "status": "completed",
                    "summary": "planner가 sprint plan/spec/todo를 정리했습니다.",
                    "insights": ["sprint docs를 role report 시점에도 바로 동기화합니다."],
                    "proposals": {
                        "backlog_items": [
                            {
                                "backlog_id": backlog_item["backlog_id"],
                                "title": backlog_item["title"],
                                "summary": backlog_item["summary"],
                                "scope": backlog_item["scope"],
                                "kind": backlog_item["kind"],
                                "priority_rank": backlog_item["priority_rank"],
                                "milestone_title": sprint_state["milestone_title"],
                            }
                        ]
                    },
                    "artifacts": [],
                    "next_role": "designer",
                    "approval_needed": False,
                    "error": "",
                }
                message = DiscordMessage(
                    message_id="relay-sprint-planner-sync",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id=service.discord_config.get_role("planner").bot_id,
                    author_name="planner",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=request_record["request_id"],
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope=request_record["scope"],
                    params={"_teams_kind": "report", "result": planner_result},
                )

                delegate_mock = AsyncMock(return_value=True)
                reply_mock = AsyncMock()
                with (
                    patch.object(service, "_delegate_request", delegate_mock),
                    patch.object(service, "_reply_to_requester", reply_mock),
                ):
                    asyncio.run(service._handle_role_report(message, envelope))

                updated_sprint_state = service._load_sprint_state(sprint_state["sprint_id"])
                updated_request = service._load_request(request_record["request_id"])
                self.assertEqual(
                    [item["title"] for item in updated_sprint_state["selected_items"]],
                    ["planner synced todo"],
                )
                self.assertEqual(
                    len(updated_sprint_state["planning_iterations"]),
                    1,
                )
                self.assertEqual(
                    updated_sprint_state["planning_iterations"][0]["request_id"],
                    request_record["request_id"],
                )
                artifact_paths = service._sprint_artifact_paths(updated_sprint_state)
                self.assertIn(
                    "planner가 sprint plan/spec/todo를 정리했습니다.",
                    artifact_paths["plan"].read_text(encoding="utf-8"),
                )
                self.assertIn(
                    "sprint docs를 role report 시점에도 바로 동기화합니다.",
                    artifact_paths["spec"].read_text(encoding="utf-8"),
                )
                todo_backlog_text = artifact_paths["todo_backlog"].read_text(encoding="utf-8")
                self.assertIn("planner synced todo", todo_backlog_text)
                self.assertNotIn("selected backlog 없음", todo_backlog_text)
                self.assertIn(
                    request_record["request_id"],
                    artifact_paths["iteration_log"].read_text(encoding="utf-8"),
                )
                self.assertEqual(updated_request["status"], "completed")
                delegate_mock.assert_not_called()
                reply_mock.assert_awaited()
