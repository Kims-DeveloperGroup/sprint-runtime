from __future__ import annotations

from datetime import UTC, datetime
import unittest

from teams_runtime.core.request_reply import (
    apply_blocked_duplicate_augmentation,
    apply_blocked_duplicate_retry,
    apply_request_resume_context,
    build_planning_envelope_with_inferred_verification,
    build_duplicate_request_fingerprint,
    build_forwarded_request_params,
    build_forwarded_user_envelope,
    build_request_fingerprint_from_route,
    build_request_record,
    build_request_record_seed,
    build_requester_route,
    extract_original_requester,
    merge_requester_route,
    planning_envelope_has_explicit_source_context,
    request_identity_from_envelope,
    request_identity_matches,
    resolve_request_reply_route,
    should_request_sprint_milestone_for_relay_intake,
)
from teams_runtime.discord.client import DiscordMessage
from teams_runtime.models import MessageEnvelope


class TeamsRuntimeRequestReplyTests(unittest.TestCase):
    @staticmethod
    def _message() -> DiscordMessage:
        return DiscordMessage(
            message_id="message-1",
            channel_id="channel-direct",
            guild_id="guild-1",
            author_id="user-direct",
            author_name="Direct User",
            content="hello",
            is_dm=False,
            mentions_bot=True,
            created_at=datetime(2026, 4, 19, 12, 0, tzinfo=UTC),
        )

    @staticmethod
    def _forwarded_envelope() -> MessageEnvelope:
        return MessageEnvelope(
            request_id="req-1",
            sender="planner",
            target="orchestrator",
            intent="route",
            urgency="normal",
            scope="",
            params={
                "original_requester": {
                    "author_id": "user-forwarded",
                    "author_name": "Forwarded User",
                    "channel_id": "channel-forwarded",
                    "guild_id": "guild-forwarded",
                    "is_dm": False,
                    "message_id": "message-forwarded",
                }
            },
            body="body",
        )

    def test_extract_original_requester_prefers_nested_payload(self):
        requester = extract_original_requester(
            {
                "original_requester": {
                    "author_id": "user-1",
                    "channel_id": "channel-1",
                    "is_dm": False,
                },
                "requester_author_id": "ignored-user",
            }
        )

        self.assertEqual(requester["author_id"], "user-1")
        self.assertEqual(requester["channel_id"], "channel-1")
        self.assertFalse(requester["is_dm"])

    def test_merge_requester_route_preserves_first_non_empty_values(self):
        merged = merge_requester_route(
            {"author_id": "user-1", "channel_id": ""},
            {"author_id": "user-2", "channel_id": "channel-1", "is_dm": True},
        )

        self.assertEqual(merged["author_id"], "user-1")
        self.assertEqual(merged["channel_id"], "channel-1")
        self.assertTrue(merged["is_dm"])

    def test_build_requester_route_prefers_forwarded_original_requester(self):
        route = build_requester_route(
            self._message(),
            self._forwarded_envelope(),
            forwarded=True,
        )

        self.assertEqual(route["author_id"], "user-forwarded")
        self.assertEqual(route["channel_id"], "channel-forwarded")
        self.assertEqual(route["author_name"], "Forwarded User")

    def test_build_forwarded_request_params_preserves_public_requested_role(self):
        params = build_forwarded_request_params(
            self._message(),
            MessageEnvelope(
                request_id="req-2",
                sender="research",
                target="research",
                intent="route",
                urgency="normal",
                scope="scope",
                params={"existing": "value"},
                body="body",
            ),
            valid_user_requested_roles={"planner", "research"},
        )

        self.assertEqual(params["_teams_kind"], "forward")
        self.assertEqual(params["existing"], "value")
        self.assertEqual(params["requester_author_id"], "user-direct")
        self.assertEqual(params["user_requested_role"], "research")

    def test_build_forwarded_user_envelope_wraps_requester_metadata(self):
        forwarded = build_forwarded_user_envelope(
            self._message(),
            MessageEnvelope(
                request_id=None,
                sender="developer",
                target="version_controller",
                intent="route",
                urgency="high",
                scope="scope",
                artifacts=["a.md"],
                params={"existing": "value"},
                body="body",
            ),
            sender_role="developer",
            request_id="generated-1",
            valid_user_requested_roles={"planner", "research"},
        )

        self.assertEqual(forwarded.request_id, "generated-1")
        self.assertEqual(forwarded.sender, "developer")
        self.assertEqual(forwarded.target, "orchestrator")
        self.assertEqual(forwarded.params["existing"], "value")
        self.assertEqual(forwarded.params["_teams_kind"], "forward")
        self.assertEqual(forwarded.params["user_requested_role"], "")
        self.assertEqual(forwarded.params["requester_channel_id"], "channel-direct")

    def test_request_identity_from_envelope_uses_requester_route(self):
        author_id, channel_id = request_identity_from_envelope(
            self._message(),
            self._forwarded_envelope(),
            forwarded=True,
        )

        self.assertEqual(author_id, "user-forwarded")
        self.assertEqual(channel_id, "channel-forwarded")

    def test_request_identity_matches_checks_reply_route_identity(self):
        self.assertTrue(
            request_identity_matches(
                {"reply_route": {"author_id": "user-1", "channel_id": "channel-1"}},
                author_id="user-1",
                channel_id="channel-1",
            )
        )
        self.assertFalse(
            request_identity_matches(
                {"reply_route": {"author_id": "user-1", "channel_id": "channel-2"}},
                author_id="user-1",
                channel_id="channel-1",
            )
        )

    def test_build_request_fingerprint_from_route_uses_route_identity(self):
        self.assertEqual(
            build_request_fingerprint_from_route(
                {"author_id": "user-1", "channel_id": "channel-1"},
                intent="Plan",
                scope="Fix runtime logs",
            ),
            build_request_fingerprint_from_route(
                {"author_id": "user-1", "channel_id": "channel-1"},
                intent="plan",
                scope="fix   runtime logs",
            ),
        )

    def test_build_duplicate_request_fingerprint_prefers_forwarded_requester(self):
        fingerprint = build_duplicate_request_fingerprint(
            self._message(),
            self._forwarded_envelope(),
        )

        direct_fingerprint = build_request_fingerprint_from_route(
            {"author_id": "user-direct", "channel_id": "channel-direct"},
            intent="route",
            scope="",
        )
        forwarded_fingerprint = build_request_fingerprint_from_route(
            {"author_id": "user-forwarded", "channel_id": "channel-forwarded"},
            intent="route",
            scope="",
        )

        self.assertEqual(fingerprint, forwarded_fingerprint)
        self.assertNotEqual(fingerprint, direct_fingerprint)

    def test_build_request_record_seed_normalizes_forwarded_requester_context(self):
        seed = build_request_record_seed(
            self._message(),
            MessageEnvelope(
                request_id="req-3",
                sender="planner",
                target="developer",
                intent="implement",
                urgency="normal",
                scope="fix bug",
                params={
                    "original_requester": {
                        "author_id": "user-forwarded",
                        "channel_id": "channel-forwarded",
                    }
                },
                body="body",
            ),
            forwarded=True,
            valid_user_requested_roles={"planner", "developer"},
        )

        self.assertEqual(seed.author_id, "user-forwarded")
        self.assertEqual(seed.channel_id, "channel-forwarded")
        self.assertEqual(seed.reply_route["author_name"], "Direct User")
        self.assertEqual(seed.params["user_requested_role"], "developer")
        self.assertEqual(seed.params["original_requester"]["author_id"], "user-forwarded")
        self.assertEqual(seed.params["original_requester"]["author_name"], "Direct User")

    def test_build_request_record_uses_seed_values_and_timestamps(self):
        seed = build_request_record_seed(
            self._message(),
            MessageEnvelope(
                request_id="req-4",
                sender="planner",
                target="developer",
                intent="implement",
                urgency="high",
                scope="fix runtime",
                artifacts=["a.md"],
                params={},
                body="body",
            ),
            forwarded=False,
            valid_user_requested_roles={"planner", "developer"},
        )

        record = build_request_record(
            seed,
            envelope=MessageEnvelope(
                request_id="req-4",
                sender="planner",
                target="developer",
                intent="implement",
                urgency="high",
                scope="fix runtime",
                artifacts=["a.md"],
                params={},
                body="body",
            ),
            request_id="req-4",
            sprint_id="2026-Sprint-01",
            source_message_created_at="2026-04-19T12:00:00+00:00",
            created_at="2026-04-19T12:01:00+00:00",
            updated_at="2026-04-19T12:01:00+00:00",
        )

        self.assertEqual(record["request_id"], "req-4")
        self.assertEqual(record["sprint_id"], "2026-Sprint-01")
        self.assertEqual(record["source_message_created_at"], "2026-04-19T12:00:00+00:00")
        self.assertEqual(record["created_at"], "2026-04-19T12:01:00+00:00")
        self.assertEqual(record["updated_at"], "2026-04-19T12:01:00+00:00")
        self.assertEqual(record["reply_route"]["author_id"], "user-direct")
        self.assertEqual(record["fingerprint"], seed.fingerprint)
        self.assertEqual(record["params"]["user_requested_role"], "developer")

    def test_planning_envelope_has_explicit_source_context_detects_artifacts_and_markers(self):
        self.assertTrue(
            planning_envelope_has_explicit_source_context(
                MessageEnvelope(
                    request_id="req-5",
                    sender="user",
                    target="orchestrator",
                    intent="route",
                    urgency="normal",
                    scope="scope",
                    artifacts=["shared_workspace/spec.md"],
                    body="body",
                )
            )
        )
        self.assertTrue(
            planning_envelope_has_explicit_source_context(
                MessageEnvelope(
                    request_id="req-6",
                    sender="user",
                    target="orchestrator",
                    intent="route",
                    urgency="normal",
                    scope="verify shared_workspace/spec.md",
                    body="body",
                )
            )
        )
        self.assertFalse(
            planning_envelope_has_explicit_source_context(
                MessageEnvelope(
                    request_id="req-7",
                    sender="user",
                    target="orchestrator",
                    intent="route",
                    urgency="normal",
                    scope="planning follow-up",
                    body="최근 확인된 문서를 참고해 todo를 쪼개 주세요.",
                )
            )
        )

    def test_build_planning_envelope_with_inferred_verification_adds_artifact_and_params(self):
        enriched = build_planning_envelope_with_inferred_verification(
            MessageEnvelope(
                request_id="req-8",
                sender="user",
                target="orchestrator",
                intent="route",
                urgency="normal",
                scope="planning follow-up",
                params={"existing": "value"},
                body="todo를 쪼개 주세요.",
            ),
            verification_request_id="20260326-verified1",
            artifact_path="./shared_workspace/teams_service_evolution_plan.md",
        )

        self.assertEqual(
            enriched.artifacts,
            ["./shared_workspace/teams_service_evolution_plan.md"],
        )
        self.assertEqual(enriched.params["existing"], "value")
        self.assertEqual(enriched.params["inferred_source_request_id"], "20260326-verified1")
        self.assertEqual(
            enriched.params["inferred_source_artifact"],
            "./shared_workspace/teams_service_evolution_plan.md",
        )

    def test_build_planning_envelope_with_inferred_verification_preserves_existing_inferred_params(self):
        enriched = build_planning_envelope_with_inferred_verification(
            MessageEnvelope(
                request_id="req-9",
                sender="user",
                target="orchestrator",
                intent="route",
                urgency="normal",
                scope="planning follow-up",
                params={
                    "inferred_source_request_id": "existing-request",
                    "inferred_source_artifact": "existing.md",
                },
                body="todo를 쪼개 주세요.",
            ),
            verification_request_id="20260326-verified2",
            artifact_path="verified.md",
        )

        self.assertEqual(enriched.artifacts, ["verified.md"])
        self.assertEqual(enriched.params["inferred_source_request_id"], "existing-request")
        self.assertEqual(enriched.params["inferred_source_artifact"], "existing.md")

    def test_apply_blocked_duplicate_retry_updates_request_for_local_retry(self):
        request_record = {
            "request_id": "20260401-f0fe73e0",
            "status": "blocked",
            "scope": "old scope",
            "body": "old body",
            "artifacts": ["keep.md"],
            "params": {},
            "current_role": "planner",
            "next_role": "planner",
            "reply_route": {},
            "events": [],
        }

        updated = apply_blocked_duplicate_retry(
            request_record,
            requester_route={
                "author_id": "user-1",
                "author_name": "tester",
                "channel_id": "dm-1",
                "guild_id": "",
                "is_dm": True,
                "message_id": "msg-retry",
            },
            scope="new scope",
            followup_body="new body",
            existing_artifacts=["keep.md"],
            routing_context={"selection_source": "blocked_retry"},
            message_id="msg-retry",
        )

        self.assertEqual(updated["status"], "delegated")
        self.assertEqual(updated["current_role"], "orchestrator")
        self.assertEqual(updated["next_role"], "orchestrator")
        self.assertEqual(updated["scope"], "new scope")
        self.assertEqual(updated["body"], "new body")
        self.assertEqual(updated["artifacts"], ["keep.md"])
        self.assertEqual(updated["reply_route"]["channel_id"], "dm-1")
        self.assertEqual(updated["params"]["retry_followup_message_id"], "msg-retry")
        self.assertEqual(updated["params"]["retry_followup_body"], "new body")
        self.assertEqual(updated["routing_context"]["selection_source"], "blocked_retry")
        self.assertTrue(any(event.get("type") == "retried" for event in updated.get("events") or []))

    def test_apply_blocked_duplicate_augmentation_updates_body_and_artifacts(self):
        request_record = {
            "request_id": "20260326-d24ea592",
            "body": "old body",
            "artifacts": ["existing.md"],
        }

        updated = apply_blocked_duplicate_augmentation(
            request_record,
            followup_body="new body",
            existing_artifacts=["existing.md"],
            new_artifacts=["added.md"],
        )

        self.assertEqual(updated["body"], "new body")
        self.assertEqual(updated["artifacts"], ["existing.md", "added.md"])

    def test_apply_request_resume_context_sets_resume_metadata_and_event(self):
        request_record = {
            "request_id": "20260326-d24ea592",
            "status": "blocked",
            "current_role": "planner",
            "next_role": "planner",
            "artifacts": ["existing.md"],
            "params": {},
            "events": [],
        }

        updated = apply_request_resume_context(
            request_record,
            next_role="planner",
            summary="검증 완료된 기획 문서를 연결해 기존 blocked 요청을 재개했습니다.",
            routing_context={"selection_source": "planning_resume"},
            artifact_path="verified.md",
            verified_by_request_id="20260326-verified1",
            followup_message_id="msg-followup-1",
            followup_body="new context",
        )

        self.assertEqual(updated["status"], "delegated")
        self.assertEqual(updated["current_role"], "planner")
        self.assertEqual(updated["next_role"], "planner")
        self.assertEqual(updated["artifacts"], ["existing.md", "verified.md"])
        self.assertEqual(updated["params"]["verified_source_artifact"], "verified.md")
        self.assertEqual(updated["params"]["verified_source_request_id"], "20260326-verified1")
        self.assertEqual(updated["params"]["resume_followup_message_id"], "msg-followup-1")
        self.assertEqual(updated["params"]["resume_followup_body"], "new context")
        self.assertEqual(updated["routing_context"]["selection_source"], "planning_resume")
        self.assertTrue(any(event.get("type") == "resumed" for event in updated.get("events") or []))

    def test_resolve_request_reply_route_marks_recovered_original_requester(self):
        resolution = resolve_request_reply_route(
            {},
            {
                "original_requester": {
                    "author_id": "user-1",
                    "channel_id": "channel-recovered",
                    "is_dm": False,
                }
            },
        )

        self.assertEqual(resolution.source, "original_requester")
        self.assertEqual(resolution.route["channel_id"], "channel-recovered")
        self.assertEqual(
            resolution.recovered_reply_route,
            {
                "author_id": "user-1",
                "channel_id": "channel-recovered",
                "is_dm": False,
            },
        )

    def test_should_request_sprint_milestone_for_relay_intake_requires_relay_channel(self):
        self.assertTrue(
            should_request_sprint_milestone_for_relay_intake(
                intent="route",
                requester_route={"channel_id": "relay-channel", "is_dm": False},
                relay_channel_id="relay-channel",
                has_active_sprint=False,
            )
        )
        self.assertFalse(
            should_request_sprint_milestone_for_relay_intake(
                intent="route",
                requester_route={"channel_id": "other-channel", "is_dm": False},
                relay_channel_id="relay-channel",
                has_active_sprint=False,
            )
        )
        self.assertFalse(
            should_request_sprint_milestone_for_relay_intake(
                intent="route",
                requester_route={"channel_id": "relay-channel", "is_dm": False},
                relay_channel_id="relay-channel",
                has_active_sprint=True,
            )
        )


if __name__ == "__main__":
    unittest.main()
