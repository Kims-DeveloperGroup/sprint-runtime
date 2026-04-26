from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.runtime.codex_runner import CodexRunner, extract_json_object
from teams_runtime.runtime.identities import service_runtime_identity
from teams_runtime.runtime.session_manager import RoleSessionManager
from teams_runtime.shared.models import MessageEnvelope, RequestRecord, RoleResult, RoleRuntimeConfig
from teams_runtime.workflows.roles import render_role_prompt_spec
from teams_runtime.workflows.roles.planner import normalize_planner_proposals


ALLOWED_ROLE_STATUSES = {"completed", "blocked", "failed"}
ROLE_STATUS_SCHEMA_ERROR_PREFIXES = ("missing status", "invalid status=")
STALE_SESSION_RUNTIME_ERROR_TERMS = (
    "thread/resume failed",
    "no rollout found for thread",
)
TERMINAL_TODO_STATUSES = {"completed", "committed", "verified", "done"}
WRITE_DENIAL_TERMS = (
    "operation not permitted",
    "permissionerror",
    "permission denied",
    "read-only",
    "readonly",
    "read only",
    "non-writable",
    "write denied",
    "쓰기 불가",
    "읽기 전용",
    "쓰기 제한",
    "writable하지",
    "writable root",
    "writable roots",
    "symlink target",
    "sandbox",
    "샌드박스",
)

LOGGER = logging.getLogger("teams_runtime.runtime.codex")


def _contains_write_denial_signal(text: str) -> bool:
    return any(term in text for term in WRITE_DENIAL_TERMS)


def _contains_stale_session_signal(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    return any(term in normalized for term in STALE_SESSION_RUNTIME_ERROR_TERMS)


def _truncate_log_text(value: Any, *, limit: int = 160) -> str:
    normalized = str(value or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 3, 0)].rstrip() + "..."


def _normalize_string_list_field(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return []


def _collapse_whitespace(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def normalize_role_payload(payload: dict[str, Any]) -> RoleResult:
    normalized = dict(payload) if isinstance(payload, dict) else {}
    validation_notes: list[str] = []
    blocking_notes: list[str] = []

    raw_status = normalized.get("status")
    status = raw_status.strip() if isinstance(raw_status, str) else ""
    if not status:
        note = "missing status"
        validation_notes.append(note)
        blocking_notes.append(note)
        status = "failed"
    elif status == "awaiting_approval":
        note = "approval flow is no longer supported; converted awaiting_approval to blocked"
        validation_notes.append(note)
        blocking_notes.append(note)
        status = "blocked"
    elif status not in ALLOWED_ROLE_STATUSES:
        note = f"invalid status={status}"
        validation_notes.append(note)
        blocking_notes.append(note)
        status = "failed"
    normalized["status"] = status

    normalized["summary"] = str(normalized.get("summary") or "").strip()

    raw_insights = normalized.get("insights")
    if isinstance(raw_insights, list):
        normalized["insights"] = [str(item).strip() for item in raw_insights if str(item).strip()]
    elif isinstance(raw_insights, str):
        normalized["insights"] = [raw_insights.strip()] if raw_insights.strip() else []
        validation_notes.append("coerced insights from string")
    else:
        normalized["insights"] = []
        if raw_insights not in (None, ""):
            validation_notes.append("reset invalid insights payload")

    raw_proposals = normalized.get("proposals")
    if isinstance(raw_proposals, dict):
        normalized["proposals"] = raw_proposals
    else:
        normalized["proposals"] = {}
        if raw_proposals not in (None, ""):
            validation_notes.append("reset invalid proposals payload")

    raw_artifacts = normalized.get("artifacts")
    if isinstance(raw_artifacts, list):
        normalized["artifacts"] = [str(item).strip() for item in raw_artifacts if str(item).strip()]
    elif isinstance(raw_artifacts, str):
        normalized["artifacts"] = [raw_artifacts.strip()] if raw_artifacts.strip() else []
        validation_notes.append("coerced artifacts from string")
    else:
        normalized["artifacts"] = []
        if raw_artifacts not in (None, ""):
            validation_notes.append("reset invalid artifacts payload")

    normalized["next_role"] = ""

    routing = normalized["proposals"].get("routing")
    if isinstance(routing, dict):
        sanitized_routing = dict(routing)
        sanitized_routing.pop("recommended_next_role", None)
        normalized["proposals"] = dict(normalized["proposals"])
        normalized["proposals"]["routing"] = sanitized_routing

    role = str(normalized.get("role") or "").strip().lower()
    if role == "planner":
        planner_proposals, planner_notes = normalize_planner_proposals(normalized["proposals"])
        normalized["proposals"] = planner_proposals
        validation_notes.extend(planner_notes)

    if bool(normalized.pop("approval_needed", False)):
        note = "approval flow is no longer supported; converted approval_needed to blocked"
        validation_notes.append(note)
        blocking_notes.append(note)
        normalized["status"] = "blocked"
    existing_error = str(normalized.get("error") or "").strip()
    normalized["validation_notes"] = validation_notes
    if blocking_notes:
        joined = "; ".join(blocking_notes)
        normalized["error"] = f"{existing_error} | {joined}".strip(" |")
    else:
        normalized["error"] = existing_error
    return normalized


def _role_status_schema_error(payload: dict[str, Any]) -> str:
    notes = payload.get("validation_notes") or []
    if not isinstance(notes, list):
        return ""
    for note in notes:
        normalized = str(note or "").strip()
        if any(normalized.startswith(prefix) for prefix in ROLE_STATUS_SCHEMA_ERROR_PREFIXES):
            return normalized
    return ""


class RoleAgentRuntime:
    def __init__(
        self,
        *,
        paths: RuntimePaths,
        role: str,
        sprint_id: str,
        runtime_config: RoleRuntimeConfig,
        agent_root: Path | None = None,
        session_identity: str | None = None,
    ):
        self.paths = paths
        self.role = role
        self.sprint_id = str(sprint_id or "").strip()
        self._agent_root = agent_root
        self.runtime_identity = str(session_identity or service_runtime_identity(role)).strip() or service_runtime_identity(role)
        self.session_manager = RoleSessionManager(
            paths,
            role,
            self.sprint_id,
            agent_root=agent_root,
            runtime_identity=self.runtime_identity,
        )
        self._session_managers: dict[str, RoleSessionManager] = {
            self.sprint_id: self.session_manager,
        }
        self.codex_runner = CodexRunner(runtime_config, role=role)
        self._run_lock = threading.Lock()

    def _resolve_request_sprint_id(
        self,
        envelope: MessageEnvelope,
        request_record: RequestRecord,
    ) -> str:
        request_params = (
            dict(request_record.get("params") or {})
            if isinstance(request_record.get("params"), dict)
            else {}
        )
        envelope_params = dict(envelope.params or {})
        return (
            str(request_record.get("sprint_id") or "").strip()
            or str(request_params.get("sprint_id") or "").strip()
            or str(envelope_params.get("sprint_id") or "").strip()
            or self.sprint_id
        )

    def _session_manager_for_sprint(self, sprint_id: str) -> RoleSessionManager:
        normalized = str(sprint_id or "").strip() or self.sprint_id
        manager = self._session_managers.get(normalized)
        if manager is not None:
            return manager
        manager = RoleSessionManager(
            self.paths,
            self.role,
            normalized,
            agent_root=self._agent_root,
            runtime_identity=self.runtime_identity,
        )
        self._session_managers[normalized] = manager
        return manager

    def _request_requires_default_bypass(
        self,
        envelope: MessageEnvelope,
        request_record: RequestRecord,
    ) -> bool:
        return True

    def _run_role_attempt(
        self,
        *,
        workspace: Path,
        prompt: str,
        session_id: str | None,
        default_bypass: bool,
        request_record: RequestRecord,
        request_id: str,
        sprint_id: str,
        todo_id: str,
        backlog_id: str,
    ) -> tuple[RoleResult, str | None]:
        output, resolved_session_id = self.codex_runner.run(
            workspace,
            prompt,
            session_id,
            bypass_sandbox=default_bypass,
        )
        active_session_id = resolved_session_id or session_id
        payload = self._parse_role_output(output, request_record)
        status_schema_error = _role_status_schema_error(payload)
        if status_schema_error:
            LOGGER.warning(
                "[%s] role_status_schema_error request_id=%s sprint_id=%s todo_id=%s backlog_id=%s workspace=%s session_id=%s error=%s retry_session_mode=fresh",
                self.role,
                request_id,
                sprint_id,
                todo_id,
                backlog_id,
                str(workspace),
                active_session_id or "new",
                status_schema_error,
            )
            output, resolved_session_id = self.codex_runner.run(
                workspace,
                self._build_status_schema_retry_prompt(prompt, status_schema_error),
                None,
                bypass_sandbox=default_bypass,
            )
            active_session_id = resolved_session_id or active_session_id
            payload = self._parse_role_output(output, request_record)
        if not default_bypass and self._should_retry_with_bypass(payload):
            retry_session_id = None if active_session_id else active_session_id
            LOGGER.warning(
                "[%s] sandbox_denial_detected retrying_with_bypass request_id=%s sprint_id=%s todo_id=%s backlog_id=%s workspace=%s session_id=%s retry_session_mode=%s",
                self.role,
                request_id,
                sprint_id,
                todo_id,
                backlog_id,
                str(workspace),
                active_session_id or "new",
                "fresh" if retry_session_id is None else "resume",
            )
            output, resolved_session_id = self.codex_runner.run(
                workspace,
                prompt,
                retry_session_id,
                bypass_sandbox=True,
            )
            active_session_id = resolved_session_id or active_session_id
            payload = self._parse_role_output(output, request_record)
        return payload, active_session_id

    def _build_runtime_error_payload(self, request_record: RequestRecord, error: str) -> RoleResult:
        payload = {
            "request_id": request_record["request_id"],
            "role": self.role,
            "status": "failed",
            "summary": "",
            "insights": [],
            "proposals": {},
            "artifacts": [],
            "error": str(error or "").strip() or "role execution failed",
        }
        return normalize_role_payload(payload)

    def _load_json_dict(self, path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _read_text_if_exists(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    def _extract_markdown_metadata(self, text: str, key: str) -> str:
        prefix = f"- {key}:"
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith(prefix):
                continue
            value = stripped[len(prefix) :].strip()
            return "" if value in {"", "N/A"} else value
        return ""

    def _extract_markdown_section_text(self, text: str, heading: str) -> str:
        if not text.strip():
            return ""
        lines = text.splitlines()
        collecting = False
        collected: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped == heading:
                collecting = True
                continue
            if collecting and stripped.startswith("## "):
                break
            if collecting and stripped:
                collected.append(stripped)
        return _collapse_whitespace(" ".join(collected))

    def _sprint_folder_name(self, sprint_state: dict[str, Any], sprint_id: str) -> str:
        explicit = str(sprint_state.get("sprint_folder_name") or "").strip()
        if explicit:
            return explicit
        sprint_folder = str(sprint_state.get("sprint_folder") or "").strip()
        if sprint_folder:
            return Path(sprint_folder).name
        return str(sprint_id or "").strip().replace(":", "-")

    def _build_milestone_snapshot_lines(self, sprint_state: dict[str, Any], sprint_id: str) -> list[str]:
        folder_name = self._sprint_folder_name(sprint_state, sprint_id)
        milestone_file = self.paths.sprint_artifact_file(folder_name, "milestone.md")
        milestone_text = self._read_text_if_exists(milestone_file)
        requested = (
            str(sprint_state.get("requested_milestone_title") or "").strip()
            or self._extract_markdown_metadata(milestone_text, "requested_milestone_title")
            or "없음"
        )
        active = (
            str(sprint_state.get("milestone_title") or "").strip()
            or str(sprint_state.get("refined_milestone") or "").strip()
            or self._extract_markdown_metadata(milestone_text, "revised_milestone_title")
            or "없음"
        )
        derived = self._extract_markdown_section_text(milestone_text, "## Latest Derived Framing")
        lines = [
            f"- requested_milestone: {requested}",
            f"- active_milestone: {active}",
        ]
        if derived:
            lines.append(f"- latest_derived_framing: {derived}")
        elif str(sprint_state.get("kickoff_brief") or "").strip():
            lines.append(
                f"- kickoff_brief: {_collapse_whitespace(str(sprint_state.get('kickoff_brief') or ''))}"
            )
        lines.append(f"- milestone_artifact: {milestone_file}")
        return lines

    def _format_handoff_todo_line(self, todo: dict[str, Any]) -> str:
        status = str(todo.get("status") or "").strip() or "unknown"
        title = str(todo.get("title") or todo.get("todo_id") or "Untitled").strip()
        owner = str(todo.get("owner_role") or "").strip() or "N/A"
        request_id = str(todo.get("request_id") or "").strip()
        summary = _collapse_whitespace(todo.get("summary") or "")
        blocked_reason = _collapse_whitespace(todo.get("blocked_reason") or "")
        recommended_next_step = _collapse_whitespace(todo.get("recommended_next_step") or "")
        artifacts = [str(item).strip() for item in (todo.get("artifacts") or []) if str(item).strip()]
        parts = [f"[{status}]", title, f"owner={owner}"]
        if request_id:
            parts.append(f"request_id={request_id}")
        if summary:
            parts.append(f"summary={summary}")
        if blocked_reason:
            parts.append(f"blocked={blocked_reason}")
        if recommended_next_step:
            parts.append(f"next={recommended_next_step}")
        if artifacts:
            parts.append(f"artifacts={len(artifacts)}")
        return "- " + " | ".join(parts)

    def _build_todo_snapshot_lines(self, sprint_state: dict[str, Any]) -> tuple[list[str], list[str]]:
        todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
        if not todos:
            selected_items = [dict(item) for item in (sprint_state.get("selected_items") or []) if isinstance(item, dict)]
            fallback = [
                "- [{status}] {title} | backlog_id={backlog_id}".format(
                    status=str(item.get("status") or "").strip() or "unknown",
                    title=str(item.get("title") or item.get("backlog_id") or "Untitled").strip(),
                    backlog_id=str(item.get("backlog_id") or "").strip() or "N/A",
                )
                for item in selected_items
            ]
            return ["- 완료된 todo 없음"], fallback or ["- todo 정보 없음"]
        completed: list[str] = []
        incomplete: list[str] = []
        for todo in todos:
            line = self._format_handoff_todo_line(todo)
            status = str(todo.get("status") or "").strip().lower()
            if status in TERMINAL_TODO_STATUSES:
                completed.append(line)
            else:
                incomplete.append(line)
        return completed or ["- 완료된 todo 없음"], incomplete or ["- 미완료 todo 없음"]

    def _role_report_events(self, request_record: RequestRecord) -> list[dict[str, Any]]:
        events = []
        for event in request_record.get("events") or []:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or event.get("event_type") or "").strip().lower()
            if event_type != "role_report":
                continue
            events.append(event)
        return events

    def _build_recent_role_progress_lines(self, request_record: RequestRecord) -> list[str]:
        latest_by_actor: list[dict[str, Any]] = []
        seen: set[str] = set()
        for event in reversed(self._role_report_events(request_record)):
            actor = str(event.get("actor") or "").strip() or "unknown"
            if actor in seen:
                continue
            seen.add(actor)
            latest_by_actor.append(event)
        latest_by_actor.reverse()
        if not latest_by_actor:
            return ["- 최근 role_report 없음"]
        lines = []
        for event in latest_by_actor:
            actor = str(event.get("actor") or "").strip() or "unknown"
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            status = str(payload.get("status") or "").strip() or "N/A"
            summary = _collapse_whitespace(event.get("summary") or payload.get("summary") or "")
            lines.append(f"- {actor}: [{status}] {summary or 'summary 없음'}")
        return lines

    def _latest_role_event(
        self,
        request_record: RequestRecord,
        *,
        actor: str | None = None,
        allowed_statuses: set[str] | None = None,
    ) -> dict[str, Any]:
        for event in reversed(self._role_report_events(request_record)):
            current_actor = str(event.get("actor") or "").strip()
            if actor is not None and current_actor != actor:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            status = str(payload.get("status") or "").strip().lower()
            if allowed_statuses is not None and status not in allowed_statuses:
                continue
            return event
        return {}

    def _latest_workflow_transition(self, request_record: RequestRecord) -> dict[str, Any]:
        result = request_record.get("result") if isinstance(request_record.get("result"), dict) else {}
        proposals = result.get("proposals") if isinstance(result.get("proposals"), dict) else {}
        transition = proposals.get("workflow_transition") if isinstance(proposals.get("workflow_transition"), dict) else {}
        if transition:
            return dict(transition)
        for event in reversed(self._role_report_events(request_record)):
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            proposals = payload.get("proposals") if isinstance(payload.get("proposals"), dict) else {}
            transition = proposals.get("workflow_transition") if isinstance(proposals.get("workflow_transition"), dict) else {}
            if transition:
                return dict(transition)
        return {}

    def _build_role_continuity_lines(
        self,
        request_record: RequestRecord,
        *,
        previous_state: Any,
        fresh_state: Any,
    ) -> list[str]:
        latest_same_role = self._latest_role_event(request_record, actor=self.role)
        latest_same_role_completed = self._latest_role_event(
            request_record,
            actor=self.role,
            allowed_statuses={"completed", "blocked"},
        )
        lines = [
            f"- continuing_role: {self.role}",
            "- continuation_mode: fresh_session_same_role",
            f"- stale_session_id: {str(getattr(previous_state, 'session_id', '') or '').strip() or '없음'}",
            f"- stale_session_workspace: {str(getattr(previous_state, 'workspace_path', '') or '').strip() or '없음'}",
            f"- fresh_session_workspace: {str(getattr(fresh_state, 'workspace_path', '') or '').strip() or '없음'}",
            "- role_private_state: ./todo.md, ./history.md, ./journal.md, ./sources/",
            "- shared_runtime_state: ./shared_workspace, ./.teams_runtime",
        ]
        if latest_same_role:
            payload = latest_same_role.get("payload") if isinstance(latest_same_role.get("payload"), dict) else {}
            lines.append(
                "- latest_same_role_report: [{status}] {summary}".format(
                    status=str(payload.get("status") or "").strip() or "N/A",
                    summary=_collapse_whitespace(latest_same_role.get("summary") or payload.get("summary") or "") or "summary 없음",
                )
            )
        if latest_same_role_completed:
            payload = (
                latest_same_role_completed.get("payload")
                if isinstance(latest_same_role_completed.get("payload"), dict)
                else {}
            )
            artifacts = [str(item).strip() for item in (payload.get("artifacts") or []) if str(item).strip()]
            if artifacts:
                lines.append("- latest_same_role_artifacts: " + ", ".join(artifacts))
        return lines

    def _build_current_task_lines(self, request_record: RequestRecord) -> list[str]:
        params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
        workflow = dict(params.get("workflow") or {}) if isinstance(params.get("workflow"), dict) else {}
        transition = self._latest_workflow_transition(request_record)
        unresolved_items = [
            _collapse_whitespace(item)
            for item in (transition.get("unresolved_items") or [])
            if _collapse_whitespace(item)
        ]
        lines = [
            f"- request_id: {str(request_record.get('request_id') or '').strip() or 'N/A'}",
            f"- backlog_id: {str(request_record.get('backlog_id') or '').strip() or 'N/A'}",
            f"- todo_id: {str(request_record.get('todo_id') or '').strip() or 'N/A'}",
            f"- current_role: {str(request_record.get('current_role') or '').strip() or 'N/A'}",
            f"- next_role: {str(request_record.get('next_role') or '').strip() or 'N/A'}",
            f"- workflow_phase: {str(workflow.get('phase') or '').strip() or 'N/A'}",
            f"- workflow_step: {str(workflow.get('step') or '').strip() or 'N/A'}",
            f"- phase_owner: {str(workflow.get('phase_owner') or '').strip() or 'N/A'}",
            f"- phase_status: {str(workflow.get('phase_status') or '').strip() or 'N/A'}",
        ]
        transition_reason = _collapse_whitespace(transition.get("reason") or "")
        if transition_reason:
            lines.append(f"- workflow_transition_reason: {transition_reason}")
        if unresolved_items:
            lines.append("- unresolved_items:")
            lines.extend(f"  - {item}" for item in unresolved_items[:5])
        latest_result = request_record.get("result") if isinstance(request_record.get("result"), dict) else {}
        latest_status = str(latest_result.get("status") or "").strip()
        latest_summary = _collapse_whitespace(latest_result.get("summary") or "")
        if latest_status or latest_summary:
            lines.append(
                "- latest_request_result: [{status}] {summary}".format(
                    status=latest_status or "N/A",
                    summary=latest_summary or "summary 없음",
                )
            )
        return lines

    def _build_required_next_action_lines(self, request_record: RequestRecord) -> list[str]:
        params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
        workflow = dict(params.get("workflow") or {}) if isinstance(params.get("workflow"), dict) else {}
        step = str(workflow.get("step") or "").strip().lower()
        lines = [
            f"- Continue as the same `{self.role}` role for this sprint with a fresh session, not as a new role.",
            "- Reconstruct state from the milestone, todo ledger, Current request, and linked shared artifacts before deciding.",
        ]
        if self.role == "planner" and step == "planner_finalize":
            lines.append(
                "- Finish `planner_finalize` by reconciling planner-owned sprint documents and returning a fresh planner result with explicit artifact paths and workflow transition evidence."
            )
        elif step:
            lines.append(
                f"- Resume the current workflow step `{step}` and return a strict role-result JSON grounded in directly checked evidence."
            )
        else:
            lines.append("- Resume the current role-owned action and return a strict role-result JSON grounded in directly checked evidence.")
        return lines

    def _build_retry_handoff_snapshot(
        self,
        envelope: MessageEnvelope,
        request_record: RequestRecord,
        *,
        current_sprint_id: str,
        previous_state: Any,
        fresh_state: Any,
    ) -> str:
        sprint_state = self._load_json_dict(self.paths.sprint_file(current_sprint_id))
        completed_todos, incomplete_todos = self._build_todo_snapshot_lines(sprint_state)
        sections = [
            "Sprint Handoff Snapshot",
            "",
            "Sprint Identity",
            f"- sprint_id: {current_sprint_id or 'N/A'}",
            f"- sprint_status: {str(sprint_state.get('status') or '').strip() or 'N/A'}",
            f"- sprint_trigger: {str(sprint_state.get('trigger') or '').strip() or 'N/A'}",
            "",
            "Current Sprint Milestone",
            *self._build_milestone_snapshot_lines(sprint_state, current_sprint_id),
            "",
            "Sprint Todos Completed",
            *completed_todos,
            "",
            "Sprint Todos Incomplete",
            *incomplete_todos,
            "",
            "Recent Role Progress",
            *self._build_recent_role_progress_lines(request_record),
            "",
            "Role Continuity",
            *self._build_role_continuity_lines(
                request_record,
                previous_state=previous_state,
                fresh_state=fresh_state,
            ),
            "",
            "Current Task",
            *self._build_current_task_lines(request_record),
            "",
            "Required Next Action",
            *self._build_required_next_action_lines(request_record),
            "",
            "Incoming Scope Reminder",
            f"- scope: {_collapse_whitespace(envelope.scope or request_record.get('scope') or '') or 'N/A'}",
            f"- body: {_collapse_whitespace(envelope.body or request_record.get('body') or '') or 'N/A'}",
        ]
        return "\n".join(sections)

    def _build_stale_session_retry_prompt(self, prompt: str, *, runtime_error: str, handoff_snapshot: str) -> str:
        return f"""{prompt}

The previous Codex session could not be resumed because the underlying rollout is no longer available.
- runtime_error: {runtime_error}

Start from a fresh Codex session, but continue the same sprint and the same role ownership.
Do not assume prior chat memory from the missing rollout still exists.
Use the handoff snapshot below plus the linked shared files to restore continuity before acting.

{handoff_snapshot}
"""

    def run_task(self, envelope: MessageEnvelope, request_record: RequestRecord) -> RoleResult:
        with self._run_lock:
            current_sprint_id = self._resolve_request_sprint_id(envelope, request_record)
            session_manager = self._session_manager_for_sprint(current_sprint_id)
            state = session_manager.ensure_session()
            prompt = self._build_prompt(
                envelope,
                request_record,
                current_sprint_id=current_sprint_id,
            )
            active_session_id = state.session_id or None
            request_id = str(request_record.get("request_id") or envelope.request_id or "").strip() or "unknown"
            sprint_id = current_sprint_id or "N/A"
            todo_id = str(request_record.get("todo_id") or "").strip() or "N/A"
            backlog_id = str(request_record.get("backlog_id") or "").strip() or "N/A"
            LOGGER.info(
                "[%s] task_start request_id=%s sprint_id=%s todo_id=%s backlog_id=%s intent=%s session_id=%s workspace=%s scope=%s",
                self.role,
                request_id,
                sprint_id,
                todo_id,
                backlog_id,
                str(envelope.intent or "route"),
                active_session_id or "new",
                state.workspace_path,
                _truncate_log_text(envelope.scope or request_record.get("scope") or "", limit=120) or "N/A",
            )
            default_bypass = self._request_requires_default_bypass(envelope, request_record)
            try:
                payload, active_session_id = self._run_role_attempt(
                    workspace=Path(state.workspace_path),
                    prompt=prompt,
                    session_id=active_session_id,
                    default_bypass=default_bypass,
                    request_record=request_record,
                    request_id=request_id,
                    sprint_id=sprint_id,
                    todo_id=todo_id,
                    backlog_id=backlog_id,
                )
            except RuntimeError as exc:
                runtime_error = str(exc) or "role execution failed"
                if _contains_stale_session_signal(runtime_error):
                    previous_state = state
                    state = session_manager.rotate_session(state)
                    active_session_id = None
                    retry_prompt = self._build_stale_session_retry_prompt(
                        prompt,
                        runtime_error=runtime_error,
                        handoff_snapshot=self._build_retry_handoff_snapshot(
                            envelope,
                            request_record,
                            current_sprint_id=current_sprint_id,
                            previous_state=previous_state,
                            fresh_state=state,
                        ),
                    )
                    LOGGER.warning(
                        "[%s] stale_session_detected retrying_with_fresh_session request_id=%s sprint_id=%s todo_id=%s backlog_id=%s stale_session_id=%s stale_workspace=%s fresh_workspace=%s",
                        self.role,
                        request_id,
                        sprint_id,
                        todo_id,
                        backlog_id,
                        str(getattr(previous_state, "session_id", "") or "").strip() or "none",
                        str(getattr(previous_state, "workspace_path", "") or "").strip() or "N/A",
                        state.workspace_path,
                    )
                    try:
                        payload, active_session_id = self._run_role_attempt(
                            workspace=Path(state.workspace_path),
                            prompt=retry_prompt,
                            session_id=None,
                            default_bypass=default_bypass,
                            request_record=request_record,
                            request_id=request_id,
                            sprint_id=sprint_id,
                            todo_id=todo_id,
                            backlog_id=backlog_id,
                        )
                    except RuntimeError as retry_exc:
                        runtime_error = str(retry_exc) or runtime_error
                        LOGGER.warning(
                            "[%s] task_runtime_error_after_fresh_retry request_id=%s sprint_id=%s todo_id=%s backlog_id=%s workspace=%s error=%s",
                            self.role,
                            request_id,
                            sprint_id,
                            todo_id,
                            backlog_id,
                            state.workspace_path,
                            runtime_error,
                        )
                        payload = self._build_runtime_error_payload(request_record, runtime_error)
                else:
                    LOGGER.warning(
                        "[%s] task_runtime_error request_id=%s sprint_id=%s todo_id=%s backlog_id=%s workspace=%s error=%s",
                        self.role,
                        request_id,
                        sprint_id,
                        todo_id,
                        backlog_id,
                        Path(state.workspace_path),
                        runtime_error,
                    )
                    payload = self._build_runtime_error_payload(request_record, runtime_error)
            state = session_manager.finalize_session_id(state, active_session_id)
            payload["request_id"] = request_record["request_id"]
            payload["role"] = self.role
            payload.setdefault("status", "completed")
            payload.setdefault("summary", "")
            payload.setdefault("insights", [])
            payload.setdefault("proposals", {})
            payload.setdefault("artifacts", [])
            payload.setdefault("next_role", "")
            payload.setdefault("error", "")
            payload.setdefault("session_id", state.session_id)
            payload.setdefault("session_workspace", state.workspace_path)
            LOGGER.info(
                "[%s] task_result request_id=%s sprint_id=%s todo_id=%s backlog_id=%s status=%s next_role=%s session_id=%s workspace=%s artifacts=%s summary=%s error=%s",
                self.role,
                request_id,
                sprint_id,
                todo_id,
                backlog_id,
                str(payload.get("status") or ""),
                str(payload.get("next_role") or ""),
                state.session_id or "unknown",
                state.workspace_path,
                len(payload.get("artifacts") or []),
                _truncate_log_text(payload.get("summary") or "", limit=180) or "없음",
                _truncate_log_text(payload.get("error") or "", limit=120) or "없음",
            )
            return payload

    def _parse_role_output(self, output: str, request_record: RequestRecord) -> RoleResult:
        try:
            payload = extract_json_object(output)
        except ValueError:
            payload = {
                "request_id": request_record["request_id"],
                "role": self.role,
                "status": "failed",
                "summary": output.strip()[:1000],
                "insights": [],
                "proposals": {},
                "artifacts": [],
                "error": "Role response did not contain valid JSON.",
            }
        payload["request_id"] = request_record["request_id"]
        payload["role"] = self.role
        return normalize_role_payload(payload)

    def _build_status_schema_retry_prompt(self, prompt: str, status_schema_error: str) -> str:
        return f"""{prompt}

Your previous role-result JSON violated the strict role-result schema:
- {status_schema_error}

Return the complete role-result JSON again.
The `status` field must be exactly one string from this enum: "completed", "blocked", "failed".
Do not return placeholders, combined candidates, uppercase variants, or missing status.
"""

    def _should_retry_with_bypass(self, payload: dict[str, Any]) -> bool:
        combined = " ".join(
            [
                str(payload.get("summary") or ""),
                str(payload.get("error") or ""),
                *(str(item or "") for item in (payload.get("insights") or [])),
            ]
        ).lower()
        if self.role == "version_controller":
            if not _contains_write_denial_signal(combined):
                return False
            return "index.lock" in combined or "sandbox" in combined
        if self.role == "planner":
            if not _contains_write_denial_signal(combined):
                return False
            return (
                "planner backlog persistence blocked" in combined
                or ".teams_runtime/backlog" in combined
                or "shared_workspace/backlog.md" in combined
                or "shared_workspace/completed_backlog.md" in combined
                or "shared_workspace/sprints" in combined
                or "shared_workspace 문서" in combined
                or "shared_workspace" in combined
                or "./shared_workspace" in combined
                or "sprint 문서" in combined
                or "backlog 저장소" in combined
                or "artifact_sync" in combined
                or "plan/spec/iteration" in combined
                or "plan.md" in combined
                or "spec.md" in combined
                or "iteration_log.md" in combined
                or "iteration 동기화" in combined
                or "./workspace" in combined
                or "planner 직접 persistence" in combined
                or "sandbox" in combined
            )
        if self.role == "orchestrator":
            if not _contains_write_denial_signal(combined):
                return False
            return (
                "sprint_scheduler.json" in combined
                or ".teams_runtime/sprints" in combined
                or ".teams_runtime/sprint_scheduler" in combined
                or "shared_workspace/current_sprint.md" in combined
                or "sprint lifecycle" in combined
                or "sandbox" in combined
            )
        return False

    def _build_prompt(
        self,
        envelope: MessageEnvelope,
        request_record: RequestRecord,
        *,
        current_sprint_id: str | None = None,
    ) -> str:
        resolved_sprint_id = str(current_sprint_id or "").strip() or self._resolve_request_sprint_id(
            envelope,
            request_record,
        )
        team_workspace_hint = "./workspace/teams_generated" if self.paths.workspace_root.name == "teams_generated" else "./workspace"
        role_specific_rules, extra_fields = render_role_prompt_spec(self.role, team_workspace_hint)
        return f"""You are the {self.role} role inside teams_runtime.

Use your role workspace files for team-private coordination.
The broader project workspace that contains teams_generated is available at ./workspace.
For teams runtime shared docs and sprint artifacts, prefer ./shared_workspace.
For teams runtime state such as requests, backlog, and sprint JSON, prefer ./.teams_runtime.
Inspect or modify ./workspace only when the request is about the broader project codebase or data workspace outside the teams runtime workspace.
The teams workspace root and its config files live in {team_workspace_hint}, not duplicated in this session root.
Use ./workspace_context.md if you need the exact path mapping for this session.

Return strict JSON only with this shape:
{{
  "request_id": "{request_record['request_id']}",
  "role": "{self.role}",
  "status": "<required: completed, blocked, or failed>",
  "summary": "short Korean summary",
  "insights": ["private role insight for journal.md"],
  "proposals": {{}},
  "artifacts": [],
  "error": ""
{extra_fields}
}}
The `status` value is mandatory and must be exactly one of these strings:
`completed`, `blocked`, or `failed`.
Do not copy the placeholder or return a combined candidate expression such as `completed|blocked|failed`.

Current sprint: {resolved_sprint_id}
Treat `Current request` as the source of truth.
The relay handoff is intentionally compact.
Use the relay handoff summary and any `sources/<request_id>.request.md` snapshot only as quick orientation.
Before deciding your next action, read the latest request `result` and recent `events` inside `Current request`.
If `Current request`, relay text, and a role-local request snapshot differ, trust `Current request`.
Orchestrator exclusively owns `next_role` selection. Do not choose or rely on `next_role` in your role output; make your summary, proposals, and artifacts explicit enough for orchestrator to choose the next step.
When `Current request.params.workflow` exists, use `proposals.workflow_transition` as the structured workflow contract. The expected shape is:
`{{"outcome":"continue|advance|reopen|block|complete","target_phase":"","target_step":"","requested_role":"","reopen_category":"","reason":"...","unresolved_items":[],"finalize_phase":false}}`.
Prefer `requested_role=designer|architect` only for planner-owned advisory requests. Other roles should describe the blocker or completion clearly and let orchestrator choose the next step from the workflow contract.
Never claim a file edit, test pass, verification result, or document reflection unless you directly observed it in the current session.
Separate observed facts from inference. If you did not open the file, run the command, or inspect the artifact yourself, say that explicitly and reduce the claim instead of reporting success.
When you claim a file change or validation result, leave enough evidence in `summary`, `insights`, or `proposals` for orchestrator to verify what you actually checked.
{role_specific_rules}

Current request:
{json.dumps(request_record, ensure_ascii=False, indent=2)}

Incoming envelope:
{json.dumps(envelope.to_dict(), ensure_ascii=False, indent=2)}
"""


__all__ = [
    "RoleAgentRuntime",
    "_collapse_whitespace",
    "normalize_role_payload",
]
