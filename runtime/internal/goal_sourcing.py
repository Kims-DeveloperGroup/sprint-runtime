from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from teams_runtime.runtime.codex_runner import CodexRunner, extract_json_object
from teams_runtime.runtime.execution_policy import ModelExecutionPolicy
from teams_runtime.runtime.identities import service_runtime_identity
from teams_runtime.runtime.model_telemetry import (
    InvocationSequence,
    ModelTelemetryRecorder,
    run_with_optional_telemetry,
)
from teams_runtime.runtime.session_manager import RoleSessionManager
from teams_runtime.shared.models import RoleRuntimeConfig, TelemetryRuntimeConfig
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import utc_now_iso


LOGGER = logging.getLogger(__name__)
SOURCER_LOG_PREFIX = "[goal-sourcer]"
ALLOWED_GOAL_SOURCER_STATUSES = {"sourced", "completed", "no_action", "failed"}
MAX_SHARED_WORKSPACE_DOC_FILES = 30
MAX_SHARED_WORKSPACE_DOC_CHARS = 2_000
MAX_SHARED_WORKSPACE_DOC_TOTAL_CHARS = 32_000


def _string_list(values: Any) -> list[str]:
    raw_values: list[Any]
    if isinstance(values, str):
        raw_values = [values]
    elif isinstance(values, list):
        raw_values = values
    else:
        raw_values = []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        value = str(item or "").strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(value)
    return normalized


def _append_completion_condition_requirement(requirements: list[str], completion_condition: str) -> list[str]:
    normalized_condition = str(completion_condition or "").strip()
    if not normalized_condition:
        return requirements
    return _string_list([*requirements, f"Completion condition: {normalized_condition}"])


def _relative_shared_workspace_path(paths: RuntimePaths, path: Path) -> str:
    try:
        relative = path.resolve().relative_to(paths.shared_workspace_root.resolve())
    except ValueError:
        return str(path)
    return f"./shared_workspace/{relative.as_posix()}"


def _is_attachment_path(path: Path) -> bool:
    return any(part == "attachments" for part in path.parts)


def _shared_doc_priority(paths: RuntimePaths, path: Path, goal_state: dict[str, Any]) -> tuple[int, str]:
    hint = _relative_shared_workspace_path(paths, path)
    lower_hint = hint.lower()
    goal_id = str(goal_state.get("goal_id") or "").strip().lower()
    if goal_id and f"/goals/{goal_id}/" in lower_hint:
        priority = 0
    elif lower_hint.endswith(("/current_sprint.md", "/planning.md", "/decision_log.md")):
        priority = 1
    elif lower_hint.endswith("/sprint_history/index.md"):
        priority = 2
    elif "/sprints/" in lower_hint or "/sprint_history/" in lower_hint:
        priority = 3
    else:
        priority = 4
    return (priority, hint)


def collect_shared_workspace_doc_context(
    paths: RuntimePaths,
    goal_state: dict[str, Any],
    *,
    max_files: int = MAX_SHARED_WORKSPACE_DOC_FILES,
    max_chars_per_file: int = MAX_SHARED_WORKSPACE_DOC_CHARS,
    max_total_chars: int = MAX_SHARED_WORKSPACE_DOC_TOTAL_CHARS,
) -> list[dict[str, str]]:
    root = paths.shared_workspace_root
    if not root.exists():
        return []
    root_resolved = root.resolve()
    candidates: list[Path] = []
    for path in root.rglob("*.md"):
        if not path.is_file() or _is_attachment_path(path.relative_to(root)):
            continue
        try:
            path.resolve().relative_to(root_resolved)
        except ValueError:
            continue
        candidates.append(path)
    candidates.sort(key=lambda path: _shared_doc_priority(paths, path, goal_state))
    docs: list[dict[str, str]] = []
    total_chars = 0
    for path in candidates:
        if len(docs) >= max_files or total_chars >= max_total_chars:
            break
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        excerpt = content.strip()
        if not excerpt:
            continue
        remaining = max_total_chars - total_chars
        excerpt = excerpt[: min(max_chars_per_file, remaining)].rstrip()
        if not excerpt:
            continue
        docs.append({"path": _relative_shared_workspace_path(paths, path), "excerpt": excerpt})
        total_chars += len(excerpt)
    return docs


def _sample_sprint_history(items: list[dict[str, Any]], *, limit: int = 4) -> list[str]:
    labels: list[str] = []
    for item in items[:limit]:
        labels.append(
            " | ".join(
                part
                for part in (
                    str(item.get("sprint_id") or "").strip(),
                    str(item.get("closeout_status") or item.get("status") or "").strip(),
                    str(item.get("milestone_title") or "").strip(),
                )
                if part
            )
        )
    return [item for item in labels if item]


def normalize_goal_sourcing_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload) if isinstance(payload, dict) else {}
    status = str(normalized.get("status") or "").strip().lower()
    if status == "active":
        status = "sourced"
    if status not in ALLOWED_GOAL_SOURCER_STATUSES:
        status = "failed" if str(normalized.get("error") or "").strip() else "no_action"
    normalized["status"] = status
    normalized["summary"] = str(normalized.get("summary") or "").strip()
    normalized["error"] = str(normalized.get("error") or "").strip()
    normalized["no_action_reason"] = str(normalized.get("no_action_reason") or "").strip()
    normalized["derived_stop_condition"] = str(
        normalized.get("derived_stop_condition")
        or normalized.get("stop_condition")
        or ""
    ).strip()

    completion = normalized.get("completion_decision")
    if isinstance(completion, bool):
        completion_decision = {"completed": completion, "reason": ""}
    elif isinstance(completion, dict):
        completion_decision = dict(completion)
        completion_decision["completed"] = bool(completion_decision.get("completed"))
        completion_decision["reason"] = str(completion_decision.get("reason") or "").strip()
        completion_decision["evidence"] = _string_list(completion_decision.get("evidence"))
    else:
        completion_decision = {"completed": status == "completed", "reason": "", "evidence": []}
    if completion_decision["completed"]:
        normalized["status"] = "completed"
    normalized["completion_decision"] = completion_decision

    raw_milestone = normalized.get("next_milestone") or normalized.get("milestone")
    if isinstance(raw_milestone, str):
        raw_milestone = {"title": raw_milestone}
    milestone: dict[str, Any] = dict(raw_milestone or {}) if isinstance(raw_milestone, dict) else {}
    title = str(
        milestone.get("title")
        or milestone.get("milestone_title")
        or milestone.get("name")
        or ""
    ).strip()
    completion_condition = str(
        milestone.get("completion_condition")
        or milestone.get("completion_criteria")
        or milestone.get("done_when")
        or ""
    ).strip()
    requirements = _string_list(
        [
            *_string_list(milestone.get("requirements")),
            *_string_list(milestone.get("acceptance_criteria")),
        ]
    )
    requirements = _append_completion_condition_requirement(requirements, completion_condition)
    artifacts = _string_list(
        [
            *_string_list(milestone.get("artifacts")),
            *_string_list(milestone.get("doc_refs")),
            *_string_list(milestone.get("reference_artifacts")),
        ]
    )
    normalized["next_milestone"] = {
        "title": title,
        "summary": str(milestone.get("summary") or milestone.get("brief") or "").strip(),
        "requirements": requirements,
        "artifacts": artifacts,
    }
    if title and normalized["status"] == "no_action":
        normalized["status"] = "sourced"
    return normalized


class GoalSourcingRuntime:
    def __init__(
        self,
        *,
        paths: RuntimePaths,
        sprint_id: str,
        runtime_config: RoleRuntimeConfig,
        session_identity: str | None = None,
        telemetry_config: TelemetryRuntimeConfig | None = None,
        execution_policy: ModelExecutionPolicy | None = None,
    ):
        self.paths = paths
        self.role = "sourcer"
        self.sprint_id = sprint_id
        self.runtime_identity = str(session_identity or service_runtime_identity(self.role)).strip() or service_runtime_identity(self.role)
        self.session_manager = RoleSessionManager(
            paths,
            self.role,
            sprint_id,
            agent_root=paths.internal_agent_root("sourcer"),
            runtime_identity=self.runtime_identity,
        )
        self.telemetry_recorder = ModelTelemetryRecorder(paths, self.runtime_identity, telemetry_config)
        self.codex_runner = CodexRunner(
            runtime_config,
            role=self.role,
            telemetry_recorder=self.telemetry_recorder,
            execution_policy=execution_policy,
        )
        self._run_lock = threading.Lock()

    def source(
        self,
        *,
        goal_state: dict[str, Any],
        scheduler_state: dict[str, Any],
        current_sprint: dict[str, Any],
        recent_sprint_history: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self._run_lock:
            started_monotonic = time.monotonic()
            previous_state = self.session_manager.load()
            reused_session = (
                previous_state is not None
                and previous_state.sprint_id == self.sprint_id
                and Path(previous_state.workspace_path).is_dir()
            )
            monitoring: dict[str, Any] = {
                "started_at": utc_now_iso(),
                "reuse_session": reused_session,
                "goal_id": str(goal_state.get("goal_id") or "").strip(),
                "history_count": len(recent_sprint_history),
                "history_sample": _sample_sprint_history(recent_sprint_history),
            }
            shared_workspace_docs = collect_shared_workspace_doc_context(self.paths, goal_state)
            monitoring["shared_workspace_doc_count"] = len(shared_workspace_docs)
            monitoring["shared_workspace_doc_sample"] = [
                str(item.get("path") or "") for item in shared_workspace_docs[:5]
            ]
            state = self.session_manager.ensure_session()
            monitoring["session_workspace"] = state.workspace_path
            monitoring["session_id_before"] = state.session_id or ""
            prompt = self._build_prompt(
                goal_state=goal_state,
                scheduler_state=scheduler_state,
                current_sprint=current_sprint,
                recent_sprint_history=recent_sprint_history,
                shared_workspace_docs=shared_workspace_docs,
            )
            monitoring["prompt_chars"] = len(prompt)
            try:
                LOGGER.info(
                    "%s codex_run_start goal_id=%s workspace=%s previous_session_id=%s",
                    SOURCER_LOG_PREFIX,
                    monitoring["goal_id"],
                    state.workspace_path,
                    state.session_id or "new",
                )
                invocation_sequence = InvocationSequence(
                    runtime_identity=self.runtime_identity,
                    role=self.role,
                    purpose="goal_sourcing",
                    sprint_id=str(current_sprint.get("sprint_id") or self.sprint_id),
                    goal_id=str(goal_state.get("goal_id") or ""),
                )
                output, session_id = run_with_optional_telemetry(
                    self.codex_runner,
                    Path(state.workspace_path),
                    prompt,
                    state.session_id or None,
                    invocation_context=invocation_sequence.next("primary"),
                )
            except Exception:
                monitoring["codex_run_status"] = "failed"
                monitoring["completed_at"] = utc_now_iso()
                monitoring["elapsed_ms"] = int((time.monotonic() - started_monotonic) * 1000)
                LOGGER.exception("%s codex_run_failed goal_id=%s", SOURCER_LOG_PREFIX, monitoring["goal_id"])
                raise
            if session_id:
                self.session_manager.save(session_id, state.workspace_path)
            monitoring["session_id"] = session_id or state.session_id or ""
            try:
                parsed = json.loads(extract_json_object(output))
            except Exception:
                parsed = {
                    "status": "failed",
                    "summary": "Goal sourcer response did not contain valid JSON.",
                    "error": "Goal sourcer response did not contain valid JSON.",
                }
                monitoring["json_parse_status"] = "failed"
            else:
                monitoring["json_parse_status"] = "parsed"
            result = normalize_goal_sourcing_payload(parsed)
            monitoring["completed_at"] = utc_now_iso()
            monitoring["elapsed_ms"] = int((time.monotonic() - started_monotonic) * 1000)
            result["monitoring"] = monitoring
            result["session_id"] = monitoring["session_id"]
            result["session_workspace"] = state.workspace_path
            LOGGER.info(
                "%s completed goal_id=%s status=%s elapsed_ms=%s",
                SOURCER_LOG_PREFIX,
                monitoring["goal_id"],
                result.get("status") or "",
                monitoring["elapsed_ms"],
            )
            return result

    def _build_prompt(
        self,
        *,
        goal_state: dict[str, Any],
        scheduler_state: dict[str, Any],
        current_sprint: dict[str, Any],
        recent_sprint_history: list[dict[str, Any]],
        shared_workspace_docs: list[dict[str, str]] | None = None,
    ) -> str:
        goal_context = {
            "goal_id": goal_state.get("goal_id") or "",
            "objective": goal_state.get("objective") or "",
            "stop_condition": goal_state.get("stop_condition") or "",
            "status": goal_state.get("status") or "",
            "sourced_milestones": list(goal_state.get("sourced_milestones") or [])[-10:],
            "sprint_outcomes": list(goal_state.get("sprint_outcomes") or [])[-10:],
        }
        scheduler_context = {
            "active_sprint_id": scheduler_state.get("active_sprint_id") or "",
            "next_slot_at": scheduler_state.get("next_slot_at") or "",
            "last_trigger": scheduler_state.get("last_trigger") or "",
            "current_sprint": current_sprint or {},
            "recent_sprint_history": recent_sprint_history[-10:],
        }
        docs_context = shared_workspace_docs
        if docs_context is None:
            docs_context = collect_shared_workspace_doc_context(self.paths, goal_state)
        return f"""You are the internal sourcer for teams_runtime goal-driven sprint planning.

Your job is not autonomous backlog discovery. Your job is to advance exactly one operator-created goal.

Rules:
- If the goal has no stop_condition, derive a concrete stop condition first and return it as derived_stop_condition.
- Decide whether the stop condition is satisfied from the sprint_outcomes and recent_sprint_history.
- If satisfied, return status="completed" and completion_decision.completed=true with concise evidence.
- If not satisfied and no active sprint exists, return one next_milestone that should be started now.
- next_milestone must be sprint-ready: include a title, summary, requirements, and completion_condition.
- completion_condition is converted into a kickoff requirement, so write it as a concrete sprint closeout condition.
- Use shared workspace docs as read-only context. Do not rewrite, summarize, or bulk-plan those docs.
- Source only one milestone. Do not emit backlog items, planner review requests, execution tasks, or multi-sprint roadmaps.
- If no milestone should start, return status="no_action" with no_action_reason.
- Return only one JSON object with this shape:
{{
  "status": "sourced|completed|no_action|failed",
  "summary": "short sourcing summary",
  "derived_stop_condition": "required when missing",
  "completion_decision": {{
    "completed": false,
    "reason": "why complete or incomplete",
    "evidence": ["brief evidence"]
  }},
  "next_milestone": {{
    "title": "one sprint milestone title",
    "summary": "why this milestone advances the goal",
    "requirements": ["sprint kickoff requirement"],
    "completion_condition": "single concrete condition that proves this sprint is done",
    "artifacts": ["optional reference artifact"]
  }},
  "no_action_reason": "",
  "error": ""
}}

Goal context:
```json
{json.dumps(goal_context, ensure_ascii=False, indent=2)}
```

Scheduler context:
```json
{json.dumps(scheduler_context, ensure_ascii=False, indent=2)}
```

Shared workspace docs:
```json
{json.dumps(docs_context, ensure_ascii=False, indent=2)}
```
"""


__all__ = [
    "GoalSourcingRuntime",
    "collect_shared_workspace_doc_context",
    "normalize_goal_sourcing_payload",
]
