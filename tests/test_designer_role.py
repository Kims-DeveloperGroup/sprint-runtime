from __future__ import annotations

import tempfile
import unittest

from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.runtime.base_runtime import RoleAgentRuntime
from teams_runtime.shared.models import MessageEnvelope, RoleRuntimeConfig
from teams_runtime.workflows.roles.designer import build_designer_role_rules


class TeamsRuntimeDesignerRoleTests(unittest.TestCase):
    def test_build_designer_role_rules_mentions_advisory_only_workflow_contract(self):
        rules = build_designer_role_rules()

        self.assertIn("Designer-specific rules:", rules)
        self.assertIn("planning advisory pass", rules)
        self.assertIn("orchestrator-triggered UX reopen pass", rules)
        self.assertIn("Put durable usability judgment in `proposals.design_feedback`.", rules)
        self.assertIn("`entry_point`: one of `planning_route`, `message_readability`, `info_prioritization`, `ux_reopen`", rules)
        self.assertIn("`message_priority` concrete with at least `lead` and `defer`", rules)
        self.assertIn("`planner_advisory`", rules)
        self.assertIn("reopen_category='ux'", rules)

    def test_role_runtime_prompt_uses_designer_role_rules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="designer",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            envelope = MessageEnvelope(
                request_id="designer-advisory-request",
                sender="orchestrator",
                target="designer",
                intent="review",
                urgency="normal",
                scope="디자이너 advisory 계약 확인",
            )
            request_record = {
                "request_id": "designer-advisory-request",
                "scope": "디자이너 advisory 계약 확인",
                "body": "",
                "artifacts": [],
                "params": {
                    "workflow": {
                        "phase": "planning",
                        "step": "planner_advisory",
                    }
                },
            }

            prompt = runtime._build_prompt(envelope, request_record)

            self.assertIn("Designer-specific rules:", prompt)
            self.assertIn("planning advisory pass or an orchestrator-triggered UX reopen pass", prompt)
            self.assertIn("`proposals.design_feedback` should include:", prompt)
            self.assertIn("`message_priority` concrete with at least `lead` and `defer`", prompt)
            self.assertIn("`Current request.params.workflow.step` is `planner_advisory`", prompt)
            self.assertIn("reopen_category='ux'", prompt)


if __name__ == "__main__":
    unittest.main()
