# `teams_runtime` Specification

## Purpose

`teams_runtime` is a standalone multi-agent Discord workflow package.

It is designed to be reusable across projects instead of being coupled to a single repository or workspace. A project provides a workspace root with config files and role workspaces; `teams_runtime` provides the orchestration, Discord runtime, session handling, and action execution surface.

## Roles

The runtime supports 6 fixed roles:

- `orchestrator`
- `planner`
- `designer`
- `architect`
- `developer`
- `qa`

### Role responsibilities

- `orchestrator`
  - Owns request intake, deduplication, bounded routing, sprint-state status mutations, final request state, and registered action execution
- `planner`
  - Produces planning and requirements-oriented outputs
- `designer`
  - Produces UX, message, and interaction-oriented outputs
- `architect`
  - Produces codebase/module overviews, implementation-ready technical specs/docs, senior technical direction, and structural reviews of developer changes
- `developer`
  - Produces implementation outputs for project changes
- `qa`
  - Produces regression findings, test coverage checks, and release-readiness assessments

## Workspace Model

The runtime operates on a workspace root.

### Default workspace resolution

If `--workspace-root` is omitted:

1. If the current directory already contains both `team_runtime.yaml` and `discord_agents_config.yaml`, use the current directory.
2. Otherwise use `./teams_generated`.

### Workspace files

Required root files:

- `team_runtime.yaml`
- `discord_agents_config.yaml`

Required root directories/files scaffolded by `init`:

- `communication_protocol.md`
- `file_contracts.md`
- `shared_workspace/`
- one directory for each role

Maintainer documentation is package-local under `teams_runtime/docs/` and is not copied into generated workspaces.

Role directory contents:

- `AGENTS.md`
- `GEMINI.md`
- `todo.md`
- `history.md`
- `journal.md`
- `workspace_manifest.json`

## Config Contracts

### `discord_agents_config.yaml`

Required fields:

- `relay_channel_id` or `relay_channel_env`
- `agents`

Optional fields:

- `startup_channel_id` or `startup_channel_env`
  - defaults to `relay_channel_id`
- `report_channel_id` or `report_channel_env`
  - defaults to `startup_channel_id`

Each role under `agents` must define:

- `name`
- `role`
- `description`
- `token_env`
- `bot_id`

### Bot ID rule

`bot_id` is mandatory and is the only source of truth for:

- relay-channel mentions
- trusted inter-bot message acceptance
- role-target detection from mentions

Runtime bot discovery is not used as an authority.

### Startup announcement channel

- Each role sends a startup message when its listener becomes ready.
- The target channel is `startup_channel_id` when configured.
- If omitted, the runtime uses `relay_channel_id`.

### User-facing report channel

- User-facing sprint completion summaries are sent to `report_channel_id`.
- The relay channel is reserved for relay monitoring and debug transport, not end-user sprint closeout summaries.
- Sprint completion may therefore emit two Discord messages:
  - an operational `[작업 보고]` to the startup channel
  - a readable user summary to the report channel

### Relay transport mode

Relay transport is selected at runtime through CLI flags:

- `python -m teams_runtime start --relay-transport {internal|discord}`
- `python -m teams_runtime run --relay-transport {internal|discord}`
- `python -m teams_runtime restart --relay-transport {internal|discord}`

Defaults:

- `internal` is the default relay transport
- `discord` is supported for relay debugging

### `team_runtime.yaml`

Required fields:

- `sprint.id`

Supported sections:

- `sprint`
  - `id`
  - `interval_minutes`
  - `timezone`
  - `mode`
  - `overlap_policy`
  - `ingress_mode`
  - `discovery_scope`
  - `discovery_actions`
- `ingress`
  - `dm`
  - `mentions`
- `allowed_guild_ids`
- `role_defaults`
- `actions`

## Backlog And Sprint Model

Normal change and enhancement requests are backlog-first.

- the orchestrator accepts the user request
- it delegates planning/backlog management to planner first
- planner decides whether backlog records should be created, updated, deduplicated, or reprioritized, and persists that backlog state directly
- planner backlog persistence uses canonical `backlog_items` / `backlog_item` helper inputs and returns `proposals.backlog_writes` receipts for the affected backlog records
- planner reasoning may still include `proposals.backlog_item` / `proposals.backlog_items`, but those are not persistence instructions for orchestrator
- orchestrator verifies planner `backlog_writes` receipts against persisted backlog state and must not re-persist planner backlog proposals on behalf of planner
- an internal non-public sourcer agent can independently propose new backlog candidates from workspace/runtime findings, but planner review is required before backlog persistence
- when a sprint starts, planner must first derive sprint-relevant backlog from the current milestone, kickoff requirements, and `spec.md`
- sprint start cannot proceed with `backlog 0건`; if sprint-relevant backlog is empty, the runtime blocks with `planning_incomplete`
- the scheduler later selects pending backlog items only after that initial-phase backlog-definition gate passes
- selected items become sprint todos and are executed through internal `request_id` records with a standard workflow contract

### Standard sprint workflow

Sprint-internal execution uses these fixed phases:

- `planning`
- `implementation`
- `validation`
- `closeout`

Workflow rules:

- `planner` is the sole final owner of planning output
- `designer` and `architect` are advisory specialists during planning
- planning advisory is capped at 2 shared passes total
- sprint initial planning follows `milestone_refinement -> artifact_sync -> backlog_definition -> backlog_prioritization -> todo_finalization`
- `backlog_definition` is mandatory and must persist sprint-relevant backlog before prioritization
- planning-only clarification on planner-owned surfaces such as `current_sprint.md`, `todo_backlog.md`, and `iteration_log.md` closes in planning instead of opening implementation
- planner가 planner-owned artifact만 보고하더라도 `workflow_transition.target_phase=implementation`을 명시하면 orchestrator는 planning close 대신 다음 implementation step을 열어야 함
- implementation follows the standard sequence:
  - `architect_guidance`
  - `developer_build`
  - `architect_review`
  - `developer_revision` (only when architect review leaves actionable revision work)
  - `qa_validation` (directly after `architect_review` when no developer revision is needed)
- planner-owned doc claims from `architect` or `developer` reopen the workflow to `planner_finalize` with `reopen_category='scope'`
- `qa` owns validation
- `version_controller` owns closeout
- roles do not directly choose the next role; orchestrator applies workflow policy from structured role output
- scenario-based routing diagrams are maintained in [`architecture.md`](./architecture.md#4-standard-workflow-contract)

### Backlog state

Backlog items are stored under:

- `.teams_runtime/backlog/<backlog_id>.json`
- `shared_workspace/backlog.md`
- `shared_workspace/completed_backlog.md`

Tracked fields include:

- `backlog_id`
- `title`
- `summary`
- `kind`
- `source`
- `scope`
- `status`
- `acceptance_criteria`
- `milestone_title`
- `priority_rank`
- `planned_in_sprint_id`
- `created_at`
- `selected_in_sprint_id`
- `completed_in_sprint_id`
- `origin.milestone_ref`
- `origin.requirement_refs`
- `origin.spec_refs`

`shared_workspace/backlog.md` contains non-`done` items for active tracking.
`shared_workspace/completed_backlog.md` contains only `done` items for archive-style reference.

### Sprint state

Sprint records are stored under:

- `.teams_runtime/sprints/<sprint_id>.json`
- `.teams_runtime/sprints/<sprint_id>.events.jsonl`
- `.teams_runtime/sprint_scheduler.json`

Human-readable sprint files are stored under:

- `shared_workspace/current_sprint.md`
- `shared_workspace/sprints/<sprint_folder_name>/`
- `shared_workspace/sprints/<sprint_folder_name>/kickoff.md`
- `shared_workspace/sprints/<sprint_folder_name>/attachments/<attachment_id>_<filename>`
- `shared_workspace/sprint_history/index.md`
- `shared_workspace/sprint_history/<sprint_id>.md`

The orchestrator is the only role that runs the autonomous scheduler.

### Sprint file source of truth

- `.teams_runtime/sprints/<sprint_id>.json` is the canonical sprint record.
- `.teams_runtime/sprints/<sprint_id>.events.jsonl` is the append-only activity log for that sprint.
- Human-readable sprint files under `shared_workspace/sprints/<sprint_folder_name>/` and `shared_workspace/sprint_history/` are derived views and must be regenerated from sprint state rather than edited independently.

### Sprint file update contract

For one sprint, the runtime maintains these derived files:

- `shared_workspace/sprints/<sprint_folder_name>/index.md`
- `shared_workspace/sprints/<sprint_folder_name>/kickoff.md`
- `shared_workspace/sprints/<sprint_folder_name>/milestone.md`
- `shared_workspace/sprints/<sprint_folder_name>/plan.md`
- `shared_workspace/sprints/<sprint_folder_name>/spec.md`
- `shared_workspace/sprints/<sprint_folder_name>/todo_backlog.md`
- `shared_workspace/sprints/<sprint_folder_name>/iteration_log.md`
- `shared_workspace/sprints/<sprint_folder_name>/report.md`
- `shared_workspace/sprint_history/<sprint_id>.md`
- `shared_workspace/sprint_history/index.md`

Update rules:

- Loading or saving sprint state may repair stale sprint data. If repair changes sprint todos, selected backlog snapshot, or report body, the runtime must rewrite all sprint-folder derived files from the repaired state.
- If the repaired sprint is already closed, the runtime must also refresh `shared_workspace/sprint_history/<sprint_id>.md` and `shared_workspace/sprint_history/index.md` from the repaired state.
- `report.md`, `shared_workspace/sprint_history/<sprint_id>.md`, and `shared_workspace/sprint_history/index.md` must agree on closeout status, todo count, todo summary, and linked artifacts for a closed sprint.
- Successful closeout archives must be written from the final persisted sprint status such as `completed`, not from a transient intermediate status.
- `report.md` is a human-first sprint summary. It must include user-readable sections such as overview, A-to-Z flow, agent contributions, core issues, achievements, and major artifacts.
- The bottom of `report.md` must still contain a machine-readable summary block with stable `key=value` fields such as `sprint_id`, `todo_status_counts`, `commit_count`, `linked_artifacts`, and `closeout_message` so repair logic can re-derive closeout metadata.

### Sprint state consistency rules

`selected_backlog_ids`, `selected_items`, and `todos` do not mean the same thing.

- `selected_backlog_ids`
  - contains only backlog IDs that are still live `selected` work in the sprint
- `selected_items`
  - contains the sprint-owned backlog snapshot
  - each item must reflect normalized backlog status such as `selected`, `blocked`, or `done`
- `todos`
  - contains sprint execution records, including committed or completed work that has already left the live selected set

Normalization rules:

- A `queued` or `running` todo maps the corresponding backlog item to `selected`
- A `completed` or `committed` todo maps the corresponding backlog item to `done`
- A `blocked` or `uncommitted` todo maps the corresponding backlog item to `blocked`
- A `failed` todo maps the corresponding backlog item to `carried_over`
- Closed sprint work must not remain in `selected_backlog_ids` after normalization even though it remains in `todos`

### `current_sprint.md` behavior

- `shared_workspace/current_sprint.md` is only the active-sprint view.
- When scheduler state still points at an active sprint, the file must mirror the current sprint JSON.
- When a sprint completes and `active_sprint_id` is cleared, `shared_workspace/current_sprint.md` intentionally switches to an idle placeholder such as `active sprint 없음`.
- The last completed sprint must be read from `shared_workspace/sprint_history/<sprint_id>.md` or the sprint folder, not from `current_sprint.md`.

### Sprint behavior

- default cadence is every 180 minutes
- default timezone is `Asia/Seoul`
- persisted runtime timestamps and scheduler times are recorded in KST ISO-8601 (`+09:00`)
- generated active sprint IDs use the local KST date/time form `YYMMDD-Sprint-HH:MM`, for example `260324-Sprint-09:00`
- sprint artifact folders use a filesystem-safe form of `sprint_id` under `shared_workspace/sprints/`
- sprint state preserves both a refined `milestone_title` and immutable kickoff source fields such as `kickoff_brief`, `kickoff_requirements`, `kickoff_request_text`, `kickoff_source_request_id`, and `kickoff_reference_artifacts`
- inbound Discord attachments are stored under the resolved sprint folder so sprint-start reference docs live with the sprint artifacts
- `kickoff.md` stores the original sprint-start brief/requirements/source request, while `milestone.md` stores derived milestone framing
- sprint-start attachments, generated todo artifacts, and linked code paths referenced by sprint reports are resolved relative to the sprint folder or workspace and should be preserved in linked-artifact views
- planner receives those saved attachment paths through `request.artifacts` and should use them as planning references before declaring missing context
- scheduler state and startup/report/status outputs expose only `active_sprint_id`, while sprint records keep a single `sprint_id`
- `hybrid` mode starts on schedule or earlier when backlog is ready
- `no_overlap` allows only one active sprint at a time
- sprint history and todo history are retained over time

## Request Model

Not every incoming message immediately becomes a new `request_id`.

### Request identity

- `backlog_id`
  - one backlog candidate / deferred work item
- `request_id`
  - one runtime request record
  - may identify either an intake/planner request or a sprint-internal execution request
- `sprint.id`
  - configured session-scope id used for role session reuse/refresh

### Canonical message envelope

Supported envelope fields:

- `request_id`
- `intent`
- `urgency`
- `scope`
- `artifacts`
- `params`

`action` is accepted as an alias of `intent`.
Role routing is inferred from the Discord bot mention and runtime route state. Legacy `from` and `to`
fields are still accepted on input for compatibility, but they are not required and are not emitted in
relay message text.

Freeform `cancel` and `status` commands are also supported.
Legacy `approve request_id:...` text is recognized only to return an unsupported response.

### Request state

Requests are stored under:

- `.teams_runtime/requests/<request_id>.json`

Tracked fields include:

- request metadata
- latest role result
- reply route
- current role
- next role
- `params.workflow` for sprint-internal workflow phase, step, pass count, reopen category, and owner state
- execution metadata
- event history

### Request ownership

- The `orchestrator` is the only request authority.
- Direct messages to non-orchestrator roles are forwarded to the orchestrator before work starts.
- Direct role messages for normal change work are still backlog-first unless they are explicit orchestrator-managed control commands such as `status`, `cancel`, sprint lifecycle, or action `execute`.

## Session Model

Each role has one active session per sprint.

### Session persistence

Stored under:

- `.teams_runtime/role_sessions/<role>.json`

Archived under:

- `.teams_runtime/archive/<old_sprint_id>/<role>/`

### Session behavior

- Same sprint: reuse the role session
- New sprint: archive old session metadata and create a fresh role session on next role task

### Session filesystem access

- Session workspaces expose `./workspace`, `./shared_workspace`, and `./.teams_runtime` as convenience links into the broader runtime/project workspace.
- Those links describe path layout, not automatic sandbox authority over the resolved target.
- Non-bypass Codex runs widen writable roots to the resolved targets for those linked directories when available.
- Developer requests run with sandbox bypass by default, and planner requests that persist runtime-owned sprint/backlog artifacts under `shared_workspace/` or `.teams_runtime/` may also run with sandbox bypass by default, so resumed role sessions are not blocked by sandbox boundaries when they need to write through runtime symlinks.

## Discord Behavior

### Ingress

- user DMs
- role mentions
- trusted relay-channel bot messages (for Discord relay-mode envelopes and relay summaries)

### Relay routing

- Relay kinds are `delegate`, `report`, and `forward`.
- In `internal` transport mode (default):
  - inter-role relay is delivered by internal direct handoff
  - relay envelopes are persisted under `.teams_runtime/internal_relay/inbox/<role>/` and archived under `.teams_runtime/internal_relay/archive/<role>/`
  - each relay emits a natural-language summary to the configured relay channel for monitoring
- In `discord` transport mode:
  - inter-role relay happens through the configured relay channel
  - outgoing relay messages mention the target bot with its configured `bot_id`
  - incoming relay messages are accepted only when authored by a configured team bot ID

## Execution Model

`teams_runtime` is project-agnostic.

It does not import project-specific CLIs or local helper libraries. Instead, executable work is defined through the action registry in `team_runtime.yaml`.

### Action registry

Each action defines:

- `command`
- `lifecycle`
  - `foreground`
  - `managed`
- `domain`
- `allowed_params`

### Empty action registry

If `actions: {}` remains empty:

- collaboration workflow still works
- `execute` requests are unavailable

## Planner vs Orchestrator Boundary

- planner owns planning, backlog-management decisions, and planner-initiated backlog persistence
- orchestrator owns sprint-state status mutations, workflow phase/step state, pass limits, reopen routing, and bounded execution routing

Sprint-state status mutations include:

- backlog execution-state transitions such as `selected`, `done`, `blocked`, and `carried_over`
- `selected_in_sprint_id` and `completed_in_sprint_id`
- blocker fields such as `blocked_reason`, `blocked_by_role`, `required_inputs`, and `recommended_next_step`
- todo lifecycle state such as `queued`, `running`, `completed`, `blocked`, and `failed`
- sprint lifecycle state such as `planning`, `running`, `wrap_up`, `completed`, `failed`, and `blocked`

## Testing Surface

Package-local tests live under:

- `teams_runtime/tests/`

Current test coverage focuses on:

- config loading
- workspace defaults
- sprint-scoped sessions
- parsing and action execution
- relay transport and relay-channel behavior
