from __future__ import annotations

import tempfile
import unittest

from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.runtime.base_runtime import RoleAgentRuntime
from teams_runtime.shared.models import MessageEnvelope, RoleRuntimeConfig
from teams_runtime.workflows.roles.qa import build_qa_role_rules


class TeamsRuntimeQaRoleTests(unittest.TestCase):
    def test_build_qa_role_rules_mentions_validation_and_ux_reopen(self):
        rules = build_qa_role_rules()

        self.assertIn("QA-specific rules:", rules)
        self.assertIn("QA owns validation only", rules)
        self.assertIn("Build an evidence matrix", rules)
        self.assertIn("Current request.result", rules)
        self.assertIn("pass`, `fail`, or `not_checked`", rules)
        self.assertIn("Never claim tests were run", rules)
        self.assertIn("proposals.qa_validation", rules)
        self.assertIn('"methodology":"evidence_matrix"', rules)
        self.assertIn("reopen_category='ux'", rules)
        self.assertIn("reopen_category='verification'", rules)
        self.assertIn("planner-owned docs as evidence", rules)
        self.assertIn("reopen to `planner_finalize`", rules)
        self.assertIn("runtime sync anomaly", rules)

    def test_role_runtime_prompt_uses_qa_role_rules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="qa",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            envelope = MessageEnvelope(
                request_id="qa-validation-request",
                sender="orchestrator",
                target="qa",
                intent="validate",
                urgency="normal",
                scope="검증 contract 확인",
            )
            request_record = {
                "request_id": "qa-validation-request",
                "scope": "검증 contract 확인",
                "body": "",
                "artifacts": ["shared_workspace/sprints/spec.md"],
                "params": {
                    "workflow": {
                        "phase": "validation",
                        "step": "qa_validation",
                    }
                },
            }

            prompt = runtime._build_prompt(envelope, request_record)

            self.assertIn("QA-specific rules:", prompt)
            self.assertIn("QA owns validation only", prompt)
            self.assertIn("Build an evidence matrix", prompt)
            self.assertIn("Current request.result", prompt)
            self.assertIn("pass`, `fail`, or `not_checked`", prompt)
            self.assertIn("Never claim tests were run", prompt)
            self.assertIn("proposals.qa_validation", prompt)
            self.assertIn('"methodology":"evidence_matrix"', prompt)
            self.assertIn("reopen_category='ux'", prompt)
            self.assertIn("planner-owned docs as evidence", prompt)
            self.assertIn("reopen to `planner_finalize`", prompt)


if __name__ == "__main__":
    unittest.main()
