---
name: documentation
description: Use this skill inside the planner agent workspace when the task is to read, write, update, verify, or decompose planning, specification, backlog, milestone, or sprint documentation. When the request is to create or revise a planning document, write or modify the actual Markdown file instead of stopping at prose-only output.
---

# Documentation Skill

## When To Use

Use this skill when the planner is working from or producing planning documents, especially for:

- reading an existing planning or spec Markdown file before deciding next steps
- reading sprint attachment documents passed through `Current request.artifacts` and using them as planning references
- reading immutable sprint kickoff docs such as `shared_workspace/sprints/<sprint_folder_name>/kickoff.md` before refining sprint planning artifacts
- drafting or updating milestone, plan, spec, or todo-backlog documents
- drafting a sprint closeout report from persisted sprint evidence and related docs
- turning document content into `proposals.backlog_item` or `proposals.backlog_items`
- extracting execution phases, bundles, dependencies, or acceptance criteria from docs
- checking whether a planning request is already answered by an existing local document
- preparing `proposals.sprint_plan_update` from sprint planning context

Do not use this skill for coding, debugging, or test implementation.

## Read First

Before deciding anything, inspect the smallest relevant set from:

- `Current request`
- `Current request.artifacts`
- local sprint attachment docs under `shared_workspace/sprints/<sprint_folder_name>/attachments/` when they are referenced by the request
- source-backed research prepass fields in `Current request.result.proposals.research_report` or `research_prepass`
- raw research report artifacts under `shared_workspace/sprints/<sprint_folder_name>/research/` when referenced
- `sources/<request_id>.request.md`
- `shared_workspace/planning.md`
- `shared_workspace/backlog.md` as persisted backlog context
- `shared_workspace/completed_backlog.md` as persisted backlog context
- `shared_workspace/current_sprint.md`
- `shared_workspace/sprints/`
- `shared_workspace/sprints/<sprint_folder_name>/kickoff.md` when the request is about sprint planning
- `shared_workspace/sprint_history/index.md` when the request is sprint-relevant
- the smallest relevant prior sprint history file(s) under `shared_workspace/sprint_history/` when carry-over work, repeated blockers, prior decisions, or milestone continuity matter
- `shared_workspace/sprints/<sprint_folder_name>/milestone.md`, `plan.md`, `spec.md`, and `iteration_log.md` when the request is sprint closeout reporting
- local planning or spec Markdown paths mentioned in the request body or scope

If the request points to an existing document, read that document before claiming missing context.

## Workflow

1. Verify the source document or planning target.
   Resolve whether the task is based on an existing spec, a backlog request, or a new sprint-planning update.
2. Read before blocking.
   If a local Markdown artifact exists, inspect it directly before asking for more inputs.
3. Treat attachments as planning inputs.
   When `Current request.artifacts` includes sprint attachment docs, read the locally saved files directly and carry forward the relevant requirements, constraints, and acceptance criteria into the plan/spec/backlog output.
4. Preserve kickoff source docs.
   When a sprint has `kickoff.md` or kickoff requirements in `Current request.params`, treat them as immutable source-of-truth. Add derived framing in milestone/plan/spec outputs instead of rewriting the original kickoff content.
5. Use research to develop planning docs.
   Source-backed research is not appendix material. Use its milestone hints, problem framing, spec implications, todo hints, backing reasoning, and sources to refine abstract milestones, write spec boundaries, and derive backlog/todo acceptance criteria.
6. Use prior sprint history selectively.
   For sprint-relevant planning work, inspect `shared_workspace/sprint_history/index.md` first and then open only the smallest relevant prior sprint history file(s). Use them to recover carry-over work, repeated blockers, prior decisions, and milestone continuity, but keep the current request, active sprint docs, and kickoff context authoritative.
7. Write the document when the request is document-authoring work.
   If the task is to create, revise, or maintain a planning document, update the real `.md` file in the workspace before returning your summary.
8. Draft closeout reports from evidence, not activity prose.
   When `Current request.params._teams_kind == "sprint_closeout_report"`, return `proposals.sprint_report` with concrete functional or workflow-contract changes. Prefer what changed in behavior over meta wording about prompts, docs, routing, or tests.
9. Keep runtime backlog files out of manual editing.
   Do not hand-edit `shared_workspace/backlog.md` or `shared_workspace/completed_backlog.md`. If backlog persistence is required, use the planner backlog persistence helper instead.
10. Convert prose into structure.
   When a document already contains next steps, phases, bundles, or execution ideas, turn them into concrete `proposals.backlog_item`, `proposals.backlog_items`, or `proposals.sprint_plan_update`.
11. Keep backlog units granular.
   One backlog item should represent a single independently reviewable implementation slice. Split items that span multiple subsystems, contracts, phases, deliverables, or separate review tracks before persisting them.
12. Add research traceability.
   When source-backed research informs milestone/spec/backlog text, reference the research report artifact or source title/url in the planning output, and include `origin.research_refs` for sprint-relevant backlog.
13. Ignore local count anchoring.
   Do not copy the number of backlog items from prior planner history or shared planning logs. The current document and request determine how many items are needed.
14. Keep titles behavior-first.
   Backlog titles, todo titles, and closeout change headings should describe the functional delta or enforced workflow contract, not the activity performed to implement it.
15. Keep outputs execution-ready.
   Titles, scope, summary, priority, dependencies, and acceptance criteria should be specific enough for planner persistence and downstream execution to continue immediately.
16. Keep planner ownership clear.
   Finish documentation/planning work in planner, then leave execution-ready context so orchestrator can centrally choose whether execution should move to designer, architect, developer, or qa.

## Guardrails

- Do not stop at prose-only summaries when backlog decomposition is already possible.
- Do not stop at prose-only summaries when the task explicitly requires a document to be created or updated; modify the Markdown file itself.
- Do not directly edit `shared_workspace/backlog.md` or `shared_workspace/completed_backlog.md`; persist backlog changes through the planner helper.
- Do not bundle multiple independent implementation tracks into one backlog item just to keep the list short.
- Do not treat prior local `3건` or `3 items` examples as a template for the current backlog count.
- Do not silently ignore referenced attachments; if a file exists but is unreadable in the current session, state that limitation explicitly.
- Do not overwrite the original sprint kickoff brief or kickoff requirements when they are preserved separately from derived planning docs.
- Do not preserve a vague user milestone as the final planning frame when source-backed research provides stronger problem framing.
- Do not omit source-backed research references from planning docs or backlog origins when research shaped the plan.
- Do not treat prior sprint history as the source of truth over the current request, active sprint docs, or kickoff context.
- Do not bulk-read `shared_workspace/sprint_history/` when `index.md` and a small relevant subset are enough.
- Do not invent missing documents if a local artifact or request path can be checked directly.
- Do not fill `what_changed` or item titles with meta activity labels like `정리`, `반영`, `문서`, `라우팅`, or `회귀 테스트` when the underlying behavior change is identifiable.
- Do not choose `next_role` in planner output.
- Do not mix planning guidance with direct implementation instructions that belong to developer or architect.
- Treat `Current request` as the source of truth when snapshots and relay text differ.

## Useful Outputs

- `proposals.backlog_item`
- `proposals.backlog_items`
- `proposals.sprint_plan_update`
- `proposals.sprint_report`
- concise `summary` and durable `acceptance_criteria`
