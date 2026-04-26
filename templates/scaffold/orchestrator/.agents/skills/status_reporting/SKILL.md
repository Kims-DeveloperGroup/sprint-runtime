---
name: status_reporting
description: Use this skill inside the orchestrator agent workspace when the task is to answer backlog, request, or runtime status questions from persisted state and render a truthful operational summary.
---

# Status Reporting Skill

## When To Use

Use this skill when orchestrator is answering state or monitoring requests, especially for:

- backlog status or backlog sharing
- request/todo progress
- runtime service status summaries
- explaining whether work is pending, running, blocked, completed, or waiting for restart

Do not use this skill for sprint lifecycle commands or sprint status; use `sprint_orchestration` for those. Do not use this skill for speculative planning or for modifying execution state unless the request explicitly requires it.

## Read First

- `Current request`
- `.teams_runtime/requests/*.json`
- `.teams_runtime/sprints/*.json`
- `.teams_runtime/backlog/*.json`
- `shared_workspace/current_sprint.md`
- `shared_workspace/backlog.md`

## Workflow

1. Read persisted state first.
   Base the answer on files and runtime records, not memory or assumptions.
2. Prefer exact status words.
   Use concrete states such as pending, running, blocked, completed, or wrap_up.
3. Separate facts from next actions.
   Report current state first, then recommend what should happen next.
4. Keep summaries compact but auditable.
   Include the identifiers or artifacts needed for someone to verify the answer quickly.

## Guardrails

- Do not claim progress that is not reflected in persisted state.
- Do not guess missing sprint or backlog state from chat context alone.
- Do not hide blocker conditions when they affect execution readiness.
