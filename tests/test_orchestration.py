from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import teams_runtime.core.orchestration as orchestration_module
from teams_runtime.core.backlog_store import merge_backlog_payload
from teams_runtime.core.parsing import envelope_to_text
from teams_runtime.core.reports import build_progress_report
from teams_runtime.core.sprints import (
    build_backlog_item,
    build_sprint_artifact_folder_name,
    build_todo_item,
)
from teams_runtime.core.orchestration import (
    INTERNAL_RELAY_SUMMARY_MARKER,
    TeamService,
    _parse_report_body_json,
    _render_discord_message_chunks,
    _split_discord_chunks,
)
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.persistence import read_json, write_json
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.discord.client import (
    DiscordAttachment,
    DiscordListenError,
    DiscordSendError,
    DiscordMessage,
    MESSAGE_END_MARKER,
    MESSAGE_START_MARKER,
)
from teams_runtime.models import MessageEnvelope, RoleRuntimeConfig


class FakeDiscordClient:
    def __init__(self, *args, **kwargs):
        self.sent_channels: list[tuple[str, str]] = []
        self.sent_dms: list[tuple[str, str]] = []

    async def listen(self, on_message, on_ready=None):
        if on_ready is not None:
            result = on_ready()
            if asyncio.iscoroutine(result):
                await result
        return None

    async def send_channel_message(self, channel_id, content):
        self.sent_channels.append((str(channel_id), str(content)))
        return DiscordMessage(
            message_id="1",
            channel_id=str(channel_id),
            guild_id="1",
            author_id="999",
            author_name="bot",
            content=str(content),
            is_dm=False,
            mentions_bot=False,
            created_at=datetime.now(timezone.utc),
        )

    async def send_dm(self, user_id, content):
        self.sent_dms.append((str(user_id), str(content)))
        return DiscordMessage(
            message_id="2",
            channel_id="dm",
            guild_id=None,
            author_id="999",
            author_name="bot",
            content=str(content),
            is_dm=True,
            mentions_bot=False,
            created_at=datetime.now(timezone.utc),
        )

    async def close(self):
        return None


class FailingStartupDiscordClient(FakeDiscordClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed_startup = False

    async def send_channel_message(self, channel_id, content):
        normalized_channel_id = str(channel_id)
        if normalized_channel_id == "111111111111111111" and not self.failed_startup:
            self.failed_startup = True
            raise DiscordSendError(
                "Discord send operation failed during fetch_channel(111111111111111111) after 3 attempt(s): TimeoutError: timeout",
                attempts=3,
                last_error=asyncio.TimeoutError("timeout"),
                retryable=True,
                phase="fetch_channel(111111111111111111)",
        )
        return await super().send_channel_message(channel_id, content)


class FailingRelayDiscordClient(FakeDiscordClient):
    async def send_channel_message(self, channel_id, content):
        raise DiscordSendError(
            "Discord send operation failed during channel.send(111111111111111111) after 3 attempt(s): TimeoutError: timeout",
            attempts=3,
            last_error=asyncio.TimeoutError("timeout"),
            retryable=True,
            phase="channel.send(111111111111111111)",
        )


class FailingSourcerSendDiscordClient(FakeDiscordClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client_name = str(kwargs.get("client_name") or "")

    async def send_channel_message(self, channel_id, content):
        if self.client_name == "sourcer":
            raise DiscordSendError(
                "Discord send operation failed during channel.send(1486503058765779066) after 3 attempt(s): TimeoutError: timeout",
                attempts=3,
                last_error=asyncio.TimeoutError("timeout"),
                retryable=True,
                phase="channel.send(1486503058765779066)",
            )
        return await super().send_channel_message(channel_id, content)


class FailingSourcerDnsDiscordClient(FakeDiscordClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.client_name = str(kwargs.get("client_name") or "")

    async def send_channel_message(self, channel_id, content):
        if self.client_name == "sourcer":
            dns_error = RuntimeError(
                "Cannot connect to host discord.com:443 ssl:default [nodename nor servname provided, or not known]"
            )
            raise DiscordSendError(
                "Discord send operation failed during channel.send(1486503058765779066) after 3 attempt(s): ClientConnectorDNSError: Cannot connect to host discord.com:443 ssl:default [nodename nor servname provided, or not known]",
                attempts=3,
                last_error=dns_error,
                retryable=True,
                phase="channel.send(1486503058765779066)",
            )
        return await super().send_channel_message(channel_id, content)


class TeamsRuntimeOrchestrationTests(unittest.TestCase):
    @staticmethod
    def _workflow_phase_for_step(step):
        return {
            "planner_draft": "planning",
            "planner_advisory": "planning",
            "planner_finalize": "planning",
            "architect_guidance": "implementation",
            "developer_build": "implementation",
            "architect_review": "implementation",
            "developer_revision": "implementation",
            "qa_validation": "validation",
            "closeout": "closeout",
        }[step]

    @staticmethod
    def _workflow_role_for_step(step):
        return {
            "planner_draft": "planner",
            "planner_advisory": "designer",
            "planner_finalize": "planner",
            "architect_guidance": "architect",
            "developer_build": "developer",
            "architect_review": "architect",
            "developer_revision": "developer",
            "qa_validation": "qa",
            "closeout": "version_controller",
        }[step]

    def _make_workflow_request_record(
        self,
        *,
        step,
        phase=None,
        current_role=None,
        planning_pass_count=0,
        planning_pass_limit=2,
        review_cycle_count=0,
        review_cycle_limit=3,
        reopen_source_role="",
        reopen_category="",
    ):
        resolved_phase = phase or self._workflow_phase_for_step(step)
        resolved_role = current_role or self._workflow_role_for_step(step)
        return {
            "request_id": f"20260413-{step}-{resolved_role}",
            "status": "delegated",
            "intent": "implement",
            "urgency": "normal",
            "scope": f"workflow transition {step}",
            "body": f"workflow transition {step}",
            "artifacts": [],
            "params": {
                "_teams_kind": "sprint_internal",
                "workflow": {
                    "contract_version": 1,
                    "phase": resolved_phase,
                    "step": step,
                    "phase_owner": resolved_role,
                    "phase_status": "active",
                    "planning_pass_count": planning_pass_count,
                    "planning_pass_limit": planning_pass_limit,
                    "planning_final_owner": "planner",
                    "reopen_source_role": reopen_source_role,
                    "reopen_category": reopen_category,
                    "review_cycle_count": review_cycle_count,
                    "review_cycle_limit": review_cycle_limit,
                },
            },
            "current_role": resolved_role,
            "next_role": resolved_role,
            "owner_role": "orchestrator",
            "sprint_id": "2026-Sprint-Workflow-Matrix",
            "backlog_id": "backlog-1",
            "todo_id": "todo-1",
            "created_at": "2026-04-13T00:00:00+00:00",
            "updated_at": "2026-04-13T00:00:00+00:00",
            "fingerprint": f"workflow-{step}-{resolved_role}",
            "reply_route": {},
            "events": [],
            "result": {},
        }

    def _make_workflow_result(
        self,
        *,
        role,
        summary,
        outcome,
        target_phase="",
        target_step="",
        requested_role="",
        reopen_category="",
        finalize_phase=False,
        status="completed",
        artifacts=None,
        extra_proposals=None,
    ):
        proposals = {
            "workflow_transition": {
                "outcome": outcome,
                "target_phase": target_phase,
                "target_step": target_step,
                "requested_role": requested_role,
                "reopen_category": reopen_category,
                "reason": summary,
                "unresolved_items": [],
                "finalize_phase": finalize_phase,
            }
        }
        if extra_proposals:
            proposals.update(extra_proposals)
        return {
            "request_id": f"result-{role}",
            "role": role,
            "status": status,
            "summary": summary,
            "insights": [],
            "proposals": proposals,
            "artifacts": list(artifacts or []),
            "next_role": "",
            "approval_needed": False,
            "error": "",
        }

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
                self.assertNotIn("[teams_runtime relay_summary]", content)
                self.assertNotIn("relay_id", content)
                self.assertIn("- What: delegate payload", content)
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
                    content=f"{INTERNAL_RELAY_SUMMARY_MARKER} orchestrator -> planner (delegate)\n- request_id: N/A\n- What: planner summary message",
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
                self.assertNotIn("[teams_runtime relay_summary]", content)
                self.assertNotIn("relay_id", content)
                self.assertIn("- Why now: 다음 역할: qa", content)
                self.assertIn("- What: 첫 번째 요약 줄입니다. 두 번째 요약 줄입니다. 세 번째 요약 줄입니다.", content)
                self.assertIn("- Check now:", content)
                self.assertIn("backlog 후보 2건", content)
                self.assertIn("첫 번째 요약 줄입니다.", content)
                self.assertNotIn("- 상태:", content)
                self.assertNotIn("- 아티팩트:", content)
                self.assertNotIn("- 인사이트:", content)
                self.assertNotIn("- Context:", content)
                self.assertNotIn("previous role:", content)
                self.assertNotIn("latest summary:", content)
                self.assertIn("- Refs:", content)
                self.assertIn("workspace/a.py", content)
                self.assertIn("workspace/b.py", content)
                self.assertLess(content.index("- Why now: 다음 역할: qa"), content.index("- What: 첫 번째 요약 줄입니다. 두 번째 요약 줄입니다. 세 번째 요약 줄입니다."))

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

                self.assertIn("- What: evaluation order: program trade acceleration을 먼저 계산한다.", content)
                self.assertIn("- Check now:", content)
                self.assertIn("state transitions: candidate -> triggered는 broker concentration과 market gate가 모두 통과해야 한다.", content)
                self.assertIn("state transitions: watch -> candidate는 acceleration 임계치 충족 시에만 허용한다.", content)
                self.assertNotIn("기술 계약으로 구체화했습니다.", content)
                self.assertIn("상태 전이 테스트를 watch/candidate/triggered/suppressed별로 추가한다.", content)
                self.assertIn("- Why now: 다음 역할: developer", content)
                self.assertLess(
                    content.index("- Why now: 다음 역할: developer"),
                    content.index("- What: evaluation order: program trade acceleration을 먼저 계산한다."),
                )
                self.assertIn("- Refs:", content)
                self.assertIn("workspace/strategy_3.md", content)
                self.assertNotIn("- Context:", content)

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
                self.assertIn("- What: scope-only fallback message", content)
                self.assertNotIn("- 상태:", content)

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

                self.assertIn("- What: 핵심 구현 범위: concrete action", content)
                self.assertNotIn("- What: 정리했습니다.", content)

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

                self.assertIn("- What: 실패한 구현 결과를 정리했습니다.", content)
                self.assertIn("- Why now: 다음 역할: developer", content)
                self.assertIn("- 상태: failed", content)
                self.assertIn("- 오류:", content)
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

                self.assertIn("- What: 마일스톤을 workflow refined로 정리하고 backlog/todo 2건을 확정했습니다.", content)
                self.assertIn("- Check now:", content)
                self.assertIn("backlog/todo: manual sprint start gate", content)
                self.assertIn("backlog/todo: sprint folder artifact rendering", content)
                self.assertIn("- Constraints:", content)
                self.assertIn("완료 기준: planning을 닫을 수 있게 2건 확보", content)
                self.assertIn("- Why now: 다음 역할: designer", content)
                self.assertLess(
                    content.index("- Why now: 다음 역할: designer"),
                    content.index("- What: 마일스톤을 workflow refined로 정리하고 backlog/todo 2건을 확정했습니다."),
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

                self.assertIn("- What: info prioritization 관점 UX 판단 1건을 정리했습니다.", content)
                self.assertIn("- Check now:", content)
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
                self.assertIn("model: gpt-5.4 | reasoning: medium", service.discord_client.sent_channels[0][1])

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
                self.assertIn("| 상세", content)
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
                combined_report = "\n".join(report_contents)
                self.assertTrue(any("[작업 보고]" in content for content in startup_contents))
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

    def test_announce_startup_records_failure_and_sends_fallback_notice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FailingStartupDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                with patch.object(
                    service,
                    "_iter_startup_fallback_targets",
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
                self.assertIn("📌 sprint_id=260324-Sprint-09:00", content)
                self.assertNotIn("sprint_series_id", content)
                self.assertIn("📝 kickoff_items:", content)
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
                    requested_role="",
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
                policy_text.replace("sprint_initial_default: planner", "sprint_initial_default: architect", 1),
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
                    requested_role="",
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
                    requested_role="",
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
                    requested_role="",
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
                    requested_role="",
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
                                "requested_role": "",
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
                            "requested_role": "planner",
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
                                "requested_role": "",
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
                            "requested_role": "planner",
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
                self.assertEqual(updated["routing_context"]["requested_role"], "")
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
                self.assertEqual(updated["routing_context"]["requested_role"], "")
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
                self.assertIn("- Why this role:", relay_content)
                self.assertIn("- Context:", relay_content)
                self.assertIn("다음 단계는 designer보다 구현 역할이 더 적합합니다.", relay_content)
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
                self.assertEqual(updated["routing_context"]["requested_role"], "")
                self.assertEqual(updated["routing_context"]["request_state_class"], "execution_opened")
                self.assertGreater(updated["routing_context"]["score_total"], 0)
                _channel_id, relay_content = service.discord_client.sent_channels[0]
                self.assertIn(f"<@{developer_bot_id}>", relay_content)
                self.assertIn("- Why this role:", relay_content)
                self.assertIn("planning은 끝났고 다음 단계는 실제 구현입니다.", relay_content)
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
                            "artifacts": [],
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
                self.assertEqual(updated["routing_context"]["requested_role"], "")
                self.assertGreater(updated["routing_context"]["score_total"], 0)
                _channel_id, relay_content = service.discord_client.sent_channels[0]
                self.assertIn(f"<@{planner_bot_id}>", relay_content)
                self.assertIn("- Why this role:", relay_content)
                self.assertIn("planner가 scope와 acceptance criteria를 다시 정리해야 합니다.", relay_content)

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
                    sender="user",
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
                    "current_role": "developer",
                    "next_role": "developer",
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

                with patch.object(orchestration_module.LOGGER, "warning") as warning_mock:
                    asyncio.run(service._reply_to_requester(request_record, "status update"))

                warning_mock.assert_called_once()
                self.assertIn("channel_id is missing", warning_mock.call_args.args[0])
                self.assertIn("route_source", warning_mock.call_args.args[0])

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
                self.assertIn("- What: 버튼 레이블과 안내 문구를 함께 조정해줘", content)
                self.assertIn("- Why this role: 다음 역할: designer", content)
                self.assertIn("- Constraints:", content)
                self.assertIn("추가 입력: 현재 문구 목록", content)
                self.assertIn("완료 기준: 버튼 레이블이 역할에 맞게 정리된다.", content)
                self.assertIn("- Refs:", content)
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

                self.assertIn("- Constraints:", body)
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
                self.assertIn("- Stage: planning / planner_advisory", content)
                self.assertIn("- Refs:", content)
                self.assertNotIn("- Why this role:", content)
                self.assertNotIn("- Check now:", content)
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
                self.assertIn("- What: designer support role planning", content)
                self.assertIn("- Stage: planning / planner_finalize", content)
                self.assertIn("- Refs:", content)
                self.assertNotIn("- Why this role:", content)
                self.assertNotIn("- Check now:", content)
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
                self.assertIn("- What: workflow initial", content)
                self.assertIn("마일스톤: workflow refined", content)
                self.assertIn("backlog/todo: manual sprint start gate", content)
                self.assertIn("backlog/todo: sprint folder artifact rendering", content)
                self.assertIn("- Why this role: 다음 역할: designer", content)
                self.assertIn("- Check now:", content)
                self.assertIn("  - backlog/todo: manual sprint start gate", content)
                self.assertIn("  - backlog/todo: sprint folder artifact rendering", content)
                self.assertIn("- Check now:", content)

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
                self.assertIn("- What: 새 backlog 항목 정리", content)
                self.assertIn("- Why this role: planner 역할이 현재 단계의 다음 담당입니다.", content)
                self.assertIn("- Refs:", content)
                self.assertNotIn("- Context:", content)
                snapshot_file = service.paths.role_request_snapshot_file("planner", "20260325-firsthop1")
                self.assertTrue(snapshot_file.exists())
                snapshot_text = snapshot_file.read_text(encoding="utf-8")
                self.assertIn("what_summary: N/A", snapshot_text)
                self.assertIn("latest_context: N/A", snapshot_text)
                self.assertIn("Always trust `.teams_runtime/requests/20260325-firsthop1.json`", snapshot_text)

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
                self.assertEqual(workflow["step"], "planner_draft")
                self.assertEqual(workflow["phase_owner"], "planner")
                self.assertEqual(workflow["planning_pass_limit"], 2)
                self.assertEqual(workflow["planning_pass_count"], 0)
                self.assertEqual(workflow["review_cycle_limit"], 3)

    def test_workflow_transition_matrix_routes_expected_next_role_and_step(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                cases = [
                    {
                        "name": "planner_draft_opens_designer_advisory",
                        "request_step": "planner_draft",
                        "result": self._make_workflow_result(
                            role="planner",
                            summary="designer advisory가 필요합니다.",
                            outcome="continue",
                            target_phase="planning",
                            target_step="planner_advisory",
                            requested_role="designer",
                        ),
                        "expected_next_role": "designer",
                        "expected_phase": "planning",
                        "expected_step": "planner_advisory",
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
                            requested_role="qa",
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
                            target_step="planner_advisory",
                            reopen_category="ux",
                        ),
                        "expected_next_role": "designer",
                        "expected_phase": "planning",
                        "expected_step": "planner_advisory",
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
                                    "requested_role": "",
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
                                            "target_step": "planner_advisory",
                                            "requested_role": "designer",
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
                self.assertEqual(updated["params"]["workflow"]["step"], "planner_advisory")
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
                                            "requested_role": "",
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
                                    "requested_role": "",
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
                                    "requested_role": "",
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
                                            "requested_role": "",
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
                                            "requested_role": "",
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
                                            "requested_role": "qa",
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
                                            "target_step": "planner_advisory",
                                            "requested_role": "",
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
                self.assertEqual(updated["params"]["workflow"]["step"], "planner_advisory")
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
                                                "requested_role": "",
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
                                                "requested_role": "",
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
                                            "requested_role": "",
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
                                            "requested_role": "",
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
                                            "requested_role": "",
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
                                    "target_step": "planner_advisory",
                                    "requested_role": "architect",
                                    "reopen_category": "",
                                    "reason": "추가 technical advisory가 필요합니다.",
                                    "unresolved_items": ["technical detail"],
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
                            "artifacts": [],
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
                            }
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
                            "artifacts": [],
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

                async def fake_delegate(request_record, next_role):
                    workflow = dict(request_record.get("params", {}).get("workflow") or {})
                    workflow_step = str(workflow.get("step") or "")
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
                    else:
                        raise AssertionError(f"unexpected delegation: {next_role=} {workflow_step=}")
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
                    if next_role != "planner":
                        raise AssertionError(f"unexpected delegation: {next_role=}")
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
                    await service._handle_role_report(
                        DiscordMessage(
                            message_id="relay-planner-doc-only",
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
                    return True

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
                            "requested_role": "",
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
                                            "requested_role": "",
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
                            "requested_role": "",
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
                            "requested_role": "architect",
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
                            "requested_role": "",
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
                            "requested_role": "",
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

                async def fake_delegate(request_record, next_role):
                    workflow = dict(request_record.get("params", {}).get("workflow") or {})
                    workflow_step = str(workflow.get("step") or "")
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
                    elif next_role == "architect" and workflow_step == "architect_review":
                        architect_review_attempts["count"] += 1
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
                    else:
                        raise AssertionError(f"unexpected delegation: {next_role=} {workflow_step=}")
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

                async def fake_delegate(request_record, next_role):
                    if next_role == "planner":
                        result = {
                            "request_id": request_record["request_id"],
                            "role": "planner",
                            "status": "completed",
                            "summary": "계획을 세웠고 다음으로 실제 구현을 이어가야 합니다.",
                            "insights": [],
                            "proposals": {},
                            "artifacts": [],
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
                ):
                    asyncio.run(service._run_autonomous_sprint("backlog_ready"))

                sprint_state = json.loads(
                    service.paths.sprint_file("2026-Sprint-01-20260324T000100Z").read_text(encoding="utf-8")
                )
                scheduler_state = service._load_scheduler_state()
                self.assertEqual(sprint_state["status"], "completed")
                self.assertEqual(scheduler_state["active_sprint_id"], "")
                self.assertIn("✅ 스프린트 완료", "\n".join(content for _channel_id, content in service.discord_client.sent_channels))

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
                    ["manual sprint start gate", "sprint folder artifact rendering"],
                )
                self.assertEqual(
                    {item["milestone_title"] for item in sprint_state["selected_items"]},
                    {"workflow refined"},
                )
                self.assertEqual(
                    [todo["priority_rank"] for todo in sprint_state["todos"][:2]],
                    [2, 1],
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

                with patch("teams_runtime.core.orchestration.inspect_sprint_closeout") as inspect_mock:
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
                self.assertIn("- [blocked] KIS 활용률 검증 및 fallback 축소 기준 정리 | request_id=req-live-blocked", next_action_section)
                self.assertIn("- [queued] 데일리 요약 카드 재배치 | request_id=req-live-queued", next_action_section)
                self.assertNotIn("완료된 기록 보강", next_action_section)
                self.assertLess(
                    next_action_section.index("- [running] 실시간 상태 반영 순서 재정리 | request_id=req-live-running"),
                    next_action_section.index("- [blocked] KIS 활용률 검증 및 fallback 축소 기준 정리 | request_id=req-live-blocked"),
                )
                self.assertLess(
                    next_action_section.index("- [blocked] KIS 활용률 검증 및 fallback 축소 기준 정리 | request_id=req-live-blocked"),
                    next_action_section.index("- [queued] 데일리 요약 카드 재배치 | request_id=req-live-queued"),
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
                            "workspace/libs/gemini/workflows/intraday/signals/pullback_breakout/entry_signal_policy_v2.py",
                            "libs/gemini/workflows/intraday/realtime.py",
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
                            "tests/test_intraday_trader_mode.py",
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
                            "workspace/libs/gemini/workflows/intraday/signals/pullback_breakout/entry_signal_policy_v2.py",
                            "libs/gemini/workflows/intraday/realtime.py",
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
                                "workspace/libs/gemini/workflows/intraday/signals/pullback_breakout/entry_signal_policy_v2.py",
                                "libs/gemini/workflows/intraday/realtime.py",
                            ],
                            "next_role": "",
                            "error": "",
                        },
                        "version_control_status": "committed",
                        "version_control_sha": "kimdanta-entry-1",
                        "version_control_paths": [
                            "workspace/libs/gemini/workflows/intraday/signals/pullback_breakout/entry_signal_policy_v2.py",
                            "libs/gemini/workflows/intraday/realtime.py",
                        ],
                        "version_control_message": "entry_signal_policy_v2.py: tighten kimdanta re-entry rule",
                        "version_control_error": "",
                        "task_commit_status": "committed",
                        "task_commit_sha": "kimdanta-entry-1",
                        "task_commit_paths": [
                            "workspace/libs/gemini/workflows/intraday/signals/pullback_breakout/entry_signal_policy_v2.py",
                            "libs/gemini/workflows/intraday/realtime.py",
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
                            "tests/test_intraday_trader_mode.py",
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
                                "tests/test_intraday_trader_mode.py",
                            ],
                            "next_role": "",
                            "error": "",
                        },
                        "version_control_status": "committed",
                        "version_control_sha": "kimdanta-report-2",
                        "version_control_paths": [
                            "workspace/libs/kis/domestic_stock_ws.py",
                            "tests/test_intraday_trader_mode.py",
                        ],
                        "version_control_message": "domestic_stock_ws.py: explain kimdanta report reasoning",
                        "version_control_error": "",
                        "task_commit_status": "committed",
                        "task_commit_sha": "kimdanta-report-2",
                        "task_commit_paths": [
                            "workspace/libs/kis/domestic_stock_ws.py",
                            "tests/test_intraday_trader_mode.py",
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
                    "  - 구현 근거 아티팩트: libs/gemini/workflows/intraday/signals/pullback_breakout/entry_signal_policy_v2.py, libs/gemini/workflows/intraday/realtime.py",
                    report_body,
                )
                self.assertIn("  - 작업 범위: 김단타 진입 판단 기준 조정", report_body)
                self.assertIn(
                    "  - 참고 아티팩트: libs/gemini/workflows/intraday/signals/pullback_breakout/entry_signal_policy_v2.py, libs/gemini/workflows/intraday/realtime.py",
                    report_body,
                )
                self.assertNotIn("- 어떻게: 핵심 로직은", report_body)
                self.assertIn("- 개발자 (developer): todo 2건, 완료 2건.", report_body)
                self.assertIn(
                    "  - 근거 하이라이트: 김단타 진입 기준 재구성, 김단타 보고 근거 재구성 작업을 담당했습니다.",
                    report_body,
                )
                self.assertIn(
                    "  - 참고 산출물: libs/gemini/workflows/intraday/signals/pullback_breakout/entry_signal_policy_v2.py, libs/gemini/workflows/intraday/realtime.py, libs/kis/domestic_stock_ws.py, tests/test_intraday_trader_mode.py",
                    report_body,
                )
                self.assertIn("## 참고 아티팩트", report_body)
                self.assertIn(
                    "- 참고: [committed] 김단타 진입 기준 재구성 -> libs/gemini/workflows/intraday/signals/pullback_breakout/entry_signal_policy_v2.py",
                    report_body,
                )
                self.assertIn(
                    "libs/gemini/workflows/intraday/signals/pullback_breakout/entry_signal_policy_v2.py",
                    report_body,
                )
                self.assertIn(
                    "artifact=libs/gemini/workflows/intraday/signals/pullback_breakout/entry_signal_policy_v2.py",
                    report_body,
                )
                self.assertIn("artifact=libs/gemini/workflows/intraday/realtime.py", report_body)
                self.assertIn("artifact=libs/kis/domestic_stock_ws.py", report_body)
                self.assertIn("artifact=tests/test_intraday_trader_mode.py", report_body)
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
                self.assertIn("| 우선순위/확정", report)
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
                        "artifacts": [],
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


class TestInternalRelaySurfaceContract(unittest.TestCase):
    def test_internal_relay_summary_changes_do_not_mutate_envelope_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator", relay_transport="internal")
            envelope = MessageEnvelope(
                request_id="20260411-contract-stable-1",
                sender="architect",
                target="developer",
                intent="implement",
                urgency="normal",
                scope="contract stability check",
                body=json.dumps({"summary": "implementation detail only", "status": "completed"}, ensure_ascii=False),
                params={"_teams_kind": "report", "result": {"status": "completed", "summary": "surface only"}},
            )

            expected_envelope_dict = envelope.to_dict(include_routing=True)
            sent = asyncio.run(service._send_relay(envelope))
            self.assertTrue(sent)

            inbox_dir = service.paths.runtime_root / "internal_relay" / "inbox" / "developer"
            relay_files = sorted(inbox_dir.glob("*.json"))
            self.assertEqual(len(relay_files), 1)
            payload = json.loads(relay_files[0].read_text(encoding="utf-8"))

            self.assertEqual(set(payload.keys()), {"relay_id", "transport", "created_at", "sender_role", "target_role", "kind", "envelope"})
            self.assertEqual(payload.get("transport"), RELAY_TRANSPORT_INTERNAL)
            self.assertEqual(payload.get("kind"), "report")
            self.assertIn("relay_id", payload)
            self.assertEqual(payload.get("target_role"), "developer")

            envelope_payload = payload.get("envelope")
            self.assertIsInstance(envelope_payload, dict)
            self.assertEqual(envelope_payload, expected_envelope_dict)
            roundtrip_envelope = service._deserialize_internal_relay_envelope(envelope_payload)
            self.assertIsNotNone(roundtrip_envelope)
            self.assertEqual(roundtrip_envelope.request_id, envelope.request_id)
            self.assertEqual(roundtrip_envelope.sender, envelope.sender)
            self.assertEqual(roundtrip_envelope.target, envelope.target)
            self.assertEqual(roundtrip_envelope.intent, envelope.intent)
            self.assertEqual(roundtrip_envelope.urgency, envelope.urgency)
            self.assertEqual(roundtrip_envelope.scope, envelope.scope)
            self.assertEqual(roundtrip_envelope.body, envelope.body)
            self.assertEqual(roundtrip_envelope.params.get("_teams_kind"), envelope.params.get("_teams_kind"))

    def test_internal_relay_marker_ignore_contract_survives_surface_renders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "developer")
                envelope = MessageEnvelope(
                    request_id="20260411-contract-marker-1",
                    sender="architect",
                    target="developer",
                    intent="implement",
                    urgency="normal",
                    scope="marker contract stability check",
                    body=json.dumps({"summary": "surface change contract"}, ensure_ascii=False),
                    params={"_teams_kind": "report"},
                )
                summary_content = service._build_internal_relay_summary_message(envelope)
                message = DiscordMessage(
                    message_id="relay-marker-1",
                    channel_id="111111111111111111",
                    guild_id="guild-1",
                    author_id="111111111111111112",
                    author_name="orchestrator",
                    content=summary_content,
                    is_dm=False,
                    mentions_bot=False,
                    created_at=datetime.now(timezone.utc),
                )

                asyncio.run(service.handle_message(message))

                self.assertEqual(service.discord_client.sent_channels, [])
                self.assertEqual(service.discord_client.sent_dms, [])

    def test_delegate_snapshot_contract_fields_remain_in_snapshot_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
            request_record = {
                "request_id": "20260411-contract-snapshot-1",
                "status": "delegated",
                "intent": "route",
                "urgency": "normal",
                "scope": "surface-only delegation contract",
                "body": "surface-only delegation contract",
                "artifacts": ["shared_workspace/sprints/260411-Sprint-15-06/spec.md", "workspace/a.py"],
                "params": {
                    "_teams_kind": "sprint_internal",
                    "workflow": {
                        "contract_version": 1,
                        "phase": "implementation",
                        "step": "developer_build",
                    },
                },
                "current_role": "planner",
                "next_role": "planner",
                "owner_role": "orchestrator",
                "created_at": "2026-04-11T00:00:00+09:00",
                "updated_at": "2026-04-11T00:00:00+09:00",
                "fingerprint": "contract-snapshot-1",
                "reply_route": {},
                "events": [],
                "result": {
                    "request_id": "20260411-contract-snapshot-1",
                    "role": "planner",
                    "status": "completed",
                    "summary": "snapshot contract를 보존해야 합니다.",
                    "insights": ["surface changes only"],
                    "proposals": {
                        "implementation_guidance": {
                            "route_reason": "delegate body and summary body 분리",
                        },
                        "focus_points": ["backlog/todo: 신규 todo 확인", "required input: 명세 경계 확인"],
                        "reference_artifacts": ["workspace/a.py", "workspace/b.py"],
                    },
                },
            }

            delegation_context = service._build_delegation_context(request_record, "planner")
            snapshot_path = service._write_role_request_snapshot("planner", request_record, delegation_context)
            self.assertTrue(snapshot_path)
            snapshot_file = service.paths.role_request_snapshot_file("planner", request_record["request_id"])
            snapshot_content = snapshot_file.read_text(encoding="utf-8")
            self.assertIn("- canonical_request:", snapshot_content)
            self.assertIn("- previous_role:", snapshot_content)
            self.assertIn("- what_summary:", snapshot_content)
            self.assertIn("- reference_artifacts:", snapshot_content)

            body = service._build_delegate_body(request_record, delegation_context)
            self.assertIn("- Refs:", body)
            self.assertIn("- request:", body)
            self.assertIn("- artifacts:", body)
            self.assertIn("- note: request record가 relay보다 우선합니다.", body)
            self.assertIn("- Why this role:", body)

    def test_internal_relay_surface_renderers_preserve_contract(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator", relay_transport="internal")
                envelope = MessageEnvelope(
                    request_id="20260411-surface-contract-1",
                    sender="planner",
                    target="developer",
                    intent="implement",
                    urgency="normal",
                    scope="surface contract isolation",
                    body=json.dumps(
                        {
                            "summary": "surface-only change should not alter internal relay payload",
                            "proposals": {"implementation_guidance": {"route_reason": "contract guard"}},
                        },
                        ensure_ascii=False,
                    ),
                    params={"_teams_kind": "report", "result": {"status": "completed"}},
                )
                request_record = {
                    "request_id": "20260411-surface-contract-2",
                    "status": "completed",
                    "intent": "route",
                    "urgency": "normal",
                    "scope": "surface-only delegation contract",
                    "body": "surface-only delegation contract",
                    "artifacts": [],
                    "params": {
                        "_teams_kind": "sprint_internal",
                        "workflow": {"phase": "implementation", "step": "developer_build"},
                    },
                    "current_role": "planner",
                    "next_role": "planner",
                    "owner_role": "orchestrator",
                    "created_at": "2026-04-11T00:00:00+09:00",
                    "updated_at": "2026-04-11T00:00:00+09:00",
                    "fingerprint": "surface-contract-2",
                    "reply_route": {},
                    "events": [],
                    "result": {
                        "status": "completed",
                        "summary": "surface payload contracts are kept",
                        "proposals": {"implementation_guidance": {"route_reason": "contract isolation"}},
                    },
                }

                envelope_before = envelope.to_dict(include_routing=True)

                summary_content = service._build_internal_relay_summary_message(envelope)
                delegation_context = service._build_delegation_context(request_record, "developer")
                _ = service._build_delegate_body(request_record, delegation_context)
                envelope_after = envelope.to_dict(include_routing=True)

                self.assertTrue(summary_content.startswith(f"{INTERNAL_RELAY_SUMMARY_MARKER} planner -> developer (report)"))
                self.assertEqual(envelope_before, envelope_after)
                self.assertIn("- request_id:", summary_content)
                synthetic = service._build_internal_relay_message_stub(envelope, relay_id="relay-contract-2")
                self.assertEqual(synthetic.content, envelope_to_text(envelope))

                summary_dispatched = asyncio.run(service._send_relay(envelope))
                self.assertTrue(summary_dispatched)
                relay_file = next(
                    (service.paths.runtime_root / "internal_relay" / "inbox" / "developer").glob("*.json")
                )
                relay_payload = json.loads(relay_file.read_text(encoding="utf-8"))
                self.assertEqual(relay_payload.get("envelope"), envelope_before)
