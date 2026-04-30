from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Callable

from teams_runtime.shared.formatting import (
    build_backlog_item,
    priority_rank_sort_value,
    render_backlog_markdown,
)
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import (
    build_request_fingerprint,
    iter_json_records,
    read_json,
    utc_now_iso,
    write_json,
)


ACTIVE_BACKLOG_STATUSES = {"pending", "selected", "blocked"}
COMPLETED_BACKLOG_STATUS = "done"


def _looks_meta_backlog_title(value: Any) -> bool:
    normalized = " ".join(str(value or "").strip().lower().split())
    if not normalized:
        return False
    meta_markers = (
        "정리",
        "구체화",
        "반영",
        "동기화",
        "재구성",
        "업데이트",
        "개선",
        "prompt",
        "프롬프트",
        "문서",
        "라우팅",
        "회귀 테스트",
        "regression test",
    )
    return any(marker in normalized for marker in meta_markers)


def build_backlog_fingerprint(*, title: str, scope: str, kind: str) -> str:
    normalized = "|".join(
        [
            " ".join(str(title or "").strip().lower().split()),
            " ".join(str(scope or "").strip().lower().split()),
            str(kind or "").strip().lower(),
        ]
    )
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def build_sourcer_candidate_trace_fingerprint(candidate: dict[str, Any]) -> str:
    origin = dict(candidate.get("origin") or {})
    trace_parts: list[str] = []
    for key in sorted(origin):
        normalized_key = str(key or "").strip().lower()
        if not normalized_key or normalized_key == "sourcer_summary":
            continue
        if (
            normalized_key != "request_id"
            and not normalized_key.endswith("_request_id")
            and normalized_key not in {"action_name", "log_file", "operation_id", "role", "signal", "status"}
        ):
            continue
        value = origin.get(key)
        if isinstance(value, list):
            normalized_values = sorted(
                {
                    " ".join(str(item or "").strip().split())
                    for item in value
                    if str(item or "").strip()
                }
            )
            if normalized_values:
                trace_parts.append(f"{normalized_key}={','.join(normalized_values)}")
            continue
        normalized_value = " ".join(str(value or "").strip().split())
        if normalized_value:
            trace_parts.append(f"{normalized_key}={normalized_value}")
    return "|".join(trace_parts)


def normalize_sourcer_review_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        title = str(candidate.get("title") or "").strip()
        scope = str(candidate.get("scope") or title).strip()
        summary = str(candidate.get("summary") or scope or title).strip()
        kind = str(candidate.get("kind") or "enhancement").strip().lower() or "enhancement"
        if not title or not scope:
            continue
        normalized.append(
            {
                "title": title,
                "scope": scope,
                "summary": summary,
                "kind": kind,
                "acceptance_criteria": normalize_backlog_acceptance_criteria(
                    candidate.get("acceptance_criteria")
                ),
                "milestone_title": str(candidate.get("milestone_title") or "").strip(),
                "priority_rank": int(candidate.get("priority_rank") or 0),
                "planned_in_sprint_id": str(candidate.get("planned_in_sprint_id") or "").strip(),
                "added_during_active_sprint": bool(candidate.get("added_during_active_sprint")),
                "origin": dict(candidate.get("origin") or {}),
            }
        )
    return normalized


def normalize_blocked_backlog_review_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        backlog_id = str(candidate.get("backlog_id") or "").strip()
        title = str(candidate.get("title") or "").strip()
        scope = str(candidate.get("scope") or title).strip()
        if not backlog_id or not title or not scope:
            continue
        if str(candidate.get("status") or "").strip().lower() != "blocked":
            continue
        normalized.append(
            {
                "backlog_id": backlog_id,
                "title": title,
                "scope": scope,
                "summary": str(candidate.get("summary") or scope or title).strip(),
                "kind": str(candidate.get("kind") or "enhancement").strip().lower() or "enhancement",
                "status": "blocked",
                "blocked_reason": str(candidate.get("blocked_reason") or "").strip(),
                "blocked_by_role": str(candidate.get("blocked_by_role") or "").strip(),
                "required_inputs": normalize_backlog_acceptance_criteria(candidate.get("required_inputs")),
                "recommended_next_step": str(candidate.get("recommended_next_step") or "").strip(),
                "acceptance_criteria": normalize_backlog_acceptance_criteria(
                    candidate.get("acceptance_criteria")
                ),
                "milestone_title": str(candidate.get("milestone_title") or "").strip(),
                "priority_rank": int(candidate.get("priority_rank") or 0),
                "planned_in_sprint_id": str(candidate.get("planned_in_sprint_id") or "").strip(),
                "updated_at": str(candidate.get("updated_at") or "").strip(),
                "origin": dict(candidate.get("origin") or {}),
            }
        )
    normalized.sort(
        key=lambda item: (
            int(item.get("priority_rank") or 0) if int(item.get("priority_rank") or 0) > 0 else 10**9,
            str(item.get("updated_at") or ""),
            str(item.get("backlog_id") or ""),
        )
    )
    return normalized


def build_sourcer_review_fingerprint(candidates: list[dict[str, Any]]) -> str:
    parts = [
        "::".join(
            [
                build_backlog_fingerprint(
                    title=str(candidate.get("title") or ""),
                    scope=str(candidate.get("scope") or ""),
                    kind=str(candidate.get("kind") or ""),
                ),
                build_sourcer_candidate_trace_fingerprint(candidate),
            ]
        )
        for candidate in candidates
        if str(candidate.get("title") or "").strip()
    ]
    digest = hashlib.sha1("|".join(sorted(parts)).encode("utf-8")).hexdigest()
    return build_request_fingerprint(
        author_id="internal-sourcer",
        channel_id="backlog-sourcing-review",
        intent="plan",
        scope=f"sourcer-review:{digest}",
    )


def build_blocked_backlog_review_fingerprint(candidates: list[dict[str, Any]]) -> str:
    parts = [
        "|".join(
            [
                str(candidate.get("backlog_id") or "").strip(),
                str(candidate.get("updated_at") or "").strip(),
            ]
        )
        for candidate in candidates
        if str(candidate.get("backlog_id") or "").strip()
    ]
    digest = hashlib.sha1("|".join(sorted(parts)).encode("utf-8")).hexdigest()
    return build_request_fingerprint(
        author_id="blocked-backlog-review",
        channel_id="blocked-backlog-review",
        intent="plan",
        scope=f"blocked-backlog-review:{digest}",
    )


def render_sourcer_review_markdown(
    *,
    request_id: str,
    candidates: list[dict[str, Any]],
    sourcing_activity: dict[str, Any],
) -> str:
    lines = [
        "# Sourcer Backlog Review",
        "",
        f"- request_id: {request_id}",
        f"- candidate_count: {len(candidates)}",
        f"- sourcer_summary: {str(sourcing_activity.get('summary') or '').strip() or '없음'}",
        f"- sourcer_mode: {str(sourcing_activity.get('mode') or '').strip() or 'unknown'}",
        "",
        "## Candidates",
        "",
    ]
    for index, candidate in enumerate(candidates, start=1):
        lines.extend(
            [
                f"### {index}. {candidate.get('title') or ''}",
                f"- kind: {candidate.get('kind') or ''}",
                f"- scope: {candidate.get('scope') or ''}",
                f"- summary: {candidate.get('summary') or ''}",
            ]
        )
        acceptance = [
            str(item).strip()
            for item in (candidate.get("acceptance_criteria") or [])
            if str(item).strip()
        ]
        if acceptance:
            lines.append("- acceptance_criteria:")
            lines.extend([f"  - {item}" for item in acceptance])
        origin = dict(candidate.get("origin") or {})
        if origin:
            origin_parts = [f"{key}={value}" for key, value in origin.items() if str(value).strip()]
            if origin_parts:
                lines.append(f"- origin: {', '.join(origin_parts)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_blocked_backlog_review_markdown(
    *,
    request_id: str,
    candidates: list[dict[str, Any]],
) -> str:
    lines = [
        "# Blocked Backlog Review",
        "",
        f"- request_id: {request_id}",
        f"- candidate_count: {len(candidates)}",
        "",
        "## Blocked Items",
        "",
    ]
    for index, candidate in enumerate(candidates, start=1):
        lines.extend(
            [
                f"### {index}. {candidate.get('title') or ''}",
                f"- backlog_id: {candidate.get('backlog_id') or ''}",
                f"- kind: {candidate.get('kind') or ''}",
                f"- scope: {candidate.get('scope') or ''}",
                f"- summary: {candidate.get('summary') or ''}",
                f"- blocked_reason: {candidate.get('blocked_reason') or '없음'}",
                f"- blocked_by_role: {candidate.get('blocked_by_role') or '없음'}",
                f"- recommended_next_step: {candidate.get('recommended_next_step') or '없음'}",
            ]
        )
        required_inputs = [
            str(item).strip()
            for item in (candidate.get("required_inputs") or [])
            if str(item).strip()
        ]
        if required_inputs:
            lines.append("- required_inputs:")
            lines.extend([f"  - {item}" for item in required_inputs])
        acceptance = [
            str(item).strip()
            for item in (candidate.get("acceptance_criteria") or [])
            if str(item).strip()
        ]
        if acceptance:
            lines.append("- acceptance_criteria:")
            lines.extend([f"  - {item}" for item in acceptance])
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def classify_backlog_kind(intent: str, scope: str, summary: str = "") -> str:
    combined = " ".join([str(intent or ""), str(scope or ""), str(summary or "")]).lower()
    if any(token in combined for token in ("bug", "error", "fix", "회귀", "실패", "오류")):
        return "bug"
    if any(token in combined for token in ("feature", "new feature", "기능 추가", "신규 기능")):
        return "feature"
    if any(token in combined for token in ("chore", "cleanup", "정리", "문서", "docs")):
        return "chore"
    return "enhancement"


def normalize_backlog_acceptance_criteria(values: Any) -> list[str]:
    if isinstance(values, list):
        return [str(item).strip() for item in values if str(item).strip()]
    if isinstance(values, str):
        normalized = str(values).strip()
        return [normalized] if normalized else []
    return []


def fallback_backlog_candidates_from_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for finding in findings:
        title = str(finding.get("title") or "").strip()
        if not title:
            continue
        candidates.append(
            build_backlog_item(
                title=title,
                summary=str(finding.get("summary") or title).strip(),
                kind=str(finding.get("kind_hint") or "enhancement").strip().lower() or "enhancement",
                source="sourcer",
                scope=str(finding.get("scope") or title).strip(),
                acceptance_criteria=normalize_backlog_acceptance_criteria(
                    finding.get("acceptance_criteria")
                ),
                origin={
                    "sourcing_agent": "fallback",
                    "signal": str(finding.get("signal") or "").strip(),
                    **dict(finding.get("origin") or {}),
                },
            )
        )
    return candidates


def is_non_actionable_backlog_item(
    item: dict[str, Any],
    *,
    request_loader: Callable[[str], dict[str, Any]] | None = None,
) -> bool:
    title = str(item.get("title") or "").strip().lower()
    source = str(item.get("source") or "").strip().lower()
    if source == "discovery" and title.endswith("insight follow-up"):
        return True
    origin = dict(item.get("origin") or {})
    request_id = str(origin.get("request_id") or "").strip()
    if source == "discovery" and request_id and request_loader is not None:
        request_record = request_loader(request_id)
        params = dict(request_record.get("params") or {})
        if str(params.get("_teams_kind") or "").strip() == "sprint_internal":
            return True
    return False


def is_active_backlog_status(status: str) -> bool:
    return str(status or "").strip().lower() in ACTIVE_BACKLOG_STATUSES


def is_actionable_backlog_status(status: str) -> bool:
    return str(status or "").strip().lower() == "pending"


def is_reusable_backlog_status(status: str) -> bool:
    return str(status or "").strip().lower() in ACTIVE_BACKLOG_STATUSES


def clear_backlog_blockers(item: dict[str, Any]) -> None:
    item["blocked_reason"] = ""
    item["blocked_by_role"] = ""
    item["required_inputs"] = []
    item["recommended_next_step"] = ""


def desired_backlog_status_for_todo(todo: dict[str, Any] | None) -> str:
    status = str((todo or {}).get("status") or "").strip().lower()
    if status in {"queued", "running"}:
        return "selected"
    if status in {"completed", "committed"}:
        return "done"
    if status in {"blocked", "uncommitted"}:
        return "blocked"
    if status == "failed":
        return "carried_over"
    return ""


def _backlog_blocker_snapshot(item: dict[str, Any]) -> tuple[str, str, list[Any], str]:
    return (
        str(item.get("blocked_reason") or "").strip(),
        str(item.get("blocked_by_role") or "").strip(),
        list(item.get("required_inputs") or []),
        str(item.get("recommended_next_step") or "").strip(),
    )


def apply_backlog_state_from_todo(
    backlog_item: dict[str, Any],
    *,
    todo: dict[str, Any] | None,
    sprint_id: str,
) -> bool:
    desired_status = desired_backlog_status_for_todo(todo)
    if not desired_status:
        return False
    changed = False
    if str(backlog_item.get("status") or "").strip().lower() != desired_status:
        backlog_item["status"] = desired_status
        changed = True
    if desired_status == "selected":
        if str(backlog_item.get("selected_in_sprint_id") or "").strip() != sprint_id:
            backlog_item["selected_in_sprint_id"] = sprint_id
            changed = True
        if str(backlog_item.get("completed_in_sprint_id") or "").strip():
            backlog_item["completed_in_sprint_id"] = ""
            changed = True
        blocker_snapshot = _backlog_blocker_snapshot(backlog_item)
        clear_backlog_blockers(backlog_item)
        if blocker_snapshot != _backlog_blocker_snapshot(backlog_item):
            changed = True
        return changed
    if desired_status == "done":
        if str(backlog_item.get("selected_in_sprint_id") or "").strip() != sprint_id:
            backlog_item["selected_in_sprint_id"] = sprint_id
            changed = True
        if str(backlog_item.get("completed_in_sprint_id") or "").strip() != sprint_id:
            backlog_item["completed_in_sprint_id"] = sprint_id
            changed = True
        blocker_snapshot = _backlog_blocker_snapshot(backlog_item)
        clear_backlog_blockers(backlog_item)
        if blocker_snapshot != _backlog_blocker_snapshot(backlog_item):
            changed = True
        return changed
    if desired_status == "blocked":
        if str(backlog_item.get("selected_in_sprint_id") or "").strip():
            backlog_item["selected_in_sprint_id"] = ""
            changed = True
        if str(backlog_item.get("completed_in_sprint_id") or "").strip():
            backlog_item["completed_in_sprint_id"] = ""
            changed = True
        todo_status = str((todo or {}).get("status") or "").strip().lower()
        todo_summary = str((todo or {}).get("summary") or "").strip()
        if todo_status == "uncommitted":
            if not str(backlog_item.get("blocked_reason") or "").strip() and todo_summary:
                backlog_item["blocked_reason"] = todo_summary
                changed = True
            if str(backlog_item.get("blocked_by_role") or "").strip() != "version_controller":
                backlog_item["blocked_by_role"] = "version_controller"
                changed = True
            if not str(backlog_item.get("recommended_next_step") or "").strip():
                backlog_item["recommended_next_step"] = "version_controller recovery 또는 수동 git 정리가 필요합니다."
                changed = True
        return changed
    if desired_status == "carried_over":
        if str(backlog_item.get("selected_in_sprint_id") or "").strip():
            backlog_item["selected_in_sprint_id"] = ""
            changed = True
        if str(backlog_item.get("completed_in_sprint_id") or "").strip():
            backlog_item["completed_in_sprint_id"] = ""
            changed = True
    return changed


def build_sprint_selected_backlog_item(
    backlog_id: str,
    *,
    backlog_item: dict[str, Any] | None = None,
    selected_item: dict[str, Any] | None = None,
    todo: dict[str, Any] | None = None,
    sprint_id: str,
) -> dict[str, Any]:
    merged_item = dict(backlog_item or selected_item or {})
    if not merged_item and todo:
        merged_item = {
            "backlog_id": str(backlog_id or "").strip(),
            "title": str(todo.get("title") or "").strip(),
            "milestone_title": str(todo.get("milestone_title") or "").strip(),
            "priority_rank": int(todo.get("priority_rank") or 0),
            "acceptance_criteria": [
                str(value).strip()
                for value in (todo.get("acceptance_criteria") or [])
                if str(value).strip()
            ],
        }
    desired_status = desired_backlog_status_for_todo(todo)
    if desired_status and merged_item:
        merged_item["status"] = desired_status
        if desired_status == "done":
            merged_item["selected_in_sprint_id"] = sprint_id
            merged_item["completed_in_sprint_id"] = sprint_id
            clear_backlog_blockers(merged_item)
        elif desired_status == "selected":
            merged_item["selected_in_sprint_id"] = sprint_id
            merged_item["completed_in_sprint_id"] = ""
            clear_backlog_blockers(merged_item)
        elif desired_status == "blocked":
            merged_item["selected_in_sprint_id"] = ""
            merged_item["completed_in_sprint_id"] = ""
            if str((todo or {}).get("status") or "").strip().lower() == "uncommitted":
                if not str(merged_item.get("blocked_reason") or "").strip():
                    merged_item["blocked_reason"] = str((todo or {}).get("summary") or "").strip()
                if not str(merged_item.get("blocked_by_role") or "").strip():
                    merged_item["blocked_by_role"] = "version_controller"
                if not str(merged_item.get("recommended_next_step") or "").strip():
                    merged_item["recommended_next_step"] = "version_controller recovery 또는 수동 git 정리가 필요합니다."
        elif desired_status == "carried_over":
            merged_item["selected_in_sprint_id"] = ""
            merged_item["completed_in_sprint_id"] = ""
    return merged_item


def iter_backlog_items(paths: RuntimePaths) -> list[dict[str, Any]]:
    return list(iter_json_records(paths.backlog_dir))


def load_backlog_item(paths: RuntimePaths, backlog_id: str) -> dict[str, Any]:
    normalized_backlog_id = str(backlog_id or "").strip()
    if not normalized_backlog_id:
        return {}
    return read_json(paths.backlog_file(normalized_backlog_id))


def save_backlog_item(
    paths: RuntimePaths,
    item: dict[str, Any],
    *,
    update_timestamp: bool = True,
    refresh_markdown: bool = True,
) -> None:
    backlog_id = str(item.get("backlog_id") or "").strip()
    if not backlog_id:
        return
    if update_timestamp:
        item["updated_at"] = utc_now_iso()
    write_json(paths.backlog_file(backlog_id), item)
    if refresh_markdown:
        refresh_backlog_markdown(paths)


def refresh_backlog_markdown(paths: RuntimePaths) -> None:
    items = iter_backlog_items(paths)
    active_items = [
        item
        for item in items
        if str(item.get("status") or "").strip().lower() in ACTIVE_BACKLOG_STATUSES
    ]
    completed_items = [
        item
        for item in items
        if str(item.get("status") or "").strip().lower() == COMPLETED_BACKLOG_STATUS
    ]
    paths.shared_backlog_file.write_text(
        render_backlog_markdown(active_items),
        encoding="utf-8",
    )
    paths.shared_completed_backlog_file.write_text(
        render_backlog_markdown(
            completed_items,
            title="Completed Backlog",
            empty_message="completed backlog 없음",
        ),
        encoding="utf-8",
    )


def repair_non_actionable_carry_over_backlog_items(paths: RuntimePaths) -> set[str]:
    blocked_carry_over_ids: set[str] = set()
    for sprint_file in sorted(paths.sprints_dir.glob("*.json")):
        sprint_state = read_json(sprint_file)
        if not sprint_state:
            continue
        for todo in sprint_state.get("todos") or []:
            status = str(todo.get("status") or "").strip().lower()
            carry_over_backlog_id = str(todo.get("carry_over_backlog_id") or "").strip()
            if status == "blocked" and carry_over_backlog_id:
                blocked_carry_over_ids.add(carry_over_backlog_id)

    repaired_ids: set[str] = set()
    if not blocked_carry_over_ids:
        return repaired_ids
    for item in iter_backlog_items(paths):
        backlog_id = str(item.get("backlog_id") or "").strip()
        status = str(item.get("status") or "").strip().lower()
        if backlog_id not in blocked_carry_over_ids or status not in {"pending", "selected"}:
            continue
        item["status"] = "blocked"
        item["selected_in_sprint_id"] = ""
        item["updated_at"] = utc_now_iso()
        save_backlog_item(paths, item)
        repaired_ids.add(backlog_id)
    return repaired_ids


def drop_non_actionable_backlog_items(
    paths: RuntimePaths,
    *,
    request_loader: Callable[[str], dict[str, Any]] | None = None,
) -> set[str]:
    dropped_ids: set[str] = set()
    for item in iter_backlog_items(paths):
        if not is_non_actionable_backlog_item(item, request_loader=request_loader):
            continue
        backlog_id = str(item.get("backlog_id") or "").strip()
        if not backlog_id:
            continue
        if str(item.get("status") or "").strip().lower() == "dropped":
            dropped_ids.add(backlog_id)
            continue
        item["status"] = "dropped"
        item["selected_in_sprint_id"] = ""
        item["completed_in_sprint_id"] = ""
        item["dropped_reason"] = "agent insight is journal-only context, not backlog work"
        save_backlog_item(
            paths,
            item,
            update_timestamp=True,
            refresh_markdown=False,
        )
        dropped_ids.add(backlog_id)
    return dropped_ids


def backlog_status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    pending = sum(1 for item in items if str(item.get("status") or "") == "pending")
    selected = sum(1 for item in items if str(item.get("status") or "") == "selected")
    blocked = sum(1 for item in items if str(item.get("status") or "") == "blocked")
    done = sum(1 for item in items if str(item.get("status") or "") == "done")
    return {
        "pending": pending,
        "selected": selected,
        "blocked": blocked,
        "done": done,
        "total": pending + selected + blocked,
    }


def backlog_status_rank(value: str) -> int:
    normalized = str(value or "").strip().lower()
    return {"selected": 0, "pending": 1, "blocked": 2, "done": 3}.get(normalized, 4)


def backlog_kind_rank(value: str) -> int:
    normalized = str(value or "").strip().lower()
    return {"bug": 0, "feature": 1, "enhancement": 2, "chore": 3}.get(normalized, 4)


def backlog_priority_key(item: dict[str, Any]) -> tuple[int, int, int, int, str]:
    source_rank = 0 if str(item.get("source") or "").strip() == "user" else 1
    return (
        backlog_status_rank(str(item.get("status") or "")),
        priority_rank_sort_value(item.get("priority_rank")),
        source_rank,
        backlog_kind_rank(str(item.get("kind") or "")),
        str(item.get("created_at") or ""),
    )


def count_backlog_items_by_key(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        normalized = str(item.get(key) or "").strip().lower()
        if not normalized:
            continue
        counts[normalized] = counts.get(normalized, 0) + 1
    return counts


def backlog_status_report_context(items: list[dict[str, Any]]) -> dict[str, Any]:
    active_items = [
        item
        for item in items
        if str(item.get("status") or "").strip().lower() in ACTIVE_BACKLOG_STATUSES
    ]
    active_items.sort(key=backlog_priority_key)
    return {
        "active_items": active_items,
        "counts": backlog_status_counts(items),
        "kind_counts": count_backlog_items_by_key(active_items, "kind"),
        "source_counts": count_backlog_items_by_key(active_items, "source"),
    }


def _extract_payload_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    collected: list[Any] = []
    raw_items = payload.get("backlog_items")
    if isinstance(raw_items, list):
        collected.extend(raw_items)
    single_item = payload.get("backlog_item")
    if isinstance(single_item, (str, dict)):
        collected.append(single_item)
    if collected:
        return collected
    if any(key in payload for key in ("title", "scope", "summary")):
        return [payload]
    return []


def _normalize_candidate(
    raw_item: Any,
    *,
    default_source: str,
    source_request_id: str = "",
) -> dict[str, Any] | None:
    explicit_fields: set[str] = set()
    if isinstance(raw_item, str):
        title = str(raw_item).strip()
        scope = title
        summary = title
        kind = "enhancement"
        source = default_source
        origin: dict[str, Any] = {"request_id": source_request_id} if source_request_id else {}
        item = build_backlog_item(
            title=title,
            summary=summary,
            kind=kind,
            source=source,
            scope=scope,
            origin=origin,
        )
    elif isinstance(raw_item, dict):
        explicit_fields = {str(key).strip() for key in raw_item.keys()}
        title = str(
            raw_item.get("title")
            or raw_item.get("scope")
            or raw_item.get("summary")
            or ""
        ).strip()
        scope = str(raw_item.get("scope") or title).strip()
        summary = str(raw_item.get("summary") or title or scope).strip()
        if _looks_meta_backlog_title(title) and summary and not _looks_meta_backlog_title(summary):
            title = summary
        elif _looks_meta_backlog_title(title) and scope and not _looks_meta_backlog_title(scope):
            title = scope
        kind = str(raw_item.get("kind") or "enhancement").strip().lower() or "enhancement"
        source = str(raw_item.get("source") or default_source).strip().lower() or default_source
        origin = dict(raw_item.get("origin") or {})
        if source_request_id and "request_id" not in origin:
            origin["request_id"] = source_request_id
        item = build_backlog_item(
            title=title,
            summary=summary,
            kind=kind,
            source=source,
            scope=scope,
            acceptance_criteria=normalize_backlog_acceptance_criteria(
                raw_item.get("acceptance_criteria")
            ),
            origin=origin,
            backlog_id=str(raw_item.get("backlog_id") or "").strip() or None,
            milestone_title=str(raw_item.get("milestone_title") or "").strip(),
            priority_rank=int(raw_item.get("priority_rank") or 0),
            planned_in_sprint_id=str(raw_item.get("planned_in_sprint_id") or "").strip(),
            added_during_active_sprint=bool(raw_item.get("added_during_active_sprint")),
        )
        for field_name in (
            "status",
            "selected_in_sprint_id",
            "completed_in_sprint_id",
            "carry_over_of",
            "blocked_reason",
            "blocked_by_role",
            "recommended_next_step",
        ):
            if field_name in explicit_fields:
                item[field_name] = str(raw_item.get(field_name) or "").strip()
        if "required_inputs" in explicit_fields:
            item["required_inputs"] = [
                str(value).strip()
                for value in (raw_item.get("required_inputs") or [])
                if str(value).strip()
            ]
    else:
        return None
    if not str(item.get("title") or "").strip():
        return None
    item["_explicit_fields"] = sorted(explicit_fields)
    item["fingerprint"] = build_backlog_fingerprint(
        title=str(item.get("title") or ""),
        scope=str(item.get("scope") or ""),
        kind=str(item.get("kind") or ""),
    )
    return item


def _merge_item(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    explicit_fields = {
        str(field).strip()
        for field in (candidate.get("_explicit_fields") or [])
        if str(field).strip()
    }
    always_fields = {"title", "scope", "kind", "fingerprint"}
    for field_name in (
        "title",
        "summary",
        "kind",
        "source",
        "scope",
        "milestone_title",
        "priority_rank",
        "planned_in_sprint_id",
        "status",
        "selected_in_sprint_id",
        "completed_in_sprint_id",
        "carry_over_of",
        "blocked_reason",
        "blocked_by_role",
        "recommended_next_step",
        "fingerprint",
    ):
        if field_name in always_fields or field_name in explicit_fields:
            merged[field_name] = candidate.get(field_name)
    if "acceptance_criteria" in explicit_fields and "acceptance_criteria" in candidate:
        merged["acceptance_criteria"] = list(candidate.get("acceptance_criteria") or [])
    elif list(candidate.get("acceptance_criteria") or []) and not list(merged.get("acceptance_criteria") or []):
        merged["acceptance_criteria"] = list(candidate.get("acceptance_criteria") or [])
    if "required_inputs" in explicit_fields:
        merged["required_inputs"] = list(candidate.get("required_inputs") or [])
    if str(merged.get("status") or "").strip().lower() in {"pending", "selected", "done"}:
        merged["blocked_reason"] = ""
        merged["blocked_by_role"] = ""
        merged["required_inputs"] = []
        merged["recommended_next_step"] = ""
    if candidate.get("added_during_active_sprint"):
        merged["added_during_active_sprint"] = True
    merged.setdefault("origin", {})
    merged["origin"].update(dict(candidate.get("origin") or {}))
    return merged


def merge_backlog_payload(
    *,
    workspace_root: str | Path,
    payload: Any,
    default_source: str = "planner",
    source_request_id: str = "",
) -> dict[str, Any]:
    paths = RuntimePaths.from_root(workspace_root)
    raw_items = _extract_payload_items(payload)
    normalized_candidates = [
        candidate
        for candidate in (
            _normalize_candidate(
                raw_item,
                default_source=default_source,
                source_request_id=source_request_id,
            )
            for raw_item in raw_items
        )
        if candidate is not None
    ]
    existing_items = iter_backlog_items(paths)
    existing_by_backlog_id = {
        str(item.get("backlog_id") or "").strip(): item
        for item in existing_items
        if str(item.get("backlog_id") or "").strip()
    }
    existing_by_fingerprint = {
        str(item.get("fingerprint") or "").strip(): item
        for item in existing_items
        if str(item.get("fingerprint") or "").strip()
    }
    persisted_items: list[dict[str, Any]] = []
    added = 0
    updated = 0
    for candidate in normalized_candidates:
        backlog_id = str(candidate.get("backlog_id") or "").strip()
        fingerprint = str(candidate.get("fingerprint") or "").strip()
        existing = existing_by_backlog_id.get(backlog_id) if backlog_id else None
        if existing is None and fingerprint:
            existing = existing_by_fingerprint.get(fingerprint)
        if existing:
            merged = _merge_item(existing, candidate)
            merged["updated_at"] = utc_now_iso()
            write_json(paths.backlog_file(str(merged.get("backlog_id") or "")), merged)
            existing_by_backlog_id[str(merged.get("backlog_id") or "")] = merged
            existing_by_fingerprint[str(merged.get("fingerprint") or "")] = merged
            updated += 1
            persisted_items.append(merged)
            continue
        new_item = dict(candidate)
        new_item.pop("_explicit_fields", None)
        new_item["updated_at"] = str(new_item.get("created_at") or utc_now_iso())
        write_json(paths.backlog_file(str(new_item.get("backlog_id") or "")), new_item)
        existing_by_backlog_id[str(new_item.get("backlog_id") or "")] = new_item
        existing_by_fingerprint[str(new_item.get("fingerprint") or "")] = new_item
        added += 1
        persisted_items.append(new_item)
    refresh_backlog_markdown(paths)
    return {
        "added": added,
        "updated": updated,
        "items": [
            {
                "backlog_id": str(item.get("backlog_id") or ""),
                "title": str(item.get("title") or ""),
                "status": str(item.get("status") or ""),
                "fingerprint": str(item.get("fingerprint") or ""),
            }
            for item in persisted_items
        ],
    }


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persist planner-owned backlog updates into teams runtime state.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    merge_parser = subparsers.add_parser("merge", help="Merge backlog payload into runtime backlog state.")
    merge_parser.add_argument("--workspace-root", required=True)
    merge_parser.add_argument("--input-file", required=True)
    merge_parser.add_argument("--source", default="planner")
    merge_parser.add_argument("--request-id", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_argument_parser()
    args = parser.parse_args(argv)
    input_path = Path(args.input_file).expanduser().resolve()
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if args.command == "merge":
        result = merge_backlog_payload(
            workspace_root=args.workspace_root,
            payload=payload,
            default_source=str(args.source or "planner"),
            source_request_id=str(args.request_id or ""),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
