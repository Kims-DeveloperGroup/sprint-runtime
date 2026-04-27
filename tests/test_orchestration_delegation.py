from orchestration_test_utils import *


class TeamsRuntimeOrchestrationDelegationTests(OrchestrationTestCase):
    def test_non_orchestrator_forwards_user_channel_message_without_visible_ack(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                message = DiscordMessage(
                    message_id="msg-2",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-2",
                    author_name="tester",
                    content="<@111111111111111113>\nintent: plan\nscope: first task",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service.handle_message(message))

                self.assertEqual(len(service.discord_client.sent_channels), 2)
                self.assertEqual(service.discord_client.sent_channels[0], ("channel-1", "<@user-2> 수신양호"))
                self.assertEqual(service.discord_client.sent_channels[1][0], "111111111111111111")

    def test_non_orchestrator_ignores_human_message_targeted_to_other_role(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                developer_bot_id = service.discord_config.get_role("developer").bot_id
                message = DiscordMessage(
                    message_id="msg-3",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-3",
                    author_name="tester",
                    content=f"<@{developer_bot_id}>\nintent: implement\nscope: fix runtime log bug",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service.handle_message(message))

                self.assertEqual(service.discord_client.sent_channels, [("channel-1", "<@user-3> 수신양호")])

    def test_non_orchestrator_forwards_with_generated_request_id_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                planner_bot_id = service.discord_config.get_role("planner").bot_id
                message = DiscordMessage(
                    message_id="msg-4",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-4",
                    author_name="tester",
                    content=f"<@{planner_bot_id}>\nintent: plan\nscope: runtime log 오류 확인",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service.handle_message(message))

                self.assertEqual(len(service.discord_client.sent_channels), 2)
                self.assertEqual(service.discord_client.sent_channels[0], ("channel-1", "<@user-4> 수신양호"))
                _channel_id, relay_content = service.discord_client.sent_channels[1]
                match = re.search(r"request_id:\s*([A-Za-z0-9._-]+)", relay_content)
                self.assertIsNotNone(match)
                self.assertNotEqual(match.group(1).strip(), "")

    def test_public_research_target_is_preserved_when_forwarding_to_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "research")
                research_bot_id = service.discord_config.get_role("research").bot_id
                service._send_relay = AsyncMock(return_value=True)
                message = DiscordMessage(
                    message_id="msg-research-forward-1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-research-1",
                    author_name="tester",
                    content=f"<@{research_bot_id}>\nintent: route\nscope: compare current API pricing with sources",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service.handle_message(message))

                service._send_relay.assert_awaited_once()
                forwarded = service._send_relay.await_args.args[0]
                self.assertEqual(forwarded.sender, "research")
                self.assertEqual(forwarded.target, "orchestrator")
                self.assertEqual(forwarded.intent, "route")
                self.assertEqual(forwarded.params["user_requested_role"], "research")
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                self.assertEqual(service.discord_client.sent_channels[0], ("channel-1", "<@user-research-1> 수신양호"))

    def test_delegated_request_failure_is_reported_back_to_orchestrator(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                request_record = {
                    "request_id": "20260323-failed123",
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
                service.role_runtime.run_task = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    FileNotFoundError("missing session workspace")
                )
                envelope = MessageEnvelope(
                    request_id="20260323-failed123",
                    sender="orchestrator",
                    target="planner",
                    intent="plan",
                    urgency="normal",
                    scope="intraday trading 개선 방안 기획",
                    params={"_teams_kind": "delegate"},
                )
                message = DiscordMessage(
                    message_id="relay-1",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id="111111111111111112",
                    author_name="orchestrator",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service._handle_delegated_request(message, envelope))

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, content = service.discord_client.sent_channels[0]
                self.assertIn("intent: report", content)
                self.assertNotIn("report_ref", content)
                self.assertIn("```json", content)
                self.assertIn('"error": "missing session workspace"', content)
                persisted = service._load_request("20260323-failed123")
                self.assertEqual(persisted["result"]["role"], "planner")
                self.assertEqual(persisted["result"]["status"], "failed")
                self.assertEqual(persisted["result"]["error"], "missing session workspace")
                journal_text = service.paths.role_journal_file("planner").read_text(encoding="utf-8")
                self.assertIn("missing session workspace", journal_text)

    def test_delegated_request_persists_role_output_on_request_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                request_record = {
                    "request_id": "20260323-source123",
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
                service.role_runtime.run_task = lambda *_args, **_kwargs: {
                    "request_id": "20260323-source123",
                    "role": "planner",
                    "status": "completed",
                    "summary": "intraday trading 개선 초안을 작성했습니다.",
                    "insights": ["프로그램 순매수 반전 여부를 다음 역할이 검증해야 합니다."],
                    "proposals": {"plan": ["A", "B"]},
                    "artifacts": [],
                    "next_role": "architect",
                    "approval_needed": False,
                    "error": "",
                }
                envelope = MessageEnvelope(
                    request_id="20260323-source123",
                    sender="orchestrator",
                    target="planner",
                    intent="plan",
                    urgency="normal",
                    scope="intraday trading 개선 방안 기획",
                    params={"_teams_kind": "delegate"},
                )
                message = DiscordMessage(
                    message_id="relay-4",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id="111111111111111112",
                    author_name="orchestrator",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service._handle_delegated_request(message, envelope))

                persisted = service._load_request("20260323-source123")
                self.assertEqual(persisted["result"]["summary"], "intraday trading 개선 초안을 작성했습니다.")
                self.assertEqual(persisted["result"]["next_role"], "")
                self.assertFalse((service.paths.role_sources_dir("planner") / "20260323-source123.md").exists())
                self.assertFalse((service.paths.role_sources_dir("planner") / "20260323-source123.json").exists())
                self.assertFalse((service.paths.runtime_root / "role_reports" / "planner" / "20260323-source123.md").exists())
                self.assertFalse((service.paths.runtime_root / "role_reports" / "planner" / "20260323-source123.json").exists())
                history_text = service.paths.role_history_file("planner").read_text(encoding="utf-8")
                journal_text = service.paths.role_journal_file("planner").read_text(encoding="utf-8")
                self.assertIn("intraday trading 개선 초안을 작성했습니다.", history_text)
                self.assertIn("프로그램 순매수 반전 여부를 다음 역할이 검증해야 합니다.", history_text)
                self.assertIn("프로그램 순매수 반전 여부를 다음 역할이 검증해야 합니다.", journal_text)

    def test_internal_sprint_delegated_request_records_recent_activity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                sprint_state = {
                    "sprint_id": "260331-Sprint-08:47",
                    "sprint_name": "agent-activity-debug",
                    "sprint_display_name": "agent-activity-debug",
                    "sprint_folder": "shared_workspace/sprints/agent-activity-debug",
                    "phase": "ongoing",
                    "milestone_title": "agent activity visibility",
                    "status": "running",
                    "trigger": "manual_start",
                    "started_at": "2026-03-31T08:47:15+09:00",
                    "ended_at": "",
                    "selected_items": [],
                    "todos": [],
                }
                service._save_sprint_state(sprint_state)
                request_record = {
                    "request_id": "20260331-activity123",
                    "status": "delegated",
                    "intent": "plan",
                    "urgency": "normal",
                    "scope": "agent activity visibility",
                    "body": "agent activity visibility",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "sprint_id": "260331-Sprint-08:47",
                        "todo_id": "todo-activity-1",
                        "backlog_id": "backlog-activity-1",
                    },
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "260331-Sprint-08:47",
                    "todo_id": "todo-activity-1",
                    "backlog_id": "backlog-activity-1",
                    "created_at": "2026-03-31T08:47:15+09:00",
                    "updated_at": "2026-03-31T08:47:15+09:00",
                    "fingerprint": "f",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)
                service.role_runtime.run_task = lambda *_args, **_kwargs: {
                    "request_id": "20260331-activity123",
                    "role": "planner",
                    "status": "completed",
                    "summary": "planner 초안 작성이 끝났습니다.",
                    "insights": [],
                    "proposals": {},
                    "artifacts": ["shared_workspace/planning.md"],
                    "next_role": "",
                    "approval_needed": False,
                    "error": "",
                    "session_id": "session-activity-1",
                    "session_workspace": "/tmp/planner-session",
                }
                service._send_relay = AsyncMock(return_value=True)
                envelope = MessageEnvelope(
                    request_id="20260331-activity123",
                    sender="orchestrator",
                    target="planner",
                    intent="plan",
                    urgency="normal",
                    scope="agent activity visibility",
                    params={
                        "_teams_kind": "delegate",
                        "_origin": "sprint_internal",
                        "sprint_id": "260331-Sprint-08:47",
                        "todo_id": "todo-activity-1",
                        "backlog_id": "backlog-activity-1",
                    },
                )
                message = DiscordMessage(
                    message_id="relay-activity-1",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id="111111111111111112",
                    author_name="orchestrator",
                    content="relay",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                with self.assertLogs("teams_runtime.core.orchestration", level="INFO") as captured:
                    asyncio.run(service._handle_delegated_request(message, envelope))

                updated_sprint = service._load_sprint_state("260331-Sprint-08:47")
                activity_types = [str(item.get("event_type") or "") for item in updated_sprint.get("recent_activity") or []]
                self.assertIn("role_started", activity_types)
                self.assertIn("role_result", activity_types)
                current_sprint_text = service.paths.current_sprint_file.read_text(encoding="utf-8")
                self.assertIn("## Recent Activity", current_sprint_text)
                self.assertIn("role=planner | event=role_started", current_sprint_text)
                self.assertIn("planner 초안 작성이 끝났습니다.", current_sprint_text)
                events = [
                    json.loads(line)
                    for line in service.paths.sprint_events_file("260331-Sprint-08:47").read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                event_types = [str(item.get("type") or "") for item in events]
                self.assertIn("role_started", event_types)
                self.assertIn("role_result", event_types)
                joined_logs = "\n".join(captured.output)
                self.assertIn("sprint_activity role=planner event=role_started", joined_logs)
                self.assertIn("request_id=20260331-activity123", joined_logs)

    def test_initial_milestone_research_activity_is_labeled_before_planner_refinement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "research")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="research-first manual sprint",
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
                request_record["current_role"] = "research"
                request_record["next_role"] = "research"
                service._save_request(request_record)
                service._send_relay = AsyncMock(return_value=True)
                service.role_runtime.run_task = lambda *_args, **_kwargs: {
                    "request_id": request_record["request_id"],
                    "role": "research",
                    "status": "completed",
                    "summary": "research prepass를 planner에 전달했습니다.",
                    "insights": [],
                    "proposals": {
                        "research_signal": {
                            "needed": False,
                            "subject": "",
                            "research_query": "",
                            "reason_code": "not_needed_no_subject",
                        }
                    },
                    "artifacts": [],
                    "next_role": "",
                    "approval_needed": False,
                    "error": "",
                }
                envelope = MessageEnvelope(
                    request_id=request_record["request_id"],
                    sender="orchestrator",
                    target="research",
                    intent="research",
                    urgency="normal",
                    scope=request_record["scope"],
                    body=request_record["body"],
                    params={
                        "_teams_kind": "delegate",
                        "_origin": "sprint_internal",
                        "sprint_id": sprint_state["sprint_id"],
                    },
                )

                asyncio.run(service._process_delegated_request(envelope, request_record))

                updated_sprint = service._load_sprint_state(sprint_state["sprint_id"])
                started_activity = [
                    item
                    for item in updated_sprint.get("recent_activity") or []
                    if item.get("event_type") == "role_started"
                ][0]
                self.assertEqual(started_activity["role"], "research")
                self.assertIn("research prepass", started_activity["summary"])
                self.assertNotIn("milestone 정리", started_activity["summary"])
                current_sprint_text = service.paths.current_sprint_file.read_text(encoding="utf-8")
                self.assertIn("role=research | event=role_started", current_sprint_text)
                self.assertIn("research prepass", current_sprint_text)

    def test_pending_role_request_resume_loop_picks_up_request_created_after_startup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                request_record = {
                    "request_id": "20260403-late-resume",
                    "status": "delegated",
                    "intent": "plan",
                    "urgency": "normal",
                    "scope": "late delegated resume",
                    "body": "late delegated resume",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "",
                    "created_at": "2026-04-03T16:08:50+09:00",
                    "updated_at": "2026-04-03T16:08:50+09:00",
                    "fingerprint": "late-resume-fingerprint",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                }
                service.role_runtime.run_task = lambda *_args, **_kwargs: {
                    "request_id": "20260403-late-resume",
                    "role": "planner",
                    "status": "completed",
                    "summary": "late delegated request를 planner가 처리했습니다.",
                    "insights": [],
                    "proposals": {},
                    "artifacts": [],
                    "next_role": "",
                    "approval_needed": False,
                    "error": "",
                }
                service._send_relay = AsyncMock(return_value=True)

                sleep_calls = {"count": 0}

                async def fake_sleep(_seconds):
                    sleep_calls["count"] += 1
                    if sleep_calls["count"] == 1:
                        service._save_request(dict(request_record))
                        return None
                    raise asyncio.CancelledError()

                with (
                    patch("teams_runtime.core.orchestration.asyncio.sleep", side_effect=fake_sleep),
                    self.assertRaises(asyncio.CancelledError),
                ):
                    asyncio.run(service._resume_pending_role_requests_loop())

                updated = service._load_request("20260403-late-resume")
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["result"]["status"], "completed")
                history_text = service.paths.role_history_file("planner").read_text(encoding="utf-8")
                self.assertIn("late delegated request를 planner가 처리했습니다.", history_text)
                service._send_relay.assert_awaited()

    def test_delegate_envelope_preserves_original_requester_channel_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260324-origreq1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "preserve requester route",
                    "body": "preserve requester route",
                    "artifacts": [],
                    "params": {"_teams_kind": "forward"},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01",
                    "created_at": "2026-03-24T00:00:00+00:00",
                    "updated_at": "2026-03-24T00:00:00+00:00",
                    "fingerprint": "origreq-fp",
                    "reply_route": {
                        "author_id": "user-1",
                        "author_name": "tester",
                        "channel_id": "channel-preserved",
                        "guild_id": "guild-1",
                        "is_dm": False,
                        "message_id": "msg-origin-2",
                    },
                    "events": [],
                    "result": {},
                }

                envelope = service._build_delegate_envelope(request_record, "planner")

                self.assertEqual(envelope.params["original_requester"]["channel_id"], "channel-preserved")

    def test_internal_delegate_includes_sprint_metadata_in_relay_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260324-internal-meta",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "internal sprint todo",
                    "body": "internal sprint todo body",
                    "artifacts": [],
                    "params": {"_teams_kind": "sprint_internal"},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01-20260324T000000Z",
                    "backlog_id": "backlog-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-03-24T00:00:00+00:00",
                    "updated_at": "2026-03-24T00:00:00+00:00",
                    "fingerprint": "internal-meta",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": [],
                }

                asyncio.run(service._delegate_request(request_record, "planner"))

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, content = service.discord_client.sent_channels[0]
                self.assertIn("params:", content)
                self.assertIn("\"_origin\": \"sprint_internal\"", content)
                self.assertIn("\"sprint_id\": \"2026-Sprint-01-20260324T000000Z\"", content)
                self.assertIn("\"todo_id\": \"todo-1\"", content)
                self.assertIn("\"backlog_id\": \"backlog-1\"", content)

    def test_delegate_request_includes_compact_handoff_summary_and_request_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260325-handoff1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "알림 UX 개편",
                    "body": "버튼 레이블과 안내 문구를 함께 조정해줘",
                    "artifacts": ["shared_workspace/planning.md"],
                    "params": {},
                    "current_role": "designer",
                    "next_role": "designer",
                    "owner_role": "orchestrator",
                    "created_at": "2026-03-25T09:00:00+09:00",
                    "updated_at": "2026-03-25T09:00:00+09:00",
                    "fingerprint": "handoff1",
                    "reply_route": {},
                    "events": [],
                        "result": {
                            "request_id": "20260325-handoff1",
                            "role": "planner",
                            "status": "completed",
                            "summary": "디자인 정리가 필요한 UI 변경 요구사항으로 구조화했습니다.",
                            "insights": [],
                            "proposals": {
                                "acceptance_criteria": ["버튼 레이블이 역할에 맞게 정리된다."],
                                "required_inputs": ["현재 문구 목록"],
                            },
                            "artifacts": [".teams_runtime/requests/20260325-handoff1.json"],
                            "next_role": "designer",
                            "approval_needed": False,
                            "error": "",
                        },
                    "visited_roles": ["planner"],
                }

                asyncio.run(service._delegate_request(request_record, "designer"))

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, content = service.discord_client.sent_channels[0]
                self.assertIn("handoff | planner -> designer | route", content)
                self.assertIn("[핵심 전달]", content)
                self.assertIn("- 버튼 레이블과 안내 문구를 함께 조정해줘", content)
                self.assertIn("[이관 이유]", content)
                self.assertIn("- 다음 역할: designer", content)
                self.assertIn("[유의사항]", content)
                self.assertIn("추가 입력: 현재 문구 목록", content)
                self.assertIn("완료 기준: 버튼 레이블이 역할에 맞게 정리된다.", content)
                self.assertIn("[참고 파일]", content)
                self.assertNotIn("\"proposals\":", content)
                snapshot_file = service.paths.role_request_snapshot_file("designer", "20260325-handoff1")
                self.assertTrue(snapshot_file.exists())
                snapshot_text = snapshot_file.read_text(encoding="utf-8")
                self.assertIn("canonical_request: .teams_runtime/requests/20260325-handoff1.json", snapshot_text)
                self.assertIn("previous_role: planner", snapshot_text)
                self.assertIn("what_summary: 디자인 정리가 필요한 UI 변경 요구사항으로 구조화했습니다.", snapshot_text)
                self.assertIn("what_details:", snapshot_text)
                self.assertIn("how_summary:", snapshot_text)
                self.assertIn("latest_context:", snapshot_text)
                self.assertIn("reference_artifacts: shared_workspace/planning.md", snapshot_text)

    def test_delegate_request_does_not_duplicate_constraint_points_in_check_now(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260411-handoff-duplicate-1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "handoff duplicate guard",
                    "body": "handoff duplicate guard",
                    "artifacts": ["shared_workspace/planning.md"],
                    "params": {"_teams_kind": "sprint_internal"},
                    "current_role": "architect",
                    "next_role": "architect",
                    "owner_role": "orchestrator",
                    "created_at": "2026-04-11T00:00:00+09:00",
                    "updated_at": "2026-04-11T00:00:00+09:00",
                    "fingerprint": "handoff-dup-1",
                    "reply_route": {},
                    "events": [],
                    "result": {
                        "request_id": "20260411-handoff-duplicate-1",
                        "role": "architect",
                        "status": "completed",
                        "summary": "중복 노출 회귀를 검증합니다.",
                        "insights": [],
                        "proposals": {
                            "required_inputs": ["현재 문구 목록"],
                            "acceptance_criteria": ["버튼 레이블이 역할에 맞게 정리된다."],
                        },
                        "artifacts": ["shared_workspace/planning.md"],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "",
                    },
                    "visited_roles": ["architect"],
                }

                delegation_context = service._build_delegation_context(request_record, "designer")
                body = service._build_delegate_body(request_record, delegation_context)

                self.assertIn("[유의사항]", body)
                self.assertIn("추가 입력: 현재 문구 목록", body)
                self.assertIn("완료 기준: 버튼 레이블이 역할에 맞게 정리된다.", body)
                self.assertEqual(body.count("현재 문구 목록"), 1)
                self.assertEqual(body.count("버튼 레이블이 역할에 맞게 정리된다."), 1)

    def test_delegate_request_surfaces_designer_design_feedback_in_handoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260410-designer-handoff-1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "designer advisory handoff",
                    "body": "designer advisory handoff",
                    "artifacts": ["shared_workspace/sprints/spec.md"],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "planning",
                            "step": "planner_advisory",
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
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "created_at": "2026-04-10T00:00:00+09:00",
                    "updated_at": "2026-04-10T00:00:00+09:00",
                    "fingerprint": "designer-handoff",
                    "reply_route": {},
                    "events": [],
                    "result": {
                        "request_id": "20260410-designer-handoff-1",
                        "role": "designer",
                        "status": "completed",
                        "summary": "",
                        "insights": [],
                        "proposals": {
                            "design_feedback": {
                                "entry_point": "info_prioritization",
                                "user_judgment": [
                                    "현재 상태와 다음 액션을 첫 줄에 고정해야 합니다.",
                                    "추적 근거는 핵심 결론 뒤로 내려도 이해가 유지됩니다.",
                                ],
                                "message_priority": {
                                    "lead": "현재 상태, 다음 액션",
                                    "defer": "근거 로그, 상세 배경",
                                },
                                "routing_rationale": "planner가 정보 우선순위를 spec에 흡수하면 이후 status/report wording이 흔들리지 않습니다.",
                            },
                            "workflow_transition": {
                                "outcome": "advance",
                                "target_phase": "planning",
                                "target_step": "planner_finalize",
                                "requested_role": "",
                                "reopen_category": "",
                                "reason": "designer 판단을 planner finalization에 반영합니다.",
                                "unresolved_items": [],
                                "finalize_phase": False,
                            },
                        },
                        "artifacts": ["shared_workspace/sprints/spec.md"],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "",
                    },
                    "visited_roles": ["planner", "designer"],
                }

                asyncio.run(service._delegate_request(request_record, "planner"))

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, content = service.discord_client.sent_channels[0]
                self.assertIn("handoff | designer -> planner | route", content)
                self.assertIn("[전달 정보]", content)
                self.assertIn("- 전달 경로: start -> planning/planner_advisory@planner", content)
                self.assertIn("[참고 파일]", content)
                self.assertNotIn("[이관 이유]", content)
                self.assertNotIn("[지금 볼 것]", content)
                self.assertNotIn("판단 지점", content)
                self.assertNotIn("UX 판단", content)
                self.assertNotIn("지원 역할", content)
                snapshot_file = service.paths.role_request_snapshot_file("planner", "20260410-designer-handoff-1")
                self.assertTrue(snapshot_file.exists())
                snapshot_text = snapshot_file.read_text(encoding="utf-8")
                self.assertIn("what_summary: info prioritization 관점 UX 판단 2건을 정리했습니다.", snapshot_text)
                self.assertIn("how_summary: 핵심 레이어: 현재 상태, 다음 액션", snapshot_text)

    def test_delegate_request_surfaces_planner_support_roles_in_handoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260411-support-roles-handoff-1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "designer support role planning",
                    "body": "designer support role planning",
                    "artifacts": ["shared_workspace/sprints/spec.md"],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "planning",
                            "step": "planner_finalize",
                            "phase_owner": "planner",
                            "phase_status": "active",
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
                    "created_at": "2026-04-11T00:00:00+09:00",
                    "updated_at": "2026-04-11T00:00:00+09:00",
                    "fingerprint": "support-roles-handoff",
                    "reply_route": {},
                    "events": [],
                    "result": {
                        "request_id": "20260411-support-roles-handoff-1",
                        "role": "planner",
                        "status": "completed",
                        "summary": "",
                        "insights": [],
                        "proposals": {
                            "planning_contract": {
                                "selected_support_roles": [
                                    {
                                        "role": "architect",
                                        "support_rationale": [
                                            "designer 판단을 runtime contract와 구현 가이드로 번역합니다."
                                        ],
                                        "collaboration_points": [
                                            "planner/designer 판단 항목을 architect가 schema와 tests 계약으로 구조화합니다."
                                        ],
                                    },
                                    {
                                        "role": "qa",
                                        "support_rationale": [
                                            "designer 의도가 실제 사용자-facing 결과에 유지되는지 검증합니다."
                                        ],
                                        "collaboration_points": [
                                            "UX drift가 보이면 qa가 evidence와 함께 ux reopen을 엽니다."
                                        ],
                                    },
                                ],
                                "role_combination_rules": [
                                    "designer는 판단 원천 역할이며 architect/qa는 보조 역할이다."
                                ],
                            },
                            "workflow_transition": {
                                "outcome": "advance",
                                "target_phase": "planning",
                                "target_step": "advisory",
                                "requested_role": "architect",
                                "reopen_category": "",
                                "reason": "architect advisory로 support role 경계를 검증합니다.",
                                "unresolved_items": [],
                                "finalize_phase": False,
                            },
                        },
                        "artifacts": ["shared_workspace/sprints/spec.md"],
                        "next_role": "",
                        "approval_needed": False,
                        "error": "",
                    },
                    "visited_roles": ["planner"],
                }

                asyncio.run(service._delegate_request(request_record, "architect"))

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, content = service.discord_client.sent_channels[0]
                self.assertIn("handoff | planner -> architect | route", content)
                self.assertIn("[핵심 전달]", content)
                self.assertIn("- designer support role planning", content)
                self.assertIn("[전달 정보]", content)
                self.assertIn("- 전달 경로: start -> planning/planner_finalize@architect", content)
                self.assertIn("[참고 파일]", content)
                self.assertNotIn("[이관 이유]", content)
                self.assertNotIn("[지금 볼 것]", content)
                self.assertNotIn("판단 지점", content)
                self.assertNotIn("UX 판단", content)
                self.assertNotIn("지원 역할", content)
                snapshot_file = service.paths.role_request_snapshot_file("architect", "20260411-support-roles-handoff-1")
                self.assertTrue(snapshot_file.exists())
                snapshot_text = snapshot_file.read_text(encoding="utf-8")
                self.assertIn("what_summary: designer 보조 역할 architect, qa 조합을 정리했습니다.", snapshot_text)
                self.assertIn("how_summary: 지원 역할: architect, qa", snapshot_text)

    def test_delegate_request_includes_planner_concrete_details_in_handoff_body(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260410-planner-handoff-1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "initial sprint planning",
                    "body": "workflow initial",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "created_at": "2026-04-10T00:00:00+09:00",
                    "updated_at": "2026-04-10T00:00:00+09:00",
                    "fingerprint": "planner-handoff",
                    "reply_route": {},
                    "events": [],
                    "result": {
                        "request_id": "20260410-planner-handoff-1",
                        "role": "planner",
                        "status": "completed",
                        "summary": "초기 phase용 plan/spec과 prioritized todo를 정리했습니다.",
                        "insights": [],
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
                        "next_role": "designer",
                        "approval_needed": False,
                        "error": "",
                    },
                    "visited_roles": ["planner"],
                }

                asyncio.run(service._delegate_request(request_record, "designer"))

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, content = service.discord_client.sent_channels[0]
                self.assertIn("handoff | planner -> designer | route", content)
                self.assertIn("[핵심 전달]", content)
                self.assertIn("- workflow initial", content)
                self.assertIn("마일스톤: workflow refined", content)
                self.assertIn("backlog/todo: manual sprint start gate", content)
                self.assertIn("backlog/todo: sprint folder artifact rendering", content)
                self.assertIn("[이관 이유]", content)
                self.assertIn("- 다음 역할: designer", content)
                self.assertIn("[지금 볼 것]", content)
                self.assertIn("- backlog/todo: manual sprint start gate", content)
                self.assertIn("- backlog/todo: sprint folder artifact rendering", content)

    def test_delegate_request_omits_handoff_section_for_first_hop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260325-firsthop1",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "새 backlog 항목 정리",
                    "body": "새 backlog 항목 정리",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "created_at": "2026-03-25T09:00:00+09:00",
                    "updated_at": "2026-03-25T09:00:00+09:00",
                    "fingerprint": "firsthop1",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": [],
                }

                asyncio.run(service._delegate_request(request_record, "planner"))

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, content = service.discord_client.sent_channels[0]
                self.assertIn("handoff | orchestrator -> planner | route", content)
                self.assertIn("[핵심 전달]", content)
                self.assertIn("- 새 backlog 항목 정리", content)
                self.assertIn("[전달 정보]", content)
                self.assertIn("- 전달 경로: orchestrator -> planner", content)
                self.assertIn("[이관 이유]", content)
                self.assertIn("- planner 역할이 현재 단계의 다음 담당입니다.", content)
                self.assertIn("[참고 파일]", content)
                self.assertNotIn("[추가 맥락]", content)
                snapshot_file = service.paths.role_request_snapshot_file("planner", "20260325-firsthop1")
                self.assertTrue(snapshot_file.exists())
                snapshot_text = snapshot_file.read_text(encoding="utf-8")
                self.assertIn("what_summary: N/A", snapshot_text)
                self.assertIn("latest_context: N/A", snapshot_text)
                self.assertIn("Always trust `.teams_runtime/requests/20260325-firsthop1.json`", snapshot_text)

    def test_internal_sprint_delegation_payload_persists_cumulative_routing_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                )
                service._save_sprint_state(sprint_state)

                first_request = {
                    "request_id": "20260417-routing-path-1",
                    "status": "delegated",
                    "intent": "plan",
                    "urgency": "normal",
                    "scope": "initial milestone refinement",
                    "body": "initial milestone refinement",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "sprint_id": sprint_state["sprint_id"],
                        "sprint_phase": "initial",
                        "initial_phase_step": orchestration_module.INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
                    },
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "sprint_id": sprint_state["sprint_id"],
                    "backlog_id": "",
                    "todo_id": "",
                    "created_at": "2026-04-17T00:00:00+09:00",
                    "updated_at": "2026-04-17T00:00:00+09:00",
                    "fingerprint": "routing-path-1",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": [],
                }

                first_payload = service._build_internal_sprint_delegation_payload(first_request, "planner")
                self.assertEqual(
                    first_payload["routing_path"],
                    "start -> initial/milestone_refinement@planner",
                )
                self.assertEqual(
                    first_payload["routing_path_nodes"],
                    ["start", "initial/milestone_refinement@planner"],
                )

                service._record_internal_sprint_activity(
                    first_request,
                    event_type="role_delegated",
                    role="orchestrator",
                    status="delegated",
                    summary="planner 역할로 위임했습니다.",
                    payload=first_payload,
                )

                updated_sprint = service._load_sprint_state(sprint_state["sprint_id"])
                self.assertEqual(
                    updated_sprint["recent_activity"][-1]["routing_path"],
                    "start -> initial/milestone_refinement@planner",
                )
                self.assertEqual(
                    updated_sprint["recent_activity"][-1]["routing_path_nodes"],
                    ["start", "initial/milestone_refinement@planner"],
                )
                sprint_events = service._load_sprint_event_entries(updated_sprint)
                self.assertEqual(
                    sprint_events[-1]["payload"]["routing_path"],
                    "start -> initial/milestone_refinement@planner",
                )

                second_request = {
                    "request_id": "20260417-routing-path-2",
                    "status": "delegated",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "architect guidance",
                    "body": "architect guidance",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "sprint_id": sprint_state["sprint_id"],
                        "backlog_id": "backlog-routing-path",
                        "todo_id": "todo-routing-path",
                        "workflow": {
                            "contract_version": 1,
                            "phase": "implementation",
                            "step": "architect_guidance",
                            "phase_owner": "architect",
                            "phase_status": "active",
                            "planning_pass_count": 0,
                            "planning_pass_limit": 2,
                            "planning_final_owner": "planner",
                            "reopen_source_role": "",
                            "reopen_category": "",
                            "review_cycle_count": 0,
                            "review_cycle_limit": orchestration_module.DEFAULT_WORKFLOW_REVIEW_CYCLE_LIMIT,
                        },
                    },
                    "current_role": "architect",
                    "next_role": "architect",
                    "owner_role": "orchestrator",
                    "sprint_id": sprint_state["sprint_id"],
                    "backlog_id": "backlog-routing-path",
                    "todo_id": "todo-routing-path",
                    "created_at": "2026-04-17T00:05:00+09:00",
                    "updated_at": "2026-04-17T00:05:00+09:00",
                    "fingerprint": "routing-path-2",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": [],
                }

                second_payload = service._build_internal_sprint_delegation_payload(second_request, "architect")
                self.assertEqual(
                    second_payload["routing_path"],
                    "start -> initial/milestone_refinement@planner -> implementation/architect_guidance@architect",
                )
                self.assertEqual(
                    second_payload["routing_path_nodes"],
                    [
                        "start",
                        "initial/milestone_refinement@planner",
                        "implementation/architect_guidance@architect",
                    ],
                )

    def test_internal_sprint_completed_developer_report_delegates_to_qa_via_relay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                request_record = {
                    "request_id": "20260324-internalqa1",
                    "status": "delegated",
                    "intent": "implement",
                    "urgency": "normal",
                    "scope": "qa relay visibility",
                    "body": "qa relay visibility",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "sprint_id": "2026-Sprint-01-20260324T000000Z",
                        "backlog_id": "backlog-qa-visible",
                        "todo_id": "todo-qa-visible",
                    },
                    "current_role": "developer",
                    "next_role": "developer",
                    "owner_role": "orchestrator",
                    "sprint_id": "2026-Sprint-01-20260324T000000Z",
                    "backlog_id": "backlog-qa-visible",
                    "todo_id": "todo-qa-visible",
                    "created_at": "2026-03-24T00:00:00+00:00",
                    "updated_at": "2026-03-24T00:00:00+00:00",
                    "fingerprint": "internal-qa-fp",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                    "visited_roles": ["planner"],
                }
                service._save_request(request_record)

                message = DiscordMessage(
                    message_id="relay-developer-1",
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
                    request_id="20260324-internalqa1",
                    sender="developer",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="qa relay visibility",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "request_id": "20260324-internalqa1",
                            "role": "developer",
                            "status": "completed",
                            "summary": "구현을 마쳤습니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": ["workspace/src/example.py"],
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        },
                    },
                )

                asyncio.run(service._handle_role_report(message, envelope))

                updated = service._load_request("20260324-internalqa1")
                self.assertEqual(updated["status"], "delegated")
                self.assertEqual(updated["current_role"], "qa")
                self.assertEqual(updated["next_role"], "qa")
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, content = service.discord_client.sent_channels[0]
                self.assertIn("<@111111111111111117>", content)
                self.assertIn("request_id: 20260324-internalqa1", content)
                self.assertIn("intent: qa", content)
                self.assertIn("\"_origin\": \"sprint_internal\"", content)
                self.assertIn("\"sprint_id\": \"2026-Sprint-01-20260324T000000Z\"", content)
                self.assertIn("\"todo_id\": \"todo-qa-visible\"", content)
                self.assertIn("\"backlog_id\": \"backlog-qa-visible\"", content)

    def test_internal_sprint_request_record_initializes_workflow_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = {"sprint_id": "2026-Sprint-Workflow", "sprint_folder": ""}
                todo = {
                    "todo_id": "todo-1",
                    "backlog_id": "backlog-1",
                    "owner_role": "planner",
                }
                backlog_item = build_backlog_item(
                    title="workflow item",
                    summary="workflow item summary",
                    kind="feature",
                    source="user",
                    scope="workflow item scope",
                )

                record = service._create_internal_request_record(sprint_state, todo, backlog_item)

                workflow = dict(record["params"]["workflow"])
                self.assertEqual(workflow["contract_version"], 1)
                self.assertEqual(workflow["phase"], "planning")
                self.assertEqual(workflow["step"], "research_initial")
                self.assertEqual(workflow["phase_owner"], "research")
                self.assertEqual(workflow["planning_pass_limit"], 2)
                self.assertEqual(workflow["planning_pass_count"], 0)
                self.assertEqual(workflow["review_cycle_limit"], 3)

    def test_non_orchestrator_ready_resumes_pending_delegated_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                request_record = {
                    "request_id": "20260324-resume123",
                    "status": "delegated",
                    "intent": "plan",
                    "urgency": "normal",
                    "scope": "resume pending plan",
                    "body": "resume pending plan",
                    "artifacts": [],
                    "params": {},
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "created_at": "2026-03-24T00:00:00+00:00",
                    "updated_at": "2026-03-24T00:00:00+00:00",
                    "fingerprint": "resume-fp",
                    "reply_route": {},
                    "events": [],
                    "result": {},
                }
                service._save_request(request_record)

                async def fake_send_relay(envelope):
                    service.discord_client.sent_channels.append((service.discord_config.relay_channel_id, envelope.body))

                service._send_relay = fake_send_relay
                service.role_runtime.run_task = lambda *_args, **_kwargs: {
                    "request_id": "20260324-resume123",
                    "role": "planner",
                    "status": "completed",
                    "summary": "reconnected and resumed",
                    "insights": [],
                    "proposals": {},
                    "artifacts": [],
                    "next_role": "",
                    "approval_needed": False,
                    "error": "",
                }

                asyncio.run(service._resume_pending_role_requests())

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                self.assertIn("reconnected and resumed", service.discord_client.sent_channels[0][1])

    def test_orchestrator_ignores_trusted_relay_messages_without_supported_kind(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                message = DiscordMessage(
                    message_id="relay-2",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id="111111111111111113",
                    author_name="planner",
                    content="[작업 보고]\n- 요청: planner 에이전트 시작",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service.handle_message(message))

                self.assertEqual(service.discord_client.sent_channels, [])
                self.assertEqual(service.discord_client.sent_dms, [])
                self.assertEqual(list(service.paths.requests_dir.glob("*.json")), [])

    def test_non_orchestrator_ignores_trusted_relay_messages_without_delegate_kind(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "designer")
                message = DiscordMessage(
                    message_id="relay-3",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id="111111111111111112",
                    author_name="orchestrator",
                    content="[작업 보고]\n- 요청: orchestrator 에이전트 시작",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service.handle_message(message))

                self.assertEqual(service.discord_client.sent_channels, [])
                self.assertEqual(service.discord_client.sent_dms, [])
