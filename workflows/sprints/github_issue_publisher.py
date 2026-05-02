from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.workflows.sprints.lifecycle import build_sprint_artifact_folder_name
from teams_runtime.workflows.state.request_store import iter_request_records


SPRINT_ISSUE_MARKER_PREFIX = "<!-- teams-runtime:sprint-issue:"
COMMENT_MARKER_PREFIX = "<!-- teams-runtime:sprint-doc:"
MAX_COMMENT_BODY_CHARS = 58000
DOCUMENT_EXTENSIONS = {".md", ".markdown", ".txt", ".text", ".rst"}
SPRINT_DOC_FILENAMES = (
    "kickoff.md",
    "milestone.md",
    "plan.md",
    "spec.md",
    "iteration_log.md",
    "todo_backlog.md",
    "report.md",
)
EXCLUDED_FILENAMES = {
    "index.md",
    "README.md",
    ".events.jsonl",
    "history.md",
    "journal.md",
    "todo.md",
}
EXCLUDED_SUFFIXES = (
    ".json",
    ".jsonl",
    ".log",
    ".lock",
    ".pid",
    ".sqlite",
    ".db",
)
EXCLUDED_PARTS = {
    ".teams_runtime",
    "logs",
    "role_sessions",
    "sessions",
    "service",
}


@dataclass(slots=True, frozen=True)
class SprintIssueDocument:
    path: Path
    label: str


@dataclass(slots=True)
class GhResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


GhRunner = Callable[[list[str], str | None], GhResult]


class SprintIssuePublishError(RuntimeError):
    def __init__(self, stage: str, message: str, *, next_action: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.next_action = next_action


def _nearest_dotenv_path(start: Path) -> Path | None:
    current = start.expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate_dir in (current, *current.parents):
        candidate = candidate_dir / ".env"
        if candidate.exists():
            return candidate
    return None


def load_github_token_dotenv(paths: RuntimePaths) -> Path | None:
    dotenv_path = _nearest_dotenv_path(paths.workspace_root)
    if dotenv_path is None:
        return None
    had_gh_token = bool(os.environ.get("GH_TOKEN"))
    had_github_token = bool(os.environ.get("GITHUB_TOKEN"))
    load_dotenv(dotenv_path=dotenv_path, override=False)
    if not os.environ.get("GH_TOKEN") and not had_gh_token and os.environ.get("GITHUB_TOKEN"):
        os.environ["GH_TOKEN"] = str(os.environ["GITHUB_TOKEN"])
    if not os.environ.get("GITHUB_TOKEN") and not had_github_token and os.environ.get("GH_TOKEN"):
        os.environ["GITHUB_TOKEN"] = str(os.environ["GH_TOKEN"])
    return dotenv_path


def _stable_marker(sprint_id: str) -> str:
    return f"{SPRINT_ISSUE_MARKER_PREFIX}{sprint_id} -->"


def _comment_marker(sprint_id: str, label: str, part: int = 1) -> str:
    safe_label = re.sub(r"[^A-Za-z0-9_.:/-]+", "-", str(label or "document").strip()).strip("-")
    return f"{COMMENT_MARKER_PREFIX}{sprint_id}:{safe_label}:part-{part} -->"


def _dedupe_documents(documents: list[SprintIssueDocument]) -> list[SprintIssueDocument]:
    seen: set[Path] = set()
    deduped: list[SprintIssueDocument] = []
    for doc in documents:
        try:
            resolved = doc.path.expanduser().resolve()
        except Exception:
            resolved = doc.path
        if resolved in seen or not resolved.exists() or not resolved.is_file():
            continue
        seen.add(resolved)
        deduped.append(SprintIssueDocument(resolved, doc.label))
    return deduped


def _is_document_path(path: Path) -> bool:
    name = path.name
    if name in EXCLUDED_FILENAMES:
        return False
    lowered = name.lower()
    if any(lowered.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
        return False
    if path.suffix.lower() not in DOCUMENT_EXTENSIONS:
        return False
    if any(part in EXCLUDED_PARTS for part in path.parts):
        return False
    return True


def _resolve_workspace_path(paths: RuntimePaths, value: Any) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    normalized = raw.removeprefix("./")
    path = Path(normalized).expanduser()
    if path.is_absolute():
        return path
    if normalized.startswith("workspace/"):
        normalized = normalized.removeprefix("workspace/")
    return paths.workspace_root / normalized


def _extract_artifact_values(value: Any) -> list[str]:
    values: list[str] = []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        for key in ("path", "artifact", "file", "href", "url"):
            if str(value.get(key) or "").strip():
                values.append(str(value[key]))
        for key in ("artifacts", "reference_artifacts"):
            values.extend(_extract_artifact_values(value.get(key)))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            values.extend(_extract_artifact_values(item))
    return values


def collect_sprint_issue_documents(paths: RuntimePaths, sprint_state: dict[str, Any]) -> list[SprintIssueDocument]:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    folder_name = str(sprint_state.get("sprint_folder_name") or "").strip() or build_sprint_artifact_folder_name(sprint_id)
    sprint_dir = paths.sprint_artifact_dir(folder_name)
    documents: list[SprintIssueDocument] = []
    for filename in SPRINT_DOC_FILENAMES:
        documents.append(SprintIssueDocument(sprint_dir / filename, f"sprint/{filename}"))
    for path, label in (
        (paths.shared_backlog_file, "shared_workspace/backlog.md"),
        (paths.shared_completed_backlog_file, "shared_workspace/completed_backlog.md"),
        (paths.current_sprint_file, "shared_workspace/current_sprint.md"),
    ):
        documents.append(SprintIssueDocument(path, label))
    research_dirs = [sprint_dir / "research"]
    if sprint_id and paths.sprint_research_dir(sprint_id) not in research_dirs:
        research_dirs.append(paths.sprint_research_dir(sprint_id))
    for research_dir in research_dirs:
        if research_dir.exists():
            for path in sorted(research_dir.rglob("*.md")):
                documents.append(SprintIssueDocument(path, f"research/{path.name}"))
    artifact_values: list[str] = []
    artifact_values.extend(_extract_artifact_values(sprint_state.get("reference_artifacts")))
    for todo in sprint_state.get("todos") or []:
        if not isinstance(todo, dict):
            continue
        artifact_values.extend(_extract_artifact_values(todo.get("artifacts")))
        artifact_values.extend(_extract_artifact_values(todo.get("reference_artifacts")))
        request_id = str(todo.get("request_id") or "").strip()
        request = {}
        if request_id:
            request = next(
                (record for record in iter_request_records(paths) if str(record.get("request_id") or "") == request_id),
                {},
            )
        for source in (request, dict(request.get("result") or {}) if isinstance(request, dict) else {}):
            artifact_values.extend(_extract_artifact_values(source.get("artifacts")))
            artifact_values.extend(_extract_artifact_values(source.get("reference_artifacts")))
    for raw in artifact_values:
        path = _resolve_workspace_path(paths, raw)
        if path is not None and _is_document_path(path):
            documents.append(SprintIssueDocument(path, f"artifact/{path.name}"))
    for root in (sprint_dir, sprint_dir / "attachments"):
        if root.exists():
            for path in sorted(root.rglob("*")):
                if path.is_file() and _is_document_path(path):
                    documents.append(SprintIssueDocument(path, f"sprint/{path.relative_to(sprint_dir).as_posix()}"))
    return _dedupe_documents(documents)


def default_gh_runner(args: list[str], stdin: str | None = None) -> GhResult:
    completed = subprocess.run(
        ["gh", *args],
        input=stdin,
        text=True,
        capture_output=True,
        check=False,
    )
    return GhResult(completed.returncode, completed.stdout, completed.stderr)


def _run_gh(runner: GhRunner, args: list[str], *, stdin: str | None = None, stage: str = "gh") -> GhResult:
    result = runner(args, stdin)
    if result.returncode == 0:
        return result
    message = (result.stderr or result.stdout or f"gh {' '.join(args)} failed").strip()
    next_action = "Run gh auth login or set GH_TOKEN/GITHUB_TOKEN." if stage == "auth" else "Check gh output and retry sprint issue publishing."
    if stage == "auth":
        message = "GitHub token missing. Run gh auth login or set GH_TOKEN/GITHUB_TOKEN."
    raise SprintIssuePublishError(stage, message, next_action=next_action)


def preflight_gh(runner: GhRunner = default_gh_runner) -> str:
    if shutil.which("gh") is None and runner is default_gh_runner:
        raise SprintIssuePublishError("preflight", "GitHub CLI `gh` is not installed.", next_action="Install GitHub CLI and rerun publishing.")
    _run_gh(runner, ["--version"], stage="preflight")
    _run_gh(runner, ["auth", "status"], stage="auth")
    repo_result = _run_gh(runner, ["repo", "view", "--json", "nameWithOwner"], stage="repo")
    try:
        repo = str(json.loads(repo_result.stdout or "{}").get("nameWithOwner") or "").strip()
    except json.JSONDecodeError:
        repo = ""
    if not repo:
        raise SprintIssuePublishError("repo", "Unable to resolve GitHub repository with gh repo view.", next_action="Run inside a GitHub repository or pass a valid gh repo context.")
    return repo


def _issue_title(sprint_state: dict[str, Any]) -> str:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip() or "unknown-sprint"
    milestone = str(sprint_state.get("milestone_title") or sprint_state.get("requested_milestone_title") or "Untitled milestone").strip()
    return f"[Sprint] {sprint_id} - {milestone}"


def _find_existing_issue(runner: GhRunner, sprint_id: str) -> int | None:
    marker = _stable_marker(sprint_id)
    result = _run_gh(
        runner,
        ["issue", "list", "--state", "all", "--search", marker, "--json", "number,body", "--limit", "20"],
        stage="find_issue",
    )
    try:
        issues = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        issues = []
    for issue in issues if isinstance(issues, list) else []:
        if marker in str(issue.get("body") or ""):
            return int(issue.get("number") or 0) or None
    return None


def _related_issue_lines(runner: GhRunner, milestone: str) -> list[str]:
    keywords = " ".join(str(milestone or "").split()[:6]).strip()
    if not keywords:
        return []
    result = _run_gh(
        runner,
        ["issue", "list", "--state", "all", "--search", keywords, "--json", "number,title,state", "--limit", "10"],
        stage="related_issues",
    )
    try:
        issues = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return []
    lines: list[str] = []
    for issue in issues if isinstance(issues, list) else []:
        number = int(issue.get("number") or 0)
        title = str(issue.get("title") or "").strip()
        state = str(issue.get("state") or "").strip()
        if number and title:
            lines.append(f"- #{number} [{state}] {title}")
    return lines


def _build_issue_body(sprint_state: dict[str, Any], related_lines: list[str]) -> str:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    lines = [
        _stable_marker(sprint_id),
        "",
        f"- sprint_id: {sprint_id or 'N/A'}",
        f"- milestone_title: {sprint_state.get('milestone_title') or sprint_state.get('requested_milestone_title') or 'N/A'}",
        f"- status: {sprint_state.get('status') or 'N/A'}",
        f"- closeout_status: {sprint_state.get('closeout_status') or 'N/A'}",
    ]
    if related_lines:
        lines.extend(["", "## Related Issues", "", *related_lines])
    return "\n".join(lines).rstrip() + "\n"


def _ensure_issue(runner: GhRunner, sprint_state: dict[str, Any]) -> int:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    existing = _find_existing_issue(runner, sprint_id)
    title = _issue_title(sprint_state)
    related = _related_issue_lines(runner, str(sprint_state.get("milestone_title") or sprint_state.get("requested_milestone_title") or ""))
    body = _build_issue_body(sprint_state, related)
    if existing:
        _run_gh(runner, ["issue", "edit", str(existing), "--title", title, "--body-file", "-"], stdin=body, stage="update_issue")
        return existing
    result = _run_gh(runner, ["issue", "create", "--title", title, "--body-file", "-"], stdin=body, stage="create_issue")
    match = re.search(r"/issues/(\d+)", result.stdout or "")
    return int(match.group(1)) if match else int((result.stdout or "0").strip() or 0)


def _split_document_comment(label: str, content: str) -> list[str]:
    header = f"## {label}\n\n```text\n"
    footer = "\n```\n"
    chunk_size = max(MAX_COMMENT_BODY_CHARS - len(header) - len(footer) - 200, 1000)
    chunks = [content[index : index + chunk_size] for index in range(0, len(content), chunk_size)] or [""]
    return [f"{header}{chunk}{footer}" for chunk in chunks]


def _existing_comments_by_marker(runner: GhRunner, repo: str, issue_number: int) -> dict[str, int]:
    result = _run_gh(runner, ["api", f"repos/{repo}/issues/{issue_number}/comments"], stage="comments")
    try:
        comments = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        comments = []
    found: dict[str, int] = {}
    for comment in comments if isinstance(comments, list) else []:
        body = str(comment.get("body") or "")
        match = re.search(r"<!-- teams-runtime:sprint-doc:[^>]+ -->", body)
        comment_id = int(comment.get("id") or 0)
        if match and comment_id:
            found[match.group(0)] = comment_id
    return found


def _upsert_comment(runner: GhRunner, repo: str, issue_number: int, marker: str, body: str, existing: dict[str, int]) -> None:
    full_body = f"{marker}\n\n{body}".rstrip() + "\n"
    comment_id = existing.get(marker)
    if comment_id:
        _run_gh(
            runner,
            ["api", f"repos/{repo}/issues/comments/{comment_id}", "--method", "PATCH", "--field", f"body={full_body}"],
            stage="update_comment",
        )
        return
    _run_gh(runner, ["issue", "comment", str(issue_number), "--body-file", "-"], stdin=full_body, stage="create_comment")


def publish_sprint_issue(paths: RuntimePaths, sprint_state: dict[str, Any], *, runner: GhRunner = default_gh_runner) -> int:
    load_github_token_dotenv(paths)
    repo = preflight_gh(runner)
    issue_number = _ensure_issue(runner, sprint_state)
    if not issue_number:
        raise SprintIssuePublishError("create_issue", "Unable to determine GitHub issue number.", next_action="Check gh issue create output and retry.")
    existing_comments = _existing_comments_by_marker(runner, repo, issue_number)
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    documents = collect_sprint_issue_documents(paths, sprint_state)
    for document in documents:
        try:
            content = document.path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            content = document.path.read_text(encoding="utf-8", errors="replace")
        for index, body in enumerate(_split_document_comment(document.label, content), start=1):
            _upsert_comment(runner, repo, issue_number, _comment_marker(sprint_id, document.label, index), body, existing_comments)
    return issue_number


async def publish_sprint_issue_async(paths: RuntimePaths, sprint_state: dict[str, Any], *, runner: GhRunner = default_gh_runner) -> int:
    return await asyncio.to_thread(publish_sprint_issue, paths, dict(sprint_state), runner=runner)
