from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from teams_runtime.core.config import load_discord_agents_config, load_team_runtime_config
from teams_runtime.core.notifications import DiscordNotificationService
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.discord.client import DiscordMessage, DiscordSendError
from teams_runtime.workflows.orchestration.notifications import (
    append_markdown_entry,
    announce_startup_notification,
    build_requester_status_message,
    record_shared_role_result,
    reply_to_requester,
    refresh_role_todos,
    send_channel_reply,
    send_discord_content,
    send_immediate_receipt,
    simplify_requester_summary,
)
from teams_runtime.workflows.state.request_store import save_request


class DummyNotificationService:
    def __init__(self) -> None:
        self.send_requester_reply = AsyncMock()
        self.send_channel_reply = AsyncMock()
        self.send_immediate_receipt = AsyncMock()
        self.send_content = AsyncMock()
        self.build_startup_report = Mock(return_value="startup report")
        self.send_startup_failure_fallback = AsyncMock(return_value="")


class TeamsRuntimeOrchestrationNotificationsTests(unittest.TestCase):
    def _build_notification_service(self, workspace_root: str) -> DiscordNotificationService:
        paths = RuntimePaths.from_root(workspace_root)
        return DiscordNotificationService(
            paths=paths,
            role="orchestrator",
            discord_config=load_discord_agents_config(paths.workspace_root),
            runtime_config=load_team_runtime_config(paths.workspace_root),
            discord_client=object(),
        )

    def test_append_markdown_entry_creates_header_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = RuntimePaths.from_root(tmpdir).workspace_root / "notes.md"

            append_markdown_entry(path, "# Notes", "First", ["- hello"])
            append_markdown_entry(path, "# Notes", "Second", ["- again"])

            self.assertEqual(
                path.read_text(encoding="utf-8"),
                "# Notes\n\n## First\n- hello\n\n## Second\n- again\n",
            )

    def test_refresh_role_todos_writes_open_requests_for_role_and_orchestrator(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            save_request(
                paths,
                {
                    "request_id": "req-dev-1",
                    "status": "delegated",
                    "current_role": "developer",
                    "urgency": "normal",
                    "scope": "Build the thing",
                    "updated_at": "2026-04-22T00:00:00+00:00",
                },
                update_timestamp=False,
            )

            refresh_role_todos(paths)

            self.assertIn("req-dev-1", paths.role_todo_file("developer").read_text(encoding="utf-8"))
            self.assertIn("req-dev-1", paths.role_todo_file("orchestrator").read_text(encoding="utf-8"))
            self.assertIn("active request 없음", paths.role_todo_file("qa").read_text(encoding="utf-8"))

    def test_record_shared_role_result_writes_primary_and_history_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)

            record_shared_role_result(
                paths,
                {"request_id": "req-plan-1", "scope": "Plan the thing"},
                {
                    "role": "planner",
                    "status": "completed",
                    "summary": "Plan drafted",
                    "artifacts": ["shared_workspace/planning.md"],
                },
            )

            self.assertIn("req-plan-1", paths.shared_planning_file.read_text(encoding="utf-8"))
            self.assertIn("req-plan-1", paths.shared_history_file.read_text(encoding="utf-8"))

    def test_simplify_requester_summary_rewrites_sprint_summary_block(self) -> None:
        simplified = simplify_requester_summary(
            "## Sprint Summary\n"
            "sprint_name=Alpha Sprint\n"
            "sprint_id=2026-Sprint-03\n"
            "milestone_title=Login workflow cleanup\n"
            "phase=implementation\n"
            "status=active\n"
            "todo_summary=2 selected\n"
        )

        self.assertEqual(
            simplified,
            "\n".join(
                [
                    "현재 스프린트 상태입니다.",
                    "스프린트: Alpha Sprint",
                    "스프린트 ID: 2026-Sprint-03",
                    "마일스톤: Login workflow cleanup",
                    "단계: implementation",
                    "상태: active",
                    "작업 요약: 2 selected",
                ]
            ),
        )

    def test_build_requester_status_message_uses_simplified_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            notification_service = self._build_notification_service(tmpdir)

            message_text = build_requester_status_message(
                notification_service,
                status="delegated",
                request_id="req-status-2",
                summary=(
                    "## Sprint Summary\n"
                    "sprint_name=Alpha Sprint\n"
                    "sprint_id=2026-Sprint-03\n"
                    "milestone_title=Login workflow cleanup\n"
                ),
                related_request_ids=[],
            )

            self.assertIn("진행 중", message_text)
            self.assertIn("- 현재 상태: 현재 스프린트 상태입니다.", message_text)
            self.assertIn("- 스프린트: Alpha Sprint", message_text)
            self.assertIn("- 요청 ID: req-status-2", message_text)
            self.assertLess(
                message_text.index("- 다음: 현재 상태를 확인한 뒤 추가 응답을 기다립니다."),
                message_text.index("- 요청 ID: req-status-2"),
            )

    def test_reply_to_requester_recovers_reply_route_and_saves_request(self) -> None:
        notification_service = DummyNotificationService()
        request_record = {
            "request_id": "20260420-reply-1",
            "current_role": "orchestrator",
            "reply_route": {},
            "params": {
                "original_requester": {
                    "author_id": "user-1",
                    "author_name": "tester",
                    "channel_id": "channel-1",
                    "guild_id": "guild-1",
                    "is_dm": False,
                    "message_id": "msg-1",
                }
            },
        }
        save_request = Mock()

        asyncio.run(
            reply_to_requester(
                notification_service,
                request_record,
                "status update",
                save_request=save_request,
            )
        )

        save_request.assert_called_once_with(request_record)
        self.assertEqual(request_record["reply_route"]["channel_id"], "channel-1")
        notification_service.send_requester_reply.assert_awaited_once()
        kwargs = notification_service.send_requester_reply.await_args.kwargs
        self.assertEqual(kwargs["route"]["channel_id"], "channel-1")
        self.assertEqual(kwargs["route_source"], "original_requester")
        self.assertEqual(kwargs["content"], "status update")
        self.assertEqual(kwargs["current_role"], "orchestrator")

    def test_send_channel_reply_delegates_to_notification_service(self) -> None:
        notification_service = DummyNotificationService()
        message = DiscordMessage(
            message_id="msg-1",
            channel_id="channel-1",
            guild_id="guild-1",
            author_id="user-1",
            author_name="tester",
            content="ping",
            is_dm=False,
            mentions_bot=True,
            created_at=datetime.now(timezone.utc),
        )

        asyncio.run(send_channel_reply(notification_service, message, "hello"))

        notification_service.send_channel_reply.assert_awaited_once_with(message, "hello")

    def test_send_immediate_receipt_skips_trusted_relay_messages(self) -> None:
        notification_service = DummyNotificationService()
        message = DiscordMessage(
            message_id="relay-summary-1",
            channel_id="relay-channel",
            guild_id="guild-1",
            author_id="bot-1",
            author_name="planner",
            content="relay summary",
            is_dm=False,
            mentions_bot=False,
            created_at=datetime.now(timezone.utc),
        )

        asyncio.run(
            send_immediate_receipt(
                notification_service,
                message,
                is_trusted_relay_message=lambda _message: True,
            )
        )

        notification_service.send_immediate_receipt.assert_not_awaited()

    def test_send_immediate_receipt_forwards_untrusted_messages(self) -> None:
        notification_service = DummyNotificationService()
        message = DiscordMessage(
            message_id="msg-2",
            channel_id="channel-2",
            guild_id="guild-1",
            author_id="user-2",
            author_name="tester",
            content="hello",
            is_dm=False,
            mentions_bot=True,
            created_at=datetime.now(timezone.utc),
        )

        asyncio.run(
            send_immediate_receipt(
                notification_service,
                message,
                is_trusted_relay_message=lambda _message: False,
            )
        )

        notification_service.send_immediate_receipt.assert_awaited_once_with(message)

    def test_send_discord_content_delegates_delivery_options(self) -> None:
        notification_service = DummyNotificationService()
        send = AsyncMock()

        asyncio.run(
            send_discord_content(
                notification_service,
                content="payload",
                send=send,
                target_description="channel:1",
                prefix="<@user-1> ",
                swallow_exceptions=True,
                log_traceback=False,
            )
        )

        notification_service.send_content.assert_awaited_once_with(
            content="payload",
            send=send,
            target_description="channel:1",
            prefix="<@user-1> ",
            swallow_exceptions=True,
            log_traceback=False,
        )

    def test_sprint_completion_user_report_sends_rich_payload_before_markdown_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report = Path(tmpdir) / "report.md"
            report.write_text("# Report\n", encoding="utf-8")
            rich_send = AsyncMock()
            discord_client = Mock(send_channel_rich_message=rich_send)
            service = DiscordNotificationService(
                paths=RuntimePaths.from_root(tmpdir),
                role="orchestrator",
                discord_config=Mock(),
                runtime_config=Mock(),
                discord_client=discord_client,
            )
            service.send_content = AsyncMock()

            sent = asyncio.run(
                service.send_sprint_completion_user_report(
                    report_channel_id="123",
                    sprint_id="sprint-1",
                    content="markdown report",
                    embed={"title": "done"},
                    report_file_path=str(report),
                )
            )

            self.assertTrue(sent)
            rich_send.assert_awaited_once_with(
                "123",
                content="",
                embed={"title": "done"},
                files=[str(report)],
                allowed_mentions="none",
            )
            service.send_content.assert_not_awaited()

    def test_sprint_completion_user_report_sends_multiple_embeds_with_single_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report = Path(tmpdir) / "report.md"
            report.write_text("# Report\n", encoding="utf-8")
            rich_send = AsyncMock()
            discord_client = Mock(send_channel_rich_message=rich_send)
            service = DiscordNotificationService(
                paths=RuntimePaths.from_root(tmpdir),
                role="orchestrator",
                discord_config=Mock(),
                runtime_config=Mock(),
                discord_client=discord_client,
            )
            service.send_content = AsyncMock()

            sent = asyncio.run(
                service.send_sprint_completion_user_report(
                    report_channel_id="123",
                    sprint_id="sprint-1",
                    content="markdown report",
                    embed=[{"title": "done 1"}, {"title": "done 2"}],
                    report_file_path=str(report),
                )
            )

            self.assertTrue(sent)
            self.assertEqual(rich_send.await_count, 2)
            first_call, second_call = rich_send.await_args_list
            self.assertEqual(first_call.kwargs["embed"], {"title": "done 1"})
            self.assertEqual(first_call.kwargs["files"], [str(report)])
            self.assertEqual(second_call.kwargs["embed"], {"title": "done 2"})
            self.assertEqual(second_call.kwargs["files"], [])
            service.send_content.assert_not_awaited()

    def test_sprint_completion_user_report_falls_back_to_markdown_chunks_on_rich_failure(self) -> None:
        discord_client = Mock(send_channel_rich_message=AsyncMock(side_effect=DiscordSendError("boom")))
        service = DiscordNotificationService(
            paths=RuntimePaths.from_root(tempfile.mkdtemp()),
            role="orchestrator",
            discord_config=Mock(),
            runtime_config=Mock(),
            discord_client=discord_client,
        )
        service.send_content = AsyncMock()

        sent = asyncio.run(
            service.send_sprint_completion_user_report(
                report_channel_id="123",
                sprint_id="sprint-1",
                content="markdown report",
                embed={"title": "done"},
                report_file_path="/missing/report.md",
            )
        )

        self.assertTrue(sent)
        service.send_content.assert_awaited_once()
        self.assertEqual(service.send_content.await_args.kwargs["content"], "markdown report")

    def test_announce_startup_notification_records_success(self) -> None:
        notification_service = DummyNotificationService()
        record_state = Mock()
        send_channel_message = AsyncMock()

        asyncio.run(
            announce_startup_notification(
                notification_service,
                role="orchestrator",
                identity={"name": "Orchestrator", "id": "bot-1"},
                active_sprint_id="2026-Sprint-03",
                startup_channel_id="startup-channel",
                send_channel_message=send_channel_message,
                record_startup_notification_state=record_state,
                log_warning=Mock(),
            )
        )

        notification_service.build_startup_report.assert_called_once_with(
            identity_name="Orchestrator",
            identity_id="bot-1",
            active_sprint_id="2026-Sprint-03",
        )
        notification_service.send_content.assert_awaited_once()
        self.assertEqual(notification_service.send_content.await_args.kwargs["target_description"], "startup:startup-channel")
        record_state.assert_called_once_with(
            status="sent",
            error="",
            attempted_channel="startup-channel",
            attempts=1,
            fallback_target="",
        )

    def test_announce_startup_notification_records_fallback_failure(self) -> None:
        notification_service = DummyNotificationService()
        notification_service.send_content = AsyncMock(
            side_effect=DiscordSendError("TimeoutError: timeout", attempts=3)
        )
        notification_service.send_startup_failure_fallback = AsyncMock(return_value="report:222")
        record_state = Mock()
        log_warning = Mock()

        asyncio.run(
            announce_startup_notification(
                notification_service,
                role="orchestrator",
                identity={},
                active_sprint_id="",
                startup_channel_id="startup-channel",
                send_channel_message=AsyncMock(),
                record_startup_notification_state=record_state,
                log_warning=log_warning,
            )
        )

        notification_service.build_startup_report.assert_called_once_with(
            identity_name="unknown",
            identity_id="unknown",
            active_sprint_id="",
        )
        notification_service.send_startup_failure_fallback.assert_awaited_once()
        record_state.assert_called_once_with(
            status="fallback_sent",
            error="TimeoutError: timeout",
            attempted_channel="startup-channel",
            attempts=3,
            fallback_target="report:222",
        )
        log_warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
