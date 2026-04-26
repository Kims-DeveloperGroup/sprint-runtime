from orchestration_test_utils import *


class TeamsRuntimeOrchestrationBacklogSourcingTests(OrchestrationTestCase):
    def test_discover_backlog_candidates_reads_only_actionable_role_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                write_json(
                    service.paths.request_file("req-1"),
                    {
                        "request_id": "req-1",
                        "status": "failed",
                        "intent": "plan",
                        "scope": "intraday trading 개선안 적용",
                        "body": "intraday trading 개선안 적용",
                        "params": {},
                        "result": {
                            "request_id": "req-1",
                            "role": "planner",
                            "status": "failed",
                            "summary": "intraday trading 개선안 적용 중 오류가 발생했습니다.",
                            "insights": ["거래량 임계치 검증 TODO를 backlog로 올려야 합니다."],
                            "artifacts": [],
                            "proposals": {},
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        },
                    },
                )

                with patch.object(service.backlog_sourcer, "source", side_effect=RuntimeError("skip model")):
                    candidates = service._discover_backlog_candidates()

                summaries = {str(item.get("summary") or "") for item in candidates}
                self.assertIn("intraday trading 개선안 적용 중 오류가 발생했습니다.", summaries)
                self.assertNotIn("거래량 임계치 검증 TODO를 backlog로 올려야 합니다.", summaries)
                self.assertEqual(service._last_backlog_sourcing_activity["mode"], "fallback")
                self.assertIn("skip model", service._last_backlog_sourcing_activity["fallback_reason"])
                self.assertGreaterEqual(len(service._last_backlog_sourcing_activity["findings_sample"]), 1)

    def test_discover_backlog_candidates_uses_internal_sourcer_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                write_json(
                    service.paths.request_file("req-open"),
                    {
                        "request_id": "req-open",
                        "status": "running",
                        "intent": "route",
                        "scope": "알림 분기 기능 추가",
                        "body": "기존 알림 흐름을 채널별로 분기해야 합니다.",
                        "params": {},
                    },
                )

                with patch.object(
                    service.backlog_sourcer,
                    "source",
                    return_value={
                        "status": "completed",
                        "summary": "open request를 feature backlog로 정규화했습니다.",
                        "backlog_items": [
                            {
                                "title": "채널별 알림 분기 기능 추가",
                                "summary": "알림을 목적 채널 정책에 따라 분기하는 신규 capability가 필요합니다.",
                                "kind": "feature",
                                "scope": "채널별 알림 분기 기능 추가",
                                "acceptance_criteria": ["채널 정책에 따라 알림 분기가 동작한다."],
                                "origin": {"signal": "open_request"},
                            }
                        ],
                        "error": "",
                        "monitoring": {
                            "elapsed_ms": 187,
                            "reuse_session": True,
                            "prompt_chars": 1420,
                            "json_parse_status": "success",
                            "raw_backlog_items_count": 1,
                            "findings_sample": ["알림 분기 기능 추가"],
                            "existing_backlog_sample": ["기존 backlog 예시"],
                        },
                    },
                ):
                    candidates = service._discover_backlog_candidates()

                self.assertEqual(len(candidates), 1)
                self.assertEqual(candidates[0]["source"], "sourcer")
                self.assertEqual(candidates[0]["kind"], "feature")
                self.assertEqual(candidates[0]["title"], "채널별 알림 분기 기능 추가")
                self.assertEqual(
                    candidates[0]["acceptance_criteria"],
                    ["채널 정책에 따라 알림 분기가 동작한다."],
                )
                self.assertEqual(service._last_backlog_sourcing_activity["elapsed_ms"], 187)
                self.assertTrue(service._last_backlog_sourcing_activity["reuse_session"])
                self.assertEqual(service._last_backlog_sourcing_activity["raw_backlog_items_count"], 1)
                self.assertEqual(service._last_backlog_sourcing_activity["filtered_candidate_count"], 1)
                self.assertEqual(service._last_backlog_sourcing_activity["findings_sample"], ["알림 분기 기능 추가"])

    def test_discover_backlog_candidates_filters_sourcer_output_to_active_sprint_milestone(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                service._save_scheduler_state({"active_sprint_id": "260331-Sprint-14:00"})
                service._save_sprint_state(
                    {
                        "sprint_id": "260331-Sprint-14:00",
                        "milestone_title": "workflow initial",
                        "status": "running",
                        "trigger": "manual_start",
                        "started_at": "2026-03-31T14:00:00+09:00",
                        "selected_items": [],
                        "todos": [],
                    }
                )
                write_json(
                    service.paths.request_file("req-open"),
                    {
                        "request_id": "req-open",
                        "status": "running",
                        "intent": "route",
                        "scope": "알림 분기 기능 추가",
                        "body": "기존 알림 흐름을 채널별로 분기해야 합니다.",
                        "params": {},
                    },
                )

                with patch.object(
                    service.backlog_sourcer,
                    "source",
                    return_value={
                        "status": "completed",
                        "summary": "active sprint milestone 관련 후보만 남겨야 합니다.",
                        "backlog_items": [
                            {
                                "title": "workflow initial 가드 정리",
                                "summary": "active sprint milestone에 직접 연결된 backlog입니다.",
                                "kind": "enhancement",
                                "scope": "workflow initial 가드 정리",
                                "milestone_title": "workflow initial",
                            },
                            {
                                "title": "별도 운영 문서 정리",
                                "summary": "현재 sprint milestone과 직접 관련 없는 backlog입니다.",
                                "kind": "chore",
                                "scope": "별도 운영 문서 정리",
                                "milestone_title": "other milestone",
                            },
                            {
                                "title": "애매한 주변 개선",
                                "summary": "milestone 표기가 없는 항목입니다.",
                                "kind": "enhancement",
                                "scope": "애매한 주변 개선",
                                "milestone_title": "",
                            },
                        ],
                        "error": "",
                        "monitoring": {
                            "elapsed_ms": 120,
                            "reuse_session": True,
                            "prompt_chars": 1800,
                            "json_parse_status": "success",
                            "raw_backlog_items_count": 3,
                            "findings_sample": ["알림 분기 기능 추가"],
                            "existing_backlog_sample": [],
                        },
                    },
                ):
                    candidates = service._discover_backlog_candidates()

                self.assertEqual([item["title"] for item in candidates], ["workflow initial 가드 정리"])
                self.assertEqual(candidates[0]["milestone_title"], "workflow initial")
                self.assertEqual(service._last_backlog_sourcing_activity["active_sprint_milestone"], "workflow initial")
                self.assertEqual(service._last_backlog_sourcing_activity["raw_backlog_items_count"], 3)
                self.assertEqual(service._last_backlog_sourcing_activity["filtered_candidate_count"], 1)
                self.assertEqual(service._last_backlog_sourcing_activity["milestone_filtered_out_count"], 2)

    def test_discover_backlog_candidates_skips_failed_request_already_linked_from_backlog_origin(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                write_json(
                    service.paths.backlog_file("backlog-existing"),
                    {
                        "backlog_id": "backlog-existing",
                        "title": "orchestrator가 sprint closeout report 요청을 NameError 없이 생성한다",
                        "summary": "이미 처리된 closeout request-id 회귀입니다.",
                        "kind": "bug",
                        "source": "planner",
                        "scope": "closeout request-id 생성 경로 복구",
                        "acceptance_criteria": [],
                        "milestone_title": "sprint closeout report 생성 복구",
                        "priority_rank": 1,
                        "status": "done",
                        "origin": {
                            "latest_failed_request_id": "req-failed",
                            "request_id": "req-original",
                        },
                    },
                )
                write_json(
                    service.paths.request_file("req-failed"),
                    {
                        "request_id": "req-failed",
                        "status": "failed",
                        "intent": "route",
                        "scope": "스프린트 재시작을 시도했지만 closeout 경로에서 `slugify_sprint_value` 미정의 오류가 발생해 재시작에 실패했습니다.",
                        "body": "같은 실패 request가 다시 sourcing 되면 안 됩니다.",
                        "params": {},
                        "result": {
                            "request_id": "req-failed",
                            "role": "orchestrator",
                            "status": "failed",
                            "summary": "closeout 경로의 `slugify_sprint_value` NameError로 스프린트 재시작이 실패했습니다.",
                        },
                    },
                )

                with patch.object(service.backlog_sourcer, "source") as source_mock:
                    candidates = service._discover_backlog_candidates()

                self.assertEqual(candidates, [])
                source_mock.assert_not_called()
                self.assertEqual(service._last_backlog_sourcing_activity["findings_count"], 0)
                self.assertEqual(service._last_backlog_sourcing_activity["candidate_count"], 0)
                self.assertEqual(
                    service._last_backlog_sourcing_activity["summary"],
                    "수집할 backlog finding이 없어 sourcer 실행을 건너뛰었습니다.",
                )

    def test_poll_backlog_sourcing_once_suppresses_already_reported_sourcer_issue(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                write_json(
                    service.paths.request_file("req-open"),
                    {
                        "request_id": "req-open",
                        "status": "running",
                        "intent": "route",
                        "scope": "알림 분기 기능 추가",
                        "body": "기존 알림 흐름을 채널별로 분기해야 합니다.",
                        "params": {},
                    },
                )
                candidate = {
                    "title": "채널별 알림 분기 기능 추가",
                    "summary": "알림을 목적 채널 정책에 따라 분기하는 신규 capability가 필요합니다.",
                    "kind": "feature",
                    "scope": "채널별 알림 분기 기능 추가",
                    "origin": {"request_id": "req-open", "signal": "open_request"},
                }
                fingerprint = service._build_sourcer_review_fingerprint(
                    service._normalize_sourcer_review_candidates([candidate])
                )
                service._save_scheduler_state(
                    {
                        "last_sourcing_fingerprint": fingerprint,
                        "last_sourcing_review_status": "completed",
                        "last_sourcing_review_request_id": "review-prev",
                    }
                )

                with (
                    patch.object(
                        service.backlog_sourcer,
                        "source",
                        return_value={
                            "status": "completed",
                            "summary": "이미 보고했던 후보입니다.",
                            "backlog_items": [candidate],
                            "error": "",
                            "monitoring": {
                                "elapsed_ms": 88,
                                "raw_backlog_items_count": 1,
                                "json_parse_status": "success",
                            },
                        },
                    ),
                    patch.object(service, "_report_sourcer_activity_sync") as report_mock,
                ):
                    asyncio.run(service._poll_backlog_sourcing_once())

                report_mock.assert_not_called()
                scheduler_state = service._load_scheduler_state()
                self.assertEqual(scheduler_state["last_sourcing_status"], "duplicate_suppressed")
                self.assertEqual(scheduler_state["last_sourcing_request_id"], "review-prev")
                self.assertEqual(scheduler_state["last_sourcing_fingerprint"], fingerprint)
                self.assertEqual(len(list(service.paths.requests_dir.glob("*.json"))), 1)

    def test_poll_backlog_sourcing_once_allows_recurrence_with_new_request_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                write_json(
                    service.paths.request_file("req-open-2"),
                    {
                        "request_id": "req-open-2",
                        "status": "running",
                        "intent": "route",
                        "scope": "알림 분기 기능 추가",
                        "body": "기존 알림 흐름을 채널별로 분기해야 합니다.",
                        "params": {},
                    },
                )
                previous_candidate = {
                    "title": "채널별 알림 분기 기능 추가",
                    "summary": "이전 보고 후보입니다.",
                    "kind": "feature",
                    "scope": "채널별 알림 분기 기능 추가",
                    "origin": {"request_id": "req-open-1", "signal": "open_request"},
                }
                service._save_scheduler_state(
                    {
                        "last_sourcing_fingerprint": service._build_sourcer_review_fingerprint(
                            service._normalize_sourcer_review_candidates([previous_candidate])
                        ),
                        "last_sourcing_review_status": "completed",
                        "last_sourcing_review_request_id": "review-prev",
                    }
                )
                recurring_candidate = {
                    "title": "채널별 알림 분기 기능 추가",
                    "summary": "같은 이슈가 새 request에서 다시 관찰됐습니다.",
                    "kind": "feature",
                    "scope": "채널별 알림 분기 기능 추가",
                    "origin": {"request_id": "req-open-2", "signal": "open_request"},
                }

                with (
                    patch.object(
                        service.backlog_sourcer,
                        "source",
                        return_value={
                            "status": "completed",
                            "summary": "새 request_id라 recurrence로 판단했습니다.",
                            "backlog_items": [recurring_candidate],
                            "error": "",
                            "monitoring": {
                                "elapsed_ms": 91,
                                "raw_backlog_items_count": 1,
                                "json_parse_status": "success",
                            },
                        },
                    ),
                    patch.object(service, "_report_sourcer_activity_sync") as report_mock,
                ):
                    asyncio.run(service._poll_backlog_sourcing_once())

                report_mock.assert_called_once()
                scheduler_state = service._load_scheduler_state()
                self.assertEqual(scheduler_state["last_sourcing_status"], "queued_for_planner_review")
                self.assertEqual(scheduler_state["last_sourcing_review_status"], "queued_for_planner_review")
                self.assertNotEqual(
                    scheduler_state["last_sourcing_fingerprint"],
                    service._build_sourcer_review_fingerprint(
                        service._normalize_sourcer_review_candidates([previous_candidate])
                    ),
                )
                request_payloads = [
                    json.loads(path.read_text(encoding="utf-8"))
                    for path in service.paths.requests_dir.glob("*.json")
                ]
                self.assertEqual(
                    len([item for item in request_payloads if dict(item.get("params") or {}).get("_teams_kind") == "sourcer_review"]),
                    1,
                )

    def test_perform_backlog_sourcing_reports_activity_via_sourcer_bot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "discord_agents_config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace(
                'report_channel_id: "111111111111111111"',
                'report_channel_id: "1486503058765779066"',
                1,
            )
            config_text += """
internal_agents:
  sourcer:
    name: CS_ADMIN
    role: sourcer
    description: Internal backlog sourcing activity reporter
    token_env: AGENT_DISCORD_TOKEN_CS_ADMIN
    bot_id: "1486504738613886987"
"""
            config_path.write_text(config_text, encoding="utf-8")
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                with (
                    patch.object(
                        service,
                        "_build_backlog_sourcing_findings",
                        return_value=[
                            {
                                "signal": "runtime_log_error",
                                "title": "developer 로그 오류 점검",
                                "summary": "developer runtime log에 traceback이 남았습니다.",
                                "kind_hint": "bug",
                                "scope": "developer runtime log",
                                "acceptance_criteria": [],
                                "origin": {"role": "developer"},
                            }
                        ],
                    ),
                    patch.object(
                        service.backlog_sourcer,
                        "source",
                        return_value={
                            "status": "completed",
                            "summary": "runtime log finding을 bug backlog로 등록했습니다.",
                            "backlog_items": [
                                {
                                    "title": "developer 로그 오류 점검",
                                    "summary": "developer runtime log에 traceback이 남았습니다.",
                                    "kind": "bug",
                                    "scope": "developer runtime log",
                                    "acceptance_criteria": ["로그 원인을 재현하고 수정 방향을 정리한다."],
                                    "origin": {"role": "developer"},
                                }
                            ],
                            "error": "",
                            "session_id": "session-sourcer-1",
                            "session_workspace": "/tmp/sourcer",
                            "monitoring": {
                                "elapsed_ms": 245,
                                "reuse_session": False,
                                "prompt_chars": 1800,
                                "json_parse_status": "success",
                                "raw_backlog_items_count": 1,
                                "findings_sample": ["developer 로그 오류 점검"],
                            },
                        },
                    ),
                ):
                    added, updated, candidates = service._perform_backlog_sourcing()

                self.assertEqual((added, updated), (0, 0))
                self.assertEqual(len(candidates), 1)
                self.assertIsNotNone(service._sourcer_report_client)
                self.assertEqual(len(service._sourcer_report_client.sent_channels), 1)
                report_channel, report_content = service._sourcer_report_client.sent_channels[0]
                self.assertEqual(report_channel, "1486503058765779066")
                self.assertNotIn("```", report_content)
                self.assertNotIn("┌", report_content)
                self.assertNotIn("└", report_content)
                self.assertIn("[작업 보고]", report_content)
                self.assertIn("🧩 요청: Backlog Sourcing", report_content)
                self.assertIn("🧠 판단: runtime log finding을 bug backlog로 등록했습니다.", report_content)
                self.assertIn("📊 지표: finding 1건, raw 1건, 후보 1건, 신규 0건, 갱신 0건, 245ms", report_content)
                self.assertIn("🗂️ 후보: developer 로그 오류 점검", report_content)
                self.assertIn("➡️ 다음: planner backlog review", report_content)
                self.assertNotIn("🔎 근거:", report_content)
                self.assertNotIn("candidate_titles=", report_content)
                self.assertEqual(service._last_backlog_sourcing_activity["added_count"], 0)
                self.assertEqual(service._last_backlog_sourcing_activity["updated_count"], 0)
                self.assertEqual(service._last_backlog_sourcing_activity["report_status"], "sent")
                self.assertEqual(service._last_backlog_sourcing_activity["report_client"], "internal_sourcer")
                self.assertEqual(service._last_backlog_sourcing_activity["report_category"], "")

    def test_perform_backlog_sourcing_falls_back_to_orchestrator_client_when_internal_reporter_init_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "discord_agents_config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace(
                'report_channel_id: "111111111111111111"',
                'report_channel_id: "1486503058765779066"',
                1,
            )
            config_text += """
internal_agents:
  sourcer:
    name: CS_ADMIN
    role: sourcer
    description: Internal backlog sourcing activity reporter
    token_env: AGENT_DISCORD_TOKEN_CS_ADMIN
    bot_id: "1486504738613886987"
"""
            config_path.write_text(config_text, encoding="utf-8")

            def build_client(*args, **kwargs):
                if str(kwargs.get("client_name") or "") == "sourcer":
                    raise RuntimeError("missing sourcer token")
                return FakeDiscordClient(*args, **kwargs)

            with patch("teams_runtime.core.orchestration.DiscordClient", side_effect=build_client):
                service = TeamService(tmpdir, "orchestrator")
                with (
                    patch.object(
                        service,
                        "_build_backlog_sourcing_findings",
                        return_value=[
                            {
                                "signal": "runtime_log_error",
                                "title": "developer 로그 오류 점검",
                                "summary": "developer runtime log에 traceback이 남았습니다.",
                                "kind_hint": "bug",
                                "scope": "developer runtime log",
                                "acceptance_criteria": [],
                                "origin": {"role": "developer"},
                            }
                        ],
                    ),
                    patch.object(
                        service.backlog_sourcer,
                        "source",
                        return_value={
                            "status": "completed",
                            "summary": "runtime log finding을 bug backlog로 등록했습니다.",
                            "backlog_items": [
                                {
                                    "title": "developer 로그 오류 점검",
                                    "summary": "developer runtime log에 traceback이 남았습니다.",
                                    "kind": "bug",
                                    "scope": "developer runtime log",
                                    "acceptance_criteria": ["로그 원인을 재현하고 수정 방향을 정리한다."],
                                    "origin": {"role": "developer"},
                                }
                            ],
                            "error": "",
                            "session_id": "session-sourcer-1",
                            "session_workspace": "/tmp/sourcer",
                            "monitoring": {"elapsed_ms": 245, "raw_backlog_items_count": 1},
                        },
                    ),
                ):
                    added, updated, _ = service._perform_backlog_sourcing()

                self.assertEqual((added, updated), (0, 0))
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                self.assertEqual(service.discord_client.sent_channels[0][0], "1486503058765779066")
                self.assertEqual(service._last_backlog_sourcing_activity["report_status"], "sent")
                self.assertEqual(service._last_backlog_sourcing_activity["report_client"], "orchestrator_fallback")
                self.assertIn("internal reporter init failed", service._last_backlog_sourcing_activity["report_reason"])
                self.assertEqual(service._last_backlog_sourcing_activity["report_category"], "discord_connection_failed")
                self.assertIn("Discord API 상태", service._last_backlog_sourcing_activity["report_recovery_action"])
                state = read_json(service.paths.agent_state_file("orchestrator"))
                self.assertEqual(state["sourcer_report_status"], "sent")
                self.assertEqual(state["sourcer_report_client"], "orchestrator_fallback")
                self.assertEqual(state["sourcer_report_category"], "discord_connection_failed")

    def test_perform_backlog_sourcing_records_report_send_failure_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "discord_agents_config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace(
                'report_channel_id: "111111111111111111"',
                'report_channel_id: "1486503058765779066"',
                1,
            )
            config_text += """
internal_agents:
  sourcer:
    name: CS_ADMIN
    role: sourcer
    description: Internal backlog sourcing activity reporter
    token_env: AGENT_DISCORD_TOKEN_CS_ADMIN
    bot_id: "1486504738613886987"
"""
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FailingSourcerSendDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                with (
                    patch.object(
                        service,
                        "_build_backlog_sourcing_findings",
                        return_value=[
                            {
                                "signal": "runtime_log_error",
                                "title": "developer 로그 오류 점검",
                                "summary": "developer runtime log에 traceback이 남았습니다.",
                                "kind_hint": "bug",
                                "scope": "developer runtime log",
                                "acceptance_criteria": [],
                                "origin": {"role": "developer"},
                            }
                        ],
                    ),
                    patch.object(
                        service.backlog_sourcer,
                        "source",
                        return_value={
                            "status": "completed",
                            "summary": "runtime log finding을 bug backlog로 등록했습니다.",
                            "backlog_items": [
                                {
                                    "title": "developer 로그 오류 점검",
                                    "summary": "developer runtime log에 traceback이 남았습니다.",
                                    "kind": "bug",
                                    "scope": "developer runtime log",
                                    "acceptance_criteria": ["로그 원인을 재현하고 수정 방향을 정리한다."],
                                    "origin": {"role": "developer"},
                                }
                            ],
                            "error": "",
                            "session_id": "session-sourcer-1",
                            "session_workspace": "/tmp/sourcer",
                            "monitoring": {"elapsed_ms": 245, "raw_backlog_items_count": 1},
                        },
                    ),
                ):
                    added, updated, _ = service._perform_backlog_sourcing()

                self.assertEqual((added, updated), (0, 0))
                self.assertEqual(service._last_backlog_sourcing_activity["report_status"], "failed")
                self.assertEqual(service._last_backlog_sourcing_activity["report_client"], "internal_sourcer")
                self.assertIn("TimeoutError", service._last_backlog_sourcing_activity["report_error"])
                self.assertEqual(service._last_backlog_sourcing_activity["report_attempts"], 3)
                self.assertEqual(service._last_backlog_sourcing_activity["report_category"], "discord_timeout")
                state = read_json(service.paths.agent_state_file("orchestrator"))
                self.assertEqual(state["sourcer_report_status"], "failed")
                self.assertEqual(state["sourcer_report_attempts"], 3)
                self.assertEqual(state["sourcer_report_category"], "discord_timeout")

    def test_sourcer_report_dns_failure_records_state_without_traceback_noise(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "discord_agents_config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace(
                'report_channel_id: "111111111111111111"',
                'report_channel_id: "1486503058765779066"',
                1,
            )
            config_text += """
internal_agents:
  sourcer:
    name: CS_ADMIN
    role: sourcer
    description: Internal backlog sourcing activity reporter
    token_env: AGENT_DISCORD_TOKEN_CS_ADMIN
    bot_id: "1486504738613886987"
"""
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FailingSourcerDnsDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sourcing_activity = {
                    "status": "completed",
                    "summary": "runtime log finding을 backlog 후보로 정리했습니다.",
                    "findings_count": 1,
                    "candidate_count": 1,
                    "mode": "internal_sourcer",
                    "session_id": "session-sourcer-1",
                    "session_workspace": "/tmp/sourcer",
                    "monitoring": {"elapsed_ms": 245, "raw_backlog_items_count": 1},
                }
                candidates = [{"title": "developer 로그 오류 점검"}]

                with (
                    patch.object(orchestration_module.LOGGER, "warning") as warning_mock,
                    patch.object(orchestration_module.LOGGER, "exception") as exception_mock,
                ):
                    service._report_sourcer_activity_sync(
                        sourcing_activity=sourcing_activity,
                        added=1,
                        updated=0,
                        candidates=candidates,
                    )
                    service._report_sourcer_activity_sync(
                        sourcing_activity=sourcing_activity,
                        added=1,
                        updated=0,
                        candidates=candidates,
                    )

                state = read_json(service.paths.agent_state_file("orchestrator"))
                self.assertEqual(state["sourcer_report_status"], "failed")
                self.assertEqual(state["sourcer_report_category"], "discord_dns_failed")
                self.assertEqual(state["sourcer_report_attempts"], 3)
                self.assertEqual(state["sourcer_report_channel"], "1486503058765779066")
                self.assertTrue(state["sourcer_report_last_failure_at"])
                self.assertEqual(service._last_backlog_sourcing_activity["report_status"], "failed")
                self.assertEqual(service._last_backlog_sourcing_activity["report_category"], "discord_dns_failed")
                exception_mock.assert_not_called()
                warning_messages = [" ".join(str(arg) for arg in call.args) for call in warning_mock.call_args_list]
                self.assertTrue(any("discord_dns_failed" in message for message in warning_messages))
                self.assertTrue(any("Repeated sourcer activity Discord report failure" in message for message in warning_messages))

    def test_discover_backlog_candidates_skips_internal_sprint_requests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                write_json(
                    service.paths.request_file("req-internal"),
                    {
                        "request_id": "req-internal",
                        "status": "delegated",
                        "intent": "route",
                        "scope": "내부 스프린트 관찰",
                        "body": "request 상태=delegated",
                        "params": {"_teams_kind": "sprint_internal"},
                    },
                )

                with patch.object(service.backlog_sourcer, "source", side_effect=RuntimeError("skip model")):
                    candidates = service._discover_backlog_candidates()

                scopes = {str(item.get("scope") or "") for item in candidates}
                self.assertNotIn("내부 스프린트 관찰", scopes)

    def test_discover_backlog_candidates_skips_blocked_requests_and_role_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                write_json(
                    service.paths.request_file("req-blocked"),
                    {
                        "request_id": "req-blocked",
                        "status": "blocked",
                        "intent": "route",
                        "scope": "외부 정보 보강 필요",
                        "body": "도메인 정보 부족",
                        "params": {},
                    },
                )
                write_json(
                    service.paths.request_file("req-blocked-role"),
                    {
                        "request_id": "req-blocked-role",
                        "status": "blocked",
                        "intent": "plan",
                        "scope": "입력 정보 부족으로 planner가 보류했습니다.",
                        "body": "입력 정보 부족으로 planner가 보류했습니다.",
                        "params": {},
                        "result": {
                            "request_id": "req-blocked-role",
                            "role": "planner",
                            "status": "blocked",
                            "summary": "입력 정보 부족으로 planner가 보류했습니다.",
                            "insights": [],
                            "artifacts": [],
                            "proposals": {"required_inputs": ["도메인"]},
                            "next_role": "",
                            "approval_needed": False,
                            "error": "",
                        },
                    },
                )

                with patch.object(service.backlog_sourcer, "source", side_effect=RuntimeError("skip model")):
                    candidates = service._discover_backlog_candidates()

                scopes = {str(item.get("scope") or "") for item in candidates}
                self.assertNotIn("외부 정보 보강 필요", scopes)
                self.assertNotIn("입력 정보 부족으로 planner가 보류했습니다.", scopes)

    def test_service_purges_request_scoped_duplicate_role_output_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            write_json(
                paths.request_file("20260325-migrate1"),
                {
                    "request_id": "20260325-migrate1",
                    "status": "completed",
                    "intent": "plan",
                    "scope": "legacy migration",
                    "body": "legacy migration",
                    "params": {},
                },
            )
            source_output = paths.role_sources_dir("planner") / "20260325-migrate1.md"
            source_payload = paths.role_sources_dir("planner") / "20260325-migrate1.json"
            runtime_output = paths.runtime_root / "role_reports" / "planner" / "20260325-migrate1.md"
            runtime_payload = paths.runtime_root / "role_reports" / "planner" / "20260325-migrate1.json"
            source_output.write_text("# Legacy Output\n", encoding="utf-8")
            source_payload.write_text('{"request_id":"20260325-migrate1","role":"planner"}', encoding="utf-8")
            runtime_output.parent.mkdir(parents=True, exist_ok=True)
            runtime_output.write_text("# Runtime Output\n", encoding="utf-8")
            runtime_payload.write_text('{"request_id":"20260325-migrate1","role":"planner"}', encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                TeamService(tmpdir, "planner")

            self.assertFalse(source_output.exists())
            self.assertFalse(source_payload.exists())
            self.assertFalse(runtime_output.exists())
            self.assertFalse(runtime_payload.exists())

    def test_independent_backlog_sourcing_loop_queues_planner_review_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                with (
                    patch.object(
                        service,
                        "_build_backlog_sourcing_findings",
                        return_value=[
                            {
                                "signal": "runtime_log_error",
                                "title": "developer 로그 오류 점검",
                                "summary": "developer runtime log에 traceback이 남았습니다.",
                                "kind_hint": "bug",
                                "scope": "developer runtime log",
                                "acceptance_criteria": [],
                                "origin": {"role": "developer"},
                            }
                        ],
                    ),
                    patch.object(
                        service.backlog_sourcer,
                        "source",
                        return_value={
                            "status": "completed",
                            "summary": "runtime log finding을 bug backlog로 등록했습니다.",
                            "backlog_items": [
                                {
                                    "title": "developer 로그 오류 점검",
                                    "summary": "developer runtime log에 traceback이 남았습니다.",
                                    "kind": "bug",
                                    "scope": "developer runtime log",
                                    "acceptance_criteria": ["로그 원인을 재현하고 수정 방향을 정리한다."],
                                    "origin": {"role": "developer"},
                                }
                            ],
                            "error": "",
                        },
                    ),
                ):
                    asyncio.run(service._poll_backlog_sourcing_once())

                backlog_items = list(service.paths.backlog_dir.glob("*.json"))
                self.assertEqual(len(backlog_items), 0)
                request_files = list(service.paths.requests_dir.glob("*.json"))
                self.assertEqual(len(request_files), 1)
                request_payload = json.loads(request_files[0].read_text(encoding="utf-8"))
                self.assertEqual(request_payload["status"], "delegated")
                self.assertEqual(request_payload["current_role"], "planner")
                self.assertEqual(request_payload["next_role"], "planner")
                self.assertEqual(request_payload["params"]["_teams_kind"], "sourcer_review")
                self.assertEqual(request_payload["params"]["candidate_count"], 1)
                self.assertEqual(
                    request_payload["params"]["sourced_backlog_candidates"][0]["title"],
                    "developer 로그 오류 점검",
                )
                scheduler_state = service._load_scheduler_state()
                self.assertEqual(scheduler_state["last_sourcing_status"], "queued_for_planner_review")
                self.assertEqual(
                    scheduler_state.get("last_sourcing_request_id"),
                    request_payload["request_id"],
                )
                self.assertEqual(len(service.discord_client.sent_channels), 2)
                self.assertIn("intent: plan", service.discord_client.sent_channels[0][1])
                self.assertIn("Backlog Sourcing", service.discord_client.sent_channels[1][1])
                self.assertIn("planner_review_request_id=", service.discord_client.sent_channels[1][1])
                self.assertIsNotNone(service._sourcer_report_client)
                self.assertEqual(len(service._sourcer_report_client.sent_channels), 1)
                self.assertIn("Backlog Sourcing", service._sourcer_report_client.sent_channels[0][1])

    def test_select_backlog_items_for_sprint_ignores_blocked_items(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                blocked_item = build_backlog_item(
                    title="신규 기획",
                    summary="입력 부족으로 보류합니다.",
                    kind="enhancement",
                    source="carry_over",
                    scope="신규 기획",
                )
                blocked_item["status"] = "blocked"
                blocked_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(blocked_item.get("title") or ""),
                    scope=str(blocked_item.get("scope") or ""),
                    kind=str(blocked_item.get("kind") or ""),
                )
                service._save_backlog_item(blocked_item)

                self.assertEqual(service._select_backlog_items_for_sprint(), [])

    def test_scheduler_skips_sprint_start_when_no_actionable_backlog_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                blocked_item = build_backlog_item(
                    title="신규 기획",
                    summary="도메인과 목표 정보가 없어 보류합니다.",
                    kind="enhancement",
                    source="carry_over",
                    scope="신규 기획",
                )
                blocked_item["status"] = "blocked"
                blocked_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(blocked_item.get("title") or ""),
                    scope=str(blocked_item.get("scope") or ""),
                    kind=str(blocked_item.get("kind") or ""),
                )
                service._save_backlog_item(blocked_item)
                blocked_fingerprint = service._build_blocked_backlog_review_fingerprint(
                    service._collect_blocked_backlog_review_candidates()
                )
                service._save_scheduler_state(
                    {
                        "active_sprint_id": "",
                        "next_slot_at": "2000-01-01T00:00:00+00:00",
                        "last_blocked_review_status": "completed",
                        "last_blocked_review_fingerprint": blocked_fingerprint,
                    }
                )

                with patch.object(service, "_discover_backlog_candidates", return_value=[]):
                    asyncio.run(service._poll_scheduler_once())

                scheduler_state = service._load_scheduler_state()
                self.assertEqual(str(scheduler_state.get("active_sprint_id") or ""), "")
                self.assertEqual(str(scheduler_state.get("last_skip_reason") or ""), "no_actionable_backlog")
                self.assertEqual(list(service.paths.sprints_dir.glob("*.json")), [])
                self.assertEqual(list(service.paths.requests_dir.glob("*.json")), [])

    def test_scheduler_queues_blocked_backlog_review_before_autonomous_sprint_start(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                blocked_item = build_backlog_item(
                    title="막힌 작업",
                    summary="선행 입력이 필요합니다.",
                    kind="enhancement",
                    source="carry_over",
                    scope="막힌 작업",
                )
                blocked_item["status"] = "blocked"
                blocked_item["blocked_reason"] = "기준 문서가 없습니다."
                blocked_item["required_inputs"] = ["기준 문서"]
                blocked_item["recommended_next_step"] = "planner가 재검토합니다."
                blocked_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(blocked_item.get("title") or ""),
                    scope=str(blocked_item.get("scope") or ""),
                    kind=str(blocked_item.get("kind") or ""),
                )
                pending_item = build_backlog_item(
                    title="바로 가능한 작업",
                    summary="이미 실행 가능한 pending backlog입니다.",
                    kind="bug",
                    source="user",
                    scope="바로 가능한 작업",
                )
                service._save_backlog_item(blocked_item)
                service._save_backlog_item(pending_item)
                service._save_scheduler_state(
                    {
                        "active_sprint_id": "",
                        "next_slot_at": "2000-01-01T00:00:00+00:00",
                    }
                )

                with patch.object(service, "_run_autonomous_sprint", AsyncMock(return_value=None)) as run_sprint_mock:
                    asyncio.run(service._poll_scheduler_once())
                    asyncio.run(service._poll_scheduler_once())

                request_files = list(service.paths.requests_dir.glob("*.json"))
                self.assertEqual(len(request_files), 1)
                request_payload = json.loads(request_files[0].read_text(encoding="utf-8"))
                self.assertEqual(request_payload["status"], "delegated")
                self.assertEqual(request_payload["params"]["_teams_kind"], "blocked_backlog_review")
                self.assertEqual(request_payload["params"]["candidate_count"], 1)
                self.assertEqual(
                    request_payload["params"]["blocked_backlog_candidates"][0]["backlog_id"],
                    blocked_item["backlog_id"],
                )
                scheduler_state = service._load_scheduler_state()
                self.assertEqual(scheduler_state["last_blocked_review_status"], "queued_for_planner_review")
                self.assertEqual(
                    scheduler_state["last_blocked_review_request_id"],
                    request_payload["request_id"],
                )
                run_sprint_mock.assert_not_awaited()

    def test_blocked_backlog_review_sync_reopens_item_for_future_sprint_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                blocked_item = build_backlog_item(
                    title="재개 대상",
                    summary="선행 입력이 없어서 막혔습니다.",
                    kind="enhancement",
                    source="planner",
                    scope="재개 대상",
                )
                blocked_item["status"] = "blocked"
                blocked_item["blocked_reason"] = "의사결정 필요"
                blocked_item["blocked_by_role"] = "planner"
                blocked_item["required_inputs"] = ["의사결정"]
                blocked_item["recommended_next_step"] = "재검토"
                blocked_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(blocked_item.get("title") or ""),
                    scope=str(blocked_item.get("scope") or ""),
                    kind=str(blocked_item.get("kind") or ""),
                )
                service._save_backlog_item(blocked_item)
                request_record = service._build_blocked_backlog_review_request_record(
                    service._collect_blocked_backlog_review_candidates()
                )
                reopened_backlog = dict(blocked_item)
                reopened_backlog["status"] = "pending"
                reopened_backlog["summary"] = "이제 future sprint에서 다시 선택 가능합니다."
                reopened_backlog["blocked_reason"] = ""
                reopened_backlog["blocked_by_role"] = ""
                reopened_backlog["required_inputs"] = []
                reopened_backlog["recommended_next_step"] = ""
                service._save_backlog_item(reopened_backlog)
                backlog_artifact = service.paths.backlog_file(blocked_item["backlog_id"])

                service._sync_planner_backlog_review_from_role_report(
                    request_record,
                    {
                        "role": "planner",
                        "status": "completed",
                        "summary": "막힌 backlog를 재개했습니다.",
                        "proposals": {
                            "backlog_item": {
                                "title": "재개 대상",
                                "scope": "재개 대상",
                                "summary": "이제 future sprint에서 다시 선택 가능합니다.",
                                "kind": "enhancement",
                                "status": "pending",
                            },
                            "backlog_writes": [
                                {
                                    "status": "updated",
                                    "backlog_id": blocked_item["backlog_id"],
                                    "artifact_path": str(backlog_artifact),
                                    "changed_fields": [
                                        "status",
                                        "summary",
                                        "blocked_reason",
                                        "blocked_by_role",
                                        "required_inputs",
                                        "recommended_next_step",
                                    ],
                                }
                            ],
                        },
                    },
                )

                updated_backlog = service._load_backlog_item(blocked_item["backlog_id"])
                self.assertEqual(updated_backlog["status"], "pending")
                self.assertEqual(updated_backlog["blocked_reason"], "")
                self.assertEqual(updated_backlog["blocked_by_role"], "")
                self.assertEqual(updated_backlog["required_inputs"], [])
                self.assertEqual(updated_backlog["recommended_next_step"], "")
                selected_items = service._select_backlog_items_for_sprint()
                self.assertEqual(
                    [str(item.get("backlog_id") or "") for item in selected_items],
                    [blocked_item["backlog_id"]],
                )
                scheduler_state = service._load_scheduler_state()
                self.assertEqual(scheduler_state["last_blocked_review_status"], "completed")
                self.assertEqual(
                    scheduler_state["last_blocked_review_fingerprint"],
                    request_record["fingerprint"],
                )

    def test_select_backlog_items_for_sprint_does_not_cap_actionable_items_at_three(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                backlog_ids: list[str] = []
                for index in range(4):
                    backlog_item = build_backlog_item(
                        title=f"selected scope {index + 1}",
                        summary=f"selected scope {index + 1}",
                        kind="feature",
                        source="planner" if index % 2 else "user",
                        scope=f"selected scope {index + 1}",
                    )
                    backlog_item["priority_rank"] = 4 - index
                    service._save_backlog_item(backlog_item)
                    backlog_ids.append(backlog_item["backlog_id"])

                selected_items = service._select_backlog_items_for_sprint()

                self.assertEqual(len(selected_items), 4)
                self.assertEqual(
                    [str(item.get("backlog_id") or "") for item in selected_items],
                    backlog_ids,
                )

    def test_scheduler_starts_sprint_even_when_runtime_files_changed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            runtime_file = Path(tmpdir) / "teams_runtime" / "core" / "orchestration.py"
            runtime_file.parent.mkdir(parents=True, exist_ok=True)
            runtime_file.write_text("value = 1\n", encoding="utf-8")
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                runtime_file.write_text("value = 2\n", encoding="utf-8")
                backlog_item = build_backlog_item(
                    title="reload sensitive work",
                    summary="runtime 파일이 바뀌어도 sprint start는 계속됩니다.",
                    kind="bug",
                    source="user",
                    scope="reload sensitive work",
                )
                service._save_backlog_item(backlog_item)
                service._save_scheduler_state(
                    {
                        "active_sprint_id": "",
                        "next_slot_at": "2000-01-01T00:00:00+00:00",
                    }
                )

                with (
                    patch.object(service, "_prepare_actionable_backlog_for_sprint", return_value=[backlog_item]),
                    patch.object(service, "_run_autonomous_sprint", AsyncMock(return_value=None)) as run_sprint_mock,
                ):
                    asyncio.run(service._poll_scheduler_once())

                scheduler_state = service._load_scheduler_state()
                self.assertEqual(str(scheduler_state.get("active_sprint_id") or ""), "")
                self.assertNotEqual(str(scheduler_state.get("last_skip_reason") or ""), "restart_required")
                run_sprint_mock.assert_awaited_once_with("backlog_ready", selected_items=[backlog_item])

    def test_scheduler_does_not_create_new_sprint_while_failed_sprint_remains_active(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                failed_sprint = {
                    "sprint_id": "2026-Sprint-01-20260324T000900Z",
                    "status": "failed",
                    "trigger": "backlog_ready",
                    "started_at": "2026-03-24T00:09:00+09:00",
                    "ended_at": "2026-03-24T00:19:00+09:00",
                    "selected_backlog_ids": [],
                    "selected_items": [],
                    "todos": [],
                    "commit_sha": "",
                    "commit_shas": [],
                    "commit_count": 0,
                    "closeout_status": "failed",
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
                    "reload_required": False,
                    "reload_paths": [],
                    "reload_message": "",
                    "reload_restart_command": "",
                    "report_path": "",
                    "git_baseline": {"repo_root": "", "head_sha": "", "dirty_paths": []},
                }
                backlog_item = build_backlog_item(
                    title="still pending",
                    summary="should not spawn a redundant sprint",
                    kind="bug",
                    source="user",
                    scope="keep failed sprint active",
                )
                service._save_backlog_item(backlog_item)
                service._save_sprint_state(failed_sprint)
                service._save_scheduler_state(
                    {
                        "active_sprint_id": failed_sprint["sprint_id"],
                        "next_slot_at": "2000-01-01T00:00:00+00:00",
                    }
                )

                with patch.object(service, "_prepare_actionable_backlog_for_sprint") as prepare_mock:
                    asyncio.run(service._poll_scheduler_once())

                scheduler_state = service._load_scheduler_state()
                self.assertEqual(str(scheduler_state.get("active_sprint_id") or ""), failed_sprint["sprint_id"])
                self.assertTrue(service.paths.sprint_file(failed_sprint["sprint_id"]).exists())
                self.assertEqual(len(list(service.paths.sprints_dir.glob("*.json"))), 1)
                prepare_mock.assert_not_called()

    def test_repair_non_actionable_carry_over_backlog_items_marks_legacy_pending_items_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                carry_over_item = build_backlog_item(
                    title="신규 기획",
                    summary="입력 부족으로 보류합니다.",
                    kind="enhancement",
                    source="carry_over",
                    scope="신규 기획",
                )
                carry_over_item["status"] = "pending"
                carry_over_item["fingerprint"] = service._build_backlog_fingerprint(
                    title=str(carry_over_item.get("title") or ""),
                    scope=str(carry_over_item.get("scope") or ""),
                    kind=str(carry_over_item.get("kind") or ""),
                )
                service._save_backlog_item(carry_over_item)
                service._save_sprint_state(
                    {
                        "sprint_id": "2026-Sprint-01-20260324T000400Z",
                        "status": "completed",
                        "trigger": "test",
                        "started_at": "2026-03-24T00:04:00+00:00",
                        "ended_at": "2026-03-24T00:04:30+00:00",
                        "selected_backlog_ids": [],
                        "selected_items": [],
                        "todos": [
                            {
                                "todo_id": "todo-legacy-blocked",
                                "backlog_id": "old-backlog",
                                "title": "신규 기획",
                                "owner_role": "planner",
                                "status": "blocked",
                                "request_id": "req-legacy-blocked",
                                "artifacts": [],
                                "started_at": "",
                                "ended_at": "",
                                "summary": "입력 부족",
                                "carry_over_backlog_id": carry_over_item["backlog_id"],
                            }
                        ],
                        "commit_sha": "",
                        "report_path": "",
                    }
                )

                repaired = service._repair_non_actionable_carry_over_backlog_items()

                updated = service._load_backlog_item(carry_over_item["backlog_id"])
                self.assertIn(carry_over_item["backlog_id"], repaired)
                self.assertEqual(updated["status"], "blocked")
