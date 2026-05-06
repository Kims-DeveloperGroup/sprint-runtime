# `teams_runtime`

## Documentation Index

- [Quickstart](./docs/quickstart.md)
- [Configuration Guide](./docs/configuration_guide.md)
- [Operations Guide](./docs/operations_guide.md)
- [Implementation Notes](./docs/implementation.md)
- [Architecture](./docs/architecture.md)
- [Specification](./docs/specification.md)

## Concept

`teams_runtime` is a standalone Python package for running a small Discord-connected agent team against a project workspace. Think of it as a compact production crew: each agent has a role, and the orchestrator keeps the handoffs orderly.

The public roles are `orchestrator`, `research`, `planner`, `designer`, `architect`, `developer`, and `qa`. The orchestrator receives user requests and controls routing; research grounds sprint planning; planner turns context into backlog, specs, and todos; designer advises on UX and message clarity; architect shapes technical direction and review; developer builds; QA validates.

The internal runtime agents are `parser`, `sourcer`, and `version_controller`. They support intent parsing, backlog candidate discovery, and git-backed task or sprint closeout without becoming public Discord roles.

Normal change requests are backlog-first instead of freeform implementation. Sprint kickoff starts with the `research` prepass, then planner refines the milestone and turns the plan into sprint-ready backlog and todos. Sprint execution follows the orchestrator-governed chain: planner output, architect guidance, developer build, architect review, QA validation, then version-controller closeout when there are owned changes to commit.

## Integration Tools

Required:

- **Discord**: configure role bot tokens, role bot IDs, and `relay_channel_id`; `startup_channel_id` and `report_channel_id` are optional convenience channels.
- **Codex CLI**: the runtime starts role agents through the `codex` command on `PATH`.
- **git**: sprint closeout, task commit checks, and commit reporting depend on git being available.

Optional:

- **GitHub CLI `gh`**: enables best-effort sprint issue publishing. Authenticate with `gh auth login`, or set `GH_TOKEN` / `GITHUB_TOKEN`.
- **Deep research backend**: Gemini, NotebookLM, Drive-style file keywords, and browser profile settings are used only when external research is configured.

The Discord client loads the nearest `.env` automatically, so local tokens may be exported in the shell or stored in a workspace-adjacent `.env`.

## Installation

With Python 3.10+:

```bash
pip install -r requirements.txt
```

## Quick Start

### 1. Scaffold a workspace

```bash
python -m teams_runtime init
```

Workspace resolution uses the current directory if it already contains both config files, then `./teams_generated`, then `./workspace/teams_generated`.

Use `--reset` only when you intentionally want to rebuild generated runtime content; it preserves `discord_agents_config.yaml` and archived sprint history.

```bash
python -m teams_runtime init --reset
```

### 2. Configure Discord

Edit `<workspace-root>/discord_agents_config.yaml` and replace every placeholder snowflake before starting listeners.

```yaml
relay_channel_id: "123456789012345678"
startup_channel_id: "123456789012345679"
report_channel_id: "123456789012345680"

agents:
  orchestrator: {name: orchestrator, role: orchestrator, description: Request routing, token_env: AGENT_DISCORD_TOKEN_ORCHESTRATOR, bot_id: "123456789012345681"}
  research: {name: research, role: research, description: Pre-planning research, token_env: AGENT_DISCORD_TOKEN_RESEARCH, bot_id: "123456789012345682"}
  planner: {name: planner, role: planner, description: Planning and PRD, token_env: AGENT_DISCORD_TOKEN_PLANNER, bot_id: "123456789012345683"}
  designer: {name: designer, role: designer, description: UX and response style, token_env: AGENT_DISCORD_TOKEN_DESIGNER, bot_id: "123456789012345684"}
  architect: {name: architect, role: architect, description: Architecture and review, token_env: AGENT_DISCORD_TOKEN_ARCHITECT, bot_id: "123456789012345685"}
  developer: {name: developer, role: developer, description: Implementation, token_env: AGENT_DISCORD_TOKEN_DEVELOPER, bot_id: "123456789012345686"}
  qa: {name: qa, role: qa, description: Quality assurance, token_env: AGENT_DISCORD_TOKEN_QA, bot_id: "123456789012345687"}

internal_agents:
  sourcer: {name: CS_ADMIN, role: sourcer, description: Internal backlog sourcing reporter, token_env: AGENT_DISCORD_TOKEN_CS_ADMIN, bot_id: "123456789012345688"}
```

`relay_channel_id` is required. `startup_channel_id` defaults to the relay channel when omitted, and `report_channel_id` defaults to the startup channel.

### 3. Configure runtime policy

Edit `<workspace-root>/team_runtime.yaml`. The only required runtime field is `sprint.id`.

```yaml
sprint:
  id: "2026-Sprint-03"
  interval_minutes: 180
  timezone: "Asia/Seoul"
  mode: "hybrid"
  start_mode: "auto"
  cutoff_time: "22:00"
  overlap_policy: "no_overlap"
  ingress_mode: "backlog_first"
  discovery_scope: "broad_scan"
  discovery_actions: []

ingress:
  dm: true
  mentions: true

allowed_guild_ids: []

role_defaults:
  research: {model: "gpt-5.5", reasoning: "medium"}
  planner: {model: "gpt-5.5", reasoning: "xhigh"}
  developer: {model: "gpt-5.3-codex-spark", reasoning: "xhigh"}

research_defaults: {app: "", notebook: "", files: [], mode: "", profile_path: "", completion_timeout: 600, callback_timeout: 1200, cleanup: false}

actions: {}
```

`actions: {}` is valid. In that mode, orchestration and sprint planning still work, but user `execute` requests remain disabled.

You can update role defaults through the CLI:

```bash
python -m teams_runtime config role set --agent developer --model gpt-5.5 --reasoning high
python -m teams_runtime config research set --app "Gemini Research App" --file "market.md"
```

### 4. Export bot tokens

```bash
export AGENT_DISCORD_TOKEN_ORCHESTRATOR=...
export AGENT_DISCORD_TOKEN_RESEARCH=...
export AGENT_DISCORD_TOKEN_PLANNER=...
export AGENT_DISCORD_TOKEN_DESIGNER=...
export AGENT_DISCORD_TOKEN_ARCHITECT=...
export AGENT_DISCORD_TOKEN_DEVELOPER=...
export AGENT_DISCORD_TOKEN_QA=...
export AGENT_DISCORD_TOKEN_CS_ADMIN=...
```

### 5. Start the runtime

```bash
python -m teams_runtime start
python -m teams_runtime status
python -m teams_runtime list
```

The default relay transport is `internal`, which keeps role-to-role handoff payloads inside runtime state and posts compact monitoring summaries to Discord. Use Discord relay envelopes only when debugging relay traffic:

```bash
python -m teams_runtime start --relay-transport discord
python -m teams_runtime run --relay-transport discord
```

### 6. Start or control a sprint

```bash
python -m teams_runtime sprint start --milestone "Login workflow cleanup"
python -m teams_runtime sprint start --milestone "Login workflow cleanup" --brief "Preserve current relay flow" --requirement "Keep kickoff docs as source of truth"
python -m teams_runtime sprint status
python -m teams_runtime sprint stop
python -m teams_runtime sprint restart
```

Manual and scheduled sprint kickoff both begin with `research` before planner milestone refinement.

### 7. Send a request

DM or mention the `orchestrator` bot:

```text
intent: plan
scope: Draft the login workflow and define backlog items
```

Messaging another public role still routes through the orchestrator so request ownership, backlog state, and requester replies stay consistent.

## Workspace Overview

Generated workspaces contain `discord_agents_config.yaml`, `team_runtime.yaml`, role folders, internal agent folders, and `shared_workspace/`. Runtime machine state lives under `.teams_runtime/`, while human-readable planning and sprint artifacts live under `shared_workspace/`.

Package docs stay under [`docs/`](./docs/README.md) and are not copied into generated workspaces.

## Development

Run the test suite with:

```bash
python -m unittest discover -s tests
```
