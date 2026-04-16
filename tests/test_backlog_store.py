from __future__ import annotations

import tempfile
import unittest

from teams_runtime.core.backlog_store import build_backlog_fingerprint, merge_backlog_payload
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.persistence import read_json, write_json
from teams_runtime.core.sprints import build_backlog_item
from teams_runtime.core.template import scaffold_workspace


class TeamsRuntimeBacklogStoreTests(unittest.TestCase):
    def test_merge_backlog_payload_creates_backlog_item_from_canonical_payload(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)

            result = merge_backlog_payload(
                workspace_root=tmpdir,
                payload={
                    "backlog_item": {
                        "title": "relay failure retry policy",
                        "scope": "relay failure retry policy",
                        "summary": "Define retry and backoff handling for relay delivery failures.",
                        "kind": "chore",
                        "acceptance_criteria": ["Retry policy is documented and backlog tracked."],
                    }
                },
                default_source="planner",
                source_request_id="req-1",
            )

            self.assertEqual(result["added"], 1)
            self.assertEqual(result["updated"], 0)
            self.assertEqual(len(result["items"]), 1)
            backlog_id = result["items"][0]["backlog_id"]
            payload = read_json(paths.backlog_file(backlog_id))
            self.assertEqual(payload["title"], "relay failure retry policy")
            self.assertEqual(payload["origin"]["request_id"], "req-1")
            self.assertIn("relay failure retry policy", paths.shared_backlog_file.read_text(encoding="utf-8"))

    def test_merge_backlog_payload_updates_existing_item_by_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            existing = build_backlog_item(
                title="relay failure retry policy",
                summary="Initial summary.",
                kind="chore",
                source="planner",
                scope="relay failure retry policy",
            )
            existing["fingerprint"] = build_backlog_fingerprint(
                title=existing["title"],
                scope=existing["scope"],
                kind=existing["kind"],
            )
            write_json(paths.backlog_file(existing["backlog_id"]), existing)

            result = merge_backlog_payload(
                workspace_root=tmpdir,
                payload={
                    "backlog_item": {
                        "title": "relay failure retry policy",
                        "scope": "relay failure retry policy",
                        "summary": "Updated summary.",
                        "kind": "chore",
                        "priority_rank": 3,
                        "status": "done",
                    }
                },
                default_source="planner",
                source_request_id="req-2",
            )

            self.assertEqual(result["added"], 0)
            self.assertEqual(result["updated"], 1)
            updated = read_json(paths.backlog_file(existing["backlog_id"]))
            self.assertEqual(updated["summary"], "Updated summary.")
            self.assertEqual(updated["priority_rank"], 3)
            self.assertEqual(updated["status"], "done")
            self.assertIn("relay failure retry policy", paths.shared_completed_backlog_file.read_text(encoding="utf-8"))

    def test_merge_backlog_payload_ignores_legacy_proposal_aliases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)

            result = merge_backlog_payload(
                workspace_root=tmpdir,
                payload={
                    "proposals": {
                        "planned_backlog_updates": [
                            {
                                "title": "manual sprint todo finalization",
                                "scope": "manual sprint todo finalization",
                                "summary": "Persist the finalized sprint-ready backlog selection.",
                                "kind": "feature",
                                "milestone_title": "workflow initial",
                                "priority_rank": 2,
                                "planned_in_sprint_id": "260410-Sprint-16:32",
                            }
                        ]
                    }
                },
                default_source="planner",
                source_request_id="req-2b",
            )

            self.assertEqual(result["added"], 0)
            self.assertEqual(result["updated"], 0)
            self.assertEqual(result["items"], [])
            self.assertFalse(paths.backlog_dir.exists())

    def test_merge_backlog_payload_clears_blockers_when_item_reopens_to_pending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            existing = build_backlog_item(
                title="blocked backlog",
                summary="Need input.",
                kind="enhancement",
                source="planner",
                scope="blocked backlog",
            )
            existing["status"] = "blocked"
            existing["blocked_reason"] = "Missing dependency."
            existing["blocked_by_role"] = "planner"
            existing["required_inputs"] = ["dependency"]
            existing["recommended_next_step"] = "Resolve dependency."
            existing["fingerprint"] = build_backlog_fingerprint(
                title=existing["title"],
                scope=existing["scope"],
                kind=existing["kind"],
            )
            write_json(paths.backlog_file(existing["backlog_id"]), existing)

            result = merge_backlog_payload(
                workspace_root=tmpdir,
                payload={
                    "backlog_item": {
                        "title": "blocked backlog",
                        "scope": "blocked backlog",
                        "summary": "Dependency resolved.",
                        "kind": "enhancement",
                        "status": "pending",
                    }
                },
                default_source="planner",
                source_request_id="req-3",
            )

            self.assertEqual(result["updated"], 1)
            updated = read_json(paths.backlog_file(existing["backlog_id"]))
            self.assertEqual(updated["status"], "pending")
            self.assertEqual(updated["blocked_reason"], "")
            self.assertEqual(updated["blocked_by_role"], "")
            self.assertEqual(updated["required_inputs"], [])
            self.assertEqual(updated["recommended_next_step"], "")
