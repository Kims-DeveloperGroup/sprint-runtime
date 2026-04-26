---
name: sprint_orchestration
description: Use this skill inside the orchestrator agent workspace when the task is to operate sprint lifecycle commands, interpret sprint-control requests, and keep sprint state changes on the shared lifecycle backend instead of manual file edits.
---

# Sprint Orchestration Skill

## When To Use

Use this skill when orchestrator is managing active sprint flow, especially for:

- starting a manual sprint from a milestone
- stopping or wrapping up the active sprint
- restarting or resuming an interrupted sprint
- answering sprint status questions from persisted state
- deciding which lifecycle command the current request actually implies
- verifying the persisted sprint result after a lifecycle command runs

Do not use this skill for backlog decomposition or implementation planning that belongs to planner.

## Read First

- `Current request`
- `shared_workspace/current_sprint.md`
- `shared_workspace/sprints/`
- `.teams_runtime/sprints/<sprint_id>.json`
- `.teams_runtime/sprint_scheduler.json`

## Workflow

1. Confirm sprint state.
   Identify whether there is an active or resumable sprint, what phase it is in, and whether the user is asking for `start`, `stop`, `restart`, or `status`.
2. Use the lifecycle surface.
   Use the shared sprint lifecycle CLI as the primary path. Read `./workspace_context.md` if you need the exact team workspace root, then run one of:
   - `python -m teams_runtime sprint start --workspace-root <team_workspace_root> --milestone "..." [--brief "..."] [--requirement "..."] ...`
   - when the current request already has saved sprint doc paths or a canonical request id, also pass `--artifact "..."` and `--source-request-id "..."` so kickoff context is preserved through planner-owned sprint setup
   - `python -m teams_runtime sprint stop --workspace-root <team_workspace_root>`
   - `python -m teams_runtime sprint restart --workspace-root <team_workspace_root>`
   - `python -m teams_runtime sprint status --workspace-root <team_workspace_root>`
3. Let the backend own mutations.
   Do not manually rewrite sprint JSON, scheduler JSON, or shared sprint docs from the skill itself.
4. Verify persisted outcome.
   After the lifecycle command runs, read the persisted sprint state again and make sure the reported phase/status matches reality.
5. Keep the next control action clear.
   Say whether the sprint is waiting on planning, executing todos, wrapping up, blocked, or has no resumable state.
6. Report the outcome simply.
   For user-facing summaries, lead with the effective sprint state. Do not echo raw commands, file paths, or verification notes unless the user asked for details.

## Guardrails

- Do not start execution before prioritized todo selection exists.
- Do not run more than one active sprint at a time.
- Do not treat wrap-up as a normal admission window for new todo execution.
- Do not edit sprint state files directly when a lifecycle command/backend can perform the change.
- Do not reduce a detailed sprint kickoff request to title-only CLI input when the user already supplied requirements, notes, request id context, or reference artifacts.
