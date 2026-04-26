from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from types import SimpleNamespace

from teams_runtime.core.config import load_discord_agents_config, load_team_runtime_config
from teams_runtime.core.notifications import (
    DiscordNotificationService,
    build_sourcer_activity_report,
    build_sourcer_report_state_update,
    resolve_sourcer_report_client,
    should_suppress_sourcer_report_failure_log,
    summarize_boxed_report_excerpt,
)
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.discord.client import DiscordSendError


class _FakeDiscordClient:
    def __init__(self) -> None:
        self.sent_channels: list[tuple[str, str]] = []

    async def send_channel_message(self, channel_id: str, content: str):
        self.sent_channels.append((channel_id, content))
        return {"id": f"msg-{len(self.sent_channels)}"}


class _FailingDiscordClient:
    async def send_channel_message(self, channel_id: str, content: str):
        raise RuntimeError("temporary relay send failure")


class TeamsRuntimeNotificationsTests(unittest.TestCase):
    def _build_notification_service(self, tmpdir: str, *, client) -> DiscordNotificationService:
        paths = RuntimePaths.from_root(tmpdir)
        return DiscordNotificationService(
            paths=paths,
            role="orchestrator",
            discord_config=load_discord_agents_config(tmpdir),
            runtime_config=load_team_runtime_config(tmpdir),
            discord_client=client,
        )

    def test_send_relay_envelope_prefixes_target_bot_mention(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            client = _FakeDiscordClient()
            service = self._build_notification_service(tmpdir, client=client)

            asyncio.run(
                service.send_relay_envelope(
                    relay_channel_id="111111111111111111",
                    target_bot_id="111111111111111116",
                    content="request_id: relay-1\nscope: Implement the task",
                )
            )

            self.assertEqual(len(client.sent_channels), 1)
            channel_id, content = client.sent_channels[0]
            self.assertEqual(channel_id, "111111111111111111")
            self.assertIn("<@111111111111111116>", content)
            self.assertIn("request_id: relay-1", content)

    def test_build_startup_report_includes_identity_and_channels(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            service = self._build_notification_service(tmpdir, client=_FakeDiscordClient())

            report = service.build_startup_report(
                identity_name="orchestrator-bot",
                identity_id="bot-123",
                active_sprint_id="260419-Sprint-21:00",
            )

            self.assertIn("[준비 완료] ✅ orchestrator", report)
            self.assertIn("orchestrator-bot (bot-123)", report)
            self.assertIn("expected_bot_id", report)
            self.assertIn("260419-Sprint-21:00", report)
            self.assertIn(str(service.discord_config.startup_channel_id), report)
            self.assertIn(str(service.discord_config.relay_channel_id), report)

    def test_summarize_boxed_report_excerpt_skips_fenced_section_headers(self):
        excerpt = summarize_boxed_report_excerpt(
            "```text\n[한눈에 보기]\n- TL;DR: summary\n- sprint_id: sprint-1\n```\n\n```text\n[다음 액션]\n- 없음\n```"
        )

        self.assertEqual(excerpt, "- TL;DR: summary\n- sprint_id: sprint-1\n- 없음")

    def test_build_startup_fallback_report_uses_summarized_report_excerpt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            service = self._build_notification_service(tmpdir, client=_FakeDiscordClient())

            report = service.build_startup_fallback_report(
                report="```text\n[한눈에 보기]\n- TL;DR: summary\n- sprint_id: sprint-1\n```",
                error=DiscordSendError("startup send failed", attempts=2, phase="send"),
                fallback_target="report:333333333333333333",
            )

            self.assertIn("- TL;DR: summary", report)
            self.assertIn("attempts=2", report)
            self.assertIn("report:333333333333333333", report)

    def test_build_sourcer_activity_report_includes_metrics_and_milestone_filter(self):
        report = build_sourcer_activity_report(
            sourcing_activity={
                "status": "completed",
                "summary": "runtime findings were converted into backlog candidates.",
                "findings_count": 3,
                "candidate_count": 1,
                "elapsed_ms": 245,
                "raw_backlog_items_count": 4,
                "filtered_candidate_count": 1,
                "active_sprint_milestone": "workflow initial",
                "milestone_filtered_out_count": 3,
            },
            added=1,
            updated=0,
            candidates=[{"title": "developer log failure handling"}],
        )

        self.assertIn("[작업 보고]", report)
        self.assertIn("Backlog Sourcing", report)
        self.assertIn("finding 3건, raw 4건, 후보 1건, 신규 1건, 갱신 0건, 245ms", report)
        self.assertIn("workflow initial", report)
        self.assertIn("developer log failure handling", report)

    def test_build_sourcer_report_state_update_tracks_failure_and_success(self):
        failed_normalized, failed_state, reset = build_sourcer_report_state_update(
            agent_state={},
            status="failed",
            client_label="internal_sourcer",
            reason="timeout",
            category="discord_timeout",
            recovery_action="retry later",
            error="TimeoutError",
            attempts=3,
            channel_id="123",
            updated_at="2026-04-21T01:00:00Z",
        )

        self.assertFalse(reset)
        self.assertEqual(failed_normalized["report_last_failure_at"], "2026-04-21T01:00:00Z")
        self.assertEqual(failed_state["sourcer_report_status"], "failed")
        self.assertEqual(failed_state["sourcer_report_attempts"], 3)

        sent_normalized, sent_state, reset = build_sourcer_report_state_update(
            agent_state=failed_state,
            status="sent",
            client_label="orchestrator_fallback",
            reason="internal reporter init failed",
            category="discord_connection_failed",
            recovery_action="fallback used",
            error="",
            attempts=1,
            channel_id="123",
            updated_at="2026-04-21T01:05:00Z",
        )

        self.assertTrue(reset)
        self.assertEqual(sent_normalized["report_last_failure_at"], "2026-04-21T01:00:00Z")
        self.assertEqual(sent_normalized["report_last_success_at"], "2026-04-21T01:05:00Z")
        self.assertEqual(sent_state["sourcer_report_status"], "sent")

    def test_should_suppress_sourcer_report_failure_log_repeats_within_window(self):
        suppressed, signature, logged_at = should_suppress_sourcer_report_failure_log(
            client_label="internal_sourcer",
            category="discord_dns_failed",
            channel_id="123",
            error_text="dns",
            last_signature="",
            last_logged_at=0.0,
            now=10.0,
        )
        self.assertFalse(suppressed)
        self.assertEqual(logged_at, 10.0)

        repeated, repeated_signature, repeated_logged_at = should_suppress_sourcer_report_failure_log(
            client_label="internal_sourcer",
            category="discord_dns_failed",
            channel_id="123",
            error_text="dns",
            last_signature=signature,
            last_logged_at=logged_at,
            now=20.0,
        )
        self.assertTrue(repeated)
        self.assertEqual(repeated_signature, signature)
        self.assertEqual(repeated_logged_at, 20.0)

    def test_resolve_sourcer_report_client_creates_internal_client(self):
        created_clients: list[dict[str, object]] = []

        def factory(**kwargs):
            created_clients.append(kwargs)
            return {"client": "sourcer"}

        client, cached_client, status = resolve_sourcer_report_client(
            existing_client=None,
            sourcer_report_config=SimpleNamespace(token_env="TOKEN", bot_id="bot-1"),
            fallback_client={"client": "orchestrator"},
            discord_client_factory=factory,
            transcript_log_file="sourcer.jsonl",
            attachment_dir="attachments",
            logger=logging.getLogger("test.notifications"),
        )

        self.assertEqual(client, {"client": "sourcer"})
        self.assertEqual(cached_client, {"client": "sourcer"})
        self.assertEqual(status["client_label"], "internal_sourcer")
        self.assertEqual(created_clients[0]["token_env"], "TOKEN")
        self.assertEqual(created_clients[0]["client_name"], "sourcer")

    def test_resolve_sourcer_report_client_falls_back_when_unconfigured(self):
        fallback_client = {"client": "orchestrator"}
        client, cached_client, status = resolve_sourcer_report_client(
            existing_client=None,
            sourcer_report_config=None,
            fallback_client=fallback_client,
            discord_client_factory=lambda **_kwargs: {"client": "unused"},
            transcript_log_file="sourcer.jsonl",
            attachment_dir="attachments",
            logger=logging.getLogger("test.notifications"),
        )

        self.assertIs(client, fallback_client)
        self.assertIsNone(cached_client)
        self.assertEqual(status["client_label"], "orchestrator_fallback")
        self.assertEqual(status["category"], "reporter_not_configured")

    def test_send_internal_relay_summary_swallows_delivery_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            service = self._build_notification_service(tmpdir, client=_FailingDiscordClient())

            asyncio.run(
                service.send_internal_relay_summary(
                    relay_channel_id="111111111111111111",
                    content="내부 relay 요약: planner -> developer (delegate)",
                    request_id="relay-summary-1",
                )
            )

    def test_send_sprint_completion_user_report_routes_to_report_channel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            client = _FakeDiscordClient()
            service = self._build_notification_service(tmpdir, client=client)

            delivered = asyncio.run(
                service.send_sprint_completion_user_report(
                    report_channel_id="333333333333333333",
                    sprint_id="260405-Sprint-17:05",
                    content="**TL;DR**\nSprint completed.",
                )
            )

            self.assertTrue(delivered)
            self.assertEqual(client.sent_channels[0][0], "333333333333333333")
            self.assertIn("Sprint completed.", client.sent_channels[0][1])

    def test_send_sprint_report_routes_to_startup_channel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            client = _FakeDiscordClient()
            service = self._build_notification_service(tmpdir, client=client)

            asyncio.run(
                service.send_sprint_report(
                    startup_channel_id="222222222222222222",
                    rendered_title="✅ 스프린트 완료",
                    report="[작업 보고]\n- 요청: sprint closeout",
                )
            )

            self.assertEqual(client.sent_channels[0][0], "222222222222222222")
            self.assertIn("[작업 보고]", client.sent_channels[0][1])


if __name__ == "__main__":
    unittest.main()
