# `teams_runtime` Design Notes

## Design Goals

- Be reusable across unrelated projects
- Keep the runtime self-contained
- Make workspace state inspectable on disk
- Keep role routing inspectable and debuggable
- Preserve role continuity during a sprint
- Avoid arbitrary command execution from chat

## Key Design Decisions

### 1. Standalone package boundary

Decision:

- `teams_runtime` does not import repo-local `libs.*` modules or project policy files.

Why:

- makes the package portable
- avoids hidden coupling to this repository
- keeps future packaging/distribution simpler

Tradeoff:

- some code is duplicated instead of reused

### 2. Workspace-driven configuration

Decision:

- every project provides a workspace root with config and role files

Why:

- the runtime should be generic while each project stays configurable
- prompts, roles, and action registries belong to the workspace, not the package

Tradeoff:

- initial setup is more explicit
- maintainer docs stay package-local instead of traveling with each generated workspace

### 3. Default generated path is `teams_generated`

Decision:

- omitted `--workspace-root` resolves to `./teams_generated` unless the current directory is already a workspace

Why:

- keeps generated team files out of the project root by default
- still keeps CLI use simple from inside the workspace itself

Tradeoff:

- there is one more default path rule to remember

### 4. Orchestrator as sole request authority

Decision:

- every request is normalized through the orchestrator

Why:

- makes deduplication, bounded routing, and final status consistent
- avoids split ownership across roles

Tradeoff:

- adds an extra hop for direct messages to non-orchestrator roles

### 5. Sequential role handoff

Decision:

- only one downstream active role at a time in v1

Why:

- simpler request state
- easier debugging and workflow-policy handling
- fewer merge/conflict cases in shared artifacts

Tradeoff:

- slower than parallel fanout for some workflows

### 6. Bot IDs are config-owned

Decision:

- `bot_id` is required in `discord_agents_config.yaml`

Why:

- mention rendering must be deterministic
- trusted relay acceptance should not depend on runtime discovery

Tradeoff:

- maintainers must keep bot IDs current by hand

### 7. Internal relay transport by default

Decision:

- use internal direct relay between roles by default
- keep Discord relay-channel transport as an explicit debug mode
- emit relay summaries to Discord in both modes for operator visibility

Why:

- keeps role-to-role payload traffic readable and durable without raw Discord envelope noise
- preserves existing debug path when full envelope visibility on Discord is required

Tradeoff:

- adds one relay-mode switch to operational commands
- requires maintaining internal relay inbox/archive paths
### 8. Sprint-scoped runtime sessions

Decision:

- each runtime identity keeps one session for the configured sprint session scope and refreshes when `sprint.id` changes
- public service runtimes and orchestrator-local helper runtimes do not share session state even when they target the same logical role

Why:

- preserves useful working memory across related tasks
- creates a clear reset boundary between sprint contexts
- prevents helper invocations from polluting the service runtime's session history

Tradeoff:

- stale context can persist until sprint rollover if the sprint is left unchanged too long

### 9. Lazy sprint refresh

Decision:

- changing `sprint.id` does not immediately rebuild all role sessions; refresh happens when each role next executes work

Why:

- avoids unnecessary work for idle roles
- keeps restart behavior simple

Tradeoff:

- session rollover is not simultaneous across all roles unless they all receive work

### 10. Action registry instead of arbitrary shell

Decision:

- `execute` only runs actions declared in `team_runtime.yaml`

Why:

- safer than interpreting arbitrary command text
- easier to audit and maintain
- action behavior stays explicit in config

Tradeoff:

- operators must define commands up front

## Maintainer Guidance

### When to update the specification doc

Update `specification.md` when changing:

- config schema
- supported intents
- session policy
- request ownership rules
- execution contract

### When to update the architecture doc

Update `architecture.md` when changing:

- component boundaries
- request flow
- relay behavior
- runtime storage layout
- failure isolation behavior

### When to update the architecture policy doc

Update `architecture_policy.md` when changing:

- module ownership
- dependency direction
- compatibility shim rules
- runtime identity rules
- documentation update obligations

### When to update the implementation doc

Update `implementation.md` when changing:

- source file layout
- runtime paths
- test locations
- current limitations
- maintainer checklist

## Known Design Tensions

- The package is self-contained, but template scaffolding still lives in Python code instead of packaged template assets.
- Sequential orchestration is easier to reason about, but it limits throughput.
- Bot-ID-based trust is explicit, but it requires manual config maintenance.
- Sprint persistence improves continuity, but it can preserve outdated context if sprint hygiene is weak.
- The package is migrating toward a clearer layered layout, but current ownership still lives in `core/orchestration.py` and `runtime/codex.py` until later phases land.

## Recommended Future Refinements

- move workspace templates from code-generated strings to packaged assets
- add richer request timeline/status views
- add stronger schema validation for role JSON responses
- support optional parallel planner/designer/architect fanout with orchestrator merge rules
- add packaged installation metadata so `teams_runtime` can be installed cleanly outside this repo
