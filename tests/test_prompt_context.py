from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

import yaml

from teams_runtime.core.template import scaffold_workspace
from teams_runtime.runtime.base_runtime import RoleAgentRuntime
from teams_runtime.shared.config import load_team_runtime_config
from teams_runtime.shared.models import (
    MessageEnvelope,
    PromptContextRuntimeConfig,
    RoleRuntimeConfig,
)
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.prompt_context import (
    project_request_record_for_prompt,
    render_prompt_event_history_notice,
)
from teams_runtime.workflows.orchestration.team_service import TeamService
from teams_runtime.workflows.roles.research import build_research_decision_prompt


def _role_report(timestamp: str, role: str, summary: str) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "role_report",
        "actor": role,
        "payload": {
            "role": role,
            "status": "completed",
            "summary": summary,
        },
    }


def _envelope(request_id: str = "request-123") -> MessageEnvelope:
    return MessageEnvelope(
        request_id=request_id,
        sender="orchestrator",
        target="planner",
        intent="plan",
        urgency="normal",
        scope="Compact the request prompt.",
        body="ENVELOPE-CONTEXT-MARKER",
    )


class PromptContextProjectionTests(unittest.TestCase):
    def test_projection_backfills_latest_missing_role_evidence(self):
        events = [
            {"timestamp": "T01", "type": "created", "actor": "orchestrator", "summary": "Request created."},
            _role_report("T02", "research", "Research completed."),
            {"timestamp": "T03", "type": "delegated", "actor": "orchestrator", "summary": "To planner."},
            _role_report("T04", "planner", "Initial plan drafted."),
            _role_report("T05", "designer", "Design constraints recorded."),
            _role_report("T06", "planner", "Final plan completed."),
            {"timestamp": "T07", "type": "retried", "actor": "orchestrator", "summary": "Retried."},
            _role_report("T08", "developer", "Implementation completed."),
            _role_report("T09", "architect", "Implementation reviewed."),
            {"timestamp": "T10", "type": "delegated", "actor": "orchestrator", "summary": "To QA."},
            _role_report("T11", "qa", "A regression remains."),
            {"timestamp": "T12", "type": "resumed", "actor": "orchestrator", "summary": "Resumed."},
        ]
        request_record = {
            "request_id": "request-123",
            "status": "delegated",
            "artifacts": ["shared_workspace/spec.md"],
            "result": {
                "role": "qa",
                "status": "blocked",
                "summary": "CURRENT-RESULT-MARKER",
            },
            "events": events,
        }
        original = copy.deepcopy(request_record)

        projection = project_request_record_for_prompt(
            request_record,
            PromptContextRuntimeConfig(recent_events=4, max_events=7),
        )

        self.assertEqual(
            [event["timestamp"] for event in projection.request_record["events"]],
            ["T05", "T06", "T08", "T09", "T10", "T11", "T12"],
        )
        self.assertEqual(projection.request_record["events"][0], events[4])
        self.assertEqual(projection.total_events, 12)
        self.assertEqual(projection.included_events, 7)
        self.assertEqual(projection.omitted_events, 5)
        self.assertEqual(
            projection.notice(),
            {
                "compacted": True,
                "total_events": 12,
                "included_events": 7,
                "omitted_events": 5,
                "recent_events": 4,
                "max_events": 7,
                "selection": "recent_tail_plus_latest_role_evidence",
                "canonical_request": "./.teams_runtime/requests/request-123.json",
            },
        )
        self.assertEqual(request_record, original)

    def test_disabled_and_under_limit_histories_remain_complete(self):
        request_record = {
            "request_id": "request-disabled",
            "events": [
                _role_report(f"T{index:02d}", "planner", f"report-{index}")
                for index in range(20)
            ],
        }

        disabled = project_request_record_for_prompt(
            request_record,
            PromptContextRuntimeConfig(enabled=False, recent_events=2, max_events=3),
        )
        under_limit = project_request_record_for_prompt(
            {"request_id": "request-short", "events": request_record["events"][:3]},
            PromptContextRuntimeConfig(recent_events=2, max_events=3),
        )

        self.assertEqual(disabled.request_record["events"], request_record["events"])
        self.assertFalse(disabled.compacted)
        self.assertEqual(render_prompt_event_history_notice(disabled), "")
        self.assertEqual(under_limit.request_record["events"], request_record["events"][:3])
        self.assertFalse(under_limit.compacted)

    def test_recent_only_and_malformed_histories_are_fail_safe(self):
        events: list[object] = [
            _role_report("T01", "research", "old research"),
            _role_report("T02", "planner", "old planner"),
            {"timestamp": "T03", "type": "created"},
            "MALFORMED-RECENT-EVENT",
            {"timestamp": "T05", "type": "resumed", "summary": "LATEST-EVENT"},
        ]
        recent_only = project_request_record_for_prompt(
            {"request_id": "request-recent", "events": events},  # type: ignore[typeddict-item]
            PromptContextRuntimeConfig(recent_events=2, max_events=2),
        )
        malformed_history = project_request_record_for_prompt(
            {"request_id": "request-malformed", "events": "not-a-list"},  # type: ignore[typeddict-item]
            PromptContextRuntimeConfig(recent_events=2, max_events=2),
        )

        self.assertEqual(
            recent_only.request_record["events"],
            ["MALFORMED-RECENT-EVENT", {"timestamp": "T05", "type": "resumed", "summary": "LATEST-EVENT"}],
        )
        self.assertEqual(malformed_history.request_record["events"], "not-a-list")
        self.assertFalse(malformed_history.compacted)

    def test_backfill_skips_roles_in_tail_and_accepts_legacy_evidence_shapes(self):
        events = [
            {"timestamp": "T01", "type": "created"},
            _role_report("T02", "research", "Research evidence."),
            _role_report("T03", "planner", "Stale planner evidence."),
            {
                "timestamp": "T04",
                "type": "legacy",
                "event_type": "role_report",
                "actor": "architect",
                "payload": {"summary": "Legacy architect evidence."},
            },
            {
                "timestamp": "T05",
                "type": "commit_inspected",
                "actor": "orchestrator",
                "payload": {
                    "role": "version_controller",
                    "status": "completed",
                    "summary": "Version-control evidence.",
                },
            },
            _role_report("T06", "planner", "Planner is already represented in the tail."),
            {"timestamp": "T07", "type": "resumed"},
        ]

        projection = project_request_record_for_prompt(
            {"request_id": "request-evidence-shapes", "events": events},
            PromptContextRuntimeConfig(recent_events=2, max_events=5),
        )

        self.assertEqual(
            [event["timestamp"] for event in projection.request_record["events"]],
            ["T02", "T04", "T05", "T06", "T07"],
        )
        self.assertNotIn(events[2], projection.request_record["events"])

    def test_normal_repair_and_research_prompts_share_projection(self):
        request_record = {
            "request_id": "request-prompt",
            "scope": "Compact the request prompt.",
            "body": "",
            "artifacts": ["ARTIFACT-MARKER"],
            "params": {"workflow": {"phase": "implementation", "step": "developer_build"}},
            "result": {
                "role": "planner",
                "status": "completed",
                "summary": "CURRENT-RESULT-MARKER",
            },
            "events": [
                {"timestamp": "T01", "type": "created", "summary": "OMITTED-EVENT-MARKER"},
                _role_report("T02", "research", "RESEARCH-EVIDENCE-MARKER"),
                _role_report("T03", "planner", "OLD-PLANNER-MARKER"),
                {"timestamp": "T04", "type": "delegated", "summary": "RECENT-EVENT-ONE"},
                {"timestamp": "T05", "type": "resumed", "summary": "RECENT-EVENT-TWO"},
            ],
        }
        config = PromptContextRuntimeConfig(recent_events=2, max_events=3)
        envelope = _envelope("request-prompt")

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime = RoleAgentRuntime(
                paths=RuntimePaths.from_root(tmpdir),
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
                prompt_context_config=config,
            )
            normal_prompt = runtime._build_prompt(envelope, request_record)
            repair_prompt = runtime._build_role_result_repair_prompt(
                request_record,
                {
                    "request_id": "request-prompt",
                    "role": "planner",
                    "status": "failed",
                    "summary": "Invalid result.",
                },
                ["missing_summary"],
                current_sprint_id="sprint-a",
            )

        research_prompt = build_research_decision_prompt(
            envelope,
            request_record,
            local_sources_checked=["request.scope"],
            prompt_context_config=config,
        )

        for prompt in (normal_prompt, repair_prompt, research_prompt):
            self.assertIn('"compacted": true', prompt)
            self.assertIn("OLD-PLANNER-MARKER", prompt)
            self.assertIn("RECENT-EVENT-ONE", prompt)
            self.assertIn("RECENT-EVENT-TWO", prompt)
            self.assertNotIn("OMITTED-EVENT-MARKER", prompt)
            self.assertNotIn("RESEARCH-EVIDENCE-MARKER", prompt)
            self.assertIn("CURRENT-RESULT-MARKER", prompt)
            self.assertIn("ARTIFACT-MARKER", prompt)

        self.assertIn("ENVELOPE-CONTEXT-MARKER", normal_prompt)
        self.assertIn("ENVELOPE-CONTEXT-MARKER", research_prompt)

    def test_large_history_prompt_is_bounded_and_materially_smaller(self):
        large_text = "payload-context-" * 80
        roles = ("orchestrator", "research", "planner", "designer", "architect", "developer", "qa")
        events = [
            {
                **_role_report(f"T{index:03d}", roles[index % len(roles)], f"{index}-{large_text}"),
                "sequence": index,
            }
            for index in range(100)
        ]
        request_record = {
            "request_id": "request-large",
            "scope": "Large history",
            "body": "",
            "artifacts": [],
            "result": {
                "role": "qa",
                "status": "completed",
                "summary": "CURRENT-LARGE-RESULT",
            },
            "events": events,
        }
        envelope = _envelope("request-large")

        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            compact_runtime = RoleAgentRuntime(
                paths=paths,
                role="qa",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
                prompt_context_config=PromptContextRuntimeConfig(),
            )
            full_runtime = RoleAgentRuntime(
                paths=paths,
                role="qa",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
                prompt_context_config=PromptContextRuntimeConfig(enabled=False),
            )

            compact_prompt = compact_runtime._build_prompt(envelope, request_record)
            full_prompt = full_runtime._build_prompt(envelope, request_record)

        projection = project_request_record_for_prompt(
            request_record,
            PromptContextRuntimeConfig(),
        )
        self.assertLessEqual(projection.included_events, 16)
        self.assertLessEqual(len(compact_prompt), len(full_prompt) * 0.30)
        self.assertIn("CURRENT-LARGE-RESULT", compact_prompt)


class PromptContextConfigTests(unittest.TestCase):
    def test_config_defaults_custom_values_and_scaffold(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            scaffold_payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))

            default_config = load_team_runtime_config(tmpdir)
            self.assertEqual(default_config.prompt_context, PromptContextRuntimeConfig())
            self.assertEqual(
                scaffold_payload["prompt_context"],
                {"enabled": True, "recent_events": 8, "max_events": 16},
            )

            scaffold_payload.pop("prompt_context")
            config_path.write_text(yaml.safe_dump(scaffold_payload, sort_keys=False), encoding="utf-8")
            missing_config = load_team_runtime_config(tmpdir)
            self.assertEqual(missing_config.prompt_context, PromptContextRuntimeConfig())

            scaffold_payload["prompt_context"] = {
                "enabled": False,
                "recent_events": 4,
                "max_events": 7,
            }
            config_path.write_text(yaml.safe_dump(scaffold_payload, sort_keys=False), encoding="utf-8")
            custom_config = load_team_runtime_config(tmpdir)
            self.assertEqual(
                custom_config.prompt_context,
                PromptContextRuntimeConfig(enabled=False, recent_events=4, max_events=7),
            )

    def test_config_rejects_invalid_prompt_context_values(self):
        invalid_values = [
            ("not-a-mapping", "must be a mapping"),
            ({"enabled": "yes"}, "enabled must be a boolean"),
            ({"recent_events": True}, "recent_events must be a positive integer"),
            ({"recent_events": 0}, "recent_events must be a positive integer"),
            ({"max_events": 0}, "max_events must be a positive integer"),
            ({"recent_events": 8, "max_events": 7}, "greater than or equal"),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            base_payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))

            for prompt_context, error_pattern in invalid_values:
                with self.subTest(prompt_context=prompt_context):
                    payload = dict(base_payload)
                    payload["prompt_context"] = prompt_context
                    config_path.write_text(
                        yaml.safe_dump(payload, sort_keys=False),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, error_pattern):
                        load_team_runtime_config(tmpdir)

    def test_team_service_propagates_config_to_all_model_facing_role_runtimes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload["prompt_context"] = {
                "enabled": True,
                "recent_events": 3,
                "max_events": 6,
            }
            config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
            expected = PromptContextRuntimeConfig(recent_events=3, max_events=6)

            service = TeamService(tmpdir, "planner", enable_discord_client=False)
            research_runtime = service._runtime_for_role("research", service.runtime_config.sprint_id)

            self.assertEqual(service.role_runtime.prompt_context_config, expected)
            self.assertEqual(research_runtime.prompt_context_config, expected)
            self.assertEqual(service.version_controller_runtime.prompt_context_config, expected)


if __name__ == "__main__":
    unittest.main()
