from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from teams_runtime.core.persistence import (
    RUNTIME_TIMEZONE,
    new_backlog_id,
    new_todo_id,
    normalize_runtime_datetime,
    runtime_now,
)


def utc_now() -> datetime:
    # Kept for compatibility with older call sites; runtime timestamps are stored in KST.
    return runtime_now()


def compute_next_slot_at(
    now: datetime,
    *,
    interval_minutes: int,
    timezone_name: str,
) -> datetime:
    tz = ZoneInfo(timezone_name)
    local_now = normalize_runtime_datetime(now).astimezone(tz)
    minutes_since_midnight = (local_now.hour * 60) + local_now.minute
    next_minutes = ((minutes_since_midnight // interval_minutes) + 1) * interval_minutes
    next_day = local_now.date()
    if next_minutes >= 24 * 60:
        next_minutes -= 24 * 60
        next_day = local_now.date() + timedelta(days=1)
    next_local = datetime(
        next_day.year,
        next_day.month,
        next_day.day,
        next_minutes // 60,
        next_minutes % 60,
        tzinfo=tz,
    )
    return next_local.astimezone(tz)


def build_active_sprint_id(now: datetime | None = None) -> str:
    current = normalize_runtime_datetime(now or runtime_now()).astimezone(RUNTIME_TIMEZONE)
    return f"{current.strftime('%y%m%d')}-Sprint-{current.strftime('%H:%M')}"


def _truncate_sprint_text(value: Any, *, limit: int = 120) -> str:
    normalized = str(value or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 3, 0)].rstrip() + "..."


def _format_recent_activity_line(activity: dict[str, Any]) -> str:
    timestamp = str(activity.get("timestamp") or "").strip() or "N/A"
    role = str(activity.get("role") or "").strip() or "unknown"
    event_type = str(activity.get("event_type") or "").strip() or "activity"
    request_id = str(activity.get("request_id") or "").strip() or "N/A"
    todo_id = str(activity.get("todo_id") or "").strip() or "N/A"
    status = str(activity.get("status") or "").strip() or "N/A"
    summary = _truncate_sprint_text(activity.get("summary") or "", limit=160) or "없음"
    details = _truncate_sprint_text(activity.get("details") or "", limit=120)
    line = (
        f"- {timestamp} | role={role} | event={event_type} | status={status} "
        f"| request_id={request_id} | todo_id={todo_id} | {summary}"
    )
    if details:
        return f"{line} | details={details}"
    return line


def slugify_sprint_value(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9가-힣]+", "-", str(value or "").strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized or "sprint"


def normalize_sprint_label(value: str) -> str:
    normalized = re.sub(r"\s+", " ", str(value or "").strip())
    normalized = re.sub(r"[<>:\"/\\\\|?*\x00-\x1f]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or "sprint"


def build_daily_sprint_display_name(milestone_title: str, now: datetime | None = None) -> str:
    current = normalize_runtime_datetime(now or runtime_now()).astimezone(RUNTIME_TIMEZONE)
    return f"{current.strftime('%Y-%m-%d')}-{normalize_sprint_label(milestone_title)}"


def build_sprint_artifact_folder_name(sprint_id: str) -> str:
    normalized = normalize_sprint_label(sprint_id).replace(" ", "-").strip("-")
    normalized = re.sub(r"-{2,}", "-", normalized)
    return normalized or "sprint"


def build_sprint_cutoff_at(cutoff_time: str, *, now: datetime | None = None) -> datetime:
    current = normalize_runtime_datetime(now or runtime_now()).astimezone(RUNTIME_TIMEZONE)
    hour_text, minute_text = str(cutoff_time or "22:00").split(":", 1)
    cutoff_at = current.replace(
        hour=int(hour_text),
        minute=int(minute_text),
        second=0,
        microsecond=0,
    )
    if cutoff_at <= current:
        cutoff_at += timedelta(days=1)
    return cutoff_at


def build_backlog_item(
    *,
    title: str,
    summary: str,
    kind: str,
    source: str,
    scope: str,
    acceptance_criteria: list[str] | None = None,
    origin: dict[str, Any] | None = None,
    backlog_id: str | None = None,
    milestone_title: str = "",
    priority_rank: int | None = None,
    planned_in_sprint_id: str = "",
    added_during_active_sprint: bool = False,
) -> dict[str, Any]:
    created_at = utc_now().isoformat()
    return {
        "backlog_id": backlog_id or new_backlog_id(),
        "title": str(title or "").strip() or "Untitled backlog item",
        "summary": str(summary or "").strip(),
        "kind": str(kind or "enhancement").strip().lower() or "enhancement",
        "source": str(source or "discovery").strip().lower() or "discovery",
        "scope": str(scope or "").strip(),
        "acceptance_criteria": [
            str(item).strip() for item in (acceptance_criteria or []) if str(item).strip()
        ],
        "milestone_title": str(milestone_title or "").strip(),
        "priority_rank": int(priority_rank) if priority_rank is not None else 0,
        "planned_in_sprint_id": str(planned_in_sprint_id or "").strip(),
        "added_during_active_sprint": bool(added_during_active_sprint),
        "status": "pending",
        "origin": dict(origin or {}),
        "created_at": created_at,
        "updated_at": created_at,
        "selected_in_sprint_id": "",
        "completed_in_sprint_id": "",
        "carry_over_of": "",
        "blocked_reason": "",
        "blocked_by_role": "",
        "required_inputs": [],
        "recommended_next_step": "",
    }


def build_todo_item(backlog_item: dict[str, Any], *, owner_role: str = "planner") -> dict[str, Any]:
    created_at = utc_now().isoformat()
    return {
        "todo_id": new_todo_id(),
        "backlog_id": str(backlog_item.get("backlog_id") or "").strip(),
        "title": str(backlog_item.get("title") or "").strip(),
        "milestone_title": str(backlog_item.get("milestone_title") or "").strip(),
        "priority_rank": int(backlog_item.get("priority_rank") or 0),
        "owner_role": owner_role,
        "status": "queued",
        "acceptance_criteria": [
            str(item).strip()
            for item in (backlog_item.get("acceptance_criteria") or [])
            if str(item).strip()
        ],
        "request_id": "",
        "artifacts": [],
        "started_at": "",
        "ended_at": "",
        "summary": "",
        "carry_over_backlog_id": "",
    }


def render_backlog_markdown(
    items: Iterable[dict[str, Any]],
    *,
    title: str = "Backlog",
    empty_message: str = "backlog item 없음",
) -> str:
    lines = [f"# {title}", "", "## Items", ""]
    sorted_items = sorted(
        list(items),
        key=lambda item: (
            str(item.get("status") or ""),
            str(item.get("updated_at") or ""),
        ),
        reverse=True,
    )
    if not sorted_items:
        lines.append(f"- {empty_message}")
        return "\n".join(lines).rstrip() + "\n"
    for item in sorted_items:
        lines.extend(
            [
                f"### {item.get('title') or 'Untitled'}",
                f"- backlog_id: {item.get('backlog_id') or ''}",
                f"- status: {item.get('status') or ''}",
                f"- kind: {item.get('kind') or ''}",
                f"- source: {item.get('source') or ''}",
                f"- scope: {item.get('scope') or ''}",
                f"- summary: {item.get('summary') or ''}",
                *(
                    [f"- milestone_title: {item.get('milestone_title') or ''}"]
                    if str(item.get("milestone_title") or "").strip()
                    else []
                ),
                *(
                    [f"- priority_rank: {item.get('priority_rank') or 0}"]
                    if int(item.get("priority_rank") or 0) > 0
                    else []
                ),
                f"- created_at: {item.get('created_at') or 'N/A'}",
                f"- selected_in_sprint_id: {item.get('selected_in_sprint_id') or 'N/A'}",
                f"- completed_in_sprint_id: {item.get('completed_in_sprint_id') or 'N/A'}",
                *(
                    [f"- blocked_reason: {item.get('blocked_reason') or ''}"]
                    if str(item.get("blocked_reason") or "").strip()
                    else []
                ),
                *(
                    [f"- blocked_by_role: {item.get('blocked_by_role') or ''}"]
                    if str(item.get("blocked_by_role") or "").strip()
                    else []
                ),
                *(
                    [f"- recommended_next_step: {item.get('recommended_next_step') or ''}"]
                    if str(item.get("recommended_next_step") or "").strip()
                    else []
                ),
                *(
                    [f"- required_inputs: {', '.join(str(value).strip() for value in (item.get('required_inputs') or []) if str(value).strip())}"]
                    if list(item.get("required_inputs") or [])
                    else []
                ),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_current_sprint_markdown(sprint_state: dict[str, Any]) -> str:
    reference_artifacts = [
        str(item).strip()
        for item in (sprint_state.get("reference_artifacts") or [])
        if str(item).strip()
    ]
    kickoff_requirements = [
        str(item).strip()
        for item in (sprint_state.get("kickoff_requirements") or [])
        if str(item).strip()
    ]
    kickoff_reference_artifacts = [
        str(item).strip()
        for item in (sprint_state.get("kickoff_reference_artifacts") or [])
        if str(item).strip()
    ]
    lines = [
        "# Current Sprint",
        "",
        f"- sprint_id: {sprint_state.get('sprint_id') or ''}",
        f"- sprint_name: {sprint_state.get('sprint_name') or sprint_state.get('sprint_display_name') or 'N/A'}",
        f"- phase: {sprint_state.get('phase') or 'N/A'}",
        f"- requested_milestone_title: {sprint_state.get('requested_milestone_title') or 'N/A'}",
        f"- milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
        f"- sprint_folder: {sprint_state.get('sprint_folder') or 'N/A'}",
        f"- status: {sprint_state.get('status') or ''}",
        f"- trigger: {sprint_state.get('trigger') or ''}",
        f"- started_at: {sprint_state.get('started_at') or ''}",
        f"- ended_at: {sprint_state.get('ended_at') or 'N/A'}",
        f"- commit_sha: {sprint_state.get('commit_sha') or 'N/A'}",
        "",
        "## Kickoff",
        "",
        f"- kickoff_source_request_id: {sprint_state.get('kickoff_source_request_id') or 'N/A'}",
        "",
        "### Kickoff Brief",
        "",
        str(sprint_state.get("kickoff_brief") or "kickoff brief 없음").strip(),
        "",
        "### Kickoff Requirements",
        "",
    ]
    if kickoff_requirements:
        lines.extend(f"- {item}" for item in kickoff_requirements)
    else:
        lines.append("- kickoff requirement 없음")
    lines.extend(["", "### Kickoff Reference Artifacts", ""])
    if kickoff_reference_artifacts:
        lines.extend(f"- {item}" for item in kickoff_reference_artifacts)
    else:
        lines.append("- kickoff reference artifact 없음")
    lines.extend([
        "",
        "## Selected Backlog",
        "",
    ])
    selected_items = sprint_state.get("selected_items") or []
    if selected_items:
        for item in selected_items:
            lines.append(f"- {item.get('backlog_id') or ''} | {item.get('title') or ''}")
    else:
        lines.append("- selected backlog 없음")
    lines.extend(["", "## Todo List", ""])
    todos = sprint_state.get("todos") or []
    if todos:
        for todo in todos:
            lines.extend(
                [
                    f"### {todo.get('title') or 'Untitled'}",
                    f"- todo_id: {todo.get('todo_id') or ''}",
                    f"- backlog_id: {todo.get('backlog_id') or ''}",
                    f"- milestone_title: {todo.get('milestone_title') or 'N/A'}",
                    f"- priority_rank: {todo.get('priority_rank') or 0}",
                    f"- owner_role: {todo.get('owner_role') or ''}",
                    f"- status: {todo.get('status') or ''}",
                    f"- request_id: {todo.get('request_id') or 'N/A'}",
                    f"- summary: {todo.get('summary') or ''}",
                    f"- artifacts: {', '.join(str(item) for item in (todo.get('artifacts') or [])) or 'N/A'}",
                    "",
                ]
            )
    else:
        lines.append("- todo 없음")
    lines.extend(["", "## Reference Artifacts", ""])
    if reference_artifacts:
        lines.extend(f"- {item}" for item in reference_artifacts)
    else:
        lines.append("- reference artifact 없음")
    lines.extend(["", "## Recent Activity", ""])
    recent_activity = [dict(item) for item in (sprint_state.get("recent_activity") or []) if isinstance(item, dict)]
    if recent_activity:
        for activity in reversed(recent_activity[-12:]):
            lines.append(_format_recent_activity_line(activity))
    else:
        lines.append("- recent activity 없음")
    return "\n".join(lines).rstrip() + "\n"


def render_sprint_history_markdown(sprint_state: dict[str, Any], report_body: str) -> str:
    lines = [
        "# Sprint History",
        "",
        f"- sprint_id: {sprint_state.get('sprint_id') or ''}",
        f"- sprint_name: {sprint_state.get('sprint_name') or sprint_state.get('sprint_display_name') or 'N/A'}",
        f"- phase: {sprint_state.get('phase') or 'N/A'}",
        f"- requested_milestone_title: {sprint_state.get('requested_milestone_title') or 'N/A'}",
        f"- milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
        f"- sprint_folder: {sprint_state.get('sprint_folder') or 'N/A'}",
        f"- status: {sprint_state.get('status') or ''}",
        f"- trigger: {sprint_state.get('trigger') or ''}",
        f"- started_at: {sprint_state.get('started_at') or ''}",
        f"- ended_at: {sprint_state.get('ended_at') or ''}",
        f"- commit_sha: {sprint_state.get('commit_sha') or 'N/A'}",
        "",
        "## Todo List",
        "",
    ]
    todos = sprint_state.get("todos") or []
    if todos:
        for todo in todos:
            lines.extend(
                [
                    "### {title}".format(title=str(todo.get("title") or "")),
                    "- status: {status}".format(status=str(todo.get("status") or "")),
                    "- backlog_id: {backlog_id}".format(backlog_id=str(todo.get("backlog_id") or "")),
                    "- milestone_title: {milestone}".format(milestone=str(todo.get("milestone_title") or "N/A")),
                    "- priority_rank: {priority}".format(priority=int(todo.get("priority_rank") or 0)),
                    "- owner_role: {owner}".format(owner=str(todo.get("owner_role") or "")),
                    "- request_id: {request_id}".format(request_id=str(todo.get("request_id") or "N/A")),
                    "- summary: {summary}".format(summary=str(todo.get("summary") or "")),
                    "- artifacts: {artifacts}".format(
                        artifacts=", ".join(str(item) for item in (todo.get("artifacts") or [])) or "N/A"
                    ),
                    "- carry_over_backlog_id: {carry}".format(
                        carry=str(todo.get("carry_over_backlog_id") or "N/A")
                    ),
                    "",
                ]
            )
    else:
        lines.append("- todo 없음")
    lines.extend(["", "## Recent Activity", ""])
    recent_activity = [dict(item) for item in (sprint_state.get("recent_activity") or []) if isinstance(item, dict)]
    if recent_activity:
        for activity in reversed(recent_activity[-20:]):
            lines.append(_format_recent_activity_line(activity))
    else:
        lines.append("- recent activity 없음")
    lines.extend(["", "## Sprint Report", "", str(report_body or "").strip() or "N/A", ""])
    return "\n".join(lines).rstrip() + "\n"


def build_sprint_history_index_row(payload: dict[str, Any]) -> dict[str, Any]:
    milestone_title = str(
        payload.get("milestone_title") or payload.get("requested_milestone_title") or ""
    ).strip()
    return {
        "sprint_id": payload.get("sprint_id") or "",
        "status": payload.get("status") or "",
        "milestone_title": milestone_title,
        "started_at": payload.get("started_at") or "",
        "ended_at": payload.get("ended_at") or "",
        "commit_sha": payload.get("commit_sha") or "",
        "todo_count": len(payload.get("todos") or []) if "todos" in payload else int(payload.get("todo_count") or 0),
    }


def render_sprint_history_index_rows(rows: list[dict[str, Any]]) -> str:
    ordered_rows = sorted(rows, key=lambda item: str(item.get("started_at") or ""), reverse=True)
    lines = [
        "# Sprint History Index",
        "",
        "| sprint_id | status | milestone | started_at | ended_at | todo_count | commit_sha |",
        "| --- | --- | --- | --- | --- | ---: | --- |",
    ]
    for row in ordered_rows:
        lines.append(
            "| {sprint_id} | {status} | {milestone_title} | {started_at} | {ended_at} | {todo_count} | {commit_sha} |".format(
                sprint_id=row.get("sprint_id") or "",
                status=row.get("status") or "",
                milestone_title=row.get("milestone_title") or "N/A",
                started_at=row.get("started_at") or "",
                ended_at=row.get("ended_at") or "",
                todo_count=row.get("todo_count") or 0,
                commit_sha=row.get("commit_sha") or "N/A",
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def render_sprint_history_index(existing_entries: list[dict[str, Any]], sprint_state: dict[str, Any]) -> str:
    rows = list(existing_entries)
    rows = [row for row in rows if str(row.get("sprint_id") or "") != str(sprint_state.get("sprint_id") or "")]
    rows.append(build_sprint_history_index_row(sprint_state))
    return render_sprint_history_index_rows(rows)


def load_sprint_history_index(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows: list[dict[str, Any]] = []
    for line in lines:
        normalized = str(line).strip()
        if not normalized.startswith("|") or normalized.startswith("| ---"):
            continue
        parts = [part.strip() for part in normalized.strip("|").split("|")]
        if not parts or parts[0] == "sprint_id":
            continue
        if len(parts) == 7:
            rows.append(
                {
                    "sprint_id": parts[0],
                    "status": parts[1],
                    "milestone_title": parts[2] if parts[2] != "N/A" else "",
                    "started_at": parts[3],
                    "ended_at": parts[4],
                    "todo_count": int(parts[5]) if parts[5].isdigit() else 0,
                    "commit_sha": parts[6],
                }
            )
            continue
        if len(parts) == 6:
            rows.append(
                {
                    "sprint_id": parts[0],
                    "status": parts[1],
                    "milestone_title": "",
                    "started_at": parts[2],
                    "ended_at": parts[3],
                    "todo_count": int(parts[4]) if parts[4].isdigit() else 0,
                    "commit_sha": parts[5],
                }
            )
    return rows


def _resolve_sprint_artifact_relative_path(sprint_state: dict[str, Any], artifact: Any) -> str:
    normalized = str(artifact or "").strip()
    if not normalized:
        return ""
    artifact_path = Path(normalized)
    if artifact_path.is_absolute():
        sprint_folder = str(sprint_state.get("sprint_folder") or "").strip()
        if sprint_folder:
            try:
                return artifact_path.relative_to(Path(sprint_folder)).as_posix()
            except ValueError:
                return ""
        return ""
    posix = artifact_path.as_posix()
    if posix.startswith("./"):
        posix = posix[2:]
    sprint_folder_name = str(sprint_state.get("sprint_folder_name") or "").strip()
    if not sprint_folder_name:
        sprint_folder_name = build_sprint_artifact_folder_name(str(sprint_state.get("sprint_id") or ""))
    sprint_prefix = f"shared_workspace/sprints/{sprint_folder_name}/"
    if sprint_prefix in posix:
        return posix.split(sprint_prefix, 1)[1].lstrip("/")
    folder_prefix = f"{sprint_folder_name}/"
    if posix.startswith(folder_prefix):
        return posix[len(folder_prefix):].lstrip("/")
    if posix.startswith("workspace/teams_generated/"):
        return posix.removeprefix("workspace/teams_generated/").lstrip("/")
    if posix.startswith("workspace/"):
        return posix.removeprefix("workspace/").lstrip("/")
    if "/" not in posix:
        return posix
    return posix


def collect_sprint_todo_artifact_entries(sprint_state: dict[str, Any]) -> list[dict[str, str]]:
    core_files = {
        "index.md",
        "milestone.md",
        "plan.md",
        "spec.md",
        "todo_backlog.md",
        "iteration_log.md",
        "report.md",
    }
    entries: list[dict[str, str]] = []
    seen_paths: set[str] = set()
    for todo in sprint_state.get("todos") or []:
        for artifact in todo.get("artifacts") or []:
            relative_path = _resolve_sprint_artifact_relative_path(sprint_state, artifact)
            if not relative_path or relative_path in core_files or relative_path in seen_paths:
                continue
            seen_paths.add(relative_path)
            entries.append(
                {
                    "path": relative_path,
                    "title": str(todo.get("title") or "").strip() or "Untitled",
                    "status": str(todo.get("status") or "").strip() or "N/A",
                    "request_id": str(todo.get("request_id") or "").strip() or "N/A",
                }
            )
    return entries


def render_sprint_artifact_index_markdown(sprint_state: dict[str, Any]) -> str:
    todo_artifacts = collect_sprint_todo_artifact_entries(sprint_state)
    extra_files = [entry["path"] for entry in todo_artifacts]
    reference_artifacts = [
        str(item).strip()
        for item in (sprint_state.get("reference_artifacts") or [])
        if str(item).strip()
    ]
    lines = [
        "# Sprint Folder",
        "",
        f"- sprint_id: {sprint_state.get('sprint_id') or ''}",
        f"- sprint_name: {sprint_state.get('sprint_name') or sprint_state.get('sprint_display_name') or 'N/A'}",
        f"- requested_milestone_title: {sprint_state.get('requested_milestone_title') or 'N/A'}",
        f"- milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
        f"- phase: {sprint_state.get('phase') or 'N/A'}",
        f"- status: {sprint_state.get('status') or 'N/A'}",
        f"- started_at: {sprint_state.get('started_at') or 'N/A'}",
        f"- ended_at: {sprint_state.get('ended_at') or 'N/A'}",
        "",
        "## Files",
        "",
        "- kickoff.md",
        "- milestone.md",
        "- plan.md",
        "- spec.md",
        "- todo_backlog.md",
        "- iteration_log.md",
        "- report.md",
        "- attachments/",
    ]
    for path in extra_files:
        lines.append(f"- {path}")
    lines.append("")
    if reference_artifacts:
        lines.extend(["## Reference Artifacts", ""])
        for path in reference_artifacts:
            lines.append(f"- {path}")
        lines.append("")
    if todo_artifacts:
        lines.extend(["## Linked Todo Artifacts", ""])
        for entry in todo_artifacts:
            lines.append(
                "- [{status}] {title} | request_id={request_id} | artifact={path}".format(
                    status=entry["status"],
                    title=entry["title"],
                    request_id=entry["request_id"],
                    path=entry["path"],
                )
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
