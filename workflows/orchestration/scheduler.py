from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from teams_runtime.shared.persistence import utc_now_iso
from teams_runtime.workflows.sprints.lifecycle import (
    build_sprint_cutoff_at,
    compute_next_slot_at,
    utc_now,
)


LOGGER = logging.getLogger(__name__)


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

    def priority(item: dict[str, Any]) -> tuple[int, int, int, str]:
        priority_rank = int(item.get("priority_rank") or 0)
        if priority_rank > 0:
            return (0, -priority_rank, 0, str(item.get("created_at") or ""))
        source_rank = 0 if str(item.get("source") or "") == "user" else 1
        kind = str(item.get("kind") or "").strip().lower()
        kind_rank = {"bug": 0, "feature": 1, "enhancement": 2, "chore": 3}.get(kind, 4)
        return (1, source_rank, kind_rank, str(item.get("created_at") or ""))

    pending.sort(key=priority)
    return pending


async def maybe_queue_blocked_backlog_review_for_autonomous_start(
    service: Any,
    state: dict[str, Any],
) -> bool:
    blocked_candidates = await asyncio.to_thread(service._collect_blocked_backlog_review_candidates)
    if not blocked_candidates:
        closed_stale_reviews = False
        for request_record in service._find_open_blocked_backlog_review_requests():
            terminal_result = service._complete_blocked_backlog_review_if_no_action_terminal(
                request_record,
                summary=(
                    "active blocked backlog 후보가 없어 stale blocked_backlog_review를 "
                    "no-action terminal로 완료했습니다."
                ),
            )
            if terminal_result:
                closed_stale_reviews = True
                service._save_request(request_record)
        if closed_stale_reviews:
            state.update(service._load_scheduler_state())
        elif any(
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
    recent_terminal = service._find_recent_terminal_blocked_backlog_review_request(fingerprint)
    if recent_terminal:
        terminal_status = str(recent_terminal.get("status") or "").strip().lower()
        state["last_blocked_review_at"] = str(
            recent_terminal.get("updated_at") or recent_terminal.get("created_at") or utc_now_iso()
        )
        state["last_blocked_review_request_id"] = str(recent_terminal.get("request_id") or "")
        state["last_blocked_review_status"] = terminal_status
        state["last_blocked_review_fingerprint"] = str(recent_terminal.get("fingerprint") or "").strip()
        service._save_scheduler_state(state)
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
    if await service._poll_goal_sourcing_once(state):
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
