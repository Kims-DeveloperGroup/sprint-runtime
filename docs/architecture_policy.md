# `teams_runtime` Architecture Policy

This document defines how `teams_runtime` is structured today, what target structure the refactor is moving toward, and the rules that keep the package architecture coherent while migration is in progress.

## Current Implemented Structure

This is the current on-disk package structure and the source of truth until new modules are actually created:

```text
teams_runtime/
  cli.py
  shared/
    __init__.py
    models.py
  models.py
  core/
    actions.py
    agent_capabilities.py
    backlog_store.py
    config.py
    git_ops.py
    internal_relay.py
    notifications.py
    orchestration.py
    parsing.py
    paths.py
    persistence.py
    request_reply.py
    relay_delivery.py
    relay_summary.py
    reports.py
    request_store.py
    sprint_reporting.py
    sprints.py
    sprint_store.py
    template.py
    workflow_engine.py
    workflow_role_policy.py
    workflow_state.py
  discord/
    client.py
    lifecycle.py
  runtime/
    architect_role.py
    base_runtime.py
    codex.py
    codex_runner.py
    developer_role.py
    designer_role.py
    identities.py
    internal/
      backlog_sourcing.py
      intent_parser.py
    orchestrator_role.py
    planner_role.py
    qa_role.py
    research_role.py
    research_runtime.py
    role_registry.py
    session_manager.py
    version_controller_role.py
  templates/
  tests/
```

Current ownership:

- `cli.py`
  - CLI entrypoint and workspace resolution
- `adapters/discord/client.py`
  - canonical Discord send/listen client, message/attachment contracts, retry behavior, and validation helpers
- `adapters/discord/lifecycle.py`
  - canonical background runtime service PID/lock/state lifecycle helpers
- `workflows/orchestration/team_service.py`
  - canonical `TeamService` composition surface, backlog intake, scheduler loop, sprint execution, relay handling, and orchestration-side glue while decomposition continues
- `workflows/roles/__init__.py`
  - canonical role prompt registry, version-controller extra response fields, and role capability metadata used as orchestration routing inputs
- `shared/models.py`
  - canonical shared runtime contracts, dataclasses, and fixed role constants
- `shared/config.py`
  - canonical runtime and Discord config loading, validation, placeholder-ID guardrails, and runtime config mutation helpers
- `shared/paths.py`
  - canonical `RuntimePaths` workspace, runtime state, log, role, shared workspace, sprint artifact, and archive path contract
- `shared/persistence.py`
  - canonical JSON/JSONL helpers, runtime ID/fingerprint helpers, and KST timestamp helpers
- `shared/formatting.py`
  - canonical report/progress text box rendering, process/log summary helpers, backlog item construction, backlog markdown rendering, and current-sprint markdown rendering
- `models.py`
  - compatibility facade re-exporting `shared/models.py`
- `core/`
  - workflow-policy integration, sprint/backlog helpers, and template scaffolding
  - `orchestration.py` remains a compatibility alias for `workflows/orchestration/team_service.py`
  - `config.py` remains a compatibility facade for `shared/config.py`
  - `paths.py` remains a compatibility facade for `shared/paths.py`
  - `persistence.py` remains a compatibility facade for `shared/persistence.py`
  - `reports.py` remains a compatibility facade for `shared/formatting.py`
  - `actions.py` remains a compatibility facade for registered action execution in `workflows/repository_ops.py`
  - `agent_capabilities.py` remains a compatibility facade for role capability metadata and agent utilization policy helpers in `workflows/roles/__init__.py`
  - `backlog_store.py`, `request_store.py`, and `sprint_store.py` remain compatibility facades for `workflows/state/*`
  - `workflow_engine.py` remains a compatibility facade for `workflows/orchestration/engine.py`
  - `request_reply.py` remains a compatibility alias for requester-route and ingress helpers in `workflows/orchestration/ingress.py`
  - `sprint_reporting.py` remains a compatibility facade for `workflows/sprints/reporting.py`
  - `sprints.py` remains a compatibility facade for shared backlog/current-sprint formatting plus sprint lifecycle/reporting helpers in `workflows/sprints/*`
  - `internal_relay.py`, `relay_delivery.py`, and `relay_summary.py` remain compatibility aliases for `workflows/orchestration/relay.py`
  - `notifications.py` remains a compatibility alias for `workflows/orchestration/notifications.py`
  - `workflow_role_policy.py` remains a compatibility facade for planner/QA workflow report guardrails and planner-owned artifact policy in `workflows/orchestration/engine.py`
  - `workflow_state.py` remains a compatibility facade for pure workflow constants and state transitions in `workflows/orchestration/engine.py`
- `discord/`
  - compatibility aliases for `adapters/discord/*`
- `runtime/`
  - runtime identities, session lifecycle, Codex subprocess execution, and role/helper runtimes
  - architect-specific planning-specialist and implementation-review prompt rules stay in `architect_role.py`
  - canonical shared role runtime execution, prompt framing, and payload normalization stay in `base_runtime.py`
  - canonical Codex/Gemini subprocess execution and JSON output recovery stay in `codex_runner.py`
  - developer-specific implementation-step and revision-step prompt rules stay in `developer_role.py`
  - internal parser runtime plus status-intent normalization stay in `internal/intent_parser.py`
  - internal backlog sourcing runtime plus backlog candidate normalization stay in `internal/backlog_sourcing.py`
  - research runtime orchestration and external deep-research execution stay in `research_runtime.py`
  - `*_role.py` and `role_registry.py` remain compatibility facades for `workflows/roles/*`
  - runtime session lifecycle and workspace seeding stay in `session_manager.py`
  - version-controller task/closeout commit prompt rules stay in `version_controller_role.py`
  - `codex.py` remains compatibility-only for split runtime modules
- `templates/`
  - packaged template assets

`workflows/orchestration/team_service.py` now owns the `TeamService` surface, but it is still a large composition module to keep compatibility while helper seams continue moving outward. `runtime/base_runtime.py`, `runtime/codex_runner.py`, `runtime/session_manager.py`, `runtime/research_runtime.py`, and `runtime/internal/*` own runtime behavior; `runtime/codex.py` remains compatibility-only for legacy imports.
Shared contract additions should land in `shared/models.py`, not back in the legacy `models.py` shim.

## Target Migration Structure

This is the target structure the merged refactor plan is moving toward. It is not the current source of truth unless the modules exist on disk.

```text
teams_runtime/
  cli.py
  adapters/
    cli/
    discord/
  shared/
    models.py
    config.py
    paths.py
    persistence.py
    formatting.py
  runtime/
    identities.py
    codex_runner.py
    session_manager.py
    base_runtime.py
    internal/
      intent_parser.py
      backlog_sourcing.py
  workflows/
    orchestration/
      team_service.py
      ingress.py
      relay.py
      engine.py
      notifications.py
    state/
      backlog_store.py
      request_store.py
      sprint_store.py
    sprints/
      lifecycle.py
      reporting.py
    roles/
      orchestrator.py
      research.py
      planner.py
      designer.py
      architect.py
      developer.py
      qa.py
      version_controller.py
    repository_ops.py
  templates/
    scaffold/
    prompts/
```

## Placement And Dependency Rules

Layer ownership rules:

- `adapters/`
  - CLI and Discord entrypoints only
- `shared/`
  - pure contracts, config/path/persistence helpers, and formatting helpers
- `runtime/`
  - runtime identity, session persistence, workspace seeding, subprocess execution, and runtime wrappers
- `workflows/state/`
  - canonical backlog/request/sprint persistence, event APIs, planner-review request predicates/lookups/record assembly, internal sprint request predicates/iteration, backlog/review fingerprinting, sourcer and blocked-backlog review candidate normalization/rendering, fallback sourcer candidates, non-actionable backlog classification/drop/repair, backlog status/blocker/todo-state helpers, sprint selected-backlog view derivation, and backlog status-report context helpers
- `workflows/roles/`
  - role-specific prompt assembly, payload normalization, validation, role registry, and role capability metadata
  - capability metadata describes role strengths and signals, but routing decisions remain outside role modules
- `workflows/orchestration/`
  - routing, workflow engine, orchestration side effects, and `TeamService`
- `workflows/sprints/`
  - sprint lifecycle/report helpers, including todo construction/ranking/sorting/status derivation, recovered-todo construction/merge/reconciliation policy, sprint folder/attachment filename policy, and sprint planning request record assembly
  - sprint report rendering, closeout shaping, closeout request-id/path utility policy, artifact preview/status-label/line-limit helpers, planner initial-phase activity report key/section/body assembly, report-body parsing/refresh, history/archive helpers and write/refresh side effects, sprint report delivery body/artifact/progress-report/context assembly, terminal report section composition, sprint artifact path/spec/iteration/kickoff markdown rendering, and Spec/TODO report section formatting
- `templates/`
  - scaffold and prompt assets only

Dependency rules:

- `shared/` must not depend on `runtime/`, `workflows/`, or `adapters/`.
- `runtime/` may depend on `shared/`, but not on `adapters/` or workflow-policy modules.
- `workflows/state/` may depend on `shared/` only.
- `workflows/roles/` may depend on `shared/` and template-loading helpers, but not on `adapters/`.
- `workflows/orchestration/` may depend on `shared/`, `runtime/`, `workflows/state/`, `workflows/roles/`, and `workflows/sprints/`.
- `adapters/` may depend on lower layers, but lower layers must not depend on `adapters/`.

Policy rules:

- Workflow routing, governed routing-selection scoring, reopen decisions, and next-role selection stay centralized in `workflows/orchestration/engine.py`.
- Role modules are handlers, not routing authorities.
- Canonical backlog/request/sprint writes must go through state-store APIs once `workflows/state/*` exists.
- New target-structure packages should only be introduced with a concrete owning migration step, not as partial stubs scattered across the tree.

## Runtime Identity And Session Rules

- `runtime_identity` is distinct from `role`.
- Public service runtimes use the role name as identity, such as `planner` or `developer`.
- Orchestrator-local helper runtimes use `<owner>.local.<target>`, such as `orchestrator.local.planner`.
- Runtime identity helpers are `service_identity(role)`, `local_identity(owner_role, target_role)`, and `sanitize_identity(identity)`.
- `service_runtime_identity`, `local_runtime_identity`, and `sanitize_runtime_identity` remain compatibility aliases while imports migrate.
- Session files live at `.teams_runtime/role_sessions/<sanitized_runtime_identity>.json`.
- The unsanitized `runtime_identity` value is persisted inside the session JSON.
- Different runtime identities must never share a session file or session workspace.
- New helper runtimes must define their identity naming rule and add isolation tests before they ship.
- Operator-facing CLI surfaces remain role-oriented even when multiple runtime identities exist internally.

## Migration And Documentation Rules

- Compatibility re-exports must name the owning target module in a comment or docstring once they are introduced.
- Every shim needs a documented removal condition in `implementation.md`.
- Docs must not describe target-only modules as current implementation.
- When changing module boundaries, storage layout, runtime identity policy, or operator-visible behavior, update these docs together:
  - `README.md`
  - `docs/specification.md`
  - `docs/architecture.md`
  - `docs/implementation.md`
  - `docs/architecture_policy.md`
