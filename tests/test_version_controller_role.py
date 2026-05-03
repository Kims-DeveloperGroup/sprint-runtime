from __future__ import annotations

import tempfile
import unittest

from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.runtime.base_runtime import RoleAgentRuntime
from teams_runtime.shared.models import MessageEnvelope, RoleRuntimeConfig
from teams_runtime.workflows.roles.version_controller import build_version_controller_role_rules


class TeamsRuntimeVersionControllerRoleTests(unittest.TestCase):
    def test_build_version_controller_role_rules_mentions_commit_contract(self):
        rules = build_version_controller_role_rules()

        self.assertIn("Version-controller rules:", rules)
        self.assertIn("`Current request.version_control`", rules)
        self.assertIn("`sources/*.version_control.json`", rules)
        self.assertIn("`commit_status`, `commit_sha`, `commit_message`, `commit_paths`, and `change_detected`", rules)
        self.assertIn("`functional_title`", rules)
        self.assertIn("`committed` or `no_changes`", rules)
        self.assertIn("commit failures", rules)

    def test_role_runtime_prompt_uses_version_controller_role_rules(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="version_controller",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
                agent_root=paths.internal_agent_root("version_controller"),
            )
            envelope = MessageEnvelope(
                request_id="version-control-request",
                sender="orchestrator",
                target="version_controller",
                intent="route",
                urgency="normal",
                scope="task closeout commit",
            )
            request_record = {
                "request_id": "version-control-request",
                "scope": "task closeout commit",
                "body": "",
                "artifacts": [],
                "version_control": {
                    "title": "meta title",
                    "functional_title": "functional title",
                    "helper_command": ["git", "status", "--short"],
                },
            }

            prompt = runtime._build_prompt(envelope, request_record)

            self.assertIn("Version-controller rules:", prompt)
            self.assertIn("`Current request.version_control`", prompt)
            self.assertIn("`sources/*.version_control.json`", prompt)
            self.assertIn("`functional_title`", prompt)
            self.assertIn("commit failures", prompt)
            self.assertIn('"commit_status": "committed|no_changes|failed|no_repo"', prompt)
            self.assertIn('"change_detected": false', prompt)


if __name__ == "__main__":
    unittest.main()
