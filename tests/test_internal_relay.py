from __future__ import annotations

import json
import tempfile
import unittest

from teams_runtime.core.internal_relay import (
    archive_internal_relay_file,
    build_internal_relay_message_stub,
    deserialize_internal_relay_envelope,
    enqueue_internal_relay,
    internal_relay_archive_dir,
    internal_relay_inbox_dir,
    is_internal_relay_summary_content,
    load_internal_relay_envelope_file,
    pending_internal_relay_files,
    resolve_internal_relay_action,
)
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.shared.models import MessageEnvelope


class TeamsRuntimeInternalRelayTests(unittest.TestCase):
    def test_enqueue_internal_relay_writes_target_scoped_envelope(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            envelope = MessageEnvelope(
                request_id="20260419-internal-relay-1",
                sender="planner",
                target="developer",
                intent="delegate",
                urgency="normal",
                scope="auth gate follow-up",
                body="body text",
                params={"_teams_kind": "delegate"},
            )

            relay_id = enqueue_internal_relay(paths, sender_role="planner", envelope=envelope)

            relay_path = internal_relay_inbox_dir(paths, "developer") / f"{relay_id}.json"
            payload = json.loads(relay_path.read_text(encoding="utf-8"))
            restored = deserialize_internal_relay_envelope(payload.get("envelope"))

            self.assertEqual(payload["sender_role"], "planner")
            self.assertEqual(payload["target_role"], "developer")
            self.assertEqual(payload["kind"], "delegate")
            self.assertIsNotNone(restored)
            self.assertEqual(restored.request_id, envelope.request_id)
            self.assertEqual(restored.sender, "planner")
            self.assertEqual(restored.target, "developer")

    def test_archive_internal_relay_file_moves_file_into_role_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            inbox_dir = internal_relay_inbox_dir(paths, "developer")
            inbox_dir.mkdir(parents=True, exist_ok=True)
            relay_file = inbox_dir / "relay-1.json"
            relay_file.write_text('{"relay_id": "relay-1"}\n', encoding="utf-8")

            archive_internal_relay_file(paths, role="developer", relay_file=relay_file, invalid=True)

            self.assertFalse(relay_file.exists())
            archived = internal_relay_archive_dir(paths, "developer") / "relay-1-invalid.json"
            self.assertTrue(archived.exists())

    def test_pending_internal_relay_files_returns_sorted_json_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            inbox_dir = internal_relay_inbox_dir(paths, "developer")
            inbox_dir.mkdir(parents=True, exist_ok=True)
            (inbox_dir / "b.json").write_text("{}", encoding="utf-8")
            (inbox_dir / "a.json").write_text("{}", encoding="utf-8")
            (inbox_dir / ".tmp").write_text("{}", encoding="utf-8")

            relay_files = pending_internal_relay_files(paths, "developer")

            self.assertEqual([path.name for path in relay_files], ["a.json", "b.json"])

    def test_load_internal_relay_envelope_file_returns_roundtrip_record(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            envelope = MessageEnvelope(
                request_id="20260419-load-1",
                sender="planner",
                target="developer",
                intent="delegate",
                urgency="normal",
                scope="roundtrip",
                body="body text",
                params={"_teams_kind": "delegate"},
            )
            relay_id = enqueue_internal_relay(paths, sender_role="planner", envelope=envelope)
            relay_file = internal_relay_inbox_dir(paths, "developer") / f"{relay_id}.json"

            record = load_internal_relay_envelope_file(relay_file)

            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(record.relay_id, relay_id)
            self.assertEqual(record.relay_file, relay_file)
            self.assertEqual(record.envelope.request_id, envelope.request_id)
            self.assertEqual(record.envelope.sender, envelope.sender)
            self.assertEqual(record.envelope.target, envelope.target)

    def test_is_internal_relay_summary_content_matches_marker_on_first_line(self):
        self.assertTrue(
            is_internal_relay_summary_content(
                "내부 relay 요약: planner -> developer (delegate)\n```text\n[전달 정보]\n```",
                marker="내부 relay 요약:",
            )
        )
        self.assertFalse(
            is_internal_relay_summary_content(
                "planner -> developer (delegate)\n```text\n[전달 정보]\n```",
                marker="내부 relay 요약:",
            )
        )

    def test_build_internal_relay_message_stub_preserves_requester_route(self):
        envelope = MessageEnvelope(
            request_id="20260419-stub-1",
            sender="planner",
            target="developer",
            intent="delegate",
            urgency="normal",
            scope="scope",
            body="body text",
        )

        message = build_internal_relay_message_stub(
            envelope,
            current_role="developer",
            relay_channel_id="relay-channel",
            sender_bot_id="planner-bot",
            original_requester={
                "author_id": "user-1",
                "author_name": "tester",
                "channel_id": "dm-1",
                "is_dm": True,
            },
            relay_id="relay-123",
        )

        self.assertEqual(message.message_id, "relay-123")
        self.assertEqual(message.channel_id, "dm-1")
        self.assertIsNone(message.guild_id)
        self.assertEqual(message.author_id, "user-1")
        self.assertEqual(message.author_name, "tester")
        self.assertTrue(message.is_dm)
        self.assertIn("request_id: 20260419-stub-1", message.content)
        self.assertIn("intent: delegate", message.content)
        self.assertIn("scope: scope", message.content)
        self.assertTrue(message.content.rstrip().endswith("body text"))

    def test_resolve_internal_relay_action_distinguishes_orchestrator_and_delegate_flow(self):
        self.assertEqual(
            resolve_internal_relay_action(
                current_role="orchestrator",
                kind="report",
                envelope_target="orchestrator",
            ),
            "report",
        )
        self.assertEqual(
            resolve_internal_relay_action(
                current_role="orchestrator",
                kind="delegate",
                envelope_target="developer",
            ),
            "ignore_unsupported",
        )
        self.assertEqual(
            resolve_internal_relay_action(
                current_role="developer",
                kind="delegate",
                envelope_target="developer",
            ),
            "delegate",
        )
        self.assertEqual(
            resolve_internal_relay_action(
                current_role="developer",
                kind="report",
                envelope_target="developer",
            ),
            "ignore_missing_delegate",
        )


if __name__ == "__main__":
    unittest.main()
