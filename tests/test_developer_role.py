from __future__ import annotations

import tempfile
import unittest

from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.runtime.base_runtime import RoleAgentRuntime
from teams_runtime.shared.models import MessageEnvelope, RoleRuntimeConfig
from teams_runtime.workflows.roles.developer import build_developer_role_rules


class TeamsRuntimeDeveloperRoleTests(unittest.TestCase):
    def test_build_developer_role_rules_mentions_revision_and_message_contract(self):
        rules = build_developer_role_rules()

        self.assertIn("Developer-specific rules:", rules)
        self.assertIn("`developer_build`", rules)
        self.assertIn("`developer_revision`", rules)
        self.assertIn('target_step = "architect_review"', rules)
        self.assertIn("always return `proposals.workflow_transition`", rules)
        self.assertIn("planner-owned docs", rules)
        self.assertIn("`same meaning / same priority / same CTA` preservation work", rules)
        self.assertIn("do not silently make the UX decision in code", rules)

    def test_role_runtime_prompt_uses_developer_role_rules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="developer",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            envelope = MessageEnvelope(
                request_id="developer-review-request",
                sender="orchestrator",
                target="developer",
                intent="implement",
                urgency="normal",
                scope="메시지 contract 구현",
            )
            request_record = {
                "request_id": "developer-review-request",
                "scope": "메시지 contract 구현",
                "body": "",
                "artifacts": ["shared_workspace/sprints/spec.md"],
                "params": {
                    "workflow": {
                        "phase": "implementation",
                        "step": "developer_revision",
                    }
                },
            }

            prompt = runtime._build_prompt(envelope, request_record)

            self.assertIn("Developer-specific rules:", prompt)
            self.assertIn("`Current request.params.workflow.step` is `developer_build`", prompt)
            self.assertIn("`developer_revision`", prompt)
            self.assertIn('target_step = "architect_review"', prompt)
            self.assertIn("always return `proposals.workflow_transition`", prompt)
            self.assertIn("planner-owned docs", prompt)
            self.assertIn("`same meaning / same priority / same CTA` preservation work", prompt)


if __name__ == "__main__":
    unittest.main()
