from __future__ import annotations

import unittest

from teams_runtime.core.workflow_state import (
    WORKFLOW_STEP_ARCHITECT_REVIEW,
    WORKFLOW_STEP_PLANNER_FINALIZE,
    WORKFLOW_STEP_RESEARCH_INITIAL,
    default_workflow_state,
    infer_legacy_internal_workflow_state,
    normalize_workflow_state,
    workflow_complete_state,
    workflow_route_to_architect_review_state,
    workflow_transition,
)


class TeamsRuntimeWorkflowStateTests(unittest.TestCase):
    def test_normalize_workflow_state_filters_invalid_values(self):
        normalized = normalize_workflow_state(
            {
                "phase": "implementation",
                "step": "developer_build",
                "phase_owner": "developer",
                "phase_status": "active",
                "planning_pass_count": -3,
                "planning_pass_limit": 0,
                "review_cycle_count": -1,
                "review_cycle_limit": 0,
                "reopen_category": "invalid",
            }
        )

        self.assertEqual(normalized["phase"], "implementation")
        self.assertEqual(normalized["step"], "developer_build")
        self.assertEqual(normalized["phase_owner"], "developer")
        self.assertEqual(normalized["planning_pass_count"], 0)
        self.assertEqual(normalized["planning_pass_limit"], 2)
        self.assertEqual(normalized["review_cycle_count"], 0)
        self.assertEqual(normalized["review_cycle_limit"], 3)
        self.assertEqual(normalized["reopen_category"], "")

    def test_infer_legacy_internal_workflow_state_for_planner_after_advisory(self):
        request_record = {
            "current_role": "planner",
            "events": [
                {"event_type": "role_report", "actor": "designer"},
            ],
        }

        state = infer_legacy_internal_workflow_state(request_record)

        self.assertEqual(state["step"], WORKFLOW_STEP_PLANNER_FINALIZE)
        self.assertEqual(state["phase_owner"], "planner")
        self.assertEqual(state["phase_status"], "finalizing")
        self.assertEqual(state["planning_pass_count"], 1)

    def test_workflow_route_to_architect_review_state_increments_review_cycle(self):
        workflow_state = default_workflow_state()
        workflow_state["phase"] = "implementation"
        workflow_state["step"] = "developer_build"
        workflow_state["review_cycle_count"] = 2

        updated = workflow_route_to_architect_review_state(workflow_state, category="implementation")

        self.assertEqual(updated["phase"], "implementation")
        self.assertEqual(updated["step"], WORKFLOW_STEP_ARCHITECT_REVIEW)
        self.assertEqual(updated["phase_owner"], "architect")
        self.assertEqual(updated["review_cycle_count"], 3)
        self.assertEqual(updated["reopen_category"], "implementation")

    def test_workflow_transition_normalizes_role_and_phase_contract(self):
        transition = workflow_transition(
            {
                "proposals": {
                    "workflow_transition": {
                        "outcome": "advance",
                        "target_phase": "planning",
                        "target_step": "research_initial",
                        "reopen_category": "",
                        "reason": "research prepass first",
                        "unresolved_items": ["external grounding"],
                    }
                }
            }
        )

        self.assertEqual(transition["outcome"], "advance")
        self.assertEqual(transition["target_step"], WORKFLOW_STEP_RESEARCH_INITIAL)
        self.assertEqual(transition["unresolved_items"], ["external grounding"])

    def test_workflow_complete_state_enters_closeout(self):
        completed = workflow_complete_state(default_workflow_state())

        self.assertEqual(completed["phase"], "closeout")
        self.assertEqual(completed["step"], "closeout")
        self.assertEqual(completed["phase_owner"], "version_controller")
        self.assertEqual(completed["phase_status"], "completed")
