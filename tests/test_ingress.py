from __future__ import annotations

import unittest
from datetime import datetime, timezone

from teams_runtime.discord.client import DiscordMessage
from teams_runtime.shared.models import MessageEnvelope
from teams_runtime.workflows.orchestration.ingress import (
    analyze_blocked_duplicate_followup,
    apply_resume_request_update,
    augment_blocked_duplicate_request,
    build_created_request_record,
    build_duplicate_reopen_reply_payload,
    build_resume_routing_context_kwargs,
    build_reused_duplicate_requester_message,
    clean_kickoff_text,
    combine_envelope_scope_and_body,
    extract_ready_planning_artifact,
    extract_verification_related_request_ids,
    extract_manual_sprint_kickoff_payload,
    extract_manual_sprint_milestone_title,
    find_blocked_requests_for_verified_artifact,
    find_recent_ready_planning_verification,
    is_manual_sprint_finalize_request,
    is_manual_sprint_start_request,
    is_blocked_planning_request_waiting_for_document,
    mark_user_request_delegated_to_orchestrator,
    normalize_kickoff_requirements,
    normalize_reference_text,
    parse_kickoff_text_sections,
    request_mentions_artifact,
    retry_blocked_duplicate_request,
    verification_result_payload,
)


class TeamsRuntimeIngressTests(unittest.TestCase):
    def test_combine_envelope_scope_and_body_dedupes_duplicate_parts(self) -> None:
        envelope = MessageEnvelope(
            request_id=None,
            sender="user",
            target="orchestrator",
            intent="route",
            urgency="normal",
            scope="start sprint",
            body="start sprint",
        )

        self.assertEqual(combine_envelope_scope_and_body(envelope), "start sprint")

    def test_parse_kickoff_text_sections_extracts_brief_and_requirements(self) -> None:
        brief, requirements = parse_kickoff_text_sections(
            "start sprint\n"
            "milestone: workflow initial\n"
            "brief: keep the original kickoff scope\n"
            "requirements:\n"
            "- preserve kickoff context\n"
            "- preserve kickoff context\n"
            "document the relay contract\n"
        )

        self.assertEqual(brief, "keep the original kickoff scope")
        self.assertEqual(requirements, ["preserve kickoff context", "document the relay contract"])

    def test_extract_manual_sprint_kickoff_payload_parses_request_text_when_fields_missing(self) -> None:
        envelope = MessageEnvelope(
            request_id="request-kickoff-1",
            sender="user",
            target="orchestrator",
            intent="route",
            urgency="normal",
            scope="start sprint",
            artifacts=[
                "./shared_workspace/sprints/2026-Sprint-01/attachments/att-1_scope.md",
                "./shared_workspace/sprints/2026-Sprint-01/attachments/att-1_scope.md",
            ],
            body=(
                "milestone: workflow initial\n"
                "brief: preserve the original scope detail\n"
                "requirements:\n"
                "- keep kickoff.md authoritative\n"
                "- derive refined milestone separately\n"
            ),
        )

        payload = extract_manual_sprint_kickoff_payload(envelope)

        self.assertEqual(payload["milestone_title"], "workflow initial")
        self.assertEqual(payload["kickoff_brief"], "preserve the original scope detail")
        self.assertEqual(
            payload["kickoff_requirements"],
            ["keep kickoff.md authoritative", "derive refined milestone separately"],
        )
        self.assertEqual(payload["kickoff_source_request_id"], "request-kickoff-1")
        self.assertEqual(
            payload["kickoff_reference_artifacts"],
            ["./shared_workspace/sprints/2026-Sprint-01/attachments/att-1_scope.md"],
        )

    def test_extract_manual_sprint_milestone_title_ignores_placeholder_values(self) -> None:
        envelope = MessageEnvelope(
            request_id=None,
            sender="user",
            target="orchestrator",
            intent="route",
            urgency="normal",
            scope="start sprint",
            body="milestone: now",
        )

        self.assertEqual(extract_manual_sprint_milestone_title(envelope), "")

    def test_manual_sprint_request_detection_respects_param_override(self) -> None:
        start_envelope = MessageEnvelope(
            request_id=None,
            sender="user",
            target="orchestrator",
            intent="route",
            urgency="normal",
            scope="plain message",
            params={"sprint_control": "start"},
        )
        finalize_envelope = MessageEnvelope(
            request_id=None,
            sender="user",
            target="orchestrator",
            intent="route",
            urgency="normal",
            scope="plain message",
            params={"sprint_control": "finalize"},
        )

        self.assertTrue(is_manual_sprint_start_request(start_envelope))
        self.assertTrue(is_manual_sprint_finalize_request(finalize_envelope))

    def test_kickoff_text_normalizers_trim_and_dedupe(self) -> None:
        self.assertEqual(clean_kickoff_text("alpha  \n beta \n"), "alpha\n beta")
        self.assertEqual(normalize_kickoff_requirements(["a", " a ", "", "b"]), ["a", "b"])

    def test_build_created_request_record_populates_created_event_and_reply_route(self) -> None:
        message = DiscordMessage(
            message_id="msg-1",
            channel_id="channel-1",
            guild_id="guild-1",
            author_id="user-1",
            author_name="user",
            content="planner route this",
            is_dm=False,
            mentions_bot=False,
            created_at=datetime.now(timezone.utc),
        )
        envelope = MessageEnvelope(
            request_id=None,
            sender="user",
            target="planner",
            intent="route",
            urgency="normal",
            scope="planner route this",
            body="planner route this",
        )

        record = build_created_request_record(
            message,
            envelope,
            forwarded=False,
            request_id="request-1",
            sprint_id="2026-Sprint-03",
            source_message_created_at="2026-04-19T10:00:00+09:00",
            created_at="2026-04-19T10:01:00+09:00",
            updated_at="2026-04-19T10:01:00+09:00",
        )

        self.assertEqual(record["request_id"], "request-1")
        self.assertEqual(record["current_role"], "orchestrator")
        self.assertEqual(record["reply_route"]["author_id"], "user-1")
        self.assertEqual(record["reply_route"]["channel_id"], "channel-1")
        self.assertEqual(record["params"]["user_requested_role"], "planner")
        self.assertEqual(record["events"][0]["type"], "created")
        self.assertEqual(record["events"][0]["payload"]["forwarded"], False)

    def test_build_created_request_record_uses_original_requester_for_forwarded_input(self) -> None:
        message = DiscordMessage(
            message_id="relay-msg-1",
            channel_id="relay-channel",
            guild_id="relay-guild",
            author_id="planner-bot",
            author_name="planner",
            content="forwarded",
            is_dm=False,
            mentions_bot=False,
            created_at=datetime.now(timezone.utc),
        )
        envelope = MessageEnvelope(
            request_id="request-forwarded-1",
            sender="planner",
            target="developer",
            intent="route",
            urgency="normal",
            scope="implement this",
            body="implement this",
            params={
                "original_requester": {
                    "author_id": "user-2",
                    "author_name": "relay user",
                    "channel_id": "channel-2",
                    "guild_id": "guild-2",
                    "is_dm": True,
                    "message_id": "user-msg-2",
                }
            },
        )

        record = build_created_request_record(
            message,
            envelope,
            forwarded=True,
            request_id="request-forwarded-1",
            sprint_id="2026-Sprint-03",
            source_message_created_at="2026-04-19T10:00:00+09:00",
            created_at="2026-04-19T10:01:00+09:00",
            updated_at="2026-04-19T10:01:00+09:00",
        )

        self.assertEqual(record["reply_route"]["author_id"], "user-2")
        self.assertEqual(record["reply_route"]["channel_id"], "channel-2")
        self.assertTrue(record["reply_route"]["is_dm"])
        self.assertEqual(record["params"]["original_requester"]["author_id"], "user-2")
        self.assertEqual(record["params"]["user_requested_role"], "developer")
        self.assertEqual(record["events"][0]["payload"]["forwarded"], True)

    def test_mark_user_request_delegated_to_orchestrator_sets_status_and_event(self) -> None:
        request_record = {
            "request_id": "request-1",
            "status": "queued",
            "current_role": "orchestrator",
            "next_role": "",
            "events": [],
        }

        updated = mark_user_request_delegated_to_orchestrator(
            request_record,
            routing_context={"selected_role": "orchestrator", "selection_source": "agent_first_intake"},
        )

        self.assertEqual(updated["status"], "delegated")
        self.assertEqual(updated["current_role"], "orchestrator")
        self.assertEqual(updated["next_role"], "orchestrator")
        self.assertEqual(updated["routing_context"]["selection_source"], "agent_first_intake")
        self.assertEqual(updated["events"][-1]["type"], "delegated")

    def test_analyze_blocked_duplicate_followup_detects_new_artifacts_and_body(self) -> None:
        duplicate_request = {
            "body": "existing body",
            "artifacts": ["artifact-a.md"],
        }
        envelope = MessageEnvelope(
            request_id=None,
            sender="user",
            target="orchestrator",
            intent="route",
            urgency="normal",
            scope="scope",
            artifacts=["artifact-a.md", "artifact-b.md"],
            body="new body",
        )

        followup = analyze_blocked_duplicate_followup(duplicate_request, envelope)

        self.assertEqual(followup.existing_artifacts, ["artifact-a.md"])
        self.assertEqual(followup.new_artifacts, ["artifact-b.md"])
        self.assertEqual(followup.followup_body, "new body")
        self.assertTrue(followup.has_new_body)

    def test_retry_blocked_duplicate_request_replaces_reply_route_and_appends_retried_event(self) -> None:
        duplicate_request = {
            "request_id": "request-1",
            "status": "blocked",
            "scope": "old scope",
            "body": "old body",
            "artifacts": ["artifact-a.md"],
            "reply_route": {},
            "events": [],
            "params": {},
            "current_role": "orchestrator",
            "next_role": "orchestrator",
            "owner_role": "orchestrator",
            "result": {"role": "orchestrator"},
        }
        message = DiscordMessage(
            message_id="msg-2",
            channel_id="channel-2",
            guild_id="guild-2",
            author_id="user-2",
            author_name="user two",
            content="same request",
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
            scope="new scope",
            artifacts=["artifact-a.md"],
            body="followup body",
        )

        updated = retry_blocked_duplicate_request(
            duplicate_request,
            message=message,
            envelope=envelope,
            forwarded=False,
            routing_context={"selection_source": "blocked_retry"},
        )

        self.assertEqual(updated["status"], "delegated")
        self.assertEqual(updated["reply_route"]["author_id"], "user-2")
        self.assertEqual(updated["scope"], "new scope")
        self.assertEqual(updated["body"], "followup body")
        self.assertEqual(updated["events"][-1]["type"], "retried")
        self.assertEqual(updated["routing_context"]["selection_source"], "blocked_retry")

    def test_augment_blocked_duplicate_request_merges_new_artifacts(self) -> None:
        duplicate_request = {
            "body": "existing body",
            "artifacts": ["artifact-a.md"],
        }
        envelope = MessageEnvelope(
            request_id=None,
            sender="user",
            target="orchestrator",
            intent="route",
            urgency="normal",
            scope="scope",
            artifacts=["artifact-b.md"],
            body="existing body",
        )

        updated, followup = augment_blocked_duplicate_request(
            duplicate_request,
            envelope=envelope,
        )

        self.assertEqual(updated["artifacts"], ["artifact-a.md", "artifact-b.md"])
        self.assertEqual(followup.new_artifacts, ["artifact-b.md"])
        self.assertFalse(followup.has_new_body)

    def test_build_resume_routing_context_kwargs_normalizes_selection_fields(self) -> None:
        kwargs = build_resume_routing_context_kwargs(
            {
                "preferred_role": "planner",
                "matched_signals": [" signal-a ", ""],
                "override_reason": "manual override",
                "matched_strongest_domains": [" workflow "],
                "matched_preferred_skills": [" skill-a "],
                "matched_behavior_traits": [" trait-a "],
                "policy_source": "policy",
                "routing_phase": "planning",
                "request_state_class": "blocked",
                "score_total": 7,
                "score_breakdown": {"workflow": 7},
                "candidate_summary": ["planner"],
            },
            selected_role="planner",
            summary="resume summary",
        )

        self.assertEqual(kwargs["reason"], "resume summary")
        self.assertEqual(kwargs["preferred_role"], "planner")
        self.assertEqual(kwargs["selection_source"], "planning_resume")
        self.assertEqual(kwargs["matched_signals"], ["signal-a"])
        self.assertEqual(kwargs["matched_strongest_domains"], ["workflow"])
        self.assertEqual(kwargs["matched_preferred_skills"], ["skill-a"])
        self.assertEqual(kwargs["matched_behavior_traits"], ["trait-a"])
        self.assertEqual(kwargs["score_total"], 7)

    def test_apply_resume_request_update_sets_artifacts_params_and_resumed_event(self) -> None:
        request_record = {
            "request_id": "request-1",
            "artifacts": ["artifact-a.md"],
            "params": {},
            "events": [],
        }

        updated = apply_resume_request_update(
            request_record,
            next_role="planner",
            summary="resumed with context",
            routing_context={"selection_source": "planning_resume"},
            artifact_path="artifact-b.md",
            verified_by_request_id="verify-1",
            followup_message_id="msg-3",
            followup_body="followup detail",
        )

        self.assertEqual(updated["status"], "delegated")
        self.assertEqual(updated["current_role"], "planner")
        self.assertEqual(updated["next_role"], "planner")
        self.assertEqual(updated["artifacts"], ["artifact-a.md", "artifact-b.md"])
        self.assertEqual(updated["params"]["verified_source_request_id"], "verify-1")
        self.assertEqual(updated["params"]["resume_followup_message_id"], "msg-3")
        self.assertEqual(updated["params"]["resume_followup_body"], "followup detail")
        self.assertEqual(updated["events"][-1]["type"], "resumed")

    def test_build_duplicate_reopen_reply_payload_supports_retry_and_augment_modes(self) -> None:
        request_record = {"request_id": "request-1"}

        retried = build_duplicate_reopen_reply_payload(
            request_record,
            relay_sent=True,
            reopen_mode="retried",
        )
        augmented = build_duplicate_reopen_reply_payload(
            request_record,
            relay_sent=False,
            reopen_mode="augmented",
        )

        self.assertEqual(retried["mode"], "status")
        self.assertEqual(retried["summary"], "기존 blocked 요청을 다시 시도합니다.")
        self.assertEqual(augmented["mode"], "text")
        self.assertIn("planner relay 전송이 실패했습니다", augmented["content"])

    def test_build_reused_duplicate_requester_message_renders_status_and_role(self) -> None:
        message = build_reused_duplicate_requester_message(
            {
                "request_id": "request-1",
                "status": "blocked",
                "current_role": "planner",
            }
        )

        self.assertIn("기존 요청을 재사용합니다.", message)
        self.assertIn("request_id=request-1", message)
        self.assertIn("status=blocked", message)
        self.assertIn("current_role=planner", message)

    def test_verification_result_helpers_extract_artifact_and_related_request_ids(self) -> None:
        result = {
            "artifacts": ["artifact-a.md"],
            "proposals": {
                "verification_result": {
                    "ready_for_planning": True,
                    "location": "artifact-b.md",
                    "related_request_ids": ["request-1", "", "request-2"],
                }
            },
        }

        payload = verification_result_payload(result)

        self.assertTrue(payload["ready_for_planning"])
        self.assertEqual(extract_ready_planning_artifact(result), "artifact-b.md")
        self.assertEqual(extract_verification_related_request_ids(result), ["request-1", "request-2"])

    def test_is_blocked_planning_request_waiting_for_document_detects_blocked_reason(self) -> None:
        request_record = {
            "status": "blocked",
            "scope": "planning request",
            "body": "source of truth 문서 확인 필요",
            "result": {
                "summary": "기획 문서 확정이 선행되어야 합니다.",
                "error": "source planning document not yet confirmed",
                "proposals": {
                    "blocked_reason": {
                        "reason": "기획 문서가 아직 확정되지 않았습니다.",
                        "required_next_step": "shared workspace에서 문서를 먼저 생성/확정합니다.",
                    }
                },
            },
        }

        self.assertTrue(is_blocked_planning_request_waiting_for_document(request_record))

    def test_request_mentions_artifact_matches_filename_alias(self) -> None:
        request_record = {
            "scope": "teams service evolution plan 검토",
            "body": "teams service evolution plan을 기준으로 backlog를 정리",
            "artifacts": [],
            "result": {"summary": "teams service evolution plan 문서가 필요합니다.", "proposals": {}},
        }

        self.assertTrue(request_mentions_artifact(request_record, "./shared_workspace/planning/teams_service_evolution_plan.md"))
        self.assertEqual(normalize_reference_text("teams_service-evolution.plan"), "teams service evolution plan")

    def test_find_recent_ready_planning_verification_filters_identity_and_recency(self) -> None:
        requests = [
            {
                "request_id": "request-old",
                "status": "completed",
                "updated_at": "2026-04-20T08:00:00+09:00",
                "reply_route": {"author_id": "user-1", "channel_id": "channel-1"},
                "result": {
                    "artifacts": ["artifact-old.md"],
                    "proposals": {"verification_result": {"ready_for_planning": True}},
                },
            },
            {
                "request_id": "request-new",
                "status": "completed",
                "updated_at": "2026-04-20T09:50:00+09:00",
                "reply_route": {"author_id": "user-1", "channel_id": "channel-1"},
                "result": {
                    "artifacts": ["artifact-new.md"],
                    "proposals": {"verification_result": {"ready_for_planning": True}},
                },
            },
        ]

        request_record, artifact_path = find_recent_ready_planning_verification(
            requests,
            author_id="user-1",
            channel_id="channel-1",
            now=datetime.fromisoformat("2026-04-20T10:00:00+09:00"),
            recency_seconds=3600.0,
            parse_datetime=datetime.fromisoformat,
        )

        self.assertEqual(request_record["request_id"], "request-new")
        self.assertEqual(artifact_path, "artifact-new.md")

    def test_find_blocked_requests_for_verified_artifact_prefers_related_request_ids(self) -> None:
        blocked_request = {
            "request_id": "request-blocked-1",
            "status": "blocked",
            "scope": "planning request",
            "body": "기획 문서 확정 필요",
            "reply_route": {"author_id": "user-1", "channel_id": "channel-1"},
            "result": {
                "summary": "기획 문서 확정이 필요합니다.",
                "error": "source planning document not yet confirmed",
                "proposals": {},
            },
        }
        matched, artifact_path = find_blocked_requests_for_verified_artifact(
            {"request_id": "verification-1"},
            {
                "artifacts": ["artifact-a.md"],
                "proposals": {
                    "verification_result": {
                        "ready_for_planning": True,
                        "related_request_ids": ["request-blocked-1"],
                    }
                },
            },
            author_id="user-1",
            channel_id="channel-1",
            load_request=lambda request_id: blocked_request if request_id == "request-blocked-1" else {},
            candidate_requests=[],
        )

        self.assertEqual([record["request_id"] for record in matched], ["request-blocked-1"])
        self.assertEqual(artifact_path, "artifact-a.md")

    def test_find_blocked_requests_for_verified_artifact_infers_single_match(self) -> None:
        blocked_request = {
            "request_id": "request-blocked-2",
            "status": "blocked",
            "scope": "teams service evolution plan",
            "body": "teams service evolution plan 기준 planning 문서 필요",
            "artifacts": [],
            "reply_route": {"author_id": "user-1", "channel_id": "channel-1"},
            "result": {
                "summary": "기획 문서 확인 필요",
                "error": "source planning document not yet confirmed",
                "proposals": {},
            },
        }
        matched, artifact_path = find_blocked_requests_for_verified_artifact(
            {"request_id": "verification-2"},
            {
                "proposals": {
                    "verification_result": {
                        "ready_for_planning": True,
                        "location": "./shared_workspace/planning/teams_service_evolution_plan.md",
                    }
                }
            },
            author_id="user-1",
            channel_id="channel-1",
            load_request=lambda _request_id: {},
            candidate_requests=[blocked_request],
        )

        self.assertEqual([record["request_id"] for record in matched], ["request-blocked-2"])
        self.assertEqual(artifact_path, "./shared_workspace/planning/teams_service_evolution_plan.md")


if __name__ == "__main__":
    unittest.main()
