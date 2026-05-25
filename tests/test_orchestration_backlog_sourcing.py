from orchestration_test_utils import *


class TeamsRuntimeOrchestrationGoalSourcingTests(OrchestrationTestCase):
    def test_scheduler_active_goal_starts_goal_sourced_sprint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                asyncio.run(
                    service.start_goal_lifecycle(
                        objective="Deliver the reporting workflow",
                        stop_condition="Report workflow ships with regression coverage.",
                    )
                )

                with (
                    patch.object(service, "_resume_active_sprint", new=AsyncMock()),
                    patch.object(
                        service.goal_sourcer,
                        "source",
                        return_value={
                            "status": "sourced",
                            "summary": "Start the report archive milestone.",
                            "next_milestone": {
                                "title": "Report archive milestone",
                                "summary": "Create archive and publish flow.",
                                "requirements": ["archive final report"],
                                "completion_condition": "Final report is posted to the report channel.",
                                "artifacts": ["docs/specification.md"],
                            },
                        },
                    ) as source_mock,
                ):
                    asyncio.run(service._poll_scheduler_once())

                self.assertTrue(source_mock.called)
                scheduler_state = service._load_scheduler_state()
                sprint_id = scheduler_state["active_sprint_id"]
                sprint_state = read_json(service.paths.sprint_file(sprint_id))
                self.assertEqual(sprint_state["trigger"], "goal_sourcer")
                self.assertEqual(sprint_state["execution_mode"], "goal_sourced")
                self.assertEqual(sprint_state["goal_objective"], "Deliver the reporting workflow")
                self.assertEqual(sprint_state["goal_stop_condition"], "Report workflow ships with regression coverage.")
                self.assertEqual(sprint_state["milestone_title"], "Report archive milestone")
                self.assertEqual(
                    sprint_state["kickoff_requirements"],
                    [
                        "archive final report",
                        "Completion condition: Final report is posted to the report channel.",
                    ],
                )
                self.assertTrue(
                    any(
                        str(item.get("text") or "") == "Completion condition: Final report is posted to the report channel."
                        for item in sprint_state["original_requirements"]
                    )
                )

                goal_state = service._load_current_goal()
                self.assertEqual(goal_state["linked_sprint_ids"], [sprint_id])
                self.assertEqual(goal_state["sourced_milestones"][0]["sprint_id"], sprint_id)
                self.assertEqual(goal_state["sourced_milestones"][0]["requirements"], sprint_state["kickoff_requirements"])

    def test_goal_sourcing_is_suppressed_by_active_or_paused_goal_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                asyncio.run(service.start_goal_lifecycle(objective="Finish objective"))

                asyncio.run(
                    service.start_sprint_lifecycle(
                        "Manual active sprint",
                        resume_mode="none",
                    )
                )
                with (
                    patch.object(service, "_resume_active_sprint", new=AsyncMock()),
                    patch.object(service.goal_sourcer, "source") as source_mock,
                ):
                    asyncio.run(service._poll_scheduler_once())
                self.assertFalse(source_mock.called)

                scheduler_state = service._load_scheduler_state()
                sprint_id = scheduler_state["active_sprint_id"]
                scheduler_state["active_sprint_id"] = ""
                service._save_scheduler_state(scheduler_state)
                sprint_state = read_json(service.paths.sprint_file(sprint_id))
                sprint_state["status"] = "completed"
                service._save_sprint_state(sprint_state)

                asyncio.run(service.stop_goal_lifecycle(resume_mode="none"))
                with patch.object(service.goal_sourcer, "source") as paused_source_mock:
                    self.assertFalse(asyncio.run(service._poll_goal_sourcing_once()))
                self.assertFalse(paused_source_mock.called)

    def test_scheduler_completed_goal_archives_and_publishes_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                asyncio.run(
                    service.start_goal_lifecycle(
                        objective="Deliver the reporting workflow",
                        stop_condition="All report requirements are satisfied.",
                    )
                )
                goal_id = service._load_current_goal()["goal_id"]

                with patch.object(
                    service.goal_sourcer,
                    "source",
                    return_value={
                        "status": "completed",
                        "summary": "Goal is satisfied.",
                        "completion_decision": {
                            "completed": True,
                            "reason": "All milestones closed.",
                            "evidence": ["final sprint completed"],
                        },
                    },
                ):
                    asyncio.run(service._poll_scheduler_once())

                completed_goal = read_json(service.paths.goal_file(goal_id))
                self.assertEqual(completed_goal["status"], "completed")
                self.assertEqual(service._load_current_goal(), {})
                self.assertTrue(Path(completed_goal["final_report_path"]).exists())
                self.assertTrue(Path(completed_goal["final_report_archive_path"]).exists())
                self.assertEqual(service.discord_client.sent_channels[0][0], service.discord_config.report_channel_id)
                self.assertIn("Goal Final Report", service.discord_client.sent_channels[0][1])

    def test_cancel_goal_archives_report_and_requests_linked_sprint_wrapup(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                asyncio.run(
                    service.start_goal_lifecycle(
                        objective="Deliver the reporting workflow",
                        stop_condition="Report workflow is complete.",
                    )
                )
                goal_state = service._load_current_goal()

                asyncio.run(
                    service.start_sprint_lifecycle(
                        "Report archive milestone",
                        resume_mode="none",
                        goal_metadata={
                            "goal_id": goal_state["goal_id"],
                            "goal_objective": goal_state["objective"],
                            "goal_stop_condition": goal_state["stop_condition"],
                            "goal_sourcing_summary": "Start the next milestone.",
                        },
                    )
                )
                sprint_id = service._load_scheduler_state()["active_sprint_id"]

                message = asyncio.run(service.cancel_goal_lifecycle(resume_mode="none"))

                cancelled_goal = read_json(service.paths.goal_file(goal_state["goal_id"]))
                sprint_state = read_json(service.paths.sprint_file(sprint_id))
                self.assertEqual(cancelled_goal["status"], "cancelled")
                self.assertEqual(service._load_current_goal(), {})
                self.assertTrue(sprint_state["wrap_up_requested_at"])
                self.assertTrue(Path(cancelled_goal["final_report_path"]).exists())
                self.assertIn("published=true", message)
                self.assertEqual(service.discord_client.sent_channels[0][0], service.discord_config.report_channel_id)

    def test_goal_linked_sprint_closeout_updates_goal_history(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")
                asyncio.run(
                    service.start_goal_lifecycle(
                        objective="Deliver the reporting workflow",
                        stop_condition="Report workflow is complete.",
                    )
                )
                goal_state = service._load_current_goal()
                sprint_state = service._build_manual_sprint_state(
                    milestone_title="Report archive milestone",
                    trigger="goal_sourcer",
                )
                sprint_state["goal_id"] = goal_state["goal_id"]
                sprint_state["execution_mode"] = "goal_sourced"
                sprint_state["status"] = "completed"
                sprint_state["closeout_status"] = "committed"
                service._save_sprint_state(sprint_state)

                service._record_goal_sprint_outcome(
                    sprint_state,
                    {"status": "committed", "message": "Report workflow committed."},
                )

                updated_goal = read_json(service.paths.goal_file(goal_state["goal_id"]))
                self.assertEqual(updated_goal["linked_sprint_ids"], [sprint_state["sprint_id"]])
                self.assertEqual(updated_goal["sprint_outcomes"][0]["sprint_id"], sprint_state["sprint_id"])
                self.assertEqual(updated_goal["sprint_outcomes"][0]["closeout_status"], "committed")

    def test_backlog_sourcing_service_surface_is_removed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            with patch("teams_runtime.core.orchestration.DiscordClient", FakeDiscordClient):
                service = TeamService(tmpdir, "orchestrator")

                self.assertFalse(hasattr(service, "backlog_sourcer"))
                self.assertFalse(hasattr(service, "_poll_backlog_sourcing_once"))
                self.assertFalse(hasattr(service, "_perform_backlog_sourcing"))
                self.assertFalse(hasattr(service, "_queue_sourcer_candidates_for_planner_review"))
