from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path

from teams_runtime.adapters.cli.commands import (
    build_parser,
    cmd_goal_cancel_impl,
    cmd_goal_resume_impl,
    cmd_goal_start_impl,
    cmd_goal_status_impl,
    cmd_goal_stop_impl,
    dispatch_main,
)
from teams_runtime.core.orchestration import TeamService
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.shared.persistence import read_json
from teams_runtime.workflows.state.goal_store import (
    active_goal_id,
    archive_goal_final_report,
    cancel_goal,
    complete_goal,
    create_goal,
    iter_goal_events,
    load_active_goal,
    load_current_goal,
    pause_goal,
    record_goal_sprint_outcome,
    record_sourced_milestone,
    resume_goal,
    update_goal_stop_condition,
)


class TeamsRuntimeGoalStoreTests(unittest.TestCase):
    def test_goal_store_round_trip_events_history_and_completion_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            paths.ensure_runtime_dirs()

            goal = create_goal(paths, objective="Deliver report workflow")
            self.assertEqual(active_goal_id(paths), goal["goal_id"])
            self.assertEqual(load_active_goal(paths)["objective"], "Deliver report workflow")

            goal = update_goal_stop_condition(paths, goal, "Report workflow is complete.")
            goal = record_sourced_milestone(
                paths,
                goal,
                milestone_title="Report archive milestone",
                summary="Archive and publish final report.",
                sprint_id="260523-Sprint-10:00",
                requirements=["write final report"],
                artifacts=["docs/specification.md"],
            )
            goal = record_goal_sprint_outcome(
                paths,
                goal,
                {
                    "sprint_id": "260523-Sprint-10:00",
                    "milestone_title": "Report archive milestone",
                    "status": "completed",
                    "closeout_status": "committed",
                    "commit_shas": ["abc123"],
                },
                {"message": "Committed report workflow."},
            )
            goal = complete_goal(paths, goal, evidence={"reason": "done"})
            report = archive_goal_final_report(paths, goal, terminal_reason="done")

            persisted = read_json(paths.goal_file(goal["goal_id"]))
            self.assertEqual(persisted["status"], "completed")
            self.assertEqual(persisted["stop_condition"], "Report workflow is complete.")
            self.assertEqual(persisted["sourced_milestones"][0]["sprint_id"], "260523-Sprint-10:00")
            self.assertEqual(persisted["sprint_outcomes"][0]["closeout_status"], "committed")
            self.assertEqual(load_current_goal(paths), {})
            self.assertIn("Goal Final Report", report)
            self.assertTrue(paths.shared_goal_report_file(goal["goal_id"]).exists())
            self.assertTrue(paths.goal_archive_report_file(goal["goal_id"]).exists())
            self.assertIn("completed", [event["type"] for event in iter_goal_events(paths, goal["goal_id"])])

    def test_goal_store_stop_resume_cancel_transitions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            paths.ensure_runtime_dirs()

            goal = create_goal(
                paths,
                objective="Deliver report workflow",
                stop_condition="Report workflow is complete.",
            )
            goal = pause_goal(paths, goal, reason="operator stop")

            self.assertEqual(load_current_goal(paths)["status"], "paused")
            self.assertEqual(load_active_goal(paths), {})

            goal = resume_goal(paths, goal)
            self.assertEqual(load_active_goal(paths)["status"], "active")

            goal = cancel_goal(paths, goal, evidence={"reason": "operator cancel"})
            archive_goal_final_report(paths, goal, terminal_reason="operator cancel")

            self.assertEqual(load_current_goal(paths), {})
            self.assertEqual(active_goal_id(paths), "")
            persisted = read_json(paths.goal_file(goal["goal_id"]))
            self.assertEqual(persisted["status"], "cancelled")
            self.assertTrue(persisted["cancelled_at"])
            self.assertTrue(persisted["final_report_path"])

    def test_goal_cli_commands_drive_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            outputs: list[str] = []
            workspace_root = Path(tmpdir)

            self.assertEqual(
                cmd_goal_start_impl(
                    workspace_root,
                    objective="Deliver report workflow",
                    stop_condition="Report workflow is complete.",
                    team_service_cls=TeamService,
                    printer=outputs.append,
                ),
                0,
            )
            self.assertEqual(
                cmd_goal_status_impl(workspace_root, team_service_cls=TeamService, printer=outputs.append),
                0,
            )
            self.assertEqual(
                cmd_goal_stop_impl(workspace_root, team_service_cls=TeamService, printer=outputs.append),
                0,
            )
            self.assertEqual(
                cmd_goal_resume_impl(workspace_root, team_service_cls=TeamService, printer=outputs.append),
                0,
            )
            self.assertEqual(
                cmd_goal_cancel_impl(workspace_root, team_service_cls=TeamService, printer=outputs.append),
                0,
            )

            self.assertIn("goal started", outputs[0])
            self.assertIn("Goal Summary", outputs[1])
            self.assertIn("goal paused", outputs[2])
            self.assertIn("goal resumed", outputs[3])
            self.assertIn("goal cancelled", outputs[4])
            self.assertEqual(load_current_goal(RuntimePaths.from_root(tmpdir)), {})

    def test_goal_terminate_dispatches_to_cancel_alias(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            parser = build_parser(
                all_runtime_agents=["orchestrator"],
                internal_team_agents=["parser"],
                team_roles=["orchestrator"],
                relay_transport_internal="internal",
                relay_transport_discord="discord",
                default_relay_transport="internal",
                workspace_root_help_text="workspace",
            )
            args = parser.parse_args(["goal", "terminate", "--workspace-root", tmpdir])
            calls: list[tuple[str, Path]] = []

            def _noop(*_args, **_kwargs) -> int:
                return 0

            result = dispatch_main(
                args,
                workspace_root=Path(tmpdir),
                parser=argparse.ArgumentParser(),
                run_services=_noop,
                cmd_init=_noop,
                cmd_start=_noop,
                cmd_status=_noop,
                cmd_stop=_noop,
                cmd_restart=_noop,
                cmd_list=_noop,
                cmd_config_role_set=_noop,
                cmd_config_internal_set=_noop,
                cmd_config_research_set=_noop,
                cmd_sprint_start=_noop,
                cmd_sprint_stop=_noop,
                cmd_sprint_restart=_noop,
                cmd_sprint_status=_noop,
                cmd_goal_start=_noop,
                cmd_goal_status=_noop,
                cmd_goal_stop=_noop,
                cmd_goal_resume=_noop,
                cmd_goal_cancel=lambda root: calls.append(("cancel", root)) or 0,
                default_relay_transport="internal",
            )

            self.assertEqual(result, 0)
            self.assertEqual(calls, [("cancel", Path(tmpdir))])


if __name__ == "__main__":
    unittest.main()
