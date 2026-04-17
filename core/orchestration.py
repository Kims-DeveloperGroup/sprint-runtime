from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from teams_runtime.core.actions import ActionExecutor
from teams_runtime.core.agent_capabilities import (
    EXECUTION_AGENT_ROLES,
    get_agent_capability,
    intent_to_role_map,
    load_agent_utilization_policy,
)
from teams_runtime.core.config import load_discord_agents_config, load_team_runtime_config
from teams_runtime.core.git_ops import (
    build_sprint_commit_message,
    build_version_control_helper_command,
    capture_git_baseline,
    collect_sprint_owned_paths,
    inspect_sprint_closeout,
)
from teams_runtime.core.parsing import (
    SPRINT_CONTROL_START_PATTERN,
    envelope_to_text,
    is_manual_sprint_finalize_text,
    is_manual_sprint_start_text,
    parse_message_content,
    parse_user_message_content,
)
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.persistence import (
    append_jsonl,
    append_request_event,
    build_request_fingerprint,
    is_terminal_request,
    iter_json_records,
    new_request_id,
    normalize_runtime_datetime,
    read_json,
    utc_now_iso,
    write_json,
)
from teams_runtime.core.reports import (
    ReportSection,
    box_text_message,
    build_progress_report,
    read_process_summary,
    render_report_sections,
)
from teams_runtime.core.sprints import (
    build_active_sprint_id,
    build_sprint_artifact_folder_name,
    build_backlog_item,
    build_daily_sprint_display_name,
    collect_sprint_todo_artifact_entries,
    build_sprint_cutoff_at,
    build_todo_item,
    compute_next_slot_at,
    load_sprint_history_index,
    render_backlog_markdown,
    render_current_sprint_markdown,
    render_sprint_artifact_index_markdown,
    render_sprint_history_index,
    render_sprint_history_markdown,
    slugify_sprint_value,
    utc_now,
)
from teams_runtime.discord.client import (
    DiscordClient,
    DiscordListenError,
    DiscordMessage,
    DiscordSendError,
    MESSAGE_END_MARKER,
    MESSAGE_START_MARKER,
    classify_discord_exception,
)
from teams_runtime.models import MessageEnvelope, TEAM_ROLES
from teams_runtime.runtime.codex import (
    BacklogSourcingRuntime,
    IntentParserRuntime,
    RoleAgentRuntime,
    normalize_intent_payload,
    normalize_role_payload,
)


LOGGER = logging.getLogger(__name__)
SCHEDULER_POLL_SECONDS = 15.0
LISTENER_RETRY_SECONDS = 5.0
INTERNAL_REQUEST_POLL_SECONDS = 0.2
BACKLOG_SOURCING_POLL_SECONDS = 15.0
ROLE_REQUEST_RESUME_POLL_SECONDS = 5.0
MALFORMED_RELAY_LOG_WINDOW_SECONDS = 60.0
RELAY_TRANSPORT_INTERNAL = "internal"
RELAY_TRANSPORT_DISCORD = "discord"
VALID_RELAY_TRANSPORTS = {
    RELAY_TRANSPORT_INTERNAL,
    RELAY_TRANSPORT_DISCORD,
}
INTERNAL_RELAY_SUMMARY_MARKER = "내부 relay 요약:"

PRIMARY_SHARED_FILES = {
    "planner": "planning",
    "designer": "planning",
    "architect": "decision_log",
    "developer": "shared_history",
    "qa": "shared_history",
    "orchestrator": "shared_history",
}

SPRINT_ROLE_DISPLAY_NAMES = {
    "orchestrator": "오케스트레이터",
    "planner": "플래너",
    "designer": "디자이너",
    "architect": "아키텍트",
    "developer": "개발자",
    "qa": "QA",
    "parser": "파서",
    "sourcer": "소서",
    "version_controller": "버전 컨트롤러",
}
SPRINT_DISCORD_SUMMARY_FLOW_LIMIT = 4
SPRINT_DISCORD_SUMMARY_ROLE_LIMIT = 4
SPRINT_DISCORD_SUMMARY_ISSUE_LIMIT = 3
SPRINT_DISCORD_SUMMARY_ACHIEVEMENT_LIMIT = 3
SPRINT_DISCORD_SUMMARY_ARTIFACT_LIMIT = 3

PLANNING_CONTEXT_RECENCY_SECONDS = 3600.0
SPRINT_INITIAL_PHASE_MAX_ITERATIONS = 3
RECENT_SPRINT_ACTIVITY_LIMIT = 25
INITIAL_PHASE_STEP_MILESTONE_REFINEMENT = "milestone_refinement"
INITIAL_PHASE_STEP_ARTIFACT_SYNC = "artifact_sync"
INITIAL_PHASE_STEP_BACKLOG_DEFINITION = "backlog_definition"
INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION = "backlog_prioritization"
INITIAL_PHASE_STEP_TODO_FINALIZATION = "todo_finalization"
INITIAL_PHASE_STEPS = (
    INITIAL_PHASE_STEP_MILESTONE_REFINEMENT,
    INITIAL_PHASE_STEP_ARTIFACT_SYNC,
    INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
    INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
    INITIAL_PHASE_STEP_TODO_FINALIZATION,
)
INITIAL_PHASE_STEP_TITLES = {
    INITIAL_PHASE_STEP_MILESTONE_REFINEMENT: "milestone 정리",
    INITIAL_PHASE_STEP_ARTIFACT_SYNC: "plan/spec 동기화",
    INITIAL_PHASE_STEP_BACKLOG_DEFINITION: "backlog 정의",
    INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION: "backlog 우선순위화",
    INITIAL_PHASE_STEP_TODO_FINALIZATION: "실행 todo 확정",
}
SPRINT_ACTIVE_BACKLOG_STATUSES = {"pending", "selected", "blocked"}
SPRINT_MILESTONE_PATTERN = re.compile(r"(?im)^(?:milestone|마일스톤)\s*[:=-]\s*(?P<value>[^\n\r]+)\s*$")
SPRINT_KICKOFF_BRIEF_PATTERN = re.compile(r"(?is)^(?:brief|details|context|notes|설명|배경|메모)\s*[:=-]\s*(?P<value>.*)$")
SPRINT_KICKOFF_REQUIREMENTS_PATTERN = re.compile(
    r"(?is)^(?:requirements?|requirement|constraints?|needs?|요구사항|요건|제약)\s*[:=-]?\s*(?P<value>.*)$"
)
SPRINT_BULLET_LINE_PATTERN = re.compile(r"^\s*(?:[-*•]+|\d+[.)])\s+(?P<value>.+?)\s*$")
SPRINT_COMMAND_LINE_PATTERN = re.compile(
    r"(?is)^\s*(?:(?:start|begin|kickoff|run|open|create)\b.*\bsprint\b|스프린트.{0,24}(?:시작|열어|열기|만들|생성))\s*$"
)
WORKFLOW_SELECTION_SOURCE = "workflow_contract"
WORKFLOW_POLICY_SOURCE = "workflow_contract"
WORKFLOW_CONTRACT_VERSION = 1
WORKFLOW_PHASE_PLANNING = "planning"
WORKFLOW_PHASE_IMPLEMENTATION = "implementation"
WORKFLOW_PHASE_VALIDATION = "validation"
WORKFLOW_PHASE_CLOSEOUT = "closeout"
WORKFLOW_PHASES = {
    WORKFLOW_PHASE_PLANNING,
    WORKFLOW_PHASE_IMPLEMENTATION,
    WORKFLOW_PHASE_VALIDATION,
    WORKFLOW_PHASE_CLOSEOUT,
}
WORKFLOW_STEP_PLANNER_DRAFT = "planner_draft"
WORKFLOW_STEP_PLANNER_ADVISORY = "planner_advisory"
WORKFLOW_STEP_PLANNER_FINALIZE = "planner_finalize"
WORKFLOW_STEP_ARCHITECT_GUIDANCE = "architect_guidance"
WORKFLOW_STEP_DEVELOPER_BUILD = "developer_build"
WORKFLOW_STEP_ARCHITECT_REVIEW = "architect_review"
WORKFLOW_STEP_DEVELOPER_REVISION = "developer_revision"
WORKFLOW_STEP_QA_VALIDATION = "qa_validation"
WORKFLOW_STEP_CLOSEOUT = "closeout"
DEFAULT_WORKFLOW_REVIEW_CYCLE_LIMIT = 3
WORKFLOW_STEPS = {
    WORKFLOW_STEP_PLANNER_DRAFT,
    WORKFLOW_STEP_PLANNER_ADVISORY,
    WORKFLOW_STEP_PLANNER_FINALIZE,
    WORKFLOW_STEP_ARCHITECT_GUIDANCE,
    WORKFLOW_STEP_DEVELOPER_BUILD,
    WORKFLOW_STEP_ARCHITECT_REVIEW,
    WORKFLOW_STEP_DEVELOPER_REVISION,
    WORKFLOW_STEP_QA_VALIDATION,
    WORKFLOW_STEP_CLOSEOUT,
}
WORKFLOW_REOPEN_CATEGORIES = {"", "scope", "ux", "architecture", "implementation", "verification"}
PLANNING_ADVISORY_ROLES = {"designer", "architect"}
PLANNING_SURFACE_SPRINT_DOC_NAMES = {
    "kickoff.md",
    "milestone.md",
    "plan.md",
    "spec.md",
    "todo_backlog.md",
    "iteration_log.md",
}
PLANNER_OWNED_SPRINT_DOC_NAMES = {
    "kickoff.md",
    "milestone.md",
    "plan.md",
    "spec.md",
    "todo_backlog.md",
    "iteration_log.md",
}
PLANNING_SURFACE_ROOT_DOC_NAMES = {
    "backlog.md",
    "completed_backlog.md",
    "current_sprint.md",
}
SPRINT_SPEC_TODO_REPORT_DOC_KEYS = ("milestone", "plan", "spec", "todo_backlog", "iteration_log")
WORKFLOW_QA_REOPEN_REQUIRED_DOC_NAMES = ("spec.md", "todo_backlog.md", "iteration_log.md", "current_sprint.md")


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


def _extract_original_requester(params: dict[str, Any]) -> dict[str, Any]:
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


def _merge_requester_route(*sources: dict[str, Any] | None) -> dict[str, Any]:
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


def _parse_report_body_json(body: str) -> dict[str, Any]:
    raw = str(body or "").strip()
    if not raw:
        return {}
    candidates: list[str] = [raw]
    if raw.startswith("```"):
        lines = raw.splitlines()
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
    return {}


def _normalize_markdown_body(lines: list[str]) -> str:
    body = "\n".join(str(line).rstrip() for line in lines).strip()
    return body


def _normalize_insights(result: dict[str, Any]) -> list[str]:
    raw = result.get("insights")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        normalized = str(raw).strip()
        return [normalized] if normalized else []
    return []


def _collapse_whitespace(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _truncate_text(value: Any, *, limit: int = 240) -> str:
    normalized = _collapse_whitespace(value)
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def _join_semantic_fragments(fragments: Iterable[str], *, separator: str = " | ") -> str:
    normalized = [_collapse_whitespace(item) for item in fragments if _collapse_whitespace(item)]
    return separator.join(normalized)


def _looks_meta_summary(text: str) -> bool:
    normalized = _collapse_whitespace(text)
    if not normalized:
        return False
    meta_markers = (
        "구체화했습니다",
        "정리했습니다",
        "정리할 수 있도록",
        "바로 구현",
        "기술 계약",
        "구현 가능한 수준",
        "넘길 준비",
    )
    return any(marker in normalized for marker in meta_markers)


def _looks_meta_change_text(text: str) -> bool:
    normalized = _collapse_whitespace(text)
    if not normalized:
        return False
    meta_markers = (
        "정리했습니다",
        "정리합니다",
        "구체화했습니다",
        "반영했습니다",
        "반영된 것을 확인했습니다",
        "일관되게 반영",
        "동기화했습니다",
        "재구성했습니다",
        "업데이트했습니다",
        "개선했습니다",
        "개선 방향",
        "prompt",
        "프롬프트",
        "문서",
        "라우팅",
        "회귀 테스트",
        "regression test",
    )
    return any(marker in normalized.lower() for marker in meta_markers)


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        normalized = str(value).strip()
        return [normalized] if normalized else []
    return []


def _normalize_sprint_report_changes(value: Any) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return changes
    for raw_item in value:
        if not isinstance(raw_item, dict):
            continue
        title = _collapse_whitespace(raw_item.get("title") or "")
        why = _collapse_whitespace(raw_item.get("why") or "")
        what_changed = _collapse_whitespace(
            raw_item.get("what_changed")
            or raw_item.get("what")
            or raw_item.get("behavior")
            or raw_item.get("summary")
            or ""
        )
        meaning = _collapse_whitespace(raw_item.get("meaning") or "")
        how = _collapse_whitespace(raw_item.get("how") or "")
        artifacts = _normalize_string_list(raw_item.get("artifacts"))
        request_ids = _normalize_string_list(raw_item.get("request_ids"))
        if not any((title, why, what_changed, meaning, how, artifacts, request_ids)):
            continue
        changes.append(
            {
                "title": title,
                "why": why,
                "what_changed": what_changed,
                "meaning": meaning,
                "how": how,
                "artifacts": artifacts,
                "request_ids": request_ids,
            }
        )
    return changes


def _normalize_sprint_report_contributions(value: Any) -> list[dict[str, Any]]:
    contributions: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return contributions
    for raw_item in value:
        if isinstance(raw_item, str):
            summary = _collapse_whitespace(raw_item)
            if summary:
                contributions.append({"role": "", "summary": summary, "artifacts": []})
            continue
        if not isinstance(raw_item, dict):
            continue
        role = _collapse_whitespace(raw_item.get("role") or "")
        summary = _collapse_whitespace(
            raw_item.get("summary")
            or raw_item.get("highlight")
            or raw_item.get("what")
            or ""
        )
        artifacts = _normalize_string_list(raw_item.get("artifacts"))
        if not any((role, summary, artifacts)):
            continue
        contributions.append({"role": role, "summary": summary, "artifacts": artifacts})
    return contributions


def _normalize_sprint_report_draft(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized = {
        "headline": _collapse_whitespace(value.get("headline") or value.get("tl_dr") or ""),
        "changes": _normalize_sprint_report_changes(value.get("changes")),
        "timeline": _normalize_string_list(value.get("timeline")),
        "agent_contributions": _normalize_sprint_report_contributions(value.get("agent_contributions")),
        "issues": _normalize_string_list(value.get("issues")),
        "achievements": _normalize_string_list(value.get("achievements")),
        "highlight_artifacts": _normalize_string_list(value.get("highlight_artifacts")),
    }
    if not any(
        (
            normalized["headline"],
            normalized["changes"],
            normalized["timeline"],
            normalized["agent_contributions"],
            normalized["issues"],
            normalized["achievements"],
            normalized["highlight_artifacts"],
        )
    ):
        return {}
    return normalized


def _has_markdown_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_has_markdown_value(item) for item in value)
    if isinstance(value, dict):
        return any(_has_markdown_value(item) for item in value.values())
    return True


def _markdown_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _append_markdown_structure(lines: list[str], value: Any, *, indent: int = 0) -> None:
    prefix = "  " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if not _has_markdown_value(item):
                continue
            label = str(key).strip() or "item"
            if isinstance(item, (dict, list, tuple, set)):
                lines.append(f"{prefix}- {label}:")
                _append_markdown_structure(lines, item, indent=indent + 1)
            else:
                lines.append(f"{prefix}- {label}: {_markdown_scalar(item)}")
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if not _has_markdown_value(item):
                continue
            if isinstance(item, dict):
                title = (
                    str(item.get("title") or "").strip()
                    or str(item.get("name") or "").strip()
                    or str(item.get("summary") or "").strip()
                )
                nested = dict(item)
                if title:
                    nested.pop("title", None)
                    nested.pop("name", None)
                    if str(nested.get("summary") or "").strip() == title:
                        nested.pop("summary", None)
                    lines.append(f"{prefix}- {title}:")
                    if nested:
                        _append_markdown_structure(lines, nested, indent=indent + 1)
                else:
                    lines.append(f"{prefix}- item:")
                    _append_markdown_structure(lines, nested, indent=indent + 1)
            elif isinstance(item, (list, tuple, set)):
                lines.append(f"{prefix}- item:")
                _append_markdown_structure(lines, item, indent=indent + 1)
            else:
                lines.append(f"{prefix}- {_markdown_scalar(item)}")
        return
    if _has_markdown_value(value):
        lines.append(f"{prefix}- {_markdown_scalar(value)}")


def _extract_proposal_acceptance_criteria(proposals: dict[str, Any]) -> list[str]:
    criteria = _normalize_string_list(proposals.get("acceptance_criteria"))
    if criteria:
        return criteria
    design_feedback = proposals.get("design_feedback")
    if isinstance(design_feedback, dict):
        criteria = _normalize_string_list(design_feedback.get("acceptance_criteria"))
        if criteria:
            return criteria
    backlog_item = proposals.get("backlog_item")
    if isinstance(backlog_item, dict):
        criteria = _normalize_string_list(backlog_item.get("acceptance_criteria"))
        if criteria:
            return criteria
    backlog_items = proposals.get("backlog_items")
    if isinstance(backlog_items, list):
        for item in backlog_items:
            if not isinstance(item, dict):
                continue
            criteria = _normalize_string_list(item.get("acceptance_criteria"))
            if criteria:
                return criteria
    return []


def _extract_proposal_required_inputs(proposals: dict[str, Any]) -> list[str]:
    required_inputs = _normalize_string_list(proposals.get("required_inputs"))
    if required_inputs:
        return required_inputs
    design_feedback = proposals.get("design_feedback")
    if isinstance(design_feedback, dict):
        required_inputs = _normalize_string_list(design_feedback.get("required_inputs"))
        if required_inputs:
            return required_inputs
    return []


def _planner_relay_constraint_summaries(proposals: dict[str, Any], *, backlog_count: int) -> list[str]:
    summaries: list[str] = []
    if backlog_count > 0:
        summaries.append(f"완료 기준: planning을 닫을 수 있게 {backlog_count}건 확보")
    direct_criteria = _normalize_string_list(proposals.get("acceptance_criteria"))
    summaries.extend(
        f"완료 기준: {_collapse_whitespace(item)}"
        for item in direct_criteria
        if _collapse_whitespace(item)
    )
    return _dedupe_preserving_order(summaries)


def _normalize_constraint_point(
    value: str,
    *,
    canonical_prefix: str,
    source_prefixes: tuple[str, ...],
) -> str:
    normalized = _collapse_whitespace(value)
    if not normalized:
        return ""
    lowered = normalized.lower()
    for source_prefix in source_prefixes:
        for normalized_separator in (":", " ", " -"):
            source_with_separator = f"{source_prefix}{normalized_separator}"
            if lowered.startswith(source_with_separator):
                remainder = _collapse_whitespace(normalized[len(source_with_separator) :])
                return f"{canonical_prefix}: {remainder}" if remainder else canonical_prefix
        if lowered == source_prefix:
            return canonical_prefix
    return f"{canonical_prefix}: {normalized}"


def _constraint_point_body(value: str) -> str:
    normalized = _collapse_whitespace(value).lower()
    for prefix in (
        "완료 기준:",
        "완료기준:",
        "추가 입력:",
        "필수 입력:",
        "필수입력:",
        "필요 입력:",
        "필요입력:",
        "required input:",
        "required_inputs:",
        "acceptance criteria:",
        "acceptance_criteria:",
        "acceptancecriteria:",
    ):
        if normalized.startswith(prefix):
            remainder = _collapse_whitespace(normalized[len(prefix) :])
            return remainder if remainder else ""
        prefix_with_space = f"{prefix[:-1]} "
        if normalized.startswith(prefix_with_space):
            remainder = _collapse_whitespace(normalized[len(prefix_with_space) :])
            return remainder if remainder else ""
    return normalized


def _constraint_point_signature(value: str) -> str:
    normalized = _collapse_whitespace(value)
    if not normalized:
        return ""
    signature = _constraint_point_body(normalized)
    return signature if signature else normalized.lower()


def _designer_entry_point_label(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    labels = {
        "planning_route": "planning route",
        "message_readability": "message readability",
        "info_prioritization": "info prioritization",
        "ux_reopen": "ux reopen",
    }
    return labels.get(normalized, _collapse_whitespace(value))


def _design_feedback_priority_lines(value: Any) -> list[str]:
    if isinstance(value, dict):
        lines: list[str] = []
        lead = _collapse_whitespace(value.get("lead") or value.get("primary") or value.get("core") or "")
        summary = _collapse_whitespace(value.get("summary") or value.get("middle") or value.get("secondary") or "")
        defer = _collapse_whitespace(value.get("defer") or value.get("supporting") or value.get("background") or "")
        if lead:
            lines.append(f"핵심 레이어: {lead}")
        if summary:
            lines.append(f"요약 레이어: {summary}")
        if defer:
            lines.append(f"보조 레이어: {defer}")
        for key, item in value.items():
            normalized_key = str(key or "").strip().lower()
            if normalized_key in {"lead", "primary", "core", "summary", "middle", "secondary", "defer", "supporting", "background"}:
                continue
            text = _collapse_whitespace(item)
            if text:
                lines.append(f"정보 우선순위: {normalized_key}: {text}")
        return _dedupe_preserving_order(lines)
    if isinstance(value, (list, tuple, set)):
        return [f"정보 우선순위: {_collapse_whitespace(item)}" for item in value if _collapse_whitespace(item)]
    text = _collapse_whitespace(value)
    return [f"정보 우선순위: {text}"] if text else []


def _planning_support_role_entries(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    entries: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = _collapse_whitespace(item.get("role") or item.get("name") or "")
        if not role:
            continue
        rationale = ""
        for candidate in _normalize_string_list(item.get("support_rationale")):
            rationale = _collapse_whitespace(candidate)
            if rationale:
                break
        collaboration = ""
        for candidate in _normalize_string_list(item.get("collaboration_points")):
            collaboration = _collapse_whitespace(candidate)
            if collaboration:
                break
        entries.append(
            {
                "role": role,
                "rationale": rationale,
                "collaboration": collaboration,
            }
        )
    return entries


def _summarize_proposals(proposals: dict[str, Any]) -> str:
    if not isinstance(proposals, dict) or not proposals:
        return ""
    parts: list[str] = []
    backlog_items = proposals.get("backlog_items")
    if isinstance(backlog_items, list) and backlog_items:
        parts.append(f"backlog 후보 {len(backlog_items)}건")
    elif isinstance(proposals.get("backlog_item"), (dict, str)):
        parts.append("backlog 후보 1건")
    acceptance_criteria = _extract_proposal_acceptance_criteria(proposals)
    if acceptance_criteria:
        parts.append(f"완료 기준 {len(acceptance_criteria)}개")
    required_inputs = _extract_proposal_required_inputs(proposals)
    if required_inputs:
        parts.append(f"추가 입력 {len(required_inputs)}개 필요")
    if not parts:
        candidate_keys = [
            str(key).strip()
            for key in proposals.keys()
            if str(key).strip() not in {"routing", "acceptance_criteria", "required_inputs"}
        ]
        if candidate_keys:
            parts.append("제안 항목: " + ", ".join(candidate_keys[:3]))
    return " / ".join(parts)


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


def _compact_reference_items(values: list[str], *, limit: int = 3) -> list[str]:
    normalized = _dedupe_preserving_order(values)
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit] + [f"외 {len(normalized) - limit}건"]


def _first_meaningful_text(*values: Any) -> str:
    for value in values:
        normalized = str(value or "").strip()
        if normalized:
            return normalized
    return ""


def _format_count_summary(counts: dict[str, int], ordered_keys: list[str] | tuple[str, ...]) -> str:
    parts: list[str] = []
    for key in ordered_keys:
        value = int(counts.get(key) or 0)
        if value > 0:
            parts.append(f"{key}:{value}")
    return ", ".join(parts) if parts else "N/A"


def _decorate_sprint_report_title(title: str) -> str:
    normalized = str(title or "").strip()
    emoji_map = {
        "스프린트 시작": "🚀",
        "스프린트 TODO": "📝",
        "스프린트 완료": "✅",
        "스프린트 실패": "⚠️",
        "스프린트 종료": "🛑",
    }
    emoji = emoji_map.get(normalized)
    if not emoji or normalized.startswith(emoji):
        return normalized
    return f"{emoji} {normalized}"


def _is_terminal_todo_status(status: str) -> bool:
    return str(status or "").strip().lower() in {"completed", "committed", "failed", "blocked"}


class _NullDiscordClient:
    def __init__(self, *, client_name: str = ""):
        self.client_name = str(client_name or "").strip()
        self.sent_channels: list[tuple[str, str]] = []
        self.sent_dms: list[tuple[str, str]] = []

    def current_identity(self) -> dict[str, str]:
        return {}

    async def listen(self, _on_message, on_ready=None):
        if on_ready is not None:
            result = on_ready()
            if asyncio.iscoroutine(result):
                await result
        return None

    async def send_channel_message(self, channel_id, content):
        self.sent_channels.append((str(channel_id), str(content)))
        return DiscordMessage(
            message_id="null-channel",
            channel_id=str(channel_id),
            guild_id="",
            author_id="",
            author_name=self.client_name or "null",
            content=str(content),
            is_dm=False,
            mentions_bot=False,
            created_at=datetime.now(UTC),
        )

    async def send_dm(self, user_id, content):
        self.sent_dms.append((str(user_id), str(content)))
        return DiscordMessage(
            message_id="null-dm",
            channel_id="dm",
            guild_id=None,
            author_id="",
            author_name=self.client_name or "null",
            content=str(content),
            is_dm=True,
            mentions_bot=False,
            created_at=datetime.now(UTC),
        )

    async def close(self):
        return None


class TeamService:
    def __init__(
        self,
        workspace_root: str | Path,
        role: str,
        *,
        enable_discord_client: bool = True,
        relay_transport: str = RELAY_TRANSPORT_DISCORD,
    ):
        if role not in TEAM_ROLES:
            raise ValueError(f"Unsupported role: {role}")
        self.paths = RuntimePaths.from_root(workspace_root)
        self.paths.ensure_runtime_dirs()
        self.role = role
        normalized_relay_transport = str(relay_transport or "").strip().lower() or RELAY_TRANSPORT_DISCORD
        if normalized_relay_transport not in VALID_RELAY_TRANSPORTS:
            raise ValueError(
                "relay_transport must be one of: "
                + ", ".join(sorted(VALID_RELAY_TRANSPORTS))
            )
        self.relay_transport = normalized_relay_transport
        self.discord_config = load_discord_agents_config(self.paths.workspace_root)
        self.runtime_config = load_team_runtime_config(self.paths.workspace_root)
        self.agent_utilization_policy = load_agent_utilization_policy(self.paths.workspace_root)
        self.role_config = self.discord_config.get_role(role)
        self.action_executor = ActionExecutor(self.paths, self.runtime_config)
        self.role_runtime = RoleAgentRuntime(
            paths=self.paths,
            role=role,
            sprint_id=self.runtime_config.sprint_id,
            runtime_config=self.runtime_config.role_defaults[role],
        )
        self.intent_parser = IntentParserRuntime(
            paths=self.paths,
            sprint_id=self.runtime_config.sprint_id,
            runtime_config=self.runtime_config.role_defaults["orchestrator"],
        )
        self.backlog_sourcer = BacklogSourcingRuntime(
            paths=self.paths,
            sprint_id=self.runtime_config.sprint_id,
            runtime_config=self.runtime_config.role_defaults["orchestrator"],
        )
        self.version_controller_runtime = RoleAgentRuntime(
            paths=self.paths,
            role="version_controller",
            sprint_id=self.runtime_config.sprint_id,
            runtime_config=self.runtime_config.role_defaults["orchestrator"],
            agent_root=self.paths.internal_agent_root("version_controller"),
        )
        self._sourcer_report_config = self.discord_config.internal_agents.get("sourcer")
        self._sourcer_report_client: DiscordClient | None = None
        self._purge_request_scoped_role_output_files()
        self._role_runtime_cache: dict[tuple[str, str], RoleAgentRuntime] = {
            (role, self.runtime_config.sprint_id): self.role_runtime
        }
        self._active_request_ids: set[str] = set()
        self._active_request_ids_lock = asyncio.Lock()
        self._sprint_resume_lock = asyncio.Lock()
        self._role_resume_lock = asyncio.Lock()
        self._pending_role_request_resume_task: asyncio.Task[None] | None = None
        self._internal_relay_consumer_task: asyncio.Task[None] | None = None
        self._backlog_sourcing_lock = threading.Lock()
        self._last_backlog_sourcing_activity: dict[str, Any] = {}
        self._malformed_relay_log_times: dict[str, float] = {}
        self._last_sourcer_report_client_label = ""
        self._last_sourcer_report_reason = ""
        self._last_sourcer_report_category = ""
        self._last_sourcer_report_recovery_action = ""
        self._last_sourcer_report_failure_signature = ""
        self._last_sourcer_report_failure_logged_at = 0.0
        if enable_discord_client:
            self.discord_client = DiscordClient(
                token_env=self.role_config.token_env,
                expected_bot_id=self.role_config.bot_id,
                allowed_bot_author_ids=self.discord_config.trusted_bot_ids - {self.role_config.bot_id},
                always_listen_channel_ids={self.discord_config.relay_channel_id},
                transcript_log_file=self.paths.agent_discord_log(role),
                attachment_dir_resolver=self._resolve_message_attachment_root,
                client_name=role,
            )
        else:
            self.discord_client = _NullDiscordClient(client_name=role)
        if self.agent_utilization_policy.load_error:
            LOGGER.warning(self.agent_utilization_policy.load_error)

    def _agent_capability(self, role: str):
        return get_agent_capability(role, self.agent_utilization_policy)

    def _uses_manual_daily_sprint(self) -> bool:
        return str(self.runtime_config.sprint_start_mode or "").strip().lower() == "manual_daily"

    def _sprint_uses_manual_flow(self, sprint_state: dict[str, Any] | None = None) -> bool:
        if self._uses_manual_daily_sprint():
            return True
        state = dict(sprint_state or {})
        execution_mode = str(state.get("execution_mode") or "").strip().lower()
        if execution_mode == "manual":
            return True
        return str(state.get("trigger") or "").strip().lower() == "manual_start"

    def _load_active_sprint_state(self) -> dict[str, Any]:
        scheduler_state = self._load_scheduler_state()
        return self._load_sprint_state(str(scheduler_state.get("active_sprint_id") or ""))

    @staticmethod
    def _message_received_at(message: DiscordMessage | None) -> datetime | None:
        created_at = getattr(message, "created_at", None)
        if not isinstance(created_at, datetime):
            return None
        return normalize_runtime_datetime(created_at)

    def _request_started_at_hint(self, request_record: dict[str, Any]) -> datetime | None:
        for field_name in ("source_message_created_at", "created_at"):
            parsed = self._parse_datetime(str(request_record.get(field_name) or ""))
            if parsed is not None:
                return normalize_runtime_datetime(parsed)
        return None

    @staticmethod
    def _combine_envelope_scope_and_body(envelope: MessageEnvelope) -> str:
        parts: list[str] = []
        for raw_part in (str(envelope.scope or ""), str(envelope.body or "")):
            part = raw_part.strip()
            if part and part not in parts:
                parts.append(part)
        return "\n".join(parts).strip()

    @staticmethod
    def _normalize_kickoff_requirements(value: Any) -> list[str]:
        return _dedupe_preserving_order(_normalize_string_list(value))

    @staticmethod
    def _clean_kickoff_text(value: Any) -> str:
        return "\n".join(line.rstrip() for line in str(value or "").splitlines()).strip()

    @staticmethod
    def _parse_kickoff_text_sections(
        text: str,
    ) -> tuple[str, list[str]]:
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

    def _extract_manual_sprint_kickoff_payload(self, envelope: MessageEnvelope) -> dict[str, Any]:
        params = dict(envelope.params or {})
        milestone_title = self._extract_manual_sprint_milestone_title(envelope)
        kickoff_request_text = self._clean_kickoff_text(
            params.get("kickoff_request_text") or self._combine_envelope_scope_and_body(envelope)
        )
        kickoff_brief = self._clean_kickoff_text(params.get("kickoff_brief") or "")
        kickoff_requirements = self._normalize_kickoff_requirements(params.get("kickoff_requirements"))
        if not kickoff_brief and not kickoff_requirements and kickoff_request_text:
            parsed_brief, parsed_requirements = self._parse_kickoff_text_sections(kickoff_request_text)
            kickoff_brief = parsed_brief
            kickoff_requirements = parsed_requirements
        return {
            "milestone_title": milestone_title,
            "kickoff_brief": kickoff_brief,
            "kickoff_requirements": kickoff_requirements,
            "kickoff_request_text": kickoff_request_text,
            "kickoff_source_request_id": str(params.get("kickoff_source_request_id") or envelope.request_id or "").strip(),
            "kickoff_reference_artifacts": _dedupe_preserving_order(
                [str(item).strip() for item in (params.get("kickoff_reference_artifacts") or envelope.artifacts or []) if str(item).strip()]
            ),
        }

    def _extract_manual_sprint_milestone_title(self, envelope: MessageEnvelope) -> str:
        params = dict(envelope.params or {})
        explicit = str(params.get("milestone_title") or "").strip()
        if explicit:
            return explicit
        combined = self._combine_envelope_scope_and_body(envelope)
        if not combined:
            return ""
        matched = SPRINT_MILESTONE_PATTERN.search(combined)
        if matched:
            return str(matched.group("value") or "").strip()
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

    def _is_manual_sprint_start_request(self, envelope: MessageEnvelope) -> bool:
        combined = self._combine_envelope_scope_and_body(envelope)
        if str(dict(envelope.params or {}).get("sprint_control") or "").strip().lower() == "start":
            return True
        return is_manual_sprint_start_text(combined)

    def _is_manual_sprint_finalize_request(self, envelope: MessageEnvelope) -> bool:
        combined = self._combine_envelope_scope_and_body(envelope)
        if str(dict(envelope.params or {}).get("sprint_control") or "").strip().lower() == "finalize":
            return True
        return is_manual_sprint_finalize_text(combined)

    def _ensure_orchestrator_session_ready_for_sprint_start(self, envelope: MessageEnvelope) -> None:
        if self.role != "orchestrator":
            return
        if not self._is_manual_sprint_start_request(envelope):
            return
        try:
            self.role_runtime.session_manager.ensure_session()
        except Exception:
            LOGGER.exception("Failed to prepare orchestrator session workspace for sprint start request")

    def _build_manual_sprint_names(self, *, sprint_id: str, milestone_title: str) -> tuple[str, str]:
        display_name = build_daily_sprint_display_name(milestone_title)
        folder_name = build_sprint_artifact_folder_name(sprint_id)
        return display_name, folder_name

    def _build_idle_current_sprint_markdown(self) -> str:
        return "# Current Sprint\n\n- active sprint 없음\n"

    def _is_manual_sprint_cutoff_reached(self, sprint_state: dict[str, Any]) -> bool:
        if not self._sprint_uses_manual_flow(sprint_state):
            return False
        # 현재 정책: manual sprint는 시간 제약 없이 실행을 계속한다.
        return False

    def _build_manual_sprint_state(
        self,
        *,
        milestone_title: str,
        trigger: str,
        started_at: datetime | None = None,
        kickoff_brief: str = "",
        kickoff_requirements: list[str] | None = None,
        kickoff_request_text: str = "",
        kickoff_source_request_id: str = "",
        kickoff_reference_artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        started_at_dt = normalize_runtime_datetime(started_at)
        started_at_text = started_at_dt.isoformat()
        sprint_id = build_active_sprint_id(now=started_at_dt)
        sprint_name, folder_name = self._build_manual_sprint_names(
            sprint_id=sprint_id,
            milestone_title=milestone_title,
        )
        baseline = capture_git_baseline(self.paths.project_workspace_root)
        normalized_brief = self._clean_kickoff_text(kickoff_brief)
        normalized_requirements = self._normalize_kickoff_requirements(kickoff_requirements)
        normalized_request_text = self._clean_kickoff_text(kickoff_request_text)
        normalized_reference_artifacts = _dedupe_preserving_order(
            [str(item).strip() for item in (kickoff_reference_artifacts or []) if str(item).strip()]
        )[:12]
        return {
            "sprint_id": sprint_id,
            "sprint_name": sprint_name,
            "sprint_display_name": sprint_name,
            "sprint_folder": str(self.paths.sprint_artifact_dir(folder_name)),
            "sprint_folder_name": folder_name,
            "requested_milestone_title": str(milestone_title or "").strip(),
            "milestone_title": str(milestone_title or "").strip(),
            "kickoff_brief": normalized_brief,
            "kickoff_requirements": normalized_requirements,
            "kickoff_request_text": normalized_request_text,
            "kickoff_source_request_id": str(kickoff_source_request_id or "").strip(),
            "kickoff_reference_artifacts": normalized_reference_artifacts,
            "phase": "initial",
            "status": "planning",
            "trigger": trigger,
            "execution_mode": "manual",
            "started_at": started_at_text,
            "ended_at": "",
            "cutoff_at": build_sprint_cutoff_at(
                self.runtime_config.sprint_cutoff_time,
                now=started_at_dt,
            ).isoformat(),
            "initial_phase_ready_at": "",
            "last_planner_review_at": "",
            "wrap_up_requested_at": "",
            "selected_backlog_ids": [],
            "selected_items": [],
            "todos": [],
            "reference_artifacts": list(normalized_reference_artifacts),
            "planning_iterations": [],
            "commit_sha": "",
            "commit_shas": [],
            "commit_count": 0,
            "closeout_status": "",
            "uncommitted_paths": [],
            "version_control_status": "",
            "version_control_sha": "",
            "version_control_paths": [],
            "version_control_message": "",
            "version_control_error": "",
            "auto_commit_status": "",
            "auto_commit_sha": "",
            "auto_commit_paths": [],
            "auto_commit_message": "",
            "report_path": "",
            "git_baseline": baseline,
            "resume_from_checkpoint_requested_at": "",
            "last_resume_checkpoint_todo_id": "",
            "last_resume_checkpoint_status": "",
        }

    @staticmethod
    def _is_internal_sprint_request(request_record: dict[str, Any]) -> bool:
        params = dict(request_record.get("params") or {})
        return str(params.get("_teams_kind") or "").strip() == "sprint_internal"

    @staticmethod
    def _is_sprint_planning_request(request_record: dict[str, Any]) -> bool:
        params = dict(request_record.get("params") or {})
        return (
            str(params.get("_teams_kind") or "").strip() == "sprint_internal"
            and str(request_record.get("intent") or "").strip().lower() == "plan"
            and bool(str(params.get("sprint_phase") or "").strip())
        )

    @staticmethod
    def _initial_phase_step(request_record: dict[str, Any]) -> str:
        params = dict(request_record.get("params") or {})
        step = str(params.get("initial_phase_step") or "").strip().lower()
        return step if step in INITIAL_PHASE_STEPS else ""

    def _is_initial_phase_planner_request(self, request_record: dict[str, Any]) -> bool:
        if not self._is_sprint_planning_request(request_record):
            return False
        params = dict(request_record.get("params") or {})
        if str(params.get("sprint_phase") or "").strip().lower() != "initial":
            return False
        return bool(self._initial_phase_step(request_record))

    @staticmethod
    def _initial_phase_step_title(step: str) -> str:
        normalized = str(step or "").strip().lower()
        return INITIAL_PHASE_STEP_TITLES.get(normalized, normalized or "initial planning")

    @staticmethod
    def _initial_phase_step_position(step: str) -> int:
        normalized = str(step or "").strip().lower()
        try:
            return INITIAL_PHASE_STEPS.index(normalized) + 1
        except ValueError:
            return 0

    def _next_initial_phase_step(self, step: str) -> str:
        position = self._initial_phase_step_position(step)
        if position <= 0 or position >= len(INITIAL_PHASE_STEPS):
            return ""
        return INITIAL_PHASE_STEPS[position]

    def _initial_phase_step_instruction(self, step: str) -> str:
        normalized = str(step or "").strip().lower()
        if normalized == INITIAL_PHASE_STEP_MILESTONE_REFINEMENT:
            return (
                "Preserve the original kickoff brief, kickoff requirements, and kickoff reference artifacts first. "
                "Refine the sprint milestone title and execution framing separately in milestone-facing docs such as milestone.md. "
                "Do not select backlog items or execution todos in this step."
            )
        if normalized == INITIAL_PHASE_STEP_ARTIFACT_SYNC:
            return (
                "Update the sprint plan/spec/iteration artifacts so they reflect the latest refined milestone. "
                "Do not set planned_in_sprint_id, selected_in_sprint_id, or execution todos in this step."
            )
        if normalized == INITIAL_PHASE_STEP_BACKLOG_DEFINITION:
            return (
                "Define sprint-relevant backlog from the current milestone, kickoff requirements, and spec before any selection. "
                "Create or reopen backlog items when the persisted queue does not fully cover the sprint contract. "
                "Backlog zero is invalid in this step. Each backlog item must include concrete acceptance criteria plus origin trace "
                "for milestone_ref, requirement_refs, and spec_refs."
            )
        if normalized == INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION:
            return (
                "Prioritize only the already-defined sprint-relevant backlog work and persist priority_rank plus milestone_title. "
                "Do not create execution todos in this step, and do not proceed if sprint-relevant backlog is still zero."
            )
        if normalized == INITIAL_PHASE_STEP_TODO_FINALIZATION:
            return (
                "Finalize the execution-ready todo set for this sprint. "
                "Persist planned_in_sprint_id for the chosen backlog items and leave the prioritized todo set ready to run."
            )
        return ""

    @staticmethod
    def _is_sourcer_review_request(request_record: dict[str, Any]) -> bool:
        params = dict(request_record.get("params") or {})
        return str(params.get("_teams_kind") or "").strip() == "sourcer_review"

    @staticmethod
    def _is_blocked_backlog_review_request(request_record: dict[str, Any]) -> bool:
        params = dict(request_record.get("params") or {})
        return str(params.get("_teams_kind") or "").strip() == "blocked_backlog_review"

    @classmethod
    def _is_planner_backlog_review_request(cls, request_record: dict[str, Any]) -> bool:
        return cls._is_sourcer_review_request(request_record) or cls._is_blocked_backlog_review_request(request_record)

    @staticmethod
    def _is_terminal_internal_request_status(status: str) -> bool:
        return str(status or "").strip().lower() in {
            "completed",
            "committed",
            "failed",
            "blocked",
            "cancelled",
        }

    def _inspect_task_version_control_state(self, request_record: dict[str, Any]) -> dict[str, Any]:
        baseline = dict(request_record.get("git_baseline") or {})
        repo_root, changed_paths = collect_sprint_owned_paths(self.paths.project_workspace_root, baseline)
        if repo_root is None:
            return {
                "status": "no_repo",
                "repo_root": "",
                "changed_paths": [],
                "message": "git repository를 찾을 수 없습니다.",
            }
        if changed_paths:
            return {
                "status": "pending_changes",
                "repo_root": str(repo_root),
                "changed_paths": changed_paths,
                "message": "현재 task 소유 변경이 아직 commit되지 않았습니다.",
            }
        return {
            "status": "no_changes",
            "repo_root": str(repo_root),
            "changed_paths": [],
            "message": "현재 task 소유 변경 파일이 없습니다.",
        }

    def _record_internal_visited_role(self, request_record: dict[str, Any], role: str) -> None:
        if not self._is_internal_sprint_request(request_record):
            return
        normalized = str(role or "").strip()
        if not normalized:
            return
        visited_roles = [
            str(item).strip()
            for item in (request_record.get("visited_roles") or [])
            if str(item).strip()
        ]
        if normalized not in visited_roles:
            visited_roles.append(normalized)
        request_record["visited_roles"] = visited_roles

    @staticmethod
    def _default_workflow_state() -> dict[str, Any]:
        return {
            "contract_version": WORKFLOW_CONTRACT_VERSION,
            "phase": WORKFLOW_PHASE_PLANNING,
            "step": WORKFLOW_STEP_PLANNER_DRAFT,
            "phase_owner": "planner",
            "phase_status": "active",
            "planning_pass_count": 0,
            "planning_pass_limit": 2,
            "planning_final_owner": "planner",
            "reopen_source_role": "",
            "reopen_category": "",
            "review_cycle_count": 0,
            "review_cycle_limit": DEFAULT_WORKFLOW_REVIEW_CYCLE_LIMIT,
        }

    def _initial_workflow_state_for_internal_request(self) -> dict[str, Any]:
        state = dict(self._default_workflow_state())
        state["review_cycle_limit"] = max(
            1,
            int(self.agent_utilization_policy.implementation_review_cycle_limit or state["review_cycle_limit"]),
        )
        return state

    def _request_workflow_state(self, request_record: dict[str, Any]) -> dict[str, Any]:
        if self._is_sprint_planning_request(request_record):
            return {}
        params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
        raw = params.get("workflow")
        if not isinstance(raw, dict):
            if self._is_internal_sprint_request(request_record):
                inferred = self._infer_legacy_internal_workflow_state(request_record)
                if inferred:
                    return inferred
            return {}
        state = self._default_workflow_state()
        state["contract_version"] = int(raw.get("contract_version") or WORKFLOW_CONTRACT_VERSION)
        phase = str(raw.get("phase") or state["phase"]).strip().lower()
        step = str(raw.get("step") or state["step"]).strip().lower()
        phase_owner = str(raw.get("phase_owner") or state["phase_owner"]).strip().lower()
        phase_status = str(raw.get("phase_status") or state["phase_status"]).strip().lower()
        planning_final_owner = str(raw.get("planning_final_owner") or state["planning_final_owner"]).strip().lower()
        reopen_source_role = str(raw.get("reopen_source_role") or "").strip().lower()
        reopen_category = str(raw.get("reopen_category") or "").strip().lower()
        if phase in WORKFLOW_PHASES:
            state["phase"] = phase
        if step in WORKFLOW_STEPS:
            state["step"] = step
        if phase_owner in TEAM_ROLES or phase_owner == "version_controller":
            state["phase_owner"] = phase_owner
        if phase_status in {"active", "finalizing", "blocked", "completed"}:
            state["phase_status"] = phase_status
        if planning_final_owner in TEAM_ROLES:
            state["planning_final_owner"] = planning_final_owner
        if reopen_category in WORKFLOW_REOPEN_CATEGORIES:
            state["reopen_category"] = reopen_category
        state["reopen_source_role"] = reopen_source_role
        state["planning_pass_count"] = max(0, int(raw.get("planning_pass_count") or state["planning_pass_count"]))
        state["planning_pass_limit"] = max(1, int(raw.get("planning_pass_limit") or state["planning_pass_limit"]))
        state["review_cycle_count"] = max(0, int(raw.get("review_cycle_count") or state["review_cycle_count"]))
        state["review_cycle_limit"] = max(1, int(raw.get("review_cycle_limit") or state["review_cycle_limit"]))
        return state

    def _infer_legacy_internal_workflow_state(self, request_record: dict[str, Any]) -> dict[str, Any]:
        current_role = str(
            request_record.get("current_role")
            or request_record.get("next_role")
            or request_record.get("owner_role")
            or ""
        ).strip().lower()
        if current_role not in {"planner", "designer", "architect"}:
            return {}
        state = self._default_workflow_state()
        advisory_reports = 0
        developer_reports = 0
        qa_reports = 0
        for event in request_record.get("events") or []:
            if str(event.get("event_type") or "").strip().lower() != "role_report":
                continue
            actor = str(event.get("actor") or "").strip().lower()
            if actor in PLANNING_ADVISORY_ROLES:
                advisory_reports += 1
            elif actor == "developer":
                developer_reports += 1
            elif actor == "qa":
                qa_reports += 1
        state["planning_pass_count"] = min(advisory_reports, state["planning_pass_limit"])
        if qa_reports:
            return {}
        if developer_reports:
            return {}
        if current_role == "planner":
            state["step"] = WORKFLOW_STEP_PLANNER_FINALIZE if advisory_reports else WORKFLOW_STEP_PLANNER_DRAFT
            state["phase_owner"] = "planner"
            state["phase_status"] = "finalizing" if advisory_reports else "active"
            return state
        state["step"] = WORKFLOW_STEP_PLANNER_ADVISORY
        state["phase_owner"] = current_role
        state["phase_status"] = "active"
        if state["planning_pass_count"] == 0:
            state["planning_pass_count"] = 1
        return state

    def _set_request_workflow_state(self, request_record: dict[str, Any], workflow_state: dict[str, Any]) -> None:
        params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
        params["workflow"] = dict(workflow_state or self._default_workflow_state())
        request_record["params"] = params

    @staticmethod
    def _workflow_transition(result: dict[str, Any]) -> dict[str, Any]:
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        raw = proposals.get("workflow_transition")
        if not isinstance(raw, dict):
            return {
                "outcome": "",
                "target_phase": "",
                "target_step": "",
                "requested_role": "",
                "reopen_category": "",
                "reason": "",
                "unresolved_items": [],
                "finalize_phase": False,
            }
        outcome = str(raw.get("outcome") or "").strip().lower()
        target_phase = str(raw.get("target_phase") or "").strip().lower()
        target_step = str(raw.get("target_step") or "").strip().lower()
        requested_role = str(raw.get("requested_role") or "").strip().lower()
        reopen_category = str(raw.get("reopen_category") or "").strip().lower()
        unresolved_items = [
            str(item).strip()
            for item in (raw.get("unresolved_items") or [])
            if str(item).strip()
        ] if isinstance(raw.get("unresolved_items"), list) else []
        return {
            "outcome": outcome if outcome in {"continue", "advance", "reopen", "block", "complete"} else "",
            "target_phase": target_phase if target_phase in WORKFLOW_PHASES else "",
            "target_step": target_step if target_step in WORKFLOW_STEPS else "",
            "requested_role": requested_role if requested_role in TEAM_ROLES or requested_role == "version_controller" else "",
            "reopen_category": reopen_category if reopen_category in WORKFLOW_REOPEN_CATEGORIES else "",
            "reason": str(raw.get("reason") or "").strip(),
            "unresolved_items": unresolved_items,
            "finalize_phase": bool(raw.get("finalize_phase")),
        }

    @staticmethod
    def _workflow_transition_requests_explicit_continuation(transition: dict[str, Any]) -> bool:
        outcome = str(transition.get("outcome") or "").strip().lower()
        reopen_category = str(transition.get("reopen_category") or "").strip().lower()
        if outcome in {"advance", "continue"}:
            return True
        return outcome == "reopen" and reopen_category in WORKFLOW_REOPEN_CATEGORIES

    @staticmethod
    def _workflow_transition_requests_validation_handoff(transition: dict[str, Any]) -> bool:
        target_step = str(transition.get("target_step") or "").strip().lower()
        target_phase = str(transition.get("target_phase") or "").strip().lower()
        requested_role = str(transition.get("requested_role") or "").strip().lower()
        outcome = str(transition.get("outcome") or "").strip().lower()
        return (
            target_step == WORKFLOW_STEP_QA_VALIDATION
            or requested_role == "qa"
            or outcome == "complete"
            or target_phase == WORKFLOW_PHASE_VALIDATION
        )

    @staticmethod
    def _workflow_review_cycle_limit_reached(workflow_state: dict[str, Any]) -> bool:
        review_cycle_count = max(0, int((workflow_state or {}).get("review_cycle_count") or 0))
        review_cycle_limit = max(1, int((workflow_state or {}).get("review_cycle_limit") or DEFAULT_WORKFLOW_REVIEW_CYCLE_LIMIT))
        return review_cycle_count >= review_cycle_limit

    @staticmethod
    def _workflow_reason(result: dict[str, Any], transition: dict[str, Any], default: str) -> str:
        return (
            str(transition.get("reason") or "").strip()
            or str(result.get("summary") or "").strip()
            or default
        )

    def _workflow_request_context_text(self, request_record: dict[str, Any]) -> str:
        params = dict(request_record.get("params") or {})
        backlog_id = str(params.get("backlog_id") or request_record.get("backlog_id") or "").strip()
        backlog_item = self._load_backlog_item(backlog_id) if backlog_id else {}
        parts = [
            str(request_record.get("scope") or "").strip(),
            str(request_record.get("body") or "").strip(),
            str(backlog_item.get("title") or "").strip(),
            str(backlog_item.get("summary") or "").strip(),
            *[
                str(item).strip()
                for item in (backlog_item.get("acceptance_criteria") or [])
                if str(item).strip()
            ],
        ]
        return " ".join(part for part in parts if part)

    def _workflow_should_close_in_planning(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        current_role: str,
        transition: dict[str, Any],
    ) -> bool:
        if current_role != "planner":
            return False
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return False
        step = str(workflow_state.get("step") or "").strip().lower()
        if step not in {WORKFLOW_STEP_PLANNER_DRAFT, WORKFLOW_STEP_PLANNER_FINALIZE}:
            return False
        requested_role = str(transition.get("requested_role") or "").strip().lower()
        if requested_role in PLANNING_ADVISORY_ROLES:
            return False
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        has_planning_contract = any(
            proposals.get(key) is not None
            for key in ("root_cause_contract", "todo_brief", "planning_note")
        )
        transition_outcome = str(transition.get("outcome") or "").strip().lower()
        explicit_continuation = self._workflow_transition_requests_explicit_continuation(transition)
        explicit_planning_close = transition_outcome == "complete" or (
            bool(transition.get("finalize_phase")) and not explicit_continuation
        )
        artifacts = [
            str(item).strip()
            for item in (result.get("artifacts") or [])
            if str(item).strip()
        ]
        planning_surface_only = bool(artifacts) and all(
            self._is_planning_surface_artifact_hint(item) for item in artifacts
        )
        if not has_planning_contract:
            request_text = self._workflow_request_context_text(request_record)
            if self._request_indicates_execution(
                intent=str(request_record.get("intent") or "").strip().lower(),
                text=request_text,
            ) and not (explicit_planning_close and planning_surface_only):
                return False
        if not artifacts and not has_planning_contract:
            return False
        if artifacts and not planning_surface_only:
            return False
        return True

    def _enforce_workflow_role_report_contract(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return result
        role = str(result.get("role") or request_record.get("current_role") or "").strip().lower()
        if role == "planner":
            planner_owned_artifacts, missing_required, missing_files = self._workflow_planner_doc_contract_violation(
                request_record,
                result,
            )
            if not planner_owned_artifacts or missing_required or missing_files:
                normalized_result = dict(result)
                proposals = dict(normalized_result.get("proposals") or {})
                unresolved_items = []
                if not planner_owned_artifacts:
                    unresolved_items.append(
                        "planner는 planning 완료 전에 spec/current_sprint/todo_backlog 등 planner-owned 문서 경로를 artifacts에 남길 것"
                    )
                if missing_required:
                    unresolved_items.extend(
                        f"QA reopen으로 요구된 planning 문서 누락: {artifact}"
                        for artifact in missing_required
                    )
                if missing_files:
                    unresolved_items.extend(
                        f"artifact로 보고한 planner 문서가 실제 파일로 확인되지 않음: {artifact}"
                        for artifact in missing_files
                    )
                proposals["workflow_transition"] = {
                    "outcome": "reopen",
                    "target_phase": WORKFLOW_PHASE_PLANNING,
                    "target_step": WORKFLOW_STEP_PLANNER_FINALIZE,
                    "requested_role": "planner",
                    "reopen_category": "scope",
                    "reason": (
                        "planner는 관련 spec/todo/current_sprint 문서를 실제로 갱신한 근거 없이 "
                        "planning 완료를 보고할 수 없습니다."
                    ),
                    "unresolved_items": unresolved_items,
                    "finalize_phase": False,
                }
                normalized_result["proposals"] = proposals
                normalized_result["status"] = "blocked"
                normalized_result["summary"] = (
                    "planner 문서 계약이 닫히지 않았습니다. spec/todo/current_sprint 문서를 실제로 갱신하고 "
                    "artifact로 남긴 뒤 다시 planner finalize를 수행해야 합니다."
                )
                existing_error = str(normalized_result.get("error") or "").strip()
                contract_error = "planner 문서 갱신 근거가 부족합니다."
                normalized_result["error"] = (
                    f"{existing_error}; {contract_error}"
                    if existing_error and contract_error not in existing_error
                    else (existing_error or contract_error)
                )
                return normalize_role_payload(normalized_result)
            return result
        if role == "qa" and self._qa_result_is_runtime_sync_anomaly(request_record, result):
            normalized_result = dict(result)
            proposals = dict(normalized_result.get("proposals") or {})
            sync_summary = (
                "QA 검증은 구현/테스트 기준으로 통과했고 planner-owned 상태 문서 drift는 "
                "runtime이 canonical request/todo state로 다시 동기화합니다."
            )
            proposals["workflow_transition"] = {
                "outcome": "complete",
                "target_phase": "",
                "target_step": "",
                "requested_role": "",
                "reopen_category": "",
                "reason": sync_summary,
                "unresolved_items": [],
                "finalize_phase": False,
            }
            normalized_result["proposals"] = proposals
            normalized_result["status"] = "completed"
            normalized_result["summary"] = sync_summary
            normalized_result["error"] = ""
            return normalize_role_payload(normalized_result)
        if role == "qa" and self._qa_result_requires_planner_reopen(request_record, result):
            normalized_result = dict(result)
            proposals = dict(normalized_result.get("proposals") or {})
            existing_transition = self._workflow_transition(normalized_result)
            unresolved_items = [
                str(item).strip()
                for item in (existing_transition.get("unresolved_items") or [])
                if str(item).strip()
            ]
            for required in (
                "spec.md 기준 요구사항/공통 정책을 다시 정렬할 것",
                "todo_backlog.md와 iteration_log.md에 QA 검증 실패 원인을 planner 관점으로 반영할 것",
            ):
                if required not in unresolved_items:
                    unresolved_items.append(required)
            proposals["workflow_transition"] = {
                "outcome": "reopen",
                "target_phase": WORKFLOW_PHASE_PLANNING,
                "target_step": WORKFLOW_STEP_PLANNER_FINALIZE,
                "requested_role": "planner",
                "reopen_category": "scope",
                "reason": (
                    str(existing_transition.get("reason") or "").strip()
                    or "QA가 spec/todo planning contract mismatch를 확인해 planner 재정렬이 필요합니다."
                ),
                "unresolved_items": unresolved_items,
                "finalize_phase": False,
            }
            normalized_result["proposals"] = proposals
            if str(normalized_result.get("status") or "").strip().lower() not in {"blocked", "failed"}:
                normalized_result["status"] = "completed"
            return normalize_role_payload(normalized_result)
        if role not in {"architect", "developer"}:
            return result
        if str(workflow_state.get("phase") or "").strip().lower() != WORKFLOW_PHASE_IMPLEMENTATION:
            return result
        planner_owned_artifacts = [
            str(item).strip()
            for item in (result.get("artifacts") or [])
            if self._is_planner_owned_surface_artifact_hint(item)
        ]
        if not planner_owned_artifacts:
            return result
        normalized_result = dict(result)
        normalized_result["artifacts"] = [
            str(item).strip()
            for item in (normalized_result.get("artifacts") or [])
            if str(item).strip() and not self._is_planner_owned_surface_artifact_hint(item)
        ]
        insights = [
            str(item).strip()
            for item in (normalized_result.get("insights") or [])
            if str(item).strip()
        ]
        runtime_note = (
            "runtime이 implementation artifact에서 planner-owned 문서를 제외했습니다: "
            f"{', '.join(planner_owned_artifacts[:3])}"
        )
        if runtime_note not in insights:
            insights.append(runtime_note)
        normalized_result["insights"] = insights
        return normalize_role_payload(normalized_result)

    def _workflow_review_cycle_limit_block_decision(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
        category: str = "",
    ) -> dict[str, Any]:
        review_cycle_count = max(0, int((workflow_state or {}).get("review_cycle_count") or 0))
        review_cycle_limit = max(1, int((workflow_state or {}).get("review_cycle_limit") or DEFAULT_WORKFLOW_REVIEW_CYCLE_LIMIT))
        limit_summary = (
            f"architect review가 {review_cycle_count}회 연속 미통과하여 review cycle limit {review_cycle_limit}에 도달했습니다."
        )
        combined_summary = " ".join(
            part
            for part in (
                limit_summary,
                str(reason or "").strip(),
            )
            if str(part).strip()
        ).strip()
        return self._workflow_terminal_block_decision(
            workflow_state,
            summary=combined_summary or limit_summary,
            category=category,
        )

    def _workflow_routing_context(
        self,
        next_role: str,
        *,
        workflow_state: dict[str, Any],
        reason: str,
        requested_role: str = "",
        matched_signals: list[str] | None = None,
    ) -> dict[str, Any]:
        return self._build_routing_context(
            next_role,
            reason=reason,
            requested_role=requested_role,
            selection_source=WORKFLOW_SELECTION_SOURCE,
            matched_signals=matched_signals or [],
            policy_source=WORKFLOW_POLICY_SOURCE,
            routing_phase=str(workflow_state.get("phase") or ""),
            request_state_class=str(workflow_state.get("step") or ""),
        )

    def _workflow_route_decision(
        self,
        next_role: str,
        *,
        workflow_state: dict[str, Any],
        reason: str,
        requested_role: str = "",
        matched_signals: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "next_role": next_role,
            "routing_context": self._workflow_routing_context(
                next_role,
                workflow_state=workflow_state,
                reason=reason,
                requested_role=requested_role,
                matched_signals=matched_signals,
            ),
            "workflow_state": workflow_state,
        }

    def _workflow_terminal_block_decision(
        self,
        workflow_state: dict[str, Any],
        *,
        summary: str,
        category: str = "",
    ) -> dict[str, Any]:
        updated_state = dict(workflow_state or self._default_workflow_state())
        updated_state["phase_status"] = "blocked"
        if category in WORKFLOW_REOPEN_CATEGORIES:
            updated_state["reopen_category"] = category
        return {
            "next_role": "",
            "routing_context": {},
            "workflow_state": updated_state,
            "terminal_status": "blocked",
            "terminal_summary": summary,
        }

    def _workflow_route_to_planner_finalize(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
        category: str = "",
    ) -> dict[str, Any]:
        updated_state = dict(workflow_state or self._default_workflow_state())
        updated_state["phase"] = WORKFLOW_PHASE_PLANNING
        updated_state["step"] = WORKFLOW_STEP_PLANNER_FINALIZE
        updated_state["phase_owner"] = "planner"
        updated_state["phase_status"] = "finalizing"
        if category in WORKFLOW_REOPEN_CATEGORIES:
            updated_state["reopen_category"] = category
        return self._workflow_route_decision(
            "planner",
            workflow_state=updated_state,
            reason=reason,
            requested_role="planner",
            matched_signals=["workflow:planner_finalize"],
        )

    def _workflow_route_to_planning_advisory(
        self,
        workflow_state: dict[str, Any],
        *,
        role: str,
        reason: str,
        category: str = "",
    ) -> dict[str, Any]:
        updated_state = dict(workflow_state or self._default_workflow_state())
        if updated_state["planning_pass_count"] >= updated_state["planning_pass_limit"]:
            return self._workflow_route_to_planner_finalize(
                updated_state,
                reason="planning advisory pass 한도에 도달해 planner finalization으로 되돌립니다.",
                category=category,
            )
        updated_state["phase"] = WORKFLOW_PHASE_PLANNING
        updated_state["step"] = WORKFLOW_STEP_PLANNER_ADVISORY
        updated_state["phase_owner"] = role
        updated_state["phase_status"] = "active"
        updated_state["planning_pass_count"] = int(updated_state.get("planning_pass_count") or 0) + 1
        if category in WORKFLOW_REOPEN_CATEGORIES:
            updated_state["reopen_category"] = category
        return self._workflow_route_decision(
            role,
            workflow_state=updated_state,
            reason=reason,
            requested_role=role,
            matched_signals=[f"workflow:planning_advisory:{role}"],
        )

    def _workflow_route_to_architect_guidance(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
        category: str = "",
    ) -> dict[str, Any]:
        updated_state = dict(workflow_state or self._default_workflow_state())
        updated_state["phase"] = WORKFLOW_PHASE_IMPLEMENTATION
        updated_state["step"] = WORKFLOW_STEP_ARCHITECT_GUIDANCE
        updated_state["phase_owner"] = "architect"
        updated_state["phase_status"] = "active"
        if category in WORKFLOW_REOPEN_CATEGORIES:
            updated_state["reopen_category"] = category
        return self._workflow_route_decision(
            "architect",
            workflow_state=updated_state,
            reason=reason,
            requested_role="architect",
            matched_signals=["workflow:architect_guidance"],
        )

    def _workflow_route_to_developer_build(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
        step: str = WORKFLOW_STEP_DEVELOPER_BUILD,
        category: str = "",
    ) -> dict[str, Any]:
        updated_state = dict(workflow_state or self._default_workflow_state())
        updated_state["phase"] = WORKFLOW_PHASE_IMPLEMENTATION
        updated_state["step"] = step
        updated_state["phase_owner"] = "developer"
        updated_state["phase_status"] = "active"
        if category in WORKFLOW_REOPEN_CATEGORIES:
            updated_state["reopen_category"] = category
        return self._workflow_route_decision(
            "developer",
            workflow_state=updated_state,
            reason=reason,
            requested_role="developer",
            matched_signals=[f"workflow:{step}"],
        )

    def _workflow_route_to_architect_review(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
        category: str = "",
    ) -> dict[str, Any]:
        updated_state = dict(workflow_state or self._default_workflow_state())
        updated_state["phase"] = WORKFLOW_PHASE_IMPLEMENTATION
        updated_state["step"] = WORKFLOW_STEP_ARCHITECT_REVIEW
        updated_state["phase_owner"] = "architect"
        updated_state["phase_status"] = "active"
        updated_state["review_cycle_count"] = int(updated_state.get("review_cycle_count") or 0) + 1
        if category in WORKFLOW_REOPEN_CATEGORIES:
            updated_state["reopen_category"] = category
        return self._workflow_route_decision(
            "architect",
            workflow_state=updated_state,
            reason=reason,
            requested_role="architect",
            matched_signals=["workflow:architect_review"],
        )

    def _workflow_route_to_qa(
        self,
        workflow_state: dict[str, Any],
        *,
        reason: str,
    ) -> dict[str, Any]:
        updated_state = dict(workflow_state or self._default_workflow_state())
        updated_state["phase"] = WORKFLOW_PHASE_VALIDATION
        updated_state["step"] = WORKFLOW_STEP_QA_VALIDATION
        updated_state["phase_owner"] = "qa"
        updated_state["phase_status"] = "active"
        return self._workflow_route_decision(
            "qa",
            workflow_state=updated_state,
            reason=reason,
            requested_role="qa",
            matched_signals=["workflow:qa_validation"],
        )

    def _workflow_complete_decision(
        self,
        workflow_state: dict[str, Any],
        *,
        summary: str,
    ) -> dict[str, Any]:
        updated_state = dict(workflow_state or self._default_workflow_state())
        updated_state["phase"] = WORKFLOW_PHASE_CLOSEOUT
        updated_state["step"] = WORKFLOW_STEP_CLOSEOUT
        updated_state["phase_owner"] = "version_controller"
        updated_state["phase_status"] = "completed"
        return {
            "next_role": "",
            "routing_context": {},
            "workflow_state": updated_state,
            "terminal_summary": summary,
        }

    def _workflow_reopen_decision(
        self,
        workflow_state: dict[str, Any],
        *,
        current_role: str,
        category: str,
        reason: str,
    ) -> dict[str, Any]:
        updated_state = dict(workflow_state or self._default_workflow_state())
        updated_state["reopen_source_role"] = current_role
        updated_state["reopen_category"] = category if category in WORKFLOW_REOPEN_CATEGORIES else ""
        if category == "scope":
            return self._workflow_route_to_planner_finalize(updated_state, reason=reason, category=category)
        if category == "ux":
            return self._workflow_route_to_planning_advisory(
                updated_state,
                role="designer",
                reason=reason,
                category=category,
            )
        if category == "architecture":
            if current_role == "qa":
                return self._workflow_route_to_architect_review(updated_state, reason=reason, category=category)
            return self._workflow_route_to_architect_guidance(updated_state, reason=reason, category=category)
        if category in {"implementation", "verification"}:
            current_step = str(updated_state.get("step") or "").strip().lower()
            step = (
                WORKFLOW_STEP_DEVELOPER_REVISION
                if current_role == "qa" or current_step in {WORKFLOW_STEP_ARCHITECT_REVIEW, WORKFLOW_STEP_DEVELOPER_REVISION}
                else WORKFLOW_STEP_DEVELOPER_BUILD
            )
            return self._workflow_route_to_developer_build(
                updated_state,
                reason=reason,
                step=step,
                category=category,
            )
        return self._workflow_route_to_planner_finalize(updated_state, reason=reason, category=category)

    def _derive_workflow_routing_decision(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        sender_role: str,
    ) -> dict[str, Any] | None:
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return None
        current_role = str(result.get("role") or sender_role or request_record.get("current_role") or "").strip().lower()
        transition = self._workflow_transition(result)
        outcome = str(transition.get("outcome") or "").strip().lower()
        requested_role = str(transition.get("requested_role") or "").strip().lower()
        reopen_category = str(transition.get("reopen_category") or "").strip().lower()
        reason = self._workflow_reason(result, transition, "workflow step을 계속 진행합니다.")
        step = str(workflow_state.get("step") or "").strip().lower()

        if outcome == "block":
            return self._workflow_terminal_block_decision(
                workflow_state,
                summary=reason,
                category=reopen_category,
            )

        if step in {WORKFLOW_STEP_PLANNER_DRAFT, WORKFLOW_STEP_PLANNER_FINALIZE}:
            if current_role != "planner":
                return self._workflow_route_to_planner_finalize(
                    workflow_state,
                    reason="planning owner인 planner가 최종 정리를 이어갑니다.",
                )
            if outcome == "reopen" and (
                requested_role == "planner"
                or str(transition.get("target_step") or "").strip().lower() == WORKFLOW_STEP_PLANNER_FINALIZE
                or reopen_category == "scope"
            ):
                return self._workflow_route_to_planner_finalize(
                    workflow_state,
                    reason=reason or "planner가 planning 문서/contract를 다시 정리합니다.",
                    category=reopen_category,
                )
            if requested_role in PLANNING_ADVISORY_ROLES and outcome in {"continue", "reopen", ""}:
                if int(workflow_state.get("planning_pass_count") or 0) >= int(workflow_state.get("planning_pass_limit") or 0):
                    return self._workflow_terminal_block_decision(
                        workflow_state,
                        summary="planning advisory pass 한도에 도달해 planner가 더 이상 specialist pass를 열 수 없습니다.",
                        category=reopen_category,
                    )
                return self._workflow_route_to_planning_advisory(
                    workflow_state,
                    role=requested_role,
                    reason=reason,
                    category=reopen_category,
                )
            if self._workflow_should_close_in_planning(
                request_record,
                result,
                current_role=current_role,
                transition=transition,
            ):
                return self._workflow_complete_decision(
                    workflow_state,
                    summary=reason or "planner가 문서/계획 surface를 planning 단계에서 마무리했습니다.",
                )
            return self._workflow_route_to_architect_guidance(
                workflow_state,
                reason=reason or "planning이 정리되어 implementation guidance를 시작합니다.",
                category=reopen_category,
            )

        if step == WORKFLOW_STEP_PLANNER_ADVISORY:
            return self._workflow_route_to_planner_finalize(
                workflow_state,
                reason=reason or "specialist advisory를 planner가 반영합니다.",
                category=reopen_category,
            )

        if step == WORKFLOW_STEP_ARCHITECT_GUIDANCE:
            if outcome == "reopen":
                return self._workflow_reopen_decision(
                    workflow_state,
                    current_role=current_role or "architect",
                    category=reopen_category,
                    reason=reason,
                )
            return self._workflow_route_to_developer_build(
                workflow_state,
                reason=reason or "architect guidance를 바탕으로 developer 구현을 시작합니다.",
                step=WORKFLOW_STEP_DEVELOPER_BUILD,
                category=reopen_category,
            )

        if step == WORKFLOW_STEP_DEVELOPER_BUILD:
            if outcome == "reopen":
                return self._workflow_reopen_decision(
                    workflow_state,
                    current_role=current_role or "developer",
                    category=reopen_category,
                    reason=reason,
                )
            return self._workflow_route_to_architect_review(
                workflow_state,
                reason=reason or "developer 구현 결과를 architect가 리뷰합니다.",
                category=reopen_category,
            )

        if step == WORKFLOW_STEP_ARCHITECT_REVIEW:
            if self._workflow_transition_requests_validation_handoff(transition):
                return self._workflow_route_to_qa(
                    workflow_state,
                    reason=reason or "architect review를 통과해 QA 검증으로 넘깁니다.",
                )
            if (
                self._workflow_review_cycle_limit_reached(workflow_state)
                and self._workflow_transition_requests_explicit_continuation(transition)
            ):
                return self._workflow_review_cycle_limit_block_decision(
                    workflow_state,
                    reason=reason,
                    category=reopen_category or "implementation",
                )
            if outcome == "reopen":
                if reopen_category in {"", "implementation"}:
                    return self._workflow_route_to_developer_build(
                        workflow_state,
                        reason=reason or "architect review 결과를 developer가 반영합니다.",
                        step=WORKFLOW_STEP_DEVELOPER_REVISION,
                        category=reopen_category,
                    )
                return self._workflow_reopen_decision(
                    workflow_state,
                    current_role=current_role or "architect",
                    category=reopen_category,
                    reason=reason,
                )
            return self._workflow_route_to_developer_build(
                workflow_state,
                reason=reason or "architect review 결과를 developer가 반영합니다.",
                step=WORKFLOW_STEP_DEVELOPER_REVISION,
                category=reopen_category,
            )

        if step == WORKFLOW_STEP_DEVELOPER_REVISION:
            if outcome == "reopen":
                return self._workflow_reopen_decision(
                    workflow_state,
                    current_role=current_role or "developer",
                    category=reopen_category,
                    reason=reason,
                )
            if str(transition.get("target_step") or "").strip().lower() == WORKFLOW_STEP_ARCHITECT_REVIEW:
                return self._workflow_route_to_architect_review(
                    workflow_state,
                    reason=reason or "developer revision 결과를 architect가 다시 리뷰합니다.",
                    category=reopen_category,
                )
            return self._workflow_route_to_qa(
                workflow_state,
                reason=reason or "developer revision이 끝나 QA 검증으로 넘깁니다.",
            )

        if step == WORKFLOW_STEP_QA_VALIDATION:
            if outcome == "reopen":
                return self._workflow_reopen_decision(
                    workflow_state,
                    current_role=current_role or "qa",
                    category=reopen_category,
                    reason=reason,
                )
            return self._workflow_complete_decision(
                workflow_state,
                summary=reason or "QA 검증을 마쳐 closeout으로 진행합니다.",
            )

        return None

    def _coerce_nonterminal_workflow_role_result(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        sender_role: str,
    ) -> dict[str, Any]:
        if not self._request_workflow_state(request_record):
            return result
        result_status = str(result.get("status") or "").strip().lower()
        error_text = str(result.get("error") or "").strip()
        if result_status not in {"failed", "blocked"} and not error_text:
            return result
        transition = self._workflow_transition(result)
        if not self._workflow_transition_requests_explicit_continuation(transition):
            return result
        workflow_decision = self._derive_workflow_routing_decision(
            request_record,
            result,
            sender_role=sender_role,
        )
        if not workflow_decision:
            return result
        if str(workflow_decision.get("terminal_status") or "").strip().lower():
            return result
        if not str(workflow_decision.get("next_role") or "").strip():
            return result
        normalized_result = dict(result)
        if not str(normalized_result.get("summary") or "").strip() and error_text:
            normalized_result["summary"] = error_text
        normalized_result["status"] = "completed"
        normalized_result["error"] = ""
        return normalized_result

    @staticmethod
    def _normalize_routing_path_nodes(raw_nodes: Any) -> list[str]:
        if not isinstance(raw_nodes, list):
            return []
        return [str(item).strip() for item in raw_nodes if str(item).strip()]

    @staticmethod
    def _format_routing_path_node(*, phase: str = "", step: str = "", role: str = "") -> str:
        normalized_role = str(role or "").strip().lower() or "unknown"
        normalized_phase = str(phase or "").strip().lower()
        normalized_step = str(step or "").strip().lower()
        if normalized_phase and normalized_step:
            return f"{normalized_phase}/{normalized_step}@{normalized_role}"
        if normalized_phase:
            return f"{normalized_phase}@{normalized_role}"
        if normalized_step:
            return f"{normalized_step}@{normalized_role}"
        return normalized_role

    def _request_routing_stage_parts(self, request_record: dict[str, Any]) -> tuple[str, str]:
        workflow_state = self._request_workflow_state(request_record)
        workflow_phase = str(workflow_state.get("phase") or "").strip().lower()
        workflow_step = str(workflow_state.get("step") or "").strip().lower()
        if workflow_phase or workflow_step:
            return workflow_phase, workflow_step
        params = dict(request_record.get("params") or {})
        sprint_phase = str(params.get("sprint_phase") or "").strip().lower()
        sprint_step = str(params.get("initial_phase_step") or "").strip().lower()
        return sprint_phase, sprint_step

    def _current_request_routing_node(self, request_record: dict[str, Any], role: str) -> str:
        phase, step = self._request_routing_stage_parts(request_record)
        if phase or step:
            return self._format_routing_path_node(phase=phase, step=step, role=role)
        return str(role or "").strip().lower()

    def _seed_sprint_routing_path_nodes(self, sprint_state: dict[str, Any] | None = None) -> list[str]:
        nodes = ["start"]
        iterations = list((sprint_state or {}).get("planning_iterations") or [])
        for entry in iterations:
            if not isinstance(entry, dict):
                continue
            node = self._format_routing_path_node(
                phase=str(entry.get("phase") or "").strip().lower(),
                step=str(entry.get("step") or "").strip().lower(),
                role="planner",
            )
            if node and node != nodes[-1]:
                nodes.append(node)
        return nodes

    def _latest_sprint_routing_path_nodes(self, sprint_state: dict[str, Any]) -> list[str]:
        for activity in reversed(list(sprint_state.get("recent_activity") or [])):
            if not isinstance(activity, dict):
                continue
            nodes = self._normalize_routing_path_nodes(activity.get("routing_path_nodes"))
            if nodes:
                return nodes
        for event in reversed(self._load_sprint_event_entries(sprint_state)):
            payload = dict(event.get("payload") or {}) if isinstance(event.get("payload"), dict) else {}
            nodes = self._normalize_routing_path_nodes(payload.get("routing_path_nodes"))
            if nodes:
                return nodes
        return []

    def _build_sprint_routing_path_nodes(self, request_record: dict[str, Any], next_role: str) -> list[str]:
        if not self._is_internal_sprint_request(request_record):
            return []
        params = dict(request_record.get("params") or {})
        sprint_id = str(request_record.get("sprint_id") or params.get("sprint_id") or "").strip()
        sprint_state = self._load_sprint_state(sprint_id) if sprint_id else {}
        nodes = self._latest_sprint_routing_path_nodes(sprint_state) if sprint_state else []
        if not nodes:
            nodes = self._seed_sprint_routing_path_nodes(sprint_state)
        current_node = self._current_request_routing_node(request_record, next_role)
        if current_node and current_node != nodes[-1]:
            nodes.append(current_node)
        return nodes

    def _build_handoff_routing_path(
        self,
        request_record: dict[str, Any],
        *,
        source_role: str,
        target_role: str,
    ) -> str:
        if self._is_internal_sprint_request(request_record):
            nodes = self._build_sprint_routing_path_nodes(request_record, target_role)
            if nodes:
                return " -> ".join(nodes)
        normalized_source = str(source_role or "").strip() or "orchestrator"
        normalized_target = str(target_role or "").strip() or "unknown"
        return f"{normalized_source} -> {normalized_target}"

    def _build_internal_sprint_delegation_payload(
        self,
        request_record: dict[str, Any],
        next_role: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"next_role": next_role}
        if not self._is_internal_sprint_request(request_record):
            return payload
        routing_path_nodes = self._build_sprint_routing_path_nodes(request_record, next_role)
        if routing_path_nodes:
            payload["routing_path_nodes"] = routing_path_nodes
            payload["routing_path"] = " -> ".join(routing_path_nodes)
        routing_context = dict(request_record.get("routing_context") or {})
        if routing_context:
            payload["routing_context"] = routing_context
        return payload

    @staticmethod
    def _summarize_internal_sprint_activity_details(payload: dict[str, Any] | None = None) -> str:
        if not isinstance(payload, dict):
            return ""
        details: list[str] = []
        next_role = str(payload.get("next_role") or "").strip()
        if next_role:
            details.append(f"next={next_role}")
        session_id = str(payload.get("session_id") or "").strip()
        if session_id:
            details.append(f"session={session_id}")
        session_workspace = str(payload.get("session_workspace") or "").strip()
        if session_workspace:
            details.append(f"workspace={_truncate_text(session_workspace, limit=48)}")
        artifacts = [str(item).strip() for item in (payload.get("artifacts") or []) if str(item).strip()]
        if artifacts:
            details.append(f"artifacts={len(artifacts)}")
        error = str(payload.get("error") or "").strip()
        if error:
            details.append(f"error={_truncate_text(error, limit=80)}")
        return " | ".join(details)

    def _record_internal_sprint_activity(
        self,
        request_record: dict[str, Any],
        *,
        event_type: str,
        role: str,
        status: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if not self._is_internal_sprint_request(request_record):
            return
        params = dict(request_record.get("params") or {})
        sprint_id = str(request_record.get("sprint_id") or params.get("sprint_id") or "").strip()
        if not sprint_id:
            return
        sprint_state = self._load_sprint_state(sprint_id)
        if not sprint_state:
            return
        timestamp = utc_now_iso()
        details = self._summarize_internal_sprint_activity_details(payload)
        activity = {
            "timestamp": timestamp,
            "event_type": str(event_type or "").strip() or "activity",
            "role": str(role or "").strip() or "unknown",
            "status": str(status or "").strip() or "N/A",
            "request_id": str(request_record.get("request_id") or "").strip(),
            "todo_id": str(request_record.get("todo_id") or params.get("todo_id") or "").strip(),
            "backlog_id": str(request_record.get("backlog_id") or params.get("backlog_id") or "").strip(),
            "summary": _truncate_text(summary, limit=220) or "없음",
            "details": details,
        }
        routing_path = str((payload or {}).get("routing_path") or "").strip()
        routing_path_nodes = self._normalize_routing_path_nodes((payload or {}).get("routing_path_nodes"))
        if routing_path:
            activity["routing_path"] = routing_path
        if routing_path_nodes:
            activity["routing_path_nodes"] = routing_path_nodes
        recent_activity = [dict(item) for item in (sprint_state.get("recent_activity") or []) if isinstance(item, dict)]
        recent_activity.append(activity)
        sprint_state["recent_activity"] = recent_activity[-RECENT_SPRINT_ACTIVITY_LIMIT:]
        sprint_state["last_activity_at"] = timestamp
        self._save_sprint_state(sprint_state)
        event_payload = {
            "role": activity["role"],
            "status": activity["status"],
            "request_id": activity["request_id"],
            "todo_id": activity["todo_id"],
            "backlog_id": activity["backlog_id"],
        }
        if details:
            event_payload["details"] = details
        if routing_path:
            event_payload["routing_path"] = routing_path
        if routing_path_nodes:
            event_payload["routing_path_nodes"] = routing_path_nodes
        self._append_sprint_event(
            sprint_id,
            event_type=str(event_type or "").strip() or "activity",
            summary=activity["summary"],
            payload=event_payload,
        )
        LOGGER.info(
            "sprint_activity role=%s event=%s status=%s sprint_id=%s request_id=%s todo_id=%s backlog_id=%s summary=%s details=%s",
            activity["role"],
            activity["event_type"],
            activity["status"],
            sprint_id,
            activity["request_id"] or "N/A",
            activity["todo_id"] or "N/A",
            activity["backlog_id"] or "N/A",
            activity["summary"] or "없음",
            details or "없음",
        )

    def _request_routing_text(self, request_record: dict[str, Any], result: dict[str, Any]) -> str:
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        routing = dict(proposals.get("routing") or {}) if isinstance(proposals.get("routing"), dict) else {}
        parts = [
            str(request_record.get("intent") or ""),
            str(request_record.get("scope") or ""),
            str(request_record.get("body") or ""),
            str(result.get("summary") or ""),
            str(routing.get("reason") or ""),
            _summarize_proposals(proposals),
        ]
        return self._normalize_reference_text(" ".join(part for part in parts if str(part).strip()))

    def _has_explicit_planner_reentry_signal(self, result: dict[str, Any]) -> bool:
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        suggested_next_step = (
            dict(proposals.get("suggested_next_step") or {})
            if isinstance(proposals.get("suggested_next_step"), dict)
            else {}
        )
        if str(suggested_next_step.get("owner") or "").strip() == "planner":
            return True
        routing = dict(proposals.get("routing") or {}) if isinstance(proposals.get("routing"), dict) else {}
        reference_text = self._normalize_reference_text(
            " ".join(
                part
                for part in (
                    str(result.get("summary") or ""),
                    str(routing.get("reason") or ""),
                    str(suggested_next_step.get("reason") or ""),
                )
                if str(part).strip()
            )
        )
        return any(
            marker in reference_text
            for marker in (
                "planner",
                "planning",
                "backlog",
                "기획",
                "계획",
                "구조화",
                "재정리",
            )
        )

    def _match_reference_terms(
        self,
        terms: tuple[str, ...],
        *,
        text: str,
        prefix: str,
        limit: int,
    ) -> list[str]:
        if limit <= 0:
            return []
        matches: list[str] = []
        for term in terms:
            normalized_term = self._normalize_reference_text(term)
            if normalized_term and normalized_term in text:
                labeled = f"{prefix}:{term}"
                if labeled not in matches:
                    matches.append(labeled)
            if len(matches) >= limit:
                break
        return matches

    def _routing_signal_matches(
        self,
        role: str,
        *,
        intent: str,
        text: str,
    ) -> list[str]:
        capability = self._agent_capability(role)
        matches: list[str] = []
        if intent and intent in capability.owned_intents:
            matches.append(f"intent:{intent}")
        matches.extend(
            self._match_reference_terms(
                capability.routing_signals,
                text=text,
                prefix="routing",
                limit=4 - len(matches) if len(matches) < 4 else 0,
            )
        )
        return matches[:4]

    def _strongest_domain_matches(self, role: str, *, text: str) -> list[str]:
        capability = self._agent_capability(role)
        matches = self._match_reference_terms(
            capability.strongest_for,
            text=text,
            prefix="strength",
            limit=3,
        )
        if len(matches) < 3:
            matches.extend(
                self._match_reference_terms(
                    capability.strongest_domain_signals,
                    text=text,
                    prefix="strength_signal",
                    limit=3 - len(matches),
                )
            )
        return matches[:3]

    def _preferred_skill_matches(self, role: str, *, text: str) -> list[str]:
        capability = self._agent_capability(role)
        matches = self._match_reference_terms(
            capability.preferred_skills,
            text=text,
            prefix="preferred_skill",
            limit=2,
        )
        if len(matches) < 2:
            matches.extend(
                self._match_reference_terms(
                    capability.preferred_skill_signals,
                    text=text,
                    prefix="skill_signal",
                    limit=2 - len(matches),
                )
            )
        return matches[:2]

    def _behavior_trait_matches(self, role: str, *, text: str) -> list[str]:
        capability = self._agent_capability(role)
        matches = self._match_reference_terms(
            capability.behavior_traits,
            text=text,
            prefix="behavior_trait",
            limit=2,
        )
        if len(matches) < 2:
            matches.extend(
                self._match_reference_terms(
                    capability.behavior_signals,
                    text=text,
                    prefix="behavior_signal",
                    limit=2 - len(matches),
                )
            )
        return matches[:2]

    def _should_not_handle_matches(self, role: str, *, text: str) -> list[str]:
        capability = self._agent_capability(role)
        return self._match_reference_terms(
            capability.should_not_handle,
            text=text,
            prefix="forbidden",
            limit=2,
        )

    @staticmethod
    def _phase_for_role(role: str) -> str:
        return {
            "planner": "planning",
            "designer": "design",
            "architect": "architecture",
            "developer": "implementation",
            "qa": "validation",
        }.get(str(role or "").strip(), "planning")

    def _role_hint_score(self, role: str, *, intent: str, text: str) -> int:
        capability = self._agent_capability(role)
        score = 0
        if intent and intent in capability.owned_intents:
            score += 5
        score += len(self._routing_signal_matches(role, intent=intent, text=text))
        score += len(self._strongest_domain_matches(role, text=text)) * 2
        score += len(self._preferred_skill_matches(role, text=text))
        score += len(self._behavior_trait_matches(role, text=text))
        return score

    def _execution_evidence_score(self, role: str, *, intent: str, text: str) -> int:
        capability = self._agent_capability(role)
        score = 0
        if intent and intent in capability.owned_intents:
            score += 5
        score += len(self._routing_signal_matches(role, intent=intent, text=text))
        score += len(self._strongest_domain_matches(role, text=text)) * 2
        score += len(self._preferred_skill_matches(role, text=text))
        return score

    def _request_indicates_execution(self, *, intent: str, text: str) -> bool:
        if intent in {"design", "architect", "implement", "execute", "qa"}:
            return True
        return any(
            self._execution_evidence_score(role, intent=intent, text=text) > 0
            for role in EXECUTION_AGENT_ROLES
        )

    def _classify_request_state(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        current_role: str,
        requested_role: str,
        selection_source: str,
        text: str,
    ) -> str:
        intent = str(request_record.get("intent") or "").strip().lower()
        if selection_source == "planning_resume":
            return "blocked_resume"
        if current_role == "planner":
            if requested_role in EXECUTION_AGENT_ROLES:
                return "execution_opened"
            if self._request_indicates_execution(intent=intent, text=text):
                return "execution_opened"
            return "planning_only"
        if current_role in {"designer", "architect"}:
            return "implementation_ready"
        if current_role == "developer":
            return "qa_pending"
        if current_role == "qa":
            return "closeout_ready"
        if self._is_internal_sprint_request(request_record):
            return "execution_opened"
        return "planning_only"

    def _derive_routing_phase(
        self,
        *,
        current_role: str,
        requested_role: str,
        selection_source: str,
        request_state_class: str,
        intent: str,
        text: str,
    ) -> str:
        if selection_source == "planning_resume":
            return "resume"
        if selection_source in {"user_intake", "sourcer_review", "blocked_backlog_review"}:
            return "planning"
        if request_state_class == "blocked_resume":
            return "resume"
        if request_state_class == "closeout_ready":
            return "closeout"
        if requested_role in TEAM_ROLES:
            return self._phase_for_role(requested_role)
        if current_role == "planner":
            if request_state_class == "planning_only":
                return "planning"
            hinted_role = ""
            hinted_score = -1
            hinted_priority = -1
            for role in EXECUTION_AGENT_ROLES:
                score = self._role_hint_score(role, intent=intent, text=text)
                capability = self._agent_capability(role)
                priority = int(capability.routing_priority or 0)
                if score > hinted_score or (score == hinted_score and priority > hinted_priority):
                    hinted_role = role
                    hinted_score = score
                    hinted_priority = priority
            return self._phase_for_role(hinted_role or "planner")
        if current_role in EXECUTION_AGENT_ROLES:
            return self._phase_for_role(current_role)
        return "planning"

    def _score_candidate_role(
        self,
        role: str,
        *,
        intent: str,
        text: str,
        routing_phase: str,
        request_state_class: str,
    ) -> dict[str, Any]:
        capability = self._agent_capability(role)
        weights = self.agent_utilization_policy.weights
        matched_signals = self._routing_signal_matches(role, intent=intent, text=text)
        matched_strongest_domains = self._strongest_domain_matches(role, text=text)
        matched_preferred_skills = self._preferred_skill_matches(role, text=text)
        matched_behavior_traits = self._behavior_trait_matches(role, text=text)
        score_breakdown = {
            "owned_intent": weights.owned_intent if intent and intent in capability.owned_intents else 0,
            "routing_signals": len(matched_signals) * weights.routing_signal,
            "strongest_domains": len(matched_strongest_domains) * weights.strongest_domain_signal,
            "preferred_skills": len(matched_preferred_skills) * weights.preferred_skill_signal,
            "behavior_traits": len(matched_behavior_traits) * weights.behavior_signal,
            "phase_fit": weights.phase_fit if routing_phase in capability.phase_fit else 0,
            "request_state_fit": weights.request_state_fit if request_state_class in capability.request_state_fit else 0,
        }
        score_total = sum(score_breakdown.values())
        evidence_score = (
            score_breakdown["owned_intent"]
            + score_breakdown["routing_signals"]
            + score_breakdown["strongest_domains"]
            + score_breakdown["preferred_skills"]
            + score_breakdown["behavior_traits"]
        )
        return {
            "role": role,
            "score_total": score_total,
            "evidence_score": evidence_score,
            "score_breakdown": score_breakdown,
            "matched_signals": matched_signals,
            "matched_strongest_domains": matched_strongest_domains,
            "matched_preferred_skills": matched_preferred_skills,
            "matched_behavior_traits": matched_behavior_traits,
            "selected_for_strength": ", ".join(capability.strongest_for[:2]),
            "suggested_skills": list(capability.preferred_skills),
            "expected_behavior": capability.expected_behavior,
            "role_summary": capability.summary,
        }

    def _build_governed_routing_selection(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        current_role: str,
        requested_role: str,
        selection_source: str,
    ) -> dict[str, Any]:
        normalized_requested_role = str(requested_role or "").strip()
        intent = str(request_record.get("intent") or "").strip().lower()
        text = self._request_routing_text(request_record, result)
        request_state_class = self._classify_request_state(
            request_record,
            result,
            current_role=current_role,
            requested_role=normalized_requested_role,
            selection_source=selection_source,
            text=text,
        )
        routing_phase = self._derive_routing_phase(
            current_role=current_role,
            requested_role=normalized_requested_role,
            selection_source=selection_source,
            request_state_class=request_state_class,
            intent=intent,
            text=text,
        )
        base_selection = {
            "policy_source": self.agent_utilization_policy.policy_source,
            "routing_phase": routing_phase,
            "request_state_class": request_state_class,
            "matched_strongest_domains": [],
            "matched_preferred_skills": [],
            "matched_behavior_traits": [],
            "score_total": 0,
            "score_breakdown": {},
            "candidate_summary": [],
        }

        if selection_source == "user_intake":
            selected_role = self.agent_utilization_policy.user_intake_role
            return {
                "selected_role": selected_role,
                "requested_role": normalized_requested_role,
                "matched_signals": ["policy:user_intake"],
                "override_reason": (
                    f"Requested {normalized_requested_role}, but user_intake policy selected {selected_role}."
                    if normalized_requested_role and normalized_requested_role != selected_role
                    else ""
                ),
                **base_selection,
            }
        if selection_source in {"sourcer_review", "blocked_backlog_review", "planning_resume"}:
            selected_role = (
                self.agent_utilization_policy.sourcer_review_role
                if selection_source in {"sourcer_review", "blocked_backlog_review"}
                else self.agent_utilization_policy.planning_resume_role
            )
            return {
                "selected_role": selected_role,
                "requested_role": normalized_requested_role,
                "matched_signals": [f"policy:{selection_source}"],
                "override_reason": (
                    f"Requested {normalized_requested_role}, but {selection_source} policy selected {selected_role}."
                    if normalized_requested_role and normalized_requested_role != selected_role
                    else ""
                ),
                **base_selection,
            }
        if selection_source == "sprint_initial":
            selected_role = (
                normalized_requested_role
                if normalized_requested_role in TEAM_ROLES and normalized_requested_role != "orchestrator"
                else self.agent_utilization_policy.sprint_initial_default_role
            )
            return {
                "selected_role": selected_role,
                "requested_role": normalized_requested_role,
                "matched_signals": ["policy:sprint_initial_owner"],
                "override_reason": (
                    f"Requested {normalized_requested_role}, but sprint initial owner policy selected {selected_role}."
                    if normalized_requested_role and normalized_requested_role != selected_role
                    else ""
                ),
                **base_selection,
            }

        if current_role == "planner":
            if not normalized_requested_role and request_state_class != "execution_opened":
                return {
                    "selected_role": "",
                    "requested_role": normalized_requested_role,
                    "matched_signals": [],
                    "override_reason": "",
                    **base_selection,
                }
            candidate_roles = list(EXECUTION_AGENT_ROLES)
        elif current_role in EXECUTION_AGENT_ROLES:
            capability = self._agent_capability(current_role)
            candidate_roles = [
                role
                for role in capability.allowed_next_roles
                if role in TEAM_ROLES and role != "orchestrator"
            ]
            if not candidate_roles:
                return {
                    "selected_role": "",
                    "requested_role": normalized_requested_role,
                    "matched_signals": [],
                    "override_reason": "",
                    **base_selection,
                }
            if (
                not self._is_internal_sprint_request(request_record)
                and request_state_class not in {
                    "planning_only",
                    "implementation_ready",
                    "qa_pending",
                    "closeout_ready",
                    "blocked_resume",
                }
            ):
                return {
                    "selected_role": "",
                    "requested_role": normalized_requested_role,
                    "matched_signals": [],
                    "override_reason": "",
                    **base_selection,
                }
        else:
            candidate_roles = []

        if not candidate_roles:
            return {
                "selected_role": "",
                "requested_role": normalized_requested_role,
                "matched_signals": [],
                "override_reason": "",
                **base_selection,
            }

        scored_candidates: list[tuple[int, int, int, str, dict[str, Any]]] = []
        excluded_candidates: list[dict[str, Any]] = []
        for candidate_role in candidate_roles:
            disallowed_matches = self._should_not_handle_matches(candidate_role, text=text)
            if disallowed_matches:
                excluded_candidates.append(
                    {
                        "role": candidate_role,
                        "excluded_by_boundary": True,
                        "disallowed_matches": list(disallowed_matches),
                        "score_total": 0,
                        "score_breakdown": {},
                        "matched_signals": [],
                        "matched_strongest_domains": [],
                        "matched_preferred_skills": [],
                        "matched_behavior_traits": [],
                    }
                )
                continue
            score_details = self._score_candidate_role(
                candidate_role,
                intent=intent,
                text=text,
                routing_phase=routing_phase,
                request_state_class=request_state_class,
            )
            capability = self._agent_capability(candidate_role)
            scored_candidates.append(
                (
                    int(score_details.get("score_total") or 0),
                    int(capability.routing_priority or 0),
                    1 if candidate_role == normalized_requested_role else 0,
                    candidate_role,
                    score_details,
                )
            )
        scored_candidates.sort(reverse=True)
        candidate_summary = [
            {
                "role": role,
                "score_total": details.get("score_total") or 0,
                "score_breakdown": dict(details.get("score_breakdown") or {}),
                "matched_signals": list(details.get("matched_signals") or []),
                "matched_strongest_domains": list(details.get("matched_strongest_domains") or []),
                "matched_preferred_skills": list(details.get("matched_preferred_skills") or []),
                "matched_behavior_traits": list(details.get("matched_behavior_traits") or []),
            }
            for _candidate_score, _candidate_priority, _candidate_bonus, role, details in scored_candidates
        ] + excluded_candidates
        if not scored_candidates:
            return {
                **base_selection,
                "selected_role": "",
                "requested_role": normalized_requested_role,
                "matched_signals": [],
                "override_reason": "",
                "candidate_summary": candidate_summary,
            }
        _score, _priority, _requested_bonus, selected_role, selected_details = scored_candidates[0]
        if current_role == "planner" and not normalized_requested_role and not int(selected_details.get("evidence_score") or 0):
            return {
                **base_selection,
                "selected_role": "",
                "requested_role": normalized_requested_role,
                "matched_signals": [],
                "override_reason": "",
                "candidate_summary": candidate_summary,
            }
        if (
            current_role in EXECUTION_AGENT_ROLES
            and not self._is_internal_sprint_request(request_record)
            and not int(selected_details.get("evidence_score") or 0)
        ):
            return {
                **base_selection,
                "selected_role": "",
                "requested_role": normalized_requested_role,
                "matched_signals": [],
                "override_reason": "",
                "candidate_summary": candidate_summary,
            }
        if (
            self.agent_utilization_policy.planner_reentry_requires_explicit_signal
            and current_role in EXECUTION_AGENT_ROLES
            and selected_role == "planner"
            and not normalized_requested_role
            and not self._has_explicit_planner_reentry_signal(result)
        ):
            return {
                **base_selection,
                "selected_role": "",
                "requested_role": normalized_requested_role,
                "matched_signals": [],
                "override_reason": "",
                "candidate_summary": candidate_summary,
            }
        if current_role == "qa" and not int(selected_details.get("evidence_score") or 0):
            return {
                **base_selection,
                "selected_role": "",
                "requested_role": normalized_requested_role,
                "matched_signals": [],
                "override_reason": "",
                "candidate_summary": candidate_summary,
            }
        matched_signals = [str(item).strip() for item in (selected_details.get("matched_signals") or []) if str(item).strip()]
        if not matched_signals and selected_role == normalized_requested_role:
            matched_signals = ["requested_role"]
        return {
            "selected_role": selected_role,
            "requested_role": normalized_requested_role,
            "matched_signals": matched_signals,
            "matched_strongest_domains": [
                str(item).strip()
                for item in (selected_details.get("matched_strongest_domains") or [])
                if str(item).strip()
            ],
            "matched_preferred_skills": [
                str(item).strip()
                for item in (selected_details.get("matched_preferred_skills") or [])
                if str(item).strip()
            ],
            "matched_behavior_traits": [
                str(item).strip()
                for item in (selected_details.get("matched_behavior_traits") or [])
                if str(item).strip()
            ],
            "score_total": int(selected_details.get("score_total") or 0),
            "score_breakdown": dict(selected_details.get("score_breakdown") or {}),
            "candidate_summary": candidate_summary,
            "override_reason": "",
            "policy_source": self.agent_utilization_policy.policy_source,
            "routing_phase": routing_phase,
            "request_state_class": request_state_class,
        }

    def _build_routing_context(
        self,
        role: str,
        *,
        reason: str,
        requested_role: str = "",
        selection_source: str = "",
        matched_signals: list[str] | None = None,
        override_reason: str = "",
        matched_strongest_domains: list[str] | None = None,
        matched_preferred_skills: list[str] | None = None,
        matched_behavior_traits: list[str] | None = None,
        policy_source: str = "",
        routing_phase: str = "",
        request_state_class: str = "",
        score_total: int = 0,
        score_breakdown: dict[str, Any] | None = None,
        candidate_summary: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        capability = self._agent_capability(role)
        return {
            "selected_role": role,
            "requested_role": str(requested_role or "").strip(),
            "selection_source": str(selection_source or "").strip(),
            "policy_source": str(policy_source or self.agent_utilization_policy.policy_source).strip(),
            "routing_phase": str(routing_phase or "").strip(),
            "request_state_class": str(request_state_class or "").strip(),
            "reason": str(reason or "").strip(),
            "override_reason": str(override_reason or "").strip(),
            "matched_signals": [
                str(item).strip()
                for item in (matched_signals or [])
                if str(item).strip()
            ],
            "matched_strongest_domains": [
                str(item).strip()
                for item in (matched_strongest_domains or [])
                if str(item).strip()
            ],
            "matched_preferred_skills": [
                str(item).strip()
                for item in (matched_preferred_skills or [])
                if str(item).strip()
            ],
            "matched_behavior_traits": [
                str(item).strip()
                for item in (matched_behavior_traits or [])
                if str(item).strip()
            ],
            "score_total": int(score_total or 0),
            "score_breakdown": dict(score_breakdown or {}),
            "candidate_summary": list(candidate_summary or []),
            "selected_for_strength": ", ".join(capability.strongest_for[:2]),
            "suggested_skills": list(capability.preferred_skills),
            "expected_behavior": capability.expected_behavior,
            "behavior_traits": list(capability.behavior_traits),
            "role_summary": capability.summary,
        }

    def _derive_routing_decision_after_report(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        sender_role: str,
    ) -> dict[str, Any]:
        workflow_decision = self._derive_workflow_routing_decision(
            request_record,
            result,
            sender_role=sender_role,
        )
        if workflow_decision is not None:
            return workflow_decision
        if self._is_sourcer_review_request(request_record):
            return {
                "next_role": "",
                "routing_context": {},
            }
        current_role = str(result.get("role") or sender_role or request_record.get("current_role") or "").strip()
        if (
            self._is_sprint_planning_request(request_record)
            and current_role == "planner"
            and str(result.get("status") or "").strip().lower() in {"completed", "committed"}
        ):
            return {
                "next_role": "",
                "routing_context": {},
            }
        requested_next_role = ""
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}

        if self.agent_utilization_policy.verification_result_terminal and proposals.get("verification_result") is not None:
            return {
                "next_role": "",
                "routing_context": {},
            }
        if (
            self.agent_utilization_policy.ignore_non_planner_backlog_proposals_for_routing
            and
            current_role != "planner"
            and not self._is_internal_sprint_request(request_record)
            and (
                isinstance(proposals.get("backlog_item"), dict)
                or (
                    isinstance(proposals.get("backlog_items"), list)
                    and any(isinstance(item, dict) for item in proposals.get("backlog_items") or [])
                )
            )
        ):
            return {
                "next_role": "",
                "routing_context": {},
            }

        selection = self._build_governed_routing_selection(
            request_record,
            result,
            current_role=current_role,
            requested_role=requested_next_role,
            selection_source="role_report",
        )
        next_role = str(selection.get("selected_role") or "").strip()
        override_reason = str(selection.get("override_reason") or "").strip()
        matched_signals = [
            str(item).strip()
            for item in (selection.get("matched_signals") or [])
            if str(item).strip()
        ]
        matched_strongest_domains = [
            str(item).strip()
            for item in (selection.get("matched_strongest_domains") or [])
            if str(item).strip()
        ]
        matched_preferred_skills = [
            str(item).strip()
            for item in (selection.get("matched_preferred_skills") or [])
            if str(item).strip()
        ]
        matched_behavior_traits = [
            str(item).strip()
            for item in (selection.get("matched_behavior_traits") or [])
            if str(item).strip()
        ]

        if not self._is_internal_sprint_request(request_record):
            if not next_role:
                return {"next_role": "", "routing_context": {}}
            reason = (
                override_reason
                or f"Selected {next_role} because its strengths match the current request."
            )
            return {
                "next_role": next_role,
                "routing_context": self._build_routing_context(
                    next_role,
                    reason=reason,
                    requested_role=str(selection.get("requested_role") or ""),
                    selection_source="role_report",
                    matched_signals=matched_signals,
                    override_reason=override_reason,
                    matched_strongest_domains=matched_strongest_domains,
                    matched_preferred_skills=matched_preferred_skills,
                    matched_behavior_traits=matched_behavior_traits,
                    policy_source=str(selection.get("policy_source") or ""),
                    routing_phase=str(selection.get("routing_phase") or ""),
                    request_state_class=str(selection.get("request_state_class") or ""),
                    score_total=int(selection.get("score_total") or 0),
                    score_breakdown=dict(selection.get("score_breakdown") or {}),
                    candidate_summary=list(selection.get("candidate_summary") or []),
                ),
            }
        visited_roles = {
            str(item).strip()
            for item in (request_record.get("visited_roles") or [])
            if str(item).strip()
        }
        if (
            not next_role
            and self.agent_utilization_policy.sprint_force_qa
            and current_role != "qa"
            and "qa" not in visited_roles
        ):
            next_role = "qa"
        if next_role == "qa" and "qa" in visited_roles:
            next_role = ""
        if not next_role:
            return {"next_role": "", "routing_context": {}}
        reason = override_reason or (
            "Selected qa because sprint todos require a release-readiness closeout pass."
            if next_role == "qa"
            else f"Selected {next_role} because its strengths match the current sprint step."
        )
        return {
            "next_role": next_role,
            "routing_context": self._build_routing_context(
                next_role,
                reason=reason,
                requested_role=str(selection.get("requested_role") or ""),
                selection_source="role_report",
                matched_signals=matched_signals or (["policy:sprint_force_qa"] if next_role == "qa" else []),
                override_reason=override_reason,
                matched_strongest_domains=matched_strongest_domains,
                matched_preferred_skills=matched_preferred_skills,
                matched_behavior_traits=matched_behavior_traits,
                policy_source=str(selection.get("policy_source") or ""),
                routing_phase=str(selection.get("routing_phase") or ""),
                request_state_class=str(selection.get("request_state_class") or ""),
                score_total=int(selection.get("score_total") or 0),
                score_breakdown=dict(selection.get("score_breakdown") or {}),
                candidate_summary=list(selection.get("candidate_summary") or []),
            ),
        }

    def _derive_next_role_after_report(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        sender_role: str,
    ) -> str:
        decision = self._derive_routing_decision_after_report(
            request_record,
            result,
            sender_role=sender_role,
        )
        return str(decision.get("next_role") or "").strip()

    @staticmethod
    def _normalize_reference_text(value: str) -> str:
        normalized = str(value or "").strip().lower()
        for token in ("_", "-", "/", "\\", ".", "(", ")", "[", "]", "{", "}", ":", ","):
            normalized = normalized.replace(token, " ")
        return " ".join(normalized.split())

    @staticmethod
    def _request_identity_from_envelope(
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> tuple[str, str]:
        requester = _extract_original_requester(envelope.params) if forwarded else {}
        return (
            str(requester.get("author_id") or message.author_id),
            str(requester.get("channel_id") or message.channel_id),
        )

    @staticmethod
    def _verification_result_payload(result: dict[str, Any]) -> dict[str, Any]:
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        verification = proposals.get("verification_result")
        return dict(verification or {}) if isinstance(verification, dict) else {}

    def _extract_ready_planning_artifact(self, result: dict[str, Any]) -> str:
        verification = self._verification_result_payload(result)
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

    def _extract_verification_related_request_ids(self, result: dict[str, Any]) -> list[str]:
        verification = self._verification_result_payload(result)
        raw_ids = verification.get("related_request_ids")
        if not isinstance(raw_ids, list):
            return []
        return [str(item).strip() for item in raw_ids if str(item).strip()]

    @staticmethod
    def _request_identity_matches(request_record: dict[str, Any], *, author_id: str, channel_id: str) -> bool:
        reply_route = dict(request_record.get("reply_route") or {}) if isinstance(request_record.get("reply_route"), dict) else {}
        return (
            str(reply_route.get("author_id") or "").strip() == str(author_id or "").strip()
            and str(reply_route.get("channel_id") or "").strip() == str(channel_id or "").strip()
        )

    def _is_blocked_planning_request_waiting_for_document(self, request_record: dict[str, Any]) -> bool:
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

    def _request_mentions_artifact(self, request_record: dict[str, Any], artifact_path: str) -> bool:
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
        normalized_combined = self._normalize_reference_text(combined)
        for candidate in alias_candidates:
            normalized_candidate = self._normalize_reference_text(candidate)
            if normalized_candidate and normalized_candidate in normalized_combined:
                return True
        return False

    def _iter_requests_newest_first(self) -> list[dict[str, Any]]:
        return sorted(
            iter_json_records(self.paths.requests_dir),
            key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""),
            reverse=True,
        )

    def _find_recent_ready_planning_verification(
        self,
        *,
        author_id: str,
        channel_id: str,
    ) -> tuple[dict[str, Any], str]:
        now = utc_now()
        for request_record in self._iter_requests_newest_first():
            if str(request_record.get("status") or "").strip().lower() != "completed":
                continue
            if not self._request_identity_matches(request_record, author_id=author_id, channel_id=channel_id):
                continue
            artifact_path = self._extract_ready_planning_artifact(
                dict(request_record.get("result") or {}) if isinstance(request_record.get("result"), dict) else {}
            )
            if not artifact_path:
                continue
            updated_at = self._parse_datetime(str(request_record.get("updated_at") or request_record.get("created_at") or ""))
            if updated_at is not None and abs((now - updated_at).total_seconds()) > PLANNING_CONTEXT_RECENCY_SECONDS:
                continue
            return request_record, artifact_path
        return {}, ""

    def _find_blocked_requests_for_verified_artifact(
        self,
        verification_request: dict[str, Any],
        result: dict[str, Any],
        *,
        author_id: str,
        channel_id: str,
    ) -> tuple[list[dict[str, Any]], str]:
        artifact_path = self._extract_ready_planning_artifact(result)
        if not artifact_path:
            return [], ""
        related_request_ids = self._extract_verification_related_request_ids(result)
        matched_records: list[dict[str, Any]] = []
        if related_request_ids:
            for request_id in related_request_ids:
                request_record = self._load_request(request_id)
                if not request_record or not self._is_blocked_planning_request_waiting_for_document(request_record):
                    continue
                matched_records.append(request_record)
            return matched_records, artifact_path
        inferred_matches: list[dict[str, Any]] = []
        for request_record in self._iter_requests_newest_first():
            if str(request_record.get("request_id") or "") == str(verification_request.get("request_id") or ""):
                continue
            if not self._request_identity_matches(request_record, author_id=author_id, channel_id=channel_id):
                continue
            if not self._is_blocked_planning_request_waiting_for_document(request_record):
                continue
            if not self._request_mentions_artifact(request_record, artifact_path):
                continue
            inferred_matches.append(request_record)
        if len(inferred_matches) == 1:
            return inferred_matches, artifact_path
        return [], artifact_path

    async def _resume_request_with_context(
        self,
        request_record: dict[str, Any],
        *,
        next_role: str,
        summary: str,
        artifact_path: str = "",
        verified_by_request_id: str = "",
        followup_message_id: str = "",
        followup_body: str = "",
    ) -> bool:
        selection = self._build_governed_routing_selection(
            request_record,
            {},
            current_role=str(request_record.get("current_role") or "planner"),
            requested_role=str(next_role or "").strip(),
            selection_source="planning_resume",
        )
        normalized_next_role = str(selection.get("selected_role") or "").strip() or "planner"
        updated_artifacts = [
            str(item).strip()
            for item in request_record.get("artifacts") or []
            if str(item).strip()
        ]
        if artifact_path and artifact_path not in updated_artifacts:
            updated_artifacts.append(artifact_path)
        request_record["artifacts"] = updated_artifacts
        params = dict(request_record.get("params") or {})
        if artifact_path:
            params["verified_source_artifact"] = artifact_path
        if verified_by_request_id:
            params["verified_source_request_id"] = verified_by_request_id
        if followup_message_id:
            params["resume_followup_message_id"] = followup_message_id
        if followup_body:
            params["resume_followup_body"] = followup_body
        request_record["params"] = params
        request_record["status"] = "delegated"
        request_record["current_role"] = normalized_next_role
        request_record["next_role"] = normalized_next_role
        request_record["routing_context"] = self._build_routing_context(
            normalized_next_role,
            reason=summary or f"Selected {normalized_next_role} while resuming the request with additional context.",
            requested_role=str(selection.get("requested_role") or ""),
            selection_source="planning_resume",
            matched_signals=[
                str(item).strip()
                for item in (selection.get("matched_signals") or [])
                if str(item).strip()
            ],
            override_reason=str(selection.get("override_reason") or ""),
            matched_strongest_domains=[
                str(item).strip()
                for item in (selection.get("matched_strongest_domains") or [])
                if str(item).strip()
            ],
            matched_preferred_skills=[
                str(item).strip()
                for item in (selection.get("matched_preferred_skills") or [])
                if str(item).strip()
            ],
            matched_behavior_traits=[
                str(item).strip()
                for item in (selection.get("matched_behavior_traits") or [])
                if str(item).strip()
            ],
            policy_source=str(selection.get("policy_source") or ""),
            routing_phase=str(selection.get("routing_phase") or ""),
            request_state_class=str(selection.get("request_state_class") or ""),
            score_total=int(selection.get("score_total") or 0),
            score_breakdown=dict(selection.get("score_breakdown") or {}),
            candidate_summary=list(selection.get("candidate_summary") or []),
        )
        append_request_event(
            request_record,
            event_type="resumed",
            actor="orchestrator",
            summary=summary,
            payload={
                "next_role": normalized_next_role,
                "routing_context": dict(request_record.get("routing_context") or {}),
                "verified_source_artifact": artifact_path,
                "verified_source_request_id": verified_by_request_id,
                "message_id": followup_message_id,
            },
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="resumed",
            summary=summary,
            result=dict(request_record.get("result") or {}),
        )
        return await self._delegate_request(request_record, normalized_next_role)

    def _enrich_planning_envelope_with_recent_verification(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> MessageEnvelope:
        if envelope.artifacts:
            return envelope
        combined = f"{envelope.scope}\n{envelope.body}".lower()
        explicit_markers = ("shared_workspace/", ".md", "backlog-", "todo-", "request_id=", ".json")
        if any(marker in combined for marker in explicit_markers):
            return envelope
        author_id, channel_id = self._request_identity_from_envelope(message, envelope, forwarded=forwarded)
        verification_request, artifact_path = self._find_recent_ready_planning_verification(
            author_id=author_id,
            channel_id=channel_id,
        )
        if not artifact_path:
            return envelope
        params = dict(envelope.params)
        params.setdefault("inferred_source_request_id", str(verification_request.get("request_id") or ""))
        params.setdefault("inferred_source_artifact", artifact_path)
        return MessageEnvelope(
            request_id=envelope.request_id,
            sender=envelope.sender,
            target=envelope.target,
            intent=envelope.intent,
            urgency=envelope.urgency,
            scope=envelope.scope,
            artifacts=[artifact_path],
            params=params,
            body=envelope.body,
        )

    async def _resume_blocked_planning_request_from_recent_context(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> tuple[dict[str, Any], bool] | None:
        author_id, channel_id = self._request_identity_from_envelope(message, envelope, forwarded=forwarded)
        verification_request, _artifact_path = self._find_recent_ready_planning_verification(
            author_id=author_id,
            channel_id=channel_id,
        )
        if not verification_request:
            return None
        candidates, artifact_path = self._find_blocked_requests_for_verified_artifact(
            verification_request,
            dict(verification_request.get("result") or {}) if isinstance(verification_request.get("result"), dict) else {},
            author_id=author_id,
            channel_id=channel_id,
        )
        if len(candidates) != 1:
            return None
        resumed_request = candidates[0]
        relay_sent = await self._resume_request_with_context(
            resumed_request,
            next_role=str(resumed_request.get("current_role") or resumed_request.get("next_role") or "planner"),
            summary="검증 완료된 기획 문서를 연결해 기존 blocked 요청을 재개했습니다.",
            artifact_path=artifact_path,
            verified_by_request_id=str(verification_request.get("request_id") or ""),
            followup_message_id=message.message_id,
            followup_body=str(envelope.body or "").strip(),
        )
        return resumed_request, relay_sent

    async def _maybe_reopen_blocked_duplicate_request(
        self,
        duplicate_request: dict[str, Any],
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> tuple[dict[str, Any], bool, str] | None:
        if str(duplicate_request.get("status") or "").strip().lower() != "blocked":
            return None
        existing_artifacts = [
            str(item).strip()
            for item in duplicate_request.get("artifacts") or []
            if str(item).strip()
        ]
        new_artifacts = [str(item).strip() for item in envelope.artifacts if str(item).strip() and str(item).strip() not in existing_artifacts]
        followup_body = str(envelope.body or "").strip()
        has_new_body = bool(followup_body and followup_body != str(duplicate_request.get("body") or "").strip())
        if not new_artifacts and not has_new_body:
            if not self._should_retry_same_input_blocked_request(duplicate_request):
                return None
            if followup_body:
                duplicate_request["body"] = followup_body
            duplicate_request["scope"] = str(envelope.scope or duplicate_request.get("scope") or "").strip()
            duplicate_request["artifacts"] = existing_artifacts
            duplicate_request["reply_route"] = self._build_requester_route(message, envelope, forwarded=forwarded)
            duplicate_request["status"] = "delegated"
            duplicate_request["current_role"] = "orchestrator"
            duplicate_request["next_role"] = "orchestrator"
            duplicate_request["routing_context"] = self._build_routing_context(
                "orchestrator",
                reason="Retrying the existing blocked orchestrator-owned request from a repeated user request.",
                requested_role="orchestrator",
                selection_source="blocked_retry",
            )
            params = dict(duplicate_request.get("params") or {})
            params["retry_followup_message_id"] = message.message_id
            if followup_body:
                params["retry_followup_body"] = followup_body
            duplicate_request["params"] = params
            append_request_event(
                duplicate_request,
                event_type="retried",
                actor="orchestrator",
                summary="반복된 사용자 요청으로 기존 blocked 요청을 다시 실행합니다.",
                payload={"message_id": message.message_id},
            )
            self._save_request(duplicate_request)
            await self._run_local_orchestrator_request(duplicate_request)
            return duplicate_request, True, "retried"
        if has_new_body:
            duplicate_request["body"] = followup_body
        if new_artifacts:
            duplicate_request["artifacts"] = existing_artifacts + new_artifacts
        relay_sent = await self._resume_request_with_context(
            duplicate_request,
            next_role=str(duplicate_request.get("current_role") or duplicate_request.get("next_role") or "planner"),
            summary="후속 요청에서 보강된 입력을 반영해 기존 blocked 요청을 재개했습니다.",
            artifact_path=new_artifacts[0] if new_artifacts else "",
            verified_by_request_id=str(dict(envelope.params).get("inferred_source_request_id") or ""),
            followup_message_id=message.message_id,
            followup_body=followup_body,
        )
        return duplicate_request, relay_sent, "augmented"

    def _should_retry_same_input_blocked_request(self, request_record: dict[str, Any]) -> bool:
        if str(request_record.get("status") or "").strip().lower() != "blocked":
            return False
        if self._is_blocked_planning_request_waiting_for_document(request_record):
            return False
        current_role = str(request_record.get("current_role") or "").strip().lower()
        next_role = str(request_record.get("next_role") or "").strip().lower()
        owner_role = str(request_record.get("owner_role") or "").strip().lower()
        result_role = str(dict(request_record.get("result") or {}).get("role") or "").strip().lower()
        orchestrator_owned = "orchestrator" in {current_role, next_role, owner_role, result_role}
        return orchestrator_owned

    async def _resume_requests_from_verification_result(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> list[str]:
        author_id = str(dict(request_record.get("reply_route") or {}).get("author_id") or "").strip()
        channel_id = str(dict(request_record.get("reply_route") or {}).get("channel_id") or "").strip()
        if not author_id or not channel_id:
            return []
        candidates, artifact_path = self._find_blocked_requests_for_verified_artifact(
            request_record,
            result,
            author_id=author_id,
            channel_id=channel_id,
        )
        if not candidates:
            return []
        resumed_request_ids: list[str] = []
        for blocked_request in candidates:
            await self._resume_request_with_context(
                blocked_request,
                next_role=str(blocked_request.get("current_role") or blocked_request.get("next_role") or "planner"),
                summary="검증 완료된 기획 문서를 연결해 기존 blocked 요청을 재개했습니다.",
                artifact_path=artifact_path,
                verified_by_request_id=str(request_record.get("request_id") or ""),
            )
            resumed_request_ids.append(str(blocked_request.get("request_id") or ""))
        return resumed_request_ids

    async def _wait_for_internal_request_result(self, request_id: str) -> dict[str, Any]:
        normalized = str(request_id or "").strip()
        if not normalized:
            return {}
        while True:
            request_record = self._load_request(normalized)
            status = str(request_record.get("status") or "").strip().lower()
            if self._is_terminal_internal_request_status(status):
                result = dict(request_record.get("result") or {})
                if result:
                    return result
                return {
                    "request_id": normalized,
                    "role": str(request_record.get("current_role") or "orchestrator"),
                    "status": status or "failed",
                    "summary": str(request_record.get("body") or request_record.get("scope") or "").strip(),
                    "insights": [],
                    "proposals": {},
                    "artifacts": [str(item) for item in request_record.get("artifacts") or []],
                    "next_role": "",
                    "error": "",
                }
            await asyncio.sleep(INTERNAL_REQUEST_POLL_SECONDS)

    async def run(self) -> None:
        if self._is_internal_relay_enabled():
            self._internal_relay_consumer_task = asyncio.create_task(self._consume_internal_relay_loop())
        if self.role != "orchestrator":
            try:
                await self._listen_forever()
            finally:
                if self._internal_relay_consumer_task is not None:
                    self._internal_relay_consumer_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await self._internal_relay_consumer_task
            return
        scheduler_task = asyncio.create_task(self._scheduler_loop())
        sourcing_task = asyncio.create_task(self._backlog_sourcing_loop())
        try:
            await self._listen_forever()
        finally:
            scheduler_task.cancel()
            sourcing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await scheduler_task
            with contextlib.suppress(asyncio.CancelledError):
                await sourcing_task
            if self._internal_relay_consumer_task is not None:
                self._internal_relay_consumer_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._internal_relay_consumer_task

    async def _listen_forever(self) -> None:
        while True:
            try:
                await self.discord_client.listen(self.handle_message, on_ready=self._on_ready)
            except asyncio.CancelledError:
                raise
            except DiscordListenError as exc:
                diagnostics = classify_discord_exception(
                    exc,
                    token_env_name=self.role_config.token_env,
                    expected_bot_id=self.role_config.bot_id,
                )
                self._record_listener_health_state(
                    status="reconnecting",
                    error=diagnostics["summary"],
                    category=diagnostics["category"],
                    recovery_action=diagnostics["recovery_action"],
                )
                LOGGER.warning(
                    "Discord listener loop waiting %.1fs for role %s after listen error: %s",
                    LISTENER_RETRY_SECONDS,
                    self.role,
                    exc,
                )
            except Exception as exc:
                diagnostics = classify_discord_exception(
                    exc,
                    token_env_name=self.role_config.token_env,
                    expected_bot_id=self.role_config.bot_id,
                )
                self._record_listener_health_state(
                    status="reconnecting",
                    error=diagnostics["summary"],
                    category=diagnostics["category"],
                    recovery_action=diagnostics["recovery_action"],
                )
                LOGGER.exception("Discord listener loop failed for role %s; retrying", self.role)
            await asyncio.sleep(LISTENER_RETRY_SECONDS)

    async def _on_ready(self) -> None:
        current_identity = getattr(self.discord_client, "current_identity", None)
        identity = current_identity() if callable(current_identity) else {}
        self._record_listener_health_state(
            status="connected",
            error="",
            category="connected",
            recovery_action="",
            connected_bot_name=str(identity.get("name") or ""),
            connected_bot_id=str(identity.get("id") or ""),
        )
        await self._announce_startup()
        if self.role != "orchestrator":
            if self._pending_role_request_resume_task is None or self._pending_role_request_resume_task.done():
                self._pending_role_request_resume_task = asyncio.create_task(
                    self._resume_pending_role_requests_loop()
                )
            return
        scheduler_state = self._load_scheduler_state()
        active_sprint_id = str(scheduler_state.get("active_sprint_id") or "").strip()
        if active_sprint_id:
            asyncio.create_task(self._resume_active_sprint(active_sprint_id))
            return
        await self._maybe_request_idle_sprint_milestone(reason="startup_no_active_sprint")

    async def handle_message(self, message: DiscordMessage) -> None:
        if not self._is_message_allowed(message):
            return
        await self._send_immediate_receipt(message)
        if self.role == "orchestrator":
            await self._handle_orchestrator_message(message)
            return
        await self._handle_non_orchestrator_message(message)

    def _is_message_allowed(self, message: DiscordMessage) -> bool:
        if message.guild_id and self.runtime_config.allowed_guild_ids:
            return message.guild_id in self.runtime_config.allowed_guild_ids
        if message.is_dm and not self.runtime_config.ingress_dm:
            return False
        if not message.is_dm and not self.runtime_config.ingress_mentions and not self._is_trusted_relay_message(message):
            return False
        return True

    def _is_trusted_relay_message(self, message: DiscordMessage) -> bool:
        return (
            message.channel_id == self.discord_config.relay_channel_id
            and message.author_id in self.discord_config.trusted_bot_ids
        )

    def _is_internal_relay_enabled(self) -> bool:
        return self.relay_transport == RELAY_TRANSPORT_INTERNAL

    def _internal_relay_root(self) -> Path:
        return self.paths.runtime_root / "internal_relay"

    def _internal_relay_inbox_dir(self, role: str) -> Path:
        normalized_role = str(role or "").strip()
        return self._internal_relay_root() / "inbox" / normalized_role

    def _internal_relay_archive_dir(self, role: str) -> Path:
        normalized_role = str(role or "").strip()
        return self._internal_relay_root() / "archive" / normalized_role

    @staticmethod
    def _is_internal_relay_summary_content(content: str) -> bool:
        first_line = str(content or "").splitlines()[0].strip() if str(content or "").splitlines() else ""
        return first_line.startswith(INTERNAL_RELAY_SUMMARY_MARKER)

    def _is_internal_relay_summary_message(self, message: DiscordMessage) -> bool:
        if not self._is_trusted_relay_message(message):
            return False
        return self._is_internal_relay_summary_content(message.content)

    def _log_malformed_trusted_relay(self, *, reason: str, kind: str) -> None:
        normalized_kind = str(kind or "").strip() or "none"
        cache_key = f"{self.role}:{reason}:{normalized_kind}"
        now = time.monotonic()
        last_logged_at = self._malformed_relay_log_times.get(cache_key)
        if last_logged_at is not None and now - last_logged_at < MALFORMED_RELAY_LOG_WINDOW_SECONDS:
            return
        self._malformed_relay_log_times[cache_key] = now
        LOGGER.debug(
            "Ignoring malformed trusted relay for role %s: %s (%s)",
            self.role,
            reason,
            normalized_kind,
        )

    async def _announce_startup(self) -> None:
        current_identity = getattr(self.discord_client, "current_identity", None)
        identity = current_identity() if callable(current_identity) else {}
        identity_name = identity.get("name") or "unknown"
        identity_id = identity.get("id") or "unknown"
        report = self._build_startup_report(
            identity_name=identity_name,
            identity_id=identity_id,
        )
        startup_target = f"startup:{self.discord_config.startup_channel_id}"
        try:
            await self._send_discord_content(
                content=report,
                send=lambda chunk: self.discord_client.send_channel_message(self.discord_config.startup_channel_id, chunk),
                target_description=startup_target,
                swallow_exceptions=False,
            )
        except Exception as exc:
            send_error = exc if isinstance(exc, DiscordSendError) else DiscordSendError(str(exc))
            fallback_target = await self._send_startup_failure_fallback(report=report, error=send_error)
            self._record_startup_notification_state(
                status="fallback_sent" if fallback_target else "failed",
                error=str(send_error),
                attempted_channel=self.discord_config.startup_channel_id,
                attempts=getattr(send_error, "attempts", 1),
                fallback_target=fallback_target,
            )
            LOGGER.warning(
                "Startup notification failed for role %s via %s: %s",
                self.role,
                startup_target,
                send_error,
            )
            return
        self._record_startup_notification_state(
            status="sent",
            error="",
            attempted_channel=self.discord_config.startup_channel_id,
            attempts=1,
            fallback_target="",
        )

    def _build_startup_report(self, *, identity_name: str, identity_id: str) -> str:
        scheduler_state = self._load_scheduler_state()
        active_sprint_id = str(scheduler_state.get("active_sprint_id") or "").strip()
        return box_text_message(
            "\n".join(
                [
                    f"[준비 완료] ✅ {self.role}",
                    f"- 🤖 봇: {identity_name} ({identity_id})",
                    f"- 🔐 검증: role {self.role} | expected_bot_id {self.role_config.bot_id}",
                    f"- 🎯 현재 스프린트: {active_sprint_id or '없음'}",
                    (
                        f"- 📡 채널: startup {self.discord_config.startup_channel_id} | "
                        f"relay {self.discord_config.relay_channel_id}"
                    ),
                ]
            ).strip()
        )

    def _format_sprint_scope(self, *, sprint_id: str = "") -> str:
        scheduler_state = self._load_scheduler_state()
        active_sprint_id = str(sprint_id or scheduler_state.get("active_sprint_id") or "").strip()
        return f"현재 스프린트: {active_sprint_id or '없음'}"

    def _load_agent_state(self) -> dict[str, Any]:
        state = read_json(self.paths.agent_state_file(self.role))
        if isinstance(state, dict):
            return state
        return {}

    def _listener_state_metadata(self, *, connected_bot_id: str = "") -> dict[str, Any]:
        expected_bot_id = str(self.role_config.bot_id or "").strip()
        normalized_connected_bot_id = str(connected_bot_id or "").strip()
        return {
            "listener_configured_role": self.role,
            "listener_resolved_workspace_root": str(self.paths.workspace_root),
            "listener_discord_config_path": str(self.discord_config.config_path or ""),
            "listener_expected_bot_id": expected_bot_id,
            "listener_identity_matches_expected": bool(
                expected_bot_id
                and normalized_connected_bot_id
                and normalized_connected_bot_id == expected_bot_id
            ),
        }

    def _record_startup_notification_state(
        self,
        *,
        status: str,
        error: str,
        attempted_channel: str,
        attempts: int,
        fallback_target: str,
    ) -> None:
        state = self._load_agent_state()
        state.update(
            {
                "startup_notification_status": str(status or "").strip(),
                "startup_notification_error": str(error or "").strip(),
                "startup_notification_channel": str(attempted_channel or "").strip(),
                "startup_notification_attempts": int(attempts or 0),
                "startup_notification_fallback_target": str(fallback_target or "").strip(),
                "startup_notification_updated_at": utc_now_iso(),
            }
        )
        write_json(self.paths.agent_state_file(self.role), state)

    def _record_listener_health_state(
        self,
        *,
        status: str,
        error: str,
        category: str,
        recovery_action: str,
        connected_bot_name: str = "",
        connected_bot_id: str = "",
    ) -> None:
        state = self._load_agent_state()
        state.update(
            {
                "listener_status": str(status or "").strip(),
                "listener_error": str(error or "").strip(),
                "listener_error_category": str(category or "").strip(),
                "listener_recovery_action": str(recovery_action or "").strip(),
                "listener_connected_bot_name": str(connected_bot_name or "").strip(),
                "listener_connected_bot_id": str(connected_bot_id or "").strip(),
                "listener_updated_at": utc_now_iso(),
            }
        )
        state.update(self._listener_state_metadata(connected_bot_id=connected_bot_id))
        if str(status or "").strip() == "connected":
            state["listener_connected_at"] = utc_now_iso()
        else:
            state["listener_last_failure_at"] = utc_now_iso()
        write_json(self.paths.agent_state_file(self.role), state)

    def _record_sourcer_report_state(
        self,
        *,
        status: str,
        client_label: str,
        reason: str,
        category: str,
        recovery_action: str,
        error: str,
        attempts: int,
        channel_id: str,
    ) -> None:
        normalized = {
            "report_status": str(status or "").strip(),
            "report_client": str(client_label or "").strip(),
            "report_reason": str(reason or "").strip(),
            "report_category": str(category or "").strip(),
            "report_recovery_action": str(recovery_action or "").strip(),
            "report_error": str(error or "").strip(),
            "report_attempts": int(attempts or 0),
            "report_channel": str(channel_id or "").strip(),
            "report_updated_at": utc_now_iso(),
        }
        state = self._load_agent_state()
        last_failure_at = str(state.get("sourcer_report_last_failure_at") or "").strip()
        last_success_at = str(state.get("sourcer_report_last_success_at") or "").strip()
        if normalized["report_status"] == "failed":
            last_failure_at = normalized["report_updated_at"]
        elif normalized["report_status"] == "sent":
            last_success_at = normalized["report_updated_at"]
            self._last_sourcer_report_failure_signature = ""
            self._last_sourcer_report_failure_logged_at = 0.0
        normalized["report_last_failure_at"] = last_failure_at
        normalized["report_last_success_at"] = last_success_at
        if isinstance(self._last_backlog_sourcing_activity, dict):
            self._last_backlog_sourcing_activity.update(normalized)
        state.update(
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
        write_json(self.paths.agent_state_file(self.role), state)

    def _should_suppress_sourcer_report_failure_log(
        self,
        *,
        client_label: str,
        category: str,
        channel_id: str,
        error_text: str,
    ) -> bool:
        signature = "|".join(
            [
                str(client_label or "").strip(),
                str(category or "").strip(),
                str(channel_id or "").strip(),
                str(error_text or "").strip(),
            ]
        )
        now = time.monotonic()
        if (
            signature
            and signature == self._last_sourcer_report_failure_signature
            and (now - self._last_sourcer_report_failure_logged_at) < 300.0
        ):
            self._last_sourcer_report_failure_logged_at = now
            return True
        self._last_sourcer_report_failure_signature = signature
        self._last_sourcer_report_failure_logged_at = now
        return False

    def _log_sourcer_report_failure(
        self,
        *,
        client_label: str,
        channel_id: str,
        diagnostics: dict[str, str],
        error: BaseException,
        attempts: int,
    ) -> None:
        category = str(diagnostics.get("category") or "").strip()
        summary = str(diagnostics.get("summary") or str(error) or "").strip()
        recovery_action = str(diagnostics.get("recovery_action") or "").strip()
        suppressed = self._should_suppress_sourcer_report_failure_log(
            client_label=client_label,
            category=category,
            channel_id=channel_id,
            error_text=str(error),
        )
        if suppressed:
            LOGGER.warning(
                "Repeated sourcer activity Discord report failure via %s to report:%s (category=%s, attempts=%s, summary=%s)",
                client_label,
                channel_id,
                category or "unknown",
                int(attempts or 0),
                summary or str(error),
            )
            return
        if category in {"discord_dns_failed", "discord_timeout", "discord_connection_failed", "client_disconnected"}:
            LOGGER.warning(
                "Failed to send sourcer activity Discord report via %s to report:%s (category=%s, attempts=%s, summary=%s, recovery=%s)",
                client_label,
                channel_id,
                category or "unknown",
                int(attempts or 0),
                summary or str(error),
                recovery_action or "N/A",
            )
            return
        LOGGER.exception(
            "Failed to send sourcer activity Discord report via %s to report:%s",
            client_label,
            channel_id,
        )

    def _version_controller_sources_dir(self) -> Path:
        return self.paths.internal_agent_root("version_controller") / "sources"

    def _write_version_control_payload_file(self, payload_name: str, payload: dict[str, Any]) -> tuple[str, str]:
        sources_dir = self._version_controller_sources_dir()
        sources_dir.mkdir(parents=True, exist_ok=True)
        payload_file = sources_dir / payload_name
        write_json(payload_file, payload)
        return str(payload_file), str(payload_file.relative_to(self.paths.internal_agent_root("version_controller")))

    @staticmethod
    def _clone_jsonish(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        return json.loads(json.dumps(payload, ensure_ascii=False))

    async def _invoke_version_controller(
        self,
        *,
        request_context: dict[str, Any],
        mode: str,
        scope: str,
        summary: str,
        payload_file: str,
        helper_command: str,
        artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        envelope = MessageEnvelope(
            request_id=str(request_context.get("request_id") or ""),
            sender="orchestrator",
            target="version_controller",
            intent="execute",
            urgency="normal",
            scope=scope,
            artifacts=[str(item).strip() for item in (artifacts or []) if str(item).strip()],
            params={
                "_teams_kind": "internal_version_control",
                "version_control_mode": mode,
                "payload_file": payload_file,
                "helper_command": helper_command,
            },
            body=(
                f"version_control_mode={mode}\n"
                f"helper_command={helper_command}\n"
                f"payload_file={payload_file}\n"
                f"summary={summary}"
            ),
        )
        return await asyncio.to_thread(self.version_controller_runtime.run_task, envelope, request_context)

    async def _run_task_version_controller(
        self,
        *,
        sprint_state: dict[str, Any],
        todo: dict[str, Any],
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = str(request_record.get("request_id") or todo.get("request_id") or "").strip() or "task"
        backlog_id = str(todo.get("backlog_id") or "").strip()
        task_commit_title = str(todo.get("title") or "").strip()
        if not task_commit_title and backlog_id:
            backlog_item = self._load_backlog_item(backlog_id)
            task_commit_title = str(backlog_item.get("title") or "").strip()
        task_commit_summary = str(
            request_record.get("task_commit_summary")
            or result.get("task_commit_summary")
            or result.get("summary")
            or todo.get("title")
            or request_record.get("scope")
            or ""
        ).strip()
        functional_commit_title = (
            task_commit_summary
            if _looks_meta_change_text(task_commit_title) and task_commit_summary
            else task_commit_title
        )
        payload = {
            "mode": "task",
            "project_root": str(self.paths.project_workspace_root),
            "baseline": dict(request_record.get("git_baseline") or {}),
            "sprint_id": str(sprint_state.get("sprint_id") or ""),
            "todo_id": str(todo.get("todo_id") or ""),
            "backlog_id": backlog_id,
            "title": task_commit_title,
            "functional_title": functional_commit_title,
            "summary": task_commit_summary,
        }
        _payload_abs, payload_rel = self._write_version_control_payload_file(
            f"{request_id}.task.version_control.json",
            payload,
        )
        helper_command = build_version_control_helper_command(payload_rel)
        request_context = self._clone_jsonish(request_record)
        request_context["version_control"] = {
            "mode": "task",
            "payload_file": payload_rel,
            "helper_command": helper_command,
            "scope": str(todo.get("title") or request_record.get("scope") or ""),
            "title": task_commit_title,
            "functional_title": functional_commit_title,
            "summary": task_commit_summary,
        }
        request_context["result"] = self._clone_jsonish(result)
        request_record["version_control_status"] = "running"
        request_record["task_commit_status"] = "running"
        append_request_event(
            request_record,
            event_type="version_control_requested",
            actor="orchestrator",
            summary="version_controller로 task 완료 커밋을 위임했습니다.",
            payload={"mode": "task", "payload_file": payload_rel},
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="version_control_requested",
            summary="version_controller로 task 완료 커밋을 위임했습니다.",
            result=result,
        )
        version_result = await self._invoke_version_controller(
            request_context=request_context,
            mode="task",
            scope=str(todo.get("title") or request_record.get("scope") or "task version control"),
            summary=str(result.get("summary") or ""),
            payload_file=payload_rel,
            helper_command=helper_command,
            artifacts=[payload_rel, *[str(item) for item in (result.get("artifacts") or []) if str(item).strip()]],
        )
        append_request_event(
            request_record,
            event_type="version_control_completed",
            actor="version_controller",
            summary=str(version_result.get("summary") or "version_controller 결과를 수신했습니다."),
            payload=version_result,
        )
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="version_control_completed",
            summary=str(version_result.get("summary") or "version_controller 결과를 수신했습니다."),
            result=version_result,
        )
        return version_result

    async def _run_closeout_version_controller(
        self,
        *,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> dict[str, Any]:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip() or "closeout"
        payload = {
            "mode": "closeout",
            "project_root": str(self.paths.project_workspace_root),
            "baseline": dict(sprint_state.get("git_baseline") or {}),
            "sprint_id": sprint_id,
            "commit_message": build_sprint_commit_message(sprint_id),
        }
        _payload_abs, payload_rel = self._write_version_control_payload_file(
            f"{sprint_id}.closeout.version_control.json",
            payload,
        )
        helper_command = build_version_control_helper_command(payload_rel)
        request_context = {
            "request_id": f"{sprint_id}:closeout",
            "status": "queued",
            "current_role": "orchestrator",
            "next_role": "",
            "owner_role": "orchestrator",
            "scope": f"{sprint_id} sprint closeout",
            "body": str(closeout_result.get("message") or "").strip(),
            "artifacts": [],
            "events": [],
            "result": {
                "role": "orchestrator",
                "status": "completed",
                "summary": str(closeout_result.get("message") or "스프린트 closeout commit이 필요합니다.").strip(),
                "artifacts": [
                    str(item).strip()
                    for item in (closeout_result.get("uncommitted_paths") or [])
                    if str(item).strip()
                ],
            },
            "version_control": {
                "mode": "closeout",
                "payload_file": payload_rel,
                "helper_command": helper_command,
                "scope": f"{sprint_id} sprint closeout",
                "summary": str(closeout_result.get("message") or "").strip(),
            },
            "sprint_id": sprint_id,
        }
        return await self._invoke_version_controller(
            request_context=request_context,
            mode="closeout",
            scope=f"{sprint_id} sprint closeout",
            summary=str(closeout_result.get("message") or ""),
            payload_file=payload_rel,
            helper_command=helper_command,
            artifacts=[payload_rel],
        )

    def _iter_startup_fallback_targets(self) -> list[tuple[str, str]]:
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

    @staticmethod
    def _summarize_boxed_report_excerpt(report: str, *, limit_lines: int = 4, limit_chars: int = 240) -> str:
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

    def _build_startup_fallback_report(self, *, report: str, error: DiscordSendError, fallback_target: str) -> str:
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
            log_summary=self._summarize_boxed_report_excerpt(report),
            end_reason=str(error),
            judgment="startup 채널 전송은 실패해 대체 채널로 1회 복구 통지를 시도했습니다.",
            next_action="startup 채널 접근 상태와 Discord 네트워크 타임아웃을 확인합니다.",
            artifacts=[str(self.paths.agent_state_file(self.role))],
        )

    async def _send_startup_failure_fallback(self, *, report: str, error: DiscordSendError) -> str:
        for label, channel_id in self._iter_startup_fallback_targets():
            fallback_target = f"{label}:{channel_id}"
            try:
                await self._send_discord_content(
                    content=self._build_startup_fallback_report(
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

    def _runtime_for_role(self, role: str, sprint_id: str) -> RoleAgentRuntime:
        key = (role, sprint_id)
        cached = self._role_runtime_cache.get(key)
        if cached is not None:
            return cached
        runtime = RoleAgentRuntime(
            paths=self.paths,
            role=role,
            sprint_id=sprint_id,
            runtime_config=self.runtime_config.role_defaults[role],
        )
        self._role_runtime_cache[key] = runtime
        return runtime

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    @staticmethod
    def _build_backlog_fingerprint(*, title: str, scope: str, kind: str) -> str:
        normalized = "|".join(
            [
                " ".join(str(title or "").strip().lower().split()),
                " ".join(str(scope or "").strip().lower().split()),
                str(kind or "").strip().lower(),
            ]
        )
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_sourcer_candidate_trace_fingerprint(candidate: dict[str, Any]) -> str:
        origin = dict(candidate.get("origin") or {})
        trace_parts: list[str] = []
        for key in sorted(origin):
            normalized_key = str(key or "").strip().lower()
            if not normalized_key or normalized_key == "sourcer_summary":
                continue
            if (
                normalized_key != "request_id"
                and not normalized_key.endswith("_request_id")
                and normalized_key not in {"action_name", "log_file", "operation_id", "role", "signal", "status"}
            ):
                continue
            value = origin.get(key)
            if isinstance(value, list):
                normalized_values = sorted(
                    {
                        " ".join(str(item or "").strip().split())
                        for item in value
                        if str(item or "").strip()
                    }
                )
                if normalized_values:
                    trace_parts.append(f"{normalized_key}={','.join(normalized_values)}")
                continue
            normalized_value = " ".join(str(value or "").strip().split())
            if normalized_value:
                trace_parts.append(f"{normalized_key}={normalized_value}")
        return "|".join(trace_parts)

    def _load_scheduler_state(self) -> dict[str, Any]:
        state = read_json(self.paths.sprint_scheduler_file)
        return {
            "active_sprint_id": str(state.get("active_sprint_id") or "").strip(),
            "last_started_at": str(state.get("last_started_at") or "").strip(),
            "last_completed_at": str(state.get("last_completed_at") or "").strip(),
            "last_skipped_at": str(state.get("last_skipped_at") or "").strip(),
            "last_skip_reason": str(state.get("last_skip_reason") or "").strip(),
            "last_sourced_at": str(state.get("last_sourced_at") or "").strip(),
            "last_sourcing_status": str(state.get("last_sourcing_status") or "").strip(),
            "last_sourcing_request_id": str(state.get("last_sourcing_request_id") or "").strip(),
            "last_sourcing_fingerprint": str(state.get("last_sourcing_fingerprint") or "").strip(),
            "last_sourcing_review_status": str(state.get("last_sourcing_review_status") or "").strip(),
            "last_sourcing_review_request_id": str(state.get("last_sourcing_review_request_id") or "").strip(),
            "next_slot_at": str(state.get("next_slot_at") or "").strip(),
            "deferred_slot_at": str(state.get("deferred_slot_at") or "").strip(),
            "last_trigger": str(state.get("last_trigger") or "").strip(),
            "last_blocked_review_at": str(state.get("last_blocked_review_at") or "").strip(),
            "last_blocked_review_request_id": str(state.get("last_blocked_review_request_id") or "").strip(),
            "last_blocked_review_status": str(state.get("last_blocked_review_status") or "").strip(),
            "last_blocked_review_fingerprint": str(state.get("last_blocked_review_fingerprint") or "").strip(),
            "milestone_request_pending": bool(state.get("milestone_request_pending")),
            "milestone_request_sent_at": str(state.get("milestone_request_sent_at") or "").strip(),
            "milestone_request_channel_id": str(state.get("milestone_request_channel_id") or "").strip(),
            "milestone_request_reason": str(state.get("milestone_request_reason") or "").strip(),
        }

    def _save_scheduler_state(self, state: dict[str, Any]) -> None:
        write_json(self.paths.sprint_scheduler_file, state)

    @staticmethod
    def _clear_pending_milestone_request(state: dict[str, Any]) -> None:
        state["milestone_request_pending"] = False
        state["milestone_request_sent_at"] = ""
        state["milestone_request_channel_id"] = ""
        state["milestone_request_reason"] = ""

    @staticmethod
    def _clear_blocked_backlog_review_state(state: dict[str, Any]) -> None:
        state["last_blocked_review_at"] = ""
        state["last_blocked_review_request_id"] = ""
        state["last_blocked_review_status"] = ""
        state["last_blocked_review_fingerprint"] = ""

    @staticmethod
    def _build_idle_sprint_milestone_request_message() -> str:
        return (
            "현재 active sprint가 없습니다. 새 sprint milestone을 알려주세요.\n"
            "예: `start sprint\\nmilestone: sprint workflow initial phase 개선`"
        )

    async def _maybe_request_idle_sprint_milestone(self, *, reason: str) -> bool:
        state = self._load_scheduler_state()
        if str(state.get("active_sprint_id") or "").strip():
            return False
        if bool(state.get("milestone_request_pending")):
            return False
        relay_channel_id = str(self.discord_config.relay_channel_id or "").strip()
        if not relay_channel_id:
            return False
        try:
            await self._send_discord_content(
                content=self._build_idle_sprint_milestone_request_message(),
                send=lambda chunk: self.discord_client.send_channel_message(relay_channel_id, chunk),
                target_description=f"idle-sprint-milestone:{relay_channel_id}",
                swallow_exceptions=False,
            )
        except Exception as exc:
            LOGGER.warning("Failed to send idle sprint milestone request via relay:%s: %s", relay_channel_id, exc)
            return False
        state["milestone_request_pending"] = True
        state["milestone_request_sent_at"] = utc_now_iso()
        state["milestone_request_channel_id"] = relay_channel_id
        state["milestone_request_reason"] = str(reason or "").strip()
        self._save_scheduler_state(state)
        return True

    def _build_sourcer_existing_backlog_context(self) -> list[dict[str, Any]]:
        items = [
            item
            for item in self._iter_backlog_items()
            if self._is_active_backlog_status(str(item.get("status") or ""))
        ]
        items.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        context: list[dict[str, Any]] = []
        for item in items[:20]:
            context.append(
                {
                    "backlog_id": str(item.get("backlog_id") or ""),
                    "title": str(item.get("title") or ""),
                    "summary": str(item.get("summary") or ""),
                    "kind": str(item.get("kind") or ""),
                    "scope": str(item.get("scope") or ""),
                    "status": str(item.get("status") or ""),
                }
            )
        return context

    def _collect_backlog_linked_request_ids(self) -> set[str]:
        linked_request_ids: set[str] = set()
        for item in self._iter_backlog_items():
            origin = dict(item.get("origin") or {})
            for key, value in origin.items():
                normalized_key = str(key or "").strip().lower()
                if normalized_key != "request_id" and not normalized_key.endswith("_request_id"):
                    continue
                request_id = str(value or "").strip()
                if request_id:
                    linked_request_ids.add(request_id)
        return linked_request_ids

    def _build_backlog_sourcing_findings(self) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        backlog_linked_request_ids = self._collect_backlog_linked_request_ids()
        for record in iter_json_records(self.paths.requests_dir):
            status = str(record.get("status") or "").strip().lower()
            if status in {"completed", "committed", "cancelled", "blocked", "delegated", "queued"}:
                continue
            request_id = str(record.get("request_id") or "").strip()
            if request_id and request_id in backlog_linked_request_ids:
                continue
            params = dict(record.get("params") or {})
            if str(params.get("_teams_kind") or "").strip().lower() == "sprint_internal":
                continue
            summary = str(record.get("scope") or record.get("body") or "").strip()
            if not summary:
                continue
            findings.append(
                {
                    "signal": "open_request",
                    "title": summary,
                    "summary": f"request 상태={status}",
                    "kind_hint": self._classify_backlog_kind(str(record.get("intent") or ""), summary, status),
                    "scope": summary,
                    "acceptance_criteria": [],
                    "origin": {"request_id": record.get("request_id") or "", "status": status},
                }
            )
        if self.runtime_config.sprint_discovery_scope == "broad_scan":
            for record in iter_json_records(self.paths.requests_dir):
                if self._is_internal_sprint_request(record):
                    continue
                result = dict(record.get("result") or {}) if isinstance(record.get("result"), dict) else {}
                role = str(result.get("role") or "").strip()
                request_id = str(record.get("request_id") or result.get("request_id") or "").strip()
                if request_id and request_id in backlog_linked_request_ids:
                    continue
                summary = str(result.get("summary") or "").strip()
                status = str(result.get("status") or "").strip().lower()
                if role and status == "failed":
                    findings.append(
                        {
                            "signal": "role_failure",
                            "title": f"{role} 후속 조치 {request_id or 'unknown'}",
                            "summary": summary or str(result.get("error") or "").strip() or f"{role} 결과 점검",
                            "kind_hint": "bug",
                            "scope": summary or f"{role} role output follow-up",
                            "acceptance_criteria": [],
                            "origin": {
                                "role": role,
                                "request_id": request_id,
                                "status": status,
                                "request_file": str(self.paths.request_file(request_id)) if request_id else "",
                            },
                        }
                    )
        if self.runtime_config.sprint_discovery_scope in {"plus_git", "broad_scan"}:
            baseline = capture_git_baseline(self.paths.project_workspace_root)
            dirty_paths = [str(item) for item in baseline.get("dirty_paths") or [] if str(item).strip()]
            if dirty_paths:
                findings.append(
                    {
                        "signal": "git_dirty",
                        "title": "워크스페이스 변경 검토",
                        "summary": ", ".join(dirty_paths[:8]),
                        "kind_hint": "enhancement",
                        "scope": "git status 기반 변경 검토",
                        "acceptance_criteria": [],
                        "origin": {"dirty_paths": dirty_paths},
                    }
                )
        if self.runtime_config.sprint_discovery_scope == "broad_scan":
            for role in TEAM_ROLES:
                log_path = self.paths.agent_runtime_log(role)
                try:
                    log_text = log_path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    log_text = ""
                if "Traceback" in log_text or "ERROR" in log_text:
                    findings.append(
                        {
                            "signal": "runtime_log_error",
                            "title": f"{role} 로그 오류 점검",
                            "summary": "runtime log에 오류 흔적이 있습니다.",
                            "kind_hint": "bug",
                            "scope": f"{role} runtime log",
                            "acceptance_criteria": [],
                            "origin": {"role": role, "log_file": str(log_path)},
                        }
                    )
            for action_name in self.runtime_config.sprint_discovery_actions:
                action = self.runtime_config.actions.get(action_name)
                if action is None or action.lifecycle != "foreground":
                    continue
                try:
                    execution = self.action_executor.execute(
                        request_id=f"discovery-{utc_now().strftime('%Y%m%d%H%M%S')}",
                        action_name=action_name,
                        params={},
                    )
                except Exception as exc:
                    findings.append(
                        {
                            "signal": "discovery_action_failure",
                            "title": f"discovery action 실패: {action_name}",
                            "summary": str(exc),
                            "kind_hint": "bug",
                            "scope": f"discovery_action={action_name}",
                            "acceptance_criteria": [],
                            "origin": {"action_name": action_name},
                        }
                    )
                    continue
                if str(execution.get("status") or "").strip().lower() != "completed":
                    findings.append(
                        {
                            "signal": "discovery_action_check",
                            "title": f"discovery action 점검: {action_name}",
                            "summary": str(execution.get("report") or execution.get("message") or "").strip(),
                            "kind_hint": "bug",
                            "scope": f"discovery_action={action_name}",
                            "acceptance_criteria": [],
                            "origin": {
                                "action_name": action_name,
                                "operation_id": execution.get("operation_id") or "",
                            },
                        }
                    )
        return findings

    def _fallback_backlog_candidates_from_findings(self, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for finding in findings:
            title = str(finding.get("title") or "").strip()
            if not title:
                continue
            candidates.append(
                build_backlog_item(
                    title=title,
                    summary=str(finding.get("summary") or title).strip(),
                    kind=str(finding.get("kind_hint") or "enhancement").strip().lower() or "enhancement",
                    source="sourcer",
                    scope=str(finding.get("scope") or title).strip(),
                    acceptance_criteria=self._normalize_backlog_acceptance_criteria(
                        finding.get("acceptance_criteria")
                    ),
                    origin={
                        "sourcing_agent": "fallback",
                        "signal": str(finding.get("signal") or "").strip(),
                        **dict(finding.get("origin") or {}),
                    },
                )
            )
        return candidates

    def _iter_backlog_items(self) -> list[dict[str, Any]]:
        return list(iter_json_records(self.paths.backlog_dir))

    def _is_non_actionable_backlog_item(self, item: dict[str, Any]) -> bool:
        title = str(item.get("title") or "").strip().lower()
        source = str(item.get("source") or "").strip().lower()
        if source == "discovery" and title.endswith("insight follow-up"):
            return True
        origin = dict(item.get("origin") or {})
        request_id = str(origin.get("request_id") or "").strip()
        if source == "discovery" and request_id:
            request_record = self._load_request(request_id)
            params = dict(request_record.get("params") or {})
            if str(params.get("_teams_kind") or "").strip() == "sprint_internal":
                return True
        return False

    @staticmethod
    def _is_active_backlog_status(status: str) -> bool:
        return str(status or "").strip().lower() in {"pending", "selected", "blocked"}

    @staticmethod
    def _is_actionable_backlog_status(status: str) -> bool:
        return str(status or "").strip().lower() == "pending"

    @staticmethod
    def _is_reusable_backlog_status(status: str) -> bool:
        return str(status or "").strip().lower() in {"pending", "selected", "blocked"}

    @staticmethod
    def _clear_backlog_blockers(item: dict[str, Any]) -> None:
        item["blocked_reason"] = ""
        item["blocked_by_role"] = ""
        item["required_inputs"] = []
        item["recommended_next_step"] = ""

    @staticmethod
    def _desired_backlog_status_for_todo(todo: dict[str, Any] | None) -> str:
        status = str((todo or {}).get("status") or "").strip().lower()
        if status in {"queued", "running"}:
            return "selected"
        if status in {"completed", "committed"}:
            return "done"
        if status in {"blocked", "uncommitted"}:
            return "blocked"
        if status == "failed":
            return "carried_over"
        return ""

    @staticmethod
    def _todo_status_rank(status: str) -> int:
        normalized = str(status or "").strip().lower()
        return {
            "queued": 0,
            "running": 1,
            "blocked": 2,
            "uncommitted": 2,
            "failed": 2,
            "completed": 3,
            "committed": 4,
        }.get(normalized, -1)

    @classmethod
    def _sort_sprint_todos(cls, todos: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            todos,
            key=lambda todo: (
                0 if str(todo.get("status") or "").strip().lower() == "running" else 1,
                -int(todo.get("priority_rank") or 0),
                str(todo.get("created_at") or todo.get("started_at") or ""),
                str(todo.get("todo_id") or ""),
            ),
        )

    def _iter_sprint_task_request_records(self, sprint_id: str) -> list[dict[str, Any]]:
        normalized_sprint_id = str(sprint_id or "").strip()
        if not normalized_sprint_id:
            return []
        records: list[dict[str, Any]] = []
        for record in iter_json_records(self.paths.requests_dir):
            if not self._is_internal_sprint_request(record):
                continue
            params = dict(record.get("params") or {})
            request_sprint_id = str(record.get("sprint_id") or params.get("sprint_id") or "").strip()
            if request_sprint_id != normalized_sprint_id:
                continue
            backlog_id = str(record.get("backlog_id") or params.get("backlog_id") or "").strip()
            todo_id = str(record.get("todo_id") or params.get("todo_id") or "").strip()
            if not backlog_id and not todo_id:
                continue
            records.append(record)
        records.sort(key=lambda record: (str(record.get("created_at") or ""), str(record.get("request_id") or "")))
        return records

    @staticmethod
    def _todo_status_from_request_record(request_record: dict[str, Any]) -> str:
        result = dict(request_record.get("result") or {}) if isinstance(request_record.get("result"), dict) else {}
        candidates = [
            str(result.get("status") or "").strip().lower(),
            str(request_record.get("status") or "").strip().lower(),
        ]
        valid_statuses = {"queued", "running", "uncommitted", "committed", "completed", "blocked", "failed"}
        for candidate in candidates:
            if candidate in valid_statuses:
                return candidate
        if "delegated" in candidates:
            return "running"
        return "queued"

    def _build_recovered_sprint_todo_from_request(
        self,
        sprint_state: dict[str, Any],
        request_record: dict[str, Any],
    ) -> dict[str, Any]:
        params = dict(request_record.get("params") or {})
        request_id = str(request_record.get("request_id") or "").strip()
        backlog_id = str(request_record.get("backlog_id") or params.get("backlog_id") or "").strip()
        todo_id = str(request_record.get("todo_id") or params.get("todo_id") or "").strip() or (
            f"recovered-{request_id}" if request_id else ""
        )
        if not backlog_id and not todo_id:
            return {}

        backlog_item = self._load_backlog_item(backlog_id) if backlog_id else {}
        title = (
            str(backlog_item.get("title") or "").strip()
            or str(request_record.get("scope") or "").strip()
            or str(request_record.get("body") or "").strip()
        )
        summary = (
            str((request_record.get("result") or {}).get("summary") or "").strip()
            or str(request_record.get("task_commit_summary") or "").strip()
            or str(request_record.get("body") or "").strip()
        )
        owner_role = (
            str(request_record.get("next_role") or "").strip()
            or str(request_record.get("current_role") or "").strip()
            or "planner"
        )
        source_backlog = backlog_item or {
            "backlog_id": backlog_id,
            "title": title,
            "milestone_title": str(sprint_state.get("milestone_title") or "").strip(),
            "priority_rank": 0,
            "acceptance_criteria": [],
        }
        todo = build_todo_item(source_backlog, owner_role=owner_role)
        todo["todo_id"] = todo_id or todo["todo_id"]
        todo["backlog_id"] = backlog_id
        todo["title"] = title
        todo["milestone_title"] = (
            str(backlog_item.get("milestone_title") or "").strip()
            or str(sprint_state.get("milestone_title") or "").strip()
        )
        todo["priority_rank"] = int(backlog_item.get("priority_rank") or 0)
        todo["acceptance_criteria"] = [
            str(item).strip()
            for item in (backlog_item.get("acceptance_criteria") or [])
            if str(item).strip()
        ]
        todo["request_id"] = request_id
        todo["status"] = self._todo_status_from_request_record(request_record)
        todo["artifacts"] = self._normalize_sprint_todo_artifacts(
            request_record.get("artifacts"),
            (request_record.get("result") or {}).get("artifacts") if isinstance(request_record.get("result"), dict) else [],
            request_record.get("task_commit_paths"),
            request_record.get("version_control_paths"),
            workflow_state=self._request_workflow_state(request_record),
        )
        todo["summary"] = summary
        todo["version_control_status"] = str(request_record.get("version_control_status") or "").strip()
        todo["version_control_paths"] = [
            str(item).strip()
            for item in (request_record.get("version_control_paths") or [])
            if str(item).strip()
        ]
        todo["version_control_message"] = str(request_record.get("version_control_message") or "").strip()
        todo["version_control_error"] = str(request_record.get("version_control_error") or "").strip()
        created_at = str(request_record.get("created_at") or "").strip()
        updated_at = str(request_record.get("updated_at") or created_at).strip()
        if created_at:
            todo["created_at"] = created_at
        if updated_at:
            todo["updated_at"] = updated_at
        if todo["status"] in {"running", "completed", "committed", "blocked", "failed", "uncommitted"}:
            todo["started_at"] = created_at
        if todo["status"] in {"completed", "committed", "blocked", "failed", "uncommitted"}:
            todo["ended_at"] = updated_at
        return todo

    def _merge_recovered_sprint_todo(self, existing: dict[str, Any], recovered: dict[str, Any]) -> dict[str, Any]:
        merged = dict(existing)
        for field in ("request_id", "title", "milestone_title", "summary", "created_at"):
            if not str(merged.get(field) or "").strip() and str(recovered.get(field) or "").strip():
                merged[field] = recovered[field]
        if not int(merged.get("priority_rank") or 0) and int(recovered.get("priority_rank") or 0):
            merged["priority_rank"] = int(recovered.get("priority_rank") or 0)
        if not list(merged.get("acceptance_criteria") or []) and list(recovered.get("acceptance_criteria") or []):
            merged["acceptance_criteria"] = list(recovered.get("acceptance_criteria") or [])
        existing_updated_at = self._parse_datetime(
            str(merged.get("updated_at") or merged.get("ended_at") or merged.get("created_at") or "")
        )
        recovered_updated_at = self._parse_datetime(
            str(recovered.get("updated_at") or recovered.get("ended_at") or recovered.get("created_at") or "")
        )
        recovered_is_newer = (
            recovered_updated_at is not None
            and (existing_updated_at is None or recovered_updated_at >= existing_updated_at)
        )
        if recovered_is_newer:
            merged["status"] = recovered.get("status") or merged.get("status") or ""
            if str(recovered.get("summary") or "").strip():
                merged["summary"] = recovered["summary"]
            if str(recovered.get("started_at") or "").strip():
                merged["started_at"] = recovered["started_at"]
            if str(recovered.get("ended_at") or "").strip():
                merged["ended_at"] = recovered["ended_at"]
            if str(recovered.get("updated_at") or "").strip():
                merged["updated_at"] = recovered["updated_at"]
            merged["artifacts"] = list(recovered.get("artifacts") or [])
            for field in ("version_control_status", "version_control_message", "version_control_error"):
                if str(recovered.get(field) or "").strip():
                    merged[field] = recovered[field]
            if list(recovered.get("version_control_paths") or []):
                merged["version_control_paths"] = list(recovered.get("version_control_paths") or [])
        else:
            if self._todo_status_rank(recovered.get("status") or "") > self._todo_status_rank(merged.get("status") or ""):
                merged["status"] = recovered.get("status") or merged.get("status") or ""
                if str(recovered.get("summary") or "").strip():
                    merged["summary"] = recovered["summary"]
                if str(recovered.get("started_at") or "").strip():
                    merged["started_at"] = recovered["started_at"]
                if str(recovered.get("ended_at") or "").strip():
                    merged["ended_at"] = recovered["ended_at"]
            merged["artifacts"] = self._collect_artifact_candidates(
                merged.get("artifacts"),
                recovered.get("artifacts"),
            )
            for field in ("version_control_status", "version_control_message", "version_control_error"):
                if not str(merged.get(field) or "").strip() and str(recovered.get(field) or "").strip():
                    merged[field] = recovered[field]
            merged["version_control_paths"] = self._collect_artifact_candidates(
                merged.get("version_control_paths"),
                recovered.get("version_control_paths"),
            )
        return merged

    def _recover_sprint_todos_from_requests(self, sprint_state: dict[str, Any]) -> bool:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        if not sprint_id:
            return False
        todos = [todo for todo in (sprint_state.get("todos") or []) if isinstance(todo, dict)]
        request_records = self._iter_sprint_task_request_records(sprint_id)
        if not todos and not request_records:
            return False

        def build_indexes(items: list[dict[str, Any]]) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
            todo_indexes: dict[str, int] = {}
            request_indexes: dict[str, int] = {}
            backlog_indexes: dict[str, int] = {}
            for index, todo in enumerate(items):
                todo_id = str(todo.get("todo_id") or "").strip()
                request_id = str(todo.get("request_id") or "").strip()
                backlog_id = str(todo.get("backlog_id") or "").strip()
                if todo_id and todo_id not in todo_indexes:
                    todo_indexes[todo_id] = index
                if request_id and request_id not in request_indexes:
                    request_indexes[request_id] = index
                if backlog_id and backlog_id not in backlog_indexes:
                    backlog_indexes[backlog_id] = index
            return todo_indexes, request_indexes, backlog_indexes

        changed = False
        todo_indexes, request_indexes, backlog_indexes = build_indexes(todos)
        for request_record in request_records:
            recovered = self._build_recovered_sprint_todo_from_request(sprint_state, request_record)
            if not recovered:
                continue
            retired_request_ids = {
                str(todo.get("retry_of_request_id") or "").strip()
                for todo in todos
                if str(todo.get("retry_of_request_id") or "").strip()
            }
            todo_id = str(recovered.get("todo_id") or "").strip()
            request_id = str(recovered.get("request_id") or "").strip()
            backlog_id = str(recovered.get("backlog_id") or "").strip()
            if request_id and request_id in retired_request_ids:
                continue
            existing_index = -1
            if todo_id and todo_id in todo_indexes:
                existing_index = todo_indexes[todo_id]
            elif request_id and request_id in request_indexes:
                existing_index = request_indexes[request_id]
            elif backlog_id and backlog_id in backlog_indexes:
                existing_index = backlog_indexes[backlog_id]
            if existing_index >= 0:
                merged = self._merge_recovered_sprint_todo(todos[existing_index], recovered)
                if merged != todos[existing_index]:
                    todos[existing_index].clear()
                    todos[existing_index].update(merged)
                    changed = True
                continue
            todos.append(recovered)
            changed = True
            todo_indexes, request_indexes, backlog_indexes = build_indexes(todos)
        if not changed:
            return False
        sprint_state["todos"] = self._sort_sprint_todos(todos)
        return True

    @staticmethod
    def _parse_sprint_report_fields(report_body: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        for raw_line in str(report_body or "").splitlines():
            line = str(raw_line or "").strip()
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            normalized_key = str(key or "").strip()
            if normalized_key:
                fields[normalized_key] = str(value or "").strip()
        return fields

    @staticmethod
    def _parse_sprint_report_list_field(value: str) -> list[str]:
        normalized = str(value or "").strip()
        if not normalized or normalized.upper() == "N/A":
            return []
        return [item.strip() for item in normalized.split(",") if item.strip()]

    @staticmethod
    def _parse_sprint_report_int_field(value: str) -> int:
        try:
            return int(str(value or "").strip())
        except (TypeError, ValueError):
            return 0

    def _derived_closeout_result_from_sprint_state(self, sprint_state: dict[str, Any]) -> dict[str, Any]:
        report_fields = self._parse_sprint_report_fields(str(sprint_state.get("report_body") or ""))
        commit_shas = [
            str(item).strip()
            for item in (sprint_state.get("commit_shas") or self._parse_sprint_report_list_field(report_fields.get("commit_shas") or ""))
            if str(item).strip()
        ]
        return {
            "status": str(sprint_state.get("closeout_status") or report_fields.get("closeout_status") or "").strip(),
            "commit_count": int(sprint_state.get("commit_count") or self._parse_sprint_report_int_field(report_fields.get("commit_count") or "")),
            "commit_shas": commit_shas,
            "representative_commit_sha": str(sprint_state.get("commit_sha") or report_fields.get("commit_sha") or "").strip(),
            "sprint_tagged_commit_count": self._parse_sprint_report_int_field(
                report_fields.get("sprint_tagged_commit_count") or ""
            ),
            "sprint_tagged_commit_shas": self._parse_sprint_report_list_field(
                report_fields.get("sprint_tagged_commit_shas") or ""
            ),
            "uncommitted_paths": [
                str(item).strip()
                for item in (
                    sprint_state.get("uncommitted_paths")
                    or self._parse_sprint_report_list_field(report_fields.get("uncommitted_paths") or "")
                )
                if str(item).strip()
            ],
            "message": str(report_fields.get("closeout_message") or "").strip(),
        }

    def _refresh_sprint_report_body(self, sprint_state: dict[str, Any]) -> bool:
        report_body = str(sprint_state.get("report_body") or "").strip()
        if not report_body:
            return False
        report_fields = self._parse_sprint_report_fields(report_body)
        if not report_fields.get("sprint_id"):
            return False
        refreshed = self._build_sprint_report_body(
            sprint_state,
            self._derived_closeout_result_from_sprint_state(sprint_state),
        )
        if refreshed == report_body:
            return False
        sprint_state["report_body"] = refreshed
        return True

    def _apply_backlog_state_from_todo(
        self,
        backlog_item: dict[str, Any],
        *,
        todo: dict[str, Any] | None,
        sprint_id: str,
    ) -> bool:
        desired_status = self._desired_backlog_status_for_todo(todo)
        if not desired_status:
            return False
        changed = False
        if str(backlog_item.get("status") or "").strip().lower() != desired_status:
            backlog_item["status"] = desired_status
            changed = True
        if desired_status == "selected":
            if str(backlog_item.get("selected_in_sprint_id") or "").strip() != sprint_id:
                backlog_item["selected_in_sprint_id"] = sprint_id
                changed = True
            if str(backlog_item.get("completed_in_sprint_id") or "").strip():
                backlog_item["completed_in_sprint_id"] = ""
                changed = True
            blocker_snapshot = (
                str(backlog_item.get("blocked_reason") or "").strip(),
                str(backlog_item.get("blocked_by_role") or "").strip(),
                list(backlog_item.get("required_inputs") or []),
                str(backlog_item.get("recommended_next_step") or "").strip(),
            )
            self._clear_backlog_blockers(backlog_item)
            if blocker_snapshot != (
                str(backlog_item.get("blocked_reason") or "").strip(),
                str(backlog_item.get("blocked_by_role") or "").strip(),
                list(backlog_item.get("required_inputs") or []),
                str(backlog_item.get("recommended_next_step") or "").strip(),
            ):
                changed = True
            return changed
        if desired_status == "done":
            if str(backlog_item.get("selected_in_sprint_id") or "").strip() != sprint_id:
                backlog_item["selected_in_sprint_id"] = sprint_id
                changed = True
            if str(backlog_item.get("completed_in_sprint_id") or "").strip() != sprint_id:
                backlog_item["completed_in_sprint_id"] = sprint_id
                changed = True
            blocker_snapshot = (
                str(backlog_item.get("blocked_reason") or "").strip(),
                str(backlog_item.get("blocked_by_role") or "").strip(),
                list(backlog_item.get("required_inputs") or []),
                str(backlog_item.get("recommended_next_step") or "").strip(),
            )
            self._clear_backlog_blockers(backlog_item)
            if blocker_snapshot != (
                str(backlog_item.get("blocked_reason") or "").strip(),
                str(backlog_item.get("blocked_by_role") or "").strip(),
                list(backlog_item.get("required_inputs") or []),
                str(backlog_item.get("recommended_next_step") or "").strip(),
            ):
                changed = True
            return changed
        if desired_status == "blocked":
            if str(backlog_item.get("selected_in_sprint_id") or "").strip():
                backlog_item["selected_in_sprint_id"] = ""
                changed = True
            if str(backlog_item.get("completed_in_sprint_id") or "").strip():
                backlog_item["completed_in_sprint_id"] = ""
                changed = True
            todo_status = str((todo or {}).get("status") or "").strip().lower()
            todo_summary = str((todo or {}).get("summary") or "").strip()
            if todo_status == "uncommitted":
                if not str(backlog_item.get("blocked_reason") or "").strip() and todo_summary:
                    backlog_item["blocked_reason"] = todo_summary
                    changed = True
                if str(backlog_item.get("blocked_by_role") or "").strip() != "version_controller":
                    backlog_item["blocked_by_role"] = "version_controller"
                    changed = True
                if not str(backlog_item.get("recommended_next_step") or "").strip():
                    backlog_item["recommended_next_step"] = "version_controller recovery 또는 수동 git 정리가 필요합니다."
                    changed = True
            return changed
        if desired_status == "carried_over":
            if str(backlog_item.get("selected_in_sprint_id") or "").strip():
                backlog_item["selected_in_sprint_id"] = ""
                changed = True
            if str(backlog_item.get("completed_in_sprint_id") or "").strip():
                backlog_item["completed_in_sprint_id"] = ""
                changed = True
        return changed

    def _synchronize_sprint_todo_backlog_state(self, sprint_state: dict[str, Any], *, persist_backlog: bool = True) -> bool:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        existing_selected_items = list(sprint_state.get("selected_items") or [])
        existing_selected_backlog_ids = [
            str(item).strip()
            for item in (sprint_state.get("selected_backlog_ids") or [])
            if str(item).strip()
        ]
        todos = list(sprint_state.get("todos") or [])

        ordered_backlog_ids: list[str] = []
        selected_items_by_backlog_id: dict[str, dict[str, Any]] = {}
        todo_by_backlog_id: dict[str, dict[str, Any]] = {}

        def remember_backlog_id(backlog_id: str) -> None:
            normalized_backlog_id = str(backlog_id or "").strip()
            if not normalized_backlog_id or normalized_backlog_id in ordered_backlog_ids:
                return
            ordered_backlog_ids.append(normalized_backlog_id)

        for item in existing_selected_items:
            backlog_id = str(item.get("backlog_id") or "").strip()
            if not backlog_id or backlog_id in selected_items_by_backlog_id:
                continue
            selected_items_by_backlog_id[backlog_id] = dict(item)
            remember_backlog_id(backlog_id)
        for backlog_id in existing_selected_backlog_ids:
            remember_backlog_id(backlog_id)
        for todo in todos:
            backlog_id = str(todo.get("backlog_id") or "").strip()
            if not backlog_id:
                continue
            todo_by_backlog_id[backlog_id] = todo
            remember_backlog_id(backlog_id)

        updated_selected_items: list[dict[str, Any]] = []
        live_selected_backlog_ids: list[str] = []
        backlog_changed = False

        for backlog_id in ordered_backlog_ids:
            todo = todo_by_backlog_id.get(backlog_id)
            selected_item = dict(selected_items_by_backlog_id.get(backlog_id) or {})
            backlog_item = self._load_backlog_item(backlog_id)
            if backlog_item and self._apply_backlog_state_from_todo(backlog_item, todo=todo, sprint_id=sprint_id):
                if persist_backlog:
                    backlog_item["updated_at"] = utc_now_iso()
                    write_json(self.paths.backlog_file(backlog_id), backlog_item)
                    backlog_changed = True
            merged_item = dict(backlog_item or selected_item)
            if not merged_item and todo:
                merged_item = {
                    "backlog_id": backlog_id,
                    "title": str(todo.get("title") or "").strip(),
                    "milestone_title": str(todo.get("milestone_title") or "").strip(),
                    "priority_rank": int(todo.get("priority_rank") or 0),
                    "acceptance_criteria": [
                        str(value).strip()
                        for value in (todo.get("acceptance_criteria") or [])
                        if str(value).strip()
                    ],
                }
            desired_status = self._desired_backlog_status_for_todo(todo)
            if desired_status:
                merged_item["status"] = desired_status
                if desired_status == "done":
                    merged_item["selected_in_sprint_id"] = sprint_id
                    merged_item["completed_in_sprint_id"] = sprint_id
                    self._clear_backlog_blockers(merged_item)
                elif desired_status == "selected":
                    merged_item["selected_in_sprint_id"] = sprint_id
                    merged_item["completed_in_sprint_id"] = ""
                    self._clear_backlog_blockers(merged_item)
                elif desired_status == "blocked":
                    merged_item["selected_in_sprint_id"] = ""
                    merged_item["completed_in_sprint_id"] = ""
                    if str((todo or {}).get("status") or "").strip().lower() == "uncommitted":
                        if not str(merged_item.get("blocked_reason") or "").strip():
                            merged_item["blocked_reason"] = str((todo or {}).get("summary") or "").strip()
                        if not str(merged_item.get("blocked_by_role") or "").strip():
                            merged_item["blocked_by_role"] = "version_controller"
                        if not str(merged_item.get("recommended_next_step") or "").strip():
                            merged_item["recommended_next_step"] = "version_controller recovery 또는 수동 git 정리가 필요합니다."
                elif desired_status == "carried_over":
                    merged_item["selected_in_sprint_id"] = ""
                    merged_item["completed_in_sprint_id"] = ""
            if not merged_item:
                continue
            updated_selected_items.append(merged_item)
            if str(merged_item.get("status") or "").strip().lower() in {"pending", "selected"}:
                live_selected_backlog_ids.append(backlog_id)

        changed = False
        if updated_selected_items != existing_selected_items:
            sprint_state["selected_items"] = updated_selected_items
            changed = True
        if live_selected_backlog_ids != existing_selected_backlog_ids:
            sprint_state["selected_backlog_ids"] = live_selected_backlog_ids
            changed = True
        if backlog_changed:
            self._refresh_backlog_markdown()
        return changed or backlog_changed

    def _repair_non_actionable_carry_over_backlog_items(self) -> set[str]:
        blocked_carry_over_ids: set[str] = set()
        for sprint_file in sorted(self.paths.sprints_dir.glob("*.json")):
            sprint_state = read_json(sprint_file)
            if not sprint_state:
                continue
            for todo in sprint_state.get("todos") or []:
                status = str(todo.get("status") or "").strip().lower()
                carry_over_backlog_id = str(todo.get("carry_over_backlog_id") or "").strip()
                if status == "blocked" and carry_over_backlog_id:
                    blocked_carry_over_ids.add(carry_over_backlog_id)
        repaired_ids: set[str] = set()
        if not blocked_carry_over_ids:
            return repaired_ids
        for item in self._iter_backlog_items():
            backlog_id = str(item.get("backlog_id") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            if backlog_id not in blocked_carry_over_ids or status not in {"pending", "selected"}:
                continue
            item["status"] = "blocked"
            item["selected_in_sprint_id"] = ""
            item["updated_at"] = utc_now_iso()
            self._save_backlog_item(item)
            repaired_ids.add(backlog_id)
        return repaired_ids

    def _drop_non_actionable_backlog_items(self) -> set[str]:
        dropped_ids: set[str] = set()
        for item in self._iter_backlog_items():
            if not self._is_non_actionable_backlog_item(item):
                continue
            backlog_id = str(item.get("backlog_id") or "").strip()
            if not backlog_id:
                continue
            if str(item.get("status") or "").strip().lower() == "dropped":
                dropped_ids.add(backlog_id)
                continue
            item["status"] = "dropped"
            item["selected_in_sprint_id"] = ""
            item["completed_in_sprint_id"] = ""
            item["dropped_reason"] = "agent insight is journal-only context, not backlog work"
            item["updated_at"] = utc_now_iso()
            write_json(self.paths.backlog_file(backlog_id), item)
            dropped_ids.add(backlog_id)
        return dropped_ids

    def _load_backlog_item(self, backlog_id: str) -> dict[str, Any]:
        if not backlog_id:
            return {}
        return read_json(self.paths.backlog_file(backlog_id))

    def _save_backlog_item(self, item: dict[str, Any]) -> None:
        backlog_id = str(item.get("backlog_id") or "").strip()
        if not backlog_id:
            return
        item["updated_at"] = utc_now_iso()
        write_json(self.paths.backlog_file(backlog_id), item)
        self._refresh_backlog_markdown()

    def _refresh_backlog_markdown(self) -> None:
        self._drop_non_actionable_backlog_items()
        items = self._iter_backlog_items()
        active_items = [
            item
            for item in items
            if self._is_active_backlog_status(str(item.get("status") or ""))
        ]
        completed_items = [
            item for item in items if str(item.get("status") or "").strip().lower() == "done"
        ]
        self.paths.shared_backlog_file.write_text(
            render_backlog_markdown(active_items),
            encoding="utf-8",
        )
        self.paths.shared_completed_backlog_file.write_text(
            render_backlog_markdown(
                completed_items,
                title="Completed Backlog",
                empty_message="completed backlog 없음",
            ),
            encoding="utf-8",
        )

    def _load_sprint_state(self, sprint_id: str) -> dict[str, Any]:
        if not sprint_id:
            return {}
        sprint_state = read_json(self.paths.sprint_file(sprint_id))
        if not sprint_state:
            return {}
        self._ensure_sprint_folder_metadata(sprint_state)
        active_sprint_id = str(self._load_scheduler_state().get("active_sprint_id") or "").strip()
        sprint_state_changed = self._recover_sprint_todos_from_requests(sprint_state)
        if self._normalize_sprint_reference_attachments(sprint_state):
            sprint_state_changed = True
        if self._synchronize_sprint_todo_backlog_state(sprint_state):
            sprint_state_changed = True
        if self._refresh_sprint_report_body(sprint_state):
            sprint_state_changed = True
        if self._refresh_sprint_history_archive(sprint_state):
            sprint_state_changed = True
        if sprint_state_changed:
            sprint_state["updated_at"] = utc_now_iso()
            write_json(self.paths.sprint_file(sprint_id), sprint_state)
            if sprint_id == active_sprint_id:
                self.paths.current_sprint_file.write_text(
                    render_current_sprint_markdown(sprint_state),
                    encoding="utf-8",
                )
        if str(sprint_state.get("sprint_folder_name") or "").strip():
            self._write_sprint_artifact_files(sprint_state)
        return sprint_state

    def _save_sprint_state(self, sprint_state: dict[str, Any]) -> None:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        if not sprint_id:
            return
        self._ensure_sprint_folder_metadata(sprint_state)
        self._recover_sprint_todos_from_requests(sprint_state)
        self._normalize_sprint_reference_attachments(sprint_state)
        self._synchronize_sprint_todo_backlog_state(sprint_state)
        self._refresh_sprint_report_body(sprint_state)
        self._refresh_sprint_history_archive(sprint_state)
        sprint_state["updated_at"] = utc_now_iso()
        write_json(self.paths.sprint_file(sprint_id), sprint_state)
        self.paths.current_sprint_file.write_text(
            render_current_sprint_markdown(sprint_state),
            encoding="utf-8",
        )
        if str(sprint_state.get("sprint_folder_name") or "").strip():
            self._write_sprint_artifact_files(sprint_state)

    def _sprint_artifact_paths(self, sprint_state: dict[str, Any]) -> dict[str, Path]:
        folder_name = str(sprint_state.get("sprint_folder_name") or "").strip()
        return {
            "root": self.paths.sprint_artifact_dir(folder_name),
            "index": self.paths.sprint_artifact_file(folder_name, "index.md"),
            "kickoff": self.paths.sprint_artifact_file(folder_name, "kickoff.md"),
            "milestone": self.paths.sprint_artifact_file(folder_name, "milestone.md"),
            "plan": self.paths.sprint_artifact_file(folder_name, "plan.md"),
            "spec": self.paths.sprint_artifact_file(folder_name, "spec.md"),
            "todo_backlog": self.paths.sprint_artifact_file(folder_name, "todo_backlog.md"),
            "iteration_log": self.paths.sprint_artifact_file(folder_name, "iteration_log.md"),
            "report": self.paths.sprint_artifact_file(folder_name, "report.md"),
        }

    def _render_sprint_kickoff_markdown(self, sprint_state: dict[str, Any]) -> str:
        source_request_id = str(sprint_state.get("kickoff_source_request_id") or "").strip()
        source_request_path = (
            str(self.paths.request_file(source_request_id).relative_to(self.paths.workspace_root))
            if source_request_id
            else "N/A"
        )
        kickoff_requirements = [
            str(item).strip()
            for item in (sprint_state.get("kickoff_requirements") or [])
            if str(item).strip()
        ]
        kickoff_reference_artifacts = [
            str(item).strip()
            for item in (sprint_state.get("kickoff_reference_artifacts") or [])
            if str(item).strip()
        ]
        lines = [
            "# Sprint Kickoff",
            "",
            f"- requested_milestone_title: {sprint_state.get('requested_milestone_title') or 'N/A'}",
            f"- kickoff_source_request_id: {source_request_id or 'N/A'}",
            f"- kickoff_source_request: {source_request_path}",
            f"- started_at: {sprint_state.get('started_at') or 'N/A'}",
            "",
            "## Original Request Text",
            "",
            str(sprint_state.get("kickoff_request_text") or "kickoff request text 없음").strip(),
            "",
            "## Kickoff Brief",
            "",
            str(sprint_state.get("kickoff_brief") or "kickoff brief 없음").strip(),
            "",
            "## Kickoff Requirements",
            "",
        ]
        if kickoff_requirements:
            lines.extend(f"- {item}" for item in kickoff_requirements)
        else:
            lines.append("- kickoff requirement 없음")
        lines.extend(["", "## Kickoff Reference Artifacts", ""])
        if kickoff_reference_artifacts:
            lines.extend(f"- {item}" for item in kickoff_reference_artifacts)
        else:
            lines.append("- kickoff reference artifact 없음")
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_sprint_milestone_markdown(self, sprint_state: dict[str, Any]) -> str:
        latest = list(sprint_state.get("planning_iterations") or [])
        latest_entry = latest[-1] if latest else {}
        lines = [
            "# Sprint Milestone",
            "",
            f"- requested_milestone_title: {sprint_state.get('requested_milestone_title') or 'N/A'}",
            f"- revised_milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
            f"- sprint_name: {sprint_state.get('sprint_name') or sprint_state.get('sprint_display_name') or 'N/A'}",
            f"- phase: {sprint_state.get('phase') or 'N/A'}",
            f"- started_at: {sprint_state.get('started_at') or 'N/A'}",
            "",
            "## Kickoff Source",
            "",
            "- Preserve the original kickoff brief in `kickoff.md`.",
            "- Use this file for refined milestone framing only.",
            "",
            "## Latest Derived Framing",
            "",
            str(latest_entry.get("summary") or "planner output 없음").strip(),
            "",
        ]
        return "\n".join(lines).rstrip() + "\n"

    def _render_sprint_plan_markdown(self, sprint_state: dict[str, Any]) -> str:
        latest = list(sprint_state.get("planning_iterations") or [])
        latest_entry = latest[-1] if latest else {}
        lines = [
            "# Sprint Plan",
            "",
            f"- sprint_name: {sprint_state.get('sprint_name') or 'N/A'}",
            f"- milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
            f"- requested_milestone_title: {sprint_state.get('requested_milestone_title') or 'N/A'}",
            f"- initial_phase_ready_at: {sprint_state.get('initial_phase_ready_at') or 'N/A'}",
            "",
            "## Latest Planner Summary",
            "",
            str(latest_entry.get("summary") or "planner output 없음").strip(),
            "",
        ]
        return "\n".join(lines).rstrip() + "\n"

    def _collect_sprint_request_entries(self, sprint_state: dict[str, Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        seen_request_ids: set[str] = set()
        for todo in list(sprint_state.get("todos") or []):
            request_id = str(todo.get("request_id") or "").strip()
            if not request_id or request_id in seen_request_ids:
                continue
            seen_request_ids.add(request_id)
            request_record = self._load_request(request_id)
            if not request_record:
                continue
            entries.append({"todo": dict(todo), "request": request_record})
        return entries

    @staticmethod
    def _collect_role_report_events(request_record: dict[str, Any]) -> list[dict[str, Any]]:
        events = []
        for event in list(request_record.get("events") or []):
            if str(event.get("type") or "").strip() != "role_report":
                continue
            payload = dict(event.get("payload") or {}) if isinstance(event.get("payload"), dict) else {}
            if not payload:
                continue
            events.append({"event": event, "payload": payload})
        return events

    def _append_role_report_details(
        self,
        lines: list[str],
        payload: dict[str, Any],
        *,
        include_workflow_transition: bool,
    ) -> None:
        lines.extend(
            [
                f"- status: {payload.get('status') or 'N/A'}",
                f"- summary: {payload.get('summary') or ''}",
            ]
        )
        insights = _normalize_insights(payload)
        if insights:
            lines.extend(["", "##### Insights", ""])
            lines.extend(f"- {item}" for item in insights)
        structured = dict(payload.get("proposals") or {}) if isinstance(payload.get("proposals"), dict) else {}
        if not include_workflow_transition:
            structured.pop("workflow_transition", None)
        if structured:
            lines.extend(["", "##### Structured Output", ""])
            _append_markdown_structure(lines, structured)
        transition = self._workflow_transition(payload)
        if include_workflow_transition and any(_has_markdown_value(item) for item in transition.values()):
            lines.extend(["", "##### Workflow Transition", ""])
            _append_markdown_structure(lines, transition)
        artifacts = [str(item).strip() for item in (payload.get("artifacts") or []) if str(item).strip()]
        if artifacts:
            lines.extend(["", "##### Artifacts", ""])
            lines.extend(f"- {item}" for item in artifacts)
        lines.append("")

    def _render_sprint_spec_markdown(self, sprint_state: dict[str, Any]) -> str:
        latest = list(sprint_state.get("planning_iterations") or [])
        latest_entry = latest[-1] if latest else {}
        insights = [
            str(item).strip()
            for item in (latest_entry.get("insights") or [])
            if str(item).strip()
        ]
        lines = [
            "# Sprint Spec",
            "",
            f"- sprint_name: {sprint_state.get('sprint_name') or 'N/A'}",
            f"- milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
            f"- requested_milestone_title: {sprint_state.get('requested_milestone_title') or 'N/A'}",
            "",
            "## Planner Insights",
            "",
        ]
        if insights:
            lines.extend(f"- {item}" for item in insights)
        else:
            lines.append("- planner insight 없음")
        request_entries = self._collect_sprint_request_entries(sprint_state)
        if request_entries:
            lines.extend(
                [
                    "",
                    "## Canonical Contract Body",
                    "",
                    "- 이 본문은 `.teams_runtime/requests/*.json`의 workflow role report를 합쳐 만든 sprint-level source of truth입니다.",
                    "- role-private handoff와 runtime 메모는 보조 근거로 남기고, shared 계약과 검증 결론은 여기서 우선 확인합니다.",
                    "",
                ]
            )
            for entry in request_entries:
                todo = dict(entry.get("todo") or {})
                request_record = dict(entry.get("request") or {})
                lines.extend(
                    [
                        f"### {todo.get('title') or request_record.get('scope') or 'Untitled'}",
                        f"- backlog_id: {todo.get('backlog_id') or request_record.get('backlog_id') or 'N/A'}",
                        f"- todo_id: {todo.get('todo_id') or request_record.get('todo_id') or 'N/A'}",
                        f"- request_id: {request_record.get('request_id') or 'N/A'}",
                        f"- final_status: {request_record.get('status') or 'N/A'}",
                        f"- scope: {request_record.get('scope') or ''}",
                        "",
                    ]
                )
                role_counts: dict[str, int] = {}
                role_reports = self._collect_role_report_events(request_record)
                if not role_reports:
                    lines.append("- role report 없음")
                    lines.append("")
                    continue
                for report in role_reports:
                    payload = dict(report.get("payload") or {})
                    role = str(payload.get("role") or report["event"].get("actor") or "").strip().lower()
                    if not role:
                        role = "unknown"
                    role_counts[role] = role_counts.get(role, 0) + 1
                    role_title = SPRINT_ROLE_DISPLAY_NAMES.get(role, role.title())
                    if role_counts[role] > 1:
                        role_title = f"{role_title} #{role_counts[role]}"
                    lines.extend([f"#### {role_title}", ""])
                    self._append_role_report_details(lines, payload, include_workflow_transition=True)
        lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _render_sprint_todo_backlog_markdown(self, sprint_state: dict[str, Any]) -> str:
        lines = [
            "# Sprint Todo Backlog",
            "",
            f"- sprint_name: {sprint_state.get('sprint_name') or 'N/A'}",
            f"- milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
            "",
            "## Items",
            "",
        ]
        selected_items = list(sprint_state.get("selected_items") or [])
        if not selected_items:
            lines.append("- selected backlog 없음")
            return "\n".join(lines).rstrip() + "\n"
        for item in selected_items:
            lines.extend(
                [
                    f"### {item.get('title') or 'Untitled'}",
                    f"- backlog_id: {item.get('backlog_id') or ''}",
                    f"- status: {item.get('status') or ''}",
                    f"- priority_rank: {item.get('priority_rank') or 0}",
                    f"- summary: {item.get('summary') or ''}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    def _render_sprint_iteration_log_markdown(self, sprint_state: dict[str, Any]) -> str:
        lines = ["# Sprint Iteration Log", ""]
        iterations = list(sprint_state.get("planning_iterations") or [])
        lines.extend(["## Planning Sync", ""])
        if not iterations:
            lines.append("- planning iteration 없음")
            lines.append("")
        else:
            for entry in iterations:
                lines.extend(
                    [
                        f"### {entry.get('created_at') or 'N/A'} | {entry.get('phase') or 'N/A'}",
                        f"- request_id: {entry.get('request_id') or 'N/A'}",
                        f"- summary: {entry.get('summary') or ''}",
                        f"- phase_ready: {'yes' if entry.get('phase_ready') else 'no'}",
                        f"- artifacts: {', '.join(str(item) for item in (entry.get('artifacts') or [])) or 'N/A'}",
                        "",
                    ]
                )
                insights = [str(item).strip() for item in (entry.get("insights") or []) if str(item).strip()]
                if insights:
                    lines.append("#### Insights")
                    lines.extend(f"- {item}" for item in insights)
                    lines.append("")
        request_entries = self._collect_sprint_request_entries(sprint_state)
        lines.extend(["## Workflow Validation Trace", ""])
        if not request_entries:
            lines.append("- workflow trace 없음")
            lines.append("")
            return "\n".join(lines).rstrip() + "\n"
        for entry in request_entries:
            todo = dict(entry.get("todo") or {})
            request_record = dict(entry.get("request") or {})
            lines.extend(
                [
                    f"### {todo.get('title') or request_record.get('scope') or 'Untitled'}",
                    f"- backlog_id: {todo.get('backlog_id') or request_record.get('backlog_id') or 'N/A'}",
                    f"- todo_id: {todo.get('todo_id') or request_record.get('todo_id') or 'N/A'}",
                    f"- request_id: {request_record.get('request_id') or 'N/A'}",
                    f"- final_status: {request_record.get('status') or 'N/A'}",
                    "",
                ]
            )
            for event in list(request_record.get("events") or []):
                timestamp = str(event.get("timestamp") or "N/A").strip() or "N/A"
                actor = str(event.get("actor") or "unknown").strip() or "unknown"
                event_type = str(event.get("type") or "event").strip() or "event"
                lines.extend([f"#### {timestamp} | {actor} | {event_type}", ""])
                lines.append(f"- summary: {event.get('summary') or ''}")
                payload = dict(event.get("payload") or {}) if isinstance(event.get("payload"), dict) else {}
                if event_type == "delegated":
                    routing_context = dict(payload.get("routing_context") or {})
                    if routing_context:
                        lines.append(
                            f"- selected_role: {routing_context.get('selected_role') or routing_context.get('requested_role') or 'N/A'}"
                        )
                        lines.append(f"- reason: {routing_context.get('reason') or ''}")
                if payload and event_type == "role_report":
                    lines.append(f"- role: {payload.get('role') or actor}")
                    self._append_role_report_details(lines, payload, include_workflow_transition=True)
                else:
                    lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _inspect_sprint_documentation_closeout(self, sprint_state: dict[str, Any]) -> dict[str, Any]:
        if not str(sprint_state.get("sprint_folder_name") or "").strip():
            return {"status": "verified", "message": "sprint artifact folder가 없어 문서 closeout 검증을 생략했습니다."}
        paths = self._sprint_artifact_paths(sprint_state)
        missing_sections: list[str] = []
        spec_text = paths["spec"].read_text(encoding="utf-8") if paths["spec"].exists() else ""
        iteration_text = paths["iteration_log"].read_text(encoding="utf-8") if paths["iteration_log"].exists() else ""
        if "## Canonical Contract Body" not in spec_text:
            missing_sections.append("spec.canonical_contract_body")
        if "## Workflow Validation Trace" not in iteration_text:
            missing_sections.append("iteration_log.workflow_validation_trace")
        if missing_sections:
            return {
                "status": "planning_incomplete",
                "message": "shared spec/iteration 문서가 canonical 계약 본문과 workflow 검증 추적을 아직 닫지 못했습니다.",
                "missing_sections": missing_sections,
            }
        return {"status": "verified", "message": "shared spec/iteration 문서 closeout 검증을 통과했습니다."}

    def _write_sprint_artifact_files(self, sprint_state: dict[str, Any]) -> None:
        paths = self._sprint_artifact_paths(sprint_state)
        paths["root"].mkdir(parents=True, exist_ok=True)
        paths["index"].write_text(render_sprint_artifact_index_markdown(sprint_state), encoding="utf-8")
        paths["kickoff"].write_text(self._render_sprint_kickoff_markdown(sprint_state), encoding="utf-8")
        paths["milestone"].write_text(self._render_sprint_milestone_markdown(sprint_state), encoding="utf-8")
        paths["plan"].write_text(self._render_sprint_plan_markdown(sprint_state), encoding="utf-8")
        paths["spec"].write_text(self._render_sprint_spec_markdown(sprint_state), encoding="utf-8")
        paths["todo_backlog"].write_text(self._render_sprint_todo_backlog_markdown(sprint_state), encoding="utf-8")
        paths["iteration_log"].write_text(self._render_sprint_iteration_log_markdown(sprint_state), encoding="utf-8")
        report_body = str(sprint_state.get("report_body") or "").strip() or self._render_live_sprint_report_markdown(
            sprint_state
        )
        paths["report"].write_text(report_body.rstrip() + "\n", encoding="utf-8")

    def _required_sprint_preflight_artifact_paths(self, sprint_state: dict[str, Any]) -> list[Path]:
        paths = self._sprint_artifact_paths(sprint_state)
        required = [self.paths.current_sprint_file]
        required.extend(paths[key] for key in SPRINT_SPEC_TODO_REPORT_DOC_KEYS)
        deduped: list[Path] = []
        seen: set[str] = set()
        for path in required:
            normalized = str(path.resolve())
            if normalized in seen:
                continue
            seen.add(normalized)
            deduped.append(path)
        return deduped

    def _missing_sprint_preflight_artifacts(self, sprint_state: dict[str, Any]) -> list[str]:
        missing: list[str] = []
        for path in self._required_sprint_preflight_artifact_paths(sprint_state):
            if not path.exists() or not path.read_text(encoding="utf-8").strip():
                missing.append(self._workspace_artifact_hint(path))
        return missing

    @staticmethod
    def _report_section(title: str, lines: Iterable[str] | None) -> ReportSection:
        normalized_lines = tuple(str(item).rstrip() for item in (lines or []) if str(item).strip())
        return ReportSection(title=str(title or "").strip(), lines=normalized_lines)

    @staticmethod
    def _split_report_body_lines(body: str) -> list[str]:
        return [str(line).rstrip() for line in str(body or "").splitlines() if str(line).strip()]

    @staticmethod
    def _format_priority_value(value: Any) -> str:
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return "N/A"
        return str(normalized) if normalized > 0 else "N/A"

    def _format_backlog_report_line(self, item: dict[str, Any]) -> str:
        priority = self._format_priority_value(item.get("priority_rank"))
        status = str(item.get("status") or "").strip() or "N/A"
        title = str(item.get("title") or item.get("backlog_id") or "Untitled").strip()
        backlog_id = str(item.get("backlog_id") or "N/A").strip()
        return f"- [rank {priority}] [{status}] {title} | backlog_id={backlog_id}"

    def _format_todo_report_line(self, todo: dict[str, Any], *, include_artifacts: bool = False) -> str:
        priority = self._format_priority_value(todo.get("priority_rank"))
        status = str(todo.get("status") or "").strip() or "N/A"
        title = str(todo.get("title") or todo.get("todo_id") or "Untitled").strip()
        request_id = str(todo.get("request_id") or "N/A").strip()
        line = f"- [rank {priority}] [{status}] {title} | request_id={request_id}"
        if include_artifacts:
            artifact_count = len([item for item in (todo.get("artifacts") or []) if str(item).strip()])
            if artifact_count:
                line += f" | artifacts={artifact_count}"
        return line

    def _build_generic_sprint_report_sections(self, body: str) -> list[ReportSection]:
        body_lines = self._split_report_body_lines(body)
        if not body_lines:
            return []
        return [self._report_section("상세", body_lines)]

    def _build_sprint_kickoff_report_sections(self, sprint_state: dict[str, Any]) -> list[ReportSection]:
        kickoff_lines = [
            f"- sprint_id: {sprint_state.get('sprint_id') or 'N/A'}",
            f"- trigger: {sprint_state.get('trigger') or 'N/A'}",
            f"- selected_backlog: {len(sprint_state.get('selected_items') or [])}",
        ]
        selected_lines = self._build_sprint_kickoff_preview_lines(
            sprint_state,
            limit=max(1, len(sprint_state.get("todos") or []) or len(sprint_state.get("selected_items") or []) or 3),
        )
        return [
            self._report_section("킥오프", kickoff_lines),
            self._report_section("선정 작업", selected_lines),
        ]

    def _build_sprint_todo_list_report_sections(self, sprint_state: dict[str, Any]) -> list[ReportSection]:
        todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
        summary_lines = [
            f"- sprint_id: {sprint_state.get('sprint_id') or 'N/A'}",
            f"- todo_count: {len(todos)}",
        ]
        todo_lines = [self._format_todo_report_line(todo, include_artifacts=True) for todo in todos] or ["- todo 없음"]
        return [
            self._report_section("현재 상태", summary_lines),
            self._report_section("전체 Todo", todo_lines),
        ]

    def _build_sprint_spec_todo_report_sections(self, sprint_state: dict[str, Any]) -> list[ReportSection]:
        latest = list(sprint_state.get("planning_iterations") or [])
        latest_entry = latest[-1] if latest else {}
        milestone_title = str(
            sprint_state.get("milestone_title") or sprint_state.get("requested_milestone_title") or ""
        ).strip() or "없음"
        requested_milestone = str(sprint_state.get("requested_milestone_title") or "").strip() or "없음"
        todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
        backlog_items = self._collect_sprint_relevant_backlog_items(sprint_state)
        kickoff_requirements = [
            str(item).strip()
            for item in (sprint_state.get("kickoff_requirements") or [])
            if str(item).strip()
        ]
        artifact_paths = self._required_sprint_preflight_artifact_paths(sprint_state)
        sections = [
            self._report_section(
                "핵심 결론",
                [
                    f"- planner summary: {str(latest_entry.get('summary') or '').strip() or '없음'}",
                    f"- selected_count: {len(todos or sprint_state.get('selected_items') or [])}",
                ],
            ),
            self._report_section(
                "마일스톤",
                [
                    f"- requested: {requested_milestone}",
                    f"- active: {milestone_title}",
                ],
            ),
        ]
        if todos:
            sections.append(
                self._report_section(
                    "정의된 작업",
                    [self._format_todo_report_line(todo, include_artifacts=True) for todo in todos],
                )
            )
        else:
            sections.append(
                self._report_section(
                    "정의된 작업",
                    self._build_sprint_kickoff_preview_lines(
                        sprint_state,
                        limit=max(1, len(sprint_state.get("selected_items") or []) or 3),
                    ),
                )
            )
        if backlog_items:
            sections.append(
                self._report_section(
                    "우선순위",
                    [self._format_backlog_report_line(item) for item in backlog_items],
                )
            )
        else:
            sections.append(self._report_section("우선순위", ["- 우선순위 backlog 없음"]))
        evidence_lines = [f"- requirement: {item}" for item in kickoff_requirements]
        evidence_lines.extend(f"- doc: {self._workspace_artifact_hint(path)}" for path in artifact_paths)
        sections.append(self._report_section("근거 문서", evidence_lines[:12]))
        return sections

    def _build_terminal_sprint_report_sections(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> list[ReportSection]:
        snapshot = self._collect_sprint_report_snapshot(sprint_state, closeout_result)
        return [
            self._report_section("한눈에 보기", self._build_sprint_overview_lines(sprint_state, snapshot, full_detail=True)),
            self._report_section("변경 요약", self._build_sprint_change_summary_lines(sprint_state, snapshot, full_detail=True)),
            self._report_section("Sprint A to Z", self._build_sprint_timeline_lines(sprint_state, snapshot, full_detail=True)),
            self._report_section("에이전트 기여", self._build_sprint_agent_contribution_lines(sprint_state, snapshot, full_detail=True)),
            self._report_section("핵심 이슈", self._build_sprint_issue_lines(sprint_state, snapshot, full_detail=True)),
            self._report_section("성과", self._build_sprint_achievement_lines(sprint_state, snapshot, full_detail=True)),
            self._report_section("참고 아티팩트", self._build_sprint_artifact_lines(sprint_state, snapshot, full_detail=True)),
        ]

    def _build_sprint_spec_todo_report_body(self, sprint_state: dict[str, Any]) -> str:
        latest = list(sprint_state.get("planning_iterations") or [])
        latest_entry = latest[-1] if latest else {}
        milestone_title = str(
            sprint_state.get("milestone_title") or sprint_state.get("requested_milestone_title") or ""
        ).strip() or "없음"
        requested_milestone = str(sprint_state.get("requested_milestone_title") or "").strip() or "없음"
        lines = [
            f"sprint_id: {sprint_state.get('sprint_id') or ''}",
            "",
            "[Milestone]",
            f"- active: {milestone_title}",
            f"- requested: {requested_milestone}",
            "",
            "[Spec]",
            f"- planner summary: {str(latest_entry.get('summary') or '').strip() or '없음'}",
            "- kickoff requirements:",
        ]
        kickoff_requirements = [
            str(item).strip()
            for item in (sprint_state.get("kickoff_requirements") or [])
            if str(item).strip()
        ]
        if kickoff_requirements:
            lines.extend(f"  - {item}" for item in kickoff_requirements[:5])
        else:
            lines.append("  - 없음")
        lines.append("- planner insights:")
        insights = [
            str(item).strip()
            for item in (latest_entry.get("insights") or [])
            if str(item).strip()
        ]
        if insights:
            lines.extend(f"  - {item}" for item in insights[:5])
        else:
            lines.append("  - 없음")
        lines.extend(
            [
                "",
                "[TODO]",
            ]
        )
        todo_lines = self._build_sprint_kickoff_preview_lines(sprint_state, limit=10)
        lines.append(f"- selected_count: {len(sprint_state.get('todos') or sprint_state.get('selected_items') or [])}")
        lines.append("- items:")
        lines.extend([f"  {item}" for item in todo_lines] if todo_lines else ["  - 선택된 작업 없음"])
        return "\n".join(lines)

    async def _send_sprint_spec_todo_report(
        self,
        sprint_state: dict[str, Any],
        *,
        title: str = "📐 스프린트 Spec/TODO",
        judgment: str = "implementation 시작 전 spec/todo canonical 보고를 남겼습니다.",
        next_action: str = "implementation 진행",
        swallow_exceptions: bool = False,
    ) -> None:
        artifact_paths = self._required_sprint_preflight_artifact_paths(sprint_state)
        await self._send_sprint_report(
            title=title,
            body=self._build_sprint_spec_todo_report_body(sprint_state),
            sprint_id=str(sprint_state.get("sprint_id") or ""),
            judgment=judgment,
            next_action=next_action,
            related_artifacts=[self._workspace_artifact_hint(path) for path in artifact_paths],
            log_summary=self._build_sprint_spec_todo_report_body(sprint_state),
            sections=self._build_sprint_spec_todo_report_sections(sprint_state),
            swallow_exceptions=swallow_exceptions,
        )

    def _append_sprint_event(self, sprint_id: str, *, event_type: str, summary: str, payload: dict[str, Any] | None = None) -> None:
        append_jsonl(
            self.paths.sprint_events_file(sprint_id),
            {
                "timestamp": utc_now_iso(),
                "type": event_type,
                "summary": summary,
                "payload": dict(payload or {}),
            },
        )

    def _archive_sprint_history(self, sprint_state: dict[str, Any], report_body: str) -> str:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        history_path = self.paths.sprint_history_file(sprint_id)
        history_path.write_text(
            render_sprint_history_markdown(sprint_state, report_body),
            encoding="utf-8",
        )
        existing_rows = load_sprint_history_index(self.paths.sprint_history_index_file)
        self.paths.sprint_history_index_file.write_text(
            render_sprint_history_index(existing_rows, sprint_state),
            encoding="utf-8",
        )
        return str(history_path)

    def _should_refresh_sprint_history_archive(self, sprint_state: dict[str, Any]) -> bool:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        if not sprint_id:
            return False
        report_body = str(sprint_state.get("report_body") or "").strip()
        if not report_body:
            return False
        status = str(sprint_state.get("status") or "").strip().lower()
        ended_at = str(sprint_state.get("ended_at") or "").strip()
        return bool(ended_at or status in {"completed", "failed", "blocked", "closeout"})

    def _refresh_sprint_history_archive(self, sprint_state: dict[str, Any]) -> bool:
        if not self._should_refresh_sprint_history_archive(sprint_state):
            return False
        archived_path = self._archive_sprint_history(
            sprint_state,
            str(sprint_state.get("report_body") or "").strip(),
        )
        if str(sprint_state.get("report_path") or "").strip() == archived_path:
            return False
        sprint_state["report_path"] = archived_path
        return True

    def _classify_backlog_kind(self, intent: str, scope: str, summary: str = "") -> str:
        combined = " ".join([str(intent or ""), str(scope or ""), str(summary or "")]).lower()
        if any(token in combined for token in ("bug", "error", "fix", "회귀", "실패", "오류")):
            return "bug"
        if any(token in combined for token in ("feature", "new feature", "기능 추가", "신규 기능")):
            return "feature"
        if any(token in combined for token in ("chore", "cleanup", "정리", "문서", "docs")):
            return "chore"
        return "enhancement"

    @staticmethod
    def _normalize_backlog_acceptance_criteria(values: Any) -> list[str]:
        if isinstance(values, list):
            return [str(item).strip() for item in values if str(item).strip()]
        if isinstance(values, str):
            normalized = str(values).strip()
            return [normalized] if normalized else []
        return []

    def _select_backlog_items_for_sprint(self) -> list[dict[str, Any]]:
        if self._drop_non_actionable_backlog_items():
            self._refresh_backlog_markdown()
        repaired_ids = self._repair_non_actionable_carry_over_backlog_items()
        if repaired_ids:
            self._refresh_backlog_markdown()
        pending = [
            item
            for item in self._iter_backlog_items()
            if self._is_actionable_backlog_status(str(item.get("status") or ""))
        ]
        def priority(item: dict[str, Any]) -> tuple[int, int, str]:
            priority_rank = int(item.get("priority_rank") or 0)
            if priority_rank > 0:
                return (-priority_rank, 0, str(item.get("created_at") or ""))
            source_rank = 0 if str(item.get("source") or "") == "user" else 1
            kind = str(item.get("kind") or "").strip().lower()
            kind_rank = {"bug": 0, "feature": 1, "enhancement": 2, "chore": 3}.get(kind, 4)
            return (source_rank, kind_rank, str(item.get("created_at") or ""))
        pending.sort(key=priority)
        return pending

    def _perform_backlog_sourcing(self) -> tuple[int, int, list[dict[str, Any]]]:
        with self._backlog_sourcing_lock:
            candidates = self._discover_backlog_candidates()
            sourcing_activity = dict(self._last_backlog_sourcing_activity)
            normalized_candidates = self._normalize_sourcer_review_candidates(candidates)
            if normalized_candidates:
                fingerprint = self._build_sourcer_review_fingerprint(normalized_candidates)
                existing = self._find_open_sourcer_review_request(fingerprint)
                scheduler_state = self._load_scheduler_state()
                last_review_fingerprint = str(scheduler_state.get("last_sourcing_fingerprint") or "").strip()
                last_review_status = str(scheduler_state.get("last_sourcing_review_status") or "").strip().lower()
                last_review_request_id = str(scheduler_state.get("last_sourcing_review_request_id") or "").strip()
                if existing:
                    sourcing_activity["suppressed_duplicate_count"] = len(normalized_candidates)
                    sourcing_activity["suppressed_duplicate_fingerprint"] = fingerprint
                    sourcing_activity["duplicate_request_id"] = str(existing.get("request_id") or "").strip()
                    sourcing_activity["duplicate_review_status"] = "queued_for_planner_review"
                    sourcing_activity["summary"] = "이미 planner review로 전달된 sourcer 후보라 재보고를 건너뛰었습니다."
                    self._last_backlog_sourcing_activity = dict(sourcing_activity)
                    return (0, 0, [])
                if (
                    fingerprint
                    and fingerprint == last_review_fingerprint
                    and last_review_status in {"completed", "committed", "failed", "blocked", "cancelled"}
                ):
                    sourcing_activity["suppressed_duplicate_count"] = len(normalized_candidates)
                    sourcing_activity["suppressed_duplicate_fingerprint"] = fingerprint
                    sourcing_activity["duplicate_request_id"] = last_review_request_id
                    sourcing_activity["duplicate_review_status"] = last_review_status
                    sourcing_activity["summary"] = "이미 보고한 sourcer 후보라 재발 근거 없이 반복 보고하지 않습니다."
                    self._last_backlog_sourcing_activity = dict(sourcing_activity)
                    return (0, 0, [])
            if not candidates:
                sourcing_activity["added_count"] = 0
                sourcing_activity["updated_count"] = 0
                self._last_backlog_sourcing_activity = dict(sourcing_activity)
                self._report_sourcer_activity_sync(
                    sourcing_activity=sourcing_activity,
                    added=0,
                    updated=0,
                    candidates=[],
                )
                return (0, 0, [])
            sourcing_activity["added_count"] = 0
            sourcing_activity["updated_count"] = 0
            self._last_backlog_sourcing_activity = dict(sourcing_activity)
            self._report_sourcer_activity_sync(
                sourcing_activity=sourcing_activity,
                added=0,
                updated=0,
                candidates=candidates,
            )
            return (0, 0, candidates)

    def _prepare_actionable_backlog_for_sprint(self) -> list[dict[str, Any]]:
        return self._select_backlog_items_for_sprint()

    async def _maybe_queue_blocked_backlog_review_for_autonomous_start(
        self,
        state: dict[str, Any],
    ) -> bool:
        blocked_candidates = await asyncio.to_thread(self._collect_blocked_backlog_review_candidates)
        if not blocked_candidates:
            if any(
                str(state.get(field) or "").strip()
                for field in (
                    "last_blocked_review_at",
                    "last_blocked_review_request_id",
                    "last_blocked_review_status",
                    "last_blocked_review_fingerprint",
                )
            ):
                self._clear_blocked_backlog_review_state(state)
                self._save_scheduler_state(state)
            return False
        fingerprint = self._build_blocked_backlog_review_fingerprint(blocked_candidates)
        existing = self._find_open_blocked_backlog_review_request(fingerprint)
        if existing:
            state["last_blocked_review_at"] = utc_now_iso()
            state["last_blocked_review_request_id"] = str(existing.get("request_id") or "")
            state["last_blocked_review_status"] = "queued_for_planner_review"
            state["last_blocked_review_fingerprint"] = fingerprint
            self._save_scheduler_state(state)
            return True
        last_status = str(state.get("last_blocked_review_status") or "").strip().lower()
        if (
            str(state.get("last_blocked_review_fingerprint") or "").strip() == fingerprint
            and last_status in {"completed", "committed", "failed", "blocked", "cancelled"}
        ):
            return False
        review_result = await self._queue_blocked_backlog_for_planner_review(blocked_candidates)
        request_id = str(review_result.get("request_id") or "")
        if not request_id:
            return False
        state["last_blocked_review_at"] = utc_now_iso()
        state["last_blocked_review_request_id"] = request_id
        state["last_blocked_review_status"] = "queued_for_planner_review"
        state["last_blocked_review_fingerprint"] = fingerprint
        self._save_scheduler_state(state)
        return True

    def _backlog_sourcing_interval_seconds(self) -> float:
        return max(float(self.runtime_config.sprint_interval_minutes) * 60.0, BACKLOG_SOURCING_POLL_SECONDS)

    async def _backlog_sourcing_loop(self) -> None:
        await asyncio.sleep(2.0)
        while True:
            try:
                await self._poll_backlog_sourcing_once()
            except Exception:
                LOGGER.exception("Backlog sourcing loop failed in orchestrator")
            await asyncio.sleep(BACKLOG_SOURCING_POLL_SECONDS)

    async def _poll_backlog_sourcing_once(self) -> None:
        state = self._load_scheduler_state()
        last_sourced_at = self._parse_datetime(state.get("last_sourced_at") or "")
        now = utc_now()
        if last_sourced_at is not None:
            elapsed = (now - last_sourced_at).total_seconds()
            if elapsed < self._backlog_sourcing_interval_seconds():
                return
        added, updated, candidates = await asyncio.to_thread(self._perform_backlog_sourcing)
        state["last_sourced_at"] = utc_now_iso()
        if not (added or updated):
            if not candidates:
                suppressed_fingerprint = str(
                    self._last_backlog_sourcing_activity.get("suppressed_duplicate_fingerprint") or ""
                ).strip()
                if suppressed_fingerprint:
                    state["last_sourcing_status"] = "duplicate_suppressed"
                    state["last_sourcing_request_id"] = str(
                        self._last_backlog_sourcing_activity.get("duplicate_request_id") or ""
                    ).strip()
                    state["last_sourcing_fingerprint"] = suppressed_fingerprint
                else:
                    state["last_sourcing_status"] = "no_changes"
                    state["last_sourcing_request_id"] = ""
                self._save_scheduler_state(state)
                return
            review_result = await self._queue_sourcer_candidates_for_planner_review(
                candidates,
                sourcing_activity=dict(self._last_backlog_sourcing_activity),
            )
            state["last_sourcing_status"] = (
                "queued_for_planner_review" if review_result.get("request_id") else "no_changes"
            )
            state["last_sourcing_request_id"] = str(review_result.get("request_id") or "")
            state["last_sourcing_fingerprint"] = str(review_result.get("fingerprint") or "").strip()
            state["last_sourcing_review_status"] = state["last_sourcing_status"]
            state["last_sourcing_review_request_id"] = str(review_result.get("request_id") or "")
            self._save_scheduler_state(state)
            if not review_result.get("request_id"):
                return
            await self._send_sprint_report(
                title="Backlog Sourcing",
                body=(
                    f"candidate_count={len(candidates)}\n"
                    f"planner_review_request_id={review_result.get('request_id') or ''}\n"
                    f"planner_review_status={'reused' if review_result.get('reused') else 'delegated' if review_result.get('relay_sent') else 'relay_failed'}\n"
                    f"items={', '.join(str(item.get('title') or '') for item in candidates[:5]) or 'N/A'}"
                ),
            )
            return
        state["last_sourcing_status"] = "updated"
        state["last_sourcing_request_id"] = ""
        self._save_scheduler_state(state)
        await self._send_sprint_report(
            title="Backlog Sourcing",
            body=(
                f"added={added}\n"
                f"updated={updated}\n"
                f"items={', '.join(str(item.get('title') or '') for item in candidates[:5]) or 'N/A'}"
            ),
        )

    def _discover_backlog_candidates(self) -> list[dict[str, Any]]:
        findings = self._build_backlog_sourcing_findings()
        findings_sample = [
            _truncate_text(item.get("title") or item.get("scope") or item.get("summary") or "", limit=80)
            for item in findings[:3]
            if _truncate_text(item.get("title") or item.get("scope") or item.get("summary") or "", limit=80)
        ]
        if not findings:
            self._last_backlog_sourcing_activity = {
                "status": "completed",
                "summary": "수집할 backlog finding이 없어 sourcer 실행을 건너뛰었습니다.",
                "error": "",
                "mode": "idle",
                "findings_count": 0,
                "candidate_count": 0,
                "filtered_candidate_count": 0,
                "raw_backlog_items_count": 0,
                "existing_backlog_count": 0,
                "findings_sample": [],
                "session_id": "",
                "session_workspace": "",
                "elapsed_ms": 0,
            }
            return []
        scheduler_state = self._load_scheduler_state()
        active_sprint = self._load_sprint_state(str(scheduler_state.get("active_sprint_id") or ""))
        existing_backlog = self._build_sourcer_existing_backlog_context()
        try:
            sourced = self.backlog_sourcer.source(
                findings=findings,
                scheduler_state=scheduler_state,
                active_sprint=active_sprint,
                backlog_counts=self._backlog_counts(),
                existing_backlog=existing_backlog,
            )
        except Exception as exc:
            LOGGER.exception("Internal backlog sourcer failed; falling back to heuristic discovery")
            fallback_candidates = self._fallback_backlog_candidates_from_findings(findings)
            self._last_backlog_sourcing_activity = {
                "status": "failed",
                "summary": "internal sourcer 실행이 실패해 fallback discovery를 사용했습니다.",
                "error": str(exc),
                "mode": "fallback",
                "findings_count": len(findings),
                "candidate_count": len(fallback_candidates),
                "filtered_candidate_count": len(fallback_candidates),
                "raw_backlog_items_count": 0,
                "existing_backlog_count": len(existing_backlog),
                "findings_sample": findings_sample,
                "fallback_reason": str(exc),
                "session_id": "",
                "session_workspace": "",
                "elapsed_ms": 0,
            }
            return fallback_candidates
        monitoring = dict(sourced.get("monitoring") or {}) if isinstance(sourced.get("monitoring"), dict) else {}
        raw_items = sourced.get("backlog_items")
        if not isinstance(raw_items, list) or not raw_items:
            fallback_candidates = self._fallback_backlog_candidates_from_findings(findings)
            self._last_backlog_sourcing_activity = {
                "status": str(sourced.get("status") or "failed").strip().lower() or "failed",
                "summary": (
                    str(sourced.get("summary") or "").strip()
                    or "internal sourcer가 backlog item을 반환하지 않아 fallback discovery를 사용했습니다."
                ),
                "error": str(sourced.get("error") or "").strip(),
                "mode": "fallback",
                "findings_count": len(findings),
                "candidate_count": len(fallback_candidates),
                "filtered_candidate_count": len(fallback_candidates),
                "raw_backlog_items_count": int(monitoring.get("raw_backlog_items_count") or 0),
                "existing_backlog_count": len(existing_backlog),
                "findings_sample": monitoring.get("findings_sample") or findings_sample,
                "existing_backlog_sample": monitoring.get("existing_backlog_sample") or [],
                "fallback_reason": (
                    str(sourced.get("error") or "").strip()
                    or "internal sourcer가 backlog item을 반환하지 않았습니다."
                ),
                "session_id": str(sourced.get("session_id") or "").strip(),
                "session_workspace": str(sourced.get("session_workspace") or "").strip(),
                "elapsed_ms": int(monitoring.get("elapsed_ms") or 0),
                "reuse_session": bool(monitoring.get("reuse_session")),
                "prompt_chars": int(monitoring.get("prompt_chars") or 0),
                "json_parse_status": str(monitoring.get("json_parse_status") or "").strip(),
            }
            return fallback_candidates
        active_sprint_milestone = str(active_sprint.get("milestone_title") or "").strip()
        candidates: list[dict[str, Any]] = []
        milestone_filtered_out_count = 0
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            item_milestone = str(item.get("milestone_title") or "").strip()
            if active_sprint_milestone and item_milestone != active_sprint_milestone:
                milestone_filtered_out_count += 1
                continue
            candidates.append(
                build_backlog_item(
                    title=title,
                    summary=str(item.get("summary") or title).strip(),
                    kind=str(item.get("kind") or "enhancement").strip().lower() or "enhancement",
                    source="sourcer",
                    scope=str(item.get("scope") or title).strip(),
                    acceptance_criteria=self._normalize_backlog_acceptance_criteria(
                        item.get("acceptance_criteria")
                    ),
                    milestone_title=item_milestone,
                    priority_rank=int(item.get("priority_rank") or 0),
                    planned_in_sprint_id=str(item.get("planned_in_sprint_id") or "").strip(),
                    added_during_active_sprint=bool(item.get("added_during_active_sprint")),
                    origin={
                        "sourcing_agent": "internal_sourcer",
                        "sourcer_summary": str(sourced.get("summary") or "").strip(),
                        **dict(item.get("origin") or {}),
                    },
                )
            )
        self._last_backlog_sourcing_activity = {
            "status": str(sourced.get("status") or "completed").strip().lower() or "completed",
            "summary": str(sourced.get("summary") or "").strip(),
            "error": str(sourced.get("error") or "").strip(),
            "mode": "internal_sourcer",
            "findings_count": len(findings),
            "candidate_count": len(candidates),
            "filtered_candidate_count": len(candidates),
            "active_sprint_milestone": active_sprint_milestone,
            "milestone_filtered_out_count": milestone_filtered_out_count,
            "raw_backlog_items_count": int(monitoring.get("raw_backlog_items_count") or len(raw_items)),
            "existing_backlog_count": len(existing_backlog),
            "findings_sample": monitoring.get("findings_sample") or findings_sample,
            "existing_backlog_sample": monitoring.get("existing_backlog_sample") or [],
            "session_id": str(sourced.get("session_id") or "").strip(),
            "session_workspace": str(sourced.get("session_workspace") or "").strip(),
            "elapsed_ms": int(monitoring.get("elapsed_ms") or 0),
            "reuse_session": bool(monitoring.get("reuse_session")),
            "prompt_chars": int(monitoring.get("prompt_chars") or 0),
            "json_parse_status": str(monitoring.get("json_parse_status") or "").strip(),
        }
        return candidates

    async def _scheduler_loop(self) -> None:
        await asyncio.sleep(2.0)
        while True:
            try:
                await self._poll_scheduler_once()
            except Exception:
                LOGGER.exception("Scheduler loop failed in orchestrator")
            await asyncio.sleep(SCHEDULER_POLL_SECONDS)

    async def _poll_scheduler_once(self) -> None:
        state = self._load_scheduler_state()
        now = utc_now()
        next_slot = self._parse_datetime(state.get("next_slot_at") or "")
        if next_slot is None:
            next_slot = compute_next_slot_at(
                now,
                interval_minutes=self.runtime_config.sprint_interval_minutes,
                timezone_name=self.runtime_config.sprint_timezone,
            )
            state["next_slot_at"] = next_slot.isoformat()
            self._save_scheduler_state(state)
        if state.get("active_sprint_id"):
            await self._resume_active_sprint(str(state.get("active_sprint_id") or ""))
            state = self._load_scheduler_state()
            if state.get("active_sprint_id") and next_slot <= now and not state.get("deferred_slot_at"):
                state["deferred_slot_at"] = next_slot.isoformat()
                state["next_slot_at"] = compute_next_slot_at(
                    now,
                    interval_minutes=self.runtime_config.sprint_interval_minutes,
                    timezone_name=self.runtime_config.sprint_timezone,
                ).isoformat()
                self._save_scheduler_state(state)
            return
        await self._maybe_request_idle_sprint_milestone(reason="idle_no_active_sprint")
        # Reload so we do not overwrite freshly-persisted milestone-request flags
        # with the stale pre-send scheduler snapshot from the beginning of this poll.
        state = self._load_scheduler_state()
        if self._uses_manual_daily_sprint():
            next_cutoff = build_sprint_cutoff_at(self.runtime_config.sprint_cutoff_time, now=now)
            if next_cutoff <= now:
                next_cutoff = next_cutoff + timedelta(days=1)
            state["next_slot_at"] = next_cutoff.isoformat()
            self._save_scheduler_state(state)
            return
        if await self._maybe_queue_blocked_backlog_review_for_autonomous_start(state):
            return
        backlog_ready = any(
            str(item.get("status") or "").strip().lower() == "pending" for item in self._iter_backlog_items()
        )
        trigger = ""
        if state.get("deferred_slot_at"):
            trigger = "deferred_slot"
            state["deferred_slot_at"] = ""
        elif self.runtime_config.sprint_mode == "hybrid" and backlog_ready:
            trigger = "backlog_ready"
        elif next_slot <= now:
            trigger = "scheduled_slot"
            state["next_slot_at"] = compute_next_slot_at(
                now,
                interval_minutes=self.runtime_config.sprint_interval_minutes,
                timezone_name=self.runtime_config.sprint_timezone,
            ).isoformat()
        if not trigger:
            self._save_scheduler_state(state)
            return
        selected_items = await asyncio.to_thread(self._prepare_actionable_backlog_for_sprint)
        if not selected_items:
            state["last_skipped_at"] = utc_now_iso()
            state["last_skip_reason"] = "no_actionable_backlog"
            self._save_scheduler_state(state)
            return
        self._save_scheduler_state(state)
        await self._run_autonomous_sprint(trigger, selected_items=selected_items)

    async def _run_autonomous_sprint(self, trigger: str, *, selected_items: list[dict[str, Any]] | None = None) -> None:
        scheduler_state = self._load_scheduler_state()
        if scheduler_state.get("active_sprint_id"):
            return
        started_at = utc_now_iso()
        sprint_id = build_active_sprint_id()
        folder_name = build_sprint_artifact_folder_name(sprint_id)
        scheduler_state["active_sprint_id"] = sprint_id
        scheduler_state["last_started_at"] = started_at
        scheduler_state["last_trigger"] = trigger
        self._clear_pending_milestone_request(scheduler_state)
        self._save_scheduler_state(scheduler_state)

        baseline = capture_git_baseline(self.paths.project_workspace_root)
        sprint_state = {
            "sprint_id": sprint_id,
            "sprint_name": sprint_id,
            "sprint_display_name": sprint_id,
            "sprint_folder": str(self.paths.sprint_artifact_dir(folder_name)),
            "sprint_folder_name": folder_name,
            "status": "planning",
            "trigger": trigger,
            "execution_mode": "auto",
            "started_at": started_at,
            "ended_at": "",
            "selected_backlog_ids": [],
            "selected_items": [],
            "todos": [],
            "reference_artifacts": [],
            "commit_sha": "",
            "commit_shas": [],
            "commit_count": 0,
            "closeout_status": "",
            "uncommitted_paths": [],
            "version_control_status": "",
            "version_control_sha": "",
            "version_control_paths": [],
            "version_control_message": "",
            "version_control_error": "",
            "auto_commit_status": "",
            "auto_commit_sha": "",
            "auto_commit_paths": [],
            "auto_commit_message": "",
            "report_path": "",
            "git_baseline": baseline,
            "resume_from_checkpoint_requested_at": "",
            "last_resume_checkpoint_todo_id": "",
            "last_resume_checkpoint_status": "",
        }
        self._save_sprint_state(sprint_state)
        self._append_sprint_event(sprint_id, event_type="started", summary=f"스프린트를 시작했습니다. trigger={trigger}")
        try:
            if selected_items is None:
                selected_items = await asyncio.to_thread(self._prepare_actionable_backlog_for_sprint)
            if not selected_items:
                sprint_state["status"] = "completed"
                sprint_state["closeout_status"] = "no_backlog_selected"
                sprint_state["ended_at"] = utc_now_iso()
                closeout_result = {
                    "status": "no_backlog_selected",
                    "message": "선택할 backlog가 없어 스프린트를 종료했습니다.",
                    "commit_count": 0,
                    "commit_shas": [],
                    "representative_commit_sha": "",
                    "uncommitted_paths": [],
                }
                sprint_state["report_body"] = await self._prepare_sprint_report_body(sprint_state, closeout_result)
                sprint_state["report_path"] = self._archive_sprint_history(sprint_state, sprint_state["report_body"])
                self._save_sprint_state(sprint_state)
                await self._send_terminal_sprint_reports(
                    title="🛑 스프린트 종료",
                    sprint_state=sprint_state,
                    closeout_result=closeout_result,
                )
                self._finish_scheduler_after_sprint(sprint_state)
                return

            for item in selected_items:
                item["status"] = "selected"
                item["selected_in_sprint_id"] = sprint_id
                self._save_backlog_item(item)
            sprint_state["selected_backlog_ids"] = [str(item.get("backlog_id") or "") for item in selected_items]
            sprint_state["selected_items"] = selected_items
            sprint_state["todos"] = [build_todo_item(item, owner_role="planner") for item in selected_items]
            await self._continue_sprint(sprint_state, announce=True)
        except Exception as exc:
            await self._fail_sprint_due_to_exception(sprint_state, exc)

    def _finish_scheduler_after_sprint(self, sprint_state: dict[str, Any], *, clear_active: bool | None = None) -> None:
        state = self._load_scheduler_state()
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        status = str(sprint_state.get("status") or "").strip().lower()
        if clear_active is None:
            clear_active = status == "completed"
        if clear_active:
            state["active_sprint_id"] = ""
            state["last_completed_at"] = utc_now_iso()
        elif sprint_id:
            state["active_sprint_id"] = sprint_id
        self._save_scheduler_state(state)
        if clear_active:
            self.paths.current_sprint_file.write_text(self._build_idle_current_sprint_markdown(), encoding="utf-8")

    @staticmethod
    def _is_resumable_blocked_sprint(sprint_state: dict[str, Any]) -> bool:
        report_body = str(sprint_state.get("report_body") or "").strip().lower()
        if (
            str(sprint_state.get("status") or "").strip().lower() == "blocked"
            and str(sprint_state.get("phase") or "").strip().lower() == "initial"
            and (
                "initial phase" in report_body
                or "initial phase planning" in report_body
            )
            and ("시작하지 못했습니다" in report_body or "중단했습니다" in report_body)
        ):
            return True
        return (
            str(sprint_state.get("status") or "").strip().lower() == "blocked"
            and str(sprint_state.get("closeout_status") or "").strip().lower()
            in {"planning_incomplete", "restart_required"}
        )

    @staticmethod
    def _is_wrap_up_requested(sprint_state: dict[str, Any]) -> bool:
        return bool(str(sprint_state.get("wrap_up_requested_at") or "").strip())

    @staticmethod
    def _is_executable_todo_status(status: str) -> bool:
        return str(status or "").strip().lower() in {"queued", "running", "uncommitted"}

    def _select_restart_checkpoint_todo(
        self,
        sprint_state: dict[str, Any],
    ) -> tuple[dict[str, Any], str, dict[str, Any]] | None:
        todos = list(sprint_state.get("todos") or [])
        best_candidate: tuple[tuple[int, int, float, int], dict[str, Any], str, dict[str, Any]] | None = None
        status_priority = {"running": 3, "uncommitted": 2, "blocked": 1}
        for index, todo in enumerate(todos):
            normalized_status = str(todo.get("status") or "").strip().lower()
            if normalized_status not in status_priority:
                continue
            request_record: dict[str, Any] = {}
            request_id = str(todo.get("request_id") or "").strip()
            if request_id:
                request_record = self._load_request(request_id)
            checkpoint_at = (
                self._parse_datetime(
                    str(
                        request_record.get("updated_at")
                        or request_record.get("created_at")
                        or todo.get("ended_at")
                        or todo.get("started_at")
                        or ""
                    )
                )
            )
            candidate_key = (
                status_priority[normalized_status],
                1 if checkpoint_at is not None else 0,
                checkpoint_at.timestamp() if checkpoint_at is not None else float("-inf"),
                index,
            )
            if best_candidate is None or candidate_key > best_candidate[0]:
                best_candidate = (candidate_key, todo, normalized_status, request_record)
        if best_candidate is None:
            return None
        return best_candidate[1], best_candidate[2], best_candidate[3]

    def _mark_restart_checkpoint_backlog_selected(
        self,
        sprint_state: dict[str, Any],
        *,
        backlog_id: str,
    ) -> None:
        normalized_backlog_id = str(backlog_id or "").strip()
        if not normalized_backlog_id:
            return
        backlog_item = self._load_backlog_item(normalized_backlog_id)
        if not backlog_item:
            return
        if str(backlog_item.get("status") or "").strip().lower() != "done":
            backlog_item["status"] = "selected"
            backlog_item["selected_in_sprint_id"] = str(sprint_state.get("sprint_id") or "")
            backlog_item["completed_in_sprint_id"] = ""
            self._clear_backlog_blockers(backlog_item)
            self._save_backlog_item(backlog_item)
        selected_items = list(sprint_state.get("selected_items") or [])
        updated_selected_items: list[dict[str, Any]] = []
        replaced = False
        for item in selected_items:
            if str(item.get("backlog_id") or "").strip() == normalized_backlog_id:
                merged = dict(item)
                if str(backlog_item.get("status") or "").strip().lower() != "done":
                    merged["status"] = "selected"
                    merged["selected_in_sprint_id"] = str(sprint_state.get("sprint_id") or "")
                    merged["completed_in_sprint_id"] = ""
                    self._clear_backlog_blockers(merged)
                updated_selected_items.append(merged)
                replaced = True
            else:
                updated_selected_items.append(item)
        if not replaced and backlog_item:
            updated_selected_items.append(dict(backlog_item))
        sprint_state["selected_items"] = updated_selected_items
        selected_backlog_ids = [
            str(item).strip()
            for item in (sprint_state.get("selected_backlog_ids") or [])
            if str(item).strip()
        ]
        if normalized_backlog_id not in selected_backlog_ids:
            selected_backlog_ids.append(normalized_backlog_id)
        sprint_state["selected_backlog_ids"] = selected_backlog_ids

    def _prepare_requested_restart_checkpoint(self, sprint_state: dict[str, Any]) -> bool:
        requested_at = str(sprint_state.get("resume_from_checkpoint_requested_at") or "").strip()
        if not requested_at:
            return False
        sprint_state["resume_from_checkpoint_requested_at"] = ""
        sprint_state["last_resume_checkpoint_todo_id"] = ""
        sprint_state["last_resume_checkpoint_status"] = ""
        candidate = self._select_restart_checkpoint_todo(sprint_state)
        if candidate is None:
            return True
        todo, previous_status, request_record = candidate
        todo_id = str(todo.get("todo_id") or "").strip()
        previous_request_id = str(todo.get("request_id") or "").strip()
        backlog_id = str(todo.get("backlog_id") or "").strip()
        if previous_status == "blocked":
            if previous_request_id:
                todo["retry_of_request_id"] = previous_request_id
            todo["request_id"] = ""
            todo["status"] = "queued"
            todo["ended_at"] = ""
            todo["carry_over_backlog_id"] = ""
            todo["version_control_status"] = ""
            todo["version_control_paths"] = []
            todo["version_control_message"] = ""
            todo["version_control_error"] = ""
            self._mark_restart_checkpoint_backlog_selected(
                sprint_state,
                backlog_id=backlog_id,
            )
            summary = "마지막 blocked todo를 재시도하도록 restart checkpoint를 복원했습니다."
        else:
            summary = "마지막 execution checkpoint부터 sprint를 재개합니다."
        todos = list(sprint_state.get("todos") or [])
        sprint_state["todos"] = [todo] + [
            item
            for item in todos
            if str(item.get("todo_id") or "").strip() != todo_id
        ]
        sprint_state["last_resume_checkpoint_todo_id"] = todo_id
        sprint_state["last_resume_checkpoint_status"] = previous_status
        self._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="restart_checkpoint",
            summary=summary,
            payload={
                "todo_id": todo_id,
                "backlog_id": backlog_id,
                "previous_status": previous_status,
                "request_id": previous_request_id,
                "checkpoint_request_updated_at": str(request_record.get("updated_at") or "").strip(),
            },
        )
        return True

    async def _resume_active_sprint(self, sprint_id: str) -> None:
        async with self._sprint_resume_lock:
            sprint_state = self._load_sprint_state(sprint_id)
            if not sprint_state:
                LOGGER.warning("Clearing missing active sprint state: %s", sprint_id)
                self._finish_scheduler_after_sprint({"sprint_id": sprint_id})
                return
            status = str(sprint_state.get("status") or "").strip().lower()
            if status == "completed":
                self._finish_scheduler_after_sprint(sprint_state)
                return
            if self._is_resumable_blocked_sprint(sprint_state):
                sprint_state["ended_at"] = ""
                sprint_state.pop("reload_required", None)
                sprint_state.pop("reload_paths", None)
                sprint_state.pop("reload_message", None)
                sprint_state.pop("reload_restart_command", None)
                self._append_sprint_event(
                    sprint_id,
                    event_type="resumed",
                    summary="blocked sprint를 같은 sprint_id로 재개했습니다.",
                )
                self._save_sprint_state(sprint_state)
                await self._continue_sprint(sprint_state, announce=False)
                return
            if status in {"failed", "blocked"}:
                self._finish_scheduler_after_sprint(sprint_state, clear_active=False)
                return
            LOGGER.info("Resuming active sprint %s with status=%s", sprint_id, status or "unknown")
            self._append_sprint_event(sprint_id, event_type="resumed", summary="오케스트레이터가 active sprint를 재개했습니다.")
            try:
                await self._continue_sprint(sprint_state, announce=False)
            except Exception as exc:
                await self._fail_sprint_due_to_exception(sprint_state, exc)

    def _prune_dropped_backlog_from_sprint(self, sprint_state: dict[str, Any], dropped_ids: set[str]) -> bool:
        if not dropped_ids:
            return False
        existing_todos = list(sprint_state.get("todos") or [])
        kept_todos: list[dict[str, Any]] = []
        pruned_count = 0
        for todo in existing_todos:
            backlog_id = str(todo.get("backlog_id") or "").strip()
            status = str(todo.get("status") or "").strip().lower()
            if backlog_id in dropped_ids and status == "queued":
                pruned_count += 1
                continue
            kept_todos.append(todo)
        if pruned_count == 0:
            return False
        kept_ids = {str(todo.get("backlog_id") or "").strip() for todo in kept_todos}
        sprint_state["todos"] = kept_todos
        sprint_state["selected_backlog_ids"] = [backlog_id for backlog_id in sprint_state.get("selected_backlog_ids") or [] if str(backlog_id or "").strip() in kept_ids]
        sprint_state["selected_items"] = [
            item
            for item in sprint_state.get("selected_items") or []
            if str(item.get("backlog_id") or "").strip() in kept_ids
        ]
        self._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="backlog_pruned",
            summary="실행 불가 backlog 항목을 active sprint에서 제거했습니다.",
            payload={"removed_count": pruned_count},
        )
        return True

    async def _claim_request(self, request_id: str) -> bool:
        normalized = str(request_id or "").strip()
        if not normalized:
            return False
        async with self._active_request_ids_lock:
            if normalized in self._active_request_ids:
                return False
            self._active_request_ids.add(normalized)
            return True

    async def _release_request(self, request_id: str) -> None:
        normalized = str(request_id or "").strip()
        if not normalized:
            return
        async with self._active_request_ids_lock:
            self._active_request_ids.discard(normalized)

    async def _resume_pending_role_requests(self) -> None:
        if self.role == "orchestrator":
            return
        async with self._role_resume_lock:
            pending_records = [
                record
                for record in iter_json_records(self.paths.requests_dir)
                if str(record.get("status") or "").strip().lower() == "delegated"
                and str(record.get("current_role") or "").strip() == self.role
            ]
            pending_records.sort(key=lambda record: str(record.get("updated_at") or ""))
            for request_record in pending_records:
                await self._resume_pending_delegated_request(request_record)

    async def _resume_pending_role_requests_loop(self) -> None:
        while True:
            try:
                await self._resume_pending_role_requests()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Pending delegated request resume loop failed for role %s", self.role)
            await asyncio.sleep(ROLE_REQUEST_RESUME_POLL_SECONDS)

    async def _resume_pending_delegated_request(self, request_record: dict[str, Any]) -> None:
        request_id = str(request_record.get("request_id") or "").strip()
        if not request_id or not await self._claim_request(request_id):
            return
        try:
            result = dict(request_record.get("result") or {}) if isinstance(request_record.get("result"), dict) else {}
            if (
                str(result.get("request_id") or "").strip() == request_id
                and str(result.get("role") or "").strip() == self.role
            ):
                result_envelope = MessageEnvelope(
                    request_id=request_id,
                    sender=self.role,
                    target="orchestrator",
                    intent="report",
                    urgency=str(request_record.get("urgency") or "normal"),
                    scope=str(request_record.get("scope") or ""),
                    artifacts=[str(item) for item in result.get("artifacts") or []],
                    params={
                        "_teams_kind": "report",
                        "result": result,
                        "_resumed_after_reconnect": True,
                    },
                    body=json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
                )
                await self._send_relay(result_envelope, request_record=request_record)
                return
            envelope = self._build_delegate_envelope(
                request_record,
                self.role,
                extra_params={
                    **dict(request_record.get("params") or {}),
                    "_resumed_after_reconnect": True,
                },
            )
            await self._process_delegated_request(envelope, request_record)
        finally:
            await self._release_request(request_id)

    def _workspace_artifact_hint(self, path: Path) -> str:
        resolved = path.resolve()
        local_roots = (
            (self.paths.shared_workspace_root.resolve(), "./shared_workspace"),
            (self.paths.runtime_root.resolve(), "./.teams_runtime"),
            (self.paths.docs_root.resolve(), "./docs"),
        )
        for root, prefix in local_roots:
            try:
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            return prefix if str(relative) == "." else f"{prefix}/{relative.as_posix()}"
        base = "./workspace/teams_generated" if self.paths.workspace_root.name == "teams_generated" else "./workspace"
        try:
            relative = resolved.relative_to(self.paths.workspace_root.resolve())
        except ValueError:
            relative = resolved.relative_to(self.paths.project_workspace_root.resolve())
            return f"{base}/{relative.as_posix()}"
        return f"{base}/{relative.as_posix()}"

    @staticmethod
    def _normalize_artifact_hint(value: Any) -> str:
        normalized = str(value or "").strip().replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    @classmethod
    def _is_planning_surface_artifact_hint(cls, artifact_hint: Any) -> bool:
        normalized = cls._normalize_artifact_hint(artifact_hint).lower()
        if not normalized:
            return False
        name = PurePosixPath(normalized).name
        if normalized.startswith(".teams_runtime/requests/") and name.endswith(".json"):
            return True
        if normalized.startswith(".teams_runtime/backlog/") and name.endswith(".json"):
            return True
        if normalized.startswith("shared_workspace/sprint_history/") and name.endswith(".md"):
            return True
        if not normalized.startswith("shared_workspace/"):
            return False
        if name in PLANNING_SURFACE_ROOT_DOC_NAMES:
            return True
        return "/sprints/" in normalized and name in PLANNING_SURFACE_SPRINT_DOC_NAMES

    @classmethod
    def _is_planner_owned_surface_artifact_hint(cls, artifact_hint: Any) -> bool:
        normalized = cls._normalize_artifact_hint(artifact_hint).lower()
        if not normalized or not normalized.startswith("shared_workspace/"):
            return False
        name = PurePosixPath(normalized).name
        if name in PLANNING_SURFACE_ROOT_DOC_NAMES:
            return True
        return "/sprints/" in normalized and name in PLANNER_OWNED_SPRINT_DOC_NAMES

    def _normalize_sprint_todo_artifacts(
        self,
        *artifact_groups: Any,
        workflow_state: dict[str, Any] | None = None,
    ) -> list[str]:
        candidates = self._collect_artifact_candidates(*artifact_groups)
        phase = str((workflow_state or {}).get("phase") or "").strip().lower()
        if phase in {WORKFLOW_PHASE_IMPLEMENTATION, WORKFLOW_PHASE_VALIDATION}:
            return [
                artifact
                for artifact in candidates
                if not self._is_planner_owned_surface_artifact_hint(artifact)
            ]
        return candidates

    def _required_workflow_planner_doc_hints(self, request_record: dict[str, Any]) -> list[str]:
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return []
        required: list[str] = []

        def append_required(hint: Any) -> None:
            normalized_hint = self._normalize_artifact_hint(hint)
            if normalized_hint and normalized_hint not in required:
                required.append(normalized_hint)

        request_artifacts = [
            self._normalize_artifact_hint(item)
            for item in (request_record.get("artifacts") or [])
            if self._is_planning_surface_artifact_hint(item)
        ]
        if str(workflow_state.get("reopen_source_role") or "").strip().lower() == "qa":
            for name in WORKFLOW_QA_REOPEN_REQUIRED_DOC_NAMES:
                match = next(
                    (artifact for artifact in request_artifacts if PurePosixPath(artifact).name == name),
                    "",
                )
                append_required(match)
            sprint_id = str(request_record.get("sprint_id") or "").strip()
            sprint_state = self._load_sprint_state(sprint_id) if sprint_id else {}
            if sprint_state:
                artifact_paths = self._sprint_artifact_paths(sprint_state)
                for key in ("spec", "todo_backlog", "iteration_log"):
                    append_required(self._workspace_artifact_hint(artifact_paths[key]))
                append_required(self._workspace_artifact_hint(self.paths.current_sprint_file))
        return required

    def _workflow_planner_doc_contract_violation(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> tuple[list[str], list[str], list[str]]:
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return ([], [], [])
        role = str(result.get("role") or request_record.get("current_role") or "").strip().lower()
        if role != "planner":
            return ([], [], [])
        if str(workflow_state.get("phase") or "").strip().lower() != WORKFLOW_PHASE_PLANNING:
            return ([], [], [])
        result_artifacts = [
            self._normalize_artifact_hint(item)
            for item in (result.get("artifacts") or [])
            if self._is_planning_surface_artifact_hint(item)
        ]
        planner_owned_artifacts = [
            artifact for artifact in result_artifacts if self._is_planner_owned_surface_artifact_hint(artifact)
        ]
        missing_required = [
            artifact
            for artifact in self._required_workflow_planner_doc_hints(request_record)
            if artifact not in planner_owned_artifacts
        ]
        missing_files = [
            artifact for artifact in planner_owned_artifacts if self._resolve_artifact_path(artifact) is None
        ]
        return (planner_owned_artifacts, missing_required, missing_files)

    def _qa_result_requires_planner_reopen(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> bool:
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return False
        if str(workflow_state.get("step") or "").strip().lower() != WORKFLOW_STEP_QA_VALIDATION:
            return False
        role = str(result.get("role") or request_record.get("current_role") or "").strip().lower()
        if role != "qa":
            return False
        transition = self._workflow_transition(result)
        status = str(result.get("status") or "").strip().lower()
        if (
            transition.get("outcome") != "reopen"
            and status not in {"blocked", "failed"}
            and not str(result.get("error") or "").strip()
        ):
            return False
        if str(transition.get("target_step") or "").strip().lower() == WORKFLOW_STEP_PLANNER_FINALIZE:
            return False
        if str(transition.get("reopen_category") or "").strip().lower() == "scope":
            return False
        combined = " ".join(
            [
                str(result.get("summary") or ""),
                str(result.get("error") or ""),
                str(transition.get("reason") or ""),
                *[
                    str(item).strip()
                    for item in (result.get("insights") or [])
                    if str(item).strip()
                ],
                *[
                    str(item).strip()
                    for item in (transition.get("unresolved_items") or [])
                    if str(item).strip()
                ],
            ]
        ).lower()
        return any(
            token in combined
            for token in (
                "spec.md",
                "spec ",
                "spec/",
                "acceptance criteria",
                "acceptance criterion",
                "acceptance 기준",
                "수용기준",
            )
        )

    def _qa_result_is_runtime_sync_anomaly(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> bool:
        workflow_state = self._request_workflow_state(request_record)
        if not workflow_state:
            return False
        if str(workflow_state.get("step") or "").strip().lower() != WORKFLOW_STEP_QA_VALIDATION:
            return False
        role = str(result.get("role") or request_record.get("current_role") or "").strip().lower()
        if role != "qa":
            return False
        transition = self._workflow_transition(result)
        combined = " ".join(
            [
                str(result.get("summary") or ""),
                str(result.get("error") or ""),
                str(transition.get("reason") or ""),
                *[
                    str(item).strip()
                    for item in (result.get("insights") or [])
                    if str(item).strip()
                ],
                *[
                    str(item).strip()
                    for item in (transition.get("unresolved_items") or [])
                    if str(item).strip()
                ],
            ]
        ).lower()
        has_sync_signal = any(
            token in combined
            for token in (
                "current_sprint",
                "todo_backlog",
                "iteration_log",
                "planner-owned",
                "planning doc",
                "planning 문서",
                "sync",
                "동기화",
            )
        )
        if not has_sync_signal:
            return False
        return not self._qa_result_requires_planner_reopen(request_record, result)

    @staticmethod
    def _normalize_backlog_file_candidates(values: Any) -> list[Any]:
        normalized: list[Any] = []
        seen: set[int] = set()

        def walk(raw: Any) -> None:
            if isinstance(raw, dict):
                raw_items = raw.get("backlog_items")
                if isinstance(raw_items, list):
                    for item in raw_items:
                        if isinstance(item, (str, dict)):
                            normalized_id = id(item)
                            if normalized_id not in seen:
                                seen.add(normalized_id)
                                normalized.append(item)
                raw_item = raw.get("backlog_item")
                if isinstance(raw_item, (str, dict)):
                    raw_item_id = id(raw_item)
                    if raw_item_id not in seen:
                        seen.add(raw_item_id)
                        normalized.append(raw_item)
                for nested in raw.values():
                    walk(nested)
                return
            if isinstance(raw, list):
                for nested in raw:
                    walk(nested)

        walk(values)
        return normalized

    def _collect_backlog_candidates_from_payload(self, payload: Any) -> list[Any]:
        if not payload:
            return []
        return self._normalize_backlog_file_candidates(payload)

    @staticmethod
    def _planner_backlog_write_receipts(proposals: dict[str, Any]) -> list[dict[str, Any]]:
        receipts: list[dict[str, Any]] = []
        seen: set[str] = set()
        raw_values: list[Any] = []
        raw_list = proposals.get("backlog_writes")
        if isinstance(raw_list, list):
            raw_values.extend(raw_list)
        raw_single = proposals.get("backlog_write")
        if isinstance(raw_single, dict):
            raw_values.append(raw_single)

        for raw_value in raw_values:
            if not isinstance(raw_value, dict):
                continue
            backlog_id = str(raw_value.get("backlog_id") or "").strip()
            artifact_path = str(
                raw_value.get("artifact_path")
                or raw_value.get("artifact")
                or raw_value.get("path")
                or ""
            ).strip()
            if not backlog_id and not artifact_path:
                continue
            dedupe_key = str(backlog_id or artifact_path).strip().lower()
            if dedupe_key and dedupe_key in seen:
                continue
            if dedupe_key:
                seen.add(dedupe_key)
            receipt = dict(raw_value)
            if backlog_id:
                receipt["backlog_id"] = backlog_id
            else:
                receipt.pop("backlog_id", None)
            if artifact_path:
                receipt["artifact_path"] = artifact_path
            else:
                receipt.pop("artifact_path", None)
            receipt.pop("artifact", None)
            receipt.pop("path", None)
            receipts.append(receipt)
        return receipts

    def _resolve_artifact_path(self, artifact_hint: str) -> Path | None:
        raw_hint = str(artifact_hint or "").strip()
        if not raw_hint:
            return None
        normalized = raw_hint.strip()

        if normalized.startswith("./"):
            normalized = normalized[2:]

        candidate_hints: list[str] = [normalized]
        if normalized.startswith("workspace/teams_generated/"):
            candidate_hints.append(normalized.removeprefix("workspace/teams_generated/"))
            candidate_hints.append(f"teams_generated/{normalized.removeprefix('workspace/teams_generated/')}")
        elif normalized.startswith("workspace/") and self.paths.workspace_root.name == "teams_generated":
            candidate_hints.append(normalized.removeprefix("workspace/"))

        candidates: list[Path] = []
        for hint in _dedupe_preserving_order(candidate_hints):
            hint_path = Path(hint)
            if hint_path.is_absolute():
                candidate_abs = hint_path.resolve()
                if candidate_abs.exists():
                    return candidate_abs
                continue

            workspace_prefix = "teams_generated" if self.paths.workspace_root.name == "teams_generated" else "workspace"
            base_prefixes = [".teams_runtime", "shared_workspace", "docs", workspace_prefix]
            if workspace_prefix == "workspace":
                base_prefixes.append("workspace/teams_generated")

            candidates.extend(
                [
                    self.paths.workspace_root / hint_path,
                    self.paths.runtime_root / hint_path,
                    self.paths.project_workspace_root / hint_path,
                    self.paths.backlog_dir / hint_path,
                ]
            )

            for base in (self.paths.workspace_root, self.paths.runtime_root, self.paths.project_workspace_root):
                for prefix in base_prefixes:
                    if hint == prefix or hint.startswith(f"{prefix}/"):
                        continue
                    candidates.append(base / prefix / hint_path)

            if hint.startswith("workspace/"):
                candidates.append(self.paths.project_workspace_root / hint_path)
            if hint.startswith(".."):
                candidates.append(self.paths.project_workspace_root / hint_path)

            if normalized.startswith("workspace/teams_generated/"):
                workspace_alias = normalized.removeprefix("workspace/teams_generated/")
                candidates.append(self.paths.workspace_root / workspace_alias)
                candidates.append(self.paths.project_workspace_root / f"teams_generated/{workspace_alias}")
                candidates.append(self.paths.runtime_root / workspace_alias)
                candidates.append(self.paths.runtime_root / f"teams_generated/{workspace_alias}")

        for candidate in candidates:
            try:
                resolved = candidate.resolve()
            except (OSError, ValueError):
                continue
            if resolved.exists():
                return resolved
        return None

    def _backlog_artifact_candidate_paths(self, request_record: dict[str, Any], result: dict[str, Any]) -> list[str]:
        candidate_paths: list[str] = []
        for raw_artifact in [
            *(request_record.get("artifacts") or []),
            *(result.get("artifacts") or []),
        ]:
            artifact = str(raw_artifact or "").strip()
            if not artifact:
                continue
            lower = artifact.lower()
            if "backlog-" not in lower or not lower.endswith(".json"):
                continue
            if artifact not in candidate_paths:
                candidate_paths.append(artifact)
        return candidate_paths

    @staticmethod
    def _collect_artifact_candidates(*sequences: Iterable[Any]) -> list[str]:
        values: list[str] = []
        for sequence in sequences:
            if not sequence:
                continue
            for raw_value in sequence:
                normalized = str(raw_value or "").strip()
                if normalized:
                    values.append(normalized)
        return _dedupe_preserving_order(values)

    def _load_backlog_candidates_from_artifact(self, artifact_path: str) -> list[Any]:
        resolved = self._resolve_artifact_path(artifact_path)
        if resolved is None or not resolved.is_file():
            return []
        payload = read_json(resolved)
        if isinstance(payload, dict):
            if "backlog_id" in payload:
                return [payload]
            candidates = self._collect_backlog_candidates_from_payload(payload)
            if candidates:
                return candidates
        elif isinstance(payload, list):
            return [item for item in payload if isinstance(item, (dict, str))]
        return []

    def _sync_planner_backlog_from_report(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        persist: bool = True,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "proposal_items": 0,
            "receipt_items": 0,
            "artifact_items": 0,
            "merged_items": 0,
            "persisted_backlog_items": 0,
            "missing_backlog_artifacts": [],
            "missing_backlog_receipts": [],
            "planner_persisted_backlog": False,
            "verified_backlog_items": 0,
        }

        params = dict(request_record.get("params") or {})
        request_kind = str(params.get("_teams_kind") or "").strip()
        sprint_id = str(params.get("sprint_id") or request_record.get("sprint_id") or "").strip()
        if not sprint_id and request_kind not in {"sourcer_review", "blocked_backlog_review"}:
            return summary

        proposal_candidates = self._collect_backlog_candidates_from_payload(result.get("proposals") or {})
        proposal_items = [item for item in proposal_candidates if isinstance(item, (dict, str))][:120]
        summary["proposal_items"] = len(proposal_items)
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        receipts = self._planner_backlog_write_receipts(proposals)
        summary["receipt_items"] = len(receipts)
        if proposal_items and not receipts:
            summary["missing_backlog_receipts"].append("planner backlog_writes receipt missing")

        verified_backlog_ids: set[str] = set()
        for receipt in receipts:
            backlog_id = str(receipt.get("backlog_id") or "").strip()
            artifact_path = str(receipt.get("artifact_path") or "").strip()
            verified = False

            if backlog_id:
                persisted_item = self._load_backlog_item(backlog_id)
                if persisted_item:
                    verified_backlog_ids.add(backlog_id)
                    verified = True

            if not verified and artifact_path:
                resolved = self._resolve_artifact_path(artifact_path)
                if resolved is None:
                    summary["missing_backlog_artifacts"].append(artifact_path)
                    continue
                backlog_candidates = self._load_backlog_candidates_from_artifact(artifact_path)
                if not backlog_candidates:
                    summary["missing_backlog_artifacts"].append(artifact_path)
                    continue
                summary["artifact_items"] += len(backlog_candidates)
                for item in backlog_candidates:
                    item_backlog_id = str(item.get("backlog_id") or "").strip()
                    if item_backlog_id:
                        verified_backlog_ids.add(item_backlog_id)
                verified = True

            if not verified:
                summary["missing_backlog_receipts"].append(backlog_id or artifact_path or json.dumps(receipt, ensure_ascii=False))

        summary["verified_backlog_items"] = len(verified_backlog_ids)
        summary["persisted_backlog_items"] = len(verified_backlog_ids)
        summary["planner_persisted_backlog"] = bool(verified_backlog_ids)
        return summary

    def _message_attachment_artifacts(self, message: DiscordMessage) -> list[str]:
        artifacts: list[str] = []
        for attachment in message.attachments:
            saved_path = str(attachment.saved_path or "").strip()
            if not saved_path:
                continue
            try:
                artifact_hint = self._workspace_artifact_hint(Path(saved_path))
            except Exception:
                artifact_hint = saved_path
            if artifact_hint not in artifacts:
                artifacts.append(artifact_hint)
        return artifacts

    @staticmethod
    def _is_attachment_only_save_failure(message: DiscordMessage) -> bool:
        if str(message.content or "").strip():
            return False
        if not message.attachments:
            return False
        return not any(str(item.saved_path or "").strip() for item in message.attachments)

    def _normalize_sourcer_review_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            title = str(candidate.get("title") or "").strip()
            scope = str(candidate.get("scope") or title).strip()
            summary = str(candidate.get("summary") or scope or title).strip()
            kind = str(candidate.get("kind") or "enhancement").strip().lower() or "enhancement"
            if not title or not scope:
                continue
            normalized.append(
                {
                    "title": title,
                    "scope": scope,
                    "summary": summary,
                    "kind": kind,
                    "acceptance_criteria": self._normalize_backlog_acceptance_criteria(
                        candidate.get("acceptance_criteria")
                    ),
                    "milestone_title": str(candidate.get("milestone_title") or "").strip(),
                    "priority_rank": int(candidate.get("priority_rank") or 0),
                    "planned_in_sprint_id": str(candidate.get("planned_in_sprint_id") or "").strip(),
                    "added_during_active_sprint": bool(candidate.get("added_during_active_sprint")),
                    "origin": dict(candidate.get("origin") or {}),
                }
            )
        return normalized

    def _normalize_blocked_backlog_review_candidates(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            backlog_id = str(candidate.get("backlog_id") or "").strip()
            title = str(candidate.get("title") or "").strip()
            scope = str(candidate.get("scope") or title).strip()
            if not backlog_id or not title or not scope:
                continue
            if str(candidate.get("status") or "").strip().lower() != "blocked":
                continue
            normalized.append(
                {
                    "backlog_id": backlog_id,
                    "title": title,
                    "scope": scope,
                    "summary": str(candidate.get("summary") or scope or title).strip(),
                    "kind": str(candidate.get("kind") or "enhancement").strip().lower() or "enhancement",
                    "status": "blocked",
                    "blocked_reason": str(candidate.get("blocked_reason") or "").strip(),
                    "blocked_by_role": str(candidate.get("blocked_by_role") or "").strip(),
                    "required_inputs": self._normalize_backlog_acceptance_criteria(
                        candidate.get("required_inputs")
                    ),
                    "recommended_next_step": str(candidate.get("recommended_next_step") or "").strip(),
                    "acceptance_criteria": self._normalize_backlog_acceptance_criteria(
                        candidate.get("acceptance_criteria")
                    ),
                    "milestone_title": str(candidate.get("milestone_title") or "").strip(),
                    "priority_rank": int(candidate.get("priority_rank") or 0),
                    "planned_in_sprint_id": str(candidate.get("planned_in_sprint_id") or "").strip(),
                    "updated_at": str(candidate.get("updated_at") or "").strip(),
                    "origin": dict(candidate.get("origin") or {}),
                }
            )
        normalized.sort(
            key=lambda item: (
                int(item.get("priority_rank") or 0) if int(item.get("priority_rank") or 0) > 0 else 10**9,
                str(item.get("updated_at") or ""),
                str(item.get("backlog_id") or ""),
            )
        )
        return normalized

    def _collect_blocked_backlog_review_candidates(self) -> list[dict[str, Any]]:
        return self._normalize_blocked_backlog_review_candidates(
            [
                item
                for item in self._iter_backlog_items()
                if str(item.get("status") or "").strip().lower() == "blocked"
            ]
        )

    def _build_sourcer_review_fingerprint(self, candidates: list[dict[str, Any]]) -> str:
        parts = [
            "::".join(
                [
                    self._build_backlog_fingerprint(
                        title=str(candidate.get("title") or ""),
                        scope=str(candidate.get("scope") or ""),
                        kind=str(candidate.get("kind") or ""),
                    ),
                    self._build_sourcer_candidate_trace_fingerprint(candidate),
                ]
            )
            for candidate in candidates
            if str(candidate.get("title") or "").strip()
        ]
        digest = hashlib.sha1("|".join(sorted(parts)).encode("utf-8")).hexdigest()
        return build_request_fingerprint(
            author_id="internal-sourcer",
            channel_id="backlog-sourcing-review",
            intent="plan",
            scope=f"sourcer-review:{digest}",
        )

    def _build_blocked_backlog_review_fingerprint(self, candidates: list[dict[str, Any]]) -> str:
        parts = [
            "|".join(
                [
                    str(candidate.get("backlog_id") or "").strip(),
                    str(candidate.get("updated_at") or "").strip(),
                ]
            )
            for candidate in candidates
            if str(candidate.get("backlog_id") or "").strip()
        ]
        digest = hashlib.sha1("|".join(sorted(parts)).encode("utf-8")).hexdigest()
        return build_request_fingerprint(
            author_id="blocked-backlog-review",
            channel_id="blocked-backlog-review",
            intent="plan",
            scope=f"blocked-backlog-review:{digest}",
        )

    def _find_open_sourcer_review_request(self, fingerprint: str) -> dict[str, Any]:
        normalized = str(fingerprint or "").strip()
        if not normalized:
            return {}
        for request_record in iter_json_records(self.paths.requests_dir):
            if not self._is_sourcer_review_request(request_record):
                continue
            if str(request_record.get("fingerprint") or "").strip() != normalized:
                continue
            status = str(request_record.get("status") or "").strip().lower()
            if self._is_terminal_internal_request_status(status):
                continue
            return request_record
        return {}

    def _find_open_blocked_backlog_review_request(self, fingerprint: str) -> dict[str, Any]:
        normalized = str(fingerprint or "").strip()
        if not normalized:
            return {}
        for request_record in iter_json_records(self.paths.requests_dir):
            if not self._is_blocked_backlog_review_request(request_record):
                continue
            if str(request_record.get("fingerprint") or "").strip() != normalized:
                continue
            status = str(request_record.get("status") or "").strip().lower()
            if self._is_terminal_internal_request_status(status):
                continue
            return request_record
        return {}

    def _find_open_sprint_planning_request(
        self,
        *,
        sprint_id: str,
        phase: str,
        step: str = "",
    ) -> dict[str, Any]:
        normalized_sprint_id = str(sprint_id or "").strip()
        normalized_phase = str(phase or "").strip().lower()
        normalized_step = str(step or "").strip().lower()
        if not normalized_sprint_id or not normalized_phase:
            return {}
        latest_request: dict[str, Any] = {}
        latest_updated_at = ""
        for request_record in iter_json_records(self.paths.requests_dir):
            if not self._is_sprint_planning_request(request_record):
                continue
            params = dict(request_record.get("params") or {})
            request_sprint_id = str(
                request_record.get("sprint_id") or params.get("sprint_id") or ""
            ).strip()
            if request_sprint_id != normalized_sprint_id:
                continue
            if str(params.get("sprint_phase") or "").strip().lower() != normalized_phase:
                continue
            if self._initial_phase_step(request_record) != normalized_step:
                continue
            status = str(request_record.get("status") or "").strip().lower()
            if self._is_terminal_internal_request_status(status):
                continue
            updated_at = str(
                request_record.get("updated_at") or request_record.get("created_at") or ""
            ).strip()
            if not latest_request or updated_at >= latest_updated_at:
                latest_request = request_record
                latest_updated_at = updated_at
        return latest_request

    def _render_sourcer_review_markdown(
        self,
        *,
        request_id: str,
        candidates: list[dict[str, Any]],
        sourcing_activity: dict[str, Any],
    ) -> str:
        lines = [
            "# Sourcer Backlog Review",
            "",
            f"- request_id: {request_id}",
            f"- candidate_count: {len(candidates)}",
            f"- sourcer_summary: {str(sourcing_activity.get('summary') or '').strip() or '없음'}",
            f"- sourcer_mode: {str(sourcing_activity.get('mode') or '').strip() or 'unknown'}",
            "",
            "## Candidates",
            "",
        ]
        for index, candidate in enumerate(candidates, start=1):
            lines.extend(
                [
                    f"### {index}. {candidate.get('title') or ''}",
                    f"- kind: {candidate.get('kind') or ''}",
                    f"- scope: {candidate.get('scope') or ''}",
                    f"- summary: {candidate.get('summary') or ''}",
                ]
            )
            acceptance = [str(item).strip() for item in (candidate.get("acceptance_criteria") or []) if str(item).strip()]
            if acceptance:
                lines.append("- acceptance_criteria:")
                lines.extend([f"  - {item}" for item in acceptance])
            origin = dict(candidate.get("origin") or {})
            if origin:
                origin_parts = [f"{key}={value}" for key, value in origin.items() if str(value).strip()]
                if origin_parts:
                    lines.append(f"- origin: {', '.join(origin_parts)}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _render_blocked_backlog_review_markdown(
        self,
        *,
        request_id: str,
        candidates: list[dict[str, Any]],
    ) -> str:
        lines = [
            "# Blocked Backlog Review",
            "",
            f"- request_id: {request_id}",
            f"- candidate_count: {len(candidates)}",
            "",
            "## Blocked Items",
            "",
        ]
        for index, candidate in enumerate(candidates, start=1):
            lines.extend(
                [
                    f"### {index}. {candidate.get('title') or ''}",
                    f"- backlog_id: {candidate.get('backlog_id') or ''}",
                    f"- kind: {candidate.get('kind') or ''}",
                    f"- scope: {candidate.get('scope') or ''}",
                    f"- summary: {candidate.get('summary') or ''}",
                    f"- blocked_reason: {candidate.get('blocked_reason') or '없음'}",
                    f"- blocked_by_role: {candidate.get('blocked_by_role') or '없음'}",
                    f"- recommended_next_step: {candidate.get('recommended_next_step') or '없음'}",
                ]
            )
            required_inputs = [
                str(item).strip() for item in (candidate.get("required_inputs") or []) if str(item).strip()
            ]
            if required_inputs:
                lines.append("- required_inputs:")
                lines.extend([f"  - {item}" for item in required_inputs])
            acceptance = [
                str(item).strip()
                for item in (candidate.get("acceptance_criteria") or [])
                if str(item).strip()
            ]
            if acceptance:
                lines.append("- acceptance_criteria:")
                lines.extend([f"  - {item}" for item in acceptance])
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def _build_sourcer_review_request_record(
        self,
        candidates: list[dict[str, Any]],
        *,
        sourcing_activity: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = new_request_id()
        normalized_candidates = self._normalize_sourcer_review_candidates(candidates)
        review_dir = self.paths.shared_workspace_root / "sourcer_reviews"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_file = review_dir / f"{request_id}.md"
        review_file.write_text(
            self._render_sourcer_review_markdown(
                request_id=request_id,
                candidates=normalized_candidates,
                sourcing_activity=sourcing_activity,
            ),
            encoding="utf-8",
        )
        record = {
            "request_id": request_id,
            "status": "queued",
            "intent": "plan",
            "urgency": "normal",
            "scope": "autonomous backlog sourcing review",
            "body": (
                "Internal sourcer produced backlog candidates. "
                "Planner owns backlog management strictly, so review these candidates, "
                "make add/update/dedupe/prioritization decisions, and persist any accepted backlog changes directly. "
                "Do not route directly to implementation roles from this request."
            ),
            "artifacts": [self._workspace_artifact_hint(review_file)],
            "params": {
                "_teams_kind": "sourcer_review",
                "sourcing_mode": str(sourcing_activity.get("mode") or "").strip() or "internal_sourcer",
                "sourcing_summary": str(sourcing_activity.get("summary") or "").strip(),
                "candidate_count": len(normalized_candidates),
                "sourced_backlog_candidates": normalized_candidates,
            },
            "current_role": "orchestrator",
            "next_role": "planner",
            "owner_role": "orchestrator",
            "sprint_id": self.runtime_config.sprint_id,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "fingerprint": self._build_sourcer_review_fingerprint(normalized_candidates),
            "reply_route": {},
            "events": [],
            "result": {},
        }
        append_request_event(
            record,
            event_type="created",
            actor="orchestrator",
            summary="internal sourcer 후보에 대한 planner backlog review 요청을 생성했습니다.",
        )
        self._save_request(record)
        return record

    def _build_blocked_backlog_review_request_record(
        self,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        request_id = new_request_id()
        normalized_candidates = self._normalize_blocked_backlog_review_candidates(candidates)
        review_dir = self.paths.shared_workspace_root / "blocked_backlog_reviews"
        review_dir.mkdir(parents=True, exist_ok=True)
        review_file = review_dir / f"{request_id}.md"
        review_file.write_text(
            self._render_blocked_backlog_review_markdown(
                request_id=request_id,
                candidates=normalized_candidates,
            ),
            encoding="utf-8",
        )
        record = {
            "request_id": request_id,
            "status": "queued",
            "intent": "plan",
            "urgency": "normal",
            "scope": "autonomous blocked backlog review",
            "body": (
                "Current blocked backlog is not automatically eligible for future sprint selection. "
                "Review these blocked items, decide which ones should remain blocked versus reopen to pending, "
                "persist any accepted backlog state changes directly, clear blocker metadata when reopening, "
                "and do not mark work selected in this request."
            ),
            "artifacts": [self._workspace_artifact_hint(review_file)],
            "params": {
                "_teams_kind": "blocked_backlog_review",
                "candidate_count": len(normalized_candidates),
                "blocked_backlog_candidates": normalized_candidates,
            },
            "current_role": "orchestrator",
            "next_role": "planner",
            "owner_role": "orchestrator",
            "sprint_id": "",
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "fingerprint": self._build_blocked_backlog_review_fingerprint(normalized_candidates),
            "reply_route": {},
            "events": [],
            "result": {},
        }
        append_request_event(
            record,
            event_type="created",
            actor="orchestrator",
            summary="blocked backlog 재검토를 위한 planner review 요청을 생성했습니다.",
        )
        self._save_request(record)
        return record

    async def _queue_sourcer_candidates_for_planner_review(
        self,
        candidates: list[dict[str, Any]],
        *,
        sourcing_activity: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_candidates = self._normalize_sourcer_review_candidates(candidates)
        if not normalized_candidates:
            return {"request_id": "", "created": False, "reused": False, "relay_sent": False, "fingerprint": ""}
        fingerprint = self._build_sourcer_review_fingerprint(normalized_candidates)
        existing = self._find_open_sourcer_review_request(fingerprint)
        if existing:
            request_id = str(existing.get("request_id") or "").strip()
            self._last_backlog_sourcing_activity["planner_review_request_id"] = request_id
            self._last_backlog_sourcing_activity["planner_review_status"] = "reused"
            self._last_backlog_sourcing_activity["planner_review_candidate_count"] = len(normalized_candidates)
            return {
                "request_id": request_id,
                "created": False,
                "reused": True,
                "relay_sent": True,
                "fingerprint": fingerprint,
            }
        request_record = self._build_sourcer_review_request_record(
            normalized_candidates,
            sourcing_activity=sourcing_activity,
        )
        request_record["status"] = "delegated"
        request_record["current_role"] = "planner"
        request_record["next_role"] = "planner"
        selection = self._build_governed_routing_selection(
            request_record,
            {},
            current_role="orchestrator",
            requested_role="planner",
            selection_source="sourcer_review",
        )
        request_record["routing_context"] = self._build_routing_context(
            "planner",
            reason="Selected planner because sourcer candidates require planner-owned backlog review and persistence.",
            requested_role=str(selection.get("requested_role") or ""),
            selection_source="sourcer_review",
            matched_signals=[
                str(item).strip()
                for item in (selection.get("matched_signals") or [])
                if str(item).strip()
            ],
            override_reason=str(selection.get("override_reason") or ""),
            matched_strongest_domains=[
                str(item).strip()
                for item in (selection.get("matched_strongest_domains") or [])
                if str(item).strip()
            ],
            matched_preferred_skills=[
                str(item).strip()
                for item in (selection.get("matched_preferred_skills") or [])
                if str(item).strip()
            ],
            matched_behavior_traits=[
                str(item).strip()
                for item in (selection.get("matched_behavior_traits") or [])
                if str(item).strip()
            ],
            policy_source=str(selection.get("policy_source") or ""),
            routing_phase=str(selection.get("routing_phase") or ""),
            request_state_class=str(selection.get("request_state_class") or ""),
            score_total=int(selection.get("score_total") or 0),
            score_breakdown=dict(selection.get("score_breakdown") or {}),
            candidate_summary=list(selection.get("candidate_summary") or []),
        )
        append_request_event(
            request_record,
            event_type="delegated",
            actor="orchestrator",
            summary="internal sourcer 후보를 planner backlog review로 전달했습니다.",
            payload={"routing_context": dict(request_record.get("routing_context") or {})},
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="delegated",
            summary="internal sourcer 후보를 planner backlog review로 전달했습니다.",
        )
        relay_sent = await self._delegate_request(request_record, "planner")
        self._last_backlog_sourcing_activity["planner_review_request_id"] = str(request_record.get("request_id") or "")
        self._last_backlog_sourcing_activity["planner_review_status"] = "delegated" if relay_sent else "relay_failed"
        self._last_backlog_sourcing_activity["planner_review_candidate_count"] = len(normalized_candidates)
        return {
            "request_id": str(request_record.get("request_id") or ""),
            "created": True,
            "reused": False,
            "relay_sent": relay_sent,
            "fingerprint": fingerprint,
        }

    async def _queue_blocked_backlog_for_planner_review(
        self,
        candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        normalized_candidates = self._normalize_blocked_backlog_review_candidates(candidates)
        if not normalized_candidates:
            return {"request_id": "", "created": False, "reused": False, "relay_sent": False}
        fingerprint = self._build_blocked_backlog_review_fingerprint(normalized_candidates)
        existing = self._find_open_blocked_backlog_review_request(fingerprint)
        if existing:
            return {
                "request_id": str(existing.get("request_id") or ""),
                "created": False,
                "reused": True,
                "relay_sent": True,
            }
        request_record = self._build_blocked_backlog_review_request_record(normalized_candidates)
        request_record["status"] = "delegated"
        request_record["current_role"] = "planner"
        request_record["next_role"] = "planner"
        selection = self._build_governed_routing_selection(
            request_record,
            {},
            current_role="orchestrator",
            requested_role="planner",
            selection_source="blocked_backlog_review",
        )
        request_record["routing_context"] = self._build_routing_context(
            "planner",
            reason=(
                "Selected planner because blocked backlog must be explicitly reopened or kept blocked "
                "before future sprint selection."
            ),
            requested_role=str(selection.get("requested_role") or ""),
            selection_source="blocked_backlog_review",
            matched_signals=[
                str(item).strip()
                for item in (selection.get("matched_signals") or [])
                if str(item).strip()
            ],
            override_reason=str(selection.get("override_reason") or ""),
            matched_strongest_domains=[
                str(item).strip()
                for item in (selection.get("matched_strongest_domains") or [])
                if str(item).strip()
            ],
            matched_preferred_skills=[
                str(item).strip()
                for item in (selection.get("matched_preferred_skills") or [])
                if str(item).strip()
            ],
            matched_behavior_traits=[
                str(item).strip()
                for item in (selection.get("matched_behavior_traits") or [])
                if str(item).strip()
            ],
            policy_source=str(selection.get("policy_source") or ""),
            routing_phase=str(selection.get("routing_phase") or ""),
            request_state_class=str(selection.get("request_state_class") or ""),
            score_total=int(selection.get("score_total") or 0),
            score_breakdown=dict(selection.get("score_breakdown") or {}),
            candidate_summary=list(selection.get("candidate_summary") or []),
        )
        append_request_event(
            request_record,
            event_type="delegated",
            actor="orchestrator",
            summary="blocked backlog review를 planner로 전달했습니다.",
            payload={"routing_context": dict(request_record.get("routing_context") or {})},
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="delegated",
            summary="blocked backlog review를 planner로 전달했습니다.",
        )
        relay_sent = await self._delegate_request(request_record, "planner")
        return {
            "request_id": str(request_record.get("request_id") or ""),
            "created": True,
            "reused": False,
            "relay_sent": relay_sent,
        }

    def _build_sprint_planning_request_record(
        self,
        sprint_state: dict[str, Any],
        *,
        phase: str,
        iteration: int,
        step: str = "",
    ) -> dict[str, Any]:
        artifact_paths = self._sprint_artifact_paths(sprint_state)
        normalized_step = str(step or "").strip().lower()
        existing_request = self._find_open_sprint_planning_request(
            sprint_id=str(sprint_state.get("sprint_id") or ""),
            phase=phase,
            step=normalized_step,
        )
        if existing_request:
            return existing_request
        request_id = new_request_id()
        step_title = self._initial_phase_step_title(normalized_step) if normalized_step else ""
        scope = (
            (
                f"sprint initial {normalized_step} for {sprint_state.get('milestone_title') or ''}"
                if normalized_step
                else f"sprint initial planning for {sprint_state.get('milestone_title') or ''}"
            )
            if phase == "initial"
            else f"sprint ongoing review for {sprint_state.get('milestone_title') or ''}"
        )
        body_lines = [
            "Current sprint requires planner-owned milestone refinement, plan/spec updates, mandatory backlog definition, and prioritized backlog/todo selection.",
            "Preserve the original kickoff brief, kickoff requirements, and kickoff reference artifacts as immutable source-of-truth.",
            "Only include backlog items and sprint todos that directly advance this sprint's single milestone.",
            "Do not promote unrelated maintenance or side quests into planned_in_sprint_id for this sprint.",
            "Use the persisted backlog artifacts in Current request.artifacts as backlog history and queue input, but if the current milestone, kickoff requirements, and spec are not fully covered, create or reopen sprint-relevant backlog before prioritization. backlog zero is invalid.",
            f"phase={phase}",
            f"iteration={iteration}",
            f"requested_milestone_title={sprint_state.get('requested_milestone_title') or ''}",
            f"milestone_title={sprint_state.get('milestone_title') or ''}",
            f"sprint_name={sprint_state.get('sprint_name') or ''}",
            f"sprint_folder={sprint_state.get('sprint_folder') or ''}",
        ]
        kickoff_brief = str(sprint_state.get("kickoff_brief") or "").strip()
        kickoff_requirements = [
            str(item).strip()
            for item in (sprint_state.get("kickoff_requirements") or [])
            if str(item).strip()
        ]
        kickoff_source_request_id = str(sprint_state.get("kickoff_source_request_id") or "").strip()
        if kickoff_source_request_id:
            body_lines.append(f"kickoff_source_request_id={kickoff_source_request_id}")
        if kickoff_brief:
            body_lines.extend(["kickoff_brief:", kickoff_brief])
        if kickoff_requirements:
            body_lines.append("kickoff_requirements:")
            body_lines.extend(f"- {item}" for item in kickoff_requirements)
        if phase == "initial" and normalized_step:
            body_lines.extend(
                [
                    f"initial_phase_step={normalized_step}",
                    f"step_title={step_title}",
                    self._initial_phase_step_instruction(normalized_step),
                ]
            )
        body = "\n".join(line for line in body_lines if str(line).strip())
        artifacts = _dedupe_preserving_order(
            [
                self._workspace_artifact_hint(self.paths.shared_backlog_file),
                self._workspace_artifact_hint(self.paths.shared_completed_backlog_file),
                self._workspace_artifact_hint(self.paths.current_sprint_file),
                self._workspace_artifact_hint(artifact_paths["kickoff"]),
                self._workspace_artifact_hint(artifact_paths["milestone"]),
                self._workspace_artifact_hint(artifact_paths["plan"]),
                self._workspace_artifact_hint(artifact_paths["spec"]),
                self._workspace_artifact_hint(artifact_paths["todo_backlog"]),
                self._workspace_artifact_hint(artifact_paths["iteration_log"]),
                *[
                    str(item).strip()
                    for item in (sprint_state.get("kickoff_reference_artifacts") or [])
                    if str(item).strip()
                ],
            ]
        )
        record = {
            "request_id": request_id,
            "status": "queued",
            "intent": "plan",
            "urgency": "normal",
            "scope": scope,
            "body": body,
            "artifacts": artifacts,
            "params": {
                "_teams_kind": "sprint_internal",
                "sprint_id": sprint_state.get("sprint_id") or "",
                "sprint_phase": phase,
                "initial_phase_step": normalized_step,
                "requested_milestone_title": sprint_state.get("requested_milestone_title") or "",
                "milestone_title": sprint_state.get("milestone_title") or "",
                "kickoff_brief": sprint_state.get("kickoff_brief") or "",
                "kickoff_requirements": list(sprint_state.get("kickoff_requirements") or []),
                "kickoff_request_text": sprint_state.get("kickoff_request_text") or "",
                "kickoff_source_request_id": kickoff_source_request_id,
                "kickoff_reference_artifacts": list(sprint_state.get("kickoff_reference_artifacts") or []),
                "sprint_name": sprint_state.get("sprint_name") or "",
                "sprint_folder": sprint_state.get("sprint_folder") or "",
            },
            "current_role": "orchestrator",
            "next_role": "planner",
            "owner_role": "orchestrator",
            "sprint_id": str(sprint_state.get("sprint_id") or ""),
            "backlog_id": "",
            "todo_id": "",
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "fingerprint": build_request_fingerprint(
                author_id="sprint-planner",
                channel_id=str(sprint_state.get("sprint_id") or ""),
                intent="plan",
                scope=scope,
            ),
            "reply_route": {},
            "events": [],
            "result": {},
            "git_baseline": capture_git_baseline(self.paths.project_workspace_root),
            "version_control_status": "",
            "version_control_sha": "",
            "version_control_paths": [],
            "version_control_message": "",
            "version_control_error": "",
            "task_commit_status": "",
            "task_commit_sha": "",
            "task_commit_paths": [],
            "task_commit_message": "",
            "visited_roles": [],
        }
        append_request_event(
            record,
            event_type="created",
            actor="sprint_runner",
            summary=(
                f"스프린트 {phase} planning 요청을 생성했습니다."
                if not normalized_step
                else f"스프린트 {phase} planning 요청을 생성했습니다. step={step_title}"
            ),
        )
        self._save_request(record)
        return record

    def _maybe_update_sprint_name_from_result(self, sprint_state: dict[str, Any], result: dict[str, Any]) -> None:
        proposals = dict(result.get("proposals") or {})
        revised_title = str(
            proposals.get("revised_milestone_title")
            or dict(proposals.get("sprint_plan_update") or {}).get("revised_milestone_title")
            or ""
        ).strip()
        if not revised_title or revised_title == str(sprint_state.get("milestone_title") or "").strip():
            return
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        display_name, folder_name = self._build_manual_sprint_names(
            sprint_id=sprint_id,
            milestone_title=revised_title,
        )
        sprint_state["milestone_title"] = revised_title
        sprint_state["sprint_name"] = display_name
        sprint_state["sprint_display_name"] = display_name
        sprint_state["sprint_folder_name"] = folder_name
        sprint_state["sprint_folder"] = str(self.paths.sprint_artifact_dir(folder_name))

    def _sync_manual_sprint_queue(self, sprint_state: dict[str, Any]) -> None:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        if not sprint_id:
            return
        sprint_milestone_title = str(sprint_state.get("milestone_title") or "").strip()
        selected_items = [
            item
            for item in self._iter_backlog_items()
            if sprint_id
            in {
                str(item.get("planned_in_sprint_id") or "").strip(),
                str(item.get("selected_in_sprint_id") or "").strip(),
            }
            and str(item.get("status") or "").strip().lower() in {"pending", "selected"}
        ]
        selected_items.sort(
            key=lambda item: (
                -int(item.get("priority_rank") or 0),
                str(item.get("created_at") or ""),
            )
        )
        for item in selected_items:
            if sprint_milestone_title and str(item.get("milestone_title") or "").strip() != sprint_milestone_title:
                item["milestone_title"] = sprint_milestone_title
            if str(item.get("status") or "").strip().lower() == "pending":
                item["status"] = "selected"
                item["selected_in_sprint_id"] = sprint_id
            self._save_backlog_item(item)
        sprint_state["selected_items"] = selected_items
        sprint_state["selected_backlog_ids"] = [str(item.get("backlog_id") or "") for item in selected_items]
        existing_by_backlog_id = {
            str(todo.get("backlog_id") or "").strip(): dict(todo)
            for todo in (sprint_state.get("todos") or [])
            if str(todo.get("backlog_id") or "").strip()
        }
        updated_todos: list[dict[str, Any]] = []
        selected_backlog_ids = set()
        for item in selected_items:
            backlog_id = str(item.get("backlog_id") or "").strip()
            selected_backlog_ids.add(backlog_id)
            existing = existing_by_backlog_id.pop(backlog_id, None)
            if existing is None:
                updated_todos.append(build_todo_item(item, owner_role="planner"))
                continue
            existing["title"] = str(item.get("title") or existing.get("title") or "").strip()
            existing["milestone_title"] = sprint_milestone_title or str(
                item.get("milestone_title") or existing.get("milestone_title") or ""
            ).strip()
            existing["priority_rank"] = int(item.get("priority_rank") or existing.get("priority_rank") or 0)
            existing["acceptance_criteria"] = [
                str(value).strip()
                for value in (item.get("acceptance_criteria") or existing.get("acceptance_criteria") or [])
                if str(value).strip()
            ]
            updated_todos.append(existing)
        for backlog_id, todo in existing_by_backlog_id.items():
            status = str(todo.get("status") or "").strip().lower()
            if backlog_id in selected_backlog_ids or status in {
                "running",
                "completed",
                "committed",
                "blocked",
                "failed",
                "uncommitted",
            }:
                updated_todos.append(todo)
        sprint_state["todos"] = self._sort_sprint_todos(updated_todos)

    @staticmethod
    def _normalize_trace_list(values: Any) -> list[str]:
        if isinstance(values, list):
            return [str(item).strip() for item in values if str(item).strip()]
        if isinstance(values, str):
            normalized = str(values).strip()
            return [normalized] if normalized else []
        return []

    def _collect_sprint_relevant_backlog_items(self, sprint_state: dict[str, Any]) -> list[dict[str, Any]]:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        milestone_title = str(sprint_state.get("milestone_title") or "").strip()
        relevant_items: list[dict[str, Any]] = []
        for item in self._iter_backlog_items():
            status = str(item.get("status") or "").strip().lower()
            if status not in SPRINT_ACTIVE_BACKLOG_STATUSES:
                continue
            item_milestone = str(item.get("milestone_title") or "").strip()
            planned_in_sprint_id = str(item.get("planned_in_sprint_id") or "").strip()
            selected_in_sprint_id = str(item.get("selected_in_sprint_id") or "").strip()
            if sprint_id and sprint_id in {planned_in_sprint_id, selected_in_sprint_id}:
                relevant_items.append(item)
                continue
            if milestone_title and item_milestone == milestone_title:
                relevant_items.append(item)
        relevant_items.sort(
            key=lambda item: (
                int(item.get("priority_rank") or 0) if int(item.get("priority_rank") or 0) > 0 else 10**9,
                str(item.get("created_at") or ""),
                str(item.get("backlog_id") or ""),
            )
        )
        return relevant_items

    def _validate_initial_phase_step_result(
        self,
        sprint_state: dict[str, Any],
        *,
        request_record: dict[str, Any],
        sync_summary: dict[str, Any],
    ) -> str:
        step = self._initial_phase_step(request_record)
        if not step:
            return ""
        relevant_items = self._collect_sprint_relevant_backlog_items(sprint_state)
        if step in {
            INITIAL_PHASE_STEP_BACKLOG_DEFINITION,
            INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION,
            INITIAL_PHASE_STEP_TODO_FINALIZATION,
        } and not relevant_items:
            return (
                f"initial phase {self._initial_phase_step_title(step)} 단계에서 sprint-relevant backlog가 0건입니다. "
                "backlog 0건 상태는 허용되지 않습니다."
            )
        if step != INITIAL_PHASE_STEP_BACKLOG_DEFINITION:
            return ""
        if not bool(sync_summary.get("planner_persisted_backlog")):
            return (
                "initial phase backlog 정의 단계에서 planner가 sprint-relevant backlog를 실제로 persist하지 않았습니다. "
                "문서 정리만으로는 다음 단계로 진행할 수 없습니다."
            )
        kickoff_requirements = self._normalize_trace_list(sprint_state.get("kickoff_requirements") or [])
        validation_errors: list[str] = []
        for item in relevant_items:
            title = str(item.get("title") or item.get("backlog_id") or "unnamed backlog").strip()
            acceptance = self._normalize_trace_list(item.get("acceptance_criteria") or [])
            origin = dict(item.get("origin") or {})
            milestone_ref = str(origin.get("milestone_ref") or "").strip()
            requirement_refs = self._normalize_trace_list(origin.get("requirement_refs") or [])
            spec_refs = self._normalize_trace_list(origin.get("spec_refs") or [])
            if not acceptance:
                validation_errors.append(f"{title}: acceptance_criteria 없음")
            if not milestone_ref:
                validation_errors.append(f"{title}: origin.milestone_ref 없음")
            if kickoff_requirements and not requirement_refs:
                validation_errors.append(f"{title}: origin.requirement_refs 없음")
            if not spec_refs:
                validation_errors.append(f"{title}: origin.spec_refs 없음")
        if validation_errors:
            return (
                "initial phase backlog 정의 단계의 backlog trace가 부족합니다. "
                + "; ".join(validation_errors[:4])
            )
        return ""

    def _record_sprint_planning_iteration(
        self,
        sprint_state: dict[str, Any],
        *,
        phase: str,
        step: str,
        request_record: dict[str, Any],
        result: dict[str, Any],
        phase_ready: bool,
    ) -> None:
        iterations = list(sprint_state.get("planning_iterations") or [])
        iteration_entry = {
            "created_at": utc_now_iso(),
            "phase": phase,
            "step": step,
            "request_id": str(request_record.get("request_id") or ""),
            "summary": str(result.get("summary") or "").strip(),
            "insights": [str(item).strip() for item in (result.get("insights") or []) if str(item).strip()],
            "artifacts": [str(item).strip() for item in (result.get("artifacts") or []) if str(item).strip()],
            "phase_ready": bool(phase_ready),
        }
        matched_index = next(
            (
                index
                for index, entry in enumerate(iterations)
                if str(entry.get("request_id") or "").strip() == iteration_entry["request_id"]
                and str(entry.get("phase") or "").strip() == iteration_entry["phase"]
            ),
            -1,
        )
        if matched_index >= 0:
            existing_created_at = str(iterations[matched_index].get("created_at") or "").strip()
            if existing_created_at:
                iteration_entry["created_at"] = existing_created_at
            iterations[matched_index] = iteration_entry
        else:
            iterations.append(iteration_entry)
        sprint_state["planning_iterations"] = iterations

    def _sync_sprint_planning_state(
        self,
        sprint_state: dict[str, Any],
        *,
        phase: str,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> bool:
        self._maybe_update_sprint_name_from_result(sprint_state, result)
        self._sync_manual_sprint_queue(sprint_state)
        params = dict(request_record.get("params") or {})
        step = str(params.get("initial_phase_step") or "").strip().lower()
        phase_ready = bool(sprint_state.get("selected_items"))
        if phase == "initial" and step and step != INITIAL_PHASE_STEP_TODO_FINALIZATION:
            phase_ready = False
        self._record_sprint_planning_iteration(
            sprint_state,
            phase=phase,
            step=step,
            request_record=request_record,
            result=result,
            phase_ready=phase_ready,
        )
        return phase_ready

    def _sync_internal_sprint_artifacts_from_role_report(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._is_internal_sprint_request(request_record):
            return {}
        if str(result.get("role") or "").strip() != "planner":
            return {}
        if str(result.get("status") or "").strip().lower() not in {"completed", "committed"}:
            return {}
        params = dict(request_record.get("params") or {})
        sprint_id = str(request_record.get("sprint_id") or params.get("sprint_id") or "").strip()
        if not sprint_id:
            return {}
        sprint_state = self._load_sprint_state(sprint_id)
        if not sprint_state:
            return {}
        phase = str(params.get("sprint_phase") or "").strip() or "initial"
        sync_summary = self._sync_planner_backlog_from_report(request_record, result)
        request_record["planning_sync_summary"] = sync_summary
        self._sync_sprint_planning_state(
            sprint_state,
            phase=phase,
            request_record=request_record,
            result=result,
        )
        self._save_sprint_state(sprint_state)
        iteration_entry = sync_summary
        if iteration_entry.get("missing_backlog_artifacts") or iteration_entry.get("missing_backlog_receipts"):
            self._append_sprint_event(
                sprint_id,
                event_type="planning_sync_warning",
                summary="planner backlog persistence receipt 또는 아티팩트를 확인할 수 없습니다.",
                payload={
                    "request_id": request_record.get("request_id") or "",
                    "missing_backlog_artifacts": list(iteration_entry.get("missing_backlog_artifacts") or []),
                    "missing_backlog_receipts": list(iteration_entry.get("missing_backlog_receipts") or []),
                },
            )
        return sync_summary

    def _sync_planner_backlog_review_from_role_report(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if not self._is_planner_backlog_review_request(request_record):
            return {}
        if str(result.get("role") or "").strip() != "planner":
            return {}
        status = str(result.get("status") or "").strip().lower()
        if not self._is_terminal_internal_request_status(status):
            return {}
        sync_summary: dict[str, Any] = {}
        if status in {"completed", "committed"}:
            sync_summary = self._sync_planner_backlog_from_report(request_record, result)
            request_record["planning_sync_summary"] = sync_summary
        if self._is_blocked_backlog_review_request(request_record):
            scheduler_state = self._load_scheduler_state()
            scheduler_state["last_blocked_review_at"] = utc_now_iso()
            scheduler_state["last_blocked_review_request_id"] = str(request_record.get("request_id") or "")
            scheduler_state["last_blocked_review_status"] = status or "completed"
            scheduler_state["last_blocked_review_fingerprint"] = str(request_record.get("fingerprint") or "").strip()
            self._save_scheduler_state(scheduler_state)
        if self._is_sourcer_review_request(request_record):
            scheduler_state = self._load_scheduler_state()
            scheduler_state["last_sourcing_fingerprint"] = str(request_record.get("fingerprint") or "").strip()
            scheduler_state["last_sourcing_review_status"] = status or "completed"
            scheduler_state["last_sourcing_review_request_id"] = str(request_record.get("request_id") or "")
            self._save_scheduler_state(scheduler_state)
        return sync_summary

    def _apply_sprint_planning_result(
        self,
        sprint_state: dict[str, Any],
        *,
        phase: str,
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> bool:
        step = self._initial_phase_step(request_record)
        phase_ready = self._sync_sprint_planning_state(
            sprint_state,
            phase=phase,
            request_record=request_record,
            result=result,
        )
        sync_summary = (
            dict(request_record.get("planning_sync_summary"))
            if isinstance(request_record.get("planning_sync_summary"), dict)
            else self._sync_planner_backlog_from_report(request_record, result, persist=False)
        )
        self._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="planning_sync",
            summary=(
                f"sprint {phase} planning 결과를 반영했습니다."
                if not step
                else f"sprint {phase} planning 결과를 반영했습니다. step={self._initial_phase_step_title(step)}"
            ),
            payload={
                "request_id": request_record.get("request_id") or "",
                "planner_persisted_backlog": bool(sync_summary.get("planner_persisted_backlog")),
                "selected_count": len(sprint_state.get("selected_items") or []),
                "proposal_items": int(sync_summary.get("proposal_items") or 0),
                "receipt_items": int(sync_summary.get("receipt_items") or 0),
                "artifact_items": int(sync_summary.get("artifact_items") or 0),
                "merged_items": int(sync_summary.get("merged_items") or 0),
                "verified_backlog_items": int(sync_summary.get("verified_backlog_items") or 0),
                "persisted_backlog_items": int(sync_summary.get("persisted_backlog_items") or 0),
                "missing_backlog_artifacts": sync_summary.get("missing_backlog_artifacts") or [],
                "missing_backlog_receipts": sync_summary.get("missing_backlog_receipts") or [],
                "initial_phase_step": step,
            },
        )
        if sync_summary.get("missing_backlog_artifacts") or sync_summary.get("missing_backlog_receipts"):
            self._append_sprint_event(
                str(sprint_state.get("sprint_id") or ""),
                event_type="planning_sync_warning",
                summary="planner backlog persistence receipt 또는 아티팩트 확인이 필요합니다.",
                payload={
                    "request_id": request_record.get("request_id") or "",
                    "missing_backlog_artifacts": list(sync_summary.get("missing_backlog_artifacts") or []),
                    "missing_backlog_receipts": list(sync_summary.get("missing_backlog_receipts") or []),
                },
            )
        validation_error = ""
        if phase == "initial":
            validation_error = self._validate_initial_phase_step_result(
                sprint_state,
                request_record=request_record,
                sync_summary=sync_summary,
            )
        request_record["initial_phase_validation_error"] = validation_error
        self._save_request(request_record)
        if validation_error:
            self._append_sprint_event(
                str(sprint_state.get("sprint_id") or ""),
                event_type="planning_sync_invalid",
                summary=validation_error,
                payload={
                    "request_id": request_record.get("request_id") or "",
                    "initial_phase_step": step,
                },
            )
            return False
        return phase_ready

    async def _run_initial_sprint_phase(self, sprint_state: dict[str, Any]) -> bool:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        if self._is_wrap_up_requested(sprint_state):
            sprint_state["phase"] = "wrap_up"
            self._save_sprint_state(sprint_state)
            return False
        for iteration in range(1, SPRINT_INITIAL_PHASE_MAX_ITERATIONS + 1):
            phase_ready = False
            for step in INITIAL_PHASE_STEPS:
                if self._is_wrap_up_requested(sprint_state):
                    sprint_state["phase"] = "wrap_up"
                    self._save_sprint_state(sprint_state)
                    return False
                request_record = self._build_sprint_planning_request_record(
                    sprint_state,
                    phase="initial",
                    iteration=iteration,
                    step=step,
                )
                result = await self._run_internal_request_chain(
                    sprint_id=sprint_id,
                    request_record=request_record,
                    initial_role="planner",
                )
                request_record = self._load_request(str(request_record.get("request_id") or "")) or request_record
                if str(result.get("status") or "").strip().lower() != "completed":
                    sprint_state["status"] = "blocked"
                    sprint_state["closeout_status"] = "planning_incomplete"
                    sprint_state["ended_at"] = utc_now_iso()
                    closeout_result = {
                        "status": "planning_incomplete",
                        "message": (
                            "initial phase planning이 완료되지 않아 sprint를 시작하지 못했습니다. "
                            f"step={self._initial_phase_step_title(step)} | "
                            f"request_id={request_record.get('request_id') or ''} | "
                            f"summary={result.get('summary') or result.get('error') or ''}"
                        ).strip(),
                        "commit_count": int(sprint_state.get("commit_count") or 0),
                        "commit_shas": list(sprint_state.get("commit_shas") or []),
                        "representative_commit_sha": str(sprint_state.get("commit_sha") or ""),
                        "uncommitted_paths": list(sprint_state.get("uncommitted_paths") or []),
                    }
                    sprint_state["report_body"] = await self._prepare_sprint_report_body(sprint_state, closeout_result)
                    sprint_state["report_path"] = self._archive_sprint_history(sprint_state, sprint_state["report_body"])
                    self._save_sprint_state(sprint_state)
                    await self._send_terminal_sprint_reports(
                        title="⚠️ 스프린트 시작 실패",
                        sprint_state=sprint_state,
                        closeout_result=closeout_result,
                    )
                    self._finish_scheduler_after_sprint(sprint_state, clear_active=True)
                    return False
                phase_ready = self._apply_sprint_planning_result(
                    sprint_state,
                    phase="initial",
                    request_record=request_record,
                    result=result,
                )
                validation_error = str(request_record.get("initial_phase_validation_error") or "").strip()
                if validation_error:
                    sprint_state["status"] = "blocked"
                    sprint_state["closeout_status"] = "planning_incomplete"
                    sprint_state["ended_at"] = utc_now_iso()
                    closeout_result = {
                        "status": "planning_incomplete",
                        "message": validation_error,
                        "commit_count": int(sprint_state.get("commit_count") or 0),
                        "commit_shas": list(sprint_state.get("commit_shas") or []),
                        "representative_commit_sha": str(sprint_state.get("commit_sha") or ""),
                        "uncommitted_paths": list(sprint_state.get("uncommitted_paths") or []),
                    }
                    sprint_state["report_body"] = await self._prepare_sprint_report_body(sprint_state, closeout_result)
                    sprint_state["report_path"] = self._archive_sprint_history(sprint_state, sprint_state["report_body"])
                    self._save_sprint_state(sprint_state)
                    await self._send_terminal_sprint_reports(
                        title="⚠️ 스프린트 시작 실패",
                        sprint_state=sprint_state,
                        closeout_result=closeout_result,
                    )
                    self._finish_scheduler_after_sprint(sprint_state, clear_active=True)
                    return False
                self._save_sprint_state(sprint_state)
            if phase_ready:
                self._save_sprint_state(sprint_state)
                missing_artifacts = self._missing_sprint_preflight_artifacts(sprint_state)
                if missing_artifacts:
                    sprint_state["status"] = "blocked"
                    sprint_state["closeout_status"] = "planning_incomplete"
                    sprint_state["ended_at"] = utc_now_iso()
                    closeout_result = {
                        "status": "planning_incomplete",
                        "message": (
                            "initial phase는 완료됐지만 sprint start 전 canonical spec/todo 문서가 아직 닫히지 않았습니다. "
                            f"missing={', '.join(missing_artifacts)}"
                        ).strip(),
                        "commit_count": int(sprint_state.get("commit_count") or 0),
                        "commit_shas": list(sprint_state.get("commit_shas") or []),
                        "representative_commit_sha": str(sprint_state.get("commit_sha") or ""),
                        "uncommitted_paths": list(sprint_state.get("uncommitted_paths") or []),
                    }
                    sprint_state["report_body"] = await self._prepare_sprint_report_body(sprint_state, closeout_result)
                    sprint_state["report_path"] = self._archive_sprint_history(sprint_state, sprint_state["report_body"])
                    self._save_sprint_state(sprint_state)
                    await self._send_terminal_sprint_reports(
                        title="⚠️ 스프린트 시작 실패",
                        sprint_state=sprint_state,
                        closeout_result=closeout_result,
                    )
                    self._finish_scheduler_after_sprint(sprint_state, clear_active=True)
                    return False
                try:
                    await self._send_sprint_spec_todo_report(sprint_state, swallow_exceptions=False)
                except Exception as exc:
                    sprint_state["status"] = "blocked"
                    sprint_state["closeout_status"] = "planning_incomplete"
                    sprint_state["ended_at"] = utc_now_iso()
                    closeout_result = {
                        "status": "planning_incomplete",
                        "message": (
                            "initial phase는 완료됐지만 sprint start 전 spec/todo 보고 전송에 실패했습니다. "
                            f"{str(exc).strip()}"
                        ).strip(),
                        "commit_count": int(sprint_state.get("commit_count") or 0),
                        "commit_shas": list(sprint_state.get("commit_shas") or []),
                        "representative_commit_sha": str(sprint_state.get("commit_sha") or ""),
                        "uncommitted_paths": list(sprint_state.get("uncommitted_paths") or []),
                    }
                    sprint_state["report_body"] = await self._prepare_sprint_report_body(sprint_state, closeout_result)
                    sprint_state["report_path"] = self._archive_sprint_history(sprint_state, sprint_state["report_body"])
                    self._save_sprint_state(sprint_state)
                    await self._send_terminal_sprint_reports(
                        title="⚠️ 스프린트 시작 실패",
                        sprint_state=sprint_state,
                        closeout_result=closeout_result,
                    )
                    self._finish_scheduler_after_sprint(sprint_state, clear_active=True)
                    return False
                sprint_state["phase"] = "ongoing"
                sprint_state["status"] = "running"
                sprint_state["initial_phase_ready_at"] = utc_now_iso()
                sprint_state["last_planner_review_at"] = utc_now_iso()
                self._save_sprint_state(sprint_state)
                return True
        sprint_state["status"] = "blocked"
        sprint_state["closeout_status"] = "planning_incomplete"
        sprint_state["ended_at"] = utc_now_iso()
        closeout_result = {
            "status": "planning_incomplete",
            "message": "initial phase에서 실행 가능한 prioritized todo를 만들지 못해 sprint를 중단했습니다.",
            "commit_count": int(sprint_state.get("commit_count") or 0),
            "commit_shas": list(sprint_state.get("commit_shas") or []),
            "representative_commit_sha": str(sprint_state.get("commit_sha") or ""),
            "uncommitted_paths": list(sprint_state.get("uncommitted_paths") or []),
        }
        sprint_state["report_body"] = await self._prepare_sprint_report_body(sprint_state, closeout_result)
        sprint_state["report_path"] = self._archive_sprint_history(sprint_state, sprint_state["report_body"])
        self._save_sprint_state(sprint_state)
        await self._send_terminal_sprint_reports(
            title="⚠️ 스프린트 시작 실패",
            sprint_state=sprint_state,
            closeout_result=closeout_result,
        )
        self._finish_scheduler_after_sprint(sprint_state, clear_active=True)
        return False

    async def _run_ongoing_sprint_review(self, sprint_state: dict[str, Any], *, force: bool = False) -> None:
        if self._is_wrap_up_requested(sprint_state):
            sprint_state["phase"] = "wrap_up"
            self._save_sprint_state(sprint_state)
            return
        last_review_at = self._parse_datetime(str(sprint_state.get("last_planner_review_at") or ""))
        if not force and last_review_at is not None:
            elapsed_seconds = (utc_now() - last_review_at).total_seconds()
            if elapsed_seconds < max(float(self.runtime_config.sprint_interval_minutes) * 60.0, 1.0):
                return
        request_record = self._build_sprint_planning_request_record(
            sprint_state,
            phase="ongoing_review",
            iteration=len(sprint_state.get("planning_iterations") or []) + 1,
        )
        result = await self._run_internal_request_chain(
            sprint_id=str(sprint_state.get("sprint_id") or ""),
            request_record=request_record,
            initial_role="planner",
        )
        request_record = self._load_request(str(request_record.get("request_id") or "")) or request_record
        if str(result.get("status") or "").strip().lower() != "completed":
            return
        self._apply_sprint_planning_result(
            sprint_state,
            phase="ongoing_review",
            request_record=request_record,
            result=result,
        )
        sprint_state["last_planner_review_at"] = utc_now_iso()
        self._save_sprint_state(sprint_state)

    async def _continue_manual_daily_sprint(self, sprint_state: dict[str, Any], *, announce: bool) -> None:
        if str(sprint_state.get("phase") or "").strip() == "initial":
            if self._is_wrap_up_requested(sprint_state):
                sprint_state["phase"] = "wrap_up"
                self._save_sprint_state(sprint_state)
                await self._finalize_sprint(sprint_state)
                return
            ready = await self._run_initial_sprint_phase(sprint_state)
            if not ready:
                if self._is_wrap_up_requested(sprint_state):
                    sprint_state["phase"] = "wrap_up"
                    self._save_sprint_state(sprint_state)
                    await self._finalize_sprint(sprint_state)
                return
            announce = True
        sprint_state["phase"] = "ongoing"
        sprint_state["status"] = "running"
        self._save_sprint_state(sprint_state)
        if announce:
            await self._send_sprint_kickoff(sprint_state)
            await self._send_sprint_todo_list(sprint_state)
        force_review = not bool(sprint_state.get("last_planner_review_at"))
        while True:
            if self._is_wrap_up_requested(sprint_state) or self._is_manual_sprint_cutoff_reached(sprint_state):
                sprint_state["phase"] = "wrap_up"
                self._save_sprint_state(sprint_state)
                await self._finalize_sprint(sprint_state)
                return
            await self._run_ongoing_sprint_review(sprint_state, force=force_review)
            force_review = False
            self._sync_manual_sprint_queue(sprint_state)
            self._save_sprint_state(sprint_state)
            next_todo = next(
                (
                    todo
                    for todo in (sprint_state.get("todos") or [])
                    if self._is_executable_todo_status(str(todo.get("status") or "").strip().lower())
                ),
                None,
            )
            if next_todo is None:
                sprint_state["phase"] = "wrap_up"
                self._save_sprint_state(sprint_state)
                await self._finalize_sprint(sprint_state)
                return
            await self._execute_sprint_todo(sprint_state, next_todo)
            self._save_sprint_state(sprint_state)
            force_review = True

    async def _continue_sprint(self, sprint_state: dict[str, Any], *, announce: bool) -> None:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        if not sprint_id:
            return
        if self._prepare_requested_restart_checkpoint(sprint_state):
            self._save_sprint_state(sprint_state)
        if self._sprint_uses_manual_flow(sprint_state):
            await self._continue_manual_daily_sprint(sprint_state, announce=announce)
            return
        dropped_ids = self._drop_non_actionable_backlog_items()
        repaired_ids = self._repair_non_actionable_carry_over_backlog_items()
        combined_pruned_ids = dropped_ids | repaired_ids
        if combined_pruned_ids:
            self._refresh_backlog_markdown()
        if self._prune_dropped_backlog_from_sprint(sprint_state, combined_pruned_ids):
            self._save_sprint_state(sprint_state)
        if self._is_wrap_up_requested(sprint_state):
            sprint_state["phase"] = "wrap_up"
            self._save_sprint_state(sprint_state)
            await self._finalize_sprint(sprint_state)
            return
        if not list(sprint_state.get("selected_items") or []):
            sprint_state["status"] = "completed"
            sprint_state["closeout_status"] = "no_selected_backlog"
            sprint_state["ended_at"] = utc_now_iso()
            closeout_result = {
                "status": "no_selected_backlog",
                "message": "선택된 backlog가 없어 스프린트를 종료했습니다.",
                "commit_count": int(sprint_state.get("commit_count") or 0),
                "commit_shas": list(sprint_state.get("commit_shas") or []),
                "representative_commit_sha": str(sprint_state.get("commit_sha") or ""),
                "uncommitted_paths": list(sprint_state.get("uncommitted_paths") or []),
            }
            sprint_state["report_body"] = await self._prepare_sprint_report_body(sprint_state, closeout_result)
            sprint_state["report_path"] = self._archive_sprint_history(sprint_state, sprint_state["report_body"])
            self._save_sprint_state(sprint_state)
            await self._send_terminal_sprint_reports(
                title="🛑 스프린트 종료",
                sprint_state=sprint_state,
                closeout_result=closeout_result,
            )
            self._finish_scheduler_after_sprint(sprint_state)
            return
        if not list(sprint_state.get("todos") or []):
            sprint_state["todos"] = [
                build_todo_item(item, owner_role="planner")
                for item in sprint_state.get("selected_items") or []
            ]
        sprint_state["status"] = "running"
        self._save_sprint_state(sprint_state)
        if announce:
            await self._send_sprint_kickoff(sprint_state)
            await self._send_sprint_todo_list(sprint_state)
        for todo in sprint_state.get("todos") or []:
            if self._is_wrap_up_requested(sprint_state):
                sprint_state["phase"] = "wrap_up"
                self._save_sprint_state(sprint_state)
                await self._finalize_sprint(sprint_state)
                return
            if _is_terminal_todo_status(str(todo.get("status") or "")):
                continue
            await self._execute_sprint_todo(sprint_state, todo)
            self._save_sprint_state(sprint_state)
            if str(todo.get("status") or "").strip().lower() == "uncommitted":
                return
            if self._is_wrap_up_requested(sprint_state):
                sprint_state["phase"] = "wrap_up"
                self._save_sprint_state(sprint_state)
                await self._finalize_sprint(sprint_state)
                return
        await self._finalize_sprint(sprint_state)

    async def _finalize_sprint(self, sprint_state: dict[str, Any]) -> None:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        if not sprint_id:
            return
        sprint_state["status"] = "closeout"
        self._save_sprint_state(sprint_state)
        baseline = sprint_state.get("git_baseline") or {}
        documentation_closeout = self._inspect_sprint_documentation_closeout(sprint_state)
        if str(documentation_closeout.get("status") or "").strip() != "verified":
            closeout_result = {
                "status": str(documentation_closeout.get("status") or "planning_incomplete").strip(),
                "repo_root": str(baseline.get("repo_root") or ""),
                "commit_count": 0,
                "commit_shas": [],
                "representative_commit_sha": "",
                "uncommitted_paths": [],
                "message": str(documentation_closeout.get("message") or "").strip(),
                "missing_sections": list(documentation_closeout.get("missing_sections") or []),
            }
        else:
            closeout_result = await asyncio.to_thread(
                inspect_sprint_closeout,
                self.paths.project_workspace_root,
                baseline,
                sprint_id,
            )
        if closeout_result.get("status") == "pending_changes":
            version_control_result = await self._run_closeout_version_controller(
                sprint_state=sprint_state,
                closeout_result=closeout_result,
            )
            sprint_state["version_control_status"] = str(
                version_control_result.get("commit_status") or version_control_result.get("status") or ""
            ).strip()
            sprint_state["version_control_sha"] = str(version_control_result.get("commit_sha") or "").strip()
            sprint_state["version_control_paths"] = [
                str(item).strip()
                for item in (
                    version_control_result.get("commit_paths")
                    or version_control_result.get("changed_paths")
                    or []
                )
                if str(item).strip()
            ]
            sprint_state["version_control_message"] = str(version_control_result.get("commit_message") or "").strip()
            sprint_state["version_control_error"] = str(version_control_result.get("error") or "").strip()
            sprint_state["auto_commit_status"] = sprint_state["version_control_status"]
            sprint_state["auto_commit_sha"] = sprint_state["version_control_sha"]
            sprint_state["auto_commit_paths"] = list(sprint_state["version_control_paths"])
            sprint_state["auto_commit_message"] = sprint_state["version_control_message"]
            if sprint_state["version_control_status"] in {"committed", "no_changes"}:
                closeout_result = await asyncio.to_thread(
                    inspect_sprint_closeout,
                    self.paths.project_workspace_root,
                    baseline,
                    sprint_id,
                )
            else:
                closeout_result = {
                    "status": "version_control_failed",
                    "repo_root": str(closeout_result.get("repo_root") or ""),
                    "commit_count": int(closeout_result.get("commit_count") or 0),
                    "commit_shas": [
                        str(item).strip()
                        for item in (closeout_result.get("commit_shas") or [])
                        if str(item).strip()
                    ],
                    "representative_commit_sha": str(closeout_result.get("representative_commit_sha") or "").strip(),
                    "uncommitted_paths": [
                        str(item).strip()
                        for item in (
                            version_control_result.get("commit_paths")
                            or version_control_result.get("changed_paths")
                            or closeout_result.get("uncommitted_paths")
                            or []
                        )
                        if str(item).strip()
                    ],
                    "message": (
                        "스프린트 closeout version_controller 단계에 실패했습니다. "
                        f"{str(version_control_result.get('summary') or version_control_result.get('error') or '').strip()}"
                    ).strip(),
                }
        else:
            sprint_state["version_control_status"] = "not_needed"
            sprint_state["version_control_sha"] = ""
            sprint_state["version_control_paths"] = []
            sprint_state["version_control_message"] = ""
            sprint_state["version_control_error"] = ""
            sprint_state["auto_commit_status"] = "not_needed"
            sprint_state["auto_commit_sha"] = ""
            sprint_state["auto_commit_paths"] = []
            sprint_state["auto_commit_message"] = ""
        sprint_state["commit_sha"] = str(closeout_result.get("representative_commit_sha") or "")
        sprint_state["commit_shas"] = [
            str(item).strip()
            for item in (closeout_result.get("commit_shas") or [])
            if str(item).strip()
        ]
        sprint_state["commit_count"] = int(closeout_result.get("commit_count") or 0)
        sprint_state["closeout_status"] = str(closeout_result.get("status") or "").strip()
        sprint_state["uncommitted_paths"] = [
            str(item).strip()
            for item in (closeout_result.get("uncommitted_paths") or [])
            if str(item).strip()
        ]
        if closeout_result.get("status") in {"verified", "no_new_commits", "no_repo", "warning_missing_sprint_tag"}:
            sprint_state["status"] = "completed"
        else:
            sprint_state["status"] = "failed"
        sprint_state["ended_at"] = utc_now_iso()
        report_body = await self._prepare_sprint_report_body(sprint_state, closeout_result)
        sprint_state["report_body"] = report_body
        sprint_state["report_path"] = self._archive_sprint_history(sprint_state, report_body)
        self._save_sprint_state(sprint_state)
        report_title = "⚠️ 스프린트 완료(경고)" if closeout_result.get("status") == "warning_missing_sprint_tag" else (
            "✅ 스프린트 완료" if sprint_state["status"] == "completed" else "⚠️ 스프린트 실패"
        )
        await self._send_terminal_sprint_reports(
            title=report_title,
            sprint_state=sprint_state,
            closeout_result=closeout_result,
            judgment=str(closeout_result.get("message") or report_title).strip(),
            commit_message=(
                str(sprint_state.get("version_control_message") or "").strip()
                if str(sprint_state.get("version_control_status") or "").strip() == "committed"
                else ""
            ),
            related_artifacts=self._collect_artifact_candidates(
                [
                    str(sprint_state.get("report_path") or "").strip(),
                    *[
                        str(item).strip()
                        for item in (sprint_state.get("version_control_paths") or [])
                        if str(item).strip()
                    ],
                    *[
                        str(item).strip()
                        for item in (sprint_state.get("uncommitted_paths") or [])
                        if str(item).strip()
                    ],
                ]
            ),
        )
        self._finish_scheduler_after_sprint(sprint_state)

    async def _fail_sprint_due_to_exception(self, sprint_state: dict[str, Any], exc: Exception) -> None:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip() or "unknown"
        LOGGER.exception("Autonomous sprint failed unexpectedly: %s", sprint_id)
        sprint_state["status"] = "failed"
        sprint_state["ended_at"] = utc_now_iso()
        self._append_sprint_event(
            sprint_id,
            event_type="failed",
            summary="스프린트 실행 중 예외가 발생했습니다.",
            payload={"error": str(exc)},
        )
        closeout_result = {
            "status": "failed",
            "message": str(exc),
            "representative_commit_sha": "",
            "commit_count": int(sprint_state.get("commit_count") or 0),
            "commit_shas": list(sprint_state.get("commit_shas") or []),
            "uncommitted_paths": list(sprint_state.get("uncommitted_paths") or []),
        }
        report_body = await self._prepare_sprint_report_body(sprint_state, closeout_result)
        sprint_state["report_body"] = report_body
        sprint_state["report_path"] = self._archive_sprint_history(sprint_state, report_body)
        self._save_sprint_state(sprint_state)
        await self._send_terminal_sprint_reports(
            title="⚠️ 스프린트 실패",
            sprint_state=sprint_state,
            closeout_result=closeout_result,
        )
        self._finish_scheduler_after_sprint(sprint_state)

    async def _send_sprint_kickoff(self, sprint_state: dict[str, Any]) -> None:
        body_lines = [
            f"📌 sprint_id={sprint_state.get('sprint_id') or ''}",
            f"🧭 trigger={sprint_state.get('trigger') or ''}",
            f"🗂️ selected_backlog={len(sprint_state.get('selected_items') or [])}",
            "📝 kickoff_items:",
            *self._build_sprint_kickoff_preview_lines(sprint_state),
        ]
        body = "\n".join(body_lines)
        await self._send_sprint_report(
            title="🚀 스프린트 시작",
            body=body,
            sections=self._build_sprint_kickoff_report_sections(sprint_state),
        )

    def _build_sprint_kickoff_preview_lines(self, sprint_state: dict[str, Any], *, limit: int = 3) -> list[str]:
        todos = list(sprint_state.get("todos") or [])
        selected_items = list(sprint_state.get("selected_items") or [])
        if todos:
            lines = [
                "- {title} | todo_id={todo_id} | owner={owner}".format(
                    title=_truncate_text(str(todo.get("title") or "").strip() or "Untitled", limit=60),
                    todo_id=str(todo.get("todo_id") or "").strip() or "N/A",
                    owner=str(todo.get("owner_role") or "").strip() or "N/A",
                )
                for todo in todos[:limit]
            ]
            remaining = len(todos) - limit
            if remaining > 0:
                lines.append(f"- ... 외 {remaining}건")
            return lines
        if selected_items:
            lines = [
                "- {title} | backlog_id={backlog_id}".format(
                    title=_truncate_text(str(item.get("title") or "").strip() or "Untitled", limit=60),
                    backlog_id=str(item.get("backlog_id") or "").strip() or "N/A",
                )
                for item in selected_items[:limit]
            ]
            remaining = len(selected_items) - limit
            if remaining > 0:
                lines.append(f"- ... 외 {remaining}건")
            return lines
        return ["- 선택된 작업 없음"]

    async def _send_sprint_todo_list(self, sprint_state: dict[str, Any]) -> None:
        lines = [
            f"sprint_id={sprint_state.get('sprint_id') or ''}",
            "todo_list:",
        ]
        for todo in sprint_state.get("todos") or []:
            lines.append(f"- {todo.get('todo_id') or ''} | {todo.get('title') or ''} | owner={todo.get('owner_role') or ''}")
        await self._send_sprint_report(
            title="스프린트 TODO",
            body="\n".join(lines),
            sections=self._build_sprint_todo_list_report_sections(sprint_state),
        )

    async def _send_sprint_completion_user_report(
        self,
        *,
        title: str,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> None:
        channel_id = str(self.discord_config.report_channel_id or "").strip()
        if not channel_id:
            return
        try:
            await self._send_discord_content(
                content=self._render_sprint_completion_user_report(
                    sprint_state,
                    closeout_result,
                    title=title,
                ),
                send=lambda chunk: self.discord_client.send_channel_message(channel_id, chunk),
                target_description=f"sprint-user-report:{channel_id}:{sprint_state.get('sprint_id') or ''}",
                swallow_exceptions=False,
                log_traceback=False,
            )
        except Exception as exc:
            LOGGER.warning(
                "Failed to send user-facing sprint summary for sprint %s to report:%s: %s",
                sprint_state.get("sprint_id") or "unknown",
                channel_id,
                exc,
            )

    async def _send_terminal_sprint_reports(
        self,
        *,
        title: str,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
        judgment: str = "",
        commit_message: str = "",
        related_artifacts: list[str] | None = None,
    ) -> None:
        report_body = str(sprint_state.get("report_body") or "").strip()
        await self._send_sprint_report(
            title=title,
            body=report_body,
            sprint_id=str(sprint_state.get("sprint_id") or ""),
            judgment=judgment or str(closeout_result.get("message") or title).strip(),
            commit_message=commit_message,
            related_artifacts=related_artifacts,
            log_summary=self._build_sprint_progress_log_summary(sprint_state, closeout_result),
            sections=self._build_terminal_sprint_report_sections(sprint_state, closeout_result),
        )
        await self._send_sprint_completion_user_report(
            title=title,
            sprint_state=sprint_state,
            closeout_result=closeout_result,
        )

    async def _send_sprint_report(
        self,
        *,
        title: str,
        body: str,
        sprint_id: str = "",
        status: str = "완료",
        end_reason: str = "없음",
        judgment: str = "",
        next_action: str = "대기",
        commit_message: str = "",
        related_artifacts: list[str] | None = None,
        log_summary: str = "",
        sections: list[ReportSection] | None = None,
        swallow_exceptions: bool = True,
    ) -> None:
        rendered_title = _decorate_sprint_report_title(title)
        active_sprint_id = str(sprint_id or self._load_scheduler_state().get("active_sprint_id") or "").strip()
        report_artifacts = self._collect_artifact_candidates(
            related_artifacts or [],
            [
                str(self.paths.current_sprint_file),
                str(self.paths.shared_backlog_file),
                str(self.paths.shared_completed_backlog_file),
                *([str(self.paths.sprint_events_file(active_sprint_id))] if active_sprint_id else []),
            ],
        )
        report = build_progress_report(
            request=rendered_title,
            scope=self._format_sprint_scope(sprint_id=sprint_id),
            status=status,
            list_summary="sprint runner",
            detail_summary=rendered_title,
            process_summary="없음",
            log_summary=log_summary or (body[:500] if body else "없음"),
            end_reason=end_reason,
            judgment=judgment or rendered_title,
            next_action=next_action,
            commit_message=commit_message,
            artifacts=report_artifacts,
            sections=sections if sections is not None else self._build_generic_sprint_report_sections(body),
        )
        await self._send_discord_content(
            content=report,
            send=lambda chunk: self.discord_client.send_channel_message(self.discord_config.startup_channel_id, chunk),
            target_description=f"sprint-report:{self.discord_config.startup_channel_id}:{rendered_title}",
            swallow_exceptions=swallow_exceptions,
        )

    @staticmethod
    def _planner_initial_phase_report_keys(request_record: dict[str, Any]) -> list[str]:
        return [
            str(item).strip()
            for item in (request_record.get("planner_initial_phase_report_keys") or [])
            if str(item).strip()
        ]

    def _planner_initial_phase_report_key(
        self,
        request_record: dict[str, Any],
        *,
        event_type: str,
        status: str,
        summary: str,
    ) -> str:
        step = self._initial_phase_step(request_record)
        normalized_summary = _truncate_text(" ".join(str(summary or "").split()), limit=160)
        digest = hashlib.sha1(normalized_summary.encode("utf-8")).hexdigest()[:10] if normalized_summary else "none"
        return ":".join(
            [
                str(request_record.get("request_id") or "").strip(),
                step or "initial",
                str(event_type or "").strip().lower() or "activity",
                str(status or "").strip().lower() or "unknown",
                digest,
            ]
        )

    def _planner_initial_phase_work_lines(
        self,
        *,
        step: str,
        sprint_state: dict[str, Any],
        proposals: dict[str, Any],
    ) -> list[str]:
        if step == INITIAL_PHASE_STEP_MILESTONE_REFINEMENT:
            return []
        if step == INITIAL_PHASE_STEP_TODO_FINALIZATION:
            todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
            if todos:
                return [self._format_todo_report_line(todo, include_artifacts=True) for todo in todos]
        backlog_items = self._collect_sprint_relevant_backlog_items(sprint_state) if sprint_state else []
        if backlog_items:
            return [self._format_backlog_report_line(item) for item in backlog_items]
        proposal_items = proposals.get("backlog_items")
        if isinstance(proposal_items, list):
            lines: list[str] = []
            for item in proposal_items:
                if isinstance(item, dict):
                    title = str(item.get("title") or item.get("backlog_id") or "Untitled").strip()
                    item_summary = str(item.get("summary") or "").strip()
                    line = f"- {title}"
                    if item_summary:
                        line += f" | {item_summary}"
                    lines.append(line)
                elif str(item).strip():
                    lines.append(f"- {str(item).strip()}")
            return lines
        return []

    def _planner_initial_phase_priority_lines(
        self,
        *,
        step: str,
        sprint_state: dict[str, Any],
        proposals: dict[str, Any],
    ) -> list[str]:
        if step not in {INITIAL_PHASE_STEP_BACKLOG_PRIORITIZATION, INITIAL_PHASE_STEP_TODO_FINALIZATION}:
            return []
        if step == INITIAL_PHASE_STEP_TODO_FINALIZATION:
            todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
            if todos:
                return [self._format_todo_report_line(todo, include_artifacts=True) for todo in todos]
        backlog_items = self._collect_sprint_relevant_backlog_items(sprint_state) if sprint_state else []
        if backlog_items:
            return [self._format_backlog_report_line(item) for item in backlog_items]
        return self._planner_initial_phase_work_lines(step=step, sprint_state=sprint_state, proposals=proposals)

    def _build_planner_initial_phase_activity_sections(
        self,
        request_record: dict[str, Any],
        *,
        step: str,
        step_position: int,
        event_type: str,
        status: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> list[ReportSection]:
        sprint_id = str(request_record.get("sprint_id") or dict(request_record.get("params") or {}).get("sprint_id") or "").strip()
        sprint_state = self._load_sprint_state(sprint_id) if sprint_id else {}
        proposals = dict(payload.get("proposals") or {}) if isinstance(payload, dict) and isinstance(payload.get("proposals"), dict) else {}
        semantic_context = self._build_role_result_semantic_context(payload or {}) if isinstance(payload, dict) else {}
        requested_milestone = (
            str(sprint_state.get("requested_milestone_title") or "").strip()
            or str(dict(request_record.get("params") or {}).get("milestone_title") or "").strip()
            or "없음"
        )
        revised_milestone = (
            str(proposals.get("revised_milestone_title") or "").strip()
            or str(sprint_state.get("milestone_title") or "").strip()
            or requested_milestone
        )
        step_title = self._initial_phase_step_title(step)
        overview_lines = [
            f"- 단계: {step_position}/{len(INITIAL_PHASE_STEPS)} {step_title}",
            f"- 결과: {str(semantic_context.get('what_summary') or summary or step_title).strip()}",
            f"- 다음: {self._planner_initial_phase_next_action(request_record, event_type, status)}",
        ]
        milestone_lines = [
            f"- requested: {requested_milestone}",
            f"- revised: {revised_milestone}",
        ]
        why_summary = str(semantic_context.get("why_summary") or "").strip()
        if why_summary:
            milestone_lines.append(f"- rationale: {why_summary}")
        evidence_lines = [
            f"- requirement_ref: {item}"
            for item in (
                [str(value).strip() for value in (sprint_state.get("kickoff_requirements") or []) if str(value).strip()]
            )[:5]
        ]
        doc_refs = self._planner_doc_targets(proposals)
        if doc_refs:
            evidence_lines.extend(f"- doc_ref: {item}" for item in doc_refs[:5])
        request_artifacts = [str(item).strip() for item in (request_record.get("artifacts") or []) if str(item).strip()]
        if request_artifacts:
            evidence_lines.extend(f"- evidence_ref: {item}" for item in request_artifacts[:5])
        sections = [
            self._report_section("핵심 결론", overview_lines),
            self._report_section("마일스톤", milestone_lines + evidence_lines[:6]),
        ]
        work_lines = self._planner_initial_phase_work_lines(step=step, sprint_state=sprint_state, proposals=proposals)
        if work_lines:
            sections.append(self._report_section("정의된 작업", work_lines))
        priority_lines = self._planner_initial_phase_priority_lines(step=step, sprint_state=sprint_state, proposals=proposals)
        if priority_lines:
            sections.append(self._report_section("우선순위/확정", priority_lines))
        if step == INITIAL_PHASE_STEP_ARTIFACT_SYNC:
            sync_lines = [f"- doc_sync: {item}" for item in (doc_refs[:8] or request_artifacts[:8])]
            if sync_lines:
                sections.append(self._report_section("문서/근거", sync_lines))
        elif step != INITIAL_PHASE_STEP_MILESTONE_REFINEMENT:
            doc_lines = [f"- doc_ref: {item}" for item in (doc_refs[:8] or request_artifacts[:8])]
            if doc_lines:
                sections.append(self._report_section("문서/근거", doc_lines))
        constraint_lines = [
            str(item).strip()
            for item in (semantic_context.get("constraint_points") or [])
            if str(item).strip()
        ]
        error_text = str((payload or {}).get("error") or "").strip()
        if error_text:
            constraint_lines.append(f"error: {error_text}")
        if constraint_lines:
            sections.append(self._report_section("차단/리스크", constraint_lines[:6]))
        return sections

    def _planner_initial_phase_next_action(self, request_record: dict[str, Any], event_type: str, status: str) -> str:
        step = self._initial_phase_step(request_record)
        normalized_status = str(status or "").strip().lower()
        if normalized_status in {"failed", "blocked"}:
            return "orchestrator 확인"
        if str(event_type or "").strip().lower() == "role_started":
            return f"{self._initial_phase_step_title(step)} 진행 중"
        next_step = self._next_initial_phase_step(step)
        if next_step:
            return f"다음 단계: {self._initial_phase_step_title(next_step)}"
        return "initial phase 완료 대기"

    def _build_planner_initial_phase_activity_report(
        self,
        request_record: dict[str, Any],
        *,
        event_type: str,
        status: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        step = self._initial_phase_step(request_record)
        if not step:
            return ""
        sprint_id = str(request_record.get("sprint_id") or dict(request_record.get("params") or {}).get("sprint_id") or "").strip()
        step_position = self._initial_phase_step_position(step)
        step_title = self._initial_phase_step_title(step)
        normalized_event = str(event_type or "").strip().lower()
        status_text = {
            "running": "진행중",
            "completed": "완료",
            "committed": "완료",
            "failed": "실패",
            "blocked": "중단",
        }.get(str(status or "").strip().lower(), str(status or "").strip() or "안내")
        request_label = (
            f"planner initial {step_position}/{len(INITIAL_PHASE_STEPS)} 시작"
            if normalized_event == "role_started"
            else f"planner initial {step_position}/{len(INITIAL_PHASE_STEPS)} 체크포인트"
        )
        semantic_context = self._build_role_result_semantic_context(payload or {}) if isinstance(payload, dict) else {}
        sections = self._build_planner_initial_phase_activity_sections(
            request_record,
            step=step,
            step_position=step_position,
            event_type=normalized_event,
            status=str(status or ""),
            summary=summary,
            payload=payload,
        )
        semantic_detail_summary = str(semantic_context.get("what_summary") or "").strip() or str(summary or step_title).strip()
        judgment = semantic_detail_summary or str(summary or step_title).strip()
        return build_progress_report(
            request=request_label,
            scope=f"{self._format_sprint_scope(sprint_id=sprint_id)} | initial {step_position}/{len(INITIAL_PHASE_STEPS)} {step_title}",
            status=status_text,
            list_summary="",
            detail_summary=semantic_detail_summary,
            process_summary="",
            log_summary="",
            end_reason="없음",
            judgment=judgment,
            next_action=self._planner_initial_phase_next_action(request_record, normalized_event, str(status or "")),
            artifacts=[str(item) for item in (request_record.get("artifacts") or []) if str(item).strip()],
            sections=sections,
        )

    async def _maybe_report_planner_initial_phase_activity(
        self,
        request_record: dict[str, Any],
        *,
        event_type: str,
        status: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if self.role != "planner":
            return
        if not self._is_initial_phase_planner_request(request_record):
            return
        channel_id = str(self.discord_config.report_channel_id or "").strip()
        if not channel_id:
            return
        report = self._build_planner_initial_phase_activity_report(
            request_record,
            event_type=event_type,
            status=status,
            summary=summary,
            payload=payload,
        )
        if not report:
            return
        report_key = self._planner_initial_phase_report_key(
            request_record,
            event_type=event_type,
            status=status,
            summary=summary,
        )
        if report_key in self._planner_initial_phase_report_keys(request_record):
            return
        try:
            await self._send_discord_content(
                content=report,
                send=lambda chunk: self.discord_client.send_channel_message(channel_id, chunk),
                target_description=f"planner-initial-phase:{channel_id}:{request_record.get('request_id') or ''}:{event_type}",
                swallow_exceptions=False,
                log_traceback=False,
            )
        except Exception as exc:
            LOGGER.warning(
                "Failed to send planner initial phase report for request %s step=%s event=%s: %s",
                request_record.get("request_id") or "unknown",
                self._initial_phase_step(request_record) or "unknown",
                event_type,
                exc,
            )
            return
        keys = self._planner_initial_phase_report_keys(request_record)
        keys.append(report_key)
        request_record["planner_initial_phase_report_keys"] = keys[-12:]
        self._persist_request_result(request_record)

    def _get_sourcer_report_client(self) -> DiscordClient | None:
        self._last_sourcer_report_client_label = ""
        self._last_sourcer_report_reason = ""
        self._last_sourcer_report_category = ""
        self._last_sourcer_report_recovery_action = ""
        if self._sourcer_report_config is not None:
            if self._sourcer_report_client is not None:
                self._last_sourcer_report_client_label = "internal_sourcer"
                return self._sourcer_report_client
            try:
                self._sourcer_report_client = DiscordClient(
                    token_env=self._sourcer_report_config.token_env,
                    expected_bot_id=self._sourcer_report_config.bot_id,
                    transcript_log_file=self.paths.discord_logs_dir / "sourcer.jsonl",
                    attachment_dir=self.paths.sprint_attachment_root(self._default_attachment_sprint_folder_name()),
                    client_name="sourcer",
                )
                self._last_sourcer_report_client_label = "internal_sourcer"
                return self._sourcer_report_client
            except Exception as exc:
                diagnostics = classify_discord_exception(
                    exc,
                    token_env_name=self._sourcer_report_config.token_env,
                    expected_bot_id=self._sourcer_report_config.bot_id,
                )
                self._last_sourcer_report_reason = f"internal reporter init failed: {diagnostics['summary']}"
                self._last_sourcer_report_category = diagnostics["category"]
                self._last_sourcer_report_recovery_action = diagnostics["recovery_action"]
                LOGGER.exception("Failed to initialize sourcer Discord reporter; falling back to orchestrator client")
        else:
            self._last_sourcer_report_reason = "internal sourcer reporter is not configured"
            self._last_sourcer_report_category = "reporter_not_configured"
            self._last_sourcer_report_recovery_action = "discord_agents_config.yaml에 internal_agents.sourcer 설정을 추가합니다."

        self._last_sourcer_report_client_label = "orchestrator_fallback"
        return self.discord_client

    def _build_sourcer_activity_report(
        self,
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

    def _report_sourcer_activity_sync(
        self,
        *,
        sourcing_activity: dict[str, Any],
        added: int,
        updated: int,
        candidates: list[dict[str, Any]],
    ) -> None:
        report_client = self._get_sourcer_report_client()
        if report_client is None:
            reason = self._last_sourcer_report_reason or "report client unavailable"
            LOGGER.warning("Skipping sourcer activity Discord report: %s", reason)
            self._record_sourcer_report_state(
                status="skipped",
                client_label="unavailable",
                reason=reason,
                category=self._last_sourcer_report_category or "report_client_unavailable",
                recovery_action=self._last_sourcer_report_recovery_action,
                error="",
                attempts=0,
                channel_id=self.discord_config.report_channel_id,
            )
            return
        client_label = self._last_sourcer_report_client_label or "unknown"
        client_reason = self._last_sourcer_report_reason
        client_category = self._last_sourcer_report_category
        client_recovery_action = self._last_sourcer_report_recovery_action
        report = self._build_sourcer_activity_report(
            sourcing_activity=sourcing_activity,
            added=added,
            updated=updated,
            candidates=candidates,
        )
        LOGGER.info(
            "Sending sourcer activity report via %s to report:%s (findings=%s, added=%s, updated=%s)",
            client_label,
            self.discord_config.report_channel_id,
            sourcing_activity.get("findings_count") or 0,
            added,
            updated,
        )

        async def send_report() -> None:
            await self._send_discord_content(
                content=report,
                send=lambda chunk: report_client.send_channel_message(self.discord_config.report_channel_id, chunk),
                target_description=f"sourcer-report:{self.discord_config.report_channel_id}",
                swallow_exceptions=False,
                log_traceback=False,
            )

        try:
            asyncio.run(send_report())
            self._record_sourcer_report_state(
                status="sent",
                client_label=client_label,
                reason=client_reason,
                category=client_category,
                recovery_action=client_recovery_action,
                error="",
                attempts=1,
                channel_id=self.discord_config.report_channel_id,
            )
        except Exception as exc:
            if isinstance(exc, RuntimeError) and "event loop" in str(exc).lower():
                reason = "event loop is already running"
                LOGGER.warning("Skipped sourcer activity Discord report because %s.", reason)
                self._record_sourcer_report_state(
                    status="skipped",
                    client_label=client_label,
                    reason=reason,
                    category="event_loop_running",
                    recovery_action="현재 실행 중인 이벤트 루프 바깥에서 sourcer report를 전송합니다.",
                    error="",
                    attempts=0,
                    channel_id=self.discord_config.report_channel_id,
                )
                return
            diagnostics = classify_discord_exception(
                getattr(exc, "last_error", None) or exc,
                token_env_name=(
                    self._sourcer_report_config.token_env if self._sourcer_report_config is not None else self.role_config.token_env
                ),
                expected_bot_id=(
                    self._sourcer_report_config.bot_id if self._sourcer_report_config is not None else self.role_config.bot_id
                ),
            )
            attempts = int(getattr(exc, "attempts", 1) or 1)
            self._log_sourcer_report_failure(
                client_label=client_label,
                channel_id=self.discord_config.report_channel_id,
                diagnostics=diagnostics,
                error=exc,
                attempts=attempts,
            )
            self._record_sourcer_report_state(
                status="failed",
                client_label=client_label,
                reason=client_reason or diagnostics["summary"] or "send failed",
                category=diagnostics["category"],
                recovery_action=diagnostics["recovery_action"],
                error=str(exc),
                attempts=attempts,
                channel_id=self.discord_config.report_channel_id,
            )

    async def _resume_uncommitted_sprint_todo(
        self,
        *,
        sprint_state: dict[str, Any],
        todo: dict[str, Any],
        request_record: dict[str, Any],
    ) -> dict[str, Any]:
        persisted_result = dict(request_record.get("result") or {})
        if not persisted_result:
            persisted_result = {
                "request_id": str(request_record.get("request_id") or todo.get("request_id") or ""),
                "role": str(request_record.get("current_role") or "orchestrator"),
                "status": "uncommitted",
                "summary": str(request_record.get("task_commit_summary") or todo.get("summary") or todo.get("title") or ""),
                "insights": [],
                "proposals": {},
                "artifacts": [str(item) for item in (request_record.get("artifacts") or todo.get("artifacts") or []) if str(item).strip()],
                "next_role": "",
                "error": str(request_record.get("version_control_error") or ""),
            }
        persisted_result.setdefault("request_id", str(request_record.get("request_id") or todo.get("request_id") or ""))
        persisted_result.setdefault("role", str(request_record.get("current_role") or "orchestrator"))
        persisted_result.setdefault("insights", [])
        persisted_result.setdefault("proposals", {})
        persisted_result.setdefault("artifacts", [str(item) for item in (request_record.get("artifacts") or todo.get("artifacts") or []) if str(item).strip()])
        persisted_result.setdefault("next_role", "")
        if str(persisted_result.get("status") or "").strip().lower() not in {"completed", "uncommitted"}:
            persisted_result["status"] = "uncommitted"
        return await self._enforce_task_commit_for_completed_todo(
            sprint_state=sprint_state,
            todo=todo,
            request_record=request_record,
            result=persisted_result,
        )

    async def _execute_sprint_todo(self, sprint_state: dict[str, Any], todo: dict[str, Any]) -> None:
        backlog_item = self._load_backlog_item(str(todo.get("backlog_id") or ""))
        request_record: dict[str, Any] = {}
        existing_request_id = str(todo.get("request_id") or "").strip()
        if existing_request_id:
            request_record = self._load_request(existing_request_id)
        existing_request_status = str(request_record.get("status") or "").strip().lower()
        recovering_uncommitted = existing_request_status == "uncommitted" or str(todo.get("status") or "").strip().lower() == "uncommitted"
        if not recovering_uncommitted:
            todo["status"] = "running"
            todo["started_at"] = str(todo.get("started_at") or utc_now_iso())
            self._save_sprint_state(sprint_state)
            self._append_sprint_event(
                str(sprint_state.get("sprint_id") or ""),
                event_type="todo_started",
                summary=str(todo.get("title") or ""),
                payload={"todo_id": todo.get("todo_id") or "", "backlog_id": todo.get("backlog_id") or ""},
            )
        if request_record and not self._is_terminal_internal_request_status(existing_request_status):
            todo["request_id"] = request_record["request_id"]
            self._save_sprint_state(sprint_state)
        elif not request_record:
            request_record = self._create_internal_request_record(sprint_state, todo, backlog_item)
            todo["request_id"] = request_record["request_id"]
            self._save_sprint_state(sprint_state)
        if recovering_uncommitted and request_record:
            result = await self._resume_uncommitted_sprint_todo(
                sprint_state=sprint_state,
                todo=todo,
                request_record=request_record,
            )
        else:
            result = await self._run_internal_request_chain(
                sprint_id=str(sprint_state.get("sprint_id") or ""),
                request_record=request_record,
                initial_role=str(todo.get("owner_role") or "planner"),
            )
            request_record = self._load_request(str(todo.get("request_id") or "")) or request_record
            result = await self._enforce_task_commit_for_completed_todo(
                sprint_state=sprint_state,
                todo=todo,
                request_record=request_record,
                result=result,
            )
        todo["artifacts"] = self._normalize_sprint_todo_artifacts(
            result.get("artifacts"),
            workflow_state=self._request_workflow_state(request_record),
        )
        todo["summary"] = str(result.get("summary") or "").strip()
        todo["updated_at"] = utc_now_iso()
        todo["version_control_status"] = str(result.get("version_control_status") or "").strip()
        todo["version_control_paths"] = [
            str(item).strip()
            for item in (result.get("version_control_paths") or [])
            if str(item).strip()
        ]
        todo["version_control_message"] = str(result.get("version_control_message") or "").strip()
        todo["version_control_error"] = str(result.get("version_control_error") or "").strip()
        todo["ended_at"] = utc_now_iso()
        status = str(result.get("status") or "").strip().lower()
        if status in {"completed", "committed"}:
            todo["status"] = status
            backlog_item["status"] = "done"
            self._clear_backlog_blockers(backlog_item)
            backlog_item["completed_in_sprint_id"] = str(sprint_state.get("sprint_id") or "")
            self._save_backlog_item(backlog_item)
        elif status == "uncommitted":
            todo["status"] = "uncommitted"
            backlog_item["status"] = "blocked"
            backlog_item["selected_in_sprint_id"] = ""
            backlog_item["completed_in_sprint_id"] = ""
            backlog_item["blocked_reason"] = str(result.get("summary") or result.get("error") or "").strip()
            backlog_item["blocked_by_role"] = "version_controller"
            backlog_item["recommended_next_step"] = "version_controller recovery 또는 수동 git 정리가 필요합니다."
            backlog_item["required_inputs"] = []
            self._save_backlog_item(backlog_item)
            todo["carry_over_backlog_id"] = str(backlog_item.get("backlog_id") or "")
        elif status == "blocked":
            todo["status"] = "blocked"
            backlog_item["status"] = "blocked"
            backlog_item["selected_in_sprint_id"] = ""
            backlog_item["completed_in_sprint_id"] = ""
            backlog_item["blocked_reason"] = str(result.get("summary") or result.get("error") or "").strip()
            backlog_item["blocked_by_role"] = str(result.get("role") or request_record.get("current_role") or "").strip()
            proposals = dict(result.get("proposals") or {})
            backlog_item["recommended_next_step"] = str(proposals.get("recommended_next_step") or "").strip()
            backlog_item["required_inputs"] = [
                str(value).strip()
                for value in (proposals.get("required_inputs") or [])
                if str(value).strip()
            ]
            self._save_backlog_item(backlog_item)
            todo["carry_over_backlog_id"] = str(backlog_item.get("backlog_id") or "")
        else:
            todo["status"] = "failed"
            carry_over = build_backlog_item(
                title=str(todo.get("title") or ""),
                summary=str(result.get("summary") or result.get("error") or "carry-over"),
                kind=self._classify_backlog_kind("", str(todo.get("title") or ""), str(result.get("summary") or "")),
                source="carry_over",
                scope=str(request_record.get("scope") or ""),
                acceptance_criteria=list(todo.get("acceptance_criteria") or []),
                milestone_title=str(todo.get("milestone_title") or ""),
                priority_rank=int(todo.get("priority_rank") or 0),
                origin={"sprint_id": sprint_state.get("sprint_id") or "", "todo_id": todo.get("todo_id") or ""},
            )
            carry_over["carry_over_of"] = str(backlog_item.get("backlog_id") or "")
            carry_over["fingerprint"] = self._build_backlog_fingerprint(
                title=str(carry_over.get("title") or ""),
                scope=str(carry_over.get("scope") or ""),
                kind=str(carry_over.get("kind") or ""),
            )
            backlog_item["status"] = "carried_over"
            self._save_backlog_item(backlog_item)
            self._save_backlog_item(carry_over)
            todo["carry_over_backlog_id"] = carry_over["backlog_id"]
        self._synchronize_sprint_todo_backlog_state(sprint_state)
        self._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="todo_completed",
            summary=str(todo.get("summary") or todo.get("title") or ""),
            payload={"todo_id": todo.get("todo_id") or "", "status": todo.get("status") or ""},
        )
        await self._send_sprint_report(
            title=f"TODO {todo.get('status') or ''}",
            body=(
                f"{todo.get('title') or ''}\n"
                f"request_id={todo.get('request_id') or ''}\n"
                f"summary={todo.get('summary') or ''}\n"
                f"version_control_status={todo.get('version_control_status') or 'not_needed'}\n"
                f"version_control_paths={', '.join(str(item).strip() for item in (todo.get('version_control_paths') or []) if str(item).strip()) or 'N/A'}"
            ),
            judgment=str(todo.get("summary") or todo.get("title") or "").strip(),
            commit_message=(
                _first_meaningful_text(
                    todo.get("version_control_message"),
                    request_record.get("task_commit_message"),
                    request_record.get("version_control_message"),
                )
                if str(todo.get("status") or "").strip().lower() == "committed"
                else ""
            ),
            related_artifacts=[
                str(item).strip()
                for item in (todo.get("artifacts") or [])
                if str(item).strip()
            ],
        )

    async def _enforce_task_commit_for_completed_todo(
        self,
        *,
        sprint_state: dict[str, Any],
        todo: dict[str, Any],
        request_record: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        result_status = str(result.get("status") or "").strip().lower()
        request_status = str(request_record.get("status") or "").strip().lower()
        recovering_uncommitted = request_status == "uncommitted" or result_status == "uncommitted"
        if result_status not in {"completed", "uncommitted"}:
            return result
        task_commit_summary = str(
            request_record.get("task_commit_summary")
            or result.get("task_commit_summary")
            or result.get("summary")
            or todo.get("title")
            or request_record.get("scope")
            or ""
        ).strip()
        request_record["task_commit_summary"] = task_commit_summary
        result["task_commit_summary"] = task_commit_summary
        inspection = self._inspect_task_version_control_state(request_record)
        inspected_paths = [
            str(item).strip()
            for item in (inspection.get("changed_paths") or [])
            if str(item).strip()
        ]
        if inspection.get("status") == "no_changes":
            request_record["status"] = "completed"
            request_record["version_control_status"] = "no_changes"
            request_record["version_control_sha"] = ""
            request_record["version_control_paths"] = []
            request_record["version_control_message"] = ""
            request_record["version_control_error"] = ""
            request_record["task_commit_status"] = "no_changes"
            request_record["task_commit_sha"] = ""
            request_record["task_commit_paths"] = []
            request_record["task_commit_message"] = ""
            result["status"] = "completed"
            result["summary"] = task_commit_summary or str(result.get("summary") or "").strip()
            result["error"] = ""
            result["version_control_status"] = "no_changes"
            result["version_control_sha"] = ""
            result["version_control_paths"] = []
            result["version_control_message"] = ""
            result["version_control_error"] = ""
            result["task_commit_status"] = "no_changes"
            result["task_commit_sha"] = ""
            result["task_commit_paths"] = []
            result["task_commit_message"] = ""
            tracked_artifacts = self._collect_artifact_candidates(
                request_record.get("artifacts"),
                result.get("artifacts"),
                todo.get("artifacts"),
                result.get("version_control_paths"),
                result.get("task_commit_paths"),
            )
            result["artifacts"] = tracked_artifacts
            request_record["artifacts"] = tracked_artifacts
            request_record["result"] = result
            self._save_request(request_record)
            return result
        if inspection.get("status") == "pending_changes" or recovering_uncommitted:
            pending_paths = inspected_paths or [
                str(item).strip()
                for item in (
                    request_record.get("version_control_paths")
                    or request_record.get("task_commit_paths")
                    or result.get("version_control_paths")
                    or result.get("task_commit_paths")
                    or []
                )
                if str(item).strip()
            ]
            request_record["status"] = "uncommitted"
            request_record["current_role"] = "orchestrator"
            request_record["next_role"] = ""
            request_record["version_control_status"] = "requested"
            request_record["version_control_sha"] = ""
            request_record["version_control_paths"] = pending_paths
            request_record["version_control_message"] = ""
            request_record["version_control_error"] = ""
            request_record["task_commit_status"] = "requested"
            request_record["task_commit_sha"] = ""
            request_record["task_commit_paths"] = pending_paths
            request_record["task_commit_message"] = ""
            result["status"] = "uncommitted"
            result["version_control_status"] = "requested"
            result["version_control_sha"] = ""
            result["version_control_paths"] = pending_paths
            result["version_control_message"] = ""
            result["version_control_error"] = ""
            result["task_commit_status"] = "requested"
            result["task_commit_sha"] = ""
            result["task_commit_paths"] = pending_paths
            result["task_commit_message"] = ""
            tracked_artifacts = self._collect_artifact_candidates(
                request_record.get("artifacts"),
                result.get("artifacts"),
                todo.get("artifacts"),
                inspected_paths,
                pending_paths,
            )
            result["artifacts"] = tracked_artifacts
            request_record["artifacts"] = tracked_artifacts
            request_record["result"] = result
            todo["status"] = "uncommitted"
            todo["version_control_status"] = "requested"
            todo["version_control_paths"] = list(pending_paths)
            todo["version_control_message"] = ""
            todo["version_control_error"] = ""
            self._save_sprint_state(sprint_state)
            self._save_request(request_record)
        version_control_result = await self._run_task_version_controller(
            sprint_state=sprint_state,
            todo=todo,
            request_record=request_record,
            result=result,
        )
        commit_status = str(version_control_result.get("commit_status") or version_control_result.get("status") or "").strip()
        commit_sha = str(version_control_result.get("commit_sha") or "").strip()
        commit_paths = [
            str(item).strip()
            for item in (
                version_control_result.get("commit_paths")
                or version_control_result.get("changed_paths")
                or []
            )
            if str(item).strip()
        ]
        commit_message = str(version_control_result.get("commit_message") or "").strip()
        version_control_error = str(version_control_result.get("error") or "").strip()
        version_control_summary = str(version_control_result.get("summary") or "").strip()
        request_record["version_control_status"] = commit_status
        request_record["version_control_sha"] = commit_sha
        request_record["version_control_paths"] = commit_paths
        request_record["version_control_message"] = commit_message
        request_record["version_control_error"] = version_control_error
        request_record["task_commit_status"] = commit_status
        request_record["task_commit_sha"] = commit_sha
        request_record["task_commit_paths"] = commit_paths
        request_record["task_commit_message"] = commit_message
        result["version_control_status"] = commit_status
        result["version_control_sha"] = commit_sha
        result["version_control_paths"] = commit_paths
        result["version_control_message"] = commit_message
        result["version_control_error"] = version_control_error
        result["task_commit_status"] = commit_status
        result["task_commit_sha"] = commit_sha
        result["task_commit_paths"] = commit_paths
        result["task_commit_message"] = commit_message
        if commit_status in {"failed", "no_repo"} or str(version_control_result.get("status") or "").strip().lower() in {"blocked", "failed"}:
            failure_summary = (
                "Task 완료 직전 version_controller 커밋 단계에 실패했습니다. "
                f"{version_control_summary or version_control_error}"
            ).strip()
            tracked_artifacts = self._collect_artifact_candidates(
                request_record.get("artifacts"),
                result.get("artifacts"),
                todo.get("artifacts"),
                commit_paths,
                request_record.get("version_control_paths"),
                request_record.get("task_commit_paths"),
            )
            result["artifacts"] = tracked_artifacts
            request_record["artifacts"] = tracked_artifacts
            request_record["status"] = "uncommitted" if inspected_paths or commit_paths or recovering_uncommitted else "blocked"
            request_record["result"] = {
                **result,
                "status": "uncommitted" if inspected_paths or commit_paths or recovering_uncommitted else "blocked",
                "summary": failure_summary,
                "error": version_control_error or version_control_summary,
            }
            self._save_request(request_record)
            return dict(request_record["result"])
        final_status = "committed" if commit_status == "committed" else "completed"
        request_record["status"] = final_status
        result["status"] = final_status
        result["summary"] = task_commit_summary or str(result.get("summary") or "").strip()
        result["error"] = ""
        tracked_artifacts = self._collect_artifact_candidates(
            request_record.get("artifacts"),
            result.get("artifacts"),
            todo.get("artifacts"),
            commit_paths,
            request_record.get("version_control_paths"),
            request_record.get("task_commit_paths"),
            inspected_paths,
        )
        result["artifacts"] = tracked_artifacts
        request_record["artifacts"] = tracked_artifacts
        request_record["result"] = result
        self._save_request(request_record)
        return result

    def _create_internal_request_record(
        self,
        sprint_state: dict[str, Any],
        todo: dict[str, Any],
        backlog_item: dict[str, Any],
    ) -> dict[str, Any]:
        request_id = new_request_id()
        record = {
            "request_id": request_id,
            "status": "queued",
            "intent": "route",
            "urgency": "normal",
            "scope": str(backlog_item.get("scope") or backlog_item.get("title") or "").strip(),
            "body": str(backlog_item.get("summary") or backlog_item.get("scope") or "").strip(),
            "artifacts": [],
            "params": {
                "_teams_kind": "sprint_internal",
                "sprint_id": sprint_state.get("sprint_id") or "",
                "backlog_id": todo.get("backlog_id") or "",
                "todo_id": todo.get("todo_id") or "",
                "workflow": self._initial_workflow_state_for_internal_request(),
            },
            "current_role": "orchestrator",
            "next_role": str(todo.get("owner_role") or "planner"),
            "owner_role": "orchestrator",
            "sprint_id": str(sprint_state.get("sprint_id") or ""),
            "backlog_id": str(todo.get("backlog_id") or ""),
            "todo_id": str(todo.get("todo_id") or ""),
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "fingerprint": build_request_fingerprint(
                author_id="sprint-runner",
                channel_id=str(sprint_state.get("sprint_id") or ""),
                intent="route",
                scope=str(backlog_item.get("scope") or backlog_item.get("title") or ""),
            ),
            "reply_route": {},
            "events": [],
            "result": {},
            "git_baseline": capture_git_baseline(self.paths.project_workspace_root),
            "version_control_status": "",
            "version_control_sha": "",
            "version_control_paths": [],
            "version_control_message": "",
            "version_control_error": "",
            "task_commit_status": "",
            "task_commit_sha": "",
            "task_commit_paths": [],
            "task_commit_message": "",
            "visited_roles": [],
        }
        append_request_event(
            record,
            event_type="created",
            actor="sprint_runner",
            summary="스프린트 내부 요청을 생성했습니다.",
        )
        self._save_request(record)
        self._append_role_history(
            "orchestrator",
            record,
            event_type="created",
            summary="스프린트 내부 요청을 생성했습니다.",
        )
        return record

    async def _run_internal_request_chain(
        self,
        *,
        sprint_id: str,
        request_record: dict[str, Any],
        initial_role: str,
    ) -> dict[str, Any]:
        selection = self._build_governed_routing_selection(
            request_record,
            {},
            current_role="orchestrator",
            requested_role=str(initial_role or "").strip(),
            selection_source="sprint_initial",
        )
        next_role = str(selection.get("selected_role") or "").strip() or "planner"
        current_status = str(request_record.get("status") or "").strip().lower()
        if current_status in {"queued", ""}:
            request_record["status"] = "delegated"
            request_record["current_role"] = next_role
            request_record["next_role"] = next_role
            request_record["routing_context"] = self._build_routing_context(
                next_role,
                reason=f"Selected {next_role} as the current best role for this sprint step.",
                requested_role=str(selection.get("requested_role") or ""),
                selection_source="sprint_initial",
                matched_signals=[
                    str(item).strip()
                    for item in (selection.get("matched_signals") or [])
                    if str(item).strip()
                ],
                override_reason=str(selection.get("override_reason") or ""),
                matched_strongest_domains=[
                    str(item).strip()
                    for item in (selection.get("matched_strongest_domains") or [])
                    if str(item).strip()
                ],
                matched_preferred_skills=[
                    str(item).strip()
                    for item in (selection.get("matched_preferred_skills") or [])
                    if str(item).strip()
                ],
                matched_behavior_traits=[
                    str(item).strip()
                    for item in (selection.get("matched_behavior_traits") or [])
                    if str(item).strip()
                ],
                policy_source=str(selection.get("policy_source") or ""),
                routing_phase=str(selection.get("routing_phase") or ""),
                request_state_class=str(selection.get("request_state_class") or ""),
                score_total=int(selection.get("score_total") or 0),
                score_breakdown=dict(selection.get("score_breakdown") or {}),
                candidate_summary=list(selection.get("candidate_summary") or []),
            )
            append_request_event(
                request_record,
                event_type="delegated",
                actor="orchestrator",
                summary=f"{next_role} 역할로 위임했습니다.",
                payload={"routing_context": dict(request_record.get("routing_context") or {})},
            )
            self._save_request(request_record)
            self._append_role_history(
                "orchestrator",
                request_record,
                event_type="delegated",
                summary=f"{next_role} 역할로 위임했습니다.",
            )
            self._record_internal_sprint_activity(
                request_record,
                event_type="role_delegated",
                role="orchestrator",
                status=str(request_record.get("status") or ""),
                summary=f"{next_role} 역할로 위임했습니다.",
                payload=self._build_internal_sprint_delegation_payload(request_record, next_role),
            )
            await self._delegate_request(request_record, next_role)
        return await self._wait_for_internal_request_result(str(request_record.get("request_id") or ""))

    @staticmethod
    def _sprint_status_label(status: str) -> str:
        normalized = str(status or "").strip().lower()
        return {
            "completed": "완료",
            "failed": "실패",
            "blocked": "중단",
            "running": "진행중",
            "planning": "계획중",
            "closeout": "마감중",
        }.get(normalized, str(status or "").strip() or "N/A")

    @staticmethod
    def _sprint_role_display_name(role: str) -> str:
        normalized = str(role or "").strip().lower()
        return SPRINT_ROLE_DISPLAY_NAMES.get(normalized, normalized or "기타")

    @staticmethod
    def _limit_sprint_report_lines(lines: Iterable[str], *, limit: int) -> list[str]:
        normalized = [str(item).strip() for item in lines if str(item).strip()]
        if len(normalized) <= limit:
            return normalized
        return normalized[:limit] + [f"- ... 외 {len(normalized) - limit}건"]

    @staticmethod
    def _format_sprint_report_text(value: Any, *, full_detail: bool = False, limit: int = 240) -> str:
        normalized = _collapse_whitespace(value)
        if full_detail:
            return normalized
        return _truncate_text(normalized, limit=limit)

    def _format_sprint_duration(self, sprint_state: dict[str, Any]) -> str:
        started_at = self._parse_datetime(str(sprint_state.get("started_at") or ""))
        if started_at is None:
            return "N/A"
        ended_at = self._parse_datetime(str(sprint_state.get("ended_at") or "")) or utc_now()
        elapsed_seconds = max(0, int((ended_at - started_at).total_seconds()))
        days, remainder = divmod(elapsed_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)
        parts: list[str] = []
        if days:
            parts.append(f"{days}일")
        if hours:
            parts.append(f"{hours}시간")
        if minutes:
            parts.append(f"{minutes}분")
        if seconds and not parts:
            parts.append(f"{seconds}초")
        return " ".join(parts) if parts else "0초"

    def _load_sprint_event_entries(self, sprint_state: dict[str, Any]) -> list[dict[str, Any]]:
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        if not sprint_id:
            return []
        path = self.paths.sprint_events_file(sprint_id)
        try:
            raw_lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        events: list[dict[str, Any]] = []
        for raw_line in raw_lines:
            line = str(raw_line or "").strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
        return events

    def _preview_sprint_artifact_path(
        self,
        sprint_state: dict[str, Any],
        value: str,
        *,
        full_detail: bool = False,
    ) -> str:
        normalized = str(value or "").strip().replace("\\", "/")
        if not normalized:
            return ""
        if normalized.startswith("./"):
            normalized = normalized[2:]
        sprint_folder_name = str(sprint_state.get("sprint_folder_name") or "").strip()
        if sprint_folder_name:
            sprint_prefix = f"shared_workspace/sprints/{sprint_folder_name}/"
            if sprint_prefix in normalized:
                normalized = normalized.split(sprint_prefix, 1)[1].lstrip("/")
        if normalized.startswith("workspace/teams_generated/"):
            normalized = normalized.removeprefix("workspace/teams_generated/").lstrip("/")
        elif normalized.startswith("workspace/"):
            normalized = normalized.removeprefix("workspace/").lstrip("/")
        workspace_root = str(self.paths.workspace_root).replace("\\", "/")
        if normalized.startswith(workspace_root):
            try:
                normalized = Path(normalized).resolve().relative_to(self.paths.workspace_root).as_posix()
            except Exception:
                normalized = normalized if full_detail else (Path(normalized).name or normalized)
        if full_detail:
            return normalized
        if len(normalized) <= 72:
            return normalized
        return Path(normalized).name or _truncate_text(normalized, limit=72)

    def _sprint_report_draft(self, sprint_state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        if snapshot is not None:
            draft = _normalize_sprint_report_draft(snapshot.get("planner_report_draft"))
            if draft:
                return draft
        return _normalize_sprint_report_draft(sprint_state.get("planner_report_draft"))

    def _planner_closeout_request_id(self, sprint_state: dict[str, Any]) -> str:
        sprint_id = slugify_sprint_value(str(sprint_state.get("sprint_id") or "sprint"))
        return f"planner-closeout-report-{sprint_id}"

    def _relative_workspace_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.paths.workspace_root).as_posix())
        except ValueError:
            return str(path.as_posix())

    def _write_planner_closeout_context_file(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> str:
        request_id = self._planner_closeout_request_id(sprint_state)
        source_dir = self.paths.role_sources_dir("planner")
        source_dir.mkdir(parents=True, exist_ok=True)
        payload_path = source_dir / f"{request_id}.closeout_report.json"
        request_ids = [
            str(todo.get("request_id") or "").strip()
            for todo in (snapshot.get("todos") or [])
            if str(todo.get("request_id") or "").strip()
        ]
        payload = {
            "sprint_id": str(sprint_state.get("sprint_id") or ""),
            "sprint_name": str(sprint_state.get("sprint_name") or sprint_state.get("sprint_display_name") or ""),
            "milestone_title": str(sprint_state.get("milestone_title") or ""),
            "status": str(sprint_state.get("status") or ""),
            "closeout_result": dict(closeout_result or {}),
            "todo_summary": str(snapshot.get("todo_summary") or ""),
            "commit_count": int(snapshot.get("commit_count") or 0),
            "commit_sha": str(snapshot.get("commit_sha") or ""),
            "linked_artifacts": list(snapshot.get("linked_artifacts") or []),
            "request_files": [
                self._relative_workspace_path(self.paths.request_file(request_id))
                for request_id in request_ids
            ],
        }
        write_json(payload_path, payload)
        return self._relative_workspace_path(payload_path)

    def _planner_closeout_artifacts(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        context_file: str,
    ) -> list[str]:
        artifacts: list[str] = []
        if context_file:
            artifacts.append(context_file)
        sprint_folder_name = str(
            sprint_state.get("sprint_folder_name") or build_sprint_artifact_folder_name(str(sprint_state.get("sprint_id") or ""))
        ).strip()
        for filename in ("kickoff.md", "milestone.md", "spec.md", "iteration_log.md", "report.md"):
            path = self.paths.sprint_artifact_file(sprint_folder_name, filename)
            if path.exists():
                artifacts.append(self._relative_workspace_path(path))
        for todo in snapshot.get("todos") or []:
            request_id = str(todo.get("request_id") or "").strip()
            if request_id:
                artifacts.append(self._relative_workspace_path(self.paths.request_file(request_id)))
        return _dedupe_preserving_order(artifacts)

    async def _draft_sprint_report_via_planner(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> dict[str, Any]:
        snapshot = self._collect_sprint_report_snapshot(sprint_state, closeout_result)
        context_file = self._write_planner_closeout_context_file(sprint_state, closeout_result, snapshot)
        artifacts = self._planner_closeout_artifacts(sprint_state, snapshot, context_file=context_file)
        request_id = self._planner_closeout_request_id(sprint_state)
        request_context = {
            "request_id": request_id,
            "status": "queued",
            "intent": "plan",
            "urgency": "normal",
            "scope": f"{str(sprint_state.get('sprint_id') or '').strip() or 'sprint'} closeout report",
            "body": (
                "Persisted sprint evidence를 읽고 canonical sprint final report용 의미 중심 요약을 작성합니다."
            ),
            "artifacts": list(artifacts),
            "params": {
                "_teams_kind": "sprint_closeout_report",
                "sprint_id": str(sprint_state.get("sprint_id") or ""),
                "closeout_status": str(closeout_result.get("status") or ""),
                "closeout_message": str(closeout_result.get("message") or ""),
                "milestone_title": str(sprint_state.get("milestone_title") or ""),
            },
            "current_role": "planner",
            "next_role": "",
            "owner_role": "orchestrator",
            "sprint_id": str(sprint_state.get("sprint_id") or ""),
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "fingerprint": request_id,
            "reply_route": {},
            "events": [],
            "result": {},
            "visited_roles": ["orchestrator"],
        }
        envelope = MessageEnvelope(
            request_id=request_id,
            sender="orchestrator",
            target="planner",
            intent="plan",
            urgency="normal",
            scope=str(request_context.get("scope") or ""),
            artifacts=list(artifacts),
            params={"_teams_kind": "sprint_closeout_report"},
            body=(
                "Persisted sprint evidence를 읽고 `proposals.sprint_report`로 canonical closeout draft를 작성하세요. "
                "제목/무엇이 달라졌나/의미는 기능 변화 또는 workflow contract 변화 중심으로 작성하고, "
                "prompt·문서·라우팅·회귀 테스트 반영 같은 meta activity 문구는 그대로 반복하지 마세요."
            ),
        )
        try:
            runtime = self._runtime_for_role("planner", self.runtime_config.sprint_id)
            result = await asyncio.to_thread(runtime.run_task, envelope, request_context)
        except Exception:
            LOGGER.exception(
                "Planner closeout report drafting failed for sprint %s",
                sprint_state.get("sprint_id") or "unknown",
            )
            return {}
        normalized = normalize_role_payload(result)
        draft = _normalize_sprint_report_draft(
            dict(normalized.get("proposals") or {}).get("sprint_report")
        )
        if not draft:
            LOGGER.warning(
                "Planner closeout report draft missing or invalid for sprint %s",
                sprint_state.get("sprint_id") or "unknown",
            )
        return draft

    async def _prepare_sprint_report_body(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> str:
        sprint_state["planner_report_draft"] = await self._draft_sprint_report_via_planner(sprint_state, closeout_result)
        report_body = self._build_sprint_report_body(sprint_state, closeout_result)
        sprint_state["report_body"] = report_body
        return report_body

    @staticmethod
    def _extract_sprint_change_subject(*values: Any) -> str:
        normalized_values = [_collapse_whitespace(value) for value in values if _collapse_whitespace(value)]
        subject_hints = (
            "김단타",
            "딜리게이터",
            "손석희",
            "오케스트레이터",
            "플래너",
            "디자이너",
            "아키텍트",
            "개발자",
            "QA",
            "파서",
            "소서",
            "버전 컨트롤러",
            "orchestrator",
            "planner",
            "designer",
            "architect",
            "developer",
            "qa",
            "version_controller",
        )
        for value in normalized_values:
            for hint in subject_hints:
                if hint and hint in value:
                    return hint
        return ""

    def _collect_sprint_delivered_changes(
        self,
        sprint_state: dict[str, Any],
        todos: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        milestone = _collapse_whitespace(
            sprint_state.get("milestone_title") or sprint_state.get("requested_milestone_title") or ""
        )
        changes: list[dict[str, Any]] = []
        for todo in todos:
            status = str(todo.get("status") or "").strip().lower()
            if status not in {"committed", "completed"}:
                continue
            request_record = self._load_request(str(todo.get("request_id") or ""))
            result = dict(request_record.get("result") or {}) if isinstance(request_record, dict) else {}
            semantic_context = self._latest_sprint_change_semantic_context(request_record)
            insights = _normalize_insights(result)
            title = _first_meaningful_text(todo.get("title"), request_record.get("scope"), "Untitled change")
            scope = _first_meaningful_text(request_record.get("scope"), todo.get("summary"), title)
            what_changed = self._resolve_sprint_change_behavior_text(
                semantic_context,
                request_record.get("task_commit_summary"),
                result.get("summary"),
                todo.get("summary"),
                title,
            )
            functional_title = self._resolve_sprint_change_title(
                title,
                scope,
                semantic_context,
                what_changed,
            )
            artifacts = [
                preview
                for preview in (
                    self._preview_sprint_artifact_path(sprint_state, artifact, full_detail=True)
                    for artifact in self._collect_artifact_candidates(
                        todo.get("artifacts"),
                        request_record.get("version_control_paths"),
                        request_record.get("task_commit_paths"),
                        result.get("artifacts"),
                    )
                )
                if preview
            ]
            subject = self._extract_sprint_change_subject(
                functional_title,
                what_changed,
                scope,
                semantic_context.get("what_summary"),
                semantic_context.get("why_summary"),
                *insights,
            )
            if milestone and scope:
                why = f"`{milestone}` 마일스톤을 위해 `{scope}` 작업을 반영했습니다."
            elif milestone:
                why = f"`{milestone}` 마일스톤을 달성하기 위한 핵심 변경입니다."
            elif scope:
                why = f"`{scope}` 요구를 실제 동작 변화로 연결했습니다."
            else:
                why = "이번 스프린트 목표를 실제 동작 변화로 연결했습니다."
            changes.append(
                {
                    "title": functional_title,
                    "subject": subject,
                    "scope": scope,
                    "what_changed": what_changed,
                    "insights": insights,
                    "artifacts": artifacts,
                    "why": why,
                    "semantic_context": semantic_context,
                }
            )
        return changes

    def _latest_sprint_change_semantic_context(self, request_record: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(request_record, dict):
            return {}
        for report in reversed(self._collect_role_report_events(request_record)):
            payload = dict(report.get("payload") or {})
            if not payload:
                continue
            semantic_context = self._build_role_result_semantic_context(payload)
            if any(
                (
                    semantic_context.get("what_summary"),
                    semantic_context.get("what_details"),
                    semantic_context.get("how_summary"),
                    semantic_context.get("why_summary"),
                )
            ):
                return semantic_context
        result = dict(request_record.get("result") or {})
        return self._build_role_result_semantic_context(result) if result else {}

    def _resolve_sprint_change_behavior_text(self, semantic_context: dict[str, Any], *fallbacks: Any) -> str:
        semantic_summary = _collapse_whitespace(semantic_context.get("what_summary") or "")
        semantic_details = [
            _collapse_whitespace(item)
            for item in (semantic_context.get("what_details") or [])
            if _collapse_whitespace(item)
        ]
        if semantic_summary and not _looks_meta_change_text(semantic_summary):
            return semantic_summary
        for detail in semantic_details:
            if not _looks_meta_change_text(detail):
                return detail
        return _first_meaningful_text(*fallbacks)

    def _resolve_sprint_change_title(
        self,
        title: str,
        scope: str,
        semantic_context: dict[str, Any],
        what_changed: str,
    ) -> str:
        normalized_title = _collapse_whitespace(title)
        normalized_scope = _collapse_whitespace(scope)
        semantic_summary = _collapse_whitespace(semantic_context.get("what_summary") or "")
        semantic_details = [
            _collapse_whitespace(item)
            for item in (semantic_context.get("what_details") or [])
            if _collapse_whitespace(item)
        ]
        if normalized_title and not _looks_meta_change_text(normalized_title):
            return normalized_title
        if semantic_summary and not _looks_meta_change_text(semantic_summary):
            return semantic_summary
        for detail in semantic_details:
            if not _looks_meta_change_text(detail):
                return detail
        if normalized_scope and not _looks_meta_change_text(normalized_scope):
            return normalized_scope
        return normalized_title or normalized_scope or _collapse_whitespace(what_changed) or "Untitled change"

    def _build_sprint_change_behavior_summary(self, change: dict[str, Any]) -> str:
        subject = str(change.get("subject") or "").strip()
        what_changed = _collapse_whitespace(change.get("what_changed") or "")
        if not what_changed:
            return "실제 동작 변화 설명을 별도로 남기지 않았습니다."
        if subject and subject not in what_changed and not what_changed.startswith("이제 "):
            return f"이제 {subject}는 {what_changed}"
        return what_changed

    def _build_sprint_change_meaning(self, change: dict[str, Any]) -> str:
        subject = str(change.get("subject") or "").strip()
        what_changed = _collapse_whitespace(change.get("what_changed") or "")
        semantic_context = dict(change.get("semantic_context") or {})
        semantic_why = _collapse_whitespace(semantic_context.get("why_summary") or "")
        if semantic_why:
            return semantic_why
        insight_text = " ".join(str(item).strip() for item in (change.get("insights") or []) if str(item).strip())
        combined = " ".join(part for part in [what_changed, insight_text] if part).lower()
        subject_noun = subject or "이 기능"
        subject_actor = f"{subject_noun}가"
        subject_output = f"{subject_noun}의"
        if any(keyword in combined for keyword in ("workflow", "routing", "planner", "designer", "architect", "contract", "advisory", "finalization")):
            return f"이번 스프린트 기준으로는 이제 {subject_actor} planning/workflow 계약을 더 엄격하게 따른다는 의미입니다."
        if any(keyword in combined for keyword in ("리포트", "요약", "메시지", "문서", "보고")):
            return f"사용자 입장에서는 {subject_output} 출력과 설명이 더 읽기 쉽고 바로 이해되는 방향으로 바뀐다는 의미입니다."
        if any(keyword in combined for keyword in ("기준", "조건", "규칙", "policy", "정책", "threshold", "임계치", "판단")):
            return f"사용자 입장에서는 이제 {subject_actor} 언제 어떤 판단을 내리는지 기준이 더 분명해진다는 의미입니다."
        if any(keyword in combined for keyword in ("사이클", "주기", "cadence", "interval", "타이밍", "실시간")):
            return f"사용자 입장에서는 이제 {subject_actor} 반응 타이밍과 판단 주기를 다르게 가져간다는 의미입니다."
        if any(keyword in combined for keyword in ("동기화", "정리", "재구성", "구성", "전환", "개선", "정돈")):
            return f"사용자 입장에서는 이제 {subject_actor} 동작과 결과를 더 일관되게 보여 준다는 의미입니다."
        if what_changed:
            return f"사용자 입장에서는 이제 {subject_actor} `{what_changed}` 방향으로 동작한다고 이해하면 됩니다."
        return f"사용자 입장에서는 이제 {subject_actor} 동작과 결과 해석 방식이 달라졌다고 보면 됩니다."

    def _build_sprint_change_how_lines(
        self,
        change: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        lines: list[str] = ["- 어떻게:"]
        insights = [str(item).strip() for item in (change.get("insights") or []) if str(item).strip()]
        artifacts = [str(item).strip() for item in (change.get("artifacts") or []) if str(item).strip()]
        scope = _collapse_whitespace(change.get("scope") or "")
        if insights:
            lines.append(
                "  - 핵심 로직: "
                + self._format_sprint_report_text(
                    " / ".join(insights),
                    full_detail=full_detail,
                    limit=160,
                )
            )
        if artifacts:
            lines.append(
                "  - 구현 근거 아티팩트: "
                + self._format_sprint_report_text(
                    ", ".join(artifacts),
                    full_detail=full_detail,
                    limit=180,
                )
            )
        if scope:
            lines.append(
                "  - 작업 범위: "
                + self._format_sprint_report_text(
                    scope,
                    full_detail=full_detail,
                    limit=160,
                )
            )
        if len(lines) == 1:
            lines.append("- 요약된 구현 근거 없음")
        return lines

    def _build_sprint_change_summary_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        draft = self._sprint_report_draft(sprint_state, snapshot)
        draft_changes = list(draft.get("changes") or [])
        if draft_changes:
            lines: list[str] = []
            for index, change in enumerate(draft_changes):
                title = self._format_sprint_report_text(
                    change.get("title") or "Untitled change",
                    full_detail=full_detail,
                    limit=96,
                )
                lines.extend(
                    [
                        f"### {title}",
                        "- 왜: "
                        + self._format_sprint_report_text(
                            change.get("why") or "이번 스프린트 목표를 실제 변화로 연결했습니다.",
                            full_detail=full_detail,
                            limit=120,
                        ),
                        "- 무엇이 달라졌나: "
                        + self._format_sprint_report_text(
                            change.get("what_changed") or "실제 동작 변화 설명을 별도로 남기지 않았습니다.",
                            full_detail=full_detail,
                            limit=140,
                        ),
                        "- 의미: "
                        + self._format_sprint_report_text(
                            change.get("meaning") or "이번 스프린트 결과의 의미를 별도로 남기지 않았습니다.",
                            full_detail=full_detail,
                            limit=140,
                        ),
                        "- 어떻게: "
                        + self._format_sprint_report_text(
                            change.get("how") or "관련 요청, 문서, 산출물을 함께 검토해 반영했습니다.",
                            full_detail=full_detail,
                            limit=160,
                        ),
                    ]
                )
                artifacts = [str(item).strip() for item in (change.get("artifacts") or []) if str(item).strip()]
                if artifacts:
                    lines.append("- 관련 아티팩트: " + ", ".join(artifacts))
                if index < len(draft_changes) - 1:
                    lines.append("")
            return lines
        changes = list(snapshot.get("delivered_changes") or [])
        if not changes:
            milestone = self._format_sprint_report_text(
                sprint_state.get("milestone_title") or sprint_state.get("requested_milestone_title") or "이번 스프린트",
                full_detail=full_detail,
                limit=80,
            )
            closeout_message = self._format_sprint_report_text(
                snapshot.get("closeout_message") or "closeout 상태를 정리했습니다.",
                full_detail=full_detail,
                limit=120,
            )
            return [
                f"- 왜: `{milestone}` 기준으로 이번 스프린트 결과를 마감했습니다.",
                "- 무엇이 달라졌나: 실제로 완료/커밋된 delivered change는 없었습니다.",
                "- 의미: 사용자 입장에서는 이번 스프린트가 새로운 동작 변경보다 상태 정리와 closeout 확인 중심으로 끝났다는 의미입니다.",
                "- 어떻게:",
                f"  - closeout 정리: {closeout_message}",
            ]

        lines: list[str] = []
        for index, change in enumerate(changes):
            title = self._format_sprint_report_text(
                change.get("title") or "Untitled change",
                full_detail=full_detail,
                limit=96,
            )
            lines.extend(
                [
                    f"### {title}",
                    f"- 왜: {self._format_sprint_report_text(change.get('why') or '', full_detail=full_detail, limit=120)}",
                    (
                        "- 무엇이 달라졌나: "
                        + self._format_sprint_report_text(
                            self._build_sprint_change_behavior_summary(change),
                            full_detail=full_detail,
                            limit=140,
                        )
                    ),
                    (
                        "- 의미: "
                        + self._format_sprint_report_text(
                            self._build_sprint_change_meaning(change),
                            full_detail=full_detail,
                            limit=140,
                        )
                    ),
                ]
            )
            lines.extend(self._build_sprint_change_how_lines(change, full_detail=full_detail))
            artifacts = [str(item).strip() for item in (change.get("artifacts") or []) if str(item).strip()]
            if artifacts:
                lines.append("  - 참고 아티팩트: " + ", ".join(artifacts))
            if index < len(changes) - 1:
                lines.append("")
        return lines

    def _build_machine_sprint_report_lines(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> list[str]:
        commit_shas = [
            str(item).strip()
            for item in (closeout_result.get("commit_shas") or sprint_state.get("commit_shas") or [])
            if str(item).strip()
        ]
        sprint_tagged_commit_shas = [
            str(item).strip()
            for item in (closeout_result.get("sprint_tagged_commit_shas") or [])
            if str(item).strip()
        ]
        uncommitted_paths = [
            str(item).strip()
            for item in (closeout_result.get("uncommitted_paths") or sprint_state.get("uncommitted_paths") or [])
            if str(item).strip()
        ]
        todo_status_counts = self._count_by_key(list(sprint_state.get("todos") or []), "status")
        lines = [
            f"sprint_id={sprint_state.get('sprint_id') or ''}",
            f"sprint_name={sprint_state.get('sprint_name') or sprint_state.get('sprint_display_name') or 'N/A'}",
            f"phase={sprint_state.get('phase') or 'N/A'}",
            f"milestone_title={sprint_state.get('milestone_title') or 'N/A'}",
            f"sprint_folder={sprint_state.get('sprint_folder') or 'N/A'}",
            f"status={sprint_state.get('status') or ''}",
            f"trigger={sprint_state.get('trigger') or ''}",
            f"closeout_status={closeout_result.get('status') or sprint_state.get('closeout_status') or ''}",
            f"version_control_status={sprint_state.get('version_control_status') or 'N/A'}",
            f"version_control_sha={sprint_state.get('version_control_sha') or 'N/A'}",
            f"auto_commit_status={sprint_state.get('auto_commit_status') or 'N/A'}",
            f"auto_commit_sha={sprint_state.get('auto_commit_sha') or 'N/A'}",
            f"commit_count={closeout_result.get('commit_count') or sprint_state.get('commit_count') or 0}",
            f"sprint_tagged_commit_count={closeout_result.get('sprint_tagged_commit_count') or 0}",
            f"commit_sha={closeout_result.get('representative_commit_sha') or sprint_state.get('commit_sha') or 'N/A'}",
            f"commit_shas={', '.join(commit_shas) if commit_shas else 'N/A'}",
            f"sprint_tagged_commit_shas={', '.join(sprint_tagged_commit_shas) if sprint_tagged_commit_shas else 'N/A'}",
            f"uncommitted_paths={', '.join(uncommitted_paths) if uncommitted_paths else 'N/A'}",
            f"todo_status_counts={_format_count_summary(todo_status_counts, ['running', 'queued', 'uncommitted', 'committed', 'completed', 'blocked', 'failed'])}",
            (
                "version_control_paths="
                + (
                    ", ".join(
                        str(item).strip()
                        for item in (sprint_state.get("version_control_paths") or [])
                        if str(item).strip()
                    )
                    or "N/A"
                )
            ),
            (
                "auto_commit_paths="
                + (
                    ", ".join(
                        str(item).strip()
                        for item in (sprint_state.get("auto_commit_paths") or [])
                        if str(item).strip()
                    )
                    or "N/A"
                )
            ),
            "",
            "todo_summary:",
        ]
        for todo in sprint_state.get("todos") or []:
            lines.append(
                "- [{status}] {title} | request_id={request_id} | carry_over={carry}".format(
                    status=str(todo.get("status") or ""),
                    title=str(todo.get("title") or ""),
                    request_id=str(todo.get("request_id") or "N/A"),
                    carry=str(todo.get("carry_over_backlog_id") or "N/A"),
                )
            )
        linked_artifacts = collect_sprint_todo_artifact_entries(sprint_state)
        if linked_artifacts:
            lines.extend(["", "linked_artifacts:"])
            for entry in linked_artifacts:
                lines.append(
                    "- [{status}] {title} | request_id={request_id} | artifact={path}".format(
                        status=entry["status"],
                        title=entry["title"],
                        request_id=entry["request_id"],
                        path=entry["path"],
                    )
                )
        if closeout_result.get("message"):
            lines.extend(["", f"closeout_message={closeout_result.get('message') or ''}"])
        if sprint_state.get("version_control_message"):
            lines.append(f"version_control_message={sprint_state.get('version_control_message') or ''}")
        if sprint_state.get("auto_commit_message"):
            lines.append(f"auto_commit_message={sprint_state.get('auto_commit_message') or ''}")
        return lines

    def _collect_sprint_report_snapshot(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
    ) -> dict[str, Any]:
        todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
        todo_status_counts = self._count_by_key(todos, "status")
        return {
            "todos": todos,
            "delivered_changes": self._collect_sprint_delivered_changes(sprint_state, todos),
            "planner_report_draft": self._sprint_report_draft(sprint_state),
            "linked_artifacts": collect_sprint_todo_artifact_entries(sprint_state),
            "todo_status_counts": todo_status_counts,
            "todo_summary": _format_count_summary(
                todo_status_counts,
                ["running", "queued", "uncommitted", "committed", "completed", "blocked", "failed"],
            ),
            "commit_count": int(closeout_result.get("commit_count") or sprint_state.get("commit_count") or 0),
            "commit_sha": str(closeout_result.get("representative_commit_sha") or sprint_state.get("commit_sha") or "").strip(),
            "closeout_status": str(closeout_result.get("status") or sprint_state.get("closeout_status") or "").strip(),
            "closeout_message": str(closeout_result.get("message") or "").strip(),
            "uncommitted_paths": [
                str(item).strip()
                for item in (closeout_result.get("uncommitted_paths") or sprint_state.get("uncommitted_paths") or [])
                if str(item).strip()
            ],
            "events": self._load_sprint_event_entries(sprint_state),
            "duration": self._format_sprint_duration(sprint_state),
            "status_label": self._sprint_status_label(str(sprint_state.get("status") or "")),
        }

    def _build_sprint_headline(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> str:
        draft = self._sprint_report_draft(sprint_state, snapshot)
        if draft.get("headline"):
            return self._format_sprint_report_text(draft["headline"], full_detail=full_detail, limit=96)
        milestone = self._format_sprint_report_text(
            sprint_state.get("milestone_title") or sprint_state.get("requested_milestone_title") or "이 스프린트",
            full_detail=full_detail,
            limit=48,
        )
        completed_count = int(snapshot["todo_status_counts"].get("committed") or 0) + int(
            snapshot["todo_status_counts"].get("completed") or 0
        )
        issue_count = (
            int(snapshot["todo_status_counts"].get("blocked") or 0)
            + int(snapshot["todo_status_counts"].get("failed") or 0)
            + int(snapshot["todo_status_counts"].get("uncommitted") or 0)
        )
        parts = [f"{milestone} 스프린트를 {snapshot.get('status_label') or '완료'}했습니다."]
        if completed_count:
            parts.append(f"핵심 작업 {completed_count}건을 반영했습니다.")
        elif snapshot.get("todos"):
            parts.append(f"todo {len(snapshot['todos'])}건을 정리했습니다.")
        if issue_count:
            parts.append(f"핵심 이슈 {issue_count}건이 남았습니다.")
        elif int(snapshot.get("commit_count") or 0) > 0 and str(snapshot.get("commit_sha") or "").strip():
            parts.append(f"대표 커밋 {str(snapshot['commit_sha'])[:7]}를 남겼습니다.")
        elif str(snapshot.get("closeout_message") or "").strip():
            parts.append(
                self._format_sprint_report_text(
                    snapshot["closeout_message"],
                    full_detail=full_detail,
                    limit=72,
                )
            )
        return " ".join(parts).strip()

    def _build_sprint_overview_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        commit_summary = "없음"
        if int(snapshot.get("commit_count") or 0) > 0:
            commit_summary = f"{snapshot['commit_count']}건"
            if str(snapshot.get("commit_sha") or "").strip():
                commit_summary += f" | 대표 {str(snapshot['commit_sha'])[:7]}"
        return [
            f"- TL;DR: {self._build_sprint_headline(sprint_state, snapshot, full_detail=full_detail)}",
            f"- sprint_id: {sprint_state.get('sprint_id') or 'N/A'}",
            f"- sprint_name: {sprint_state.get('sprint_name') or sprint_state.get('sprint_display_name') or 'N/A'}",
            f"- milestone: {sprint_state.get('milestone_title') or 'N/A'}",
            (
                f"- 상태: {snapshot.get('status_label') or 'N/A'}"
                + (
                    f" | closeout={snapshot.get('closeout_status')}"
                    if str(snapshot.get("closeout_status") or "").strip()
                    else ""
                )
            ),
            f"- 기간: {snapshot.get('duration') or 'N/A'}",
            f"- todo 요약: {snapshot.get('todo_summary') or 'N/A'}",
            f"- commit 요약: {commit_summary}",
            f"- 주요 아티팩트: {len(snapshot.get('linked_artifacts') or [])}건",
        ]

    def _build_sprint_timeline_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        draft = self._sprint_report_draft(sprint_state, snapshot)
        draft_timeline = [str(item).strip() for item in (draft.get("timeline") or []) if str(item).strip()]
        if draft_timeline:
            return [
                "- "
                + self._format_sprint_report_text(
                    item.removeprefix("- ").strip(),
                    full_detail=full_detail,
                    limit=160,
                )
                for item in draft_timeline
            ]
        lines: list[str] = []
        total_scope = len(sprint_state.get("selected_items") or []) or len(snapshot.get("todos") or [])
        trigger = str(sprint_state.get("trigger") or "manual_start").strip() or "manual_start"
        lines.append(f"- 시작: `{trigger}`로 스프린트를 열고 {total_scope}건을 작업 범위에 올렸습니다.")
        planning_sync_events = [event for event in snapshot.get("events") or [] if str(event.get("type") or "") == "planning_sync"]
        planning_iterations = list(sprint_state.get("planning_iterations") or [])
        if planning_sync_events or planning_iterations:
            planning_count = len(planning_sync_events) or len(planning_iterations)
            planning_note = ""
            if planning_sync_events:
                planning_note = self._format_sprint_report_text(
                    planning_sync_events[-1].get("summary") or "",
                    full_detail=full_detail,
                    limit=96,
                )
            elif planning_iterations:
                latest_iteration = planning_iterations[-1] if isinstance(planning_iterations[-1], dict) else {}
                planning_note = self._format_sprint_report_text(
                    _first_meaningful_text(
                        latest_iteration.get("summary"),
                        latest_iteration.get("step"),
                        "planning sync를 정리했습니다.",
                    ),
                    full_detail=full_detail,
                    limit=96,
                )
            lines.append(f"- 계획: planning sync {planning_count}회로 {_first_meaningful_text(planning_note, '실행 범위를 구체화했습니다.')}")
        todos = snapshot.get("todos") or []
        if todos:
            owner_roles = _dedupe_preserving_order(
                [
                    self._sprint_role_display_name(str(todo.get("owner_role") or "planner"))
                    for todo in todos
                    if str(todo.get("owner_role") or "").strip()
                ]
            )
            role_summary = ", ".join(owner_roles) if owner_roles else "플래너"
            lines.append(f"- 실행: {role_summary}가 todo {len(todos)}건을 처리했습니다.")
        role_events = [
            event
            for event in snapshot.get("events") or []
            if str(event.get("type") or "") in {"role_result", "request_completed"}
        ]
        if role_events:
            verification_summary = self._format_sprint_report_text(
                role_events[-1].get("summary") or "",
                full_detail=full_detail,
                limit=100,
            )
            lines.append(f"- 검증: {_first_meaningful_text(verification_summary, '역할별 결과를 회수해 스프린트 상태를 검증했습니다.')}")
        elif str(snapshot.get("closeout_message") or "").strip():
            lines.append(
                "- 검증: "
                + self._format_sprint_report_text(
                    snapshot["closeout_message"],
                    full_detail=full_detail,
                    limit=100,
                )
            )
        finish_bits = [f"상태 {snapshot.get('status_label') or 'N/A'}"]
        if str(snapshot.get("closeout_status") or "").strip():
            finish_bits.append(f"closeout={snapshot['closeout_status']}")
        if int(snapshot.get("commit_count") or 0) > 0:
            finish_bits.append(f"commit {snapshot['commit_count']}건")
        if int(len(snapshot.get("linked_artifacts") or [])) > 0:
            finish_bits.append(f"artifact {len(snapshot['linked_artifacts'])}건")
        lines.append(f"- 마감: {', '.join(finish_bits)}으로 정리했습니다.")
        return lines or ["- 흐름 요약 없음"]

    def _build_sprint_agent_contribution_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        draft = self._sprint_report_draft(sprint_state, snapshot)
        draft_contributions = list(draft.get("agent_contributions") or [])
        if draft_contributions:
            lines: list[str] = []
            for item in draft_contributions:
                role = str(item.get("role") or "").strip()
                role_label = self._sprint_role_display_name(role) if role else "역할"
                summary = self._format_sprint_report_text(
                    item.get("summary") or "스프린트 진행을 지원했습니다.",
                    full_detail=full_detail,
                    limit=140,
                )
                line = f"- {role_label}" + (f" ({role})" if role else "") + f": {summary}"
                artifacts = [str(value).strip() for value in (item.get("artifacts") or []) if str(value).strip()]
                if artifacts:
                    line += " 주요 산출물: " + ", ".join(artifacts)
                lines.append(line)
            return lines
        contributions: dict[str, dict[str, Any]] = {}

        def ensure_role(role: str) -> dict[str, Any]:
            normalized = str(role or "").strip().lower() or "orchestrator"
            if normalized not in contributions:
                contributions[normalized] = {
                    "todo_count": 0,
                    "completed_count": 0,
                    "issue_count": 0,
                    "event_count": 0,
                    "titles": [],
                    "highlights": [],
                    "artifacts": [],
                }
            return contributions[normalized]

        for todo in snapshot.get("todos") or []:
            role = str(todo.get("owner_role") or "planner")
            data = ensure_role(role)
            data["todo_count"] += 1
            status = str(todo.get("status") or "").strip().lower()
            if status in {"committed", "completed"}:
                data["completed_count"] += 1
            elif status in {"blocked", "failed", "uncommitted"}:
                data["issue_count"] += 1
            title = self._format_sprint_report_text(
                todo.get("title") or "",
                full_detail=full_detail,
                limit=72,
            )
            if title and title not in data["titles"]:
                data["titles"].append(title)
            for artifact in todo.get("artifacts") or []:
                preview = self._preview_sprint_artifact_path(sprint_state, str(artifact), full_detail=full_detail)
                if preview and preview not in data["artifacts"]:
                    data["artifacts"].append(preview)

        for event in snapshot.get("events") or []:
            payload = dict(event.get("payload") or {}) if isinstance(event.get("payload"), dict) else {}
            role = str(payload.get("role") or "").strip()
            if not role:
                continue
            data = ensure_role(role)
            data["event_count"] += 1
            summary = self._format_sprint_report_text(
                event.get("summary") or "",
                full_detail=full_detail,
                limit=108,
            )
            if summary and summary not in data["highlights"]:
                data["highlights"].append(summary)

        version_control_status = str(sprint_state.get("version_control_status") or "").strip().lower()
        if version_control_status and version_control_status != "not_needed":
            data = ensure_role("version_controller")
            data["event_count"] += 1
            summary = self._format_sprint_report_text(
                _first_meaningful_text(
                    sprint_state.get("version_control_message"),
                    snapshot.get("closeout_message"),
                    f"closeout version control 상태={version_control_status}",
                ),
                full_detail=full_detail,
                limit=108,
            )
            if summary and summary not in data["highlights"]:
                data["highlights"].append(summary)
            for artifact in sprint_state.get("version_control_paths") or []:
                preview = self._preview_sprint_artifact_path(sprint_state, str(artifact), full_detail=full_detail)
                if preview and preview not in data["artifacts"]:
                    data["artifacts"].append(preview)

        ordered_roles = [
            *TEAM_ROLES,
            "version_controller",
            *sorted(role for role in contributions if role not in {*TEAM_ROLES, "version_controller"}),
        ]
        lines: list[str] = []
        for role in ordered_roles:
            data = contributions.get(role)
            if not data:
                continue
            stats: list[str] = []
            if int(data.get("todo_count") or 0) > 0:
                stats.append(f"todo {int(data['todo_count'])}건")
            if int(data.get("completed_count") or 0) > 0:
                stats.append(f"완료 {int(data['completed_count'])}건")
            elif int(data.get("issue_count") or 0) > 0:
                stats.append(f"이슈 {int(data['issue_count'])}건")
            elif int(data.get("event_count") or 0) > 0:
                stats.append(f"이벤트 {int(data['event_count'])}건")
            if full_detail:
                highlight_parts = [str(item).strip() for item in (data.get("highlights") or []) if str(item).strip()]
                if not highlight_parts and data.get("titles"):
                    highlight_parts = [f"{', '.join(data['titles'])} 작업을 담당했습니다."]
                highlight = " | ".join(highlight_parts) if highlight_parts else "스프린트 진행을 지원했습니다."
            else:
                highlight = _first_meaningful_text(
                    *(data.get("highlights") or []),
                    (f"{data['titles'][0]} 등 {int(data['todo_count'])}건을 담당했습니다." if data.get("titles") else ""),
                    "스프린트 진행을 지원했습니다.",
                )
            lines.append(f"- {self._sprint_role_display_name(role)} ({role}): {', '.join(stats) or '활동 기록'}.")
            if highlight:
                lines.append(
                    "  - 근거 하이라이트: "
                    + self._format_sprint_report_text(
                        highlight,
                        full_detail=full_detail,
                        limit=120,
                    )
                )
            artifact_items = [str(item).strip() for item in (data.get("artifacts") or []) if str(item).strip()]
            if full_detail:
                artifact_preview = ", ".join(artifact_items).strip()
            else:
                artifact_preview = ", ".join(artifact_items[:2]).strip()
            if artifact_preview:
                remaining = max(0, len(artifact_items) - 2)
                if not full_detail and remaining > 0:
                    artifact_preview += f" 외 {remaining}건"
                lines.append(f"  - 참고 산출물: {artifact_preview}")
        return lines or ["- 역할별 기여 기록 없음"]

    def _build_sprint_issue_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        draft = self._sprint_report_draft(sprint_state, snapshot)
        draft_issues = [str(item).strip() for item in (draft.get("issues") or []) if str(item).strip()]
        if draft_issues:
            return [
                "- "
                + self._format_sprint_report_text(
                    item.removeprefix("- ").strip(),
                    full_detail=full_detail,
                    limit=140,
                )
                for item in draft_issues
            ]
        issues: list[str] = []
        seen: set[str] = set()
        for todo in snapshot.get("todos") or []:
            status = str(todo.get("status") or "").strip().lower()
            if status not in {"blocked", "failed", "uncommitted"}:
                continue
            reason = _first_meaningful_text(
                todo.get("summary"),
                todo.get("carry_over_backlog_id"),
                "후속 조치가 필요합니다.",
            )
            line = "- [{status}] {title}: {reason}".format(
                status=status,
                title=self._format_sprint_report_text(todo.get("title") or "Untitled", full_detail=full_detail, limit=96),
                reason=self._format_sprint_report_text(reason, full_detail=full_detail, limit=108),
            )
            if line not in seen:
                seen.add(line)
                issues.append(line)
        if snapshot.get("uncommitted_paths"):
            preview_items: list[str] = []
            raw_paths = list(snapshot.get("uncommitted_paths") or [])
            if not full_detail:
                raw_paths = raw_paths[:3]
            for raw_path in raw_paths:
                preview = self._preview_sprint_artifact_path(sprint_state, raw_path, full_detail=full_detail)
                if preview:
                    preview_items.append(preview)
            preview_text = ", ".join(preview_items)
            overflow = max(0, len(snapshot.get("uncommitted_paths") or []) - len(preview_items))
            if not full_detail and overflow > 0:
                preview_text = f"{preview_text} 외 {overflow}건" if preview_text else f"외 {overflow}건"
            line = f"- 참고: 미커밋 경로 {len(snapshot.get('uncommitted_paths') or [])}건 | {preview_text or '상세 경로 확인 필요'}"
            if line not in seen:
                seen.add(line)
                issues.append(line)
        for event in snapshot.get("events") or []:
            payload = dict(event.get("payload") or {}) if isinstance(event.get("payload"), dict) else {}
            role = str(payload.get("role") or "").strip()
            error = _first_meaningful_text(payload.get("error"), payload.get("details"))
            if not error:
                continue
            line = (
                f"- {self._sprint_role_display_name(role)} 이슈: "
                + self._format_sprint_report_text(error, full_detail=full_detail, limit=108)
            )
            if line not in seen:
                seen.add(line)
                issues.append(line)
        if not issues and str(sprint_state.get("status") or "").strip().lower() in {"failed", "blocked"}:
            issues.append(
                "- "
                + self._format_sprint_report_text(
                    _first_meaningful_text(snapshot.get("closeout_message"), "스프린트 마감 단계에서 이슈가 발생했습니다."),
                    full_detail=full_detail,
                    limit=112,
                )
            )
        return issues or ["- 핵심 이슈 없음"]

    def _build_sprint_achievement_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        draft = self._sprint_report_draft(sprint_state, snapshot)
        draft_achievements = [str(item).strip() for item in (draft.get("achievements") or []) if str(item).strip()]
        if draft_achievements:
            return [
                "- "
                + self._format_sprint_report_text(
                    item.removeprefix("- ").strip(),
                    full_detail=full_detail,
                    limit=140,
                )
                for item in draft_achievements
            ]
        achievements: list[str] = []
        for todo in snapshot.get("todos") or []:
            status = str(todo.get("status") or "").strip().lower()
            if status not in {"committed", "completed"}:
                continue
            achievements.append(
                "- [{status}] {title}".format(
                    status=status,
                    title=self._format_sprint_report_text(todo.get("title") or "Untitled", full_detail=full_detail, limit=96),
                )
            )
        if int(snapshot.get("commit_count") or 0) > 0:
            commit_line = f"- closeout commit {int(snapshot['commit_count'])}건을 남겼습니다."
            if str(snapshot.get("commit_sha") or "").strip():
                commit_line += f" 대표 SHA={str(snapshot['commit_sha'])[:7]}"
            achievements.append(commit_line)
        if int(len(snapshot.get("linked_artifacts") or [])) > 0:
            achievements.append(f"- 주요 산출물 {len(snapshot['linked_artifacts'])}건을 report에 연결했습니다.")
        if (
            str(sprint_state.get("status") or "").strip().lower() == "completed"
            and str(snapshot.get("closeout_message") or "").strip()
        ):
            achievements.append(
                "- "
                + self._format_sprint_report_text(
                    snapshot["closeout_message"],
                    full_detail=full_detail,
                    limit=108,
                )
            )
        achievements = _dedupe_preserving_order(achievements)
        return achievements or ["- 주요 성과 없음"]

    def _build_sprint_artifact_lines(
        self,
        sprint_state: dict[str, Any],
        snapshot: dict[str, Any],
        *,
        full_detail: bool = False,
    ) -> list[str]:
        draft = self._sprint_report_draft(sprint_state, snapshot)
        draft_artifacts = [str(item).strip() for item in (draft.get("highlight_artifacts") or []) if str(item).strip()]
        if draft_artifacts:
            return [
                "- "
                + self._format_sprint_report_text(
                    item.removeprefix("- ").strip(),
                    full_detail=full_detail,
                    limit=160,
                )
                for item in draft_artifacts
            ]
        lines: list[str] = []
        for entry in snapshot.get("linked_artifacts") or []:
            lines.append(
                "- 참고: [{status}] {title} -> {path}".format(
                    status=entry["status"],
                    title=self._format_sprint_report_text(entry["title"], full_detail=full_detail, limit=72),
                    path=entry["path"],
                )
            )
        if lines:
            return lines
        commit_paths: list[str] = []
        for raw_path in sprint_state.get("version_control_paths") or []:
            preview = self._preview_sprint_artifact_path(sprint_state, str(raw_path), full_detail=full_detail)
            if preview:
                commit_paths.append(preview)
        if commit_paths:
            return [f"- 참고: {item}" for item in (commit_paths if full_detail else commit_paths[:5])]
        return ["- 참고 아티팩트 없음"]

    def _build_sprint_progress_log_summary(self, sprint_state: dict[str, Any], closeout_result: dict[str, Any]) -> str:
        snapshot = self._collect_sprint_report_snapshot(sprint_state, closeout_result)
        lines = [
            self._build_sprint_headline(sprint_state, snapshot, full_detail=False),
            f"todo={snapshot.get('todo_summary') or 'N/A'}",
            f"commit={int(snapshot.get('commit_count') or 0)}, artifact={len(snapshot.get('linked_artifacts') or [])}",
        ]
        issue_lines = self._build_sprint_issue_lines(sprint_state, snapshot, full_detail=False)
        achievement_lines = self._build_sprint_achievement_lines(sprint_state, snapshot, full_detail=False)
        if issue_lines and issue_lines[0] != "- 핵심 이슈 없음":
            lines.append(issue_lines[0].removeprefix("- ").strip())
        elif achievement_lines and achievement_lines[0] != "- 주요 성과 없음":
            lines.append(achievement_lines[0].removeprefix("- ").strip())
        return "\n".join(lines)

    def _render_sprint_completion_user_report(
        self,
        sprint_state: dict[str, Any],
        closeout_result: dict[str, Any],
        *,
        title: str,
    ) -> str:
        snapshot = self._collect_sprint_report_snapshot(sprint_state, closeout_result)
        report_path = self.paths.sprint_artifact_file(
            str(sprint_state.get("sprint_folder_name") or build_sprint_artifact_folder_name(str(sprint_state.get("sprint_id") or ""))),
            "report.md",
        )
        try:
            report_path_text = report_path.relative_to(self.paths.workspace_root).as_posix()
        except ValueError:
            report_path_text = report_path.as_posix()
        commit_metric = f"{int(snapshot.get('commit_count') or 0)}"
        if str(snapshot.get("commit_sha") or "").strip():
            commit_metric += f" ({str(snapshot['commit_sha'])[:7]})"
        lines = [
            f"## {_decorate_sprint_report_title(title)} 사용자 요약",
            "",
            f"**TL;DR** {self._build_sprint_headline(sprint_state, snapshot, full_detail=True)}",
            "",
            "```text",
            f"sprint_id : {sprint_state.get('sprint_id') or 'N/A'}",
            f"status    : {snapshot.get('status_label') or 'N/A'}",
            f"duration  : {snapshot.get('duration') or 'N/A'}",
            f"todo      : {snapshot.get('todo_summary') or 'N/A'}",
            f"commits   : {commit_metric}",
            f"artifacts : {len(snapshot.get('linked_artifacts') or [])}",
            "```",
            "",
            "🔄 변경 요약",
            *self._build_sprint_change_summary_lines(sprint_state, snapshot, full_detail=True),
            "",
            "🧭 흐름",
            *self._build_sprint_timeline_lines(sprint_state, snapshot, full_detail=True),
            "",
            "🤖 에이전트 기여",
            *self._build_sprint_agent_contribution_lines(sprint_state, snapshot, full_detail=True),
            "",
            "⚠️ 핵심 이슈",
            *self._build_sprint_issue_lines(sprint_state, snapshot, full_detail=True),
            "",
            "🏁 성과",
            *self._build_sprint_achievement_lines(sprint_state, snapshot, full_detail=True),
            "",
            "📎 참고 아티팩트",
            *self._build_sprint_artifact_lines(sprint_state, snapshot, full_detail=True),
            "",
            f"상세 보고: `{report_path_text}`",
        ]
        return "\n".join(lines).strip()

    def _build_sprint_report_body(self, sprint_state: dict[str, Any], closeout_result: dict[str, Any]) -> str:
        snapshot = self._collect_sprint_report_snapshot(sprint_state, closeout_result)
        lines = [
            "# Sprint Report",
            "",
            "## 한눈에 보기",
            "",
            *self._build_sprint_overview_lines(sprint_state, snapshot, full_detail=True),
            "",
            "## 변경 요약",
            "",
            *self._build_sprint_change_summary_lines(sprint_state, snapshot, full_detail=True),
            "",
            "## Sprint A to Z",
            "",
            *self._build_sprint_timeline_lines(sprint_state, snapshot, full_detail=True),
            "",
            "## 에이전트 기여",
            "",
            *self._build_sprint_agent_contribution_lines(sprint_state, snapshot, full_detail=True),
            "",
            "## 핵심 이슈",
            "",
            *self._build_sprint_issue_lines(sprint_state, snapshot, full_detail=True),
            "",
            "## 성과",
            "",
            *self._build_sprint_achievement_lines(sprint_state, snapshot, full_detail=True),
            "",
            "## 참고 아티팩트",
            "",
            *self._build_sprint_artifact_lines(sprint_state, snapshot, full_detail=True),
            "",
            "## 머신 요약",
            "",
            *self._build_machine_sprint_report_lines(sprint_state, closeout_result),
        ]
        return "\n".join(lines).strip()

    def _render_live_sprint_report_markdown(self, sprint_state: dict[str, Any]) -> str:
        todos = [dict(item) for item in (sprint_state.get("todos") or []) if isinstance(item, dict)]
        todo_status_counts = self._count_by_key(todos, "status")
        linked_artifacts = collect_sprint_todo_artifact_entries(sprint_state)
        milestone = str(sprint_state.get("milestone_title") or "이 스프린트").strip() or "이 스프린트"
        status_label = self._sprint_status_label(str(sprint_state.get("status") or ""))
        running_count = int(todo_status_counts.get("running") or 0)
        issue_count = (
            int(todo_status_counts.get("blocked") or 0)
            + int(todo_status_counts.get("failed") or 0)
            + int(todo_status_counts.get("uncommitted") or 0)
        )
        queued_count = int(todo_status_counts.get("queued") or 0)
        completed_count = int(todo_status_counts.get("committed") or 0) + int(todo_status_counts.get("completed") or 0)
        headline_parts = [f"{milestone} 스프린트가 {status_label} 상태입니다."]
        if running_count:
            headline_parts.append(f"실행 중 {running_count}건이 있습니다.")
        if issue_count:
            headline_parts.append(f"후속 확인 {issue_count}건이 남아 있습니다.")
        elif queued_count:
            headline_parts.append(f"다음 대기 작업 {queued_count}건이 있습니다.")
        elif completed_count:
            headline_parts.append("현재 남은 후속 액션이 없습니다.")
        elif todos:
            headline_parts.append(f"todo {len(todos)}건을 추적 중입니다.")
        else:
            headline_parts.append("등록된 todo가 없습니다.")

        next_action_priority = {
            "running": 0,
            "blocked": 1,
            "failed": 1,
            "uncommitted": 1,
            "queued": 2,
        }
        actionable_todos: list[tuple[int, int, int, dict[str, Any]]] = []
        for index, todo in enumerate(todos):
            status = str(todo.get("status") or "").strip().lower()
            if status not in next_action_priority:
                continue
            priority_rank = todo.get("priority_rank")
            try:
                normalized_rank = int(priority_rank)
            except (TypeError, ValueError):
                normalized_rank = 999999
            actionable_todos.append((next_action_priority[status], normalized_rank, index, todo))
        actionable_todos.sort(key=lambda item: (item[0], item[1], item[2]))

        lines = [
            "# Sprint Report",
            "",
            "## 한눈에 보기",
            "",
            f"- TL;DR: {' '.join(headline_parts)}",
            f"- sprint_id: {sprint_state.get('sprint_id') or 'N/A'}",
            f"- milestone: {milestone}",
            f"- 상태: {status_label}",
            f"- phase: {sprint_state.get('phase') or 'N/A'}",
            (
                "- todo 요약: "
                + _format_count_summary(
                    todo_status_counts,
                    ["running", "queued", "uncommitted", "committed", "completed", "blocked", "failed"],
                )
            ),
            "",
            "## 다음 액션",
            "",
        ]
        if actionable_todos:
            for _, _, _, todo in actionable_todos:
                lines.append(
                    "- [{status}] {title} | request_id={request_id}".format(
                        status=str(todo.get("status") or ""),
                        title=str(todo.get("title") or ""),
                        request_id=str(todo.get("request_id") or "N/A"),
                    )
                )
        elif todos:
            lines.append("- 현재 남은 후속 액션 없음")
        else:
            lines.append("- 다음 액션 후보 없음")
        lines.extend(
            [
                "",
                "## Todo Summary",
                "",
            ]
        )
        if todos:
            for todo in todos:
                lines.append(
                    "- [{status}] {title} | request_id={request_id} | artifacts={artifacts}".format(
                        status=str(todo.get("status") or ""),
                        title=str(todo.get("title") or ""),
                        request_id=str(todo.get("request_id") or "N/A"),
                        artifacts=", ".join(
                            str(item).strip() for item in (todo.get("artifacts") or []) if str(item).strip()
                        )
                        or "N/A",
                    )
                )
        else:
            lines.append("- todo 없음")
        lines.extend(
            [
                "",
                (
                    "- todo_summary: "
                    + _format_count_summary(
                        todo_status_counts,
                        ["running", "queued", "uncommitted", "committed", "completed", "blocked", "failed"],
                    )
                ),
                "",
                "## Linked Todo Artifacts",
                "",
            ]
        )
        if linked_artifacts:
            for entry in linked_artifacts:
                lines.append(
                    "- [{status}] {title} | request_id={request_id} | artifact={path}".format(
                        status=entry["status"],
                        title=entry["title"],
                        request_id=entry["request_id"],
                        path=entry["path"],
                    )
                )
        else:
            lines.append("- linked sprint artifact 없음")
        return "\n".join(lines).rstrip()

    def _ensure_markdown_file(self, path: Path, header: str) -> None:
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{header}\n", encoding="utf-8")

    def _append_markdown_entry(self, path: Path, header: str, title: str, lines: list[str]) -> None:
        self._ensure_markdown_file(path, header)
        existing = path.read_text(encoding="utf-8").rstrip()
        entry_body = _normalize_markdown_body(lines)
        entry = f"## {title}"
        if entry_body:
            entry = f"{entry}\n{entry_body}"
        updated = f"{existing}\n\n{entry}\n" if existing else f"{header}\n\n{entry}\n"
        path.write_text(updated, encoding="utf-8")

    def _refresh_role_todos(self) -> None:
        records = list(iter_json_records(self.paths.requests_dir))
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
            self.paths.role_todo_file(role).write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def _append_role_history(
        self,
        role: str,
        request_record: dict[str, Any],
        *,
        event_type: str,
        summary: str,
        result: dict[str, Any] | None = None,
    ) -> None:
        insights = _normalize_insights(result or {})
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
        self._append_markdown_entry(
            self.paths.role_history_file(role),
            f"# {role.title()} History",
            f"{utc_now_iso()} | {request_record.get('request_id') or 'unknown'}",
            lines,
        )

    def _append_role_journal(
        self,
        role: str,
        request_record: dict[str, Any],
        *,
        title: str,
        lines: list[str],
    ) -> None:
        self._append_markdown_entry(
            self.paths.role_journal_file(role),
            f"# {role.title()} Journal",
            f"{utc_now_iso()} | {request_record.get('request_id') or 'unknown'} | {title}",
            lines,
        )

    def _append_shared_workspace_entry(
        self,
        destination: str,
        *,
        request_record: dict[str, Any],
        title: str,
        lines: list[str],
    ) -> None:
        path_map = {
            "planning": (self.paths.shared_planning_file, "# Shared Planning"),
            "decision_log": (self.paths.shared_decision_log_file, "# Decision Log"),
            "shared_history": (self.paths.shared_history_file, "# Shared History"),
            "sync_contract": (self.paths.shared_sync_contract_file, "# Sync Contract"),
        }
        path, header = path_map[destination]
        self._append_markdown_entry(path, header, title, lines)

    def _record_shared_role_result(self, request_record: dict[str, Any], result: dict[str, Any]) -> None:
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
            self._append_shared_workspace_entry(
                primary_destination,
                request_record=request_record,
                title=f"{utc_now_iso()} | {role} | {request_record.get('request_id') or 'unknown'}",
                lines=base_lines,
            )
        if primary_destination != "shared_history":
            self._append_shared_workspace_entry(
                "shared_history",
                request_record=request_record,
                title=f"{utc_now_iso()} | {role} | {request_record.get('request_id') or 'unknown'}",
                lines=base_lines,
            )

    async def _handle_orchestrator_message(self, message: DiscordMessage) -> None:
        if self._is_trusted_relay_message(message):
            if self._is_internal_relay_summary_message(message):
                return
            envelope = parse_message_content(
                message.content,
                bot_ids_by_role={role: cfg.bot_id for role, cfg in self.discord_config.agents.items()},
                default_sender="user",
                default_target="orchestrator",
            )
            kind = str(envelope.params.get("_teams_kind") or "").strip()
            if kind == "report":
                await self._handle_role_report(message, envelope)
                return
            if kind == "forward":
                await self._handle_user_request(message, envelope, forwarded=True)
                return
            self._log_malformed_trusted_relay(reason="unsupported kind for orchestrator", kind=kind)
            return
        if self._is_attachment_only_save_failure(message):
            await self._send_channel_reply(
                message,
                "첨부 파일을 저장하지 못했습니다. 파일을 다시 보내거나 본문과 함께 다시 요청해 주세요.",
            )
            return
        envelope = parse_user_message_content(
            message.content,
            artifacts=self._message_attachment_artifacts(message),
            bot_ids_by_role={role: cfg.bot_id for role, cfg in self.discord_config.agents.items()},
            default_sender="user",
            default_target="orchestrator",
        )
        await self._handle_user_request(message, envelope, forwarded=False)

    async def _handle_non_orchestrator_message(self, message: DiscordMessage) -> None:
        if self._is_trusted_relay_message(message):
            if self._is_internal_relay_summary_message(message):
                return
            envelope = parse_message_content(
                message.content,
                bot_ids_by_role={role: cfg.bot_id for role, cfg in self.discord_config.agents.items()},
                default_sender="user",
                default_target=self.role,
            )
            kind = str(envelope.params.get("_teams_kind") or "").strip()
            if kind != "delegate":
                self._log_malformed_trusted_relay(reason="missing delegate kind", kind=kind)
                return
            if envelope.target != self.role:
                return
            await self._handle_delegated_request(message, envelope)
            return
        if self._is_attachment_only_save_failure(message):
            await self._send_channel_reply(
                message,
                "첨부 파일을 저장하지 못했습니다. 파일을 다시 보내거나 본문과 함께 다시 요청해 주세요.",
            )
            return
        envelope = parse_user_message_content(
            message.content,
            artifacts=self._message_attachment_artifacts(message),
            bot_ids_by_role={role: cfg.bot_id for role, cfg in self.discord_config.agents.items()},
            default_sender="user",
            default_target=self.role,
        )
        if envelope.target != self.role:
            LOGGER.info(
                "Ignoring user message for other role in %s: target=%s",
                self.role,
                envelope.target,
            )
            return
        await self._forward_user_request(message, envelope)

    async def _handle_user_request(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> None:
        self._ensure_orchestrator_session_ready_for_sprint_start(envelope)
        duplicate_request = self._find_duplicate_request(message, envelope)
        if duplicate_request:
            reopened_request = await self._maybe_reopen_blocked_duplicate_request(
                duplicate_request,
                message,
                envelope,
                forwarded=forwarded,
            )
            if reopened_request:
                request_record, relay_sent, reopen_mode = reopened_request
                if reopen_mode == "retried":
                    await self._reply_to_requester(
                        request_record,
                        self._build_requester_status_message(
                            status="delegated",
                            request_id=str(request_record.get("request_id") or ""),
                            summary="기존 blocked 요청을 다시 시도합니다.",
                        ),
                    )
                else:
                    await self._reply_to_requester(
                        request_record,
                        (
                            "기존 blocked 요청을 보강된 입력으로 재개했습니다. "
                            f"request_id={request_record['request_id']}"
                            if relay_sent
                            else (
                                "기존 blocked 요청은 재개했지만 planner relay 전송이 실패했습니다. "
                                f"request_id={request_record['request_id']}"
                            )
                        ),
                    )
                return
            append_request_event(
                duplicate_request,
                event_type="reused",
                actor="orchestrator",
                summary="중복 요청을 기존 request에 연결했습니다.",
                payload={"message_id": message.message_id},
            )
            self._save_request(duplicate_request)
            await self._reply_to_requester(
                duplicate_request,
                (
                    "기존 요청을 재사용합니다. "
                    f"request_id={duplicate_request['request_id']}\n"
                    f"status={duplicate_request.get('status') or 'unknown'}\n"
                    f"current_role={duplicate_request.get('current_role') or 'orchestrator'}"
                ),
            )
            return
        request_record = self._create_request_record(message, envelope, forwarded=forwarded)
        request_record["status"] = "delegated"
        request_record["current_role"] = "orchestrator"
        request_record["next_role"] = "orchestrator"
        request_record["routing_context"] = self._build_routing_context(
            "orchestrator",
            reason="Selected orchestrator as the first agent owner for this user-originated request.",
            requested_role="orchestrator",
            selection_source="agent_first_intake",
        )
        append_request_event(
            request_record,
            event_type="delegated",
            actor="orchestrator",
            summary="사용자 작업 요청을 orchestrator agent로 전달했습니다.",
            payload={"routing_context": dict(request_record.get("routing_context") or {})},
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="delegated",
            summary="사용자 작업 요청을 orchestrator agent로 전달했습니다.",
        )
        await self._run_local_orchestrator_request(request_record)

    def _backlog_counts(self) -> dict[str, int]:
        if self._drop_non_actionable_backlog_items():
            self._refresh_backlog_markdown()
        repaired_ids = self._repair_non_actionable_carry_over_backlog_items()
        if repaired_ids:
            self._refresh_backlog_markdown()
        items = self._iter_backlog_items()
        pending = sum(1 for item in items if str(item.get("status") or "") == "pending")
        selected = sum(1 for item in items if str(item.get("status") or "") == "selected")
        blocked = sum(1 for item in items if str(item.get("status") or "") == "blocked")
        done = sum(1 for item in items if str(item.get("status") or "") == "done")
        return {
            "pending": pending,
            "selected": selected,
            "blocked": blocked,
            "done": done,
            "total": pending + selected + blocked,
        }

    @staticmethod
    def _status_rank(value: str) -> int:
        normalized = str(value or "").strip().lower()
        return {"selected": 0, "pending": 1, "blocked": 2, "done": 3}.get(normalized, 4)

    @staticmethod
    def _kind_rank(value: str) -> int:
        normalized = str(value or "").strip().lower()
        return {"bug": 0, "feature": 1, "enhancement": 2, "chore": 3}.get(normalized, 4)

    def _backlog_priority_key(self, item: dict[str, Any]) -> tuple[int, int, int, str]:
        source_rank = 0 if str(item.get("source") or "").strip() == "user" else 1
        priority_rank = int(item.get("priority_rank") or 0)
        return (
            self._status_rank(str(item.get("status") or "")),
            -priority_rank,
            source_rank,
            self._kind_rank(str(item.get("kind") or "")),
            str(item.get("created_at") or ""),
        )

    @staticmethod
    def _count_by_key(items: list[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            normalized = str(item.get(key) or "").strip().lower()
            if not normalized:
                continue
            counts[normalized] = counts.get(normalized, 0) + 1
        return counts

    def _render_backlog_status_report(self) -> str:
        items = self._iter_backlog_items()
        active_items = [
            item
            for item in items
            if str(item.get("status") or "").strip().lower() in {"selected", "pending", "blocked"}
        ]
        active_items.sort(key=self._backlog_priority_key)
        counts = self._backlog_counts()
        kind_counts = self._count_by_key(active_items, "kind")
        source_counts = self._count_by_key(active_items, "source")
        lines = [
            "## Backlog Summary",
            f"- counts: pending={counts['pending']}, selected={counts['selected']}, blocked={counts['blocked']}, total={counts['total']}",
            f"- kind_summary: {_format_count_summary(kind_counts, ['bug', 'feature', 'enhancement', 'chore'])}",
            f"- source_summary: {_format_count_summary(source_counts, ['user', 'sourcer', 'carry_over'])}",
            "",
            "## Priority Backlog",
        ]
        if active_items:
            for item in active_items[:12]:
                lines.append(
                    "- [{status}] {title} | backlog_id={backlog_id} | kind={kind} | source={source}".format(
                        status=str(item.get("status") or "").strip() or "unknown",
                        backlog_id=str(item.get("backlog_id") or "").strip() or "N/A",
                        title=str(item.get("title") or item.get("scope") or "").strip() or "Untitled",
                        kind=str(item.get("kind") or "").strip() or "N/A",
                        source=str(item.get("source") or "").strip() or "N/A",
                    )
                )
        else:
            lines.append("- active backlog 없음")
        return "\n".join(lines)

    def _render_sprint_status_report(
        self,
        sprint_state: dict[str, Any],
        *,
        is_active: bool,
        scheduler_state: dict[str, Any],
    ) -> str:
        selected_items = list(sprint_state.get("selected_items") or [])
        todos = list(sprint_state.get("todos") or [])
        todo_status_counts = self._count_by_key(todos, "status")
        selected_kind_counts = self._count_by_key(selected_items, "kind")
        lines = [
            "## Sprint Summary",
            f"- view: {'active' if is_active else 'latest'}",
            f"- sprint_id: {sprint_state.get('sprint_id') or ''}",
            f"- sprint_name: {sprint_state.get('sprint_name') or sprint_state.get('sprint_display_name') or 'N/A'}",
            f"- phase: {sprint_state.get('phase') or 'N/A'}",
            f"- milestone_title: {sprint_state.get('milestone_title') or 'N/A'}",
            f"- status: {sprint_state.get('status') or ''}",
            f"- trigger: {sprint_state.get('trigger') or ''}",
            f"- started_at: {sprint_state.get('started_at') or ''}",
            f"- ended_at: {sprint_state.get('ended_at') or 'N/A'}",
            f"- closeout_status: {sprint_state.get('closeout_status') or 'N/A'}",
            f"- commit_count: {sprint_state.get('commit_count') or 0}",
            f"- commit_sha: {sprint_state.get('commit_sha') or 'N/A'}",
            f"- next_slot_at: {scheduler_state.get('next_slot_at') or 'N/A'}",
            f"- todo_summary: {_format_count_summary(todo_status_counts, ['running', 'queued', 'uncommitted', 'committed', 'completed', 'blocked', 'failed'])}",
            f"- backlog_kind_summary: {_format_count_summary(selected_kind_counts, ['bug', 'feature', 'enhancement', 'chore'])}",
            "",
            "## Sprint Tasks",
        ]
        if todos:
            for todo in todos[:12]:
                lines.append(
                    "- [{status}] {title} | todo_id={todo_id} | backlog_id={backlog_id} | request_id={request_id}".format(
                        status=str(todo.get("status") or "").strip() or "unknown",
                        title=str(todo.get("title") or "").strip() or "Untitled",
                        todo_id=str(todo.get("todo_id") or "").strip() or "N/A",
                        backlog_id=str(todo.get("backlog_id") or "").strip() or "N/A",
                        request_id=str(todo.get("request_id") or "").strip() or "N/A",
                    )
                )
        elif selected_items:
            for item in selected_items[:12]:
                lines.append(
                    "- [selected] {title} | backlog_id={backlog_id} | kind={kind}".format(
                        title=str(item.get("title") or item.get("scope") or "").strip() or "Untitled",
                        backlog_id=str(item.get("backlog_id") or "").strip() or "N/A",
                        kind=str(item.get("kind") or "").strip() or "N/A",
                    )
                )
        else:
            lines.append("- sprint task 없음")
        return "\n".join(lines)

    @staticmethod
    def _build_requester_route(
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> dict[str, Any]:
        requester = _extract_original_requester(envelope.params) if forwarded else {}
        return {
            "author_id": requester.get("author_id", message.author_id) if forwarded else message.author_id,
            "author_name": requester.get("author_name", message.author_name) if forwarded else message.author_name,
            "channel_id": requester.get("channel_id", message.channel_id) if forwarded else message.channel_id,
            "guild_id": requester.get("guild_id", message.guild_id or "") if forwarded else (message.guild_id or ""),
            "is_dm": requester.get("is_dm", message.is_dm) if forwarded else message.is_dm,
            "message_id": requester.get("message_id", message.message_id) if forwarded else message.message_id,
        }

    @staticmethod
    def _extract_sprint_folder_name(sprint_state: dict[str, Any] | None) -> str:
        state = dict(sprint_state or {})
        folder_name = str(state.get("sprint_folder_name") or "").strip()
        if folder_name:
            return folder_name
        sprint_folder = str(state.get("sprint_folder") or "").strip()
        if sprint_folder:
            candidate = Path(sprint_folder).name
            if candidate:
                return candidate
        sprint_id = str(state.get("sprint_id") or "").strip()
        if sprint_id:
            return build_sprint_artifact_folder_name(sprint_id)
        return ""

    def _ensure_sprint_folder_metadata(self, sprint_state: dict[str, Any]) -> None:
        folder_name = self._extract_sprint_folder_name(sprint_state)
        if not folder_name:
            return
        sprint_state["sprint_folder_name"] = folder_name
        if not str(sprint_state.get("sprint_folder") or "").strip():
            sprint_state["sprint_folder"] = str(self.paths.sprint_artifact_dir(folder_name))

    def _default_attachment_sprint_folder_name(self) -> str:
        return build_sprint_artifact_folder_name(str(self.runtime_config.sprint_id or ""))

    def _resolve_message_attachment_root(self, message: DiscordMessage) -> Path:
        scheduler_state = self._load_scheduler_state()
        active_sprint_id = str(scheduler_state.get("active_sprint_id") or "").strip()
        active_sprint = self._load_sprint_state(active_sprint_id) if active_sprint_id else {}
        folder_name = self._extract_sprint_folder_name(active_sprint)
        if not folder_name and active_sprint_id:
            folder_name = build_sprint_artifact_folder_name(active_sprint_id)
        if not folder_name and is_manual_sprint_start_text(str(message.content or "")):
            folder_name = build_sprint_artifact_folder_name(
                build_active_sprint_id(now=self._message_received_at(message))
            )
        if not folder_name:
            folder_name = self._default_attachment_sprint_folder_name()
        return self.paths.sprint_attachment_root(folder_name)

    @staticmethod
    def _attachment_storage_relative_path(path: Path) -> Path:
        return Path(path.name)

    def _sprint_attachment_filename(
        self,
        artifact_hint: str,
        *,
        resolved: Path | None = None,
    ) -> str:
        candidate = resolved
        if candidate is not None:
            try:
                relative = candidate.resolve().relative_to(self.paths.sprint_artifacts_root.resolve())
            except ValueError:
                relative = None
            if relative is not None and len(relative.parts) >= 3 and relative.parts[1] == "attachments":
                return candidate.name

        normalized = str(artifact_hint or "").strip()
        if normalized.startswith("./"):
            normalized = normalized[2:]
        hint_path = Path(normalized)
        parts = hint_path.parts
        if len(parts) >= 4 and parts[0] == "shared_workspace" and parts[1] == "sprints" and parts[3] == "attachments":
            return hint_path.name
        return ""

    def _relocate_artifacts_to_sprint_folder(
        self,
        artifacts: list[str],
        sprint_state: dict[str, Any],
    ) -> list[str]:
        folder_name = self._extract_sprint_folder_name(sprint_state)
        if not folder_name:
            return [str(item).strip() for item in artifacts if str(item).strip()]
        relocated: list[str] = []
        destination_root = self.paths.sprint_attachment_root(folder_name)
        for artifact in artifacts:
            normalized = str(artifact or "").strip()
            if not normalized:
                continue
            resolved = self._resolve_artifact_path(normalized)
            destination: Path | None = None
            attachment_filename = self._sprint_attachment_filename(normalized, resolved=resolved)
            if attachment_filename:
                destination = destination_root / attachment_filename
            if resolved is None or not resolved.exists():
                if destination is not None and destination.exists():
                    artifact_hint = self._workspace_artifact_hint(destination)
                    if artifact_hint not in relocated:
                        relocated.append(artifact_hint)
                    continue
                if normalized not in relocated:
                    relocated.append(normalized)
                continue
            if destination is None:
                destination = destination_root / self._attachment_storage_relative_path(resolved)
            if resolved.resolve() != destination.resolve():
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    if destination.is_dir():
                        shutil.rmtree(destination)
                    else:
                        destination.unlink()
                shutil.move(str(resolved), str(destination))
                resolved = destination
            artifact_hint = self._workspace_artifact_hint(resolved)
            if artifact_hint not in relocated:
                relocated.append(artifact_hint)
        return relocated

    def _normalize_sprint_reference_attachments(self, sprint_state: dict[str, Any]) -> bool:
        folder_name = self._extract_sprint_folder_name(sprint_state)
        if not folder_name:
            return False
        artifact_fields = ("kickoff_reference_artifacts", "reference_artifacts")
        original_by_field: dict[str, list[str]] = {}
        relocation_candidates: list[str] = []

        for field_name in artifact_fields:
            original_values = [
                str(item).strip()
                for item in (sprint_state.get(field_name) or [])
                if str(item).strip()
            ]
            original_by_field[field_name] = original_values
            for artifact in original_values:
                if not self._sprint_attachment_filename(artifact):
                    continue
                if artifact not in relocation_candidates:
                    relocation_candidates.append(artifact)

        if not relocation_candidates:
            return False

        relocated_values = self._relocate_artifacts_to_sprint_folder(relocation_candidates, sprint_state)
        relocation_map = {
            source: destination
            for source, destination in zip(relocation_candidates, relocated_values)
            if str(destination or "").strip()
        }

        changed = False
        for field_name, original_values in original_by_field.items():
            normalized_values = _dedupe_preserving_order(
                [relocation_map.get(value, value) for value in original_values]
            )[:12]
            if normalized_values != original_values:
                sprint_state[field_name] = normalized_values
                changed = True
        return changed

    def _load_latest_sprint_state(self) -> dict[str, Any]:
        sprint_files = sorted(self.paths.sprints_dir.glob("*.json"))
        if not sprint_files:
            return {}
        return read_json(sprint_files[-1])

    def _load_status_target_sprint(self) -> tuple[dict[str, Any], bool, dict[str, Any]]:
        scheduler_state = self._load_scheduler_state()
        sprint_state = self._load_sprint_state(str(scheduler_state.get("active_sprint_id") or ""))
        is_active = bool(sprint_state)
        if not sprint_state:
            sprint_state = self._load_latest_sprint_state()
        return sprint_state, is_active, scheduler_state

    def build_sprint_status_message(self) -> str:
        sprint_state, is_active, scheduler_state = self._load_status_target_sprint()
        if not sprint_state:
            return "기록된 sprint가 없습니다."
        return self._render_sprint_status_report(
            sprint_state,
            is_active=is_active,
            scheduler_state=scheduler_state,
        )

    async def start_sprint_lifecycle(
        self,
        milestone_title: str,
        *,
        trigger: str = "manual_start",
        resume_mode: str = "background",
        started_at: datetime | None = None,
        kickoff_brief: str = "",
        kickoff_requirements: list[str] | None = None,
        kickoff_request_text: str = "",
        kickoff_source_request_id: str = "",
        kickoff_reference_artifacts: list[str] | None = None,
    ) -> str:
        active_sprint = self._load_active_sprint_state()
        if active_sprint:
            return (
                "이미 active sprint가 있습니다.\n"
                f"sprint_id={active_sprint.get('sprint_id') or ''}\n"
                f"phase={active_sprint.get('phase') or 'N/A'}\n"
                f"milestone={active_sprint.get('milestone_title') or 'N/A'}"
            )
        normalized_milestone = str(milestone_title or "").strip()
        if not normalized_milestone:
            return "스프린트를 시작하려면 milestone을 알려주세요. 예: `milestone: sprint workflow initial phase 개선`"
        sprint_state = self._build_manual_sprint_state(
            milestone_title=normalized_milestone,
            trigger=trigger,
            started_at=started_at,
            kickoff_brief=kickoff_brief,
            kickoff_requirements=kickoff_requirements,
            kickoff_request_text=kickoff_request_text,
            kickoff_source_request_id=kickoff_source_request_id,
            kickoff_reference_artifacts=kickoff_reference_artifacts,
        )
        scheduler_state = self._load_scheduler_state()
        scheduler_state["active_sprint_id"] = str(sprint_state.get("sprint_id") or "")
        scheduler_state["last_started_at"] = str(sprint_state.get("started_at") or "")
        scheduler_state["last_trigger"] = trigger
        self._clear_pending_milestone_request(scheduler_state)
        self._save_scheduler_state(scheduler_state)
        self._save_sprint_state(sprint_state)
        self._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="started",
            summary="사용자 milestone 기반 manual sprint를 시작했습니다.",
            payload={"milestone_title": sprint_state.get("milestone_title") or ""},
        )
        sprint_id = str(sprint_state.get("sprint_id") or "")
        if resume_mode == "await":
            await self._resume_active_sprint(sprint_id)
        elif resume_mode == "background":
            asyncio.create_task(self._resume_active_sprint(sprint_id))
        refreshed = self._load_sprint_state(str(sprint_state.get("sprint_id") or "")) or sprint_state
        return (
            "manual sprint initial phase를 시작했습니다.\n"
            f"sprint_id={refreshed.get('sprint_id') or ''}\n"
            f"sprint_name={refreshed.get('sprint_name') or ''}\n"
            f"milestone={refreshed.get('milestone_title') or ''}"
        )

    async def stop_sprint_lifecycle(self, *, resume_mode: str = "background") -> str:
        sprint_state = self._load_active_sprint_state()
        if not sprint_state:
            return "현재 active sprint가 없어 종료할 대상이 없습니다."
        status = str(sprint_state.get("status") or "").strip().lower()
        if status in {"failed", "blocked"}:
            self._finish_scheduler_after_sprint(sprint_state, clear_active=True)
            return (
                "terminal sprint를 종료 처리했습니다.\n"
                f"sprint_id={sprint_state.get('sprint_id') or ''}\n"
                f"status={status or 'N/A'}"
            )
        sprint_state["wrap_up_requested_at"] = utc_now_iso()
        self._append_sprint_event(
            str(sprint_state.get("sprint_id") or ""),
            event_type="wrap_up_requested",
            summary="사용자가 현재 sprint 종료를 요청했습니다.",
        )
        self._save_sprint_state(sprint_state)
        sprint_id = str(sprint_state.get("sprint_id") or "")
        if resume_mode == "await":
            await self._resume_active_sprint(sprint_id)
        elif resume_mode == "background":
            asyncio.create_task(self._resume_active_sprint(sprint_id))
        refreshed = self._load_sprint_state(str(sprint_state.get("sprint_id") or "")) or sprint_state
        running_todo = any(
            str(todo.get("status") or "").strip().lower() == "running"
            for todo in refreshed.get("todos") or []
        )
        return (
            "현재 sprint를 wrap up 대상으로 표시했고 즉시 전환을 시도합니다.\n"
            f"sprint_id={refreshed.get('sprint_id') or ''}\n"
            + ("현재 실행 중인 task는 현재 주기에서 마무리 후 전환될 수 있습니다." if running_todo else "곧 wrap up을 시작합니다.")
        )

    async def restart_sprint_lifecycle(self, *, resume_mode: str = "background") -> str:
        scheduler_state = self._load_scheduler_state()
        sprint_state = self._load_sprint_state(str(scheduler_state.get("active_sprint_id") or ""))
        if not sprint_state:
            sprint_state = self._load_latest_sprint_state()
        if not sprint_state:
            return "재개할 sprint가 없습니다."
        sprint_id = str(sprint_state.get("sprint_id") or "").strip()
        status = str(sprint_state.get("status") or "").strip().lower()
        if not sprint_id:
            return "재개할 sprint가 없습니다."
        if status == "completed":
            return f"이미 완료된 sprint라 재개할 수 없습니다. sprint_id={sprint_id}"
        if status in {"failed", "blocked"} and not self._is_resumable_blocked_sprint(sprint_state):
            return f"현재 상태에서는 sprint를 재개할 수 없습니다. sprint_id={sprint_id}\nstatus={status or 'N/A'}"
        sprint_state["resume_from_checkpoint_requested_at"] = utc_now_iso()
        self._append_sprint_event(
            sprint_id,
            event_type="restart_requested",
            summary="사용자가 마지막 execution checkpoint부터 sprint 재개를 요청했습니다.",
        )
        self._save_sprint_state(sprint_state)
        scheduler_state["active_sprint_id"] = sprint_id
        scheduler_state["last_trigger"] = "manual_restart"
        self._save_scheduler_state(scheduler_state)
        if resume_mode == "await":
            await self._resume_active_sprint(sprint_id)
        elif resume_mode == "background":
            asyncio.create_task(self._resume_active_sprint(sprint_id))
        refreshed = self._load_sprint_state(sprint_id) or sprint_state
        return (
            "active sprint 재개를 요청했습니다.\n"
            f"sprint_id={refreshed.get('sprint_id') or ''}\n"
            f"phase={refreshed.get('phase') or 'N/A'}\n"
            f"status={refreshed.get('status') or 'N/A'}"
        )

    async def _handle_manual_sprint_start_request(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> None:
        kickoff_payload = self._extract_manual_sprint_kickoff_payload(envelope)
        milestone_title = str(kickoff_payload.get("milestone_title") or "").strip()
        message_text = await self.start_sprint_lifecycle(
            milestone_title,
            trigger="manual_start",
            resume_mode="background",
            started_at=self._message_received_at(message),
            kickoff_brief=str(kickoff_payload.get("kickoff_brief") or "").strip(),
            kickoff_requirements=list(kickoff_payload.get("kickoff_requirements") or []),
            kickoff_request_text=str(kickoff_payload.get("kickoff_request_text") or "").strip(),
            kickoff_source_request_id=str(kickoff_payload.get("kickoff_source_request_id") or "").strip(),
            kickoff_reference_artifacts=list(kickoff_payload.get("kickoff_reference_artifacts") or []),
        )
        sprint_state = self._load_active_sprint_state()
        if sprint_state and envelope.artifacts:
            relocated_artifacts = self._relocate_artifacts_to_sprint_folder(
                list(envelope.artifacts),
                sprint_state,
            )
            if relocated_artifacts:
                sprint_state["reference_artifacts"] = list(relocated_artifacts)[:12]
                sprint_state["kickoff_reference_artifacts"] = list(relocated_artifacts)[:12]
                self._save_sprint_state(sprint_state)
                self._append_sprint_event(
                    str(sprint_state.get("sprint_id") or ""),
                    event_type="reference_artifacts_linked",
                    summary="사용자가 전달한 sprint reference 문서를 sprint folder에 정리했습니다.",
                    payload={"artifacts": relocated_artifacts},
                )
        await self._reply_to_requester(
            {"reply_route": self._build_requester_route(message, envelope, forwarded=forwarded)},
            message_text,
        )

    async def _handle_manual_sprint_finalize_request(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> None:
        message_text = await self.stop_sprint_lifecycle(resume_mode="background")
        await self._reply_to_requester(
            {"reply_route": self._build_requester_route(message, envelope, forwarded=forwarded)},
            message_text,
        )

    def _should_request_sprint_milestone_for_relay_intake(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
        *,
        forwarded: bool,
    ) -> bool:
        if str(envelope.intent or "").strip().lower() != "route":
            return False
        requester_route = self._build_requester_route(message, envelope, forwarded=forwarded)
        if bool(requester_route.get("is_dm")):
            return False
        channel_id = str(requester_route.get("channel_id") or "").strip()
        relay_channel_id = str(self.discord_config.relay_channel_id or "").strip()
        if not channel_id or channel_id != relay_channel_id:
            return False
        return not bool(self._load_active_sprint_state())

    async def _reinterpret_user_envelope(
        self,
        message: DiscordMessage,
        envelope: MessageEnvelope,
    ) -> MessageEnvelope:
        raw_text = str(message.content or "").strip()
        if not raw_text:
            return envelope
        scheduler_state = self._load_scheduler_state()
        active_sprint = self._load_sprint_state(str(scheduler_state.get("active_sprint_id") or ""))
        try:
            classification = await asyncio.to_thread(
                self.intent_parser.classify,
                raw_text=raw_text,
                envelope=envelope,
                scheduler_state=scheduler_state,
                active_sprint=active_sprint,
                backlog_counts=self._backlog_counts(),
                forwarded=False,
            )
        except Exception:
            LOGGER.exception("Intent parser failed for message: %s", raw_text)
            return envelope
        classification = normalize_intent_payload(classification)
        merged_params = dict(envelope.params)
        parser_params = classification.get("params")
        if isinstance(parser_params, dict):
            merged_params.update(parser_params)

        interpreted = MessageEnvelope(
            request_id=str(classification.get("request_id") or envelope.request_id or "").strip() or None,
            sender=envelope.sender,
            target="orchestrator",
            intent=str(classification.get("intent") or envelope.intent or "route").strip().lower() or "route",
            urgency=envelope.urgency,
            scope=str(classification.get("scope") or envelope.scope or "").strip() or envelope.scope,
            artifacts=list(envelope.artifacts),
            params={
                **merged_params,
                "_intent_source": "internal_parser",
                "parser_confidence": str(classification.get("confidence") or "").strip(),
                "parser_reason": str(classification.get("reason") or "").strip(),
            },
            body=str(classification.get("body") or envelope.body or "").strip(),
        )
        LOGGER.info(
            "Internal parser classified intake: intent=%s scope=%s request_id=%s confidence=%s",
            interpreted.intent,
            interpreted.scope,
            interpreted.request_id or "",
            interpreted.params.get("parser_confidence") or "",
        )
        return interpreted

    @staticmethod
    def _request_handling_mode(result: dict[str, Any]) -> str:
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        handling = dict(proposals.get("request_handling") or {}) if isinstance(proposals.get("request_handling"), dict) else {}
        return str(handling.get("mode") or "").strip().lower()

    @staticmethod
    def _control_action_from_result(result: dict[str, Any]) -> dict[str, Any]:
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        action = proposals.get("control_action")
        return dict(action) if isinstance(action, dict) else {}

    def _cancel_request_by_id(self, request_id: str) -> str:
        request_record = self._load_request(request_id)
        if not request_record:
            return "취소할 request_id를 찾을 수 없습니다."
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
            return warning
        request_record["status"] = "cancelled"
        append_request_event(
            request_record,
            event_type="cancelled",
            actor="orchestrator",
            summary="사용자 요청으로 취소되었습니다.",
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="cancelled",
            summary="사용자 요청으로 취소되었습니다.",
        )
        return "요청을 취소했습니다."

    async def _run_registered_action_for_request(
        self,
        request_record: dict[str, Any],
        *,
        action_name: str,
        params: dict[str, Any],
    ) -> dict[str, str]:
        if not action_name:
            return {"status": "failed", "summary": "action_name이 필요합니다."}
        if action_name not in self.runtime_config.actions:
            return {"status": "failed", "summary": f"등록되지 않은 action입니다: {action_name}"}
        try:
            execution = await asyncio.to_thread(
                self.action_executor.execute,
                request_id=request_record["request_id"],
                action_name=action_name,
                params=params,
            )
        except Exception as exc:
            return {"status": "failed", "summary": str(exc)}
        request_record["operation_id"] = execution["operation_id"]
        request_record["action_status"] = execution["status"]
        append_request_event(
            request_record,
            event_type="action_execute",
            actor="orchestrator",
            summary=f"{action_name} 액션을 실행했습니다.",
            payload=execution,
        )
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="action_execute",
            summary=f"{action_name} 액션을 실행했습니다.",
        )
        return {
            "status": "failed" if str(execution.get("status") or "").strip().lower() == "failed" else "completed",
            "summary": str(execution.get("report") or "액션을 실행했습니다."),
        }

    async def _apply_control_action(self, request_record: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        action = self._control_action_from_result(result)
        if not action:
            return {}
        kind = str(action.get("kind") or "").strip().lower()
        if kind == "sprint_lifecycle":
            command = str(action.get("command") or "").strip().lower()
            milestone_title = str(action.get("milestone_title") or "").strip()
            if command == "start":
                summary = await self.start_sprint_lifecycle(
                    milestone_title,
                    trigger="manual_start",
                    resume_mode="background",
                    started_at=self._request_started_at_hint(request_record),
                    kickoff_brief=str(action.get("kickoff_brief") or "").strip(),
                    kickoff_requirements=_normalize_string_list(action.get("kickoff_requirements")),
                    kickoff_request_text=str(action.get("kickoff_request_text") or "").strip(),
                    kickoff_source_request_id=str(action.get("kickoff_source_request_id") or "").strip(),
                    kickoff_reference_artifacts=_normalize_string_list(action.get("kickoff_reference_artifacts")),
                )
            elif command == "stop":
                summary = await self.stop_sprint_lifecycle(resume_mode="background")
            elif command == "restart":
                summary = await self.restart_sprint_lifecycle(resume_mode="background")
            elif command == "status":
                summary = self.build_sprint_status_message()
            else:
                summary = f"지원하지 않는 sprint lifecycle command입니다: {command or 'N/A'}"
            return {
                "status": "completed",
                "summary": summary,
                "force_complete": True,
            }
        if kind == "cancel_request":
            target_request_id = str(action.get("request_id") or "").strip()
            summary = self._cancel_request_by_id(target_request_id)
            if summary == "취소할 request_id를 찾을 수 없습니다.":
                status = "failed"
                reply_status = "failed"
            elif "취소할 수 없습니다." in summary or "uncommitted 상태" in summary:
                status = "blocked"
                reply_status = "blocked"
            else:
                status = "completed"
                reply_status = "cancelled"
            return {
                "status": status,
                "reply_status": reply_status,
                "request_id": target_request_id,
                "summary": summary,
                "force_complete": True,
            }
        if kind == "execute_action":
            action_name = str(action.get("action_name") or "").strip()
            params = dict(action.get("params") or {}) if isinstance(action.get("params"), dict) else {}
            action_result = await self._run_registered_action_for_request(
                request_record,
                action_name=action_name,
                params=params,
            )
            return {
                "status": str(action_result.get("status") or "completed").strip().lower() or "completed",
                "summary": str(action_result.get("summary") or "").strip(),
                "force_complete": True,
            }
        return {
            "status": "failed",
            "summary": f"지원하지 않는 control_action입니다: {kind or 'N/A'}",
            "force_complete": True,
        }

    async def _apply_role_result(
        self,
        request_record: dict[str, Any],
        result: dict[str, Any],
        *,
        sender_role: str,
    ) -> None:
        append_request_event(
            request_record,
            event_type="role_report",
            actor=str(result.get("role") or sender_role),
            summary=str(result.get("summary") or "역할 보고를 수신했습니다."),
            payload=result,
        )
        self._record_internal_visited_role(
            request_record,
            str(result.get("role") or sender_role),
        )
        request_record["result"] = result
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="role_report",
            summary=str(result.get("summary") or "역할 보고를 수신했습니다."),
            result=result,
        )
        self._record_shared_role_result(request_record, result)
        self._sync_planner_backlog_review_from_role_report(request_record, result)
        self._sync_internal_sprint_artifacts_from_role_report(request_record, result)
        control_outcome = await self._apply_control_action(request_record, result)
        if control_outcome:
            result = dict(result)
            if str(control_outcome.get("summary") or "").strip():
                result["summary"] = str(control_outcome.get("summary") or "").strip()
            if str(control_outcome.get("status") or "").strip():
                result["status"] = str(control_outcome.get("status") or "").strip().lower()
            request_record["result"] = result
        reply_request_id = str(control_outcome.get("request_id") or request_record["request_id"]) if control_outcome else str(request_record["request_id"])
        reply_status_override = (
            str(control_outcome.get("reply_status") or "").strip().lower()
            if control_outcome
            else ""
        )
        force_complete = bool(control_outcome.get("force_complete")) if control_outcome else False
        if not force_complete and self._request_handling_mode(result) == "complete":
            force_complete = True
        if not force_complete:
            result = self._coerce_nonterminal_workflow_role_result(
                request_record,
                result,
                sender_role=sender_role,
            )
            request_record["result"] = result
        result_status = str(result.get("status") or "").strip().lower()
        if result_status in {"failed", "blocked"} or str(result.get("error") or "").strip():
            workflow_terminal_decision = self._derive_workflow_routing_decision(
                request_record,
                result,
                sender_role=sender_role,
            ) or {}
            workflow_state = dict(workflow_terminal_decision.get("workflow_state") or {})
            if workflow_state:
                self._set_request_workflow_state(request_record, workflow_state)
            terminal_status = str(workflow_terminal_decision.get("terminal_status") or "").strip().lower()
            terminal_summary = str(workflow_terminal_decision.get("terminal_summary") or "").strip()
            if terminal_status == "blocked":
                result = dict(result)
                if terminal_summary:
                    result["summary"] = terminal_summary
                result["status"] = "blocked"
                if not str(result.get("error") or "").strip():
                    result["error"] = terminal_summary
                request_record["result"] = result
                result_status = "blocked"
            request_record["status"] = result_status or "failed"
            self._save_request(request_record)
            self._record_internal_sprint_activity(
                request_record,
                event_type=f"request_{request_record['status']}",
                role="orchestrator",
                status=str(request_record.get("status") or ""),
                summary=str(result.get("summary") or result.get("error") or ""),
                payload=result,
            )
            self._append_role_journal(
                "orchestrator",
                request_record,
                title=request_record["status"],
                lines=[
                    f"- role: {result.get('role') or sender_role}",
                    f"- summary: {result.get('summary') or ''}",
                    f"- error: {result.get('error') or '없음'}",
                ],
            )
            if request_record["status"] == "blocked":
                message_text = self._build_requester_status_message(
                    status=reply_status_override or "blocked",
                    request_id=reply_request_id,
                    summary=str(result.get("summary") or result.get("error") or ""),
                )
            else:
                message_text = self._build_requester_status_message(
                    status=reply_status_override or "failed",
                    request_id=reply_request_id,
                    summary=str(result.get("error") or result.get("summary") or ""),
                )
            await self._reply_to_requester(request_record, message_text)
            return
        prior_workflow_state = self._request_workflow_state(request_record)
        if force_complete:
            routing_decision = {"next_role": "", "routing_context": {}}
        else:
            routing_decision = self._derive_routing_decision_after_report(
                request_record,
                result,
                sender_role=sender_role,
            )
        workflow_state = dict(routing_decision.get("workflow_state") or {})
        if workflow_state:
            self._set_request_workflow_state(request_record, workflow_state)
            request_record["result"] = result
        terminal_status = str(routing_decision.get("terminal_status") or "").strip().lower()
        terminal_summary = str(routing_decision.get("terminal_summary") or "").strip()
        if (
            terminal_status != "blocked"
            and str(result.get("role") or sender_role).strip().lower() == "planner"
            and str(prior_workflow_state.get("phase") or "").strip().lower() == WORKFLOW_PHASE_PLANNING
            and str(routing_decision.get("next_role") or "").strip().lower() != "planner"
        ):
            sprint_id = str(request_record.get("sprint_id") or "").strip()
            sprint_state = self._load_sprint_state(sprint_id) if sprint_id else {}
            if sprint_state:
                try:
                    await self._send_sprint_spec_todo_report(
                        sprint_state,
                        title="♻️ 스프린트 Spec/TODO 재보고",
                        judgment="planner 결과를 반영한 canonical spec/todo를 다시 보고했습니다.",
                        next_action=(
                            f"{routing_decision.get('next_role') or 'closeout'} 진행"
                            if str(routing_decision.get("next_role") or "").strip()
                            else "planner 결과 closeout"
                        ),
                        swallow_exceptions=False,
                    )
                except Exception as exc:
                    terminal_status = "blocked"
                    terminal_summary = (
                        "planner 결과는 준비됐지만 spec/todo 재보고 전송에 실패했습니다. "
                        f"{str(exc).strip()}"
                    ).strip()
                    blocked_state = dict(self._request_workflow_state(request_record) or self._default_workflow_state())
                    blocked_state["phase"] = WORKFLOW_PHASE_PLANNING
                    blocked_state["step"] = WORKFLOW_STEP_PLANNER_FINALIZE
                    blocked_state["phase_owner"] = "planner"
                    blocked_state["phase_status"] = "blocked"
                    self._set_request_workflow_state(request_record, blocked_state)
                    result = dict(result)
                    result["status"] = "blocked"
                    result["summary"] = terminal_summary
                    result["error"] = terminal_summary
                    request_record["result"] = result
        if terminal_status == "blocked":
            result = dict(result)
            if terminal_summary:
                result["summary"] = terminal_summary
            result["status"] = "blocked"
            if not str(result.get("error") or "").strip():
                result["error"] = terminal_summary
            request_record["result"] = result
            request_record["status"] = "blocked"
            self._save_request(request_record)
            self._record_internal_sprint_activity(
                request_record,
                event_type="request_blocked",
                role="orchestrator",
                status="blocked",
                summary=str(result.get("summary") or result.get("error") or ""),
                payload=result,
            )
            self._append_role_journal(
                "orchestrator",
                request_record,
                title="blocked",
                lines=[
                    f"- role: {result.get('role') or sender_role}",
                    f"- summary: {result.get('summary') or ''}",
                    f"- error: {result.get('error') or '없음'}",
                ],
            )
            await self._reply_to_requester(
                request_record,
                self._build_requester_status_message(
                    status=reply_status_override or "blocked",
                    request_id=reply_request_id,
                    summary=str(result.get("summary") or result.get("error") or ""),
                ),
            )
            return
        next_role = str(routing_decision.get("next_role") or "").strip()
        if next_role:
            request_record["status"] = "delegated"
            request_record["current_role"] = next_role
            request_record["next_role"] = next_role
            request_record["routing_context"] = dict(routing_decision.get("routing_context") or {})
            self._save_request(request_record)
            self._record_internal_sprint_activity(
                request_record,
                event_type="role_delegated",
                role="orchestrator",
                status=str(request_record.get("status") or ""),
                summary=f"{next_role} 역할로 다시 위임했습니다.",
                payload=self._build_internal_sprint_delegation_payload(request_record, next_role),
            )
            self._append_role_history(
                "orchestrator",
                request_record,
                event_type="delegated",
                summary=(
                    f"{next_role} 역할로 다시 위임했습니다. "
                    f"{request_record['routing_context'].get('reason') or ''}"
                ).strip(),
                result=result,
            )
            relay_sent = await self._delegate_request(request_record, next_role)
            await self._reply_to_requester(
                request_record,
                self._build_requester_status_message(
                    status="delegated" if relay_sent else "failed",
                    request_id=reply_request_id,
                    summary=(
                        f"{next_role} 역할로 전달했습니다."
                        if relay_sent
                        else f"{next_role} relay 전송이 실패해 요청 전달을 완료하지 못했습니다."
                    ),
                ),
            )
            return
        request_record["status"] = "completed"
        request_record["current_role"] = "orchestrator"
        request_record["next_role"] = ""
        self._save_request(request_record)
        self._record_internal_sprint_activity(
            request_record,
            event_type="request_completed",
            role="orchestrator",
            status="completed",
            summary=str(result.get("summary") or "요청이 완료되었습니다."),
            payload=result,
        )
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="completed",
            summary=str(result.get("summary") or "요청이 완료되었습니다."),
            result=result,
        )
        resumed_request_ids = await self._resume_requests_from_verification_result(request_record, result)
        await self._reply_to_requester(
            request_record,
            self._build_requester_status_message(
                status=reply_status_override or "completed",
                request_id=reply_request_id,
                summary=str(result.get("summary") or ""),
                related_request_ids=resumed_request_ids,
            ),
        )

    async def _run_local_orchestrator_request(self, request_record: dict[str, Any]) -> None:
        request_id = str(request_record.get("request_id") or "").strip()
        if not request_id or not await self._claim_request(request_id):
            LOGGER.info("Skipping local orchestrator request already in progress: %s", request_id or "unknown")
            return
        try:
            envelope = self._build_delegate_envelope(
                request_record,
                "orchestrator",
                extra_params={"_teams_kind": "local_delegate"},
            )
            try:
                result = await asyncio.to_thread(self.role_runtime.run_task, envelope, request_record)
            except Exception as exc:
                LOGGER.exception("Local orchestrator request failed while processing %s", request_id)
                result = {
                    "request_id": request_record["request_id"],
                    "role": "orchestrator",
                    "status": "failed",
                    "summary": "orchestrator 역할 처리 중 오류가 발생했습니다.",
                    "proposals": {},
                    "artifacts": [],
                    "next_role": "",
                    "error": str(exc),
                }
            result = normalize_role_payload(result)
            await self._apply_role_result(
                request_record,
                result,
                sender_role="orchestrator",
            )
        finally:
            await self._release_request(request_id)

    async def _handle_delegated_request(self, message: DiscordMessage, envelope: MessageEnvelope) -> None:
        request_record = self._load_request(envelope.request_id or "")
        if not request_record:
            return
        request_id = str(request_record.get("request_id") or "").strip()
        if not request_id or not await self._claim_request(request_id):
            LOGGER.info("Skipping delegated request already in progress for role %s: %s", self.role, request_id or "unknown")
            return
        try:
            await self._process_delegated_request(envelope, request_record)
        finally:
            await self._release_request(request_id)

    async def _process_delegated_request(self, envelope: MessageEnvelope, request_record: dict[str, Any]) -> None:
        self._record_internal_sprint_activity(
            request_record,
            event_type="role_started",
            role=self.role,
            status="running",
            summary=(
                f"{self._initial_phase_step_title(self._initial_phase_step(request_record))}을 시작했습니다."
                if self._is_initial_phase_planner_request(request_record)
                else f"{self.role} 역할이 요청 처리를 시작했습니다."
            ),
        )
        await self._maybe_report_planner_initial_phase_activity(
            request_record,
            event_type="role_started",
            status="running",
            summary=(
                f"{self._initial_phase_step_title(self._initial_phase_step(request_record))}을 시작했습니다."
                if self._is_initial_phase_planner_request(request_record)
                else f"{self.role} 역할이 요청 처리를 시작했습니다."
            ),
        )
        try:
            result = await asyncio.to_thread(self.role_runtime.run_task, envelope, request_record)
        except Exception as exc:
            LOGGER.exception("Role %s failed while processing request %s", self.role, request_record.get("request_id"))
            result = {
                "request_id": request_record["request_id"],
                "role": self.role,
                "status": "failed",
                "summary": f"{self.role} 역할 처리 중 오류가 발생했습니다.",
                "proposals": {},
                "artifacts": [],
                "next_role": "",
                "error": str(exc),
            }
        result = normalize_role_payload(result)
        request_record["result"] = dict(result)
        self._persist_request_result(request_record)
        self._record_internal_sprint_activity(
            request_record,
            event_type="role_result",
            role=str(result.get("role") or self.role),
            status=str(result.get("status") or ""),
            summary=str(result.get("summary") or result.get("error") or ""),
            payload=result,
        )
        result_status = str(result.get("status") or "").strip().lower()
        if self._is_initial_phase_planner_request(request_record):
            report_event_type = "planner_checkpoint" if result_status in {"completed", "committed"} else "role_result"
            await self._maybe_report_planner_initial_phase_activity(
                request_record,
                event_type=report_event_type,
                status=result_status or "completed",
                summary=str(result.get("summary") or result.get("error") or ""),
                payload=result,
            )
        self._append_role_history(
            self.role,
            request_record,
            event_type="role_result",
            summary=str(result.get("summary") or ""),
            result=result,
        )
        insights = _normalize_insights(result)
        if insights:
            self._append_role_journal(
                self.role,
                request_record,
                title="insights",
                lines=[f"- {insight}" for insight in insights],
            )
        if str(result.get("status") or "").strip().lower() in {"failed", "blocked"} or str(result.get("error") or "").strip():
            self._append_role_journal(
                self.role,
                request_record,
                title="role_result",
                lines=[
                    f"- status: {result.get('status') or ''}",
                    f"- summary: {result.get('summary') or ''}",
                    f"- error: {result.get('error') or '없음'}",
                ],
            )
        result_envelope = MessageEnvelope(
            request_id=request_record["request_id"],
            sender=self.role,
            target="orchestrator",
            intent="report",
            urgency=envelope.urgency,
            scope=request_record["scope"],
            artifacts=[str(item) for item in result.get("artifacts") or []],
            params={
                "_teams_kind": "report",
                "result": result,
            },
            body=json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True),
        )
        await self._send_relay(result_envelope)

    async def _handle_role_report(self, message: DiscordMessage, envelope: MessageEnvelope) -> None:
        request_record = self._load_request(envelope.request_id or "")
        if not request_record:
            return
        result = envelope.params.get("result") if isinstance(envelope.params.get("result"), dict) else {}
        if not result:
            result = _parse_report_body_json(envelope.body)
        if not result:
            persisted_result = dict(request_record.get("result") or {}) if isinstance(request_record.get("result"), dict) else {}
            if (
                str(persisted_result.get("request_id") or "").strip() == str(request_record.get("request_id") or "").strip()
                and (
                    not str(envelope.sender or "").strip()
                    or str(persisted_result.get("role") or "").strip() == str(envelope.sender or "").strip()
                )
            ):
                result = persisted_result
        if result:
            result = normalize_role_payload(result)
        if result.get("approval_needed") or str(result.get("status") or "").strip().lower() == "awaiting_approval":
            result = dict(result)
            compatibility_summary = " ".join(
                part
                for part in (
                    str(result.get("summary") or "").strip(),
                    "approval flow is no longer supported in teams_runtime.",
                )
                if part
            ).strip()
            existing_error = str(result.get("error") or "").strip()
            compatibility_error = "approval flow is no longer supported"
            result["status"] = "blocked"
            result["summary"] = compatibility_summary or compatibility_error
            result["error"] = (
                f"{existing_error}; {compatibility_error}"
                if existing_error and compatibility_error not in existing_error
                else (existing_error or compatibility_error)
            )
            result["next_role"] = ""
            result.pop("approval_needed", None)
        stale_reason = self._stale_role_report_reason(request_record, envelope, result)
        if stale_reason:
            LOGGER.info(
                "Ignoring stale role report for request %s from %s: %s",
                request_record.get("request_id") or "unknown",
                str(envelope.sender or result.get("role") or "unknown").strip() or "unknown",
                stale_reason,
            )
            return
        result = self._enforce_workflow_role_report_contract(request_record, result)
        await self._apply_role_result(
            request_record,
            result,
            sender_role=str(envelope.sender or ""),
        )

    async def _forward_user_request(self, message: DiscordMessage, envelope: MessageEnvelope) -> None:
        request_id = envelope.request_id or new_request_id()
        forwarded_envelope = MessageEnvelope(
            request_id=request_id,
            sender=self.role,
            target="orchestrator",
            intent=envelope.intent,
            urgency=envelope.urgency,
            scope=envelope.scope,
            artifacts=envelope.artifacts,
            params={
                **dict(envelope.params),
                "_teams_kind": "forward",
                "requester_author_id": message.author_id,
                "requester_author_name": message.author_name,
                "requester_channel_id": message.channel_id,
                "requester_guild_id": message.guild_id or "",
                "requester_is_dm": message.is_dm,
                "requester_message_id": message.message_id,
            },
            body=envelope.body,
        )
        await self._send_relay(forwarded_envelope)

    async def _reply_status_request(self, message: DiscordMessage, envelope: MessageEnvelope) -> None:
        if not envelope.request_id:
            scope_text = str(envelope.scope or envelope.body or "").strip().lower()
            if scope_text == "sprint":
                scheduler_state = self._load_scheduler_state()
                active_sprint = self._load_sprint_state(str(scheduler_state.get("active_sprint_id") or ""))
                is_active = bool(active_sprint)
                if not active_sprint:
                    sprint_files = sorted(self.paths.sprints_dir.glob("*.json"))
                    active_sprint = read_json(sprint_files[-1]) if sprint_files else {}
                if not active_sprint:
                    await self._send_channel_reply(message, "기록된 sprint가 없습니다.")
                    return
                await self._send_channel_reply(
                    message,
                    self._render_sprint_status_report(
                        active_sprint,
                        is_active=is_active,
                        scheduler_state=scheduler_state,
                    ),
                )
                return
            if scope_text == "backlog":
                await self._send_channel_reply(message, self._render_backlog_status_report())
                return
        request_record = self._load_request(envelope.request_id or "")
        if not request_record:
            await self._send_channel_reply(message, "해당 request_id를 찾을 수 없습니다.")
            return
        lines = [
            f"request_id={request_record['request_id']}",
            f"status={request_record.get('status') or 'unknown'}",
            f"current_role={request_record.get('current_role') or 'N/A'}",
            self._format_sprint_scope(sprint_id=str(request_record.get("sprint_id") or "")),
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
            operation = self.action_executor.get_operation_status(str(request_record["operation_id"]))
            if operation:
                lines.append(f"operation_status={operation.get('status')}")
        await self._send_channel_reply(message, "\n".join(lines))

    async def _cancel_request(self, message: DiscordMessage, envelope: MessageEnvelope) -> None:
        request_record = self._load_request(envelope.request_id or "")
        if not request_record:
            await self._send_channel_reply(message, "취소할 request_id를 찾을 수 없습니다.")
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
            await self._reply_to_requester(
                request_record,
                self._build_requester_status_message(
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
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="cancelled",
            summary="사용자 요청으로 취소되었습니다.",
        )
        await self._reply_to_requester(
            request_record,
            self._build_requester_status_message(
                status="cancelled",
                request_id=str(request_record["request_id"]),
                summary="요청을 취소했습니다.",
            ),
        )

    async def _execute_registered_action(self, message: DiscordMessage, envelope: MessageEnvelope) -> None:
        action_name = str(envelope.params.get("action_name") or "").strip()
        if not action_name:
            await self._send_channel_reply(message, "action_name이 필요합니다.")
            return
        if action_name not in self.runtime_config.actions:
            await self._send_channel_reply(message, f"등록되지 않은 action입니다: {action_name}")
            return
        action = self.runtime_config.actions[action_name]
        request_record = self._create_request_record(message, envelope, forwarded=False)
        execution = await asyncio.to_thread(
            self.action_executor.execute,
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
        self._save_request(request_record)
        self._append_role_history(
            "orchestrator",
            request_record,
            event_type="action_execute",
            summary=f"{action_name} 액션을 실행했습니다.",
        )
        await self._reply_to_requester(request_record, execution.get("report") or "액션을 실행했습니다.")

    async def _delegate_request(self, request_record: dict[str, Any], next_role: str) -> bool:
        delegation_context = self._build_delegation_context(request_record, next_role)
        snapshot_path = self._write_role_request_snapshot(next_role, request_record, delegation_context)
        if snapshot_path:
            delegation_context["snapshot_path"] = snapshot_path
        request_record["delegation_context"] = dict(delegation_context)
        self._save_request(request_record)
        envelope = self._build_delegate_envelope(
            request_record,
            next_role,
            delegation_context=delegation_context,
        )
        return await self._send_relay(envelope, request_record=request_record)

    def _record_relay_delivery(
        self,
        request_record: dict[str, Any],
        *,
        status: str,
        target_description: str,
        attempts: int,
        error: str,
        envelope: MessageEnvelope,
    ) -> None:
        request_record["relay_send_status"] = str(status or "").strip()
        request_record["relay_send_target"] = str(target_description or "").strip()
        request_record["relay_send_attempts"] = int(attempts or 0)
        request_record["relay_send_error"] = str(error or "").strip()
        request_record["relay_send_updated_at"] = utc_now_iso()
        if status == "failed":
            append_request_event(
                request_record,
                event_type="relay_send_failed",
                actor="orchestrator",
                summary=f"relay 채널 전송이 실패했습니다. target={target_description}",
                payload={
                    "target": target_description,
                    "attempts": int(attempts or 0),
                    "error": str(error or "").strip(),
                    "envelope_target": str(envelope.target or "").strip(),
                    "intent": str(envelope.intent or "").strip(),
                    "scope": _truncate_text(str(envelope.scope or "").strip(), limit=120),
                },
            )
            self._append_role_history(
                "orchestrator",
                request_record,
                event_type="relay_send_failed",
                summary=f"relay 채널 전송이 실패했습니다. target={target_description}",
            )
        self._save_request(request_record)

    def _build_internal_relay_record_id(self, envelope: MessageEnvelope) -> str:
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

    def _enqueue_internal_relay(self, envelope: MessageEnvelope) -> str:
        target_role = str(envelope.target or "").strip()
        if target_role not in TEAM_ROLES:
            raise ValueError(f"Unsupported internal relay target role: {target_role or 'unknown'}")
        relay_id = self._build_internal_relay_record_id(envelope)
        inbox_dir = self._internal_relay_inbox_dir(target_role)
        inbox_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "relay_id": relay_id,
            "transport": RELAY_TRANSPORT_INTERNAL,
            "created_at": utc_now_iso(),
            "sender_role": self.role,
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

    def _archive_internal_relay_file(self, relay_file: Path, *, invalid: bool = False) -> None:
        archive_dir = self._internal_relay_archive_dir(self.role)
        archive_dir.mkdir(parents=True, exist_ok=True)
        stem = relay_file.stem + ("-invalid" if invalid else "")
        destination = archive_dir / f"{stem}{relay_file.suffix or '.json'}"
        suffix = 1
        while destination.exists():
            destination = archive_dir / f"{stem}-{suffix}{relay_file.suffix or '.json'}"
            suffix += 1
        relay_file.replace(destination)

    @staticmethod
    def _deserialize_internal_relay_envelope(payload: Any) -> MessageEnvelope | None:
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

    def _build_internal_relay_message_stub(
        self,
        envelope: MessageEnvelope,
        *,
        relay_id: str = "",
    ) -> DiscordMessage:
        sender_role = str(envelope.sender or "").strip()
        requester = _extract_original_requester(dict(envelope.params or {}))
        sender_bot_id = ""
        sender_config = self.discord_config.agents.get(sender_role)
        if sender_config is not None:
            sender_bot_id = str(sender_config.bot_id or "").strip()
        is_dm = bool(requester.get("is_dm")) if "is_dm" in requester else False
        guild_id = str(requester.get("guild_id") or "").strip()
        return DiscordMessage(
            message_id=relay_id or f"internal-{self.role}-{int(time.time() * 1000)}",
            channel_id=str(requester.get("channel_id") or self.discord_config.relay_channel_id),
            guild_id=None if is_dm else (guild_id or "internal-relay"),
            author_id=str(requester.get("author_id") or sender_bot_id or "internal-relay"),
            author_name=str(requester.get("author_name") or sender_role or "internal-relay"),
            content=envelope_to_text(envelope),
            is_dm=is_dm,
            mentions_bot=False,
            created_at=datetime.now(UTC),
        )

    async def _process_internal_relay_envelope(
        self,
        envelope: MessageEnvelope,
        *,
        relay_id: str = "",
    ) -> None:
        kind = str(envelope.params.get("_teams_kind") or "").strip()
        synthetic_message = self._build_internal_relay_message_stub(envelope, relay_id=relay_id)
        if self.role == "orchestrator":
            if kind == "report":
                await self._handle_role_report(synthetic_message, envelope)
                return
            if kind == "forward":
                await self._handle_user_request(synthetic_message, envelope, forwarded=True)
                return
            self._log_malformed_trusted_relay(reason="unsupported internal relay kind for orchestrator", kind=kind)
            return
        if kind != "delegate":
            self._log_malformed_trusted_relay(reason="missing internal delegate kind", kind=kind)
            return
        if envelope.target != self.role:
            return
        await self._handle_delegated_request(synthetic_message, envelope)

    async def _consume_internal_relay_once(self) -> None:
        inbox_dir = self._internal_relay_inbox_dir(self.role)
        if not inbox_dir.exists():
            return
        relay_files = sorted(inbox_dir.glob("*.json"))
        for relay_file in relay_files:
            payload = read_json(relay_file)
            if not payload:
                self._archive_internal_relay_file(relay_file, invalid=True)
                continue
            envelope = self._deserialize_internal_relay_envelope(payload.get("envelope"))
            if envelope is None:
                self._archive_internal_relay_file(relay_file, invalid=True)
                continue
            try:
                await self._process_internal_relay_envelope(
                    envelope,
                    relay_id=str(payload.get("relay_id") or relay_file.stem),
                )
            except Exception:
                LOGGER.exception(
                    "Failed to process internal relay envelope for role %s (file=%s)",
                    self.role,
                    relay_file,
                )
                return
            self._archive_internal_relay_file(relay_file)

    async def _consume_internal_relay_loop(self) -> None:
        while True:
            try:
                await self._consume_internal_relay_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Internal relay consumer loop failed for role %s", self.role)
            await asyncio.sleep(INTERNAL_REQUEST_POLL_SECONDS)

    @staticmethod
    def _parse_json_payload_from_text(raw_text: str) -> Any:
        normalized = str(raw_text or "").strip()
        if not normalized:
            return {}
        try:
            return json.loads(normalized)
        except json.JSONDecodeError:
            parsed_dict = _parse_report_body_json(normalized)
            if parsed_dict:
                return parsed_dict
        return {}

    @staticmethod
    def _relay_summary_text_fragments(
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

    @staticmethod
    def _append_report_section(
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

    @classmethod
    def _relay_report_sections_from_lines(
        cls,
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
                cls._append_report_section(sections, current_title, current_lines)
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
                    remainder = stripped[len(prefix):].strip()
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

    @staticmethod
    def _render_report_sections_message(
        header: str,
        sections: Iterable[ReportSection] | None,
        *,
        max_inner_width: int = 96,
    ) -> str:
        rendered_sections = render_report_sections(sections, max_inner_width=max_inner_width)
        parts = [str(header or "").strip(), str(rendered_sections or "").strip()]
        return "\n".join(part for part in parts if part).strip()

    @staticmethod
    def _extract_semantic_leaf_lines(
        value: Any,
        *,
        prefix: str = "",
        skip_keys: set[str] | None = None,
    ) -> list[str]:
        excluded = set(skip_keys or set())
        lines: list[str] = []
        if isinstance(value, dict):
            for key, item in value.items():
                normalized_key = str(key or "").strip()
                if not normalized_key or normalized_key in excluded:
                    continue
                label = normalized_key.replace("_", " ")
                if isinstance(item, (dict, list, tuple, set)):
                    next_prefix = f"{prefix}{label}: " if normalized_key not in {"details", "items", "rules"} else prefix
                    lines.extend(
                        TeamService._extract_semantic_leaf_lines(
                            item,
                            prefix=next_prefix,
                            skip_keys=excluded,
                        )
                    )
                    continue
                text = _collapse_whitespace(item)
                if not text:
                    continue
                if text.lower() in {"true", "false"} and normalized_key in {"finalize_phase"}:
                    continue
                lines.append(f"{prefix}{label}: {text}" if prefix or label else text)
            return lines
        if isinstance(value, (list, tuple, set)):
            for item in value:
                lines.extend(TeamService._extract_semantic_leaf_lines(item, prefix=prefix, skip_keys=excluded))
            return lines
        text = _collapse_whitespace(value)
        return [f"{prefix}{text}" if prefix else text] if text else []

    def _proposal_semantic_details(
        self,
        proposals: dict[str, Any],
        *,
        payload_names: tuple[str, ...],
        transition: dict[str, Any],
    ) -> dict[str, list[str]]:
        skip_keys = {
            "requested_role",
            "target_phase",
            "target_step",
            "finalize_phase",
            "outcome",
            "reopen_category",
        }
        what_details: list[str] = []
        how_details: list[str] = []
        why_details: list[str] = []
        for payload_name in payload_names:
            payload = proposals.get(payload_name)
            if not isinstance(payload, dict):
                continue
            if payload_name == "implementation_guidance":
                for key, label in (
                    ("evaluation_order", "평가 순서: "),
                    ("state_transitions", "상태 전이: "),
                    ("triggered_conditions", "발동 조건: "),
                    ("suppressed_conditions", "억제 조건: "),
                    ("fail_closed_conditions", "실패 시 차단: "),
                    ("decision_rules", "판단 규칙: "),
                    ("contracts", "구현 계약: "),
                ):
                    what_details.extend(
                        self._extract_semantic_leaf_lines(payload.get(key), prefix=label, skip_keys=skip_keys)
                    )
                for key in ("implementation_steps", "file_changes", "test_follow_up", "validation_steps"):
                    how_details.extend(
                        self._extract_semantic_leaf_lines(payload.get(key), prefix="", skip_keys=skip_keys)
                    )
                for key in ("reasoning", "decision_rationale", "guardrails", "risks", "invariants"):
                    why_details.extend(
                        self._extract_semantic_leaf_lines(payload.get(key), prefix="", skip_keys=skip_keys)
                    )
            elif payload_name in {"code_review", "verification_result", "qa_validation"}:
                what_details.extend(
                    self._extract_semantic_leaf_lines(payload.get("findings"), prefix="finding: ", skip_keys=skip_keys)
                )
                how_details.extend(
                    self._extract_semantic_leaf_lines(payload.get("passed_checks"), prefix="check: ", skip_keys=skip_keys)
                )
                why_details.extend(
                    self._extract_semantic_leaf_lines(payload.get("residual_risks"), prefix="risk: ", skip_keys=skip_keys)
                )
            elif payload_name == "design_feedback":
                entry_point_label = _designer_entry_point_label(payload.get("entry_point"))
                if entry_point_label:
                    what_details.append(f"판단 지점: {entry_point_label}")
                what_details.extend(
                    self._extract_semantic_leaf_lines(payload.get("rules"), prefix="", skip_keys=skip_keys)
                )
                what_details.extend(
                    self._extract_semantic_leaf_lines(payload.get("user_judgment"), prefix="UX 판단: ", skip_keys=skip_keys)
                )
                how_details.extend(_design_feedback_priority_lines(payload.get("message_priority")))
                how_details.extend(
                    self._extract_semantic_leaf_lines(payload.get("required_inputs"), prefix="추가 입력: ", skip_keys=skip_keys)
                )
                how_details.extend(
                    self._extract_semantic_leaf_lines(payload.get("acceptance_criteria"), prefix="완료 기준: ", skip_keys=skip_keys)
                )
                why_details.extend(
                    self._extract_semantic_leaf_lines(payload.get("routing_rationale"), prefix="", skip_keys=skip_keys)
                )
            elif payload_name == "planning_contract":
                support_roles = _planning_support_role_entries(payload.get("selected_support_roles"))
                if support_roles:
                    what_details.append(
                        "지원 역할: " + ", ".join(entry["role"] for entry in support_roles[:3])
                    )
                    how_details.extend(
                        f"{entry['role']}: {entry['rationale']}"
                        for entry in support_roles
                        if entry["rationale"]
                    )
                    why_details.extend(
                        f"{entry['role']} 협업: {entry['collaboration']}"
                        for entry in support_roles
                        if entry["collaboration"]
                    )
                what_details.extend(
                    self._extract_semantic_leaf_lines(
                        payload.get("role_combination_rules"),
                        prefix="협업 경계: ",
                        skip_keys=skip_keys,
                    )
                )
            else:
                what_details.extend(self._extract_semantic_leaf_lines(payload, prefix="", skip_keys=skip_keys))
        if transition.get("unresolved_items"):
            what_details.extend(
                f"남은 과제: {item}"
                for item in transition.get("unresolved_items") or []
                if _collapse_whitespace(item)
            )
        if transition.get("reason"):
            why_details.append(_collapse_whitespace(transition.get("reason") or ""))
        return {
            "what_details": _dedupe_preserving_order([item for item in what_details if _collapse_whitespace(item)])[:4],
            "how_details": _dedupe_preserving_order([item for item in how_details if _collapse_whitespace(item)])[:3],
            "why_details": _dedupe_preserving_order([item for item in why_details if _collapse_whitespace(item)])[:3],
        }

    @staticmethod
    def _planner_backlog_titles(proposals: dict[str, Any], *, limit: int = 3) -> list[str]:
        items: list[str] = []
        backlog_item = proposals.get("backlog_item")
        if isinstance(backlog_item, dict):
            title = _collapse_whitespace(backlog_item.get("title") or backlog_item.get("scope") or backlog_item.get("summary") or "")
            if title:
                items.append(title)
        backlog_items = proposals.get("backlog_items")
        if isinstance(backlog_items, list):
            for item in backlog_items:
                if not isinstance(item, dict):
                    continue
                title = _collapse_whitespace(item.get("title") or item.get("scope") or item.get("summary") or "")
                if title:
                    items.append(title)
        return _dedupe_preserving_order(items)[:limit]

    @staticmethod
    def _planner_doc_targets(proposals: dict[str, Any], *, limit: int = 3) -> list[str]:
        payload = proposals.get("sprint_plan_update")
        if not isinstance(payload, dict):
            return []
        lines: list[str] = []
        for key in ("updated_docs", "synced_artifacts", "touched_sections"):
            value = payload.get(key)
            if isinstance(value, list):
                for item in value:
                    normalized = _collapse_whitespace(item)
                    if normalized:
                        lines.append(normalized)
        return _dedupe_preserving_order(lines)[:limit]

    def _build_role_result_semantic_context(self, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(result, dict) or not result:
            return {
                "from_role": "",
                "what_summary": "",
                "what_details": [],
                "how_details": [],
                "how_summary": "",
                "why_summary": "",
                "route_reason": "",
                "latest_context_summary": "",
                "context_points": [],
                "reference_artifacts": [],
            }
        role = str(result.get("role") or "").strip()
        proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}
        payload_names = (
            "planning_contract",
            "planning_package",
            "design_feedback",
            "implementation_guidance",
            "implementation_report",
            "code_review",
            "verification_result",
            "qa_validation",
            "sprint_plan_update",
        )
        transition = self._workflow_transition(result)
        result_artifacts = [
            str(item).strip()
            for item in (result.get("artifacts") or [])
            if str(item).strip()
        ]
        acceptance_criteria = self._normalize_backlog_acceptance_criteria(
            _extract_proposal_acceptance_criteria(proposals)
        )[:3]
        required_inputs = _extract_proposal_required_inputs(proposals)[:3]
        unresolved_items = [
            str(item).strip()
            for item in (transition.get("unresolved_items") or [])
            if str(item).strip()
        ][:3]
        findings = self._proposal_nested_string_list(proposals, payload_names, "findings")[:3]
        residual_risks = self._proposal_nested_string_list(proposals, payload_names, "residual_risks")[:3]
        passed_checks = self._proposal_nested_string_list(proposals, payload_names, "passed_checks")[:3]
        backlog_count = 0
        if isinstance(proposals.get("backlog_items"), list):
            backlog_count = len(
                [
                    item
                    for item in (proposals.get("backlog_items") or [])
                    if isinstance(item, dict) or str(item).strip()
                ]
            )
        elif isinstance(proposals.get("backlog_item"), (dict, str)):
            backlog_count = 1

        semantic_details = self._proposal_semantic_details(
            proposals,
            payload_names=payload_names,
            transition=transition,
        )
        what_details = list(semantic_details.get("what_details") or [])
        how_details = list(semantic_details.get("how_details") or [])
        why_details = list(semantic_details.get("why_details") or [])
        planner_backlog_titles = self._planner_backlog_titles(proposals)
        planner_doc_targets = self._planner_doc_targets(proposals)
        planning_contract = (
            proposals.get("planning_contract")
            if isinstance(proposals.get("planning_contract"), dict)
            else {}
        )
        support_role_entries = _planning_support_role_entries(planning_contract.get("selected_support_roles"))
        support_role_names = [entry["role"] for entry in support_role_entries[:3]]
        revised_milestone_title = _collapse_whitespace(proposals.get("revised_milestone_title") or "")
        design_feedback = proposals.get("design_feedback") if isinstance(proposals.get("design_feedback"), dict) else {}
        designer_entry_point = _designer_entry_point_label(design_feedback.get("entry_point"))
        designer_judgments = _normalize_string_list(design_feedback.get("user_judgment"))[:3]
        designer_message_priority = _design_feedback_priority_lines(design_feedback.get("message_priority"))[:3]
        designer_routing_rationale = _collapse_whitespace(design_feedback.get("routing_rationale") or "")
        if role == "planner":
            planner_details: list[str] = []
            if revised_milestone_title:
                planner_details.append(f"마일스톤: {revised_milestone_title}")
            if support_role_names:
                planner_details.append("지원 역할: " + ", ".join(support_role_names))
            planner_details.extend(f"backlog/todo: {item}" for item in planner_backlog_titles[:3])
            what_details = _dedupe_preserving_order(planner_details + what_details)[:4]
        elif role == "designer":
            designer_details: list[str] = []
            if designer_entry_point:
                designer_details.append(f"판단 지점: {designer_entry_point}")
            designer_details.extend(f"UX 판단: {item}" for item in designer_judgments[:2])
            designer_details.extend(designer_message_priority[:2])
            what_details = _dedupe_preserving_order(designer_details + what_details)[:4]

        what_summary = _collapse_whitespace(result.get("summary") or "")
        if _looks_meta_summary(what_summary) and what_details:
            what_summary = ""
        if (
            role == "planner"
            and what_summary
            and (revised_milestone_title or planner_backlog_titles)
            and any(marker in what_summary for marker in ("정리했습니다", "확정했습니다", "구조화했습니다"))
        ):
            what_summary = ""
        if not what_summary:
            if role == "planner":
                if revised_milestone_title and planner_backlog_titles:
                    what_summary = f"마일스톤을 {revised_milestone_title}로 정리하고 backlog/todo {len(planner_backlog_titles)}건을 확정했습니다."
                elif revised_milestone_title:
                    what_summary = f"마일스톤을 {revised_milestone_title}로 정리했습니다."
                elif planner_backlog_titles:
                    what_summary = f"실행 backlog/todo {len(planner_backlog_titles)}건을 정리했습니다."
                elif backlog_count > 0:
                    what_summary = f"마일스톤 기준 backlog/todo {backlog_count}건을 정리했습니다."
                elif support_role_names:
                    what_summary = f"designer 보조 역할 {', '.join(support_role_names[:2])} 조합을 정리했습니다."
                elif required_inputs:
                    what_summary = f"planning 진행에 필요한 추가 입력 {len(required_inputs)}건을 정리했습니다."
                elif acceptance_criteria:
                    what_summary = f"planning 완료 기준 {len(acceptance_criteria)}건을 정리했습니다."
            elif role == "designer":
                if designer_entry_point and designer_judgments:
                    what_summary = f"{designer_entry_point} 관점 UX 판단 {len(designer_judgments)}건을 정리했습니다."
                elif designer_entry_point:
                    what_summary = f"{designer_entry_point} 관점 advisory를 정리했습니다."
                elif designer_judgments:
                    what_summary = f"UX 판단 {len(designer_judgments)}건을 정리했습니다."
                elif required_inputs:
                    what_summary = f"UX 검토에 필요한 추가 입력 {len(required_inputs)}건을 정리했습니다."
                elif acceptance_criteria:
                    what_summary = f"UX 완료 기준 {len(acceptance_criteria)}건을 정리했습니다."
            elif role == "architect":
                if findings:
                    what_summary = f"구조 검토 결과 핵심 finding {len(findings)}건을 남겼습니다."
                elif what_details:
                    what_summary = what_details[0]
                elif str(transition.get("target_step") or "").strip() == WORKFLOW_STEP_DEVELOPER_BUILD:
                    what_summary = "구현 전 구조 guidance를 정리했습니다."
                elif str(transition.get("target_step") or "").strip() == WORKFLOW_STEP_DEVELOPER_REVISION:
                    what_summary = "구조 리뷰를 마쳤고 developer 반영이 필요합니다."
            elif role == "developer":
                if result_artifacts:
                    what_summary = f"구현 변경/산출물 {len(result_artifacts)}건을 남겼습니다."
                elif str(transition.get("target_step") or "").strip() == WORKFLOW_STEP_ARCHITECT_REVIEW:
                    what_summary = "구현을 마치고 architect review로 넘길 준비를 마쳤습니다."
            elif role == "qa":
                if findings:
                    what_summary = f"검증 결과 핵심 finding {len(findings)}건을 확인했습니다."
                elif passed_checks:
                    what_summary = f"검증 통과 근거 {len(passed_checks)}건을 남겼습니다."
        if not what_summary:
            if what_details:
                what_summary = what_details[0]
            else:
                what_summary = _collapse_whitespace(transition.get("reason") or _summarize_proposals(proposals))

        context_points: list[str] = []
        if role == "planner":
            if revised_milestone_title:
                context_points.append(f"마일스톤: {revised_milestone_title}")
            if support_role_names:
                context_points.append("지원 역할: " + ", ".join(support_role_names))
            if planner_backlog_titles:
                context_points.extend(f"backlog/todo: {item}" for item in planner_backlog_titles[:3])
            if planner_doc_targets:
                context_points.extend(f"동기화 문서: {item}" for item in planner_doc_targets[:2])
        if what_details:
            context_points.extend(what_details[:3])
        if required_inputs:
            context_points.extend(f"추가 입력: {item}" for item in required_inputs[:2])
        if acceptance_criteria:
            context_points.extend(f"완료 기준: {item}" for item in acceptance_criteria[:2])
        if findings:
            context_points.extend(f"finding: {item}" for item in findings[:2])
        if unresolved_items:
            context_points.extend(f"남은 과제: {item}" for item in unresolved_items[:2])
        if residual_risks:
            context_points.extend(f"리스크: {item}" for item in residual_risks[:2])
        if not context_points and passed_checks:
            context_points.extend(f"검증 근거: {item}" for item in passed_checks[:2])

        how_fragments: list[str] = []
        if role == "planner":
            if planner_doc_targets:
                how_fragments.append("문서 동기화: " + ", ".join(planner_doc_targets[:2]))
            if support_role_names:
                how_fragments.append("지원 역할: " + ", ".join(support_role_names[:2]))
            if planner_backlog_titles:
                how_fragments.append("우선순위 항목: " + ", ".join(planner_backlog_titles[:2]))
        elif role == "designer" and designer_message_priority:
            how_fragments.extend(designer_message_priority[:3])
        if how_details:
            how_fragments.extend(how_details[:3])
        if required_inputs:
            how_fragments.append(f"추가 입력 {len(required_inputs)}건을 반영해야 합니다.")
        if acceptance_criteria:
            how_fragments.append(f"완료 기준 {len(acceptance_criteria)}건을 기준으로 진행합니다.")
        if findings:
            how_fragments.append(f"핵심 finding {len(findings)}건을 우선 반영해야 합니다.")
        elif passed_checks:
            how_fragments.append(f"검증 통과 근거 {len(passed_checks)}건이 확보됐습니다.")
        if not how_fragments:
            proposal_summary = _collapse_whitespace(_summarize_proposals(proposals))
            if proposal_summary:
                how_fragments.append(proposal_summary)
        how_summary = _join_semantic_fragments(_dedupe_preserving_order(how_fragments)[:3])

        why_fragments: list[str] = []
        if role == "planner":
            if required_inputs:
                why_fragments.append(f"추가 입력 {len(required_inputs)}건이 확보돼야 planning을 닫을 수 있습니다.")
            elif acceptance_criteria:
                why_fragments.append(f"실행 역할이 바로 이어받을 수 있도록 완료 기준 {len(acceptance_criteria)}건을 명시했습니다.")
            elif planner_backlog_titles:
                why_fragments.append("orchestrator가 후속 실행 역할을 고를 수 있도록 실행 대상을 명시했습니다.")
        elif role == "designer" and designer_routing_rationale:
            why_fragments.append(designer_routing_rationale)
        if why_details:
            why_fragments.extend(why_details[:2])
        next_role = _collapse_whitespace(result.get("next_role") or "")
        transition_reason = _collapse_whitespace(transition.get("reason") or "")
        if (
            transition_reason
            and not next_role
            and transition_reason != what_summary
            and transition_reason not in why_fragments
        ):
            why_fragments.append(transition_reason)
        why_summary = _join_semantic_fragments(_dedupe_preserving_order(why_fragments)[:2])
        route_reason = ""
        if next_role:
            route_reason = f"다음 역할: {next_role}"
            if transition_reason:
                route_reason = f"{route_reason} | {transition_reason}"

        latest_context_summary = _join_semantic_fragments(
            fragment for fragment in (what_summary, how_summary) if fragment
        )
        return {
            "from_role": role,
            "what_summary": what_summary,
            "what_details": what_details,
            "how_details": how_details,
            "how_summary": how_summary,
            "why_summary": why_summary,
            "route_reason": route_reason,
            "latest_context_summary": latest_context_summary,
            "context_points": _dedupe_preserving_order(context_points)[:4],
            "reference_artifacts": result_artifacts,
        }

    def _summarize_relay_body(self, envelope: MessageEnvelope) -> list[str]:
        kind = str(envelope.params.get("_teams_kind") or "").strip()
        payload: Any = {}
        if kind == "report" and isinstance(envelope.params.get("result"), dict):
            payload = dict(envelope.params.get("result") or {})
            if not str(payload.get("role") or "").strip():
                payload["role"] = str(envelope.sender or "").strip()
        if not payload:
            payload = self._parse_json_payload_from_text(envelope.body)
        if isinstance(payload, dict) and payload:
            proposals = dict(payload.get("proposals") or {}) if isinstance(payload.get("proposals"), dict) else {}
            transition = self._workflow_transition(payload)
            payload_names = (
                "planning_contract",
                "planning_package",
                "design_feedback",
                "implementation_guidance",
                "implementation_report",
                "code_review",
                "verification_result",
                "qa_validation",
                "sprint_plan_update",
            )
            status = _collapse_whitespace(payload.get("status") or "")
            is_exception_status = status in {"failed", "blocked", "reopen", "needs_revision"}
            exception_summary = _collapse_whitespace(payload.get("summary") or "")
            semantic_context = self._build_role_result_semantic_context(payload)
            error_fragments = self._relay_summary_text_fragments(payload.get("error") or "", width=116, max_lines=3)
            reference_artifacts = [str(item).strip() for item in (semantic_context.get("reference_artifacts") or []) if str(item).strip()]
            details: list[str] = []
            seen: set[str] = set()
            sender_role = _collapse_whitespace(str(envelope.sender or ""))
            semantic_overlap_candidates: list[str] = []

            def is_context_redundant(candidate: str) -> bool:
                normalized = _collapse_whitespace(candidate).lower()
                if not normalized:
                    return True
                segments = [segment.strip() for segment in normalized.split(" | ") if segment.strip()]
                if not segments:
                    segments = [normalized]
                for segment in segments:
                    segment_redundant = False
                    for source in semantic_overlap_candidates:
                        source_normalized = _collapse_whitespace(source).lower()
                        if not source_normalized:
                            continue
                        if source_normalized == segment or source_normalized in segment or segment in source_normalized:
                            segment_redundant = True
                            break
                    if not segment_redundant:
                        return False
                return True

            def dedupe_seen(value: str) -> bool:
                normalized = _collapse_whitespace(value).lower()
                if not normalized or normalized in seen:
                    return False
                seen.add(normalized)
                return True

            what_summary = _collapse_whitespace(semantic_context.get("what_summary") or "")
            if (
                what_summary
                and _looks_meta_summary(what_summary)
                and not semantic_context.get("what_details")
                and not (is_exception_status and what_summary == exception_summary)
            ):
                what_summary = ""
            if not what_summary:
                if is_exception_status:
                    what_fallback_candidates = [
                        exception_summary,
                        str(payload.get("scope") or "").strip(),
                        str(payload.get("body") or "").strip(),
                        str(envelope.scope or "").strip(),
                        str(envelope.body or "").strip(),
                    ]
                else:
                    what_fallback_candidates = [
                        str(payload.get("scope") or "").strip(),
                        str(payload.get("body") or "").strip(),
                        str(envelope.scope or "").strip(),
                        str(envelope.body or "").strip(),
                        str(payload.get("summary") or "").strip(),
                    ]
                for candidate in what_fallback_candidates:
                    fallback_fragments = self._relay_summary_text_fragments(candidate, width=116, max_lines=1)
                    if fallback_fragments:
                        candidate_summary = _collapse_whitespace(fallback_fragments[0])
                        normalized_candidate = _collapse_whitespace(candidate)
                        if _looks_meta_summary(candidate_summary) and not (
                            is_exception_status and normalized_candidate and normalized_candidate == exception_summary
                        ):
                            what_summary = ""
                            continue
                        what_summary = candidate_summary
                        break
            what_details = [
                str(item).strip()
                for item in (semantic_context.get("what_details") or [])
                if str(item).strip() and str(item).strip() != what_summary
            ]
            context_points = [
                str(item).strip()
                for item in (semantic_context.get("context_points") or [])
                if str(item).strip()
            ]
            how_details = [
                str(item).strip()
                for item in (semantic_context.get("how_details") or [])
                if str(item).strip()
            ]
            how_summary = str(semantic_context.get("how_summary") or "").strip()
            why_summary = str(semantic_context.get("why_summary") or "").strip()
            route_reason = str(semantic_context.get("route_reason") or "").strip()
            source_role = _collapse_whitespace(semantic_context.get("from_role") or sender_role)
            planner_backlog_count = 0
            backlog_items = proposals.get("backlog_items")
            backlog_item = proposals.get("backlog_item")
            if isinstance(backlog_items, list):
                planner_backlog_count = len(
                    [item for item in backlog_items if isinstance(item, dict) or str(item).strip()]
                )
            elif isinstance(backlog_item, (dict, str)):
                planner_backlog_count = 1

            def _check_now_priority(point: str) -> int:
                normalized = _collapse_whitespace(point).lower()
                if not normalized:
                    return 0
                score = 0
                if "state transitions" in normalized or "상태 전이" in normalized or "->" in normalized:
                    score += 30
                if "candidate" in normalized and "triggered" in normalized:
                    score += 20
                if "watch -> candidate" in normalized:
                    score += 20
                if "backlog/todo" in normalized:
                    score += 18
                if "핵심 레이어" in normalized:
                    score += 18
                if "판단 지점" in normalized:
                    score += 16
                if "ux 판단" in normalized:
                    score += 14
                if source_role == "planner" and (
                    "우선순위" in normalized
                    or "backlog/todo" in normalized
                    or "마일스톤" in normalized
                ):
                    score += 8
                if source_role == "designer" and (
                    "리드" in normalized or "lead" in normalized or "현재 상태" in normalized
                ):
                    score += 8
                if source_role == "architect" and (
                    "implementation" in normalized or "구현" in normalized
                ):
                    score += 5
                return score

            def _is_constraint_point(point: str) -> bool:
                normalized = _collapse_whitespace(point).lower()
                return bool(
                    normalized.startswith("필요 입력")
                    or normalized.startswith("완료 기준")
                    or normalized.startswith("미해결")
                    or normalized.startswith("finding")
                    or normalized.startswith("리스크")
                )

            deferred_constraint_points = [
                item for item in context_points if _is_constraint_point(item)
            ]
            check_now_candidates: list[str] = []
            check_now_candidates.extend(what_details)
            check_now_candidates.extend(
                item for item in context_points if not _is_constraint_point(item)
            )
            if how_summary:
                check_now_candidates.append(how_summary)
            if why_summary and not route_reason:
                check_now_candidates.append(why_summary)
            raw_check_now_points = _dedupe_preserving_order(item for item in check_now_candidates if item)
            check_now_points = [
                item
                for _, _, item in sorted(
                    (
                        (-_check_now_priority(item), idx, item)
                        for idx, item in enumerate(raw_check_now_points)
                    ),
                    key=lambda item: (item[0], item[1]),
                )
            ]

            if what_summary and dedupe_seen(what_summary):
                what_line = f"- What: {what_summary}"
            else:
                what_line = None

            if route_reason and dedupe_seen(route_reason):
                details.append(f"- Why now: {route_reason}")
            elif why_summary and dedupe_seen(why_summary):
                details.append(f"- Why now: {why_summary}")

            if what_line:
                details.append(what_line)

            if check_now_points:
                details.append("- Check now:")
                details.extend(f"  - {item}" for item in check_now_points[:2])
            constraint_points: list[str] = []
            required_inputs = _extract_proposal_required_inputs(proposals)[:2]
            if source_role == "planner":
                acceptance_criteria = _planner_relay_constraint_summaries(
                    proposals,
                    backlog_count=planner_backlog_count,
                )[:2]
            else:
                acceptance_criteria = [
                    f"완료 기준: {_collapse_whitespace(item)}"
                    for item in self._normalize_backlog_acceptance_criteria(
                        _extract_proposal_acceptance_criteria(proposals)
                    )[:2]
                    if _collapse_whitespace(item)
                ]
            unresolved_items = [str(item).strip() for item in (transition.get("unresolved_items") or []) if str(item).strip()][:2]
            findings = self._proposal_nested_string_list(proposals, payload_names, "findings")[:2]
            residual_risks = self._proposal_nested_string_list(proposals, payload_names, "residual_risks")[:2]
            supporting_execution_points: list[str] = []
            if source_role == "architect":
                for item in how_details:
                    normalized = _collapse_whitespace(item)
                    lowered = normalized.lower()
                    if not normalized:
                        continue
                    if "테스트" in normalized or "validation" in lowered or "검증" in normalized:
                        supporting_execution_points.append(normalized)
                if not supporting_execution_points:
                    supporting_execution_points.extend(
                        _collapse_whitespace(item) for item in how_details if _collapse_whitespace(item)
                    )
            constraint_points.extend(
                f"필요 입력: {_collapse_whitespace(item)}"
                for item in required_inputs
                if _collapse_whitespace(item)
            )
            constraint_points.extend(item for item in acceptance_criteria if _collapse_whitespace(item))
            constraint_points.extend(
                f"미해결: {_collapse_whitespace(item)}"
                for item in unresolved_items
                if _collapse_whitespace(item)
            )
            constraint_points.extend(
                f"finding: {_collapse_whitespace(item)}"
                for item in findings
                if _collapse_whitespace(item)
            )
            constraint_points.extend(
                f"리스크: {_collapse_whitespace(item)}"
                for item in residual_risks
                if _collapse_whitespace(item)
            )
            constraint_points.extend(
                point for point in supporting_execution_points
                if point and _collapse_whitespace(point)
            )
            constraint_points.extend(
                point for point in deferred_constraint_points
                if point and _collapse_whitespace(point)
            )
            constraint_points = _dedupe_preserving_order(
                point for point in constraint_points
                if point and _collapse_whitespace(point) and dedupe_seen(point)
            )
            if constraint_points:
                details.append("- Constraints:")
                details.extend(f"  - {item}" for item in constraint_points[:2])
            semantic_overlap_candidates.extend(
                [
                    what_summary,
                    *what_details,
                    how_summary,
                    route_reason,
                    str(semantic_context.get("why_summary") or ""),
                    str(envelope.sender or ""),
                    str(envelope.target or ""),
                ]
            )
            if status and is_exception_status:
                if dedupe_seen(f"상태: {status}"):
                    details.append(f"- 상태: {status}")
            if error_fragments:
                details.append("- 오류:")
                details.extend(f"  - {fragment}" for fragment in error_fragments)
            if is_exception_status and not any(
                line.startswith(prefix)
                for line in details
                for prefix in ("- Why now:", "- Check now:", "- Constraints:", "- Refs:")
            ):
                context_points: list[str] = []
                from_role = _collapse_whitespace(semantic_context.get("from_role") or "")
                if from_role and from_role != sender_role:
                    context_points.append(f"previous role: {from_role}")
                latest_context_summary = _collapse_whitespace(semantic_context.get("latest_context_summary") or "")
                if latest_context_summary and not is_context_redundant(latest_context_summary):
                    context_points.append(f"latest summary: {latest_context_summary}")
                acceptance_criteria = self._normalize_backlog_acceptance_criteria(
                    _extract_proposal_acceptance_criteria(proposals)
                )
                for item in acceptance_criteria[:2]:
                    normalized = _collapse_whitespace(item)
                    if normalized:
                        context_points.append(f"완료 기준: {normalized}")
                for item in _extract_proposal_required_inputs(proposals)[:2]:
                    normalized = _collapse_whitespace(item)
                    if normalized:
                        context_points.append(f"필요 입력: {normalized}")
                for item in (transition.get("unresolved_items") or []):
                    normalized = _collapse_whitespace(item)
                    if normalized:
                        context_points.append(f"미해결: {normalized}")
                for item in self._proposal_nested_string_list(proposals, payload_names, "findings")[:2]:
                    normalized = _collapse_whitespace(item)
                    if normalized:
                        context_points.append(f"finding: {normalized}")
                context_points = _dedupe_preserving_order(
                    fragment
                    for fragment in context_points
                    if fragment and dedupe_seen(fragment)
                )
                if context_points:
                    details.append("- Context:")
                    details.extend(f"  - {fragment}" for fragment in context_points[:2])
            if reference_artifacts:
                details.append("- Refs:")
                details.extend(f"  - {artifact}" for artifact in reference_artifacts[:2])
            if details:
                return details
            keys = [str(key).strip() for key in payload.keys() if str(key).strip()]
            if keys:
                return [f"- JSON 필드: {', '.join(keys[:5])}"]
        if isinstance(payload, list) and payload:
            return [f"- JSON 배열: 총 {len(payload)}개 항목"]
        fallback_fragments = self._relay_summary_text_fragments(
            envelope.body or envelope.scope or "",
            width=116,
            max_lines=1,
        )
        if fallback_fragments:
            return [f"- What: {fallback_fragments[0]}"]
        return ["- 본문이 비어 있거나 요약 가능한 필드를 찾지 못했습니다."]

    def _build_internal_relay_summary_message(self, envelope: MessageEnvelope) -> str:
        kind = str(envelope.params.get("_teams_kind") or "").strip() or "unknown"
        sender = str(envelope.sender or "").strip() or "unknown"
        target = str(envelope.target or "").strip() or "unknown"
        header = f"{INTERNAL_RELAY_SUMMARY_MARKER} {sender} -> {target} ({kind})"
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
            *self._relay_report_sections_from_lines(
                self._summarize_relay_body(envelope),
                default_title="핵심 전달",
            ),
        ]
        return self._render_report_sections_message(header, sections)

    async def _send_internal_relay_summary(self, envelope: MessageEnvelope) -> None:
        relay_channel_id = str(self.discord_config.relay_channel_id or "").strip()
        if not relay_channel_id:
            return
        summary = self._build_internal_relay_summary_message(envelope)
        try:
            await self._send_discord_content(
                content=summary,
                send=lambda chunk: self.discord_client.send_channel_message(relay_channel_id, chunk),
                target_description=f"relay_summary:{relay_channel_id}",
                swallow_exceptions=False,
                log_traceback=False,
            )
        except Exception as exc:
            LOGGER.warning(
                "Failed to send internal relay summary for request %s to relay:%s: %s",
                envelope.request_id or "unknown",
                relay_channel_id,
                exc,
            )

    async def _send_relay(self, envelope: MessageEnvelope, *, request_record: dict[str, Any] | None = None) -> bool:
        if self._is_internal_relay_enabled():
            target_description = f"internal:{envelope.target}"
            try:
                relay_id = self._enqueue_internal_relay(envelope)
            except Exception as exc:
                LOGGER.warning(
                    "Internal relay enqueue failed for request %s to %s: %s",
                    envelope.request_id or "unknown",
                    target_description,
                    exc,
                )
                if request_record is not None:
                    self._record_relay_delivery(
                        request_record,
                        status="failed",
                        target_description=target_description,
                        attempts=1,
                        error=str(exc),
                        envelope=envelope,
                    )
                return False
            if request_record is not None:
                self._record_relay_delivery(
                    request_record,
                    status="sent",
                    target_description=target_description,
                    attempts=1,
                    error="",
                    envelope=envelope,
                )
            await self._send_internal_relay_summary(envelope)
            return True
        mention = f"<@{self.discord_config.get_role(envelope.target).bot_id}>\n"
        target_description = f"relay:{self.discord_config.relay_channel_id}"
        try:
            await self._send_discord_content(
                content=envelope_to_text(envelope),
                send=lambda chunk: self.discord_client.send_channel_message(self.discord_config.relay_channel_id, chunk),
                target_description=target_description,
                prefix=mention,
            )
        except Exception as exc:
            send_error = exc if isinstance(exc, DiscordSendError) else DiscordSendError(str(exc))
            LOGGER.warning(
                "Relay send failed for request %s to %s after %s attempt(s): %s",
                envelope.request_id or "unknown",
                target_description,
                getattr(send_error, "attempts", 1),
                send_error,
            )
            if request_record is not None:
                self._record_relay_delivery(
                    request_record,
                    status="failed",
                    target_description=target_description,
                    attempts=getattr(send_error, "attempts", 1),
                    error=str(send_error),
                    envelope=envelope,
                )
            return False
        if request_record is not None:
            self._record_relay_delivery(
                request_record,
                status="sent",
                target_description=target_description,
                attempts=1,
                error="",
                envelope=envelope,
            )
        return True

    def _resolve_request_reply_route(self, request_record: dict[str, Any]) -> tuple[dict[str, Any], str]:
        persisted_route = dict(request_record.get("reply_route") or {}) if isinstance(request_record.get("reply_route"), dict) else {}
        params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
        original_requester = _extract_original_requester(params)
        merged_route = _merge_requester_route(persisted_route, original_requester)
        source = "reply_route"
        if str(merged_route.get("channel_id") or "").strip() and not str(persisted_route.get("channel_id") or "").strip():
            source = "original_requester"
            request_record["reply_route"] = {
                **persisted_route,
                **merged_route,
            }
            self._save_request(request_record)
        return merged_route, source

    @staticmethod
    def _extract_summary_field(summary: str, field_name: str) -> str:
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

    @staticmethod
    def _first_sentence(text: str) -> str:
        normalized = " ".join(str(text or "").split())
        if not normalized:
            return ""
        pieces = re.split(r"(?<=[.!?])\s+", normalized, maxsplit=1)
        return pieces[0].strip()

    def _simplify_requester_summary(self, summary: str) -> str:
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
            sprint_name = self._extract_summary_field(normalized, "sprint_name")
            sprint_id = self._extract_summary_field(normalized, "sprint_id")
            milestone = self._extract_summary_field(normalized, "milestone_title")
            phase = self._extract_summary_field(normalized, "phase")
            status = self._extract_summary_field(normalized, "status")
            todo_summary = self._extract_summary_field(normalized, "todo_summary")
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
            sprint_name = self._extract_summary_field(normalized, "sprint_name")
            sprint_id = self._extract_summary_field(normalized, "sprint_id")
            milestone = self._extract_summary_field(normalized, "milestone")
            lines = ["스프린트를 시작했습니다."]
            if sprint_name or sprint_id:
                lines.append(f"스프린트: {sprint_name or sprint_id}")
            if milestone:
                lines.append(f"마일스톤: {milestone}")
            return "\n".join(lines)

        if normalized.startswith("이미 active sprint가 있습니다."):
            sprint_id = self._extract_summary_field(normalized, "sprint_id")
            phase = self._extract_summary_field(normalized, "phase")
            milestone = self._extract_summary_field(normalized, "milestone")
            lines = ["이미 진행 중인 스프린트가 있습니다."]
            if sprint_id:
                lines.append(f"스프린트 ID: {sprint_id}")
            if milestone:
                lines.append(f"마일스톤: {milestone}")
            if phase:
                lines.append(f"현재 단계: {phase}")
            return "\n".join(lines)

        if normalized.startswith("현재 sprint를 wrap up 대상"):
            sprint_id = self._extract_summary_field(normalized, "sprint_id")
            lines = ["스프린트 종료를 요청했습니다."]
            if sprint_id:
                lines.append(f"스프린트 ID: {sprint_id}")
            if "진행 중 task가 끝나면 wrap up을 시작합니다." in normalized:
                lines.append("현재 상태: 진행 중인 작업이 끝나면 마무리를 시작합니다.")
            elif "곧 wrap up을 시작합니다." in normalized:
                lines.append("현재 상태: 곧 마무리를 시작합니다.")
            return "\n".join(lines)

        if normalized.startswith("terminal sprint를 종료 처리했습니다."):
            sprint_id = self._extract_summary_field(normalized, "sprint_id")
            status = self._extract_summary_field(normalized, "status")
            lines = ["스프린트를 종료했습니다."]
            if sprint_id:
                lines.append(f"스프린트 ID: {sprint_id}")
            if status:
                lines.append(f"현재 상태: {status}")
            return "\n".join(lines)

        if normalized.startswith("재개할 sprint가 없습니다."):
            return "재개할 스프린트가 없습니다."

        if normalized.startswith("이미 완료된 sprint라 재개할 수 없습니다."):
            sprint_id = self._extract_summary_field(normalized, "sprint_id")
            lines = ["이미 완료된 스프린트라 재개할 수 없습니다."]
            if sprint_id:
                lines.append(f"스프린트 ID: {sprint_id}")
            return "\n".join(lines)

        if normalized.startswith("현재 상태에서는 sprint를 재개할 수 없습니다."):
            sprint_id = self._extract_summary_field(normalized, "sprint_id")
            status = self._extract_summary_field(normalized, "status")
            lines = ["현재 상태에서는 스프린트를 재개할 수 없습니다."]
            if sprint_id:
                lines.append(f"스프린트 ID: {sprint_id}")
            if status:
                lines.append(f"현재 상태: {status}")
            return "\n".join(lines)

        if normalized.startswith("active sprint 재개를 요청했습니다."):
            sprint_id = self._extract_summary_field(normalized, "sprint_id")
            phase = self._extract_summary_field(normalized, "phase")
            status = self._extract_summary_field(normalized, "status")
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
            first_sentence = self._first_sentence(before)
            state_text = after.strip().rstrip(".")
            lines: list[str] = []
            if first_sentence:
                lines.append(first_sentence)
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
        return self._first_sentence(cleaned_lines[0]) or cleaned_lines[0]

    def _build_requester_status_message(
        self,
        *,
        status: str,
        request_id: str,
        summary: str,
        related_request_ids: list[str] | None = None,
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
        detail_text = self._simplify_requester_summary(summary)
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

    async def _reply_to_requester(self, request_record: dict[str, Any], content: str) -> None:
        route, route_source = self._resolve_request_reply_route(request_record)
        target_content = str(content or "").strip()
        if not target_content:
            return
        if route.get("is_dm"):
            author_id = str(route.get("author_id") or "").strip()
            if not author_id:
                LOGGER.info(
                    "Skipping requester reply for request %s because DM author_id is missing.",
                    request_record.get("request_id") or "unknown",
                )
                return
            await self._send_discord_content(
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
                request_record.get("request_id") or "unknown",
                request_record.get("current_role") or "unknown",
                route_source,
                json.dumps(request_record.get("reply_route") or {}, ensure_ascii=False, sort_keys=True),
                json.dumps(_extract_original_requester(dict(request_record.get("params") or {})), ensure_ascii=False, sort_keys=True),
                ",".join(sorted(str(key) for key in (request_record.get("params") or {}).keys())),
            )
            return
        prefix = f"<@{author_id}> " if author_id else ""
        await self._send_discord_content(
            content=target_content,
            send=lambda chunk: self.discord_client.send_channel_message(channel_id, chunk),
            target_description=f"channel:{channel_id}",
            prefix=prefix,
        )

    async def _send_channel_reply(self, message: DiscordMessage, content: str) -> None:
        if message.is_dm:
            await self._send_discord_content(
                content=content,
                send=lambda chunk: self.discord_client.send_dm(message.author_id, chunk),
                target_description=f"dm:{message.author_id}",
            )
            return
        prefix = f"<@{message.author_id}> "
        await self._send_discord_content(
            content=content,
            send=lambda chunk: self.discord_client.send_channel_message(message.channel_id, chunk),
            target_description=f"channel:{message.channel_id}",
            prefix=prefix,
        )

    async def _send_immediate_receipt(self, message: DiscordMessage) -> None:
        if self._is_trusted_relay_message(message):
            return
        async with self._cross_process_send_lock():
            if message.is_dm:
                await self.discord_client.send_dm(message.author_id, "수신양호")
                return
            await self.discord_client.send_channel_message(message.channel_id, f"<@{message.author_id}> 수신양호")

    def _build_runtime_signature_suffix(self) -> str:
        role_config = self.runtime_config.role_defaults.get(self.role)
        if role_config is None:
            return ""
        model_name = str(role_config.model or "").strip()
        if not model_name:
            return ""
        reasoning = "None" if "gemini" in model_name.lower() else str(role_config.reasoning or "").strip() or "medium"
        return f"\n\nmodel: {model_name} | reasoning: {reasoning}"

    def _append_runtime_signature(self, content: str) -> str:
        normalized = str(content or "").strip()
        if not normalized:
            return ""
        suffix = self._build_runtime_signature_suffix()
        if not suffix:
            return normalized
        if normalized.endswith(suffix.strip()):
            return normalized
        return f"{normalized}{suffix}"

    @contextlib.asynccontextmanager
    async def _cross_process_send_lock(self):
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

    async def _send_discord_content(
        self,
        *,
        content: str,
        send,
        target_description: str,
        prefix: str = "",
        swallow_exceptions: bool = False,
        log_traceback: bool = True,
    ) -> None:
        rendered_content = self._append_runtime_signature(content)
        chunks = _render_discord_message_chunks(rendered_content, prefix=prefix)
        total = len(chunks)
        async with self._cross_process_send_lock():
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

    def _create_request_record(self, message: DiscordMessage, envelope: MessageEnvelope, *, forwarded: bool) -> dict[str, Any]:
        requester = _extract_original_requester(envelope.params) if forwarded else None
        author_id = str((requester or {}).get("author_id") or message.author_id)
        channel_id = str((requester or {}).get("channel_id") or message.channel_id)
        is_dm = bool((requester or {}).get("is_dm") if forwarded else message.is_dm)
        request_id = envelope.request_id or new_request_id()
        source_message_created_at = self._message_received_at(message)
        params = dict(envelope.params)
        if forwarded:
            normalized_requester = _merge_requester_route(
                requester,
                {
                    "author_id": author_id,
                    "author_name": str((requester or {}).get("author_name") or message.author_name),
                    "channel_id": channel_id,
                    "guild_id": str((requester or {}).get("guild_id") or message.guild_id or ""),
                    "is_dm": is_dm,
                    "message_id": str((requester or {}).get("message_id") or message.message_id),
                },
            )
            if normalized_requester:
                params["original_requester"] = normalized_requester
        record = {
            "request_id": request_id,
            "status": "queued",
            "intent": envelope.intent,
            "urgency": envelope.urgency,
            "scope": envelope.scope,
            "body": envelope.body,
            "artifacts": list(envelope.artifacts),
            "params": params,
            "current_role": "orchestrator",
            "next_role": "",
            "owner_role": "orchestrator",
            "sprint_id": self.runtime_config.sprint_id,
            "source_message_created_at": source_message_created_at.isoformat() if source_message_created_at else "",
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "fingerprint": build_request_fingerprint(
                author_id=author_id,
                channel_id=channel_id,
                intent=envelope.intent,
                scope=envelope.scope,
            ),
            "reply_route": {
                "author_id": author_id,
                "author_name": str((requester or {}).get("author_name") or message.author_name),
                "channel_id": channel_id,
                "guild_id": str((requester or {}).get("guild_id") or message.guild_id or ""),
                "is_dm": is_dm,
                "message_id": str((requester or {}).get("message_id") or message.message_id),
            },
            "events": [],
            "result": {},
        }
        append_request_event(
            record,
            event_type="created",
            actor="orchestrator",
            summary="요청을 접수했습니다.",
            payload={"forwarded": forwarded},
        )
        self._save_request(record)
        self._append_role_history(
            "orchestrator",
            record,
            event_type="created",
            summary="요청을 접수했습니다.",
        )
        return record

    def _find_duplicate_request(self, message: DiscordMessage, envelope: MessageEnvelope) -> dict[str, Any] | None:
        requester = _extract_original_requester(envelope.params)
        fingerprint = build_request_fingerprint(
            author_id=str(requester.get("author_id") or message.author_id),
            channel_id=str(requester.get("channel_id") or message.channel_id),
            intent=envelope.intent,
            scope=envelope.scope,
        )
        for record in iter_json_records(self.paths.requests_dir):
            if is_terminal_request(record):
                continue
            if record.get("fingerprint") == fingerprint:
                return record
        return None

    def _load_request(self, request_id: str) -> dict[str, Any]:
        if not request_id:
            return {}
        return read_json(self.paths.request_file(request_id))

    @staticmethod
    def _unlink_if_exists(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def _purge_request_scoped_role_output_files(self) -> None:
        request_ids = {
            path.stem
            for path in self.paths.requests_dir.glob("*.json")
            if path.is_file() and path.stem
        }
        if not request_ids:
            return
        for role in TEAM_ROLES:
            for request_id in request_ids:
                self._unlink_if_exists(self.paths.role_sources_dir(role) / f"{request_id}.md")
                self._unlink_if_exists(self.paths.role_sources_dir(role) / f"{request_id}.json")
                self._unlink_if_exists(self.paths.runtime_root / "role_reports" / role / f"{request_id}.md")
                self._unlink_if_exists(self.paths.runtime_root / "role_reports" / role / f"{request_id}.json")

    def _save_request(self, request_record: dict[str, Any]) -> None:
        request_record["updated_at"] = utc_now_iso()
        write_json(self.paths.request_file(request_record["request_id"]), request_record)
        self._refresh_role_todos()

    def _persist_request_result(self, request_record: dict[str, Any]) -> None:
        request_record["updated_at"] = utc_now_iso()
        write_json(self.paths.request_file(request_record["request_id"]), request_record)

    @staticmethod
    def _is_terminal_request_status(status: str) -> bool:
        return str(status or "").strip().lower() in {"completed", "committed", "cancelled", "failed"}

    def _stale_role_report_reason(
        self,
        request_record: dict[str, Any],
        envelope: MessageEnvelope,
        result: dict[str, Any],
    ) -> str:
        request_status = str(request_record.get("status") or "").strip().lower()
        workflow_state = self._request_workflow_state(request_record)
        workflow_phase = str(workflow_state.get("phase") or "").strip().lower()
        workflow_phase_status = str(workflow_state.get("phase_status") or "").strip().lower()
        sender_role = str(envelope.sender or "").strip().lower()
        result_role = str(result.get("role") or "").strip().lower()
        report_role = sender_role or result_role
        current_role = str(request_record.get("current_role") or "").strip().lower()

        if self._is_terminal_request_status(request_status) or (
            workflow_phase == WORKFLOW_PHASE_CLOSEOUT and workflow_phase_status == "completed"
        ):
            return "request is already closed"
        if report_role and current_role and report_role != current_role:
            return f"request currently expects {current_role}, not {report_role}"
        return ""

    @staticmethod
    def _proposal_nested_string_list(
        proposals: dict[str, Any],
        payload_names: tuple[str, ...],
        field_name: str,
    ) -> list[str]:
        for payload_name in payload_names:
            payload = proposals.get(payload_name)
            if not isinstance(payload, dict):
                continue
            values = _normalize_string_list(payload.get(field_name))
            if values:
                return values
        return []

    @staticmethod
    def _delegate_task_text(request_record: dict[str, Any]) -> str:
        body = str(request_record.get("body") or "").strip()
        scope = str(request_record.get("scope") or "").strip()
        if body and body != scope:
            return body
        return scope or body

    def _synthesize_latest_role_context(self, result: dict[str, Any]) -> dict[str, Any]:
        semantic_context = self._build_role_result_semantic_context(result)
        return {
            "from_role": str(semantic_context.get("from_role") or "").strip(),
            "what_summary": str(semantic_context.get("what_summary") or "").strip(),
            "what_details": list(semantic_context.get("what_details") or []),
            "how_summary": str(semantic_context.get("how_summary") or "").strip(),
            "why_summary": str(semantic_context.get("why_summary") or "").strip(),
            "route_reason": str(semantic_context.get("route_reason") or "").strip(),
            "latest_context_summary": str(semantic_context.get("latest_context_summary") or "").strip(),
            "focus_points": list(semantic_context.get("context_points") or []),
            "reference_artifacts": list(semantic_context.get("reference_artifacts") or []),
        }

    def _build_delegation_context(self, request_record: dict[str, Any], next_role: str) -> dict[str, Any]:
        request_id = str(request_record.get("request_id") or "").strip()
        canonical_request = (
            str(self.paths.request_file(request_id).relative_to(self.paths.workspace_root))
            if request_id
            else ""
        )
        routing_context = dict(request_record.get("routing_context") or {})
        workflow_state = self._request_workflow_state(request_record)
        latest_context = self._synthesize_latest_role_context(dict(request_record.get("result") or {}))
        proposals = dict(dict(request_record.get("result") or {}).get("proposals") or {})
        workflow_transition = dict(proposals.get("workflow_transition") or {})
        workflow_phase = _collapse_whitespace(str(workflow_state.get("phase") or "").strip())
        workflow_step = _collapse_whitespace(str(workflow_state.get("step") or "").strip())
        workflow_stage = f"{workflow_phase} / {workflow_step}" if workflow_phase and workflow_step else workflow_phase
        has_workflow_stage = bool(workflow_stage)
        reference_artifacts = _compact_reference_items(
            [
                item
                for item in (
                    list(latest_context.get("reference_artifacts") or [])
                    + [str(item).strip() for item in (request_record.get("artifacts") or []) if str(item).strip()]
                )
                if item and item != canonical_request
            ],
            limit=3,
        )
        routing_reason = str(routing_context.get("reason") or "").strip()
        if not routing_reason:
            routing_reason = str(latest_context.get("route_reason") or "").strip()
        if not routing_reason:
            routing_reason = str(workflow_transition.get("reason") or "").strip()
        if not routing_reason:
            if workflow_phase and workflow_step:
                routing_reason = f"{next_role} 역할이 {workflow_phase}/{workflow_step} 단계에서 이어받아야 합니다."
            elif workflow_phase:
                routing_reason = f"{next_role} 역할이 {workflow_phase} 단계에서 이어받아야 합니다."
            else:
                routing_reason = f"{next_role} 역할이 현재 단계의 다음 담당입니다."
        routing_evidence: list[str] = []
        matched_signals = [
            str(item).strip()
            for item in (routing_context.get("matched_signals") or [])
            if str(item).strip()
        ]
        matched_domains = [
            str(item).strip()
            for item in (routing_context.get("matched_strongest_domains") or [])
            if str(item).strip()
        ]
        matched_skills = [
            str(item).strip()
            for item in (routing_context.get("matched_preferred_skills") or [])
            if str(item).strip()
        ]
        if matched_signals:
            routing_evidence.append(f"signals: {', '.join(matched_signals[:3])}")
        if matched_domains:
            routing_evidence.append(f"domains: {', '.join(matched_domains[:2])}")
        if matched_skills:
            routing_evidence.append(f"skills: {', '.join(matched_skills[:2])}")
        if str(routing_context.get("routing_phase") or "").strip():
            routing_evidence.append(f"phase: {routing_context['routing_phase']}")
        if str(routing_context.get("request_state_class") or "").strip():
            routing_evidence.append(f"state: {routing_context['request_state_class']}")
        latest_why_summary = str(latest_context.get("why_summary") or "").strip()
        why_summary = _join_semantic_fragments([latest_why_summary, routing_reason, *routing_evidence[:3]])
        required_inputs = _normalize_string_list(_extract_proposal_required_inputs(proposals))[:2]
        acceptance_criteria = _normalize_string_list(_extract_proposal_acceptance_criteria(proposals))[:2]
        unresolved_items = _normalize_string_list(workflow_transition.get("unresolved_items"))[:2]
        payload_names = (
            "planning_contract",
            "planning_package",
            "design_feedback",
            "implementation_guidance",
            "implementation_report",
            "code_review",
            "verification_result",
            "qa_validation",
            "sprint_plan_update",
        )
        findings = _normalize_string_list(self._proposal_nested_string_list(proposals, payload_names, "findings"))[:2]
        residual_risks = _normalize_string_list(self._proposal_nested_string_list(proposals, payload_names, "residual_risks"))[:2]
        focus_points = [
            str(item).strip()
            for item in (
                [
                    _normalize_constraint_point(
                        item,
                        canonical_prefix="추가 입력",
                        source_prefixes=("required input", "required_inputs", "필요 입력", "필수 입력", "추가 입력"),
                    )
                    for item in required_inputs
                ]
                + [
                    _normalize_constraint_point(
                        item,
                        canonical_prefix="완료 기준",
                        source_prefixes=(
                            "acceptance criteria",
                            "acceptance_criteria",
                            "complete criteria",
                            "완료 기준",
                        ),
                    )
                    for item in acceptance_criteria
                ]
                + [f"미해결: {item}" for item in unresolved_items]
                + [f"finding: {item}" for item in findings]
                + [f"리스크: {item}" for item in residual_risks]
                + list(latest_context.get("focus_points") or [])
            )
            if str(item).strip()
        ]
        focus_points_signature_seen: set[str] = set()
        deduped_focus_points: list[str] = []
        for point in focus_points:
            signature = _constraint_point_signature(point)
            if signature and signature not in focus_points_signature_seen:
                focus_points_signature_seen.add(signature)
                deduped_focus_points.append(point)
        focus_points = deduped_focus_points

        constraint_prefixes = (
            "required input:",
            "required_inputs:",
            "완료 기준:",
            "완료기준:",
            "추가 입력:",
            "미해결:",
            "남은 과제:",
            "finding:",
            "리스크:",
            "협업 경계:",
            "주의:",
            "주의사항:",
        )
        immediate_priority_order = {
            "backlog/todo:": 0,
            "todo:": 0,
            "backlog:": 0,
            "required input:": 1,
            "required_inputs:": 1,
            "필수 입력:": 1,
            "필요 입력:": 1,
            "추가 입력:": 1,
            "acceptance criteria:": 2,
            "acceptance_criteria:": 2,
            "완료 기준:": 2,
            "완료기준:": 2,
            "unresolved:": 3,
            "남은 과제:": 3,
            "미해결:": 3,
            "finding:": 4,
            "리스크:": 5,
            "협업 경계:": 5,
            "주의:": 5,
            "주의사항:": 5,
        }

        def _check_point_priority(item: str) -> int:
            lowered = item.lower()
            for prefix, priority in immediate_priority_order.items():
                if lowered.startswith(prefix):
                    return priority
            return 6

        immediate_checks: list[str] = []
        constraint_points: list[str] = []
        for item in focus_points:
            item_lower = item.lower()
            if any(item_lower.startswith(prefix) for prefix in constraint_prefixes):
                constraint_points.append(item)
            else:
                immediate_checks.append(item)
        immediate_checks_with_order = [
            item
            for _priority, _index, item in sorted(
                (
                    (_check_point_priority(item), index, item)
                    for index, item in enumerate(immediate_checks)
                ),
                key=lambda x: (x[0], x[1]),
            )
        ]

        return {
            "task_text": self._delegate_task_text(request_record),
            "target_role": next_role,
            "from_role": str(latest_context.get("from_role") or "").strip(),
            "what_summary": str(latest_context.get("what_summary") or "").strip(),
            "what_details": list(latest_context.get("what_details") or []),
            "how_summary": str(latest_context.get("how_summary") or "").strip(),
            "why_summary": why_summary,
            "route_reason": str(latest_context.get("route_reason") or "").strip(),
            "latest_context_summary": str(latest_context.get("latest_context_summary") or "").strip(),
            "focus_points": focus_points,
            "immediate_checks": immediate_checks_with_order[:2],
            "constraint_points": constraint_points[:2],
            "reference_artifacts": reference_artifacts,
            "routing_reason": routing_reason,
            "workflow_phase": workflow_phase,
            "workflow_step": workflow_step,
            "workflow_stage": workflow_stage,
            "has_workflow_stage": has_workflow_stage,
            "canonical_request": canonical_request,
            "snapshot_path": "",
            "why_this_role": routing_reason,
        }

    def _build_delegate_body(self, request_record: dict[str, Any], delegation_context: dict[str, Any]) -> str:
        task_text = str(delegation_context.get("task_text") or "").strip() or self._delegate_task_text(request_record)
        source_role = str(delegation_context.get("from_role") or "").strip() or "orchestrator"
        target_role = str(delegation_context.get("target_role") or "").strip() or "unknown"
        has_workflow_stage = bool(
            str(delegation_context.get("has_workflow_stage") or "").strip()
            or str(delegation_context.get("workflow_phase") or "").strip()
            or str(delegation_context.get("workflow_step") or "").strip()
        )
        header = f"handoff | {source_role} -> {target_role} | {request_record.get('intent') or 'route'}"
        sections: list[ReportSection] = []
        routing_path = self._build_handoff_routing_path(
            request_record,
            source_role=source_role,
            target_role=target_role,
        )
        meta_lines: list[str] = []
        if routing_path:
            meta_lines.append(f"- 전달 경로: {routing_path}")

        what_details = _dedupe_preserving_order(
            [
                str(item).strip()
                for item in (delegation_context.get("what_details") or [])
                if str(item).strip() and str(item).strip() != task_text
            ]
        )

        what_summary = str(delegation_context.get("what_summary") or "").strip()
        how_summary = str(delegation_context.get("how_summary") or "").strip()
        why_summary = str(delegation_context.get("why_summary") or "").strip()
        why_this_role = str(delegation_context.get("why_this_role") or delegation_context.get("routing_reason") or "").strip()
        detail_context_points = [] if has_workflow_stage else what_details

        check_now_points = _dedupe_preserving_order(
            [
                str(item).strip()
                for item in (delegation_context.get("immediate_checks") or [])
                if str(item).strip()
            ]
        )
        constraint_points = _dedupe_preserving_order(
            [str(item).strip() for item in (delegation_context.get("constraint_points") or []) if str(item).strip()]
        )
        constraint_point_signatures = {
            _constraint_point_signature(item)
            for item in constraint_points
            if _constraint_point_signature(item)
        }
        if constraint_point_signatures:
            check_now_points = [
                item
                for item in check_now_points
                if _constraint_point_signature(item) not in constraint_point_signatures
                ]
        if not check_now_points:
            check_now_points = what_details[:2]
            if constraint_point_signatures:
                check_now_points = [
                    item
                    for item in check_now_points
                    if _constraint_point_signature(item) not in constraint_point_signatures
                ]

        context_points: list[str] = []
        has_follow_up_sections = bool((check_now_points and not has_workflow_stage) or constraint_points)
        if not has_follow_up_sections:
            context_candidates = _dedupe_preserving_order(
                [
                    item
                    for item in (
                        [what_summary, how_summary, why_summary]
                        + detail_context_points
                        + (delegation_context.get("focus_points") or [])
                    )
                    if item and str(item).strip() != task_text
                ]
            )
            suppressed_context = {
                _collapse_whitespace(point).lower()
                for point in (
                    list(check_now_points)
                    + list(constraint_points)
                    + [what_summary, why_this_role, how_summary, why_summary]
                )
            }
            suppressed_context.update(_constraint_point_body(point) for point in suppressed_context if point)
            context_points = [
                point
                for point in context_candidates
                if _collapse_whitespace(point).lower() not in suppressed_context
            ]
        task_lines = [f"- {task_text or 'N/A'}"]
        if what_details and not has_workflow_stage:
            task_lines.extend(f"- {item}" for item in what_details[:2])
        self._append_report_section(sections, "전달 정보", meta_lines)
        self._append_report_section(sections, "핵심 전달", task_lines)
        if why_this_role and not has_workflow_stage:
            self._append_report_section(sections, "이관 이유", [f"- {why_this_role}"])
        if check_now_points and not has_workflow_stage:
            self._append_report_section(sections, "지금 볼 것", [f"- {item}" for item in check_now_points[:2]])
        if constraint_points:
            self._append_report_section(sections, "유의사항", [f"- {item}" for item in constraint_points[:2]])
        if context_points and not has_workflow_stage:
            self._append_report_section(sections, "추가 맥락", [f"- {item}" for item in context_points[:2]])

        ref_lines = [f"- 요청 기록: {delegation_context.get('canonical_request') or 'N/A'}"]
        if delegation_context.get("snapshot_path"):
            ref_lines.append(f"- 스냅샷: {delegation_context['snapshot_path']}")
        if delegation_context.get("reference_artifacts"):
            ref_lines.append(
                f"- 참고 산출물: {', '.join(str(item) for item in delegation_context['reference_artifacts'])}"
            )
        ref_lines.append("- 주의: request record가 relay보다 우선합니다.")
        self._append_report_section(sections, "참고 파일", ref_lines)
        return self._render_report_sections_message(header, sections)

    def _format_role_request_snapshot_markdown(
        self,
        *,
        role: str,
        request_record: dict[str, Any],
        delegation_context: dict[str, Any],
    ) -> str:
        request_id = str(request_record.get("request_id") or "").strip()
        canonical_request = str(self.paths.request_file(request_id).relative_to(self.paths.workspace_root))
        task_text = str(delegation_context.get("task_text") or "").strip() or self._delegate_task_text(request_record)
        workflow_phase = str(delegation_context.get("workflow_phase") or "").strip()
        workflow_step = str(delegation_context.get("workflow_step") or "").strip()
        lines = [
            f"# {role.title()} Request Snapshot",
            "",
            f"- request_id: {request_id}",
            f"- delegated_to: {role}",
            f"- captured_at: {utc_now_iso()}",
            f"- canonical_request: {canonical_request}",
            f"- urgency: {request_record.get('urgency') or 'normal'}",
            f"- workflow: {f'{workflow_phase} / {workflow_step}' if workflow_phase or workflow_step else 'N/A'}",
            f"- why_now: {delegation_context.get('routing_reason') or 'N/A'}",
            f"- previous_role: {delegation_context.get('from_role') or 'N/A'}",
            f"- what_summary: {delegation_context.get('what_summary') or 'N/A'}",
            (
                "- what_details: "
                + (
                    " | ".join(delegation_context.get("what_details") or [])
                    if delegation_context.get("what_details")
                    else "N/A"
                )
            ),
            f"- how_summary: {delegation_context.get('how_summary') or 'N/A'}",
            f"- why_summary: {delegation_context.get('why_summary') or 'N/A'}",
            f"- latest_context: {delegation_context.get('latest_context_summary') or 'N/A'}",
            (
                "- focus_points: "
                + (
                    " | ".join(delegation_context.get("focus_points") or [])
                    if delegation_context.get("focus_points")
                    else "N/A"
                )
            ),
            (
                "- reference_artifacts: "
                + (
                    ", ".join(delegation_context.get("reference_artifacts") or [])
                    if delegation_context.get("reference_artifacts")
                    else "N/A"
                )
            ),
            "",
            "## Task",
            "",
            task_text or "N/A",
            "",
            "## Source Of Truth",
            "",
            "- `Current request` JSON is authoritative.",
            f"- Always trust `{canonical_request}` over relay text or this snapshot if they differ.",
            "",
        ]
        return "\n".join(lines)

    def _write_role_request_snapshot(
        self,
        role: str,
        request_record: dict[str, Any],
        delegation_context: dict[str, Any],
    ) -> str:
        request_id = str(request_record.get("request_id") or "").strip()
        if not request_id:
            return ""
        snapshot_file = self.paths.role_request_snapshot_file(role, request_id)
        snapshot_file.parent.mkdir(parents=True, exist_ok=True)
        snapshot_file.write_text(
            self._format_role_request_snapshot_markdown(
                role=role,
                request_record=request_record,
                delegation_context=delegation_context,
            ),
            encoding="utf-8",
        )
        return str(snapshot_file.relative_to(self.paths.workspace_root))

    def _build_delegate_envelope(
        self,
        request_record: dict[str, Any],
        next_role: str,
        *,
        delegation_context: dict[str, Any] | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> MessageEnvelope:
        delegated_intent = self._intent_for_role(next_role, request_record.get("intent") or "route")
        relay_params = {"_teams_kind": "delegate"}
        if self._is_internal_sprint_request(request_record):
            relay_params.update(
                {
                    "_origin": "sprint_internal",
                    "sprint_id": str(request_record.get("sprint_id") or ""),
                    "todo_id": str(request_record.get("todo_id") or ""),
                    "backlog_id": str(request_record.get("backlog_id") or ""),
                }
            )
        if extra_params:
            relay_params.update(extra_params)
        original_requester = _merge_requester_route(
            dict(request_record.get("reply_route") or {}) if isinstance(request_record.get("reply_route"), dict) else {},
            _extract_original_requester(dict(request_record.get("params") or {})),
        )
        if original_requester:
            relay_params["original_requester"] = original_requester
        delegation_context = dict(delegation_context or request_record.get("delegation_context") or {})
        if not delegation_context:
            delegation_context = self._build_delegation_context(request_record, next_role)
        return MessageEnvelope(
            request_id=request_record["request_id"],
            sender="orchestrator",
            target=next_role,
            intent=delegated_intent,
            urgency=str(request_record.get("urgency") or "normal"),
            scope=str(request_record.get("scope") or ""),
            artifacts=[str(item) for item in request_record.get("artifacts") or []],
            params=relay_params,
            body=self._build_delegate_body(request_record, delegation_context),
        )

    def _initial_role_for_request(self, request_record: dict[str, Any]) -> str:
        return self._intent_to_role(str(request_record.get("intent") or "route"))

    def _intent_to_role(self, intent: str) -> str:
        return intent_to_role_map(self.agent_utilization_policy).get(str(intent).strip().lower(), "planner")

    def _intent_for_role(self, role: str, fallback_intent: str) -> str:
        if role in TEAM_ROLES:
            return self._agent_capability(role).default_intent
        return fallback_intent
