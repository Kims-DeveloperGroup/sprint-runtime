from __future__ import annotations

import unittest

from teams_runtime.workflows.roles.planner import (
    build_planner_role_rules,
    normalize_planner_backlog_candidate,
    normalize_planner_backlog_write,
    normalize_planner_proposals,
)


class TeamsRuntimePlannerRoleTests(unittest.TestCase):
    def test_normalize_planner_backlog_candidate_coerces_defaults(self):
        candidate = normalize_planner_backlog_candidate(
            {
                "scope": "manual sprint todo finalization",
                "summary": "",
                "kind": "new-feature",
                "acceptance_criteria": "invalid",
                "origin": None,
            }
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["title"], "manual sprint todo finalization")
        self.assertEqual(candidate["summary"], "manual sprint todo finalization")
        self.assertEqual(candidate["scope"], "manual sprint todo finalization")
        self.assertEqual(candidate["kind"], "feature")
        self.assertEqual(candidate["acceptance_criteria"], ["invalid"])
        self.assertEqual(candidate["origin"], {})

    def test_normalize_planner_backlog_write_prefers_canonical_fields(self):
        receipt = normalize_planner_backlog_write(
            {
                "backlog_id": "backlog-1",
                "path": "./.teams_runtime/backlog/backlog-1.json",
                "fields": ["priority_rank", "status"],
                "status": "UPDATED",
            }
        )

        self.assertEqual(
            receipt,
            {
                "backlog_id": "backlog-1",
                "artifact_path": "./.teams_runtime/backlog/backlog-1.json",
                "status": "updated",
                "changed_fields": ["priority_rank", "status"],
            },
        )

    def test_normalize_planner_proposals_normalizes_aliases_and_dedupes(self):
        proposals, notes = normalize_planner_proposals(
            {
                "backlog_item": {
                    "title": "slice A",
                    "summary": "slice A",
                    "kind": "feature",
                },
                "planned_backlog_updates": [
                    {
                        "title": "slice A",
                        "summary": "slice A",
                        "kind": "feature",
                    },
                    {
                        "title": "slice B",
                        "summary": "slice B",
                        "kind": "enhancement",
                    },
                ],
                "backlog_write": {
                    "backlog_id": "backlog-1",
                    "artifact": "./shared_workspace/backlog.md",
                },
                "backlog_writes": [
                    {
                        "backlog_id": "backlog-1",
                        "artifact_path": "./shared_workspace/backlog.md",
                    },
                    {
                        "backlog_id": "backlog-2",
                        "artifact_path": "./.teams_runtime/backlog/backlog-2.json",
                    },
                ],
            }
        )

        self.assertEqual([item["title"] for item in proposals["backlog_items"]], ["slice A", "slice B"])
        self.assertEqual(proposals["backlog_item"]["title"], "slice A")
        self.assertEqual(len(proposals["backlog_writes"]), 2)
        self.assertEqual(proposals["backlog_write"]["backlog_id"], "backlog-1")
        self.assertIn("normalized planner backlog_item to backlog_items", notes)
        self.assertIn("normalized planner planned_backlog_updates to backlog_items", notes)
        self.assertIn("normalized planner backlog_write to backlog_writes", notes)

    def test_build_planner_role_rules_mentions_runtime_persistence_and_workflow(self):
        rules = build_planner_role_rules("./workspace/teams_generated")

        self.assertIn("proposals.backlog_writes", rules)
        self.assertIn("proposals.workflow_transition", rules)
        self.assertIn("sprint_closeout_report", rules)
        self.assertIn("./shared_workspace", rules)
        self.assertIn("Current request.designer_context", rules)
        self.assertIn("Preserve the designer's `lead / summary / defer` priority", rules)


if __name__ == "__main__":
    unittest.main()
