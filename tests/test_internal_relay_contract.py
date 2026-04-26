from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import teams_runtime.core.orchestration as orchestration_module
from teams_runtime.core.orchestration import INTERNAL_RELAY_SUMMARY_MARKER, TeamService
from teams_runtime.core.parsing import envelope_to_text
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.discord.client import DiscordMessage
from teams_runtime.models import MessageEnvelope


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

            self.assertEqual(
                set(payload.keys()),
                {"relay_id", "transport", "created_at", "sender_role", "target_role", "kind", "envelope"},
            )
            self.assertEqual(payload.get("transport"), orchestration_module.RELAY_TRANSPORT_INTERNAL)
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
            self.assertIn("[참고 파일]", body)
            self.assertIn("- 요청 기록:", body)
            self.assertIn("- 참고 산출물:", body)
            self.assertIn("- 주의: request record가 relay보다 우선합니다.", body)
            self.assertIn("[전달 정보]", body)

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
                self.assertIn("[전달 정보]", summary_content)
                self.assertIn("- 요청 ID:", summary_content)
                synthetic = service._build_internal_relay_message_stub(envelope, relay_id="relay-contract-2")
                self.assertEqual(synthetic.content, envelope_to_text(envelope))

                summary_dispatched = asyncio.run(service._send_relay(envelope))
                self.assertTrue(summary_dispatched)
                relay_file = next(
                    (service.paths.runtime_root / "internal_relay" / "inbox" / "developer").glob("*.json")
                )
                relay_payload = json.loads(relay_file.read_text(encoding="utf-8"))
                self.assertEqual(relay_payload.get("envelope"), envelope_before)
