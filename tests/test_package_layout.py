from __future__ import annotations

import importlib
import unittest


class TeamsRuntimePackageLayoutTests(unittest.TestCase):
    def test_target_package_wrappers_import_current_modules(self):
        wrapper_expectations = {
            "teams_runtime.adapters.discord.client": "DiscordClient",
            "teams_runtime.adapters.discord.lifecycle": "role_service_status",
            "teams_runtime.shared.config": "load_team_runtime_config",
            "teams_runtime.shared.paths": "RuntimePaths",
            "teams_runtime.shared.persistence": "read_json",
            "teams_runtime.shared.formatting": "render_report_sections",
            "teams_runtime.runtime.base_runtime": "RoleAgentRuntime",
            "teams_runtime.runtime.session_manager": "RoleSessionManager",
            "teams_runtime.runtime.codex": "RoleAgentRuntime",
            "teams_runtime.workflows.orchestration.team_service": "TeamService",
            "teams_runtime.workflows.orchestration.engine": "default_workflow_state",
            "teams_runtime.workflows.orchestration.ingress": "parse_user_message_content",
            "teams_runtime.workflows.orchestration.relay": "enqueue_internal_relay",
            "teams_runtime.workflows.orchestration.delegation": "build_delegate_envelope",
            "teams_runtime.workflows.orchestration.notifications": "DiscordNotificationService",
            "teams_runtime.workflows.state.backlog_store": "merge_backlog_payload",
            "teams_runtime.workflows.state.request_store": "append_request_event",
            "teams_runtime.workflows.state.sprint_store": "append_sprint_event",
            "teams_runtime.workflows.sprints.lifecycle": "build_sprint_artifact_folder_name",
            "teams_runtime.workflows.sprints.reporting": "render_sprint_report_body",
            "teams_runtime.workflows.roles.orchestrator": "build_orchestrator_role_rules",
            "teams_runtime.workflows.roles.research": "build_research_prompt",
            "teams_runtime.workflows.roles.planner": "build_planner_role_rules",
            "teams_runtime.workflows.roles.designer": "build_designer_role_rules",
            "teams_runtime.workflows.roles.architect": "build_architect_role_rules",
            "teams_runtime.workflows.roles.developer": "build_developer_role_rules",
            "teams_runtime.workflows.roles.qa": "build_qa_role_rules",
            "teams_runtime.workflows.roles.version_controller": "build_version_controller_role_rules",
            "teams_runtime.workflows.repository_ops": "capture_git_baseline",
        }

        for module_name, attribute_name in wrapper_expectations.items():
            with self.subTest(module=module_name, attribute=attribute_name):
                module = importlib.import_module(module_name)
                self.assertTrue(hasattr(module, attribute_name))

    def test_cli_adapter_module_exposes_parser_and_dispatch(self):
        module = importlib.import_module("teams_runtime.adapters.cli.commands")

        self.assertTrue(hasattr(module, "build_parser"))
        self.assertTrue(hasattr(module, "dispatch_main"))
