from __future__ import annotations

import unittest

from teams_runtime.workflows.orchestration.engine import (
    WORKFLOW_STEP_ARCHITECT_REVIEW,
    WORKFLOW_STEP_PLANNER_DRAFT,
    WORKFLOW_STEP_QA_VALIDATION,
    WORKFLOW_STEP_RESEARCH_INITIAL,
    build_governed_routing_selection,
    coerce_nonterminal_workflow_role_result,
    default_workflow_state,
    derive_workflow_routing_decision,
    normalize_routing_reference_text,
    strongest_domain_matches,
    workflow_should_close_in_planning,
    workflow_transition,
)
from teams_runtime.workflows.roles import default_agent_utilization_policy


class TeamsRuntimeWorkflowEngineTests(unittest.TestCase):
    def test_research_initial_advances_to_planner_draft(self):
        workflow_state = default_workflow_state()
        workflow_state["step"] = WORKFLOW_STEP_RESEARCH_INITIAL
        workflow_state["phase_owner"] = "research"

        decision = derive_workflow_routing_decision(
            workflow_state,
            workflow_transition({}),
            current_role="research",
            reason="research prepass 결과를 planner가 이어갑니다.",
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision["next_role"], "planner")
        self.assertEqual(decision["workflow_state"]["step"], WORKFLOW_STEP_PLANNER_DRAFT)
        self.assertEqual(decision["workflow_state"]["phase_owner"], "planner")

    def test_planner_requested_designer_advisory_opens_planning_pass(self):
        workflow_state = default_workflow_state()
        workflow_state["step"] = WORKFLOW_STEP_PLANNER_DRAFT

        transition = workflow_transition(
            {
                "proposals": {
                    "workflow_transition": {
                        "outcome": "continue",
                        "requested_role": "designer",
                        "target_phase": "planning",
                        "target_step": "planner_advisory",
                    }
                }
            }
        )

        decision = derive_workflow_routing_decision(
            workflow_state,
            transition,
            current_role="planner",
            reason="UX advisory가 필요합니다.",
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision["next_role"], "designer")
        self.assertEqual(decision["workflow_state"]["step"], "planner_advisory")
        self.assertEqual(decision["workflow_state"]["phase_owner"], "designer")
        self.assertEqual(decision["workflow_state"]["planning_pass_count"], 1)

    def test_planning_closeout_respects_should_close_in_planning(self):
        workflow_state = default_workflow_state()
        workflow_state["step"] = WORKFLOW_STEP_PLANNER_DRAFT

        transition = workflow_transition(
            {
                "proposals": {
                    "workflow_transition": {
                        "outcome": "complete",
                        "finalize_phase": True,
                    }
                }
            }
        )

        decision = derive_workflow_routing_decision(
            workflow_state,
            transition,
            current_role="planner",
            reason="planner 문서 계약만 정리했습니다.",
            should_close_in_planning=True,
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision["next_role"], "")
        self.assertEqual(decision["workflow_state"]["phase"], "closeout")
        self.assertEqual(decision["workflow_state"]["phase_owner"], "version_controller")
        self.assertEqual(decision["workflow_state"]["phase_status"], "completed")

    def test_workflow_should_close_in_planning_accepts_planner_surface_artifact(self):
        workflow_state = default_workflow_state()
        transition = workflow_transition(
            {
                "proposals": {
                    "workflow_transition": {
                        "outcome": "complete",
                        "finalize_phase": True,
                    }
                }
            }
        )

        self.assertTrue(
            workflow_should_close_in_planning(
                workflow_state=workflow_state,
                current_role="planner",
                transition=transition,
                proposals={},
                artifacts=["shared_workspace/current_sprint.md"],
                request_indicates_execution_flag=True,
            )
        )

    def test_workflow_should_close_in_planning_rejects_implementation_handoff_without_execution_signal(self):
        workflow_state = default_workflow_state()
        transition = workflow_transition(
            {
                "proposals": {
                    "workflow_transition": {
                        "outcome": "advance",
                        "target_phase": "implementation",
                    }
                }
            }
        )

        should_close = workflow_should_close_in_planning(
            workflow_state=workflow_state,
            current_role="planner",
            transition=transition,
            proposals={},
            artifacts=["shared_workspace/sprints/example/spec.md"],
            request_indicates_execution_flag=False,
        )

        self.assertFalse(should_close)
        decision = derive_workflow_routing_decision(
            workflow_state,
            transition,
            current_role="planner",
            reason="planner 문서만 남겼지만 implementation handoff를 명시했습니다.",
            should_close_in_planning=should_close,
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision["next_role"], "architect")
        self.assertEqual(decision["workflow_state"]["phase"], "implementation")
        self.assertEqual(decision["workflow_state"]["step"], "architect_guidance")

    def test_workflow_should_close_in_planning_rejects_execution_artifact(self):
        workflow_state = default_workflow_state()
        transition = workflow_transition({})

        self.assertFalse(
            workflow_should_close_in_planning(
                workflow_state=workflow_state,
                current_role="planner",
                transition=transition,
                proposals={"root_cause_contract": {"summary": "ok"}},
                artifacts=["src/app.py"],
                request_indicates_execution_flag=False,
            )
        )

    def test_architect_review_explicit_continuation_blocks_at_limit(self):
        workflow_state = default_workflow_state()
        workflow_state["phase"] = "implementation"
        workflow_state["step"] = WORKFLOW_STEP_ARCHITECT_REVIEW
        workflow_state["phase_owner"] = "architect"
        workflow_state["review_cycle_count"] = 3
        workflow_state["review_cycle_limit"] = 3

        transition = workflow_transition(
            {
                "proposals": {
                    "workflow_transition": {
                        "outcome": "advance",
                        "target_phase": "implementation",
                        "target_step": "developer_revision",
                    }
                }
            }
        )

        decision = derive_workflow_routing_decision(
            workflow_state,
            transition,
            current_role="architect",
            reason="review cycle limit에 도달했습니다.",
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision["terminal_status"], "blocked")
        self.assertEqual(decision["workflow_state"]["phase_status"], "blocked")
        self.assertEqual(decision["workflow_state"]["reopen_category"], "implementation")

    def test_qa_verification_reopen_returns_to_developer_revision(self):
        workflow_state = default_workflow_state()
        workflow_state["phase"] = "validation"
        workflow_state["step"] = WORKFLOW_STEP_QA_VALIDATION
        workflow_state["phase_owner"] = "qa"

        transition = workflow_transition(
            {
                "proposals": {
                    "workflow_transition": {
                        "outcome": "reopen",
                        "target_phase": "implementation",
                        "target_step": "developer_revision",
                        "reopen_category": "verification",
                    }
                }
            }
        )

        decision = derive_workflow_routing_decision(
            workflow_state,
            transition,
            current_role="qa",
            reason="verification mismatch를 developer가 수정해야 합니다.",
        )

        self.assertIsNotNone(decision)
        self.assertEqual(decision["next_role"], "developer")
        self.assertEqual(decision["workflow_state"]["step"], "developer_revision")
        self.assertEqual(decision["workflow_state"]["phase_owner"], "developer")
        self.assertEqual(decision["workflow_state"]["reopen_category"], "verification")

    def test_role_capability_terms_match_in_engine(self):
        policy = default_agent_utilization_policy()

        matches = strongest_domain_matches(
            "architect",
            policy=policy,
            text=normalize_routing_reference_text("system architecture 정리가 먼저 필요합니다."),
        )

        self.assertIn("strength:system architecture", matches)

    def test_planner_report_routes_technical_spec_to_architect_in_engine(self):
        policy = default_agent_utilization_policy()

        selection = build_governed_routing_selection(
            {
                "request_id": "req-architect-spec",
                "intent": "route",
                "scope": "teams_runtime module structure overview와 developer 구현용 technical specification 작성",
                "body": "file impact와 interface contract를 정리해줘.",
            },
            policy=policy,
            current_role="planner",
            requested_role="",
            selection_source="role_report",
            routing_text=normalize_routing_reference_text(
                "route teams_runtime module structure overview와 developer 구현용 technical specification 작성 "
                "file impact와 interface contract를 정리해줘. "
                "planning은 끝났고 다음 단계는 technical specification과 module structure overview입니다."
            ),
            is_internal_sprint_request=False,
            planner_reentry_has_explicit_signal=False,
        )

        self.assertEqual(selection["selected_role"], "architect")
        self.assertIn("routing:technical specification", selection["matched_signals"])

    def test_coerce_nonterminal_workflow_role_result_allows_explicit_handoff(self):
        transition = workflow_transition(
            {
                "proposals": {
                    "workflow_transition": {
                        "outcome": "continue",
                        "requested_role": "developer",
                    }
                }
            }
        )

        result = coerce_nonterminal_workflow_role_result(
            {
                "role": "architect",
                "status": "blocked",
                "summary": "",
                "error": "developer follow-up required",
            },
            transition=transition,
            workflow_decision={"next_role": "developer", "workflow_state": default_workflow_state()},
        )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["error"], "")
        self.assertEqual(result["summary"], "developer follow-up required")

    def test_coerce_nonterminal_workflow_role_result_keeps_terminal_block(self):
        transition = workflow_transition(
            {
                "proposals": {
                    "workflow_transition": {
                        "outcome": "continue",
                        "requested_role": "developer",
                    }
                }
            }
        )
        original = {
            "role": "architect",
            "status": "blocked",
            "summary": "review cycle exhausted",
            "error": "blocked",
        }

        result = coerce_nonterminal_workflow_role_result(
            original,
            transition=transition,
            workflow_decision={"next_role": "", "terminal_status": "blocked"},
        )

        self.assertIs(result, original)


if __name__ == "__main__":
    unittest.main()
