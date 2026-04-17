# `teams_runtime`

Standalone multi-agent Discord workflow runtime for backlog-first project execution.

`teams_runtime` is a self-contained Python package that runs a fixed team of Discord-connected roles against a project workspace. It handles request intake, planner-owned backlog management, autonomous sprint execution, role session management, relay transport, and git-backed closeout without depending on repo-local `libs.*` modules.

## What It Provides

- 6 fixed public roles: `orchestrator`, `planner`, `designer`, `architect`, `developer`, `qa`
- 3 internal runtime agents: `parser`, `sourcer`, `version_controller`
- orchestrator-first request intake over Discord DMs and mentions
- backlog-first routing instead of immediate freeform execution
- autonomous or manual sprint lifecycle
- a fixed execution chain for sprint work:
  `planner -> architect guidance -> developer -> architect review -> qa -> version_controller`
- workspace scaffolding for role folders, shared docs, and runtime state
- runtime-selectable relay transport:
  `internal` by default, `discord` for relay-channel debugging
- persisted machine-readable state under `.teams_runtime/`
- human-readable planning and sprint artifacts under `shared_workspace/`

## Workflow Model

Normal user requests do not go straight to implementation.

1. The `orchestrator` receives the Discord message first.
2. Planning and backlog decisions go to `planner`.
3. Planner persists backlog changes directly.
4. The scheduler or operator starts a sprint from actionable backlog.
5. Sprint todos run through the orchestrator-governed workflow contract.
6. `version_controller` handles task closeout and commit reporting.

Planning and implementation are intentionally separated. Planner-owned surfaces such as `shared_workspace/backlog.md`, `completed_backlog.md`, and `current_sprint.md` stay under planning ownership; execution roles do not redefine those files as implementation output.

## Requirements

- Python 3.10+
- `git` on `PATH`
- `codex` CLI on `PATH`
- Discord bot tokens for the configured roles

The Discord client also loads the nearest `.env` file automatically, so tokens may be exported in the shell or stored in a local `.env`.

## Installation

Install the package dependencies:

```bash
pip install -r requirements.txt
```

Commands below assume `teams_runtime` is importable, either because you are running from the parent directory that contains this package or because that parent directory is on `PYTHONPATH`.

## Quick Start

### 1. Scaffold a workspace

```bash
python -m teams_runtime init
```

Workspace resolution is computed as:

1. current directory if it already contains both `team_runtime.yaml` and `discord_agents_config.yaml`
2. `./teams_generated`
3. `./workspace/teams_generated`

`init` uses `<workspace-root>` as the resolved target and writes generated runtime content there. If the target already exists, it preserves:

- `discord_agents_config.yaml`
- archived sprint history under `shared_workspace/sprint_history/`

### 2. Configure Discord bots

Edit `<workspace-root>/discord_agents_config.yaml` and replace every scaffold placeholder snowflake before starting listeners.

Minimal example:

```yaml
relay_channel_id: "123456789012345678"
startup_channel_id: "123456789012345679"
report_channel_id: "123456789012345680"

agents:
  orchestrator:
    name: "Orchestrator"
    role: "orchestrator"
    description: "Owns request routing"
    token_env: "AGENT_DISCORD_TOKEN_ORCHESTRATOR"
    bot_id: "123456789012345681"
  planner:
    name: "Planner"
    role: "planner"
    description: "Planning role"
    token_env: "AGENT_DISCORD_TOKEN_PLANNER"
    bot_id: "123456789012345682"
  designer:
    name: "Designer"
    role: "designer"
    description: "UX and message advisory role"
    token_env: "AGENT_DISCORD_TOKEN_DESIGNER"
    bot_id: "123456789012345683"
  architect:
    name: "Architect"
    role: "architect"
    description: "Architecture and code review role"
    token_env: "AGENT_DISCORD_TOKEN_ARCHITECT"
    bot_id: "123456789012345684"
  developer:
    name: "Developer"
    role: "developer"
    description: "Implementation role"
    token_env: "AGENT_DISCORD_TOKEN_DEVELOPER"
    bot_id: "123456789012345685"
  qa:
    name: "QA"
    role: "qa"
    description: "Validation role"
    token_env: "AGENT_DISCORD_TOKEN_QA"
    bot_id: "123456789012345686"

internal_agents:
  sourcer:
    name: "CS_ADMIN"
    role: "sourcer"
    description: "Internal backlog sourcing reporter"
    token_env: "AGENT_DISCORD_TOKEN_CS_ADMIN"
    bot_id: "123456789012345687"
```

Notes:

- `relay_channel_id` is required
- `startup_channel_id` defaults to `relay_channel_id` if omitted
- `report_channel_id` defaults to `startup_channel_id` if omitted
- every public role requires `name`, `role`, `description`, `token_env`, and `bot_id`
- `internal_agents` is optional; the scaffold includes `sourcer` by default

### 3. Configure runtime policy

Edit `<workspace-root>/team_runtime.yaml`.

The only required field is:

```yaml
sprint:
  id: "2026-Sprint-03"
```

A practical starter config looks like:

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
  planner:
    model: "gpt-5.4"
    reasoning: "xhigh"
  developer:
    model: "gpt-5.3-codex-spark"
    reasoning: "xhigh"

actions: {}
```

`actions: {}` is valid. In that mode, orchestration still works, but user `execute` requests remain disabled.

You can also update role defaults through the CLI:

```bash
python -m teams_runtime config role set --agent developer --model gpt-5.4 --reasoning high
```

### 4. Export bot tokens

```bash
export AGENT_DISCORD_TOKEN_ORCHESTRATOR=...
export AGENT_DISCORD_TOKEN_PLANNER=...
export AGENT_DISCORD_TOKEN_DESIGNER=...
export AGENT_DISCORD_TOKEN_ARCHITECT=...
export AGENT_DISCORD_TOKEN_DEVELOPER=...
export AGENT_DISCORD_TOKEN_QA=...
export AGENT_DISCORD_TOKEN_CS_ADMIN=...
```

### 5. Start the runtime

```bash
# default relay transport: internal (relay summary + persisted envelope)
python -m teams_runtime start
# internal relay persists full envelope at:
# <workspace-root>/.teams_runtime/internal_relay/inbox/<role>/<relay_id>.json
# then moves processed relays to:
# <workspace-root>/.teams_runtime/internal_relay/archive/<role>/<relay_id>.json
# and posts a compact summary to relay channel
# internal mode includes summary posts to relay channel while persisting full payload in files

# debug relay transport: Discord relay-channel envelopes
python -m teams_runtime start --relay-transport discord
```

Foreground mode is also available:

```bash
# internal relay default
python -m teams_runtime run
# explicit envelopes for debugging
python -m teams_runtime run --relay-transport discord
```

### 6. Check runtime status

```bash
python -m teams_runtime status
python -m teams_runtime status --backlog
python -m teams_runtime sprint status
python -m teams_runtime list
```

### 7. Start or control a sprint

```bash
python -m teams_runtime sprint start --milestone "Login workflow cleanup"
python -m teams_runtime sprint start --milestone "Login workflow cleanup" --brief "Preserve current relay flow" --requirement "Keep kickoff docs as source of truth"
python -m teams_runtime sprint stop
python -m teams_runtime sprint restart
```

### 8. Send a request

DM or mention the `orchestrator` bot with a request such as:

```text
intent: plan
scope: Draft the login workflow and define backlog items
```

User-originated requests are orchestrator-first. Messaging another role directly still routes through the orchestrator as the runtime governor.

## Common Commands

```bash
python -m teams_runtime init
python -m teams_runtime start
python -m teams_runtime stop
python -m teams_runtime restart
python -m teams_runtime status
python -m teams_runtime status --request-id 20260323-abcd1234
python -m teams_runtime start --agent orchestrator
python -m teams_runtime status --agent developer
python -m teams_runtime config role set --agent planner --model gpt-5.4 --reasoning medium
python -m teams_runtime sprint status
```

## Workspace Layout

Generated workspace (resolved `<workspace-root>`):

```text
<workspace-root>/
├── discord_agents_config.yaml
├── team_runtime.yaml
├── communication_protocol.md
├── file_contracts.md
├── COMMIT_POLICY.md
├── orchestrator/
├── planner/
├── designer/
├── architect/
├── developer/
├── qa/
├── internal/
└── shared_workspace/
```

`internal/` below is a container for generated internal workspaces:
`internal/parser`, `internal/sourcer`, `internal/version_controller`.
(`discord_agents_config.yaml`의 `internal_agents.sourcer`는 Discord presence/reporting 설정 샘플이며,
`internal/` 하위 동시 생성되는 작업공간 구성을 의미하지 않습니다.)

Role-session workspace (seeded per runtime session):

```text
workspace/                  # symlink to project root
shared_workspace/           # runtime-owned symlink to shared space
.teams_runtime/             # runtime-owned symlink for state and receipts
communication_protocol.md   # seeded runtime context
file_contracts.md          # seeded runtime context
COMMIT_POLICY.md           # seeded runtime context
workspace_context.md        # seeded runtime context
```

각 role 디렉터리(`orchestrator`, `planner`, `designer`, `architect`, `developer`, `qa`)에는
세션 생성 시 기본 코디네이션 파일이 존재하며, 공통으로 `AGENTS.md`, `todo.md`, `history.md`,
`journal.md`, `sources/`가 남습니다.

Runtime state and generated artifacts are written under:

- `.teams_runtime/backlog/`
- `.teams_runtime/requests/`
- `.teams_runtime/sprints/`
- `.teams_runtime/role_sessions/`
- `.teams_runtime/internal_relay/inbox/<role>/`
- `.teams_runtime/internal_relay/archive/<role>/` (processed relay archive)
- `logs/agents/`
- `logs/discord/`
- `logs/operations/`
- `shared_workspace/backlog.md`
- `shared_workspace/completed_backlog.md`
- `shared_workspace/current_sprint.md`
- `shared_workspace/sprints/<sprint_folder_name>/`
- `shared_workspace/sprint_history/`

Package docs stay under [`docs/`](./docs/README.md) and are not copied into runtime-generated workspaces.

## Relay Transport

`teams_runtime` supports two relay modes between roles:

- `internal`
  persisted in-runtime handoff with full envelope persistence at
  `<workspace-root>/.teams_runtime/internal_relay/inbox/<role>/<relay_id>.json`,
  then moves processed envelopes to
  `<workspace-root>/.teams_runtime/internal_relay/archive/<role>/<relay_id>.json`,
  and posts a relay summary text in relay channel
- `discord`
  explicit relay-channel envelopes with target mentions for debugging and replay inspection

Select the mode with `--relay-transport` on `run`, `start`, or `restart`.

## Development

Run the test suite with:

```bash
python -m unittest discover -s tests
```

## Documentation

User guides:

- [Quickstart](./docs/quickstart.md)
- [Configuration Guide](./docs/configuration_guide.md)
- [Operations Guide](./docs/operations_guide.md)

Maintainer/reference docs:

- [Docs Index](./docs/README.md)
- [Specification](./docs/specification.md)
- [Architecture](./docs/architecture.md)
- [Design](./docs/design.md)
- [Implementation Notes](./docs/implementation.md)

## Notes

- Placeholder Discord IDs in the scaffold are rejected at runtime unless a test-only override is enabled.
- Planner is the only role that persists planner-owned backlog changes (canonical payload: `backlog_items` / `backlog_item`).
- Planner persists canonical payload (`backlog_items` / `backlog_item`) and `proposals.backlog_writes` receipts.
  Orchestrator validates persisted payload/receipts and runtime state; it does not re-persist planner proposals.
- `planned_backlog_updates` is compatibility alias for transition compatibility, not a new planning contract.
- `start`, `run`, `restart` are command-line transport targets for `--relay-transport`; other commands keep defaults.
- Sprint start cannot continue with zero actionable sprint backlog; the runtime blocks with `planning_incomplete` instead.
- `status --sprint` still works as a compatibility alias for `sprint status`.
