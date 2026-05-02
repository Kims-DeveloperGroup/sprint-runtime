"""Orchestration notification helpers plus compatibility re-exports."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable

from teams_runtime.workflows.orchestration.ingress import extract_original_requester, resolve_request_reply_route
from teams_runtime.workflows.state.request_store import iter_request_records, is_terminal_request
from teams_runtime.adapters.discord.client import (
    DiscordMessage,
    DiscordSendError,
    MESSAGE_END_MARKER,
    MESSAGE_START_MARKER,
    classify_discord_exception,
)
from teams_runtime.shared.formatting import box_text_message, build_progress_report
from teams_runtime.shared.models import DiscordAgentsConfig, RequestRecord, RoleResult, TEAM_ROLES, TeamRuntimeConfig
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import utc_now_iso


LOGGER = logging.getLogger(__name__)
PRIMARY_SHARED_FILES = {
    "research": "planning",
    "planner": "planning",
    "designer": "planning",
    "architect": "decision_log",
    "developer": "shared_history",
    "qa": "shared_history",
    "orchestrator": "shared_history",
}


def normalize_markdown_body(lines: list[str]) -> str:
    return "\n".join(str(line).rstrip() for line in lines).strip()


def normalize_insights(result: dict[str, Any]) -> list[str]:
    raw = result.get("insights")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        normalized = str(raw).strip()
        return [normalized] if normalized else []
    return []


def ensure_markdown_file(path: Path, header: str) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{header}\n", encoding="utf-8")


def append_markdown_entry(path: Path, header: str, title: str, lines: list[str]) -> None:
    ensure_markdown_file(path, header)
    existing = path.read_text(encoding="utf-8").rstrip()
    entry_body = normalize_markdown_body(lines)
    entry = f"## {title}"
    if entry_body:
        entry = f"{entry}\n{entry_body}"
    updated = f"{existing}\n\n{entry}\n" if existing else f"{header}\n\n{entry}\n"
    path.write_text(updated, encoding="utf-8")


def refresh_role_todos(paths: RuntimePaths) -> None:
    records = list(iter_request_records(paths))
    open_records = [record for record in records if not is_terminal_request(record)]
    for role in TEAM_ROLES:
        relevant = [
            record
            for record in open_records
            if role == "orchestrator" or str(record.get("current_role") or "").strip() == role
        ]
        relevant.sort(key=lambda record: str(record.get("updated_at") or ""), reverse=True)
        lines = [f"# {role.title()} Todo", "", "## Active Requests"]
        if relevant:
            lines.append("")
            for record in relevant:
                lines.append(
                    "- [{status}] {request_id} | urgency={urgency} | current_role={current_role} | scope={scope}".format(
                        status=str(record.get("status") or "unknown"),
                        request_id=str(record.get("request_id") or ""),
                        urgency=str(record.get("urgency") or "normal"),
                        current_role=str(record.get("current_role") or "N/A"),
                        scope=str(record.get("scope") or "").strip() or "N/A",
                    )
                )
        else:
            lines.extend(["", "- active request 없음"])
        paths.role_todo_file(role).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def append_role_history(
    paths: RuntimePaths,
    role: str,
    request_record: dict[str, Any],
    *,
    event_type: str,
    summary: str,
    result: dict[str, Any] | None = None,
) -> None:
    insights = normalize_insights(result or {})
    lines = [
        f"- request_id: {request_record.get('request_id') or ''}",
        f"- event: {event_type}",
        f"- status: {request_record.get('status') or 'unknown'}",
        f"- scope: {request_record.get('scope') or ''}",
        f"- summary: {summary or ''}",
    ]
    if result:
        lines.extend(
            [
                f"- result_status: {result.get('status') or ''}",
                f"- artifacts: {', '.join(str(item) for item in result.get('artifacts') or []) or 'N/A'}",
                f"- insights: {' | '.join(insights) if insights else 'N/A'}",
            ]
        )
    append_markdown_entry(
        paths.role_history_file(role),
        f"# {role.title()} History",
        f"{utc_now_iso()} | {request_record.get('request_id') or 'unknown'}",
        lines,
    )


def append_role_journal(
    paths: RuntimePaths,
    role: str,
    request_record: dict[str, Any],
    *,
    title: str,
    lines: list[str],
) -> None:
    append_markdown_entry(
        paths.role_journal_file(role),
        f"# {role.title()} Journal",
        f"{utc_now_iso()} | {request_record.get('request_id') or 'unknown'} | {title}",
        lines,
    )


def append_shared_workspace_entry(
    paths: RuntimePaths,
    destination: str,
    *,
    request_record: dict[str, Any],
    title: str,
    lines: list[str],
) -> None:
    path_map = {
        "planning": (paths.shared_planning_file, "# Shared Planning"),
        "decision_log": (paths.shared_decision_log_file, "# Decision Log"),
        "shared_history": (paths.shared_history_file, "# Shared History"),
        "sync_contract": (paths.shared_sync_contract_file, "# Sync Contract"),
    }
    path, header = path_map[destination]
    append_markdown_entry(path, header, title, lines)


def record_shared_role_result(paths: RuntimePaths, request_record: RequestRecord, result: RoleResult) -> None:
    role = str(result.get("role") or "").strip()
    if not role:
        return
    base_lines = [
        f"- request_id: {request_record.get('request_id') or ''}",
        f"- role: {role}",
        f"- status: {result.get('status') or ''}",
        f"- scope: {request_record.get('scope') or ''}",
        f"- summary: {result.get('summary') or ''}",
        f"- artifacts: {', '.join(str(item) for item in result.get('artifacts') or []) or 'N/A'}",
    ]
    primary_destination = PRIMARY_SHARED_FILES.get(role)
    if primary_destination:
        append_shared_workspace_entry(
            paths,
            primary_destination,
            request_record=request_record,
            title=f"{utc_now_iso()} | {role} | {request_record.get('request_id') or 'unknown'}",
            lines=base_lines,
        )
    if primary_destination != "shared_history":
        append_shared_workspace_entry(
            paths,
            "shared_history",
            request_record=request_record,
            title=f"{utc_now_iso()} | {role} | {request_record.get('request_id') or 'unknown'}",
            lines=base_lines,
        )


def _split_discord_chunks(content: str, limit: int = 2000) -> list[str]:
    normalized = str(content or "").strip()
    if not normalized:
        return []
    if limit <= 0:
        return [normalized]

    def split_text_fragment(text: str, fragment_limit: int) -> list[str]:
        remaining = str(text or "").strip()
        if not remaining:
            return []
        pieces: list[str] = []
        while len(remaining) > fragment_limit:
            split_at = remaining.rfind("\n", 0, fragment_limit + 1)
            if split_at <= 0:
                split_at = remaining.rfind(" ", 0, fragment_limit + 1)
            if split_at <= 0:
                split_at = fragment_limit
            piece = remaining[:split_at].rstrip()
            if not piece:
                piece = remaining[:fragment_limit]
            pieces.append(piece)
            remaining = remaining[len(piece):].lstrip()
        if remaining:
            pieces.append(remaining)
        return pieces

    def split_code_block(block: str, fragment_limit: int) -> list[str]:
        lines = block.splitlines()
        stripped_block = block.strip()
        if len(lines) == 1 and stripped_block.startswith("```") and stripped_block.endswith("```"):
            first_fence = stripped_block.find("```")
            last_fence = stripped_block.rfind("```")
            if last_fence > first_fence:
                body = stripped_block[first_fence + 3 : last_fence].strip()
                if not body:
                    return [block]
                available = fragment_limit - len("```") * 2 - 2
                if available <= 0:
                    return split_text_fragment(block, fragment_limit)
                pieces = split_text_fragment(body, available)
                return [f"```\n{piece}\n```" for piece in pieces]
        if len(lines) < 2:
            return split_text_fragment(block, fragment_limit)
        opening_fence = lines[0]
        closing_fence = lines[-1]
        if not opening_fence.strip().startswith("```") or not closing_fence.strip().startswith("```"):
            return split_text_fragment(block, fragment_limit)
        available = fragment_limit - len(opening_fence) - len(closing_fence) - 2
        if available <= 0:
            return split_text_fragment(block, fragment_limit)
        body_lines = lines[1:-1]
        if not body_lines:
            return [block]
        pieces: list[str] = []
        current_lines: list[str] = []
        current_length = 0
        for line in body_lines:
            line_parts = [line[i : i + available] for i in range(0, len(line), available)] or [""]
            for part in line_parts:
                candidate_length = len(part) if not current_lines else current_length + 1 + len(part)
                if current_lines and len(opening_fence) + len(closing_fence) + candidate_length + 2 > fragment_limit:
                    pieces.append(f"{opening_fence}\n" + "\n".join(current_lines) + f"\n{closing_fence}")
                    current_lines = [part]
                    current_length = len(part)
                    continue
                current_lines.append(part)
                current_length = candidate_length
        if current_lines:
            pieces.append(f"{opening_fence}\n" + "\n".join(current_lines) + f"\n{closing_fence}")
        return pieces

    blocks: list[str] = []
    current_lines: list[str] = []
    in_code_block = False
    for line in normalized.splitlines():
        stripped = line.strip()
        is_fence = stripped.startswith("```")
        if in_code_block:
            current_lines.append(line)
            if is_fence:
                blocks.append("\n".join(current_lines).strip())
                current_lines = []
                in_code_block = False
            continue
        if is_fence:
            if current_lines:
                blocks.append("\n".join(current_lines).strip())
                current_lines = []
            if stripped.endswith("```") and stripped.count("```") >= 2:
                blocks.append(stripped)
                continue
            current_lines = [line]
            in_code_block = True
            continue
        if not stripped:
            if current_lines:
                blocks.append("\n".join(current_lines).strip())
                current_lines = []
            continue
        current_lines.append(line)
    if current_lines:
        blocks.append("\n".join(current_lines).strip())

    chunks: list[str] = []
    current_chunk = ""
    for block in blocks:
        block_pieces = (
            split_code_block(block, limit)
            if block.startswith("```") and block.endswith("```")
            else split_text_fragment(block, limit)
        )
        for piece in block_pieces:
            candidate = piece if not current_chunk else f"{current_chunk}\n\n{piece}"
            if len(candidate) <= limit:
                current_chunk = candidate
                continue
            if current_chunk:
                chunks.append(current_chunk)
            current_chunk = piece
    if current_chunk:
        chunks.append(current_chunk)
    return chunks


def _render_discord_message_chunks(
    content: str,
    *,
    limit: int = 2000,
    prefix: str = "",
    include_sequence_markers: bool = True,
) -> list[str]:
    normalized = str(content or "").strip()
    if not normalized:
        return []
    normalized = f"{MESSAGE_START_MARKER}\n{normalized}\n{MESSAGE_END_MARKER}"
    base_limit = limit - len(prefix)
    raw_chunks = _split_discord_chunks(normalized, max(1, base_limit))
    total = len(raw_chunks)
    if include_sequence_markers and total > 1:
        marker_width = len(f"[{total}/{total}]\n")
        raw_chunks = _split_discord_chunks(normalized, max(1, base_limit - marker_width))
        total = len(raw_chunks)
    rendered_chunks: list[str] = []
    for index, chunk in enumerate(raw_chunks, start=1):
        marker = f"[{index}/{total}]\n" if include_sequence_markers and total > 1 else ""
        rendered_chunks.append(f"{prefix}{marker}{chunk}")
    return rendered_chunks


def summarize_boxed_report_excerpt(report: str, *, limit_lines: int = 4, limit_chars: int = 240) -> str:
    lines: list[str] = []
    for raw_line in str(report or "").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("```") or stripped.startswith(("┌", "└")):
            continue
        if stripped.startswith("+") and stripped.endswith("+"):
            continue
        cleaned = stripped.strip("│").strip()
        if not cleaned:
            continue
        if cleaned.startswith("[") and cleaned.endswith("]"):
            continue
        lines.append(cleaned)
        if len(lines) >= limit_lines:
            break
    excerpt = "\n".join(lines).strip()
    if len(excerpt) <= limit_chars:
        return excerpt
    return excerpt[: max(0, limit_chars - 1)].rstrip() + "…"


def build_sourcer_activity_report(
    *,
    sourcing_activity: dict[str, Any],
    added: int,
    updated: int,
    candidates: list[dict[str, Any]],
) -> str:
    findings_count = int(sourcing_activity.get("findings_count") or 0)
    candidate_count = int(sourcing_activity.get("candidate_count") or len(candidates))
    status = str(sourcing_activity.get("status") or "").strip().lower() or "completed"
    report_status = "실패" if status == "failed" else "완료"
    summary = str(sourcing_activity.get("summary") or "").strip()
    error_text = str(sourcing_activity.get("error") or "").strip() or "없음"
    elapsed_ms = int(sourcing_activity.get("elapsed_ms") or 0)
    raw_item_count = int(sourcing_activity.get("raw_backlog_items_count") or 0)
    filtered_candidate_count = int(sourcing_activity.get("filtered_candidate_count") or candidate_count)
    milestone_title = str(sourcing_activity.get("active_sprint_milestone") or "").strip()
    milestone_filtered_out_count = int(sourcing_activity.get("milestone_filtered_out_count") or 0)
    candidate_titles = ", ".join(
        str(item.get("title") or item.get("scope") or "").strip()
        for item in candidates[:3]
        if str(item.get("title") or item.get("scope") or "").strip()
    ) or "없음"
    lines = [
        "[작업 보고]",
        "- 🧩 요청: Backlog Sourcing",
        f"- {'⚠️' if status == 'failed' else '✅'} 상태: {report_status}",
        (
            f"- 🧠 판단: "
            f"{summary or ('sourcer activity failed' if status == 'failed' else 'sourcer activity completed')}"
        ),
        (
            f"- 📊 지표: finding {findings_count}건, raw {raw_item_count}건, "
            f"후보 {filtered_candidate_count}건, 신규 {added}건, 갱신 {updated}건, {elapsed_ms}ms"
        ),
        f"- 🗂️ 후보: {candidate_titles}",
    ]
    if milestone_title:
        lines.append(
            f"- 🎯 milestone 필터: {milestone_title} (제외 {milestone_filtered_out_count}건)"
        )
    if error_text != "없음":
        lines.append(f"- ⚠️ 오류: {error_text}")
    lines.append(f"- ➡️ 다음: {'planner backlog review' if candidate_count else '대기'}")
    return box_text_message("\n".join(lines))


def build_sourcer_report_state_update(
    *,
    agent_state: dict[str, Any],
    status: str,
    client_label: str,
    reason: str,
    category: str,
    recovery_action: str,
    error: str,
    attempts: int,
    channel_id: str,
    updated_at: str,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    normalized = {
        "report_status": str(status or "").strip(),
        "report_client": str(client_label or "").strip(),
        "report_reason": str(reason or "").strip(),
        "report_category": str(category or "").strip(),
        "report_recovery_action": str(recovery_action or "").strip(),
        "report_error": str(error or "").strip(),
        "report_attempts": int(attempts or 0),
        "report_channel": str(channel_id or "").strip(),
        "report_updated_at": str(updated_at or "").strip(),
    }
    updated_state = dict(agent_state or {})
    last_failure_at = str(updated_state.get("sourcer_report_last_failure_at") or "").strip()
    last_success_at = str(updated_state.get("sourcer_report_last_success_at") or "").strip()
    reset_failure_suppression = False
    if normalized["report_status"] == "failed":
        last_failure_at = normalized["report_updated_at"]
    elif normalized["report_status"] == "sent":
        last_success_at = normalized["report_updated_at"]
        reset_failure_suppression = True
    normalized["report_last_failure_at"] = last_failure_at
    normalized["report_last_success_at"] = last_success_at
    updated_state.update(
        {
            "sourcer_report_status": normalized["report_status"],
            "sourcer_report_client": normalized["report_client"],
            "sourcer_report_reason": normalized["report_reason"],
            "sourcer_report_category": normalized["report_category"],
            "sourcer_report_recovery_action": normalized["report_recovery_action"],
            "sourcer_report_error": normalized["report_error"],
            "sourcer_report_attempts": normalized["report_attempts"],
            "sourcer_report_channel": normalized["report_channel"],
            "sourcer_report_updated_at": normalized["report_updated_at"],
            "sourcer_report_last_failure_at": normalized["report_last_failure_at"],
            "sourcer_report_last_success_at": normalized["report_last_success_at"],
        }
    )
    return normalized, updated_state, reset_failure_suppression


def should_suppress_sourcer_report_failure_log(
    *,
    client_label: str,
    category: str,
    channel_id: str,
    error_text: str,
    last_signature: str,
    last_logged_at: float,
    now: float,
    window_seconds: float = 300.0,
) -> tuple[bool, str, float]:
    signature = "|".join(
        [
            str(client_label or "").strip(),
            str(category or "").strip(),
            str(channel_id or "").strip(),
            str(error_text or "").strip(),
        ]
    )
    if signature and signature == last_signature and (now - float(last_logged_at or 0.0)) < window_seconds:
        return True, signature, now
    return False, signature, now


def log_sourcer_report_failure(
    *,
    logger: logging.Logger,
    client_label: str,
    channel_id: str,
    diagnostics: dict[str, str],
    error: BaseException,
    attempts: int,
    suppressed: bool,
) -> None:
    category = str(diagnostics.get("category") or "").strip()
    summary = str(diagnostics.get("summary") or str(error) or "").strip()
    recovery_action = str(diagnostics.get("recovery_action") or "").strip()
    if suppressed:
        logger.warning(
            "Repeated sourcer activity Discord report failure via %s to report:%s (category=%s, attempts=%s, summary=%s)",
            client_label,
            channel_id,
            category or "unknown",
            int(attempts or 0),
            summary or str(error),
        )
        return
    if category in {"discord_dns_failed", "discord_timeout", "discord_connection_failed", "client_disconnected"}:
        logger.warning(
            "Failed to send sourcer activity Discord report via %s to report:%s (category=%s, attempts=%s, summary=%s, recovery=%s)",
            client_label,
            channel_id,
            category or "unknown",
            int(attempts or 0),
            summary or str(error),
            recovery_action or "N/A",
        )
        return
    logger.exception(
        "Failed to send sourcer activity Discord report via %s to report:%s",
        client_label,
        channel_id,
    )


def resolve_sourcer_report_client(
    *,
    existing_client: Any | None,
    sourcer_report_config: Any | None,
    fallback_client: Any,
    discord_client_factory: Callable[..., Any],
    transcript_log_file: Any,
    attachment_dir: Any,
    logger: logging.Logger,
) -> tuple[Any | None, Any | None, dict[str, str]]:
    status = {
        "client_label": "",
        "reason": "",
        "category": "",
        "recovery_action": "",
    }
    if sourcer_report_config is not None:
        if existing_client is not None:
            status["client_label"] = "internal_sourcer"
            return existing_client, existing_client, status
        try:
            created_client = discord_client_factory(
                token_env=sourcer_report_config.token_env,
                expected_bot_id=sourcer_report_config.bot_id,
                transcript_log_file=transcript_log_file,
                attachment_dir=attachment_dir,
                client_name="sourcer",
            )
            status["client_label"] = "internal_sourcer"
            return created_client, created_client, status
        except Exception as exc:
            diagnostics = classify_discord_exception(
                exc,
                token_env_name=sourcer_report_config.token_env,
                expected_bot_id=sourcer_report_config.bot_id,
            )
            status["reason"] = f"internal reporter init failed: {diagnostics['summary']}"
            status["category"] = diagnostics["category"]
            status["recovery_action"] = diagnostics["recovery_action"]
            logger.exception("Failed to initialize sourcer Discord reporter; falling back to orchestrator client")
    else:
        status["reason"] = "internal sourcer reporter is not configured"
        status["category"] = "reporter_not_configured"
        status["recovery_action"] = "discord_agents_config.yaml에 internal_agents.sourcer 설정을 추가합니다."

    status["client_label"] = "orchestrator_fallback"
    return fallback_client, existing_client, status


def get_sourcer_report_client_for_service(
    service: Any,
    *,
    discord_client_factory: Callable[..., Any],
    logger: logging.Logger,
) -> Any | None:
    client, cached_client, status = resolve_sourcer_report_client(
        existing_client=service._sourcer_report_client,
        sourcer_report_config=service._sourcer_report_config,
        fallback_client=service.discord_client,
        discord_client_factory=discord_client_factory,
        transcript_log_file=service.paths.discord_logs_dir / "sourcer.jsonl",
        attachment_dir=service.paths.sprint_attachment_root(service._default_attachment_sprint_folder_name()),
        logger=logger,
    )
    service._sourcer_report_client = cached_client
    service._last_sourcer_report_client_label = status["client_label"]
    service._last_sourcer_report_reason = status["reason"]
    service._last_sourcer_report_category = status["category"]
    service._last_sourcer_report_recovery_action = status["recovery_action"]
    return client


def report_sourcer_activity_sync(
    service: Any,
    *,
    sourcing_activity: dict[str, Any],
    added: int,
    updated: int,
    candidates: list[dict[str, Any]],
    logger: logging.Logger,
) -> None:
    report_client = service._get_sourcer_report_client()
    if report_client is None:
        reason = service._last_sourcer_report_reason or "report client unavailable"
        logger.warning("Skipping sourcer activity Discord report: %s", reason)
        service._record_sourcer_report_state(
            status="skipped",
            client_label="unavailable",
            reason=reason,
            category=service._last_sourcer_report_category or "report_client_unavailable",
            recovery_action=service._last_sourcer_report_recovery_action,
            error="",
            attempts=0,
            channel_id=service.discord_config.report_channel_id,
        )
        return
    client_label = service._last_sourcer_report_client_label or "unknown"
    client_reason = service._last_sourcer_report_reason
    client_category = service._last_sourcer_report_category
    client_recovery_action = service._last_sourcer_report_recovery_action
    report = build_sourcer_activity_report(
        sourcing_activity=sourcing_activity,
        added=added,
        updated=updated,
        candidates=candidates,
    )
    logger.info(
        "Sending sourcer activity report via %s to report:%s (findings=%s, added=%s, updated=%s)",
        client_label,
        service.discord_config.report_channel_id,
        sourcing_activity.get("findings_count") or 0,
        added,
        updated,
    )

    async def send_report() -> None:
        await service._send_discord_content(
            content=report,
            send=lambda chunk: report_client.send_channel_message(service.discord_config.report_channel_id, chunk),
            target_description=f"sourcer-report:{service.discord_config.report_channel_id}",
            swallow_exceptions=False,
            log_traceback=False,
        )

    try:
        asyncio.run(send_report())
        service._record_sourcer_report_state(
            status="sent",
            client_label=client_label,
            reason=client_reason,
            category=client_category,
            recovery_action=client_recovery_action,
            error="",
            attempts=1,
            channel_id=service.discord_config.report_channel_id,
        )
    except Exception as exc:
        if isinstance(exc, RuntimeError) and "event loop" in str(exc).lower():
            reason = "event loop is already running"
            logger.warning("Skipped sourcer activity Discord report because %s.", reason)
            service._record_sourcer_report_state(
                status="skipped",
                client_label=client_label,
                reason=reason,
                category="event_loop_running",
                recovery_action="현재 실행 중인 이벤트 루프 바깥에서 sourcer report를 전송합니다.",
                error="",
                attempts=0,
                channel_id=service.discord_config.report_channel_id,
            )
            return
        diagnostics = classify_discord_exception(
            getattr(exc, "last_error", None) or exc,
            token_env_name=(
                service._sourcer_report_config.token_env
                if service._sourcer_report_config is not None
                else service.role_config.token_env
            ),
            expected_bot_id=(
                service._sourcer_report_config.bot_id
                if service._sourcer_report_config is not None
                else service.role_config.bot_id
            ),
        )
        attempts = int(getattr(exc, "attempts", 1) or 1)
        suppressed, signature, logged_at = should_suppress_sourcer_report_failure_log(
            client_label=client_label,
            category=diagnostics["category"],
            channel_id=service.discord_config.report_channel_id,
            error_text=str(exc),
            last_signature=service._last_sourcer_report_failure_signature,
            last_logged_at=service._last_sourcer_report_failure_logged_at,
            now=time.monotonic(),
        )
        service._last_sourcer_report_failure_signature = signature
        service._last_sourcer_report_failure_logged_at = logged_at
        log_sourcer_report_failure(
            logger=logger,
            client_label=client_label,
            channel_id=service.discord_config.report_channel_id,
            diagnostics=diagnostics,
            error=exc,
            attempts=attempts,
            suppressed=suppressed,
        )
        service._record_sourcer_report_state(
            status="failed",
            client_label=client_label,
            reason=client_reason or diagnostics["summary"] or "send failed",
            category=diagnostics["category"],
            recovery_action=diagnostics["recovery_action"],
            error=str(exc),
            attempts=attempts,
            channel_id=service.discord_config.report_channel_id,
        )


class DiscordNotificationService:
    def __init__(
        self,
        *,
        paths: RuntimePaths,
        role: str,
        discord_config: DiscordAgentsConfig,
        runtime_config: TeamRuntimeConfig,
        discord_client: Any,
    ):
        self.paths = paths
        self.role = role
        self.discord_config = discord_config
        self.runtime_config = runtime_config
        self.discord_client = discord_client

    def build_runtime_signature_suffix(self) -> str:
        role_config = self.runtime_config.role_defaults.get(self.role)
        if role_config is None:
            return ""
        model_name = str(role_config.model or "").strip()
        if not model_name:
            return ""
        reasoning = "None" if "gemini" in model_name.lower() else str(role_config.reasoning or "").strip() or "medium"
        return f"\n\nmodel: {model_name} | reasoning: {reasoning}"

    def append_runtime_signature(self, content: str) -> str:
        normalized = str(content or "").strip()
        if not normalized:
            return ""
        suffix = self.build_runtime_signature_suffix()
        if not suffix:
            return normalized
        if normalized.endswith(suffix.strip()):
            return normalized
        return f"{normalized}{suffix}"

    def build_startup_report(
        self,
        *,
        identity_name: str,
        identity_id: str,
        active_sprint_id: str,
    ) -> str:
        role_config = self.discord_config.get_role(self.role)
        return box_text_message(
            "\n".join(
                [
                    f"[준비 완료] ✅ {self.role}",
                    f"- 🤖 봇: {identity_name} ({identity_id})",
                    f"- 🔐 검증: role {self.role} | expected_bot_id {role_config.bot_id}",
                    f"- 🎯 현재 스프린트: {str(active_sprint_id or '').strip() or '없음'}",
                    (
                        f"- 📡 채널: startup {self.discord_config.startup_channel_id} | "
                        f"relay {self.discord_config.relay_channel_id}"
                    ),
                ]
            ).strip()
        )

    @contextlib.asynccontextmanager
    async def cross_process_send_lock(self):
        lock_file = self.paths.runtime_root / "discord_send.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_file.open("a+", encoding="utf-8")
        try:
            await asyncio.to_thread(fcntl.flock, handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    async def send_content(
        self,
        *,
        content: str,
        send,
        target_description: str,
        prefix: str = "",
        swallow_exceptions: bool = False,
        log_traceback: bool = True,
    ) -> None:
        rendered_content = self.append_runtime_signature(content)
        chunks = _render_discord_message_chunks(rendered_content, prefix=prefix)
        total = len(chunks)
        async with self.cross_process_send_lock():
            for index, chunk in enumerate(chunks, start=1):
                try:
                    await send(chunk)
                except Exception as exc:
                    if log_traceback:
                        LOGGER.exception(
                            "Failed to send Discord chunk %s/%s to %s",
                            index,
                            total,
                            target_description,
                        )
                    else:
                        LOGGER.warning(
                            "Failed to send Discord chunk %s/%s to %s: %s",
                            index,
                            total,
                            target_description,
                            exc,
                        )
                    if not swallow_exceptions:
                        raise

    async def send_channel_reply(self, message: DiscordMessage, content: str) -> None:
        if message.is_dm:
            await self.send_content(
                content=content,
                send=lambda chunk: self.discord_client.send_dm(message.author_id, chunk),
                target_description=f"dm:{message.author_id}",
            )
            return
        prefix = f"<@{message.author_id}> "
        await self.send_content(
            content=content,
            send=lambda chunk: self.discord_client.send_channel_message(message.channel_id, chunk),
            target_description=f"channel:{message.channel_id}",
            prefix=prefix,
        )

    async def send_requester_reply(
        self,
        *,
        route: dict[str, Any],
        content: str,
        request_id: str,
        route_source: str,
        current_role: str,
        reply_route_snapshot: dict[str, Any],
        original_requester: dict[str, Any],
        params_keys: list[str],
    ) -> None:
        target_content = str(content or "").strip()
        if not target_content:
            return
        if route.get("is_dm"):
            author_id = str(route.get("author_id") or "").strip()
            if not author_id:
                LOGGER.info(
                    "Skipping requester reply for request %s because DM author_id is missing.",
                    request_id or "unknown",
                )
                return
            await self.send_content(
                content=target_content,
                send=lambda chunk: self.discord_client.send_dm(author_id, chunk),
                target_description=f"dm:{author_id}",
            )
            return
        author_id = str(route.get("author_id") or "").strip()
        channel_id = str(route.get("channel_id") or "").strip()
        if not channel_id:
            LOGGER.warning(
                "Skipping requester reply for request %s because channel_id is missing. current_role=%s route_source=%s reply_route=%s original_requester=%s params_keys=%s",
                request_id or "unknown",
                current_role or "unknown",
                route_source,
                json.dumps(reply_route_snapshot or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(original_requester or {}, ensure_ascii=False, sort_keys=True),
                ",".join(sorted(str(key) for key in (params_keys or []))),
            )
            return
        prefix = f"<@{author_id}> " if author_id else ""
        await self.send_content(
            content=target_content,
            send=lambda chunk: self.discord_client.send_channel_message(channel_id, chunk),
            target_description=f"channel:{channel_id}",
            prefix=prefix,
        )

    async def send_internal_relay_summary(
        self,
        *,
        relay_channel_id: str,
        content: str,
        request_id: str = "",
    ) -> None:
        normalized_channel_id = str(relay_channel_id or "").strip()
        if not normalized_channel_id:
            return
        try:
            await self.send_content(
                content=content,
                send=lambda chunk: self.discord_client.send_channel_message(normalized_channel_id, chunk),
                target_description=f"relay_summary:{normalized_channel_id}",
                swallow_exceptions=False,
                log_traceback=False,
            )
        except Exception as exc:
            LOGGER.warning(
                "Failed to send internal relay summary for request %s to relay:%s: %s",
                request_id or "unknown",
                normalized_channel_id,
                exc,
            )

    async def send_sprint_completion_user_report(
        self,
        *,
        report_channel_id: str,
        sprint_id: str,
        content: str,
        embed: dict[str, Any] | list[dict[str, Any]] | None = None,
        report_file_path: str = "",
    ) -> bool:
        normalized_channel_id = str(report_channel_id or "").strip()
        if not normalized_channel_id:
            return False
        rich_send = getattr(self.discord_client, "send_channel_rich_message", None)
        if callable(rich_send) and (embed or report_file_path):
            try:
                embeds = embed if isinstance(embed, list) else [embed] if embed else [None]
                for index, payload in enumerate(embeds):
                    await rich_send(
                        normalized_channel_id,
                        content="",
                        embed=payload,
                        files=[report_file_path] if index == 0 and str(report_file_path or "").strip() else [],
                        allowed_mentions="none",
                    )
                return True
            except Exception as exc:
                LOGGER.warning(
                    "Failed to send rich user-facing sprint summary for sprint %s to report:%s; falling back to markdown chunks: %s",
                    sprint_id or "unknown",
                    normalized_channel_id,
                    exc,
                )
        try:
            await self.send_content(
                content=content,
                send=lambda chunk: self.discord_client.send_channel_message(normalized_channel_id, chunk),
                target_description=f"sprint-user-report:{normalized_channel_id}:{sprint_id or ''}",
                swallow_exceptions=False,
                log_traceback=False,
            )
            return True
        except Exception as exc:
            LOGGER.warning(
                "Failed to send user-facing sprint summary for sprint %s to report:%s: %s",
                sprint_id or "unknown",
                normalized_channel_id,
                exc,
            )
            return False

    async def send_sprint_report(
        self,
        *,
        startup_channel_id: str,
        rendered_title: str,
        report: str,
        swallow_exceptions: bool = True,
    ) -> None:
        normalized_channel_id = str(startup_channel_id or "").strip()
        await self.send_content(
            content=report,
            send=lambda chunk: self.discord_client.send_channel_message(normalized_channel_id, chunk),
            target_description=f"sprint-report:{normalized_channel_id}:{rendered_title}",
            swallow_exceptions=swallow_exceptions,
        )

    async def send_relay_envelope(
        self,
        *,
        relay_channel_id: str,
        target_bot_id: str,
        content: str,
    ) -> None:
        normalized_channel_id = str(relay_channel_id or "").strip()
        prefix = f"<@{str(target_bot_id or '').strip()}>\n" if str(target_bot_id or "").strip() else ""
        await self.send_content(
            content=content,
            send=lambda chunk: self.discord_client.send_channel_message(normalized_channel_id, chunk),
            target_description=f"relay:{normalized_channel_id}",
            prefix=prefix,
        )

    async def send_immediate_receipt(self, message: DiscordMessage) -> None:
        async with self.cross_process_send_lock():
            if message.is_dm:
                await self.discord_client.send_dm(message.author_id, "수신양호")
                return
            await self.discord_client.send_channel_message(message.channel_id, f"<@{message.author_id}> 수신양호")

    def iter_startup_fallback_targets(self) -> list[tuple[str, str]]:
        startup_channel = str(self.discord_config.startup_channel_id or "").strip()
        candidates = [
            ("report", str(self.discord_config.report_channel_id or "").strip()),
            ("relay", str(self.discord_config.relay_channel_id or "").strip()),
        ]
        seen: set[str] = set()
        targets: list[tuple[str, str]] = []
        for label, channel_id in candidates:
            if not channel_id or channel_id == startup_channel or channel_id in seen:
                continue
            seen.add(channel_id)
            targets.append((label, channel_id))
        return targets

    def build_startup_fallback_report(self, *, report: str, error: DiscordSendError, fallback_target: str) -> str:
        return build_progress_report(
            request=f"{self.role} startup 알림 복구",
            scope=f"startup {self.discord_config.startup_channel_id} -> {fallback_target}",
            status="실패",
            list_summary="",
            detail_summary=(
                f"phase={getattr(error, 'phase', '') or 'send'}, "
                f"attempts={getattr(error, 'attempts', 1)}"
            ),
            process_summary="없음",
            log_summary=summarize_boxed_report_excerpt(report),
            end_reason=str(error),
            judgment="startup 채널 전송은 실패해 대체 채널로 1회 복구 통지를 시도했습니다.",
            next_action="startup 채널 접근 상태와 Discord 네트워크 타임아웃을 확인합니다.",
            artifacts=[str(self.paths.agent_state_file(self.role))],
        )

    async def send_startup_failure_fallback(
        self,
        *,
        report: str,
        error: DiscordSendError,
        fallback_targets: list[tuple[str, str]] | None = None,
    ) -> str:
        targets = list(fallback_targets) if fallback_targets is not None else self.iter_startup_fallback_targets()
        for label, channel_id in targets:
            fallback_target = f"{label}:{channel_id}"
            try:
                await self.send_content(
                    content=self.build_startup_fallback_report(
                        report=report,
                        error=error,
                        fallback_target=fallback_target,
                    ),
                    send=lambda chunk, channel_id=channel_id: self.discord_client.send_channel_message(channel_id, chunk),
                    target_description=fallback_target,
                    swallow_exceptions=False,
                )
                return fallback_target
            except Exception as fallback_exc:
                LOGGER.warning(
                    "Fallback startup notification failed for role %s via %s: %s",
                    self.role,
                    fallback_target,
                    fallback_exc,
                )
        return ""

    def build_requester_status_message(
        self,
        *,
        status: str,
        request_id: str,
        summary: str,
        related_request_ids: list[str] | None = None,
        simplify_summary: Callable[[str], str] | None = None,
    ) -> str:
        normalized_status = str(status or "").strip().lower()
        header_map = {
            "completed": "완료",
            "delegated": "진행 중",
            "blocked": "차단됨",
            "failed": "실패",
            "cancelled": "취소됨",
        }
        primary_label_map = {
            "completed": "결과",
            "delegated": "현재 상태",
            "blocked": "이유",
            "failed": "이유",
            "cancelled": "결과",
        }
        header = header_map.get(normalized_status, "안내")
        primary_label = primary_label_map.get(normalized_status, "내용")
        next_action_map = {
            "completed": "결과를 확인하고 필요하면 후속 요청을 이어갑니다.",
            "delegated": "현재 상태를 확인한 뒤 추가 응답을 기다립니다.",
            "blocked": "차단 이유를 확인하고 필요한 입력이나 재요청 여부를 판단합니다.",
            "failed": "실패 이유를 확인하고 재시도 또는 새 요청 여부를 판단합니다.",
            "cancelled": "필요하면 새 요청을 생성합니다.",
        }
        detail_text = (
            simplify_summary(summary)
            if callable(simplify_summary)
            else str(summary or "").strip()
        )
        detail_lines = [line.strip() for line in detail_text.splitlines() if line.strip()]
        lines = [header]
        if detail_lines:
            lines.append(f"- {primary_label}: {detail_lines[0]}")
            next_action = next_action_map.get(normalized_status, "요약을 확인합니다.")
            if next_action:
                lines.append(f"- 다음: {next_action}")
            for line in detail_lines[1:]:
                lines.append(f"- {line}")
        elif normalized_status == "completed":
            lines.append("- 결과: 요청을 처리했습니다.")
            lines.append(f"- 다음: {next_action_map['completed']}")
        else:
            next_action = next_action_map.get(normalized_status, "요약을 확인합니다.")
            if next_action:
                lines.append(f"- 다음: {next_action}")
        if related_request_ids:
            lines.append(f"- 관련 요청 재개: {', '.join(related_request_ids)}")
        if request_id:
            lines.append(f"- 요청 ID: {request_id}")
        return "\n".join(lines)


def extract_summary_field(summary: str, field_name: str) -> str:
    normalized_field = str(field_name or "").strip().lower()
    if not normalized_field:
        return ""
    prefixes = (
        f"{normalized_field}=",
        f"- {normalized_field}:",
        f"{normalized_field}:",
    )
    for raw_line in str(summary or "").splitlines():
        stripped = raw_line.strip()
        lowered = stripped.lower()
        for prefix in prefixes:
            if lowered.startswith(prefix):
                return stripped[len(prefix) :].strip()
    return ""


def first_sentence(text: str) -> str:
    normalized = " ".join(str(text or "").split())
    if not normalized:
        return ""
    pieces = re.split(r"(?<=[.!?])\s+", normalized, maxsplit=1)
    return pieces[0].strip()


def simplify_requester_summary(summary: str) -> str:
    normalized = str(summary or "").replace("\r\n", "\n").strip()
    if not normalized:
        return ""
    normalized = re.sub(r"python -m teams_runtime[^\n]*?를 반환했고,\s*", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)

    if "종료할 활성 스프린트가 없었습니다" in normalized or "현재 active sprint가 없어 종료할 대상이 없습니다." in normalized:
        return "\n".join(
            [
                "진행 중인 스프린트가 없어 종료할 대상이 없습니다.",
                "현재 상태: 진행 중인 스프린트 없음",
            ]
        )

    if normalized.startswith("## Sprint Summary"):
        sprint_name = extract_summary_field(normalized, "sprint_name")
        sprint_id = extract_summary_field(normalized, "sprint_id")
        milestone = extract_summary_field(normalized, "milestone_title")
        phase = extract_summary_field(normalized, "phase")
        status = extract_summary_field(normalized, "status")
        todo_summary = extract_summary_field(normalized, "todo_summary")
        if sprint_name == "N/A":
            sprint_name = ""
        lines = ["현재 스프린트 상태입니다."]
        if sprint_name or sprint_id:
            lines.append(f"스프린트: {sprint_name or sprint_id}")
        if sprint_id and sprint_name and sprint_id != sprint_name:
            lines.append(f"스프린트 ID: {sprint_id}")
        if milestone and milestone != "N/A":
            lines.append(f"마일스톤: {milestone}")
        if phase and phase != "N/A":
            lines.append(f"단계: {phase}")
        if status and status != "N/A":
            lines.append(f"상태: {status}")
        if todo_summary and todo_summary != "N/A":
            lines.append(f"작업 요약: {todo_summary}")
        return "\n".join(lines)

    if normalized.startswith("manual sprint initial phase를 시작했습니다."):
        sprint_name = extract_summary_field(normalized, "sprint_name")
        sprint_id = extract_summary_field(normalized, "sprint_id")
        milestone = extract_summary_field(normalized, "milestone")
        lines = ["스프린트를 시작했습니다."]
        if sprint_name or sprint_id:
            lines.append(f"스프린트: {sprint_name or sprint_id}")
        if milestone:
            lines.append(f"마일스톤: {milestone}")
        return "\n".join(lines)

    if normalized.startswith("이미 active sprint가 있습니다."):
        sprint_id = extract_summary_field(normalized, "sprint_id")
        phase = extract_summary_field(normalized, "phase")
        milestone = extract_summary_field(normalized, "milestone")
        lines = ["이미 진행 중인 스프린트가 있습니다."]
        if sprint_id:
            lines.append(f"스프린트 ID: {sprint_id}")
        if milestone:
            lines.append(f"마일스톤: {milestone}")
        if phase:
            lines.append(f"현재 단계: {phase}")
        return "\n".join(lines)

    if normalized.startswith("현재 sprint를 wrap up 대상"):
        sprint_id = extract_summary_field(normalized, "sprint_id")
        lines = ["스프린트 종료를 요청했습니다."]
        if sprint_id:
            lines.append(f"스프린트 ID: {sprint_id}")
        if "진행 중 task가 끝나면 wrap up을 시작합니다." in normalized:
            lines.append("현재 상태: 진행 중인 작업이 끝나면 마무리를 시작합니다.")
        elif "곧 wrap up을 시작합니다." in normalized:
            lines.append("현재 상태: 곧 마무리를 시작합니다.")
        return "\n".join(lines)

    if normalized.startswith("terminal sprint를 종료 처리했습니다."):
        sprint_id = extract_summary_field(normalized, "sprint_id")
        status = extract_summary_field(normalized, "status")
        lines = ["스프린트를 종료했습니다."]
        if sprint_id:
            lines.append(f"스프린트 ID: {sprint_id}")
        if status:
            lines.append(f"현재 상태: {status}")
        return "\n".join(lines)

    if normalized.startswith("재개할 sprint가 없습니다."):
        return "재개할 스프린트가 없습니다."

    if normalized.startswith("이미 완료된 sprint라 재개할 수 없습니다."):
        sprint_id = extract_summary_field(normalized, "sprint_id")
        lines = ["이미 완료된 스프린트라 재개할 수 없습니다."]
        if sprint_id:
            lines.append(f"스프린트 ID: {sprint_id}")
        return "\n".join(lines)

    if normalized.startswith("현재 상태에서는 sprint를 재개할 수 없습니다."):
        sprint_id = extract_summary_field(normalized, "sprint_id")
        status = extract_summary_field(normalized, "status")
        lines = ["현재 상태에서는 스프린트를 재개할 수 없습니다."]
        if sprint_id:
            lines.append(f"스프린트 ID: {sprint_id}")
        if status:
            lines.append(f"현재 상태: {status}")
        return "\n".join(lines)

    if normalized.startswith("active sprint 재개를 요청했습니다."):
        sprint_id = extract_summary_field(normalized, "sprint_id")
        phase = extract_summary_field(normalized, "phase")
        status = extract_summary_field(normalized, "status")
        lines = ["스프린트 재개를 요청했습니다."]
        if sprint_id:
            lines.append(f"스프린트 ID: {sprint_id}")
        if phase:
            lines.append(f"현재 단계: {phase}")
        if status:
            lines.append(f"현재 상태: {status}")
        return "\n".join(lines)

    if "현재 상태는" in normalized:
        before, after = normalized.split("현재 상태는", 1)
        initial_sentence = first_sentence(before)
        state_text = after.strip().rstrip(".")
        lines: list[str] = []
        if initial_sentence:
            lines.append(initial_sentence)
        if state_text:
            lines.append(f"현재 상태: {state_text}")
        if lines:
            return "\n".join(lines)

    cleaned_lines = [
        line.strip()
        for line in normalized.splitlines()
        if line.strip() and not line.strip().startswith("request_id=")
    ]
    if not cleaned_lines:
        return ""
    if len(cleaned_lines) <= 4:
        return "\n".join(cleaned_lines)
    return first_sentence(cleaned_lines[0]) or cleaned_lines[0]


def build_requester_status_message(
    notification_service: DiscordNotificationService,
    *,
    status: str,
    request_id: str,
    summary: str,
    related_request_ids: list[str] | None = None,
) -> str:
    return notification_service.build_requester_status_message(
        status=status,
        request_id=request_id,
        summary=summary,
        related_request_ids=related_request_ids,
        simplify_summary=simplify_requester_summary,
    )


async def reply_to_requester(
    notification_service: DiscordNotificationService,
    request_record: RequestRecord,
    content: str,
    *,
    save_request: Callable[[RequestRecord], None],
) -> None:
    persisted_route = (
        dict(request_record.get("reply_route") or {})
        if isinstance(request_record.get("reply_route"), dict)
        else {}
    )
    params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
    resolution = resolve_request_reply_route(persisted_route, params)
    if resolution.recovered_reply_route is not None:
        request_record["reply_route"] = dict(resolution.recovered_reply_route)
        save_request(request_record)
    await notification_service.send_requester_reply(
        route=dict(resolution.route),
        content=content,
        request_id=str(request_record.get("request_id") or ""),
        route_source=resolution.source,
        current_role=str(request_record.get("current_role") or ""),
        reply_route_snapshot=persisted_route,
        original_requester=extract_original_requester(params),
        params_keys=list(params.keys()),
    )


async def send_channel_reply(
    notification_service: DiscordNotificationService,
    message: DiscordMessage,
    content: str,
) -> None:
    await notification_service.send_channel_reply(message, content)


async def send_immediate_receipt(
    notification_service: DiscordNotificationService,
    message: DiscordMessage,
    *,
    is_trusted_relay_message: Callable[[DiscordMessage], bool],
) -> None:
    if is_trusted_relay_message(message):
        return
    await notification_service.send_immediate_receipt(message)


async def send_discord_content(
    notification_service: DiscordNotificationService,
    *,
    content: str,
    send: Callable[..., Any],
    target_description: str,
    prefix: str = "",
    swallow_exceptions: bool = False,
    log_traceback: bool = True,
) -> None:
    await notification_service.send_content(
        content=content,
        send=send,
        target_description=target_description,
        prefix=prefix,
        swallow_exceptions=swallow_exceptions,
        log_traceback=log_traceback,
    )


async def announce_startup_notification(
    notification_service: DiscordNotificationService,
    *,
    role: str,
    identity: dict[str, Any],
    active_sprint_id: str,
    startup_channel_id: str,
    send_channel_message: Callable[[str], Any],
    record_startup_notification_state: Callable[..., None],
    log_warning: Callable[..., None],
) -> None:
    identity_name = identity.get("name") or "unknown"
    identity_id = identity.get("id") or "unknown"
    report = notification_service.build_startup_report(
        identity_name=identity_name,
        identity_id=identity_id,
        active_sprint_id=str(active_sprint_id or "").strip(),
    )
    startup_target = f"startup:{startup_channel_id}"
    try:
        await send_discord_content(
            notification_service,
            content=report,
            send=send_channel_message,
            target_description=startup_target,
            swallow_exceptions=False,
        )
    except Exception as exc:
        send_error = exc if isinstance(exc, DiscordSendError) else DiscordSendError(str(exc))
        fallback_target = await notification_service.send_startup_failure_fallback(
            report=report,
            error=send_error,
        )
        record_startup_notification_state(
            status="fallback_sent" if fallback_target else "failed",
            error=str(send_error),
            attempted_channel=startup_channel_id,
            attempts=getattr(send_error, "attempts", 1),
            fallback_target=fallback_target,
        )
        log_warning(
            "Startup notification failed for role %s via %s: %s",
            role,
            startup_target,
            send_error,
        )
        return
    record_startup_notification_state(
        status="sent",
        error="",
        attempted_channel=startup_channel_id,
        attempts=1,
        fallback_target="",
    )
