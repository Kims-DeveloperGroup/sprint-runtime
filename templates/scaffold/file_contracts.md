# Team File Contracts

- shared_workspace: shared planning, decision, and history docs
- shared_workspace/backlog.md: runtime-maintained active backlog list
- shared_workspace/completed_backlog.md: runtime-maintained completed backlog archive
- shared_workspace/current_sprint.md: runtime-maintained active sprint plan and todo list
- shared_workspace/sprints/<sprint_folder_name>/: sprint-id-keyed planning/spec/report folders
- shared_workspace/sprints/<sprint_folder_name>/kickoff.md: immutable kickoff brief, requirements, source request link, and kickoff reference docs
- shared_workspace/sprints/<sprint_folder_name>/attachments/<attachment_id>_<filename>: inbound Discord attachments and sprint-start reference docs
- shared_workspace/sprints/<sprint_id>/research/<request_id>.md: research raw report artifact written before planner when external grounding is needed
- shared_workspace/sprint_history/: archived sprint reports and todo history
- .teams_runtime/requests/<request_id>.json: canonical request record including the latest role result
- <role>/todo.md: runtime-maintained current task list for open requests
- <role>/history.md: runtime-appended execution history
- <role>/journal.md: runtime-appended personal insights plus notable notes and failures
- <role>/sources/: role-private reference files plus runtime-written request snapshots like `sources/<request_id>.request.md`
- <role>/workspace_manifest.json: agent profile and permissions
- internal/parser/: internal semantic intent-classification workspace used by orchestrator
- internal/sourcer/: internal backlog sourcing workspace used by orchestrator
- internal/version_controller/: internal commit-management workspace used by orchestrator
