from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from teams_runtime.core.template import refresh_workspace_prompt_assets, scaffold_workspace
from teams_runtime.shared.models import INTERNAL_TEAM_AGENTS, TEAM_ROLES


class TeamsRuntimeTemplateAssetTests(unittest.TestCase):
    def test_scaffold_workspace_uses_file_backed_role_prompt_assets(self):
        repo_root = Path(__file__).resolve().parent.parent
        prompt_path = repo_root / "templates" / "prompts" / "orchestrator.md"
        expected_prompt = prompt_path.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            rendered_prompt = (Path(tmpdir) / "orchestrator" / "AGENTS.md").read_text(encoding="utf-8")

        self.assertEqual(rendered_prompt, expected_prompt)

    def test_scaffold_workspace_uses_file_backed_internal_prompt_assets(self):
        repo_root = Path(__file__).resolve().parent.parent
        prompt_path = repo_root / "templates" / "prompts" / "version_controller.md"
        expected_prompt = prompt_path.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            rendered_prompt = (
                Path(tmpdir) / "internal" / "version_controller" / "AGENTS.md"
            ).read_text(encoding="utf-8")

        self.assertEqual(rendered_prompt, expected_prompt)

    def test_public_role_prompts_require_original_requirement_refs_except_version_controller(self):
        repo_root = Path(__file__).resolve().parent.parent
        prompt_root = repo_root / "templates" / "prompts"
        for role in ("orchestrator", "research", "planner", "designer", "architect", "developer", "qa"):
            with self.subTest(role=role):
                prompt = (prompt_root / f"{role}.md").read_text(encoding="utf-8")
                self.assertIn("REQ-*", prompt)
                self.assertIn("original_requirements", prompt)

        version_controller_prompt = (prompt_root / "version_controller.md").read_text(encoding="utf-8")
        self.assertNotIn("original_requirements", version_controller_prompt)
        self.assertNotIn("REQ-*", version_controller_prompt)

    def test_scaffold_workspace_uses_file_backed_runtime_config_assets(self):
        repo_root = Path(__file__).resolve().parent.parent
        expected_config = (repo_root / "templates" / "scaffold" / "team_runtime.yaml").read_text(
            encoding="utf-8"
        )
        expected_protocol = (repo_root / "templates" / "scaffold" / "communication_protocol.md").read_text(
            encoding="utf-8"
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            rendered_config = (Path(tmpdir) / "team_runtime.yaml").read_text(encoding="utf-8")
            rendered_protocol = (Path(tmpdir) / "communication_protocol.md").read_text(encoding="utf-8")

        self.assertEqual(rendered_config, expected_config)
        self.assertEqual(rendered_protocol, expected_protocol)

    def test_scaffold_workspace_uses_file_backed_skill_assets(self):
        repo_root = Path(__file__).resolve().parent.parent
        skill_path = (
            repo_root
            / "templates"
            / "scaffold"
            / "planner"
            / ".agents"
            / "skills"
            / "backlog_management"
            / "SKILL.md"
        )
        expected_skill = skill_path.read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            rendered_skill = (
                Path(tmpdir) / "planner" / ".agents" / "skills" / "backlog_management" / "SKILL.md"
            ).read_text(encoding="utf-8")

        self.assertEqual(rendered_skill, expected_skill)

    def test_scaffold_workspace_installs_github_issues_skill_for_public_roles_only(self):
        repo_root = Path(__file__).resolve().parent.parent
        template_root = repo_root / "templates" / "scaffold" / "skills" / "github_issues"
        expected_skill = (template_root / "SKILL.md").read_text(encoding="utf-8")
        expected_list_script = (template_root / "scripts" / "list_issues.py").read_text(encoding="utf-8")
        expected_view_script = (template_root / "scripts" / "view_issue.py").read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir)
            scaffold_workspace(workspace_root)

            for role in TEAM_ROLES:
                skill_root = workspace_root / role / ".agents" / "skills" / "github_issues"
                self.assertEqual((skill_root / "SKILL.md").read_text(encoding="utf-8"), expected_skill)
                self.assertEqual(
                    (skill_root / "scripts" / "list_issues.py").read_text(encoding="utf-8"),
                    expected_list_script,
                )
                self.assertEqual(
                    (skill_root / "scripts" / "view_issue.py").read_text(encoding="utf-8"),
                    expected_view_script,
                )

            for agent_name in INTERNAL_TEAM_AGENTS:
                self.assertFalse(
                    (
                        workspace_root
                        / "internal"
                        / agent_name
                        / ".agents"
                        / "skills"
                        / "github_issues"
                        / "SKILL.md"
                    ).exists()
                )

    def test_teams_runtime_skill_documents_safe_default_init_refresh(self):
        repo_root = Path(__file__).resolve().parent.parent
        skill_path = repo_root / "templates" / "scaffold" / ".agents" / "skills" / "teams-runtime" / "SKILL.md"
        skill_text = skill_path.read_text(encoding="utf-8")

        self.assertIn("plain `init` as the safe prompt-refresh path", skill_text)
        self.assertIn("without resetting `.teams_runtime/` or `shared_workspace/`", skill_text)
        self.assertIn("`init --reset` as destructive", skill_text)

    def test_refresh_workspace_prompt_assets_does_not_reset_runtime_state(self):
        repo_root = Path(__file__).resolve().parent.parent
        expected_prompt = (repo_root / "templates" / "prompts" / "planner.md").read_text(
            encoding="utf-8"
        )
        expected_skill = (
            repo_root
            / "templates"
            / "scaffold"
            / "planner"
            / ".agents"
            / "skills"
            / "backlog_management"
            / "SKILL.md"
        ).read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir).resolve()
            scaffold_workspace(workspace_root)
            (workspace_root / "planner" / "AGENTS.md").write_text("old planner prompt", encoding="utf-8")
            (workspace_root / "planner" / ".agents" / "skills" / "backlog_management" / "SKILL.md").write_text(
                "old backlog skill",
                encoding="utf-8",
            )
            (workspace_root / "planner" / "history.md").write_text("preserve role history", encoding="utf-8")
            (workspace_root / "shared_workspace" / "current_sprint.md").write_text(
                "preserve current sprint",
                encoding="utf-8",
            )
            (workspace_root / ".teams_runtime").mkdir(exist_ok=True)
            (workspace_root / ".teams_runtime" / "state.json").write_text('{"preserve": true}', encoding="utf-8")

            updated = refresh_workspace_prompt_assets(workspace_root)

            self.assertIn(workspace_root / "planner" / "AGENTS.md", updated)
            self.assertEqual((workspace_root / "planner" / "AGENTS.md").read_text(encoding="utf-8"), expected_prompt)
            self.assertEqual(
                (workspace_root / "planner" / ".agents" / "skills" / "backlog_management" / "SKILL.md").read_text(
                    encoding="utf-8"
                ),
                expected_skill,
            )
            self.assertEqual((workspace_root / "planner" / "history.md").read_text(encoding="utf-8"), "preserve role history")
            self.assertEqual(
                (workspace_root / "shared_workspace" / "current_sprint.md").read_text(encoding="utf-8"),
                "preserve current sprint",
            )
            self.assertTrue((workspace_root / ".teams_runtime" / "state.json").exists())

    def test_refresh_workspace_prompt_assets_backfills_public_github_issues_skill(self):
        repo_root = Path(__file__).resolve().parent.parent
        template_root = repo_root / "templates" / "scaffold" / "skills" / "github_issues"
        expected_skill = (template_root / "SKILL.md").read_text(encoding="utf-8")
        expected_list_script = (template_root / "scripts" / "list_issues.py").read_text(encoding="utf-8")
        expected_view_script = (template_root / "scripts" / "view_issue.py").read_text(encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir).resolve()
            scaffold_workspace(workspace_root)
            for role in TEAM_ROLES:
                agents_dir = workspace_root / role / ".agents"
                if role in {"orchestrator", "planner"}:
                    shutil.rmtree(agents_dir / "skills" / "github_issues")
                else:
                    shutil.rmtree(agents_dir)
            (workspace_root / "planner" / "history.md").write_text("preserve role history", encoding="utf-8")
            (workspace_root / ".teams_runtime").mkdir(exist_ok=True)
            (workspace_root / ".teams_runtime" / "state.json").write_text('{"preserve": true}', encoding="utf-8")

            updated = refresh_workspace_prompt_assets(workspace_root)

            for role in TEAM_ROLES:
                skill_root = workspace_root / role / ".agents" / "skills" / "github_issues"
                skill_path = skill_root / "SKILL.md"
                list_script = skill_root / "scripts" / "list_issues.py"
                view_script = skill_root / "scripts" / "view_issue.py"
                self.assertIn(skill_path, updated)
                self.assertEqual(skill_path.read_text(encoding="utf-8"), expected_skill)
                self.assertIn(list_script, updated)
                self.assertEqual(list_script.read_text(encoding="utf-8"), expected_list_script)
                self.assertIn(view_script, updated)
                self.assertEqual(view_script.read_text(encoding="utf-8"), expected_view_script)
            for agent_name in INTERNAL_TEAM_AGENTS:
                self.assertFalse(
                    (
                        workspace_root
                        / "internal"
                        / agent_name
                        / ".agents"
                        / "skills"
                        / "github_issues"
                        / "SKILL.md"
                    ).exists()
                )
            self.assertEqual((workspace_root / "planner" / "history.md").read_text(encoding="utf-8"), "preserve role history")
            self.assertTrue((workspace_root / ".teams_runtime" / "state.json").exists())

    def test_refresh_workspace_prompt_assets_requires_existing_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(FileNotFoundError):
                refresh_workspace_prompt_assets(Path(tmpdir))
