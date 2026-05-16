---
name: github_issues
description: Use this read-only skill inside a public role workspace when the task needs GitHub issue lookup, issue search, or issue detail review for the linked project repository.
---

# GitHub Issues Skill

## When To Use

Use this skill when a public team role needs read-only GitHub issue context, especially for:

- listing open issues for the linked project repository
- searching open or closed issues by keyword, label, or status
- reading an issue body and comments before planning, implementation, design, architecture, or QA work
- grounding backlog, sprint, or validation decisions in existing GitHub issue context

Do not use this skill to create, update, comment on, close, or reopen issues.

## Repo Resolution

1. Choose the project repository directory.
   Use `./workspace` when it exists. Otherwise use the current directory.
2. Confirm the repository before reading issues.
   From the chosen directory, run:

```bash
gh repo view --json nameWithOwner,url
```

3. Report missing setup clearly.
   If `gh` is not installed, the directory is not a GitHub repository, or GitHub auth fails, say that plainly and include the failed setup step. Do not expose tokens or credential values.

## Listing And Search

Prefer the bundled read-only helper when available:

```bash
python .agents/skills/github_issues/scripts/list_issues.py
```

Search all issues:

```bash
python .agents/skills/github_issues/scripts/list_issues.py --search "<query>"
```

List recent open issues:

```bash
gh issue list --state open --limit 30 --json number,title,state,labels,assignees,updatedAt,url
```

Search all issues when the request has a query, keyword, label, or historical context:

```bash
gh issue list --state all --search "<query>" --limit 30 --json number,title,state,labels,assignees,updatedAt,url
```

Prefer concise summaries that include issue number, title, state, labels, assignees, updated time, and URL when available.

## Reading

Prefer the bundled read-only helper when available:

```bash
python .agents/skills/github_issues/scripts/view_issue.py <number-or-url>
```

Read issue details and comments:

```bash
gh issue view <number-or-url> --comments --json number,title,state,body,labels,assignees,author,createdAt,updatedAt,url,comments
```

When summarizing an issue, distinguish the issue author's requested behavior from later comment discussion, and call out unresolved questions or acceptance criteria when they appear.

## Guardrails

- Read-only only.
- Do not run `gh issue create`, `gh issue edit`, `gh issue comment`, `gh issue close`, or `gh issue reopen`.
- Do not mutate labels, assignees, milestones, projects, or issue state.
- Do not expose GitHub tokens, auth headers, or credential helper output.
- If GitHub CLI auth is missing or insufficient, report the auth failure clearly and stop instead of trying alternate credential paths.
- Keep issue lookups scoped to the linked project repository unless the user explicitly provides another repository or issue URL.
