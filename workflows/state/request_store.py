from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from teams_runtime.shared.models import TERMINAL_REQUEST_STATUSES, RequestRecord
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import (
    iter_json_records,
    normalize_runtime_datetime,
    read_json,
    utc_now_iso,
    write_json,
)


INTERNAL_TERMINAL_REQUEST_STATUSES = {
    "completed",
    "committed",
    "failed",
    "blocked",
    "cancelled",
}
RECENT_BLOCKED_BACKLOG_REVIEW_WINDOW_SECONDS = 24 * 60 * 60


def iter_request_records(paths: RuntimePaths) -> list[RequestRecord]:
    return list(iter_json_records(paths.requests_dir))


def load_request(paths: RuntimePaths, request_id: str) -> RequestRecord:
    normalized_request_id = str(request_id or "").strip()
    if not normalized_request_id:
        return {}
    return read_json(paths.request_file(normalized_request_id))


def save_request(
    paths: RuntimePaths,
    request_record: RequestRecord,
    *,
    update_timestamp: bool = True,
) -> None:
    request_id = str(request_record.get("request_id") or "").strip()
    if not request_id:
        return
    if update_timestamp:
        request_record["updated_at"] = utc_now_iso()
    write_json(paths.request_file(request_id), request_record)


def append_request_event(
    request_record: dict[str, Any],
    *,
    event_type: str,
    actor: str,
    summary: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "timestamp": utc_now_iso(),
        "type": event_type,
        "actor": actor,
        "summary": summary,
    }
    if payload:
        event["payload"] = payload
    request_record.setdefault("events", []).append(event)
    request_record["updated_at"] = utc_now_iso()
    return request_record


def is_terminal_request(record: dict[str, Any]) -> bool:
    return str(record.get("status") or "").strip().lower() in TERMINAL_REQUEST_STATUSES


def is_terminal_internal_request_status(status: str) -> bool:
    return str(status or "").strip().lower() in INTERNAL_TERMINAL_REQUEST_STATUSES


def _request_kind(request_record: dict[str, Any]) -> str:
    params = dict(request_record.get("params") or {})
    return str(params.get("_teams_kind") or "").strip()


def is_internal_sprint_request(request_record: dict[str, Any]) -> bool:
    return _request_kind(request_record) == "sprint_internal"


def is_blocked_backlog_review_request(request_record: dict[str, Any]) -> bool:
    return _request_kind(request_record) == "blocked_backlog_review"


def is_planner_backlog_review_request(request_record: dict[str, Any]) -> bool:
    return is_blocked_backlog_review_request(request_record)


def find_open_request_by_fingerprint(
    paths: RuntimePaths,
    *,
    fingerprint: str,
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    normalized = str(fingerprint or "").strip()
    if not normalized:
        return {}
    for request_record in iter_request_records(paths):
        if not predicate(request_record):
            continue
        if str(request_record.get("fingerprint") or "").strip() != normalized:
            continue
        status = str(request_record.get("status") or "").strip().lower()
        if is_terminal_internal_request_status(status):
            continue
        return request_record
    return {}


def find_open_blocked_backlog_review_request(paths: RuntimePaths, fingerprint: str) -> dict[str, Any]:
    return find_open_request_by_fingerprint(
        paths,
        fingerprint=fingerprint,
        predicate=is_blocked_backlog_review_request,
    )


def find_open_blocked_backlog_review_requests(paths: RuntimePaths) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for request_record in iter_request_records(paths):
        if not is_blocked_backlog_review_request(request_record):
            continue
        status = str(request_record.get("status") or "").strip().lower()
        if is_terminal_internal_request_status(status):
            continue
        records.append(request_record)
    records.sort(key=lambda record: (str(record.get("created_at") or ""), str(record.get("request_id") or "")))
    return records


def _request_record_timestamp(request_record: dict[str, Any]) -> datetime | None:
    for field_name in ("updated_at", "created_at"):
        raw = str(request_record.get(field_name) or "").strip()
        if not raw:
            continue
        try:
            return normalize_runtime_datetime(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            continue
    return None


def find_recent_terminal_request_by_fingerprint(
    paths: RuntimePaths,
    *,
    fingerprint: str,
    predicate: Callable[[dict[str, Any]], bool],
    recent_seconds: float,
) -> dict[str, Any]:
    normalized = str(fingerprint or "").strip()
    if not normalized or recent_seconds <= 0:
        return {}
    cutoff = normalize_runtime_datetime() - timedelta(seconds=float(recent_seconds))
    latest_request: dict[str, Any] = {}
    latest_timestamp: datetime | None = None
    for request_record in iter_request_records(paths):
        if not predicate(request_record):
            continue
        if str(request_record.get("fingerprint") or "").strip() != normalized:
            continue
        status = str(request_record.get("status") or "").strip().lower()
        if not is_terminal_internal_request_status(status):
            continue
        timestamp = _request_record_timestamp(request_record)
        if timestamp is None or timestamp < cutoff:
            continue
        if latest_timestamp is None or timestamp >= latest_timestamp:
            latest_request = request_record
            latest_timestamp = timestamp
    return latest_request


def find_recent_terminal_blocked_backlog_review_request(
    paths: RuntimePaths,
    fingerprint: str,
    *,
    recent_seconds: float = RECENT_BLOCKED_BACKLOG_REVIEW_WINDOW_SECONDS,
) -> dict[str, Any]:
    return find_recent_terminal_request_by_fingerprint(
        paths,
        fingerprint=fingerprint,
        predicate=is_blocked_backlog_review_request,
        recent_seconds=recent_seconds,
    )


def iter_sprint_task_request_records(paths: RuntimePaths, sprint_id: str) -> list[dict[str, Any]]:
    normalized_sprint_id = str(sprint_id or "").strip()
    if not normalized_sprint_id:
        return []
    records: list[dict[str, Any]] = []
    for record in iter_request_records(paths):
        if not is_internal_sprint_request(record):
            continue
        params = dict(record.get("params") or {})
        request_sprint_id = str(record.get("sprint_id") or params.get("sprint_id") or "").strip()
        if request_sprint_id != normalized_sprint_id:
            continue
        backlog_id = str(record.get("backlog_id") or params.get("backlog_id") or "").strip()
        todo_id = str(record.get("todo_id") or params.get("todo_id") or "").strip()
        if not backlog_id and not todo_id:
            continue
        records.append(record)
    records.sort(key=lambda record: (str(record.get("created_at") or ""), str(record.get("request_id") or "")))
    return records


def build_blocked_backlog_review_request_record(
    *,
    request_id: str,
    candidates: list[dict[str, Any]],
    artifact_hint: str,
    fingerprint: str,
) -> dict[str, Any]:
    record = {
        "request_id": request_id,
        "status": "queued",
        "intent": "plan",
        "urgency": "normal",
        "scope": "autonomous blocked backlog review",
        "body": (
            "Current blocked backlog is not automatically eligible for future sprint selection. "
            "Review these blocked items, decide which ones should remain blocked versus reopen to pending, "
            "persist any accepted backlog state changes directly, clear blocker metadata when reopening, "
            "and do not mark work selected in this request. "
            "If every candidate is already done, selected/running, missing, or otherwise no longer blocked, "
            "return proposals.blocked_backlog_review.decision=no_action_terminal with buckets "
            "already_done, already_selected_or_running, missing_or_empty, and still_blocked, "
            "and do not include backlog_writes."
        ),
        "artifacts": [artifact_hint],
        "params": {
            "_teams_kind": "blocked_backlog_review",
            "candidate_count": len(candidates),
            "blocked_backlog_candidates": candidates,
        },
        "current_role": "orchestrator",
        "next_role": "planner",
        "owner_role": "orchestrator",
        "sprint_id": "",
        "created_at": utc_now_iso(),
        "updated_at": utc_now_iso(),
        "fingerprint": str(fingerprint or "").strip(),
        "reply_route": {},
        "events": [],
        "result": {},
    }
    append_request_event(
        record,
        event_type="created",
        actor="orchestrator",
        summary="blocked backlog 재검토를 위한 planner review 요청을 생성했습니다.",
    )
    return record
