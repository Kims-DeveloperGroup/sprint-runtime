from __future__ import annotations

import tempfile
import unittest

from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.runtime.base_runtime import RoleAgentRuntime
from teams_runtime.shared.models import MessageEnvelope, RoleRuntimeConfig
from teams_runtime.workflows.roles.architect import build_architect_role_rules


class TeamsRuntimeArchitectRoleTests(unittest.TestCase):
    def test_build_architect_role_rules_mentions_review_and_planner_doc_boundaries(self):
        rules = build_architect_role_rules()

        self.assertIn("Architect-specific rules:", rules)
        self.assertIn("`architect_advisory`", rules)
        self.assertIn("translate that intent into implementation contracts", rules)
        self.assertIn("`architect_guidance`", rules)
        self.assertIn("`architect_review`", rules)
        self.assertIn("read-only planning evidence", rules)
        self.assertIn('target_step = "qa_validation"', rules)
        self.assertIn("top-level `status=\"completed\"`", rules)
        self.assertIn("top-level `blocked`", rules)

    def test_role_runtime_prompt_uses_architect_role_rules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="architect",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            envelope = MessageEnvelope(
                request_id="architect-review-request",
                sender="orchestrator",
                target="architect",
                intent="review",
                urgency="normal",
                scope="구조 리뷰 계약 확인",
            )
            request_record = {
                "request_id": "architect-review-request",
                "scope": "구조 리뷰 계약 확인",
                "body": "",
                "artifacts": ["shared_workspace/sprints/spec.md"],
                "params": {
                    "workflow": {
                        "phase": "implementation",
                        "step": "architect_review",
                    }
                },
            }

            prompt = runtime._build_prompt(envelope, request_record)

            self.assertIn("Architect-specific rules:", prompt)
            self.assertIn("`Current request.params.workflow.step` is `architect_advisory`", prompt)
            self.assertIn("translate that intent into implementation contracts", prompt)
            self.assertIn("`architect_review` passes without further developer work", prompt)
            self.assertIn('target_step = "qa_validation"', prompt)
            self.assertIn("top-level `blocked`", prompt)


if __name__ == "__main__":
    unittest.main()
