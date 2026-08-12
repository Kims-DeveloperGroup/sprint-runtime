from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from teams_runtime.shared.models import PromptContextRuntimeConfig, RequestRecord


PROMPT_EVENT_SELECTION_POLICY = "recent_tail_plus_latest_role_evidence"


@dataclass(slots=True, frozen=True)
class PromptRequestProjection:
    request_record: RequestRecord
    total_events: int
    included_events: int
    omitted_events: int
    recent_events: int
    max_events: int
    canonical_request: str

    @property
    def compacted(self) -> bool:
        return self.omitted_events > 0

    def notice(self) -> dict[str, Any]:
        if not self.compacted:
            return {}
        return {
            "compacted": True,
            "total_events": self.total_events,
            "included_events": self.included_events,
            "omitted_events": self.omitted_events,
            "recent_events": self.recent_events,
            "max_events": self.max_events,
            "selection": PROMPT_EVENT_SELECTION_POLICY,
            "canonical_request": self.canonical_request,
        }


def _role_evidence_identity(event: Any) -> str:
    if not isinstance(event, dict):
        return ""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    payload_role = str(payload.get("role") or "").strip().lower()
    payload_status = str(payload.get("status") or "").strip().lower()
    event_types = {
        str(event.get(field_name) or "").strip().lower()
        for field_name in ("type", "event_type")
    }
    if "role_report" not in event_types and not (payload_role and payload_status):
        return ""
    return payload_role or str(event.get("actor") or "").strip().lower()


def _canonical_request_path(request_record: RequestRecord) -> str:
    request_id = str(request_record.get("request_id") or "").strip()
    if not request_id:
        return ""
    return f"./.teams_runtime/requests/{request_id}.json"


def project_request_record_for_prompt(
    request_record: RequestRecord,
    config: PromptContextRuntimeConfig | None = None,
) -> PromptRequestProjection:
    resolved_config = config or PromptContextRuntimeConfig()
    projected_record: RequestRecord = dict(request_record)
    raw_events = request_record.get("events")
    if not isinstance(raw_events, list):
        return PromptRequestProjection(
            request_record=projected_record,
            total_events=0,
            included_events=0,
            omitted_events=0,
            recent_events=resolved_config.recent_events,
            max_events=resolved_config.max_events,
            canonical_request=_canonical_request_path(request_record),
        )

    total_events = len(raw_events)
    if not resolved_config.enabled or total_events <= resolved_config.max_events:
        return PromptRequestProjection(
            request_record=projected_record,
            total_events=total_events,
            included_events=total_events,
            omitted_events=0,
            recent_events=resolved_config.recent_events,
            max_events=resolved_config.max_events,
            canonical_request=_canonical_request_path(request_record),
        )

    tail_start = total_events - resolved_config.recent_events
    selected_indices = set(range(tail_start, total_events))
    represented_roles = {
        role
        for role in (_role_evidence_identity(raw_events[index]) for index in selected_indices)
        if role
    }

    for index in range(tail_start - 1, -1, -1):
        if len(selected_indices) >= resolved_config.max_events:
            break
        role = _role_evidence_identity(raw_events[index])
        if not role or role in represented_roles:
            continue
        selected_indices.add(index)
        represented_roles.add(role)

    selected_events = [raw_events[index] for index in sorted(selected_indices)]
    projected_record["events"] = selected_events  # type: ignore[typeddict-item]
    included_events = len(selected_events)
    return PromptRequestProjection(
        request_record=projected_record,
        total_events=total_events,
        included_events=included_events,
        omitted_events=total_events - included_events,
        recent_events=resolved_config.recent_events,
        max_events=resolved_config.max_events,
        canonical_request=_canonical_request_path(request_record),
    )


def render_prompt_event_history_notice(projection: PromptRequestProjection) -> str:
    if not projection.compacted:
        return ""
    return f"""Current request event-history projection:
{json.dumps(projection.notice(), ensure_ascii=False, indent=2)}
The `events` array below contains complete selected events, not summaries.
Omitted events still exist in the canonical request and must not be treated as events that never happened.
Open `canonical_request` only when the current decision requires evidence missing from the selected events.
"""


__all__ = [
    "PROMPT_EVENT_SELECTION_POLICY",
    "PromptRequestProjection",
    "project_request_record_for_prompt",
    "render_prompt_event_history_notice",
]
