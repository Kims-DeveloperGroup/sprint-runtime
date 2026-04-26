from __future__ import annotations

import unittest

from teams_runtime.core.workflow_role_policy import (
    enforce_workflow_role_report_contract,
    is_planner_owned_surface_artifact_hint,
    is_planning_surface_artifact_hint,
    qa_result_is_runtime_sync_anomaly,
    qa_result_requires_planner_reopen,
    required_workflow_planner_doc_hints,
    workflow_planner_doc_contract_violation,
)
from teams_runtime.core.workflow_state import default_workflow_state


class TeamsRuntimeWorkflowRolePolicyTests(unittest.TestCase):
    def test_required_workflow_planner_doc_hints_dedupes_prefixed_paths(self):
        required = required_workflow_planner_doc_hints(
            reopen_source_role="qa",
            request_artifacts=[
                "shared_workspace/current_sprint.md",
                "./shared_workspace/sprints/demo/spec.md",
                "shared_workspace/sprints/demo/todo_backlog.md",
            ],
            sprint_artifact_hints=[
                "./shared_workspace/sprints/demo/spec.md",
                "shared_workspace/sprints/demo/iteration_log.md",
                "shared_workspace/current_sprint.md",
            ],
        )

        self.assertEqual(
            required,
            [
                "shared_workspace/sprints/demo/spec.md",
                "shared_workspace/sprints/demo/todo_backlog.md",
                "shared_workspace/current_sprint.md",
                "shared_workspace/sprints/demo/iteration_log.md",
            ],
        )

    def test_workflow_planner_doc_contract_violation_reports_missing_required_and_files(self):
        workflow_state = default_workflow_state()
        workflow_state["phase"] = "planning"
        workflow_state["step"] = "planner_finalize"
        workflow_state["phase_owner"] = "planner"

        planner_owned_artifacts, missing_required, missing_files = workflow_planner_doc_contract_violation(
            workflow_state=workflow_state,
            role="planner",
            result_artifacts=[
                "./shared_workspace/sprints/demo/spec.md",
                "./shared_workspace/current_sprint.md",
            ],
            required_hints=[
                "shared_workspace/current_sprint.md",
                "shared_workspace/sprints/demo/spec.md",
                "shared_workspace/sprints/demo/todo_backlog.md",
            ],
            artifact_exists=lambda artifact: artifact.endswith("current_sprint.md"),
        )

        self.assertEqual(
            planner_owned_artifacts,
            [
                "shared_workspace/sprints/demo/spec.md",
                "shared_workspace/current_sprint.md",
            ],
        )
        self.assertEqual(missing_required, ["shared_workspace/sprints/demo/todo_backlog.md"])
        self.assertEqual(missing_files, ["shared_workspace/sprints/demo/spec.md"])

    def test_qa_result_requires_planner_reopen_for_spec_contract_mismatch(self):
        workflow_state = default_workflow_state()
        workflow_state["phase"] = "validation"
        workflow_state["step"] = "qa_validation"
        workflow_state["phase_owner"] = "qa"

        result = {
            "status": "completed",
            "summary": "spec.md acceptance criteria mismatch가 남아 있습니다.",
            "error": "",
            "insights": [],
        }
        transition = {
            "outcome": "reopen",
            "target_step": "developer_revision",
            "reopen_category": "verification",
            "reason": "acceptance criteria와 구현 결과가 어긋납니다.",
            "unresolved_items": [],
        }

        self.assertTrue(
            qa_result_requires_planner_reopen(
                workflow_state=workflow_state,
                role="qa",
                result=result,
                transition=transition,
            )
        )

    def test_qa_result_is_runtime_sync_anomaly_only_for_doc_drift(self):
        workflow_state = default_workflow_state()
        workflow_state["phase"] = "validation"
        workflow_state["step"] = "qa_validation"
        workflow_state["phase_owner"] = "qa"

        result = {
            "status": "completed",
            "summary": "planner-owned planning 문서 sync drift만 보입니다.",
            "error": "",
            "insights": ["current_sprint / todo_backlog 동기화 필요"],
        }
        transition = {
            "outcome": "advance",
            "target_step": "",
            "reopen_category": "",
            "reason": "planning doc sync only",
            "unresolved_items": [],
        }

        self.assertTrue(
            qa_result_is_runtime_sync_anomaly(
                workflow_state=workflow_state,
                role="qa",
                result=result,
                transition=transition,
            )
        )

    def test_enforce_workflow_role_report_contract_blocks_missing_planner_docs(self):
        workflow_state = default_workflow_state()
        workflow_state["phase"] = "planning"
        workflow_state["step"] = "planner_finalize"
        workflow_state["phase_owner"] = "planner"

        result = {
            "role": "planner",
            "status": "completed",
            "summary": "planner가 문서를 일부만 정리했습니다.",
            "insights": [],
            "proposals": {},
            "artifacts": ["./shared_workspace/sprints/demo/spec.md"],
            "error": "",
        }

        updated = enforce_workflow_role_report_contract(
            workflow_state=workflow_state,
            role="planner",
            result=result,
            planner_doc_contract=(
                ["shared_workspace/sprints/demo/spec.md"],
                ["shared_workspace/current_sprint.md"],
                [],
            ),
            transition={},
        )

        self.assertEqual(updated["status"], "blocked")
        self.assertIn("planner 문서 계약", updated["summary"])
        self.assertEqual(updated["proposals"]["workflow_transition"]["target_step"], "planner_finalize")

    def test_enforce_workflow_role_report_contract_strips_planner_owned_impl_artifacts(self):
        workflow_state = default_workflow_state()
        workflow_state["phase"] = "implementation"
        workflow_state["step"] = "developer_build"
        workflow_state["phase_owner"] = "developer"

        result = {
            "role": "developer",
            "status": "completed",
            "summary": "구현을 마쳤습니다.",
            "insights": [],
            "proposals": {},
            "artifacts": [
                "./shared_workspace/sprints/demo/todo_backlog.md",
                "workspace/src/example.py",
            ],
            "error": "",
        }

        updated = enforce_workflow_role_report_contract(
            workflow_state=workflow_state,
            role="developer",
            result=result,
            transition={},
        )

        self.assertEqual(updated["artifacts"], ["workspace/src/example.py"])
        self.assertTrue(
            any("planner-owned 문서를 제외했습니다" in item for item in (updated["insights"] or []))
        )

    def test_artifact_hint_helpers_classify_planning_surfaces(self):
        self.assertTrue(is_planning_surface_artifact_hint("./shared_workspace/current_sprint.md"))
        self.assertTrue(is_planner_owned_surface_artifact_hint("./shared_workspace/sprints/demo/spec.md"))
        self.assertFalse(is_planner_owned_surface_artifact_hint("workspace/src/example.py"))


if __name__ == "__main__":
    unittest.main()
