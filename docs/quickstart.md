# `teams_runtime` Quickstart

This guide is for getting a new `teams_runtime` workspace running for the first time.

## 1. Install Dependencies

```bash
pip install -r /Users/anonymousanonym/Documents/jupyter-workspace/teams_runtime/requirements.txt
```

The runtime also expects:

- Python available on `PATH`
- the `codex` CLI available on `PATH`
- Discord bot tokens for each configured role

## 2. Create A Workspace

From your project root:

```bash
python -m teams_runtime init
```

If the target workspace already contains generated `teams_runtime` config, `init` refreshes copied role prompts and packaged runtime skills by default without resetting `.teams_runtime/` or `shared_workspace/`.

To rebuild generated files from scratch, use:

```bash
python -m teams_runtime init --reset
```

`--reset` preserves `discord_agents_config.yaml` and archived sprint history under `shared_workspace/sprint_history/`, but it resets runtime state and shared workspace files.

Default behavior:

- if the current directory is already a workspace root, that directory is used
- otherwise `./teams_generated` is created

The generated workspace contains:

- `team_runtime.yaml`
- `discord_agents_config.yaml`
- role folders such as `planner/`, `architect/`, `developer/`, `qa/`
- `shared_workspace/`

It does not contain package documentation.

## 3. Configure Discord Bots

Edit `teams_generated/discord_agents_config.yaml`.

Each role needs:

- `name`
- `role`
- `description`
- `token_env`
- `bot_id`

The root config also needs a relay channel:

- `relay_channel_id`
  - or `relay_channel_env`
- `startup_channel_id`
  - or `startup_channel_env`
  - optional, defaults to `relay_channel_id`

The relay channel is still required even when role-to-role relay transport is internal by default, because startup and relay-summary messages are reported there.

Minimal example:

```yaml
relay_channel_id: "123456789012345678"
startup_channel_id: "123456789012345679"
agents:
  orchestrator:
    name: "Orchestrator"
    role: "orchestrator"
    description: "Owns request routing"
    token_env: "AGENT_DISCORD_TOKEN_ORCHESTRATOR"
    bot_id: "123456789012345601"
  planner:
    name: "Planner"
    role: "planner"
    description: "Planning role"
    token_env: "AGENT_DISCORD_TOKEN_PLANNER"
    bot_id: "123456789012345602"
  designer:
    name: "Designer"
    role: "designer"
    description: "Design role"
    token_env: "AGENT_DISCORD_TOKEN_DESIGNER"
    bot_id: "123456789012345603"
  architect:
    name: "Architect"
    role: "architect"
    description: "Architecture, technical spec, and code review role"
    token_env: "AGENT_DISCORD_TOKEN_ARCHITECT"
    bot_id: "123456789012345604"
  developer:
    name: "Developer"
    role: "developer"
    description: "Implementation role"
    token_env: "AGENT_DISCORD_TOKEN_DEVELOPER"
    bot_id: "123456789012345605"
  qa:
    name: "QA"
    role: "qa"
    description: "Quality assurance role"
    token_env: "AGENT_DISCORD_TOKEN_QA"
    bot_id: "123456789012345606"
```

## 4. Configure Runtime Policy

Edit `teams_generated/team_runtime.yaml`.

To update a role's runtime model or reasoning through the CLI:

```bash
python -m teams_runtime config role set --agent developer --model gpt-5.5 --reasoning high
```

If the role is already running, restart that role after saving the config.

The most important required field is:

```yaml
sprint:
  id: "2026-Sprint-03"
```

A practical starter config is:

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

ingress:
  dm: true
  mentions: true

allowed_guild_ids: []

actions: {}
```

Leaving `actions: {}` empty is valid. In that case:

- role collaboration works
- `execute` commands are disabled

## 5. Export Bot Tokens

Export the environment variables referenced by each role's `token_env`:

```bash
export AGENT_DISCORD_TOKEN_ORCHESTRATOR=...
export AGENT_DISCORD_TOKEN_PLANNER=...
export AGENT_DISCORD_TOKEN_DESIGNER=...
export AGENT_DISCORD_TOKEN_ARCHITECT=...
export AGENT_DISCORD_TOKEN_DEVELOPER=...
export AGENT_DISCORD_TOKEN_QA=...
```

## 6. Start The Runtime

Start all role services in the background:

```bash
# default relay transport is internal direct relay
python -m teams_runtime start
# debug mode: force Discord relay-channel transport
python -m teams_runtime start --relay-transport discord
```

Check that they are running:

```bash
python -m teams_runtime status
python -m teams_runtime list
python -m teams_runtime status --backlog
python -m teams_runtime sprint status
```

You can also operate sprint lifecycle directly:

```bash
python -m teams_runtime sprint start --milestone "로그인 기능 워크플로 정리"
python -m teams_runtime sprint start --milestone "로그인 기능 워크플로 정리" --brief "기존 relay flow 유지" --requirement "kickoff docs를 source-of-truth로 보존"
python -m teams_runtime sprint stop
python -m teams_runtime sprint restart
```

`python -m teams_runtime status --sprint` still works as a compatibility alias.

Foreground mode is also available:

```bash
python -m teams_runtime run
python -m teams_runtime run --relay-transport discord
```

## 7. Send A First Request

DM or mention the orchestrator bot with a simple request:

```text
intent: plan
scope: 로그인 기능 초안 설계해줘
```

You can also message another role directly. That role will forward the request to the orchestrator first, and the orchestrator agent becomes the first owner of the request.

In the autonomous sprint model, normal change requests do not execute immediately.

- the orchestrator first routes them to planner for planning and backlog-management decisions
- the first reply includes `request_id=...`
- backlog IDs appear only after planner directly persists a backlog record and reports it
- when the scheduler or an operator starts a sprint, the first initial-phase delegation is `research` with workflow step `research_initial`; the resulting research prepass report reaches planner before milestone refinement
- planner then uses that report to refine the raw kickoff milestone, write or update specs, define sprint-relevant backlog, prioritize it, and only then turn selected backlog items into sprint todos
- `backlog 0건` is not a valid sprint-start state; the runtime blocks the sprint with `planning_incomplete` instead
- sprint execution creates additional sprint-internal `request_id` values for each todo; these are separate from the intake/planner request ID

When a sprint todo starts, it follows the standard orchestrator-governed workflow:

- planning owner: `planner`
- optional planning advisory: `designer` or `architect`, up to 2 shared passes total
- implementation: `architect guidance -> developer build -> architect review -> developer revision (if needed)`
- validation: `qa` (`architect_review` pass can hand off directly to `qa_validation`)
- closeout: `version_controller`
- planning-only clarification on planner-owned surfaces closes in planning instead of opening implementation
- planner `workflow_transition` that explicitly advances to `implementation` still opens the next implementation step even when the planner report only includes `spec.md` / `iteration_log.md`-style artifacts
- planner-owned doc claims from `architect` or `developer` reopen to `planner_finalize`
- scenario-based sequence diagrams are documented in [`architecture.md`](./architecture.md#4-standard-workflow-contract)

Roles report structured workflow state back to orchestrator instead of directly picking the next role.

## 8. Check Runtime State

The runtime writes state under:

- `teams_generated/.teams_runtime/requests/`
- `teams_generated/.teams_runtime/backlog/`
- `teams_generated/.teams_runtime/sprints/`
- `teams_generated/.teams_runtime/role_sessions/`
  - one active metadata file per runtime identity
- `teams_generated/.teams_runtime/agents/`

The runtime writes logs under:

- `teams_generated/logs/agents/`
- `teams_generated/logs/discord/`
- `teams_generated/logs/operations/`

Those files are useful when you need to inspect what the orchestrator delegated, which role is active, or whether a sprint session has rolled over.

Human-readable sprint tracking files live under:

- `teams_generated/shared_workspace/backlog.md`
- `teams_generated/shared_workspace/completed_backlog.md`
- `teams_generated/shared_workspace/current_sprint.md`
- `teams_generated/shared_workspace/sprint_history/`

## Next Reading

- [Configuration Guide](./configuration_guide.md)
- [Operations Guide](./operations_guide.md)
- [Architecture](./architecture.md)
