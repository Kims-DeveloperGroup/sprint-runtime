---
name: teams-runtime
description: Operate and debug generated `teams_runtime` workspaces. Use when starting, stopping, restarting, listing, or inspecting `teams_runtime`; checking sprint, backlog, or request state; validating process liveness; investigating relay/runtime failures; or when users refer to the runtime as `teams`, `팀즈`, or `팀`.
---

# Teams Runtime

Use this skill to operate a generated `teams_runtime` workspace through its public CLI and persisted runtime artifacts. Prefer the packaged command surface over manual state edits, and collect evidence before reporting operational conclusions.

## Read First

Open the smallest relevant source set first:

- `README.md`
- `communication_protocol.md`
- `file_contracts.md`

When investigating runtime state, inspect these workspace artifacts as needed:

- `.teams_runtime/requests/`
- `.teams_runtime/backlog/`
- `.teams_runtime/sprints/`
- `shared_workspace/current_sprint.md`
- `shared_workspace/sprint_history/`
- `logs/agents/`
- `logs/discord/`

## Workflow

1. Use the public CLI first.
   Prefer `python -m teams_runtime ...` over reading or editing state files directly.
2. Gather evidence before reporting status.
   For operational work, check `list` and `status`, then use `ps` when process liveness matters.
3. Use the standard progress report for lifecycle operations.
   After `start`, `list`, `status`, `stop`, `restart`, `sprint start`, `sprint stop`, or `sprint restart`, emit the workspace-standard `[작업 보고]` with evidence from CLI output and `ps` when needed.
4. Prefer managed background operations.
   Default to `start`, `status`, `list`, `restart`, and `stop`. Use foreground `run` only when the user explicitly wants an attached process.
5. Keep relay mode intentional.
   Default to `--relay-transport internal`. Use `--relay-transport discord` only for relay debugging.
6. Treat `restart` as a manual lifecycle command.
   Do not assume automatic restart-on-change behavior.
7. Treat plain `init` as the safe prompt-refresh path for existing workspaces.
   `python -m teams_runtime init --workspace-root <generated_workspace_root>` refreshes copied role prompts and packaged runtime skills by default without resetting `.teams_runtime/` or `shared_workspace/`.
8. Treat `init --reset` as destructive to live runtime state.
   `python -m teams_runtime init --workspace-root <generated_workspace_root> --reset` rebuilds generated runtime content and should be called out before use. It preserves `discord_agents_config.yaml` and archived sprint history under `shared_workspace/sprint_history/`, but resets runtime state and shared workspace files.

## Core Commands

Common lifecycle commands:

```bash
python -m teams_runtime init --workspace-root .
python -m teams_runtime init --workspace-root . --reset
python -m teams_runtime start --workspace-root .
python -m teams_runtime status --workspace-root .
python -m teams_runtime list --workspace-root .
python -m teams_runtime restart --workspace-root .
python -m teams_runtime stop --workspace-root .
```

Target one agent when needed:

```bash
python -m teams_runtime start --workspace-root . --agent orchestrator
python -m teams_runtime status --workspace-root . --agent developer
python -m teams_runtime restart --workspace-root . --agent qa
python -m teams_runtime stop --workspace-root . --agent planner
```

Sprint control:

```bash
python -m teams_runtime sprint status --workspace-root .
python -m teams_runtime sprint start --workspace-root . --milestone "로그인 기능 워크플로 정리"
python -m teams_runtime sprint start --workspace-root . --milestone "로그인 기능 워크플로 정리" --brief "기존 relay flow 유지" --requirement "kickoff docs를 source-of-truth로 보존"
python -m teams_runtime sprint stop --workspace-root .
python -m teams_runtime sprint restart --workspace-root .
```

Use the bundled helper script when you want a compact read-only snapshot:

```bash
python ./.agents/skills/teams-runtime/scripts/collect_runtime_snapshot.py --workspace-root . --sprint --include-ps
python ./.agents/skills/teams-runtime/scripts/collect_runtime_snapshot.py --workspace-root . --agent orchestrator --log-role orchestrator
```

## Debug Flow

1. Confirm visible runtime state first.
   Run `list` and the smallest relevant `status` variant.
2. Confirm the process table if the runtime should be live.
   Use `ps` to verify the expected process exists before declaring the runtime stopped or hung.
3. Inspect persisted state next.
   Use `.teams_runtime/requests`, `.teams_runtime/sprints`, `.teams_runtime/backlog`, and `shared_workspace/current_sprint.md` to reconcile CLI output with stored state.
4. Read logs only after you know what failed.
   Prefer `logs/agents/<role>.log` for role/runtime execution and `logs/discord/` when Discord ingress or relay delivery is suspect.
5. Separate failure classes.
   Distinguish backlog intake, sprint scheduling, delegated request execution, and relay delivery problems instead of treating all failures as "runtime down."

## Guardrails

- Do not edit `.teams_runtime/*.json` directly for normal operations.
- Do not claim runtime state from chat context alone.
- Do not default to `run` for long-lived operation requests.
- Do not use `--relay-transport discord` unless relay debugging is the goal.
- Do not imply that code changes automatically require or trigger a restart.

## Verification

Useful smoke commands:

```bash
python -m teams_runtime --help
python -m teams_runtime sprint --help
python ./.agents/skills/teams-runtime/scripts/collect_runtime_snapshot.py --workspace-root . --sprint --include-ps
```
