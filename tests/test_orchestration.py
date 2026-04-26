from orchestration_test_utils import *


class TeamsRuntimeOrchestrationTests(OrchestrationTestCase):
    def test_split_discord_chunks_preserves_fenced_code_blocks(self):
        code_lines = "\n".join(f"print('line-{index}')  # {'x' * 60}" for index in range(80))
        content = f"서론 문단\n\n```python\n{code_lines}\n```\n\n결론 문단"

        chunks = _split_discord_chunks(content, limit=500)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 500)
            if "```" in chunk:
                self.assertEqual(chunk.count("```"), 2)

    def test_split_discord_chunks_recovers_single_line_fenced_code_blocks(self):
        content = f"```{'x' * 900}```"

        chunks = _split_discord_chunks(content, limit=180)

        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 180)
            self.assertTrue(chunk.startswith("```"))
            self.assertTrue(chunk.endswith("```"))
            self.assertEqual(chunk.count("```"), 2)
            self.assertIn("\n", chunk)

    def test_render_discord_message_chunks_adds_sequence_markers_and_respects_prefix_limit(self):
        rendered = _render_discord_message_chunks("A" * 4500, prefix="<@user> ")

        self.assertGreater(len(rendered), 2)
        self.assertTrue(rendered[0].startswith("<@user> [1/"))
        self.assertIn("[2/", rendered[1])
        for chunk in rendered:
            self.assertLessEqual(len(chunk), 2000)

    def test_render_discord_message_chunks_uses_runtime_markers(self):
        rendered = _render_discord_message_chunks("payload", prefix="")

        self.assertEqual(rendered, [f"{MESSAGE_START_MARKER}\npayload\n{MESSAGE_END_MARKER}"])

    def test_parse_report_body_json_recovers_chunk_merged_fenced_json(self):
        body = """```json
{
  "approval_needed": false,
  "artifacts": [],
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
  "request_id": "20260325-8499077d",
  "role": "qa",
  "status": "completed",
  "summary": "방법론 개선 후속 기획이 필요합니다."
}
```"""

        parsed = _parse_report_body_json(body)

        self.assertEqual(parsed["role"], "qa")
        self.assertEqual(parsed["next_role"], "planner")
        self.assertEqual(parsed["status"], "completed")
        self.assertEqual(parsed["request_id"], "20260325-8499077d")

    def test_non_orchestrator_listener_retries_after_listen_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                calls: list[int] = []

                async def fake_listen(_on_message, on_ready=None):
                    calls.append(len(calls) + 1)
                    if len(calls) == 1:
                        raise DiscordListenError("temporary network issue")
                    raise asyncio.CancelledError()

                service.discord_client.listen = fake_listen

                with patch.object(orchestration_module.LOGGER, "warning") as warning_mock:
                    with patch.object(orchestration_module.LOGGER, "exception") as exception_mock:
                        with self.assertRaises(asyncio.CancelledError):
                            asyncio.run(service._listen_forever())

                self.assertEqual(calls, [1, 2])
                exception_mock.assert_not_called()
                warning_mock.assert_called_once()
                self.assertIn("after listen error", warning_mock.call_args.args[0])
                state = read_json(service.paths.agent_state_file("planner"))
                self.assertEqual(state["listener_status"], "reconnecting")
                self.assertEqual(state["listener_error_category"], "discord_connection_failed")

    def test_non_orchestrator_suppresses_repeated_malformed_trusted_relay_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                trusted_author_id = next(iter(service.discord_config.trusted_bot_ids))
                message = DiscordMessage(
                    message_id="relay-no-kind-1",
                    channel_id=service.discord_config.relay_channel_id,
                    guild_id="guild-1",
                    author_id=trusted_author_id,
                    author_name="relay",
                    content=f"<@{service.role_config.bot_id}>\nrequest_id: relay-1\nintent: implement\nscope: test",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(orchestration_module.LOGGER, "debug") as debug_mock:
                    with patch.object(orchestration_module.LOGGER, "info") as info_mock:
                        asyncio.run(service.handle_message(message))
                        asyncio.run(service.handle_message(message))

                info_mock.assert_not_called()
                debug_mock.assert_called_once()
                self.assertIn("Ignoring malformed trusted relay", debug_mock.call_args.args[0])

    def test_orchestrator_suppresses_repeated_unsupported_trusted_relay_logs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                trusted_author_id = next(iter(service.discord_config.trusted_bot_ids))
                message = DiscordMessage(
                    message_id="relay-unsupported-kind-1",
                    channel_id=service.discord_config.relay_channel_id,
                    guild_id="guild-1",
                    author_id=trusted_author_id,
                    author_name="relay",
                    content="request_id: relay-2\nintent: route\nscope: test\nparams:\n  _teams_kind: none",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                with patch.object(orchestration_module.LOGGER, "debug") as debug_mock:
                    with patch.object(orchestration_module.LOGGER, "info") as info_mock:
                        asyncio.run(service.handle_message(message))
                        asyncio.run(service.handle_message(message))

                info_mock.assert_not_called()
                debug_mock.assert_called_once()
                self.assertIn("Ignoring malformed trusted relay", debug_mock.call_args.args[0])

    def test_send_relay_mentions_target_bot_id_from_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                envelope = MessageEnvelope(
                    request_id="20260322-abcd1234",
                    sender="orchestrator",
                    target="developer",
                    intent="implement",
                    urgency="normal",
                    scope="Implement the task",
                )

                asyncio.run(service._send_relay(envelope))

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                channel_id, content = service.discord_client.sent_channels[0]
                self.assertEqual(channel_id, "111111111111111111")
                self.assertIn("<@111111111111111116>", content)
                self.assertNotIn("\nfrom:", content)
                self.assertNotIn("\nto:", content)

    def test_send_relay_internal_transport_enqueues_inbox_payload_and_posts_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator", relay_transport="internal")
                envelope = MessageEnvelope(
                    request_id="20260322-internal1",
                    sender="orchestrator",
                    target="developer",
                    intent="implement",
                    urgency="normal",
                    scope="Internal relay summary test",
                    body=json.dumps({"summary": "delegate payload", "status": "queued"}, ensure_ascii=False),
                    params={"_teams_kind": "delegate"},
                )

                sent = asyncio.run(service._send_relay(envelope))

                self.assertTrue(sent)
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                channel_id, content = service.discord_client.sent_channels[0]
                self.assertEqual(channel_id, service.discord_config.relay_channel_id)
                self.assertIn(f"{INTERNAL_RELAY_SUMMARY_MARKER} orchestrator -> developer (delegate)", content)
                self.assertIn("```text", content)
                self.assertNotIn("[teams_runtime relay_summary]", content)
                self.assertNotIn("relay_id", content)
                self.assertIn("[핵심 전달]", content)
                self.assertIn("- delegate payload", content)
                self.assertIn("delegate payload", content)
                inbox_dir = service.paths.runtime_root / "internal_relay" / "inbox" / "developer"
                relay_files = sorted(inbox_dir.glob("*.json"))
                self.assertEqual(len(relay_files), 1)
                payload = read_json(relay_files[0])
                self.assertEqual(payload.get("transport"), "internal")
                self.assertEqual(dict(payload.get("envelope") or {}).get("to"), "developer")

    def test_internal_relay_inbox_delegate_is_consumed_by_target_role(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                orchestrator_service = TeamService(tmpdir, "orchestrator", relay_transport="internal")
                planner_service = TeamService(tmpdir, "planner", relay_transport="internal")
                envelope = MessageEnvelope(
                    request_id="20260322-internal2",
                    sender="orchestrator",
                    target="planner",
                    intent="plan",
                    urgency="normal",
                    scope="consume internal relay delegate",
                    params={"_teams_kind": "delegate"},
                )

                asyncio.run(orchestrator_service._send_relay(envelope))
                planner_service._handle_delegated_request = AsyncMock()

                asyncio.run(planner_service._consume_internal_relay_once())

                planner_service._handle_delegated_request.assert_awaited_once()
                planner_inbox_dir = planner_service.paths.runtime_root / "internal_relay" / "inbox" / "planner"
                self.assertEqual(sorted(planner_inbox_dir.glob("*.json")), [])

    def test_non_orchestrator_ignores_internal_relay_summary_marker_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                service._handle_delegated_request = AsyncMock()
                trusted_author_id = next(iter(service.discord_config.trusted_bot_ids))
                message = DiscordMessage(
                    message_id="relay-summary-1",
                    channel_id=service.discord_config.relay_channel_id,
                    guild_id="guild-1",
                    author_id=trusted_author_id,
                    author_name="relay-summary",
                    content=(
                        f"{INTERNAL_RELAY_SUMMARY_MARKER} orchestrator -> planner (delegate)\n"
                        "```text\n[전달 정보]\n- 요청 ID: N/A\n```\n\n"
                        "```text\n[핵심 전달]\n- planner summary message\n```"
                    ),
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service.handle_message(message))

                service._handle_delegated_request.assert_not_awaited()
                self.assertEqual(service.discord_client.sent_channels, [])
                self.assertEqual(service.discord_client.sent_dms, [])

    def test_internal_relay_summary_message_keeps_multiline_body_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator", relay_transport="internal")
                envelope = MessageEnvelope(
                    request_id="20260405-summary-rich",
                    sender="architect",
                    target="developer",
                    intent="implement",
                    urgency="normal",
                    scope="relay summary readability improvement",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "status": "completed",
                            "summary": "첫 번째 요약 줄입니다.\n두 번째 요약 줄입니다.\n세 번째 요약 줄입니다.",
                            "next_role": "qa",
                            "proposals": {"backlog_items": [{"title": "a"}, {"title": "b"}]},
                            "artifacts": ["workspace/a.py", "workspace/b.py"],
                            "insights": ["one", "two"],
                        },
                    },
                )

                content = service._build_internal_relay_summary_message(envelope)

                self.assertIn(f"{INTERNAL_RELAY_SUMMARY_MARKER} architect -> developer (report)", content)
                self.assertIn("```text", content)
                self.assertNotIn("[teams_runtime relay_summary]", content)
                self.assertNotIn("relay_id", content)
                self.assertIn("[이관 이유]", content)
                self.assertIn("- 다음 역할: qa", content)
                self.assertIn("[핵심 전달]", content)
                self.assertIn("- 첫 번째 요약 줄입니다. 두 번째 요약 줄입니다. 세 번째 요약 줄입니다.", content)
                self.assertIn("[지금 볼 것]", content)
                self.assertIn("backlog 후보 2건", content)
                self.assertIn("첫 번째 요약 줄입니다.", content)
                self.assertNotIn("[상태]", content)
                self.assertNotIn("- 아티팩트:", content)
                self.assertNotIn("- 인사이트:", content)
                self.assertNotIn("[추가 맥락]", content)
                self.assertNotIn("previous role:", content)
                self.assertNotIn("latest summary:", content)
                self.assertIn("[참고 파일]", content)
                self.assertIn("workspace/a.py", content)
                self.assertIn("workspace/b.py", content)
                self.assertLess(content.index("[이관 이유]"), content.index("[핵심 전달]"))

    def test_internal_relay_summary_prefers_concrete_implementation_guidance_over_meta_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator", relay_transport="internal")
                envelope = MessageEnvelope(
                    request_id="20260410-semantic-guidance-1",
                    sender="architect",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="strategy_3 관측 엔진 전이 규칙 구체화",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "status": "completed",
                            "summary": "strategy_3 판단 엔진의 상태 전이와 triggered 허용 조건을 developer가 바로 구현·정리할 수 있도록 기술 계약으로 구체화했습니다.",
                            "next_role": "developer",
                            "proposals": {
                                "implementation_guidance": {
                                    "evaluation_order": [
                                        "program trade acceleration을 먼저 계산한다.",
                                        "그 다음 broker concentration과 market gate를 순서대로 평가한다.",
                                    ],
                                    "state_transitions": [
                                        "watch -> candidate는 acceleration 임계치 충족 시에만 허용한다.",
                                        "candidate -> triggered는 broker concentration과 market gate가 모두 통과해야 한다.",
                                    ],
                                    "triggered_conditions": [
                                        "required stream이 하나라도 stale이면 triggered를 금지한다."
                                    ],
                                    "fail_closed_conditions": [
                                        "market gate 계산 실패 시 decision을 suppressed로 고정한다."
                                    ],
                                    "implementation_steps": [
                                        "strategy_3 evaluator에 evaluation order를 고정한다.",
                                        "상태 전이 테스트를 watch/candidate/triggered/suppressed별로 추가한다.",
                                    ],
                                    "decision_rationale": [
                                        "gate 계산 실패를 watch로 두면 false positive가 날 수 있어 fail-closed가 필요하다."
                                    ],
                                },
                                "workflow_transition": {
                                    "outcome": "advance",
                                    "target_phase": "implementation",
                                    "target_step": "developer_build",
                                    "reason": "평가 순서와 fail-closed 억제 조건이 정리되어 developer가 구현을 진행할 수 있습니다.",
                                    "unresolved_items": [
                                        "triggered 승격 테스트 fixture를 함께 보강해야 합니다."
                                    ],
                                },
                            },
                            "artifacts": ["workspace/strategy_3.md"],
                            "insights": ["state machine clarified"],
                            "error": "",
                        },
                    },
                )

                content = service._build_internal_relay_summary_message(envelope)

                self.assertIn("[핵심 전달]", content)
                self.assertIn("- 평가 순서: program trade acceleration을 먼저 계산한다.", content)
                self.assertIn("[지금 볼 것]", content)
                self.assertIn("상태 전이: candidate -> triggered는 broker concentration과 market gate가 모두 통과해야 한다.", content)
                self.assertIn("상태 전이: watch -> candidate는 acceleration 임계치 충족 시에만 허용한다.", content)
                self.assertNotIn("기술 계약으로 구체화했습니다.", content)
                self.assertIn("상태 전이 테스트를 watch/candidate/triggered/suppressed별로 추가한다.", content)
                self.assertIn("[이관 이유]", content)
                self.assertIn("- 다음 역할: developer", content)
                self.assertLess(
                    content.index("[이관 이유]"),
                    content.index("[핵심 전달]"),
                )
                self.assertIn("[참고 파일]", content)
                self.assertIn("workspace/strategy_3.md", content)
                self.assertNotIn("[추가 맥락]", content)

    def test_internal_relay_summary_fallback_uses_scope_or_body_as_what(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator", relay_transport="internal")
                envelope = MessageEnvelope(
                    request_id="20260411-fallback-1",
                    sender="planner",
                    target="developer",
                    intent="implement",
                    urgency="normal",
                    scope="scope-only fallback message",
                    body="",
                    params={"_teams_kind": "delegate"},
                )

                content = service._build_internal_relay_summary_message(envelope)

                self.assertIn(f"{INTERNAL_RELAY_SUMMARY_MARKER} planner -> developer (delegate)", content)
                self.assertIn("[핵심 전달]", content)
                self.assertIn("- scope-only fallback message", content)
                self.assertNotIn("[상태]", content)

    def test_internal_relay_summary_fallback_prefers_scope_over_meta_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator", relay_transport="internal")
                semantic_context = {
                    "what_summary": "",
                    "what_details": [],
                    "how_summary": "",
                    "why_summary": "",
                    "route_reason": "",
                }
                envelope = MessageEnvelope(
                    request_id="20260411-fallback-scope-2",
                    sender="planner",
                    target="developer",
                    intent="implement",
                    urgency="normal",
                    scope="",
                    body=json.dumps(
                        {
                            "summary": "정리했습니다.",
                            "scope": "핵심 구현 범위: concrete action",
                        },
                        ensure_ascii=False,
                    ),
                    params={"_teams_kind": "delegate"},
                )

                with patch.object(service, "_build_role_result_semantic_context", return_value=semantic_context):
                    content = service._build_internal_relay_summary_message(envelope)

                self.assertIn("[핵심 전달]", content)
                self.assertIn("- 핵심 구현 범위: concrete action", content)
                self.assertNotIn("- 정리했습니다.", content)

    def test_internal_relay_summary_shows_status_when_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator", relay_transport="internal")
                envelope = MessageEnvelope(
                    request_id="20260411-error-state-1",
                    sender="architect",
                    target="developer",
                    intent="implement",
                    urgency="normal",
                    scope="failure path message",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "status": "failed",
                            "summary": "실패한 구현 결과를 정리했습니다.",
                            "next_role": "developer",
                            "error": "runtime error: timeout while saving artifacts",
                            "artifacts": ["workspace/a.py", "workspace/b.py"],
                            "insights": ["one"],
                        },
                    },
                )

                content = service._build_internal_relay_summary_message(envelope)

                self.assertIn("[핵심 전달]", content)
                self.assertIn("- 실패한 구현 결과를 정리했습니다.", content)
                self.assertIn("[이관 이유]", content)
                self.assertIn("- 다음 역할: developer", content)
                self.assertIn("[상태]", content)
                self.assertIn("- failed", content)
                self.assertIn("[오류]", content)
                self.assertIn("runtime error: timeout while saving artifacts", content)
                self.assertNotIn("- 아티팩트:", content)
                self.assertNotIn("- 인사이트:", content)

    def test_internal_relay_summary_makes_planner_backlog_and_milestone_outputs_concrete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator", relay_transport="internal")
                envelope = MessageEnvelope(
                    request_id="20260410-planner-semantic-1",
                    sender="planner",
                    target="orchestrator",
                    intent="report",
                    urgency="normal",
                    scope="initial sprint planning",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "status": "completed",
                            "summary": "초기 phase용 plan/spec과 prioritized todo를 정리했습니다.",
                            "next_role": "designer",
                            "proposals": {
                                "revised_milestone_title": "workflow refined",
                                "backlog_items": [
                                    {
                                        "title": "manual sprint start gate",
                                        "summary": "milestone 없이는 sprint를 시작하지 않도록 정리",
                                        "acceptance_criteria": ["milestone 없이 시작되지 않는다."],
                                    },
                                    {
                                        "title": "sprint folder artifact rendering",
                                        "summary": "sprint folder living docs를 렌더링",
                                    },
                                ],
                                "required_inputs": ["현재 kickoff 문서"],
                            },
                            "artifacts": [],
                            "insights": [],
                            "error": "",
                        },
                    },
                )

                content = service._build_internal_relay_summary_message(envelope)

                self.assertIn("[핵심 전달]", content)
                self.assertIn("- 마일스톤을 workflow refined로 정리하고 backlog/todo 2건을 확정했습니다.", content)
                self.assertIn("[지금 볼 것]", content)
                self.assertIn("backlog/todo: manual sprint start gate", content)
                self.assertIn("backlog/todo: sprint folder artifact rendering", content)
                self.assertIn("[유의사항]", content)
                self.assertIn("완료 기준: planning을 닫을 수 있게 2건 확보", content)
                self.assertIn("[이관 이유]", content)
                self.assertIn("- 다음 역할: designer", content)
                self.assertLess(
                    content.index("[이관 이유]"),
                    content.index("[핵심 전달]"),
                )

    def test_build_progress_report_prioritizes_next_action_before_evidence(self):
        report = build_progress_report(
            request="Backlog Sourcing",
            scope="runtime logs",
            status="완료",
            list_summary="candidate 1건",
            detail_summary="runtime log finding을 bug backlog로 등록했습니다.",
            process_summary="pid=1",
            log_summary="sample log",
            end_reason="없음",
            judgment="runtime log finding을 bug backlog로 등록했습니다.",
            next_action="planner backlog review",
            artifacts=["shared_workspace/backlog.md"],
        )

        self.assertIn("➡️ 다음: planner backlog review", report)
        self.assertIn("🔎 근거:", report)
        self.assertLess(report.index("➡️ 다음: planner backlog review"), report.index("🔎 근거:"))

    def test_build_progress_report_renders_sections_as_fenced_text_blocks(self):
        report = build_progress_report(
            request="Sprint Closeout",
            scope="현재 스프린트: 260416-Sprint-22:53",
            status="완료",
            list_summary="sprint runner",
            detail_summary="closeout summary",
            process_summary="없음",
            log_summary="sample log",
            end_reason="없음",
            judgment="closeout summary",
            next_action="대기",
            sections=[
                orchestration_module.ReportSection(
                    title="한눈에 보기",
                    lines=(
                        "- TL;DR: README/.gitignore 계약 정렬은 전진했지만 final QA 재검증 없이 closeout됐습니다.",
                        "- sprint_id: 260416-Sprint-22:53",
                    ),
                )
            ],
        )

        self.assertIn("```text\n[한눈에 보기]", report)
        self.assertIn("- TL;DR:", report)
        self.assertNotIn("+---", report)
        self.assertNotIn("| 한눈에 보기", report)

    def test_summarize_boxed_report_excerpt_skips_fenced_section_headers(self):
        excerpt = summarize_boxed_report_excerpt(
            "```text\n[한눈에 보기]\n- TL;DR: summary\n- sprint_id: sprint-1\n```\n\n```text\n[다음 액션]\n- 없음\n```"
        )

        self.assertEqual(excerpt, "- TL;DR: summary\n- sprint_id: sprint-1\n- 없음")

    def test_internal_relay_summary_surfaces_core_and_supporting_layers_from_design_feedback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator", relay_transport="internal")
                envelope = MessageEnvelope(
                    request_id="20260411-layer-summary-1",
                    sender="designer",
                    target="planner",
                    intent="report",
                    urgency="normal",
                    scope="사용자-facing 정보 레이어 정리",
                    params={
                        "_teams_kind": "report",
                        "result": {
                            "status": "completed",
                            "summary": "",
                            "proposals": {
                                "design_feedback": {
                                    "entry_point": "info_prioritization",
                                    "user_judgment": [
                                        "현재 상태와 다음 액션은 핵심 레이어로 유지해야 합니다."
                                    ],
                                    "message_priority": {
                                        "lead": "현재 상태, 다음 액션",
                                        "summary": "현재 상황을 이해하는 중간 설명",
                                        "defer": "상세 로그, 참고 artifact",
                                    },
                                    "routing_rationale": "planner가 정보 계층을 spec에 반영하면 surface별 일관성이 올라갑니다.",
                                }
                            },
                            "artifacts": ["shared_workspace/sprints/spec.md"],
                            "insights": [],
                            "error": "",
                        },
                    },
                )

                content = service._build_internal_relay_summary_message(envelope)

                self.assertIn("[핵심 전달]", content)
                self.assertIn("- info prioritization 관점 UX 판단 1건을 정리했습니다.", content)
                self.assertIn("[지금 볼 것]", content)
                self.assertIn("핵심 레이어: 현재 상태, 다음 액션", content)

    def test_orchestrator_records_relay_failure_without_raising_from_callback_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FailingRelayDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                def fake_orchestrator_run_task(_envelope, request_record):
                    return {
                        "request_id": request_record["request_id"],
                        "role": "orchestrator",
                        "status": "completed",
                        "summary": "planner가 이어서 처리해야 하는 planning 요청입니다.",
                        "insights": [],
                        "proposals": {},
                        "artifacts": [],
                        "error": "",
                    }

                message = DiscordMessage(
                    message_id="msg-plan-relay-fail",
                    channel_id="dm-plan-fail",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="intent: route\nscope: planning\nplanner로 넘겨야 하는 요청",
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
                                "reason": "Selected planner for relay failure test.",
                            },
                        },
                    ),
                ):
                    asyncio.run(service.handle_message(message))

                request_files = list(service.paths.requests_dir.glob("*.json"))
                self.assertEqual(len(request_files), 1)
                request_payload = json.loads(request_files[0].read_text(encoding="utf-8"))
                self.assertEqual(request_payload["status"], "delegated")
                self.assertEqual(request_payload["relay_send_status"], "failed")
                self.assertEqual(request_payload["relay_send_target"], "relay:111111111111111111")
                self.assertEqual(request_payload["relay_send_attempts"], 3)
                self.assertIn("TimeoutError", request_payload["relay_send_error"])
                event_types = [str(event.get("type") or "") for event in request_payload.get("events") or []]
                self.assertIn("relay_send_failed", event_types)
                self.assertEqual(service.discord_client.sent_channels, [])
                self.assertEqual(len(service.discord_client.sent_dms), 2)
                self.assertIn("planner relay 전송이 실패해 요청 전달을 완료하지 못했습니다", service.discord_client.sent_dms[1][1])

    def test_send_channel_reply_splits_long_code_block_with_prefix_and_sequence_markers(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                code_lines = "\n".join(f"line_{index} = '{'x' * 70}'" for index in range(70))
                content = f"```python\n{code_lines}\n```"
                message = DiscordMessage(
                    message_id="reply-1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="tester",
                    content="ping",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service._send_channel_reply(message, content))

                self.assertGreater(len(service.discord_client.sent_channels), 1)
                self.assertTrue(service.discord_client.sent_channels[0][1].startswith("<@user-1> [1/"))
                for _channel_id, chunk in service.discord_client.sent_channels:
                    self.assertLessEqual(len(chunk), 2000)
                    if "```" in chunk:
                        self.assertEqual(chunk.count("```"), 2)

    def test_send_channel_reply_appends_runtime_model_and_reasoning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                message = DiscordMessage(
                    message_id="reply-runtime-1",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="tester",
                    content="ping",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service._send_channel_reply(message, "hello"))

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                self.assertIn("model: gpt-5.5 | reasoning: medium", service.discord_client.sent_channels[0][1])

    def test_send_channel_reply_appends_none_reasoning_for_gemini_model(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                service.runtime_config.role_defaults["orchestrator"] = RoleRuntimeConfig(
                    model="gemini-2.5-pro",
                    reasoning="high",
                )
                message = DiscordMessage(
                    message_id="reply-runtime-2",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="tester",
                    content="ping",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service._send_channel_reply(message, "hello"))

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                self.assertIn("model: gemini-2.5-pro | reasoning: None", service.discord_client.sent_channels[0][1])

    def test_announce_startup_sends_progress_report_to_startup_channel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                asyncio.run(service._announce_startup())

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                channel_id, content = service.discord_client.sent_channels[0]
                self.assertEqual(channel_id, "111111111111111111")
                self.assertNotIn("```", content)
                self.assertNotIn("┌", content)
                self.assertNotIn("└", content)
                self.assertIn("[준비 완료] ✅ orchestrator", content)
                self.assertIn("🎯 현재 스프린트: 없음", content)
                self.assertIn("📡 채널: startup 111111111111111111 | relay 111111111111111111", content)
                self.assertNotIn("[작업 보고]", content)

    def test_send_sprint_report_uses_active_sprint_id_in_scope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                service._save_scheduler_state({"active_sprint_id": "260330-Sprint-19:16"})

                asyncio.run(
                    service._send_sprint_report(
                        title="🚀 스프린트 시작",
                        body="sprint started",
                    )
                )

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, content = service.discord_client.sent_channels[0]
                self.assertIn("현재 스프린트: 260330-Sprint-19:16", content)
                self.assertIn("[상세]", content)
                self.assertIn("sprint started", content)
                self.assertNotIn("sprint_series_id", content)

    def test_send_terminal_sprint_reports_routes_user_summary_to_report_channel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "discord_agents_config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('startup_channel_id: "111111111111111111"', 'startup_channel_id: "222222222222222222"', 1)
            config_text = config_text.replace('report_channel_id: "111111111111111111"', 'report_channel_id: "333333333333333333"', 1)
            config_text = config_text.replace('relay_channel_id: "111111111111111111"', 'relay_channel_id: "444444444444444444"', 1)
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="discord routing summary",
                    trigger="manual_start",
                )
                sprint_state["phase"] = "wrap_up"
                sprint_state["status"] = "completed"
                sprint_state["closeout_status"] = "verified"
                sprint_state["ended_at"] = "2026-04-05T17:05:00+09:00"
                sprint_state["todos"] = [
                    {
                        "todo_id": "todo-discord-summary",
                        "backlog_id": "backlog-discord-summary",
                        "title": "Sprint Discord summary 개선",
                        "milestone_title": sprint_state["milestone_title"],
                        "priority_rank": 1,
                        "owner_role": "developer",
                        "status": "committed",
                        "request_id": "20260405-discord-summary",
                        "summary": "사용자용 스프린트 요약 메시지를 정리합니다.",
                        "artifacts": ["workspace/libs/runtime/reporting.py"],
                        "started_at": "",
                        "ended_at": "",
                        "carry_over_backlog_id": "",
                    }
                ]
                service._save_request(
                    {
                        "request_id": "20260405-discord-summary",
                        "status": "committed",
                        "intent": "route",
                        "urgency": "normal",
                        "scope": "김단타 sprint final summary 개선",
                        "body": "사용자용 스프린트 완료 보고를 의미 중심으로 정리합니다.",
                        "artifacts": ["workspace/libs/runtime/reporting.py"],
                        "params": {},
                        "current_role": "developer",
                        "next_role": "",
                        "owner_role": "developer",
                        "sprint_id": sprint_state["sprint_id"],
                        "created_at": "2026-04-05T17:00:00+09:00",
                        "updated_at": "2026-04-05T17:03:00+09:00",
                        "fingerprint": "20260405-discord-summary",
                        "reply_route": {},
                        "events": [],
                        "result": {
                            "request_id": "20260405-discord-summary",
                            "role": "developer",
                            "status": "committed",
                            "summary": "김단타 sprint final summary가 이제 변화 이유와 의미를 함께 설명하도록 바뀌었습니다.",
                            "insights": [
                                "사용자가 왜 closeout 결과가 달라졌는지 보고서만 보고 바로 이해할 수 있게 정리합니다."
                            ],
                            "proposals": {},
                            "artifacts": ["workspace/libs/runtime/reporting.py"],
                            "next_role": "",
                            "error": "",
                        },
                        "version_control_status": "committed",
                        "version_control_sha": "abcdef0123456789",
                        "version_control_paths": ["workspace/libs/runtime/reporting.py"],
                        "version_control_message": "reporting.py: explain sprint change meaning",
                        "version_control_error": "",
                        "task_commit_status": "committed",
                        "task_commit_sha": "abcdef0123456789",
                        "task_commit_paths": ["workspace/libs/runtime/reporting.py"],
                        "task_commit_message": "reporting.py: explain sprint change meaning",
                        "visited_roles": ["planner", "developer"],
                        "task_commit_summary": "김단타 sprint final summary가 변화 이유와 의미를 함께 보여 주도록 정리했습니다.",
                    }
                )
                closeout_result = {
                    "status": "verified",
                    "message": "스프린트 closeout 검증을 완료했습니다.",
                    "commit_count": 1,
                    "commit_shas": ["abcdef0123456789"],
                    "representative_commit_sha": "abcdef0123456789",
                    "uncommitted_paths": [],
                }
                sprint_state["report_body"] = service._build_sprint_report_body(sprint_state, closeout_result)

                asyncio.run(
                    service._send_terminal_sprint_reports(
                        title="✅ 스프린트 완료",
                        sprint_state=sprint_state,
                        closeout_result=closeout_result,
                    )
                )

                channel_ids = [channel_id for channel_id, _content in service.discord_client.sent_channels]
                self.assertIn("222222222222222222", channel_ids)
                self.assertIn("333333333333333333", channel_ids)
                self.assertNotIn("444444444444444444", channel_ids)

                startup_contents = [
                    content for channel_id, content in service.discord_client.sent_channels if channel_id == "222222222222222222"
                ]
                report_contents = [
                    content for channel_id, content in service.discord_client.sent_channels if channel_id == "333333333333333333"
                ]
                combined_startup = "\n".join(startup_contents)
                combined_report = "\n".join(report_contents)
                self.assertTrue(any("[작업 보고]" in content for content in startup_contents))
                self.assertNotIn("🔄 변경 요약", combined_startup)
                self.assertNotIn("🧭 흐름", combined_startup)
                self.assertNotIn("🤖 에이전트 기여", combined_startup)
                self.assertNotIn("무엇이 달라졌나:", combined_startup)
                self.assertNotIn("의미:", combined_startup)
                self.assertNotIn("어떻게:", combined_startup)
                self.assertIn("**TL;DR**", combined_report)
                self.assertIn("```text", combined_report)
                self.assertIn(f"sprint_id : {sprint_state['sprint_id']}", combined_report)
                self.assertIn("🔄 변경 요약", combined_report)
                self.assertIn("무엇이 달라졌나:", combined_report)
                self.assertIn("의미:", combined_report)
                self.assertIn("어떻게:", combined_report)
                self.assertIn("🧭 흐름", combined_report)
                self.assertIn("🤖 에이전트 기여", combined_report)
                self.assertNotIn("... 외", combined_report)
                self.assertFalse(any("[작업 보고]" in content for content in report_contents))

    def test_send_terminal_sprint_reports_falls_back_to_detailed_relay_report_when_user_summary_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "discord_agents_config.yaml"
            config_text = config_path.read_text(encoding="utf-8")
            config_text = config_text.replace('startup_channel_id: "111111111111111111"', 'startup_channel_id: "222222222222222222"', 1)
            config_text = config_text.replace('report_channel_id: "111111111111111111"', 'report_channel_id: "333333333333333333"', 1)
            config_path.write_text(config_text, encoding="utf-8")

            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="discord routing summary fallback",
                    trigger="manual_start",
                )
                sprint_state["phase"] = "wrap_up"
                sprint_state["status"] = "completed"
                sprint_state["closeout_status"] = "verified"
                sprint_state["ended_at"] = "2026-04-05T17:05:00+09:00"
                sprint_state["todos"] = [
                    {
                        "todo_id": "todo-discord-summary-fallback",
                        "backlog_id": "backlog-discord-summary-fallback",
                        "title": "Sprint Discord summary fallback",
                        "milestone_title": sprint_state["milestone_title"],
                        "priority_rank": 1,
                        "owner_role": "developer",
                        "status": "committed",
                        "request_id": "20260405-discord-summary-fallback",
                        "summary": "사용자용 스프린트 요약 실패 시 relay 보고가 fallback detail을 유지합니다.",
                        "artifacts": ["workspace/libs/runtime/reporting.py"],
                        "started_at": "",
                        "ended_at": "",
                        "carry_over_backlog_id": "",
                    }
                ]
                service._save_request(
                    {
                        "request_id": "20260405-discord-summary-fallback",
                        "status": "committed",
                        "intent": "route",
                        "urgency": "normal",
                        "scope": "김단타 sprint final summary fallback",
                        "body": "사용자용 스프린트 완료 보고 전송 실패 시 relay fallback을 유지합니다.",
                        "artifacts": ["workspace/libs/runtime/reporting.py"],
                        "params": {},
                        "current_role": "developer",
                        "next_role": "",
                        "owner_role": "developer",
                        "sprint_id": sprint_state["sprint_id"],
                        "created_at": "2026-04-05T17:00:00+09:00",
                        "updated_at": "2026-04-05T17:03:00+09:00",
                        "fingerprint": "20260405-discord-summary-fallback",
                        "reply_route": {},
                        "events": [],
                        "result": {
                            "request_id": "20260405-discord-summary-fallback",
                            "role": "developer",
                            "status": "committed",
                            "summary": "relay fallback에서도 closeout 변화 이유와 의미를 확인할 수 있습니다.",
                            "insights": ["report channel 전송 실패 시 relay가 detail fallback을 제공합니다."],
                            "proposals": {},
                            "artifacts": ["workspace/libs/runtime/reporting.py"],
                            "next_role": "",
                            "error": "",
                        },
                        "version_control_status": "committed",
                        "version_control_sha": "abcdef0123456789",
                        "version_control_paths": ["workspace/libs/runtime/reporting.py"],
                        "version_control_message": "reporting.py: fallback relay summary",
                        "version_control_error": "",
                        "task_commit_status": "committed",
                        "task_commit_sha": "abcdef0123456789",
                        "task_commit_paths": ["workspace/libs/runtime/reporting.py"],
                        "task_commit_message": "reporting.py: fallback relay summary",
                        "visited_roles": ["planner", "developer"],
                        "task_commit_summary": "relay fallback에서도 변화 이유와 의미를 유지하도록 정리했습니다.",
                    }
                )
                closeout_result = {
                    "status": "verified",
                    "message": "스프린트 closeout 검증을 완료했습니다.",
                    "commit_count": 1,
                    "commit_shas": ["abcdef0123456789"],
                    "representative_commit_sha": "abcdef0123456789",
                    "uncommitted_paths": [],
                }
                sprint_state["report_body"] = service._build_sprint_report_body(sprint_state, closeout_result)
                service._send_sprint_completion_user_report = AsyncMock(return_value=False)

                asyncio.run(
                    service._send_terminal_sprint_reports(
                        title="✅ 스프린트 완료",
                        sprint_state=sprint_state,
                        closeout_result=closeout_result,
                    )
                )

                startup_contents = [
                    content for channel_id, content in service.discord_client.sent_channels if channel_id == "222222222222222222"
                ]
                combined_startup = "\n".join(startup_contents)
                self.assertTrue(any("[작업 보고]" in content for content in startup_contents))
                self.assertIn("[변경 요약]", combined_startup)
                self.assertIn("[Sprint A to Z]", combined_startup)
                self.assertIn("[에이전트 기여]", combined_startup)
                self.assertIn("무엇이 달라졌나:", combined_startup)
                self.assertIn("의미:", combined_startup)
                self.assertIn("어떻게:", combined_startup)
                service._send_sprint_completion_user_report.assert_awaited_once()

    def test_announce_startup_records_failure_and_sends_fallback_notice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FailingStartupDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                with patch.object(
                    service.notification_service,
                    "iter_startup_fallback_targets",
                    return_value=[("report", "222222222222222222")],
                ):
                    asyncio.run(service._announce_startup())

                state = read_json(service.paths.agent_state_file("orchestrator"))
                self.assertEqual(state["startup_notification_status"], "fallback_sent")
                self.assertEqual(state["startup_notification_channel"], "111111111111111111")
                self.assertEqual(state["startup_notification_attempts"], 3)
                self.assertIn("TimeoutError", state["startup_notification_error"])
                self.assertEqual(state["startup_notification_fallback_target"], "report:222222222222222222")
                self.assertEqual(len(service.discord_client.sent_channels), 1)
                fallback_channel_id, fallback_content = service.discord_client.sent_channels[0]
                self.assertEqual(fallback_channel_id, "222222222222222222")
                self.assertIn("startup 알림 복구", fallback_content)

    def test_on_ready_requests_milestone_when_no_active_sprint_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                asyncio.run(service._on_ready())

                self.assertEqual(len(service.discord_client.sent_channels), 2)
                self.assertNotIn("```", service.discord_client.sent_channels[0][1])
                self.assertNotIn("┌", service.discord_client.sent_channels[0][1])
                self.assertIn("[준비 완료] ✅ orchestrator", service.discord_client.sent_channels[0][1])
                self.assertIn("active sprint가 없습니다", service.discord_client.sent_channels[1][1])
                self.assertIn("milestone", service.discord_client.sent_channels[1][1].lower())
                scheduler_state = service._load_scheduler_state()
                self.assertTrue(scheduler_state["milestone_request_pending"])
                self.assertEqual(
                    scheduler_state["milestone_request_channel_id"],
                    service.discord_config.relay_channel_id,
                )
                self.assertEqual(scheduler_state["milestone_request_reason"], "startup_no_active_sprint")

    def test_poll_scheduler_once_does_not_repeat_pending_idle_milestone_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                service._save_scheduler_state(
                    {
                        "active_sprint_id": "",
                        "milestone_request_pending": True,
                        "milestone_request_sent_at": "2026-03-31T15:10:00+09:00",
                        "milestone_request_channel_id": service.discord_config.relay_channel_id,
                        "milestone_request_reason": "startup_no_active_sprint",
                    }
                )

                asyncio.run(service._poll_scheduler_once())

                self.assertEqual(service.discord_client.sent_channels, [])

    def test_poll_scheduler_once_preserves_idle_milestone_pending_after_first_send(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                asyncio.run(service._poll_scheduler_once())

                scheduler_state = service._load_scheduler_state()
                self.assertTrue(scheduler_state["milestone_request_pending"])
                self.assertEqual(
                    scheduler_state["milestone_request_channel_id"],
                    service.discord_config.relay_channel_id,
                )
                self.assertEqual(scheduler_state["milestone_request_reason"], "idle_no_active_sprint")
                self.assertEqual(len(service.discord_client.sent_channels), 1)

                asyncio.run(service._poll_scheduler_once())

                self.assertEqual(len(service.discord_client.sent_channels), 1)

    def test_handle_message_sends_immediate_receipt_for_dm(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                service._handle_orchestrator_message = AsyncMock()
                message = DiscordMessage(
                    message_id="msg-receipt-dm",
                    channel_id="dm-1",
                    guild_id=None,
                    author_id="user-1",
                    author_name="tester",
                    content="intent: status\nscope: sprint",
                    is_dm=True,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service.handle_message(message))

                self.assertEqual(service.discord_client.sent_dms, [("user-1", "수신양호")])
                service._handle_orchestrator_message.assert_awaited_once_with(message)

    def test_handle_message_sends_immediate_receipt_for_guild_message(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                service._handle_non_orchestrator_message = AsyncMock()
                message = DiscordMessage(
                    message_id="msg-receipt-guild",
                    channel_id="channel-1",
                    guild_id="guild-1",
                    author_id="user-1",
                    author_name="tester",
                    content=f"<@{service.role_config.bot_id}> status sprint",
                    is_dm=False,
                    mentions_bot=True,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service.handle_message(message))

                self.assertEqual(service.discord_client.sent_channels, [("channel-1", "<@user-1> 수신양호")])
                service._handle_non_orchestrator_message.assert_awaited_once_with(message)

    def test_handle_message_skips_immediate_receipt_for_trusted_relay(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "planner")
                service._handle_non_orchestrator_message = AsyncMock()
                message = DiscordMessage(
                    message_id="relay-receipt-skip",
                    channel_id=service.discord_config.relay_channel_id,
                    guild_id="guild-1",
                    author_id=next(iter(service.discord_config.trusted_bot_ids)),
                    author_name="relay",
                    content="request_id: relay-1\nintent: plan\nscope: first task\nparams:\n  _teams_kind: delegate",
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service.handle_message(message))

                self.assertEqual(service.discord_client.sent_channels, [])
                self.assertEqual(service.discord_client.sent_dms, [])
                service._handle_non_orchestrator_message.assert_awaited_once_with(message)

    def test_send_sprint_kickoff_uses_emoji_title(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                asyncio.run(
                    service._send_sprint_kickoff(
                        {
                            "sprint_id": "260324-Sprint-09:00",
                            "trigger": "backlog_ready",
                            "selected_items": [{"backlog_id": "b1", "title": "선택된 backlog"}],
                            "todos": [
                                {
                                    "todo_id": "todo-1",
                                    "title": "planner가 이번 스프린트 계획을 정리합니다.",
                                    "owner_role": "planner",
                                }
                            ],
                        }
                    )
                )

                self.assertEqual(len(service.discord_client.sent_channels), 1)
                _channel_id, content = service.discord_client.sent_channels[0]
                self.assertIn("🚀 스프린트 시작", content)
                self.assertIn("[킥오프]", content)
                self.assertIn("- sprint_id: 260324-Sprint-09:00", content)
                self.assertNotIn("sprint_series_id", content)
                self.assertIn("[선정 작업]", content)
                self.assertIn("planner가 이번 스프린트 계획을 정리합니다.", content)
                self.assertIn("todo_id=todo-1", content)

    def test_send_sprint_kickoff_falls_back_to_selected_backlog_when_todo_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                asyncio.run(
                    service._send_sprint_kickoff(
                        {
                            "sprint_id": "2026-Sprint-01-20260324T000001Z",
                            "trigger": "backlog_ready",
                            "selected_items": [{"backlog_id": "backlog-1", "title": "intraday trading 개선"}],
                            "todos": [],
                        }
                    )
                )

                _channel_id, content = service.discord_client.sent_channels[0]
                self.assertIn("intraday trading 개선", content)
                self.assertIn("backlog_id=backlog-1", content)

    def test_send_sprint_kickoff_shows_empty_state_when_no_selection(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                asyncio.run(
                    service._send_sprint_kickoff(
                        {
                            "sprint_id": "2026-Sprint-01-20260324T000002Z",
                            "trigger": "backlog_ready",
                            "selected_items": [],
                            "todos": [],
                        }
                    )
                )

                _channel_id, content = service.discord_client.sent_channels[0]
                self.assertIn("선택된 작업 없음", content)
