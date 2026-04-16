from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from teams_runtime.models import TERMINAL_REQUEST_STATUSES

RUNTIME_TIMEZONE = ZoneInfo("Asia/Seoul")
TIMESTAMP_FIELD_NAMES = {
    "timestamp",
    "created_at",
    "updated_at",
    "last_used_at",
    "started_at",
    "ended_at",
    "last_started_at",
    "last_completed_at",
    "last_skipped_at",
    "next_slot_at",
    "deferred_slot_at",
}


def runtime_now() -> datetime:
    return datetime.now(RUNTIME_TIMEZONE)


def normalize_runtime_datetime(value: datetime | None = None) -> datetime:
    current = value or runtime_now()
    if current.tzinfo is None:
        return current.replace(tzinfo=RUNTIME_TIMEZONE)
    return current.astimezone(RUNTIME_TIMEZONE)


def runtime_now_iso() -> str:
    return runtime_now().isoformat()


def datetime_to_runtime_iso(value: datetime | None) -> str:
    return normalize_runtime_datetime(value).isoformat()


def utc_now_iso() -> str:
    # Kept for compatibility with older call sites; runtime timestamps are stored in KST.
    return runtime_now_iso()


def normalize_runtime_timestamp_value(field_name: str, value: Any) -> Any:
    if field_name not in TIMESTAMP_FIELD_NAMES or not isinstance(value, str):
        return value
    normalized = value.strip()
    if not normalized:
        return value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return value
    return datetime_to_runtime_iso(parsed)


def normalize_runtime_timestamps(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            str(key): normalize_runtime_timestamps(
                normalize_runtime_timestamp_value(str(key), value)
            )
            for key, value in payload.items()
        }
    if isinstance(payload, list):
        return [normalize_runtime_timestamps(item) for item in payload]
    return payload


def read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        return {}
    normalized = normalize_runtime_timestamps(payload)
    return normalized if isinstance(normalized, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def iter_jsonl_records(path: Path) -> Iterable[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        normalized = str(line).strip()
        if not normalized:
            continue
        try:
            payload = json.loads(normalized)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            normalized_payload = normalize_runtime_timestamps(payload)
            if isinstance(normalized_payload, dict):
                records.append(normalized_payload)
    return records


def iter_json_records(directory: Path) -> Iterable[dict[str, Any]]:
    if not directory.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        payload = read_json(path)
        if payload:
            records.append(payload)
    return records


def new_request_id(now: datetime | None = None) -> str:
    current = normalize_runtime_datetime(now)
    return f"{current.strftime('%Y%m%d')}-{secrets.token_hex(4)}"


def new_backlog_id(now: datetime | None = None) -> str:
    current = normalize_runtime_datetime(now)
    return f"backlog-{current.strftime('%Y%m%d')}-{secrets.token_hex(4)}"


def new_todo_id(now: datetime | None = None) -> str:
    current = normalize_runtime_datetime(now)
    return f"todo-{current.strftime('%H%M%S')}-{secrets.token_hex(3)}"


def build_request_fingerprint(
    *,
    author_id: str,
    channel_id: str,
    intent: str,
    scope: str,
) -> str:
    normalized = "|".join(
        [
            str(author_id).strip(),
            str(channel_id).strip(),
            " ".join(str(intent).strip().lower().split()),
            " ".join(str(scope).strip().lower().split()),
        ]
    )
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


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
