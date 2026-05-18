from __future__ import annotations

import unittest

from teams_runtime.runtime.role_result_contract import (
    is_restart_repairable_invalid_contract_payload,
    render_role_result_contract,
    validate_role_result_contract,
)


class TeamsRuntimeRoleResultContractTests(unittest.TestCase):
    def _qa_workflow_request(self) -> dict:
        return {
            "request_id": "qa-contract-request",
            "params": {
                "workflow": {
                    "phase": "validation",
                    "step": "qa_validation",
                }
            },
        }

    def _transition(self) -> dict:
        return {
            "outcome": "complete",
            "target_phase": "",
            "target_step": "",
            "reopen_category": "",
            "reason": "QA evidence matrix passed.",
            "unresolved_items": [],
            "finalize_phase": False,
        }

    def test_workflow_qa_result_without_qa_validation_is_invalid(self):
        payload = {
            "role": "qa",
            "status": "completed",
            "summary": "검증 통과",
            "insights": [],
            "proposals": {"workflow_transition": self._transition()},
            "artifacts": [],
            "error": "",
        }

        issues = validate_role_result_contract(
            payload,
            request_record=self._qa_workflow_request(),
            role="qa",
        )

        self.assertIn("missing_qa_validation", issues)

    def test_workflow_qa_result_with_missing_decision_or_empty_evidence_is_invalid(self):
        payload = {
            "role": "qa",
            "status": "completed",
            "summary": "검증 근거 부족",
            "insights": [],
            "proposals": {
                "workflow_transition": self._transition(),
                "qa_validation": {
                    "methodology": "evidence_matrix",
                    "evidence_matrix": [],
                    "passed_checks": [],
                    "findings": [],
                    "residual_risks": [],
                    "not_checked": [],
                },
            },
            "artifacts": [],
            "error": "",
        }

        issues = validate_role_result_contract(
            payload,
            request_record=self._qa_workflow_request(),
            role="qa",
        )

        self.assertIn("qa_validation_missing_keys:decision", issues)
        self.assertIn("invalid_qa_validation_decision:", issues)
        self.assertIn("empty_qa_validation_evidence_matrix", issues)

    def test_valid_workflow_qa_evidence_matrix_passes_contract_validation(self):
        payload = {
            "role": "qa",
            "status": "completed",
            "summary": "evidence matrix 기준 검증 통과",
            "insights": [],
            "proposals": {
                "workflow_transition": self._transition(),
                "qa_validation": {
                    "methodology": "evidence_matrix",
                    "decision": "pass",
                    "evidence_matrix": [
                        {
                            "criterion": "acceptance criteria",
                            "source": "spec.md",
                            "evidence": "spec.md criterion and developer report match.",
                            "result": "pass",
                        }
                    ],
                    "passed_checks": ["acceptance criteria matched"],
                    "findings": [],
                    "residual_risks": [],
                    "not_checked": [],
                },
            },
            "artifacts": [],
            "error": "",
        }

        issues = validate_role_result_contract(
            payload,
            request_record=self._qa_workflow_request(),
            role="qa",
        )

        self.assertEqual(issues, [])

    def test_korean_prompt_example_summary_is_invalid_placeholder(self):
        payload = {
            "role": "developer",
            "status": "completed",
            "summary": "이 세션에서 직접 확인한 실제 한국어 요약",
            "insights": [],
            "proposals": {},
            "artifacts": [],
            "error": "",
        }

        issues = validate_role_result_contract(payload, role="developer")

        self.assertIn("copied_placeholder_summary", issues)

    def test_rendered_contract_does_not_prompt_rejected_summary_placeholder(self):
        contract = render_role_result_contract(request_id="request-1", role="developer")

        self.assertNotIn("이 세션에서 직접 확인한 실제 한국어 요약", contract)
        self.assertIn("<실제 실행 근거를 반영한 한국어 요약 작성>", contract)
        self.assertIn('"proposals": {}', contract)
        self.assertNotIn('"workflow_transition"', contract)

    def test_workflow_rendered_contract_includes_transition_shape(self):
        contract = render_role_result_contract(
            request_id="request-1",
            role="planner",
            workflow_required=True,
        )

        self.assertNotIn("이 세션에서 직접 확인한 실제 한국어 요약", contract)
        self.assertIn('"workflow_transition"', contract)
        self.assertIn('"outcome": "complete"', contract)
        self.assertIn('"unresolved_items": []', contract)
        self.assertIn("<실제 workflow 전환 사유를 작성>", contract)

    def test_workflow_transition_prompt_reason_is_invalid_placeholder(self):
        payload = {
            "role": "planner",
            "status": "completed",
            "summary": "planner finalize를 수행했습니다.",
            "insights": [],
            "proposals": {
                "workflow_transition": {
                    "outcome": "complete",
                    "target_phase": "",
                    "target_step": "",
                    "reopen_category": "",
                    "reason": "<실제 workflow 전환 사유를 작성>",
                    "unresolved_items": [],
                    "finalize_phase": False,
                }
            },
            "artifacts": [],
            "error": "",
        }

        issues = validate_role_result_contract(
            payload,
            request_record={
                "params": {
                    "workflow": {
                        "phase": "planning",
                        "step": "planner_finalize",
                    }
                }
            },
            role="planner",
        )

        self.assertIn("copied_workflow_transition_placeholder", issues)

    def test_restart_repairable_invalid_contract_requires_missing_transition(self):
        self.assertTrue(
            is_restart_repairable_invalid_contract_payload(
                {
                    "contract_status": "invalid",
                    "contract_repair_attempted": True,
                    "contract_issues": ["copied_placeholder_summary", "missing_workflow_transition"],
                    "proposals": {},
                }
            )
        )
        self.assertFalse(
            is_restart_repairable_invalid_contract_payload(
                {
                    "contract_status": "invalid",
                    "contract_repair_attempted": True,
                    "contract_issues": ["copied_placeholder_summary", "missing_workflow_transition"],
                    "proposals": {"workflow_transition": self._transition()},
                }
            )
        )
        self.assertTrue(
            is_restart_repairable_invalid_contract_payload(
                {
                    "status": "completed",
                    "summary": "<실제 실행 근거를 반영한 한국어 요약 작성>",
                    "proposals": {
                        "workflow_transition": {
                            "reason": "<실제 workflow 전환 사유를 작성>",
                        }
                    },
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
