from __future__ import annotations

import unittest

from teams_runtime.workflows.roles import (
    ROLE_PROMPT_SPECS,
    VERSION_CONTROLLER_EXTRA_FIELDS,
    get_role_prompt_spec,
    render_role_prompt_spec,
)


class TeamsRuntimeRoleRegistryTests(unittest.TestCase):
    def test_role_prompt_specs_cover_runtime_roles_with_prompt_modules(self):
        for role in (
            "orchestrator",
            "planner",
            "designer",
            "architect",
            "developer",
            "qa",
            "version_controller",
        ):
            self.assertIn(role, ROLE_PROMPT_SPECS)
            self.assertIsNotNone(get_role_prompt_spec(role))

    def test_render_role_prompt_spec_returns_workspace_sensitive_rules_for_orchestrator(self):
        rules, extra_fields = render_role_prompt_spec("orchestrator", "./workspace")

        self.assertIn("Orchestrator-specific rules:", rules)
        self.assertIn("python -m teams_runtime sprint start|stop|restart|status --workspace-root ./workspace", rules)
        self.assertEqual(extra_fields, "")

    def test_render_role_prompt_spec_returns_version_controller_extra_fields(self):
        rules, extra_fields = render_role_prompt_spec("version_controller", "./workspace")

        self.assertIn("Version-controller rules:", rules)
        self.assertEqual(extra_fields, VERSION_CONTROLLER_EXTRA_FIELDS)
        self.assertIn('"commit_status": "committed|no_changes|failed|no_repo"', extra_fields)

    def test_render_role_prompt_spec_returns_empty_values_for_unknown_role(self):
        rules, extra_fields = render_role_prompt_spec("research", "./workspace")

        self.assertEqual(rules, "")
        self.assertEqual(extra_fields, "")


if __name__ == "__main__":
    unittest.main()
