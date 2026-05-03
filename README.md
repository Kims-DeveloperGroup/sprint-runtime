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

## Package Layout Status

- `cli.py` remains the legacy import-stable entrypoint facade and test-compatible patch surface.
- `adapters/cli/commands.py` now owns argparse parser registration, top-level command dispatch, and shared CLI command implementation plumbing.
- `adapters/discord/*` now owns Discord client and background lifecycle behavior; `discord/*` remains as compatibility aliases.
- `shared/formatting.py` now owns report/progress formatting helpers, backlog item construction, backlog markdown rendering, and current-sprint markdown rendering; `core/reports.py` remains as a compatibility facade, and `core/sprints.py` re-exports the moved formatting helpers for compatibility.
- `shared/config.py` now owns runtime and Discord config loading/updating; `core/config.py` remains as a compatibility facade.
- `shared/paths.py` now owns the `RuntimePaths` workspace/path contract; `core/paths.py` remains as a compatibility facade.
- `shared/persistence.py` now owns shared JSON/JSONL persistence helpers; `core/persistence.py` remains as a compatibility facade.
- `workflows/state/*.py` now owns backlog/request/sprint persistence, event helpers, planner-review request predicates/lookups/record assembly, internal sprint request predicates/iteration, backlog/review fingerprinting, sourcer and blocked-backlog review candidate normalization/rendering, fallback sourcer candidates, non-actionable backlog classification/drop/repair, backlog status/blocker/todo-state helpers, sprint selected-backlog view derivation, backlog kind/acceptance normalization, and backlog status-report context helpers; `core/*_store.py` remains as compatibility facades.
- `workflows/orchestration/team_service.py` now owns `TeamService`; `core/orchestration.py` remains as a compatibility alias.
- `workflows/orchestration/engine.py` now owns pure workflow state, routing decisions, governed routing-selection scoring, and role report guardrails; `core/workflow_engine.py` remains as a compatibility facade.
- `workflows/roles/__init__.py` now owns the role prompt registry and agent utilization capability metadata; `core/agent_capabilities.py` remains as a compatibility facade.
- `workflows/repository_ops.py` now owns registered action execution and sprint git/version-control helpers; `core/actions.py` and `core/git_ops.py` remain as compatibility facades.
- `workflows/orchestration/ingress.py` now owns requester-route extraction, request-ingress record/seed/fingerprint assembly, duplicate-request fingerprint helpers, blocked-duplicate retry/augment glue, request-resume mutation, planning-envelope source detection, forwarded-request requester metadata packaging, relay-intake milestone gating, reply-route recovery, manual sprint ingress detection, kickoff payload parsing, duplicate requester-reply payload rendering, and planning-verification artifact selection / blocked-request matching helpers; `core/request_reply.py` remains as a compatibility alias.
- `workflows/orchestration/relay.py` now owns relay delivery status/event glue, internal relay file queue helpers, relay summary rendering, internal relay consume/dispatch helpers, and transport branching between internal relay persistence versus Discord relay-channel delivery; `core/internal_relay.py`, `core/relay_delivery.py`, and `core/relay_summary.py` remain as compatibility aliases.
- `workflows/orchestration/notifications.py` now owns Discord notification delivery, chunking, startup/fallback reports, sourcer report client selection, sourcer activity report rendering, sourcer report state/failure-log policy, requester summary simplification, requester status-message assembly, requester reply-route recovery / dispatch glue, channel reply delegation, immediate-receipt trusted-relay suppression, generic Discord content send delegation, and startup notification send/fallback state glue; `core/notifications.py` remains as a compatibility alias.
- `workflows/sprints/lifecycle.py` now owns sprint IDs, scheduler slots, artifact-folder naming, sprint folder/attachment filename policy, todo construction/ranking/sorting/status derivation, recovered-todo construction/merge/reconciliation policy, manual sprint flow detection, manual sprint names, idle current-sprint markdown, manual cutoff policy, manual sprint state assembly, initial planning phase step metadata, sprint-relevant backlog selection, initial-phase validation policy, sprint planning request record assembly, planning-iteration bookkeeping, and phase-ready policy; `core/sprints.py` remains as a compatibility facade.
- Most remaining monolith behavior still executes in `workflows/orchestration/team_service.py` and legacy runtime facades, while the target packages continue moving from re-export seams to owning modules.
- `core/template.py` now reads scaffold markdown assets from `templates/scaffold/*`; public role and version-controller prompts are file-backed through `templates/prompts/*`, while parser/sourcer internal prompts remain under `templates/prompts/internal/*` with in-module fallback.

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

`init` uses `<workspace-root>` as the resolved target. If the target is already a generated workspace, `init` refreshes copied prompts and packaged runtime skills without resetting `.teams_runtime/` or `shared_workspace/`.

Use `--reset` when you intentionally want to rebuild generated runtime content. Reset mode preserves:

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
    model: "gpt-5.5"
    reasoning: "xhigh"
  developer:
    model: "gpt-5.3-codex-spark"
    reasoning: "xhigh"

actions: {}
```

`actions: {}` is valid. In that mode, orchestration still works, but user `execute` requests remain disabled.

You can also update role defaults through the CLI:

```bash
python -m teams_runtime config role set --agent developer --model gpt-5.5 --reasoning high
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

Manual and scheduled sprint kickoff always begins with the `research` prepass before planner milestone refinement. The research report defines the external or local-evidence subject, provides sources or backing rationale, and is handed to planner so milestone refinement, specs, backlog, and todos are developed beyond the raw kickoff milestone.

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
python -m teams_runtime config role set --agent planner --model gpt-5.5 --reasoning medium
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
  - one active metadata file per runtime identity; public service runtimes still use role-name files such as `planner.json`
  - runtime identity helpers are `service_identity`, `local_identity`, and `sanitize_identity`; `*_runtime_identity` names remain compatibility aliases
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

`teams_runtime` uses Python's standard-library `unittest` runner for package-local
tests. Do not use `pytest` as the `teams_runtime` test execution tool.

From the package directory, run the test suite with:

```bash
python -m unittest discover -s tests
```

From the parent repository directory, use:

```bash
python -m unittest discover -s teams_runtime/tests
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
- [Architecture Policy](./docs/architecture_policy.md)
- [Design](./docs/design.md)
- [Implementation Notes](./docs/implementation.md)

## Notes

- Placeholder Discord IDs in the scaffold are rejected at runtime unless a test-only override is enabled.
- Planner is the only role that persists planner-owned backlog changes (canonical payload: `backlog_items` / `backlog_item`).
- Planner persists canonical payload (`backlog_items` / `backlog_item`) and `proposals.backlog_writes` receipts.
  Orchestrator validates persisted payload/receipts and runtime state; it does not re-persist planner proposals.
- `planned_backlog_updates` is compatibility alias for transition compatibility, not a new planning contract.
- Canonical shared runtime contracts now live in `teams_runtime/shared/models.py`; `teams_runtime/models.py` remains as a compatibility facade during migration.
- Canonical pure workflow state helpers, routing-policy decisions, governed routing-selection scoring, planner/QA workflow report guardrails, and planner-owned artifact filtering now live in `teams_runtime/workflows/orchestration/engine.py`; the related `teams_runtime/core/workflow_*.py` files remain compatibility facades.
- Canonical role capability metadata and agent utilization policy loading now live in `teams_runtime/workflows/roles/__init__.py`; `teams_runtime/core/agent_capabilities.py` remains a compatibility facade.
- Canonical requester-route extraction, construction, merge, request-ingress record/seed/fingerprint assembly, duplicate-request fingerprint helpers, blocked-duplicate retry/augmentation mutation, request-resume mutation, planning-envelope explicit-source detection, inferred verification enrichment, forwarded-request requester metadata packaging, request-identity matching, relay-intake milestone gating, and reply-route recovery now live in `teams_runtime/workflows/orchestration/ingress.py`; `teams_runtime/core/request_reply.py` remains a compatibility alias.
- Canonical sprint report headline, overview, timeline, delivered-change title/behavior/artifact/why assembly, sprint report snapshot assembly, planner closeout context/artifact/request/envelope assembly, closeout request-id/path utility policy, terminal state update plus closeout-result state/payload assembly, report path text, artifact preview/status-label/line-limit helpers, planner initial-phase activity report key/section/body assembly, report-body field parsing/derived closeout refresh, history-archive refresh gating, history archive markdown/index/path preparation and write/refresh side effects, history index parsing/rendering, sprint artifact path mapping plus index/kickoff/milestone/plan/spec/todo-backlog/iteration-log rendering, kickoff preview/body rendering, role-report contract/validation-trace rendering, Spec/TODO report section/body formatting, history archive report_path update decision, report archive report_body/report_path state update, and terminal sprint report title/judgment/commit/artifact assembly, change-summary behavior/meaning/how rendering, agent-contribution, issue, achievement, and artifact helper rendering plus machine summary, sprint/backlog status rendering, progress summary, full report-body, sprint report delivery body/artifact/progress-report/context assembly, terminal report section composition, and user-facing/live sprint report markdown assembly now live in `teams_runtime/workflows/sprints/reporting.py`.
- Canonical relay-send status mutation, relay failure-payload shaping, internal relay path/enqueue/archive, inbox scanning/loading, envelope round-trip helpers, synthetic relay-message stubs, pure action resolution, relay-summary fragment wrapping, relay-section grouping, and section-message rendering now live in `teams_runtime/workflows/orchestration/relay.py`.
- Canonical startup report rendering, boxed-report excerpt summarization, sourcer report client selection, sourcer activity report rendering, sourcer report state/failure-log policy, low-level Discord chunking, runtime signature tagging, cross-process send locking, startup fallback recovery, requester-status message formatting, requester reply delivery, immediate receipts, sprint completion user-summary delivery, sprint progress report delivery, internal relay summary delivery, Discord relay-envelope sending, and orchestration notification glue now live in `teams_runtime/workflows/orchestration/notifications.py`; `teams_runtime/core/notifications.py` remains a compatibility alias.
- `teams_runtime/core/internal_relay.py`, `teams_runtime/core/relay_delivery.py`, and `teams_runtime/core/relay_summary.py` remain compatibility aliases for relay helpers during import migration.
- Sprint ID, scheduler slot, artifact-folder naming, sprint folder/attachment filename policy, todo construction/ranking/sorting/status derivation, recovered-todo construction/merge/reconciliation policy, manual sprint flow detection, manual sprint names, idle current-sprint markdown, manual cutoff policy, manual sprint state assembly, initial planning phase step metadata, sprint-relevant backlog selection, initial-phase validation policy, sprint planning request record assembly, planning-iteration bookkeeping, and phase-ready policy now live in `teams_runtime/workflows/sprints/lifecycle.py`; `teams_runtime/core/sprints.py` remains compatibility-only.
- Planner-specific runtime prompt rules and proposal normalization now live in `teams_runtime/workflows/roles/planner.py`.
- Research-specific prepass prompt/rule parsing helpers now live in `teams_runtime/workflows/roles/research.py`.
- Canonical research runtime orchestration and external deep-research execution now live in `teams_runtime/runtime/research_runtime.py`.
- Designer-specific runtime prompt rules now live in `teams_runtime/workflows/roles/designer.py`.
- Architect-specific runtime prompt rules now live in `teams_runtime/workflows/roles/architect.py`.
- Developer-specific runtime prompt rules now live in `teams_runtime/workflows/roles/developer.py`.
- QA-specific runtime prompt rules now live in `teams_runtime/workflows/roles/qa.py`.
- Version-controller runtime prompt rules now live in `teams_runtime/workflows/roles/version_controller.py`.
- Canonical shared role runtime execution and payload normalization now live in `teams_runtime/runtime/base_runtime.py`.
- Canonical runtime subprocess execution and JSON output recovery now live in `teams_runtime/runtime/codex_runner.py`.
- Internal parser/runtime intent classification now lives in `teams_runtime/runtime/internal/intent_parser.py`.
- Internal backlog sourcing runtime now lives in `teams_runtime/runtime/internal/backlog_sourcing.py`.
- Canonical runtime session lifecycle and workspace seeding now live in `teams_runtime/runtime/session_manager.py`.
- Orchestrator runtime prompt rules now live in `teams_runtime/workflows/roles/orchestrator.py`.
- Runtime-side registration of role prompt modules now lives in `teams_runtime/workflows/roles/__init__.py`.
- `start`, `run`, `restart` are command-line transport targets for `--relay-transport`; other commands keep defaults.
- Sprint start cannot continue with zero actionable sprint backlog; the runtime blocks with `planning_incomplete` instead.
- `status --sprint` still works as a compatibility alias for `sprint status`.
