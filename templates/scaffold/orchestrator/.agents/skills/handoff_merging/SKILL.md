---
name: handoff_merging
description: Use this skill inside the orchestrator agent workspace when the task is to merge a role result back into the request record, apply structured proposals, decide the next hop, and preserve the request as the source of truth.
---

# Handoff Merging Skill

## When To Use

Use this skill when orchestrator is processing a role handoff or completion result, especially for:

- merging planner, designer, architect, developer, or qa results into `Current request`
- applying role outputs to sprint state and respecting planner-owned backlog persistence
- deciding whether to continue to another role, block, or complete
- keeping relay summaries compact while preserving the durable request record
- reconciling conflicting relay text, snapshots, and stored request state

Do not use this skill for the role-specific work itself.

## Read First

- `Current request`
- `Current request.result`
- `Current request.events`
- `sources/<request_id>.request.md` when present
- the latest role output being merged

## Workflow

1. Treat the request record as source of truth.
   Relay summaries are convenience context, not durable state.
2. Merge structured outputs first.
   Apply `proposals.*`, `proposals.workflow_transition`, status changes, and blocked metadata before polishing the human summary.
3. Respect ownership boundaries.
   Backlog additions, updates, reprioritization, and completed-backlog moves are planner-owned persistence steps. Orchestrator should read those results, not rewrite them.
4. Keep the next hop explicit.
   Decide whether the task should continue to another role, go to version_controller, block, or finish.
   If `Current request.params.workflow` exists, update phase/step state and pass counters before selecting the next hop.
5. Preserve durable rationale.
   Store enough summary context that the next step can continue without rereading the whole conversation.
6. Close the loop on side effects.
   If the merged result implies sprint or closeout updates, make sure those writes happen. If it implies backlog work, make sure planner-owned persistence already happened and that planner returned a backlog receipt, or queue planner review first.
7. Carry routing intent into the handoff.
   Record why the chosen role was selected, which skills it should check first, and what behavior is expected from that role.

## Guardrails

- Do not let relay text override the stored request record.
- Do not persist planner backlog proposals on behalf of planner. Verify `proposals.backlog_writes` and reload backlog state instead.
- Do not turn sourcer candidates into backlog records without planner review.
- Do not mark a task complete before required side effects are done.
- Do not send work directly role-to-role without coming back through orchestrator.
- Do not leave routing reasons or skill expectations implicit when delegating.
