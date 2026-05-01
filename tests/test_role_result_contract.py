from __future__ import annotations

import unittest

from teams_runtime.runtime.role_result_contract import validate_role_result_contract


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


if __name__ == "__main__":
    unittest.main()
