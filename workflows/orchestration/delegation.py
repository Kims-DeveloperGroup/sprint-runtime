"""Delegated request processing and role-result application helpers."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Iterable

from teams_runtime.runtime.base_runtime import normalize_role_payload
from teams_runtime.shared.formatting import ReportSection
from teams_runtime.shared.models import MessageEnvelope
from teams_runtime.shared.persistence import utc_now_iso
from teams_runtime.workflows.orchestration.ingress import extract_original_requester, merge_requester_route
from teams_runtime.workflows.orchestration.engine import (
    WORKFLOW_PHASE_PLANNING,
    WORKFLOW_STEP_ARCHITECT_REVIEW,
    WORKFLOW_STEP_DEVELOPER_BUILD,
    WORKFLOW_STEP_DEVELOPER_REVISION,
    WORKFLOW_STEP_PLANNER_FINALIZE,
    workflow_transition,
)
from teams_runtime.workflows.orchestration.relay import (
    append_report_section,
    relay_summary_text_fragments,
    render_report_sections_message,
)
from teams_runtime.workflows.roles.research import research_reason_code_summary
from teams_runtime.workflows.state.backlog_store import normalize_backlog_acceptance_criteria
from teams_runtime.workflows.state.request_store import append_request_event


LOGGER = logging.getLogger("teams_runtime.workflows.orchestration.delegation")


def normalize_insights(result: dict[str, Any]) -> list[str]:
    raw = result.get("insights")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        normalized = str(raw).strip()
        return [normalized] if normalized else []
    return []


def _collapse_whitespace(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _join_semantic_fragments(fragments: Iterable[str], *, separator: str = " | ") -> str:
    normalized = [_collapse_whitespace(item) for item in fragments if _collapse_whitespace(item)]
    return separator.join(normalized)


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        normalized = str(value).strip()
        return [normalized] if normalized else []
    return []


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
        source = source_prefix.strip()
        if not source:
            continue
        for separator in (":", "："):
            source_with_separator = f"{source}{separator}"
            if lowered.startswith(source_with_separator.lower()):
                remainder = _collapse_whitespace(normalized[len(source_with_separator) :])
                return f"{canonical_prefix}: {remainder}" if remainder else canonical_prefix
    return f"{canonical_prefix}: {normalized}"


def _constraint_point_body(value: str) -> str:
    normalized = _collapse_whitespace(value).lower()
    if not normalized:
        return ""
    prefixes = (
        "required input",
        "required_inputs",
        "필수 입력",
        "필요 입력",
        "추가 입력",
        "acceptance criteria",
        "acceptance_criteria",
        "complete criteria",
        "완료 기준",
        "완료기준",
    )
    for prefix in prefixes:
        prefix_with_colon = f"{prefix}:"
        if normalized.startswith(prefix_with_colon):
            remainder = _collapse_whitespace(normalized[len(prefix_with_colon) :])
            return remainder or normalized
        prefix_with_space = f"{prefix} "
        if normalized.startswith(prefix_with_space):
            remainder = _collapse_whitespace(normalized[len(prefix_with_space) :])
            return remainder or normalized
    return normalized


def _constraint_point_signature(value: str) -> str:
    normalized = _collapse_whitespace(value)
    if not normalized:
        return ""
    signature = _constraint_point_body(normalized)
    return signature or normalized.lower()


def _dedupe_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _compact_reference_items(values: list[str], *, limit: int = 3) -> list[str]:
    normalized = _dedupe_preserving_order(values)
    if len(normalized) <= limit:
        return normalized
    compacted = normalized[:limit]
    compacted[-1] = f"{compacted[-1]} 외 {len(normalized) - limit}건"
    return compacted


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
    research_report = proposals.get("research_report")
    if isinstance(research_report, dict):
        backing_sources = research_report.get("backing_sources")
        if isinstance(backing_sources, list) and backing_sources:
            parts.append(f"research source {len(backing_sources)}건")
        else:
            parts.append("research 판단 1건")
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


def extract_semantic_leaf_lines(
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
                    extract_semantic_leaf_lines(
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
            lines.extend(extract_semantic_leaf_lines(item, prefix=prefix, skip_keys=excluded))
        return lines
    text = _collapse_whitespace(value)
    return [f"{prefix}{text}" if prefix else text] if text else []


def proposal_semantic_details(
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
                    extract_semantic_leaf_lines(payload.get(key), prefix=label, skip_keys=skip_keys)
                )
            for key in ("implementation_steps", "file_changes", "test_follow_up", "validation_steps"):
                how_details.extend(
                    extract_semantic_leaf_lines(payload.get(key), prefix="", skip_keys=skip_keys)
                )
            for key in ("reasoning", "decision_rationale", "guardrails", "risks", "invariants"):
                why_details.extend(
                    extract_semantic_leaf_lines(payload.get(key), prefix="", skip_keys=skip_keys)
                )
        elif payload_name in {"code_review", "verification_result", "qa_validation"}:
            what_details.extend(
                extract_semantic_leaf_lines(payload.get("findings"), prefix="finding: ", skip_keys=skip_keys)
            )
            how_details.extend(
                extract_semantic_leaf_lines(payload.get("passed_checks"), prefix="check: ", skip_keys=skip_keys)
            )
            why_details.extend(
                extract_semantic_leaf_lines(payload.get("residual_risks"), prefix="risk: ", skip_keys=skip_keys)
            )
        elif payload_name == "design_feedback":
            entry_point_label = _designer_entry_point_label(payload.get("entry_point"))
            if entry_point_label:
                what_details.append(f"판단 지점: {entry_point_label}")
            what_details.extend(
                extract_semantic_leaf_lines(payload.get("rules"), prefix="", skip_keys=skip_keys)
            )
            what_details.extend(
                extract_semantic_leaf_lines(payload.get("user_judgment"), prefix="UX 판단: ", skip_keys=skip_keys)
            )
            how_details.extend(_design_feedback_priority_lines(payload.get("message_priority")))
            how_details.extend(
                extract_semantic_leaf_lines(payload.get("required_inputs"), prefix="추가 입력: ", skip_keys=skip_keys)
            )
            how_details.extend(
                extract_semantic_leaf_lines(payload.get("acceptance_criteria"), prefix="완료 기준: ", skip_keys=skip_keys)
            )
            why_details.extend(
                extract_semantic_leaf_lines(payload.get("routing_rationale"), prefix="", skip_keys=skip_keys)
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
                extract_semantic_leaf_lines(
                    payload.get("role_combination_rules"),
                    prefix="협업 경계: ",
                    skip_keys=skip_keys,
                )
            )
        elif payload_name == "research_report":
            headline = _collapse_whitespace(payload.get("headline") or "")
            if headline:
                what_details.append(f"research headline: {headline}")
            what_details.extend(
                extract_semantic_leaf_lines(payload.get("planner_guidance"), prefix="planner guidance: ", skip_keys=skip_keys)
            )
            backing_sources = payload.get("backing_sources")
            if isinstance(backing_sources, list) and backing_sources:
                what_details.append(f"backing sources: {len(backing_sources)}건")
                how_details.extend(
                    extract_semantic_leaf_lines(backing_sources[:3], prefix="", skip_keys=skip_keys)
                )
            why_details.extend(
                extract_semantic_leaf_lines(payload.get("open_questions"), prefix="open question: ", skip_keys=skip_keys)
            )
        else:
            what_details.extend(extract_semantic_leaf_lines(payload, prefix="", skip_keys=skip_keys))
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


def planner_backlog_titles(proposals: dict[str, Any], *, limit: int = 3) -> list[str]:
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


def planner_doc_targets(proposals: dict[str, Any], *, limit: int = 3) -> list[str]:
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


def build_role_result_semantic_context(result: dict[str, Any]) -> dict[str, Any]:
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
        "research_report",
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
    transition = workflow_transition(result)
    result_artifacts = [
        str(item).strip()
        for item in (result.get("artifacts") or [])
        if str(item).strip()
    ]
    acceptance_criteria = normalize_backlog_acceptance_criteria(
        _extract_proposal_acceptance_criteria(proposals)
    )[:3]
    required_inputs = _extract_proposal_required_inputs(proposals)[:3]
    unresolved_items = [
        str(item).strip()
        for item in (transition.get("unresolved_items") or [])
        if str(item).strip()
    ][:3]
    findings = proposal_nested_string_list(proposals, payload_names, "findings")[:3]
    residual_risks = proposal_nested_string_list(proposals, payload_names, "residual_risks")[:3]
    passed_checks = proposal_nested_string_list(proposals, payload_names, "passed_checks")[:3]
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

    semantic_details = proposal_semantic_details(
        proposals,
        payload_names=payload_names,
        transition=transition,
    )
    what_details = list(semantic_details.get("what_details") or [])
    how_details = list(semantic_details.get("how_details") or [])
    why_details = list(semantic_details.get("why_details") or [])
    backlog_titles = planner_backlog_titles(proposals)
    doc_targets = planner_doc_targets(proposals)
    planning_contract = (
        proposals.get("planning_contract")
        if isinstance(proposals.get("planning_contract"), dict)
        else {}
    )
    support_role_entries = _planning_support_role_entries(planning_contract.get("selected_support_roles"))
    support_role_names = [entry["role"] for entry in support_role_entries[:3]]
    revised_milestone_title = _collapse_whitespace(proposals.get("revised_milestone_title") or "")
    research_report = proposals.get("research_report") if isinstance(proposals.get("research_report"), dict) else {}
    research_signal = proposals.get("research_signal") if isinstance(proposals.get("research_signal"), dict) else {}
    research_headline = _collapse_whitespace(research_report.get("headline") or "")
    research_guidance = _collapse_whitespace(research_report.get("planner_guidance") or "")
    research_subject = _collapse_whitespace(research_signal.get("subject") or "")
    research_sources = research_report.get("backing_sources") if isinstance(research_report.get("backing_sources"), list) else []
    design_feedback = proposals.get("design_feedback") if isinstance(proposals.get("design_feedback"), dict) else {}
    designer_entry_point = _designer_entry_point_label(design_feedback.get("entry_point"))
    designer_judgments = _normalize_string_list(design_feedback.get("user_judgment"))[:3]
    designer_message_priority = _design_feedback_priority_lines(design_feedback.get("message_priority"))[:3]
    designer_routing_rationale = _collapse_whitespace(design_feedback.get("routing_rationale") or "")
    if role == "research":
        research_details: list[str] = []
        if research_subject:
            research_details.append(f"research subject: {research_subject}")
        if research_headline:
            research_details.append(f"headline: {research_headline}")
        if research_sources:
            research_details.append(f"backing sources: {len(research_sources)}건")
        what_details = _dedupe_preserving_order(research_details + what_details)[:4]
    elif role == "planner":
        planner_details: list[str] = []
        if revised_milestone_title:
            planner_details.append(f"마일스톤: {revised_milestone_title}")
        if support_role_names:
            planner_details.append("지원 역할: " + ", ".join(support_role_names))
        planner_details.extend(f"backlog/todo: {item}" for item in backlog_titles[:3])
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
        and (revised_milestone_title or backlog_titles)
        and any(marker in what_summary for marker in ("정리했습니다", "확정했습니다", "구조화했습니다"))
    ):
        what_summary = ""
    if not what_summary:
        if role == "research":
            if research_headline and research_sources:
                what_summary = f"{research_headline} | backing sources {len(research_sources)}건을 planner에 전달했습니다."
            elif research_headline:
                what_summary = research_headline
            elif research_subject:
                what_summary = f"{research_subject}에 대한 research 판단을 정리했습니다."
        elif role == "planner":
            if revised_milestone_title and backlog_titles:
                what_summary = f"마일스톤을 {revised_milestone_title}로 정리하고 backlog/todo {len(backlog_titles)}건을 확정했습니다."
            elif revised_milestone_title:
                what_summary = f"마일스톤을 {revised_milestone_title}로 정리했습니다."
            elif backlog_titles:
                what_summary = f"실행 backlog/todo {len(backlog_titles)}건을 정리했습니다."
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
    if role == "research":
        if research_subject:
            context_points.append(f"research subject: {research_subject}")
        if research_sources:
            context_points.append(f"backing sources: {len(research_sources)}건")
        if research_guidance:
            context_points.append(f"planner guidance: {research_guidance}")
    if role == "planner":
        if revised_milestone_title:
            context_points.append(f"마일스톤: {revised_milestone_title}")
        if support_role_names:
            context_points.append("지원 역할: " + ", ".join(support_role_names))
        if backlog_titles:
            context_points.extend(f"backlog/todo: {item}" for item in backlog_titles[:3])
        if doc_targets:
            context_points.extend(f"동기화 문서: {item}" for item in doc_targets[:2])
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
    if role == "research":
        if research_guidance:
            how_fragments.append(f"planner guidance: {research_guidance}")
        if research_sources:
            how_fragments.append(f"source bundle {len(research_sources)}건을 참고합니다.")
    elif role == "planner":
        if doc_targets:
            how_fragments.append("문서 동기화: " + ", ".join(doc_targets[:2]))
        if support_role_names:
            how_fragments.append("지원 역할: " + ", ".join(support_role_names[:2]))
        if backlog_titles:
            how_fragments.append("우선순위 항목: " + ", ".join(backlog_titles[:2]))
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
    if role == "research":
        reason_code = _collapse_whitespace(research_signal.get("reason_code") or "")
        if reason_code:
            why_fragments.append("research 판단 근거: " + research_reason_code_summary(reason_code))
    elif role == "planner":
        if required_inputs:
            why_fragments.append(f"추가 입력 {len(required_inputs)}건이 확보돼야 planning을 닫을 수 있습니다.")
        elif acceptance_criteria:
            why_fragments.append(f"실행 역할이 바로 이어받을 수 있도록 완료 기준 {len(acceptance_criteria)}건을 명시했습니다.")
        elif backlog_titles:
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


def proposal_nested_string_list(
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


def delegate_task_text(request_record: dict[str, Any]) -> str:
    body = str(request_record.get("body") or "").strip()
    scope = str(request_record.get("scope") or "").strip()
    if body and body != scope:
        return body
    return scope or body


def build_handoff_routing_path(
    service: Any,
    request_record: dict[str, Any],
    *,
    source_role: str,
    target_role: str,
) -> str:
    if service._is_internal_sprint_request(request_record):
        nodes = service._build_sprint_routing_path_nodes(request_record, target_role)
        if nodes:
            return " -> ".join(nodes)
    normalized_source = str(source_role or "").strip() or "orchestrator"
    normalized_target = str(target_role or "").strip() or "unknown"
    return f"{normalized_source} -> {normalized_target}"


def build_internal_sprint_delegation_payload(
    service: Any,
    request_record: dict[str, Any],
    next_role: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"next_role": next_role}
    if not service._is_internal_sprint_request(request_record):
        return payload
    routing_path_nodes = service._build_sprint_routing_path_nodes(request_record, next_role)
    if routing_path_nodes:
        payload["routing_path_nodes"] = routing_path_nodes
        payload["routing_path"] = " -> ".join(routing_path_nodes)
    routing_context = dict(request_record.get("routing_context") or {})
    if routing_context:
        payload["routing_context"] = routing_context
    return payload


def synthesize_latest_role_context(service: Any, result: dict[str, Any]) -> dict[str, Any]:
    semantic_context = service._build_role_result_semantic_context(result)
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


def summarize_relay_body(service: Any, envelope: MessageEnvelope) -> list[str]:
    kind = str(envelope.params.get("_teams_kind") or "").strip()
    payload: Any = {}
    if kind == "report" and isinstance(envelope.params.get("result"), dict):
        payload = dict(envelope.params.get("result") or {})
        if not str(payload.get("role") or "").strip():
            payload["role"] = str(envelope.sender or "").strip()
    if not payload:
        payload = service._parse_json_payload_from_text(envelope.body)
    if isinstance(payload, dict) and payload:
        proposals = dict(payload.get("proposals") or {}) if isinstance(payload.get("proposals"), dict) else {}
        transition = workflow_transition(payload)
        payload_names = (
            "research_report",
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
        semantic_context = service._build_role_result_semantic_context(payload)
        error_fragments = relay_summary_text_fragments(payload.get("error") or "", width=116, max_lines=3)
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
                fallback_fragments = relay_summary_text_fragments(candidate, width=116, max_lines=1)
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
                for item in normalize_backlog_acceptance_criteria(
                    _extract_proposal_acceptance_criteria(proposals)
                )[:2]
                if _collapse_whitespace(item)
            ]
        unresolved_items = [str(item).strip() for item in (transition.get("unresolved_items") or []) if str(item).strip()][:2]
        findings = proposal_nested_string_list(proposals, payload_names, "findings")[:2]
        residual_risks = proposal_nested_string_list(proposals, payload_names, "residual_risks")[:2]
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
            acceptance_criteria = normalize_backlog_acceptance_criteria(
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
            for item in proposal_nested_string_list(proposals, payload_names, "findings")[:2]:
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
    fallback_fragments = relay_summary_text_fragments(
        envelope.body or envelope.scope or "",
        width=116,
        max_lines=1,
    )
    if fallback_fragments:
        return [f"- What: {fallback_fragments[0]}"]
    return ["- 본문이 비어 있거나 요약 가능한 필드를 찾지 못했습니다."]


def build_delegation_context(service: Any, request_record: dict[str, Any], next_role: str) -> dict[str, Any]:
    request_id = str(request_record.get("request_id") or "").strip()
    canonical_request = (
        str(service.paths.request_file(request_id).relative_to(service.paths.workspace_root))
        if request_id
        else ""
    )
    routing_context = dict(request_record.get("routing_context") or {})
    workflow_state = service._request_workflow_state(request_record)
    latest_context = synthesize_latest_role_context(service, dict(request_record.get("result") or {}))
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
    findings = _normalize_string_list(proposal_nested_string_list(proposals, payload_names, "findings"))[:2]
    residual_risks = _normalize_string_list(proposal_nested_string_list(proposals, payload_names, "residual_risks"))[:2]
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
        "task_text": delegate_task_text(request_record),
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


def build_delegate_body(service: Any, request_record: dict[str, Any], delegation_context: dict[str, Any]) -> str:
    task_text = str(delegation_context.get("task_text") or "").strip() or delegate_task_text(request_record)
    source_role = str(delegation_context.get("from_role") or "").strip() or "orchestrator"
    target_role = str(delegation_context.get("target_role") or "").strip() or "unknown"
    has_workflow_stage = bool(
        str(delegation_context.get("has_workflow_stage") or "").strip()
        or str(delegation_context.get("workflow_phase") or "").strip()
        or str(delegation_context.get("workflow_step") or "").strip()
    )
    header = f"handoff | {source_role} -> {target_role} | {request_record.get('intent') or 'route'}"
    sections: list[ReportSection] = []
    routing_path = service._build_handoff_routing_path(
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
            if point
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
    append_report_section(sections, "전달 정보", meta_lines)
    append_report_section(sections, "핵심 전달", task_lines)
    if why_this_role and not has_workflow_stage:
        append_report_section(sections, "이관 이유", [f"- {why_this_role}"])
    if check_now_points and not has_workflow_stage:
        append_report_section(sections, "지금 볼 것", [f"- {item}" for item in check_now_points[:2]])
    if constraint_points:
        append_report_section(sections, "유의사항", [f"- {item}" for item in constraint_points[:2]])
    if context_points and not has_workflow_stage:
        append_report_section(sections, "추가 맥락", [f"- {item}" for item in context_points[:2]])

    ref_lines = [f"- 요청 기록: {delegation_context.get('canonical_request') or 'N/A'}"]
    if delegation_context.get("snapshot_path"):
        ref_lines.append(f"- 스냅샷: {delegation_context['snapshot_path']}")
    if delegation_context.get("reference_artifacts"):
        ref_lines.append(
            f"- 참고 산출물: {', '.join(str(item) for item in delegation_context['reference_artifacts'])}"
        )
    ref_lines.append("- 주의: request record가 relay보다 우선합니다.")
    append_report_section(sections, "참고 파일", ref_lines)
    return render_report_sections_message(header, sections)


def format_role_request_snapshot_markdown(
    service: Any,
    *,
    role: str,
    request_record: dict[str, Any],
    delegation_context: dict[str, Any],
) -> str:
    request_id = str(request_record.get("request_id") or "").strip()
    canonical_request = str(service.paths.request_file(request_id).relative_to(service.paths.workspace_root))
    task_text = str(delegation_context.get("task_text") or "").strip() or delegate_task_text(request_record)
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


def write_role_request_snapshot(
    service: Any,
    role: str,
    request_record: dict[str, Any],
    delegation_context: dict[str, Any],
) -> str:
    request_id = str(request_record.get("request_id") or "").strip()
    if not request_id:
        return ""
    snapshot_file = service.paths.role_request_snapshot_file(role, request_id)
    snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    snapshot_file.write_text(
        format_role_request_snapshot_markdown(
            service,
            role=role,
            request_record=request_record,
            delegation_context=delegation_context,
        ),
        encoding="utf-8",
    )
    return str(snapshot_file.relative_to(service.paths.workspace_root))


def build_delegate_envelope(
    service: Any,
    request_record: dict[str, Any],
    next_role: str,
    *,
    delegation_context: dict[str, Any] | None = None,
    extra_params: dict[str, Any] | None = None,
) -> MessageEnvelope:
    delegated_intent = service._intent_for_role(next_role, request_record.get("intent") or "route")
    relay_params = {"_teams_kind": "delegate"}
    if service._is_internal_sprint_request(request_record):
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
    original_requester = merge_requester_route(
        dict(request_record.get("reply_route") or {}) if isinstance(request_record.get("reply_route"), dict) else {},
        extract_original_requester(dict(request_record.get("params") or {})),
    )
    if original_requester:
        relay_params["original_requester"] = original_requester
    delegation_context = dict(delegation_context or request_record.get("delegation_context") or {})
    if not delegation_context:
        delegation_context = build_delegation_context(service, request_record, next_role)
    return MessageEnvelope(
        request_id=request_record["request_id"],
        sender="orchestrator",
        target=next_role,
        intent=delegated_intent,
        urgency=str(request_record.get("urgency") or "normal"),
        scope=str(request_record.get("scope") or ""),
        artifacts=[str(item) for item in request_record.get("artifacts") or []],
        params=relay_params,
        body=build_delegate_body(service, request_record, delegation_context),
    )


def derive_routing_decision_after_report(
    service: Any,
    request_record: dict[str, Any],
    result: dict[str, Any],
    *,
    sender_role: str,
) -> dict[str, Any]:
    workflow_decision = service._derive_workflow_routing_decision(
        request_record,
        result,
        sender_role=sender_role,
    )
    if workflow_decision is not None:
        return workflow_decision
    if service._is_sourcer_review_request(request_record):
        return {
            "next_role": "",
            "routing_context": {},
        }
    current_role = str(result.get("role") or sender_role or request_record.get("current_role") or "").strip()
    if (
        service._is_sprint_planning_request(request_record)
        and current_role == "planner"
        and str(result.get("status") or "").strip().lower() in {"completed", "committed"}
    ):
        return {
            "next_role": "",
            "routing_context": {},
        }
    if current_role == "orchestrator" and not service._request_workflow_state(request_record):
        request_params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
        requested_role = str(request_params.get("user_requested_role") or "").strip().lower()
        seeded_state = service._research_first_workflow_state()
        return service._workflow_route_to_research_initial(
            seeded_state,
            reason=(
                "Selected research as the standard pre-planning step before planner."
                if requested_role != "research"
                else "Selected research because the user explicitly targeted the public research role."
            ),
        )
    requested_next_role = ""
    proposals = dict(result.get("proposals") or {}) if isinstance(result.get("proposals"), dict) else {}

    if service.agent_utilization_policy.verification_result_terminal and proposals.get("verification_result") is not None:
        return {
            "next_role": "",
            "routing_context": {},
        }
    if (
        service.agent_utilization_policy.ignore_non_planner_backlog_proposals_for_routing
        and current_role != "planner"
        and not service._is_internal_sprint_request(request_record)
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

    selection = service._build_governed_routing_selection(
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

    if not service._is_internal_sprint_request(request_record):
        if not next_role:
            return {"next_role": "", "routing_context": {}}
        reason = (
            override_reason
            or f"Selected {next_role} because its strengths match the current request."
        )
        return {
            "next_role": next_role,
            "routing_context": service._build_routing_context(
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
        and service.agent_utilization_policy.sprint_force_qa
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
        "routing_context": service._build_routing_context(
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


async def apply_role_result(
    service: Any,
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
    service._record_internal_visited_role(
        request_record,
        str(result.get("role") or sender_role),
    )
    request_record["result"] = result
    service._append_role_history(
        "orchestrator",
        request_record,
        event_type="role_report",
        summary=str(result.get("summary") or "역할 보고를 수신했습니다."),
        result=result,
    )
    service._record_shared_role_result(request_record, result)
    service._sync_planner_backlog_review_from_role_report(request_record, result)
    service._sync_internal_sprint_artifacts_from_role_report(request_record, result)
    control_outcome = await service._apply_control_action(request_record, result)
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
    if not force_complete and service._request_handling_mode(result) == "complete":
        force_complete = True
    if not force_complete:
        result = service._coerce_nonterminal_workflow_role_result(
            request_record,
            result,
            sender_role=sender_role,
        )
        request_record["result"] = result
    result_status = str(result.get("status") or "").strip().lower()
    if result_status in {"failed", "blocked"} or str(result.get("error") or "").strip():
        workflow_terminal_decision = service._derive_workflow_routing_decision(
            request_record,
            result,
            sender_role=sender_role,
        ) or {}
        workflow_state = dict(workflow_terminal_decision.get("workflow_state") or {})
        if workflow_state:
            service._set_request_workflow_state(request_record, workflow_state)
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
        service._save_request(request_record)
        service._record_internal_sprint_activity(
            request_record,
            event_type=f"request_{request_record['status']}",
            role="orchestrator",
            status=str(request_record.get("status") or ""),
            summary=str(result.get("summary") or result.get("error") or ""),
            payload=result,
        )
        service._append_role_journal(
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
            message_text = service._build_requester_status_message(
                status=reply_status_override or "blocked",
                request_id=reply_request_id,
                summary=str(result.get("summary") or result.get("error") or ""),
            )
        else:
            message_text = service._build_requester_status_message(
                status=reply_status_override or "failed",
                request_id=reply_request_id,
                summary=str(result.get("error") or result.get("summary") or ""),
            )
        await service._reply_to_requester(request_record, message_text)
        return
    prior_workflow_state = service._request_workflow_state(request_record)
    if force_complete:
        routing_decision = {"next_role": "", "routing_context": {}}
    else:
        routing_decision = service._derive_routing_decision_after_report(
            request_record,
            result,
            sender_role=sender_role,
        )
    workflow_state = dict(routing_decision.get("workflow_state") or {})
    if workflow_state:
        service._set_request_workflow_state(request_record, workflow_state)
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
        sprint_state = service._load_sprint_state(sprint_id) if sprint_id else {}
        if sprint_state:
            try:
                await service._send_sprint_spec_todo_report(
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
                blocked_state = dict(service._request_workflow_state(request_record) or service._default_workflow_state())
                blocked_state["phase"] = WORKFLOW_PHASE_PLANNING
                blocked_state["step"] = WORKFLOW_STEP_PLANNER_FINALIZE
                blocked_state["phase_owner"] = "planner"
                blocked_state["phase_status"] = "blocked"
                service._set_request_workflow_state(request_record, blocked_state)
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
        service._save_request(request_record)
        service._record_internal_sprint_activity(
            request_record,
            event_type="request_blocked",
            role="orchestrator",
            status="blocked",
            summary=str(result.get("summary") or result.get("error") or ""),
            payload=result,
        )
        service._append_role_journal(
            "orchestrator",
            request_record,
            title="blocked",
            lines=[
                f"- role: {result.get('role') or sender_role}",
                f"- summary: {result.get('summary') or ''}",
                f"- error: {result.get('error') or '없음'}",
            ],
        )
        await service._reply_to_requester(
            request_record,
            service._build_requester_status_message(
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
        service._save_request(request_record)
        service._record_internal_sprint_activity(
            request_record,
            event_type="role_delegated",
            role="orchestrator",
            status=str(request_record.get("status") or ""),
            summary=f"{next_role} 역할로 다시 위임했습니다.",
            payload=service._build_internal_sprint_delegation_payload(request_record, next_role),
        )
        service._append_role_history(
            "orchestrator",
            request_record,
            event_type="delegated",
            summary=(
                f"{next_role} 역할로 다시 위임했습니다. "
                f"{request_record['routing_context'].get('reason') or ''}"
            ).strip(),
            result=result,
        )
        relay_sent = await service._delegate_request(request_record, next_role)
        await service._reply_to_requester(
            request_record,
            service._build_requester_status_message(
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
    service._save_request(request_record)
    service._record_internal_sprint_activity(
        request_record,
        event_type="request_completed",
        role="orchestrator",
        status="completed",
        summary=str(result.get("summary") or "요청이 완료되었습니다."),
        payload=result,
    )
    service._append_role_history(
        "orchestrator",
        request_record,
        event_type="completed",
        summary=str(result.get("summary") or "요청이 완료되었습니다."),
        result=result,
    )
    resumed_request_ids = await service._resume_requests_from_verification_result(request_record, result)
    await service._reply_to_requester(
        request_record,
        service._build_requester_status_message(
            status=reply_status_override or "completed",
            request_id=reply_request_id,
            summary=str(result.get("summary") or ""),
            related_request_ids=resumed_request_ids,
        ),
    )


async def process_delegated_request(service: Any, envelope: MessageEnvelope, request_record: dict[str, Any]) -> None:
    service._record_internal_sprint_activity(
        request_record,
        event_type="role_started",
        role=service.role,
        status="running",
        summary=(
            f"{service._initial_phase_step_title(service._initial_phase_step(request_record))}을 시작했습니다."
            if service._is_initial_phase_planner_request(request_record)
            else f"{service.role} 역할이 요청 처리를 시작했습니다."
        ),
    )
    await service._maybe_report_planner_initial_phase_activity(
        request_record,
        event_type="role_started",
        status="running",
        summary=(
            f"{service._initial_phase_step_title(service._initial_phase_step(request_record))}을 시작했습니다."
            if service._is_initial_phase_planner_request(request_record)
            else f"{service.role} 역할이 요청 처리를 시작했습니다."
        ),
    )
    try:
        result = await asyncio.to_thread(service.role_runtime.run_task, envelope, request_record)
    except Exception as exc:
        LOGGER.exception("Role %s failed while processing request %s", service.role, request_record.get("request_id"))
        result = {
            "request_id": request_record["request_id"],
            "role": service.role,
            "status": "failed",
            "summary": f"{service.role} 역할 처리 중 오류가 발생했습니다.",
            "proposals": {},
            "artifacts": [],
            "next_role": "",
            "error": str(exc),
        }
    result = normalize_role_payload(result)
    request_record["result"] = dict(result)
    service._persist_request_result(request_record)
    service._record_internal_sprint_activity(
        request_record,
        event_type="role_result",
        role=str(result.get("role") or service.role),
        status=str(result.get("status") or ""),
        summary=str(result.get("summary") or result.get("error") or ""),
        payload=result,
    )
    result_status = str(result.get("status") or "").strip().lower()
    if service._is_initial_phase_planner_request(request_record):
        report_event_type = "planner_checkpoint" if result_status in {"completed", "committed"} else "role_result"
        await service._maybe_report_planner_initial_phase_activity(
            request_record,
            event_type=report_event_type,
            status=result_status or "completed",
            summary=str(result.get("summary") or result.get("error") or ""),
            payload=result,
        )
    service._append_role_history(
        service.role,
        request_record,
        event_type="role_result",
        summary=str(result.get("summary") or ""),
        result=result,
    )
    insights = normalize_insights(result)
    if insights:
        service._append_role_journal(
            service.role,
            request_record,
            title="insights",
            lines=[f"- {insight}" for insight in insights],
        )
    if str(result.get("status") or "").strip().lower() in {"failed", "blocked"} or str(result.get("error") or "").strip():
        service._append_role_journal(
            service.role,
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
        sender=service.role,
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
    await service._send_relay(result_envelope)


async def delegate_request(service: Any, request_record: dict[str, Any], next_role: str) -> bool:
    delegation_context = build_delegation_context(service, request_record, next_role)
    snapshot_path = write_role_request_snapshot(service, next_role, request_record, delegation_context)
    if snapshot_path:
        delegation_context["snapshot_path"] = snapshot_path
    request_record["delegation_context"] = dict(delegation_context)
    service._save_request(request_record)
    envelope = build_delegate_envelope(
        service,
        request_record,
        next_role,
        delegation_context=delegation_context,
    )
    return await service._send_relay(envelope, request_record=request_record)


async def run_local_orchestrator_request(service: Any, request_record: dict[str, Any]) -> None:
    request_id = str(request_record.get("request_id") or "").strip()
    if not request_id or not await service._claim_request(request_id):
        LOGGER.info("Skipping local orchestrator request already in progress: %s", request_id or "unknown")
        return
    try:
        envelope = service._build_delegate_envelope(
            request_record,
            "orchestrator",
            extra_params={"_teams_kind": "local_delegate"},
        )
        try:
            result = await asyncio.to_thread(service.role_runtime.run_task, envelope, request_record)
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
        await service._apply_role_result(
            request_record,
            result,
            sender_role="orchestrator",
        )
    finally:
        await service._release_request(request_id)


async def handle_delegated_request(service: Any, message: Any, envelope: MessageEnvelope) -> None:
    request_record = service._load_request(envelope.request_id or "")
    if not request_record:
        return
    request_id = str(request_record.get("request_id") or "").strip()
    if not request_id or not await service._claim_request(request_id):
        LOGGER.info(
            "Skipping delegated request already in progress for role %s: %s",
            service.role,
            request_id or "unknown",
        )
        return
    try:
        await service._process_delegated_request(envelope, request_record)
    finally:
        await service._release_request(request_id)


async def handle_role_report(service: Any, message: Any, envelope: MessageEnvelope) -> None:
    request_record = service._load_request(envelope.request_id or "")
    if not request_record:
        return
    result = envelope.params.get("result") if isinstance(envelope.params.get("result"), dict) else {}
    if not result:
        parsed = service._parse_json_payload_from_text(envelope.body)
        result = parsed if isinstance(parsed, dict) else {}
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
    stale_reason = service._stale_role_report_reason(request_record, envelope, result)
    if stale_reason:
        LOGGER.info(
            "Ignoring stale role report for request %s from %s: %s",
            request_record.get("request_id") or "unknown",
            str(envelope.sender or result.get("role") or "unknown").strip() or "unknown",
            stale_reason,
        )
        return
    result = service._enforce_workflow_role_report_contract(request_record, result)
    await service._apply_role_result(
        request_record,
        result,
        sender_role=str(envelope.sender or ""),
    )
