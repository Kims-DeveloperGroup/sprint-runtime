---
name: backlog_decomposition
description: Use this skill inside the planner agent workspace when the task is to convert planning prose, specs, a sprint milestone, bundles, or phases into executable backlog items, todo candidates, priorities, dependencies, and acceptance criteria.
---

# Backlog Decomposition Skill

## When To Use

Use this skill when the planner must turn existing planning material into structured execution units, especially for:

- converting a spec or plan into `proposals.backlog_item` or `proposals.backlog_items`
- splitting bundles or phases into reviewable backlog units
- deriving dependencies, priority order, or a single sprint milestone's backlog breakdown
- rewriting vague work into explicit scope and acceptance criteria
- checking whether a request already contains enough structure to create backlog entries immediately

Do not use this skill for implementation, testing, or architecture changes.

## Read First

- `Current request`
- local planning or spec Markdown files mentioned in the request
- `shared_workspace/backlog.md`
- `shared_workspace/current_sprint.md`
- `shared_workspace/sprints/`

## Workflow

1. Identify the planning source.
   Confirm which document, artifact, or request section contains the candidate work items.
2. Extract execution units.
   Break phases, bundles, and vague prose into concrete backlog-sized units. One backlog item should represent a single independently reviewable implementation slice.
3. Structure every item.
   Each item should have a title, scope, summary, and acceptance criteria. Add `priority_rank`, `planned_in_sprint_id`, and the current sprint `milestone_title` when available.
4. Name the behavior change, not the implementation activity.
   Use titles that describe the functional delta or workflow-contract change. Avoid activity-first labels such as `정리`, `반영`, `문서`, `라우팅`, or `회귀 테스트` unless they are the actual product behavior change.
5. Split cross-cutting items first.
   If one candidate spans multiple subsystems, contracts, phases, deliverables, or separate architect/developer/qa review tracks, split it before returning backlog proposals.
6. Prepare orchestrator-ready output.
   Return storage-ready `proposals.backlog_item` or `proposals.backlog_items`.
7. Match the source, not a default count.
   Do not force the decomposition into three items; return the exact number of backlog items the source material supports. More than three is normal when the source contains multiple independent slices.
8. Ignore local count anchoring.
   Do not reuse prior planner history or shared planning logs as a template for how many backlog items to emit.

## Guardrails

- Do not stop at summarization when decomposition is already possible.
- Do not emit placeholder backlog items with empty acceptance criteria unless the source is genuinely incomplete.
- Do not mix multiple unrelated implementation tracks into one backlog item.
- Do not keep umbrella backlog items just because recent local examples happened to use three entries.
- Do not emit meta activity titles when the source already reveals the concrete functional change.
