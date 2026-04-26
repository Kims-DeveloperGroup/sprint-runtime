from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
import unicodedata

from teams_runtime.shared.persistence import (
    new_backlog_id,
    runtime_now,
)


@dataclass(frozen=True, slots=True)
class ReportSection:
    title: str
    lines: tuple[str, ...]


def _has_meaningful_text(value: str) -> bool:
    normalized = str(value or "").strip()
    return normalized not in {"", "N/A", "없음"}


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
    created_at = runtime_now().isoformat()
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


def _escape_fenced_block_content(value: str) -> str:
    return str(value or "").replace("```", "``\u200b`")


def _display_width(value: str) -> int:
    width = 0
    for char in str(value or ""):
        if char in {"\u200b", "\u200d", "\ufe0f"}:
            continue
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def _split_display_fragment(value: str, max_width: int) -> tuple[str, str]:
    text = str(value or "")
    if not text or max_width <= 0:
        return text[:1], text[1:]
    width = 0
    split_at: int | None = None
    for index, char in enumerate(text):
        char_width = _display_width(char)
        if width + char_width > max_width:
            if split_at is None:
                split_at = index if index > 0 else 1
            break
        width += char_width
        if char.isspace() or char in {"|", ",", ";", "/", ")", "]"}:
            split_at = index + 1
    else:
        return text, ""
    fragment = text[:split_at].rstrip()
    remaining = text[split_at:].lstrip()
    if fragment:
        return fragment, remaining
    return text[:1], text[1:]


def _wrap_box_line(value: str, *, max_width: int) -> list[str]:
    text = str(value or "")
    if _display_width(text) <= max_width:
        return [text]
    continuation_prefix = "  " if text.startswith("- ") else ("    " if text.startswith("  ") else "")
    lines: list[str] = []
    remaining = text
    prefix = ""
    while remaining:
        available = max(1, max_width - _display_width(prefix))
        fragment, remaining = _split_display_fragment(remaining, available)
        lines.append(f"{prefix}{fragment}".rstrip())
        prefix = continuation_prefix
    return lines or [text]


def _pad_display_width(value: str, width: int) -> str:
    padding = max(0, int(width or 0) - _display_width(value))
    return f"{value}{' ' * padding}"


def _normalize_section_lines(lines: Iterable[str] | None) -> list[str]:
    normalized: list[str] = []
    for item in lines or []:
        text = str(item or "").rstrip()
        if not text:
            normalized.append("")
            continue
        normalized.append(text)
    while normalized and not normalized[0]:
        normalized.pop(0)
    while normalized and not normalized[-1]:
        normalized.pop()
    return normalized


def render_text_box(
    title: str,
    lines: Iterable[str] | None,
    *,
    max_inner_width: int = 88,
) -> str:
    rendered_lines = _normalize_section_lines(lines)
    if not rendered_lines:
        rendered_lines = ["- 없음"]
    wrapped_lines: list[str] = []
    for line in rendered_lines:
        wrapped_lines.extend(_wrap_box_line(_escape_fenced_block_content(line), max_width=max(16, int(max_inner_width or 88))))
    inner_width = max(
        _display_width(str(title or "").strip() or "Section"),
        *(_display_width(line) for line in wrapped_lines),
        16,
    )
    border = "+" + "-" * (inner_width + 2) + "+"
    title_line = f"| {_pad_display_width(str(title or '').strip() or 'Section', inner_width)} |"
    body_lines = [f"| {_pad_display_width(line, inner_width)} |" for line in wrapped_lines]
    return "\n".join([border, title_line, border, *body_lines, border])


def render_report_sections(
    sections: Iterable[ReportSection] | None,
    *,
    max_inner_width: int = 88,
) -> str:
    rendered: list[str] = []
    for section in sections or []:
        title = str(section.title or "").strip()
        if not title:
            continue
        rendered_lines = _normalize_section_lines(section.lines)
        if not rendered_lines:
            rendered_lines = ["- 없음"]
        wrapped_lines: list[str] = []
        for line in rendered_lines:
            wrapped_lines.extend(
                _wrap_box_line(
                    _escape_fenced_block_content(line),
                    max_width=max(16, int(max_inner_width or 88)),
                )
            )
        body = "\n".join([f"[{title}]", *wrapped_lines]).strip()
        rendered.append(f"```text\n{body}\n```")
    return "\n\n".join(rendered)


def box_text_message(content: str, *, info_string: str = "", max_inner_width: int = 88) -> str:
    normalized = str(content or "").strip()
    if not normalized:
        return ""
    raw_lines: list[str] = []
    for line in _escape_fenced_block_content(normalized).splitlines() or [""]:
        raw_lines.extend(_wrap_box_line(line, max_width=max(24, int(max_inner_width or 88))))
    return "\n".join(raw_lines)


def _status_emoji(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"완료", "completed", "committed", "connected", "sent", "성공"}:
        return "✅"
    if normalized in {"진행중", "running", "executing", "started"}:
        return "⏳"
    if normalized in {"대기", "pending", "queued", "waiting"}:
        return "⏸️"
    if normalized in {"중단", "blocked", "cancelled", "canceled", "stopped"}:
        return "⛔"
    if normalized in {"실패", "failed", "error"}:
        return "⚠️"
    return "ℹ️"


def _humanize_artifact_path(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    path = Path(normalized)
    marker_names = {
        "shared_workspace",
        ".teams_runtime",
        "logs",
        "tmp",
        "operations",
        "sources",
        "sessions",
        "workspace",
    }
    parts = list(path.parts)
    for index, part in enumerate(parts):
        if part in marker_names:
            return "/".join(parts[index:])
    if len(parts) > 4:
        return "/".join(parts[-4:])
    return normalized


def _summarize_artifacts(artifacts: list[str] | None) -> str:
    normalized = [_humanize_artifact_path(str(item)) for item in (artifacts or []) if str(item).strip()]
    if not normalized:
        return ""
    preview = normalized[:3]
    if len(normalized) > 3:
        preview.append(f"외 {len(normalized) - 3}건")
    return ", ".join(preview)


def read_process_summary(pid: int | None) -> str:
    if pid is None:
        return "N/A"
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=,ppid=,stat=,etime=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return f"N/A ({exc})"
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[0] if lines else "N/A"


def read_runtime_log_tail(runtime_log_file: Path, max_lines: int = 20) -> str:
    try:
        lines = runtime_log_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    return "\n".join(lines[-max_lines:])


def _append_labeled_lines(lines: list[str], label: str, value: str, *, prefix: str = "- ") -> None:
    normalized = str(value or "").strip()
    if not _has_meaningful_text(normalized):
        return
    rows = [row.strip() for row in normalized.splitlines() if row.strip()]
    if not rows:
        return
    if len(rows) == 1:
        lines.append(f"{prefix}{label}: {rows[0]}")
        return
    lines.append(f"{prefix}{label}:")
    lines.extend(f"  {row}" for row in rows)


def build_progress_report(
    *,
    request: str,
    scope: str,
    status: str,
    list_summary: str,
    detail_summary: str,
    process_summary: str,
    log_summary: str,
    end_reason: str,
    judgment: str,
    next_action: str,
    commit_message: str = "",
    artifacts: list[str] | None = None,
    sections: list[ReportSection] | None = None,
) -> str:
    artifact_summary = _summarize_artifacts(artifacts)
    summary_text = str(judgment or detail_summary or request).strip()
    reference_lines: list[str] = []

    def append_reference(label: str, value: str) -> None:
        normalized = str(value or "").strip()
        if not _has_meaningful_text(normalized):
            return
        rows = [row.strip() for row in normalized.splitlines() if row.strip()]
        if not rows:
            return
        if len(rows) == 1:
            reference_lines.append(f"  • {label}: {rows[0]}")
            return
        reference_lines.append(f"  • {label}:")
        reference_lines.extend(f"    {row}" for row in rows)

    if not sections:
        if _has_meaningful_text(detail_summary) and str(detail_summary).strip() != summary_text:
            append_reference("상세", str(detail_summary).strip())
        append_reference("리스트", list_summary)
        append_reference("프로세스", process_summary)
        append_reference("로그", log_summary)

    lines = [
        "[작업 보고]",
        f"- 🧩 요청: {request}",
        f"- {_status_emoji(status)} 상태: {status}",
    ]
    if _has_meaningful_text(next_action):
        _append_labeled_lines(lines, "➡️ 다음", next_action)
    _append_labeled_lines(lines, "📍 범위", scope)
    if summary_text != str(request or "").strip():
        _append_labeled_lines(lines, "🧠 판단", summary_text)
    _append_labeled_lines(lines, "🧾 커밋", commit_message)
    if reference_lines:
        lines.append("- 🔎 근거:")
        lines.extend(reference_lines)
    if _has_meaningful_text(end_reason) and str(end_reason).strip() not in {summary_text, "없음"}:
        _append_labeled_lines(lines, "⚠️ 종료 사유", end_reason)
    if artifact_summary:
        _append_labeled_lines(lines, "📎 관련 아티팩트", artifact_summary)
    body_parts = [box_text_message("\n".join(lines))]
    rendered_sections = render_report_sections(sections)
    if rendered_sections:
        body_parts.append(rendered_sections)
    return "\n\n".join(part for part in body_parts if part.strip())
