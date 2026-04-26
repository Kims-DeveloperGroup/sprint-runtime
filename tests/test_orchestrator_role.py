from __future__ import annotations

import tempfile
import unittest

from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.runtime.base_runtime import RoleAgentRuntime
from teams_runtime.shared.models import MessageEnvelope, RoleRuntimeConfig
from teams_runtime.workflows.roles.orchestrator import build_orchestrator_role_rules


class TeamsRuntimeOrchestratorRoleTests(unittest.TestCase):
    def test_build_orchestrator_role_rules_mentions_skill_routing_and_control_actions(self):
        rules = build_orchestrator_role_rules("./workspace")

        self.assertIn("Orchestrator-specific rules:", rules)
        self.assertIn("./.agents/skills/sprint_orchestration/SKILL.md", rules)
        self.assertIn("python -m teams_runtime sprint start|stop|restart|status --workspace-root ./workspace", rules)
        self.assertIn("compatibility-only fallback", rules)
        self.assertIn('Return `proposals.control_action = {"kind": "cancel_request", "request_id": "..."}`', rules)
        self.assertIn('return `proposals.control_action = {"kind": "execute_action", "action_name": "...", "params": {...}}`', rules)
        self.assertIn("./.agents/skills/status_reporting/SKILL.md", rules)

    def test_role_runtime_prompt_uses_orchestrator_role_rules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="orchestrator",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            envelope = MessageEnvelope(
                request_id="orchestrator-sprint-request",
                sender="user",
                target="orchestrator",
                intent="route",
                urgency="normal",
                scope="스프린트 현황 파악",
                body="스프린트 현황 파악",
            )
            request_record = {
                "request_id": "orchestrator-sprint-request",
                "scope": "스프린트 현황 파악",
                "body": "스프린트 현황 파악",
                "artifacts": [],
            }

            prompt = runtime._build_prompt(envelope, request_record)

            self.assertIn("Orchestrator-specific rules:", prompt)
            self.assertIn("./.agents/skills/sprint_orchestration/SKILL.md", prompt)
            self.assertIn("python -m teams_runtime sprint start|stop|restart|status --workspace-root ./workspace", prompt)
            self.assertIn("compatibility-only fallback", prompt)
            self.assertIn("./.agents/skills/status_reporting/SKILL.md", prompt)


if __name__ == "__main__":
    unittest.main()
