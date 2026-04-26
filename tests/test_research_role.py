from __future__ import annotations

import unittest

from teams_runtime.workflows.roles.research import (
    RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED,
    RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING,
    RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE,
    build_research_decision_prompt,
    build_research_decision_retry_prompt,
    default_research_planner_guidance,
    default_research_signal,
    is_research_reason_code_schema_error,
    normalize_research_decision,
    parse_research_report,
    research_reason_code_summary,
    research_skip_summary,
)
from teams_runtime.shared.models import MessageEnvelope


class TeamsRuntimeResearchRoleTests(unittest.TestCase):
    def test_default_research_signal_falls_back_to_blocked_reason_code(self):
        signal = default_research_signal(reason_code="unsupported")

        self.assertEqual(
            signal,
            {
                "needed": False,
                "subject": "",
                "research_query": "",
                "reason_code": RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED,
            },
        )

    def test_normalize_research_decision_requires_concrete_query_when_needed(self):
        with self.assertRaisesRegex(ValueError, "research_query"):
            normalize_research_decision(
                {
                    "needed": True,
                    "subject": "Current provider pricing",
                    "research_query": "",
                    "reason_code": RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING,
                    "planner_guidance": "외부 근거가 필요합니다.",
                }
            )

    def test_build_research_decision_prompt_mentions_targeting_and_reason_code_rules(self):
        envelope = MessageEnvelope(
            request_id="request-1",
            sender="orchestrator",
            target="research",
            intent="route",
            urgency="normal",
            scope="현재 API pricing 비교",
            body="planner가 provider cost assumptions를 결정하기 전에 확인이 필요합니다.",
            params={"research": {"app": "Research App"}},
        )
        request_record = {
            "request_id": "request-1",
            "scope": envelope.scope,
            "body": envelope.body,
            "params": {
                "user_requested_role": "research",
                "workflow": {"step": "research_initial", "phase_owner": "research"},
            },
        }

        prompt = build_research_decision_prompt(
            envelope,
            request_record,
            local_sources_checked=["request.scope", "shared_workspace/sprints/current/plan.md"],
        )

        self.assertIn("Public research role explicitly targeted: true", prompt)
        self.assertIn("needed_external_grounding", prompt)
        self.assertIn("not_needed_local_evidence", prompt)
        self.assertIn("not_needed_no_subject", prompt)
        self.assertIn("Local sources already checked:", prompt)
        self.assertIn('"reason_code": "<required:', prompt)
        self.assertNotIn('"reason_code": "needed_external_grounding|not_needed_local_evidence|not_needed_no_subject"', prompt)

    def test_research_decision_retry_prompt_restates_reason_code_enum(self):
        retry_prompt = build_research_decision_retry_prompt(
            "original prompt",
            "Unsupported research reason_code: needed_external_grounding|not_needed_local_evidence",
        )

        self.assertTrue(
            is_research_reason_code_schema_error(
                "Unsupported research reason_code: needed_external_grounding|not_needed_local_evidence"
            )
        )
        self.assertIn("strict research decision schema", retry_prompt)
        self.assertIn("needed_external_grounding", retry_prompt)
        self.assertIn("not_needed_local_evidence", retry_prompt)
        self.assertIn("not_needed_no_subject", retry_prompt)
        self.assertIn("Do not return placeholders", retry_prompt)

    def test_parse_research_report_extracts_sections_and_sources(self):
        report = """# Executive Summary
Provider pricing differs enough that planner should preserve neutral abstractions.

# Planner Guidance
Keep pricing assumptions soft until provider choice is finalized.

# Backing Sources
- title: OpenAI API Pricing
  url: https://openai.com/api/pricing
  published_at: 2026-04-01
  relevance: Confirms current OpenAI list pricing.
  summary: Lists current flagship API pricing tiers.
- title: Gemini API Pricing
  url: https://ai.google.dev/gemini-api/docs/pricing
  published_at: 2026-04-02
  relevance: Confirms current Gemini list pricing.
  summary: Lists current Gemini model pricing tiers.

# Open Questions
- Should planner optimize for latency or pure token cost first?
"""

        parsed = parse_research_report(report)

        self.assertEqual(
            parsed["headline"],
            "Provider pricing differs enough that planner should preserve neutral abstractions.",
        )
        self.assertIn("Keep pricing assumptions soft", parsed["planner_guidance"])
        self.assertEqual(len(parsed["backing_sources"]), 2)
        self.assertEqual(parsed["backing_sources"][0]["title"], "OpenAI API Pricing")
        self.assertEqual(
            parsed["open_questions"],
            ["Should planner optimize for latency or pure token cost first?"],
        )

    def test_planner_guidance_and_skip_summary_use_reason_code(self):
        signal = {
            "needed": False,
            "subject": "Current provider pricing",
            "research_query": "Compare current provider pricing with official sources.",
            "reason_code": RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE,
        }

        guidance = default_research_planner_guidance(
            signal,
            local_sources_checked=["request.scope", "shared_workspace/sprints/current/plan.md"],
        )
        skip_summary = research_skip_summary(signal)
        reason_summary = research_reason_code_summary(signal["reason_code"])

        self.assertIn("local evidence", guidance)
        self.assertIn("Current provider pricing", skip_summary)
        self.assertIn("local artifact", reason_summary)


if __name__ == "__main__":
    unittest.main()
