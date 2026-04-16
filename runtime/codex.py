from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from teams_runtime.core.parsing import is_manual_sprint_finalize_text, is_manual_sprint_start_text
from teams_runtime.core.persistence import utc_now_iso, write_json
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.models import MessageEnvelope, RoleRuntimeConfig, RoleSessionState


SESSION_ID_PATTERN = re.compile(r"session id:\s*([0-9a-fA-F-]+)", re.IGNORECASE)
REQUEST_ID_TEXT_PATTERN = re.compile(r"\brequest[_\s-]*id\s*[:=]?\s*([A-Za-z0-9._-]+)", re.IGNORECASE)
ALLOWED_ROLE_STATUSES = {"completed", "blocked", "failed"}
ALLOWED_PARSER_INTENTS = {"route", "status", "cancel", "execute"}
ALLOWED_PARSER_CONFIDENCE = {"low", "medium", "high"}
ALLOWED_SOURCER_STATUSES = {"completed", "failed"}
ALLOWED_BACKLOG_KINDS = {"bug", "enhancement", "feature", "chore"}
LOGGER = logging.getLogger(__name__)
SOURCER_LOG_PREFIX = "[sourcer]"

STATUS_INQUIRY_TERMS = (
    "what",
    "which",
    "show",
    "share",
    "tell",
    "list",
    "current",
    "ongoing",
    "active",
    "status",
    "progress",
    "현황",
    "공유",
    "보여",
    "알려",
    "지금",
    "현재",
)
SPRINT_STATUS_TERMS = (
    "sprint",
    "spring",
    "스프린트",
    "working",
    "workign",
    "running",
    "focus",
    "goal",
    "milestone",
    "진행",
)
BACKLOG_STATUS_TERMS = (
    "backlog",
    "백로그",
    "todo",
    "todos",
    "task",
    "tasks",
    "item",
    "items",
    "할일",
    "작업",
    "항목",
)
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


def _normalize_inquiry_text(value: str) -> str:
    normalized = str(value or "").strip().lower()
    for token in ("_", "-", "/", "\\", ".", ",", ":", ";", "!", "?", "(", ")", "[", "]", "{", "}", "\"", "'"):
        normalized = normalized.replace(token, " ")
    return " ".join(normalized.split())


def _contains_any_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _contains_write_denial_signal(text: str) -> bool:
    return any(term in text for term in WRITE_DENIAL_TERMS)


def _truncate_log_text(value: Any, *, limit: int = 160) -> str:
    normalized = str(value or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 3, 0)].rstrip() + "..."


def infer_status_inquiry_payload(raw_text: str) -> dict[str, Any] | None:
    normalized = _normalize_inquiry_text(raw_text)
    if not normalized:
        return None
    if is_manual_sprint_start_text(raw_text) or is_manual_sprint_finalize_text(raw_text):
        return None

    request_match = REQUEST_ID_TEXT_PATTERN.search(str(raw_text or ""))
    if request_match and _contains_any_term(normalized, STATUS_INQUIRY_TERMS):
        request_id = str(request_match.group(1) or "").strip()
        if request_id:
            return {
                "intent": "status",
                "scope": "request",
                "request_id": request_id,
                "body": "",
                "reason": "자연어 request 상태 조회로 해석",
                "confidence": "high",
            }

    if _contains_any_term(normalized, ("backlog", "백로그")) and _contains_any_term(normalized, STATUS_INQUIRY_TERMS):
        return {
            "intent": "status",
            "scope": "backlog",
            "request_id": "",
            "body": "",
            "reason": "자연어 backlog 상태 조회로 해석",
            "confidence": "high",
        }

    if _contains_any_term(normalized, ("sprint", "spring", "스프린트")) and _contains_any_term(
        normalized,
        STATUS_INQUIRY_TERMS,
    ):
        return {
            "intent": "status",
            "scope": "sprint",
            "request_id": "",
            "body": "",
            "reason": "자연어 sprint 상태 조회로 해석",
            "confidence": "high",
        }

    return None


def _sample_item_labels(items: list[dict[str, Any]], *, limit: int = 3) -> list[str]:
    labels: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("title") or item.get("scope") or item.get("summary") or "").strip()
        if not label:
            continue
        labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def extract_json_object(text: str) -> dict[str, Any]:
    normalized = str(text or "").strip()
    if not normalized:
        raise ValueError("Empty response.")

    candidates: list[str] = [normalized]
    if normalized.startswith("```"):
        lines = normalized.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            candidates.append("\n".join(lines[1:-1]).strip())

        fenced_segments: list[str] = []
        segment_lines: list[str] = []
        in_fenced_json = False
        for line in lines:
            stripped = line.strip()
            if not in_fenced_json:
                if stripped.startswith("```"):
                    in_fenced_json = True
                    segment_lines = []
                continue
            if stripped == "```":
                fenced_segments.append("\n".join(segment_lines).strip())
                segment_lines = []
                in_fenced_json = False
                continue
            segment_lines.append(line)

        merged_fenced = "\n".join(segment for segment in fenced_segments if segment).strip()
        if merged_fenced:
            candidates.append(merged_fenced)

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    decoder = json.JSONDecoder()
    for index, char in enumerate(normalized):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(normalized[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("No JSON object found in response.")


def _normalize_string_list_field(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return []


def _normalize_planner_backlog_candidate(raw_item: Any) -> dict[str, Any] | None:
    if isinstance(raw_item, str):
        title = str(raw_item).strip()
        if not title:
            return None
        return {
            "title": title,
            "summary": title,
            "kind": "enhancement",
            "scope": title,
            "acceptance_criteria": [],
            "origin": {},
        }
    if not isinstance(raw_item, dict):
        return None

    normalized = dict(raw_item)
    title = str(
        normalized.get("title")
        or normalized.get("scope")
        or normalized.get("summary")
        or ""
    ).strip()
    if not title:
        return None

    kind = str(normalized.get("kind") or "enhancement").strip().lower().replace(" ", "_")
    if kind in {"new_feature", "new-feature"}:
        kind = "feature"
    if kind not in ALLOWED_BACKLOG_KINDS:
        kind = "enhancement"

    normalized["title"] = title
    normalized["summary"] = str(normalized.get("summary") or title).strip()
    normalized["scope"] = str(normalized.get("scope") or title).strip()
    normalized["kind"] = kind
    normalized["acceptance_criteria"] = _normalize_string_list_field(normalized.get("acceptance_criteria"))
    origin = normalized.get("origin")
    normalized["origin"] = dict(origin or {}) if isinstance(origin, dict) else {}
    return normalized


def _normalize_planner_backlog_write(raw_write: Any) -> dict[str, Any] | None:
    if not isinstance(raw_write, dict):
        return None

    normalized = dict(raw_write)
    backlog_id = str(normalized.get("backlog_id") or "").strip()
    artifact_path = str(
        normalized.get("artifact_path")
        or normalized.get("artifact")
        or normalized.get("path")
        or ""
    ).strip()
    if not backlog_id and not artifact_path:
        return None

    status = str(normalized.get("status") or "").strip().lower()
    changed_fields = _normalize_string_list_field(
        normalized.get("changed_fields")
        if normalized.get("changed_fields") is not None
        else normalized.get("fields")
    )

    if backlog_id:
        normalized["backlog_id"] = backlog_id
    else:
        normalized.pop("backlog_id", None)
    if artifact_path:
        normalized["artifact_path"] = artifact_path
    else:
        normalized.pop("artifact_path", None)
    if status:
        normalized["status"] = status
    else:
        normalized.pop("status", None)
    if changed_fields:
        normalized["changed_fields"] = changed_fields
    else:
        normalized.pop("changed_fields", None)
    normalized.pop("artifact", None)
    normalized.pop("path", None)
    normalized.pop("fields", None)
    return normalized


def normalize_planner_proposals(proposals: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    normalized = dict(proposals) if isinstance(proposals, dict) else {}
    validation_notes: list[str] = []

    raw_items: list[Any] = []
    if isinstance(normalized.get("backlog_items"), list):
        raw_items.extend(normalized.get("backlog_items") or [])
    if isinstance(normalized.get("backlog_item"), (dict, str)):
        raw_items.append(normalized.get("backlog_item"))
        validation_notes.append("normalized planner backlog_item to backlog_items")
    if isinstance(normalized.get("planned_backlog_updates"), list):
        raw_items.extend(normalized.get("planned_backlog_updates") or [])
        validation_notes.append("normalized planner planned_backlog_updates to backlog_items")

    backlog_items: list[dict[str, Any]] = []
    seen_backlog_items: set[str] = set()
    for raw_item in raw_items:
        candidate = _normalize_planner_backlog_candidate(raw_item)
        if candidate is None:
            continue
        dedupe_key = "::".join(
            [
                str(candidate.get("title") or "").strip().lower(),
                str(candidate.get("scope") or "").strip().lower(),
                str(candidate.get("kind") or "").strip().lower(),
            ]
        )
        if dedupe_key in seen_backlog_items:
            continue
        seen_backlog_items.add(dedupe_key)
        backlog_items.append(candidate)
    if backlog_items:
        normalized["backlog_items"] = backlog_items
        if len(backlog_items) == 1:
            normalized["backlog_item"] = backlog_items[0]
    else:
        normalized.pop("backlog_items", None)
        normalized.pop("backlog_item", None)
    normalized.pop("planned_backlog_updates", None)

    raw_writes: list[Any] = []
    if isinstance(normalized.get("backlog_writes"), list):
        raw_writes.extend(normalized.get("backlog_writes") or [])
    if isinstance(normalized.get("backlog_write"), dict):
        raw_writes.append(normalized.get("backlog_write"))
        validation_notes.append("normalized planner backlog_write to backlog_writes")

    backlog_writes: list[dict[str, Any]] = []
    seen_backlog_writes: set[str] = set()
    for raw_write in raw_writes:
        receipt = _normalize_planner_backlog_write(raw_write)
        if receipt is None:
            continue
        dedupe_key = str(receipt.get("backlog_id") or receipt.get("artifact_path") or "").strip().lower()
        if dedupe_key and dedupe_key in seen_backlog_writes:
            continue
        if dedupe_key:
            seen_backlog_writes.add(dedupe_key)
        backlog_writes.append(receipt)
    if backlog_writes:
        normalized["backlog_writes"] = backlog_writes
        if len(backlog_writes) == 1:
            normalized["backlog_write"] = backlog_writes[0]
    else:
        normalized.pop("backlog_writes", None)
        normalized.pop("backlog_write", None)

    return normalized, validation_notes


def normalize_role_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload) if isinstance(payload, dict) else {}
    validation_notes: list[str] = []
    blocking_notes: list[str] = []

    status = str(normalized.get("status") or "").strip().lower()
    if not status:
        status = "completed"
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


def normalize_intent_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload) if isinstance(payload, dict) else {}

    intent = str(normalized.get("intent") or "").strip().lower()
    if intent not in ALLOWED_PARSER_INTENTS:
        intent = "route"
    normalized["intent"] = intent
    raw_params = normalized.get("params")
    normalized["params"] = dict(raw_params) if isinstance(raw_params, dict) else {}

    scope = str(normalized.get("scope") or "").strip()
    request_id = str(normalized.get("request_id") or "").strip()
    if intent == "status":
        if request_id:
            normalized["scope"] = "request"
        elif scope in {"sprint", "backlog"}:
            normalized["scope"] = scope
        else:
            inferred = infer_status_inquiry_payload(
                " ".join(
                    part
                    for part in (
                        scope,
                        str(normalized.get("body") or "").strip(),
                        str(normalized.get("reason") or "").strip(),
                    )
                    if str(part).strip()
                )
            )
            if inferred:
                normalized["scope"] = str(inferred.get("scope") or "").strip() or scope
                inferred_request_id = str(inferred.get("request_id") or "").strip()
                if inferred_request_id:
                    request_id = inferred_request_id
            else:
                normalized["intent"] = "route"
                normalized["scope"] = scope or str(normalized.get("body") or "").strip()
    elif intent == "cancel":
        normalized["scope"] = "request" if request_id else (scope or str(normalized.get("body") or "").strip())
    else:
        normalized["scope"] = scope or str(normalized.get("body") or "").strip()

    normalized["body"] = str(normalized.get("body") or "").strip()
    normalized["request_id"] = request_id
    normalized["reason"] = str(normalized.get("reason") or "").strip()

    confidence = str(normalized.get("confidence") or "").strip().lower()
    if confidence not in ALLOWED_PARSER_CONFIDENCE:
        confidence = "medium"
    normalized["confidence"] = confidence
    return normalized


def normalize_backlog_sourcing_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload) if isinstance(payload, dict) else {}

    status = str(normalized.get("status") or "").strip().lower()
    if status not in ALLOWED_SOURCER_STATUSES:
        status = "failed" if status else "completed"
    normalized["status"] = status
    normalized["summary"] = str(normalized.get("summary") or "").strip()
    normalized["error"] = str(normalized.get("error") or "").strip()

    raw_items = normalized.get("backlog_items")
    if not isinstance(raw_items, list):
        single = normalized.get("backlog_item")
        raw_items = [single] if isinstance(single, (dict, str)) else []

    items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if isinstance(raw_item, str):
            title = str(raw_item).strip()
            if not title:
                continue
            items.append(
                {
                    "title": title,
                    "summary": title,
                    "kind": "enhancement",
                    "scope": title,
                    "acceptance_criteria": [],
                    "origin": {},
                }
            )
            continue
        if not isinstance(raw_item, dict):
            continue
        title = str(
            raw_item.get("title")
            or raw_item.get("scope")
            or raw_item.get("summary")
            or ""
        ).strip()
        if not title:
            continue
        lowered_title = title.lower()
        summary_text = str(raw_item.get("summary") or title).strip()
        scope_text = str(raw_item.get("scope") or title).strip()
        if any(marker in lowered_title for marker in ("정리", "구체화", "반영", "동기화", "재구성", "업데이트", "개선", "prompt", "프롬프트", "문서", "라우팅", "회귀 테스트")):
            if summary_text and summary_text != title:
                title = summary_text
            elif scope_text and scope_text != title:
                title = scope_text
        kind = str(raw_item.get("kind") or "enhancement").strip().lower().replace(" ", "_")
        if kind in {"new_feature", "new-feature"}:
            kind = "feature"
        if kind not in ALLOWED_BACKLOG_KINDS:
            kind = "enhancement"
        acceptance = raw_item.get("acceptance_criteria")
        if isinstance(acceptance, list):
            acceptance_criteria = [str(item).strip() for item in acceptance if str(item).strip()]
        elif isinstance(acceptance, str):
            acceptance_criteria = [acceptance.strip()] if acceptance.strip() else []
        else:
            acceptance_criteria = []
        origin = raw_item.get("origin")
        items.append(
            {
                "title": title,
                "summary": summary_text,
                "kind": kind,
                "scope": scope_text,
                "acceptance_criteria": acceptance_criteria,
                "origin": dict(origin or {}) if isinstance(origin, dict) else {},
            }
        )
    normalized["backlog_items"] = items
    return normalized


class RoleSessionManager:
    def __init__(self, paths: RuntimePaths, role: str, sprint_id: str, *, agent_root: Path | None = None):
        self.paths = paths
        self.role = role
        self.sprint_id = sprint_id
        self._agent_root = agent_root

    @property
    def role_root(self) -> Path:
        return self._agent_root or self.paths.role_root(self.role)

    def load(self) -> RoleSessionState | None:
        payload = {}
        state_file = self.paths.session_state_file(self.role)
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        state = RoleSessionState.from_dict(payload)
        if not state.workspace_path:
            return None
        return state

    def ensure_session(self) -> RoleSessionState:
        self.paths.ensure_runtime_dirs()
        current = self.load()
        if (
            current is not None
            and current.sprint_id == self.sprint_id
            and Path(current.workspace_path).is_dir()
        ):
            self._seed_workspace(Path(current.workspace_path))
            current.last_used_at = utc_now_iso()
            self.save(current)
            return current
        if current is not None:
            self.archive(current)
        return self.create()

    def save(self, state: RoleSessionState) -> None:
        self.paths.role_sessions_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.paths.session_state_file(self.role), state.to_dict())

    def archive(self, state: RoleSessionState) -> None:
        archive_dir = self.paths.archived_session_dir(state.sprint_id or "unknown", self.role)
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_file = archive_dir / f"{Path(state.workspace_path).name or self.role}.json"
        write_json(archive_file, state.to_dict())
        try:
            self.paths.session_state_file(self.role).unlink()
        except FileNotFoundError:
            pass

    def create(self) -> RoleSessionState:
        runtime_id = uuid.uuid4().hex
        sessions_root = self.role_root / "sessions"
        session_workspace = sessions_root / runtime_id
        session_workspace.mkdir(parents=True, exist_ok=False)
        self._seed_workspace(session_workspace)
        state = RoleSessionState(
            role=self.role,
            sprint_id=self.sprint_id,
            session_id="",
            workspace_path=str(session_workspace),
            created_at=utc_now_iso(),
            last_used_at=utc_now_iso(),
        )
        self.save(state)
        return state

    def finalize_session_id(self, state: RoleSessionState, session_id: str | None) -> RoleSessionState:
        normalized = str(session_id or "").strip()
        if not normalized:
            state.last_used_at = utc_now_iso()
            self.save(state)
            return state
        state.session_id = normalized
        state.last_used_at = utc_now_iso()
        self.save(state)
        return state

    def _seed_workspace(self, session_workspace: Path) -> None:
        shared_targets = [
            "AGENTS.md",
            "GEMINI.md",
            "todo.md",
            "history.md",
            "journal.md",
            "sources",
            ".agents",
            "workspace_manifest.json",
        ]
        for filename in shared_targets:
            source = self.role_root / filename
            target = session_workspace / filename
            if source.exists() and not (target.exists() or target.is_symlink()):
                target.symlink_to(source)
        project_workspace = self.paths.project_workspace_root
        workspace_link = session_workspace / "workspace"
        if project_workspace.exists() and not (workspace_link.exists() or workspace_link.is_symlink()):
            workspace_link.symlink_to(project_workspace)
        for legacy_name in ("team_runtime.yaml", "discord_agents_config.yaml"):
            legacy_target = session_workspace / legacy_name
            if legacy_target.is_symlink():
                legacy_target.unlink()
        for shared_name in (
            "shared_workspace",
            ".teams_runtime",
            "docs",
            "communication_protocol.md",
            "file_contracts.md",
            "COMMIT_POLICY.md",
        ):
            source = self.paths.workspace_root / shared_name
            target = session_workspace / Path(shared_name).name
            if source.exists() and not (target.exists() or target.is_symlink()):
                target.symlink_to(source)
        context_file = session_workspace / "workspace_context.md"
        context_file.write_text(self._build_workspace_context(session_workspace), encoding="utf-8")

    def _build_workspace_context(self, session_workspace: Path) -> str:
        return "\n".join(
            [
                "# Workspace Context",
                "",
                f"- session_workspace: {session_workspace}",
                f"- teams_runtime_root: {self.paths.workspace_root}",
                f"- project_workspace_root: {self.paths.project_workspace_root}",
                "- shared sprint/docs artifacts: use ./shared_workspace",
                "- teams runtime state files: use ./.teams_runtime",
                "- actual project edits outside teams runtime: use ./workspace",
                "- teams runtime config root is the current workspace when `./.teams_runtime` exists; only fall back to ./workspace/teams_generated when a session-local runtime path is unavailable",
                "- role-private coordination files live in the current session root alongside AGENTS.md, todo.md, history.md, journal.md, and sources/",
                "",
            ]
        )


class CodexRunner:
    def __init__(self, runtime_config: RoleRuntimeConfig, *, role: str = ""):
        self.runtime_config = runtime_config
        self.role = str(role or "").strip()

    def _discover_extra_writable_dirs(self, workspace: Path) -> list[str]:
        extra_dirs: list[str] = []
        seen: set[str] = set()
        for directory_name in ("workspace", "shared_workspace", ".teams_runtime"):
            candidate = workspace / directory_name
            if not candidate.exists():
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            resolved_text = str(resolved)
            if resolved != workspace and resolved_text not in seen:
                seen.add(resolved_text)
                extra_dirs.append(resolved_text)
        return extra_dirs

    def _build_command(
        self,
        *,
        workspace: Path,
        prompt: str,
        session_id: str | None,
        output_file: Path,
        bypass_sandbox: bool,
    ) -> tuple[list[str], str | None]:
        is_gemini = "gemini" in self.runtime_config.model.lower()

        if is_gemini:
            command = ["gemini"]
            if session_id:
                command.extend(["--resume", session_id])
            command.extend(["--model", self.runtime_config.model])

            for extra_dir in self._discover_extra_writable_dirs(workspace):
                command.extend(["--include-directories", extra_dir])

            if bypass_sandbox:
                command.append("--yolo")

            command.extend(["--output-format", "json"])
            command.extend(["--prompt", prompt])
            return command, None

        command = ["codex", "exec"]
        if session_id:
            command.extend(
                [
                    "resume",
                    "--model",
                    self.runtime_config.model,
                    "-o",
                    str(output_file),
                    "--skip-git-repo-check",
                ]
            )
            if bypass_sandbox:
                command.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                command.append("--full-auto")
            command.extend(["-c", f'model_reasoning_effort="{self.runtime_config.reasoning}"'])
            command.extend(["-c", 'personality="friendly"'])
            command.extend([session_id, "-"])
            return command, prompt
        else:
            command.extend(
                [
                    "-",
                    "--model",
                    self.runtime_config.model,
                    "-o",
                    str(output_file),
                    "--skip-git-repo-check",
                    "-C",
                    str(workspace),
                ]
            )
        for extra_dir in self._discover_extra_writable_dirs(workspace):
            command.extend(["--add-dir", extra_dir])
        if bypass_sandbox:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.append("--full-auto")
        command.extend(["-c", f'model_reasoning_effort="{self.runtime_config.reasoning}"'])
        command.extend(["-c", 'personality="friendly"'])
        return command, prompt

    def run(
        self,
        workspace: Path,
        prompt: str,
        session_id: str | None,
        *,
        bypass_sandbox: bool = False,
    ) -> tuple[str, str | None]:
        abs_workspace = workspace.expanduser().resolve()
        output_file = abs_workspace / ".teams_runtime_codex_output.txt"
        try:
            output_file.unlink()
        except FileNotFoundError:
            pass
        command, stdin_input = self._build_command(
            workspace=abs_workspace,
            prompt=prompt,
            session_id=session_id,
            output_file=output_file,
            bypass_sandbox=bypass_sandbox,
        )

        is_gemini = "gemini" in self.runtime_config.model.lower()
        env = {**os.environ, "HOME": str(Path.home())}
        if is_gemini:
            env["GEMINI_SYSTEM_MD"] = str(abs_workspace / "GEMINI.md")
            gemini_dir = abs_workspace / ".gemini"
            gemini_dir.mkdir(parents=True, exist_ok=True)
            skills_symlink = gemini_dir / "skills"
            agents_skills = abs_workspace / ".agents" / "skills"
            if agents_skills.exists() and not skills_symlink.exists():
                try:
                    skills_symlink.symlink_to(agents_skills)
                except OSError:
                    pass

        process = subprocess.run(
            command,
            cwd=str(abs_workspace),
            capture_output=True,
            input=stdin_input,
            text=True,
            env=env,
            check=False,
        )

        output = ""
        resolved_session_id = session_id

        if is_gemini:
            try:
                res_json = json.loads(process.stdout)
                output = res_json.get("response", "").strip()
                resolved_session_id = res_json.get("session_id") or res_json.get("sessionId") or session_id
                if not output and res_json.get("error"):
                    error_info = res_json.get("error")
                    output = error_info.get("message") if isinstance(error_info, dict) else str(error_info)
            except json.JSONDecodeError:
                output = process.stdout.strip() or process.stderr.strip()
        else:
            combined = "\n".join(
                part for part in [process.stdout.strip(), process.stderr.strip()] if part
            ).strip()
            session_match = SESSION_ID_PATTERN.search(combined)
            resolved_session_id = session_match.group(1).strip() if session_match else session_id
            if output_file.exists():
                output = output_file.read_text(encoding="utf-8").strip()
            if not output:
                output = process.stdout.strip() or combined

        if process.returncode != 0:
            cli_name = "Gemini" if is_gemini else "Codex"
            if output:
                try:
                    extract_json_object(output)
                except ValueError:
                    raise RuntimeError(output or f"{cli_name} command failed.")
                LOGGER.warning(
                    "[%s] %s command exited with code %s but produced a valid JSON payload; preserving role result",
                    self.role,
                    cli_name,
                    process.returncode,
                )
            else:
                raise RuntimeError(f"{cli_name} command failed.")
        return output, resolved_session_id


class RoleAgentRuntime:
    def __init__(
        self,
        *,
        paths: RuntimePaths,
        role: str,
        sprint_id: str,
        runtime_config: RoleRuntimeConfig,
        agent_root: Path | None = None,
    ):
        self.paths = paths
        self.role = role
        self.sprint_id = str(sprint_id or "").strip()
        self._agent_root = agent_root
        self.session_manager = RoleSessionManager(
            paths,
            role,
            self.sprint_id,
            agent_root=agent_root,
        )
        self._session_managers: dict[str, RoleSessionManager] = {
            self.sprint_id: self.session_manager,
        }
        self.codex_runner = CodexRunner(runtime_config, role=role)
        self._run_lock = threading.Lock()

    def _resolve_request_sprint_id(
        self,
        envelope: MessageEnvelope,
        request_record: dict[str, Any],
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
        )
        self._session_managers[normalized] = manager
        return manager

    def _request_requires_default_bypass(
        self,
        envelope: MessageEnvelope,
        request_record: dict[str, Any],
    ) -> bool:
        # All roles now default to sandbox bypass mode.
        return True

    def run_task(self, envelope: MessageEnvelope, request_record: dict[str, Any]) -> dict[str, Any]:
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

    def _parse_role_output(self, output: str, request_record: dict[str, Any]) -> dict[str, Any]:
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
        else:
            return False

    def _build_prompt(
        self,
        envelope: MessageEnvelope,
        request_record: dict[str, Any],
        *,
        current_sprint_id: str | None = None,
    ) -> str:
        resolved_sprint_id = str(current_sprint_id or "").strip() or self._resolve_request_sprint_id(
            envelope,
            request_record,
        )
        team_workspace_hint = "./workspace/teams_generated" if self.paths.workspace_root.name == "teams_generated" else "./workspace"
        role_specific_rules = ""
        extra_fields = ""
        if self.role == "orchestrator":
            role_specific_rules = f"""

Orchestrator-specific rules:
- You are the first agent owner for user-originated requests. Do not assume the runtime already classified the request into status, cancel, sprint control, or planner delegation.
- If you can answer or finish the request yourself, set `proposals.request_handling = {{"mode": "complete"}}`.
- If the request should continue to another role, leave `proposals.request_handling` unset and make the next-step need explicit in your summary, proposals, and artifacts.
- For sprint lifecycle requests and sprint status questions, inspect `./.agents/skills/sprint_orchestration/SKILL.md` first and follow that skill.
- For sprint lifecycle requests, use the explicit lifecycle command surface from the skill, such as `python -m teams_runtime sprint start|stop|restart|status --workspace-root {team_workspace_hint}`. Re-read persisted sprint state after the command and summarize the observed result.
- Keep orchestrator summaries short and user-facing. Lead with the actual outcome, and do not echo raw command lines, file paths, or verification steps unless the user explicitly asked for that detail.
- For no-op lifecycle outcomes such as "nothing to stop" or "already no active sprint", say that plainly in Korean instead of describing the command you ran.
- Do not edit sprint state files directly. Legacy `proposals.control_action = {{"kind": "sprint_lifecycle", ...}}` is compatibility-only fallback, not the primary path for user-originated sprint work.
- For request cancellation, do not edit request JSON files directly. Return `proposals.control_action = {{"kind": "cancel_request", "request_id": "..."}}` and set `proposals.request_handling.mode` to `complete`.
- For registered action execution, return `proposals.control_action = {{"kind": "execute_action", "action_name": "...", "params": {{...}}}}` and set `proposals.request_handling.mode` to `complete`.
- For non-sprint status questions, inspect `./.agents/skills/status_reporting/SKILL.md`, read persisted runtime state, and answer directly instead of delegating unless another role is genuinely needed.
"""
        elif self.role == "planner":
            role_specific_rules = """

Planner-specific rules:
- Treat `Current request.artifacts` as planning reference inputs. If they point to local docs or saved sprint attachments such as `shared_workspace/sprints/.../attachments/...`, inspect them directly and extract requirements, constraints, dependencies, and acceptance criteria into your planning output.
- If sprint kickoff context is preserved separately in `kickoff.md` or `Current request.params.kickoff_*`, treat that kickoff content as immutable source-of-truth and add derived framing in milestone/plan/spec outputs instead of rewriting the original kickoff brief.
- For sprint-relevant planning work such as `initial`, `ongoing_review`, sprint continuity, or sprint-related backlog shaping, inspect `./shared_workspace/sprint_history/index.md` first and then only the smallest relevant prior sprint history file(s) under `./shared_workspace/sprint_history/` to recover carry-over work, repeated blockers, milestone continuity, and already-closed decisions. Use prior sprint history as comparative evidence only; the current request, active sprint docs, and kickoff context remain authoritative. If no relevant history exists, proceed without blocking.
- If `Current request.artifacts`, `params.verified_source_artifact`, or the request body/scope point to an existing local planning/spec Markdown file, inspect that file before blocking for a missing source document.
- If a referenced attachment exists but is not directly readable in the current session, say that explicitly in your result and keep the attachment visible as a referenced planning input instead of silently ignoring it.
- If a confirmed planning/spec document already contains actionable next steps, backlog bundles, or implementation phases, convert them into `proposals.backlog_item` or `proposals.backlog_items` with concrete title/scope/summary/acceptance_criteria instead of stopping at prose-only confirmation.
- For action-required requests based on an existing plan document, do not stop after document verification when backlog decomposition is already possible.
- Treat one backlog item as one `independently reviewable implementation slice`.
- If a candidate backlog item spans multiple subsystems, contracts, phases, deliverables, or separate architect/developer/qa review tracks, split it before returning backlog proposals.
- Do not copy prior local planner history or shared planning logs that happened to use three items; current source material determines the count.
- Do not default sprint planning or backlog decomposition to three items. Return the exact number of backlog items/todos justified by the milestone and source material after splitting work into smaller reviewable slices; one item is valid, and more than three is valid when the milestone genuinely requires it.
- For sprint docs inside the teams runtime workspace, modify `./shared_workspace/...` directly instead of going through `./workspace/...`.
- For planner-owned runtime persistence, prefer `./.teams_runtime/...` over `./workspace/teams_generated/.teams_runtime/...` when the session exposes that local path.
- For planner-owned backlog persistence, prefer the helper in `teams_runtime.core.backlog_store` with canonical `backlog_items` payloads instead of inventing ad-hoc proposal keys or relying on orchestrator-side merges.
- After backlog persistence succeeds, return `proposals.backlog_writes` receipts with the affected `backlog_id` values and any touched artifact paths. `proposals.backlog_item` / `proposals.backlog_items` remain rationale and planning context, not persistence instructions for orchestrator.
- Do not choose `next_role`. Orchestrator owns routing selection after your report, so leave clear execution-ready summary, backlog proposals, acceptance criteria, and downstream-relevant context instead.
- If `Current request.params._teams_kind == "blocked_backlog_review"`, inspect the current blocked backlog candidates, explicitly decide which items remain blocked versus reopen to `pending`, clear blocker fields when reopening, and do not mark reopened work `selected` in this request.
- When `Current request.params.workflow` exists, treat planner as the sole planning owner. Use `proposals.workflow_transition` to either request one advisory specialist pass from `designer` or `architect`, or finalize planning so implementation can advance.
- For Discord/operator message changes, classify the work first: `renderer-only` applies only when semantic meaning, copy hierarchy, user decision path, and CTA are already fixed, and the change is limited to rendering/contract repair such as escaping, field mapping, compact-contract wiring, or truncation fixes.
- If the request changes reading order, omission tolerance, title/summary/body/action priority, or CTA wording/tone, treat it as designer advisory work instead of renderer-only implementation.
- Treat `readability-only` as a non-advisory bucket only when the same reading order still holds and the fix merely makes the already-approved structure easier to scan.
- For Discord message layers, use distinct trigger language: `relay` when immediate status/warning/action priority changes, `handoff` when the next role's first-read context changes, and `summary` when long-term keep-vs-omit rules change.
- If a request mixes rendering repair with user-facing message judgment, do not keep both concerns inside one execution slice. Split it into `technical slice` and `designer advisory slice`, and state whether `technical slice 선행` or `designer advisory 선행` applies before handing work forward.
- When planner opens designer advisory for message judgment, preserve the minimum evidence needed for the split: before/after message example or intended output hierarchy, plus one line saying whether the problem is `표시 오류` or `사용자 판단 혼선`.
- If `Current request.params.sprint_phase == "initial"` and `Current request.params.initial_phase_step` is set, treat the request as a planner-only substep:
  - `milestone_refinement`: preserve the original sprint kickoff brief/requirements/reference artifacts, then refine the milestone title/framing and update milestone-facing docs only.
  - `artifact_sync`: update plan/spec/iteration-log artifacts only.
  - `backlog_definition`: derive sprint-relevant backlog from the current milestone, kickoff requirements, and `spec.md`; create or reopen backlog items before any selection. Backlog zero is invalid. Every backlog item must include concrete `acceptance_criteria` plus `origin.milestone_ref`, `origin.requirement_refs`, and `origin.spec_refs`.
  - `backlog_prioritization`: persist prioritization for the already-defined sprint-relevant backlog with `priority_rank` and `milestone_title`, but do not set `planned_in_sprint_id` or execution todos yet.
  - `todo_finalization`: finalize the execution-ready sprint backlog set and persist `planned_in_sprint_id` for chosen items.
- During initial-phase substeps before `todo_finalization`, do not open execution and do not leave `planned_in_sprint_id`/selected todo state behind.
- During `backlog_definition`, treat persisted backlog as historical input, not as a hard cap. If milestone/requirements/spec are not fully covered, create or reopen sprint-relevant backlog instead of returning backlog zero.
- During `initial` planning and `ongoing_review`, reconsider blocked backlog when it is relevant to the sprint. Only reopened `pending` items may be promoted into the sprint; blocked items stay out of selection until reopened.
- When you create or revise backlog/todo titles, prefer functional wording that says what changed behaviorally or contractually. Avoid meta activity labels such as `정리`, `구체화`, `반영`, `문서/라우팅/회귀 테스트 반영`, or `prompt 개선` unless the functional change cannot be stated more concretely.
- Planner owns backlog/current-sprint/sprint-planning surfaces such as `shared_workspace/backlog.md`, `completed_backlog.md`, `current_sprint.md`, and sprint docs like `milestone.md`, `plan.md`, `spec.md`, `todo_backlog.md`, `iteration_log.md`.
- If the work is a document/planning-only clarification on those surfaces, close it in planning instead of opening implementation for architect/developer/qa.
- Do not report planning complete unless the relevant planner-owned docs were actually created or updated and their paths are present in `artifacts`.
- If QA reopens a request for spec/acceptance mismatch, rewrite the affected planning docs first and only then report planner finalization.
- If `Current request.params._teams_kind == "sprint_closeout_report"`, inspect the persisted sprint evidence in `Current request.artifacts` and return `proposals.sprint_report` with this shape:
  - `headline`: one-line sprint significance
  - `changes`: list of objects with `title`, `why`, `what_changed`, `meaning`, `how`, optional `artifacts`, optional `request_ids`
  - `timeline`: short fact-based lines
  - `agent_contributions`: optional list of `{role, summary, artifacts}`
  - `issues`: optional short lines
  - `achievements`: optional short lines
  - `highlight_artifacts`: optional artifact lines
- In `sprint_closeout_report`, do not mutate backlog or sprint state. Base the draft on persisted request JSON, role reports, sprint docs, and linked artifacts only.
"""
        elif self.role == "designer":
            role_specific_rules = """

Designer-specific rules:
- If `Current request.params.workflow` exists, designer participates only as a planning advisory pass or an orchestrator-triggered UX reopen pass. Do not act as an execution owner.
- Treat `architect` as the support role that translates designer judgment into implementation contracts, and treat `qa` as the support role that checks whether designer intent survived into user-facing output.
- Put durable usability judgment in `proposals.design_feedback`.
- `proposals.design_feedback` should include:
  - `entry_point`: one of `planning_route`, `message_readability`, `info_prioritization`, `ux_reopen`
  - `user_judgment`: 1-3 concise usability judgments
  - `message_priority`: what to lead with vs what can wait, including layer-specific `summary` guidance when relay/handoff/summary boundaries are part of the decision
  - `routing_rationale`: a short rationale planner/orchestrator can reuse
  - optional `required_inputs`, `acceptance_criteria`
- Treat runtime operator messages such as progress reports, compact relay summaries, and requester-facing status updates as the primary `message_readability` / `info_prioritization` surfaces for this backlog.
- When you review messages, make `message_priority` concrete with at least `lead` and `defer` so developer/planner can translate the advice into rendering order.
- When the task is about user-facing data selection, use `message_priority.lead` as the core layer, `summary` as the layer-reassignment or keep-vs-promote guidance, and `defer` as the supporting layer.
- If `Current request.params.workflow.step` is `planner_advisory`, keep the result advisory-only and use `proposals.workflow_transition` so orchestrator sends the request back to planner finalization.
- If the workflow is reopening with `reopen_category='ux'`, keep the result advisory-only and leave the next execution decision to orchestrator through `proposals.workflow_transition`.
"""
        elif self.role == "architect":
            role_specific_rules = """

Architect-specific rules:
- If `Current request.params.workflow.step` is `planner_advisory`, act as a planning specialist only and return advisory output plus `proposals.workflow_transition` so planner can finalize.
- When designer advisory already defined usability, readability, or info-priority intent, translate that intent into implementation contracts and stage-fit guidance instead of replacing the designer decision itself.
- If the step is `architect_guidance`, produce implementation-ready technical guidance and then advance the workflow toward developer execution unless you must reopen or block.
- If the step is `architect_review`, review the implemented change for structural fit and emit review findings plus `proposals.workflow_transition` for developer revision.
- Treat planner-owned docs such as `backlog.md`, `completed_backlog.md`, `current_sprint.md`, `milestone.md`, `plan.md`, `spec.md`, `todo_backlog.md`, and `iteration_log.md` as read-only planning evidence, not implementation targets.
- Planner-owned 문서 정합성 점검과 상태 문서 동기화는 architect execution/review scope가 아니다. Implementation 단계에서는 코드, 테스트, 인터페이스, 구조 적합성만 판단하고 planner-owned doc drift는 runtime/orchestrator concern으로 남긴다.
- If `architect_review` passes without further developer work, set `proposals.workflow_transition.target_step = "qa_validation"` so orchestrator hands the todo directly to QA.
- In `architect_review`, use top-level `status="completed"` when the review step finished but developer revision is still required.
- Reserve top-level `blocked` for true hard blockers that should stop the current todo instead of continuing to developer revision.
- Workflow-managed architect review retries are capped, so repeated non-pass review loops should keep findings concrete and escalate to a real blocker or planning reopen when the issue is not converging.
"""
        elif self.role == "developer":
            role_specific_rules = """

Developer-specific rules:
- If `Current request.params.workflow.step` is `developer_build`, implement the planned change and leave test/validation context for the next review step.
- If the step is `developer_revision`, focus on addressing architect review findings before QA.
- If `developer_revision` needs another architect pass before QA, set `proposals.workflow_transition.target_step = "architect_review"` explicitly.
- When `Current request.params.workflow` exists, always return `proposals.workflow_transition`. Use `reopen` only when scope, UX, architecture, or implementation blockers truly require orchestrator to reroute.
- Do not edit or claim planner-owned docs such as `backlog.md`, `completed_backlog.md`, `current_sprint.md`, `milestone.md`, `plan.md`, `spec.md`, `todo_backlog.md`, or `iteration_log.md` as implementation output.
- If you find a mismatch on those planning surfaces, report the observed file state and reopen/block instead of claiming the document was updated.
- Treat renderer-only message work as `same meaning / same priority / same CTA` preservation work. Do not redesign information order, omission policy, or CTA wording during developer implementation unless planner/designer already supplied that contract.
- If implementation reveals a mixed case or missing designer judgment for message priority, do not silently make the UX decision in code. Leave the technical fix bounded, or reopen/block with explicit evidence that designer/planner input is still required.
"""
        elif self.role == "qa":
            role_specific_rules = """

QA-specific rules:
- When `Current request.params.workflow` exists, QA owns validation only. Return evidence-driven pass/fail findings and include `proposals.workflow_transition`.
- Read `spec.md` and the relevant planning docs before deciding pass/fail. Validate the implementation against both the code/test result and the spec contract.
- If the implemented result drifts from designer intent in message readability, information ordering, or user-facing structure, prefer `reopen_category='ux'` so orchestrator can reopen the workflow for designer support.
- You may cite planner-owned docs as evidence, but do not turn planner-owned doc mismatch into a developer fix request unless the implementation artifact actually changed those surfaces by contract.
- If validation fails because `spec.md` or explicit acceptance criteria no longer matches the accepted contract, reopen to `planner_finalize` instead of developer revision and cite the mismatched clauses or documents. `current_sprint.md` or other planner tracking doc drift alone is a runtime sync anomaly, not a QA blocker.
- Use `reopen` only when concrete scope, UX, architecture, implementation, or verification issues require another role.
"""
        elif self.role == "version_controller":
            extra_fields = """,
  "commit_status": "committed|no_changes|failed|no_repo",
  "commit_sha": "",
  "commit_message": "",
  "commit_paths": [],
  "change_detected": false"""
            role_specific_rules = """

Version-controller rules:
- Read `Current request.version_control` and the referenced `sources/*.version_control.json` payload before deciding anything.
- Run the provided git helper command and mirror its result into `commit_status`, `commit_sha`, `commit_message`, `commit_paths`, and `change_detected`.
- When both `title` and `functional_title` are present, treat `functional_title` as the preferred concrete behavior-change label.
- Keep top-level `status` as `completed` when `commit_status` is `committed` or `no_changes`.
- Use top-level `blocked` for task-mode commit failures and top-level `failed` for closeout-mode failures.
- Do not invent a commit result without running the helper command.
"""
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
  "status": "completed|blocked|failed",
  "summary": "short Korean summary",
  "insights": ["private role insight for journal.md"],
  "proposals": {{}},
  "artifacts": [],
  "error": ""
{extra_fields}
}}

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


class IntentParserRuntime:
    def __init__(
        self,
        *,
        paths: RuntimePaths,
        sprint_id: str,
        runtime_config: RoleRuntimeConfig,
    ):
        self.paths = paths
        self.role = "parser"
        self.sprint_id = sprint_id
        self.session_manager = RoleSessionManager(
            paths,
            self.role,
            sprint_id,
            agent_root=paths.internal_agent_root("parser"),
        )
        self.codex_runner = CodexRunner(runtime_config, role=self.role)
        self._run_lock = threading.Lock()

    def classify(
        self,
        *,
        raw_text: str,
        envelope: MessageEnvelope,
        scheduler_state: dict[str, Any],
        active_sprint: dict[str, Any],
        backlog_counts: dict[str, int],
        forwarded: bool,
    ) -> dict[str, Any]:
        with self._run_lock:
            state = self.session_manager.ensure_session()
            prompt = self._build_prompt(
                raw_text=raw_text,
                envelope=envelope,
                scheduler_state=scheduler_state,
                active_sprint=active_sprint,
                backlog_counts=backlog_counts,
                forwarded=forwarded,
            )
            output, session_id = self.codex_runner.run(Path(state.workspace_path), prompt, state.session_id or None)
            state = self.session_manager.finalize_session_id(state, session_id)
            try:
                payload = extract_json_object(output)
            except ValueError:
                payload = {
                    "intent": "route",
                    "scope": str(envelope.scope or raw_text or "").strip(),
                    "request_id": str(envelope.request_id or "").strip(),
                    "body": str(envelope.body or raw_text or "").strip(),
                    "reason": "intent parser response did not contain valid JSON",
                    "confidence": "low",
                }
            payload = normalize_intent_payload(payload)
            payload.setdefault("session_id", state.session_id)
            payload.setdefault("session_workspace", state.workspace_path)
            return payload

    def _build_prompt(
        self,
        *,
        raw_text: str,
        envelope: MessageEnvelope,
        scheduler_state: dict[str, Any],
        active_sprint: dict[str, Any],
        backlog_counts: dict[str, int],
        forwarded: bool,
    ) -> str:
        return f"""You are the internal parser agent inside teams_runtime.

You are not a public Discord bot. You help the orchestrator classify natural-language intake conservatively.

Return strict JSON only with this shape:
{{
  "intent": "route|status|cancel|execute",
  "scope": "sprint|backlog|request|short normalized scope",
  "request_id": "",
  "body": "normalized body or empty",
  "params": {{}},
  "reason": "short Korean reason",
  "confidence": "low|medium|high"
}}

Rules:
- Only return "status" when the message is clearly asking for current status or progress.
- For request status, include request_id and set scope to "request".
- For sprint status, set scope to "sprint".
- For backlog status, set scope to "backlog".
- Return "cancel" only when the user is clearly asking to cancel a request. Preserve request_id when available.
- Return "execute" only when the user is clearly invoking a registered action.
- If the current parsed envelope already contains `params.action_name`, preserve execute intent and that param.
- For manual sprint control phrases like `start sprint` and `finalize sprint`, return intent `route` and set `params.sprint_control` to `start` or `finalize`.
- English questions such as "What sprint is ongoing?", "What is the current sprint working for?", and "What are todos in backlog?" should map to sprint/backlog status when they are clearly asking for current state.
- Minor typos in those status questions can still be treated as status when the intent is obvious.
- Treat approval-like phrases as normal route text; approval is not a supported control flow.
- If uncertain, return intent "route".
- Preserve the user's work request meaning in route scope/body.

Configured sprint id: {self.sprint_id}
Forwarded via non-orchestrator role: {json.dumps(forwarded)}
Raw user text:
{raw_text}

Current parsed envelope:
{json.dumps(envelope.to_dict(include_routing=True), ensure_ascii=False, indent=2)}

Scheduler state:
{json.dumps(scheduler_state, ensure_ascii=False, indent=2)}

Active sprint summary:
{json.dumps({
    "sprint_id": active_sprint.get("sprint_id") or "",
    "status": active_sprint.get("status") or "",
    "trigger": active_sprint.get("trigger") or "",
}, ensure_ascii=False, indent=2)}

Backlog counts:
{json.dumps(backlog_counts, ensure_ascii=False, indent=2)}
"""


class BacklogSourcingRuntime:
    def __init__(
        self,
        *,
        paths: RuntimePaths,
        sprint_id: str,
        runtime_config: RoleRuntimeConfig,
    ):
        self.paths = paths
        self.role = "sourcer"
        self.sprint_id = sprint_id
        self.session_manager = RoleSessionManager(
            paths,
            self.role,
            sprint_id,
            agent_root=paths.internal_agent_root("sourcer"),
        )
        self.codex_runner = CodexRunner(runtime_config, role=self.role)
        self._run_lock = threading.Lock()

    def source(
        self,
        *,
        findings: list[dict[str, Any]],
        scheduler_state: dict[str, Any],
        active_sprint: dict[str, Any],
        backlog_counts: dict[str, int],
        existing_backlog: list[dict[str, Any]],
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
                "findings_count": len(findings),
                "findings_sample": _sample_item_labels(findings),
                "existing_backlog_count": len(existing_backlog),
                "existing_backlog_sample": _sample_item_labels(existing_backlog),
                "fallback_used": False,
            }
            LOGGER.info(
                "%s start findings=%s existing_backlog=%s reuse_session=%s",
                SOURCER_LOG_PREFIX,
                len(findings),
                len(existing_backlog),
                reused_session,
            )
            state = self.session_manager.ensure_session()
            monitoring["session_workspace"] = state.workspace_path
            monitoring["session_id_before"] = state.session_id or ""
            LOGGER.info(
                "%s prompt_build_start workspace=%s session_id=%s findings_sample=%s",
                SOURCER_LOG_PREFIX,
                state.workspace_path,
                state.session_id or "new",
                ", ".join(monitoring["findings_sample"]) or "none",
            )
            prompt = self._build_prompt(
                findings=findings,
                scheduler_state=scheduler_state,
                active_sprint=active_sprint,
                backlog_counts=backlog_counts,
                existing_backlog=existing_backlog,
            )
            monitoring["prompt_chars"] = len(prompt)
            LOGGER.info(
                "%s prompt_build_complete chars=%s existing_backlog_sample=%s",
                SOURCER_LOG_PREFIX,
                len(prompt),
                ", ".join(monitoring["existing_backlog_sample"]) or "none",
            )
            try:
                LOGGER.info(
                    "%s codex_run_start workspace=%s previous_session_id=%s",
                    SOURCER_LOG_PREFIX,
                    state.workspace_path,
                    state.session_id or "new",
                )
                output, session_id = self.codex_runner.run(
                    Path(state.workspace_path),
                    prompt,
                    state.session_id or None,
                )
            except Exception:
                monitoring["codex_run_status"] = "failed"
                monitoring["completed_at"] = utc_now_iso()
                monitoring["elapsed_ms"] = int((time.monotonic() - started_monotonic) * 1000)
                LOGGER.exception(
                    "%s codex_run_failed elapsed_ms=%s workspace=%s",
                    SOURCER_LOG_PREFIX,
                    monitoring["elapsed_ms"],
                    state.workspace_path,
                )
                raise
            monitoring["codex_run_status"] = "completed"
            monitoring["output_chars"] = len(output)
            LOGGER.info(
                "%s codex_run_complete output_chars=%s session_id=%s",
                SOURCER_LOG_PREFIX,
                len(output),
                session_id or state.session_id or "unknown",
            )
            state = self.session_manager.finalize_session_id(state, session_id)
            monitoring["session_id"] = state.session_id
            monitoring["session_workspace"] = state.workspace_path
            try:
                payload = extract_json_object(output)
                monitoring["json_parse_status"] = "success"
                LOGGER.info("%s json_parse_success session_id=%s", SOURCER_LOG_PREFIX, state.session_id or "unknown")
            except ValueError:
                monitoring["json_parse_status"] = "failed"
                monitoring["json_parse_error"] = "Backlog sourcer response did not contain valid JSON."
                LOGGER.warning(
                    "%s json_parse_failed session_id=%s error=%s",
                    SOURCER_LOG_PREFIX,
                    state.session_id or "unknown",
                    monitoring["json_parse_error"],
                )
                payload = {
                    "status": "failed",
                    "summary": "",
                    "backlog_items": [],
                    "error": "Backlog sourcer response did not contain valid JSON.",
                }
            payload = normalize_backlog_sourcing_payload(payload)
            raw_items = payload.get("backlog_items") if isinstance(payload.get("backlog_items"), list) else []
            monitoring["raw_backlog_items_count"] = len(raw_items)
            monitoring["raw_backlog_item_sample"] = _sample_item_labels(raw_items)
            monitoring["fallback_used"] = bool(not raw_items)
            monitoring["completed_at"] = utc_now_iso()
            monitoring["elapsed_ms"] = int((time.monotonic() - started_monotonic) * 1000)
            LOGGER.info(
                "%s complete status=%s raw_items=%s fallback_used=%s elapsed_ms=%s",
                SOURCER_LOG_PREFIX,
                str(payload.get("status") or "").strip() or "unknown",
                len(raw_items),
                monitoring["fallback_used"],
                monitoring["elapsed_ms"],
            )
            payload.setdefault("session_id", state.session_id)
            payload.setdefault("session_workspace", state.workspace_path)
            payload["monitoring"] = monitoring
            return payload

    def _build_prompt(
        self,
        *,
        findings: list[dict[str, Any]],
        scheduler_state: dict[str, Any],
        active_sprint: dict[str, Any],
        backlog_counts: dict[str, int],
        existing_backlog: list[dict[str, Any]],
    ) -> str:
        return f"""You are the internal backlog sourcing agent inside teams_runtime.

You are not a public Discord bot. You independently inspect runtime findings and propose backlog work for later execution.

Return strict JSON only with this shape:
{{
  "status": "completed|failed",
  "summary": "short Korean sourcing summary",
  "backlog_items": [
    {{
      "title": "short backlog title",
      "summary": "why this should enter backlog",
      "kind": "bug|enhancement|feature|chore",
      "scope": "clear actionable scope",
      "acceptance_criteria": ["optional acceptance criterion"],
      "milestone_title": "optional active sprint milestone when clearly relevant",
      "origin": {{}}
    }}
  ],
  "error": ""
}}

Rules:
- Only propose real future work items, not journal-only observations.
- Prefer bug when the finding indicates failure, traceback, regression, or broken behavior.
- Prefer feature for genuinely new capability requests, not generic improvements.
- Prefer enhancement for improving an existing workflow, agent, or document.
- Prefer chore for maintenance/cleanup tasks with low user-facing product impact.
- Avoid duplicates when an equivalent backlog item already exists.
- Do not emit blocked-only restatements unless there is a concrete next task for later.
- Keep each backlog item independently actionable.
- If an active sprint milestone exists, focus only on backlog items that clearly advance that milestone.
- If an active sprint milestone exists, prefer returning no backlog items over returning unrelated work.
- If an active sprint milestone exists, set `milestone_title` to that active sprint milestone on every returned item.
- Do not set `planned_in_sprint_id`; sourcer is backlog-first and planner decides sprint todo promotion.

Configured sprint id: {self.sprint_id}
Scheduler state:
{json.dumps(scheduler_state, ensure_ascii=False, indent=2)}

Active sprint summary:
{json.dumps({
    "sprint_id": active_sprint.get("sprint_id") or "",
    "sprint_name": active_sprint.get("sprint_name") or active_sprint.get("sprint_display_name") or "",
    "phase": active_sprint.get("phase") or "",
    "milestone_title": active_sprint.get("milestone_title") or "",
    "status": active_sprint.get("status") or "",
    "trigger": active_sprint.get("trigger") or "",
}, ensure_ascii=False, indent=2)}

Backlog counts:
{json.dumps(backlog_counts, ensure_ascii=False, indent=2)}

Existing active backlog:
{json.dumps(existing_backlog, ensure_ascii=False, indent=2)}

Raw findings:
{json.dumps(findings, ensure_ascii=False, indent=2)}
"""
