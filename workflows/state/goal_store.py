from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from teams_runtime.shared.models import GoalState
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import (
    append_jsonl,
    iter_jsonl_records,
    normalize_runtime_datetime,
    read_json,
    utc_now_iso,
    write_json,
)


GOAL_STATUSES = {"active", "paused", "completed", "cancelled", "failed"}
RESUMABLE_GOAL_STATUSES = {"active", "paused"}
TERMINAL_GOAL_STATUSES = {"completed", "cancelled", "failed"}


def new_goal_id(now: datetime | None = None) -> str:
    current = normalize_runtime_datetime(now)
    return f"goal-{current.strftime('%Y%m%d')}-{secrets.token_hex(4)}"


def normalize_goal_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    return normalized if normalized in GOAL_STATUSES else "active"


def load_goal_scheduler(paths: RuntimePaths) -> dict[str, Any]:
    payload = read_json(paths.goal_scheduler_file)
    return payload if isinstance(payload, dict) else {}


def save_goal_scheduler(paths: RuntimePaths, state: dict[str, Any]) -> None:
    payload = dict(state or {})
    payload["updated_at"] = utc_now_iso()
    write_json(paths.goal_scheduler_file, payload)


def active_goal_id(paths: RuntimePaths) -> str:
    return str(load_goal_scheduler(paths).get("active_goal_id") or "").strip()


def set_active_goal_id(paths: RuntimePaths, goal_id: str) -> None:
    state = load_goal_scheduler(paths)
    state["active_goal_id"] = str(goal_id or "").strip()
    save_goal_scheduler(paths, state)


def clear_active_goal_id(paths: RuntimePaths, goal_id: str = "") -> None:
    state = load_goal_scheduler(paths)
    current = str(state.get("active_goal_id") or "").strip()
    if goal_id and current and current != str(goal_id or "").strip():
        return
    state["active_goal_id"] = ""
    save_goal_scheduler(paths, state)


def load_goal(paths: RuntimePaths, goal_id: str) -> GoalState:
    normalized_goal_id = str(goal_id or "").strip()
    if not normalized_goal_id:
        return {}
    return read_json(paths.goal_file(normalized_goal_id))


def save_goal(paths: RuntimePaths, goal: GoalState, *, update_timestamp: bool = True) -> None:
    goal_id = str(goal.get("goal_id") or "").strip()
    if not goal_id:
        return
    payload = dict(goal)
    if update_timestamp:
        payload["updated_at"] = utc_now_iso()
    write_json(paths.goal_file(goal_id), payload)


def iter_goal_states(paths: RuntimePaths) -> list[GoalState]:
    if not paths.goals_dir.exists():
        return []
    goals: list[GoalState] = []
    for goal_dir in sorted(paths.goals_dir.iterdir()):
        if not goal_dir.is_dir():
            continue
        payload = read_json(goal_dir / "goal.json")
        if payload:
            goals.append(payload)
    goals.sort(key=lambda goal: str(goal.get("created_at") or ""))
    return goals


def load_current_goal(paths: RuntimePaths) -> GoalState:
    goal = load_goal(paths, active_goal_id(paths))
    if not goal:
        return {}
    if normalize_goal_status(str(goal.get("status") or "")) not in RESUMABLE_GOAL_STATUSES:
        clear_active_goal_id(paths, str(goal.get("goal_id") or ""))
        return {}
    return goal


def load_active_goal(paths: RuntimePaths) -> GoalState:
    goal = load_current_goal(paths)
    if normalize_goal_status(str(goal.get("status") or "")) != "active":
        return {}
    return goal


def append_goal_event(
    paths: RuntimePaths,
    goal_id: str,
    *,
    event_type: str,
    actor: str,
    summary: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event = {
        "timestamp": utc_now_iso(),
        "type": str(event_type or "").strip(),
        "actor": str(actor or "").strip(),
        "summary": str(summary or "").strip(),
    }
    if payload:
        event["payload"] = dict(payload)
    append_jsonl(paths.goal_events_file(goal_id), event)
    return event


def iter_goal_events(paths: RuntimePaths, goal_id: str) -> list[dict[str, Any]]:
    return list(iter_jsonl_records(paths.goal_events_file(goal_id)))


def build_goal_state(
    *,
    objective: str,
    stop_condition: str = "",
    goal_id: str | None = None,
    created_at: str | None = None,
) -> GoalState:
    now = created_at or utc_now_iso()
    return {
        "goal_id": str(goal_id or new_goal_id()).strip(),
        "objective": str(objective or "").strip(),
        "stop_condition": str(stop_condition or "").strip(),
        "status": "active",
        "sourced_milestones": [],
        "linked_sprint_ids": [],
        "sprint_outcomes": [],
        "completion_evidence": {},
        "cancellation_evidence": {},
        "failure_evidence": {},
        "final_report_path": "",
        "final_report_archive_path": "",
        "created_at": now,
        "updated_at": now,
        "paused_at": "",
        "resumed_at": "",
        "completed_at": "",
        "cancelled_at": "",
        "failed_at": "",
    }


def create_goal(paths: RuntimePaths, *, objective: str, stop_condition: str = "") -> GoalState:
    normalized_objective = str(objective or "").strip()
    if not normalized_objective:
        raise ValueError("objective must be a non-empty string")
    existing = load_current_goal(paths)
    if existing:
        raise ValueError(f"goal already exists: {existing.get('goal_id')}")
    goal = build_goal_state(objective=normalized_objective, stop_condition=stop_condition)
    save_goal(paths, goal, update_timestamp=False)
    set_active_goal_id(paths, str(goal["goal_id"]))
    append_goal_event(
        paths,
        str(goal["goal_id"]),
        event_type="started",
        actor="cli",
        summary="Goal started.",
        payload={"objective": goal["objective"], "stop_condition": goal["stop_condition"]},
    )
    return goal

def update_goal_stop_condition(paths: RuntimePaths, goal: GoalState, stop_condition: str) -> GoalState:
    normalized = str(stop_condition or "").strip()
    if not normalized or normalized == str(goal.get("stop_condition") or "").strip():
        return goal
    goal["stop_condition"] = normalized
    goal["updated_at"] = utc_now_iso()
    save_goal(paths, goal, update_timestamp=False)
    append_goal_event(
        paths,
        str(goal.get("goal_id") or ""),
        event_type="stop_condition_derived",
        actor="sourcer",
        summary="Goal stop condition derived.",
        payload={"stop_condition": normalized},
    )
    return goal


def pause_goal(paths: RuntimePaths, goal: GoalState, *, actor: str = "cli", reason: str = "") -> GoalState:
    if normalize_goal_status(str(goal.get("status") or "")) != "active":
        return goal
    goal["status"] = "paused"
    goal["paused_at"] = utc_now_iso()
    goal["updated_at"] = goal["paused_at"]
    save_goal(paths, goal, update_timestamp=False)
    append_goal_event(
        paths,
        str(goal.get("goal_id") or ""),
        event_type="paused",
        actor=actor,
        summary=reason or "Goal paused.",
    )
    return goal


def resume_goal(paths: RuntimePaths, goal: GoalState, *, actor: str = "cli") -> GoalState:
    if normalize_goal_status(str(goal.get("status") or "")) != "paused":
        return goal
    goal["status"] = "active"
    goal["resumed_at"] = utc_now_iso()
    goal["updated_at"] = goal["resumed_at"]
    save_goal(paths, goal, update_timestamp=False)
    set_active_goal_id(paths, str(goal.get("goal_id") or ""))
    append_goal_event(
        paths,
        str(goal.get("goal_id") or ""),
        event_type="resumed",
        actor=actor,
        summary="Goal resumed.",
    )
    return goal


def record_sourced_milestone(
    paths: RuntimePaths,
    goal: GoalState,
    *,
    milestone_title: str,
    summary: str = "",
    sprint_id: str = "",
    requirements: list[str] | None = None,
    artifacts: list[str] | None = None,
) -> GoalState:
    normalized_milestone = str(milestone_title or "").strip()
    if not normalized_milestone:
        return goal
    entry = {
        "milestone_title": normalized_milestone,
        "summary": str(summary or "").strip(),
        "sprint_id": str(sprint_id or "").strip(),
        "requirements": [str(item).strip() for item in (requirements or []) if str(item).strip()],
        "artifacts": [str(item).strip() for item in (artifacts or []) if str(item).strip()],
        "sourced_at": utc_now_iso(),
        "status": "started" if str(sprint_id or "").strip() else "sourced",
    }
    goal.setdefault("sourced_milestones", []).append(entry)
    if entry["sprint_id"]:
        linked = [str(item).strip() for item in goal.get("linked_sprint_ids") or [] if str(item).strip()]
        if entry["sprint_id"] not in linked:
            linked.append(entry["sprint_id"])
        goal["linked_sprint_ids"] = linked
    save_goal(paths, goal)
    append_goal_event(
        paths,
        str(goal.get("goal_id") or ""),
        event_type="milestone_sourced",
        actor="sourcer",
        summary=f"Milestone sourced: {normalized_milestone}",
        payload=entry,
    )
    return goal


def record_goal_sprint_outcome(
    paths: RuntimePaths,
    goal: GoalState,
    sprint_state: dict[str, Any],
    closeout_result: dict[str, Any] | None = None,
) -> GoalState:
    sprint_id = str(sprint_state.get("sprint_id") or "").strip()
    if not sprint_id:
        return goal
    closeout = dict(closeout_result or {})
    outcome = {
        "sprint_id": sprint_id,
        "milestone_title": str(sprint_state.get("milestone_title") or "").strip(),
        "status": str(sprint_state.get("status") or "").strip(),
        "closeout_status": str(sprint_state.get("closeout_status") or closeout.get("status") or "").strip(),
        "summary": str(closeout.get("message") or sprint_state.get("report_summary") or "").strip(),
        "report_path": str(sprint_state.get("report_path") or "").strip(),
        "commit_sha": str(sprint_state.get("commit_sha") or closeout.get("representative_commit_sha") or "").strip(),
        "commit_shas": [
            str(item).strip()
            for item in (sprint_state.get("commit_shas") or closeout.get("commit_shas") or [])
            if str(item).strip()
        ],
        "ended_at": str(sprint_state.get("ended_at") or "").strip(),
        "recorded_at": utc_now_iso(),
    }
    outcomes = [dict(item) for item in goal.get("sprint_outcomes") or [] if isinstance(item, dict)]
    replaced = False
    for index, existing in enumerate(outcomes):
        if str(existing.get("sprint_id") or "").strip() == sprint_id:
            outcomes[index] = outcome
            replaced = True
            break
    if not replaced:
        outcomes.append(outcome)
    goal["sprint_outcomes"] = outcomes
    linked = [str(item).strip() for item in goal.get("linked_sprint_ids") or [] if str(item).strip()]
    if sprint_id not in linked:
        linked.append(sprint_id)
    goal["linked_sprint_ids"] = linked
    for milestone in goal.get("sourced_milestones") or []:
        if not isinstance(milestone, dict):
            continue
        if str(milestone.get("sprint_id") or "").strip() == sprint_id:
            milestone["status"] = "closed"
            milestone["closed_at"] = outcome["recorded_at"]
            milestone["closeout_status"] = outcome["closeout_status"]
    save_goal(paths, goal)
    append_goal_event(
        paths,
        str(goal.get("goal_id") or ""),
        event_type="sprint_outcome_recorded",
        actor="orchestrator",
        summary=f"Sprint outcome recorded: {sprint_id}",
        payload=outcome,
    )
    return goal


def render_goal_final_report(goal: GoalState, *, terminal_reason: str = "") -> str:
    status = normalize_goal_status(str(goal.get("status") or ""))
    lines = [
        f"# Goal Final Report: {goal.get('goal_id') or ''}",
        "",
        f"- status: {status}",
        f"- objective: {goal.get('objective') or ''}",
        f"- stop_condition: {goal.get('stop_condition') or 'N/A'}",
        f"- created_at: {goal.get('created_at') or ''}",
        f"- completed_at: {goal.get('completed_at') or ''}",
        f"- cancelled_at: {goal.get('cancelled_at') or ''}",
        f"- failed_at: {goal.get('failed_at') or ''}",
        f"- terminal_reason: {terminal_reason or 'N/A'}",
        "",
        "## Sourced Milestones",
        "",
    ]
    milestones = [dict(item) for item in goal.get("sourced_milestones") or [] if isinstance(item, dict)]
    if not milestones:
        lines.append("- None")
    for item in milestones:
        lines.append(
            "- "
            f"{item.get('milestone_title') or 'N/A'} "
            f"(sprint_id={item.get('sprint_id') or 'N/A'}, status={item.get('status') or 'N/A'})"
        )
    lines.extend(["", "## Sprint Outcomes", ""])
    outcomes = [dict(item) for item in goal.get("sprint_outcomes") or [] if isinstance(item, dict)]
    if not outcomes:
        lines.append("- None")
    for item in outcomes:
        lines.append(
            "- "
            f"{item.get('sprint_id') or 'N/A'}: "
            f"{item.get('status') or 'N/A'} / {item.get('closeout_status') or 'N/A'} "
            f"- {item.get('milestone_title') or 'N/A'}"
        )
    evidence_key = {
        "completed": "completion_evidence",
        "cancelled": "cancellation_evidence",
        "failed": "failure_evidence",
    }.get(status, "")
    evidence = dict(goal.get(evidence_key) or {}) if evidence_key else {}
    if evidence:
        lines.extend(["", "## Evidence", ""])
        for key in sorted(evidence):
            lines.append(f"- {key}: {evidence[key]}")
    return "\n".join(lines).rstrip() + "\n"


def archive_goal_final_report(
    paths: RuntimePaths,
    goal: GoalState,
    *,
    terminal_reason: str = "",
) -> str:
    goal_id = str(goal.get("goal_id") or "").strip()
    if not goal_id:
        return ""
    report = render_goal_final_report(goal, terminal_reason=terminal_reason)
    shared_path = paths.shared_goal_report_file(goal_id)
    archive_path = paths.goal_archive_report_file(goal_id)
    shared_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    shared_path.write_text(report, encoding="utf-8")
    archive_path.write_text(report, encoding="utf-8")
    goal["final_report_path"] = str(shared_path)
    goal["final_report_archive_path"] = str(archive_path)
    save_goal(paths, goal)
    return report


def complete_goal(
    paths: RuntimePaths,
    goal: GoalState,
    *,
    evidence: dict[str, Any] | None = None,
    actor: str = "sourcer",
) -> GoalState:
    if normalize_goal_status(str(goal.get("status") or "")) in TERMINAL_GOAL_STATUSES:
        return goal
    goal["status"] = "completed"
    goal["completed_at"] = utc_now_iso()
    goal["completion_evidence"] = dict(evidence or {})
    goal["updated_at"] = goal["completed_at"]
    save_goal(paths, goal, update_timestamp=False)
    clear_active_goal_id(paths, str(goal.get("goal_id") or ""))
    append_goal_event(
        paths,
        str(goal.get("goal_id") or ""),
        event_type="completed",
        actor=actor,
        summary="Goal completed.",
        payload=goal["completion_evidence"],
    )
    return goal


def cancel_goal(
    paths: RuntimePaths,
    goal: GoalState,
    *,
    evidence: dict[str, Any] | None = None,
    actor: str = "cli",
) -> GoalState:
    if normalize_goal_status(str(goal.get("status") or "")) in TERMINAL_GOAL_STATUSES:
        return goal
    goal["status"] = "cancelled"
    goal["cancelled_at"] = utc_now_iso()
    goal["cancellation_evidence"] = dict(evidence or {})
    goal["updated_at"] = goal["cancelled_at"]
    save_goal(paths, goal, update_timestamp=False)
    clear_active_goal_id(paths, str(goal.get("goal_id") or ""))
    append_goal_event(
        paths,
        str(goal.get("goal_id") or ""),
        event_type="cancelled",
        actor=actor,
        summary="Goal cancelled.",
        payload=goal["cancellation_evidence"],
    )
    return goal


def fail_goal(
    paths: RuntimePaths,
    goal: GoalState,
    *,
    evidence: dict[str, Any] | None = None,
    actor: str = "sourcer",
) -> GoalState:
    if normalize_goal_status(str(goal.get("status") or "")) in TERMINAL_GOAL_STATUSES:
        return goal
    goal["status"] = "failed"
    goal["failed_at"] = utc_now_iso()
    goal["failure_evidence"] = dict(evidence or {})
    goal["updated_at"] = goal["failed_at"]
    save_goal(paths, goal, update_timestamp=False)
    clear_active_goal_id(paths, str(goal.get("goal_id") or ""))
    append_goal_event(
        paths,
        str(goal.get("goal_id") or ""),
        event_type="failed",
        actor=actor,
        summary="Goal failed.",
        payload=goal["failure_evidence"],
    )
    return goal
