from __future__ import annotations

import tempfile
import unittest

from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.workflows.orchestration.artifacts import (
    collect_backlog_candidates_from_payload,
    planner_backlog_write_receipts,
    resolve_artifact_path,
    workspace_artifact_hint,
)


class TeamsRuntimeArtifactHelperTests(unittest.TestCase):
    def test_workspace_artifact_hint_prefers_workspace_local_roots(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            shared_path = paths.shared_workspace_root / "planning.md"
            runtime_path = paths.runtime_root / "requests" / "sample.json"
            docs_path = paths.docs_root / "architecture.md"
            for path in (shared_path, runtime_path, docs_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")

            self.assertEqual(workspace_artifact_hint(paths, shared_path), "./shared_workspace/planning.md")
            self.assertEqual(workspace_artifact_hint(paths, runtime_path), "./.teams_runtime/requests/sample.json")
            self.assertEqual(workspace_artifact_hint(paths, docs_path), "./docs/architecture.md")

    def test_resolve_artifact_path_supports_workspace_teams_generated_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(f"{tmpdir}/teams_generated")
            shared_path = paths.shared_workspace_root / "planning.md"
            source_path = paths.workspace_root / "src" / "sample.py"
            for path in (shared_path, source_path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("x", encoding="utf-8")

            self.assertEqual(
                resolve_artifact_path(paths, "./workspace/teams_generated/shared_workspace/planning.md"),
                shared_path.resolve(),
            )
            self.assertEqual(
                resolve_artifact_path(paths, "./workspace/teams_generated/src/sample.py"),
                source_path.resolve(),
            )
            self.assertEqual(resolve_artifact_path(paths, "./workspace/src/sample.py"), source_path.resolve())

    def test_backlog_payload_and_receipts_normalize_nested_shapes(self) -> None:
        payload = {
            "nested": {
                "backlog_items": [
                    {"backlog_id": "B-1"},
                    "B-2",
                ],
            },
            "backlog_item": {"backlog_id": "B-3"},
        }
        proposals = {
            "backlog_writes": [
                {"backlog_id": "B-1", "artifact": "./shared_workspace/backlog-B-1.json"},
                {"path": "./shared_workspace/backlog-B-2.json"},
                {"path": "./shared_workspace/backlog-B-2.json"},
            ],
        }

        self.assertEqual(
            collect_backlog_candidates_from_payload(payload),
            [{"backlog_id": "B-3"}, {"backlog_id": "B-1"}, "B-2"],
        )
        self.assertEqual(
            planner_backlog_write_receipts(proposals),
            [
                {"backlog_id": "B-1", "artifact_path": "./shared_workspace/backlog-B-1.json"},
                {"artifact_path": "./shared_workspace/backlog-B-2.json"},
            ],
        )


if __name__ == "__main__":
    unittest.main()
