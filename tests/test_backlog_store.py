from __future__ import annotations

import tempfile
import unittest

from teams_runtime.workflows.state.backlog_store import (
    backlog_status_counts,
    backlog_status_report_context,
    apply_backlog_state_from_todo,
    build_backlog_fingerprint,
    build_blocked_backlog_review_fingerprint,
    build_sprint_selected_backlog_item,
    build_sourcer_candidate_trace_fingerprint,
    build_sourcer_review_fingerprint,
    classify_backlog_kind,
    clear_backlog_blockers,
    desired_backlog_status_for_todo,
    drop_non_actionable_backlog_items,
    fallback_backlog_candidates_from_findings,
    is_actionable_backlog_status,
    is_active_backlog_status,
    is_non_actionable_backlog_item,
    is_reusable_backlog_status,
    load_backlog_item,
    merge_backlog_payload,
    normalize_blocked_backlog_review_candidates,
    normalize_backlog_acceptance_criteria,
    normalize_sourcer_review_candidates,
    render_blocked_backlog_review_markdown,
    repair_non_actionable_carry_over_backlog_items,
    render_sourcer_review_markdown,
    save_backlog_item,
)
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.shared.persistence import read_json, write_json
from teams_runtime.core.sprints import build_backlog_item
from teams_runtime.core.template import scaffold_workspace


class TeamsRuntimeBacklogStoreTests(unittest.TestCase):
    def test_backlog_classification_and_acceptance_normalization_helpers(self):
        self.assertEqual(classify_backlog_kind("fix", "runtime error", ""), "bug")
        self.assertEqual(classify_backlog_kind("new feature", "planner handoff", ""), "feature")
        self.assertEqual(classify_backlog_kind("", "docs cleanup", "문서 정리"), "chore")
        self.assertEqual(classify_backlog_kind("", "workflow routing", "tune scoring"), "enhancement")
        self.assertEqual(normalize_backlog_acceptance_criteria("done when tested"), ["done when tested"])
        self.assertEqual(normalize_backlog_acceptance_criteria([" a ", "", "b"]), ["a", "b"])

    def test_backlog_status_and_blocker_helpers(self):
        self.assertTrue(is_active_backlog_status("pending"))
        self.assertTrue(is_active_backlog_status("selected"))
        self.assertTrue(is_active_backlog_status("blocked"))
        self.assertFalse(is_active_backlog_status("done"))
        self.assertTrue(is_actionable_backlog_status("pending"))
        self.assertFalse(is_actionable_backlog_status("selected"))
        self.assertTrue(is_reusable_backlog_status("blocked"))

        item = {
            "blocked_reason": "missing input",
            "blocked_by_role": "planner",
            "required_inputs": ["input"],
            "recommended_next_step": "ask",
        }
        clear_backlog_blockers(item)
        self.assertEqual(item["blocked_reason"], "")
        self.assertEqual(item["blocked_by_role"], "")
        self.assertEqual(item["required_inputs"], [])
        self.assertEqual(item["recommended_next_step"], "")

        self.assertEqual(desired_backlog_status_for_todo({"status": "running"}), "selected")
        self.assertEqual(desired_backlog_status_for_todo({"status": "committed"}), "done")
        self.assertEqual(desired_backlog_status_for_todo({"status": "uncommitted"}), "blocked")
        self.assertEqual(desired_backlog_status_for_todo({"status": "failed"}), "carried_over")
        self.assertEqual(desired_backlog_status_for_todo({"status": "skipped"}), "")

    def test_apply_backlog_state_from_todo_updates_completion_and_blockers(self):
        item = {
            "status": "blocked",
            "selected_in_sprint_id": "",
            "completed_in_sprint_id": "",
            "blocked_reason": "missing input",
            "blocked_by_role": "planner",
            "required_inputs": ["input"],
            "recommended_next_step": "ask",
        }

        self.assertTrue(
            apply_backlog_state_from_todo(
                item,
                todo={"status": "completed"},
                sprint_id="sprint-1",
            )
        )

        self.assertEqual(item["status"], "done")
        self.assertEqual(item["selected_in_sprint_id"], "sprint-1")
        self.assertEqual(item["completed_in_sprint_id"], "sprint-1")
        self.assertEqual(item["blocked_reason"], "")
        self.assertEqual(item["blocked_by_role"], "")
        self.assertEqual(item["required_inputs"], [])
        self.assertEqual(item["recommended_next_step"], "")

        blocked_item = {
            "status": "selected",
            "selected_in_sprint_id": "sprint-1",
            "completed_in_sprint_id": "sprint-1",
        }

        self.assertTrue(
            apply_backlog_state_from_todo(
                blocked_item,
                todo={"status": "uncommitted", "summary": "git commit failed"},
                sprint_id="sprint-1",
            )
        )

        self.assertEqual(blocked_item["status"], "blocked")
        self.assertEqual(blocked_item["selected_in_sprint_id"], "")
        self.assertEqual(blocked_item["completed_in_sprint_id"], "")
        self.assertEqual(blocked_item["blocked_reason"], "git commit failed")
        self.assertEqual(blocked_item["blocked_by_role"], "version_controller")
        self.assertEqual(
            blocked_item["recommended_next_step"],
            "version_controller recovery 또는 수동 git 정리가 필요합니다.",
        )

    def test_build_sprint_selected_backlog_item_merges_todo_state_for_sprint_view(self):
        selected = build_sprint_selected_backlog_item(
            "backlog-1",
            selected_item={
                "backlog_id": "backlog-1",
                "title": "Existing backlog",
                "status": "blocked",
                "blocked_reason": "old",
            },
            todo={"status": "running"},
            sprint_id="sprint-1",
        )

        self.assertEqual(selected["status"], "selected")
        self.assertEqual(selected["selected_in_sprint_id"], "sprint-1")
        self.assertEqual(selected["completed_in_sprint_id"], "")
        self.assertEqual(selected["blocked_reason"], "")

        from_todo = build_sprint_selected_backlog_item(
            "backlog-2",
            todo={
                "title": "Recovered todo",
                "milestone_title": "Runtime cleanup",
                "priority_rank": 3,
                "acceptance_criteria": ["tests pass", ""],
                "status": "failed",
            },
            sprint_id="sprint-1",
        )

        self.assertEqual(from_todo["backlog_id"], "backlog-2")
        self.assertEqual(from_todo["title"], "Recovered todo")
        self.assertEqual(from_todo["acceptance_criteria"], ["tests pass"])
        self.assertEqual(from_todo["status"], "carried_over")
        self.assertEqual(from_todo["selected_in_sprint_id"], "")
        self.assertEqual(from_todo["completed_in_sprint_id"], "")

    def test_fallback_backlog_candidates_from_findings_preserves_origin_trace(self):
        candidates = fallback_backlog_candidates_from_findings(
            [
                {},
                {
                    "title": "Runtime log error",
                    "summary": "Investigate runtime log.",
                    "kind_hint": "bug",
                    "scope": "planner runtime",
                    "acceptance_criteria": "log checked",
                    "signal": "runtime_log_error",
                    "origin": {"role": "planner"},
                },
            ]
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["title"], "Runtime log error")
        self.assertEqual(candidates[0]["kind"], "bug")
        self.assertEqual(candidates[0]["source"], "sourcer")
        self.assertEqual(candidates[0]["acceptance_criteria"], ["log checked"])
        self.assertEqual(candidates[0]["origin"]["sourcing_agent"], "fallback")
        self.assertEqual(candidates[0]["origin"]["signal"], "runtime_log_error")
        self.assertEqual(candidates[0]["origin"]["role"], "planner")

    def test_non_actionable_backlog_item_detects_discovery_followups_and_internal_requests(self):
        self.assertTrue(
            is_non_actionable_backlog_item(
                {
                    "title": "Market insight follow-up",
                    "source": "discovery",
                }
            )
        )
        self.assertTrue(
            is_non_actionable_backlog_item(
                {
                    "title": "Internal planning",
                    "source": "discovery",
                    "origin": {"request_id": "req-1"},
                },
                request_loader=lambda _request_id: {"params": {"_teams_kind": "sprint_internal"}},
            )
        )
        self.assertFalse(
            is_non_actionable_backlog_item(
                {
                    "title": "User follow-up",
                    "source": "planner",
                    "origin": {"request_id": "req-2"},
                },
                request_loader=lambda _request_id: {"params": {"_teams_kind": "sprint_internal"}},
            )
        )

    def test_sourcer_candidate_trace_fingerprint_filters_stable_origin_keys(self):
        fingerprint = build_sourcer_candidate_trace_fingerprint(
            {
                "origin": {
                    "request_id": " req-1 ",
                    "related_request_id": "req-2",
                    "signal": " runtime_log_error ",
                    "sourcer_summary": "ignored",
                    "irrelevant": "ignored",
                    "operation_id": [" op-2 ", "op-1", ""],
                }
            }
        )

        self.assertEqual(
            fingerprint,
            "operation_id=op-1,op-2|related_request_id=req-2|request_id=req-1|signal=runtime_log_error",
        )

    def test_sourcer_review_candidates_normalize_and_fingerprint_stably(self):
        normalized = normalize_sourcer_review_candidates(
            [
                {},
                {
                    "title": " Runtime routing ",
                    "scope": " Runtime routing ",
                    "summary": " Clarify route ownership ",
                    "kind": "Chore",
                    "acceptance_criteria": "documented",
                    "priority_rank": "2",
                    "origin": {"request_id": "req-1", "sourcer_summary": "ignored"},
                },
                {"title": "missing scope", "scope": ""},
            ]
        )

        self.assertEqual(len(normalized), 2)
        self.assertEqual(normalized[0]["title"], "Runtime routing")
        self.assertEqual(normalized[0]["kind"], "chore")
        self.assertEqual(normalized[0]["acceptance_criteria"], ["documented"])
        self.assertEqual(normalized[1]["scope"], "missing scope")

        reversed_fingerprint = build_sourcer_review_fingerprint(list(reversed(normalized)))
        self.assertEqual(build_sourcer_review_fingerprint(normalized), reversed_fingerprint)

        changed_trace = [dict(item) for item in normalized]
        changed_trace[0]["origin"] = {"request_id": "req-2"}
        self.assertNotEqual(build_sourcer_review_fingerprint(normalized), build_sourcer_review_fingerprint(changed_trace))

    def test_blocked_backlog_review_candidates_normalize_sort_and_fingerprint_stably(self):
        normalized = normalize_blocked_backlog_review_candidates(
            [
                {
                    "backlog_id": "backlog-2",
                    "title": "second",
                    "scope": "second",
                    "status": "blocked",
                    "priority_rank": 2,
                    "updated_at": "2026-04-02T00:00:00Z",
                    "required_inputs": "dependency",
                },
                {
                    "backlog_id": "ignored",
                    "title": "done",
                    "scope": "done",
                    "status": "done",
                },
                {
                    "backlog_id": "backlog-1",
                    "title": "first",
                    "scope": "first",
                    "status": "blocked",
                    "priority_rank": 1,
                    "updated_at": "2026-04-03T00:00:00Z",
                },
            ]
        )

        self.assertEqual([item["backlog_id"] for item in normalized], ["backlog-1", "backlog-2"])
        self.assertEqual(normalized[1]["required_inputs"], ["dependency"])
        self.assertEqual(
            build_blocked_backlog_review_fingerprint(normalized),
            build_blocked_backlog_review_fingerprint(list(reversed(normalized))),
        )

        changed_update = [dict(item) for item in normalized]
        changed_update[0]["updated_at"] = "2026-04-04T00:00:00Z"
        self.assertNotEqual(
            build_blocked_backlog_review_fingerprint(normalized),
            build_blocked_backlog_review_fingerprint(changed_update),
        )

    def test_review_markdown_renderers_include_candidate_context(self):
        sourcer_markdown = render_sourcer_review_markdown(
            request_id="req-1",
            candidates=[
                {
                    "title": "Runtime routing",
                    "kind": "chore",
                    "scope": "routing",
                    "summary": "Clarify route ownership.",
                    "acceptance_criteria": ["documented"],
                    "origin": {"request_id": "source-1"},
                }
            ],
            sourcing_activity={"summary": "New candidate found.", "mode": "internal_sourcer"},
        )

        self.assertIn("# Sourcer Backlog Review", sourcer_markdown)
        self.assertIn("- sourcer_summary: New candidate found.", sourcer_markdown)
        self.assertIn("- origin: request_id=source-1", sourcer_markdown)

        blocked_markdown = render_blocked_backlog_review_markdown(
            request_id="req-2",
            candidates=[
                {
                    "backlog_id": "backlog-1",
                    "title": "Blocked item",
                    "kind": "feature",
                    "scope": "runtime",
                    "summary": "Needs input.",
                    "blocked_reason": "Missing dependency",
                    "required_inputs": ["dependency"],
                    "recommended_next_step": "Ask owner.",
                }
            ],
        )

        self.assertIn("# Blocked Backlog Review", blocked_markdown)
        self.assertIn("- backlog_id: backlog-1", blocked_markdown)
        self.assertIn("- required_inputs:", blocked_markdown)

    def test_save_and_load_backlog_item_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            item = build_backlog_item(
                title="request store extraction",
                summary="Use a dedicated backlog store helper.",
                kind="chore",
                source="planner",
                scope="request store extraction",
            )

            save_backlog_item(paths, item)

            reloaded = load_backlog_item(paths, item["backlog_id"])
            self.assertEqual(reloaded["title"], "request store extraction")
            self.assertIn("request store extraction", paths.shared_backlog_file.read_text(encoding="utf-8"))

    def test_drop_non_actionable_backlog_items_marks_discovery_context_as_dropped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            item = build_backlog_item(
                title="Market insight follow-up",
                summary="Internal journal-only context.",
                kind="chore",
                source="discovery",
                scope="Market insight follow-up",
            )
            save_backlog_item(paths, item, refresh_markdown=False)

            dropped_ids = drop_non_actionable_backlog_items(paths)

            self.assertEqual(dropped_ids, {item["backlog_id"]})
            reloaded = load_backlog_item(paths, item["backlog_id"])
            self.assertEqual(reloaded["status"], "dropped")
            self.assertEqual(reloaded["selected_in_sprint_id"], "")
            self.assertEqual(reloaded["completed_in_sprint_id"], "")
            self.assertEqual(reloaded["dropped_reason"], "agent insight is journal-only context, not backlog work")

    def test_repair_non_actionable_carry_over_backlog_items_marks_pending_items_blocked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            item = build_backlog_item(
                title="Carry-over context",
                summary="Legacy carry-over item.",
                kind="chore",
                source="planner",
                scope="Carry-over context",
            )
            item["status"] = "pending"
            save_backlog_item(paths, item, refresh_markdown=False)
            write_json(
                paths.sprint_file("sprint-1"),
                {
                    "sprint_id": "sprint-1",
                    "todos": [
                        {
                            "status": "blocked",
                            "carry_over_backlog_id": item["backlog_id"],
                        }
                    ],
                },
            )

            repaired_ids = repair_non_actionable_carry_over_backlog_items(paths)

            self.assertEqual(repaired_ids, {item["backlog_id"]})
            reloaded = load_backlog_item(paths, item["backlog_id"])
            self.assertEqual(reloaded["status"], "blocked")
            self.assertEqual(reloaded["selected_in_sprint_id"], "")

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

    def test_backlog_status_report_context_counts_and_orders_active_items(self):
        items = [
            {"title": "done", "status": "done", "kind": "chore", "source": "planner", "priority_rank": 9},
            {"title": "pending low", "status": "pending", "kind": "feature", "source": "planner", "priority_rank": 1},
            {"title": "selected", "status": "selected", "kind": "bug", "source": "planner", "priority_rank": 1},
            {"title": "pending user", "status": "pending", "kind": "enhancement", "source": "user", "priority_rank": 5},
            {"title": "blocked", "status": "blocked", "kind": "chore", "source": "planner", "priority_rank": 10},
        ]

        context = backlog_status_report_context(items)

        self.assertEqual(
            backlog_status_counts(items),
            {"pending": 2, "selected": 1, "blocked": 1, "done": 1, "total": 4},
        )
        self.assertEqual(
            [item["title"] for item in context["active_items"]],
            ["selected", "pending user", "pending low", "blocked"],
        )
        self.assertEqual(context["kind_counts"]["bug"], 1)
        self.assertEqual(context["source_counts"]["user"], 1)
