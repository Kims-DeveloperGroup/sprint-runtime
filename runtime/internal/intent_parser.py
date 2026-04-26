from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from teams_runtime.workflows.orchestration.ingress import is_manual_sprint_finalize_text, is_manual_sprint_start_text
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.runtime.codex_runner import CodexRunner, extract_json_object
from teams_runtime.runtime.identities import service_runtime_identity
from teams_runtime.runtime.session_manager import RoleSessionManager
from teams_runtime.shared.models import MessageEnvelope, RoleRuntimeConfig


REQUEST_ID_TEXT_PATTERN = re.compile(r"\brequest[_\s-]*id\s*[:=]?\s*([A-Za-z0-9._-]+)", re.IGNORECASE)
ALLOWED_PARSER_INTENTS = {"route", "status", "cancel", "execute"}
ALLOWED_PARSER_CONFIDENCE = {"low", "medium", "high"}
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


def _normalize_inquiry_text(value: str) -> str:
    normalized = str(value or "").strip().lower()
    for token in ("_", "-", "/", "\\", ".", ",", ":", ";", "!", "?", "(", ")", "[", "]", "{", "}", "\"", "'"):
        normalized = normalized.replace(token, " ")
    return " ".join(normalized.split())


def _contains_any_term(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


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


class IntentParserRuntime:
    def __init__(
        self,
        *,
        paths: RuntimePaths,
        sprint_id: str,
        runtime_config: RoleRuntimeConfig,
        session_identity: str | None = None,
    ):
        self.paths = paths
        self.role = "parser"
        self.sprint_id = sprint_id
        self.runtime_identity = str(session_identity or service_runtime_identity(self.role)).strip() or service_runtime_identity(self.role)
        self.session_manager = RoleSessionManager(
            paths,
            self.role,
            sprint_id,
            agent_root=paths.internal_agent_root("parser"),
            runtime_identity=self.runtime_identity,
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


__all__ = [
    "IntentParserRuntime",
    "infer_status_inquiry_payload",
    "normalize_intent_payload",
]
