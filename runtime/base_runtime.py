from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.runtime.codex_runner import CodexRunner, extract_json_object
from teams_runtime.runtime.identities import service_runtime_identity
from teams_runtime.runtime.role_result_contract import (
    ALLOWED_ROLE_STATUSES,
    CONTRACT_STATUS_INVALID,
    CONTRACT_STATUS_REPAIRED,
    PROMPT_PLACEHOLDER_INSIGHT,
    PROMPT_PLACEHOLDER_SUMMARY,
    PROMPT_STATUS_ENUM_LITERAL,
    describe_contract_issues,
    is_invalid_contract_payload,
    render_role_result_contract,
    summarize_contract_issues,
    validate_role_result_contract,
)
from teams_runtime.runtime.session_manager import RoleSessionManager
from teams_runtime.shared.models import MessageEnvelope, RequestRecord, RoleResult, RoleRuntimeConfig
from teams_runtime.workflows.roles import render_role_prompt_spec
from teams_runtime.workflows.roles.planner import normalize_planner_proposals


CONTRACT_REPAIR_MAX_ATTEMPTS = 1
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


def _coerce_prompt_status_enum_literal(payload: dict[str, Any]) -> str:
    proposals = payload.get("proposals")
    transition = proposals.get("workflow_transition") if isinstance(proposals, dict) else {}
    if isinstance(transition, dict):
        outcome = str(transition.get("outcome") or "").strip().lower()
        if outcome in {"block", "reopen"}:
            return "blocked"
        if outcome in {"continue", "advance", "complete"}:
            return "completed"
        if any(
            str(transition.get(key) or "").strip()
            for key in ("target_phase", "target_step")
        ):
            return "completed"
    if str(payload.get("error") or "").strip():
        return "failed"
    return "failed"


def _merge_contract_issue_lists(*issue_groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for issues in issue_groups:
        for issue in issues:
            normalized = str(issue or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            merged.append(normalized)
    return merged


def normalize_role_payload(payload: dict[str, Any]) -> RoleResult:
    normalized = dict(payload) if isinstance(payload, dict) else {}
    validation_notes: list[str] = []
    blocking_notes: list[str] = []

    status = str(normalized.get("status") or "").strip().lower()
    if not status:
        status = "completed"
    elif status == PROMPT_STATUS_ENUM_LITERAL:
        validation_notes.append("coerced copied prompt status enum literal")
        status = _coerce_prompt_status_enum_literal(normalized)
        if status == "failed":
            blocking_notes.append("status copied prompt enum literal without a concrete role result")
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
    if normalized["summary"].lower() == PROMPT_PLACEHOLDER_SUMMARY:
        normalized["summary"] = ""
        validation_notes.append("reset placeholder summary copied from prompt")

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
    if normalized["insights"]:
        filtered_insights = [
            item
            for item in normalized["insights"]
            if item.lower() != PROMPT_PLACEHOLDER_INSIGHT
        ]
        if len(filtered_insights) != len(normalized["insights"]):
            validation_notes.append("reset placeholder insights copied from prompt")
        normalized["insights"] = filtered_insights

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
                output, resolved_session_id = self.codex_runner.run(
                    Path(state.workspace_path),
                    prompt,
                    active_session_id,
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
                        state.workspace_path,
                        active_session_id or "new",
                        "fresh" if retry_session_id is None else "resume",
                    )
                    output, resolved_session_id = self.codex_runner.run(
                        Path(state.workspace_path),
                        prompt,
                        retry_session_id,
                        bypass_sandbox=True,
                    )
                    active_session_id = resolved_session_id or active_session_id
                    payload = self._parse_role_output(output, request_record)
                payload, active_session_id = self._repair_invalid_role_payload_once(
                    payload,
                    request_record,
                    current_sprint_id=current_sprint_id,
                    workspace_path=Path(state.workspace_path),
                    active_session_id=active_session_id,
                    bypass_sandbox=default_bypass,
                )
            except RuntimeError as exc:
                LOGGER.warning(
                    "[%s] task_runtime_error request_id=%s sprint_id=%s todo_id=%s backlog_id=%s workspace=%s error=%s",
                    self.role,
                    request_id,
                    sprint_id,
                    todo_id,
                    backlog_id,
                    state.workspace_path,
                    str(exc),
                )
                payload = {
                    "request_id": request_record["request_id"],
                    "role": self.role,
                    "status": "failed",
                    "summary": "",
                    "insights": [],
                    "proposals": {},
                    "artifacts": [],
                    "error": str(exc) or "role execution failed",
                }
                payload = normalize_role_payload(payload)
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
        contract_issues: list[str] = []
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
            contract_issues.append("missing_json_object")
        payload["request_id"] = request_record["request_id"]
        payload["role"] = self.role
        contract_issues = _merge_contract_issue_lists(
            contract_issues,
            validate_role_result_contract(payload, request_record=request_record, role=self.role),
        )
        normalized = normalize_role_payload(payload)
        if contract_issues:
            normalized["contract_status"] = CONTRACT_STATUS_INVALID
            normalized["contract_issues"] = contract_issues
        return normalized

    def _repair_invalid_role_payload_once(
        self,
        payload: RoleResult,
        request_record: RequestRecord,
        *,
        current_sprint_id: str,
        workspace_path: Path,
        active_session_id: str | None,
        bypass_sandbox: bool,
    ) -> tuple[RoleResult, str | None]:
        if not is_invalid_contract_payload(payload):
            return payload, active_session_id

        observed_issues = _merge_contract_issue_lists(list(payload.get("contract_issues") or []))
        attempts = 0
        latest_payload = dict(payload)
        latest_session_id = active_session_id
        while attempts < CONTRACT_REPAIR_MAX_ATTEMPTS and is_invalid_contract_payload(latest_payload):
            attempts += 1
            issue_summary = summarize_contract_issues(observed_issues) or "role result contract validation failed"
            LOGGER.warning(
                "[%s] invalid_role_payload request_id=%s sprint_id=%s attempt=%s session_id=%s issues=%s",
                self.role,
                str(request_record.get("request_id") or "unknown"),
                current_sprint_id or "N/A",
                attempts,
                latest_session_id or "new",
                issue_summary,
            )
            repair_prompt = self._build_role_result_repair_prompt(
                request_record,
                latest_payload,
                observed_issues,
                current_sprint_id=current_sprint_id,
            )
            output, resolved_session_id = self.codex_runner.run(
                workspace_path,
                repair_prompt,
                latest_session_id,
                bypass_sandbox=bypass_sandbox,
            )
            latest_session_id = resolved_session_id or latest_session_id
            latest_payload = self._parse_role_output(output, request_record)
            observed_issues = _merge_contract_issue_lists(
                observed_issues,
                list(latest_payload.get("contract_issues") or []),
            )

        if is_invalid_contract_payload(latest_payload):
            return (
                self._build_invalid_role_payload_result(
                    request_record,
                    observed_issues,
                    contract_repair_attempted=attempts > 0,
                ),
                latest_session_id,
            )

        if attempts > 0:
            latest_payload = dict(latest_payload)
            latest_payload["contract_status"] = CONTRACT_STATUS_REPAIRED
            latest_payload["contract_issues"] = observed_issues
            latest_payload["contract_repair_attempted"] = True
        return latest_payload, latest_session_id

    def _build_invalid_role_payload_result(
        self,
        request_record: RequestRecord,
        issues: list[str],
        *,
        contract_repair_attempted: bool,
    ) -> RoleResult:
        error_summary = summarize_contract_issues(issues) or "role result contract validation failed"
        payload = normalize_role_payload(
            {
                "request_id": request_record["request_id"],
                "role": self.role,
                "status": "failed",
                "summary": "역할 결과 JSON contract를 복구하지 못했습니다.",
                "insights": [],
                "proposals": {},
                "artifacts": [],
                "error": f"invalid_role_payload: {error_summary}",
            }
        )
        payload["contract_status"] = CONTRACT_STATUS_INVALID
        payload["contract_issues"] = list(issues)
        payload["contract_repair_attempted"] = contract_repair_attempted
        return payload

    def _build_role_result_repair_prompt(
        self,
        request_record: RequestRecord,
        invalid_payload: RoleResult,
        issues: list[str],
        *,
        current_sprint_id: str,
    ) -> str:
        team_workspace_hint = "./workspace/teams_generated" if self.paths.workspace_root.name == "teams_generated" else "./workspace"
        role_specific_rules, extra_fields = render_role_prompt_spec(self.role, team_workspace_hint)
        contract_block = render_role_result_contract(
            request_id=str(request_record["request_id"]),
            role=self.role,
            extra_fields=extra_fields,
        )
        issue_lines = "\n".join(f"- {line}" for line in describe_contract_issues(issues)) or "- unknown contract error"
        return f"""Your previous teams_runtime role result was rejected by contract validation.
Do not perform new repository work unless it is strictly necessary to fix the role result JSON.
Use only facts you already observed in this session. If you cannot justify `completed`, return `blocked` or `failed` with a concrete Korean summary and error.

Validation errors:
{issue_lines}

Previous invalid payload:
```json
{json.dumps(invalid_payload, ensure_ascii=False, indent=2, sort_keys=True)}
```

Return corrected strict JSON only with this shape:
{contract_block}

Current sprint: {current_sprint_id or "N/A"}
Treat `Current request` as the source of truth.
When `Current request.params.workflow` exists, `proposals.workflow_transition` is required for workflow-managed roles.
Never copy schema enums or placeholder example text literally.
{role_specific_rules}

Current request:
{json.dumps(request_record, ensure_ascii=False, indent=2)}
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
        contract_block = render_role_result_contract(
            request_id=str(request_record["request_id"]),
            role=self.role,
            extra_fields=extra_fields,
        )
        return f"""You are the {self.role} role inside teams_runtime.

Use your role workspace files for team-private coordination.
The broader project workspace that contains teams_generated is available at ./workspace.
For teams runtime shared docs and sprint artifacts, prefer ./shared_workspace.
For teams runtime state such as requests, backlog, and sprint JSON, prefer ./.teams_runtime.
Inspect or modify ./workspace only when the request is about the broader project codebase or data workspace outside the teams runtime workspace.
The teams workspace root and its config files live in {team_workspace_hint}, not duplicated in this session root.
Use ./workspace_context.md if you need the exact path mapping for this session.

Return strict JSON only with this shape:
{contract_block}

Current sprint: {resolved_sprint_id}
Treat `Current request` as the source of truth.
The relay handoff is intentionally compact.
Use the relay handoff summary and any `sources/<request_id>.request.md` snapshot only as quick orientation.
Before deciding your next action, read the latest request `result` and recent `events` inside `Current request`.
If `Current request`, relay text, and a role-local request snapshot differ, trust `Current request`.
Orchestrator exclusively owns `next_role` selection. Do not choose or rely on `next_role` in your role output; make your summary, proposals, and artifacts explicit enough for orchestrator to choose the next step.
When `Current request.params.workflow` exists, use `proposals.workflow_transition` as the structured workflow contract. The expected shape is:
`{{"outcome":"continue|advance|reopen|block|complete","target_phase":"","target_step":"","reopen_category":"","reason":"...","unresolved_items":[],"finalize_phase":false}}`.
Planner-owned advisory requests must use role-specific target steps such as `designer_advisory` or `architect_advisory`; other roles should describe the blocker or completion clearly and let orchestrator choose the next step from the workflow contract.
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
