---
name: sprint_planning
description: Use this skill inside the planner agent workspace when the task is to shape an initial sprint plan, reprioritize backlog during ongoing review, or decide whether work should be promoted into the active sprint.
---

# Sprint Planning Skill

## When To Use

Use this skill when the planner is operating on active sprint planning state, especially for:

- `initial` sprint setup
- `ongoing_review` reprioritization
- single refined-milestone planning grounded in an immutable kickoff brief
- deciding which backlog items should be promoted into the sprint
- preparing `proposals.sprint_plan_update`
- assigning `priority_rank`, `milestone_title`, and `planned_in_sprint_id`

Do not use this skill for non-sprint documentation tasks that do not affect sprint queue shape.

## Read First

- `Current request`
- `Current request.params.sprint_phase`
- `shared_workspace/current_sprint.md`
- `shared_workspace/backlog.md`
- sprint kickoff docs in `shared_workspace/sprints/<sprint_folder_name>/kickoff.md`
- sprint folder docs in `shared_workspace/sprints/<sprint_folder_name>/`
- `shared_workspace/sprint_history/index.md` when the request is sprint-relevant
- the smallest relevant prior sprint history file(s) under `shared_workspace/sprint_history/` when carry-over work, repeated blockers, milestone continuity, or already-closed decisions matter

## Workflow

1. Confirm the sprint phase.
   Distinguish `initial` planning from `ongoing_review`.
2. Read current sprint state before changing priorities.
   Use the existing milestone, preserved kickoff brief, kickoff requirements, and queue context as the baseline.
3. Use prior sprint history as comparative context.
   For sprint-relevant planning, inspect `shared_workspace/sprint_history/index.md` first and then open only the smallest relevant prior sprint history file(s). Use them to recover carry-over work, repeated blockers, milestone continuity, and already-closed decisions, but keep the current request, active sprint docs, and kickoff context authoritative.
4. Recommend only actionable updates.
   Persist sprint-bound backlog updates directly before returning, then describe the queue change in `proposals.sprint_plan_update`, summary, and `proposals.backlog_writes`.
5. Keep sprint item names behavior-first.
   Promoted backlog and todo titles should describe the functional delta or workflow-contract change, not the activity performed to implement it.
6. Keep promotion decisions explicit.
   When a backlog item should move into the sprint, persist its `priority_rank`, `planned_in_sprint_id`, and the sprint's single `milestone_title` directly in backlog state.
7. Avoid planner-only dead ends.
   If execution should continue after planning, leave enough context for orchestrator to choose the downstream execution role centrally.
8. Filter by milestone relevance.
   Include only tasks that directly advance the sprint's single milestone. Leave unrelated maintenance, cleanup, or parallel ideas in backlog instead of promoting them into this sprint.
9. Prefer smaller reviewable slices.
   A sprint backlog item should still be a single independently reviewable implementation slice. Split multi-subsystem, multi-contract, multi-phase, or multi-deliverable candidates before promotion.
10. Size the sprint to the milestone.
   Do not default to three promoted items. Choose the exact number justified by the sprint milestone, preserved kickoff context, and current backlog state. More than three promoted items is normal when the milestone spans multiple independent slices.
11. Ignore local count anchoring.
   Do not copy prior planner history or shared planning logs that happened to show three promoted items.
12. Reopen blocked backlog explicitly.
   If a blocked item is now ready, persist it back to `pending`, clear blocker fields, and only then consider it for sprint promotion.

## Guardrails

- Do not promote work into the sprint without making the sprint's single milestone and priority rationale explicit.
- Do not promote side quests just because they are convenient; sprint inclusion must be milestone-relevant.
- Do not default to three sprint items when the work naturally collapses to one or expands beyond three.
- Do not bundle multiple independent implementation slices into one sprint item just to keep the sprint short.
- Do not move a `blocked` item directly into sprint selection; reopen it to `pending` first or leave it blocked with updated blocker context.
- Do not rewrite `kickoff.md` or erase original kickoff requirements while refining the sprint.
- Do not treat prior sprint history as the source of truth over the current request, active sprint docs, or kickoff context.
- Do not bulk-read `shared_workspace/sprint_history/` when `index.md` and a small relevant subset are enough.
- Do not leave `ongoing_review` outputs as prose-only commentary when queue updates are already clear.
- Do not preserve meta activity titles like `정리`, `반영`, `문서`, `라우팅`, or `회귀 테스트` when a concrete functional change title can be written.
- Do not choose `next_role` in planner output.
