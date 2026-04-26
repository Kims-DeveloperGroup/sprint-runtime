from orchestration_test_utils import *


class TeamsRuntimeOrchestrationManualSprintTests(OrchestrationTestCase):
    def test_manual_sprint_start_requests_milestone_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('  start_mode: "auto"\n', '  start_mode: "manual_daily"\n', 1)
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                message = DiscordMessage(
                    message_id="1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="user",
                    content="start sprint",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=None,
                    sender="user",
                    target="orchestrator",
                    intent="route",
                    urgency="normal",
                    scope="start sprint",
                    body="start sprint",
                )

                asyncio.run(service._handle_manual_sprint_start_request(message, envelope, forwarded=False))

                scheduler_state = service._load_scheduler_state()
                self.assertEqual(str(scheduler_state.get("active_sprint_id") or ""), "")
                self.assertTrue(any("milestone" in content.lower() for _channel_id, content in service.discord_client.sent_channels))

    def test_manual_sprint_start_creates_sprint_folder_and_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('  start_mode: "auto"\n', '  start_mode: "manual_daily"\n', 1)
            config_path.write_text(config_text, encoding="utf-8")

            def swallow_task(coro):
                coro.close()
                return None

            with (
                patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient),
                patch("teams_runtime.core.orchestration.asyncio.create_task", side_effect=swallow_task),
            ):
                service = TeamService(tmpdir, "orchestrator")
                message = DiscordMessage(
                    message_id="1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="user",
                    content="start sprint\nmilestone: sprint workflow initial phase 개선",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=None,
                    sender="user",
                    target="orchestrator",
                    intent="route",
                    urgency="normal",
                    scope="start sprint",
                    body="milestone: sprint workflow initial phase 개선",
                )

                asyncio.run(service._handle_manual_sprint_start_request(message, envelope, forwarded=False))

                scheduler_state = service._load_scheduler_state()
                sprint_id = str(scheduler_state.get("active_sprint_id") or "")
                sprint_state = service._load_sprint_state(sprint_id)
                self.assertTrue(sprint_id)
                self.assertEqual(sprint_state["phase"], "initial")
                self.assertEqual(sprint_state["status"], "planning")
                self.assertEqual(sprint_state["milestone_title"], "sprint workflow initial phase 개선")
                artifact_root = Path(sprint_state["sprint_folder"])
                self.assertEqual(artifact_root.name, build_sprint_artifact_folder_name(sprint_id))
                self.assertTrue((artifact_root / "index.md").exists())
                self.assertTrue((artifact_root / "kickoff.md").exists())
                self.assertTrue((artifact_root / "milestone.md").exists())
                self.assertTrue((artifact_root / "plan.md").exists())
                self.assertTrue((artifact_root / "spec.md").exists())
                self.assertTrue((artifact_root / "todo_backlog.md").exists())
                current_sprint_text = service.paths.current_sprint_file.read_text(encoding="utf-8")
                self.assertIn("phase: initial", current_sprint_text)
                self.assertIn("milestone_title: sprint workflow initial phase 개선", current_sprint_text)
                self.assertEqual(sprint_state["execution_mode"], "manual")

    def test_manual_sprint_start_preserves_kickoff_brief_and_requirements(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('  start_mode: "auto"\n', '  start_mode: "manual_daily"\n', 1)
            config_path.write_text(config_text, encoding="utf-8")

            def swallow_task(coro):
                coro.close()
                return None

            with (
                patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient),
                patch("teams_runtime.core.orchestration.asyncio.create_task", side_effect=swallow_task),
                patch("teams_runtime.core.orchestration.build_active_sprint_id", return_value="260404-Sprint-13:00"),
            ):
                service = TeamService(tmpdir, "orchestrator")
                message = DiscordMessage(
                    message_id="kickoff-1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="user",
                    content=(
                        "start sprint\n"
                        "milestone: KIS 스캘핑 고도화\n"
                        "brief: 호가 반응과 손절 규칙을 먼저 정리\n"
                        "requirements:\n"
                        "- 기존 relay flow는 유지\n"
                        "- planner가 kickoff docs를 source-of-truth로 사용\n"
                    ),
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id="request-kickoff-1",
                    sender="user",
                    target="orchestrator",
                    intent="route",
                    urgency="normal",
                    scope="start sprint",
                    body=(
                        "milestone: KIS 스캘핑 고도화\n"
                        "brief: 호가 반응과 손절 규칙을 먼저 정리\n"
                        "requirements:\n"
                        "- 기존 relay flow는 유지\n"
                        "- planner가 kickoff docs를 source-of-truth로 사용\n"
                    ),
                    artifacts=["./shared_workspace/sprints/2026-Sprint-01/attachments/att-1_brief.md"],
                )
                staged_path = (
                    Path(tmpdir)
                    / "shared_workspace"
                    / "sprints"
                    / "2026-Sprint-01"
                    / "attachments"
                    / "att-1_brief.md"
                )
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path.write_text("# kickoff brief\n", encoding="utf-8")

                asyncio.run(service._handle_manual_sprint_start_request(message, envelope, forwarded=False))

                sprint_state = service._load_active_sprint_state()
                self.assertEqual(sprint_state["requested_milestone_title"], "KIS 스캘핑 고도화")
                self.assertEqual(sprint_state["milestone_title"], "KIS 스캘핑 고도화")
                self.assertEqual(sprint_state["kickoff_brief"], "호가 반응과 손절 규칙을 먼저 정리")
                self.assertEqual(
                    sprint_state["kickoff_requirements"],
                    ["기존 relay flow는 유지", "planner가 kickoff docs를 source-of-truth로 사용"],
                )
                self.assertEqual(sprint_state["kickoff_source_request_id"], "request-kickoff-1")
                self.assertEqual(
                    sprint_state["kickoff_reference_artifacts"],
                    ["./shared_workspace/sprints/260404-Sprint-13-00/attachments/att-1_brief.md"],
                )
                kickoff_text = service._sprint_artifact_paths(sprint_state)["kickoff"].read_text(encoding="utf-8")
                self.assertIn("requested_milestone_title: KIS 스캘핑 고도화", kickoff_text)
                self.assertIn("호가 반응과 손절 규칙을 먼저 정리", kickoff_text)
                self.assertIn("기존 relay flow는 유지", kickoff_text)
                self.assertIn("request-kickoff-1", kickoff_text)
                milestone_text = service._sprint_artifact_paths(sprint_state)["milestone"].read_text(encoding="utf-8")
                self.assertIn("Preserve the original kickoff brief in `kickoff.md`.", milestone_text)
                current_sprint_text = service.paths.current_sprint_file.read_text(encoding="utf-8")
                self.assertIn("## Kickoff", current_sprint_text)
                self.assertIn("kickoff_source_request_id: request-kickoff-1", current_sprint_text)
                self.assertIn("planner가 kickoff docs를 source-of-truth로 사용", current_sprint_text)

    def test_manual_sprint_start_relocates_request_attachments_into_new_sprint_folder(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('  start_mode: "auto"\n', '  start_mode: "manual_daily"\n', 1)
            config_path.write_text(config_text, encoding="utf-8")

            def swallow_task(coro):
                coro.close()
                return None

            with (
                patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient),
                patch("teams_runtime.core.orchestration.asyncio.create_task", side_effect=swallow_task),
                patch("teams_runtime.core.orchestration.build_active_sprint_id", return_value="260404-Sprint-12:00"),
            ):
                service = TeamService(tmpdir, "orchestrator")
                staged_path = (
                    Path(tmpdir)
                    / "shared_workspace"
                    / "sprints"
                    / "2026-Sprint-01"
                    / "attachments"
                    / "att-1_brief.md"
                )
                staged_path.parent.mkdir(parents=True, exist_ok=True)
                staged_path.write_text("# kickoff brief\n", encoding="utf-8")
                message = DiscordMessage(
                    message_id="msg-start-1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="user",
                    content="start sprint\nmilestone: attachment relocation",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )
                envelope = MessageEnvelope(
                    request_id=None,
                    sender="user",
                    target="orchestrator",
                    intent="route",
                    urgency="normal",
                    scope="start sprint",
                    artifacts=["./shared_workspace/sprints/2026-Sprint-01/attachments/att-1_brief.md"],
                    body="milestone: attachment relocation",
                )

                asyncio.run(service._handle_manual_sprint_start_request(message, envelope, forwarded=False))

                sprint_state = service._load_active_sprint_state()
                relocated_path = (
                    Path(tmpdir)
                    / "shared_workspace"
                    / "sprints"
                    / "260404-Sprint-12-00"
                    / "attachments"
                    / "att-1_brief.md"
                )
                self.assertEqual(sprint_state["sprint_folder_name"], "260404-Sprint-12-00")
                self.assertFalse(staged_path.exists())
                self.assertTrue(relocated_path.exists())
                self.assertEqual(
                    sprint_state["reference_artifacts"],
                    ["./shared_workspace/sprints/260404-Sprint-12-00/attachments/att-1_brief.md"],
                )
                self.assertEqual(
                    sprint_state["kickoff_reference_artifacts"],
                    ["./shared_workspace/sprints/260404-Sprint-12-00/attachments/att-1_brief.md"],
                )
                current_sprint_text = service.paths.current_sprint_file.read_text(encoding="utf-8")
                self.assertIn("## Reference Artifacts", current_sprint_text)
                self.assertIn(
                    "./shared_workspace/sprints/260404-Sprint-12-00/attachments/att-1_brief.md",
                    current_sprint_text,
                )

    def test_start_sprint_lifecycle_rehomes_staged_kickoff_attachment_into_actual_sprint_folder(self):
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
                staged_path.write_text("# KIS API\n", encoding="utf-8")

                asyncio.run(
                    service.start_sprint_lifecycle(
                        "김단타 OBI 스캘핑 전략 전환 및 NXT 확장장 대응",
                        trigger="manual_start",
                        resume_mode="skip",
                        started_at=datetime.fromisoformat("2026-04-05T21:18:38+09:00"),
                        kickoff_source_request_id="request-origin-1",
                        kickoff_reference_artifacts=[
                            "./shared_workspace/sprints/260405-Sprint-21-17/attachments/1490324395179380827_KIS_API_.md"
                        ],
                    )
                )

                sprint_state = service._load_active_sprint_state()
                relocated_path = (
                    Path(tmpdir)
                    / "shared_workspace"
                    / "sprints"
                    / "260405-Sprint-21-18"
                    / "attachments"
                    / "1490324395179380827_KIS_API_.md"
                )
                expected_hint = "./shared_workspace/sprints/260405-Sprint-21-18/attachments/1490324395179380827_KIS_API_.md"

                self.assertEqual(sprint_state["sprint_folder_name"], "260405-Sprint-21-18")
                self.assertFalse(staged_path.exists())
                self.assertTrue(relocated_path.exists())
                self.assertEqual(sprint_state["kickoff_reference_artifacts"], [expected_hint])
                self.assertEqual(sprint_state["reference_artifacts"], [expected_hint])

    def test_resolve_message_attachment_root_uses_scheduler_active_sprint_id_when_sprint_state_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                service._save_scheduler_state(
                    {
                        "active_sprint_id": "260405-Sprint-16:34",
                        "last_started_at": "",
                        "last_completed_at": "",
                        "next_slot_at": "",
                        "deferred_slot_at": "",
                        "last_trigger": "manual_start",
                    }
                )
                message = DiscordMessage(
                    message_id="attachment-root-1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="user",
                    content="첨부 파일 확인해줘",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                attachment_root = service._resolve_message_attachment_root(message)

                self.assertEqual(
                    attachment_root,
                    service.paths.sprint_attachment_root("260405-Sprint-16-34"),
                )

    def test_handle_message_manual_sprint_start_in_auto_mode_requests_milestone(self):
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
                        "summary": "milestone이 비어 있는 sprint start 요청입니다.",
                        "insights": [],
                        "proposals": {
                            "request_handling": {"mode": "complete"},
                            "control_action": {
                                "kind": "sprint_lifecycle",
                                "command": "start",
                                "milestone_title": "",
                            },
                        },
                        "artifacts": [],
                        "error": "",
                    }

                service.intent_parser = _FailingIntentParser()
                message = DiscordMessage(
                    message_id="manual-start-auto-1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="user",
                    content="start sprint",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service.handle_message(message))

                scheduler_state = service._load_scheduler_state()
                self.assertEqual(str(scheduler_state.get("active_sprint_id") or ""), "")
                self.assertEqual(service.discord_client.sent_channels[0], ("channel-1", "<@user-1> 수신양호"))
                self.assertIn("milestone", service.discord_client.sent_channels[1][1].lower())

    def test_handle_message_manual_sprint_start_in_auto_mode_uses_source_message_timestamp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)

            def swallow_task(coro):
                coro.close()
                return None

            with (
                patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient),
                patch("teams_runtime.core.orchestration.asyncio.create_task", side_effect=swallow_task),
            ):
                service = TeamService(tmpdir, "orchestrator")
                observed: dict[str, str] = {}

                def fake_orchestrator_run_task(_envelope, request_record):
                    observed["source_message_created_at"] = str(request_record.get("source_message_created_at") or "")
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "manual sprint start 요청을 lifecycle backend로 전달했습니다.",
                        "insights": [],
                        "proposals": {
                            "request_handling": {"mode": "complete"},
                            "control_action": {
                                "kind": "sprint_lifecycle",
                                "command": "start",
                                "milestone_title": "attachment intake alignment",
                            },
                        },
                        "artifacts": [],
                        "error": "",
                    }

                message_created_at = datetime(2026, 4, 5, 7, 34, tzinfo=timezone.utc)
                message = DiscordMessage(
                    message_id="manual-start-auto-timestamp",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="user",
                    content="start sprint\nmilestone: attachment intake alignment",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=message_created_at,
                )

                call_state = {"without_now": 0}

                def build_active_sprint_id_side_effect(now=None):
                    if now is not None:
                        return "260405-Sprint-16:34"
                    call_state["without_now"] += 1
                    if call_state["without_now"] == 1:
                        return "260405-Sprint-16:33"
                    return "260405-Sprint-16:34"

                with (
                    patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task),
                    patch(
                        "teams_runtime.core.orchestration.build_active_sprint_id",
                        side_effect=build_active_sprint_id_side_effect,
                    ),
                ):
                    asyncio.run(service.handle_message(message))

                scheduler_state = service._load_scheduler_state()
                sprint_id = str(scheduler_state.get("active_sprint_id") or "")
                self.assertEqual(sprint_id, "260405-Sprint-16:34")
                self.assertEqual(observed["source_message_created_at"], "2026-04-05T16:34:00+09:00")

    def test_handle_message_manual_sprint_start_in_auto_mode_creates_manual_sprint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)

            def swallow_task(coro):
                coro.close()
                return None

            with (
                patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient),
                patch("teams_runtime.core.orchestration.asyncio.create_task", side_effect=swallow_task),
            ):
                service = TeamService(tmpdir, "orchestrator")

                def fake_orchestrator_run_task(_envelope, request_record):
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "manual sprint start 요청을 lifecycle backend로 전달했습니다.",
                        "insights": [],
                        "proposals": {
                            "request_handling": {"mode": "complete"},
                            "control_action": {
                                "kind": "sprint_lifecycle",
                                "command": "start",
                                "milestone_title": "sprint workflow initial phase 개선",
                            },
                        },
                        "artifacts": [],
                        "error": "",
                    }

                message = DiscordMessage(
                    message_id="manual-start-auto-2",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="user",
                    content="start sprint\nmilestone: sprint workflow initial phase 개선",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service.handle_message(message))

                scheduler_state = service._load_scheduler_state()
                sprint_id = str(scheduler_state.get("active_sprint_id") or "")
                sprint_state = service._load_sprint_state(sprint_id)
                self.assertTrue(sprint_id)
                self.assertFalse(bool(scheduler_state.get("milestone_request_pending")))
                self.assertEqual(sprint_state["milestone_title"], "sprint workflow initial phase 개선")
                self.assertEqual(sprint_state["execution_mode"], "manual")
                self.assertEqual(sprint_state["phase"], "initial")
                self.assertEqual(service.discord_client.sent_channels[0], ("channel-1", "<@user-1> 수신양호"))
                self.assertIn("완료", service.discord_client.sent_channels[1][1])
                self.assertIn("스프린트를 시작했습니다.", service.discord_client.sent_channels[1][1])

    def test_handle_message_manual_sprint_start_prepares_orchestrator_workspace_link_when_generated_workspace_is_fresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(workspace_root, "orchestrator")

                observed: dict[str, str] = {}

                def fake_orchestrator_run_task(_envelope, request_record):
                    state = service.role_runtime.session_manager.load()
                    self.assertIsNotNone(state)
                    session_workspace = Path(str(state.workspace_path))
                    workspace_link = session_workspace / "workspace"
                    runtime_link = session_workspace / ".teams_runtime"
                    observed["workspace_path"] = str(session_workspace)
                    observed["workspace_target"] = str(workspace_link.resolve())
                    self.assertTrue(workspace_link.exists())
                    self.assertTrue(workspace_link.is_symlink())
                    self.assertTrue(runtime_link.exists())
                    self.assertTrue(runtime_link.is_symlink())
                    self.assertTrue((session_workspace / "workspace_context.md").exists())
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "스프린트 시작 요청을 확인했습니다.",
                        "insights": [],
                        "proposals": {"request_handling": {"mode": "complete"}},
                        "artifacts": [],
                        "error": "",
                    }

                message = DiscordMessage(
                    message_id="manual-start-workspace-link-1",
                    channel_id="dm-start-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="user",
                    content='스프린트 시작해.\nmilestone은 "KIS 스캘핑 고도화"',
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service.handle_message(message))

                self.assertEqual(observed["workspace_target"], str(Path(tmpdir).resolve()))
                self.assertIn("/orchestrator/sessions/", observed["workspace_path"])

    def test_workspace_artifact_hint_prefers_session_local_shared_and_runtime_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(workspace_root, "orchestrator")

                shared_path = workspace_root / "shared_workspace" / "planning.md"
                runtime_path = workspace_root / ".teams_runtime" / "requests" / "sample.json"
                project_path = Path(tmpdir) / "src" / "sample.py"
                project_path.parent.mkdir(parents=True, exist_ok=True)
                project_path.write_text("print('ok')\n", encoding="utf-8")

                self.assertEqual(service._workspace_artifact_hint(shared_path), "./shared_workspace/planning.md")
                self.assertEqual(service._workspace_artifact_hint(runtime_path), "./.teams_runtime/requests/sample.json")
                self.assertEqual(service._workspace_artifact_hint(project_path), "./workspace/teams_generated/src/sample.py")

    def test_resolve_artifact_path_supports_workspace_teams_generated_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(workspace_root, "orchestrator")

                legacy_shared = workspace_root / "shared_workspace" / "planning.md"
                legacy_shared.parent.mkdir(parents=True, exist_ok=True)
                legacy_shared.write_text("hello\n", encoding="utf-8")

                workspace_sample = workspace_root / "src" / "sample.py"
                workspace_sample.parent.mkdir(parents=True, exist_ok=True)
                workspace_sample.write_text("print('ok')\n", encoding="utf-8")

                self.assertEqual(
                    service._resolve_artifact_path("./workspace/teams_generated/shared_workspace/planning.md"),
                    legacy_shared.resolve(),
                )
                self.assertEqual(
                    service._resolve_artifact_path("./workspace/teams_generated/src/sample.py"),
                    workspace_sample.resolve(),
                )
                self.assertEqual(
                    service._resolve_artifact_path("./workspace/src/sample.py"),
                    workspace_sample.resolve(),
                )

    def test_handle_message_creates_request_from_attachment_only_message_with_artifact_hint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(workspace_root, "orchestrator")
                saved_path = (
                    workspace_root
                    / "shared_workspace"
                    / "sprints"
                    / "2026-Sprint-01"
                    / "attachments"
                    / "att-1_note.txt"
                )
                saved_path.parent.mkdir(parents=True, exist_ok=True)
                saved_path.write_text("attachment payload\n", encoding="utf-8")

                def fake_orchestrator_run_task(_envelope, request_record):
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "첨부 파일 기반 요청을 접수했습니다.",
                        "insights": [],
                        "proposals": {"request_handling": {"mode": "complete"}},
                        "artifacts": list(request_record.get("artifacts") or []),
                        "error": "",
                    }

                message = DiscordMessage(
                    message_id="msg-attach-only",
                    channel_id="dm-attach-only",
                    guild_id=None,
                    author_id="user-1",
                    author_name="user",
                    content="",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                    attachments=(
                        DiscordAttachment(
                            attachment_id="att-1",
                            filename="note.txt",
                            saved_path=str(saved_path.resolve()),
                        ),
                    ),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service.handle_message(message))

                request_files = list(service.paths.requests_dir.glob("*.json"))
                self.assertEqual(len(request_files), 1)
                request_payload = json.loads(request_files[0].read_text(encoding="utf-8"))
                self.assertEqual(request_payload["scope"], "첨부 파일 1건이 포함된 사용자 요청")
                self.assertEqual(request_payload["body"], "첨부 파일 1건이 포함된 사용자 요청")
                self.assertEqual(
                    request_payload["artifacts"],
                    ["./shared_workspace/sprints/2026-Sprint-01/attachments/att-1_note.txt"],
                )
                self.assertEqual(service.discord_client.sent_dms[0], ("user-1", "수신양호"))
                self.assertIn("완료", service.discord_client.sent_dms[1][1])

    def test_handle_message_rejects_attachment_only_message_when_all_saves_fail(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(workspace_root, "orchestrator")
                message = DiscordMessage(
                    message_id="msg-attach-fail",
                    channel_id="dm-attach-fail",
                    guild_id=None,
                    author_id="user-1",
                    author_name="user",
                    content="",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                    attachments=(
                        DiscordAttachment(
                            attachment_id="att-1",
                            filename="note.txt",
                            save_error="download failed",
                        ),
                    ),
                )

                asyncio.run(service.handle_message(message))

                self.assertEqual(list(service.paths.requests_dir.glob("*.json")), [])
                self.assertEqual(service.discord_client.sent_dms[0], ("user-1", "수신양호"))
                self.assertIn("첨부 파일을 저장하지 못했습니다", service.discord_client.sent_dms[1][1])

    def test_null_discord_client_uses_utc_timestamp_without_timezone_symbol(self):
        client = orchestration_module._NullDiscordClient(client_name="test")

        sent_channel = asyncio.run(client.send_channel_message("channel-1", "hello"))
        sent_dm = asyncio.run(client.send_dm("user-1", "hi"))

        self.assertEqual(sent_channel.channel_id, "channel-1")
        self.assertEqual(sent_dm.channel_id, "dm")
        self.assertEqual(sent_channel.created_at.tzinfo, timezone.utc)
        self.assertEqual(sent_dm.created_at.tzinfo, timezone.utc)

    def test_continue_sprint_uses_manual_flow_for_manual_override_state_in_auto_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                )
                service._continue_manual_daily_sprint = AsyncMock(return_value=None)

                asyncio.run(service._continue_sprint(sprint_state, announce=False))

                service._continue_manual_daily_sprint.assert_awaited_once_with(sprint_state, announce=False)

    def test_handle_message_manual_sprint_finalize_in_auto_mode_marks_wrap_up(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)

            def swallow_task(coro):
                coro.close()
                return None

            with (
                patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient),
                patch("teams_runtime.core.orchestration.asyncio.create_task", side_effect=swallow_task),
            ):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="workflow initial",
                    trigger="manual_start",
                )
                sprint_state["phase"] = "ongoing"
                sprint_state["status"] = "running"
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

                def fake_orchestrator_run_task(_envelope, request_record):
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "manual sprint stop 요청을 lifecycle backend로 전달했습니다.",
                        "insights": [],
                        "proposals": {
                            "request_handling": {"mode": "complete"},
                            "control_action": {
                                "kind": "sprint_lifecycle",
                                "command": "stop",
                            },
                        },
                        "artifacts": [],
                        "error": "",
                    }

                message = DiscordMessage(
                    message_id="manual-finalize-auto-1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="user",
                    content="finalize sprint",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(service.role_runtime, "run_task", side_effect=fake_orchestrator_run_task):
                    asyncio.run(service.handle_message(message))

                updated = service._load_sprint_state(sprint_state["sprint_id"])
                self.assertTrue(str(updated.get("wrap_up_requested_at") or "").strip())
                self.assertEqual(service.discord_client.sent_channels[0], ("channel-1", "<@user-1> 수신양호"))
                self.assertIn("완료", service.discord_client.sent_channels[1][1])
                self.assertIn("스프린트 종료를 요청했습니다.", service.discord_client.sent_channels[1][1])
