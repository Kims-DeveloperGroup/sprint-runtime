from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from teams_runtime.core.internal_relay import enqueue_internal_relay
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.discord.client import DiscordSendError
from teams_runtime.shared.models import MessageEnvelope
from teams_runtime.workflows.orchestration.relay import (
    consume_internal_relay_once,
    process_internal_relay_envelope,
    record_relay_delivery,
    send_relay_transport,
)


class TeamsRuntimeRelayTests(unittest.TestCase):
    def test_record_relay_delivery_appends_failure_event(self) -> None:
        request_record: dict[str, object] = {}
        envelope = MessageEnvelope(
            request_id="20260420-relay-1",
            sender="orchestrator",
            target="planner",
            intent="route",
            urgency="normal",
            scope="planner follow-up",
        )

        summary = record_relay_delivery(
            request_record,
            status="failed",
            target_description="relay:111111111111111111",
            attempts=3,
            error="TimeoutError: timeout",
            envelope=envelope,
            updated_at="2026-04-20T10:00:00+09:00",
        )

        self.assertEqual(summary, "relay 채널 전송이 실패했습니다. target=relay:111111111111111111")
        self.assertEqual(request_record["relay_send_status"], "failed")
        self.assertEqual(request_record["relay_send_target"], "relay:111111111111111111")
        self.assertEqual(request_record["relay_send_attempts"], 3)
        self.assertEqual(request_record["relay_send_error"], "TimeoutError: timeout")
        self.assertEqual(request_record["relay_send_updated_at"], "2026-04-20T10:00:00+09:00")
        event = request_record["events"][-1]
        self.assertEqual(event["type"], "relay_send_failed")
        self.assertEqual(event["payload"]["envelope_target"], "planner")
        self.assertEqual(event["payload"]["intent"], "route")

    def test_process_internal_relay_envelope_routes_forward_and_delegate_actions(self) -> None:
        report_handler = AsyncMock()
        user_handler = AsyncMock()
        delegated_handler = AsyncMock()
        build_stub = Mock(side_effect=lambda envelope, relay_id="": {"relay_id": relay_id, "target": envelope.target})
        log_malformed = Mock()

        forward_envelope = MessageEnvelope(
            request_id="20260420-relay-2",
            sender="developer",
            target="orchestrator",
            intent="route",
            urgency="normal",
            scope="forward this to intake",
            params={"_teams_kind": "forward"},
        )
        delegate_envelope = MessageEnvelope(
            request_id="20260420-relay-3",
            sender="orchestrator",
            target="planner",
            intent="plan",
            urgency="normal",
            scope="delegate planning work",
            params={"_teams_kind": "delegate"},
        )

        asyncio.run(
            process_internal_relay_envelope(
                forward_envelope,
                current_role="orchestrator",
                relay_id="relay-forward-1",
                build_internal_relay_message_stub=build_stub,
                handle_role_report=report_handler,
                handle_user_request=user_handler,
                handle_delegated_request=delegated_handler,
                log_malformed_trusted_relay=log_malformed,
            )
        )
        asyncio.run(
            process_internal_relay_envelope(
                delegate_envelope,
                current_role="planner",
                relay_id="relay-delegate-1",
                build_internal_relay_message_stub=build_stub,
                handle_role_report=report_handler,
                handle_user_request=user_handler,
                handle_delegated_request=delegated_handler,
                log_malformed_trusted_relay=log_malformed,
            )
        )

        report_handler.assert_not_awaited()
        user_handler.assert_awaited_once_with(
            {"relay_id": "relay-forward-1", "target": "orchestrator"},
            forward_envelope,
            forwarded=True,
        )
        delegated_handler.assert_awaited_once_with(
            {"relay_id": "relay-delegate-1", "target": "planner"},
            delegate_envelope,
        )
        log_malformed.assert_not_called()

    def test_consume_internal_relay_once_processes_valid_and_invalid_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            envelope = MessageEnvelope(
                request_id="20260420-relay-4",
                sender="planner",
                target="developer",
                intent="delegate",
                urgency="normal",
                scope="process this relay",
                params={"_teams_kind": "delegate"},
            )
            relay_id = enqueue_internal_relay(paths, sender_role="planner", envelope=envelope)
            inbox_dir = paths.runtime_root / "internal_relay" / "inbox" / "developer"
            (inbox_dir / "broken.json").write_text('{"relay_id": "broken"}\n', encoding="utf-8")

            processed: list[tuple[str | None, str]] = []
            archived: list[tuple[str, bool]] = []

            async def fake_process(envelope: MessageEnvelope, *, relay_id: str = "") -> None:
                processed.append((envelope.request_id, relay_id))

            def fake_archive(relay_file: Path, *, invalid: bool = False) -> None:
                archived.append((relay_file.name, invalid))

            asyncio.run(
                consume_internal_relay_once(
                    paths=paths,
                    role="developer",
                    archive_internal_relay_file=fake_archive,
                    process_internal_relay_envelope=fake_process,
                    log_exception=Mock(),
                )
            )

            self.assertEqual(processed, [("20260420-relay-4", relay_id)])
            self.assertCountEqual(
                archived,
                [("broken.json", True), (f"{relay_id}.json", False)],
            )

    def test_send_relay_transport_records_internal_success(self) -> None:
        envelope = MessageEnvelope(
            request_id="20260420-relay-5",
            sender="orchestrator",
            target="developer",
            intent="implement",
            urgency="normal",
            scope="internal relay success",
        )
        request_record: dict[str, object] = {}
        enqueue = Mock(return_value="relay-1")
        send_summary = AsyncMock()
        send_discord = AsyncMock()
        record_delivery = Mock()

        sent = asyncio.run(
            send_relay_transport(
                envelope,
                request_record=request_record,
                use_internal_relay=True,
                current_role="orchestrator",
                relay_channel_id="111111111111111111",
                target_bot_id="111111111111111116",
                enqueue_internal_relay=enqueue,
                send_internal_relay_summary=send_summary,
                send_discord_relay_envelope=send_discord,
                record_relay_delivery=record_delivery,
                log_warning=Mock(),
            )
        )

        self.assertTrue(sent)
        enqueue.assert_called_once_with(envelope)
        send_summary.assert_awaited_once_with(envelope)
        send_discord.assert_not_awaited()
        self.assertEqual(record_delivery.call_args.kwargs["status"], "sent")
        self.assertEqual(record_delivery.call_args.kwargs["target_description"], "internal:developer")

    def test_send_relay_transport_normalizes_discord_send_failures(self) -> None:
        envelope = MessageEnvelope(
            request_id="20260420-relay-6",
            sender="orchestrator",
            target="developer",
            intent="implement",
            urgency="normal",
            scope="discord relay failure",
        )
        send_discord = AsyncMock(side_effect=DiscordSendError("TimeoutError: timeout", attempts=3))
        record_delivery = Mock()
        log_warning = Mock()

        sent = asyncio.run(
            send_relay_transport(
                envelope,
                request_record={},
                use_internal_relay=False,
                current_role="orchestrator",
                relay_channel_id="111111111111111111",
                target_bot_id="111111111111111116",
                enqueue_internal_relay=Mock(),
                send_internal_relay_summary=AsyncMock(),
                send_discord_relay_envelope=send_discord,
                record_relay_delivery=record_delivery,
                log_warning=log_warning,
            )
        )

        self.assertFalse(sent)
        send_discord.assert_awaited_once()
        self.assertIn("request_id: 20260420-relay-6", send_discord.await_args.kwargs["content"])
        self.assertEqual(record_delivery.call_args.kwargs["status"], "failed")
        self.assertEqual(record_delivery.call_args.kwargs["attempts"], 3)
        self.assertEqual(record_delivery.call_args.kwargs["target_description"], "relay:111111111111111111")
        log_warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
