# Teams Commit Policy

This file is the commit policy for generated teams agents under `teams_generated/`.

It is separate from any repo-wide commit guide. When a teams agent creates or plans commits, this file is the source of truth.

The internal version_controller agent is the primary owner of task-completion and closeout commit execution in the sprint runtime.

## Required Commit Unit

- The default commit unit is one `Backlog Task`.
- One `Backlog Task` means one selected backlog item or one sprint todo.
- Do not mix changes from different backlog items or different todos in one commit.

## Required Commit Message Format

- Every commit message must start with the active sprint prefix: `[{sprint_id}]`.
- Every commit message should include `{todo_id}` first, or `{backlog_id}` when no todo exists yet.
- Every commit message must name the main file name or function/class.
- Every commit message must describe the concrete behavior change.

Preferred format:

```text
[{sprint_id}] {todo_id|backlog_id} {main_file_or_function}: {concrete behavior change}
```

Good examples:

```text
[260326-Sprint-14:04] todo-140422-51a086 orchestration.py: record sourcer report failure diagnostics
[260326-Sprint-10:32] backlog-20260326-26904a13 fetch_candle.py: support ranged 5-minute candle queries
```

Bad examples:

```text
fix bug
[260324-Sprint-09:00] update code
[260324-Sprint-09:00] developer: work on report channel
```

## Agent Rules

- Reread this file before planning or creating commits.
- If a prompt gives a shorter commit rule, this file wins.
- Do not leave a task in `completed` state while task-owned changes remain uncommitted. version_controller must commit them first or return a blocked/failed reason.
- If a commit mixes multiple backlog/todo units, split or rewrite it before considering the task done unless the user explicitly asked for a squash.
