from orchestration_test_utils import *


class TeamsRuntimeOrchestrationIntakeRoutingTests(OrchestrationTestCase):
    def test_orchestrator_routes_plan_request_to_planner_for_user_dm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                planner_bot_id = service.discord_config.get_role("planner").bot_id
                observed: dict[str, str] = {}

                def fake_orchestrator_run_task(envelope, request_record):
                    observed["incoming_intent"] = str(envelope.intent or "")
                    observed["incoming_scope"] = str(request_record.get("scope") or "")
                    observed["incoming_body"] = str(request_record.get("body") or "")
                    observed["request_intent"] = str(request_record.get("intent") or "")
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "planner 검토가 필요한 planning 요청으로 정리했습니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": [],
                        "error": "",
                    }

                message = DiscordMessage(
                    message_id="msg-1",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="intent: plan\nscope: first task",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with (
                    patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task),
                    patch.object(
                        service,
                        "_derive_routing_decision_after_report",
                        return_value={
                            "next_role": "planner",
                            "routing_context": {
                                "selected_role": "planner",
                                "reason": "Selected planner for planning-first follow-up.",
                            },
                        },
                    ),
                ):
                    asyncio.run(service.handle_message(message))

                self.assertEqual(len(service.discord_client.sent_dms), 2)
                self.assertEqual(service.discord_client.sent_dms[0], ("user-1", "수신양호"))
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                self.assertIn("진행 중", service.discord_client.sent_dms[1][1])
                self.assertIn("planner 역할로 전달했습니다.", service.discord_client.sent_dms[1][1])
                _channel_id, relay_content = service.discord_client.sent_channels[0]
                self.assertIn(f"<@{planner_bot_id}>", relay_content)
                self.assertIn("intent: plan", relay_content)
                self.assertEqual(observed["incoming_intent"], "route")
                self.assertEqual(observed["incoming_body"], "intent: plan\nscope: first task")
                self.assertEqual(observed["request_intent"], "route")
                request_files = list(service.paths.requests_dir.glob("*.json"))
                self.assertEqual(len(request_files), 1)
                request_payload = json.loads(request_files[0].read_text(encoding="utf-8"))
                self.assertEqual(request_payload["status"], "delegated")
                self.assertEqual(request_payload["current_role"], "planner")
                self.assertEqual(request_payload["next_role"], "planner")
                self.assertEqual(list(service.paths.backlog_dir.glob("*.json")), [])

    def test_orchestrator_seeds_standard_user_request_to_research_workflow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                research_bot_id = service.discord_config.get_role("research").bot_id

                def fake_orchestrator_run_task(_envelope, request_record):
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "즉시 답변보다 research-preplanning이 적합한 작업 요청입니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": [],
                        "error": "",
                    }

                message = DiscordMessage(
                    message_id="msg-research-seed",
                    channel_id="dm-research-seed",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="Add a new workflow role and plan the change.",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service.handle_message(message))

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, relay_content = service.discord_client.sent_channels[0]
                self.assertIn(f"<@{research_bot_id}>", relay_content)
                request_payload = json.loads(next(service.paths.requests_dir.glob("*.json")).read_text(encoding="utf-8"))
                self.assertEqual(request_payload["status"], "delegated")
                self.assertEqual(request_payload["current_role"], "research")
                self.assertEqual(request_payload["next_role"], "research")
                self.assertEqual(request_payload["params"]["workflow"]["step"], "research_initial")
                self.assertEqual(request_payload["params"]["workflow"]["phase_owner"], "research")

    def test_orchestrator_routes_planning_request_to_planner_before_backlog_creation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                planner_bot_id = service.discord_config.get_role("planner").bot_id

                def fake_orchestrator_run_task(envelope, request_record):
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "planner가 먼저 정리해야 하는 planning/backlog 요청입니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": [],
                        "error": "",
                    }

                message = DiscordMessage(
                    message_id="msg-plan-1",
                    channel_id="dm-plan-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="intent: route\nscope: planning\n기획 문서와 백로그 정리 필요",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with (
                    patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task),
                    patch.object(
                        service,
                        "_derive_routing_decision_after_report",
                        return_value={
                            "next_role": "planner",
                            "routing_context": {
                                "selected_role": "planner",
                                "reason": "Selected planner for backlog shaping.",
                            },
                        },
                    ),
                ):
                    asyncio.run(service.handle_message(message))

                self.assertEqual(len(service.discord_client.sent_dms), 2)
                self.assertEqual(service.discord_client.sent_dms[0], ("user-1", "수신양호"))
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                self.assertIn("진행 중", service.discord_client.sent_dms[1][1])
                self.assertIn("planner 역할로 전달했습니다.", service.discord_client.sent_dms[1][1])
                _channel_id, relay_content = service.discord_client.sent_channels[0]
                self.assertIn(f"<@{planner_bot_id}>", relay_content)
                self.assertIn("intent: plan", relay_content)
                request_files = list(service.paths.requests_dir.glob("*.json"))
                self.assertEqual(len(request_files), 1)
                request_payload = json.loads(request_files[0].read_text(encoding="utf-8"))
                self.assertEqual(request_payload["status"], "delegated")
                self.assertEqual(request_payload["current_role"], "planner")
                self.assertEqual(request_payload["next_role"], "planner")
                self.assertEqual(list(service.paths.backlog_dir.glob("*.json")), [])

    def test_orchestrator_reuses_duplicate_planner_requests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                def fake_orchestrator_run_task(_envelope, request_record):
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "planner 후속 검토로 전달합니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": [],
                        "error": "",
                    }

                message = DiscordMessage(
                    message_id="msg-1",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="intent: plan\nscope: first task",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with (
                    patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task),
                    patch.object(
                        service,
                        "_derive_routing_decision_after_report",
                        return_value={
                            "next_role": "planner",
                            "routing_context": {
                                "selected_role": "planner",
                                "reason": "Selected planner for duplicate planning request.",
                            },
                        },
                    ),
                ):
                    asyncio.run(service.handle_message(message))
                    asyncio.run(service.handle_message(message))

                request_files = list(service.paths.requests_dir.glob("*.json"))
                self.assertEqual(len(request_files), 1)
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                self.assertIn("기존 요청을 재사용합니다.", service.discord_client.sent_dms[-1][1])
                self.assertIn("current_role=planner", service.discord_client.sent_dms[-1][1])
                request_payload = json.loads(request_files[0].read_text(encoding="utf-8"))
                self.assertEqual(request_payload["status"], "delegated")
                self.assertEqual(request_payload["current_role"], "planner")
                self.assertEqual(request_payload["events"][-1]["type"], "reused")
                self.assertEqual(list(service.paths.backlog_dir.glob("*.json")), [])

    def test_orchestrator_routes_generic_route_request_to_planner_under_strict_backlog_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                planner_bot_id = service.discord_config.get_role("planner").bot_id

                def fake_orchestrator_run_task(_envelope, request_record):
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "planner가 먼저 backlog 방향을 정리해야 하는 route 요청입니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": [],
                        "error": "",
                    }

                message = DiscordMessage(
                    message_id="msg-route-strict-1",
                    channel_id="dm-route-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="intent: route\nscope: Discord relay workflow 개선\n실패한 relay 재시도 흐름을 정리해줘",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )
                with (
                    patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task),
                    patch.object(
                        service,
                        "_derive_routing_decision_after_report",
                        return_value={
                            "next_role": "planner",
                            "routing_context": {
                                "selected_role": "planner",
                                "reason": "Selected planner under strict backlog policy.",
                            },
                        },
                    ),
                ):
                    asyncio.run(service.handle_message(message))

                self.assertEqual(len(service.discord_client.sent_dms), 2)
                self.assertEqual(service.discord_client.sent_dms[0], ("user-1", "수신양호"))
                self.assertIn("진행 중", service.discord_client.sent_dms[1][1])
                self.assertIn("planner 역할로 전달했습니다.", service.discord_client.sent_dms[1][1])
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, relay_content = service.discord_client.sent_channels[0]
                self.assertIn(f"<@{planner_bot_id}>", relay_content)
                self.assertIn("intent: plan", relay_content)
                request_files = list(service.paths.requests_dir.glob("*.json"))
                self.assertEqual(len(request_files), 1)
                request_payload = json.loads(request_files[0].read_text(encoding="utf-8"))
                self.assertEqual(request_payload["status"], "delegated")
                self.assertEqual(request_payload["current_role"], "planner")
                self.assertEqual(request_payload["next_role"], "planner")
                self.assertEqual(list(service.paths.backlog_dir.glob("*.json")), [])

    def test_non_planner_role_report_backlog_proposals_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260329-backlogignore1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "Discord relay workflow 개선",
                    "body": "developer가 구현 중 발견한 후속 backlog 후보입니다.",
                    "artifacts": [],
                    "params": {},
                    "current_role": "developer",
                    "next_role": "developer",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-29T00:00:00+00:00",
                    "updated_at": "2026-03-29T00:00:00+00:00",
                    "fingerprint": "backlog-ignore-fp",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                message = DiscordMessage(
                    message_id="relay-backlogignore-1",
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
                    request_id="20260329-backlogignore1",
                    sender="developer",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="Discord relay workflow 개선",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260329-backlogignore1",
                            "role": "developer",
                            "status": "completed",
                            "summary": "구현 중 추가 backlog 후보를 발견했습니다.",
                            "insights": [],
                            "proposals": {
                                "backlog_item": {
                                    "title": "relay 실패 재시도 정책 정리",
                                    "scope": "relay 실패 재시도 정책 정리",
                                    "summary": "후속 스프린트에서 retry/backoff 정책을 문서화한다.",
                                    "kind": "chore",
                                }
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260329-backlogignore1")
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(service._iter_backlog_items(), [])
                self.assertFalse(any(event.get("type") == "backlog_sync" for event in updated.get("events") or []))
                backlog_text = service.paths.shared_backlog_file.read_text(encoding="utf-8")
                self.assertNotIn("relay 실패 재시도 정책 정리", backlog_text)

    def test_user_intake_route_follows_agent_utilization_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            policy_path = (
                Path(tmpdir)
                / "orchestrator"
                / ".agents"
                / "skills"
                / "agent_utilization"
                / "policy.yaml"
            )
            policy_text = policy_path.read_text(encoding="utf-8")
            policy_path.write_text(
                policy_text.replace("user_intake: planner", "user_intake: designer", 1),
                encoding="utf-8",
            )
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                selection = service._build_governed_routing_selection(
                    {
                        "request_id": "req-policy-intake",
                        "intent": "route",
                        "scope": "디스코드 메시지 표현 개선",
                        "body": "가독성과 문구를 다듬어야 합니다.",
                    },
                    {},
                    current_role="orchestrator",
                    preferred_role="",
                    selection_source="user_intake",
                )

                self.assertEqual(selection["selected_role"], "designer")
                self.assertEqual(selection["matched_signals"], ["policy:user_intake"])

    def test_sprint_initial_default_role_follows_agent_utilization_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            policy_path = (
                Path(tmpdir)
                / "orchestrator"
                / ".agents"
                / "skills"
                / "agent_utilization"
                / "policy.yaml"
            )
            policy_text = policy_path.read_text(encoding="utf-8")
            policy_path.write_text(
                policy_text.replace("sprint_initial_default: research", "sprint_initial_default: architect", 1),
                encoding="utf-8",
            )
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                selection = service._build_governed_routing_selection(
                    {
                        "request_id": "req-policy-sprint",
                        "intent": "route",
                        "scope": "relay workflow 구조 재정리",
                        "body": "초기 구조화가 먼저 필요합니다.",
                    },
                    {},
                    current_role="orchestrator",
                    preferred_role="",
                    selection_source="sprint_initial",
                )

                self.assertEqual(selection["selected_role"], "architect")
                self.assertEqual(selection["matched_signals"], ["policy:sprint_initial_owner"])

    def test_strongest_for_matches_use_direct_capability_terms(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                matches = service._strongest_domain_matches(
                    "architect",
                    text=service._normalize_reference_text("system architecture 정리가 먼저 필요합니다."),
                )

                self.assertIn("strength:system architecture", matches)

    def test_planner_routes_technical_spec_requests_to_architect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                selection = service._build_governed_routing_selection(
                    {
                        "request_id": "req-architect-spec",
                        "intent": "route",
                        "scope": "teams_runtime module structure overview와 developer 구현용 technical specification 작성",
                        "body": "file impact와 interface contract를 정리해줘.",
                    },
                    {
                        "role": "planner",
                        "status": "completed",
                        "summary": "planning은 끝났고 다음 단계는 technical specification과 module structure overview입니다.",
                        "proposals": {},
                    },
                    current_role="planner",
                    preferred_role="",
                    selection_source="role_report",
                )

                self.assertEqual(selection["selected_role"], "architect")
                self.assertIn("routing:technical specification", selection["matched_signals"])

    def test_developer_can_handoff_explicit_technical_review_to_architect(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                selection = service._build_governed_routing_selection(
                    {
                        "request_id": "req-architect-review",
                        "intent": "implement",
                        "scope": "relay workflow patch",
                        "body": "구현 후 developer change review와 module structure 확인이 필요합니다.",
                    },
                    {
                        "role": "developer",
                        "status": "completed",
                        "summary": "구현을 마쳤고 다음 단계는 developer change review와 technical review입니다.",
                        "proposals": {},
                    },
                    current_role="developer",
                    preferred_role="",
                    selection_source="role_report",
                )

                self.assertEqual(selection["selected_role"], "architect")
                self.assertIn("routing:developer change review", selection["matched_signals"])

    def test_preferred_skill_matches_use_direct_capability_terms(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                matches = service._preferred_skill_matches(
                    "orchestrator",
                    text=service._normalize_reference_text("이번 단계는 sprint closeout 기준으로 정리해야 합니다."),
                )

                self.assertIn("preferred_skill:sprint_closeout", matches)

    def test_behavior_trait_matches_use_direct_capability_terms(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                matches = service._behavior_trait_matches(
                    "designer",
                    text=service._normalize_reference_text("presentation-aware하게 응답 문구를 다듬어야 합니다."),
                )

                self.assertIn("behavior_trait:presentation-aware", matches)

    def test_should_not_handle_excludes_candidate_before_scoring(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                selection = service._build_governed_routing_selection(
                    {
                        "request_id": "req-boundary-filter",
                        "intent": "route",
                        "scope": "closeout 판단",
                        "body": "final commit ownership 정리와 release readiness 확인이 필요합니다.",
                    },
                    {
                        "role": "architect",
                        "status": "completed",
                        "summary": "final commit ownership 정리와 release readiness 확인이 필요합니다.",
                        "proposals": {},
                    },
                    current_role="architect",
                    preferred_role="",
                    selection_source="role_report",
                )

                excluded = {
                    str(item.get("role") or ""): item
                    for item in (selection.get("candidate_summary") or [])
                    if item.get("excluded_by_boundary")
                }
                self.assertIn("developer", excluded)
                self.assertIn("forbidden:final commit ownership", excluded["developer"]["disallowed_matches"])

    def test_orchestrator_handles_natural_language_sprint_status_via_local_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                service._save_scheduler_state(
                    {
                        "active_sprint_id": "260324-Sprint-09:00",
                        "last_started_at": "2026-03-24T00:00:00+00:00",
                        "last_completed_at": "",
                        "next_slot_at": "2026-03-24T03:00:00+00:00",
                        "deferred_slot_at": "",
                        "last_trigger": "backlog_ready",
                    }
                )
                service._save_sprint_state(
                    {
                        "sprint_id": "260324-Sprint-09:00",
                        "status": "running",
                        "trigger": "backlog_ready",
                        "started_at": "2026-03-24T00:00:00+00:00",
                        "ended_at": "",
                        "selected_backlog_ids": [],
                        "selected_items": [],
                        "todos": [],
                        "commit_sha": "",
                        "report_path": "",
                    }
                )
                observed: dict[str, str] = {}

                class _FailingIntentParser:
                    def classify(self, **kwargs):
                        raise AssertionError("user intake should not go through the intent parser")

                def fake_orchestrator_run_task(envelope, request_record):
                    observed["body"] = str(request_record.get("body") or "")
                    observed["intent"] = str(request_record.get("intent") or "")
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "현재 sprint 상태를 확인했습니다.",
                        "insights": [],
                        "proposals": {
                            "request_handling": {"mode": "complete"},
                            "control_action": {
                                "kind": "sprint_lifecycle",
                                "command": "status",
                            },
                        },
                        "artifacts": [],
                        "error": "",
                    }

                service.intent_parser = _FailingIntentParser()
                message = DiscordMessage(
                    message_id="msg-nl-status",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="현재 스프린트 공유해줘",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service.handle_message(message))

                self.assertEqual(len(service.discord_client.sent_dms), 2)
                self.assertEqual(service.discord_client.sent_dms[0], ("user-1", "수신양호"))
                self.assertIn("완료", service.discord_client.sent_dms[1][1])
                self.assertIn("현재 스프린트 상태입니다.", service.discord_client.sent_dms[1][1])
                self.assertIn("스프린트: 260324-Sprint-09:00", service.discord_client.sent_dms[1][1])
                self.assertIn("상태: running", service.discord_client.sent_dms[1][1])
                self.assertNotIn("sprint_series_id", service.discord_client.sent_dms[1][1])
                self.assertEqual(observed["body"], "현재 스프린트 공유해줘")
                self.assertEqual(observed["intent"], "route")
                request_files = list(service.paths.requests_dir.glob("*.json"))
                self.assertEqual(len(request_files), 1)
                request_payload = json.loads(request_files[0].read_text(encoding="utf-8"))
                self.assertEqual(request_payload["status"], "completed")
                self.assertEqual(request_payload["current_role"], "orchestrator")
                self.assertEqual(list(service.paths.backlog_dir.glob("*.json")), [])

    def test_orchestrator_preserves_structured_status_text_for_local_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                observed: dict[str, str] = {}

                class _FailingIntentParser:
                    def classify(self, **kwargs):
                        raise AssertionError("user intake should not go through the intent parser")

                def fake_orchestrator_run_task(envelope, request_record):
                    observed["body"] = str(request_record.get("body") or "")
                    observed["intent"] = str(request_record.get("intent") or "")
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "구조화된 상태 요청을 sprint status로 해석했습니다.",
                        "insights": [],
                        "proposals": {
                            "request_handling": {"mode": "complete"},
                            "control_action": {
                                "kind": "sprint_lifecycle",
                                "command": "status",
                            },
                        },
                        "artifacts": [],
                        "error": "",
                    }

                service.intent_parser = _FailingIntentParser()
                message = DiscordMessage(
                    message_id="msg-structured-status",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="intent: status\nscope: sprint",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service.handle_message(message))

                self.assertEqual(observed["body"], "intent: status\nscope: sprint")
                self.assertEqual(observed["intent"], "route")
                self.assertIn("기록된 sprint가 없습니다.", service.discord_client.sent_dms[1][1])

    def test_orchestrator_handles_cancel_request_via_local_agent_control_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                cancellable_request = {
                    "request_id": "req-cancel-1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "cancel target",
                    "body": "cancel target",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-30T00:00:00+00:00",
                    "updated_at": "2026-03-30T00:00:00+00:00",
                    "fingerprint": "cancel-target",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "dm-1",
                        "guild_id": "",
                        "is_dm": True,
                        "message_id": "cancel-target-message",
                    },
                    "events": [],
                    "result": {},
                }
                service._save_request(cancellable_request)

                class _FailingIntentParser:
                    def classify(self, **kwargs):
                        raise AssertionError("user intake should not go through the intent parser")

                def fake_orchestrator_run_task(_envelope, request_record):
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "취소 요청을 확인했습니다.",
                        "insights": [],
                        "proposals": {
                            "request_handling": {"mode": "complete"},
                            "control_action": {
                                "kind": "cancel_request",
                                "request_id": "req-cancel-1",
                            },
                        },
                        "artifacts": [],
                        "error": "",
                    }

                service.intent_parser = _FailingIntentParser()
                message = DiscordMessage(
                    message_id="msg-cancel-status",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="cancel request_id:req-cancel-1",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service.handle_message(message))

                updated = service._load_request("req-cancel-1")
                self.assertEqual(updated["status"], "cancelled")
                self.assertIn("취소됨", service.discord_client.sent_dms[1][1])
                self.assertIn("- 결과: 요청을 취소했습니다.", service.discord_client.sent_dms[1][1])
                self.assertIn("- 요청 ID: req-cancel-1", service.discord_client.sent_dms[1][1])

    def test_requester_status_message_places_next_action_before_request_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                message_text = service._build_requester_status_message(
                    status="delegated",
                    request_id="req-status-1",
                    summary="planner가 알림 메시지 readability를 검토 중입니다.",
                    related_request_ids=[],
                )

                self.assertIn("진행 중", message_text)
                self.assertIn("- 현재 상태: planner가 알림 메시지 readability를 검토 중입니다.", message_text)
                self.assertIn("- 다음: 현재 상태를 확인한 뒤 추가 응답을 기다립니다.", message_text)
                self.assertIn("- 요청 ID: req-status-1", message_text)
                self.assertLess(
                    message_text.index("- 다음: 현재 상태를 확인한 뒤 추가 응답을 기다립니다."),
                    message_text.index("- 요청 ID: req-status-1"),
                )

    def test_orchestrator_handles_execute_request_via_local_agent_control_action(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                observed: dict[str, str] = {}

                class _FailingIntentParser:
                    def classify(self, **kwargs):
                        raise AssertionError("user intake should not go through the intent parser")

                def fake_orchestrator_run_task(envelope, request_record):
                    observed["body"] = str(request_record.get("body") or "")
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "등록된 action 실행 요청입니다.",
                        "insights": [],
                        "proposals": {
                            "request_handling": {"mode": "complete"},
                            "control_action": {
                                "kind": "execute_action",
                                "action_name": "echo",
                                "params": {"value": "hello"},
                            },
                        },
                        "artifacts": [],
                        "error": "",
                    }

                service.intent_parser = _FailingIntentParser()
                message = DiscordMessage(
                    message_id="msg-execute",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content='intent: execute\nparams: {"action_name":"echo"}',
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    with patch.object(
                        service,
                        "_run_registered_action_for_request",
                        new=AsyncMock(return_value={"status": "completed", "summary": "echo 액션을 실행했습니다."}),
                    ) as execute_mock:
                        asyncio.run(service.handle_message(message))

                self.assertEqual(observed["body"], 'intent: execute\nparams: {"action_name":"echo"}')
                execute_mock.assert_awaited_once()
                kwargs = execute_mock.await_args.kwargs
                self.assertEqual(kwargs["action_name"], "echo")
                self.assertEqual(kwargs["params"], {"value": "hello"})
                self.assertIn("echo 액션을 실행했습니다.", service.discord_client.sent_dms[1][1])

    def test_status_sprint_includes_task_titles_and_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_item = build_backlog_item(
                    title="알림 UX 개편",
                    summary="알림 흐름을 정리합니다.",
                    kind="feature",
                    source="user",
                    scope="알림 UX 개편",
                )
                todo = build_todo_item(backlog_item, owner_role="planner")
                todo["request_id"] = "req-sprint-1"
                todo["status"] = "running"
                sprint_state = {
                    "sprint_id": "260324-Sprint-09:00",
                    "status": "running",
                    "trigger": "backlog_ready",
                    "started_at": "2026-03-24T00:00:00+09:00",
                    "ended_at": "",
                    "closeout_status": "",
                    "commit_count": 0,
                    "commit_sha": "",
                    "selected_items": [dict(backlog_item)],
                    "todos": [todo],
                }
                service._save_sprint_state(sprint_state)
                service._save_scheduler_state(
                    {
                        "active_sprint_id": sprint_state["sprint_id"],
                        "next_slot_at": "2026-03-24T03:00:00+09:00",
                    }
                )
                message = DiscordMessage(
                    message_id="msg-status-sprint",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="status sprint",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=None,
                    sender="user",
                    target="orchestrator",
                    intent="status",
                    urgency="normal",
                    scope="sprint",
                )

                asyncio.run(service._reply_status_request(message, envelope))

                self.assertEqual(len(service.discord_client.sent_dms), 1)
                reply = service.discord_client.sent_dms[0][1]
                self.assertIn("## Sprint Summary", reply)
                self.assertNotIn("sprint_series_id", reply)
                self.assertIn("todo_summary: running:1", reply)
                self.assertIn("backlog_kind_summary: feature:1", reply)
                self.assertIn("알림 UX 개편", reply)
                self.assertIn("todo_id=", reply)
                self.assertIn("backlog_id=", reply)
                self.assertIn("request_id=req-sprint-1", reply)

    def test_status_request_shows_commit_message_and_hides_deprecated_restart_policy(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260405-commitstatus1",
                    "status": "committed",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "작업보고 포맷 개편",
                    "body": "작업보고 포맷 개편",
                    "artifacts": ["teams_runtime/core/reports.py"],
                    "params": {},
                    "current_role": "developer",
                    "next_role": "",
                    "owner_role": "orchestrator",
                    "sprint_id": "260405-Sprint-16:34",
                    "created_at": "2026-04-05T16:34:00+09:00",
                    "updated_at": "2026-04-05T16:35:00+09:00",
                    "fingerprint": "commit-status-fp",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "version_control_status": "committed",
                    "version_control_paths": ["teams_runtime/core/reports.py"],
                    "version_control_message": "[260405-Sprint-16:34] reports.py: compact 작업 보고 layout",
                    "task_commit_message": "[260405-Sprint-16:34] reports.py: compact 작업 보고 layout",
                    "restart_policy_status": "not_needed",
                }
                service._save_request(request_record)
                message = DiscordMessage(
                    message_id="msg-status-request",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="status request",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id="20260405-commitstatus1",
                    sender="user",
                    target="orchestrator",
                    intent="status",
                    urgency="normal",
                    scope="작업보고 포맷 개편",
                )

                asyncio.run(service._reply_status_request(message, envelope))

                self.assertEqual(len(service.discord_client.sent_dms), 1)
                reply = service.discord_client.sent_dms[0][1]
                self.assertIn("request_id=20260405-commitstatus1", reply)
                self.assertIn("version_control_status=committed", reply)
                self.assertIn(
                    "commit_message=[260405-Sprint-16:34] reports.py: compact 작업 보고 layout",
                    reply,
                )
                self.assertNotIn("restart_policy_status", reply)

    def test_status_backlog_includes_priority_titles_ids_and_kind_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                bug_item = build_backlog_item(
                    title="로그 오류 복구",
                    summary="오류 로그를 복구합니다.",
                    kind="bug",
                    source="user",
                    scope="로그 오류 복구",
                )
                feature_item = build_backlog_item(
                    title="알림 분기 기능 추가",
                    summary="채널별 알림 분기 기능을 추가합니다.",
                    kind="feature",
                    source="sourcer",
                    scope="알림 분기 기능 추가",
                )
                blocked_item = build_backlog_item(
                    title="도메인 기획 정리",
                    summary="입력 정보 부족으로 보류합니다.",
                    kind="enhancement",
                    source="carry_over",
                    scope="도메인 기획 정리",
                )
                feature_item["status"] = "selected"
                blocked_item["status"] = "blocked"
                service._save_backlog_item(bug_item)
                service._save_backlog_item(feature_item)
                service._save_backlog_item(blocked_item)
                message = DiscordMessage(
                    message_id="msg-status-backlog",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="status backlog",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=None,
                    sender="user",
                    target="orchestrator",
                    intent="status",
                    urgency="normal",
                    scope="backlog",
                )

                asyncio.run(service._reply_status_request(message, envelope))

                self.assertEqual(len(service.discord_client.sent_dms), 1)
                reply = service.discord_client.sent_dms[0][1]
                self.assertIn("## Backlog Summary", reply)
                self.assertIn("kind_summary: bug:1, feature:1, enhancement:1", reply)
                self.assertIn("source_summary: user:1, sourcer:1, carry_over:1", reply)
                self.assertIn("backlog_id", reply)
                self.assertIn("로그 오류 복구", reply)
                self.assertIn("알림 분기 기능 추가", reply)
                self.assertIn("도메인 기획 정리", reply)

    def test_status_helper_still_works_while_plan_request_continues_even_after_runtime_file_changes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            runtime_file = Path(tmpdir) / "teams_runtime" / "core" / "orchestration.py"
            runtime_file.parent.mkdir(parents=True, exist_ok=True)
            runtime_file.write_text("value = 1\n", encoding="utf-8")
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                runtime_file.write_text("value = 2\n", encoding="utf-8")
                status_message = DiscordMessage(
                    message_id="msg-status-backlog-reload",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="status backlog",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )
                status_envelope = MessageEnvelope(
                    request_id=None,
                    sender="user",
                    target="orchestrator",
                    intent="status",
                    urgency="normal",
                    scope="backlog",
                )
                plan_message = DiscordMessage(
                    message_id="msg-plan-reload",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="plan new feature",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )
                plan_envelope = MessageEnvelope(
                    request_id=None,
                    sender="user",
                    target="orchestrator",
                    intent="plan",
                    urgency="normal",
                    scope="new feature",
                    body="new feature",
                )

                def fake_orchestrator_run_task(_envelope, request_record):
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "runtime 파일 변경이 있어도 plan 요청을 계속 처리했습니다.",
                        "insights": [],
                        "proposals": {"request_handling": {"mode": "complete"}},
                        "artifacts": [],
                        "error": "",
                    }

                asyncio.run(service._reply_status_request(status_message, status_envelope))
                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service._handle_user_request(plan_message, plan_envelope, forwarded=False))

                self.assertEqual(len(list(service.paths.requests_dir.glob("*.json"))), 1)
                self.assertEqual(len(service.discord_client.sent_dms), 2)
                self.assertIn("## Backlog Summary", service.discord_client.sent_dms[0][1])
                self.assertNotIn("## Runtime Reload", service.discord_client.sent_dms[0][1])
                self.assertIn("runtime 파일 변경이 있어도 plan 요청을 계속 처리했습니다.", service.discord_client.sent_dms[1][1])

    def test_handle_user_request_with_recent_verified_document_routes_to_local_orchestrator_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                now_iso = datetime.now(timezone.utc).isoformat()
                blocked_request = {
                    "request_id": "20260326-d24ea592",
                    "status": "blocked",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "teams service evolution plan",
                    "body": "teams service evolution plan을 바탕으로 todo를 도출하고 그에 따른 작업을 진행해달라는 요청",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "fingerprint": "blocked-plan-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-blocked",
                    },
                    "events": [],
                    "result": {
                        "request_id": "20260326-d24ea592",
                        "role": "planner",
                        "status": "blocked",
                        "summary": "teams service evolution plan 기반 todo 도출은 원본 기획 문서 존재와 내용 확정이 선행되어야 해 현재는 차단 상태로 정리했습니다.",
                        "insights": [],
                        "proposals": {
                            "blocked_reason": {
                                "reason": "`teams service evolution plan` 문서의 실제 생성 및 확정 경로가 선행 확인되지 않았습니다.",
                                "required_next_step": "shared workspace에 문서 파일을 먼저 생성·확정한 뒤, 그 문서를 기준으로 planner가 backlog/todo를 분해하는 후속 요청을 진행해야 합니다.",
                            }
                        },
                        "artifacts": [],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "source planning document not yet confirmed",
                    },
                }
                verification_request = {
                    "request_id": "20260326-0529cebc",
                    "status": "completed",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "document verification",
                    "body": "문서의 존재 여부와 내용이 맞는지 확인하고 확정해달라는 요청",
                    "artifacts": ["./workspace/teams_generated/shared_workspace/teams_service_evolution_plan.md"],
                    "params": {},
                    "current_role": "orchestrator",
                    "next_role": "",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "fingerprint": "verification-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-verification",
                    },
                    "events": [],
                    "result": {
                        "request_id": "20260326-0529cebc",
                        "role": "qa",
                        "status": "completed",
                        "summary": "기획 문서는 shared workspace에 실제 존재하며, 요구한 핵심 항목을 포함해 후속 planning 기준 문서로 확정 가능한 수준입니다.",
                        "insights": [],
                        "proposals": {
                            "verification_result": {
                                "document_exists": True,
                                "content_match": True,
                                "ready_for_planning": True,
                                "location": "./workspace/teams_generated/shared_workspace/teams_service_evolution_plan.md",
                            }
                        },
                        "artifacts": ["./workspace/teams_generated/shared_workspace/teams_service_evolution_plan.md"],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "",
                    },
                }
                service._save_request(blocked_request)
                service._save_request(verification_request)
                observed: dict[str, object] = {}

                message = DiscordMessage(
                    message_id="msg-followup-1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="tester",
                    content="백로그에 todo를 등록하고 이어서 구현까지 진행해달라",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=None,
                    sender="user",
                    target="orchestrator",
                    intent="route",
                    urgency="normal",
                    scope="backlog todo registration and implementation",
                    body="백로그에 todo를 등록하고 이어서 구현까지 진행해달라는 요청",
                )

                def fake_orchestrator_run_task(_envelope, request_record):
                    observed["body"] = str(request_record.get("body") or "")
                    observed["artifacts"] = list(request_record.get("artifacts") or [])
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "최근 verification 결과를 참고할 수 있는 follow-up 요청으로 접수했습니다.",
                        "insights": [],
                        "proposals": {
                            "request_handling": {"mode": "complete"},
                        },
                        "artifacts": [],
                        "error": "",
                    }

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service._handle_user_request(message, envelope, forwarded=False))

                blocked_after = service._load_request("20260326-d24ea592")
                self.assertEqual(blocked_after["status"], "blocked")
                request_files = list(service.paths.requests_dir.glob("*.json"))
                self.assertEqual(len(request_files), 3)
                new_requests = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in request_files
                    if path.stem not in {"20260326-d24ea592", "20260326-0529cebc"}
                ]
                self.assertEqual(len(new_requests), 1)
                self.assertEqual(new_requests[0]["status"], "completed")
                self.assertEqual(new_requests[0]["current_role"], "orchestrator")
                self.assertEqual(observed["body"], "백로그에 todo를 등록하고 이어서 구현까지 진행해달라는 요청")
                self.assertEqual(observed["artifacts"], [])
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                self.assertIn("완료", service.discord_client.sent_channels[0][1])

    def test_orchestrator_local_planner_runtime_isolated_from_planner_service_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                orchestrator_service = TeamService(tmpdir, "orchestrator")
                planner_service = TeamService(tmpdir, "planner")
                local_planner_runtime = orchestrator_service._runtime_for_role(
                    "planner",
                    orchestrator_service.runtime_config.sprint_id,
                )
                planner_runtime = planner_service.role_runtime

                class _TaggedRunner:
                    def __init__(self, session_id: str):
                        self.session_id = session_id

                    def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                        return (
                            json.dumps(
                                {
                                    "status": "completed",
                                    "summary": "ok",
                                    "error": "",
                                    "proposals": {},
                                    "artifacts": [],
                                },
                                ensure_ascii=False,
                            ),
                            self.session_id,
                        )

                planner_runtime.codex_runner = _TaggedRunner("planner-service-session")
                local_planner_runtime.codex_runner = _TaggedRunner("planner-local-session")
                envelope = MessageEnvelope(
                    request_id="request-1",
                    sender="orchestrator",
                    target="planner",
                    intent="plan",
                    urgency="normal",
                    scope="scope",
                )
                request_record = {
                    "request_id": "request-1",
                    "scope": "scope",
                    "body": "",
                    "artifacts": [],
                    "sprint_id": orchestrator_service.runtime_config.sprint_id,
                }

                planner_runtime.run_task(envelope, dict(request_record))
                local_planner_runtime.run_task(envelope, dict(request_record))

                planner_state = planner_runtime.session_manager.load()
                local_state = local_planner_runtime.session_manager.load()

                self.assertIsNotNone(planner_state)
                self.assertIsNotNone(local_state)
                self.assertEqual(planner_state.session_id, "planner-service-session")
                self.assertEqual(local_state.session_id, "planner-local-session")
                self.assertEqual(planner_state.runtime_identity, "planner")
                self.assertEqual(local_state.runtime_identity, "orchestrator.local.planner")
                self.assertNotEqual(planner_state.workspace_path, local_state.workspace_path)

    def test_handle_user_request_reopens_blocked_duplicate_when_followup_adds_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                now_iso = datetime.now(timezone.utc).isoformat()
                blocked_request = {
                    "request_id": "20260326-d24ea592",
                    "status": "blocked",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "teams service evolution plan",
                    "body": "기획 문서 기준으로 후속 todo를 정리해달라는 요청",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "fingerprint": "blocked-dup-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-blocked",
                    },
                    "events": [],
                    "result": {
                        "request_id": "20260326-d24ea592",
                        "role": "planner",
                        "status": "blocked",
                        "summary": "기획 문서 확인 전이라 보류합니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": [],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "source planning document not yet confirmed",
                    },
                }
                service._save_request(blocked_request)

                message = DiscordMessage(
                    message_id="msg-duplicate-followup-1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="tester",
                    content="teams service evolution plan 다시 진행해줘",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=None,
                    sender="user",
                    target="orchestrator",
                    intent="route",
                    urgency="normal",
                    scope="teams service evolution plan",
                    artifacts=["./workspace/teams_generated/shared_workspace/teams_service_evolution_plan.md"],
                    body="teams service evolution plan을 기준 문서로 사용해서 이어서 진행해달라",
                )

                with patch.object(service, "_find_duplicate_request", return_value=service._load_request("20260326-d24ea592")):
                    asyncio.run(service._handle_user_request(message, envelope, forwarded=False))

                reopened_request = service._load_request("20260326-d24ea592")
                self.assertEqual(reopened_request["status"], "delegated")
                self.assertEqual(reopened_request["current_role"], "planner")
                self.assertIn(
                    "./workspace/teams_generated/shared_workspace/teams_service_evolution_plan.md",
                    reopened_request["artifacts"],
                )
                self.assertEqual(len(list(service.paths.requests_dir.glob("*.json"))), 1)
                self.assertIn("재개", service.discord_client.sent_channels[1][1])

    def test_handle_user_request_retries_blocked_orchestrator_duplicate_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                now_iso = datetime.now(timezone.utc).isoformat()
                blocked_request = {
                    "request_id": "20260401-f0fe73e0",
                    "status": "blocked",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "스프린트 시작해.\nmilestone은 \"KIS 스캘핑 고도화\"",
                    "body": "스프린트 시작해.\nmilestone은 \"KIS 스캘핑 고도화\"",
                    "artifacts": [],
                    "params": {},
                    "current_role": "orchestrator",
                    "next_role": "orchestrator",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "fingerprint": "blocked-orchestrator-dup-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "dm-1",
                        "guild_id": "",
                        "is_dm": True,
                        "message_id": "message-blocked",
                    },
                    "events": [],
                    "result": {
                        "request_id": "20260401-f0fe73e0",
                        "role": "orchestrator",
                        "status": "blocked",
                        "summary": "스프린트 시작은 해석했지만 sprint lifecycle CLI가 쓰기에 실패했습니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": [],
                        "next_role": "",
                        "error": "PermissionError: [Errno 1] Operation not permitted: '/repo/teams_generated/.teams_runtime/sprint_scheduler.json'",
                    },
                }
                service._save_request(blocked_request)

                message = DiscordMessage(
                    message_id="msg-sprint-retry-1",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="스프린트 시작해.\nmilestone은 \"KIS 스캘핑 고도화\"",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=None,
                    sender="user",
                    target="orchestrator",
                    intent="route",
                    urgency="normal",
                    scope="스프린트 시작해.\nmilestone은 \"KIS 스캘핑 고도화\"",
                    artifacts=[],
                    body="스프린트 시작해.\nmilestone은 \"KIS 스캘핑 고도화\"",
                )

                observed = {"calls": 0}

                def fake_orchestrator_run_task(_envelope, request_record):
                    observed["calls"] += 1
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "스프린트를 시작했습니다.",
                        "insights": [],
                        "proposals": {"request_handling": {"mode": "complete"}},
                        "artifacts": [],
                        "error": "",
                    }

                with (
                    patch.object(service, "_find_duplicate_request", return_value=service._load_request("20260401-f0fe73e0")),
                    patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task),
                ):
                    asyncio.run(service._handle_user_request(message, envelope, forwarded=False))

                retried_request = service._load_request("20260401-f0fe73e0")
                self.assertEqual(observed["calls"], 1)
                self.assertEqual(retried_request["status"], "completed")
                self.assertEqual(retried_request["current_role"], "orchestrator")
                self.assertTrue(any(event.get("type") == "retried" for event in retried_request.get("events") or []))
                self.assertEqual(len(service.discord_client.sent_dms), 2)
                joined_replies = "\n".join(message for _user_id, message in service.discord_client.sent_dms)
                self.assertIn("기존 blocked 요청을 다시 시도합니다.", joined_replies)
                self.assertIn("완료", joined_replies)

    def test_orchestrator_role_report_verification_resumes_matching_blocked_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                now_iso = datetime.now(timezone.utc).isoformat()
                blocked_request = {
                    "request_id": "20260326-d24ea592",
                    "status": "blocked",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "teams service evolution plan",
                    "body": "teams service evolution plan을 바탕으로 todo를 도출하고 그에 따른 작업을 진행해달라는 요청",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "fingerprint": "blocked-plan-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-blocked",
                    },
                    "events": [],
                    "result": {
                        "request_id": "20260326-d24ea592",
                        "role": "planner",
                        "status": "blocked",
                        "summary": "teams service evolution plan 기반 todo 도출은 원본 기획 문서 존재와 내용 확정이 선행되어야 해 현재는 차단 상태로 정리했습니다.",
                        "insights": [],
                        "proposals": {
                            "blocked_reason": {
                                "reason": "`teams service evolution plan` 문서의 실제 생성 및 확정 경로가 선행 확인되지 않았습니다.",
                                "required_next_step": "shared workspace에 문서 파일을 먼저 생성·확정한 뒤, 그 문서를 기준으로 planner가 backlog/todo를 분해하는 후속 요청을 진행해야 합니다.",
                            }
                        },
                        "artifacts": [],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "source planning document not yet confirmed",
                    },
                }
                verification_request = {
                    "request_id": "20260326-0529cebc",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "document verification",
                    "body": "문서의 존재 여부와 내용이 맞는지 확인하고 확정해달라는 요청",
                    "artifacts": [],
                    "params": {},
                    "current_role": "qa",
                    "next_role": "qa",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "fingerprint": "verification-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-verification",
                    },
                    "events": [],
                    "result": {},
                }
                service._save_request(blocked_request)
                service._save_request(verification_request)

                message = DiscordMessage(
                    message_id="relay-verification-1",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id=service.discord_config.get_role("qa").bot_id,
                    author_name="qa",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id="20260326-0529cebc",
                    sender="qa",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="document verification",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260326-0529cebc",
                            "role": "qa",
                            "status": "completed",
                            "summary": "기획 문서는 shared workspace에 실제 존재하며 후속 planning 기준 문서로 사용할 수 있습니다.",
                            "insights": [],
                            "proposals": {
                                "verification_result": {
                                    "document_exists": True,
                                    "content_match": True,
                                    "ready_for_planning": True,
                                    "location": "./workspace/teams_generated/shared_workspace/teams_service_evolution_plan.md",
                                }
                            },
                            "artifacts": ["./workspace/teams_generated/shared_workspace/teams_service_evolution_plan.md"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated_verification = service._load_request("20260326-0529cebc")
                resumed_request = service._load_request("20260326-d24ea592")
                self.assertEqual(updated_verification["status"], "completed")
                self.assertEqual(resumed_request["status"], "delegated")
                self.assertEqual(resumed_request["current_role"], "planner")
                self.assertIn(
                    "./workspace/teams_generated/shared_workspace/teams_service_evolution_plan.md",
                    resumed_request["artifacts"],
                )
                self.assertEqual(len(service.discord_client.sent_channels), 2)
                self.assertIn("- 관련 요청 재개: 20260326-d24ea592", service.discord_client.sent_channels[1][1])

    def test_orchestrator_loads_role_report_from_persisted_request_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260323-reportref1",
                    "status": "delegated",
                    "intent": "plan",
                    "urgency": "normal",
                    "scope": "intraday trading 개선 방안 기획",
                    "body": "intraday trading 개선 방안 기획",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-23T00:00:00+00:00",
                    "updated_at": "2026-03-23T00:00:00+00:00",
                    "fingerprint": "f",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-1",
                    },
                    "events": [],
                    "result": {
                        "request_id": "20260323-reportref1",
                        "role": "planner",
                        "status": "completed",
                        "summary": "요약입니다.",
                        "next_role": "",
                        "approval_needed": False,
                        "artifacts": [],
                        "proposals": {},
                        "error": "",
                    },
                }
                service._save_request(request_record)
                envelope = MessageEnvelope(
                    request_id="20260323-reportref1",
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="intraday trading 개선 방안 기획",
                    params={
                        "_teams_kind": "report",
                        "report_status": "completed",
                    },
                )
                message = DiscordMessage(
                    message_id="relay-5",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id="111111111111111113",
                    author_name="planner",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260323-reportref1")
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(updated["result"]["summary"], "요약입니다.")
                planning_text = service.paths.shared_planning_file.read_text(encoding="utf-8")
                shared_history_text = service.paths.shared_history_file.read_text(encoding="utf-8")
                planner_todo_text = service.paths.role_todo_file("planner").read_text(encoding="utf-8")
                self.assertIn("요약입니다.", planning_text)
                self.assertIn("요약입니다.", shared_history_text)
                self.assertIn("active request 없음", planner_todo_text)

    def test_orchestrator_ignores_stale_planner_report_after_request_moves_to_developer(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260414-stale-planner-to-developer",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "stale planner relay",
                    "body": "stale planner relay",
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
                            "review_cycle_limit": 3,
                        },
                    },
                    "current_role": "developer",
                    "next_role": "developer",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-04-14T00:00:00+00:00",
                    "updated_at": "2026-04-14T00:00:00+00:00",
                    "fingerprint": "stale-planner-to-developer",
                    "reply_route": {},
                    "events": [],
                    "result": {
                        "request_id": "20260414-stale-planner-to-developer",
                        "role": "architect",
                        "status": "completed",
                        "summary": "architect가 developer build 단계로 넘겼습니다.",
                        "next_role": "",
                        "approval_needed": False,
                        "artifacts": [],
                        "proposals": {
                            "workflow_transition": {
                                "outcome": "advance",
                                "target_phase": "implementation",
                                "target_step": "developer_build",
                                "reopen_category": "",
                                "reason": "developer build로 진행합니다.",
                                "unresolved_items": [],
                                "finalize_phase": False,
                            }
                        },
                        "error": "",
                    },
                }
                service._save_request(request_record)

                stale_result = {
                    "request_id": request_record["request_id"],
                    "role": "planner",
                    "status": "blocked",
                    "summary": "planner 문서 계약이 닫히지 않았습니다.",
                    "next_role": "",
                    "approval_needed": False,
                    "artifacts": [],
                    "proposals": {
                        "workflow_transition": {
                            "outcome": "reopen",
                            "target_phase": "planning",
                            "target_step": "planner_finalize",
                            "reopen_category": "scope",
                            "reason": "stale planner finalize",
                            "unresolved_items": ["planner 문서 정리 필요"],
                            "finalize_phase": False,
                        }
                    },
                    "error": "planner 문서 정리 필요",
                }

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-stale-planner-to-developer",
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
                            params={"_teams_kind": "report", "result": stale_result},
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "developer")
                self.assertEqual(updated["result"]["role"], "architect")
                self.assertEqual(updated["result"]["summary"], "architect가 developer build 단계로 넘겼습니다.")
                self.assertEqual(updated["events"], [])
                shared_history_text = service.paths.shared_history_file.read_text(encoding="utf-8")
                self.assertNotIn("planner 문서 계약이 닫히지 않았습니다.", shared_history_text)

    def test_orchestrator_ignores_stale_planner_report_after_request_committed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260414-stale-planner-after-commit",
                    "status": "committed",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "stale planner relay after commit",
                    "body": "stale planner relay after commit",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "closeout",
                            "step": "closeout",
                            "phase_owner": "version_controller",
                            "phase_status": "completed",
                            "planning_pass_count": 1,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 0,
                            "review_cycle_limit": 3,
                        },
                    },
                    "current_role": "orchestrator",
                    "next_role": "",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-04-14T00:00:00+00:00",
                    "updated_at": "2026-04-14T00:00:00+00:00",
                    "fingerprint": "stale-planner-after-commit",
                    "reply_route": {},
                    "events": [],
                    "result": {
                        "request_id": "20260414-stale-planner-after-commit",
                        "role": "version_controller",
                        "status": "completed",
                        "summary": "closeout commit까지 완료했습니다.",
                        "next_role": "",
                        "approval_needed": False,
                        "artifacts": [],
                        "proposals": {
                            "workflow_transition": {
                                "outcome": "complete",
                                "target_phase": "closeout",
                                "target_step": "closeout",
                                "reopen_category": "",
                                "reason": "closeout을 마쳤습니다.",
                                "unresolved_items": [],
                                "finalize_phase": True,
                            }
                        },
                        "error": "",
                    },
                }
                service._save_request(request_record)

                stale_result = {
                    "request_id": request_record["request_id"],
                    "role": "planner",
                    "status": "blocked",
                    "summary": "planner 문서 계약이 닫히지 않았습니다.",
                    "next_role": "",
                    "approval_needed": False,
                    "artifacts": [],
                    "proposals": {
                        "workflow_transition": {
                            "outcome": "reopen",
                            "target_phase": "planning",
                            "target_step": "planner_finalize",
                            "reopen_category": "scope",
                            "reason": "stale planner finalize",
                            "unresolved_items": ["planner 문서 정리 필요"],
                            "finalize_phase": False,
                        }
                    },
                    "error": "planner 문서 정리 필요",
                }

                asyncio.run(
                    service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-stale-planner-after-commit",
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
                            params={"_teams_kind": "report", "result": stale_result},
                        ),
                    )
                )

                updated = service._load_request(request_record["request_id"])
                self.assertEqual(updated["status"], "committed")
                self.assertEqual(updated["current_role"], "orchestrator")
                self.assertEqual(updated["result"]["role"], "version_controller")
                self.assertEqual(updated["result"]["summary"], "closeout commit까지 완료했습니다.")
                self.assertEqual(updated["events"], [])
                shared_history_text = service.paths.shared_history_file.read_text(encoding="utf-8")
                self.assertNotIn("planner 문서 계약이 닫히지 않았습니다.", shared_history_text)

    def test_orchestrator_does_not_persist_planner_backlog_proposals_from_role_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260324-backlogsync1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "다음 스프린트 백로그 정리",
                    "body": "planner가 backlog 후보를 정리합니다.",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-24T00:00:00+00:00",
                    "updated_at": "2026-03-24T00:00:00+00:00",
                    "fingerprint": "backlog-sync-fp",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                message = DiscordMessage(
                    message_id="relay-backlogsync-1",
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
                    request_id="20260324-backlogsync1",
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="다음 스프린트 백로그 정리",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260324-backlogsync1",
                            "role": "planner",
                            "status": "completed",
                            "summary": "후속 백로그 1건을 등록 대상으로 정리했습니다.",
                            "insights": [],
                            "proposals": {
                                "backlog_items": [
                                    {
                                        "title": "backlog 등록 후 다음 스프린트 선택 검증",
                                        "scope": "backlog 등록 후 다음 스프린트 선택 검증",
                                        "summary": "backlog.md 반영과 다음 스프린트 입력 경로를 검증합니다.",
                                        "kind": "chore",
                                        "acceptance_criteria": [
                                            "shared_workspace/backlog.md에 항목이 보인다.",
                                            "다음 스프린트 선택 대상에 포함된다.",
                                        ],
                                    }
                                ]
                            },
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260324-backlogsync1")
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(service._iter_backlog_items(), [])
                backlog_text = service.paths.shared_backlog_file.read_text(encoding="utf-8")
                self.assertNotIn("backlog 등록 후 다음 스프린트 선택 검증", backlog_text)
                self.assertFalse(any(event.get("type") == "backlog_sync" for event in updated.get("events") or []))

    def test_orchestrator_role_report_replies_without_backlog_id_when_planner_has_not_persisted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260324-backlogsingle1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "backlog",
                    "body": "긴 메시지 분할 발송 이슈를 backlog에 추가한다.",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-24T00:00:00+00:00",
                    "updated_at": "2026-03-24T00:00:00+00:00",
                    "fingerprint": "backlog-single-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-1",
                    },
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                message = DiscordMessage(
                    message_id="relay-backlogsingle-1",
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
                    request_id="20260324-backlogsingle1",
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="backlog",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260324-backlogsingle1",
                            "role": "planner",
                            "status": "completed",
                            "summary": "긴 메시지 분할 발송 이슈를 backlog에 추가할 수 있도록 정리했습니다.",
                            "insights": [],
                            "proposals": {
                                "backlog_item": {
                                    "title": "긴 메시지 분할 발송 이슈 해결",
                                    "scope": "긴 메시지 분할 발송 이슈 해결",
                                    "summary": "긴 Discord 메시지를 분할 발송할 때 문단과 코드블록 훼손을 줄인다.",
                                    "kind": "bugfix",
                                    "acceptance_criteria": [
                                        "2000자 초과 메시지가 문단 경계를 최대한 보존하며 분할된다.",
                                        "분할 메시지 순서를 사용자가 이해할 수 있다.",
                                    ],
                                }
                            },
                            "artifacts": [],
                            "next_role": "orchestrator",
                            "approval_needed": False,
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                self.assertEqual(service._iter_backlog_items(), [])
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, reply = service.discord_client.sent_channels[0]
                self.assertIn("완료", reply)
                self.assertIn("- 요청 ID: 20260324-backlogsingle1", reply)
                self.assertIn("긴 메시지 분할 발송 이슈를 backlog에 추가할 수 있도록 정리했습니다.", reply)
                self.assertNotIn("backlog_id=", reply)

    def test_orchestrator_autonomously_selects_developer_for_action_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                developer_bot_id = service.discord_config.get_role("developer").bot_id
                request_record = {
                    "request_id": "20260324-routingnext1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "디스코드 메시지 가독성 개선",
                    "body": "디스코드 메시지 가독성 개선을 실제로 구현해줘",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-24T00:00:00+00:00",
                    "updated_at": "2026-03-24T00:00:00+00:00",
                    "fingerprint": "routing-next-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-1",
                    },
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                message = DiscordMessage(
                    message_id="relay-routingnext-1",
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
                    request_id="20260324-routingnext1",
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="디스코드 메시지 가독성 개선",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260324-routingnext1",
                            "role": "planner",
                            "status": "completed",
                            "summary": "구현은 developer가 이어서 진행하는 것이 적절합니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260324-routingnext1")
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "developer")
                self.assertEqual(updated["next_role"], "developer")
                self.assertEqual(updated["routing_context"]["preferred_role"], "")
                self.assertEqual(len(service.discord_client.sent_channels), 2)
                _channel_id, relay_content = service.discord_client.sent_channels[0]
                self.assertIn(f"<@{developer_bot_id}>", relay_content)
                self.assertIn("intent: implement", relay_content)
                _reply_channel_id, reply = service.discord_client.sent_channels[1]
                self.assertIn("진행 중", reply)
                self.assertIn("developer 역할로 전달했습니다.", reply)

    def test_orchestrator_centralizes_selection_over_planner_role_suggestion(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                developer_bot_id = service.discord_config.get_role("developer").bot_id
                request_record = {
                    "request_id": "20260329-routingoverride1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "디스코드 메시지 렌더링 코드 구현",
                    "body": "버튼 레이블 정리 이후 실제 렌더링 코드를 구현하고 회귀 없이 반영해줘",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-29T00:00:00+00:00",
                    "updated_at": "2026-03-29T00:00:00+00:00",
                    "fingerprint": "routing-override-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-1",
                    },
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                message = DiscordMessage(
                    message_id="relay-routingoverride-1",
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
                    request_id="20260329-routingoverride1",
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="디스코드 메시지 렌더링 코드 구현",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260329-routingoverride1",
                            "role": "planner",
                            "status": "completed",
                            "summary": "다음 단계는 designer보다 구현 역할이 더 적합합니다.",
                            "insights": [],
                            "proposals": {
                                "routing": {
                                    "recommended_next_role": "designer",
                                }
                            },
                            "artifacts": [],
                            "next_role": "designer",
                            "approval_needed": False,
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260329-routingoverride1")
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "developer")
                self.assertEqual(updated["next_role"], "developer")
                self.assertIn("routing_context", updated)
                self.assertEqual(updated["result"]["next_role"], "")
                self.assertEqual(updated["routing_context"]["selected_role"], "developer")
                self.assertEqual(updated["routing_context"]["preferred_role"], "")
                self.assertEqual(updated["routing_context"]["selection_source"], "role_report")
                self.assertEqual(updated["routing_context"]["policy_source"], "workspace_skill_policy")
                self.assertEqual(updated["routing_context"]["routing_phase"], "implementation")
                self.assertEqual(updated["routing_context"]["request_state_class"], "execution_opened")
                self.assertGreater(updated["routing_context"]["score_total"], 0)
                self.assertIn("score_breakdown", updated["routing_context"])
                self.assertIn("candidate_summary", updated["routing_context"])
                self.assertTrue(updated["routing_context"]["matched_signals"])
                self.assertEqual(updated["routing_context"]["override_reason"], "")
                self.assertIn("delegation_context", updated)
                self.assertEqual(updated["delegation_context"]["from_role"], "planner")
                self.assertIn(
                    "다음 단계는 designer보다 구현 역할이 더 적합합니다.",
                    updated["delegation_context"]["latest_context_summary"],
                )
                self.assertEqual(len(service.discord_client.sent_channels), 2)
                _channel_id, relay_content = service.discord_client.sent_channels[0]
                self.assertIn(f"<@{developer_bot_id}>", relay_content)
                self.assertIn("handoff | planner -> developer | route", relay_content)
                self.assertIn("[이관 이유]", relay_content)
                self.assertIn("[핵심 전달]", relay_content)
                self.assertIn("Selected developer because its strengths match the current request.", relay_content)
                self.assertNotIn("- score total:", relay_content)

    def test_orchestrator_autonomously_selects_developer_when_planner_omits_next_role_for_action_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                developer_bot_id = service.discord_config.get_role("developer").bot_id
                request_record = {
                    "request_id": "20260330-autonextroute1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "디스코드 상태 응답 렌더링 코드 수정",
                    "body": "상태 응답 문구를 정리한 뒤 실제 렌더링 코드를 수정하고 회귀 없이 반영해줘",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-30T00:00:00+00:00",
                    "updated_at": "2026-03-30T00:00:00+00:00",
                    "fingerprint": "auto-next-role-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-1",
                    },
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                message = DiscordMessage(
                    message_id="relay-autonextroute-1",
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
                    request_id="20260330-autonextroute1",
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="디스코드 상태 응답 렌더링 코드 수정",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260330-autonextroute1",
                            "role": "planner",
                            "status": "completed",
                            "summary": "planning은 끝났고 다음 단계는 실제 구현입니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": [],
                            "next_role": "",
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260330-autonextroute1")
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "developer")
                self.assertEqual(updated["next_role"], "developer")
                self.assertEqual(updated["routing_context"]["selected_role"], "developer")
                self.assertEqual(updated["routing_context"]["preferred_role"], "")
                self.assertEqual(updated["routing_context"]["request_state_class"], "execution_opened")
                self.assertGreater(updated["routing_context"]["score_total"], 0)
                _channel_id, relay_content = service.discord_client.sent_channels[0]
                self.assertIn(f"<@{developer_bot_id}>", relay_content)
                self.assertIn("[이관 이유]", relay_content)
                self.assertIn("Selected developer because its strengths match the current request.", relay_content)
                self.assertIn("[핵심 전달]", relay_content)
                self.assertNotIn("- score total:", relay_content)

    def test_orchestrator_ignores_planner_self_loop_and_completes_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260325-plannerloop1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "운영 프로젝트 수익화 모델 검토 및 설계",
                    "body": "현재 운영 중인 프로젝트의 제품 구조와 사용자 흐름을 기준으로 현실적인 수익화 모델을 검토하고 우선 적용 가능한 monetization 전략안을 설계한다.",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "sprint_id": "2026-Sprint-01-20260325T082910KST",
                        "backlog_id": "backlog-20260325-735ab42a",
                        "todo_id": "todo-082910-74741d",
                    },
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01-20260325T082910KST",
                    "backlog_id": "backlog-20260325-735ab42a",
                    "todo_id": "todo-082910-74741d",
                    "created_at": "2026-03-25T08:33:34+09:00",
                    "updated_at": "2026-03-25T08:33:34+09:00",
                    "fingerprint": "planner-loop-fp",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner"],
                }
                service._save_request(request_record)
                service.paths.current_sprint_file.write_text("# current sprint\n", encoding="utf-8")
                message = DiscordMessage(
                    message_id="relay-planner-loop-1",
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
                    request_id="20260325-plannerloop1",
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="운영 프로젝트 수익화 모델 검토 및 설계",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260325-plannerloop1",
                            "role": "planner",
                            "status": "completed",
                            "summary": "운영 프로젝트 수익화 모델 검토 작업을 시장·제품·실험 계획까지 포함한 기획 todo로 구체화했습니다.",
                            "insights": [],
                            "proposals": {
                                "routing": {
                                    "recommended_next_role": "planner",
                                }
                            },
                            "artifacts": ["./shared_workspace/current_sprint.md"],
                            "next_role": "planner",
                            "approval_needed": False,
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260325-plannerloop1")
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "architect")
                self.assertEqual(updated["next_role"], "architect")
                architect_bot_id = service.discord_config.get_role("architect").bot_id
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, relay_content = service.discord_client.sent_channels[0]
                self.assertIn(f"<@{architect_bot_id}>", relay_content)
                self.assertIn("intent: architect", relay_content)

    def test_orchestrator_does_not_open_execution_when_planner_completes_without_next_role(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260330-plannerdone1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "서비스 개선 아이디어 정리",
                    "body": "개선 아이디어를 backlog 관점으로 정리하고 문서화해줘",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-30T00:00:00+00:00",
                    "updated_at": "2026-03-30T00:00:00+00:00",
                    "fingerprint": "planner-done-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-1",
                    },
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                message = DiscordMessage(
                    message_id="relay-plannerdone-1",
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
                    request_id="20260330-plannerdone1",
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="서비스 개선 아이디어 정리",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260330-plannerdone1",
                            "role": "planner",
                            "status": "completed",
                            "summary": "planning/backlog 정리를 완료했고 추가 실행 역할은 열지 않습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": [],
                            "next_role": "",
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260330-plannerdone1")
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(updated["current_role"], "orchestrator")
                self.assertEqual(updated["next_role"], "")
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _reply_channel_id, reply = service.discord_client.sent_channels[0]
                self.assertIn("완료", reply)
                self.assertIn("- 요청 ID: 20260330-plannerdone1", reply)

    def test_orchestrator_autonomously_selects_planner_reentry_from_execution_role(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                planner_bot_id = service.discord_config.get_role("planner").bot_id
                request_record = {
                    "request_id": "20260330-plannerreentry1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "실행 중 요구사항 재정리가 필요한 구현 요청",
                    "body": "요구사항이 흔들려서 planner 재정리가 필요하다",
                    "artifacts": [],
                    "params": {},
                    "current_role": "developer",
                    "next_role": "developer",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-30T00:00:00+00:00",
                    "updated_at": "2026-03-30T00:00:00+00:00",
                    "fingerprint": "planner-reentry-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-1",
                    },
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                message = DiscordMessage(
                    message_id="relay-plannerreentry-1",
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
                    request_id="20260330-plannerreentry1",
                    sender="developer",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="실행 중 요구사항 재정리가 필요한 구현 요청",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260330-plannerreentry1",
                            "role": "developer",
                            "status": "completed",
                            "summary": "구현을 이어가기 전에 planner가 scope와 acceptance criteria를 다시 정리해야 합니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": [],
                            "next_role": "",
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260330-plannerreentry1")
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "planner")
                self.assertEqual(updated["next_role"], "planner")
                self.assertEqual(updated["routing_context"]["selected_role"], "planner")
                self.assertEqual(updated["routing_context"]["preferred_role"], "")
                self.assertGreater(updated["routing_context"]["score_total"], 0)
                _channel_id, relay_content = service.discord_client.sent_channels[0]
                self.assertIn(f"<@{planner_bot_id}>", relay_content)
                self.assertIn("[이관 이유]", relay_content)
                self.assertIn("Selected planner because its strengths match the current request.", relay_content)
                self.assertIn("[핵심 전달]", relay_content)

    def test_orchestrator_rejects_legacy_approve_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                class _FailingIntentParser:
                    def classify(self, **kwargs):
                        raise AssertionError("user intake should not go through the intent parser")

                def fake_orchestrator_run_task(_envelope, request_record):
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "approval is no longer supported in teams_runtime.",
                        "insights": [],
                        "proposals": {
                            "request_handling": {"mode": "complete"},
                        },
                        "artifacts": [],
                        "error": "",
                    }

                service.intent_parser = _FailingIntentParser()
                message = DiscordMessage(
                    message_id="user-approve-1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="tester",
                    content="approve request_id: 20260330-deadbeef",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service.handle_message(message))

                self.assertEqual(len(service.discord_client.sent_channels), 2)
                _channel_id, reply = service.discord_client.sent_channels[1]
                self.assertIn("approval is no longer supported", reply)

    def test_orchestrator_converts_legacy_approval_result_into_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260330-approvalcompat1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "legacy approval compatibility",
                    "body": "legacy approval compatibility",
                    "artifacts": [],
                    "params": {},
                    "current_role": "developer",
                    "next_role": "developer",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-30T00:00:00+00:00",
                    "updated_at": "2026-03-30T00:00:00+00:00",
                    "fingerprint": "approval-compat-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-1",
                    },
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                message = DiscordMessage(
                    message_id="relay-approvalcompat-1",
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
                    request_id="20260330-approvalcompat1",
                    sender="developer",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="legacy approval compatibility",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260330-approvalcompat1",
                            "role": "developer",
                            "status": "awaiting_approval",
                            "summary": "legacy approval result",
                            "insights": [],
                            "proposals": {},
                            "artifacts": [],
                            "next_role": "",
                            "approval_needed": True,
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260330-approvalcompat1")
                self.assertEqual(updated["status"], "blocked")
                self.assertEqual(updated["result"]["status"], "blocked")
                self.assertNotIn("approval_needed", updated["result"])
                self.assertIn("approval flow is no longer supported", updated["result"]["error"])
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, reply = service.discord_client.sent_channels[0]
                self.assertIn("차단됨", reply)

    def test_orchestrator_loads_role_report_from_body_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260323-bodyjson1",
                    "status": "delegated",
                    "intent": "plan",
                    "urgency": "normal",
                    "scope": "intraday trading 개선 방안 기획",
                    "body": "intraday trading 개선 방안 기획",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-23T00:00:00+00:00",
                    "updated_at": "2026-03-23T00:00:00+00:00",
                    "fingerprint": "f",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-1",
                    },
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                envelope = MessageEnvelope(
                    request_id="20260323-bodyjson1",
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="intraday trading 개선 방안 기획",
                    params={"_teams_kind": "report"},
                    body='```json\n{"request_id":"20260323-bodyjson1","role":"planner","status":"completed","summary":"본문 JSON에서 복구했습니다.","next_role":"","approval_needed":false,"artifacts":[],"proposals":{},"error":""}\n```',
                )
                message = DiscordMessage(
                    message_id="relay-6",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id="111111111111111113",
                    author_name="planner",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260323-bodyjson1")
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(updated["result"]["summary"], "본문 JSON에서 복구했습니다.")

    def test_orchestrator_marks_invalid_role_contract_reports_distinctly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260428-invalid-contract-1",
                    "status": "delegated",
                    "intent": "plan",
                    "urgency": "normal",
                    "scope": "planner finalize invalid contract",
                    "body": "planner finalize invalid contract",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-04-28T00:00:00+00:00",
                    "updated_at": "2026-04-28T00:00:00+00:00",
                    "fingerprint": "f",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-1",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "message-1",
                    },
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                envelope = MessageEnvelope(
                    request_id="20260428-invalid-contract-1",
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="planner finalize invalid contract",
                    params={"_teams_kind": "report"},
                    body='```json\n{"request_id":"20260428-invalid-contract-1","role":"planner","status":"completed|blocked|failed","summary":"short Korean summary","insights":["private role insight for journal.md"],"proposals":{},"artifacts":[],"error":""}\n```',
                )
                message = DiscordMessage(
                    message_id="relay-invalid-contract",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id="111111111111111113",
                    author_name="planner",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260428-invalid-contract-1")
                self.assertEqual(updated["status"], "failed")
                self.assertEqual(updated["result"]["contract_status"], "invalid")
                self.assertIn("copied_prompt_status_enum_literal", updated["result"]["contract_issues"])
                history_text = service.paths.role_history_file("orchestrator").read_text(encoding="utf-8")
                journal_text = service.paths.role_journal_file("orchestrator").read_text(encoding="utf-8")
                self.assertIn("invalid_role_payload", history_text)
                self.assertIn("invalid_role_payload", journal_text)

    def test_orchestrator_recovers_chunk_merged_qa_report_body_and_routes_to_planner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                planner_bot_id = service.discord_config.get_role("planner").bot_id
                request_record = {
                    "request_id": "20260325-qa2planner1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "intraday trading methodology improvement",
                    "body": "intraday_trading 방법론을 고도화해달라는 요청",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "sprint_id": "2026-Sprint-01-20260325T212259KST",
                        "backlog_id": "backlog-20260325-64b1f52c",
                        "todo_id": "todo-212259-bb7063",
                    },
                    "current_role": "qa",
                    "next_role": "qa",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01-20260325T212259KST",
                    "backlog_id": "backlog-20260325-64b1f52c",
                    "todo_id": "todo-212259-bb7063",
                    "created_at": "2026-03-25T21:23:02.118515+09:00",
                    "updated_at": "2026-03-25T21:23:02.118515+09:00",
                    "fingerprint": "qa-to-planner-fp",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["user"],
                }
                service._save_request(request_record)
                envelope = MessageEnvelope(
                    request_id="20260325-qa2planner1",
                    sender="qa",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="intraday trading methodology improvement",
                    params={"_teams_kind": "report"},
                    body="""```json
{
  "approval_needed": false,
  "artifacts": [
    "./workspace/apps/김단타/AGENTS.md"
  ],
  "error": "",
  "insights": [],
  "next_role": "planner",
  "proposals": {
    "suggested_next_step": {
      "owner": "planner"
    }
  },
```
```json
  "request_id": "20260325-qa2planner1",
  "role": "qa",
  "status": "completed",
  "summary": "현재 방법론은 planner가 후속 구조화를 맡는 것이 적절합니다."
}
```""",
                )
                message = DiscordMessage(
                    message_id="relay-qa2planner-1",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id=service.discord_config.get_role("qa").bot_id,
                    author_name="qa",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260325-qa2planner1")
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "planner")
                self.assertEqual(updated["next_role"], "planner")
                self.assertEqual(updated["result"]["role"], "qa")
                self.assertEqual(updated["result"]["next_role"], "")
                self.assertIn("qa", updated["visited_roles"])
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, relay_content = service.discord_client.sent_channels[0]
                self.assertIn(f"<@{planner_bot_id}>", relay_content)
                self.assertIn("intent: plan", relay_content)

    def test_orchestrator_role_report_skips_requester_reply_when_reply_route_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260324-internal1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "internal sprint todo",
                    "body": "internal sprint todo",
                    "artifacts": [],
                    "params": {"_teams_kind": "sprint_internal"},
                    "current_role": "qa",
                    "next_role": "qa",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-24T00:00:00+00:00",
                    "updated_at": "2026-03-24T00:00:00+00:00",
                    "fingerprint": "internal-fp",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                envelope = MessageEnvelope(
                    request_id="20260324-internal1",
                    sender="qa",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="internal sprint todo",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260324-internal1",
                            "role": "qa",
                            "status": "completed",
                            "summary": "내부 작업을 마쳤습니다.",
                            "next_role": "",
                            "approval_needed": False,
                            "artifacts": [],
                            "proposals": {},
                            "error": "",
                        },
                    },
                )
                message = DiscordMessage(
                    message_id="relay-internal-1",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id="111111111111111117",
                    author_name="qa",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260324-internal1")
                self.assertEqual(updated["status"], "completed")
                self.assertEqual(updated["result"]["summary"], "내부 작업을 마쳤습니다.")
                self.assertEqual(service.discord_client.sent_channels, [])
                self.assertEqual(service.discord_client.sent_dms, [])

    def test_orchestrator_role_report_recovers_reply_route_from_original_requester_params(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260324-replyroute1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "reply route recovery",
                    "body": "reply route recovery",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "forward",
                        "original_requester": {
                            "author_id": "user-1",
                            "author_name": "tester",
                            "channel_id": "channel-recovered",
                            "guild_id": "guild-1",
                            "is_dm": False,
                            "message_id": "msg-origin-1",
                        },
                    },
                    "current_role": "developer",
                    "next_role": "developer",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-24T00:00:00+00:00",
                    "updated_at": "2026-03-24T00:00:00+00:00",
                    "fingerprint": "replyroute-fp",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                envelope = MessageEnvelope(
                    request_id="20260324-replyroute1",
                    sender="developer",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="reply route recovery",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260324-replyroute1",
                            "role": "developer",
                            "status": "completed",
                            "summary": "답장 경로를 복구했습니다.",
                            "next_role": "",
                            "approval_needed": False,
                            "artifacts": [],
                            "proposals": {},
                            "error": "",
                        },
                    },
                )
                message = DiscordMessage(
                    message_id="relay-replyroute-1",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id="111111111111111116",
                    author_name="developer",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260324-replyroute1")
                self.assertEqual(updated["reply_route"]["channel_id"], "channel-recovered")
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                self.assertEqual(service.discord_client.sent_channels[0][0], "channel-recovered")
                self.assertIn("답장 경로를 복구했습니다.", service.discord_client.sent_channels[0][1])

    def test_reply_to_requester_logs_diagnostics_when_channel_id_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260324-replydiag1",
                    "status": "completed",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "missing reply route diagnostics",
                    "body": "missing reply route diagnostics",
                    "artifacts": [],
                    "params": {"_teams_kind": "delegate"},
                    "current_role": "orchestrator",
                    "next_role": "",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-24T00:00:00+00:00",
                    "updated_at": "2026-03-24T00:00:00+00:00",
                    "fingerprint": "replydiag-fp",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                }

                with patch("teams_runtime.core.notifications.LOGGER.warning") as warning_mock:
                    asyncio.run(service._reply_to_requester(request_record, "status update"))

                warning_mock.assert_called_once()
                self.assertIn("channel_id is missing", warning_mock.call_args.args[0])
                self.assertIn("route_source", warning_mock.call_args.args[0])

    def test_handle_message_routes_relay_request_through_local_orchestrator_when_no_active_sprint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                observed: dict[str, str] = {}

                def fake_orchestrator_run_task(envelope, request_record):
                    observed["body"] = str(request_record.get("body") or "")
                    observed["intent"] = str(request_record.get("intent") or "")
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "active sprint 없이도 orchestrator agent가 먼저 요청을 접수했습니다.",
                        "insights": [],
                        "proposals": {
                            "request_handling": {"mode": "complete"},
                        },
                        "artifacts": [],
                        "error": "",
                    }

                relay_channel_id = service.discord_config.relay_channel_id
                message = DiscordMessage(
                    message_id="relay-no-sprint-1",
                    channel_id=relay_channel_id,
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="user",
                    content="김단타를 개선해줘",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service.handle_message(message))

                scheduler_state = service._load_scheduler_state()
                self.assertEqual(str(scheduler_state.get("active_sprint_id") or ""), "")
                self.assertEqual(service.discord_client.sent_channels[0], (relay_channel_id, "<@user-1> 수신양호"))
                self.assertIn("완료", service.discord_client.sent_channels[1][1])
                self.assertEqual(observed["body"], "김단타를 개선해줘")
                self.assertEqual(observed["intent"], "route")
                self.assertEqual(len(list(service.paths.requests_dir.glob("*.json"))), 1)

    def test_handle_forwarded_relay_route_uses_local_orchestrator_agent_when_no_active_sprint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                trusted_author_id = next(iter(service.discord_config.trusted_bot_ids))
                observed: dict[str, str] = {}
                forwarded = MessageEnvelope(
                    request_id="",
                    sender="planner",
                    target="orchestrator",
                    intent="route",
                    urgency="normal",
                    scope="김단타 개선",
                    params={
                        "_teams_kind": "forward",
                        "original_requester": {
                            "author_id": "user-1",
                            "author_name": "user",
                            "channel_id": service.discord_config.relay_channel_id,
                            "guild_id": "guild-1",
                            "is_dm": False,
                            "message_id": "user-msg-1",
                        },
                    },
                    body="김단타를 개선해줘",
                )
                def fake_orchestrator_run_task(envelope, request_record):
                    observed["body"] = str(request_record.get("body") or "")
                    observed["intent"] = str(request_record.get("intent") or "")
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "forwarded relay 요청도 orchestrator agent가 먼저 처리했습니다.",
                        "insights": [],
                        "proposals": {
                            "request_handling": {"mode": "complete"},
                        },
                        "artifacts": [],
                        "error": "",
                    }

                message = DiscordMessage(
                    message_id="relay-forward-no-sprint-1",
                    channel_id=service.discord_config.relay_channel_id,
                    guild_id="guild-1",
                    author_id=trusted_author_id,
                    author_name="planner",
                    content=envelope_to_text(forwarded),
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service.handle_message(message))

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                self.assertEqual(service.discord_client.sent_channels[0][0], service.discord_config.relay_channel_id)
                self.assertIn("완료", service.discord_client.sent_channels[0][1])
                self.assertEqual(observed["body"], "김단타를 개선해줘")
                self.assertEqual(observed["intent"], "route")
                self.assertEqual(len(list(service.paths.requests_dir.glob("*.json"))), 1)
