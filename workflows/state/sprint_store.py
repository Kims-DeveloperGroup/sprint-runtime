from __future__ import annotations

from typing import Any

from teams_runtime.shared.formatting import render_current_sprint_markdown
from teams_runtime.shared.models import SprintState
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import (
    append_jsonl,
    iter_json_records,
    iter_jsonl_records,
    read_json,
    utc_now_iso,
    write_json,
)


def iter_sprint_states(paths: RuntimePaths) -> list[SprintState]:
    return list(iter_json_records(paths.sprints_dir))


def load_sprint_state(paths: RuntimePaths, sprint_id: str) -> SprintState:
    normalized_sprint_id = str(sprint_id or "").strip()
    if not normalized_sprint_id:
        return {}
    return read_json(paths.sprint_file(normalized_sprint_id))


def save_sprint_state(
    paths: RuntimePaths,
    sprint_state: SprintState | dict[str, Any],
    *,
    update_timestamp: bool = False,
    write_current_sprint: bool = False,
) -> None:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    if not sprint_id:
        return
    if update_timestamp:
        sprint_state["updated_at"] = utc_now_iso()
    write_json(paths.sprint_file(sprint_id), sprint_state)
    if write_current_sprint:
        paths.current_sprint_file.write_text(
            render_current_sprint_markdown(sprint_state),
            encoding="utf-8",
        )


def append_sprint_event(
    paths: RuntimePaths,
    sprint_id: str,
    *,
    event_type: str,
    summary: str,
    payload: dict[str, Any] | None = None,
) -> None:
    normalized_sprint_id = str(sprint_id or "").strip()
    if not normalized_sprint_id:
        return
    append_jsonl(
        paths.sprint_events_file(normalized_sprint_id),
        {
            "timestamp": utc_now_iso(),
            "type": event_type,
            "summary": summary,
            "payload": dict(payload or {}),
        },
    )


def iter_sprint_event_entries(paths: RuntimePaths, sprint_id: str) -> list[dict[str, Any]]:
    normalized_sprint_id = str(sprint_id or "").strip()
    if not normalized_sprint_id:
        return []
    return list(iter_jsonl_records(paths.sprint_events_file(normalized_sprint_id)))
