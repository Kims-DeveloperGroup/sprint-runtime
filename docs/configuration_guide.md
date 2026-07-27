# `teams_runtime` Configuration Guide

This guide explains what to configure in a workspace and what each important field means.

## Workspace Root

`teams_runtime` operates on one workspace root.

Default resolution:

1. If the current directory contains both `team_runtime.yaml` and `discord_agents_config.yaml`, use it.
2. Otherwise use `./teams_generated`.
3. If `./teams_generated` is not present, fall back to `./workspace/teams_generated`.

You can always override this with `--workspace-root`.

Operational recommendation:

- treat `teams_generated/discord_agents_config.yaml` as the runtime source of truth
- if you also keep a project-root `discord_agents_config.yaml`, keep it synced as a compatibility copy only
- runtime status files record the resolved workspace root and config path so you can confirm which file was used

## `discord_agents_config.yaml`

This file defines the Discord bot topology.

Scaffolded files intentionally start with placeholder snowflakes such as `111111111111111111`.
Those placeholders are valid only for templates and tests. Runtime listener startup rejects them unless an explicit test override is enabled.

## Required Top-Level Fields

- `agents`
- `relay_channel_id`
  - or `relay_channel_env`
- `startup_channel_id`
  - or `startup_channel_env`
  - optional, defaults to `relay_channel_id`

## Required Per-Role Fields

- `name`
- `role`
- `description`
- `token_env`
- `bot_id`

## Why `bot_id` Matters

`bot_id` is required because it is used for:

- target mentions in Discord relay-mode messages
- trusted team-bot allowlisting
- detecting which role was mentioned in a guild message

`teams_runtime` does not treat runtime bot discovery as the source of truth.

## Startup Announcements

Each role listener sends a startup message when it becomes ready on Discord.

Channel selection rule:

1. use `startup_channel_id` when set
2. otherwise use `relay_channel_id`

Example:

```yaml
relay_channel_id: "123456789012345678"
startup_channel_id: "123456789012345679"
```

## Relay Transport Mode

Relay transport is selected at runtime through CLI flags, not `team_runtime.yaml`.

Supported values:

- `internal` (default)
  - role-to-role relay (`delegate`, `report`, `forward`) is delivered by internal direct runtime handoff
  - relay channel receives natural-language relay summaries for monitoring
- `discord`
  - role-to-role relay uses relay-channel envelope messages with target mentions (debug mode)

Examples:

```bash
python -m teams_runtime start --relay-transport internal
python -m teams_runtime start --relay-transport discord
python -m teams_runtime run --relay-transport discord
```

## `team_runtime.yaml`

This file defines runtime policy.

## Required Field

```yaml
sprint:
  id: "2026-Sprint-03"
```

## Sprint Scheduler Fields

`sprint` also controls the autonomous scheduler:

```yaml
sprint:
  id: "2026-Sprint-03"
  interval_minutes: 180
  timezone: "Asia/Seoul"
  mode: "hybrid"
  overlap_policy: "no_overlap"
  ingress_mode: "backlog_first"
  discovery_scope: "broad_scan"
  discovery_actions: []
```

Meaning:

- `interval_minutes`
  - default sprint cadence, 180 minutes by default
- `timezone`
  - wall-clock basis for slot calculation
- `mode`
  - `hybrid` starts on schedule or early when backlog is ready
- `overlap_policy`
  - `no_overlap` means only one active sprint at a time
- `ingress_mode`
  - `backlog_first` means normal change requests become backlog items first
- `discovery_scope`
  - legacy compatibility setting; autonomous backlog candidate sourcing is disabled
- `discovery_actions`
  - legacy compatibility setting; goal-driven sourcer operation ignores discovery actions and only runs from an active CLI goal

## Important Sections

### `ingress`

Controls allowed user ingress.

```yaml
ingress:
  dm: true
  mentions: true
```

### `allowed_guild_ids`

Optional guild allowlist:

```yaml
allowed_guild_ids:
  - "123456789012345678"
```

If left empty, guild filtering is not enforced.

### `role_defaults`

Override the Codex or Gemini model, plus Codex reasoning level, per role.

You can edit `teams_generated/team_runtime.yaml` directly or update it through the CLI:

```bash
python -m teams_runtime config role set --agent planner --model gpt-5.5 --reasoning medium
python -m teams_runtime config role set --agent developer --model gemini-3.1-pro-preview
```

Example:

```yaml
role_defaults:
  planner:
    model: "gpt-5.5"
    reasoning: "medium"
  developer:
    model: "gpt-5.5"
    reasoning: "high"
```

After changing a running role's config, restart that role to apply the new settings.

### `actions`

Defines which `execute` actions are allowed.

Example:

```yaml
actions:
  test_unittest:
    command: ["python", "-m", "unittest", "discover", "-s", "{target}"]
    lifecycle: "foreground"
    domain: "개발"
    allowed_params: ["target"]
```

For `teams_runtime` package-local tests, use Python's standard-library
`unittest` runner. `pytest` is not the `teams_runtime` test execution tool.

Rules:

- `command` must be a list
- `lifecycle` must be `foreground` or `managed`
- only `allowed_params` may be passed
- placeholders such as `{target}` must match provided params

## Empty `actions`

This is valid:

```yaml
actions: {}
```

That means:

- the orchestration workflow still runs
- `execute` requests are not available

## `request_id` vs `sprint.id`

- `backlog_id`
  - identifies one backlog item
  - stored under `.teams_runtime/backlog/`
- `request_id`
  - identifies one runtime request record
  - may refer to an intake/planner request or a sprint-internal execution request
  - stored under `.teams_runtime/requests/`
- `sprint.id`
  - identifies the configured session-scope context
  - used to decide session reuse versus refresh
  - stored in `.teams_runtime/role_sessions/<sanitized_runtime_identity>.json`

Public service runtimes still use role-name identities such as `planner`, so their session files remain `planner.json`-style files. Orchestrator-local helper runtimes use separate files such as `orchestrator.local.planner.json`.

Many `backlog_id` and `request_id` values may exist while one configured sprint session scope remains active.

### `prompt_context`

Controls how much persisted request event history is copied into model prompts:

```yaml
prompt_context:
  enabled: true
  recent_events: 8
  max_events: 16
```

Defaults:

- `enabled: true`
- `recent_events: 8`
- `max_events: 16`

Both event limits must be positive integers, and `max_events` must be greater than or equal to `recent_events`. Role services load these values at startup, so restart them after changing this section. There is no CLI mutation command for this policy.

Compaction changes only the prompt projection. The canonical request JSON under `.teams_runtime/requests/` retains every event, and the current request metadata, result, artifacts, and selected event payloads are not truncated or summarized.

#### Compaction And Backfill

**Compaction** is executed only when `enabled` is `true` and the request contains more than `max_events` events. The runtime:

1. Includes the last `recent_events` entries by list position, regardless of event shape.
2. Treats roles represented in that recent tail as already covered.
3. Scans older events from newest to oldest.
4. Backfills the newest evidence for each role not already represented, stopping at `max_events`.
5. Restores the selected events to their original chronological order.

**Backfill** means using remaining capacity to retain older role evidence that would otherwise disappear behind the recent-event boundary. An older event qualifies as role evidence when either `type` or legacy `event_type` is `role_report`, or when its `payload` contains non-empty `role` and `status` fields. The role identity comes from `payload.role`, falling back to the event `actor`.

Backfill is not a summary, database repair, or write to history. It copies complete existing event objects into the prompt. Repeated reports for one role do not consume multiple backfill slots; the newest qualifying older report wins. If `max_events` equals `recent_events`, no backfill capacity exists and the projection is recent-only.

#### Worked Example

With `recent_events: 4` and `max_events: 7`, suppose the persisted request has this abbreviated event list:

```json
[
  {"timestamp": "T01", "type": "created", "actor": "orchestrator"},
  {"timestamp": "T02", "type": "role_report", "payload": {"role": "research", "status": "completed"}},
  {"timestamp": "T03", "type": "delegated", "actor": "orchestrator"},
  {"timestamp": "T04", "type": "role_report", "payload": {"role": "planner", "status": "completed", "summary": "initial plan"}},
  {"timestamp": "T05", "type": "role_report", "payload": {"role": "designer", "status": "completed"}},
  {"timestamp": "T06", "type": "role_report", "payload": {"role": "planner", "status": "completed", "summary": "final plan"}},
  {"timestamp": "T07", "type": "retried", "actor": "orchestrator"},
  {"timestamp": "T08", "type": "role_report", "payload": {"role": "developer", "status": "completed"}},
  {"timestamp": "T09", "type": "role_report", "payload": {"role": "architect", "status": "completed"}},
  {"timestamp": "T10", "type": "delegated", "actor": "orchestrator"},
  {"timestamp": "T11", "type": "role_report", "payload": {"role": "qa", "status": "blocked"}},
  {"timestamp": "T12", "type": "resumed", "actor": "orchestrator"}
]
```

The recent tail is `T09` through `T12`, representing `architect` and `qa`. Three slots remain. Scanning backward selects `T08` for `developer`, `T06` for `planner`, and `T05` for `designer`. `T04` is skipped because the newer planner report already represents that role. Capacity is then full, so older `research` evidence at `T02` is not selected.

The prompt receives:

```json
[
  {"timestamp": "T05", "type": "role_report", "payload": {"role": "designer", "status": "completed"}},
  {"timestamp": "T06", "type": "role_report", "payload": {"role": "planner", "status": "completed", "summary": "final plan"}},
  {"timestamp": "T08", "type": "role_report", "payload": {"role": "developer", "status": "completed"}},
  {"timestamp": "T09", "type": "role_report", "payload": {"role": "architect", "status": "completed"}},
  {"timestamp": "T10", "type": "delegated", "actor": "orchestrator"},
  {"timestamp": "T11", "type": "role_report", "payload": {"role": "qa", "status": "blocked"}},
  {"timestamp": "T12", "type": "resumed", "actor": "orchestrator"}
]
```

The adjacent prompt notice reports `total_events: 12`, `included_events: 7`, `omitted_events: 5`, the selection policy, and the canonical request path. A role may open that canonical file when its current decision needs omitted evidence.

For immediate rollback, set `enabled: false` and restart role services. This restores full event-history inclusion in prompts without changing persisted request data.

## Changing `sprint.id`

To rotate the configured sprint session scope:

1. Update `team_runtime.yaml`
2. Restart services

```bash
python -m teams_runtime restart
```

What happens next:

- the new sprint ID is read by restarted services
- each runtime identity refreshes its session lazily on its next task
- the old session metadata is archived under `.teams_runtime/archive/`

## Recommended First Configuration

For a first setup, keep it simple:

- enable `dm` and `mentions`
- leave `actions: {}` empty until you need runtime command execution
- verify all six `bot_id` values before testing mentions

## Next Reading

- [Quickstart](./quickstart.md)
- [Operations Guide](./operations_guide.md)
- [Specification](./specification.md)
