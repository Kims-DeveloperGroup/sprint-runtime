from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from teams_runtime.shared.formatting import build_backlog_item
from teams_runtime.shared.models import TEAM_ROLES
from teams_runtime.shared.persistence import utc_now_iso
from teams_runtime.workflows.repository_ops import capture_git_baseline
from teams_runtime.workflows.sprints.lifecycle import (
    build_sprint_cutoff_at,
    compute_next_slot_at,
    utc_now,
)
from teams_runtime.workflows.state.request_store import iter_request_records


LOGGER = logging.getLogger(__name__)


def _truncate_text(value: Any, *, limit: int = 240) -> str:
    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def build_sourcer_existing_backlog_context(service: Any) -> list[dict[str, Any]]:
    items = [
        item
        for item in service._iter_backlog_items()
        if service._is_active_backlog_status(str(item.get("status") or ""))
    ]
    items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    context: list[dict[str, Any]] = []
    for item in items[:20]:
        context.append(
            {
                "backlog_id": str(item.get("backlog_id") or ""),
                "title": str(item.get("title") or ""),
                "summary": str(item.get("summary") or ""),
                "kind": str(item.get("kind") or ""),
                "scope": str(item.get("scope") or ""),
                "status": str(item.get("status") or ""),
            }
        )
    return context


def collect_backlog_linked_request_ids(service: Any) -> set[str]:
    linked_request_ids: set[str] = set()
    for item in service._iter_backlog_items():
        origin = dict(item.get("origin") or {})
        for key, value in origin.items():
            normalized_key = str(key or "").strip().lower()
            if normalized_key != "request_id" and not normalized_key.endswith("_request_id"):
                continue
            request_id = str(value or "").strip()
            if request_id:
                linked_request_ids.add(request_id)
    return linked_request_ids


def build_backlog_sourcing_findings(service: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    backlog_linked_request_ids = service._collect_backlog_linked_request_ids()
    for record in iter_request_records(service.paths):
        status = str(record.get("status") or "").strip().lower()
        if status in {"completed", "committed", "cancelled", "blocked", "delegated", "queued"}:
            continue
        request_id = str(record.get("request_id") or "").strip()
        if request_id and request_id in backlog_linked_request_ids:
            continue
        params = dict(record.get("params") or {})
        if str(params.get("_teams_kind") or "").strip().lower() == "sprint_internal":
            continue
        summary = str(record.get("scope") or record.get("body") or "").strip()
        if not summary:
            continue
        findings.append(
            {
                "signal": "open_request",
                "title": summary,
                "summary": f"request 상태={status}",
                "kind_hint": service._classify_backlog_kind(str(record.get("intent") or ""), summary, status),
                "scope": summary,
                "acceptance_criteria": [],
                "origin": {"request_id": record.get("request_id") or "", "status": status},
            }
        )
    if service.runtime_config.sprint_discovery_scope == "broad_scan":
        for record in iter_request_records(service.paths):
            if service._is_internal_sprint_request(record):
                continue
            result = dict(record.get("result") or {}) if isinstance(record.get("result"), dict) else {}
            role = str(result.get("role") or "").strip()
            request_id = str(record.get("request_id") or result.get("request_id") or "").strip()
            if request_id and request_id in backlog_linked_request_ids:
                continue
            summary = str(result.get("summary") or "").strip()
            status = str(result.get("status") or "").strip().lower()
            if role and status == "failed":
                findings.append(
                    {
                        "signal": "role_failure",
                        "title": f"{role} 후속 조치 {request_id or 'unknown'}",
                        "summary": summary or str(result.get("error") or "").strip() or f"{role} 결과 점검",
                        "kind_hint": "bug",
                        "scope": summary or f"{role} role output follow-up",
                        "acceptance_criteria": [],
                        "origin": {
                            "role": role,
                            "request_id": request_id,
                            "status": status,
                            "request_file": str(service.paths.request_file(request_id)) if request_id else "",
                        },
                    }
                )
    if service.runtime_config.sprint_discovery_scope in {"plus_git", "broad_scan"}:
        baseline = capture_git_baseline(service.paths.project_workspace_root)
        dirty_paths = [str(item) for item in baseline.get("dirty_paths") or [] if str(item).strip()]
        if dirty_paths:
            findings.append(
                {
                    "signal": "git_dirty",
                    "title": "워크스페이스 변경 검토",
                    "summary": ", ".join(dirty_paths[:8]),
                    "kind_hint": "enhancement",
                    "scope": "git status 기반 변경 검토",
                    "acceptance_criteria": [],
                    "origin": {"dirty_paths": dirty_paths},
                }
            )
    if service.runtime_config.sprint_discovery_scope == "broad_scan":
        for role in TEAM_ROLES:
            log_path = service.paths.agent_runtime_log(role)
            try:
                log_text = log_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                log_text = ""
            if "Traceback" in log_text or "ERROR" in log_text:
                findings.append(
                    {
                        "signal": "runtime_log_error",
                        "title": f"{role} 로그 오류 점검",
                        "summary": "runtime log에 오류 흔적이 있습니다.",
                        "kind_hint": "bug",
                        "scope": f"{role} runtime log",
                        "acceptance_criteria": [],
                        "origin": {"role": role, "log_file": str(log_path)},
                    }
                )
        for action_name in service.runtime_config.sprint_discovery_actions:
            action = service.runtime_config.actions.get(action_name)
            if action is None or action.lifecycle != "foreground":
                continue
            try:
                execution = service.action_executor.execute(
                    request_id=f"discovery-{utc_now().strftime('%Y%m%d%H%M%S')}",
                    action_name=action_name,
                    params={},
                )
            except Exception as exc:
                findings.append(
                    {
                        "signal": "discovery_action_failure",
                        "title": f"discovery action 실패: {action_name}",
                        "summary": str(exc),
                        "kind_hint": "bug",
                        "scope": f"discovery_action={action_name}",
                        "acceptance_criteria": [],
                        "origin": {"action_name": action_name},
                    }
                )
                continue
            if str(execution.get("status") or "").strip().lower() != "completed":
                findings.append(
                    {
                        "signal": "discovery_action_check",
                        "title": f"discovery action 점검: {action_name}",
                        "summary": str(execution.get("report") or execution.get("message") or "").strip(),
                        "kind_hint": "bug",
                        "scope": f"discovery_action={action_name}",
                        "acceptance_criteria": [],
                        "origin": {
                            "action_name": action_name,
                            "operation_id": execution.get("operation_id") or "",
                        },
                    }
                )
    return findings


def select_backlog_items_for_sprint(service: Any) -> list[dict[str, Any]]:
    if service._drop_non_actionable_backlog_items():
        service._refresh_backlog_markdown()
    repaired_ids = service._repair_non_actionable_carry_over_backlog_items()
    if repaired_ids:
        service._refresh_backlog_markdown()
    pending = [
        item
        for item in service._iter_backlog_items()
        if service._is_actionable_backlog_status(str(item.get("status") or ""))
    ]

    def priority(item: dict[str, Any]) -> tuple[int, int, str]:
        priority_rank = int(item.get("priority_rank") or 0)
        if priority_rank > 0:
            return (-priority_rank, 0, str(item.get("created_at") or ""))
        source_rank = 0 if str(item.get("source") or "") == "user" else 1
        kind = str(item.get("kind") or "").strip().lower()
        kind_rank = {"bug": 0, "feature": 1, "enhancement": 2, "chore": 3}.get(kind, 4)
        return (source_rank, kind_rank, str(item.get("created_at") or ""))

    pending.sort(key=priority)
    return pending


def discover_backlog_candidates(service: Any) -> list[dict[str, Any]]:
    findings = service._build_backlog_sourcing_findings()
    findings_sample = [
        _truncate_text(item.get("title") or item.get("scope") or item.get("summary") or "", limit=80)
        for item in findings[:3]
        if _truncate_text(item.get("title") or item.get("scope") or item.get("summary") or "", limit=80)
    ]
    if not findings:
        service._last_backlog_sourcing_activity = {
            "status": "completed",
            "summary": "수집할 backlog finding이 없어 sourcer 실행을 건너뛰었습니다.",
            "error": "",
            "mode": "idle",
            "findings_count": 0,
            "candidate_count": 0,
            "filtered_candidate_count": 0,
            "raw_backlog_items_count": 0,
            "existing_backlog_count": 0,
            "findings_sample": [],
            "session_id": "",
            "session_workspace": "",
            "elapsed_ms": 0,
        }
        return []
    scheduler_state = service._load_scheduler_state()
    active_sprint = service._load_sprint_state(str(scheduler_state.get("active_sprint_id") or ""))
    existing_backlog = service._build_sourcer_existing_backlog_context()
    try:
        sourced = service.backlog_sourcer.source(
            findings=findings,
            scheduler_state=scheduler_state,
            active_sprint=active_sprint,
            backlog_counts=service._backlog_counts(),
            existing_backlog=existing_backlog,
        )
    except Exception as exc:
        LOGGER.exception("Internal backlog sourcer failed; falling back to heuristic discovery")
        fallback_candidates = service._fallback_backlog_candidates_from_findings(findings)
        service._last_backlog_sourcing_activity = {
            "status": "failed",
            "summary": "internal sourcer 실행이 실패해 fallback discovery를 사용했습니다.",
            "error": str(exc),
            "mode": "fallback",
            "findings_count": len(findings),
            "candidate_count": len(fallback_candidates),
            "filtered_candidate_count": len(fallback_candidates),
            "raw_backlog_items_count": 0,
            "existing_backlog_count": len(existing_backlog),
            "findings_sample": findings_sample,
            "fallback_reason": str(exc),
            "session_id": "",
            "session_workspace": "",
            "elapsed_ms": 0,
        }
        return fallback_candidates
    monitoring = dict(sourced.get("monitoring") or {}) if isinstance(sourced.get("monitoring"), dict) else {}
    raw_items = sourced.get("backlog_items")
    if not isinstance(raw_items, list) or not raw_items:
        fallback_candidates = service._fallback_backlog_candidates_from_findings(findings)
        service._last_backlog_sourcing_activity = {
            "status": str(sourced.get("status") or "failed").strip().lower() or "failed",
            "summary": (
                str(sourced.get("summary") or "").strip()
                or "internal sourcer가 backlog item을 반환하지 않아 fallback discovery를 사용했습니다."
            ),
            "error": str(sourced.get("error") or "").strip(),
            "mode": "fallback",
            "findings_count": len(findings),
            "candidate_count": len(fallback_candidates),
            "filtered_candidate_count": len(fallback_candidates),
            "raw_backlog_items_count": int(monitoring.get("raw_backlog_items_count") or 0),
            "existing_backlog_count": len(existing_backlog),
            "findings_sample": monitoring.get("findings_sample") or findings_sample,
            "existing_backlog_sample": monitoring.get("existing_backlog_sample") or [],
            "fallback_reason": (
                str(sourced.get("error") or "").strip()
                or "internal sourcer가 backlog item을 반환하지 않았습니다."
            ),
            "session_id": str(sourced.get("session_id") or "").strip(),
            "session_workspace": str(sourced.get("session_workspace") or "").strip(),
            "elapsed_ms": int(monitoring.get("elapsed_ms") or 0),
            "reuse_session": bool(monitoring.get("reuse_session")),
            "prompt_chars": int(monitoring.get("prompt_chars") or 0),
            "json_parse_status": str(monitoring.get("json_parse_status") or "").strip(),
        }
        return fallback_candidates
    active_sprint_milestone = str(active_sprint.get("milestone_title") or "").strip()
    candidates: list[dict[str, Any]] = []
    milestone_filtered_out_count = 0
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        item_milestone = str(item.get("milestone_title") or "").strip()
        if active_sprint_milestone and item_milestone != active_sprint_milestone:
            milestone_filtered_out_count += 1
            continue
        candidates.append(
            build_backlog_item(
                title=title,
                summary=str(item.get("summary") or title).strip(),
                kind=str(item.get("kind") or "enhancement").strip().lower() or "enhancement",
                source="sourcer",
                scope=str(item.get("scope") or title).strip(),
                acceptance_criteria=service._normalize_backlog_acceptance_criteria(
                    item.get("acceptance_criteria")
                ),
                milestone_title=item_milestone,
                priority_rank=int(item.get("priority_rank") or 0),
                planned_in_sprint_id=str(item.get("planned_in_sprint_id") or "").strip(),
                added_during_active_sprint=bool(item.get("added_during_active_sprint")),
                origin={
                    "sourcing_agent": "internal_sourcer",
                    "sourcer_summary": str(sourced.get("summary") or "").strip(),
                    **dict(item.get("origin") or {}),
                },
            )
        )
    service._last_backlog_sourcing_activity = {
        "status": str(sourced.get("status") or "completed").strip().lower() or "completed",
        "summary": str(sourced.get("summary") or "").strip(),
        "error": str(sourced.get("error") or "").strip(),
        "mode": "internal_sourcer",
        "findings_count": len(findings),
        "candidate_count": len(candidates),
        "filtered_candidate_count": len(candidates),
        "active_sprint_milestone": active_sprint_milestone,
        "milestone_filtered_out_count": milestone_filtered_out_count,
        "raw_backlog_items_count": int(monitoring.get("raw_backlog_items_count") or len(raw_items)),
        "existing_backlog_count": len(existing_backlog),
        "findings_sample": monitoring.get("findings_sample") or findings_sample,
        "existing_backlog_sample": monitoring.get("existing_backlog_sample") or [],
        "session_id": str(sourced.get("session_id") or "").strip(),
        "session_workspace": str(sourced.get("session_workspace") or "").strip(),
        "elapsed_ms": int(monitoring.get("elapsed_ms") or 0),
        "reuse_session": bool(monitoring.get("reuse_session")),
        "prompt_chars": int(monitoring.get("prompt_chars") or 0),
        "json_parse_status": str(monitoring.get("json_parse_status") or "").strip(),
    }
    return candidates


def perform_backlog_sourcing(service: Any) -> tuple[int, int, list[dict[str, Any]]]:
    with service._backlog_sourcing_lock:
        candidates = service._discover_backlog_candidates()
        sourcing_activity = dict(service._last_backlog_sourcing_activity)
        normalized_candidates = service._normalize_sourcer_review_candidates(candidates)
        if normalized_candidates:
            fingerprint = service._build_sourcer_review_fingerprint(normalized_candidates)
            existing = service._find_open_sourcer_review_request(fingerprint)
            scheduler_state = service._load_scheduler_state()
            last_review_fingerprint = str(scheduler_state.get("last_sourcing_fingerprint") or "").strip()
            last_review_status = str(scheduler_state.get("last_sourcing_review_status") or "").strip().lower()
            last_review_request_id = str(scheduler_state.get("last_sourcing_review_request_id") or "").strip()
            if existing:
                sourcing_activity["suppressed_duplicate_count"] = len(normalized_candidates)
                sourcing_activity["suppressed_duplicate_fingerprint"] = fingerprint
                sourcing_activity["duplicate_request_id"] = str(existing.get("request_id") or "").strip()
                sourcing_activity["duplicate_review_status"] = "queued_for_planner_review"
                sourcing_activity["summary"] = "이미 planner review로 전달된 sourcer 후보라 재보고를 건너뛰었습니다."
                service._last_backlog_sourcing_activity = dict(sourcing_activity)
                return (0, 0, [])
            if (
                fingerprint
                and fingerprint == last_review_fingerprint
                and last_review_status in {"completed", "committed", "failed", "blocked", "cancelled"}
            ):
                sourcing_activity["suppressed_duplicate_count"] = len(normalized_candidates)
                sourcing_activity["suppressed_duplicate_fingerprint"] = fingerprint
                sourcing_activity["duplicate_request_id"] = last_review_request_id
                sourcing_activity["duplicate_review_status"] = last_review_status
                sourcing_activity["summary"] = "이미 보고한 sourcer 후보라 재발 근거 없이 반복 보고하지 않습니다."
                service._last_backlog_sourcing_activity = dict(sourcing_activity)
                return (0, 0, [])
        if not candidates:
            sourcing_activity["added_count"] = 0
            sourcing_activity["updated_count"] = 0
            service._last_backlog_sourcing_activity = dict(sourcing_activity)
            service._report_sourcer_activity_sync(
                sourcing_activity=sourcing_activity,
                added=0,
                updated=0,
                candidates=[],
            )
            return (0, 0, [])
        sourcing_activity["added_count"] = 0
        sourcing_activity["updated_count"] = 0
        service._last_backlog_sourcing_activity = dict(sourcing_activity)
        service._report_sourcer_activity_sync(
            sourcing_activity=sourcing_activity,
            added=0,
            updated=0,
            candidates=candidates,
        )
        return (0, 0, candidates)


async def maybe_queue_blocked_backlog_review_for_autonomous_start(
    service: Any,
    state: dict[str, Any],
) -> bool:
    blocked_candidates = await asyncio.to_thread(service._collect_blocked_backlog_review_candidates)
    if not blocked_candidates:
        if any(
            str(state.get(field) or "").strip()
            for field in (
                "last_blocked_review_at",
                "last_blocked_review_request_id",
                "last_blocked_review_status",
                "last_blocked_review_fingerprint",
            )
        ):
            service._clear_blocked_backlog_review_state(state)
            service._save_scheduler_state(state)
        return False
    fingerprint = service._build_blocked_backlog_review_fingerprint(blocked_candidates)
    existing = service._find_open_blocked_backlog_review_request(fingerprint)
    if existing:
        state["last_blocked_review_at"] = utc_now_iso()
        state["last_blocked_review_request_id"] = str(existing.get("request_id") or "")
        state["last_blocked_review_status"] = "queued_for_planner_review"
        state["last_blocked_review_fingerprint"] = fingerprint
        service._save_scheduler_state(state)
        return True
    last_status = str(state.get("last_blocked_review_status") or "").strip().lower()
    if (
        str(state.get("last_blocked_review_fingerprint") or "").strip() == fingerprint
        and last_status in {"completed", "committed", "failed", "blocked", "cancelled"}
    ):
        return False
    review_result = await service._queue_blocked_backlog_for_planner_review(blocked_candidates)
    request_id = str(review_result.get("request_id") or "")
    if not request_id:
        return False
    state["last_blocked_review_at"] = utc_now_iso()
    state["last_blocked_review_request_id"] = request_id
    state["last_blocked_review_status"] = "queued_for_planner_review"
    state["last_blocked_review_fingerprint"] = fingerprint
    service._save_scheduler_state(state)
    return True


def backlog_sourcing_interval_seconds(service: Any, *, minimum_interval_seconds: float) -> float:
    return max(float(service.runtime_config.sprint_interval_minutes) * 60.0, minimum_interval_seconds)


async def backlog_sourcing_loop(service: Any, *, poll_seconds: float) -> None:
    await asyncio.sleep(2.0)
    while True:
        try:
            await service._poll_backlog_sourcing_once()
        except Exception:
            LOGGER.exception("Backlog sourcing loop failed in orchestrator")
        await asyncio.sleep(poll_seconds)


async def poll_backlog_sourcing_once(service: Any) -> None:
    state = service._load_scheduler_state()
    last_sourced_at = service._parse_datetime(state.get("last_sourced_at") or "")
    now = utc_now()
    if last_sourced_at is not None:
        elapsed = (now - last_sourced_at).total_seconds()
        if elapsed < service._backlog_sourcing_interval_seconds():
            return
    added, updated, candidates = await asyncio.to_thread(service._perform_backlog_sourcing)
    state["last_sourced_at"] = utc_now_iso()
    if not (added or updated):
        if not candidates:
            suppressed_fingerprint = str(
                service._last_backlog_sourcing_activity.get("suppressed_duplicate_fingerprint") or ""
            ).strip()
            if suppressed_fingerprint:
                state["last_sourcing_status"] = "duplicate_suppressed"
                state["last_sourcing_request_id"] = str(
                    service._last_backlog_sourcing_activity.get("duplicate_request_id") or ""
                ).strip()
                state["last_sourcing_fingerprint"] = suppressed_fingerprint
            else:
                state["last_sourcing_status"] = "no_changes"
                state["last_sourcing_request_id"] = ""
            service._save_scheduler_state(state)
            return
        review_result = await service._queue_sourcer_candidates_for_planner_review(
            candidates,
            sourcing_activity=dict(service._last_backlog_sourcing_activity),
        )
        state["last_sourcing_status"] = (
            "queued_for_planner_review" if review_result.get("request_id") else "no_changes"
        )
        state["last_sourcing_request_id"] = str(review_result.get("request_id") or "")
        state["last_sourcing_fingerprint"] = str(review_result.get("fingerprint") or "").strip()
        state["last_sourcing_review_status"] = state["last_sourcing_status"]
        state["last_sourcing_review_request_id"] = str(review_result.get("request_id") or "")
        service._save_scheduler_state(state)
        if not review_result.get("request_id"):
            return
        await service._send_sprint_report(
            title="Backlog Sourcing",
            body=(
                f"candidate_count={len(candidates)}\n"
                f"planner_review_request_id={review_result.get('request_id') or ''}\n"
                f"planner_review_status={'reused' if review_result.get('reused') else 'delegated' if review_result.get('relay_sent') else 'relay_failed'}\n"
                f"items={', '.join(str(item.get('title') or '') for item in candidates[:5]) or 'N/A'}"
            ),
        )
        return
    state["last_sourcing_status"] = "updated"
    state["last_sourcing_request_id"] = ""
    service._save_scheduler_state(state)
    await service._send_sprint_report(
        title="Backlog Sourcing",
        body=(
            f"added={added}\n"
            f"updated={updated}\n"
            f"items={', '.join(str(item.get('title') or '') for item in candidates[:5]) or 'N/A'}"
        ),
    )


async def scheduler_loop(service: Any, *, poll_seconds: float) -> None:
    await asyncio.sleep(2.0)
    while True:
        try:
            await service._poll_scheduler_once()
        except Exception:
            LOGGER.exception("Scheduler loop failed in orchestrator")
        await asyncio.sleep(poll_seconds)


async def poll_scheduler_once(service: Any) -> None:
    state = service._load_scheduler_state()
    now = utc_now()
    next_slot = service._parse_datetime(state.get("next_slot_at") or "")
    if next_slot is None:
        next_slot = compute_next_slot_at(
            now,
            interval_minutes=service.runtime_config.sprint_interval_minutes,
            timezone_name=service.runtime_config.sprint_timezone,
        )
        state["next_slot_at"] = next_slot.isoformat()
        service._save_scheduler_state(state)
    if state.get("active_sprint_id"):
        await service._resume_active_sprint(str(state.get("active_sprint_id") or ""))
        state = service._load_scheduler_state()
        if state.get("active_sprint_id") and next_slot <= now and not state.get("deferred_slot_at"):
            state["deferred_slot_at"] = next_slot.isoformat()
            state["next_slot_at"] = compute_next_slot_at(
                now,
                interval_minutes=service.runtime_config.sprint_interval_minutes,
                timezone_name=service.runtime_config.sprint_timezone,
            ).isoformat()
            service._save_scheduler_state(state)
        return
    await service._maybe_request_idle_sprint_milestone(reason="idle_no_active_sprint")
    state = service._load_scheduler_state()
    if service._uses_manual_daily_sprint():
        next_cutoff = build_sprint_cutoff_at(service.runtime_config.sprint_cutoff_time, now=now)
        if next_cutoff <= now:
            next_cutoff = next_cutoff + timedelta(days=1)
        state["next_slot_at"] = next_cutoff.isoformat()
        service._save_scheduler_state(state)
        return
    if await service._maybe_queue_blocked_backlog_review_for_autonomous_start(state):
        return
    backlog_ready = any(
        str(item.get("status") or "").strip().lower() == "pending" for item in service._iter_backlog_items()
    )
    trigger = ""
    if state.get("deferred_slot_at"):
        trigger = "deferred_slot"
        state["deferred_slot_at"] = ""
    elif service.runtime_config.sprint_mode == "hybrid" and backlog_ready:
        trigger = "backlog_ready"
    elif next_slot <= now:
        trigger = "scheduled_slot"
        state["next_slot_at"] = compute_next_slot_at(
            now,
            interval_minutes=service.runtime_config.sprint_interval_minutes,
            timezone_name=service.runtime_config.sprint_timezone,
        ).isoformat()
    if not trigger:
        service._save_scheduler_state(state)
        return
    selected_items = await asyncio.to_thread(service._prepare_actionable_backlog_for_sprint)
    if not selected_items:
        state["last_skipped_at"] = utc_now_iso()
        state["last_skip_reason"] = "no_actionable_backlog"
        service._save_scheduler_state(state)
        return
    service._save_scheduler_state(state)
    await service._run_autonomous_sprint(trigger, selected_items=selected_items)
