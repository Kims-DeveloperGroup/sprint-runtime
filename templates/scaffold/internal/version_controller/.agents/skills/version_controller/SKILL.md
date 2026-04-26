---
name: version_controller
description: Use this skill inside the internal version_controller agent workspace when the task is to create, verify, split, squash, amend, rewrite, or explain git commits for a completed sprint todo or sprint closeout. Use it only for commit-ready change management, not for general implementation work.
---

# Version Controller Skill

## When To Use

Use this skill when the internal version_controller agent is handling commit execution or commit-history decisions, especially for:

- task-completion commits for one backlog item or sprint todo
- sprint closeout commits for leftover sprint-owned changes
- commit verification after helper execution
- commit-message selection and validation
- rewriting or merging related task commits when explicitly requested
- explaining which changes were committed and which remain uncommitted

Do not use this skill for general coding or debugging work that belongs to planner, developer, or qa.

## Read First

Before making any commit decision, inspect:

- `./COMMIT_POLICY.md`
- `Current request`
- `sources/*.version_control.json`
- `git status --short`
- `git diff --cached --stat`

If helper output already exists, treat that helper result as the source of truth for commit status.

## Workflow

1. Confirm commit scope from the version-control payload.
   Use the active `todo_id`, `backlog_id`, `sprint_id`, baseline, and changed paths to keep the commit unit narrow.
2. Reread teams commit policy.
   `./COMMIT_POLICY.md` wins over shorter prompt summaries.
3. Keep one commit unit per backlog task.
   Do not mix different todo or backlog scopes in one commit unless the user explicitly asked for a squash.
4. Prefer helper-driven commit execution.
   When a helper command is provided, run it and mirror its result instead of improvising an alternative flow.
5. Use precise commit messages.
   Include the sprint prefix and the main file or function when a commit is created.
6. Verify the outcome.
   After commit execution, confirm `commit_status`, `commit_sha`, `commit_message`, `commit_paths`, and whether unrelated changes remain.

## Guardrails

- Never mark a task commit as successful when helper output says `failed` or `no_repo`.
- Never hide uncommitted task-owned changes behind a `completed` result.
- Do not use repo-wide `./workspace/COMMIT_POLICY.md` for teams-runtime task commits.
- Do not rewrite history unless the user explicitly requested it.
- If a history rewrite is requested, create a lightweight backup branch first.

## Useful Commands

- `git status --short`
- `git diff --stat`
- `git diff --cached --stat`
- `git log --oneline --decorate -n 10`
- `git add -- <path>...`
- `git commit -m "<message>"`
- `git commit --amend`
- `git cherry-pick <sha>`
- `git cherry-pick --no-commit <sha>`
- `git branch <backup-name>`
- `git switch <branch>`

## Output Expectations

When finishing a version-control step, always make these explicit:

- `commit_status`
- `commit_sha`
- `commit_message`
- `commit_paths`
- whether any unrelated changes remain in the worktree
