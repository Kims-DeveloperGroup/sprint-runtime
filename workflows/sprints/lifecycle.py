"""Sprint lifecycle helpers plus compatibility re-exports."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from teams_runtime.shared.persistence import (
    RUNTIME_TIMEZONE,
    build_request_fingerprint,
    new_request_id,
    new_todo_id,
    normalize_runtime_datetime,
    runtime_now,
    utc_now_iso,
)
from teams_runtime.shared.formatting import (
    build_backlog_item,
    priority_rank_sort_key,
    priority_rank_sort_value,
)
from teams_runtime.workflows.repository_ops import capture_git_baseline, inspect_sprint_closeout
from teams_runtime.workflows.state.request_store import append_request_event
from teams_runtime.workflows.state.backlog_store import (
    build_sprint_selected_backlog_item,
    save_backlog_item,
)
from teams_runtime.workflows.state.sprint_store import (
    load_sprint_state,
    save_sprint_state,
)
from teams_runtime.workflows.roles.research import RESEARCH_REPORT_LIST_FIELDS


LOGGER = logging.getLogger("teams_runtime.workflows.sprints.lifecycle")

INITIAL_PHASE_STEP_MILESTONE_REFINEMENT = "milestone_refinement"
INITIAL_PHASE_STEP_ARTIFACT_SYNC = "artifact_sync"
INITIAL_PHASE_STEP_BACKLOG_DEFINITION = "backlog_definition"
INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION = "backlog_prioritization"
INITIAL_PHASE_STEP_TODO_FINALIZATION = "todo_finalization"
INITIAL_PHASE_STEPS = (
    INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
    INITIAL_PHASE_STEP_ARTIFACT_SYNC,
    INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
    INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
    INITIAL_PHASE_STEP_TODO_FINALIZATION,
)
INITIAL_PHASE_STEP_TITLES = {
    INITIAL_PHASE_STEP_MILESTONE_REFINEMENT: "milestone 정리",
    INITIAL_PHASE_STEP_ARTIFACT_SYNC: "plan/spec 동기화",
    INITIAL_PHASE_STEP_BACKLOG_DEFINITION: "backlog 정의",
    INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION: "backlog 우선순위화",
    INITIAL_PHASE_STEP_TODO_FINALIZATION: "실행 todo 확정",
}
SPRINT_ACTIVE_BACKLOG_STATUSES = {"pending", "selected", "blocked"}


def _string_list(values: Any) -> list[str]:
    return [str(item).strip() for item in (values or []) if str(item).strip()]


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


def extract_sprint_folder_name(sprint_state: dict[str, Any] | None) -> str:
    state = dict(sprint_state or {})
    folder_name = str(state.get("sprint_folder_name") or "").strip()
    if folder_name:
        return folder_name
    sprint_folder = str(state.get("sprint_folder") or "").strip()
    if sprint_folder:
        candidate = Path(sprint_folder).name
        if candidate:
            return candidate
    sprint_id = str(state.get("sprint_id") or "").strip()
    if sprint_id:
        return build_sprint_artifact_folder_name(sprint_id)
    return ""


def attachment_storage_relative_path(path: Path) -> Path:
    return Path(path.name)


def sprint_attachment_filename(
    artifact_hint: str,
    *,
    resolved: Path | None = None,
    sprint_artifacts_root: Path | None = None,
) -> str:
    candidate = resolved
    if candidate is not None and sprint_artifacts_root is not None:
        try:
            relative = candidate.resolve().relative_to(sprint_artifacts_root.resolve())
        except ValueError:
            relative = None
        if relative is not None and len(relative.parts) >= 3 and relative.parts[1] == "attachments":
            return candidate.name

    normalized = str(artifact_hint or "").strip()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    hint_path = Path(normalized)
    parts = hint_path.parts
    if len(parts) >= 4 and parts[0] == "shared_workspace" and parts[1] == "sprints" and parts[3] == "attachments":
        return hint_path.name
    return ""


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


def todo_status_rank(status: str) -> int:
    normalized = str(status or "").strip().lower()
    return {
        "queued": 0,
        "running": 1,
        "blocked": 2,
        "uncommitted": 2,
        "failed": 2,
        "completed": 3,
        "committed": 4,
    }.get(normalized, -1)


def sort_sprint_todos(todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        todos,
        key=lambda todo: (
            0 if str(todo.get("dependency_gate_bypass") or "").strip() == "restart_checkpoint" else 1,
            0 if str(todo.get("status") or "").strip().lower() == "running" else 1,
            priority_rank_sort_value(todo.get("priority_rank")),
            str(todo.get("created_at") or todo.get("started_at") or ""),
            str(todo.get("todo_id") or ""),
        ),
    )


def successful_todo_status(status: str) -> bool:
    return str(status or "").strip().lower() in {"completed", "committed"}


def sprint_todo_dependency_waiting_on(
    todo: dict[str, Any],
    todos: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    current_rank = priority_rank_sort_value(todo.get("priority_rank"))
    if current_rank <= 1 or current_rank >= 10**9:
        return []
    waiting: list[dict[str, Any]] = []
    current_todo_id = str(todo.get("todo_id") or "").strip()
    for candidate in todos:
        candidate_todo_id = str(candidate.get("todo_id") or "").strip()
        if current_todo_id and candidate_todo_id == current_todo_id:
            continue
        candidate_rank = priority_rank_sort_value(candidate.get("priority_rank"))
        if candidate_rank >= current_rank:
            continue
        if successful_todo_status(str(candidate.get("status") or "")):
            continue
        waiting.append(candidate)
    return sorted(waiting, key=priority_rank_sort_key)


def sprint_todo_dependencies_satisfied(
    todo: dict[str, Any],
    todos: list[dict[str, Any]],
) -> bool:
    return not sprint_todo_dependency_waiting_on(todo, todos)


def todo_status_from_request_record(request_record: dict[str, Any]) -> str:
    result = dict(request_record.get("result") or {}) if isinstance(request_record.get("result"), dict) else {}
    candidates = [
        str(result.get("status") or "").strip().lower(),
        str(request_record.get("status") or "").strip().lower(),
    ]
    valid_statuses = {"queued", "running", "uncommitted", "committed", "completed", "blocked", "failed"}
    for candidate in candidates:
        if candidate in valid_statuses:
            return candidate
    if "delegated" in candidates:
        return "running"
    return "queued"


def _parse_datetime_value(value: str) -> datetime | None:
    normalized = str(value or "").strip()
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _collect_string_candidates(*sequences: Any) -> list[str]:
    values: list[str] = []
    for sequence in sequences:
        if not sequence:
            continue
        for raw_value in sequence:
            normalized = str(raw_value or "").strip()
            if normalized:
                values.append(normalized)
    return _dedupe_preserving_order(values)


def merge_recovered_sprint_todo(existing: dict[str, Any], recovered: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for field in ("request_id", "title", "milestone_title", "summary", "created_at"):
        if not str(merged.get(field) or "").strip() and str(recovered.get(field) or "").strip():
            merged[field] = recovered[field]
    if not int(merged.get("priority_rank") or 0) and int(recovered.get("priority_rank") or 0):
        merged["priority_rank"] = int(recovered.get("priority_rank") or 0)
    if not list(merged.get("acceptance_criteria") or []) and list(recovered.get("acceptance_criteria") or []):
        merged["acceptance_criteria"] = list(recovered.get("acceptance_criteria") or [])
    existing_updated_at = _parse_datetime_value(
        str(merged.get("updated_at") or merged.get("ended_at") or merged.get("created_at") or "")
    )
    recovered_updated_at = _parse_datetime_value(
        str(recovered.get("updated_at") or recovered.get("ended_at") or recovered.get("created_at") or "")
    )
    recovered_is_newer = (
        recovered_updated_at is not None
        and (existing_updated_at is None or recovered_updated_at >= existing_updated_at)
    )
    if recovered_is_newer:
        merged["status"] = recovered.get("status") or merged.get("status") or ""
        if str(recovered.get("summary") or "").strip():
            merged["summary"] = recovered["summary"]
        if str(recovered.get("started_at") or "").strip():
            merged["started_at"] = recovered["started_at"]
        if str(recovered.get("ended_at") or "").strip():
            merged["ended_at"] = recovered["ended_at"]
        if str(recovered.get("updated_at") or "").strip():
            merged["updated_at"] = recovered["updated_at"]
        merged["artifacts"] = list(recovered.get("artifacts") or [])
        for field in ("version_control_status", "version_control_message", "version_control_error"):
            if str(recovered.get(field) or "").strip():
                merged[field] = recovered[field]
        if list(recovered.get("version_control_paths") or []):
            merged["version_control_paths"] = list(recovered.get("version_control_paths") or [])
    else:
        if todo_status_rank(recovered.get("status") or "") > todo_status_rank(merged.get("status") or ""):
            merged["status"] = recovered.get("status") or merged.get("status") or ""
            if str(recovered.get("summary") or "").strip():
                merged["summary"] = recovered["summary"]
            if str(recovered.get("started_at") or "").strip():
                merged["started_at"] = recovered["started_at"]
            if str(recovered.get("ended_at") or "").strip():
                merged["ended_at"] = recovered["ended_at"]
        merged["artifacts"] = _collect_string_candidates(
            merged.get("artifacts"),
            recovered.get("artifacts"),
        )
        for field in ("version_control_status", "version_control_message", "version_control_error"):
            if not str(merged.get(field) or "").strip() and str(recovered.get(field) or "").strip():
                merged[field] = recovered[field]
        merged["version_control_paths"] = _collect_string_candidates(
            merged.get("version_control_paths"),
            recovered.get("version_control_paths"),
        )
    return merged


def build_recovered_sprint_todo_from_request(
    sprint_state: dict[str, Any],
    request_record: dict[str, Any],
    *,
    backlog_item: dict[str, Any] | None = None,
    artifacts: list[str] | None = None,
) -> dict[str, Any]:
    params = dict(request_record.get("params") or {})
    result = dict(request_record.get("result") or {}) if isinstance(request_record.get("result"), dict) else {}
    request_id = str(request_record.get("request_id") or "").strip()
    backlog_id = str(request_record.get("backlog_id") or params.get("backlog_id") or "").strip()
    todo_id = str(request_record.get("todo_id") or params.get("todo_id") or "").strip() or (
        f"recovered-{request_id}" if request_id else ""
    )
    if not backlog_id and not todo_id:
        return {}

    source_backlog = dict(backlog_item or {})
    title = (
        str(source_backlog.get("title") or "").strip()
        or str(request_record.get("scope") or "").strip()
        or str(request_record.get("body") or "").strip()
    )
    summary = (
        str(result.get("summary") or "").strip()
        or str(request_record.get("task_commit_summary") or "").strip()
        or str(request_record.get("body") or "").strip()
    )
    owner_role = (
        str(request_record.get("next_role") or "").strip()
        or str(request_record.get("current_role") or "").strip()
        or "planner"
    )
    todo_source = source_backlog or {
        "backlog_id": backlog_id,
        "title": title,
        "milestone_title": str(sprint_state.get("milestone_title") or "").strip(),
        "priority_rank": 0,
        "acceptance_criteria": [],
    }
    todo = build_todo_item(todo_source, owner_role=owner_role)
    todo["todo_id"] = todo_id or todo["todo_id"]
    todo["backlog_id"] = backlog_id
    todo["title"] = title
    todo["milestone_title"] = (
        str(source_backlog.get("milestone_title") or "").strip()
        or str(sprint_state.get("milestone_title") or "").strip()
    )
    todo["priority_rank"] = int(source_backlog.get("priority_rank") or 0)
    todo["acceptance_criteria"] = [
        str(item).strip()
        for item in (source_backlog.get("acceptance_criteria") or [])
        if str(item).strip()
    ]
    todo["request_id"] = request_id
    todo["status"] = todo_status_from_request_record(request_record)
    todo["artifacts"] = list(artifacts or [])
    todo["summary"] = summary
    todo["version_control_status"] = str(request_record.get("version_control_status") or "").strip()
    todo["version_control_paths"] = [
        str(item).strip()
        for item in (request_record.get("version_control_paths") or [])
        if str(item).strip()
    ]
    todo["version_control_message"] = str(request_record.get("version_control_message") or "").strip()
    todo["version_control_error"] = str(request_record.get("version_control_error") or "").strip()
    created_at = str(request_record.get("created_at") or "").strip()
    updated_at = str(request_record.get("updated_at") or created_at).strip()
    if created_at:
        todo["created_at"] = created_at
    if updated_at:
        todo["updated_at"] = updated_at
    if todo["status"] in {"running", "completed", "committed", "blocked", "failed", "uncommitted"}:
        todo["started_at"] = created_at
    if todo["status"] in {"completed", "committed", "blocked", "failed", "uncommitted"}:
        todo["ended_at"] = updated_at
    return todo


def _build_todo_identity_indexes(
    todos: list[dict[str, Any]],
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    todo_indexes: dict[str, int] = {}
    request_indexes: dict[str, int] = {}
    backlog_indexes: dict[str, int] = {}
    for index, todo in enumerate(todos):
        todo_id = str(todo.get("todo_id") or "").strip()
        request_id = str(todo.get("request_id") or "").strip()
        backlog_id = str(todo.get("backlog_id") or "").strip()
        if todo_id and todo_id not in todo_indexes:
            todo_indexes[todo_id] = index
        if request_id and request_id not in request_indexes:
            request_indexes[request_id] = index
        if backlog_id and backlog_id not in backlog_indexes:
            backlog_indexes[backlog_id] = index
    return todo_indexes, request_indexes, backlog_indexes


def recover_sprint_todos_from_recovered(
    sprint_state: dict[str, Any],
    recovered_todos: list[dict[str, Any]],
) -> bool:
    todos = [todo for todo in (sprint_state.get("todos") or []) if isinstance(todo, dict)]
    if not todos and not recovered_todos:
        return False

    changed = False
    todo_indexes, request_indexes, backlog_indexes = _build_todo_identity_indexes(todos)
    for recovered in recovered_todos:
        if not recovered:
            continue
        retired_request_ids = {
            str(todo.get("retry_of_request_id") or "").strip()
            for todo in todos
            if str(todo.get("retry_of_request_id") or "").strip()
        }
        todo_id = str(recovered.get("todo_id") or "").strip()
        request_id = str(recovered.get("request_id") or "").strip()
        backlog_id = str(recovered.get("backlog_id") or "").strip()
        if request_id and request_id in retired_request_ids:
            continue
        existing_index = -1
        if todo_id and todo_id in todo_indexes:
            existing_index = todo_indexes[todo_id]
        elif request_id and request_id in request_indexes:
            existing_index = request_indexes[request_id]
        elif backlog_id and backlog_id in backlog_indexes:
            existing_index = backlog_indexes[backlog_id]
        if existing_index >= 0:
            merged = merge_recovered_sprint_todo(todos[existing_index], recovered)
            if merged != todos[existing_index]:
                todos[existing_index].clear()
                todos[existing_index].update(merged)
                changed = True
            continue
        todos.append(recovered)
        changed = True
        todo_indexes, request_indexes, backlog_indexes = _build_todo_identity_indexes(todos)
    if not changed:
        return False
    sprint_state["todos"] = sort_sprint_todos(todos)
    return True


def _dedupe_preserving_order(values) -> list:
    seen: set[str] = set()
    result: list = []
    for value in values:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [line.strip("- ").strip() for line in value.splitlines()]
    elif isinstance(value, (list, tuple, set)):
        values = [str(item).strip() for item in value]
    else:
        values = []
    return [item for item in values if item]


def _clean_sprint_text(value: Any) -> str:
    return "\n".join(line.rstrip() for line in str(value or "").splitlines()).strip()


def _normalize_sprint_requirements(value: Any) -> list[str]:
    return _dedupe_preserving_order(_normalize_string_list(value))


def normalize_trace_list(values: Any) -> list[str]:
    if isinstance(values, list):
        return [str(item).strip() for item in values if str(item).strip()]
    if isinstance(values, str):
        normalized = str(values).strip()
        return [normalized] if normalized else []
    return []


def collect_sprint_relevant_backlog_items(
    sprint_state: dict[str, Any],
    backlog_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    milestone_title = str(sprint_state.get("milestone_title") or "").strip()
    relevant_items: list[dict[str, Any]] = []
    for item in backlog_items:
        status = str(item.get("status") or "").strip().lower()
        if status not in SPRINT_ACTIVE_BACKLOG_STATUSES:
            continue
        origin = dict(item.get("origin") or {})
        origin_sprint_id = str(origin.get("sprint_id") or "").strip()
        item_milestone = str(item.get("milestone_title") or "").strip()
        planned_in_sprint_id = str(item.get("planned_in_sprint_id") or "").strip()
        selected_in_sprint_id = str(item.get("selected_in_sprint_id") or "").strip()
        if sprint_id and sprint_id in {planned_in_sprint_id, selected_in_sprint_id}:
            relevant_items.append(item)
            continue
        if sprint_id and origin_sprint_id == sprint_id:
            relevant_items.append(item)
            continue
        if milestone_title and item_milestone == milestone_title:
            relevant_items.append(item)
    relevant_items.sort(
        key=lambda item: (
            priority_rank_sort_value(item.get("priority_rank")),
            str(item.get("created_at") or ""),
            str(item.get("backlog_id") or ""),
        )
    )
    return relevant_items


def is_sprint_planning_request(request_record: dict[str, Any]) -> bool:
    params = dict(request_record.get("params") or {})
    return (
        str(params.get("_teams_kind") or "").strip() == "sprint_internal"
        and str(request_record.get("intent") or "").strip().lower() == "plan"
        and bool(str(params.get("sprint_phase") or "").strip())
    )


def initial_phase_step(request_record: dict[str, Any]) -> str:
    params = dict(request_record.get("params") or {})
    step = str(params.get("initial_phase_step") or "").strip().lower()
    return step if step in INITIAL_PHASE_STEPS else ""


def is_initial_phase_planner_request(request_record: dict[str, Any]) -> bool:
    if not is_sprint_planning_request(request_record):
        return False
    params = dict(request_record.get("params") or {})
    if str(params.get("sprint_phase") or "").strip().lower() != "initial":
        return False
    return bool(initial_phase_step(request_record))


def sprint_research_prepass_completed(sprint_state: dict[str, Any] | None) -> bool:
    prepass = dict((sprint_state or {}).get("research_prepass") or {})
    return str(prepass.get("status") or "").strip().lower() == "completed"


def should_start_sprint_research_prepass(
    sprint_state: dict[str, Any],
    *,
    phase: str,
    iteration: int,
    step: str,
) -> bool:
    normalized_phase = str(phase or "").strip().lower()
    normalized_step = str(step or "").strip().lower()
    if normalized_phase != "initial":
        return False
    if int(iteration or 0) != 1:
        return False
    if normalized_step != INITIAL_PHASE_STEP_MILESTONE_REFINEMENT:
        return False
    return not sprint_research_prepass_completed(sprint_state)


def sprint_research_prepass_artifacts(sprint_state: dict[str, Any] | None) -> list[str]:
    prepass = dict((sprint_state or {}).get("research_prepass") or {})
    return _string_list(prepass.get("artifacts"))


def sprint_research_prepass_source_backed(sprint_state: dict[str, Any] | None) -> bool:
    prepass = dict((sprint_state or {}).get("research_prepass") or {})
    if str(prepass.get("status") or "").strip().lower() != "completed":
        return False
    return any(
        isinstance(item, dict)
        and str(item.get("title") or "").strip()
        and str(item.get("url") or "").strip().startswith(("http://", "https://"))
        for item in (prepass.get("backing_sources") or [])
    )


def sprint_research_prepass_reference_lines(sprint_state: dict[str, Any] | None) -> list[str]:
    prepass = dict((sprint_state or {}).get("research_prepass") or {})
    sources = [
        item
        for item in (prepass.get("backing_sources") or [])
        if isinstance(item, dict) and (str(item.get("title") or "").strip() or str(item.get("url") or "").strip())
    ]
    return [
        f"{str(source.get('title') or '').strip()} | {str(source.get('url') or '').strip()}".strip(" |")
        for source in sources
    ]


def sprint_research_prepass_body_lines(sprint_state: dict[str, Any] | None) -> list[str]:
    prepass = dict((sprint_state or {}).get("research_prepass") or {})
    if not prepass:
        return []
    subject_definition = (
        dict(prepass.get("research_subject_definition") or {})
        if isinstance(prepass.get("research_subject_definition"), dict)
        else {}
    )
    sources = [
        item
        for item in (prepass.get("backing_sources") or [])
        if isinstance(item, dict) and (str(item.get("title") or "").strip() or str(item.get("url") or "").strip())
    ]
    lines = [
        "research_prepass:",
        f"- request_id: {prepass.get('request_id') or ''}",
        f"- status: {prepass.get('status') or ''}",
        f"- reason_code: {prepass.get('reason_code') or ''}",
        f"- subject: {prepass.get('subject') or ''}",
        f"- research_query: {prepass.get('research_query') or ''}",
        f"- research_url: {prepass.get('research_url') or ''}",
        f"- headline: {prepass.get('headline') or ''}",
        f"- planner_guidance: {prepass.get('planner_guidance') or ''}",
        f"- backing_sources: {len(sources)}",
    ]
    if subject_definition:
        lines.append("- research_subject_definition:")
        for field in (
            "planning_decision",
            "knowledge_gap",
            "external_boundary",
            "planner_impact",
            "candidate_subject",
            "research_query",
            "no_subject_rationale",
        ):
            value = str(subject_definition.get(field) or "").strip()
            if value:
                lines.append(f"  - {field}: {value}")
        for field in ("source_requirements", "rejected_subjects"):
            items = _string_list(subject_definition.get(field))
            if not items:
                continue
            lines.append(f"  - {field}:")
            lines.extend(f"    - {item}" for item in items[:5])
    if sources:
        lines.append("- backing_source_refs:")
        for source in sources[:3]:
            title = str(source.get("title") or "").strip()
            url = str(source.get("url") or "").strip()
            lines.append(f"  - {title} | {url}".rstrip(" |"))
    section_labels = {
        "milestone_refinement_hints": "milestone_refinement_hints",
        "problem_framing_hints": "problem_framing_hints",
        "spec_implications": "spec_implications",
        "todo_definition_hints": "todo_definition_hints",
        "backing_reasoning": "backing_reasoning",
        "open_questions": "open_questions",
    }
    for field, label in section_labels.items():
        items = _string_list(prepass.get(field))
        if not items:
            continue
        lines.append(f"- {label}:")
        lines.extend(f"  - {item}" for item in items[:5])
    artifacts = sprint_research_prepass_artifacts(sprint_state)
    if artifacts:
        lines.append(f"- artifacts: {', '.join(artifacts[:3])}")
    return [line for line in lines if str(line).strip()]


def initial_phase_step_title(step: str) -> str:
    normalized = str(step or "").strip().lower()
    return INITIAL_PHASE_STEP_TITLES.get(normalized, normalized or "initial planning")


def initial_phase_step_position(step: str) -> int:
    normalized = str(step or "").strip().lower()
    try:
        return INITIAL_PHASE_STEPS.index(normalized) + 1
    except ValueError:
        return 0


def next_initial_phase_step(step: str) -> str:
    position = initial_phase_step_position(step)
    if position <= 0 or position >= len(INITIAL_PHASE_STEPS):
        return ""
    return INITIAL_PHASE_STEPS[position]


def initial_phase_step_instruction(step: str) -> str:
    normalized = str(step or "").strip().lower()
    if normalized == INITIAL_PHASE_STEP_MILESTONE_REFINEMENT:
        return (
            "Preserve the original kickoff brief, kickoff requirements, and kickoff reference artifacts first. "
            "Refine the sprint milestone title and execution framing separately in milestone-facing docs such as milestone.md. "
            "Do not select backlog items or execution todos in this step."
        )
    if normalized == INITIAL_PHASE_STEP_ARTIFACT_SYNC:
        return (
            "Update the sprint plan/spec/iteration artifacts so they reflect the latest refined milestone. "
            "Do not set planned_in_sprint_id, selected_in_sprint_id, or execution todos in this step."
        )
    if normalized == INITIAL_PHASE_STEP_BACKLOG_DEFINITION:
        return (
            "Define sprint-relevant backlog from the current milestone, kickoff requirements, and spec before any selection. "
            "Create or reopen backlog items when the persisted queue does not fully cover the sprint contract. "
            "Backlog zero is invalid in this step. Each backlog item must include concrete acceptance criteria plus origin trace "
            "for milestone_ref, requirement_refs, and spec_refs. Each item must also be linkable to this sprint through "
            "milestone_title or origin.sprint_id; leave planned_in_sprint_id and selected_in_sprint_id unset until later steps."
        )
    if normalized == INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION:
        return (
            "Prioritize only the already-defined sprint-relevant backlog work and persist priority_rank plus milestone_title. "
            "Use priority_rank=1 for the first dependency to execute; larger priority_rank values run later. "
            "Do not create execution todos in this step, and do not proceed if sprint-relevant backlog is still zero. "
            "If any sprint-relevant item still lacks priority_rank or milestone_title, the same step will reopen."
        )
    if normalized == INITIAL_PHASE_STEP_TODO_FINALIZATION:
        return (
            "Finalize the execution-ready todo set for this sprint. "
            "Persist planned_in_sprint_id for the chosen backlog items and leave the prioritized todo set ready to run "
            "in ascending priority_rank order. If selected backlog ids or todos are not persisted, the same step will reopen."
        )
    return ""


def build_sprint_planning_request_record(
    sprint_state: dict[str, Any],
    *,
    phase: str,
    iteration: int,
    step: str = "",
    request_id: str,
    artifacts: list[str],
    created_at: str,
    updated_at: str,
    git_baseline: dict[str, Any],
) -> dict[str, Any]:
    normalized_phase = str(phase or "").strip()
    normalized_step = str(step or "").strip().lower()
    milestone_title = str(sprint_state.get("milestone_title") or "").strip()
    step_title = initial_phase_step_title(normalized_step) if normalized_step else ""
    scope = (
        (
            f"sprint initial {normalized_step} for {milestone_title}"
            if normalized_step
            else f"sprint initial planning for {milestone_title}"
        )
        if normalized_phase == "initial"
        else f"sprint ongoing review for {milestone_title}"
    )
    body_lines = [
        "Current sprint requires planner-owned milestone refinement, plan/spec updates, mandatory backlog definition, and prioritized backlog/todo selection.",
        "Preserve the original kickoff brief, kickoff requirements, and kickoff reference artifacts as immutable source-of-truth.",
        "Only include backlog items and sprint todos that directly advance this sprint's single milestone.",
        "Do not promote unrelated maintenance or side quests into planned_in_sprint_id for this sprint.",
        "Use the persisted backlog artifacts in Current request.artifacts as backlog history and queue input, but if the current milestone, kickoff requirements, and spec are not fully covered, create or reopen sprint-relevant backlog before prioritization. backlog zero is invalid.",
        f"phase={normalized_phase}",
        f"iteration={iteration}",
        f"requested_milestone_title={sprint_state.get('requested_milestone_title') or ''}",
        f"milestone_title={sprint_state.get('milestone_title') or ''}",
        f"sprint_name={sprint_state.get('sprint_name') or ''}",
        f"sprint_folder={sprint_state.get('sprint_folder') or ''}",
    ]
    kickoff_brief = str(sprint_state.get("kickoff_brief") or "").strip()
    kickoff_requirements = [
        str(item).strip()
        for item in (sprint_state.get("kickoff_requirements") or [])
        if str(item).strip()
    ]
    kickoff_source_request_id = str(sprint_state.get("kickoff_source_request_id") or "").strip()
    if kickoff_source_request_id:
        body_lines.append(f"kickoff_source_request_id={kickoff_source_request_id}")
    if kickoff_brief:
        body_lines.extend(["kickoff_brief:", kickoff_brief])
    if kickoff_requirements:
        body_lines.append("kickoff_requirements:")
        body_lines.extend(f"- {item}" for item in kickoff_requirements)
    if normalized_phase == "initial" and normalized_step:
        body_lines.extend(
            [
                f"initial_phase_step={normalized_step}",
                f"step_title={step_title}",
                initial_phase_step_instruction(normalized_step),
            ]
        )
    reopened_step = str(sprint_state.get("last_initial_phase_reopen_step") or "").strip().lower()
    if normalized_phase == "initial" and normalized_step and reopened_step == normalized_step:
        reopen_counts = dict(sprint_state.get("initial_phase_reopen_counts") or {})
        body_lines.extend(
            [
                "initial_phase_reopen:",
                f"- reopen_of_request_id: {sprint_state.get('last_initial_phase_reopen_request_id') or ''}",
                f"- reopen_count: {int(reopen_counts.get(normalized_step) or 0)}",
                f"- reason: {sprint_state.get('last_initial_phase_reopen_reason') or ''}",
                "- policy: 이 단계의 필수 완료 조건이 충족되지 않아 같은 initial phase step이 재오픈되었습니다. 누락된 evidence를 보강하고 readback 근거를 남기세요.",
            ]
        )
    body_lines.extend(sprint_research_prepass_body_lines(sprint_state))
    body = "\n".join(line for line in body_lines if str(line).strip())
    sprint_id = str(sprint_state.get("sprint_id") or "")
    return {
        "request_id": str(request_id or "").strip(),
        "status": "queued",
        "intent": "plan",
        "urgency": "normal",
        "scope": scope,
        "body": body,
        "artifacts": _dedupe_preserving_order([str(item).strip() for item in artifacts if str(item).strip()]),
        "params": {
            "_teams_kind": "sprint_internal",
            "sprint_id": sprint_id,
            "sprint_phase": normalized_phase,
            "initial_phase_step": normalized_step,
            "requested_milestone_title": sprint_state.get("requested_milestone_title") or "",
            "milestone_title": sprint_state.get("milestone_title") or "",
            "kickoff_brief": sprint_state.get("kickoff_brief") or "",
            "kickoff_requirements": list(sprint_state.get("kickoff_requirements") or []),
            "kickoff_request_text": sprint_state.get("kickoff_request_text") or "",
            "kickoff_source_request_id": kickoff_source_request_id,
            "kickoff_reference_artifacts": list(sprint_state.get("kickoff_reference_artifacts") or []),
            "sprint_name": sprint_state.get("sprint_name") or "",
            "sprint_folder": sprint_state.get("sprint_folder") or "",
            "reopen_of_request_id": (
                sprint_state.get("last_initial_phase_reopen_request_id") or ""
                if reopened_step == normalized_step
                else ""
            ),
            "initial_phase_reopen_reason": (
                sprint_state.get("last_initial_phase_reopen_reason") or ""
                if reopened_step == normalized_step
                else ""
            ),
            "initial_phase_reopen_count": (
                int(dict(sprint_state.get("initial_phase_reopen_counts") or {}).get(normalized_step) or 0)
                if reopened_step == normalized_step
                else 0
            ),
        },
        "current_role": "orchestrator",
        "next_role": "planner",
        "owner_role": "orchestrator",
        "sprint_id": sprint_id,
        "backlog_id": "",
        "todo_id": "",
        "created_at": str(created_at or "").strip(),
        "updated_at": str(updated_at or "").strip(),
        "fingerprint": build_request_fingerprint(
            author_id="sprint-planner",
            channel_id=sprint_id,
            intent="plan",
            scope=scope,
        ),
        "reply_route": {},
        "events": [],
        "result": {},
        "git_baseline": dict(git_baseline or {}),
        "version_control_status": "",
        "version_control_sha": "",
        "version_control_paths": [],
        "version_control_message": "",
        "version_control_error": "",
        "task_commit_status": "",
        "task_commit_sha": "",
        "task_commit_paths": [],
        "task_commit_message": "",
        "visited_roles": [],
    }


def validate_initial_phase_step_result(
    sprint_state: dict[str, Any],
    *,
    request_record: dict[str, Any],
    sync_summary: dict[str, Any],
    relevant_items: list[dict[str, Any]],
    result: dict[str, Any] | None = None,
) -> str:
    step = initial_phase_step(request_record)
    if not step:
        return ""
    role_result = (
        dict(result or {})
        if isinstance(result, dict)
        else dict(request_record.get("result") or {})
        if isinstance(request_record.get("result"), dict)
        else {}
    )
    proposals = dict(role_result.get("proposals") or {}) if isinstance(role_result.get("proposals"), dict) else {}
    sprint_plan_update = (
        dict(proposals.get("sprint_plan_update") or {})
        if isinstance(proposals.get("sprint_plan_update"), dict)
        else {}
    )
    source_backed_research = sprint_research_prepass_source_backed(sprint_state)
    if step == INITIAL_PHASE_STEP_MILESTONE_REFINEMENT and source_backed_research:
        requested_title = str(sprint_state.get("requested_milestone_title") or "").strip()
        revised_title = str(
            proposals.get("revised_milestone_title")
            or sprint_plan_update.get("revised_milestone_title")
            or ""
        ).strip()
        refinement_rationale = str(
            proposals.get("milestone_refinement_rationale")
            or proposals.get("refinement_rationale")
            or sprint_plan_update.get("milestone_refinement_rationale")
            or sprint_plan_update.get("refinement_rationale")
            or ""
        ).strip()
        developed_framing = str(
            proposals.get("problem_framing")
            or proposals.get("developed_problem_statement")
            or sprint_plan_update.get("problem_framing")
            or sprint_plan_update.get("developed_problem_statement")
            or sprint_plan_update.get("refined_milestone_summary")
            or ""
        ).strip()
        research_refs = normalize_trace_list(
            proposals.get("research_refs")
            or sprint_plan_update.get("research_refs")
            or sprint_plan_update.get("source_refs")
            or []
        )
        if not revised_title:
            return (
                "initial phase milestone 정리 단계에서 source-backed research를 반영한 "
                "proposals.sprint_plan_update.revised_milestone_title이 없습니다."
            )
        if not refinement_rationale:
            return (
                "initial phase milestone 정리 단계에서 source-backed research를 milestone으로 연결한 "
                "refinement_rationale이 없습니다."
            )
        if revised_title == requested_title and not (developed_framing and research_refs):
            return (
                "initial phase milestone 정리 단계에서 user 요청 milestone을 그대로 채택했습니다. "
                "같은 제목을 유지하려면 developed problem framing과 research_refs가 필요합니다."
            )
    if step in {
        INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
        INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
        INITIAL_PHASE_STEP_TODO_FINALIZATION,
    } and not relevant_items:
        return (
            f"initial phase {initial_phase_step_title(step)} 단계에서 sprint-relevant backlog가 0건입니다. "
            "backlog 0건 상태는 허용되지 않습니다."
        )
    if step == INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION:
        validation_errors: list[str] = []
        for item in relevant_items:
            title = str(item.get("title") or item.get("backlog_id") or "unnamed backlog").strip()
            priority_rank = int(item.get("priority_rank") or 0)
            milestone_title = str(item.get("milestone_title") or "").strip()
            if priority_rank <= 0:
                validation_errors.append(f"{title}: priority_rank 없음")
            if not milestone_title:
                validation_errors.append(f"{title}: milestone_title 없음")
        if validation_errors:
            return (
                "initial phase backlog 우선순위화 단계의 완료 조건이 부족합니다. "
                + "; ".join(validation_errors[:4])
            )
        return ""
    if step == INITIAL_PHASE_STEP_TODO_FINALIZATION:
        selected_items = [item for item in (sprint_state.get("selected_items") or []) if isinstance(item, dict)]
        selected_backlog_ids = [
            str(item).strip() for item in (sprint_state.get("selected_backlog_ids") or []) if str(item).strip()
        ]
        todos = [item for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
        if not selected_items or not selected_backlog_ids or not todos:
            return (
                "initial phase 실행 todo 확정 단계에서 selected backlog 또는 sprint todo가 persist되지 않았습니다. "
                "planned_in_sprint_id와 실행 todo를 확정해야 합니다."
            )
        missing_planned = [
            str(item.get("title") or item.get("backlog_id") or "unnamed backlog").strip()
            for item in selected_items
            if str(item.get("planned_in_sprint_id") or "").strip()
            != str(sprint_state.get("sprint_id") or "").strip()
        ]
        if missing_planned:
            return (
                "initial phase 실행 todo 확정 단계에서 planned_in_sprint_id가 현재 sprint와 연결되지 않았습니다. "
                + "; ".join(missing_planned[:4])
            )
        return ""
    if step != INITIAL_PHASE_STEP_BACKLOG_DEFINITION:
        return ""
    if not bool(sync_summary.get("planner_persisted_backlog")):
        return (
            "initial phase backlog 정의 단계에서 planner가 sprint-relevant backlog를 실제로 persist하지 않았습니다. "
            "문서 정리만으로는 다음 단계로 진행할 수 없습니다."
        )
    kickoff_requirements = normalize_trace_list(sprint_state.get("kickoff_requirements") or [])
    validation_errors: list[str] = []
    for item in relevant_items:
        title = str(item.get("title") or item.get("backlog_id") or "unnamed backlog").strip()
        acceptance = normalize_trace_list(item.get("acceptance_criteria") or [])
        origin = dict(item.get("origin") or {})
        milestone_ref = str(origin.get("milestone_ref") or "").strip()
        requirement_refs = normalize_trace_list(origin.get("requirement_refs") or [])
        spec_refs = normalize_trace_list(origin.get("spec_refs") or [])
        research_refs = normalize_trace_list(origin.get("research_refs") or [])
        origin_sprint_id = str(origin.get("sprint_id") or "").strip()
        item_milestone = str(item.get("milestone_title") or "").strip()
        if not acceptance:
            validation_errors.append(f"{title}: acceptance_criteria 없음")
        if not milestone_ref:
            validation_errors.append(f"{title}: origin.milestone_ref 없음")
        if not origin_sprint_id and not item_milestone:
            validation_errors.append(f"{title}: milestone_title 또는 origin.sprint_id 없음")
        if kickoff_requirements and not requirement_refs:
            validation_errors.append(f"{title}: origin.requirement_refs 없음")
        if not spec_refs:
            validation_errors.append(f"{title}: origin.spec_refs 없음")
        if source_backed_research and not research_refs:
            validation_errors.append(f"{title}: origin.research_refs 없음")
    if validation_errors:
        return (
            "initial phase backlog 정의 단계의 backlog trace가 부족합니다. "
            + "; ".join(validation_errors[:4])
        )
    return ""


def sprint_planning_phase_ready(
    sprint_state: dict[str, Any],
    *,
    phase: str,
    step: str,
) -> bool:
    phase_ready = bool(sprint_state.get("selected_items"))
    if phase == "initial" and step and step != INITIAL_PHASE_STEP_TODO_FINALIZATION:
        return False
    return phase_ready


def build_sprint_planning_iteration_entry(
    *,
    created_at: str,
    phase: str,
    step: str,
    request_record: dict[str, Any],
    result: dict[str, Any],
    phase_ready: bool,
) -> dict[str, Any]:
    return {
        "created_at": str(created_at or "").strip(),
        "phase": phase,
        "step": step,
        "request_id": str(request_record.get("request_id") or ""),
        "summary": str(result.get("summary") or "").strip(),
        "insights": [str(item).strip() for item in (result.get("insights") or []) if str(item).strip()],
        "artifacts": [str(item).strip() for item in (result.get("artifacts") or []) if str(item).strip()],
        "phase_ready": bool(phase_ready),
    }


def record_sprint_planning_iteration(
    sprint_state: dict[str, Any],
    *,
    created_at: str,
    phase: str,
    step: str,
    request_record: dict[str, Any],
    result: dict[str, Any],
    phase_ready: bool,
) -> None:
    iterations = list(sprint_state.get("planning_iterations") or [])
    iteration_entry = build_sprint_planning_iteration_entry(
        created_at=created_at,
        phase=phase,
        step=step,
        request_record=request_record,
        result=result,
        phase_ready=phase_ready,
    )
    matched_index = next(
        (
            index
            for index, entry in enumerate(iterations)
            if str(entry.get("request_id") or "").strip() == iteration_entry["request_id"]
            and str(entry.get("phase") or "").strip() == iteration_entry["phase"]
        ),
        -1,
    )
    if matched_index >= 0:
        existing_created_at = str(iterations[matched_index].get("created_at") or "").strip()
        if existing_created_at:
            iteration_entry["created_at"] = existing_created_at
        iterations[matched_index] = iteration_entry
    else:
        iterations.append(iteration_entry)
    sprint_state["planning_iterations"] = iterations


def build_recovered_sprint_todo_from_request_for_service(
    service: Any,
    sprint_state: dict[str, Any],
    request_record: dict[str, Any],
) -> dict[str, Any]:
    params = dict(request_record.get("params") or {})
    request_id = str(request_record.get("request_id") or "").strip()
    backlog_id = str(request_record.get("backlog_id") or params.get("backlog_id") or "").strip()
    todo_id = str(request_record.get("todo_id") or params.get("todo_id") or "").strip() or (
        f"recovered-{request_id}" if request_id else ""
    )
    if not backlog_id and not todo_id:
        return {}

    backlog_item = service._load_backlog_item(backlog_id) if backlog_id else {}
    artifacts = service._normalize_sprint_todo_artifacts(
        request_record.get("artifacts"),
        (request_record.get("result") or {}).get("artifacts") if isinstance(request_record.get("result"), dict) else [],
        request_record.get("task_commit_paths"),
        request_record.get("version_control_paths"),
        workflow_state=service._request_workflow_state(request_record),
    )
    return build_recovered_sprint_todo_from_request(
        sprint_state,
        request_record,
        backlog_item=backlog_item,
        artifacts=artifacts,
    )


def recover_sprint_todos_from_requests(service: Any, sprint_state: dict[str, Any]) -> bool:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    if not sprint_id:
        return False
    request_records = service._iter_sprint_task_request_records(sprint_id)
    if not sprint_state.get("todos") and not request_records:
        return False
    recovered_todos: list[dict[str, Any]] = []
    for request_record in request_records:
        recovered = service._build_recovered_sprint_todo_from_request(sprint_state, request_record)
        if not recovered:
            continue
        recovered_todos.append(recovered)
    return recover_sprint_todos_from_recovered(sprint_state, recovered_todos)


def synchronize_sprint_todo_backlog_state(
    service: Any,
    sprint_state: dict[str, Any],
    *,
    persist_backlog: bool = True,
) -> bool:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    existing_selected_items = list(sprint_state.get("selected_items") or [])
    existing_selected_backlog_ids = [
        str(item).strip()
        for item in (sprint_state.get("selected_backlog_ids") or [])
        if str(item).strip()
    ]
    todos = list(sprint_state.get("todos") or [])

    ordered_backlog_ids: list[str] = []
    selected_items_by_backlog_id: dict[str, dict[str, Any]] = {}
    todo_by_backlog_id: dict[str, dict[str, Any]] = {}

    def remember_backlog_id(backlog_id: str) -> None:
        normalized_backlog_id = str(backlog_id or "").strip()
        if not normalized_backlog_id or normalized_backlog_id in ordered_backlog_ids:
            return
        ordered_backlog_ids.append(normalized_backlog_id)

    for item in existing_selected_items:
        backlog_id = str(item.get("backlog_id") or "").strip()
        if not backlog_id or backlog_id in selected_items_by_backlog_id:
            continue
        selected_items_by_backlog_id[backlog_id] = dict(item)
        remember_backlog_id(backlog_id)
    for backlog_id in existing_selected_backlog_ids:
        remember_backlog_id(backlog_id)
    for todo in todos:
        backlog_id = str(todo.get("backlog_id") or "").strip()
        if not backlog_id:
            continue
        todo_by_backlog_id[backlog_id] = todo
        remember_backlog_id(backlog_id)

    updated_selected_items: list[dict[str, Any]] = []
    live_selected_backlog_ids: list[str] = []
    backlog_changed = False

    for backlog_id in ordered_backlog_ids:
        todo = todo_by_backlog_id.get(backlog_id)
        selected_item = dict(selected_items_by_backlog_id.get(backlog_id) or {})
        backlog_item = service._load_backlog_item(backlog_id)
        if backlog_item and service._apply_backlog_state_from_todo(backlog_item, todo=todo, sprint_id=sprint_id):
            if persist_backlog:
                save_backlog_item(
                    service.paths,
                    backlog_item,
                    update_timestamp=True,
                    refresh_markdown=False,
                )
                backlog_changed = True
        merged_item = build_sprint_selected_backlog_item(
            backlog_id,
            backlog_item=backlog_item,
            selected_item=selected_item,
            todo=todo,
            sprint_id=sprint_id,
        )
        if not merged_item:
            continue
        updated_selected_items.append(merged_item)
        if str(merged_item.get("status") or "").strip().lower() in {"pending", "selected"}:
            live_selected_backlog_ids.append(backlog_id)

    updated_selected_items = sorted(
        updated_selected_items,
        key=priority_rank_sort_key,
    )
    live_selected_backlog_ids = [
        str(item.get("backlog_id") or "").strip()
        for item in updated_selected_items
        if str(item.get("backlog_id") or "").strip()
        and str(item.get("status") or "").strip().lower() in {"pending", "selected"}
    ]
    sorted_todos = sort_sprint_todos(todos)

    changed = False
    if updated_selected_items != existing_selected_items:
        sprint_state["selected_items"] = updated_selected_items
        changed = True
    if live_selected_backlog_ids != existing_selected_backlog_ids:
        sprint_state["selected_backlog_ids"] = live_selected_backlog_ids
        changed = True
    if sorted_todos != todos:
        sprint_state["todos"] = sorted_todos
        changed = True
    if backlog_changed:
        service._refresh_backlog_markdown()
    return changed or backlog_changed


def load_sprint_state_with_sync(service: Any, sprint_id: str) -> dict[str, Any]:
    if not sprint_id:
        return {}
    sprint_state = load_sprint_state(service.paths, sprint_id)
    if not sprint_state:
        return {}
    service._ensure_sprint_folder_metadata(sprint_state)
    active_sprint_id = str(service._load_scheduler_state().get("active_sprint_id") or "").strip()
    sprint_state_changed = service._recover_sprint_todos_from_requests(sprint_state)
    if service._normalize_sprint_reference_attachments(sprint_state):
        sprint_state_changed = True
    if service._synchronize_sprint_todo_backlog_state(sprint_state):
        sprint_state_changed = True
    if service._refresh_sprint_report_body(sprint_state):
        sprint_state_changed = True
    if service._refresh_sprint_history_archive(sprint_state):
        sprint_state_changed = True
    if sprint_state_changed:
        sprint_state["updated_at"] = utc_now_iso()
        save_sprint_state(
            service.paths,
            sprint_state,
            write_current_sprint=sprint_id == active_sprint_id,
        )
    if str(sprint_state.get("sprint_folder_name") or "").strip():
        service._write_sprint_artifact_files(sprint_state)
    return sprint_state


def save_sprint_state_with_sync(service: Any, sprint_state: dict[str, Any]) -> None:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    if not sprint_id:
        return
    service._ensure_sprint_folder_metadata(sprint_state)
    service._recover_sprint_todos_from_requests(sprint_state)
    service._normalize_sprint_reference_attachments(sprint_state)
    service._synchronize_sprint_todo_backlog_state(sprint_state)
    service._refresh_sprint_report_body(sprint_state)
    service._refresh_sprint_history_archive(sprint_state)
    save_sprint_state(
        service.paths,
        sprint_state,
        update_timestamp=True,
        write_current_sprint=True,
    )
    if str(sprint_state.get("sprint_folder_name") or "").strip():
        service._write_sprint_artifact_files(sprint_state)


def maybe_update_sprint_name_from_result(service: Any, sprint_state: dict[str, Any], result: dict[str, Any]) -> None:
    proposals = dict(result.get("proposals") or {})
    revised_title = str(
        proposals.get("revised_milestone_title")
        or dict(proposals.get("sprint_plan_update") or {}).get("revised_milestone_title")
        or ""
    ).strip()
    if not revised_title or revised_title == str(sprint_state.get("milestone_title") or "").strip():
        return
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    display_name, folder_name = service._build_manual_sprint_names(
        sprint_id=sprint_id,
        milestone_title=revised_title,
    )
    sprint_state["milestone_title"] = revised_title
    sprint_state["sprint_name"] = display_name
    sprint_state["sprint_display_name"] = display_name
    sprint_state["sprint_folder_name"] = folder_name
    sprint_state["sprint_folder"] = str(service.paths.sprint_artifact_dir(folder_name))


def sync_manual_sprint_queue(service: Any, sprint_state: dict[str, Any]) -> None:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    if not sprint_id:
        return
    sprint_milestone_title = str(sprint_state.get("milestone_title") or "").strip()
    selected_items = [
        item
        for item in service._iter_backlog_items()
        if sprint_id
        in {
            str(item.get("planned_in_sprint_id") or "").strip(),
            str(item.get("selected_in_sprint_id") or "").strip(),
        }
        and str(item.get("status") or "").strip().lower() in {"pending", "selected"}
    ]
    selected_items.sort(
        key=lambda item: (
            priority_rank_sort_value(item.get("priority_rank")),
            str(item.get("created_at") or ""),
        )
    )
    for item in selected_items:
        if sprint_milestone_title and str(item.get("milestone_title") or "").strip() != sprint_milestone_title:
            item["milestone_title"] = sprint_milestone_title
        if str(item.get("status") or "").strip().lower() == "pending":
            item["status"] = "selected"
            item["selected_in_sprint_id"] = sprint_id
        service._save_backlog_item(item)
    sprint_state["selected_items"] = selected_items
    sprint_state["selected_backlog_ids"] = [str(item.get("backlog_id") or "") for item in selected_items]
    existing_by_backlog_id = {
        str(todo.get("backlog_id") or "").strip(): dict(todo)
        for todo in (sprint_state.get("todos") or [])
        if str(todo.get("backlog_id") or "").strip()
    }
    updated_todos: list[dict[str, Any]] = []
    selected_backlog_ids = set()
    for item in selected_items:
        backlog_id = str(item.get("backlog_id") or "").strip()
        selected_backlog_ids.add(backlog_id)
        existing = existing_by_backlog_id.pop(backlog_id, None)
        if existing is None:
            updated_todos.append(build_todo_item(item, owner_role="planner"))
            continue
        existing["title"] = str(item.get("title") or existing.get("title") or "").strip()
        existing["milestone_title"] = sprint_milestone_title or str(
            item.get("milestone_title") or existing.get("milestone_title") or ""
        ).strip()
        existing["priority_rank"] = int(item.get("priority_rank") or existing.get("priority_rank") or 0)
        existing["acceptance_criteria"] = [
            str(value).strip()
            for value in (item.get("acceptance_criteria") or existing.get("acceptance_criteria") or [])
            if str(value).strip()
        ]
        updated_todos.append(existing)
    for backlog_id, todo in existing_by_backlog_id.items():
        status = str(todo.get("status") or "").strip().lower()
        if backlog_id in selected_backlog_ids or status in {
            "running",
            "completed",
            "committed",
            "blocked",
            "failed",
            "uncommitted",
        }:
            updated_todos.append(todo)
    sprint_state["todos"] = service._sort_sprint_todos(updated_todos)


def sync_sprint_planning_state(
    service: Any,
    sprint_state: dict[str, Any],
    *,
    phase: str,
    request_record: dict[str, Any],
    result: dict[str, Any],
) -> bool:
    service._merge_persisted_sprint_research_prepass(sprint_state)
    service._maybe_update_sprint_name_from_result(sprint_state, result)
    service._sync_manual_sprint_queue(sprint_state)
    params = dict(request_record.get("params") or {})
    step = str(params.get("initial_phase_step") or "").strip().lower()
    phase_ready = sprint_planning_phase_ready(
        sprint_state,
        phase=phase,
        step=step,
    )
    service._record_sprint_planning_iteration(
        sprint_state,
        phase=phase,
        step=step,
        request_record=request_record,
        result=result,
        phase_ready=phase_ready,
    )
    return phase_ready


def sync_internal_sprint_artifacts_from_role_report(
    service: Any,
    request_record: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    if not service._is_internal_sprint_request(request_record):
        return {}
    role = str(result.get("role") or "").strip()
    if role == "research":
        status = str(result.get("status") or "").strip().lower()
        if status != "completed":
            return {}
        params = dict(request_record.get("params") or {})
        sprint_id = str(request_record.get("sprint_id") or params.get("sprint_id") or "").strip()
        if not sprint_id:
            return {}
        sprint_state = service._load_sprint_state(sprint_id)
        if not sprint_state:
            return {}
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        research_signal = dict(proposals.get("research_signal") or {}) if isinstance(proposals.get("research_signal"), dict) else {}
        research_report = dict(proposals.get("research_report") or {}) if isinstance(proposals.get("research_report"), dict) else {}
        raw_subject_definition = proposals.get("research_subject_definition")
        if not isinstance(raw_subject_definition, dict) or not raw_subject_definition:
            raw_subject_definition = research_report.get("research_subject_definition")
        subject_definition = dict(raw_subject_definition or {}) if isinstance(raw_subject_definition, dict) else {}
        report_artifact = str(research_report.get("report_artifact") or "").strip()
        artifacts = _dedupe_preserving_order(
            [
                *[str(item).strip() for item in (result.get("artifacts") or []) if str(item).strip()],
                report_artifact,
            ]
        )
        backing_sources = [
            dict(item)
            for item in (research_report.get("backing_sources") or [])
            if isinstance(item, dict)
        ]
        prepass = {
            "request_id": str(request_record.get("request_id") or result.get("request_id") or "").strip(),
            "status": status,
            "reason_code": str(research_signal.get("reason_code") or "").strip(),
            "subject": str(research_signal.get("subject") or "").strip(),
            "research_query": str(research_signal.get("research_query") or "").strip(),
            "research_url": str(research_report.get("research_url") or "").strip(),
            "research_subject_definition": subject_definition,
            "headline": str(research_report.get("headline") or result.get("summary") or "").strip(),
            "planner_guidance": str(research_report.get("planner_guidance") or "").strip(),
            "backing_sources": backing_sources,
            "artifacts": artifacts,
            "completed_at": utc_now_iso(),
        }
        for field in RESEARCH_REPORT_LIST_FIELDS:
            prepass[field] = _string_list(research_report.get(field))
        sprint_state["research_prepass"] = prepass
        sprint_state["reference_artifacts"] = _dedupe_preserving_order(
            [
                *[str(item).strip() for item in (sprint_state.get("reference_artifacts") or []) if str(item).strip()],
                *artifacts,
            ]
        )
        service._save_sprint_state(sprint_state)
        service._append_sprint_event(
            sprint_id,
            event_type="research_prepass_completed",
            summary=str(prepass.get("headline") or "sprint planning research prepass를 완료했습니다."),
            payload={
                "request_id": prepass["request_id"],
                "artifacts": artifacts,
                "backing_source_count": len(backing_sources),
            },
        )
        return prepass
    if role != "planner":
        return {}
    if str(result.get("status") or "").strip().lower() not in {"completed", "committed"}:
        return {}
    params = dict(request_record.get("params") or {})
    sprint_id = str(request_record.get("sprint_id") or params.get("sprint_id") or "").strip()
    if not sprint_id:
        return {}
    sprint_state = service._load_sprint_state(sprint_id)
    if not sprint_state:
        return {}
    phase = str(params.get("sprint_phase") or "").strip() or "initial"
    sync_summary = service._sync_planner_backlog_from_report(request_record, result)
    request_record["planning_sync_summary"] = sync_summary
    service._sync_sprint_planning_state(
        sprint_state,
        phase=phase,
        request_record=request_record,
        result=result,
    )
    service._save_sprint_state(sprint_state)
    iteration_entry = sync_summary
    if iteration_entry.get("missing_backlog_artifacts") or iteration_entry.get("missing_backlog_receipts"):
        service._append_sprint_event(
            sprint_id,
            event_type="planning_sync_warning",
            summary="planner backlog persistence receipt 또는 아티팩트를 확인할 수 없습니다.",
            payload={
                "request_id": request_record.get("request_id") or "",
                "missing_backlog_artifacts": list(iteration_entry.get("missing_backlog_artifacts") or []),
                "missing_backlog_receipts": list(iteration_entry.get("missing_backlog_receipts") or []),
            },
        )
    return sync_summary


def sync_planner_backlog_review_from_role_report(
    service: Any,
    request_record: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    if not service._is_planner_backlog_review_request(request_record):
        return {}
    if str(result.get("role") or "").strip() != "planner":
        return {}
    status = str(result.get("status") or "").strip().lower()
    if not service._is_terminal_internal_request_status(status):
        return {}
    sync_summary: dict[str, Any] = {}
    if status in {"completed", "committed"}:
        sync_summary = service._sync_planner_backlog_from_report(request_record, result)
        request_record["planning_sync_summary"] = sync_summary
    if service._is_blocked_backlog_review_request(request_record):
        scheduler_state = service._load_scheduler_state()
        scheduler_state["last_blocked_review_at"] = utc_now_iso()
        scheduler_state["last_blocked_review_request_id"] = str(request_record.get("request_id") or "")
        scheduler_state["last_blocked_review_status"] = status or "completed"
        scheduler_state["last_blocked_review_fingerprint"] = str(request_record.get("fingerprint") or "").strip()
        service._save_scheduler_state(scheduler_state)
    if service._is_sourcer_review_request(request_record):
        scheduler_state = service._load_scheduler_state()
        scheduler_state["last_sourcing_fingerprint"] = str(request_record.get("fingerprint") or "").strip()
        scheduler_state["last_sourcing_review_status"] = status or "completed"
        scheduler_state["last_sourcing_review_request_id"] = str(request_record.get("request_id") or "")
        service._save_scheduler_state(scheduler_state)
    return sync_summary


def apply_sprint_planning_result(
    service: Any,
    sprint_state: dict[str, Any],
    *,
    phase: str,
    request_record: dict[str, Any],
    result: dict[str, Any],
) -> bool:
    step = service._initial_phase_step(request_record)
    phase_ready = service._sync_sprint_planning_state(
        sprint_state,
        phase=phase,
        request_record=request_record,
        result=result,
    )
    sync_summary = (
        dict(request_record.get("planning_sync_summary"))
        if isinstance(request_record.get("planning_sync_summary"), dict)
        else service._sync_planner_backlog_from_report(request_record, result, persist=False)
    )
    service._append_sprint_event(
        str(sprint_state.get("sprint_id") or ""),
        event_type="planning_sync",
        summary=(
            f"sprint {phase} planning 결과를 반영했습니다."
            if not step
            else f"sprint {phase} planning 결과를 반영했습니다. step={service._initial_phase_step_title(step)}"
        ),
        payload={
            "request_id": request_record.get("request_id") or "",
            "planner_persisted_backlog": bool(sync_summary.get("planner_persisted_backlog")),
            "selected_count": len(sprint_state.get("selected_items") or []),
            "proposal_items": int(sync_summary.get("proposal_items") or 0),
            "receipt_items": int(sync_summary.get("receipt_items") or 0),
            "artifact_items": int(sync_summary.get("artifact_items") or 0),
            "merged_items": int(sync_summary.get("merged_items") or 0),
            "verified_backlog_items": int(sync_summary.get("verified_backlog_items") or 0),
            "persisted_backlog_items": int(sync_summary.get("persisted_backlog_items") or 0),
            "missing_backlog_artifacts": sync_summary.get("missing_backlog_artifacts") or [],
            "missing_backlog_receipts": sync_summary.get("missing_backlog_receipts") or [],
            "initial_phase_step": step,
        },
    )
    if sync_summary.get("missing_backlog_artifacts") or sync_summary.get("missing_backlog_receipts"):
        service._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="planning_sync_warning",
            summary="planner backlog persistence receipt 또는 아티팩트 확인이 필요합니다.",
            payload={
                "request_id": request_record.get("request_id") or "",
                "missing_backlog_artifacts": list(sync_summary.get("missing_backlog_artifacts") or []),
                "missing_backlog_receipts": list(sync_summary.get("missing_backlog_receipts") or []),
            },
        )
    validation_error = ""
    if phase == "initial":
        validation_error = service._validate_initial_phase_step_result(
            sprint_state,
            request_record=request_record,
            sync_summary=sync_summary,
            result=result,
        )
    request_record["initial_phase_validation_error"] = validation_error
    service._save_request(request_record)
    if validation_error:
        service._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="planning_sync_invalid",
            summary=validation_error,
            payload={
                "request_id": request_record.get("request_id") or "",
                "initial_phase_step": step,
            },
        )
        return False
    return phase_ready


def uses_manual_daily_sprint(sprint_start_mode: str) -> bool:
    return str(sprint_start_mode or "").strip().lower() == "manual_daily"


def sprint_uses_manual_flow(
    *,
    sprint_start_mode: str,
    sprint_state: dict[str, Any] | None = None,
) -> bool:
    if uses_manual_daily_sprint(sprint_start_mode):
        return True
    state = dict(sprint_state or {})
    execution_mode = str(state.get("execution_mode") or "").strip().lower()
    if execution_mode == "manual":
        return True
    return str(state.get("trigger") or "").strip().lower() == "manual_start"


def build_manual_sprint_names(*, sprint_id: str, milestone_title: str) -> tuple[str, str]:
    display_name = build_daily_sprint_display_name(milestone_title)
    folder_name = build_sprint_artifact_folder_name(sprint_id)
    return display_name, folder_name


def build_idle_current_sprint_markdown() -> str:
    return "# Current Sprint\n\n- active sprint 없음\n"


def is_manual_sprint_cutoff_reached(
    *,
    sprint_start_mode: str,
    sprint_state: dict[str, Any],
) -> bool:
    if not sprint_uses_manual_flow(
        sprint_start_mode=sprint_start_mode,
        sprint_state=sprint_state,
    ):
        return False
    # Current policy: manual sprints continue without time cutoff enforcement.
    return False


def build_manual_sprint_state(
    *,
    milestone_title: str,
    trigger: str,
    sprint_cutoff_time: str,
    sprint_artifacts_root: Path,
    git_baseline: dict[str, Any],
    build_sprint_id: Callable[..., str] | None = None,
    started_at: datetime | None = None,
    kickoff_brief: str = "",
    kickoff_requirements: list[str] | None = None,
    kickoff_request_text: str = "",
    kickoff_source_request_id: str = "",
    kickoff_reference_artifacts: list[str] | None = None,
) -> dict[str, Any]:
    started_at_dt = normalize_runtime_datetime(started_at)
    started_at_text = started_at_dt.isoformat()
    sprint_id_factory = build_sprint_id or build_active_sprint_id
    sprint_id = sprint_id_factory(now=started_at_dt)
    sprint_name, folder_name = build_manual_sprint_names(
        sprint_id=sprint_id,
        milestone_title=milestone_title,
    )
    normalized_brief = _clean_sprint_text(kickoff_brief)
    normalized_requirements = _normalize_sprint_requirements(kickoff_requirements)
    normalized_request_text = _clean_sprint_text(kickoff_request_text)
    normalized_reference_artifacts = _dedupe_preserving_order(
        [str(item).strip() for item in (kickoff_reference_artifacts or []) if str(item).strip()]
    )[:12]
    return {
        "sprint_id": sprint_id,
        "sprint_name": sprint_name,
        "sprint_display_name": sprint_name,
        "sprint_folder": str(Path(sprint_artifacts_root) / folder_name),
        "sprint_folder_name": folder_name,
        "requested_milestone_title": str(milestone_title or "").strip(),
        "milestone_title": str(milestone_title or "").strip(),
        "kickoff_brief": normalized_brief,
        "kickoff_requirements": normalized_requirements,
        "kickoff_request_text": normalized_request_text,
        "kickoff_source_request_id": str(kickoff_source_request_id or "").strip(),
        "kickoff_reference_artifacts": normalized_reference_artifacts,
        "phase": "initial",
        "status": "planning",
        "trigger": trigger,
        "execution_mode": "manual",
        "started_at": started_at_text,
        "ended_at": "",
        "cutoff_at": build_sprint_cutoff_at(
            sprint_cutoff_time,
            now=started_at_dt,
        ).isoformat(),
        "initial_phase_ready_at": "",
        "last_planner_review_at": "",
        "wrap_up_requested_at": "",
        "selected_backlog_ids": [],
        "selected_items": [],
        "todos": [],
        "reference_artifacts": list(normalized_reference_artifacts),
        "planning_iterations": [],
        "commit_sha": "",
        "commit_shas": [],
        "commit_count": 0,
        "closeout_status": "",
        "uncommitted_paths": [],
        "version_control_status": "",
        "version_control_sha": "",
        "version_control_paths": [],
        "version_control_message": "",
        "version_control_error": "",
        "auto_commit_status": "",
        "auto_commit_sha": "",
        "auto_commit_paths": [],
        "auto_commit_message": "",
        "report_path": "",
        "git_baseline": dict(git_baseline or {}),
        "resume_from_checkpoint_requested_at": "",
        "last_resume_checkpoint_todo_id": "",
        "last_resume_checkpoint_status": "",
    }


def is_resumable_blocked_sprint(sprint_state: dict[str, Any]) -> bool:
    report_body = str(sprint_state.get("report_body") or "").strip().lower()
    if (
        str(sprint_state.get("status") or "").strip().lower() == "blocked"
        and str(sprint_state.get("phase") or "").strip().lower() == "initial"
        and ("initial phase" in report_body or "initial phase planning" in report_body)
        and ("시작하지 못했습니다" in report_body or "중단했습니다" in report_body)
    ):
        return True
    return (
        str(sprint_state.get("status") or "").strip().lower() == "blocked"
        and str(sprint_state.get("closeout_status") or "").strip().lower()
        in {"planning_incomplete", "restart_required"}
    )


def is_wrap_up_requested(sprint_state: dict[str, Any]) -> bool:
    return bool(str(sprint_state.get("wrap_up_requested_at") or "").strip())


def is_executable_todo_status(status: str) -> bool:
    return str(status or "").strip().lower() in {"queued", "running", "uncommitted"}


def is_terminal_todo_status(status: str) -> bool:
    return str(status or "").strip().lower() in {"completed", "committed", "blocked", "failed"}


def first_meaningful_text(*values: Any) -> str:
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


async def run_autonomous_sprint(
    service: Any,
    trigger: str,
    *,
    selected_items: list[dict[str, Any]] | None = None,
) -> None:
    scheduler_state = service._load_scheduler_state()
    if scheduler_state.get("active_sprint_id"):
        return
    started_at = utc_now_iso()
    build_sprint_id = getattr(service, "_build_active_sprint_id", None)
    sprint_id = build_sprint_id() if callable(build_sprint_id) else build_active_sprint_id()
    folder_name = build_sprint_artifact_folder_name(sprint_id)
    scheduler_state["active_sprint_id"] = sprint_id
    scheduler_state["last_started_at"] = started_at
    scheduler_state["last_trigger"] = trigger
    service._clear_pending_milestone_request(scheduler_state)
    service._save_scheduler_state(scheduler_state)

    capture_baseline = getattr(service, "_capture_git_baseline", None)
    baseline = (
        capture_baseline()
        if callable(capture_baseline)
        else capture_git_baseline(service.paths.project_workspace_root)
    )
    sprint_state = {
        "sprint_id": sprint_id,
        "sprint_name": sprint_id,
        "sprint_display_name": sprint_id,
        "sprint_folder": str(service.paths.sprint_artifact_dir(folder_name)),
        "sprint_folder_name": folder_name,
        "status": "planning",
        "trigger": trigger,
        "execution_mode": "auto",
        "started_at": started_at,
        "ended_at": "",
        "selected_backlog_ids": [],
        "selected_items": [],
        "todos": [],
        "reference_artifacts": [],
        "commit_sha": "",
        "commit_shas": [],
        "commit_count": 0,
        "closeout_status": "",
        "uncommitted_paths": [],
        "version_control_status": "",
        "version_control_sha": "",
        "version_control_paths": [],
        "version_control_message": "",
        "version_control_error": "",
        "auto_commit_status": "",
        "auto_commit_sha": "",
        "auto_commit_paths": [],
        "auto_commit_message": "",
        "report_path": "",
        "git_baseline": baseline,
        "resume_from_checkpoint_requested_at": "",
        "last_resume_checkpoint_todo_id": "",
        "last_resume_checkpoint_status": "",
    }
    service._save_sprint_state(sprint_state)
    service._append_sprint_event(sprint_id, event_type="started", summary=f"스프린트를 시작했습니다. trigger={trigger}")
    try:
        if selected_items is None:
            selected_items = await asyncio.to_thread(service._prepare_actionable_backlog_for_sprint)
        if not selected_items:
            await service._complete_terminal_sprint(
                sprint_state,
                status="completed",
                closeout_status="no_backlog_selected",
                terminal_title="🛑 스프린트 종료",
                message="선택할 backlog가 없어 스프린트를 종료했습니다.",
                commit_count=0,
                commit_shas=[],
                representative_commit_sha="",
                uncommitted_paths=[],
            )
            return

        selected_items = sorted(selected_items, key=priority_rank_sort_key)
        for item in selected_items:
            item["status"] = "selected"
            item["selected_in_sprint_id"] = sprint_id
            service._save_backlog_item(item)
        sprint_state["selected_backlog_ids"] = [str(item.get("backlog_id") or "") for item in selected_items]
        sprint_state["selected_items"] = selected_items
        sprint_state["todos"] = [build_todo_item(item, owner_role="planner") for item in selected_items]
        await service._continue_sprint(sprint_state, announce=True)
    except Exception as exc:
        await service._fail_sprint_due_to_exception(sprint_state, exc)


def finish_scheduler_after_sprint(
    service: Any,
    sprint_state: dict[str, Any],
    *,
    clear_active: bool | None = None,
) -> None:
    state = service._load_scheduler_state()
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    status = str(sprint_state.get("status") or "").strip().lower()
    if clear_active is None:
        clear_active = status == "completed"
    if clear_active:
        state["active_sprint_id"] = ""
        state["last_completed_at"] = utc_now_iso()
    elif sprint_id:
        state["active_sprint_id"] = sprint_id
    service._save_scheduler_state(state)
    if clear_active:
        service.paths.current_sprint_file.write_text(service._build_idle_current_sprint_markdown(), encoding="utf-8")


def select_restart_checkpoint_todo(
    service: Any,
    sprint_state: dict[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
    todos = list(sprint_state.get("todos") or [])
    best_candidate: tuple[tuple[int, int, float, int], dict[str, Any], str, dict[str, Any]] | None = None
    status_priority = {"running": 3, "uncommitted": 2, "blocked": 1}
    for index, todo in enumerate(todos):
        normalized_status = str(todo.get("status") or "").strip().lower()
        if normalized_status not in status_priority:
            continue
        request_record: dict[str, Any] = {}
        request_id = str(todo.get("request_id") or "").strip()
        if request_id:
            request_record = service._load_request(request_id)
        checkpoint_at = service._parse_datetime(
            str(
                request_record.get("updated_at")
                or request_record.get("created_at")
                or todo.get("ended_at")
                or todo.get("started_at")
                or ""
            )
        )
        candidate_key = (
            status_priority[normalized_status],
            1 if checkpoint_at is not None else 0,
            checkpoint_at.timestamp() if checkpoint_at is not None else float("-inf"),
            index,
        )
        if best_candidate is None or candidate_key > best_candidate[0]:
            best_candidate = (candidate_key, todo, normalized_status, request_record)
    if best_candidate is None:
        return None
    return best_candidate[1], best_candidate[2], best_candidate[3]


def mark_restart_checkpoint_backlog_selected(
    service: Any,
    sprint_state: dict[str, Any],
    *,
    backlog_id: str,
) -> None:
    normalized_backlog_id = str(backlog_id or "").strip()
    if not normalized_backlog_id:
        return
    backlog_item = service._load_backlog_item(normalized_backlog_id)
    if not backlog_item:
        return
    if str(backlog_item.get("status") or "").strip().lower() != "done":
        backlog_item["status"] = "selected"
        backlog_item["selected_in_sprint_id"] = str(sprint_state.get("sprint_id") or "")
        backlog_item["completed_in_sprint_id"] = ""
        service._clear_backlog_blockers(backlog_item)
        service._save_backlog_item(backlog_item)
    selected_items = list(sprint_state.get("selected_items") or [])
    updated_selected_items: list[dict[str, Any]] = []
    replaced = False
    for item in selected_items:
        if str(item.get("backlog_id") or "").strip() == normalized_backlog_id:
            merged = dict(item)
            if str(backlog_item.get("status") or "").strip().lower() != "done":
                merged["status"] = "selected"
                merged["selected_in_sprint_id"] = str(sprint_state.get("sprint_id") or "")
                merged["completed_in_sprint_id"] = ""
                service._clear_backlog_blockers(merged)
            updated_selected_items.append(merged)
            replaced = True
        else:
            updated_selected_items.append(item)
    if not replaced and backlog_item:
        updated_selected_items.append(dict(backlog_item))
    sprint_state["selected_items"] = updated_selected_items
    selected_backlog_ids = [
        str(item).strip()
        for item in (sprint_state.get("selected_backlog_ids") or [])
        if str(item).strip()
    ]
    if normalized_backlog_id not in selected_backlog_ids:
        selected_backlog_ids.append(normalized_backlog_id)
    sprint_state["selected_backlog_ids"] = selected_backlog_ids


def prepare_requested_restart_checkpoint(service: Any, sprint_state: dict[str, Any]) -> bool:
    requested_at = str(sprint_state.get("resume_from_checkpoint_requested_at") or "").strip()
    if not requested_at:
        return False
    sprint_state["resume_from_checkpoint_requested_at"] = ""
    sprint_state["last_resume_checkpoint_todo_id"] = ""
    sprint_state["last_resume_checkpoint_status"] = ""
    candidate = service._select_restart_checkpoint_todo(sprint_state)
    if candidate is None:
        return True
    todo, previous_status, request_record = candidate
    todo_id = str(todo.get("todo_id") or "").strip()
    previous_request_id = str(todo.get("request_id") or "").strip()
    backlog_id = str(todo.get("backlog_id") or "").strip()
    if previous_status == "blocked":
        if previous_request_id:
            todo["retry_of_request_id"] = previous_request_id
        todo["request_id"] = ""
        todo["status"] = "queued"
        todo["dependency_gate_bypass"] = "restart_checkpoint"
        todo["ended_at"] = ""
        todo["carry_over_backlog_id"] = ""
        todo["version_control_status"] = ""
        todo["version_control_paths"] = []
        todo["version_control_message"] = ""
        todo["version_control_error"] = ""
        service._mark_restart_checkpoint_backlog_selected(sprint_state, backlog_id=backlog_id)
        summary = "마지막 blocked todo를 재시도하도록 restart checkpoint를 복원했습니다."
    else:
        summary = "마지막 execution checkpoint부터 sprint를 재개합니다."
    todos = list(sprint_state.get("todos") or [])
    sprint_state["todos"] = [todo] + [
        item
        for item in todos
        if str(item.get("todo_id") or "").strip() != todo_id
    ]
    sprint_state["last_resume_checkpoint_todo_id"] = todo_id
    sprint_state["last_resume_checkpoint_status"] = previous_status
    service._append_sprint_event(
        str(sprint_state.get("sprint_id") or ""),
        event_type="restart_checkpoint",
        summary=summary,
        payload={
            "todo_id": todo_id,
            "backlog_id": backlog_id,
            "previous_status": previous_status,
            "request_id": previous_request_id,
            "checkpoint_request_updated_at": str(request_record.get("updated_at") or "").strip(),
        },
    )
    return True


async def resume_active_sprint(service: Any, sprint_id: str) -> None:
    async with service._sprint_resume_lock:
        sprint_state = service._load_sprint_state(sprint_id)
        if not sprint_state:
            LOGGER.warning("Clearing missing active sprint state: %s", sprint_id)
            service._finish_scheduler_after_sprint({"sprint_id": sprint_id})
            return
        status = str(sprint_state.get("status") or "").strip().lower()
        if status == "completed":
            service._finish_scheduler_after_sprint(sprint_state)
            return
        if service._is_resumable_blocked_sprint(sprint_state):
            sprint_state["ended_at"] = ""
            sprint_state.pop("reload_required", None)
            sprint_state.pop("reload_paths", None)
            sprint_state.pop("reload_message", None)
            sprint_state.pop("reload_restart_command", None)
            service._append_sprint_event(
                sprint_id,
                event_type="resumed",
                summary="blocked sprint를 같은 sprint_id로 재개했습니다.",
            )
            service._save_sprint_state(sprint_state)
            await service._continue_sprint(sprint_state, announce=False)
            return
        if status in {"failed", "blocked"}:
            service._finish_scheduler_after_sprint(sprint_state, clear_active=False)
            return
        LOGGER.info("Resuming active sprint %s with status=%s", sprint_id, status or "unknown")
        service._append_sprint_event(sprint_id, event_type="resumed", summary="오케스트레이터가 active sprint를 재개했습니다.")
        try:
            await service._continue_sprint(sprint_state, announce=False)
        except Exception as exc:
            await service._fail_sprint_due_to_exception(sprint_state, exc)


async def continue_manual_daily_sprint(
    service: Any,
    sprint_state: dict[str, Any],
    *,
    announce: bool,
) -> None:
    if str(sprint_state.get("phase") or "").strip() == "initial":
        if service._is_wrap_up_requested(sprint_state):
            sprint_state["phase"] = "wrap_up"
            service._save_sprint_state(sprint_state)
            await service._finalize_sprint(sprint_state)
            return
        ready = await service._run_initial_sprint_phase(sprint_state)
        if not ready:
            if service._is_wrap_up_requested(sprint_state):
                sprint_state["phase"] = "wrap_up"
                service._save_sprint_state(sprint_state)
                await service._finalize_sprint(sprint_state)
            return
        announce = True
    sprint_state["phase"] = "ongoing"
    sprint_state["status"] = "running"
    service._save_sprint_state(sprint_state)
    if announce:
        await service._send_sprint_kickoff(sprint_state)
        await service._send_sprint_todo_list(sprint_state)
    force_review = not bool(sprint_state.get("last_planner_review_at"))
    while True:
        if service._is_wrap_up_requested(sprint_state) or service._is_manual_sprint_cutoff_reached(sprint_state):
            sprint_state["phase"] = "wrap_up"
            service._save_sprint_state(sprint_state)
            await service._finalize_sprint(sprint_state)
            return
        await service._run_ongoing_sprint_review(sprint_state, force=force_review)
        force_review = False
        service._sync_manual_sprint_queue(sprint_state)
        sprint_state["todos"] = service._sort_sprint_todos(list(sprint_state.get("todos") or []))
        service._save_sprint_state(sprint_state)
        next_todo = next(
            (
                todo
                for todo in (sprint_state.get("todos") or [])
                if service._is_executable_todo_status(str(todo.get("status") or "").strip().lower())
                and sprint_todo_dependencies_satisfied(todo, list(sprint_state.get("todos") or []))
            ),
            None,
        )
        if next_todo is None:
            sprint_state["phase"] = "wrap_up"
            service._save_sprint_state(sprint_state)
            await service._finalize_sprint(sprint_state)
            return
        await service._execute_sprint_todo(sprint_state, next_todo)
        service._save_sprint_state(sprint_state)
        force_review = True


async def continue_sprint(
    service: Any,
    sprint_state: dict[str, Any],
    *,
    announce: bool,
) -> None:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    if not sprint_id:
        return
    if service._prepare_requested_restart_checkpoint(sprint_state):
        service._save_sprint_state(sprint_state)
    if service._sprint_uses_manual_flow(sprint_state):
        await service._continue_manual_daily_sprint(sprint_state, announce=announce)
        return
    dropped_ids = service._drop_non_actionable_backlog_items()
    repaired_ids = service._repair_non_actionable_carry_over_backlog_items()
    combined_pruned_ids = dropped_ids | repaired_ids
    if combined_pruned_ids:
        service._refresh_backlog_markdown()
    if service._prune_dropped_backlog_from_sprint(sprint_state, combined_pruned_ids):
        service._save_sprint_state(sprint_state)
    if service._is_wrap_up_requested(sprint_state):
        sprint_state["phase"] = "wrap_up"
        service._save_sprint_state(sprint_state)
        await service._finalize_sprint(sprint_state)
        return
    if not list(sprint_state.get("selected_items") or []):
        await service._complete_terminal_sprint(
            sprint_state,
            status="completed",
            closeout_status="no_selected_backlog",
            terminal_title="🛑 스프린트 종료",
            message="선택된 backlog가 없어 스프린트를 종료했습니다.",
        )
        return
    if not list(sprint_state.get("todos") or []):
        sprint_state["todos"] = [
            build_todo_item(item, owner_role="planner")
            for item in sprint_state.get("selected_items") or []
        ]
    sprint_state["todos"] = service._sort_sprint_todos(list(sprint_state.get("todos") or []))
    sprint_state["status"] = "running"
    service._save_sprint_state(sprint_state)
    if announce:
        await service._send_sprint_kickoff(sprint_state)
        await service._send_sprint_todo_list(sprint_state)
    for todo in sprint_state.get("todos") or []:
        if service._is_wrap_up_requested(sprint_state):
            sprint_state["phase"] = "wrap_up"
            service._save_sprint_state(sprint_state)
            await service._finalize_sprint(sprint_state)
            return
        if is_terminal_todo_status(str(todo.get("status") or "")):
            continue
        if not sprint_todo_dependencies_satisfied(todo, list(sprint_state.get("todos") or [])):
            continue
        await service._execute_sprint_todo(sprint_state, todo)
        service._save_sprint_state(sprint_state)
        if str(todo.get("status") or "").strip().lower() == "uncommitted":
            return
        if service._is_wrap_up_requested(sprint_state):
            sprint_state["phase"] = "wrap_up"
            service._save_sprint_state(sprint_state)
            await service._finalize_sprint(sprint_state)
            return
    await service._finalize_sprint(sprint_state)


async def finalize_sprint(service: Any, sprint_state: dict[str, Any]) -> None:
    from teams_runtime.workflows.sprints.reporting import build_sprint_closeout_result

    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    if not sprint_id:
        return
    sprint_state["status"] = "closeout"
    service._save_sprint_state(sprint_state)
    baseline = sprint_state.get("git_baseline") or {}
    documentation_closeout = service._inspect_sprint_documentation_closeout(sprint_state)
    if str(documentation_closeout.get("status") or "").strip() != "verified":
        closeout_result = build_sprint_closeout_result(
            sprint_state=sprint_state,
            status=str(documentation_closeout.get("status") or "planning_incomplete").strip(),
            message=str(documentation_closeout.get("message") or "").strip(),
            commit_count=0,
            commit_shas=[],
            representative_commit_sha="",
            uncommitted_paths=[],
        )
        closeout_result["repo_root"] = str(baseline.get("repo_root") or "")
        closeout_result["missing_sections"] = list(documentation_closeout.get("missing_sections") or [])
    else:
        inspect_closeout = getattr(service, "_inspect_git_sprint_closeout", None)
        closeout_result = await asyncio.to_thread(
            inspect_closeout,
            baseline,
            sprint_id,
        ) if callable(inspect_closeout) else await asyncio.to_thread(
            inspect_sprint_closeout,
            service.paths.project_workspace_root,
            baseline,
            sprint_id,
        )
    if closeout_result.get("status") == "pending_changes":
        version_control_result = await service._run_closeout_version_controller(
            sprint_state=sprint_state,
            closeout_result=closeout_result,
        )
        sprint_state["version_control_status"] = str(
            version_control_result.get("commit_status") or version_control_result.get("status") or ""
        ).strip()
        sprint_state["version_control_sha"] = str(version_control_result.get("commit_sha") or "").strip()
        sprint_state["version_control_paths"] = [
            str(item).strip()
            for item in (
                version_control_result.get("commit_paths")
                or version_control_result.get("changed_paths")
                or []
            )
            if str(item).strip()
        ]
        sprint_state["version_control_message"] = str(version_control_result.get("commit_message") or "").strip()
        sprint_state["version_control_error"] = str(version_control_result.get("error") or "").strip()
        sprint_state["auto_commit_status"] = sprint_state["version_control_status"]
        sprint_state["auto_commit_sha"] = sprint_state["version_control_sha"]
        sprint_state["auto_commit_paths"] = list(sprint_state["version_control_paths"])
        sprint_state["auto_commit_message"] = sprint_state["version_control_message"]
        if sprint_state["version_control_status"] in {"committed", "no_changes"}:
            inspect_closeout = getattr(service, "_inspect_git_sprint_closeout", None)
            closeout_result = await asyncio.to_thread(
                inspect_closeout,
                baseline,
                sprint_id,
            ) if callable(inspect_closeout) else await asyncio.to_thread(
                inspect_sprint_closeout,
                service.paths.project_workspace_root,
                baseline,
                sprint_id,
            )
        else:
            previous_repo_root = str(closeout_result.get("repo_root") or "")
            closeout_result = build_sprint_closeout_result(
                sprint_state=sprint_state,
                status="version_control_failed",
                message=(
                    "스프린트 closeout version_controller 단계에 실패했습니다. "
                    f"{str(version_control_result.get('summary') or version_control_result.get('error') or '').strip()}"
                ).strip(),
                commit_count=int(closeout_result.get("commit_count") or 0),
                commit_shas=[
                    str(item).strip()
                    for item in (closeout_result.get("commit_shas") or [])
                    if str(item).strip()
                ],
                representative_commit_sha=str(closeout_result.get("representative_commit_sha") or "").strip(),
                uncommitted_paths=[
                    str(item).strip()
                    for item in (
                        version_control_result.get("commit_paths")
                        or version_control_result.get("changed_paths")
                        or closeout_result.get("uncommitted_paths")
                        or []
                    )
                    if str(item).strip()
                ],
            )
            closeout_result["repo_root"] = previous_repo_root
    else:
        sprint_state["version_control_status"] = "not_needed"
        sprint_state["version_control_sha"] = ""
        sprint_state["version_control_paths"] = []
        sprint_state["version_control_message"] = ""
        sprint_state["version_control_error"] = ""
        sprint_state["auto_commit_status"] = "not_needed"
        sprint_state["auto_commit_sha"] = ""
        sprint_state["auto_commit_paths"] = []
        sprint_state["auto_commit_message"] = ""
    await service._complete_terminal_sprint_from_closeout_result(
        sprint_state,
        closeout_result=closeout_result,
        terminal_title="",
    )


async def fail_sprint_due_to_exception(service: Any, sprint_state: dict[str, Any], exc: Exception) -> None:
    from teams_runtime.workflows.sprints.reporting import build_sprint_closeout_result

    sprint_id = str(sprint_state.get("sprint_id") or "").strip() or "unknown"
    LOGGER.exception("Autonomous sprint failed unexpectedly: %s", sprint_id)
    service._append_sprint_event(
        sprint_id,
        event_type="failed",
        summary="스프린트 실행 중 예외가 발생했습니다.",
        payload={"error": str(exc)},
    )
    closeout_result = build_sprint_closeout_result(
        sprint_state=sprint_state,
        status="failed",
        message=str(exc),
        representative_commit_sha="",
    )
    await service._complete_terminal_sprint_from_closeout_result(
        sprint_state,
        closeout_result=closeout_result,
        terminal_title="⚠️ 스프린트 실패",
    )


def create_internal_request_record(
    service: Any,
    sprint_state: dict[str, Any],
    todo: dict[str, Any],
    backlog_item: dict[str, Any],
) -> dict[str, Any]:
    request_id = new_request_id()
    workflow_state = service._initial_workflow_state_for_internal_request()
    initial_role = str(
        workflow_state.get("phase_owner")
        or todo.get("owner_role")
        or "planner"
    ).strip() or "planner"
    record = {
        "request_id": request_id,
        "status": "queued",
        "intent": "route",
        "urgency": "normal",
        "scope": str(backlog_item.get("scope") or backlog_item.get("title") or "").strip(),
        "body": str(backlog_item.get("summary") or backlog_item.get("scope") or "").strip(),
        "artifacts": [],
        "params": {
            "_teams_kind": "sprint_internal",
            "sprint_id": sprint_state.get("sprint_id") or "",
            "backlog_id": todo.get("backlog_id") or "",
            "todo_id": todo.get("todo_id") or "",
            "workflow": workflow_state,
        },
        "current_role": "orchestrator",
        "next_role": initial_role,
        "owner_role": "orchestrator",
        "sprint_id": str(sprint_state.get("sprint_id") or ""),
        "backlog_id": str(todo.get("backlog_id") or ""),
        "todo_id": str(todo.get("todo_id") or ""),
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "fingerprint": build_request_fingerprint(
            author_id="sprint-runner",
            channel_id=str(sprint_state.get("sprint_id") or ""),
            intent="route",
            scope=str(backlog_item.get("scope") or backlog_item.get("title") or ""),
        ),
        "reply_route": {},
        "events": [],
        "result": {},
        "git_baseline": capture_git_baseline(service.paths.project_workspace_root),
        "version_control_status": "",
        "version_control_sha": "",
        "version_control_paths": [],
        "version_control_message": "",
        "version_control_error": "",
        "task_commit_status": "",
        "task_commit_sha": "",
        "task_commit_paths": [],
        "task_commit_message": "",
        "visited_roles": [],
    }
    append_request_event(
        record,
        event_type="created",
        actor="sprint_runner",
        summary="스프린트 내부 요청을 생성했습니다.",
    )
    service._save_request(record)
    service._append_role_history(
        "orchestrator",
        record,
        event_type="created",
        summary="스프린트 내부 요청을 생성했습니다.",
    )
    return record


async def run_internal_request_chain(
    service: Any,
    *,
    sprint_id: str,
    request_record: dict[str, Any],
    initial_role: str,
) -> dict[str, Any]:
    workflow_state = service._request_workflow_state(request_record)
    seeded_initial_role = str(workflow_state.get("phase_owner") or "").strip() if workflow_state else ""
    selection = service._build_governed_routing_selection(
        request_record,
        {},
        current_role="orchestrator",
        preferred_role=seeded_initial_role or str(initial_role or "").strip(),
        selection_source="sprint_initial",
    )
    next_role = str(selection.get("selected_role") or "").strip() or seeded_initial_role or "planner"
    current_status = str(request_record.get("status") or "").strip().lower()
    if current_status in {"queued", ""}:
        request_record["status"] = "delegated"
        request_record["current_role"] = next_role
        request_record["next_role"] = next_role
        request_record["routing_context"] = service._build_routing_context(
            next_role,
            reason=f"Selected {next_role} as the current best role for this sprint step.",
            preferred_role=str(selection.get("preferred_role") or ""),
            selection_source="sprint_initial",
            matched_signals=[
                str(item).strip()
                for item in (selection.get("matched_signals") or [])
                if str(item).strip()
            ],
            override_reason=str(selection.get("override_reason") or ""),
            matched_strongest_domains=[
                str(item).strip()
                for item in (selection.get("matched_strongest_domains") or [])
                if str(item).strip()
            ],
            matched_preferred_skills=[
                str(item).strip()
                for item in (selection.get("matched_preferred_skills") or [])
                if str(item).strip()
            ],
            matched_behavior_traits=[
                str(item).strip()
                for item in (selection.get("matched_behavior_traits") or [])
                if str(item).strip()
            ],
            policy_source=str(selection.get("policy_source") or ""),
            routing_phase=str(selection.get("routing_phase") or ""),
            request_state_class=str(selection.get("request_state_class") or ""),
            score_total=int(selection.get("score_total") or 0),
            score_breakdown=dict(selection.get("score_breakdown") or {}),
            candidate_summary=list(selection.get("candidate_summary") or []),
        )
        append_request_event(
            request_record,
            event_type="delegated",
            actor="orchestrator",
            summary=f"{next_role} 역할로 위임했습니다.",
            payload={"routing_context": dict(request_record.get("routing_context") or {})},
        )
        service._save_request(request_record)
        service._append_role_history(
            "orchestrator",
            request_record,
            event_type="delegated",
            summary=f"{next_role} 역할로 위임했습니다.",
        )
        service._record_internal_sprint_activity(
            request_record,
            event_type="role_delegated",
            role="orchestrator",
            status=str(request_record.get("status") or ""),
            summary=f"{next_role} 역할로 위임했습니다.",
            payload=service._build_internal_sprint_delegation_payload(request_record, next_role),
        )
        await service._delegate_request(request_record, next_role)
    return await service._wait_for_internal_request_result(str(request_record.get("request_id") or ""))


async def resume_uncommitted_sprint_todo(
    service: Any,
    *,
    sprint_state: dict[str, Any],
    todo: dict[str, Any],
    request_record: dict[str, Any],
) -> dict[str, Any]:
    persisted_result = dict(request_record.get("result") or {})
    if not persisted_result:
        persisted_result = {
            "request_id": str(request_record.get("request_id") or todo.get("request_id") or ""),
            "role": str(request_record.get("current_role") or "orchestrator"),
            "status": "uncommitted",
            "summary": str(request_record.get("task_commit_summary") or todo.get("summary") or todo.get("title") or ""),
            "insights": [],
            "proposals": {},
            "artifacts": [
                str(item)
                for item in (request_record.get("artifacts") or todo.get("artifacts") or [])
                if str(item).strip()
            ],
            "next_role": "",
            "error": str(request_record.get("version_control_error") or ""),
        }
    persisted_result.setdefault("request_id", str(request_record.get("request_id") or todo.get("request_id") or ""))
    persisted_result.setdefault("role", str(request_record.get("current_role") or "orchestrator"))
    persisted_result.setdefault("insights", [])
    persisted_result.setdefault("proposals", {})
    persisted_result.setdefault(
        "artifacts",
        [
            str(item)
            for item in (request_record.get("artifacts") or todo.get("artifacts") or [])
            if str(item).strip()
        ],
    )
    persisted_result.setdefault("next_role", "")
    if str(persisted_result.get("status") or "").strip().lower() not in {"completed", "uncommitted"}:
        persisted_result["status"] = "uncommitted"
    return await service._enforce_task_commit_for_completed_todo(
        sprint_state=sprint_state,
        todo=todo,
        request_record=request_record,
        result=persisted_result,
    )


async def execute_sprint_todo(service: Any, sprint_state: dict[str, Any], todo: dict[str, Any]) -> None:
    backlog_item = service._load_backlog_item(str(todo.get("backlog_id") or ""))
    bypass_dependency_gate = str(todo.get("dependency_gate_bypass") or "").strip() == "restart_checkpoint"
    waiting_on = (
        []
        if bypass_dependency_gate
        else sprint_todo_dependency_waiting_on(todo, list(sprint_state.get("todos") or []))
    )
    if waiting_on:
        service._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="todo_dependency_wait",
            summary=str(todo.get("title") or ""),
            payload={
                "todo_id": todo.get("todo_id") or "",
                "backlog_id": todo.get("backlog_id") or "",
                "waiting_on": [
                    {
                        "todo_id": str(item.get("todo_id") or ""),
                        "backlog_id": str(item.get("backlog_id") or ""),
                        "priority_rank": int(item.get("priority_rank") or 0),
                        "status": str(item.get("status") or ""),
                    }
                    for item in waiting_on
                ],
            },
        )
        return
    request_record: dict[str, Any] = {}
    existing_request_id = str(todo.get("request_id") or "").strip()
    if existing_request_id:
        request_record = service._load_request(existing_request_id)
    existing_request_status = str(request_record.get("status") or "").strip().lower()
    recovering_uncommitted = existing_request_status == "uncommitted" or str(todo.get("status") or "").strip().lower() == "uncommitted"
    if not recovering_uncommitted:
        todo["status"] = "running"
        todo["started_at"] = str(todo.get("started_at") or utc_now_iso())
        service._save_sprint_state(sprint_state)
        service._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="todo_started",
            summary=str(todo.get("title") or ""),
            payload={"todo_id": todo.get("todo_id") or "", "backlog_id": todo.get("backlog_id") or ""},
        )
    if request_record and not service._is_terminal_internal_request_status(existing_request_status):
        todo["request_id"] = request_record["request_id"]
        service._save_sprint_state(sprint_state)
    elif not request_record:
        request_record = service._create_internal_request_record(sprint_state, todo, backlog_item)
        todo["request_id"] = request_record["request_id"]
        service._save_sprint_state(sprint_state)
    if recovering_uncommitted and request_record:
        result = await service._resume_uncommitted_sprint_todo(
            sprint_state=sprint_state,
            todo=todo,
            request_record=request_record,
        )
    else:
        result = await service._run_internal_request_chain(
            sprint_id=str(sprint_state.get("sprint_id") or ""),
            request_record=request_record,
            initial_role=str(todo.get("owner_role") or "planner"),
        )
        request_record = service._load_request(str(todo.get("request_id") or "")) or request_record
        result = await service._enforce_task_commit_for_completed_todo(
            sprint_state=sprint_state,
            todo=todo,
            request_record=request_record,
            result=result,
        )
    todo["artifacts"] = service._normalize_sprint_todo_artifacts(
        result.get("artifacts"),
        workflow_state=service._request_workflow_state(request_record),
    )
    todo["summary"] = str(result.get("summary") or "").strip()
    todo["updated_at"] = utc_now_iso()
    todo["version_control_status"] = str(result.get("version_control_status") or "").strip()
    todo["version_control_paths"] = [
        str(item).strip()
        for item in (result.get("version_control_paths") or [])
        if str(item).strip()
    ]
    todo["version_control_message"] = str(result.get("version_control_message") or "").strip()
    todo["version_control_error"] = str(result.get("version_control_error") or "").strip()
    todo["ended_at"] = utc_now_iso()
    status = str(result.get("status") or "").strip().lower()
    if status in {"completed", "committed"}:
        todo["status"] = status
        backlog_item["status"] = "done"
        service._clear_backlog_blockers(backlog_item)
        backlog_item["completed_in_sprint_id"] = str(sprint_state.get("sprint_id") or "")
        service._save_backlog_item(backlog_item)
    elif status == "uncommitted":
        todo["status"] = "uncommitted"
        backlog_item["status"] = "blocked"
        backlog_item["selected_in_sprint_id"] = ""
        backlog_item["completed_in_sprint_id"] = ""
        backlog_item["blocked_reason"] = str(result.get("summary") or result.get("error") or "").strip()
        backlog_item["blocked_by_role"] = "version_controller"
        backlog_item["recommended_next_step"] = "version_controller recovery 또는 수동 git 정리가 필요합니다."
        backlog_item["required_inputs"] = []
        service._save_backlog_item(backlog_item)
        todo["carry_over_backlog_id"] = str(backlog_item.get("backlog_id") or "")
    elif status == "blocked":
        todo["status"] = "blocked"
        backlog_item["status"] = "blocked"
        backlog_item["selected_in_sprint_id"] = ""
        backlog_item["completed_in_sprint_id"] = ""
        backlog_item["blocked_reason"] = str(result.get("summary") or result.get("error") or "").strip()
        backlog_item["blocked_by_role"] = str(result.get("role") or request_record.get("current_role") or "").strip()
        proposals = dict(result.get("proposals") or {})
        backlog_item["recommended_next_step"] = str(proposals.get("recommended_next_step") or "").strip()
        backlog_item["required_inputs"] = [
            str(value).strip()
            for value in (proposals.get("required_inputs") or [])
            if str(value).strip()
        ]
        service._save_backlog_item(backlog_item)
        todo["carry_over_backlog_id"] = str(backlog_item.get("backlog_id") or "")
    else:
        todo["status"] = "failed"
        carry_over = build_backlog_item(
            title=str(todo.get("title") or ""),
            summary=str(result.get("summary") or result.get("error") or "carry-over"),
            kind=service._classify_backlog_kind("", str(todo.get("title") or ""), str(result.get("summary") or "")),
            source="carry_over",
            scope=str(request_record.get("scope") or ""),
            acceptance_criteria=list(todo.get("acceptance_criteria") or []),
            milestone_title=str(todo.get("milestone_title") or ""),
            priority_rank=int(todo.get("priority_rank") or 0),
            origin={"sprint_id": sprint_state.get("sprint_id") or "", "todo_id": todo.get("todo_id") or ""},
        )
        carry_over["carry_over_of"] = str(backlog_item.get("backlog_id") or "")
        carry_over["fingerprint"] = service._build_backlog_fingerprint(
            title=str(carry_over.get("title") or ""),
            scope=str(carry_over.get("scope") or ""),
            kind=str(carry_over.get("kind") or ""),
        )
        backlog_item["status"] = "carried_over"
        service._save_backlog_item(backlog_item)
        service._save_backlog_item(carry_over)
        todo["carry_over_backlog_id"] = carry_over["backlog_id"]
    service._synchronize_sprint_todo_backlog_state(sprint_state)
    service._append_sprint_event(
        str(sprint_state.get("sprint_id") or ""),
        event_type="todo_completed",
        summary=str(todo.get("summary") or todo.get("title") or ""),
        payload={"todo_id": todo.get("todo_id") or "", "status": todo.get("status") or ""},
    )
    await service._send_sprint_report(
        title=f"TODO {todo.get('status') or ''}",
        body=(
            f"{todo.get('title') or ''}\n"
            f"request_id={todo.get('request_id') or ''}\n"
            f"summary={todo.get('summary') or ''}\n"
            f"version_control_status={todo.get('version_control_status') or 'not_needed'}\n"
            f"version_control_paths={', '.join(str(item).strip() for item in (todo.get('version_control_paths') or []) if str(item).strip()) or 'N/A'}"
        ),
        judgment=str(todo.get("summary") or todo.get("title") or "").strip(),
        commit_message=(
            first_meaningful_text(
                todo.get("version_control_message"),
                request_record.get("task_commit_message"),
                request_record.get("version_control_message"),
            )
            if str(todo.get("status") or "").strip().lower() == "committed"
            else ""
        ),
        related_artifacts=[
            str(item).strip()
            for item in (todo.get("artifacts") or [])
            if str(item).strip()
        ],
    )


async def enforce_task_commit_for_completed_todo(
    service: Any,
    *,
    sprint_state: dict[str, Any],
    todo: dict[str, Any],
    request_record: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    result_status = str(result.get("status") or "").strip().lower()
    request_status = str(request_record.get("status") or "").strip().lower()
    recovering_uncommitted = request_status == "uncommitted" or result_status == "uncommitted"
    if result_status not in {"completed", "uncommitted"}:
        return result
    task_commit_summary = str(
        request_record.get("task_commit_summary")
        or result.get("task_commit_summary")
        or result.get("summary")
        or todo.get("title")
        or request_record.get("scope")
        or ""
    ).strip()
    request_record["task_commit_summary"] = task_commit_summary
    result["task_commit_summary"] = task_commit_summary
    inspection = service._inspect_task_version_control_state(request_record)
    inspected_paths = [
        str(item).strip()
        for item in (inspection.get("changed_paths") or [])
        if str(item).strip()
    ]
    if inspection.get("status") == "no_changes":
        request_record["status"] = "completed"
        request_record["version_control_status"] = "no_changes"
        request_record["version_control_sha"] = ""
        request_record["version_control_paths"] = []
        request_record["version_control_message"] = ""
        request_record["version_control_error"] = ""
        request_record["task_commit_status"] = "no_changes"
        request_record["task_commit_sha"] = ""
        request_record["task_commit_paths"] = []
        request_record["task_commit_message"] = ""
        result["status"] = "completed"
        result["summary"] = task_commit_summary or str(result.get("summary") or "").strip()
        result["error"] = ""
        result["version_control_status"] = "no_changes"
        result["version_control_sha"] = ""
        result["version_control_paths"] = []
        result["version_control_message"] = ""
        result["version_control_error"] = ""
        result["task_commit_status"] = "no_changes"
        result["task_commit_sha"] = ""
        result["task_commit_paths"] = []
        result["task_commit_message"] = ""
        tracked_artifacts = service._collect_artifact_candidates(
            request_record.get("artifacts"),
            result.get("artifacts"),
            todo.get("artifacts"),
            result.get("version_control_paths"),
            result.get("task_commit_paths"),
        )
        result["artifacts"] = tracked_artifacts
        request_record["artifacts"] = tracked_artifacts
        request_record["result"] = result
        service._save_request(request_record)
        return result
    if inspection.get("status") == "pending_changes" or recovering_uncommitted:
        pending_paths = inspected_paths or [
            str(item).strip()
            for item in (
                request_record.get("version_control_paths")
                or request_record.get("task_commit_paths")
                or result.get("version_control_paths")
                or result.get("task_commit_paths")
                or []
            )
            if str(item).strip()
        ]
        request_record["status"] = "uncommitted"
        request_record["current_role"] = "orchestrator"
        request_record["next_role"] = ""
        request_record["version_control_status"] = "requested"
        request_record["version_control_sha"] = ""
        request_record["version_control_paths"] = pending_paths
        request_record["version_control_message"] = ""
        request_record["version_control_error"] = ""
        request_record["task_commit_status"] = "requested"
        request_record["task_commit_sha"] = ""
        request_record["task_commit_paths"] = pending_paths
        request_record["task_commit_message"] = ""
        result["status"] = "uncommitted"
        result["version_control_status"] = "requested"
        result["version_control_sha"] = ""
        result["version_control_paths"] = pending_paths
        result["version_control_message"] = ""
        result["version_control_error"] = ""
        result["task_commit_status"] = "requested"
        result["task_commit_sha"] = ""
        result["task_commit_paths"] = pending_paths
        result["task_commit_message"] = ""
        tracked_artifacts = service._collect_artifact_candidates(
            request_record.get("artifacts"),
            result.get("artifacts"),
            todo.get("artifacts"),
            inspected_paths,
            pending_paths,
        )
        result["artifacts"] = tracked_artifacts
        request_record["artifacts"] = tracked_artifacts
        request_record["result"] = result
        todo["status"] = "uncommitted"
        todo["version_control_status"] = "requested"
        todo["version_control_paths"] = list(pending_paths)
        todo["version_control_message"] = ""
        todo["version_control_error"] = ""
        service._save_sprint_state(sprint_state)
        service._save_request(request_record)
    version_control_result = await service._run_task_version_controller(
        sprint_state=sprint_state,
        todo=todo,
        request_record=request_record,
        result=result,
    )
    commit_status = str(version_control_result.get("commit_status") or version_control_result.get("status") or "").strip()
    commit_sha = str(version_control_result.get("commit_sha") or "").strip()
    commit_paths = [
        str(item).strip()
        for item in (version_control_result.get("commit_paths") or version_control_result.get("changed_paths") or [])
        if str(item).strip()
    ]
    commit_message = str(version_control_result.get("commit_message") or "").strip()
    version_control_error = str(version_control_result.get("error") or "").strip()
    version_control_summary = str(version_control_result.get("summary") or "").strip()
    request_record["version_control_status"] = commit_status
    request_record["version_control_sha"] = commit_sha
    request_record["version_control_paths"] = commit_paths
    request_record["version_control_message"] = commit_message
    request_record["version_control_error"] = version_control_error
    request_record["task_commit_status"] = commit_status
    request_record["task_commit_sha"] = commit_sha
    request_record["task_commit_paths"] = commit_paths
    request_record["task_commit_message"] = commit_message
    result["version_control_status"] = commit_status
    result["version_control_sha"] = commit_sha
    result["version_control_paths"] = commit_paths
    result["version_control_message"] = commit_message
    result["version_control_error"] = version_control_error
    result["task_commit_status"] = commit_status
    result["task_commit_sha"] = commit_sha
    result["task_commit_paths"] = commit_paths
    result["task_commit_message"] = commit_message
    if commit_status in {"failed", "no_repo"} or str(version_control_result.get("status") or "").strip().lower() in {"blocked", "failed"}:
        failure_summary = (
            "Task 완료 직전 version_controller 커밋 단계에 실패했습니다. "
            f"{version_control_summary or version_control_error}"
        ).strip()
        tracked_artifacts = service._collect_artifact_candidates(
            request_record.get("artifacts"),
            result.get("artifacts"),
            todo.get("artifacts"),
            commit_paths,
            request_record.get("version_control_paths"),
            request_record.get("task_commit_paths"),
        )
        result["artifacts"] = tracked_artifacts
        request_record["artifacts"] = tracked_artifacts
        request_record["status"] = "uncommitted" if inspected_paths or commit_paths or recovering_uncommitted else "blocked"
        request_record["result"] = {
            **result,
            "status": "uncommitted" if inspected_paths or commit_paths or recovering_uncommitted else "blocked",
            "summary": failure_summary,
            "error": version_control_error or version_control_summary,
        }
        service._save_request(request_record)
        return dict(request_record["result"])
    final_status = "committed" if commit_status == "committed" else "completed"
    request_record["status"] = final_status
    result["status"] = final_status
    result["summary"] = task_commit_summary or str(result.get("summary") or "").strip()
    result["error"] = ""
    tracked_artifacts = service._collect_artifact_candidates(
        request_record.get("artifacts"),
        result.get("artifacts"),
        todo.get("artifacts"),
        commit_paths,
        request_record.get("version_control_paths"),
        request_record.get("task_commit_paths"),
        inspected_paths,
    )
    result["artifacts"] = tracked_artifacts
    request_record["artifacts"] = tracked_artifacts
    request_record["result"] = result
    service._save_request(request_record)
    return result
