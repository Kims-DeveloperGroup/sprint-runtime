from __future__ import annotations

import tempfile
import unittest

from pathlib import Path

from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.runtime.identities import local_runtime_identity
from teams_runtime.runtime.session_manager import RoleSessionManager


class TeamsRuntimeSessionManagerTests(unittest.TestCase):
    def test_session_manager_creates_runtime_identity_scoped_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime_identity = local_runtime_identity("orchestrator", "planner")
            manager = RoleSessionManager(
                paths,
                "planner",
                "sprint-a",
                runtime_identity=runtime_identity,
            )

            state = manager.ensure_session()

            self.assertEqual(state.runtime_identity, runtime_identity)
            self.assertIn("/sessions/", state.workspace_path)
            self.assertTrue(paths.session_state_file("planner", runtime_identity=runtime_identity).exists())

    def test_session_manager_workspace_context_mentions_runtime_roots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            manager = RoleSessionManager(paths, "planner", "sprint-a")

            state = manager.ensure_session()
            context_text = (Path(state.workspace_path) / "workspace_context.md").read_text(encoding="utf-8")

            self.assertIn("teams_runtime_root", context_text)
            self.assertIn("project_workspace_root", context_text)
            self.assertIn("./shared_workspace", context_text)
            self.assertIn("./workspace", context_text)


if __name__ == "__main__":
    unittest.main()
