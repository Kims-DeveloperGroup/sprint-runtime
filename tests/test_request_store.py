from __future__ import annotations

import tempfile
import unittest

from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.workflows.state.request_store import (
    append_request_event,
    build_blocked_backlog_review_request_record,
    build_sourcer_review_request_record,
    find_open_blocked_backlog_review_request,
    find_open_sourcer_review_request,
    is_blocked_backlog_review_request,
    is_internal_sprint_request,
    is_planner_backlog_review_request,
    is_sourcer_review_request,
    is_terminal_internal_request_status,
    is_terminal_request,
    iter_request_records,
    iter_sprint_task_request_records,
    load_request,
    save_request,
)


class TeamsRuntimeRequestStoreTests(unittest.TestCase):
    def test_save_and_load_request_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            request_record = {
                "request_id": "req-1",
                "status": "queued",
                "intent": "route",
                "scope": "request store extraction",
                "body": "Persist request state via helper.",
                "params": {},
                "artifacts": [],
                "events": [],
                "result": {},
            }

            save_request(paths, request_record)

            loaded = load_request(paths, "req-1")
            self.assertEqual(loaded["request_id"], "req-1")
            self.assertEqual(loaded["status"], "queued")
            self.assertTrue(loaded["updated_at"])

    def test_iter_request_records_returns_saved_requests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)

            save_request(paths, {"request_id": "req-a", "status": "queued", "events": [], "result": {}})
            save_request(paths, {"request_id": "req-b", "status": "completed", "events": [], "result": {}})

            request_ids = {record["request_id"] for record in iter_request_records(paths)}
            self.assertEqual(request_ids, {"req-a", "req-b"})

    def test_append_request_event_updates_record_and_timestamp(self):
        record = {"request_id": "req-1", "status": "queued", "events": []}

        append_request_event(
            record,
            event_type="delegated",
            actor="orchestrator",
            summary="planner delegated",
            payload={"next_role": "planner"},
        )

        self.assertEqual(record["events"][0]["type"], "delegated")
        self.assertEqual(record["events"][0]["payload"]["next_role"], "planner")
        self.assertTrue(record["updated_at"])

    def test_is_terminal_request_matches_terminal_statuses(self):
        self.assertTrue(is_terminal_request({"status": "completed"}))
        self.assertFalse(is_terminal_request({"status": "queued"}))

    def test_planner_review_request_predicates_match_request_kind(self):
        sourcer_review = {"params": {"_teams_kind": "sourcer_review"}}
        blocked_review = {"params": {"_teams_kind": "blocked_backlog_review"}}
        sprint_internal = {"params": {"_teams_kind": "sprint_internal"}}
        other = {"params": {"_teams_kind": "sprint_internal"}}

        self.assertTrue(is_sourcer_review_request(sourcer_review))
        self.assertTrue(is_blocked_backlog_review_request(blocked_review))
        self.assertTrue(is_internal_sprint_request(sprint_internal))
        self.assertTrue(is_planner_backlog_review_request(sourcer_review))
        self.assertTrue(is_planner_backlog_review_request(blocked_review))
        self.assertFalse(is_planner_backlog_review_request(other))
        self.assertTrue(is_terminal_internal_request_status("blocked"))
        self.assertFalse(is_terminal_internal_request_status("queued"))

    def test_find_open_review_requests_by_fingerprint_skips_terminal_records(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            save_request(
                paths,
                {
                    "request_id": "sourcer-open",
                    "status": "queued",
                    "fingerprint": "fp-sourcer",
                    "params": {"_teams_kind": "sourcer_review"},
                },
            )
            save_request(
                paths,
                {
                    "request_id": "blocked-open",
                    "status": "queued",
                    "fingerprint": "fp-blocked",
                    "params": {"_teams_kind": "blocked_backlog_review"},
                },
            )
            save_request(
                paths,
                {
                    "request_id": "sourcer-terminal",
                    "status": "blocked",
                    "fingerprint": "fp-terminal",
                    "params": {"_teams_kind": "sourcer_review"},
                },
            )

            self.assertEqual(
                find_open_sourcer_review_request(paths, "fp-sourcer")["request_id"],
                "sourcer-open",
            )
            self.assertEqual(
                find_open_blocked_backlog_review_request(paths, "fp-blocked")["request_id"],
                "blocked-open",
            )
            self.assertEqual(find_open_sourcer_review_request(paths, "fp-terminal"), {})

    def test_iter_sprint_task_request_records_filters_internal_sprint_tasks(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            save_request(
                paths,
                {
                    "request_id": "req-2",
                    "status": "queued",
                    "sprint_id": "sprint-1",
                    "backlog_id": "backlog-2",
                    "created_at": "2026-04-21T02:00:00Z",
                    "params": {"_teams_kind": "sprint_internal"},
                },
            )
            save_request(
                paths,
                {
                    "request_id": "req-1",
                    "status": "queued",
                    "sprint_id": "sprint-1",
                    "todo_id": "todo-1",
                    "created_at": "2026-04-21T01:00:00Z",
                    "params": {"_teams_kind": "sprint_internal"},
                },
            )
            save_request(
                paths,
                {
                    "request_id": "req-no-task",
                    "status": "queued",
                    "sprint_id": "sprint-1",
                    "created_at": "2026-04-21T03:00:00Z",
                    "params": {"_teams_kind": "sprint_internal"},
                },
            )
            save_request(
                paths,
                {
                    "request_id": "req-other-kind",
                    "status": "queued",
                    "sprint_id": "sprint-1",
                    "backlog_id": "backlog-3",
                    "created_at": "2026-04-21T04:00:00Z",
                    "params": {"_teams_kind": "sourcer_review"},
                },
            )

            records = iter_sprint_task_request_records(paths, "sprint-1")

            self.assertEqual([record["request_id"] for record in records], ["req-1", "req-2"])

    def test_build_sourcer_review_request_record_sets_planner_review_contract(self):
        record = build_sourcer_review_request_record(
            request_id="req-sourcer",
            candidates=[{"title": "candidate"}],
            sourcing_activity={"mode": "internal_sourcer", "summary": "found one"},
            artifact_hint="shared_workspace/sourcer_reviews/req-sourcer.md",
            sprint_id="260421-Sprint-12:00",
            fingerprint="fp-sourcer",
        )

        self.assertEqual(record["request_id"], "req-sourcer")
        self.assertEqual(record["params"]["_teams_kind"], "sourcer_review")
        self.assertEqual(record["params"]["candidate_count"], 1)
        self.assertEqual(record["artifacts"], ["shared_workspace/sourcer_reviews/req-sourcer.md"])
        self.assertEqual(record["sprint_id"], "260421-Sprint-12:00")
        self.assertEqual(record["fingerprint"], "fp-sourcer")
        self.assertEqual(record["events"][0]["type"], "created")

    def test_build_blocked_backlog_review_request_record_sets_planner_review_contract(self):
        record = build_blocked_backlog_review_request_record(
            request_id="req-blocked",
            candidates=[{"backlog_id": "backlog-1"}],
            artifact_hint="shared_workspace/blocked_backlog_reviews/req-blocked.md",
            fingerprint="fp-blocked",
        )

        self.assertEqual(record["request_id"], "req-blocked")
        self.assertEqual(record["params"]["_teams_kind"], "blocked_backlog_review")
        self.assertEqual(record["params"]["candidate_count"], 1)
        self.assertEqual(record["artifacts"], ["shared_workspace/blocked_backlog_reviews/req-blocked.md"])
        self.assertEqual(record["sprint_id"], "")
        self.assertEqual(record["fingerprint"], "fp-blocked")
        self.assertEqual(record["events"][0]["actor"], "orchestrator")
