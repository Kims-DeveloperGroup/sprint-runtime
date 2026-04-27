from __future__ import annotations

import json
import unittest

from teams_runtime.workflows.roles.research import (
    RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED,
    RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING,
    RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE,
    build_research_decision_prompt,
    build_research_prompt,
    default_research_planner_guidance,
    default_research_signal,
    normalize_research_decision,
    normalize_research_subject_definition,
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
                    "research_subject_definition": {
                        "planning_decision": "provider cost assumption",
                        "knowledge_gap": "current provider pricing",
                        "external_boundary": "official pricing changes outside repo",
                        "planner_impact": "planner should keep provider assumptions configurable",
                        "candidate_subject": "Current provider pricing",
                        "research_query": "",
                        "source_requirements": ["official pricing pages"],
                        "rejected_subjects": ["repo implementation details"],
                        "no_subject_rationale": "",
                    },
                    "planner_guidance": "외부 근거가 필요합니다.",
                }
            )

    def test_normalize_research_decision_returns_subject_definition(self):
        payload = normalize_research_decision(
            {
                "needed": True,
                "subject": "Current provider pricing",
                "research_query": "Compare current provider pricing from official pages.",
                "reason_code": RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING,
                "research_subject_definition": {
                    "planning_decision": "provider cost assumption",
                    "knowledge_gap": "current provider pricing",
                    "external_boundary": "official pricing changes outside repo",
                    "planner_impact": "planner should keep provider assumptions configurable",
                    "candidate_subject": "Current provider pricing",
                    "research_query": "Compare current provider pricing from official pages.",
                    "source_requirements": ["official pricing pages"],
                    "rejected_subjects": ["repo implementation details"],
                    "no_subject_rationale": "",
                },
                "planner_guidance": "외부 근거가 필요합니다.",
            }
        )

        self.assertEqual(payload["signal"]["subject"], "Current provider pricing")
        self.assertEqual(
            payload["research_subject_definition"]["planning_decision"],
            "provider cost assumption",
        )

    def test_normalize_research_subject_definition_rejects_copied_milestone(self):
        with self.assertRaisesRegex(ValueError, "must not simply copy"):
            normalize_research_subject_definition(
                {
                    "needed": True,
                    "subject": "Improve runtime planning",
                    "research_query": "Research runtime planning.",
                    "research_subject_definition": {
                        "planning_decision": "milestone framing",
                        "knowledge_gap": "current workflow planning practice",
                        "external_boundary": "external workflow research guidance",
                        "planner_impact": "planner should refine the milestone",
                        "candidate_subject": "Improve runtime planning",
                        "research_query": "Research runtime planning.",
                        "source_requirements": ["workflow planning sources"],
                    },
                },
                reason_code=RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING,
                needed=True,
                request_record={
                    "scope": "Improve runtime planning",
                    "params": {"requested_milestone_title": "Improve runtime planning"},
                },
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
        self.assertIn("research_subject_definition", prompt)
        self.assertIn("planning_decision", prompt)
        self.assertIn("external_boundary", prompt)
        self.assertIn("Local sources already checked:", prompt)

    def test_build_research_prompt_uses_curated_structured_json(self):
        envelope = MessageEnvelope(
            request_id="request-structured",
            sender="orchestrator",
            target="research",
            intent="route",
            urgency="normal",
            scope="latest provider pricing",
            body="Need current provider pricing before planner locks cost assumptions.",
        )
        request_record = {
            "request_id": "request-structured",
            "scope": envelope.scope,
            "body": envelope.body,
            "params": {
                "requested_milestone_title": "Choose provider",
                "milestone_title": "Choose provider",
                "kickoff_requirements": ["Keep cost assumptions explicit"],
            },
        }

        prompt = build_research_prompt(
            envelope,
            request_record,
            signal={
                "needed": True,
                "subject": "Current provider pricing",
                "research_query": "Compare current provider pricing from official pages.",
                "reason_code": RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING,
            },
            subject_definition={
                "planning_decision": "provider cost assumption",
                "knowledge_gap": "current provider pricing",
                "external_boundary": "official pricing changes outside repo",
                "planner_impact": "planner should keep provider assumptions configurable",
                "candidate_subject": "Current provider pricing",
                "research_query": "Compare current provider pricing from official pages.",
                "source_requirements": ["official pricing pages"],
                "rejected_subjects": ["repo implementation details"],
                "no_subject_rationale": "",
            },
            local_sources_checked=["request.scope", "shared_workspace/current_sprint.md"],
            artifact_hint="shared_workspace/sprints/sprint-a/research/request-structured.md",
        )
        prompt_payload = json.loads(prompt)

        self.assertEqual(
            sorted(prompt_payload.keys()),
            [
                "defined_subject",
                "expected_report",
                "local_context_checked",
                "planner_impact",
                "research_mission",
                "source_requirements",
                "sprint_context",
            ],
        )
        self.assertEqual(prompt_payload["defined_subject"]["subject"], "Current provider pricing")
        self.assertIn("official pricing pages", prompt_payload["source_requirements"])
        self.assertNotIn('"request_id"', prompt)
        self.assertNotIn('"Incoming envelope"', prompt)

    def test_parse_research_report_extracts_sections_and_sources(self):
        report = """# Executive Summary
Provider pricing differs enough that planner should preserve neutral abstractions.

# Planner Guidance
Keep pricing assumptions soft until provider choice is finalized.

# Milestone Refinement Hints
- Refine provider selection into provider-neutral cost-boundary planning.

# Problem Framing Hints
- Provider pricing volatility is a planning constraint, not just an implementation detail.

# Spec Implications
- Keep provider pricing assumptions configurable in the spec.

# Todo Definition Hints
- Split provider abstraction and pricing verification into separate backlog slices.

# Backing Reasoning
- Official pricing pages directly affect planner's cost assumptions.

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
        self.assertEqual(
            parsed["milestone_refinement_hints"],
            ["Refine provider selection into provider-neutral cost-boundary planning."],
        )
        self.assertEqual(
            parsed["backing_reasoning"],
            ["Official pricing pages directly affect planner's cost assumptions."],
        )
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
