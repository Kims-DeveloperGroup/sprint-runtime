"""Ingress parsing and request-intake helpers plus compatibility re-exports."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Collection

from teams_runtime.shared.persistence import build_request_fingerprint, new_request_id, read_json
from teams_runtime.workflows.state.request_store import append_request_event
from teams_runtime.adapters.discord.client import DiscordListenError, DiscordMessage, classify_discord_exception
from teams_runtime.shared.models import MessageEnvelope, RequestRecord, TEAM_ROLES


LOGGER = logging.getLogger(__name__)
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


ATTACHMENT_SAVE_FAILURE_REPLY = "첨부 파일을 저장하지 못했습니다. 파일을 다시 보내거나 본문과 함께 다시 요청해 주세요."


def is_attachment_only_save_failure(message: DiscordMessage) -> bool:
    if str(message.content or "").strip():
        return False
    if not message.attachments:
        return False
    return not any(str(item.saved_path or "").strip() for item in message.attachments)


def _bot_ids_by_role(service: Any) -> dict[str, str]:
    return {
        role: cfg.bot_id
        for role, cfg in getattr(getattr(service, "discord_config", None), "agents", {}).items()
    }


async def listen_forever(
    service: Any,
    *,
    retry_seconds: float,
    logger: logging.Logger,
) -> None:
    while True:
        try:
            await service.discord_client.listen(service.handle_message, on_ready=service._on_ready)
        except asyncio.CancelledError:
            raise
        except DiscordListenError as exc:
            diagnostics = classify_discord_exception(
                exc,
                token_env_name=service.role_config.token_env,
                expected_bot_id=service.role_config.bot_id,
            )
            service._record_listener_health_state(
                status="reconnecting",
                error=diagnostics["summary"],
                category=diagnostics["category"],
                recovery_action=diagnostics["recovery_action"],
            )
            logger.warning(
                "Discord listener loop waiting %.1fs for role %s after listen error: %s",
                retry_seconds,
                service.role,
                exc,
            )
        except Exception as exc:
            diagnostics = classify_discord_exception(
                exc,
                token_env_name=service.role_config.token_env,
                expected_bot_id=service.role_config.bot_id,
            )
            service._record_listener_health_state(
                status="reconnecting",
                error=diagnostics["summary"],
                category=diagnostics["category"],
                recovery_action=diagnostics["recovery_action"],
            )
            logger.exception("Discord listener loop failed for role %s; retrying", service.role)
        await asyncio.sleep(retry_seconds)


async def on_ready(service: Any) -> None:
    current_identity = getattr(service.discord_client, "current_identity", None)
    identity = current_identity() if callable(current_identity) else {}
    service._record_listener_health_state(
        status="connected",
        error="",
        category="connected",
        recovery_action="",
        connected_bot_name=str(identity.get("name") or ""),
        connected_bot_id=str(identity.get("id") or ""),
    )
    await service._announce_startup()
    if service.role != "orchestrator":
        if service._pending_role_request_resume_task is None or service._pending_role_request_resume_task.done():
            service._pending_role_request_resume_task = asyncio.create_task(
                service._resume_pending_role_requests_loop()
            )
        return
    scheduler_state = service._load_scheduler_state()
    active_sprint_id = str(scheduler_state.get("active_sprint_id") or "").strip()
    if active_sprint_id:
        asyncio.create_task(service._resume_active_sprint(active_sprint_id))
        return
    await service._maybe_request_idle_sprint_milestone(reason="startup_no_active_sprint")


async def handle_message(service: Any, message: DiscordMessage) -> None:
    if not is_message_allowed(service, message):
        return
    await service._send_immediate_receipt(message)
    if service.role == "orchestrator":
        await service._handle_orchestrator_message(message)
        return
    await service._handle_non_orchestrator_message(message)


def is_message_allowed(service: Any, message: DiscordMessage) -> bool:
    if message.guild_id and service.runtime_config.allowed_guild_ids:
        return message.guild_id in service.runtime_config.allowed_guild_ids
    if message.is_dm and not service.runtime_config.ingress_dm:
        return False
    if (
        not message.is_dm
        and not service.runtime_config.ingress_mentions
        and not is_trusted_relay_message(service, message)
    ):
        return False
    return True


def is_trusted_relay_message(service: Any, message: DiscordMessage) -> bool:
    return (
        message.channel_id == service.discord_config.relay_channel_id
        and message.author_id in service.discord_config.trusted_bot_ids
    )


async def handle_orchestrator_message(service: Any, message: DiscordMessage) -> None:
    if service._is_trusted_relay_message(message):
        if service._is_internal_relay_summary_message(message):
            return
        envelope = parse_message_content(
            message.content,
            bot_ids_by_role=_bot_ids_by_role(service),
            default_sender="user",
            default_target="orchestrator",
        )
        kind = str(envelope.params.get("_teams_kind") or "").strip()
        if kind == "report":
            await service._handle_role_report(message, envelope)
            return
        if kind == "forward":
            await service._handle_user_request(message, envelope, forwarded=True)
            return
        service._log_malformed_trusted_relay(reason="unsupported kind for orchestrator", kind=kind)
        return
    if is_attachment_only_save_failure(message):
        await service._send_channel_reply(message, ATTACHMENT_SAVE_FAILURE_REPLY)
        return
    envelope = parse_user_message_content(
        message.content,
        artifacts=service._message_attachment_artifacts(message),
        bot_ids_by_role=_bot_ids_by_role(service),
        default_sender="user",
        default_target="orchestrator",
    )
    await service._handle_user_request(message, envelope, forwarded=False)


async def handle_non_orchestrator_message(service: Any, message: DiscordMessage) -> None:
    if service._is_trusted_relay_message(message):
        if service._is_internal_relay_summary_message(message):
            return
        envelope = parse_message_content(
            message.content,
            bot_ids_by_role=_bot_ids_by_role(service),
            default_sender="user",
            default_target=service.role,
        )
        kind = str(envelope.params.get("_teams_kind") or "").strip()
        if kind != "delegate":
            service._log_malformed_trusted_relay(reason="missing delegate kind", kind=kind)
            return
        if envelope.target != service.role:
            return
        await service._handle_delegated_request(message, envelope)
        return
    if is_attachment_only_save_failure(message):
        await service._send_channel_reply(message, ATTACHMENT_SAVE_FAILURE_REPLY)
        return
    envelope = parse_user_message_content(
        message.content,
        artifacts=service._message_attachment_artifacts(message),
        bot_ids_by_role=_bot_ids_by_role(service),
        default_sender="user",
        default_target=service.role,
    )
    if envelope.target != service.role:
        LOGGER.info(
            "Ignoring user message for other role in %s: target=%s",
            service.role,
            envelope.target,
        )
        return
    await service._forward_user_request(message, envelope)


async def handle_user_request(
    service: Any,
    message: DiscordMessage,
    envelope: MessageEnvelope,
    *,
    forwarded: bool,
) -> None:
    service._ensure_orchestrator_session_ready_for_sprint_start(envelope)
    duplicate_request = service._find_duplicate_request(message, envelope)
    if duplicate_request:
        reopened_request = await service._maybe_reopen_blocked_duplicate_request(
            duplicate_request,
            message,
            envelope,
            forwarded=forwarded,
        )
        if reopened_request:
            request_record, relay_sent, reopen_mode = reopened_request
            reopen_reply = build_duplicate_reopen_reply_payload(
                request_record,
                relay_sent=relay_sent,
                reopen_mode=reopen_mode,
            )
            if reopen_reply.get("mode") == "status":
                await service._reply_to_requester(
                    request_record,
                    service._build_requester_status_message(
                        status=str(reopen_reply.get("status") or "delegated"),
                        request_id=str(reopen_reply.get("request_id") or request_record.get("request_id") or ""),
                        summary=str(reopen_reply.get("summary") or ""),
                    ),
                )
            else:
                await service._reply_to_requester(
                    request_record,
                    str(reopen_reply.get("content") or ""),
                )
            return
        append_request_event(
            duplicate_request,
            event_type="reused",
            actor="orchestrator",
            summary="중복 요청을 기존 request에 연결했습니다.",
            payload={"message_id": message.message_id},
        )
        service._save_request(duplicate_request)
        await service._reply_to_requester(
            duplicate_request,
            build_reused_duplicate_requester_message(duplicate_request),
        )
        return
    request_record = service._create_request_record(message, envelope, forwarded=forwarded)
    request_record = mark_user_request_delegated_to_orchestrator(
        request_record,
        routing_context=service._build_routing_context(
            "orchestrator",
            reason="Selected orchestrator as the first agent owner for this user-originated request.",
            requested_role="orchestrator",
            selection_source="agent_first_intake",
        ),
    )
    service._save_request(request_record)
    service._append_role_history(
        "orchestrator",
        request_record,
        event_type="delegated",
        summary="사용자 작업 요청을 orchestrator agent로 전달했습니다.",
    )
    await service._run_local_orchestrator_request(request_record)


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


_PARSING_EXPORTS = [
    "KEY_VALUE_PATTERN",
    "MENTION_PATTERN",
    "SPRINT_CONTROL_FINALIZE_PATTERN",
    "SPRINT_CONTROL_START_PATTERN",
    "STRUCTURED_ENVELOPE_KEYS",
    "detect_message_shape",
    "detect_target_role_from_mentions",
    "envelope_to_text",
    "is_manual_sprint_finalize_text",
    "is_manual_sprint_start_text",
    "parse_message_content",
    "parse_user_message_content",
]



SPRINT_MILESTONE_PATTERN = re.compile(r"(?im)^(?:milestone|마일스톤)\s*[:=-]\s*(?P<value>[^\n\r]+)\s*$")
SPRINT_KICKOFF_BRIEF_PATTERN = re.compile(r"(?is)^(?:brief|details|context|notes|설명|배경|메모)\s*[:=-]\s*(?P<value>.*)$")
SPRINT_KICKOFF_REQUIREMENTS_PATTERN = re.compile(
    r"(?is)^(?:requirements?|requirement|constraints?|needs?|요구사항|요건|제약)\s*[:=-]?\s*(?P<value>.*)$"
)
SPRINT_BULLET_LINE_PATTERN = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s+(?P<value>.+?)\s*$")
SPRINT_COMMAND_LINE_PATTERN = re.compile(
    r"(?is)^\s*(?:(?:start|begin|kickoff|run|open|create)\b.*\bsprint\b|스프린트.{0,24}(?:시작|열어|열기|만들|생성))\s*$"
)
PLANNING_ENVELOPE_EXPLICIT_SOURCE_MARKERS = (
    "shared_workspace/",
    ".md",
    "backlog-",
    "todo-",
    "request_id=",
    ".json",
)


def extract_original_requester(params: dict[str, Any]) -> dict[str, Any]:
    nested = params.get("original_requester")
    if isinstance(nested, dict):
        return nested
    flat_keys = {
        "author_id": "requester_author_id",
        "author_name": "requester_author_name",
        "channel_id": "requester_channel_id",
        "guild_id": "requester_guild_id",
        "message_id": "requester_message_id",
    }
    requester = {
        target_key: str(params.get(source_key) or "").strip()
        for target_key, source_key in flat_keys.items()
        if str(params.get(source_key) or "").strip()
    }
    if "requester_is_dm" in params:
        requester["is_dm"] = bool(params.get("requester_is_dm"))
    return requester


def merge_requester_route(*sources: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("author_id", "author_name", "channel_id", "guild_id", "message_id"):
            value = str(source.get(key) or "").strip()
            if value and not str(merged.get(key) or "").strip():
                merged[key] = value
        if "is_dm" in source and "is_dm" not in merged:
            merged["is_dm"] = bool(source.get("is_dm"))
    return merged


def build_requester_route(
    message: DiscordMessage,
    envelope: MessageEnvelope,
    *,
    forwarded: bool,
) -> dict[str, Any]:
    requester = extract_original_requester(envelope.params) if forwarded else {}
    return {
        "author_id": requester.get("author_id", message.author_id) if forwarded else message.author_id,
        "author_name": requester.get("author_name", message.author_name) if forwarded else message.author_name,
        "channel_id": requester.get("channel_id", message.channel_id) if forwarded else message.channel_id,
        "guild_id": requester.get("guild_id", message.guild_id or "") if forwarded else (message.guild_id or ""),
        "is_dm": requester.get("is_dm", message.is_dm) if forwarded else message.is_dm,
        "message_id": requester.get("message_id", message.message_id) if forwarded else message.message_id,
    }


def build_request_fingerprint_from_route(
    route: dict[str, Any],
    *,
    intent: str,
    scope: str,
) -> str:
    return build_request_fingerprint(
        author_id=str(route.get("author_id") or "").strip(),
        channel_id=str(route.get("channel_id") or "").strip(),
        intent=intent,
        scope=scope,
    )


def request_identity_from_envelope(
    message: DiscordMessage,
    envelope: MessageEnvelope,
    *,
    forwarded: bool,
) -> tuple[str, str]:
    requester_route = build_requester_route(message, envelope, forwarded=forwarded)
    return (
        str(requester_route.get("author_id") or ""),
        str(requester_route.get("channel_id") or ""),
    )


def build_duplicate_request_fingerprint(
    message: DiscordMessage,
    envelope: MessageEnvelope,
) -> str:
    requester = extract_original_requester(envelope.params)
    requester_route = build_requester_route(
        message,
        envelope,
        forwarded=bool(requester),
    )
    return build_request_fingerprint_from_route(
        requester_route,
        intent=envelope.intent,
        scope=envelope.scope,
    )


def request_identity_matches(request_record: dict[str, Any], *, author_id: str, channel_id: str) -> bool:
    reply_route = dict(request_record.get("reply_route") or {}) if isinstance(request_record.get("reply_route"), dict) else {}
    return (
        str(reply_route.get("author_id") or "").strip() == str(author_id or "").strip()
        and str(reply_route.get("channel_id") or "").strip() == str(channel_id or "").strip()
    )


def should_request_sprint_milestone_for_relay_intake(
    *,
    intent: str,
    requester_route: dict[str, Any],
    relay_channel_id: str,
    has_active_sprint: bool,
) -> bool:
    if str(intent or "").strip().lower() != "route":
        return False
    if bool(requester_route.get("is_dm")):
        return False
    channel_id = str(requester_route.get("channel_id") or "").strip()
    normalized_relay_channel_id = str(relay_channel_id or "").strip()
    if not channel_id or channel_id != normalized_relay_channel_id:
        return False
    return not has_active_sprint


def build_forwarded_request_params(
    message: DiscordMessage,
    envelope: MessageEnvelope,
    *,
    valid_user_requested_roles: Collection[str],
) -> dict[str, Any]:
    user_requested_role = str(envelope.target or "").strip().lower()
    return {
        **dict(envelope.params),
        "_teams_kind": "forward",
        "requester_author_id": message.author_id,
        "requester_author_name": message.author_name,
        "requester_channel_id": message.channel_id,
        "requester_guild_id": message.guild_id or "",
        "requester_is_dm": message.is_dm,
        "requester_message_id": message.message_id,
        "user_requested_role": user_requested_role if user_requested_role in set(valid_user_requested_roles) else "",
    }


def build_forwarded_user_envelope(
    message: DiscordMessage,
    envelope: MessageEnvelope,
    *,
    sender_role: str,
    request_id: str,
    valid_user_requested_roles: Collection[str],
) -> MessageEnvelope:
    return MessageEnvelope(
        request_id=request_id,
        sender=sender_role,
        target="orchestrator",
        intent=envelope.intent,
        urgency=envelope.urgency,
        scope=envelope.scope,
        artifacts=list(envelope.artifacts),
        params=build_forwarded_request_params(
            message,
            envelope,
            valid_user_requested_roles=valid_user_requested_roles,
        ),
        body=envelope.body,
    )


async def forward_user_request(service: Any, message: DiscordMessage, envelope: MessageEnvelope) -> None:
    request_id = envelope.request_id or new_request_id()
    forwarded_envelope = build_forwarded_user_envelope(
        message,
        envelope,
        sender_role=service.role,
        request_id=request_id,
        valid_user_requested_roles=TEAM_ROLES,
    )
    await service._send_relay(forwarded_envelope)


async def reply_status_request(service: Any, message: DiscordMessage, envelope: MessageEnvelope) -> None:
    if not envelope.request_id:
        scope_text = str(envelope.scope or envelope.body or "").strip().lower()
        if scope_text == "sprint":
            scheduler_state = service._load_scheduler_state()
            active_sprint = service._load_sprint_state(str(scheduler_state.get("active_sprint_id") or ""))
            is_active = bool(active_sprint)
            if not active_sprint:
                sprint_files = sorted(service.paths.sprints_dir.glob("*.json"))
                active_sprint = read_json(sprint_files[-1]) if sprint_files else {}
            if not active_sprint:
                await service._send_channel_reply(message, "기록된 sprint가 없습니다.")
                return
            await service._send_channel_reply(
                message,
                service._render_sprint_status_report(
                    active_sprint,
                    is_active=is_active,
                    scheduler_state=scheduler_state,
                ),
            )
            return
        if scope_text == "backlog":
            await service._send_channel_reply(message, service._render_backlog_status_report())
            return
    request_record = service._load_request(envelope.request_id or "")
    if not request_record:
        await service._send_channel_reply(message, "해당 request_id를 찾을 수 없습니다.")
        return
    lines = [
        f"request_id={request_record['request_id']}",
        f"status={request_record.get('status') or 'unknown'}",
        f"current_role={request_record.get('current_role') or 'N/A'}",
        service._format_sprint_scope(sprint_id=str(request_record.get("sprint_id") or "")),
    ]
    if request_record.get("version_control_status"):
        lines.append(f"version_control_status={request_record.get('version_control_status')}")
    commit_message = (
        _first_meaningful_text(
            request_record.get("task_commit_message"),
            request_record.get("version_control_message"),
        )
        if str(request_record.get("version_control_status") or "").strip() == "committed"
        else ""
    )
    if commit_message:
        lines.append(f"commit_message={commit_message}")
    version_control_paths = [
        str(item).strip()
        for item in (
            request_record.get("version_control_paths")
            or request_record.get("task_commit_paths")
            or []
        )
        if str(item).strip()
    ]
    if version_control_paths:
        lines.append(f"version_control_paths={', '.join(version_control_paths)}")
    if request_record.get("operation_id"):
        operation = service.action_executor.get_operation_status(str(request_record["operation_id"]))
        if operation:
            lines.append(f"operation_status={operation.get('status')}")
    await service._send_channel_reply(message, "\n".join(lines))


async def cancel_request(service: Any, message: DiscordMessage, envelope: MessageEnvelope) -> None:
    request_record = service._load_request(envelope.request_id or "")
    if not request_record:
        await service._send_channel_reply(message, "취소할 request_id를 찾을 수 없습니다.")
        return
    if str(request_record.get("status") or "").strip().lower() == "uncommitted":
        version_control_paths = [
            str(item).strip()
            for item in (
                request_record.get("version_control_paths")
                or request_record.get("task_commit_paths")
                or []
            )
            if str(item).strip()
        ]
        warning = (
            f"요청은 아직 uncommitted 상태라 취소할 수 없습니다. request_id={request_record['request_id']}\n"
            "task-owned 변경이 남아 있으니 version_controller recovery 또는 수동 git 정리가 필요합니다."
        )
        if version_control_paths:
            warning += "\nremaining_paths=" + ", ".join(version_control_paths)
        await service._reply_to_requester(
            request_record,
            service._build_requester_status_message(
                status="blocked",
                request_id=str(request_record["request_id"]),
                summary=warning,
            ),
        )
        return
    request_record["status"] = "cancelled"
    append_request_event(
        request_record,
        event_type="cancelled",
        actor="orchestrator",
        summary="사용자 요청으로 취소되었습니다.",
    )
    service._save_request(request_record)
    service._append_role_history(
        "orchestrator",
        request_record,
        event_type="cancelled",
        summary="사용자 요청으로 취소되었습니다.",
    )
    await service._reply_to_requester(
        request_record,
        service._build_requester_status_message(
            status="cancelled",
            request_id=str(request_record["request_id"]),
            summary="요청을 취소했습니다.",
        ),
    )


async def execute_registered_action(service: Any, message: DiscordMessage, envelope: MessageEnvelope) -> None:
    action_name = str(envelope.params.get("action_name") or "").strip()
    if not action_name:
        await service._send_channel_reply(message, "action_name이 필요합니다.")
        return
    if action_name not in service.runtime_config.actions:
        await service._send_channel_reply(message, f"등록되지 않은 action입니다: {action_name}")
        return
    request_record = service._create_request_record(message, envelope, forwarded=False)
    execution = await asyncio.to_thread(
        service.action_executor.execute,
        request_id=request_record["request_id"],
        action_name=action_name,
        params={k: v for k, v in envelope.params.items() if k != "action_name"},
    )
    request_record["operation_id"] = execution["operation_id"]
    request_record["status"] = execution["status"]
    append_request_event(
        request_record,
        event_type="action_execute",
        actor="orchestrator",
        summary=f"{action_name} 액션을 실행했습니다.",
        payload=execution,
    )
    service._save_request(request_record)
    service._append_role_history(
        "orchestrator",
        request_record,
        event_type="action_execute",
        summary=f"{action_name} 액션을 실행했습니다.",
    )
    await service._reply_to_requester(request_record, execution.get("report") or "액션을 실행했습니다.")


def planning_envelope_has_explicit_source_context(envelope: MessageEnvelope) -> bool:
    if envelope.artifacts:
        return True
    combined = f"{envelope.scope}\n{envelope.body}".lower()
    return any(marker in combined for marker in PLANNING_ENVELOPE_EXPLICIT_SOURCE_MARKERS)


def build_planning_envelope_with_inferred_verification(
    envelope: MessageEnvelope,
    *,
    verification_request_id: str,
    artifact_path: str,
) -> MessageEnvelope:
    normalized_artifact_path = str(artifact_path or "").strip()
    if not normalized_artifact_path:
        return envelope
    artifacts = [str(item).strip() for item in envelope.artifacts if str(item).strip()]
    if normalized_artifact_path not in artifacts:
        artifacts.append(normalized_artifact_path)
    params = dict(envelope.params)
    normalized_request_id = str(verification_request_id or "").strip()
    if normalized_request_id:
        params.setdefault("inferred_source_request_id", normalized_request_id)
    params.setdefault("inferred_source_artifact", normalized_artifact_path)
    return MessageEnvelope(
        request_id=envelope.request_id,
        sender=envelope.sender,
        target=envelope.target,
        intent=envelope.intent,
        urgency=envelope.urgency,
        scope=envelope.scope,
        artifacts=artifacts,
        params=params,
        body=envelope.body,
    )


@dataclass(frozen=True, slots=True)
class RequestRecordSeed:
    author_id: str
    channel_id: str
    is_dm: bool
    params: dict[str, Any]
    reply_route: dict[str, Any]
    fingerprint: str


def build_request_record_seed(
    message: DiscordMessage,
    envelope: MessageEnvelope,
    *,
    forwarded: bool,
    valid_user_requested_roles: Collection[str],
) -> RequestRecordSeed:
    requester = extract_original_requester(envelope.params) if forwarded else {}
    reply_route = build_requester_route(message, envelope, forwarded=forwarded)
    params = dict(envelope.params)
    if envelope.target in valid_user_requested_roles and envelope.target != "orchestrator":
        params["user_requested_role"] = str(envelope.target)
    if forwarded:
        normalized_requester = merge_requester_route(requester, reply_route)
        if normalized_requester:
            params["original_requester"] = normalized_requester
    return RequestRecordSeed(
        author_id=str(reply_route.get("author_id") or "").strip(),
        channel_id=str(reply_route.get("channel_id") or "").strip(),
        is_dm=bool(reply_route.get("is_dm")),
        params=params,
        reply_route=dict(reply_route),
        fingerprint=build_request_fingerprint_from_route(
            reply_route,
            intent=envelope.intent,
            scope=envelope.scope,
        ),
    )


def build_request_record(
    seed: RequestRecordSeed,
    *,
    envelope: MessageEnvelope,
    request_id: str,
    sprint_id: str,
    source_message_created_at: str,
    created_at: str,
    updated_at: str,
) -> RequestRecord:
    return {
        "request_id": str(request_id or "").strip(),
        "status": "queued",
        "intent": envelope.intent,
        "urgency": envelope.urgency,
        "scope": envelope.scope,
        "body": envelope.body,
        "artifacts": list(envelope.artifacts),
        "params": dict(seed.params),
        "current_role": "orchestrator",
        "next_role": "",
        "owner_role": "orchestrator",
        "sprint_id": str(sprint_id or "").strip(),
        "source_message_created_at": str(source_message_created_at or "").strip(),
        "created_at": str(created_at or "").strip(),
        "updated_at": str(updated_at or "").strip(),
        "fingerprint": str(seed.fingerprint or "").strip(),
        "reply_route": dict(seed.reply_route),
        "events": [],
        "result": {},
    }


def apply_blocked_duplicate_retry(
    request_record: dict[str, Any],
    *,
    requester_route: dict[str, Any],
    scope: str,
    followup_body: str,
    existing_artifacts: list[str],
    routing_context: dict[str, Any],
    message_id: str,
) -> dict[str, Any]:
    normalized_followup_body = str(followup_body or "").strip()
    if normalized_followup_body:
        request_record["body"] = normalized_followup_body
    request_record["scope"] = str(scope or request_record.get("scope") or "").strip()
    request_record["artifacts"] = [str(item).strip() for item in existing_artifacts if str(item).strip()]
    request_record["reply_route"] = dict(requester_route)
    request_record["status"] = "delegated"
    request_record["current_role"] = "orchestrator"
    request_record["next_role"] = "orchestrator"
    request_record["routing_context"] = dict(routing_context or {})
    params = dict(request_record.get("params") or {})
    params["retry_followup_message_id"] = str(message_id or "").strip()
    if normalized_followup_body:
        params["retry_followup_body"] = normalized_followup_body
    request_record["params"] = params
    append_request_event(
        request_record,
        event_type="retried",
        actor="orchestrator",
        summary="반복된 사용자 요청으로 기존 blocked 요청을 다시 실행합니다.",
        payload={"message_id": str(message_id or "").strip()},
    )
    return request_record


def apply_blocked_duplicate_augmentation(
    request_record: dict[str, Any],
    *,
    followup_body: str,
    existing_artifacts: list[str],
    new_artifacts: list[str],
) -> dict[str, Any]:
    normalized_followup_body = str(followup_body or "").strip()
    if normalized_followup_body:
        request_record["body"] = normalized_followup_body
    merged_artifacts = [str(item).strip() for item in existing_artifacts if str(item).strip()]
    merged_artifacts.extend(
        str(item).strip()
        for item in new_artifacts
        if str(item).strip()
    )
    request_record["artifacts"] = merged_artifacts
    return request_record


def apply_request_resume_context(
    request_record: dict[str, Any],
    *,
    next_role: str,
    summary: str,
    routing_context: dict[str, Any],
    artifact_path: str = "",
    verified_by_request_id: str = "",
    followup_message_id: str = "",
    followup_body: str = "",
) -> dict[str, Any]:
    normalized_next_role = str(next_role or "").strip()
    updated_artifacts = [
        str(item).strip()
        for item in request_record.get("artifacts") or []
        if str(item).strip()
    ]
    normalized_artifact_path = str(artifact_path or "").strip()
    if normalized_artifact_path and normalized_artifact_path not in updated_artifacts:
        updated_artifacts.append(normalized_artifact_path)
    request_record["artifacts"] = updated_artifacts
    params = dict(request_record.get("params") or {})
    if normalized_artifact_path:
        params["verified_source_artifact"] = normalized_artifact_path
    if str(verified_by_request_id or "").strip():
        params["verified_source_request_id"] = str(verified_by_request_id).strip()
    if str(followup_message_id or "").strip():
        params["resume_followup_message_id"] = str(followup_message_id).strip()
    if str(followup_body or "").strip():
        params["resume_followup_body"] = str(followup_body).strip()
    request_record["params"] = params
    request_record["status"] = "delegated"
    request_record["current_role"] = normalized_next_role
    request_record["next_role"] = normalized_next_role
    request_record["routing_context"] = dict(routing_context or {})
    append_request_event(
        request_record,
        event_type="resumed",
        actor="orchestrator",
        summary=str(summary or "").strip(),
        payload={
            "next_role": normalized_next_role,
            "routing_context": dict(request_record.get("routing_context") or {}),
            "verified_source_artifact": normalized_artifact_path,
            "verified_source_request_id": str(verified_by_request_id or "").strip(),
            "message_id": str(followup_message_id or "").strip(),
        },
    )
    return request_record


@dataclass(frozen=True, slots=True)
class ResolvedRequesterRoute:
    route: dict[str, Any]
    source: str
    recovered_reply_route: dict[str, Any] | None = None


def resolve_request_reply_route(
    persisted_route: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> ResolvedRequesterRoute:
    normalized_persisted_route = dict(persisted_route or {}) if isinstance(persisted_route, dict) else {}
    normalized_params = dict(params or {}) if isinstance(params, dict) else {}
    original_requester = extract_original_requester(normalized_params)
    merged_route = merge_requester_route(normalized_persisted_route, original_requester)
    source = "reply_route"
    recovered_reply_route: dict[str, Any] | None = None
    if str(merged_route.get("channel_id") or "").strip() and not str(normalized_persisted_route.get("channel_id") or "").strip():
        source = "original_requester"
        recovered_reply_route = {
            **normalized_persisted_route,
            **merged_route,
        }
    return ResolvedRequesterRoute(
        route=merged_route,
        source=source,
        recovered_reply_route=recovered_reply_route,
    )


def _dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for item in values:
        candidate = str(item).strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _first_meaningful_text(*values: Any) -> str:
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        normalized = str(value).strip()
        return [normalized] if normalized else []
    return []


def combine_envelope_scope_and_body(envelope: MessageEnvelope) -> str:
    parts: list[str] = []
    for raw_part in (str(envelope.scope or ""), str(envelope.body or "")):
        part = raw_part.strip()
        if part and part not in parts:
            parts.append(part)
    return "\n".join(parts).strip()


def normalize_kickoff_requirements(value: Any) -> list[str]:
    return _dedupe_preserving_order(_normalize_string_list(value))


def clean_kickoff_text(value: Any) -> str:
    return "\n".join(line.rstrip() for line in str(value or "").splitlines()).strip()


def parse_kickoff_text_sections(text: str) -> tuple[str, list[str]]:
    brief_lines: list[str] = []
    requirements: list[str] = []
    in_requirements = False
    for raw_line in str(text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            if in_requirements and requirements:
                in_requirements = False
            continue
        if SPRINT_COMMAND_LINE_PATTERN.match(stripped):
            continue
        if SPRINT_MILESTONE_PATTERN.match(stripped):
            continue
        requirement_header = SPRINT_KICKOFF_REQUIREMENTS_PATTERN.match(stripped)
        if requirement_header:
            inline_value = str(requirement_header.group("value") or "").strip()
            if inline_value:
                bullet_match = SPRINT_BULLET_LINE_PATTERN.match(inline_value)
                requirements.append(
                    str(bullet_match.group("value") if bullet_match else inline_value).strip()
                )
            in_requirements = True
            continue
        brief_header = SPRINT_KICKOFF_BRIEF_PATTERN.match(stripped)
        if brief_header:
            inline_value = str(brief_header.group("value") or "").strip()
            if inline_value:
                brief_lines.append(inline_value)
            in_requirements = False
            continue
        bullet_match = SPRINT_BULLET_LINE_PATTERN.match(stripped)
        if bullet_match:
            requirements.append(str(bullet_match.group("value") or "").strip())
            continue
        if in_requirements:
            requirements.append(stripped)
            continue
        brief_lines.append(line.strip())
    brief = "\n".join(value for value in brief_lines if value).strip()
    return brief, _dedupe_preserving_order([item for item in requirements if item])


def extract_manual_sprint_milestone_title(envelope: MessageEnvelope) -> str:
    params = dict(envelope.params or {})
    explicit = str(params.get("milestone_title") or "").strip()
    if explicit:
        return explicit
    combined = combine_envelope_scope_and_body(envelope)
    if not combined:
        return ""
    matched = SPRINT_MILESTONE_PATTERN.search(combined)
    if matched:
        matched_value = str(matched.group("value") or "").strip()
        return "" if matched_value.lower() in {"", "today", "now"} else matched_value
    if not SPRINT_CONTROL_START_PATTERN.search(combined):
        return ""
    candidate_lines: list[str] = []
    in_requirements = False
    for raw_line in combined.splitlines():
        stripped = raw_line.strip()
        if not stripped or SPRINT_COMMAND_LINE_PATTERN.match(stripped):
            continue
        requirement_header = SPRINT_KICKOFF_REQUIREMENTS_PATTERN.match(stripped)
        if requirement_header:
            in_requirements = True
            continue
        if SPRINT_KICKOFF_BRIEF_PATTERN.match(stripped):
            in_requirements = False
            continue
        if in_requirements or SPRINT_BULLET_LINE_PATTERN.match(stripped):
            continue
        candidate_lines.append(stripped)
    remaining = candidate_lines[0] if candidate_lines else SPRINT_CONTROL_START_PATTERN.sub(" ", combined, count=1)
    remaining = re.sub(r"(?is)\b(for|with|using|about)\b", " ", remaining)
    remaining = re.sub(r"(?is)^(milestone|마일스톤)\b", " ", remaining).strip(" :-\n\t")
    normalized = " ".join(str(remaining).split())
    return "" if normalized.lower() in {"", "today", "now"} else normalized


def extract_manual_sprint_kickoff_payload(envelope: MessageEnvelope) -> dict[str, Any]:
    params = dict(envelope.params or {})
    milestone_title = extract_manual_sprint_milestone_title(envelope)
    kickoff_request_text = clean_kickoff_text(
        params.get("kickoff_request_text") or combine_envelope_scope_and_body(envelope)
    )
    kickoff_brief = clean_kickoff_text(params.get("kickoff_brief") or "")
    kickoff_requirements = normalize_kickoff_requirements(params.get("kickoff_requirements"))
    if not kickoff_brief and not kickoff_requirements and kickoff_request_text:
        parsed_brief, parsed_requirements = parse_kickoff_text_sections(kickoff_request_text)
        kickoff_brief = parsed_brief
        kickoff_requirements = parsed_requirements
    return {
        "milestone_title": milestone_title,
        "kickoff_brief": kickoff_brief,
        "kickoff_requirements": kickoff_requirements,
        "kickoff_request_text": kickoff_request_text,
        "kickoff_source_request_id": str(params.get("kickoff_source_request_id") or envelope.request_id or "").strip(),
        "kickoff_reference_artifacts": _dedupe_preserving_order(
            [
                str(item).strip()
                for item in (params.get("kickoff_reference_artifacts") or envelope.artifacts or [])
                if str(item).strip()
            ]
        ),
    }


def is_manual_sprint_start_request(envelope: MessageEnvelope) -> bool:
    combined = combine_envelope_scope_and_body(envelope)
    if str(dict(envelope.params or {}).get("sprint_control") or "").strip().lower() == "start":
        return True
    return is_manual_sprint_start_text(combined)


def is_manual_sprint_finalize_request(envelope: MessageEnvelope) -> bool:
    combined = combine_envelope_scope_and_body(envelope)
    if str(dict(envelope.params or {}).get("sprint_control") or "").strip().lower() == "finalize":
        return True
    return is_manual_sprint_finalize_text(combined)


def build_created_request_record(
    message: DiscordMessage,
    envelope: MessageEnvelope,
    *,
    forwarded: bool,
    request_id: str,
    sprint_id: str,
    source_message_created_at: str,
    created_at: str,
    updated_at: str,
) -> RequestRecord:
    seed = build_request_record_seed(
        message,
        envelope,
        forwarded=forwarded,
        valid_user_requested_roles=TEAM_ROLES,
    )
    record = build_request_record(
        seed,
        envelope=envelope,
        request_id=request_id,
        sprint_id=sprint_id,
        source_message_created_at=source_message_created_at,
        created_at=created_at,
        updated_at=updated_at,
    )
    append_request_event(
        record,
        event_type="created",
        actor="orchestrator",
        summary="요청을 접수했습니다.",
        payload={"forwarded": forwarded},
    )
    return record


def mark_user_request_delegated_to_orchestrator(
    request_record: RequestRecord,
    *,
    routing_context: dict[str, Any],
) -> RequestRecord:
    request_record["status"] = "delegated"
    request_record["current_role"] = "orchestrator"
    request_record["next_role"] = "orchestrator"
    request_record["routing_context"] = dict(routing_context or {})
    append_request_event(
        request_record,
        event_type="delegated",
        actor="orchestrator",
        summary="사용자 작업 요청을 orchestrator agent로 전달했습니다.",
        payload={"routing_context": dict(request_record.get("routing_context") or {})},
    )
    return request_record


@dataclass(frozen=True, slots=True)
class BlockedDuplicateFollowup:
    existing_artifacts: list[str]
    new_artifacts: list[str]
    followup_body: str
    has_new_body: bool


def analyze_blocked_duplicate_followup(
    duplicate_request: dict[str, Any],
    envelope: MessageEnvelope,
) -> BlockedDuplicateFollowup:
    existing_artifacts = [
        str(item).strip()
        for item in (duplicate_request.get("artifacts") or [])
        if str(item).strip()
    ]
    new_artifacts = [
        str(item).strip()
        for item in envelope.artifacts
        if str(item).strip() and str(item).strip() not in existing_artifacts
    ]
    followup_body = str(envelope.body or "").strip()
    has_new_body = bool(followup_body and followup_body != str(duplicate_request.get("body") or "").strip())
    return BlockedDuplicateFollowup(
        existing_artifacts=existing_artifacts,
        new_artifacts=new_artifacts,
        followup_body=followup_body,
        has_new_body=has_new_body,
    )


def retry_blocked_duplicate_request(
    duplicate_request: dict[str, Any],
    *,
    message: DiscordMessage,
    envelope: MessageEnvelope,
    forwarded: bool,
    routing_context: dict[str, Any],
) -> dict[str, Any]:
    followup = analyze_blocked_duplicate_followup(duplicate_request, envelope)
    return apply_blocked_duplicate_retry(
        duplicate_request,
        requester_route=build_requester_route(message, envelope, forwarded=forwarded),
        scope=str(envelope.scope or ""),
        followup_body=followup.followup_body,
        existing_artifacts=followup.existing_artifacts,
        routing_context=dict(routing_context or {}),
        message_id=message.message_id,
    )


def augment_blocked_duplicate_request(
    duplicate_request: dict[str, Any],
    *,
    envelope: MessageEnvelope,
) -> tuple[dict[str, Any], BlockedDuplicateFollowup]:
    followup = analyze_blocked_duplicate_followup(duplicate_request, envelope)
    updated = apply_blocked_duplicate_augmentation(
        duplicate_request,
        followup_body=followup.followup_body,
        existing_artifacts=followup.existing_artifacts,
        new_artifacts=followup.new_artifacts,
    )
    return updated, followup


def build_resume_routing_context_kwargs(
    selection: dict[str, Any],
    *,
    selected_role: str,
    summary: str,
) -> dict[str, Any]:
    return {
        "reason": summary or f"Selected {selected_role} while resuming the request with additional context.",
        "requested_role": str(selection.get("requested_role") or ""),
        "selection_source": "planning_resume",
        "matched_signals": [
            str(item).strip()
            for item in (selection.get("matched_signals") or [])
            if str(item).strip()
        ],
        "override_reason": str(selection.get("override_reason") or ""),
        "matched_strongest_domains": [
            str(item).strip()
            for item in (selection.get("matched_strongest_domains") or [])
            if str(item).strip()
        ],
        "matched_preferred_skills": [
            str(item).strip()
            for item in (selection.get("matched_preferred_skills") or [])
            if str(item).strip()
        ],
        "matched_behavior_traits": [
            str(item).strip()
            for item in (selection.get("matched_behavior_traits") or [])
            if str(item).strip()
        ],
        "policy_source": str(selection.get("policy_source") or ""),
        "routing_phase": str(selection.get("routing_phase") or ""),
        "request_state_class": str(selection.get("request_state_class") or ""),
        "score_total": int(selection.get("score_total") or 0),
        "score_breakdown": dict(selection.get("score_breakdown") or {}),
        "candidate_summary": list(selection.get("candidate_summary") or []),
    }


def apply_resume_request_update(
    request_record: dict[str, Any],
    *,
    next_role: str,
    summary: str,
    routing_context: dict[str, Any],
    artifact_path: str = "",
    verified_by_request_id: str = "",
    followup_message_id: str = "",
    followup_body: str = "",
) -> dict[str, Any]:
    return apply_request_resume_context(
        request_record,
        next_role=next_role,
        summary=summary,
        routing_context=dict(routing_context or {}),
        artifact_path=artifact_path,
        verified_by_request_id=verified_by_request_id,
        followup_message_id=followup_message_id,
        followup_body=followup_body,
    )


def build_duplicate_reopen_reply_payload(
    request_record: dict[str, Any],
    *,
    relay_sent: bool,
    reopen_mode: str,
) -> dict[str, str]:
    request_id = str(request_record.get("request_id") or "")
    if str(reopen_mode or "").strip() == "retried":
        return {
            "mode": "status",
            "status": "delegated",
            "request_id": request_id,
            "summary": "기존 blocked 요청을 다시 시도합니다.",
        }
    return {
        "mode": "text",
        "content": (
            "기존 blocked 요청을 보강된 입력으로 재개했습니다. "
            f"request_id={request_id}"
            if relay_sent
            else (
                "기존 blocked 요청은 재개했지만 planner relay 전송이 실패했습니다. "
                f"request_id={request_id}"
            )
        ),
    }


def build_reused_duplicate_requester_message(request_record: dict[str, Any]) -> str:
    return (
        "기존 요청을 재사용합니다. "
        f"request_id={request_record['request_id']}\n"
        f"status={request_record.get('status') or 'unknown'}\n"
        f"current_role={request_record.get('current_role') or 'orchestrator'}"
    )


def normalize_reference_text(value: str) -> str:
    normalized = str(value or "").strip().lower()
    for token in ("_", "-", "/", "\\", ".", "(", ")", "[", "]", "{", "}", ":", ","):
        normalized = normalized.replace(token, " ")
    return " ".join(normalized.split())


def verification_result_payload(result: dict[str, Any]) -> dict[str, Any]:
    proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
    verification = proposals.get("verification_result")
    return dict(verification or {}) if isinstance(verification, dict) else {}


def extract_ready_planning_artifact(result: dict[str, Any]) -> str:
    verification = verification_result_payload(result)
    if not verification or not bool(verification.get("ready_for_planning")):
        return ""
    location = str(verification.get("location") or "").strip()
    if location:
        return location
    for item in result.get("artifacts") or []:
        normalized = str(item).strip()
        if normalized:
            return normalized
    return ""


def extract_verification_related_request_ids(result: dict[str, Any]) -> list[str]:
    verification = verification_result_payload(result)
    raw_ids = verification.get("related_request_ids")
    if not isinstance(raw_ids, list):
        return []
    return [str(item).strip() for item in raw_ids if str(item).strip()]


def is_blocked_planning_request_waiting_for_document(request_record: dict[str, Any]) -> bool:
    if str(request_record.get("status") or "").strip().lower() != "blocked":
        return False
    result = dict(request_record.get("result") or {}) if isinstance(request_record.get("result"), dict) else {}
    proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
    blocked_reason = proposals.get("blocked_reason")
    blocked_reason_dict = dict(blocked_reason or {}) if isinstance(blocked_reason, dict) else {}
    combined = "\n".join(
        [
            str(request_record.get("scope") or ""),
            str(request_record.get("body") or ""),
            str(result.get("summary") or ""),
            str(result.get("error") or ""),
            str(blocked_reason_dict.get("reason") or ""),
            str(blocked_reason_dict.get("required_next_step") or ""),
        ]
    )
    combined_lower = combined.lower()
    if "source planning document not yet confirmed" in combined_lower:
        return True
    return (
        ("planning document" in combined_lower or "source of truth" in combined_lower or "기획 문서" in combined)
        and ("confirm" in combined_lower or "확정" in combined or "생성" in combined)
    )


def request_mentions_artifact(request_record: dict[str, Any], artifact_path: str) -> bool:
    normalized_artifact = str(artifact_path or "").strip()
    if not normalized_artifact:
        return False
    existing_artifacts = [str(item).strip() for item in request_record.get("artifacts") or [] if str(item).strip()]
    if normalized_artifact in existing_artifacts:
        return True
    alias_candidates = {
        normalized_artifact,
        Path(normalized_artifact).name,
        Path(normalized_artifact).stem,
        Path(normalized_artifact).stem.replace("_", " "),
    }
    combined = "\n".join(
        [
            str(request_record.get("scope") or ""),
            str(request_record.get("body") or ""),
            str(dict(request_record.get("result") or {}).get("summary") or ""),
            str(dict(dict(request_record.get("result") or {}).get("proposals") or {}).get("blocked_reason") or ""),
            " ".join(existing_artifacts),
        ]
    )
    normalized_combined = normalize_reference_text(combined)
    for candidate in alias_candidates:
        normalized_candidate = normalize_reference_text(candidate)
        if normalized_candidate and normalized_candidate in normalized_combined:
            return True
    return False


def find_recent_ready_planning_verification(
    requests: list[dict[str, Any]],
    *,
    author_id: str,
    channel_id: str,
    now: datetime,
    recency_seconds: float,
    parse_datetime,
) -> tuple[dict[str, Any], str]:
    for request_record in requests:
        if str(request_record.get("status") or "").strip().lower() != "completed":
            continue
        if not request_identity_matches(request_record, author_id=author_id, channel_id=channel_id):
            continue
        artifact_path = extract_ready_planning_artifact(
            dict(request_record.get("result") or {}) if isinstance(request_record.get("result"), dict) else {}
        )
        if not artifact_path:
            continue
        updated_at = parse_datetime(str(request_record.get("updated_at") or request_record.get("created_at") or ""))
        if updated_at is not None and abs((now - updated_at).total_seconds()) > recency_seconds:
            continue
        return request_record, artifact_path
    return {}, ""


def find_blocked_requests_for_verified_artifact(
    verification_request: dict[str, Any],
    result: dict[str, Any],
    *,
    author_id: str,
    channel_id: str,
    load_request,
    candidate_requests: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    artifact_path = extract_ready_planning_artifact(result)
    if not artifact_path:
        return [], ""
    related_request_ids = extract_verification_related_request_ids(result)
    matched_records: list[dict[str, Any]] = []
    if related_request_ids:
        for request_id in related_request_ids:
            request_record = load_request(request_id)
            if not request_record or not is_blocked_planning_request_waiting_for_document(request_record):
                continue
            matched_records.append(request_record)
        return matched_records, artifact_path
    inferred_matches: list[dict[str, Any]] = []
    for request_record in candidate_requests:
        if str(request_record.get("request_id") or "") == str(verification_request.get("request_id") or ""):
            continue
        if not request_identity_matches(request_record, author_id=author_id, channel_id=channel_id):
            continue
        if not is_blocked_planning_request_waiting_for_document(request_record):
            continue
        if not request_mentions_artifact(request_record, artifact_path):
            continue
        inferred_matches.append(request_record)
    if len(inferred_matches) == 1:
        return inferred_matches, artifact_path
    return [], artifact_path
