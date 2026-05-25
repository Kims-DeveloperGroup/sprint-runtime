from __future__ import annotations

import asyncio
import tempfile
import unittest

from teams_runtime.core.config import load_discord_agents_config, load_team_runtime_config
from teams_runtime.core.notifications import (
    DiscordNotificationService,
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
            self.assertIn("런타임: model: gpt-5.5 | reasoning: medium", report)
            self.assertIn("expected_bot_id", report)
            self.assertIn("260419-Sprint-21:00", report)
            self.assertIn(str(service.discord_config.startup_channel_id), report)
            self.assertIn(str(service.discord_config.relay_channel_id), report)

    def test_append_runtime_signature_does_not_duplicate_startup_runtime_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            service = self._build_notification_service(tmpdir, client=_FakeDiscordClient())

            content = "[준비 완료] ✅ orchestrator\n- 🧠 런타임: model: gpt-5.5 | reasoning: medium"

            rendered = service.append_runtime_signature(content)

            self.assertEqual(rendered.count("model: gpt-5.5 | reasoning: medium"), 1)

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
