"""Relay transport, delivery, and internal queue helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import textwrap
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from teams_runtime.workflows.orchestration.ingress import envelope_to_text
from teams_runtime.adapters.discord.client import DiscordMessage, DiscordSendError
from teams_runtime.shared.formatting import ReportSection, render_report_sections
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import read_json, utc_now_iso
from teams_runtime.workflows.state.request_store import append_request_event
from teams_runtime.shared.models import MessageEnvelope, RequestRecord, TEAM_ROLES


INTERNAL_RELAY_TRANSPORT = "internal"


@dataclass(frozen=True, slots=True)
class InternalRelayEnvelopeFile:
    relay_id: str
    relay_file: Path
    envelope: MessageEnvelope


def _collapse_whitespace(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _truncate_text(value: Any, *, limit: int = 240) -> str:
    normalized = _collapse_whitespace(value)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def internal_relay_root(paths: RuntimePaths) -> Path:
    return paths.runtime_root / "internal_relay"


def internal_relay_inbox_dir(paths: RuntimePaths, role: str) -> Path:
    normalized_role = str(role or "").strip()
    return internal_relay_root(paths) / "inbox" / normalized_role


def internal_relay_archive_dir(paths: RuntimePaths, role: str) -> Path:
    normalized_role = str(role or "").strip()
    return internal_relay_root(paths) / "archive" / normalized_role


def is_internal_relay_summary_content(content: str, *, marker: str) -> bool:
    first_line = str(content or "").splitlines()[0].strip() if str(content or "").splitlines() else ""
    return first_line.startswith(str(marker or "").strip())


def build_internal_relay_record_id(envelope: MessageEnvelope) -> str:
    seed = "|".join(
        [
            utc_now_iso(),
            str(time.time_ns()),
            str(os.getpid()),
            str(envelope.request_id or ""),
            str(envelope.sender or ""),
            str(envelope.target or ""),
            str(envelope.intent or ""),
        ]
    )
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def enqueue_internal_relay(
    paths: RuntimePaths,
    *,
    sender_role: str,
    envelope: MessageEnvelope,
    transport: str = INTERNAL_RELAY_TRANSPORT,
) -> str:
    target_role = str(envelope.target or "").strip()
    if target_role not in TEAM_ROLES:
        raise ValueError(f"Unsupported internal relay target role: {target_role or 'unknown'}")
    relay_id = build_internal_relay_record_id(envelope)
    inbox_dir = internal_relay_inbox_dir(paths, target_role)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "relay_id": relay_id,
        "transport": str(transport or INTERNAL_RELAY_TRANSPORT),
        "created_at": utc_now_iso(),
        "sender_role": str(sender_role or "").strip(),
        "target_role": target_role,
        "kind": str(envelope.params.get("_teams_kind") or "").strip(),
        "envelope": envelope.to_dict(include_routing=True),
    }
    temp_path = inbox_dir / f".{relay_id}.tmp"
    relay_path = inbox_dir / f"{relay_id}.json"
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(relay_path)
    return relay_id


def archive_internal_relay_file(
    paths: RuntimePaths,
    *,
    role: str,
    relay_file: Path,
    invalid: bool = False,
) -> None:
    archive_dir = internal_relay_archive_dir(paths, role)
    archive_dir.mkdir(parents=True, exist_ok=True)
    stem = relay_file.stem + ("-invalid" if invalid else "")
    destination = archive_dir / f"{stem}{relay_file.suffix or '.json'}"
    suffix = 1
    while destination.exists():
        destination = archive_dir / f"{stem}-{suffix}{relay_file.suffix or '.json'}"
        suffix += 1
    relay_file.replace(destination)


def pending_internal_relay_files(paths: RuntimePaths, role: str) -> list[Path]:
    inbox_dir = internal_relay_inbox_dir(paths, role)
    if not inbox_dir.exists():
        return []
    return sorted(inbox_dir.glob("*.json"))


def deserialize_internal_relay_envelope(payload: Any) -> MessageEnvelope | None:
    if not isinstance(payload, dict):
        return None
    sender = str(payload.get("from") or payload.get("sender") or "").strip()
    target = str(payload.get("to") or payload.get("target") or "").strip()
    if not sender or not target:
        return None
    artifacts = [
        str(item).strip()
        for item in (payload.get("artifacts") or [])
        if str(item).strip()
    ]
    params = dict(payload.get("params") or {}) if isinstance(payload.get("params"), dict) else {}
    return MessageEnvelope(
        request_id=str(payload.get("request_id") or "").strip() or None,
        sender=sender,
        target=target,
        intent=str(payload.get("intent") or "").strip(),
        urgency=str(payload.get("urgency") or "normal").strip() or "normal",
        scope=str(payload.get("scope") or "").strip(),
        artifacts=artifacts,
        params=params,
        body=str(payload.get("body") or ""),
    )


def load_internal_relay_envelope_file(relay_file: Path) -> InternalRelayEnvelopeFile | None:
    payload = read_json(relay_file)
    if not payload:
        return None
    envelope = deserialize_internal_relay_envelope(payload.get("envelope"))
    if envelope is None:
        return None
    return InternalRelayEnvelopeFile(
        relay_id=str(payload.get("relay_id") or relay_file.stem),
        relay_file=relay_file,
        envelope=envelope,
    )


def build_internal_relay_message_stub(
    envelope: MessageEnvelope,
    *,
    current_role: str,
    relay_channel_id: str,
    original_requester: dict[str, Any] | None = None,
    sender_bot_id: str = "",
    relay_id: str = "",
) -> DiscordMessage:
    requester = dict(original_requester or {})
    sender_role = str(envelope.sender or "").strip()
    is_dm = bool(requester.get("is_dm")) if "is_dm" in requester else False
    guild_id = str(requester.get("guild_id") or "").strip()
    return DiscordMessage(
        message_id=relay_id or f"internal-{current_role}-{int(time.time() * 1000)}",
        channel_id=str(requester.get("channel_id") or relay_channel_id),
        guild_id=None if is_dm else (guild_id or "internal-relay"),
        author_id=str(requester.get("author_id") or sender_bot_id or "internal-relay"),
        author_name=str(requester.get("author_name") or sender_role or "internal-relay"),
        content=envelope_to_text(envelope),
        is_dm=is_dm,
        mentions_bot=False,
        created_at=datetime.now(UTC),
    )


def resolve_internal_relay_action(
    *,
    current_role: str,
    kind: str,
    envelope_target: str,
) -> str:
    normalized_role = str(current_role or "").strip()
    normalized_kind = str(kind or "").strip()
    normalized_target = str(envelope_target or "").strip()
    if normalized_role == "orchestrator":
        if normalized_kind == "report":
            return "report"
        if normalized_kind == "forward":
            return "forward"
        return "ignore_unsupported"
    if normalized_kind != "delegate":
        return "ignore_missing_delegate"
    if normalized_target != normalized_role:
        return "ignore_target_mismatch"
    return "delegate"


def apply_relay_delivery_status(
    request_record: dict[str, Any],
    *,
    status: str,
    target_description: str,
    attempts: int,
    error: str,
    updated_at: str,
) -> None:
    request_record["relay_send_status"] = str(status or "").strip()
    request_record["relay_send_target"] = str(target_description or "").strip()
    request_record["relay_send_attempts"] = int(attempts or 0)
    request_record["relay_send_error"] = str(error or "").strip()
    request_record["relay_send_updated_at"] = str(updated_at or "").strip()


def relay_delivery_failure_summary(target_description: str) -> str:
    return f"relay 채널 전송이 실패했습니다. target={str(target_description or '').strip()}"


def build_relay_delivery_failure_payload(
    *,
    target_description: str,
    attempts: int,
    error: str,
    envelope_target: str,
    intent: str,
    scope: str,
) -> dict[str, Any]:
    return {
        "target": str(target_description or "").strip(),
        "attempts": int(attempts or 0),
        "error": str(error or "").strip(),
        "envelope_target": str(envelope_target or "").strip(),
        "intent": str(intent or "").strip(),
        "scope": _truncate_text(str(scope or "").strip(), limit=120),
    }


def relay_summary_text_fragments(
    value: Any,
    *,
    width: int = 120,
    max_lines: int = 8,
) -> list[str]:
    raw_lines = [_collapse_whitespace(line) for line in str(value or "").splitlines()]
    normalized_lines = [line for line in raw_lines if line]
    if not normalized_lines:
        return []
    fragments: list[str] = []
    for line in normalized_lines:
        wrapped = textwrap.wrap(
            line,
            width=max(32, width),
            break_long_words=False,
            break_on_hyphens=False,
        )
        fragments.extend(wrapped or [line])
    if len(fragments) <= max_lines:
        return fragments
    return fragments[:max_lines] + [f"... 외 {len(fragments) - max_lines}줄"]


def append_report_section(
    sections: list[ReportSection],
    title: str,
    lines: Iterable[str] | None,
) -> None:
    normalized_lines: list[str] = []
    for item in lines or []:
        text = str(item or "").strip()
        if not text:
            continue
        normalized_lines.append(text if text.startswith("- ") else f"- {text}")
    if normalized_lines:
        sections.append(ReportSection(title=title, lines=tuple(normalized_lines)))


def relay_report_sections_from_lines(
    lines: Iterable[str] | None,
    *,
    default_title: str = "핵심 전달",
) -> list[ReportSection]:
    prefix_to_title = (
        ("- Why now:", "이관 이유"),
        ("- What:", "핵심 전달"),
        ("- Check now:", "지금 볼 것"),
        ("- Constraints:", "유의사항"),
        ("- Refs:", "참고 파일"),
        ("- Context:", "추가 맥락"),
        ("- 오류:", "오류"),
        ("- 상태:", "상태"),
    )
    sections: list[ReportSection] = []
    current_title = ""
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_lines
        if current_title and current_lines:
            append_report_section(sections, current_title, current_lines)
        current_title = ""
        current_lines = []

    for item in lines or []:
        stripped = str(item or "").strip()
        if not stripped:
            continue
        matched = False
        for prefix, title in prefix_to_title:
            if stripped.startswith(prefix):
                flush()
                current_title = title
                remainder = stripped[len(prefix) :].strip()
                if remainder:
                    current_lines.append(f"- {remainder}")
                matched = True
                break
        if matched:
            continue
        if not current_title:
            current_title = default_title
        current_lines.append(stripped if stripped.startswith("- ") else f"- {stripped}")

    flush()
    return sections


def render_report_sections_message(
    header: str,
    sections: Iterable[ReportSection] | None,
    *,
    max_inner_width: int = 96,
) -> str:
    rendered_sections = render_report_sections(sections, max_inner_width=max_inner_width)
    parts = [str(header or "").strip(), str(rendered_sections or "").strip()]
    return "\n".join(part for part in parts if part).strip()


def build_internal_relay_summary_message(
    envelope: MessageEnvelope,
    *,
    marker: str,
    summary_lines: Iterable[str] | None,
) -> str:
    kind = str(envelope.params.get("_teams_kind") or "").strip() or "unknown"
    sender = str(envelope.sender or "").strip() or "unknown"
    target = str(envelope.target or "").strip() or "unknown"
    header = f"{marker} {sender} -> {target} ({kind})"
    sections = [
        ReportSection(
            title="전달 정보",
            lines=(
                f"- 요청 ID: {envelope.request_id or 'N/A'}",
                f"- 보낸 역할: {sender}",
                f"- 받는 역할: {target}",
                f"- relay 종류: {kind}",
            ),
        ),
        *relay_report_sections_from_lines(summary_lines, default_title="핵심 전달"),
    ]
    return render_report_sections_message(header, sections)


def record_relay_delivery(
    request_record: dict[str, Any],
    *,
    status: str,
    target_description: str,
    attempts: int,
    error: str,
    envelope: MessageEnvelope,
    updated_at: str,
) -> str:
    apply_relay_delivery_status(
        request_record,
        status=status,
        target_description=target_description,
        attempts=attempts,
        error=error,
        updated_at=updated_at,
    )
    if status != "failed":
        return ""
    summary = relay_delivery_failure_summary(target_description)
    append_request_event(
        request_record,
        event_type="relay_send_failed",
        actor="orchestrator",
        summary=summary,
        payload=build_relay_delivery_failure_payload(
            target_description=target_description,
            attempts=attempts,
            error=error,
            envelope_target=str(envelope.target or "").strip(),
            intent=str(envelope.intent or "").strip(),
            scope=str(envelope.scope or "").strip(),
        ),
    )
    return summary


async def process_internal_relay_envelope(
    envelope: MessageEnvelope,
    *,
    current_role: str,
    relay_id: str = "",
    build_internal_relay_message_stub: Callable[..., Any],
    handle_role_report: Callable[[Any, MessageEnvelope], Awaitable[None]],
    handle_user_request: Callable[..., Awaitable[None]],
    handle_delegated_request: Callable[..., Awaitable[None]],
    log_malformed_trusted_relay: Callable[..., None],
) -> None:
    kind = str(envelope.params.get("_teams_kind") or "").strip()
    synthetic_message = build_internal_relay_message_stub(envelope, relay_id=relay_id)
    action = resolve_internal_relay_action(
        current_role=current_role,
        kind=kind,
        envelope_target=str(envelope.target or "").strip(),
    )
    if action == "report":
        await handle_role_report(synthetic_message, envelope)
        return
    if action == "forward":
        await handle_user_request(synthetic_message, envelope, forwarded=True)
        return
    if action == "ignore_unsupported":
        log_malformed_trusted_relay(
            reason="unsupported internal relay kind for orchestrator",
            kind=kind,
        )
        return
    if action == "ignore_missing_delegate":
        log_malformed_trusted_relay(
            reason="missing internal delegate kind",
            kind=kind,
        )
        return
    if action == "ignore_target_mismatch":
        return
    await handle_delegated_request(synthetic_message, envelope)


async def consume_internal_relay_once(
    *,
    paths,
    role: str,
    archive_internal_relay_file: Callable[[Path], None] | Callable[..., None],
    process_internal_relay_envelope: Callable[..., Awaitable[None]],
    log_exception: Callable[..., None],
) -> None:
    for relay_file in pending_internal_relay_files(paths, role):
        record = load_internal_relay_envelope_file(relay_file)
        if record is None:
            archive_internal_relay_file(relay_file, invalid=True)
            continue
        try:
            await process_internal_relay_envelope(
                record.envelope,
                relay_id=record.relay_id,
            )
        except Exception:
            log_exception(
                "Failed to process internal relay envelope for role %s (file=%s)",
                role,
                relay_file,
            )
            return
        archive_internal_relay_file(record.relay_file)


async def consume_internal_relay_loop(
    *,
    role: str,
    consume_internal_relay_once: Callable[[], Awaitable[None]],
    poll_seconds: float,
    log_exception: Callable[..., None],
) -> None:
    while True:
        try:
            await consume_internal_relay_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            log_exception("Internal relay consumer loop failed for role %s", role)
        await asyncio.sleep(poll_seconds)


async def send_relay_transport(
    envelope: MessageEnvelope,
    *,
    request_record: RequestRecord | None,
    use_internal_relay: bool,
    current_role: str,
    relay_channel_id: str,
    target_bot_id: str,
    enqueue_internal_relay: Callable[[MessageEnvelope], str],
    send_internal_relay_summary: Callable[[MessageEnvelope], Awaitable[None]],
    send_discord_relay_envelope: Callable[..., Awaitable[None]],
    record_relay_delivery: Callable[..., None],
    log_warning: Callable[..., None],
) -> bool:
    if use_internal_relay:
        target_description = f"internal:{envelope.target}"
        try:
            enqueue_internal_relay(envelope)
        except Exception as exc:
            log_warning(
                "Internal relay enqueue failed for request %s to %s: %s",
                envelope.request_id or "unknown",
                target_description,
                exc,
            )
            if request_record is not None:
                record_relay_delivery(
                    request_record,
                    status="failed",
                    target_description=target_description,
                    attempts=1,
                    error=str(exc),
                    envelope=envelope,
                )
            return False
        if request_record is not None:
            record_relay_delivery(
                request_record,
                status="sent",
                target_description=target_description,
                attempts=1,
                error="",
                envelope=envelope,
            )
        await send_internal_relay_summary(envelope)
        return True

    target_description = f"relay:{relay_channel_id}"
    try:
        await send_discord_relay_envelope(
            relay_channel_id=relay_channel_id,
            target_bot_id=target_bot_id,
            content=envelope_to_text(envelope),
        )
    except Exception as exc:
        send_error = exc if isinstance(exc, DiscordSendError) else DiscordSendError(str(exc))
        log_warning(
            "Relay send failed for request %s to %s after %s attempt(s): %s",
            envelope.request_id or "unknown",
            target_description,
            getattr(send_error, "attempts", 1),
            send_error,
        )
        if request_record is not None:
            record_relay_delivery(
                request_record,
                status="failed",
                target_description=target_description,
                attempts=getattr(send_error, "attempts", 1),
                error=str(send_error),
                envelope=envelope,
            )
        return False
    if request_record is not None:
        record_relay_delivery(
            request_record,
            status="sent",
            target_description=target_description,
            attempts=1,
            error="",
            envelope=envelope,
        )
    return True
