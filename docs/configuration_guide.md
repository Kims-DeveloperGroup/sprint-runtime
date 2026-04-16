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
  - controls how broadly the orchestrator scans for backlog candidates
- `discovery_actions`
  - optional registered foreground actions run during discovery

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

Override the Codex model or reasoning level per role.

You can edit `teams_generated/team_runtime.yaml` directly or update it through the CLI:

```bash
python -m teams_runtime config role set --agent planner --model gpt-5.4 --reasoning medium
python -m teams_runtime config role set --agent developer --model gemini-2.5-pro
```

Example:

```yaml
role_defaults:
  planner:
    model: "gpt-5.4"
    reasoning: "medium"
  developer:
    model: "gpt-5.4"
    reasoning: "high"
```

After changing a running role's config, restart that role to apply the new settings.

### `actions`

Defines which `execute` actions are allowed.

Example:

```yaml
actions:
  test_pytest:
    command: ["python", "-m", "pytest", "{target}"]
    lifecycle: "foreground"
    domain: "개발"
    allowed_params: ["target"]
```

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
  - stored in `.teams_runtime/role_sessions/<role>.json`

Many `backlog_id` and `request_id` values may exist while one configured sprint session scope remains active.

## Changing `sprint.id`

To rotate the configured sprint session scope:

1. Update `team_runtime.yaml`
2. Restart services

```bash
python -m teams_runtime restart
```

What happens next:

- the new sprint ID is read by restarted services
- each role refreshes its session lazily on its next task
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
