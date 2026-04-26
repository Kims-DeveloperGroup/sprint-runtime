---
name: backlog_management
description: Use this skill inside the planner agent workspace when the task is to manage backlog direction end-to-end, including additions, updates, reprioritization, dedupe judgments, and completed-backlog decisions, while expressing the result in planner-owned proposals.
---

# Backlog Management Skill

## When To Use

Use this skill when the planner is responsible for backlog management decisions, especially for:

- deciding whether a new request should become a backlog item
- updating or rewriting an existing backlog item
- reprioritizing backlog entries
- judging whether items are duplicates, overlaps, or should be merged
- deciding whether an item belongs in active backlog or completed backlog
- persisting backlog management outcomes directly from planner after the decision is made

Do not use this skill for implementation or qa execution. This skill includes planner-owned backlog persistence.

## Read First

- `Current request`
- `Current request.artifacts`
- `shared_workspace/backlog.md`
- `shared_workspace/completed_backlog.md`
- relevant planning/spec Markdown files
- any existing backlog records or prior planner outputs tied to the same scope

## Workflow

1. Identify the backlog decision to make.
   Distinguish add, update, dedupe, reprioritize, complete, or carry-over handling.
2. Read the current backlog context first.
   Inspect existing backlog items before inventing a new one.
3. Make the management decision explicit.
   State clearly whether work should be added, merged, updated, re-ranked, moved, or left unchanged.
4. Normalize titles toward functional change.
   When creating or rewriting backlog items, make the title describe the behavior or workflow-contract change. Replace activity-first labels like `정리`, `반영`, `문서`, `라우팅`, or `회귀 테스트` with the concrete functional delta whenever the source supports it.
5. Persist the backlog decision directly.
   Use the planner backlog persistence helper to update `.teams_runtime/backlog/*.json` and refresh `shared_workspace/backlog.md` / `completed_backlog.md`.
6. Express the result in planner output.
   Use `proposals.backlog_item`, `proposals.backlog_items`, or summary text as durable rationale after persistence, and mention affected `backlog_id` values when persistence happened.
7. Keep execution routing separate.
   If the request should continue beyond backlog management, leave clear downstream execution context so orchestrator can select the right next role.

## Guardrails

- Do not leave dedupe or merge judgments implicit.
- Do not assume orchestrator will invent missing backlog semantics after the planner response.
- Do not leave backlog persistence undone after deciding add/update/reprioritize/complete.
- Do not hand-edit backlog markdown when the helper can persist canonical state for you.
- Do not stop at prose-only commentary when a structured backlog decision is already clear.
- Do not keep a meta activity title when the source already tells you what changed functionally.

## Helper Command

Use this command when planner needs to persist backlog changes directly:

```bash
python -m teams_runtime.workflows.state.backlog_store merge --workspace-root ./workspace/teams_generated --input-file <payload.json> --source planner --request-id <request_id>
```
