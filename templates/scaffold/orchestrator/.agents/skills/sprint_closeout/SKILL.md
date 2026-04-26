---
name: sprint_closeout
description: Use this skill inside the orchestrator agent workspace when the task is to finalize a sprint, verify closeout conditions, coordinate version_controller closeout work, and publish the sprint report and history updates.
---

# Sprint Closeout Skill

## When To Use

Use this skill when orchestrator is ending or wrapping up a sprint, especially for:

- verifying that active todo execution has stopped
- checking whether leftover sprint-owned changes need version_controller closeout handling
- assembling sprint summary and archive artifacts
- clearing the active sprint pointer and persisting closeout state

Do not use this skill for normal mid-sprint execution routing.

## Read First

- `shared_workspace/current_sprint.md`
- `shared_workspace/sprint_history/`
- `.teams_runtime/sprints/<sprint_id>.json`
- sprint event log
- closeout-related version_controller payloads or results

## Workflow

1. Confirm the sprint is ready to close.
   Make sure admission has stopped and in-flight todo work is no longer running.
2. Verify closeout side effects.
   If leftover sprint-owned changes exist, delegate the commit check to version_controller before finalizing.
3. Persist the final sprint state.
   Write the report, archive pointers, and clear active-sprint metadata in the correct order.
4. Leave an auditable summary.
   Record what completed, what carried over, and whether any restart or closeout issues remain.

## Guardrails

- Do not create direct closeout commits in orchestrator; delegate commit work to version_controller.
- Do not finalize a sprint while active execution is still incomplete.
- Do not skip report and archive updates after state finalization.
