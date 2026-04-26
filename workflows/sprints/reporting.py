from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from teams_runtime.shared.formatting import ReportSection, build_progress_report
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.workflows.sprints.lifecycle import (
    INITIAL_PHASE_STEP_ARTIFACT_SYNC,
    INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
    INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
    INITIAL_PHASE_STEP_TODO_FINALIZATION,
    INITIAL_PHASE_STEPS,
    build_sprint_artifact_folder_name,
    initial_phase_step,
    initial_phase_step_position,
    initial_phase_step_title,
    next_initial_phase_step,
    slugify_sprint_value,
    utc_now,
)


SPRINT_ROLE_DISPLAY_NAMES = {
    "orchestrator": "오케스트레이터",
    "research": "리서처",
    "planner": "플래너",
    "designer": "디자이너",
    "architect": "아키텍트",
    "developer": "개발자",
    "qa": "QA",
    "parser": "파서",
    "sourcer": "소서",
    "version_controller": "버전 컨트롤러",
}
WorkflowTransitionProvider = Callable[[dict[str, Any]], dict[str, Any]]


def decorate_sprint_report_title(title: str) -> str:
    normalized = str(title or "").strip()
    emoji_map = {
        "스프린트 시작": "🚀",
        "스프린트 TODO": "📝",
        "스프린트 완료": "✅",
        "스프린트 실패": "⚠️",
        "스프린트 종료": "🛑",
    }
    emoji = emoji_map.get(normalized)
    if not emoji or normalized.startswith(emoji):
        return normalized
    return f"{emoji} {normalized}"


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _first_meaningful_text(*values: Any) -> str:
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _collapse_whitespace(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _truncate_sprint_text(value: Any, *, limit: int = 120) -> str:
    normalized = str(value or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 3, 0)].rstrip() + "..."


def _truncate_report_text(value: Any, *, limit: int = 240) -> str:
    normalized = _collapse_whitespace(value)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def format_sprint_report_text(value: Any, *, full_detail: bool = False, limit: int = 240) -> str:
    normalized = _collapse_whitespace(value)
    if full_detail:
        return normalized
    return _truncate_report_text(normalized, limit=limit)


def sprint_status_label(status: str) -> str:
    normalized = str(status or "").strip().lower()
    return {
        "completed": "완료",
        "failed": "실패",
        "blocked": "중단",
        "running": "진행중",
        "planning": "계획중",
        "closeout": "마감중",
    }.get(normalized, str(status or "").strip() or "N/A")


def limit_sprint_report_lines(lines: Iterable[str], *, limit: int) -> list[str]:
    normalized = [str(item).strip() for item in lines if str(item).strip()]
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + [f"- ... 외 {len(normalized) - limit}건"]


def preview_sprint_artifact_path(
    sprint_state: dict[str, Any],
    value: str,
    *,
    workspace_root: Path,
    full_detail: bool = False,
) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        return ""
    if normalized.startswith("./"):
        normalized = normalized[2:]
    sprint_folder_name = str(sprint_state.get("sprint_folder_name") or "").strip()
    if sprint_folder_name:
        sprint_prefix = f"shared_workspace/sprints/{sprint_folder_name}/"
        if sprint_prefix in normalized:
            normalized = normalized.split(sprint_prefix, 1)[1].lstrip("/")
    if normalized.startswith("workspace/teams_generated/"):
        normalized = normalized.removeprefix("workspace/teams_generated/").lstrip("/")
    elif normalized.startswith("workspace/"):
        normalized = normalized.removeprefix("workspace/").lstrip("/")
    normalized_workspace_root = str(workspace_root).replace("\\", "/")
    if normalized.startswith(normalized_workspace_root):
        try:
            normalized = Path(normalized).resolve().relative_to(workspace_root).as_posix()
        except Exception:
            normalized = normalized if full_detail else (Path(normalized).name or normalized)
    if full_detail:
        return normalized
    if len(normalized) <= 72:
        return normalized
    return Path(normalized).name or _truncate_sprint_text(normalized, limit=72)


def planner_closeout_request_id(sprint_state: dict[str, Any]) -> str:
    sprint_id = slugify_sprint_value(str(sprint_state.get("sprint_id") or "sprint"))
    return f"planner-closeout-report-{sprint_id}"


def relative_workspace_path(path: Path, workspace_root: Path) -> str:
    try:
        return str(path.relative_to(workspace_root).as_posix())
    except ValueError:
        return str(path.as_posix())


def planner_initial_phase_report_keys(request_record: dict[str, Any]) -> list[str]:
    return [
        str(item).strip()
        for item in (request_record.get("planner_initial_phase_report_keys") or [])
        if str(item).strip()
    ]


def planner_initial_phase_report_key(
    request_record: dict[str, Any],
    *,
    event_type: str,
    status: str,
    summary: str,
) -> str:
    step = initial_phase_step(request_record)
    normalized_summary = _truncate_sprint_text(" ".join(str(summary or "").split()), limit=160)
    digest = hashlib.sha1(normalized_summary.encode("utf-8")).hexdigest()[:10] if normalized_summary else "none"
    return ":".join(
        [
            str(request_record.get("request_id") or "").strip(),
            step or "initial",
            str(event_type or "").strip().lower() or "activity",
            str(status or "").strip().lower() or "unknown",
            digest,
        ]
    )


def planner_initial_phase_next_action(request_record: dict[str, Any], event_type: str, status: str) -> str:
    step = initial_phase_step(request_record)
    normalized_status = str(status or "").strip().lower()
    if normalized_status in {"failed", "blocked"}:
        return "orchestrator 확인"
    if str(event_type or "").strip().lower() == "role_started":
        return f"{initial_phase_step_title(step)} 진행 중"
    next_step = next_initial_phase_step(step)
    if next_step:
        return f"다음 단계: {initial_phase_step_title(next_step)}"
    return "initial phase 완료 대기"


def planner_initial_phase_work_lines(
    *,
    step: str,
    sprint_state: dict[str, Any],
    proposals: dict[str, Any],
    backlog_items: list[dict[str, Any]],
    format_backlog_line: Callable[[dict[str, Any]], str],
    format_todo_line: Callable[[dict[str, Any]], str],
) -> list[str]:
    if step == INITIAL_PHASE_STEP_MILESTONE_REFINEMENT:
        return []
    if step == INITIAL_PHASE_STEP_TODO_FINALIZATION:
        todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
        if todos:
            return [format_todo_line(todo) for todo in todos]
    if backlog_items:
        return [format_backlog_line(item) for item in backlog_items]
    proposal_items = proposals.get("backlog_items")
    if isinstance(proposal_items, list):
        lines: list[str] = []
        for item in proposal_items:
            if isinstance(item, dict):
                title = str(item.get("title") or item.get("backlog_id") or "Untitled").strip()
                item_summary = str(item.get("summary") or "").strip()
                line = f"- {title}"
                if item_summary:
                    line += f" | {item_summary}"
                lines.append(line)
            elif str(item).strip():
                lines.append(f"- {str(item).strip()}")
        return lines
    return []


def planner_initial_phase_priority_lines(
    *,
    step: str,
    sprint_state: dict[str, Any],
    proposals: dict[str, Any],
    backlog_items: list[dict[str, Any]],
    format_backlog_line: Callable[[dict[str, Any]], str],
    format_todo_line: Callable[[dict[str, Any]], str],
) -> list[str]:
    if step not in {INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION, INITIAL_PHASE_STEP_TODO_FINALIZATION}:
        return []
    if step == INITIAL_PHASE_STEP_TODO_FINALIZATION:
        todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
        if todos:
            return [format_todo_line(todo) for todo in todos]
    if backlog_items:
        return [format_backlog_line(item) for item in backlog_items]
    return planner_initial_phase_work_lines(
        step=step,
        sprint_state=sprint_state,
        proposals=proposals,
        backlog_items=backlog_items,
        format_backlog_line=format_backlog_line,
        format_todo_line=format_todo_line,
    )


def build_planner_initial_phase_activity_sections(
    request_record: dict[str, Any],
    *,
    step: str,
    step_position: int,
    event_type: str,
    status: str,
    summary: str,
    sprint_state: dict[str, Any],
    proposals: dict[str, Any],
    semantic_context: dict[str, Any],
    backlog_items: list[dict[str, Any]],
    doc_refs: list[str],
    format_backlog_line: Callable[[dict[str, Any]], str],
    format_todo_line: Callable[[dict[str, Any]], str],
    error_text: str = "",
) -> list[ReportSection]:
    requested_milestone = (
        str(sprint_state.get("requested_milestone_title") or "").strip()
        or str(dict(request_record.get("params") or {}).get("milestone_title") or "").strip()
        or "없음"
    )
    revised_milestone = (
        str(proposals.get("revised_milestone_title") or "").strip()
        or str(sprint_state.get("milestone_title") or "").strip()
        or requested_milestone
    )
    step_title = initial_phase_step_title(step)
    overview_lines = [
        f"- 단계: {step_position}/{len(INITIAL_PHASE_STEPS)} {step_title}",
        f"- 결과: {str(semantic_context.get('what_summary') or summary or step_title).strip()}",
        f"- 다음: {planner_initial_phase_next_action(request_record, event_type, status)}",
    ]
    milestone_lines = [
        f"- requested: {requested_milestone}",
        f"- revised: {revised_milestone}",
    ]
    why_summary = str(semantic_context.get("why_summary") or "").strip()
    if why_summary:
        milestone_lines.append(f"- rationale: {why_summary}")
    evidence_lines = [
        f"- requirement_ref: {item}"
        for item in [str(value).strip() for value in (sprint_state.get("kickoff_requirements") or []) if str(value).strip()][:5]
    ]
    if doc_refs:
        evidence_lines.extend(f"- doc_ref: {item}" for item in doc_refs[:5])
    request_artifacts = [str(item).strip() for item in (request_record.get("artifacts") or []) if str(item).strip()]
    if request_artifacts:
        evidence_lines.extend(f"- evidence_ref: {item}" for item in request_artifacts[:5])
    sections = [
        report_section("핵심 결론", overview_lines),
        report_section("마일스톤", milestone_lines + evidence_lines[:6]),
    ]
    work_lines = planner_initial_phase_work_lines(
        step=step,
        sprint_state=sprint_state,
        proposals=proposals,
        backlog_items=backlog_items,
        format_backlog_line=format_backlog_line,
        format_todo_line=format_todo_line,
    )
    if work_lines:
        sections.append(report_section("정의된 작업", work_lines))
    priority_lines = planner_initial_phase_priority_lines(
        step=step,
        sprint_state=sprint_state,
        proposals=proposals,
        backlog_items=backlog_items,
        format_backlog_line=format_backlog_line,
        format_todo_line=format_todo_line,
    )
    if priority_lines:
        sections.append(report_section("우선순위/확정", priority_lines))
    if step == INITIAL_PHASE_STEP_ARTIFACT_SYNC:
        sync_lines = [f"- doc_sync: {item}" for item in (doc_refs[:8] or request_artifacts[:8])]
        if sync_lines:
            sections.append(report_section("문서/근거", sync_lines))
    elif step != INITIAL_PHASE_STEP_MILESTONE_REFINEMENT:
        doc_lines = [f"- doc_ref: {item}" for item in (doc_refs[:8] or request_artifacts[:8])]
        if doc_lines:
            sections.append(report_section("문서/근거", doc_lines))
    constraint_lines = [
        str(item).strip()
        for item in (semantic_context.get("constraint_points") or [])
        if str(item).strip()
    ]
    normalized_error = str(error_text or "").strip()
    if normalized_error:
        constraint_lines.append(f"error: {normalized_error}")
    if constraint_lines:
        sections.append(report_section("차단/리스크", constraint_lines[:6]))
    return sections


def build_planner_initial_phase_activity_report(
    request_record: dict[str, Any],
    *,
    event_type: str,
    status: str,
    summary: str,
    semantic_context: dict[str, Any],
    sprint_scope: str,
    artifacts: list[str],
    sections: list[ReportSection],
) -> str:
    step = initial_phase_step(request_record)
    if not step:
        return ""
    step_position = initial_phase_step_position(step)
    step_title = initial_phase_step_title(step)
    normalized_event = str(event_type or "").strip().lower()
    status_text = {
        "running": "진행중",
        "completed": "완료",
        "committed": "완료",
        "failed": "실패",
        "blocked": "중단",
    }.get(str(status or "").strip().lower(), str(status or "").strip() or "안내")
    request_label = (
        f"planner initial {step_position}/{len(INITIAL_PHASE_STEPS)} 시작"
        if normalized_event == "role_started"
        else f"planner initial {step_position}/{len(INITIAL_PHASE_STEPS)} 체크포인트"
    )
    semantic_detail_summary = str(semantic_context.get("what_summary") or "").strip() or str(summary or step_title).strip()
    judgment = semantic_detail_summary or str(summary or step_title).strip()
    return build_progress_report(
        request=request_label,
        scope=f"{sprint_scope} | initial {step_position}/{len(INITIAL_PHASE_STEPS)} {step_title}",
        status=status_text,
        list_summary="",
        detail_summary=semantic_detail_summary,
        process_summary="",
        log_summary="",
        end_reason="없음",
        judgment=judgment,
        next_action=planner_initial_phase_next_action(request_record, normalized_event, str(status or "")),
        artifacts=artifacts,
        sections=sections,
    )


def _parse_sprint_datetime(value: str) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def format_sprint_duration(
    sprint_state: dict[str, Any],
    *,
    now: datetime | None = None,
) -> str:
    started_at = _parse_sprint_datetime(str(sprint_state.get("started_at") or ""))
    if started_at is None:
        return "N/A"
    ended_at = _parse_sprint_datetime(str(sprint_state.get("ended_at") or "")) or now or utc_now()
    elapsed_seconds = max(0, int((ended_at - started_at).total_seconds()))
    days, remainder = divmod(elapsed_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}일")
    if hours:
        parts.append(f"{hours}시간")
    if minutes:
        parts.append(f"{minutes}분")
    if seconds and not parts:
        parts.append(f"{seconds}초")
    return " ".join(parts) if parts else "0초"


def _normalize_role_report_insights(result: dict[str, Any]) -> list[str]:
    raw = result.get("insights")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        normalized = str(raw).strip()
        return [normalized] if normalized else []
    return []


def _has_markdown_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_has_markdown_value(item) for item in value)
    if isinstance(value, dict):
        return any(_has_markdown_value(item) for item in value.values())
    return True


def _markdown_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _append_markdown_structure(lines: list[str], value: Any, *, indent: int = 0) -> None:
    prefix = "  " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if not _has_markdown_value(item):
                continue
            label = str(key).strip() or "item"
            if isinstance(item, (dict, list, tuple, set)):
                lines.append(f"{prefix}- {label}:")
                _append_markdown_structure(lines, item, indent=indent + 1)
            else:
                lines.append(f"{prefix}- {label}: {_markdown_scalar(item)}")
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if not _has_markdown_value(item):
                continue
            if isinstance(item, dict):
                title = (
                    str(item.get("title") or "").strip()
                    or str(item.get("name") or "").strip()
                    or str(item.get("summary") or "").strip()
                )
                nested = dict(item)
                if title:
                    nested.pop("title", None)
                    nested.pop("name", None)
                    if str(nested.get("summary") or "").strip() == title:
                        nested.pop("summary", None)
                    lines.append(f"{prefix}- {title}:")
                    if nested:
                        _append_markdown_structure(lines, nested, indent=indent + 1)
                else:
                    lines.append(f"{prefix}- item:")
                    _append_markdown_structure(lines, nested, indent=indent + 1)
            elif isinstance(item, (list, tuple, set)):
                lines.append(f"{prefix}- item:")
                _append_markdown_structure(lines, item, indent=indent + 1)
            else:
                lines.append(f"{prefix}- {_markdown_scalar(item)}")
        return
    if _has_markdown_value(value):
        lines.append(f"{prefix}- {_markdown_scalar(value)}")


def sprint_role_display_name(role: str) -> str:
    normalized = str(role or "").strip().lower()
    return SPRINT_ROLE_DISPLAY_NAMES.get(normalized, normalized or "기타")


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


def sprint_artifact_paths(paths: RuntimePaths, sprint_state: dict[str, Any]) -> dict[str, Path]:
    folder_name = str(sprint_state.get("sprint_folder_name") or "").strip()
    return {
        "root": paths.sprint_artifact_dir(folder_name),
        "index": paths.sprint_artifact_file(folder_name, "index.md"),
        "kickoff": paths.sprint_artifact_file(folder_name, "kickoff.md"),
        "milestone": paths.sprint_artifact_file(folder_name, "milestone.md"),
        "plan": paths.sprint_artifact_file(folder_name, "plan.md"),
        "spec": paths.sprint_artifact_file(folder_name, "spec.md"),
        "todo_backlog": paths.sprint_artifact_file(folder_name, "todo_backlog.md"),
        "iteration_log": paths.sprint_artifact_file(folder_name, "iteration_log.md"),
        "report": paths.sprint_artifact_file(folder_name, "report.md"),
    }


def render_sprint_kickoff_markdown(
    sprint_state: dict[str, Any],
    *,
    source_request_path: str,
) -> str:
    source_request_id = str(sprint_state.get("kickoff_source_request_id") or "").strip()
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
        "# Sprint Kickoff",
        "",
        f"- requested_milestone_title: {sprint_state.get('requested_milestone_title') or 'N/A'}",
        f"- kickoff_source_request_id: {source_request_id or 'N/A'}",
        f"- kickoff_source_request: {source_request_path or 'N/A'}",
        f"- started_at: {sprint_state.get('started_at') or 'N/A'}",
        "",
        "## Original Request Text",
        "",
        str(sprint_state.get("kickoff_request_text") or "kickoff request text 없음").strip(),
        "",
        "## Kickoff Brief",
        "",
        str(sprint_state.get("kickoff_brief") or "kickoff brief 없음").strip(),
        "",
        "## Kickoff Requirements",
        "",
    ]
    if kickoff_requirements:
        lines.extend(f"- {item}" for item in kickoff_requirements)
    else:
        lines.append("- kickoff requirement 없음")
    lines.extend(["", "## Kickoff Reference Artifacts", ""])
    if kickoff_reference_artifacts:
        lines.extend(f"- {item}" for item in kickoff_reference_artifacts)
    else:
        lines.append("- kickoff reference artifact 없음")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_sprint_milestone_markdown(sprint_state: dict[str, Any]) -> str:
    latest = list(sprint_state.get("planning_iterations") or [])
    latest_entry = latest[-1] if latest else {}
    lines = [
        "# Sprint Milestone",
        "",
        f"- requested_milestone_title: {sprint_state.get('requested_milestone_title') or 'N/A'}",
        f"- revised_milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
        f"- sprint_name: {sprint_state.get('sprint_name') or sprint_state.get('sprint_display_name') or 'N/A'}",
        f"- phase: {sprint_state.get('phase') or 'N/A'}",
        f"- started_at: {sprint_state.get('started_at') or 'N/A'}",
        "",
        "## Kickoff Source",
        "",
        "- Preserve the original kickoff brief in `kickoff.md`.",
        "- Use this file for refined milestone framing only.",
        "",
        "## Latest Derived Framing",
        "",
        str(latest_entry.get("summary") or "planner output 없음").strip(),
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def render_sprint_plan_markdown(sprint_state: dict[str, Any]) -> str:
    latest = list(sprint_state.get("planning_iterations") or [])
    latest_entry = latest[-1] if latest else {}
    lines = [
        "# Sprint Plan",
        "",
        f"- sprint_name: {sprint_state.get('sprint_name') or 'N/A'}",
        f"- milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
        f"- requested_milestone_title: {sprint_state.get('requested_milestone_title') or 'N/A'}",
        f"- initial_phase_ready_at: {sprint_state.get('initial_phase_ready_at') or 'N/A'}",
        "",
        "## Latest Planner Summary",
        "",
        str(latest_entry.get("summary") or "planner output 없음").strip(),
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def render_sprint_todo_backlog_markdown(sprint_state: dict[str, Any]) -> str:
    lines = [
        "# Sprint Todo Backlog",
        "",
        f"- sprint_name: {sprint_state.get('sprint_name') or 'N/A'}",
        f"- milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
        "",
        "## Items",
        "",
    ]
    selected_items = list(sprint_state.get("selected_items") or [])
    if not selected_items:
        lines.append("- selected backlog 없음")
        return "\n".join(lines).rstrip() + "\n"
    for item in selected_items:
        lines.extend(
            [
                f"### {item.get('title') or 'Untitled'}",
                f"- backlog_id: {item.get('backlog_id') or ''}",
                f"- status: {item.get('status') or ''}",
                f"- priority_rank: {item.get('priority_rank') or 0}",
                f"- summary: {item.get('summary') or ''}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def collect_sprint_role_report_events(request_record: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    for event in list(request_record.get("events") or []):
        if str(event.get("type") or "").strip() != "role_report":
            continue
        payload = dict(event.get("payload") or {}) if isinstance(event.get("payload"), dict) else {}
        if not payload:
            continue
        events.append({"event": event, "payload": payload})
    return events


def _append_sprint_role_report_details(
    lines: list[str],
    payload: dict[str, Any],
    *,
    include_workflow_transition: bool,
    workflow_transition_provider: WorkflowTransitionProvider,
) -> None:
    lines.extend(
        [
            f"- status: {payload.get('status') or 'N/A'}",
            f"- summary: {payload.get('summary') or ''}",
        ]
    )
    insights = _normalize_role_report_insights(payload)
    if insights:
        lines.extend(["", "##### Insights", ""])
        lines.extend(f"- {item}" for item in insights)
    structured = dict(payload.get("proposals") or {}) if isinstance(payload.get("proposals"), dict) else {}
    if not include_workflow_transition:
        structured.pop("workflow_transition", None)
    if structured:
        lines.extend(["", "##### Structured Output", ""])
        _append_markdown_structure(lines, structured)
    transition = dict(workflow_transition_provider(payload) or {})
    if include_workflow_transition and any(_has_markdown_value(item) for item in transition.values()):
        lines.extend(["", "##### Workflow Transition", ""])
        _append_markdown_structure(lines, transition)
    artifacts = [str(item).strip() for item in (payload.get("artifacts") or []) if str(item).strip()]
    if artifacts:
        lines.extend(["", "##### Artifacts", ""])
        lines.extend(f"- {item}" for item in artifacts)
    lines.append("")


def render_sprint_spec_markdown(
    sprint_state: dict[str, Any],
    *,
    request_entries: list[dict[str, Any]],
    workflow_transition_provider: WorkflowTransitionProvider,
) -> str:
    latest = list(sprint_state.get("planning_iterations") or [])
    latest_entry = latest[-1] if latest else {}
    insights = [
        str(item).strip()
        for item in (latest_entry.get("insights") or [])
        if str(item).strip()
    ]
    lines = [
        "# Sprint Spec",
        "",
        f"- sprint_name: {sprint_state.get('sprint_name') or 'N/A'}",
        f"- milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
        f"- requested_milestone_title: {sprint_state.get('requested_milestone_title') or 'N/A'}",
        "",
        "## Planner Insights",
        "",
    ]
    if insights:
        lines.extend(f"- {item}" for item in insights)
    else:
        lines.append("- planner insight 없음")
    if request_entries:
        lines.extend(
            [
                "",
                "## Canonical Contract Body",
                "",
                "- 이 본문은 `.teams_runtime/requests/*.json`의 workflow role report를 합쳐 만든 sprint-level source of truth입니다.",
                "- role-private handoff와 runtime 메모는 보조 근거로 남기고, shared 계약과 검증 결론은 여기서 우선 확인합니다.",
                "",
            ]
        )
        for entry in request_entries:
            todo = dict(entry.get("todo") or {})
            request_record = dict(entry.get("request") or {})
            lines.extend(
                [
                    f"### {todo.get('title') or request_record.get('scope') or 'Untitled'}",
                    f"- backlog_id: {todo.get('backlog_id') or request_record.get('backlog_id') or 'N/A'}",
                    f"- todo_id: {todo.get('todo_id') or request_record.get('todo_id') or 'N/A'}",
                    f"- request_id: {request_record.get('request_id') or 'N/A'}",
                    f"- final_status: {request_record.get('status') or 'N/A'}",
                    f"- scope: {request_record.get('scope') or ''}",
                    "",
                ]
            )
            role_counts: dict[str, int] = {}
            role_reports = collect_sprint_role_report_events(request_record)
            if not role_reports:
                lines.append("- role report 없음")
                lines.append("")
                continue
            for report in role_reports:
                event = dict(report.get("event") or {})
                payload = dict(report.get("payload") or {})
                role = str(payload.get("role") or event.get("actor") or "").strip().lower()
                if not role:
                    role = "unknown"
                role_counts[role] = role_counts.get(role, 0) + 1
                role_title = sprint_role_display_name(role)
                if role_counts[role] > 1:
                    role_title = f"{role_title} #{role_counts[role]}"
                lines.extend([f"#### {role_title}", ""])
                _append_sprint_role_report_details(
                    lines,
                    payload,
                    include_workflow_transition=True,
                    workflow_transition_provider=workflow_transition_provider,
                )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_sprint_iteration_log_markdown(
    sprint_state: dict[str, Any],
    *,
    request_entries: list[dict[str, Any]],
    workflow_transition_provider: WorkflowTransitionProvider,
) -> str:
    lines = ["# Sprint Iteration Log", ""]
    iterations = list(sprint_state.get("planning_iterations") or [])
    lines.extend(["## Planning Sync", ""])
    if not iterations:
        lines.append("- planning iteration 없음")
        lines.append("")
    else:
        for entry in iterations:
            lines.extend(
                [
                    f"### {entry.get('created_at') or 'N/A'} | {entry.get('phase') or 'N/A'}",
                    f"- request_id: {entry.get('request_id') or 'N/A'}",
                    f"- summary: {entry.get('summary') or ''}",
                    f"- phase_ready: {'yes' if entry.get('phase_ready') else 'no'}",
                    f"- artifacts: {', '.join(str(item) for item in (entry.get('artifacts') or [])) or 'N/A'}",
                    "",
                ]
            )
            insights = [str(item).strip() for item in (entry.get("insights") or []) if str(item).strip()]
            if insights:
                lines.append("#### Insights")
                lines.extend(f"- {item}" for item in insights)
                lines.append("")
    lines.extend(["## Workflow Validation Trace", ""])
    if not request_entries:
        lines.append("- workflow trace 없음")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"
    for entry in request_entries:
        todo = dict(entry.get("todo") or {})
        request_record = dict(entry.get("request") or {})
        lines.extend(
            [
                f"### {todo.get('title') or request_record.get('scope') or 'Untitled'}",
                f"- backlog_id: {todo.get('backlog_id') or request_record.get('backlog_id') or 'N/A'}",
                f"- todo_id: {todo.get('todo_id') or request_record.get('todo_id') or 'N/A'}",
                f"- request_id: {request_record.get('request_id') or 'N/A'}",
                f"- final_status: {request_record.get('status') or 'N/A'}",
                "",
            ]
        )
        for event in list(request_record.get("events") or []):
            timestamp = str(event.get("timestamp") or "N/A").strip() or "N/A"
            actor = str(event.get("actor") or "unknown").strip() or "unknown"
            event_type = str(event.get("type") or "event").strip() or "event"
            lines.extend([f"#### {timestamp} | {actor} | {event_type}", ""])
            lines.append(f"- summary: {event.get('summary') or ''}")
            payload = dict(event.get("payload") or {}) if isinstance(event.get("payload"), dict) else {}
            if event_type == "delegated":
                routing_context = dict(payload.get("routing_context") or {})
                if routing_context:
                    lines.append(
                        f"- selected_role: {routing_context.get('selected_role') or routing_context.get('requested_role') or 'N/A'}"
                    )
                    lines.append(f"- reason: {routing_context.get('reason') or ''}")
            if payload and event_type == "role_report":
                lines.append(f"- role: {payload.get('role') or actor}")
                _append_sprint_role_report_details(
                    lines,
                    payload,
                    include_workflow_transition=True,
                    workflow_transition_provider=workflow_transition_provider,
                )
            else:
                lines.append("")
        return "\n".join(lines).rstrip() + "\n"


def report_section(title: str, lines: list[str] | tuple[str, ...] | None) -> ReportSection:
    normalized_lines = [str(item).strip() for item in (lines or []) if str(item).strip()]
    return ReportSection(title=str(title or "").strip(), lines=tuple(normalized_lines))


def split_report_body_lines(body: str) -> list[str]:
    return [str(line).rstrip() for line in str(body or "").splitlines() if str(line).strip()]


def format_priority_value(value: Any) -> str:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return "N/A"
    return str(normalized) if normalized > 0 else "N/A"


def format_backlog_report_line(item: dict[str, Any]) -> str:
    priority = format_priority_value(item.get("priority_rank"))
    status = str(item.get("status") or "").strip() or "N/A"
    title = str(item.get("title") or item.get("backlog_id") or "Untitled").strip()
    backlog_id = str(item.get("backlog_id") or "N/A").strip()
    return f"- [rank {priority}] [{status}] {title} | backlog_id={backlog_id}"


def format_todo_report_line(todo: dict[str, Any], *, include_artifacts: bool = False) -> str:
    priority = format_priority_value(todo.get("priority_rank"))
    status = str(todo.get("status") or "").strip() or "N/A"
    title = str(todo.get("title") or todo.get("todo_id") or "Untitled").strip()
    request_id = str(todo.get("request_id") or "N/A").strip()
    line = f"- [rank {priority}] [{status}] {title} | request_id={request_id}"
    if include_artifacts:
        artifact_count = len([item for item in (todo.get("artifacts") or []) if str(item).strip()])
        if artifact_count:
            line += f" | artifacts={artifact_count}"
    return line


def build_generic_sprint_report_sections(body: str) -> list[ReportSection]:
    body_lines = split_report_body_lines(body)
    if not body_lines:
        return []
    return [report_section("상세", body_lines)]


def build_sprint_kickoff_report_sections(
    sprint_state: dict[str, Any],
    *,
    selected_lines: list[str],
) -> list[ReportSection]:
    kickoff_lines = [
        f"- sprint_id: {sprint_state.get('sprint_id') or 'N/A'}",
        f"- trigger: {sprint_state.get('trigger') or 'N/A'}",
        f"- selected_backlog: {len(sprint_state.get('selected_items') or [])}",
    ]
    return [
        report_section("킥오프", kickoff_lines),
        report_section("선정 작업", selected_lines),
    ]


def build_sprint_kickoff_preview_lines(sprint_state: dict[str, Any], *, limit: int = 3) -> list[str]:
    todos = list(sprint_state.get("todos") or [])
    selected_items = list(sprint_state.get("selected_items") or [])
    if todos:
        lines = [
            "- {title} | todo_id={todo_id} | owner={owner}".format(
                title=_truncate_sprint_text(str(todo.get("title") or "").strip() or "Untitled", limit=60),
                todo_id=str(todo.get("todo_id") or "").strip() or "N/A",
                owner=str(todo.get("owner_role") or "").strip() or "N/A",
            )
            for todo in todos[:limit]
        ]
        remaining = len(todos) - limit
        if remaining > 0:
            lines.append(f"- ... 외 {remaining}건")
        return lines
    if selected_items:
        lines = [
            "- {title} | backlog_id={backlog_id}".format(
                title=_truncate_sprint_text(str(item.get("title") or "").strip() or "Untitled", limit=60),
                backlog_id=str(item.get("backlog_id") or "").strip() or "N/A",
            )
            for item in selected_items[:limit]
        ]
        remaining = len(selected_items) - limit
        if remaining > 0:
            lines.append(f"- ... 외 {remaining}건")
        return lines
    return ["- 선택된 작업 없음"]


def render_sprint_kickoff_report_body(sprint_state: dict[str, Any]) -> str:
    body_lines = [
        f"📌 sprint_id={sprint_state.get('sprint_id') or ''}",
        f"🧭 trigger={sprint_state.get('trigger') or ''}",
        f"🗂️ selected_backlog={len(sprint_state.get('selected_items') or [])}",
        "📝 kickoff_items:",
        *build_sprint_kickoff_preview_lines(sprint_state),
    ]
    return "\n".join(body_lines)


def build_sprint_kickoff_report_context(sprint_state: dict[str, Any]) -> dict[str, Any]:
    selected_lines = build_sprint_kickoff_preview_lines(
        sprint_state,
        limit=max(1, len(sprint_state.get("todos") or []) or len(sprint_state.get("selected_items") or []) or 3),
    )
    return {
        "title": "🚀 스프린트 시작",
        "body": render_sprint_kickoff_report_body(sprint_state),
        "sections": build_sprint_kickoff_report_sections(sprint_state, selected_lines=selected_lines),
    }


def build_sprint_todo_list_report_sections(sprint_state: dict[str, Any]) -> list[ReportSection]:
    todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
    summary_lines = [
        f"- sprint_id: {sprint_state.get('sprint_id') or 'N/A'}",
        f"- todo_count: {len(todos)}",
    ]
    todo_lines = [format_todo_report_line(todo, include_artifacts=True) for todo in todos] or ["- todo 없음"]
    return [
        report_section("현재 상태", summary_lines),
        report_section("전체 Todo", todo_lines),
    ]


def build_sprint_todo_list_report_body(sprint_state: dict[str, Any]) -> str:
    lines = [
        f"sprint_id={sprint_state.get('sprint_id') or ''}",
        "todo_list:",
    ]
    for todo in sprint_state.get("todos") or []:
        lines.append(f"- {todo.get('todo_id') or ''} | {todo.get('title') or ''} | owner={todo.get('owner_role') or ''}")
    return "\n".join(lines)


def build_sprint_todo_list_report_context(sprint_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "스프린트 TODO",
        "body": build_sprint_todo_list_report_body(sprint_state),
        "sections": build_sprint_todo_list_report_sections(sprint_state),
    }


def build_sprint_spec_todo_report_sections(
    sprint_state: dict[str, Any],
    *,
    backlog_items: list[dict[str, Any]],
    artifact_hints: list[str],
    fallback_todo_lines: list[str],
) -> list[ReportSection]:
    latest = list(sprint_state.get("planning_iterations") or [])
    latest_entry = latest[-1] if latest else {}
    milestone_title = str(
        sprint_state.get("milestone_title") or sprint_state.get("requested_milestone_title") or ""
    ).strip() or "없음"
    requested_milestone = str(sprint_state.get("requested_milestone_title") or "").strip() or "없음"
    todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
    kickoff_requirements = [
        str(item).strip()
        for item in (sprint_state.get("kickoff_requirements") or [])
        if str(item).strip()
    ]
    sections = [
        report_section(
            "핵심 결론",
            [
                f"- planner summary: {str(latest_entry.get('summary') or '').strip() or '없음'}",
                f"- selected_count: {len(todos or sprint_state.get('selected_items') or [])}",
            ],
        ),
        report_section(
            "마일스톤",
            [
                f"- requested: {requested_milestone}",
                f"- active: {milestone_title}",
            ],
        ),
    ]
    if todos:
        sections.append(
            report_section(
                "정의된 작업",
                [format_todo_report_line(todo, include_artifacts=True) for todo in todos],
            )
        )
    else:
        sections.append(report_section("정의된 작업", fallback_todo_lines))
    if backlog_items:
        sections.append(report_section("우선순위", [format_backlog_report_line(item) for item in backlog_items]))
    else:
        sections.append(report_section("우선순위", ["- 우선순위 backlog 없음"]))
    evidence_lines = [f"- requirement: {item}" for item in kickoff_requirements]
    evidence_lines.extend(f"- doc: {hint}" for hint in artifact_hints if str(hint).strip())
    sections.append(report_section("근거 문서", evidence_lines[:12]))
    return sections


def build_sprint_spec_todo_report_body(
    sprint_state: dict[str, Any],
    *,
    todo_lines: list[str],
) -> str:
    latest = list(sprint_state.get("planning_iterations") or [])
    latest_entry = latest[-1] if latest else {}
    milestone_title = str(
        sprint_state.get("milestone_title") or sprint_state.get("requested_milestone_title") or ""
    ).strip() or "없음"
    requested_milestone = str(sprint_state.get("requested_milestone_title") or "").strip() or "없음"
    lines = [
        f"sprint_id: {sprint_state.get('sprint_id') or ''}",
        "",
        "[Milestone]",
        f"- active: {milestone_title}",
        f"- requested: {requested_milestone}",
        "",
        "[Spec]",
        f"- planner summary: {str(latest_entry.get('summary') or '').strip() or '없음'}",
        "- kickoff requirements:",
    ]
    kickoff_requirements = [
        str(item).strip()
        for item in (sprint_state.get("kickoff_requirements") or [])
        if str(item).strip()
    ]
    if kickoff_requirements:
        lines.extend(f"  - {item}" for item in kickoff_requirements[:5])
    else:
        lines.append("  - 없음")
    lines.append("- planner insights:")
    insights = [
        str(item).strip()
        for item in (latest_entry.get("insights") or [])
        if str(item).strip()
    ]
    if insights:
        lines.extend(f"  - {item}" for item in insights[:5])
    else:
        lines.append("  - 없음")
    lines.extend(["", "[TODO]"])
    lines.append(f"- selected_count: {len(sprint_state.get('todos') or sprint_state.get('selected_items') or [])}")
    lines.append("- items:")
    lines.extend([f"  {item}" for item in todo_lines] if todo_lines else ["  - 선택된 작업 없음"])
    return "\n".join(lines)


def _looks_meta_change_text(text: str) -> bool:
    normalized = _collapse_whitespace(text)
    if not normalized:
        return False
    meta_markers = (
        "정리했습니다",
        "정리합니다",
        "구체화했습니다",
        "반영했습니다",
        "반영된 것을 확인했습니다",
        "일관되게 반영",
        "동기화했습니다",
        "재구성했습니다",
        "업데이트했습니다",
        "개선했습니다",
        "개선 방향",
        "prompt",
        "프롬프트",
        "문서",
        "라우팅",
        "회귀 테스트",
        "regression test",
    )
    return any(marker in normalized.lower() for marker in meta_markers)


def _render_text(
    format_text: Callable[..., str],
    value: Any,
    *,
    full_detail: bool,
    limit: int,
) -> str:
    return str(format_text(value, full_detail=full_detail, limit=limit) or "").strip()


def build_sprint_headline(
    sprint_state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    draft: dict[str, Any] | None,
    format_text: Callable[..., str],
    full_detail: bool = False,
) -> str:
    normalized_draft = dict(draft or {})
    if normalized_draft.get("headline"):
        return _render_text(
            format_text,
            normalized_draft["headline"],
            full_detail=full_detail,
            limit=96,
        )
    milestone = _render_text(
        format_text,
        sprint_state.get("milestone_title") or sprint_state.get("requested_milestone_title") or "이 스프린트",
        full_detail=full_detail,
        limit=48,
    )
    todo_status_counts = dict(snapshot.get("todo_status_counts") or {})
    completed_count = int(todo_status_counts.get("committed") or 0) + int(todo_status_counts.get("completed") or 0)
    issue_count = (
        int(todo_status_counts.get("blocked") or 0)
        + int(todo_status_counts.get("failed") or 0)
        + int(todo_status_counts.get("uncommitted") or 0)
    )
    parts = [f"{milestone} 스프린트를 {snapshot.get('status_label') or '완료'}했습니다."]
    todos = list(snapshot.get("todos") or [])
    if completed_count:
        parts.append(f"핵심 작업 {completed_count}건을 반영했습니다.")
    elif todos:
        parts.append(f"todo {len(todos)}건을 정리했습니다.")
    if issue_count:
        parts.append(f"핵심 이슈 {issue_count}건이 남았습니다.")
    elif int(snapshot.get("commit_count") or 0) > 0 and str(snapshot.get("commit_sha") or "").strip():
        parts.append(f"대표 커밋 {str(snapshot['commit_sha'])[:7]}를 남겼습니다.")
    elif str(snapshot.get("closeout_message") or "").strip():
        parts.append(
            _render_text(
                format_text,
                snapshot["closeout_message"],
                full_detail=full_detail,
                limit=72,
            )
        )
    return " ".join(parts).strip()


def build_sprint_overview_lines(
    sprint_state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    headline: str,
) -> list[str]:
    commit_summary = "없음"
    if int(snapshot.get("commit_count") or 0) > 0:
        commit_summary = f"{snapshot['commit_count']}건"
        if str(snapshot.get("commit_sha") or "").strip():
            commit_summary += f" | 대표 {str(snapshot['commit_sha'])[:7]}"
    return [
        f"- TL;DR: {headline}",
        f"- sprint_id: {sprint_state.get('sprint_id') or 'N/A'}",
        f"- sprint_name: {sprint_state.get('sprint_name') or sprint_state.get('sprint_display_name') or 'N/A'}",
        f"- milestone: {sprint_state.get('milestone_title') or 'N/A'}",
        (
            f"- 상태: {snapshot.get('status_label') or 'N/A'}"
            + (
                f" | closeout={snapshot.get('closeout_status')}"
                if str(snapshot.get("closeout_status") or "").strip()
                else ""
            )
        ),
        f"- 기간: {snapshot.get('duration') or 'N/A'}",
        f"- todo 요약: {snapshot.get('todo_summary') or 'N/A'}",
        f"- commit 요약: {commit_summary}",
        f"- 주요 아티팩트: {len(snapshot.get('linked_artifacts') or [])}건",
    ]


def build_sprint_timeline_lines(
    sprint_state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    draft: dict[str, Any] | None,
    format_text: Callable[..., str],
    role_display_name: Callable[[str], str],
    full_detail: bool = False,
) -> list[str]:
    normalized_draft = dict(draft or {})
    draft_timeline = [str(item).strip() for item in (normalized_draft.get("timeline") or []) if str(item).strip()]
    if draft_timeline:
        return [
            "- "
            + _render_text(
                format_text,
                item.removeprefix("- ").strip(),
                full_detail=full_detail,
                limit=160,
            )
            for item in draft_timeline
        ]
    lines: list[str] = []
    todos = list(snapshot.get("todos") or [])
    total_scope = len(sprint_state.get("selected_items") or []) or len(todos)
    trigger = str(sprint_state.get("trigger") or "manual_start").strip() or "manual_start"
    lines.append(f"- 시작: `{trigger}`로 스프린트를 열고 {total_scope}건을 작업 범위에 올렸습니다.")
    planning_sync_events = [event for event in snapshot.get("events") or [] if str(event.get("type") or "") == "planning_sync"]
    planning_iterations = list(sprint_state.get("planning_iterations") or [])
    if planning_sync_events or planning_iterations:
        planning_count = len(planning_sync_events) or len(planning_iterations)
        planning_note = ""
        if planning_sync_events:
            planning_note = _render_text(
                format_text,
                planning_sync_events[-1].get("summary") or "",
                full_detail=full_detail,
                limit=96,
            )
        elif planning_iterations:
            latest_iteration = planning_iterations[-1] if isinstance(planning_iterations[-1], dict) else {}
            planning_note = _render_text(
                format_text,
                _first_meaningful_text(
                    latest_iteration.get("summary"),
                    latest_iteration.get("step"),
                    "planning sync를 정리했습니다.",
                ),
                full_detail=full_detail,
                limit=96,
            )
        lines.append(f"- 계획: planning sync {planning_count}회로 {_first_meaningful_text(planning_note, '실행 범위를 구체화했습니다.')}")
    if todos:
        owner_roles = _dedupe_preserving_order(
            [
                role_display_name(str(todo.get("owner_role") or "planner"))
                for todo in todos
                if str(todo.get("owner_role") or "").strip()
            ]
        )
        role_summary = ", ".join(owner_roles) if owner_roles else "플래너"
        lines.append(f"- 실행: {role_summary}가 todo {len(todos)}건을 처리했습니다.")
    role_events = [
        event
        for event in snapshot.get("events") or []
        if str(event.get("type") or "") in {"role_result", "request_completed"}
    ]
    if role_events:
        verification_summary = _render_text(
            format_text,
            role_events[-1].get("summary") or "",
            full_detail=full_detail,
            limit=100,
        )
        lines.append(f"- 검증: {_first_meaningful_text(verification_summary, '역할별 결과를 회수해 스프린트 상태를 검증했습니다.')}")
    elif str(snapshot.get("closeout_message") or "").strip():
        lines.append(
            "- 검증: "
            + _render_text(
                format_text,
                snapshot["closeout_message"],
                full_detail=full_detail,
                limit=100,
            )
        )
    finish_bits = [f"상태 {snapshot.get('status_label') or 'N/A'}"]
    if str(snapshot.get("closeout_status") or "").strip():
        finish_bits.append(f"closeout={snapshot['closeout_status']}")
    if int(snapshot.get("commit_count") or 0) > 0:
        finish_bits.append(f"commit {snapshot['commit_count']}건")
    if int(len(snapshot.get("linked_artifacts") or [])) > 0:
        finish_bits.append(f"artifact {len(snapshot['linked_artifacts'])}건")
    lines.append(f"- 마감: {', '.join(finish_bits)}으로 정리했습니다.")
    return lines or ["- 흐름 요약 없음"]


def extract_sprint_change_subject(*values: Any) -> str:
    normalized_values = [_collapse_whitespace(value) for value in values if _collapse_whitespace(value)]
    subject_hints = (
        "김단타",
        "딜리게이터",
        "손석희",
        "오케스트레이터",
        "플래너",
        "디자이너",
        "아키텍트",
        "개발자",
        "QA",
        "파서",
        "소서",
        "버전 컨트롤러",
        "orchestrator",
        "planner",
        "designer",
        "architect",
        "developer",
        "qa",
        "version_controller",
    )
    for value in normalized_values:
        for hint in subject_hints:
            if hint and hint in value:
                return hint
    return ""


def resolve_sprint_change_behavior_text(semantic_context: dict[str, Any], *fallbacks: Any) -> str:
    semantic_summary = _collapse_whitespace(semantic_context.get("what_summary") or "")
    semantic_details = [
        _collapse_whitespace(item)
        for item in (semantic_context.get("what_details") or [])
        if _collapse_whitespace(item)
    ]
    if semantic_summary and not _looks_meta_change_text(semantic_summary):
        return semantic_summary
    for detail in semantic_details:
        if not _looks_meta_change_text(detail):
            return detail
    return _first_meaningful_text(*fallbacks)


def resolve_sprint_change_title(
    title: str,
    scope: str,
    semantic_context: dict[str, Any],
    what_changed: str,
) -> str:
    normalized_title = _collapse_whitespace(title)
    normalized_scope = _collapse_whitespace(scope)
    semantic_summary = _collapse_whitespace(semantic_context.get("what_summary") or "")
    semantic_details = [
        _collapse_whitespace(item)
        for item in (semantic_context.get("what_details") or [])
        if _collapse_whitespace(item)
    ]
    if normalized_title and not _looks_meta_change_text(normalized_title):
        return normalized_title
    if semantic_summary and not _looks_meta_change_text(semantic_summary):
        return semantic_summary
    for detail in semantic_details:
        if not _looks_meta_change_text(detail):
            return detail
    if normalized_scope and not _looks_meta_change_text(normalized_scope):
        return normalized_scope
    return normalized_title or normalized_scope or _collapse_whitespace(what_changed) or "Untitled change"


def build_sprint_delivered_change(
    *,
    milestone: str,
    title: str,
    scope: str,
    semantic_context: dict[str, Any],
    insights: list[str],
    artifact_candidates: list[str],
    preview_artifact: Callable[[str], str],
    what_changed_fallbacks: tuple[Any, ...] = (),
) -> dict[str, Any]:
    what_changed = resolve_sprint_change_behavior_text(semantic_context, *what_changed_fallbacks)
    functional_title = resolve_sprint_change_title(
        title,
        scope,
        semantic_context,
        what_changed,
    )
    artifacts: list[str] = []
    for artifact in artifact_candidates:
        preview = str(preview_artifact(artifact) or "").strip()
        if preview:
            artifacts.append(preview)
    artifacts = _dedupe_preserving_order(artifacts)
    subject = extract_sprint_change_subject(
        functional_title,
        what_changed,
        scope,
        semantic_context.get("what_summary"),
        semantic_context.get("why_summary"),
        *insights,
    )
    if milestone and scope:
        why = f"`{milestone}` 마일스톤을 위해 `{scope}` 작업을 반영했습니다."
    elif milestone:
        why = f"`{milestone}` 마일스톤을 달성하기 위한 핵심 변경입니다."
    elif scope:
        why = f"`{scope}` 요구를 실제 동작 변화로 연결했습니다."
    else:
        why = "이번 스프린트 목표를 실제 동작 변화로 연결했습니다."
    return {
        "title": functional_title,
        "subject": subject,
        "scope": scope,
        "what_changed": what_changed,
        "insights": insights,
        "artifacts": artifacts,
        "why": why,
        "semantic_context": semantic_context,
    }


def build_sprint_report_snapshot(
    *,
    sprint_state: dict[str, Any],
    closeout_result: dict[str, Any],
    todos: list[dict[str, Any]],
    delivered_changes: list[dict[str, Any]],
    planner_report_draft: dict[str, Any] | None,
    linked_artifacts: list[Any],
    todo_status_counts: dict[str, int],
    events: list[dict[str, Any]],
    duration: str,
    status_label: str,
    format_count_summary: Callable[[dict[str, int], list[str] | tuple[str, ...]], str],
) -> dict[str, Any]:
    return {
        "todos": todos,
        "delivered_changes": delivered_changes,
        "planner_report_draft": dict(planner_report_draft or {}),
        "linked_artifacts": list(linked_artifacts or []),
        "todo_status_counts": dict(todo_status_counts or {}),
        "todo_summary": format_count_summary(
            todo_status_counts,
            ["running", "queued", "uncommitted", "committed", "completed", "blocked", "failed"],
        ),
        "commit_count": int(closeout_result.get("commit_count") or sprint_state.get("commit_count") or 0),
        "commit_sha": str(closeout_result.get("representative_commit_sha") or sprint_state.get("commit_sha") or "").strip(),
        "closeout_status": str(closeout_result.get("status") or sprint_state.get("closeout_status") or "").strip(),
        "closeout_message": str(closeout_result.get("message") or "").strip(),
        "uncommitted_paths": [
            str(item).strip()
            for item in (closeout_result.get("uncommitted_paths") or sprint_state.get("uncommitted_paths") or [])
            if str(item).strip()
        ],
        "events": list(events or []),
        "duration": str(duration or "").strip(),
        "status_label": str(status_label or "").strip(),
    }


def build_planner_closeout_context_payload(
    *,
    sprint_state: dict[str, Any],
    closeout_result: dict[str, Any],
    snapshot: dict[str, Any],
    request_files: list[str],
) -> dict[str, Any]:
    return {
        "sprint_id": str(sprint_state.get("sprint_id") or ""),
        "sprint_name": str(sprint_state.get("sprint_name") or sprint_state.get("sprint_display_name") or ""),
        "milestone_title": str(sprint_state.get("milestone_title") or ""),
        "status": str(sprint_state.get("status") or ""),
        "closeout_result": dict(closeout_result or {}),
        "todo_summary": str(snapshot.get("todo_summary") or ""),
        "commit_count": int(snapshot.get("commit_count") or 0),
        "commit_sha": str(snapshot.get("commit_sha") or ""),
        "linked_artifacts": list(snapshot.get("linked_artifacts") or []),
        "request_files": [str(item).strip() for item in request_files if str(item).strip()],
    }


def build_planner_closeout_artifacts(
    *,
    context_file: str,
    sprint_artifact_files: list[str],
    request_files: list[str],
) -> list[str]:
    artifacts: list[str] = []
    if str(context_file or "").strip():
        artifacts.append(str(context_file).strip())
    artifacts.extend(str(item).strip() for item in sprint_artifact_files if str(item).strip())
    artifacts.extend(str(item).strip() for item in request_files if str(item).strip())
    return _dedupe_preserving_order(artifacts)


def build_planner_closeout_request_context(
    *,
    sprint_state: dict[str, Any],
    closeout_result: dict[str, Any],
    request_id: str,
    artifacts: list[str],
    created_at: str,
    updated_at: str,
) -> dict[str, Any]:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    scope = f"{sprint_id or 'sprint'} closeout report"
    return {
        "request_id": request_id,
        "status": "queued",
        "intent": "plan",
        "urgency": "normal",
        "scope": scope,
        "body": "Persisted sprint evidence를 읽고 canonical sprint final report용 의미 중심 요약을 작성합니다.",
        "artifacts": [str(item).strip() for item in artifacts if str(item).strip()],
        "params": {
            "_teams_kind": "sprint_closeout_report",
            "sprint_id": sprint_id,
            "closeout_status": str(closeout_result.get("status") or ""),
            "closeout_message": str(closeout_result.get("message") or ""),
            "milestone_title": str(sprint_state.get("milestone_title") or ""),
        },
        "current_role": "planner",
        "next_role": "",
        "owner_role": "orchestrator",
        "sprint_id": sprint_id,
        "created_at": str(created_at or ""),
        "updated_at": str(updated_at or ""),
        "fingerprint": request_id,
        "reply_route": {},
        "events": [],
        "result": {},
        "visited_roles": ["orchestrator"],
    }


def build_planner_closeout_envelope_payload(
    *,
    request_id: str,
    scope: str,
    artifacts: list[str],
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "sender": "orchestrator",
        "target": "planner",
        "intent": "plan",
        "urgency": "normal",
        "scope": str(scope or "").strip(),
        "artifacts": [str(item).strip() for item in artifacts if str(item).strip()],
        "params": {"_teams_kind": "sprint_closeout_report"},
        "body": (
            "Persisted sprint evidence를 읽고 `proposals.sprint_report`로 canonical closeout draft를 작성하세요. "
            "제목/무엇이 달라졌나/의미는 기능 변화 또는 workflow contract 변화 중심으로 작성하고, "
            "prompt·문서·라우팅·회귀 테스트 반영 같은 meta activity 문구는 그대로 반복하지 마세요."
        ),
    }


def build_sprint_report_path_text(report_path: Path, workspace_root: Path) -> str:
    return relative_workspace_path(report_path, workspace_root)


def collect_sprint_report_artifacts(
    paths: RuntimePaths,
    *,
    active_sprint_id: str = "",
    related_artifacts: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    artifacts = [
        str(item).strip()
        for item in (related_artifacts or [])
        if str(item).strip()
    ]
    artifacts.extend(
        [
            str(paths.current_sprint_file),
            str(paths.shared_backlog_file),
            str(paths.shared_completed_backlog_file),
        ]
    )
    normalized_active_sprint_id = str(active_sprint_id or "").strip()
    if normalized_active_sprint_id:
        artifacts.append(str(paths.sprint_events_file(normalized_active_sprint_id)))
    return _dedupe_preserving_order(artifacts)


def build_sprint_progress_report(
    *,
    rendered_title: str,
    sprint_scope: str,
    body: str,
    report_artifacts: list[str],
    status: str = "완료",
    end_reason: str = "없음",
    judgment: str = "",
    next_action: str = "대기",
    commit_message: str = "",
    log_summary: str = "",
    sections: list[ReportSection] | None = None,
) -> str:
    return build_progress_report(
        request=rendered_title,
        scope=sprint_scope,
        status=status,
        list_summary="sprint runner",
        detail_summary=rendered_title,
        process_summary="없음",
        log_summary=log_summary or (body[:500] if body else "없음"),
        end_reason=end_reason,
        judgment=judgment or rendered_title,
        next_action=next_action,
        commit_message=commit_message,
        artifacts=report_artifacts,
        sections=sections if sections is not None else build_generic_sprint_report_sections(body),
    )


def should_refresh_sprint_history_archive(sprint_state: dict[str, Any]) -> bool:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    if not sprint_id:
        return False
    report_body = str(sprint_state.get("report_body") or "").strip()
    if not report_body:
        return False
    status = str(sprint_state.get("status") or "").strip().lower()
    ended_at = str(sprint_state.get("ended_at") or "").strip()
    return bool(ended_at or status in {"completed", "failed", "blocked", "closeout"})


def build_sprint_history_archive_payload(
    *,
    sprint_state: dict[str, Any],
    report_body: str,
    history_path: Path,
    existing_index_rows: list[dict[str, Any]],
) -> dict[str, str]:
    return {
        "archived_path": str(history_path),
        "history_markdown": render_sprint_history_markdown(sprint_state, report_body),
        "history_index_markdown": render_sprint_history_index(existing_index_rows, sprint_state),
    }


def build_sprint_history_archive_update(
    *,
    current_report_path: str,
    archived_path: str,
) -> dict[str, Any]:
    normalized_current = str(current_report_path or "").strip()
    normalized_archived = str(archived_path or "").strip()
    return {
        "report_path": normalized_archived,
        "changed": bool(normalized_archived) and normalized_current != normalized_archived,
    }


def archive_sprint_history(paths: RuntimePaths, sprint_state: dict[str, Any], report_body: str) -> str:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    history_path = paths.sprint_history_file(sprint_id)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    archive_payload = build_sprint_history_archive_payload(
        sprint_state=sprint_state,
        report_body=report_body,
        history_path=history_path,
        existing_index_rows=load_sprint_history_index(paths.sprint_history_index_file),
    )
    history_path.write_text(
        archive_payload["history_markdown"],
        encoding="utf-8",
    )
    paths.sprint_history_index_file.write_text(
        archive_payload["history_index_markdown"],
        encoding="utf-8",
    )
    return archive_payload["archived_path"]


def refresh_sprint_history_archive(paths: RuntimePaths, sprint_state: dict[str, Any]) -> bool:
    if not should_refresh_sprint_history_archive(sprint_state):
        return False
    archived_path = archive_sprint_history(
        paths,
        sprint_state,
        str(sprint_state.get("report_body") or "").strip(),
    )
    archive_update = build_sprint_history_archive_update(
        current_report_path=str(sprint_state.get("report_path") or ""),
        archived_path=archived_path,
    )
    if not archive_update["changed"]:
        return False
    sprint_state["report_path"] = archive_update["report_path"]
    return True


def build_sprint_report_archive_state(
    *,
    report_body: str,
    report_path: str,
) -> dict[str, str]:
    return {
        "report_body": str(report_body or "").strip(),
        "report_path": str(report_path or "").strip(),
    }


def build_sprint_terminal_state_update(
    *,
    status: str,
    closeout_status: str,
    ended_at: str,
) -> dict[str, str]:
    return {
        "status": str(status or "").strip(),
        "closeout_status": str(closeout_status or "").strip(),
        "ended_at": str(ended_at or "").strip(),
    }


def build_sprint_closeout_state_update(
    *,
    closeout_result: dict[str, Any],
    ended_at: str,
) -> dict[str, Any]:
    closeout_status = str(closeout_result.get("status") or "").strip()
    terminal_status = (
        "completed"
        if closeout_status in {"verified", "no_new_commits", "no_repo", "warning_missing_sprint_tag"}
        else "failed"
    )
    return {
        "commit_sha": str(closeout_result.get("representative_commit_sha") or "").strip(),
        "commit_shas": [
            str(item).strip()
            for item in (closeout_result.get("commit_shas") or [])
            if str(item).strip()
        ],
        "commit_count": int(closeout_result.get("commit_count") or 0),
        "uncommitted_paths": [
            str(item).strip()
            for item in (closeout_result.get("uncommitted_paths") or [])
            if str(item).strip()
        ],
        **build_sprint_terminal_state_update(
            status=terminal_status,
            closeout_status=closeout_status,
            ended_at=ended_at,
        ),
    }


def build_sprint_closeout_result(
    *,
    sprint_state: dict[str, Any],
    status: str,
    message: str,
    commit_count: int | None = None,
    commit_shas: list[str] | None = None,
    representative_commit_sha: str | None = None,
    uncommitted_paths: list[str] | None = None,
) -> dict[str, Any]:
    normalized_commit_shas = [
        str(item).strip()
        for item in (commit_shas if commit_shas is not None else sprint_state.get("commit_shas") or [])
        if str(item).strip()
    ]
    normalized_uncommitted_paths = [
        str(item).strip()
        for item in (uncommitted_paths if uncommitted_paths is not None else sprint_state.get("uncommitted_paths") or [])
        if str(item).strip()
    ]
    return {
        "status": str(status or "").strip(),
        "message": str(message or "").strip(),
        "commit_count": int(commit_count if commit_count is not None else sprint_state.get("commit_count") or 0),
        "commit_shas": normalized_commit_shas,
        "representative_commit_sha": str(
            representative_commit_sha
            if representative_commit_sha is not None
            else sprint_state.get("commit_sha") or ""
        ).strip(),
        "uncommitted_paths": normalized_uncommitted_paths,
    }


def build_terminal_sprint_report_context(
    *,
    sprint_state: dict[str, Any],
    closeout_result: dict[str, Any],
    title: str = "",
) -> dict[str, Any]:
    normalized_title = str(title or "").strip()
    closeout_status = str(closeout_result.get("status") or "").strip()
    sprint_status = str(sprint_state.get("status") or "").strip().lower()
    if normalized_title:
        resolved_title = normalized_title
    elif closeout_status == "warning_missing_sprint_tag":
        resolved_title = "⚠️ 스프린트 완료(경고)"
    elif sprint_status == "completed":
        resolved_title = "✅ 스프린트 완료"
    else:
        resolved_title = "⚠️ 스프린트 실패"
    commit_message = (
        str(sprint_state.get("version_control_message") or "").strip()
        if str(sprint_state.get("version_control_status") or "").strip() == "committed"
        else ""
    )
    related_artifacts = _dedupe_preserving_order(
        [
            str(sprint_state.get("report_path") or "").strip(),
            *[
                str(item).strip()
                for item in (sprint_state.get("version_control_paths") or [])
                if str(item).strip()
            ],
            *[
                str(item).strip()
                for item in (sprint_state.get("uncommitted_paths") or [])
                if str(item).strip()
            ],
        ]
    )
    return {
        "title": resolved_title,
        "judgment": str(closeout_result.get("message") or resolved_title).strip(),
        "commit_message": commit_message,
        "related_artifacts": related_artifacts,
    }


def build_closeout_terminal_report_context(
    *,
    sprint_state: dict[str, Any],
    closeout_result: dict[str, Any],
) -> dict[str, Any]:
    return build_terminal_sprint_report_context(
        sprint_state=sprint_state,
        closeout_result=closeout_result,
    )


def build_sprint_change_behavior_summary(change: dict[str, Any]) -> str:
    subject = str(change.get("subject") or "").strip()
    what_changed = _collapse_whitespace(change.get("what_changed") or "")
    if not what_changed:
        return "실제 동작 변화 설명을 별도로 남기지 않았습니다."
    if subject and subject not in what_changed and not what_changed.startswith("이제 "):
        return f"이제 {subject}는 {what_changed}"
    return what_changed


def build_sprint_change_meaning(change: dict[str, Any]) -> str:
    subject = str(change.get("subject") or "").strip()
    what_changed = _collapse_whitespace(change.get("what_changed") or "")
    semantic_context = dict(change.get("semantic_context") or {})
    semantic_why = _collapse_whitespace(semantic_context.get("why_summary") or "")
    if semantic_why:
        return semantic_why
    insight_text = " ".join(str(item).strip() for item in (change.get("insights") or []) if str(item).strip())
    combined = " ".join(part for part in [what_changed, insight_text] if part).lower()
    subject_noun = subject or "이 기능"
    subject_actor = f"{subject_noun}가"
    subject_output = f"{subject_noun}의"
    if any(keyword in combined for keyword in ("workflow", "routing", "planner", "designer", "architect", "contract", "advisory", "finalization")):
        return f"이번 스프린트 기준으로는 이제 {subject_actor} planning/workflow 계약을 더 엄격하게 따른다는 의미입니다."
    if any(keyword in combined for keyword in ("리포트", "요약", "메시지", "문서", "보고")):
        return f"사용자 입장에서는 {subject_output} 출력과 설명이 더 읽기 쉽고 바로 이해되는 방향으로 바뀐다는 의미입니다."
    if any(keyword in combined for keyword in ("기준", "조건", "규칙", "policy", "정책", "threshold", "임계치", "판단")):
        return f"사용자 입장에서는 이제 {subject_actor} 언제 어떤 판단을 내리는지 기준이 더 분명해진다는 의미입니다."
    if any(keyword in combined for keyword in ("사이클", "주기", "cadence", "interval", "타이밍", "실시간")):
        return f"사용자 입장에서는 이제 {subject_actor} 반응 타이밍과 판단 주기를 다르게 가져간다는 의미입니다."
    if any(keyword in combined for keyword in ("동기화", "정리", "재구성", "구성", "전환", "개선", "정돈")):
        return f"사용자 입장에서는 이제 {subject_actor} 동작과 결과를 더 일관되게 보여 준다는 의미입니다."
    if what_changed:
        return f"사용자 입장에서는 이제 {subject_actor} `{what_changed}` 방향으로 동작한다고 이해하면 됩니다."
    return f"사용자 입장에서는 이제 {subject_actor} 동작과 결과 해석 방식이 달라졌다고 보면 됩니다."


def build_sprint_change_how_lines(
    change: dict[str, Any],
    *,
    format_text: Callable[..., str],
    full_detail: bool = False,
) -> list[str]:
    lines: list[str] = ["- 어떻게:"]
    insights = [str(item).strip() for item in (change.get("insights") or []) if str(item).strip()]
    artifacts = [str(item).strip() for item in (change.get("artifacts") or []) if str(item).strip()]
    scope = _collapse_whitespace(change.get("scope") or "")
    if insights:
        lines.append(
            "  - 핵심 로직: "
            + _render_text(
                format_text,
                " / ".join(insights),
                full_detail=full_detail,
                limit=160,
            )
        )
    if artifacts:
        lines.append(
            "  - 구현 근거 아티팩트: "
            + _render_text(
                format_text,
                ", ".join(artifacts),
                full_detail=full_detail,
                limit=180,
            )
        )
    if scope:
        lines.append(
            "  - 작업 범위: "
            + _render_text(
                format_text,
                scope,
                full_detail=full_detail,
                limit=160,
            )
        )
    if len(lines) == 1:
        lines.append("- 요약된 구현 근거 없음")
    return lines


def build_sprint_change_summary_lines(
    sprint_state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    draft: dict[str, Any] | None,
    format_text: Callable[..., str],
    full_detail: bool = False,
) -> list[str]:
    normalized_draft = dict(draft or {})
    draft_changes = list(normalized_draft.get("changes") or [])
    if draft_changes:
        lines: list[str] = []
        for index, change in enumerate(draft_changes):
            title = _render_text(
                format_text,
                change.get("title") or "Untitled change",
                full_detail=full_detail,
                limit=96,
            )
            lines.extend(
                [
                    f"### {title}",
                    "- 왜: "
                    + _render_text(
                        format_text,
                        change.get("why") or "이번 스프린트 목표를 실제 변화로 연결했습니다.",
                        full_detail=full_detail,
                        limit=120,
                    ),
                    "- 무엇이 달라졌나: "
                    + _render_text(
                        format_text,
                        change.get("what_changed") or "실제 동작 변화 설명을 별도로 남기지 않았습니다.",
                        full_detail=full_detail,
                        limit=140,
                    ),
                    "- 의미: "
                    + _render_text(
                        format_text,
                        change.get("meaning") or "이번 스프린트 결과의 의미를 별도로 남기지 않았습니다.",
                        full_detail=full_detail,
                        limit=140,
                    ),
                    "- 어떻게: "
                    + _render_text(
                        format_text,
                        change.get("how") or "관련 요청, 문서, 산출물을 함께 검토해 반영했습니다.",
                        full_detail=full_detail,
                        limit=160,
                    ),
                ]
            )
            artifacts = [str(item).strip() for item in (change.get("artifacts") or []) if str(item).strip()]
            if artifacts:
                lines.append("- 관련 아티팩트: " + ", ".join(artifacts))
            if index < len(draft_changes) - 1:
                lines.append("")
        return lines

    changes = list(snapshot.get("delivered_changes") or [])
    if not changes:
        milestone = _render_text(
            format_text,
            sprint_state.get("milestone_title") or sprint_state.get("requested_milestone_title") or "이번 스프린트",
            full_detail=full_detail,
            limit=80,
        )
        closeout_message = _render_text(
            format_text,
            snapshot.get("closeout_message") or "closeout 상태를 정리했습니다.",
            full_detail=full_detail,
            limit=120,
        )
        return [
            f"- 왜: `{milestone}` 기준으로 이번 스프린트 결과를 마감했습니다.",
            "- 무엇이 달라졌나: 실제로 완료/커밋된 delivered change는 없었습니다.",
            "- 의미: 사용자 입장에서는 이번 스프린트가 새로운 동작 변경보다 상태 정리와 closeout 확인 중심으로 끝났다는 의미입니다.",
            "- 어떻게:",
            f"  - closeout 정리: {closeout_message}",
        ]

    lines: list[str] = []
    for index, change in enumerate(changes):
        title = _render_text(
            format_text,
            change.get("title") or "Untitled change",
            full_detail=full_detail,
            limit=96,
        )
        lines.extend(
            [
                f"### {title}",
                f"- 왜: {_render_text(format_text, change.get('why') or '', full_detail=full_detail, limit=120)}",
                (
                    "- 무엇이 달라졌나: "
                    + _render_text(
                        format_text,
                        build_sprint_change_behavior_summary(change),
                        full_detail=full_detail,
                        limit=140,
                    )
                ),
                (
                    "- 의미: "
                    + _render_text(
                        format_text,
                        build_sprint_change_meaning(change),
                        full_detail=full_detail,
                        limit=140,
                    )
                ),
            ]
        )
        lines.extend(
            build_sprint_change_how_lines(
                change,
                format_text=format_text,
                full_detail=full_detail,
            )
        )
        artifacts = [str(item).strip() for item in (change.get("artifacts") or []) if str(item).strip()]
        if artifacts:
            lines.append("  - 참고 아티팩트: " + ", ".join(artifacts))
        if index < len(changes) - 1:
            lines.append("")
    return lines


def build_sprint_agent_contribution_lines(
    sprint_state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    draft: dict[str, Any] | None,
    format_text: Callable[..., str],
    role_display_name: Callable[[str], str],
    preview_artifact: Callable[[dict[str, Any], str], str],
    team_roles: tuple[str, ...] | list[str],
    full_detail: bool = False,
) -> list[str]:
    normalized_draft = dict(draft or {})
    draft_contributions = list(normalized_draft.get("agent_contributions") or [])
    if draft_contributions:
        lines: list[str] = []
        for item in draft_contributions:
            role = str(item.get("role") or "").strip()
            role_label = role_display_name(role) if role else "역할"
            summary = _render_text(
                format_text,
                item.get("summary") or "스프린트 진행을 지원했습니다.",
                full_detail=full_detail,
                limit=140,
            )
            line = f"- {role_label}" + (f" ({role})" if role else "") + f": {summary}"
            artifacts = [str(value).strip() for value in (item.get("artifacts") or []) if str(value).strip()]
            if artifacts:
                line += " 주요 산출물: " + ", ".join(artifacts)
            lines.append(line)
        return lines
    contributions: dict[str, dict[str, Any]] = {}

    def ensure_role(role: str) -> dict[str, Any]:
        normalized = str(role or "").strip().lower() or "orchestrator"
        if normalized not in contributions:
            contributions[normalized] = {
                "todo_count": 0,
                "completed_count": 0,
                "issue_count": 0,
                "event_count": 0,
                "titles": [],
                "highlights": [],
                "artifacts": [],
            }
        return contributions[normalized]

    for todo in snapshot.get("todos") or []:
        role = str(todo.get("owner_role") or "planner")
        data = ensure_role(role)
        data["todo_count"] += 1
        status = str(todo.get("status") or "").strip().lower()
        if status in {"committed", "completed"}:
            data["completed_count"] += 1
        elif status in {"blocked", "failed", "uncommitted"}:
            data["issue_count"] += 1
        title = _render_text(
            format_text,
            todo.get("title") or "",
            full_detail=full_detail,
            limit=72,
        )
        if title and title not in data["titles"]:
            data["titles"].append(title)
        for artifact in todo.get("artifacts") or []:
            preview = str(preview_artifact(sprint_state, str(artifact)) or "").strip()
            if preview and preview not in data["artifacts"]:
                data["artifacts"].append(preview)

    for event in snapshot.get("events") or []:
        payload = dict(event.get("payload") or {}) if isinstance(event.get("payload"), dict) else {}
        role = str(payload.get("role") or "").strip()
        if not role:
            continue
        data = ensure_role(role)
        data["event_count"] += 1
        summary = _render_text(
            format_text,
            event.get("summary") or "",
            full_detail=full_detail,
            limit=108,
        )
        if summary and summary not in data["highlights"]:
            data["highlights"].append(summary)

    version_control_status = str(sprint_state.get("version_control_status") or "").strip().lower()
    if version_control_status and version_control_status != "not_needed":
        data = ensure_role("version_controller")
        data["event_count"] += 1
        summary = _render_text(
            format_text,
            _first_meaningful_text(
                sprint_state.get("version_control_message"),
                snapshot.get("closeout_message"),
                f"closeout version control 상태={version_control_status}",
            ),
            full_detail=full_detail,
            limit=108,
        )
        if summary and summary not in data["highlights"]:
            data["highlights"].append(summary)
        for artifact in sprint_state.get("version_control_paths") or []:
            preview = str(preview_artifact(sprint_state, str(artifact)) or "").strip()
            if preview and preview not in data["artifacts"]:
                data["artifacts"].append(preview)

    normalized_team_roles = tuple(team_roles)
    team_role_set = {*normalized_team_roles, "version_controller"}
    ordered_roles = [
        *normalized_team_roles,
        "version_controller",
        *sorted(role for role in contributions if role not in team_role_set),
    ]
    lines: list[str] = []
    for role in ordered_roles:
        data = contributions.get(role)
        if not data:
            continue
        stats: list[str] = []
        if int(data.get("todo_count") or 0) > 0:
            stats.append(f"todo {int(data['todo_count'])}건")
        if int(data.get("completed_count") or 0) > 0:
            stats.append(f"완료 {int(data['completed_count'])}건")
        elif int(data.get("issue_count") or 0) > 0:
            stats.append(f"이슈 {int(data['issue_count'])}건")
        elif int(data.get("event_count") or 0) > 0:
            stats.append(f"이벤트 {int(data['event_count'])}건")
        if full_detail:
            highlight_parts = [str(item).strip() for item in (data.get("highlights") or []) if str(item).strip()]
            if not highlight_parts and data.get("titles"):
                highlight_parts = [f"{', '.join(data['titles'])} 작업을 담당했습니다."]
            highlight = " | ".join(highlight_parts) if highlight_parts else "스프린트 진행을 지원했습니다."
        else:
            highlight = _first_meaningful_text(
                *(data.get("highlights") or []),
                (f"{data['titles'][0]} 등 {int(data['todo_count'])}건을 담당했습니다." if data.get("titles") else ""),
                "스프린트 진행을 지원했습니다.",
            )
        lines.append(f"- {role_display_name(role)} ({role}): {', '.join(stats) or '활동 기록'}.")
        if highlight:
            lines.append(
                "  - 근거 하이라이트: "
                + _render_text(
                    format_text,
                    highlight,
                    full_detail=full_detail,
                    limit=120,
                )
            )
        artifact_items = [str(item).strip() for item in (data.get("artifacts") or []) if str(item).strip()]
        if full_detail:
            artifact_preview = ", ".join(artifact_items).strip()
        else:
            artifact_preview = ", ".join(artifact_items[:2]).strip()
        if artifact_preview:
            remaining = max(0, len(artifact_items) - 2)
            if not full_detail and remaining > 0:
                artifact_preview += f" 외 {remaining}건"
            lines.append(f"  - 참고 산출물: {artifact_preview}")
    return lines or ["- 역할별 기여 기록 없음"]


def build_sprint_issue_lines(
    sprint_state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    draft: dict[str, Any] | None,
    format_text: Callable[..., str],
    role_display_name: Callable[[str], str],
    preview_artifact: Callable[[dict[str, Any], str], str],
    full_detail: bool = False,
) -> list[str]:
    normalized_draft = dict(draft or {})
    draft_issues = [str(item).strip() for item in (normalized_draft.get("issues") or []) if str(item).strip()]
    if draft_issues:
        return [
            "- "
            + _render_text(
                format_text,
                item.removeprefix("- ").strip(),
                full_detail=full_detail,
                limit=140,
            )
            for item in draft_issues
        ]
    issues: list[str] = []
    seen: set[str] = set()
    for todo in snapshot.get("todos") or []:
        status = str(todo.get("status") or "").strip().lower()
        if status not in {"blocked", "failed", "uncommitted"}:
            continue
        reason = _first_meaningful_text(
            todo.get("summary"),
            todo.get("carry_over_backlog_id"),
            "후속 조치가 필요합니다.",
        )
        line = "- [{status}] {title}: {reason}".format(
            status=status,
            title=_render_text(format_text, todo.get("title") or "Untitled", full_detail=full_detail, limit=96),
            reason=_render_text(format_text, reason, full_detail=full_detail, limit=108),
        )
        if line not in seen:
            seen.add(line)
            issues.append(line)
    if snapshot.get("uncommitted_paths"):
        preview_items: list[str] = []
        raw_paths = list(snapshot.get("uncommitted_paths") or [])
        if not full_detail:
            raw_paths = raw_paths[:3]
        for raw_path in raw_paths:
            preview = str(preview_artifact(sprint_state, raw_path) or "").strip()
            if preview:
                preview_items.append(preview)
        preview_text = ", ".join(preview_items)
        overflow = max(0, len(snapshot.get("uncommitted_paths") or []) - len(preview_items))
        if not full_detail and overflow > 0:
            preview_text = f"{preview_text} 외 {overflow}건" if preview_text else f"외 {overflow}건"
        line = f"- 참고: 미커밋 경로 {len(snapshot.get('uncommitted_paths') or [])}건 | {preview_text or '상세 경로 확인 필요'}"
        if line not in seen:
            seen.add(line)
            issues.append(line)
    for event in snapshot.get("events") or []:
        payload = dict(event.get("payload") or {}) if isinstance(event.get("payload"), dict) else {}
        role = str(payload.get("role") or "").strip()
        error = _first_meaningful_text(payload.get("error"), payload.get("details"))
        if not error:
            continue
        line = (
            f"- {role_display_name(role)} 이슈: "
            + _render_text(format_text, error, full_detail=full_detail, limit=108)
        )
        if line not in seen:
            seen.add(line)
            issues.append(line)
    if not issues and str(sprint_state.get("status") or "").strip().lower() in {"failed", "blocked"}:
        issues.append(
            "- "
            + _render_text(
                format_text,
                _first_meaningful_text(snapshot.get("closeout_message"), "스프린트 마감 단계에서 이슈가 발생했습니다."),
                full_detail=full_detail,
                limit=112,
            )
        )
    return issues or ["- 핵심 이슈 없음"]


def build_sprint_achievement_lines(
    sprint_state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    draft: dict[str, Any] | None,
    format_text: Callable[..., str],
    full_detail: bool = False,
) -> list[str]:
    normalized_draft = dict(draft or {})
    draft_achievements = [str(item).strip() for item in (normalized_draft.get("achievements") or []) if str(item).strip()]
    if draft_achievements:
        return [
            "- "
            + _render_text(
                format_text,
                item.removeprefix("- ").strip(),
                full_detail=full_detail,
                limit=140,
            )
            for item in draft_achievements
        ]
    achievements: list[str] = []
    for todo in snapshot.get("todos") or []:
        status = str(todo.get("status") or "").strip().lower()
        if status not in {"committed", "completed"}:
            continue
        achievements.append(
            "- [{status}] {title}".format(
                status=status,
                title=_render_text(format_text, todo.get("title") or "Untitled", full_detail=full_detail, limit=96),
            )
        )
    if int(snapshot.get("commit_count") or 0) > 0:
        commit_line = f"- closeout commit {int(snapshot['commit_count'])}건을 남겼습니다."
        if str(snapshot.get("commit_sha") or "").strip():
            commit_line += f" 대표 SHA={str(snapshot['commit_sha'])[:7]}"
        achievements.append(commit_line)
    if int(len(snapshot.get("linked_artifacts") or [])) > 0:
        achievements.append(f"- 주요 산출물 {len(snapshot['linked_artifacts'])}건을 report에 연결했습니다.")
    if (
        str(sprint_state.get("status") or "").strip().lower() == "completed"
        and str(snapshot.get("closeout_message") or "").strip()
    ):
        achievements.append(
            "- "
            + _render_text(
                format_text,
                snapshot["closeout_message"],
                full_detail=full_detail,
                limit=108,
            )
        )
    achievements = _dedupe_preserving_order(achievements)
    return achievements or ["- 주요 성과 없음"]


def build_sprint_artifact_lines(
    sprint_state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    draft: dict[str, Any] | None,
    format_text: Callable[..., str],
    preview_artifact: Callable[[dict[str, Any], str], str],
    full_detail: bool = False,
) -> list[str]:
    normalized_draft = dict(draft or {})
    draft_artifacts = [str(item).strip() for item in (normalized_draft.get("highlight_artifacts") or []) if str(item).strip()]
    if draft_artifacts:
        return [
            "- "
            + _render_text(
                format_text,
                item.removeprefix("- ").strip(),
                full_detail=full_detail,
                limit=160,
            )
            for item in draft_artifacts
        ]
    lines: list[str] = []
    for entry in snapshot.get("linked_artifacts") or []:
        lines.append(
            "- 참고: [{status}] {title} -> {path}".format(
                status=entry["status"],
                title=_render_text(format_text, entry["title"], full_detail=full_detail, limit=72),
                path=entry["path"],
            )
        )
    if lines:
        return lines
    commit_paths: list[str] = []
    for raw_path in sprint_state.get("version_control_paths") or []:
        preview = str(preview_artifact(sprint_state, str(raw_path)) or "").strip()
        if preview:
            commit_paths.append(preview)
    if commit_paths:
        return [f"- 참고: {item}" for item in (commit_paths if full_detail else commit_paths[:5])]
    return ["- 참고 아티팩트 없음"]


def render_sprint_completion_user_report(
    *,
    title: str,
    sprint_state: dict[str, Any],
    snapshot: dict[str, Any],
    report_path_text: str,
    decorate_title: Callable[[str], str],
    build_headline: Callable[[dict[str, Any], dict[str, Any], bool], str],
    build_change_summary_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_timeline_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_agent_contribution_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_issue_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_achievement_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_artifact_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
) -> str:
    commit_metric = f"{int(snapshot.get('commit_count') or 0)}"
    if str(snapshot.get("commit_sha") or "").strip():
        commit_metric += f" ({str(snapshot['commit_sha'])[:7]})"
    lines = [
        f"## {decorate_title(title)} 사용자 요약",
        "",
        f"**TL;DR** {build_headline(sprint_state, snapshot, True)}",
        "",
        "```text",
        f"sprint_id : {sprint_state.get('sprint_id') or 'N/A'}",
        f"status    : {snapshot.get('status_label') or 'N/A'}",
        f"duration  : {snapshot.get('duration') or 'N/A'}",
        f"todo      : {snapshot.get('todo_summary') or 'N/A'}",
        f"commits   : {commit_metric}",
        f"artifacts : {len(snapshot.get('linked_artifacts') or [])}",
        "```",
        "",
        "🔄 변경 요약",
        *build_change_summary_lines(sprint_state, snapshot, True),
        "",
        "🧭 흐름",
        *build_timeline_lines(sprint_state, snapshot, True),
        "",
        "🤖 에이전트 기여",
        *build_agent_contribution_lines(sprint_state, snapshot, True),
        "",
        "⚠️ 핵심 이슈",
        *build_issue_lines(sprint_state, snapshot, True),
        "",
        "🏁 성과",
        *build_achievement_lines(sprint_state, snapshot, True),
        "",
        "📎 참고 아티팩트",
        *build_artifact_lines(sprint_state, snapshot, True),
        "",
        f"상세 보고: `{report_path_text}`",
    ]
    return "\n".join(lines).strip()


def build_terminal_sprint_report_sections(
    sprint_state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    build_overview_lines: Callable[..., list[str]],
    build_change_summary_lines: Callable[..., list[str]],
    build_timeline_lines: Callable[..., list[str]],
    build_agent_contribution_lines: Callable[..., list[str]],
    build_issue_lines: Callable[..., list[str]],
    build_achievement_lines: Callable[..., list[str]],
    build_artifact_lines: Callable[..., list[str]],
) -> list[ReportSection]:
    return [
        report_section("한눈에 보기", build_overview_lines(sprint_state, snapshot, full_detail=True)),
        report_section("변경 요약", build_change_summary_lines(sprint_state, snapshot, full_detail=True)),
        report_section("Sprint A to Z", build_timeline_lines(sprint_state, snapshot, full_detail=True)),
        report_section("에이전트 기여", build_agent_contribution_lines(sprint_state, snapshot, full_detail=True)),
        report_section("핵심 이슈", build_issue_lines(sprint_state, snapshot, full_detail=True)),
        report_section("성과", build_achievement_lines(sprint_state, snapshot, full_detail=True)),
        report_section("참고 아티팩트", build_artifact_lines(sprint_state, snapshot, full_detail=True)),
    ]


def render_live_sprint_report_markdown(
    sprint_state: dict[str, Any],
    *,
    todo_status_counts: dict[str, int],
    linked_artifacts: list[dict[str, Any]],
    status_label: str,
    format_count_summary: Callable[[dict[str, int], list[str] | tuple[str, ...]], str],
) -> str:
    todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
    milestone = str(sprint_state.get("milestone_title") or "이 스프린트").strip() or "이 스프린트"
    running_count = int(todo_status_counts.get("running") or 0)
    issue_count = (
        int(todo_status_counts.get("blocked") or 0)
        + int(todo_status_counts.get("failed") or 0)
        + int(todo_status_counts.get("uncommitted") or 0)
    )
    queued_count = int(todo_status_counts.get("queued") or 0)
    completed_count = int(todo_status_counts.get("committed") or 0) + int(todo_status_counts.get("completed") or 0)
    headline_parts = [f"{milestone} 스프린트가 {status_label} 상태입니다."]
    if running_count:
        headline_parts.append(f"실행 중 {running_count}건이 있습니다.")
    if issue_count:
        headline_parts.append(f"후속 확인 {issue_count}건이 남아 있습니다.")
    elif queued_count:
        headline_parts.append(f"다음 대기 작업 {queued_count}건이 있습니다.")
    elif completed_count:
        headline_parts.append("현재 남은 후속 액션이 없습니다.")
    elif todos:
        headline_parts.append(f"todo {len(todos)}건을 추적 중입니다.")
    else:
        headline_parts.append("등록된 todo가 없습니다.")

    next_action_priority = {
        "running": 0,
        "blocked": 1,
        "failed": 1,
        "uncommitted": 1,
        "queued": 2,
    }
    actionable_todos: list[tuple[int, int, int, dict[str, Any]]] = []
    for index, todo in enumerate(todos):
        status = str(todo.get("status") or "").strip().lower()
        if status not in next_action_priority:
            continue
        priority_rank = todo.get("priority_rank")
        try:
            normalized_rank = int(priority_rank)
        except (TypeError, ValueError):
            normalized_rank = 999999
        actionable_todos.append((next_action_priority[status], normalized_rank, index, todo))
    actionable_todos.sort(key=lambda item: (item[0], item[1], item[2]))

    lines = [
        "# Sprint Report",
        "",
        "## 한눈에 보기",
        "",
        f"- TL;DR: {' '.join(headline_parts)}",
        f"- sprint_id: {sprint_state.get('sprint_id') or 'N/A'}",
        f"- milestone: {milestone}",
        f"- 상태: {status_label}",
        f"- phase: {sprint_state.get('phase') or 'N/A'}",
        (
            "- todo 요약: "
            + format_count_summary(
                todo_status_counts,
                ["running", "queued", "uncommitted", "committed", "completed", "blocked", "failed"],
            )
        ),
        "",
        "## 다음 액션",
        "",
    ]
    if actionable_todos:
        for _, _, _, todo in actionable_todos:
            lines.append(
                "- [{status}] {title} | request_id={request_id}".format(
                    status=str(todo.get("status") or ""),
                    title=str(todo.get("title") or ""),
                    request_id=str(todo.get("request_id") or "N/A"),
                )
            )
    elif todos:
        lines.append("- 현재 남은 후속 액션 없음")
    else:
        lines.append("- 다음 액션 후보 없음")
    lines.extend(
        [
            "",
            "## Todo Summary",
            "",
        ]
    )
    if todos:
        for todo in todos:
            lines.append(
                "- [{status}] {title} | request_id={request_id} | artifacts={artifacts}".format(
                    status=str(todo.get("status") or ""),
                    title=str(todo.get("title") or ""),
                    request_id=str(todo.get("request_id") or "N/A"),
                    artifacts=", ".join(
                        str(item).strip() for item in (todo.get("artifacts") or []) if str(item).strip()
                    )
                    or "N/A",
                )
            )
    else:
        lines.append("- todo 없음")
    lines.extend(
        [
            "",
            (
                "- todo_summary: "
                + format_count_summary(
                    todo_status_counts,
                    ["running", "queued", "uncommitted", "committed", "completed", "blocked", "failed"],
                )
            ),
            "",
            "## Linked Todo Artifacts",
            "",
        ]
    )
    if linked_artifacts:
        for entry in linked_artifacts:
            lines.append(
                "- [{status}] {title} | request_id={request_id} | artifact={path}".format(
                    status=entry["status"],
                    title=entry["title"],
                    request_id=entry["request_id"],
                    path=entry["path"],
                )
            )
    else:
        lines.append("- linked sprint artifact 없음")
    return "\n".join(lines).rstrip()


def build_sprint_progress_log_summary(
    sprint_state: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    build_headline: Callable[[dict[str, Any], dict[str, Any], bool], str],
    build_issue_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_achievement_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
) -> str:
    lines = [
        build_headline(sprint_state, snapshot, False),
        f"todo={snapshot.get('todo_summary') or 'N/A'}",
        f"commit={int(snapshot.get('commit_count') or 0)}, artifact={len(snapshot.get('linked_artifacts') or [])}",
    ]
    issue_lines = build_issue_lines(sprint_state, snapshot, False)
    achievement_lines = build_achievement_lines(sprint_state, snapshot, False)
    if issue_lines and issue_lines[0] != "- 핵심 이슈 없음":
        lines.append(issue_lines[0].removeprefix("- ").strip())
    elif achievement_lines and achievement_lines[0] != "- 주요 성과 없음":
        lines.append(achievement_lines[0].removeprefix("- ").strip())
    return "\n".join(lines)


def render_sprint_report_body(
    sprint_state: dict[str, Any],
    snapshot: dict[str, Any],
    closeout_result: dict[str, Any],
    *,
    build_overview_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_change_summary_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_timeline_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_agent_contribution_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_issue_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_achievement_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_artifact_lines: Callable[[dict[str, Any], dict[str, Any], bool], list[str]],
    build_machine_report_lines: Callable[[dict[str, Any], dict[str, Any]], list[str]],
) -> str:
    lines = [
        "# Sprint Report",
        "",
        "## 한눈에 보기",
        "",
        *build_overview_lines(sprint_state, snapshot, True),
        "",
        "## 변경 요약",
        "",
        *build_change_summary_lines(sprint_state, snapshot, True),
        "",
        "## Sprint A to Z",
        "",
        *build_timeline_lines(sprint_state, snapshot, True),
        "",
        "## 에이전트 기여",
        "",
        *build_agent_contribution_lines(sprint_state, snapshot, True),
        "",
        "## 핵심 이슈",
        "",
        *build_issue_lines(sprint_state, snapshot, True),
        "",
        "## 성과",
        "",
        *build_achievement_lines(sprint_state, snapshot, True),
        "",
        "## 참고 아티팩트",
        "",
        *build_artifact_lines(sprint_state, snapshot, True),
        "",
        "## 머신 요약",
        "",
        *build_machine_report_lines(sprint_state, closeout_result),
    ]
    return "\n".join(lines).strip()


def render_backlog_status_report(
    *,
    active_items: list[dict[str, Any]],
    counts: dict[str, int],
    kind_counts: dict[str, int],
    source_counts: dict[str, int],
    format_count_summary: Callable[[dict[str, int], list[str] | tuple[str, ...]], str],
) -> str:
    lines = [
        "## Backlog Summary",
        (
            f"- counts: pending={counts['pending']}, selected={counts['selected']}, "
            f"blocked={counts['blocked']}, total={counts['total']}"
        ),
        f"- kind_summary: {format_count_summary(kind_counts, ['bug', 'feature', 'enhancement', 'chore'])}",
        f"- source_summary: {format_count_summary(source_counts, ['user', 'sourcer', 'carry_over'])}",
        "",
        "## Priority Backlog",
    ]
    if active_items:
        for item in active_items[:12]:
            lines.append(
                "- [{status}] {title} | backlog_id={backlog_id} | kind={kind} | source={source}".format(
                    status=str(item.get("status") or "").strip() or "unknown",
                    backlog_id=str(item.get("backlog_id") or "").strip() or "N/A",
                    title=str(item.get("title") or item.get("scope") or "").strip() or "Untitled",
                    kind=str(item.get("kind") or "").strip() or "N/A",
                    source=str(item.get("source") or "").strip() or "N/A",
                )
            )
    else:
        lines.append("- active backlog 없음")
    return "\n".join(lines)


def render_sprint_status_report(
    sprint_state: dict[str, Any],
    *,
    is_active: bool,
    scheduler_state: dict[str, Any],
    todo_status_counts: dict[str, int],
    selected_kind_counts: dict[str, int],
    format_count_summary: Callable[[dict[str, int], list[str] | tuple[str, ...]], str],
) -> str:
    selected_items = list(sprint_state.get("selected_items") or [])
    todos = list(sprint_state.get("todos") or [])
    lines = [
        "## Sprint Summary",
        f"- view: {'active' if is_active else 'latest'}",
        f"- sprint_id: {sprint_state.get('sprint_id') or ''}",
        f"- sprint_name: {sprint_state.get('sprint_name') or sprint_state.get('sprint_display_name') or 'N/A'}",
        f"- phase: {sprint_state.get('phase') or 'N/A'}",
        f"- milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
        f"- status: {sprint_state.get('status') or ''}",
        f"- trigger: {sprint_state.get('trigger') or ''}",
        f"- started_at: {sprint_state.get('started_at') or ''}",
        f"- ended_at: {sprint_state.get('ended_at') or 'N/A'}",
        f"- closeout_status: {sprint_state.get('closeout_status') or 'N/A'}",
        f"- commit_count: {sprint_state.get('commit_count') or 0}",
        f"- commit_sha: {sprint_state.get('commit_sha') or 'N/A'}",
        f"- next_slot_at: {scheduler_state.get('next_slot_at') or 'N/A'}",
        (
            "- todo_summary: "
            + format_count_summary(
                todo_status_counts,
                ["running", "queued", "uncommitted", "committed", "completed", "blocked", "failed"],
            )
        ),
        (
            "- backlog_kind_summary: "
            + format_count_summary(selected_kind_counts, ["bug", "feature", "enhancement", "chore"])
        ),
        "",
        "## Sprint Tasks",
    ]
    if todos:
        for todo in todos[:12]:
            lines.append(
                "- [{status}] {title} | todo_id={todo_id} | backlog_id={backlog_id} | request_id={request_id}".format(
                    status=str(todo.get("status") or "").strip() or "unknown",
                    title=str(todo.get("title") or "").strip() or "Untitled",
                    todo_id=str(todo.get("todo_id") or "").strip() or "N/A",
                    backlog_id=str(todo.get("backlog_id") or "").strip() or "N/A",
                    request_id=str(todo.get("request_id") or "").strip() or "N/A",
                )
            )
    elif selected_items:
        for item in selected_items[:12]:
            lines.append(
                "- [selected] {title} | backlog_id={backlog_id} | kind={kind}".format(
                    title=str(item.get("title") or item.get("scope") or "").strip() or "Untitled",
                    backlog_id=str(item.get("backlog_id") or "").strip() or "N/A",
                    kind=str(item.get("kind") or "").strip() or "N/A",
                )
            )
    else:
        lines.append("- sprint task 없음")
    return "\n".join(lines)


def parse_sprint_report_fields(report_body: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for raw_line in str(report_body or "").splitlines():
        line = str(raw_line or "").strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = str(key or "").strip()
        if normalized_key:
            fields[normalized_key] = str(value or "").strip()
    return fields


def parse_sprint_report_list_field(value: str) -> list[str]:
    normalized = str(value or "").strip()
    if not normalized or normalized.upper() == "N/A":
        return []
    return [item.strip() for item in normalized.split(",") if item.strip()]


def parse_sprint_report_int_field(value: str) -> int:
    try:
        return int(str(value or "").strip())
    except (TypeError, ValueError):
        return 0


def build_derived_closeout_result_from_sprint_state(sprint_state: dict[str, Any]) -> dict[str, Any]:
    report_fields = parse_sprint_report_fields(str(sprint_state.get("report_body") or ""))
    commit_shas = [
        str(item).strip()
        for item in (sprint_state.get("commit_shas") or parse_sprint_report_list_field(report_fields.get("commit_shas") or ""))
        if str(item).strip()
    ]
    return {
        "status": str(sprint_state.get("closeout_status") or report_fields.get("closeout_status") or "").strip(),
        "commit_count": int(
            sprint_state.get("commit_count") or parse_sprint_report_int_field(report_fields.get("commit_count") or "")
        ),
        "commit_shas": commit_shas,
        "representative_commit_sha": str(sprint_state.get("commit_sha") or report_fields.get("commit_sha") or "").strip(),
        "sprint_tagged_commit_count": parse_sprint_report_int_field(
            report_fields.get("sprint_tagged_commit_count") or ""
        ),
        "sprint_tagged_commit_shas": parse_sprint_report_list_field(
            report_fields.get("sprint_tagged_commit_shas") or ""
        ),
        "uncommitted_paths": [
            str(item).strip()
            for item in (
                sprint_state.get("uncommitted_paths")
                or parse_sprint_report_list_field(report_fields.get("uncommitted_paths") or "")
            )
            if str(item).strip()
        ],
        "message": str(report_fields.get("closeout_message") or "").strip(),
    }


def refresh_sprint_report_body(
    sprint_state: dict[str, Any],
    *,
    build_report_body: Callable[[dict[str, Any], dict[str, Any]], str],
) -> bool:
    report_body = str(sprint_state.get("report_body") or "").strip()
    if not report_body:
        return False
    report_fields = parse_sprint_report_fields(report_body)
    if not report_fields.get("sprint_id"):
        return False
    refreshed = build_report_body(
        sprint_state,
        build_derived_closeout_result_from_sprint_state(sprint_state),
    )
    if refreshed == report_body:
        return False
    sprint_state["report_body"] = refreshed
    return True


def build_machine_sprint_report_lines(
    sprint_state: dict[str, Any],
    closeout_result: dict[str, Any],
    *,
    todo_status_counts: dict[str, int],
    linked_artifacts: list[dict[str, str]],
    format_count_summary: Callable[[dict[str, int], list[str] | tuple[str, ...]], str],
) -> list[str]:
    commit_shas = [
        str(item).strip()
        for item in (closeout_result.get("commit_shas") or sprint_state.get("commit_shas") or [])
        if str(item).strip()
    ]
    sprint_tagged_commit_shas = [
        str(item).strip()
        for item in (closeout_result.get("sprint_tagged_commit_shas") or [])
        if str(item).strip()
    ]
    uncommitted_paths = [
        str(item).strip()
        for item in (closeout_result.get("uncommitted_paths") or sprint_state.get("uncommitted_paths") or [])
        if str(item).strip()
    ]
    lines = [
        f"sprint_id={sprint_state.get('sprint_id') or ''}",
        f"sprint_name={sprint_state.get('sprint_name') or sprint_state.get('sprint_display_name') or 'N/A'}",
        f"phase={sprint_state.get('phase') or 'N/A'}",
        f"milestone_title={sprint_state.get('milestone_title') or 'N/A'}",
        f"sprint_folder={sprint_state.get('sprint_folder') or 'N/A'}",
        f"status={sprint_state.get('status') or ''}",
        f"trigger={sprint_state.get('trigger') or ''}",
        f"closeout_status={closeout_result.get('status') or sprint_state.get('closeout_status') or ''}",
        f"version_control_status={sprint_state.get('version_control_status') or 'N/A'}",
        f"version_control_sha={sprint_state.get('version_control_sha') or 'N/A'}",
        f"auto_commit_status={sprint_state.get('auto_commit_status') or 'N/A'}",
        f"auto_commit_sha={sprint_state.get('auto_commit_sha') or 'N/A'}",
        f"commit_count={closeout_result.get('commit_count') or sprint_state.get('commit_count') or 0}",
        f"sprint_tagged_commit_count={closeout_result.get('sprint_tagged_commit_count') or 0}",
        f"commit_sha={closeout_result.get('representative_commit_sha') or sprint_state.get('commit_sha') or 'N/A'}",
        f"commit_shas={', '.join(commit_shas) if commit_shas else 'N/A'}",
        f"sprint_tagged_commit_shas={', '.join(sprint_tagged_commit_shas) if sprint_tagged_commit_shas else 'N/A'}",
        f"uncommitted_paths={', '.join(uncommitted_paths) if uncommitted_paths else 'N/A'}",
        (
            "todo_status_counts="
            + format_count_summary(
                todo_status_counts,
                ["running", "queued", "uncommitted", "committed", "completed", "blocked", "failed"],
            )
        ),
        (
            "version_control_paths="
            + (
                ", ".join(
                    str(item).strip()
                    for item in (sprint_state.get("version_control_paths") or [])
                    if str(item).strip()
                )
                or "N/A"
            )
        ),
        (
            "auto_commit_paths="
            + (
                ", ".join(
                    str(item).strip()
                    for item in (sprint_state.get("auto_commit_paths") or [])
                    if str(item).strip()
                )
                or "N/A"
            )
        ),
        "",
        "todo_summary:",
    ]
    for todo in sprint_state.get("todos") or []:
        lines.append(
            "- [{status}] {title} | request_id={request_id} | carry_over={carry}".format(
                status=str(todo.get("status") or ""),
                title=str(todo.get("title") or ""),
                request_id=str(todo.get("request_id") or "N/A"),
                carry=str(todo.get("carry_over_backlog_id") or "N/A"),
            )
        )
    if linked_artifacts:
        lines.extend(["", "linked_artifacts:"])
        for entry in linked_artifacts:
            lines.append(
                "- [{status}] {title} | request_id={request_id} | artifact={path}".format(
                    status=entry["status"],
                    title=entry["title"],
                    request_id=entry["request_id"],
                    path=entry["path"],
                )
            )
    if closeout_result.get("message"):
        lines.extend(["", f"closeout_message={closeout_result.get('message') or ''}"])
    if sprint_state.get("version_control_message"):
        lines.append(f"version_control_message={sprint_state.get('version_control_message') or ''}")
    if sprint_state.get("auto_commit_message"):
        lines.append(f"auto_commit_message={sprint_state.get('auto_commit_message') or ''}")
    return lines


async def send_sprint_report_for_service(
    service: Any,
    *,
    title: str,
    body: str,
    sprint_id: str = "",
    status: str = "완료",
    end_reason: str = "없음",
    judgment: str = "",
    next_action: str = "대기",
    commit_message: str = "",
    related_artifacts: list[str] | None = None,
    log_summary: str = "",
    sections: list[ReportSection] | None = None,
    swallow_exceptions: bool = True,
) -> None:
    rendered_title = decorate_sprint_report_title(title)
    active_sprint_id = str(sprint_id or service._load_scheduler_state().get("active_sprint_id") or "").strip()
    report_artifacts = collect_sprint_report_artifacts(
        service.paths,
        active_sprint_id=active_sprint_id,
        related_artifacts=related_artifacts,
    )
    report = build_sprint_progress_report(
        rendered_title=rendered_title,
        sprint_scope=service._format_sprint_scope(sprint_id=sprint_id),
        body=body,
        report_artifacts=report_artifacts,
        status=status,
        end_reason=end_reason,
        judgment=judgment or rendered_title,
        next_action=next_action,
        commit_message=commit_message,
        log_summary=log_summary,
        sections=sections,
    )
    await service.notification_service.send_sprint_report(
        startup_channel_id=str(service.discord_config.startup_channel_id or ""),
        rendered_title=rendered_title,
        report=report,
        swallow_exceptions=swallow_exceptions,
    )


async def send_sprint_kickoff_for_service(service: Any, sprint_state: dict[str, Any]) -> None:
    context = build_sprint_kickoff_report_context(sprint_state)
    await service._send_sprint_report(
        title=str(context["title"]),
        body=str(context["body"]),
        sections=list(context["sections"]),
    )


async def send_sprint_todo_list_for_service(service: Any, sprint_state: dict[str, Any]) -> None:
    context = build_sprint_todo_list_report_context(sprint_state)
    await service._send_sprint_report(
        title=str(context["title"]),
        body=str(context["body"]),
        sections=list(context["sections"]),
    )


async def send_sprint_completion_user_report_for_service(
    service: Any,
    *,
    title: str,
    sprint_state: dict[str, Any],
    closeout_result: dict[str, Any],
) -> bool:
    return await service.notification_service.send_sprint_completion_user_report(
        report_channel_id=str(service.discord_config.report_channel_id or ""),
        sprint_id=str(sprint_state.get("sprint_id") or ""),
        content=service._render_sprint_completion_user_report(
            sprint_state,
            closeout_result,
            title=title,
        ),
    )


async def send_terminal_sprint_reports_for_service(
    service: Any,
    *,
    title: str,
    sprint_state: dict[str, Any],
    closeout_result: dict[str, Any],
    judgment: str = "",
    commit_message: str = "",
    related_artifacts: list[str] | None = None,
) -> None:
    terminal_report = build_terminal_sprint_report_context(
        sprint_state=sprint_state,
        closeout_result=closeout_result,
        title=title,
    )
    resolved_title = str(terminal_report.get("title") or title).strip()
    report_body = str(sprint_state.get("report_body") or "").strip()
    user_report_sent = await service._send_sprint_completion_user_report(
        title=resolved_title,
        sprint_state=sprint_state,
        closeout_result=closeout_result,
    )
    await service._send_sprint_report(
        title=resolved_title,
        body=report_body,
        sprint_id=str(sprint_state.get("sprint_id") or ""),
        judgment=judgment or str(terminal_report.get("judgment") or "").strip(),
        commit_message=commit_message or str(terminal_report.get("commit_message") or "").strip(),
        related_artifacts=related_artifacts if related_artifacts is not None else list(terminal_report.get("related_artifacts") or []),
        log_summary=service._build_sprint_progress_log_summary(sprint_state, closeout_result),
        sections=[] if user_report_sent else service._build_terminal_sprint_report_sections(sprint_state, closeout_result),
    )
