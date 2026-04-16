from __future__ import annotations

import json
import re
from typing import Any

from teams_runtime.models import MessageEnvelope, TEAM_ROLES


KEY_VALUE_PATTERN = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$")
MENTION_PATTERN = re.compile(r"<@!?(\d+)>")
STRUCTURED_ENVELOPE_KEYS = {
    "request_id",
    "from",
    "to",
    "intent",
    "action",
    "urgency",
    "scope",
    "artifacts",
    "params",
}
SPRINT_CONTROL_START_PATTERN = re.compile(
    r"(?is)\b(start|begin|kickoff|run|open|create)\b.{0,24}\bsprint\b|스프린트.{0,12}(시작|열어|열기|만들|생성)"
)
SPRINT_CONTROL_FINALIZE_PATTERN = re.compile(
    r"(?is)\b(finalize|finish|close|wrap\s*up|end)\b.{0,24}\bsprint\b|스프린트.{0,12}(종료|마무리|랩업|끝내)"
)


def _parse_key_value_lines(content: str) -> tuple[dict[str, str], list[str]]:
    parsed: dict[str, str] = {}
    body_lines: list[str] = []
    for raw_line in str(content or "").splitlines():
        line = raw_line.rstrip()
        match = KEY_VALUE_PATTERN.match(line.strip())
        if match:
            key = match.group(1).strip().lower()
            if key in STRUCTURED_ENVELOPE_KEYS:
                parsed[key] = match.group(2).strip()
                continue
        if line.strip():
            body_lines.append(line.strip())
    return parsed, body_lines


def _parse_freeform_command(content: str) -> dict[str, str]:
    collapsed = " ".join(str(content or "").strip().split())
    if not collapsed:
        return {}
    parts = collapsed.split()
    command = parts[0].lower()
    if command in {"approve", "cancel", "status"}:
        parsed = {"intent": command}
        match = re.search(r"request_id\s*:\s*([A-Za-z0-9._-]+)", collapsed, re.IGNORECASE)
        if match:
            parsed["request_id"] = match.group(1).strip()
        elif command == "status" and len(parts) > 1:
            scope = parts[1].strip().lower()
            if scope in {"sprint", "backlog"}:
                parsed["scope"] = scope
        return parsed
    return {}


def _strip_known_bot_mentions(content: str, bot_ids_by_role: dict[str, str] | None) -> str:
    if not bot_ids_by_role:
        return str(content or "")
    known_bot_ids = {str(bot_id) for bot_id in bot_ids_by_role.values()}

    def replace(match: re.Match[str]) -> str:
        mentioned_id = match.group(1)
        return "" if mentioned_id in known_bot_ids else match.group(0)

    cleaned_lines: list[str] = []
    for raw_line in str(content or "").splitlines():
        stripped_mentions = MENTION_PATTERN.sub(replace, raw_line)
        normalized = " ".join(stripped_mentions.split()).strip()
        if normalized:
            cleaned_lines.append(normalized)
    return "\n".join(cleaned_lines)


def detect_target_role_from_mentions(
    content: str,
    bot_ids_by_role: dict[str, str],
) -> str | None:
    mentioned_ids = [match.group(1) for match in MENTION_PATTERN.finditer(str(content or ""))]
    for role, bot_id in bot_ids_by_role.items():
        if bot_id in mentioned_ids:
            return role
    return None


def is_manual_sprint_start_text(content: str) -> bool:
    return bool(SPRINT_CONTROL_START_PATTERN.search(str(content or "")))


def is_manual_sprint_finalize_text(content: str) -> bool:
    return bool(SPRINT_CONTROL_FINALIZE_PATTERN.search(str(content or "")))


def detect_message_shape(
    content: str,
    *,
    bot_ids_by_role: dict[str, str] | None = None,
) -> str:
    sanitized_content = _strip_known_bot_mentions(content, bot_ids_by_role)
    parsed, _body_lines = _parse_key_value_lines(sanitized_content)
    if parsed:
        return "structured"
    if _parse_freeform_command(sanitized_content):
        return "command"
    return "freeform"


def parse_message_content(
    content: str,
    *,
    bot_ids_by_role: dict[str, str] | None = None,
    default_sender: str = "user",
    default_target: str = "orchestrator",
) -> MessageEnvelope:
    sanitized_content = _strip_known_bot_mentions(content, bot_ids_by_role)
    parsed, body_lines = _parse_key_value_lines(sanitized_content)
    if not parsed:
        parsed = _parse_freeform_command(sanitized_content)

    raw_params = parsed.get("params", "")
    params: dict[str, Any] = {}
    if raw_params:
        try:
            decoded = json.loads(raw_params)
        except json.JSONDecodeError:
            decoded = {}
        if isinstance(decoded, dict):
            params = decoded

    target = str(parsed.get("to") or "").strip()
    if not target and bot_ids_by_role:
        target = detect_target_role_from_mentions(content, bot_ids_by_role) or ""
    if not target:
        target = default_target
    if target not in TEAM_ROLES:
        target = default_target

    intent = str(parsed.get("intent") or parsed.get("action") or "").strip().lower()
    if not intent:
        intent = "route"

    body = "\n".join(body_lines).strip()
    scope = str(parsed.get("scope") or "").strip() or body or " ".join(sanitized_content.split())
    artifacts = [
        item.strip()
        for item in str(parsed.get("artifacts") or "").split(",")
        if item.strip()
    ]

    return MessageEnvelope(
        request_id=str(parsed.get("request_id") or "").strip() or None,
        sender=str(parsed.get("from") or default_sender).strip() or default_sender,
        target=target,
        intent=intent,
        urgency=str(parsed.get("urgency") or "normal").strip().lower() or "normal",
        scope=scope,
        artifacts=artifacts,
        params=params,
        body=body,
    )


def parse_user_message_content(
    content: str,
    *,
    artifacts: list[str] | None = None,
    bot_ids_by_role: dict[str, str] | None = None,
    default_sender: str = "user",
    default_target: str = "orchestrator",
) -> MessageEnvelope:
    sanitized_content = _strip_known_bot_mentions(content, bot_ids_by_role)
    target = default_target
    if bot_ids_by_role:
        detected = detect_target_role_from_mentions(content, bot_ids_by_role)
        if detected in TEAM_ROLES:
            target = detected
    normalized = str(sanitized_content or "").strip()
    normalized_artifacts = [str(item).strip() for item in (artifacts or []) if str(item).strip()]
    if not normalized and normalized_artifacts:
        normalized = f"첨부 파일 {len(normalized_artifacts)}건이 포함된 사용자 요청"
    return MessageEnvelope(
        request_id=None,
        sender=str(default_sender or "user").strip() or "user",
        target=target,
        intent="route",
        urgency="normal",
        scope=normalized,
        artifacts=normalized_artifacts,
        params={},
        body=normalized,
    )


def envelope_to_text(envelope: MessageEnvelope) -> str:
    artifacts_text = ", ".join(envelope.artifacts)
    params_payload = dict(envelope.params)
    result_payload = params_payload.pop("result", None)
    params_text = json.dumps(params_payload, ensure_ascii=False, sort_keys=True) if params_payload else ""
    lines = [
        f"request_id: {envelope.request_id or ''}",
        f"intent: {envelope.intent}",
        f"urgency: {envelope.urgency}",
        f"scope: {envelope.scope}",
        f"artifacts: {artifacts_text}",
        f"params: {params_text}",
    ]
    body = envelope.body
    if isinstance(result_payload, dict):
        body = "```json\n" + json.dumps(result_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"
    if body and str(body).strip() and str(body).strip() != str(envelope.scope or "").strip():
        lines.append("")
        lines.append(str(body).strip())
    return "\n".join(lines).strip()
