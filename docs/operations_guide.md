# `teams_runtime` Operations Guide

This guide covers the day-to-day commands and the runtime behavior you are most likely to check while operating `teams_runtime`.

## Core Commands

Create a workspace, or refresh copied prompts and packaged runtime skills when the target is already a workspace:

```bash
python -m teams_runtime init
```

Force a generated-file rebuild when you intentionally want to reset runtime state and shared workspace files:

```bash
python -m teams_runtime init --reset
```

Start all roles in the background:

```bash
# default relay transport is internal
python -m teams_runtime start
# debug mode: relay through Discord channel
python -m teams_runtime start --relay-transport discord
```

Run all roles in the foreground:

```bash
python -m teams_runtime run
python -m teams_runtime run --relay-transport discord
```

Check service status:

```bash
python -m teams_runtime status
python -m teams_runtime status --backlog
python -m teams_runtime sprint status
```

Operate sprint lifecycle explicitly:

```bash
python -m teams_runtime sprint start --milestone "로그인 기능 워크플로 정리"
python -m teams_runtime sprint start --milestone "로그인 기능 워크플로 정리" --brief "기존 relay flow 유지" --requirement "kickoff docs를 source-of-truth로 보존"
python -m teams_runtime sprint stop
python -m teams_runtime sprint restart
```

`python -m teams_runtime status --sprint` remains available as a compatibility alias.

## Sprint Resume Behavior

- Automatic resume is gated only by `.teams_runtime/sprint_scheduler.json` `active_sprint_id`. If it is set, orchestrator startup and the scheduler loop both call the active sprint resume path.
- `python -m teams_runtime sprint restart` is the explicit recovery path. It can restart the latest sprint when that sprint is not `completed`; for blocked sprints the runtime only allows restart when `closeout_status` is `planning_incomplete` or `restart_required`, with a legacy fallback for old initial-phase blocked reports.
- `python -m teams_runtime sprint stop` requests wrap-up for an active sprint. If the sprint is already terminal (`failed` or `blocked`), stop simply clears the active slot instead of reopening execution.
- Manual initial-phase planning failures that end with `planning_incomplete` now clear the active slot immediately, so they do not auto-resume on the next scheduler poll or orchestrator restart unless an operator explicitly runs `sprint restart`.

## Planner Backlog Contract

- Planner is the only role that persists planner-owned backlog changes.
- Planner should persist canonical `backlog_items` / `backlog_item` payloads through the backlog helper boundary, then return `proposals.backlog_writes` receipts with affected `backlog_id` values and optional artifact paths.
- `proposals.backlog_item` and `proposals.backlog_items` remain planning rationale and execution context. Orchestrator does not treat them as persistence instructions.
- Orchestrator verifies `proposals.backlog_writes` against persisted `.teams_runtime/backlog/*.json` state and then continues routing, sprint selection, and status reporting from the saved backlog state.
- During sprint `initial` planning, planner must first define or reopen sprint-relevant backlog from the current milestone, kickoff requirements, and `spec.md` before prioritization or todo finalization.
- `backlog 0건` is not an acceptable sprint-start result. If initial-phase backlog definition leaves zero sprint-relevant backlog, orchestrator blocks sprint start with `planning_incomplete`.
- Sprint backlog definition items should carry concrete `acceptance_criteria` plus planner trace in `origin.milestone_ref`, `origin.requirement_refs`, and `origin.spec_refs`.
- Legacy planner aliases such as `planned_backlog_updates` are compatibility inputs only inside role-runtime normalization. They are not accepted by the canonical backlog helper interface.

## Designer Advisory Contract

- `designer` is advisory-only inside workflow-managed requests. It participates during planner-owned advisory passes and when orchestrator reopens a workflow with `reopen_category='ux'`.
- Planner should request designer only when the open question is about user flow, information ordering, or message readability. Designer is not a default gate for every sprint todo.
- The primary message-review surfaces for this contract are progress reports, compact relay summaries, and requester-facing status notifications.
- Treat `renderer-only` as a narrow implementation bucket: same meaning, same priority, same CTA, with work limited to rendering/contract repair such as escaping, field mapping, compact-contract wiring, and truncation fixes.
- Treat `readability-only` as non-advisory only when the same reading order, decision path, and CTA still hold and the change merely makes the approved structure easier to scan.
- If a Discord/operator message change affects reading order, omission tolerance, title/summary/body/action priority, or CTA wording/tone, planner should classify it as designer advisory rather than renderer-only work.
- Use message-layer-specific triggers so designer does not become a universal gate:
  - `relay`: immediate state, warning, or single action priority changes
  - `handoff`: the next role's first-read context or promoted rationale changes
  - `summary`: long-term keep-vs-omit rules change
- If both rendering repair and user-facing judgment appear together, treat the request as `mixed-case`. Planner should split it into `technical slice` and `designer advisory slice`, then state whether `technical slice 선행` or `designer advisory 선행` applies.
- Mixed-case evidence should include at least a before/after message example or intended output hierarchy, plus one line saying whether the dominant problem is `표시 오류` or `사용자 판단 혼선`.
- Designer results should live under `proposals.design_feedback` with these keys:
  - `entry_point`: `planning_route`, `message_readability`, `info_prioritization`, or `ux_reopen`
  - `user_judgment`: 1-3 concise usability judgments
  - `message_priority`: what to lead with and what can wait; prefer explicit `lead` / `defer`, and include `summary` when layer reassignment or keep-vs-promote guidance matters
  - `routing_rationale`: short rationale planner/orchestrator can reuse
  - optional `required_inputs`, `acceptance_criteria`
- For user-facing data selection, map `message_priority.lead` to the core layer, `summary` to layer reassignment or keep-vs-promote guidance, and `defer` to the supporting layer.
- `architect` is the support role that translates designer judgment into implementation contracts and checks stage fit during architect guidance/review.
- `architect` and `developer` should preserve the approved message contract during execution. They do not redefine information ordering or CTA wording unless designer/planner already settled that change.
- `qa` is the support role that validates whether designer intent survived into shipped output and reopens with `reopen_category='ux'` only when the UX contract drifted.
- After designer advisory, orchestrator routes back to planner finalization unless the workflow was explicitly reopened for UX. Planner remains the planning owner and absorbs the advisory into shared planning artifacts.
- Orchestrator treats `design_feedback` as routing evidence only. It does not treat designer output as a direct execution order.

Check request status:

```bash
python -m teams_runtime status --request-id 20260323-abcd1234
```

List services and open requests:

```bash
python -m teams_runtime list
```

Restart services after config changes:

```bash
python -m teams_runtime restart
```

Stop services:

```bash
python -m teams_runtime stop
```

Operate on one role only:

```bash
python -m teams_runtime start --agent orchestrator
python -m teams_runtime status --agent developer
python -m teams_runtime stop --agent planner
python -m teams_runtime restart --agent qa
```

## How Requests Move

In v1 the runtime is sequential, but user-originated requests are orchestrator-agent-first.

Agent-first flow:

```text
User
  -> Orchestrator
  -> orchestrator agent decision
  -> direct completion or delegated next role
```

When the orchestrator starts a sprint, selected backlog items become internal requests and then follow the standard orchestrator-governed workflow contract.

Standard sprint chain:

```text
User
  -> Orchestrator
  -> Planner
  -> Orchestrator
  -> Designer / Architect advisory only when planning needs it
  -> Orchestrator
  -> Planner finalization
  -> Architect guidance
  -> Developer build
  -> Architect review
  -> Developer revision
  -> QA
  -> Version Controller
  -> Orchestrator
  -> User
```

Important rule:

- downstream roles do not directly assign work to each other
- every handoff goes back through the orchestrator
- the exact planning advisory specialist may vary, but the workflow phases and implementation sequence are fixed by policy
- execution-stage and QA-stage reopen decisions are made by orchestrator from structured workflow output, not summary prose

That means implementation opening is no longer the old free-form `planner -> developer` shortcut. It is:

```text
planner finalization report
  -> orchestrator merge
  -> architect guidance
  -> orchestrator merge
  -> developer
```

## Relay Transport Modes

Relay transport is runtime-selectable:

- `internal` (default)
  - role-to-role relay is delivered internally (direct runtime handoff)
  - relay channel receives natural-language summaries after each relay
- `discord`
  - role-to-role relay is delivered as Discord relay-channel envelope messages
  - intended for relay debugging

User-facing intake/replies and startup/report messages still use Discord in both modes.

## What Gets Passed Between Roles

There are two layers of context:

### 1. Relay message envelope

This is the canonical compact relay envelope:

```text
<@target_bot_id>
request_id: 20260323-abcd1234
intent: implement
urgency: normal
scope: 로그인 기능 초안 설계해줘
artifacts:
params: {"_teams_kind":"delegate"}
```

- internal mode: the envelope is delivered by internal direct handoff, and a natural-language summary is posted to the relay channel
- discord mode: the envelope itself is posted to the relay channel with target mention

### 2. Persisted request record

The target role also receives the full persisted request record through its prompt context.

That record contains:

- request metadata
- current status
- reply route
- event history
- most recent role result

So later roles can see earlier role output even though the relay message itself stays small.

## Request Examples

Plan request:

```text
intent: plan
scope: 로그인 기능 초안 설계해줘
```

Typical intake reply:

```text
planning과 backlog management를 위해 planner로 전달했습니다. request_id=20260329-...
```

If planner later persists a backlog item, a backlog-specific follow-up can mention the created identifier:

```text
backlog_id=backlog-...
resolution=created
status=pending
```

Architecture request:

```text
intent: architect
scope: teams_runtime 모듈 구조를 overview하고 developer 구현용 technical specification을 작성해줘
```

Architect review request:

```text
intent: architect
scope: 최근 developer 변경을 architecture/code review 관점에서 검토해줘
```

Implementation request:

```text
intent: implement
scope: 요청 상태 저장 로직을 보강해줘
```

QA request:

```text
intent: qa
scope: 최근 구현 변경의 회귀 위험과 누락 테스트를 검토해줘
```

Status check:

```text
status request_id:20260323-abcd1234
```

Sprint status:

```text
status sprint
```

Natural-language alias examples:

```text
현재 스프린트 공유해줘
스프린트 현황 알려줘
지금 스프린트 계획 보여줘
```

These are interpreted by the orchestrator through the internal parser agent, not by simple alias tables alone.

## Backlog Intake Notifications

Planner-first intake uses two separate Discord surfaces:

- requester reply: one user-visible ack only
- operations report: planner delegation or planner-persisted backlog update messages on the runtime reporting channel

The requester-facing message acknowledges planner delegation first.
Backlog-specific notifications are emitted only after planner has directly persisted the backlog change.

The operations report includes:

- `request_id` for planner delegation, or `backlog_id` once planner direct backlog persistence occurs
- `resolution=created|reused` only when planner backlog persistence actually happened
- `status`
- `title`

Backlog status:

```text
status backlog
```

Natural-language alias examples:

```text
현재 백로그 공유해줘
백로그 현황 보여줘
지금 백로그 알려줘
```

Cancel:

```text
cancel request_id:20260323-abcd1234
```

Legacy compatibility:

- `approve request_id:...` is no longer a supported operation
- the runtime returns an unsupported response instead of creating approval state

## Sprint Operations

Role sessions persist for the current configured sprint session scope, and the orchestrator automatically runs sprints on a schedule.

Key files:

- `shared_workspace/backlog.md`
- `shared_workspace/current_sprint.md`
- `shared_workspace/sprints/<sprint_folder_name>/`
- `shared_workspace/sprint_history/index.md`
- `shared_workspace/sprint_history/<sprint_id>.md`

Scheduler behavior:

- default interval is 180 minutes
- default mode is `hybrid`
- if backlog is ready and no sprint is active, the orchestrator may start early
- only one sprint can run at a time

To force a new configured sprint session scope for role-session refresh:

1. update `team_runtime.yaml`
2. change `sprint.id`
3. restart services

```bash
python -m teams_runtime restart
```

Session rollover is lazy:

- idle roles do not refresh immediately
- each role refreshes when it next receives work

Planner persistence behavior:

- planner sprint-internal write requests such as `artifact_sync`, `backlog_definition`, backlog prioritization, and sprint backlog persistence may update both `shared_workspace/` and `.teams_runtime/`
- developer requests run with `--dangerously-bypass-approvals-and-sandbox` by default, and planner write-bearing requests do as well, so implementation work plus runtime-owned sprint/backlog persistence are not blocked by session-symlink sandbox boundaries
- seeing `./shared_workspace` or `./.teams_runtime` inside a role session does not by itself mean the sandbox can write to the resolved target path
- non-bypass Codex runs still add the resolved targets for `./workspace`, `./shared_workspace`, and `./.teams_runtime` as extra writable roots when available

## Checking Runtime Files

Useful runtime directories:

- `.teams_runtime/requests/`
  - per-request JSON state
- `.teams_runtime/role_sessions/`
  - active session metadata per runtime identity
- `.teams_runtime/archive/`
  - archived old sprint session metadata
- `.teams_runtime/agents/`
  - background service pid/log/state files
- `.teams_runtime/internal_relay/inbox/`
  - pending internal relay envelopes by target role
- `.teams_runtime/internal_relay/archive/`
  - consumed internal relay envelopes by role

Useful log directories:

- `logs/agents/`
  - per-role runtime logs
- `logs/discord/`
  - per-role Discord inbound/outbound JSONL transcripts
- `logs/operations/`
  - action execution logs

## Troubleshooting

### Service starts but Discord intake does not work

Check:

- the matching `token_env` variables are exported
- the bot has permission to read DMs or channel messages
- the configured `bot_id` values are correct
- the relay channel ID is correct

### Internal relay looks stuck

Check:

- services started with default `internal` relay transport (or explicit `--relay-transport internal`)
- `.teams_runtime/internal_relay/inbox/<role>/` for pending envelopes
- `.teams_runtime/internal_relay/archive/<role>/` for consumed envelopes
- `logs/agents/<role>.log` for internal relay consumer errors

If you need full relay payload visibility in Discord while debugging, restart with:

```bash
python -m teams_runtime restart --relay-transport discord
```

### Mentions do not route to the expected role

Check the `bot_id` in `discord_agents_config.yaml`.

Mention routing is bot-ID based, not name based.

### A role keeps old context after sprint change

This is expected until that role receives its next task.

Public service runtimes and orchestrator-local helper runtimes roll over independently. It is normal for `.teams_runtime/role_sessions/` to contain both a service file such as `planner.json` and a helper file such as `orchestrator.local.planner.json`.

Use:

```bash
python -m teams_runtime status
```

Then send work to the role after `restart`.

### `execute` requests fail immediately

Check whether `actions` is empty in `team_runtime.yaml`.

If `actions: {}` is empty, the workflow still works, but command execution is disabled.

## Next Reading

- [Quickstart](./quickstart.md)
- [Configuration Guide](./configuration_guide.md)
- [Architecture](./architecture.md)
