# `teams_runtime` Implementation Notes

## Package Layout

### Top level

- `teams_runtime/cli.py`
  - CLI entrypoint, workspace resolution, status/list surfaces
- `teams_runtime/models.py`
  - runtime data models and fixed role constants
- `teams_runtime/requirements.txt`
  - package-local dependency list

### `core/`

- `config.py`
  - loads `team_runtime.yaml` and `discord_agents_config.yaml`
- `paths.py`
  - computes workspace, backlog, sprint, request, and log paths
- `persistence.py`
  - JSON/JSONL helpers, IDs, fingerprints, KST timestamps
- `parsing.py`
  - thin command/envelope parsing and freeform-shape detection
- `reports.py`
  - Korean `[작업 보고]` formatting
- `actions.py`
  - registered action execution
- `git_ops.py`
  - sprint git baseline capture plus machine-friendly task/closeout version-control helpers
- `sprints.py`
  - sprint IDs, markdown rendering, and scheduler-time helpers
- `orchestration.py`
  - `TeamService`, backlog intake, scheduler loop, sprint execution, relay handling
- `template.py`
  - default workspace scaffold and role prompts

### `discord/`

- `client.py`
  - standalone Discord send/listen client
- `lifecycle.py`
  - per-role background service management

### `runtime/`

- `codex.py`
  - sprint-scoped role session management, role runtimes, internal parser/sourcer/version_controller runtimes, and Codex subprocess runner

## Runtime Data Layout

Machine-readable state:

- `<workspace>/.teams_runtime/backlog/`
- `<workspace>/.teams_runtime/sprints/`
- `<workspace>/.teams_runtime/requests/`
- `<workspace>/.teams_runtime/internal_relay/`
- `<workspace>/.teams_runtime/role_sessions/`
- `<workspace>/.teams_runtime/archive/`
- `<workspace>/.teams_runtime/operations/`

Logs:

- `<workspace>/logs/agents/`
- `<workspace>/logs/agents/archive/`
- `<workspace>/logs/discord/`
- `<workspace>/logs/operations/`

Human-readable sprint state:

- `shared_workspace/backlog.md`
- `shared_workspace/completed_backlog.md`
- `shared_workspace/current_sprint.md`
- `shared_workspace/sprints/<sprint_folder_name>/`
- `shared_workspace/sprints/<sprint_folder_name>/kickoff.md`
- `shared_workspace/sprints/<sprint_folder_name>/attachments/<attachment_id>_<filename>`
- `shared_workspace/sprint_history/index.md`
- `shared_workspace/sprint_history/<sprint_id>.md`
- `internal/parser/`
- `internal/version_controller/`

Timestamp convention:

- `teams_runtime` persists runtime-generated timestamps in `Asia/Seoul`
- JSON/JSONL/markdown state files use ISO-8601 with `+09:00`
- generated sprint IDs use the local KST date/time form `YYMMDD-Sprint-HH:MM`, for example `260324-Sprint-09:00`
- sprint artifact folders use a filesystem-safe form of `sprint_id` under `shared_workspace/sprints/`
- sprint state keeps both a refined milestone title and immutable kickoff source fields so planner can preserve the original sprint-start brief while deriving execution framing separately
- inbound Discord attachments resolve to the active sprint folder; sprint-start attachments are relocated into the newly created sprint folder and recorded as sprint reference artifacts
- scheduler state and operational reports expose only the active runtime sprint id via `active_sprint_id`; sprint records themselves keep a single `sprint_id`

## Current Execution Flow

### 1. Intake

- a Discord message arrives
- listener classifies it as user ingress or trusted relay traffic
- direct user requests return a single user-visible reply by default
- role-to-role relay defaults to internal direct handoff and is not echoed back to the user channel
- relay channel still receives natural-language relay summaries for operator monitoring
- planner treats `Current request.artifacts` and sprint attachment docs under `shared_workspace/sprints/<sprint_folder_name>/attachments/` as planning reference inputs and is expected to extract requirements/constraints from them before blocking
- sprint planning requests include `kickoff.md` plus preserved kickoff params so milestone refinement does not drop the original sprint-start requirements

### 2. Backlog-first orchestration

For normal change work:

- orchestrator is the first owner for user-originated requests
- freeform intake is interpreted by the orchestrator agent rather than parser-first intake
- after control-flow requests are excluded, orchestrator delegates normal planning/backlog work to planner first
- orchestrator applies a workflow contract before capability scoring, then uses its local `agent_utilization` skill and sibling `policy.yaml` as a bounded tie-breaker inside the allowed workflow roles
- capability boundaries are also enforced directly: `should_not_handle` phrases exclude a candidate before scoring, so ownership limits are hard routing boundaries rather than advisory metadata
- capability routing is bounded by planner ownership and workflow policy; it does not bypass planner-first intake, bounded advisory passes, the mandatory architect/developer review chain, sourcer-review-to-planner, blocked planning resume, sprint initial owner policy, or version-controller-only flows
- planner owns backlog-management decisions such as add/update/dedupe/reprioritize and persists those backlog changes directly into runtime backlog state
- planner backlog persistence uses the canonical backlog helper boundary with `backlog_items` / `backlog_item` payloads, and planner returns `proposals.backlog_writes` receipts after those writes succeed
- backlog/todo definition is `spec-first`: sprint backlog must be derived from the current milestone, kickoff requirements, and `spec.md`, not only from pre-existing queue state
- `backlog.md` is updated from planner-owned backlog persistence and shows active items with `created_at`
- active backlog can include `blocked` items that are waiting for missing inputs, but only `pending` items are sprint-selectable
- `completed_backlog.md` is refreshed alongside it and keeps only `done` items
- requester acknowledgements return `request_id`; backlog IDs are surfaced when planner includes them after direct backlog persistence

Immediate control paths remain orchestrator-managed:

- `status`
- `cancel`
- sprint lifecycle
- registered action `execute`

Asynchronous work may still use a two-step user-facing flow, but the first message must carry meaningful state such as `request_id` or the next owner instead of a generic receipt.

Natural-language status and sprint-control requests do not have to match a fixed alias table exactly.
The orchestrator agent decides whether they should be answered directly, turned into sprint lifecycle commands, or routed onward.

Autonomous backlog sourcing is also planner-gated.
The internal sourcer produces backlog candidates, and orchestrator queues them as planner review requests before any backlog record is created or updated.

### 3. Scheduler loop

The orchestrator runs a background scheduler task.

On each poll:

- load scheduler state
- compute next slot if missing
- detect whether a sprint is active
- start a sprint when:
  - backlog is ready in `hybrid` mode, or
  - the scheduled slot has arrived

### 4. Sprint creation

At sprint start:

- discovery refresh runs
- sprint state is created
- immutable kickoff source fields plus `kickoff.md` are persisted from the sprint-start request before planner refines milestone framing
- planner initial phase runs in this order:
  - `milestone_refinement`
  - `artifact_sync`
  - `backlog_definition`
  - `backlog_prioritization`
  - `todo_finalization`
- `backlog_definition` must create or reopen sprint-relevant backlog from `milestone + kickoff requirements + spec`
- `backlog 0건` is invalid during sprint start; orchestrator blocks with `planning_incomplete` instead of continuing
- selected backlog items are marked `selected` only during `todo_finalization`
- todos are derived from the selected backlog items after `todo_finalization`
- `current_sprint.md` is written
- sprint kickoff and todo list are reported to Discord

### 5. Todo execution

Each sprint todo becomes an internal request record.

Internal request execution uses a workflow contract stored in request state:

- planning owner is always planner
- planning advisory is limited to a shared cap across designer and architect
- implementation always follows:
  - `architect_guidance`
  - `developer_build`
  - `architect_review`
  - `developer_revision` (only when architect review leaves actionable revision work)
  - `qa_validation` (directly after `architect_review` when no developer revision is needed)
- planner result is allowed to carry only planning artifacts such as `spec.md` and `iteration_log.md`; when its `workflow_transition` explicitly advances to `implementation`, orchestrator must delegate instead of treating `finalize_phase=true` as a terminal planning close
- orchestrator chooses the next role from workflow state and structured `proposals.workflow_transition`
- orchestrator sends delegated steps via the active relay transport (`internal` by default, `discord` in debug mode)
- role runtime runs in a sprint-scoped session
- role returns structured JSON through relay `report` back to the orchestrator using the active relay transport
- orchestrator merges result
- execution and QA roles can reopen work only through structured reopen categories interpreted by orchestrator
- after the last successful work-role result, orchestrator invokes internal `version_controller`
- `version_controller` reads a request-scoped payload file, runs the git helper, and returns structured commit metadata

This means sprint-time role traffic always remains inspectable: full envelopes in internal relay files and relay summaries in Discord.
If a todo blocks before it reaches QA, then QA relay traffic will not exist for that todo because it was never delegated there.

Todo outcomes:

- `completed`
- `committed`
- `uncommitted`
- `blocked`
- `failed`
- carry-over backlog created when needed

Blocked todos do not create a new backlog item anymore.
Instead, the original backlog item is moved back to `blocked` and stores `blocked_reason`, `required_inputs`, and `recommended_next_step`.
When a user later sends a matching request with the missing context, the orchestrator reopens that blocked backlog item back to `pending`.

If the scheduler reaches a slot but there is still no actionable backlog after discovery refresh, it skips sprint creation entirely instead of opening and immediately closing an empty sprint.

### 6. Sprint closeout

At task completion:

- orchestrator delegates task-scoped commit execution to internal `version_controller`
- if no owned change exists, the todo stays `completed`
- if owned change exists, the internal request/todo enters `uncommitted` before `version_controller` runs
- successful task commit promotes the internal request/todo to `committed`
- a version-controller failure leaves the internal request/todo in `uncommitted` so commit recovery can resume later

At sprint end:

- git baseline is compared with current worktree
- only leftover sprint-owned paths are considered
- one closeout version-controller step is attempted only when task-completion commits left changes behind
- sprint report body is built
- sprint history markdown is archived
- history index is updated
- `current_sprint.md` is reset to idle

## Discovery Inputs

Current backlog discovery can scan:

- non-terminal user/requester-facing request records
- request records whose latest role result indicates a failed role outcome
- git dirty paths
- role runtime logs for `Traceback` / `ERROR`
- optional configured `discovery_actions`

Role `insights` are not promoted into backlog candidates automatically.
They are preserved in role `journal.md`, `history.md`, and the canonical request record for later human or orchestrator reference.
Internal sprint requests are also excluded from backlog rediscovery to avoid feedback loops.

Planner-style backlog drafting requests may return `proposals.backlog_item` or `proposals.backlog_items` as rationale, but planner-owned persistence is acknowledged through `proposals.backlog_writes`.
Planner owns the backlog-management decision and direct backlog persistence.
Orchestrator verifies planner backlog receipts and reads the resulting backlog state for routing, sprint selection, and status replies instead of re-merging planner proposals.
Public roles do not own `next_role` selection.
Orchestrator reads each role result, scores the allowed downstream candidates, and decides whether execution should continue or stop.
Delegation payloads now carry routing context such as selected role, selected strength, suggested skills, expected behavior, and override reason when orchestrator opens or redirects the next handoff.

Planner owns what work should exist and how it should be planned.
Orchestrator owns sprint-state status mutations created during execution, including:

- backlog execution-state transitions such as `selected`, `done`, `blocked`, and `carried_over`
- `selected_in_sprint_id` and `completed_in_sprint_id`
- blocker fields such as `blocked_reason`, `blocked_by_role`, `required_inputs`, and `recommended_next_step`
- todo state such as `queued`, `running`, `uncommitted`, `committed`, `completed`, `blocked`, and `failed`
- sprint lifecycle state such as `planning`, `running`, `wrap_up`, `completed`, `failed`, and `blocked`

## Role Output Contract

Roles are expected to return structured JSON including:

- `request_id`
- `role`
- `status`
- `summary`
- `insights`
- `proposals`
- `artifacts`
- `error`

The runtime persists that output into:

- `.teams_runtime/requests/<request_id>.json`

It also updates:

- `<role>/history.md`
- `<role>/journal.md`

## Agent Runtime Log Verification

Role runtime logs under `<workspace>/logs/agents/<role>.log` are treated as the current service-session log.
When a role service is started in background mode, any previous non-empty runtime log is archived into
`<workspace>/logs/agents/archive/<role>-<timestamp>.log` before the new process starts.
The fresh current log begins with a startup marker such as:

```text
[teams_runtime] service_start role=planner started_at=2026-03-25T08:30:00+09:00 archived_log=...
```

Recommended recurrence check procedure:

1. Restart the target role service so a fresh session log is created.
2. Confirm the latest current log starts with the `service_start` marker for the expected role.
3. Scan only the current-session logs for known regressions:
   - `rg -n "ClientConnectorDNSError|Attempting a reconnect|Ignoring trusted relay message without (delegate kind|supported kind)" <workspace>/logs/agents/*.log`
4. Verify Discord transcript noise separately when needed:
   - `rg -n "\"status\":\"callback_failed\"" <workspace>/logs/discord/*.jsonl`
5. Re-run the runtime regression tests:
   - `python -m unittest ./workspace/teams_runtime/tests/test_discord_client.py ./workspace/teams_runtime/tests/test_orchestration.py ./workspace/teams_runtime/tests/test_config.py`

Historical errors should be inspected from `logs/agents/archive/` instead of being treated as a current regression.
- shared workspace files such as planning/decision/shared history

## Prompt And Workspace Contract

Role sessions do not work only inside `teams_generated/<role>`.
Each session also gets:

- `./workspace`
  - the broader project workspace
- `./shared_workspace`
  - the shared team coordination area
- `./.teams_runtime`
  - runtime-owned canonical state such as requests, backlog, sprints, and role-session metadata

This is how roles can work for the actual project while still keeping private coordination files separate.

Implementation note:

- These paths are typically exposed as symlinks from the role session root to the real runtime workspace.
- A visible symlink does not guarantee the Codex sandbox can write to the symlink target.
- For sandboxed runs, the runtime adds the resolved targets for `./workspace`, `./shared_workspace`, and `./.teams_runtime` as extra writable roots when possible.
- Developer requests default to `--dangerously-bypass-approvals-and-sandbox`, and planner requests that persist sprint/backlog state under `shared_workspace/` or `.teams_runtime/` do as well, so implementation work and runtime-owned persistence are not blocked by resumed-session sandbox boundaries.

In addition, there is an internal non-public workspace:

- `internal/parser/`
  - seeded like a normal session workspace
  - used by the orchestrator to classify freeform intake
  - not tied to a Discord bot identity
- `internal/sourcer/`
  - seeded like a normal session workspace
  - used by the orchestrator to turn runtime/workspace findings into planner review candidates
  - not tied to a Discord bot identity
- `internal/version_controller/`
  - seeded like a normal session workspace
  - used by the orchestrator to execute task and closeout commits through the git helper
  - not tied to a Discord bot identity

## Current Limitations

- sourcer fallback discovery is still heuristic when the internal sourcer cannot run
- sprint selection currently pulls from all pending backlog items instead of a bounded WIP budget
- no parallel multi-role fanout yet
- sprint commit generation depends on git availability and a clean-enough baseline snapshot
- generated workspace prompts are copied at scaffold time, so older workspaces may need prompt refresh when the template changes

## Maintainer Checklist

When changing the sprint model, check these together:

1. `core/config.py`
2. `core/paths.py`
3. `core/sprints.py`
4. `core/git_ops.py`
5. `core/orchestration.py`
6. `core/template.py`
7. `cli.py`
8. `teams_runtime/tests/`

## Test Command

Current focused regression command:

```bash
python -m pytest \
  teams_runtime/tests/test_config.py \
  teams_runtime/tests/test_sessions.py \
  teams_runtime/tests/test_parsing_actions.py \
  teams_runtime/tests/test_orchestration.py \
  teams_runtime/tests/test_discord_client.py
```
