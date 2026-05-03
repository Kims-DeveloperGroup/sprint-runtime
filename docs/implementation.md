# `teams_runtime` Implementation Notes

This document describes the current implemented package layout. The target refactor structure and placement rules are tracked in [`architecture_policy.md`](./architecture_policy.md). The target `adapters/`, additional `shared/*`, `workflows/`, and `templates/*` paths now exist on disk as compatibility facades and migration seams. Most remaining monolith behavior still lives in `workflows/orchestration/team_service.py` and legacy runtime facades, while the `adapters/discord/*`, `shared/*`, `workflows/roles/*`, `workflows/orchestration/engine.py`, `workflows/orchestration/ingress.py`, `workflows/orchestration/relay.py`, `workflows/orchestration/notifications.py`, `workflows/sprints/lifecycle.py`, and `workflows/sprints/reporting.py` seams now own real implementation logic instead of wrapper-only re-exports.

## Current Implemented Package Layout

```text
teams_runtime/
  cli.py
  adapters/
    cli/
      commands.py
    discord/
      client.py
      lifecycle.py
  shared/
    __init__.py
    config.py
    formatting.py
    models.py
    paths.py
    persistence.py
  models.py
  core/
    backlog_store.py
    internal_relay.py
    notifications.py
    request_reply.py
    relay_delivery.py
    relay_summary.py
    request_store.py
    sprint_reporting.py
    sprint_store.py
    workflow_engine.py
    workflow_role_policy.py
    workflow_state.py
  discord/
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
    prompts/
    scaffold/
  tests/
  workflows/
    orchestration/
      engine.py
      ingress.py
      notifications.py
      relay.py
      team_service.py
    roles/
    sprints/
      lifecycle.py
      reporting.py
    state/
      backlog_store.py
      request_store.py
      sprint_store.py
```

Current ownership notes:

- `teams_runtime/cli.py`
  - legacy CLI facade that preserves the import-stable command surface and patch points used by existing tests and callers while delegating parser/dispatch/command implementation to the adapter layer
- `teams_runtime/adapters/cli/commands.py`
  - canonical home of argparse parser registration, top-level CLI dispatch plumbing, and CLI command implementation helpers
- `teams_runtime/workflows/*`
  - landed target-package surfaces for orchestration/state/role/sprint implementation while import migration continues
- `teams_runtime/workflows/state/*.py`
  - canonical home of backlog/request/sprint persistence, event helpers, planner-review request predicates/lookups/record assembly, internal sprint request predicates/iteration, backlog/review fingerprinting, sourcer and blocked-backlog review candidate normalization/rendering, fallback sourcer candidates, non-actionable backlog classification/drop/repair, backlog status/blocker/todo-state helpers, sprint selected-backlog view derivation, backlog kind/acceptance normalization, and backlog status-report context helpers, with `core/*_store.py` kept as compatibility facades
- `teams_runtime/workflows/orchestration/ingress.py`
  - canonical home of command/envelope parsing, freeform-shape detection, requester-route extraction/construction/merge, request-ingress record/seed/fingerprint assembly, duplicate-request fingerprint helpers, blocked-duplicate retry/augment glue, request-resume mutation, planning-envelope explicit-source detection, inferred verification enrichment, forwarded-request requester metadata packaging, request-identity matching, relay-intake milestone gating, reply-route recovery, manual sprint ingress request detection, kickoff text section parsing, milestone extraction, kickoff payload assembly, initial user-intake delegation mutation, duplicate requester-reply payload rendering, status/cancel/action/forward command handlers, and planning-verification artifact selection / blocked-request matching helpers; `core/parsing.py` and `core/request_reply.py` remain compatibility aliases
- `teams_runtime/workflows/orchestration/relay.py`
  - canonical home of relay delivery status/event glue, internal relay file queue helpers, internal relay summary framing, internal relay envelope dispatch/consume helpers, and transport branching between internal relay persistence versus Discord relay-channel delivery; role-result-aware relay body summarization now lives with the delegation handoff helpers; `core/internal_relay.py`, `core/relay_delivery.py`, and `core/relay_summary.py` remain compatibility aliases
- `teams_runtime/workflows/orchestration/engine.py`
  - canonical home of pure workflow constants, state normalization, transition parsing, routing decisions, governed routing-selection scoring, planner/QA report guardrails, and planner-owned artifact policy helpers; `core/workflow_state.py`, `core/workflow_engine.py`, and `core/workflow_role_policy.py` remain compatibility facades
- `teams_runtime/workflows/orchestration/delegation.py`
  - canonical home of delegated request processing, delegated request claim/release, local orchestrator request execution, role report intake normalization, role-result application, post-report routing adapter glue, handoff routing payloads, semantic result context, semantic internal-relay body summaries, request snapshots, and delegate envelopes
- `teams_runtime/workflows/roles/__init__.py`
  - canonical home of the role prompt registry, version-controller extra response fields, and agent utilization capability metadata used by orchestration scoring; `core/agent_capabilities.py` remains compatibility-only
- `teams_runtime/workflows/orchestration/notifications.py`
  - canonical home of Discord notification delivery, low-level chunking, runtime signature tagging, cross-process send locking, startup/fallback report delivery, sourcer report client selection, sourcer activity report rendering, sourcer report state/failure-log policy, requester-summary simplification, requester status-message assembly, requester reply-route recovery / dispatch glue, channel reply delegation, immediate receipts, sprint completion user-summary delivery, sprint progress report delivery, internal relay summary delivery, Discord relay-envelope sending, generic Discord content send delegation, and startup notification send/fallback state glue; `core/notifications.py` remains a compatibility alias
- `teams_runtime/workflows/sprints/lifecycle.py`
  - canonical home of sprint ID, scheduler slot, artifact-folder naming, sprint folder/attachment filename policy, todo construction/ranking/sorting/status derivation, recovered-todo construction/merge/reconciliation policy, manual sprint flow detection, manual sprint names, idle current-sprint markdown, manual cutoff policy, manual sprint state assembly, initial planning phase step metadata/helpers, sprint-relevant backlog selection, initial-phase validation policy, sprint planning request record assembly, planning-iteration bookkeeping, phase-ready policy, autonomous sprint execution, active-sprint resume, continue/finalize flow, internal sprint request creation, internal request chain delegation, todo execution, restart-checkpoint preparation, and task-commit enforcement
- `teams_runtime/adapters/discord/*`
  - canonical Discord client and background lifecycle management; `discord/*` remains compatibility-only
- `teams_runtime/shared/formatting.py`
  - canonical home of report/progress formatting helpers, backlog item construction, backlog markdown rendering, and current-sprint markdown rendering; `core/reports.py` remains compatibility-only and `core/sprints.py` re-exports the moved sprint-facing helpers for compatibility
- `teams_runtime/shared/config.py`
  - canonical home of runtime and Discord config loading/updating; `core/config.py` remains compatibility-only
- `teams_runtime/shared/paths.py`
  - canonical home of the `RuntimePaths` workspace/path contract; `core/paths.py` remains compatibility-only
- `teams_runtime/shared/persistence.py`
  - canonical home of shared JSON/JSONL persistence helpers, runtime ID/fingerprint helpers, and KST timestamp helpers; `core/persistence.py` remains compatibility-only
- `teams_runtime/workflows/orchestration/team_service.py`
  - canonical home of `TeamService` and orchestration-side composition, with manual sprint ingress parsing delegated through `workflows/orchestration/ingress.py`, delegated request/result handling delegated through `workflows/orchestration/delegation.py`, relay transport/consume glue delegated through `workflows/orchestration/relay.py`, requester-facing status/reply/channel/receipt/startup send shaping delegated through `workflows/orchestration/notifications.py`, and sprint lifecycle execution delegated through `workflows/sprints/lifecycle.py`; `core/orchestration.py` remains compatibility-only
- `teams_runtime/core/internal_relay.py`, `teams_runtime/core/relay_delivery.py`, and `teams_runtime/core/relay_summary.py`
  - compatibility aliases for relay helpers now owned by `workflows/orchestration/relay.py`
- `teams_runtime/workflows/sprints/reporting.py`
  - canonical home of sprint report headline, overview, timeline, delivered-change title/behavior/artifact/why assembly, sprint report snapshot assembly, planner closeout context/artifact/request/envelope assembly, closeout request-id/path utility policy, terminal state update plus closeout-result state/payload assembly, report path text, artifact preview/status-label/line-limit helpers, planner initial-phase activity report key/section/body assembly, report-body field parsing/derived closeout refresh, history-archive refresh gating, history archive markdown/index/path preparation and write/refresh side effects, history index parsing/rendering, sprint artifact path mapping plus index/kickoff/milestone/plan/spec/todo-backlog/iteration-log rendering, kickoff preview/body rendering, role-report contract/validation-trace rendering, Spec/TODO report section/body formatting, history archive report_path update decision, report archive report_body/report_path state update, and terminal sprint report title/judgment/commit/artifact assembly, change-summary behavior/meaning/how rendering, agent-contribution, issue, achievement, and artifact helper rendering plus machine summary, sprint/backlog status rendering, progress summary, full report-body, sprint report delivery body/artifact/progress-report/context assembly, terminal report section composition, and user-facing/live sprint report markdown assembly
- `teams_runtime/runtime/codex.py`
  - compatibility-only surface that re-exports the generic runtime, internal helper runtimes, research runtime, session manager, and subprocess runner for legacy imports
- `teams_runtime/shared/models.py`
  - canonical home of shared typed runtime contracts and dataclasses
- `teams_runtime/runtime/identities.py`
  - current home of runtime identity naming and filename sanitization; exposes `service_identity`, `local_identity`, and `sanitize_identity` with `*_runtime_identity` compatibility aliases
- `teams_runtime/core/template.py`
  - compatibility scaffold facade that assembles the workspace from file-backed assets in `templates/scaffold/*` and `templates/prompts/*`

## Package Layout

### Top level

- `teams_runtime/cli.py`
  - legacy CLI facade, workspace resolution, command wrappers, status/list surfaces, and test-compatible patch points
- `teams_runtime/adapters/cli/commands.py`
  - argparse parser construction, top-level CLI dispatch helper, and command implementation helpers used by the legacy CLI facade
- `teams_runtime/shared/models.py`
  - canonical shared contracts: role/session config dataclasses plus typed request/backlog/sprint/workflow/result shapes
- `teams_runtime/shared/config.py`
  - canonical runtime and Discord config loading, validation, placeholder-ID guardrails, and runtime config mutation helpers
- `teams_runtime/shared/paths.py`
  - canonical `RuntimePaths` workspace, runtime state, log, role, shared workspace, sprint artifact, and archive path contract
- `teams_runtime/shared/persistence.py`
  - canonical JSON/JSONL helpers, runtime ID/fingerprint helpers, and KST timestamp helpers
- `teams_runtime/shared/formatting.py`
  - canonical report/progress text box rendering, process/log summary helpers, backlog item construction, backlog markdown rendering, and current-sprint markdown rendering
- `teams_runtime/models.py`
  - compatibility facade re-exporting `teams_runtime.shared.models`
- `teams_runtime/requirements.txt`
  - package-local dependency list

### `core/`

- `config.py`
  - compatibility facade for shared config helpers in `shared/config.py`
- `paths.py`
  - compatibility facade for shared path helpers in `shared/paths.py`
- `persistence.py`
  - compatibility facade for shared persistence helpers in `shared/persistence.py`
- `backlog_store.py`
  - canonical backlog file IO and markdown refresh helpers
- `agent_capabilities.py`
  - compatibility facade for role capability metadata and agent utilization policy helpers in `workflows/roles/__init__.py`
- `request_store.py`
  - canonical request record file IO helpers
- `request_reply.py`
  - compatibility alias for requester-route helpers in `workflows/orchestration/ingress.py`
- `sprint_reporting.py`
  - compatibility facade for sprint reporting helpers in `workflows/sprints/reporting.py`
- `sprint_store.py`
  - canonical sprint state file IO and current-sprint rendering helpers
- `workflow_state.py`
  - compatibility facade for pure workflow constants, normalization, transition parsing, and state-transition helpers in `workflows/orchestration/engine.py`
- `workflow_engine.py`
  - compatibility facade for pure workflow routing-policy decisions in `workflows/orchestration/engine.py`
- `workflow_role_policy.py`
  - compatibility facade for planner/QA workflow report guardrails and planner-owned artifact policy helpers in `workflows/orchestration/engine.py`
- `parsing.py`
  - compatibility facade for command/envelope parsing and freeform-shape detection in `workflows/orchestration/ingress.py`
- `reports.py`
  - compatibility facade for report/progress formatting helpers in `shared/formatting.py`
- `internal_relay.py`, `relay_delivery.py`, and `relay_summary.py`
  - compatibility aliases for relay helpers in `workflows/orchestration/relay.py`
- `actions.py`
  - compatibility facade for registered action execution in `workflows/repository_ops.py`
- `git_ops.py`
  - compatibility facade for sprint git baseline capture plus machine-friendly task/closeout version-control helpers in `workflows/repository_ops.py`
- `sprints.py`
  - compatibility facade for shared backlog/current-sprint formatting helpers plus sprint lifecycle/reporting helpers in `workflows/sprints/*`
- `orchestration.py`
  - compatibility alias for `TeamService` in `workflows/orchestration/team_service.py`
- `template.py`
  - default workspace scaffold facade and template asset loading

### `templates/`

- `templates/scaffold/*`
  - file-backed scaffold assets for root config/docs, shared workspace seed files, runtime operator skill assets, role skills, and internal version-controller skill assets
- `templates/prompts/*.md`
  - file-backed public role and version-controller prompt assets used during workspace scaffold generation
- `templates/prompts/internal/*.md`
  - file-backed parser/sourcer internal agent prompt assets used during workspace scaffold generation

### `adapters/`

- `adapters/cli/commands.py`
  - target-package CLI parser/dispatch layer used by `cli.py`
- `adapters/discord/client.py`
  - canonical Discord send/listen client, message/attachment contracts, retry behavior, and validation helpers
- `adapters/discord/lifecycle.py`
  - canonical background runtime service PID/lock/state lifecycle helpers

### `workflows/`

- `workflows/orchestration/team_service.py`
  - canonical `TeamService` composition surface, backlog intake, scheduler loop, sprint execution, relay handling, and orchestration-side glue while decomposition continues
- `workflows/orchestration/engine.py`
  - canonical pure workflow constants, normalized workflow state, transition parsing, routing-policy decisions, governed routing-selection scoring, planner/QA report guardrails, and planner-owned artifact policy helpers
- `workflows/orchestration/ingress.py`
  - canonical home of command/envelope parsing, freeform-shape detection, requester-route helpers, request-ingress record/seed/fingerprint assembly, duplicate-request fingerprint helpers, blocked-duplicate retry/augment glue, request-resume mutation, planning-envelope explicit-source detection, inferred verification enrichment, forwarded-request requester metadata packaging, request-identity matching, relay-intake milestone gating, reply-route recovery, manual sprint ingress detection, kickoff milestone/brief/requirement parsing, kickoff payload assembly, initial user-intake delegation mutation, duplicate requester-reply payload rendering, status/cancel/action/forward command handlers, and planning-verification artifact selection / blocked-request matching helpers
- `workflows/orchestration/relay.py`
  - target-package relay transport seam that now owns relay delivery mutation/event glue, internal relay file queue helpers, generic internal relay summary framing, internal relay consume/dispatch helpers, and internal-vs-Discord transport branching
- `workflows/orchestration/delegation.py`
  - target-package delegation seam that owns delegated request processing, delegated request claim/release, local orchestrator request execution, role report intake normalization, role-result application, post-report routing adapter glue, request snapshots, delegate envelope/body/context assembly, handoff payloads, semantic result context, and semantic relay-body summaries
- `workflows/orchestration/notifications.py`
  - target-package notification seam that now owns low-level Discord notification delivery plus requester-facing notification orchestration glue
- `workflows/state/*.py`
  - canonical backlog/request/sprint persistence, event helpers, planner-review request predicates/lookups/record assembly, internal sprint request predicates/iteration, backlog/review fingerprinting, sourcer and blocked-backlog review candidate normalization/rendering, fallback sourcer candidates, non-actionable backlog classification/drop/repair, backlog status/blocker/todo-state helpers, sprint selected-backlog view derivation, backlog kind/acceptance normalization, and backlog status-report context helpers
- `workflows/sprints/lifecycle.py`
  - target-package sprint lifecycle seam that now owns sprint ID, scheduler slot, artifact-folder naming, sprint folder/attachment filename policy, todo construction/ranking/sorting/status derivation, recovered-todo construction/merge/reconciliation policy, manual sprint flow detection, manual sprint names, idle current-sprint markdown, manual cutoff policy, manual sprint state assembly, initial planning phase step metadata/helpers, sprint-relevant backlog selection, initial-phase validation policy, sprint planning request record assembly, planning-iteration bookkeeping, phase-ready policy, autonomous sprint execution, active-sprint resume, continue/finalize flow, internal sprint request creation, internal request chain delegation, todo execution, restart-checkpoint preparation, and task-commit enforcement
- `workflows/sprints/reporting.py`
  - canonical sprint reporting, closeout, closeout request-id/path utility policy, artifact preview/status-label/line-limit helpers, planner initial-phase activity report key/section/body assembly, report-body parsing/refresh, report history, history index parsing/rendering, sprint artifact path/index/spec/iteration markdown helpers, kickoff preview/body rendering, Spec/TODO report section/body formatting, sprint report delivery body/artifact/progress-report/context assembly, and terminal report section composition
- `workflows/roles/*.py`
  - canonical role-specific prompt/payload shaping modules; `workflows/roles/__init__.py` also owns the role prompt registry and role capability metadata consumed by orchestration
- `workflows/repository_ops.py`
  - canonical registered action execution, sprint git baseline capture, and machine-friendly task/closeout version-control helpers

### `discord/`

- `client.py`
  - standalone Discord send/listen client
- `lifecycle.py`
  - per-role background service management

### `runtime/`

- `identities.py`
  - runtime identity naming and sanitization helpers, including `service_identity`, `local_identity`, and `sanitize_identity`
- `architect_role.py`
  - architect-specific planning-specialist and implementation-review prompt rules used by the generic runtime wrapper
- `codex.py`
  - compatibility facade for split runtime modules
- `developer_role.py`
  - developer-specific implementation-step and revision-step prompt rules used by the generic runtime wrapper
- `orchestrator_role.py`
  - orchestrator-specific intake/control-action prompt rules used by the generic runtime wrapper
- `planner_role.py`
  - planner-specific prompt rules and planner proposal normalization helpers used by the generic runtime wrapper
- `qa_role.py`
  - QA-specific validation-step prompt rules and reopen guidance used by the generic runtime wrapper
- `role_registry.py`
  - compatibility facade for role prompt registration in `workflows/roles/__init__.py`
- `session_manager.py`
  - canonical runtime session lifecycle, archive behavior, and session workspace seeding
- `designer_role.py`
  - designer-specific advisory prompt rules used by the generic runtime wrapper
- `version_controller_role.py`
  - version-controller task/closeout commit prompt rules used by the generic runtime wrapper

## Migration Status

- The target `adapters/`, additional `shared/*`, and `workflows/` packages now exist on disk and are safe import targets for new internal callers.
- Some landed target packages remain compatibility facades; state-store/shared/Discord ownership has moved to target packages, while most remaining monolith behavior still executes inside `core/*` and `runtime/*`.
- Discord client/lifecycle behavior now belongs in `adapters/discord/*`; `discord/*` remains compatibility-only.
- `TeamService` now belongs in `workflows/orchestration/team_service.py`; `core/orchestration.py` remains compatibility-only.
- The target migration structure and architecture rules live in [`architecture_policy.md`](./architecture_policy.md).
- Shared contracts now belong in `shared/models.py`.
- Canonical report/progress formatting helpers, backlog item construction, backlog markdown rendering, and current-sprint markdown rendering belong in `shared/formatting.py`; `core/reports.py` remains compatibility-only and `core/sprints.py` re-exports moved formatting helpers.
- Canonical runtime and Discord config loading/updating belongs in `shared/config.py`; `core/config.py` remains compatibility-only.
- Canonical workspace/runtime path helpers belong in `shared/paths.py`; `core/paths.py` remains compatibility-only.
- Canonical shared JSON/JSONL persistence, ID/fingerprint generation, and KST timestamp helpers belong in `shared/persistence.py`; `core/persistence.py` remains compatibility-only.
- Canonical request/backlog/sprint file IO, event helpers, planner-review request predicates/lookups/record assembly, internal sprint request predicates/iteration, backlog/review fingerprinting, sourcer and blocked-backlog review candidate normalization/rendering, fallback sourcer candidates, non-actionable backlog classification/drop/repair, backlog status/blocker/todo-state helpers, sprint selected-backlog view derivation, backlog kind/acceptance normalization, and backlog status-report context helpers belong in `workflows/state/request_store.py`, `workflows/state/backlog_store.py`, and `workflows/state/sprint_store.py`; `core/*_store.py` remains compatibility-only.
- Canonical pure workflow-state helpers belong in `workflows/orchestration/engine.py`; `core/workflow_state.py` remains compatibility-only.
- Canonical pure workflow routing-policy decisions and governed routing-selection scoring belong in `workflows/orchestration/engine.py`; `core/workflow_engine.py` remains compatibility-only.
- Canonical role capability metadata and agent utilization policy loading belong in `workflows/roles/__init__.py`; `core/agent_capabilities.py` remains compatibility-only.
- Canonical startup report rendering, boxed-report excerpt summarization, sourcer report client selection, sourcer activity report rendering, sourcer report state/failure-log policy, low-level Discord chunking, runtime signature tagging, cross-process send locking, startup fallback recovery, requester-status message formatting, requester reply delivery, immediate receipts, sprint completion user-summary delivery, sprint progress report delivery, internal relay summary delivery, and Discord relay-envelope sending belong in `workflows/orchestration/notifications.py`; `core/notifications.py` remains compatibility-only.
- Canonical command/envelope parsing, freeform-shape detection, requester-route extraction, construction, merge, request-ingress record/seed/fingerprint assembly, duplicate-request fingerprint helpers, blocked-duplicate retry/augmentation mutation, request-resume mutation, planning-envelope explicit-source detection, inferred verification enrichment, forwarded-request requester metadata packaging, request-identity matching, relay-intake milestone gating, reply-route recovery, and status/cancel/action/forward command handlers belong in `workflows/orchestration/ingress.py`; `core/parsing.py` and `core/request_reply.py` remain compatibility-only.
- Canonical sprint report headline, overview, timeline, delivered-change title/behavior/artifact/why assembly, sprint report snapshot assembly, planner closeout context/artifact/request/envelope assembly, closeout request-id/path utility policy, terminal state update plus closeout-result state/payload assembly, report path text, artifact preview/status-label/line-limit helpers, planner initial-phase activity report key/section/body assembly, report-body field parsing/derived closeout refresh, history-archive refresh gating, history archive markdown/index/path preparation and write/refresh side effects, history index parsing/rendering, sprint artifact path mapping plus index/kickoff/milestone/plan/spec/todo-backlog/iteration-log rendering, kickoff preview/body rendering, role-report contract/validation-trace rendering, Spec/TODO report section/body formatting, history archive report_path update decision, report archive report_body/report_path state update, and terminal sprint report title/judgment/commit/artifact assembly, change-summary behavior/meaning/how rendering, agent-contribution, issue, achievement, and artifact helper rendering plus machine summary, sprint/backlog status rendering, progress summary, full report-body, sprint report delivery body/artifact/progress-report/context assembly, terminal report section composition, and user-facing/live sprint report markdown assembly belongs in `workflows/sprints/reporting.py`; `core/sprint_reporting.py` and `core/sprints.py` remain compatibility-only for those helpers.
- Canonical relay-send status mutation, relay failure-payload shaping, internal relay file-transport helpers, inbox scanning/loading, synthetic relay-message stubs, pure action resolution utilities, relay-summary fragment wrapping, relay-section grouping, and section-message rendering belong in `workflows/orchestration/relay.py`; `core/internal_relay.py`, `core/relay_delivery.py`, and `core/relay_summary.py` remain compatibility-only.
- Canonical role-specific workflow report guardrails belong in `workflows/orchestration/engine.py`; `core/workflow_role_policy.py` remains compatibility-only.
- Architect-specific prompt shaping belongs in `workflows/roles/architect.py`.
- Developer-specific prompt shaping belongs in `workflows/roles/developer.py`.
- Orchestrator-specific prompt shaping belongs in `workflows/roles/orchestrator.py`.
- Planner-specific runtime prompt/payload shaping belongs in `workflows/roles/planner.py`.
- Research-specific prepass prompt/rule parsing belongs in `workflows/roles/research.py`.
- Research runtime orchestration and external deep-research execution belong in `runtime/research_runtime.py`.
- QA-specific prompt shaping belongs in `workflows/roles/qa.py`.
- Designer-specific advisory prompt shaping belongs in `workflows/roles/designer.py`.
- Runtime-side role prompt registration belongs in `workflows/roles/__init__.py`.
- Shared role runtime execution and payload normalization belong in `runtime/base_runtime.py`.
- Runtime subprocess execution and JSON output recovery belong in `runtime/codex_runner.py`.
- Internal parser runtime and status-intent normalization belong in `runtime/internal/intent_parser.py`.
- Internal backlog sourcing runtime and backlog candidate normalization belong in `runtime/internal/backlog_sourcing.py`.
- Runtime session lifecycle and workspace seeding belong in `runtime/session_manager.py`.
- Version-controller prompt shaping belongs in `workflows/roles/version_controller.py`.
- `cli.py` now delegates parser/dispatch and command implementation plumbing to `adapters/cli/commands.py` while preserving the legacy import surface used by existing tests and callers.
- `core/template.py` now loads scaffold assets from `templates/scaffold/*`; workspace scaffold generation uses `templates/prompts/*` for public roles plus version-controller and `templates/prompts/internal/*` for parser/sourcer. In-module prompt/scaffold blobs have been removed.
- New orchestration/reporting/runtime behavior may now target either the canonical legacy module or its landed target-package wrapper, but source-of-truth implementation ownership remains with the legacy module until the wrapper stops being a pure facade. State-store behavior now targets `workflows/state/*`.

## Runtime Data Layout

Machine-readable state:

- `<workspace>/.teams_runtime/backlog/`
- `<workspace>/.teams_runtime/sprints/`
- `<workspace>/.teams_runtime/requests/`
- `<workspace>/.teams_runtime/internal_relay/`
- `<workspace>/.teams_runtime/role_sessions/`
  - one active metadata file per `runtime_identity`, stored as `<sanitized_runtime_identity>.json`
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
- `internal/sourcer/`
- `internal/version_controller/`

Timestamp convention:

- `teams_runtime` persists runtime-generated timestamps in `Asia/Seoul`
- JSON/JSONL/markdown state files use ISO-8601 with `+09:00`
- generated sprint IDs use the local KST date/time form `YYMMDD-Sprint-HH:MM`, for example `260324-Sprint-09:00`
- sprint artifact folders use a filesystem-safe form of `sprint_id` under `shared_workspace/sprints/`
- sprint state keeps both a refined milestone title and immutable kickoff source fields so planner can preserve the original sprint-start brief while deriving execution framing separately
- inbound Discord attachments resolve to the active sprint folder; sprint-start attachments are relocated into the newly created sprint folder and recorded as sprint reference artifacts
- scheduler state and operational reports expose only the active runtime sprint id via `active_sprint_id`; sprint records themselves keep a single `sprint_id`
- public service runtimes still use role-name session files such as `planner.json`, while orchestrator-local helper runtimes use distinct files such as `orchestrator.local.planner.json`

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
- capability routing is bounded by planner ownership and workflow policy; it does not bypass normal planner-first intake, research-first sprint initial policy, bounded advisory passes, the mandatory architect/developer review chain, sourcer-review-to-planner, blocked planning resume, or version-controller-only flows
- planner owns backlog-management decisions such as add/update/dedupe/reprioritize and persists those backlog changes directly into runtime backlog state
- planner backlog persistence uses the canonical backlog helper boundary with `backlog_items` / `backlog_item` payloads, and planner returns `proposals.backlog_writes` receipts after those writes succeed
- backlog/todo definition is research-informed and `spec-first`: sprint backlog must be derived from the current milestone, kickoff requirements, research report, and `spec.md`, not only from pre-existing queue state
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
- immutable kickoff source fields plus `kickoff.md` are persisted from the sprint-start request before research and planner derive execution framing
- the first initial-phase request is delegated to `research` as `research_initial`, including manual `sprint start` flows
- the research prepass defines the research subject, sources or local-evidence/no-subject rationale, and planning hints before planner refines the milestone
- planner then runs this initial phase:
  - `milestone_refinement`
  - `artifact_sync`
  - `backlog_definition`
  - `backlog_prioritization`
  - `todo_finalization`
- `backlog_definition` must create or reopen sprint-relevant backlog from `milestone + kickoff requirements + research report + spec`
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
- role runtime runs in a sprint-scoped session isolated by `runtime_identity`
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

- `workflows/orchestration/team_service.py` remains the compatibility composition root. Status/cancel/action/forward command handlers now live in `workflows/orchestration/ingress.py`; sprint execution bodies, internal sprint request creation, request-chain delegation, restart-checkpoint flow, and task-commit enforcement now live in `workflows/sprints/lifecycle.py`; role-result application, delegated request claim/release, delegated request processing, local orchestrator request execution, role-report intake normalization, post-report routing adapter glue, handoff routing payloads, semantic result context, semantic internal-relay body summaries, request snapshots, and delegate envelopes live in `workflows/orchestration/delegation.py`.
- Remaining workflow-policy cutover work is concentrated around legacy compatibility wrappers that still need request/backlog context from `TeamService` before calling pure `engine.py` helpers.
- Legacy patch/import surfaces such as `teams_runtime.core.orchestration.build_active_sprint_id`, `capture_git_baseline`, and `inspect_sprint_closeout` remain honored through `TeamService` adapter methods while the extracted lifecycle helpers own the execution flow.
- sourcer fallback discovery is still heuristic when the internal sourcer cannot run
- sprint selection currently pulls from all pending backlog items instead of a bounded WIP budget
- no parallel multi-role fanout yet
- sprint commit generation depends on git availability and a clean-enough baseline snapshot

## Maintainer Checklist

When changing the sprint model, check these together:

1. `shared/config.py`
2. `shared/paths.py`
3. `shared/formatting.py`
4. `workflows/sprints/lifecycle.py`
5. `workflows/sprints/reporting.py`
6. `workflows/repository_ops.py`
7. `workflows/orchestration/delegation.py`
8. `workflows/orchestration/team_service.py`
9. `core/sprints.py` compatibility exports
10. `core/template.py`
11. `cli.py`
12. `teams_runtime/tests/`

## Test Command

`teams_runtime` uses Python's standard-library `unittest` runner for
package-local tests. Do not use `pytest` as the `teams_runtime` test execution
tool.

Current regression commands:

```bash
# From the parent repository directory:
python -m unittest discover -s teams_runtime/tests

# From the teams_runtime package directory:
python -m unittest discover -s tests
```
