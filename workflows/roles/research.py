from __future__ import annotations

import json
import logging
import re
from typing import Any

from teams_runtime.shared.models import (
    MessageEnvelope,
    PromptContextRuntimeConfig,
    RequestRecord,
)
from teams_runtime.shared.prompt_context import (
    project_request_record_for_prompt,
    render_prompt_event_history_notice,
)


LOGGER = logging.getLogger(__name__)

RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING = "needed_external_grounding"
RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE = "not_needed_local_evidence"
RESEARCH_REASON_CODE_NOT_NEEDED_NO_SUBJECT = "not_needed_no_subject"
RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED = "blocked_decision_failed"

ALLOWED_RESEARCH_SIGNAL_REASON_CODES = {
    RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING,
    RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE,
    RESEARCH_REASON_CODE_NOT_NEEDED_NO_SUBJECT,
    RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED,
}

MODEL_RESEARCH_SIGNAL_REASON_CODES = {
    RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING,
    RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE,
    RESEARCH_REASON_CODE_NOT_NEEDED_NO_SUBJECT,
}

RESEARCH_PLANNING_HINT_FIELDS = (
    "milestone_refinement_hints",
    "problem_framing_hints",
    "spec_implications",
    "todo_definition_hints",
)
RESEARCH_REPORT_LIST_FIELDS = (
    *RESEARCH_PLANNING_HINT_FIELDS,
    "backing_reasoning",
    "open_questions",
)
RESEARCH_SUBJECT_DEFINITION_FIELDS = (
    "planning_decision",
    "knowledge_gap",
    "external_boundary",
    "planner_impact",
    "candidate_subject",
    "research_query",
    "source_requirements",
    "rejected_subjects",
    "no_subject_rationale",
)
REQUIREMENT_TRACEABILITY_MATRIX_FIELD = "requirement_traceability_matrix"
ALLOWED_REQUIREMENT_KINDS = {
    "external_fact",
    "repo_state",
    "implementation_evidence",
    "local_policy",
    "preference",
    "mixed",
}
ALLOWED_LOCAL_EVIDENCE_TYPES = {
    "artifact",
    "runtime_observation",
    "comment",
    "opinion",
    "assumption",
}
REQUIREMENT_ID_PATTERN = re.compile(r"\bREQ-\d{3}\b", re.IGNORECASE)


def _collapse_whitespace(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


def _normalize_text_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_collapse_whitespace(item) for item in value if _collapse_whitespace(item)]
    if isinstance(value, str):
        lines = [
            _collapse_whitespace(line.strip("- ").strip())
            for line in value.splitlines()
        ]
        return [line for line in lines if line]
    return []


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y"}:
            return True
        if normalized in {"false", "0", "no", "n", ""}:
            return False
    return bool(value)


def _normalize_comparison_text(value: Any) -> str:
    return re.sub(r"\W+", "", str(value or "").strip().lower())


def _omit_empty_fields(value: Any) -> Any:
    if isinstance(value, dict):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            normalized = _omit_empty_fields(item)
            if normalized not in ("", [], {}, None):
                compact[key] = normalized
        return compact
    if isinstance(value, list):
        return [
            normalized
            for item in value
            if (normalized := _omit_empty_fields(item)) not in ("", [], {}, None)
        ]
    return value


def _request_seed_texts(request_record: RequestRecord) -> list[str]:
    params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
    values = [
        request_record.get("scope"),
        request_record.get("body"),
        params.get("requested_milestone_title"),
        params.get("milestone_title"),
        request_record.get("intent"),
    ]
    return [_collapse_whitespace(value) for value in values if _collapse_whitespace(value)]


def _matches_raw_request_text(candidate: str, request_record: RequestRecord) -> bool:
    normalized_candidate = _normalize_comparison_text(candidate)
    if not normalized_candidate:
        return False
    for seed in _request_seed_texts(request_record):
        normalized_seed = _normalize_comparison_text(seed)
        if normalized_seed and normalized_candidate == normalized_seed:
            return True
    return False


def closeout_original_requirements_from_request(
    request_record: RequestRecord | None,
) -> list[dict[str, str]]:
    if not isinstance(request_record, dict):
        return []
    params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
    candidates: list[Any] = [
        params.get("original_requirements"),
        request_record.get("original_requirements"),
    ]
    sprint_state = params.get("sprint_state")
    if isinstance(sprint_state, dict):
        candidates.append(sprint_state.get("original_requirements"))
    sprint_state = request_record.get("sprint_state")
    if isinstance(sprint_state, dict):
        candidates.append(sprint_state.get("original_requirements"))

    requirements: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for raw_requirements in candidates:
        if not isinstance(raw_requirements, list):
            continue
        for raw_item in raw_requirements:
            if not isinstance(raw_item, dict):
                continue
            if not _coerce_bool(raw_item.get("closeout_required", True)):
                continue
            req_id = str(raw_item.get("id") or raw_item.get("req_id") or "").strip().upper()
            if not REQUIREMENT_ID_PATTERN.fullmatch(req_id):
                continue
            if req_id in seen_ids:
                continue
            requirement = _collapse_whitespace(raw_item.get("text") or raw_item.get("requirement") or "")
            requirements.append({"req_id": req_id, "requirement": requirement})
            seen_ids.add(req_id)
        if requirements:
            break
    return requirements


def _normalize_local_evidence(value: Any, *, req_id: str) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    raw_items = value if isinstance(value, list) else [value]
    evidence: list[dict[str, str]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise ValueError(f"{req_id} local_evidence entries must be objects with a type.")
        evidence_type = str(raw_item.get("type") or "").strip().lower()
        if evidence_type not in ALLOWED_LOCAL_EVIDENCE_TYPES:
            raise ValueError(f"{req_id} local_evidence has unsupported type: {evidence_type or 'empty'}")
        normalized = {
            "type": evidence_type,
            "source": _collapse_whitespace(raw_item.get("source") or raw_item.get("artifact") or raw_item.get("ref") or ""),
            "summary": _collapse_whitespace(raw_item.get("summary") or raw_item.get("rationale") or raw_item.get("text") or ""),
        }
        if _collapse_whitespace(raw_item.get("quote") or ""):
            normalized["quote"] = _collapse_whitespace(raw_item.get("quote") or "")
        evidence.append(normalized)
    return evidence


def _evidence_can_satisfy_requirement_kind(
    *,
    requirement_kind: str,
    evidence: list[dict[str, str]],
) -> bool:
    evidence_types = {str(item.get("type") or "").strip().lower() for item in evidence}
    if not evidence_types:
        return False
    if requirement_kind in {"local_policy", "preference"}:
        return bool(evidence_types & {"artifact", "runtime_observation", "comment", "opinion"})
    return bool(evidence_types & {"artifact", "runtime_observation"})


def _synthesize_research_subject_from_rtm(rows: list[dict[str, Any]]) -> tuple[str, str, list[str]]:
    req_ids = [str(row.get("req_id") or "").strip() for row in rows if str(row.get("req_id") or "").strip()]
    joined_ids = ", ".join(req_ids[:6])
    subject = f"Closeout evidence gaps for {joined_ids}" if joined_ids else "Closeout requirement evidence gaps"
    gap_lines = []
    source_requirements: list[str] = []
    for row in rows:
        req_id = str(row.get("req_id") or "").strip()
        missing = "; ".join(_normalize_text_list(row.get("missing_evidence")))
        delta = _collapse_whitespace(row.get("research_query_delta") or "")
        requirement = _collapse_whitespace(row.get("requirement") or "")
        gap_lines.append(
            f"{req_id}: {delta or missing or requirement}".strip()
        )
        if delta:
            source_requirements.append(delta)
    query = (
        "Resolve these closeout requirement evidence gaps with authoritative, current sources where external grounding is needed: "
        + " | ".join(item for item in gap_lines if item)
    )
    if not source_requirements:
        source_requirements = [
            "Use authoritative or primary sources for every unresolved requirement evidence gap."
        ]
    return subject, query, source_requirements


def normalize_requirement_traceability_matrix(
    value: Any,
    *,
    request_record: RequestRecord | None = None,
) -> list[dict[str, Any]]:
    expected_requirements = closeout_original_requirements_from_request(request_record)
    expected_by_id = {item["req_id"]: item for item in expected_requirements}
    if value in (None, ""):
        raw_rows: list[Any] = []
    elif isinstance(value, list):
        raw_rows = value
    else:
        raise ValueError("requirement_traceability_matrix must be a list.")

    if not expected_by_id and raw_rows:
        raise ValueError("requirement_traceability_matrix included REQ-* rows without structured original_requirements.")

    normalized_rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_row in raw_rows:
        if not isinstance(raw_row, dict):
            raise ValueError("requirement_traceability_matrix rows must be JSON objects.")
        req_id = str(raw_row.get("req_id") or raw_row.get("id") or "").strip().upper()
        if req_id not in expected_by_id:
            raise ValueError(f"requirement_traceability_matrix has unknown req_id: {req_id or 'empty'}")
        if req_id in seen_ids:
            raise ValueError(f"requirement_traceability_matrix has duplicate req_id: {req_id}")
        seen_ids.add(req_id)

        requirement_kind = str(raw_row.get("requirement_kind") or "").strip().lower()
        if requirement_kind not in ALLOWED_REQUIREMENT_KINDS:
            raise ValueError(f"{req_id} has unsupported requirement_kind: {requirement_kind or 'empty'}")
        planner_decisions = _normalize_text_list(
            raw_row.get("planner_decisions")
            or raw_row.get("planner_decision")
        )
        if not planner_decisions:
            raise ValueError(f"{req_id} must include planner_decisions.")
        decision_rationale = _collapse_whitespace(raw_row.get("decision_rationale") or "")
        if not decision_rationale:
            raise ValueError(f"{req_id} must include decision_rationale.")

        local_evidence = _normalize_local_evidence(raw_row.get("local_evidence"), req_id=req_id)
        local_evidence_sufficient = _coerce_bool(raw_row.get("local_evidence_sufficient"))
        missing_evidence = _normalize_text_list(raw_row.get("missing_evidence"))
        if not local_evidence and not missing_evidence:
            raise ValueError(f"{req_id} must include local_evidence or explicit missing_evidence.")
        if local_evidence_sufficient and not local_evidence:
            raise ValueError(f"{req_id} cannot be locally sufficient without local_evidence.")
        if local_evidence_sufficient and not _evidence_can_satisfy_requirement_kind(
            requirement_kind=requirement_kind,
            evidence=local_evidence,
        ):
            raise ValueError(
                f"{req_id} local_evidence cannot satisfy requirement_kind={requirement_kind}."
            )
        if not local_evidence_sufficient and not missing_evidence:
            raise ValueError(f"{req_id} must include missing_evidence when local evidence is insufficient.")

        research_reopen_required = _coerce_bool(raw_row.get("research_reopen_required")) or not local_evidence_sufficient
        research_query_delta = _collapse_whitespace(raw_row.get("research_query_delta") or "")
        if research_reopen_required and not research_query_delta:
            raise ValueError(f"{req_id} must include research_query_delta when research_reopen_required=true.")
        research_status = _collapse_whitespace(raw_row.get("research_status") or "")
        if not research_status:
            research_status = "pending_external_research" if research_reopen_required else "local_sufficient"

        normalized_rows.append(
            {
                "req_id": req_id,
                "requirement": expected_by_id[req_id]["requirement"],
                "requirement_kind": requirement_kind,
                "planner_decisions": planner_decisions,
                "local_evidence": local_evidence,
                "local_evidence_sufficient": local_evidence_sufficient,
                "missing_evidence": missing_evidence,
                "research_reopen_required": research_reopen_required,
                "research_query_delta": research_query_delta,
                "research_status": research_status,
                "decision_rationale": decision_rationale,
                "source_refs": _normalize_text_list(raw_row.get("source_refs")),
                "failure_refs": _normalize_text_list(raw_row.get("failure_refs")),
            }
        )

    expected_ids = [item["req_id"] for item in expected_requirements]
    missing_ids = [req_id for req_id in expected_ids if req_id not in seen_ids]
    if missing_ids:
        raise ValueError(
            "requirement_traceability_matrix is missing closeout-required rows: "
            + ", ".join(missing_ids)
        )
    return normalized_rows


def requirement_traceability_rows_requiring_research(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in rows
        if _coerce_bool(row.get("research_reopen_required"))
    ]


def source_refs_from_backing_sources(sources: Any) -> list[str]:
    refs: list[str] = []
    if not isinstance(sources, list):
        return refs
    for item in sources:
        if not isinstance(item, dict):
            continue
        title = _collapse_whitespace(item.get("title") or "")
        url = _collapse_whitespace(item.get("url") or "")
        ref = " | ".join(part for part in (title, url) if part)
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def mark_requirement_traceability_research_status(
    rows: list[dict[str, Any]],
    *,
    research_status: str,
    source_refs: Any = None,
    failure_details: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normalized_status = _collapse_whitespace(research_status or "")
    source_ref_list = _normalize_text_list(source_refs)
    failure_ref = ""
    if failure_details:
        stage = _collapse_whitespace(failure_details.get("failure_stage") or "")
        exception_type = _collapse_whitespace(failure_details.get("exception_type") or "")
        research_url = _collapse_whitespace(failure_details.get("research_url") or "")
        failure_ref = " | ".join(
            part
            for part in (
                f"stage={stage}" if stage else "",
                f"exception={exception_type}" if exception_type else "",
                research_url,
            )
            if part
        )
    updated_rows: list[dict[str, Any]] = []
    for row in rows:
        updated = dict(row)
        if _coerce_bool(updated.get("research_reopen_required")):
            updated["research_status"] = normalized_status or updated.get("research_status") or ""
            if source_ref_list:
                updated["source_refs"] = source_ref_list
            if failure_ref:
                updated["failure_refs"] = [failure_ref]
        updated_rows.append(updated)
    return updated_rows


def _subject_definition_from_payload(raw_payload: dict[str, Any]) -> dict[str, Any]:
    raw_definition = raw_payload.get("research_subject_definition")
    if not isinstance(raw_definition, dict):
        raw_definition = {}
    return {
        "planning_decision": _collapse_whitespace(raw_definition.get("planning_decision") or ""),
        "knowledge_gap": _collapse_whitespace(raw_definition.get("knowledge_gap") or ""),
        "external_boundary": _collapse_whitespace(raw_definition.get("external_boundary") or ""),
        "planner_impact": _collapse_whitespace(raw_definition.get("planner_impact") or ""),
        "candidate_subject": _collapse_whitespace(
            raw_definition.get("candidate_subject")
            or raw_payload.get("subject")
            or ""
        ),
        "research_query": _collapse_whitespace(
            raw_definition.get("research_query")
            or raw_payload.get("research_query")
            or ""
        ),
        "source_requirements": _normalize_text_list(raw_definition.get("source_requirements")),
        "rejected_subjects": _normalize_text_list(raw_definition.get("rejected_subjects")),
        "no_subject_rationale": _collapse_whitespace(raw_definition.get("no_subject_rationale") or ""),
    }


def normalize_research_subject_definition(
    raw_payload: dict[str, Any],
    *,
    reason_code: str,
    needed: bool,
    request_record: RequestRecord | None = None,
) -> dict[str, Any]:
    definition = _subject_definition_from_payload(raw_payload)
    if reason_code == RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING:
        if not needed:
            raise ValueError("Research subject definition used needed_external_grounding with needed=false.")
        required_fields = (
            "planning_decision",
            "knowledge_gap",
            "external_boundary",
            "planner_impact",
            "candidate_subject",
            "research_query",
        )
        for field in required_fields:
            if not definition[field]:
                raise ValueError(f"Research subject definition must provide {field} when external research is needed.")
        if not definition["source_requirements"]:
            raise ValueError("Research subject definition must provide source_requirements when external research is needed.")
        if request_record is not None and _matches_raw_request_text(definition["candidate_subject"], request_record):
            raise ValueError("Research subject definition candidate_subject must not simply copy the user request or milestone.")
        if not definition["external_boundary"]:
            raise ValueError("Research subject definition must identify an external_boundary.")
    elif reason_code == RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE:
        if needed:
            raise ValueError("Research subject definition used not_needed_local_evidence with needed=true.")
        required_fields = (
            "planning_decision",
            "knowledge_gap",
            "candidate_subject",
            "research_query",
            "planner_impact",
        )
        for field in required_fields:
            if not definition[field]:
                raise ValueError(f"Research subject definition must provide {field} when local evidence is sufficient.")
    elif reason_code == RESEARCH_REASON_CODE_NOT_NEEDED_NO_SUBJECT:
        if needed:
            raise ValueError("Research subject definition used not_needed_no_subject with needed=true.")
        if definition["candidate_subject"] or definition["research_query"]:
            raise ValueError("Research subject definition must leave candidate_subject and research_query empty when no subject exists.")
        if not definition["no_subject_rationale"]:
            raise ValueError("Research subject definition must provide no_subject_rationale when no external subject exists.")
    return definition


def default_research_signal(
    *,
    reason_code: str = RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED,
) -> dict[str, Any]:
    normalized_reason = (
        reason_code
        if reason_code in ALLOWED_RESEARCH_SIGNAL_REASON_CODES
        else RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED
    )
    return {
        "needed": False,
        "subject": "",
        "research_query": "",
        "reason_code": normalized_reason,
    }


def research_reason_code_summary(reason_code: Any) -> str:
    normalized = str(reason_code or "").strip()
    if normalized == RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING:
        return "외부 grounding이 planning 판단을 바꿀 수 있어 deep research가 필요합니다."
    if normalized == RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE:
        return "검토한 local artifact만으로 planner가 planning을 이어갈 수 있습니다."
    if normalized == RESEARCH_REASON_CODE_NOT_NEEDED_NO_SUBJECT:
        return "planner 판단을 바꿀 외부 research subject가 현재 요청에 없습니다."
    if normalized == RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED:
        return "research 필요 판단 자체를 완료하지 못했습니다."
    return "research 판단 사유를 표준 reason code로 복구하지 못했습니다."


def normalize_research_decision(
    raw_payload: dict[str, Any],
    *,
    request_record: RequestRecord | None = None,
) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raise ValueError("Research decision response must be a JSON object.")
    decision_payload = dict(raw_payload)
    reason_code = str(raw_payload.get("reason_code") or "").strip()
    if reason_code not in MODEL_RESEARCH_SIGNAL_REASON_CODES:
        raise ValueError(f"Unsupported research reason_code: {reason_code or 'empty'}")
    needed = _coerce_bool(raw_payload.get("needed"))
    requirement_traceability_matrix = normalize_requirement_traceability_matrix(
        raw_payload.get(REQUIREMENT_TRACEABILITY_MATRIX_FIELD),
        request_record=request_record,
    )
    rows_requiring_research = requirement_traceability_rows_requiring_research(requirement_traceability_matrix)
    reopened_from_reason_code = ""
    if rows_requiring_research and (not needed or reason_code != RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING):
        reopened_from_reason_code = reason_code
        reason_code = RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING
        needed = True
    if rows_requiring_research and reason_code == RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING:
        synthesized_subject, synthesized_query, synthesized_source_requirements = _synthesize_research_subject_from_rtm(
            rows_requiring_research
        )
        if not _collapse_whitespace(decision_payload.get("subject") or ""):
            decision_payload["subject"] = synthesized_subject
        if not _collapse_whitespace(decision_payload.get("research_query") or ""):
            decision_payload["research_query"] = synthesized_query
        raw_definition = (
            dict(decision_payload.get("research_subject_definition") or {})
            if isinstance(decision_payload.get("research_subject_definition"), dict)
            else {}
        )
        synthesized_definition_defaults = {
            "planning_decision": "planner closeout requirement coverage and sprint backlog/todo traceability",
            "knowledge_gap": "some closeout-required REQ-* rows lack sufficient local evidence",
            "external_boundary": "Deep Research may provide current external grounding for unresolved requirement evidence gaps",
            "planner_impact": "planner should keep unresolved RTM rows visible in backlog, todos, acceptance criteria, and closeout evidence",
            "candidate_subject": decision_payload["subject"],
            "research_query": decision_payload["research_query"],
        }
        for field, default_value in synthesized_definition_defaults.items():
            if not _collapse_whitespace(raw_definition.get(field) or ""):
                raw_definition[field] = default_value
        if not _normalize_text_list(raw_definition.get("source_requirements")):
            raw_definition["source_requirements"] = synthesized_source_requirements
        decision_payload["research_subject_definition"] = raw_definition

    subject_definition = normalize_research_subject_definition(
        decision_payload,
        reason_code=reason_code,
        needed=needed,
        request_record=request_record,
    )
    subject = subject_definition["candidate_subject"]
    research_query = subject_definition["research_query"]
    planner_guidance = _collapse_whitespace(raw_payload.get("planner_guidance") or "")

    if reason_code == RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING:
        if not needed:
            raise ValueError("Research decision used needed_external_grounding with needed=false.")
        if not subject:
            raise ValueError("Research decision must provide subject when research is needed.")
        if not research_query:
            raise ValueError("Research decision must provide research_query when research is needed.")
    elif reason_code == RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE:
        if needed:
            raise ValueError("Research decision used not_needed_local_evidence with needed=true.")
        if not subject:
            raise ValueError("Research decision must provide subject when local evidence is sufficient.")
        if not research_query:
            raise ValueError("Research decision must provide research_query when local evidence is sufficient.")
    elif reason_code == RESEARCH_REASON_CODE_NOT_NEEDED_NO_SUBJECT:
        if needed:
            raise ValueError("Research decision used not_needed_no_subject with needed=true.")
        subject = ""
        research_query = ""
    if not needed and any(
        not _coerce_bool(row.get("local_evidence_sufficient")) or _coerce_bool(row.get("research_reopen_required"))
        for row in requirement_traceability_matrix
    ):
        raise ValueError("Research decision used needed=false with unresolved requirement_traceability_matrix rows.")

    signal: dict[str, Any] = {
        "needed": needed,
        "subject": subject,
        "research_query": research_query,
        "reason_code": reason_code,
    }
    if reopened_from_reason_code:
        signal["reopened_from_reason_code"] = reopened_from_reason_code

    return {
        "signal": signal,
        "research_subject_definition": subject_definition,
        REQUIREMENT_TRACEABILITY_MATRIX_FIELD: requirement_traceability_matrix,
        "planner_guidance": planner_guidance,
    }


def build_research_decision_prompt(
    envelope: MessageEnvelope,
    request_record: RequestRecord,
    *,
    local_sources_checked: list[str],
    prompt_context_config: PromptContextRuntimeConfig | None = None,
) -> str:
    request_projection = project_request_record_for_prompt(
        request_record,
        prompt_context_config,
    )
    if request_projection.compacted:
        LOGGER.info(
            "[research] prompt_context_compacted request_id=%s purpose=research_decision total_events=%s "
            "included_events=%s omitted_events=%s recent_events=%s max_events=%s",
            str(request_record.get("request_id") or "unknown"),
            request_projection.total_events,
            request_projection.included_events,
            request_projection.omitted_events,
            request_projection.recent_events,
            request_projection.max_events,
        )
    event_history_notice = render_prompt_event_history_notice(request_projection)
    params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
    public_targeted = str(params.get("user_requested_role") or "").strip().lower() == "research"
    closeout_requirements = closeout_original_requirements_from_request(request_record)
    return "\n".join(
        [
            "You are the research prepass decision gate inside teams_runtime.",
            "Decide whether planner needs external grounding beyond the current local request, repo context, and sprint artifacts.",
            "You must first build the requirement_traceability_matrix for structured closeout-required original_requirements, then define the research subject, then choose the reason_code.",
            "Do not use keyword heuristics. Read the provided request context and make the judgment.",
            "Return strict JSON only with this exact shape:",
            "{",
            '  "needed": true,',
            '  "subject": "",',
            '  "research_query": "",',
            '  "reason_code": "needed_external_grounding|not_needed_local_evidence|not_needed_no_subject",',
            '  "requirement_traceability_matrix": [',
            "    {",
            '      "req_id": "REQ-001",',
            '      "requirement": "exact normalized requirement text",',
            '      "requirement_kind": "external_fact|repo_state|implementation_evidence|local_policy|preference|mixed",',
            '      "planner_decisions": ["planner decision this requirement affects"],',
            '      "local_evidence": [{"type":"artifact|runtime_observation|comment|opinion|assumption","source":"","summary":""}],',
            '      "local_evidence_sufficient": false,',
            '      "missing_evidence": ["explicit missing evidence"],',
            '      "research_reopen_required": true,',
            '      "research_query_delta": "specific query delta needed for this REQ-*",',
            '      "research_status": "pending_external_research|local_sufficient",',
            '      "decision_rationale": "why this row is sufficient or must reopen research"',
            "    }",
            "  ],",
            '  "research_subject_definition": {',
            '    "planning_decision": "",',
            '    "knowledge_gap": "",',
            '    "external_boundary": "",',
            '    "planner_impact": "",',
            '    "candidate_subject": "",',
            '    "research_query": "",',
            '    "source_requirements": [],',
            '    "rejected_subjects": [],',
            '    "no_subject_rationale": ""',
            "  },",
            '  "planner_guidance": "짧은 한국어 planner guidance"',
            "}",
            "Rules:",
            "- `needed_external_grounding`: use only when external sources could materially change planner decisions.",
            "- `not_needed_local_evidence`: use when there is a concrete research-shaped question, but local repo/request/sprint evidence is already enough for planner.",
            "- `not_needed_no_subject`: use when the request does not contain a genuine external research subject.",
            "- The runtime reserves `blocked_decision_failed`; do not emit it.",
            "- `planning_decision`: the concrete planner decision this research may change.",
            "- `knowledge_gap`: what planner cannot responsibly decide from local request/repo/sprint context alone.",
            "- `external_boundary`: why outside/current/domain knowledge is needed instead of repo inspection.",
            "- `planner_impact`: how answers should affect milestone wording, spec boundaries, acceptance criteria, dependencies, priorities, or backlog slicing.",
            "- If `original_requirements` / `REQ-*` records exist, cite affected IDs in planner_guidance and do not recommend weakened scope without user-approved variance.",
            "- Build exactly one requirement_traceability_matrix row for each structured closeout-required `params.original_requirements` / sprint `original_requirements` item. Do not infer rows from free-text REQ-* regex matches.",
            "- Do not emit missing, duplicate, or unknown `REQ-*` rows. If no structured closeout-required original_requirements exist, emit an empty matrix.",
            "- Every RTM row needs planner_decisions, decision_rationale, and either concrete local_evidence or explicit missing_evidence.",
            "- Allowed requirement_kind values: external_fact, repo_state, implementation_evidence, local_policy, preference, mixed.",
            "- Allowed local_evidence.type values: artifact, runtime_observation, comment, opinion, assumption.",
            "- comment/opinion evidence can make a row locally sufficient only for local_policy or preference requirements.",
            "- assumption evidence never makes factual, repo-state, mixed, or implementation rows locally sufficient.",
            "- Missing local evidence must set local_evidence_sufficient=false, research_reopen_required=true, research_status=pending_external_research, and a concrete research_query_delta.",
            "- `needed=false` is valid only when every RTM row has local_evidence_sufficient=true and research_reopen_required=false.",
            "- `candidate_subject`: the smallest researchable external subject; it must be narrower than the whole milestone and must not copy the user request.",
            "- `research_query`: the exact query/instruction for deep research.",
            "- `source_requirements`: official/primary/recency/comparison/source-diversity needs for deep research.",
            "- `rejected_subjects`: near-miss subjects excluded as too broad, repo-only, locally known, or not planner-impacting.",
            "- `no_subject_rationale`: required only for `not_needed_no_subject`.",
            "- When reason_code is `needed_external_grounding` or `not_needed_local_evidence`, `subject`, `research_query`, `research_subject_definition.candidate_subject`, and `research_subject_definition.research_query` must all be non-empty and concrete.",
            "- When reason_code is `not_needed_no_subject`, subject/query fields must be empty strings and `no_subject_rationale` must be non-empty.",
            "- `planner_guidance` must be 1-2 short Korean sentences.",
            "",
            f"Public research role explicitly targeted: {json.dumps(public_targeted)}",
            "Closeout-required original_requirements for RTM:",
            *(
                [f"- {item['req_id']}: {item['requirement']}" for item in closeout_requirements]
                if closeout_requirements
                else ["- none"]
            ),
            "Local sources already checked:",
            *[f"- {item}" for item in local_sources_checked],
            "",
            event_history_notice,
            "Current request:",
            json.dumps(request_projection.request_record, ensure_ascii=False, indent=2),
            "",
            "Incoming envelope:",
            json.dumps(envelope.to_dict(), ensure_ascii=False, indent=2),
        ]
    )


def default_research_planner_guidance(
    signal: dict[str, Any],
    *,
    local_sources_checked: list[str],
) -> str:
    reason_code = str(signal.get("reason_code") or "").strip()
    subject = str(signal.get("subject") or "").strip()
    local_hint = ", ".join(local_sources_checked[:3])
    if reason_code == RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING:
        if subject:
            return f"planner는 `{subject}`에 대한 외부 근거가 정리되기 전까지 provider/policy/market 가정을 고정하지 마세요."
        return "planner는 외부 근거가 정리되기 전까지 변경 가능성이 큰 외부 가정을 고정하지 마세요."
    if reason_code == RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE:
        if local_hint:
            return f"planner는 현재 local evidence({local_hint})를 planning 근거로 사용하면 됩니다. 추가 외부 research는 열지 않아도 됩니다."
        return "planner는 현재 local evidence를 planning 근거로 사용하면 됩니다. 추가 외부 research는 열지 않아도 됩니다."
    if reason_code == RESEARCH_REASON_CODE_NOT_NEEDED_NO_SUBJECT:
        return "현재 요청에는 planner 판단을 바꿀 외부 research subject가 없습니다. repo/local sprint context만으로 planning을 이어가면 됩니다."
    return "research 필요 판단이 실패했습니다. planner는 외부 fact checking이 완료됐다고 가정하지 마세요."


def research_skip_summary(signal: dict[str, Any]) -> str:
    reason_code = str(signal.get("reason_code") or "").strip()
    subject = str(signal.get("subject") or "").strip()
    if reason_code == RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE and subject:
        return f"`{subject}`는 local evidence만으로 planner가 이어갈 수 있다고 판단했습니다."
    if reason_code == RESEARCH_REASON_CODE_NOT_NEEDED_NO_SUBJECT:
        return "외부 research subject가 없어 planner로 바로 넘길 수 있다고 판단했습니다."
    return "외부 research 없이 planner로 넘길 수 있는 요청으로 판단했습니다."


def build_research_prompt(
    envelope: MessageEnvelope,
    request_record: RequestRecord,
    *,
    signal: dict[str, Any],
    subject_definition: dict[str, Any] | None = None,
    requirement_traceability_matrix: list[dict[str, Any]] | None = None,
    local_sources_checked: list[str],
    artifact_hint: str,
) -> str:
    definition = dict(subject_definition or {})
    rtm_reopen_rows = requirement_traceability_rows_requiring_research(requirement_traceability_matrix or [])
    prompt_payload = _omit_empty_fields(
        {
            "request": {
                "subject": _collapse_whitespace(definition.get("candidate_subject") or signal.get("subject") or ""),
                "query": _collapse_whitespace(
                    definition.get("research_query")
                    or signal.get("research_query")
                    or signal.get("subject")
                    or ""
                ),
                "planner_decision": _collapse_whitespace(definition.get("planning_decision") or ""),
                "knowledge_gap": _collapse_whitespace(definition.get("knowledge_gap") or ""),
                "planner_impact": _collapse_whitespace(definition.get("planner_impact") or ""),
                "requirement_evidence_gaps": [
                    {
                        "req_id": row.get("req_id") or "",
                        "requirement": row.get("requirement") or "",
                        "requirement_kind": row.get("requirement_kind") or "",
                        "planner_decisions": _normalize_text_list(row.get("planner_decisions")),
                        "missing_evidence": _normalize_text_list(row.get("missing_evidence")),
                        "research_query_delta": _collapse_whitespace(row.get("research_query_delta") or ""),
                    }
                    for row in rtm_reopen_rows
                ],
            },
            "sources": {
                "requirements": _normalize_text_list(definition.get("source_requirements")),
                "external_boundary": _collapse_whitespace(definition.get("external_boundary") or ""),
                "excluded_subjects": _normalize_text_list(definition.get("rejected_subjects")),
                "expectations": [
                    "Use authoritative primary or official sources when available.",
                    "Prefer current sources for facts that may have changed recently.",
                    "Cite every source used for planning-relevant claims.",
                ],
            },
            "report": {
                "format": "Markdown",
                "required_headings": [
                    "Executive Summary",
                    "Planner Guidance",
                    "Milestone Refinement Hints",
                    "Problem Framing Hints",
                    "Spec Implications",
                    "Todo Definition Hints",
                    "Backing Reasoning",
                    "Backing Sources",
                    "Open Questions",
                ],
                "backing_source_fields": {
                    "title": "source title",
                    "url": "http(s) URL",
                    "published_at": "publication date or access date if unavailable",
                    "relevance": "why this source matters to planner",
                    "summary": "short source-backed finding",
                },
                "rules": [
                    "Focus only on the external research subject in this request.",
                    "When original requirements or REQ-* IDs are present, cite the affected IDs in Planner Guidance, hints, and open questions.",
                    "When requirement_evidence_gaps are present, address each gap by req_id and explain which source-backed finding reduces planner risk.",
                    "If a requirement appears impossible or unsafe, recommend planner recovery or user-approved variance instead of narrowing the requirement yourself.",
                    "Planner Guidance must explain how findings affect milestone framing, spec boundaries, or todo decomposition.",
                    "Backing Reasoning must connect sources to planning recommendations.",
                    "Backing Sources must include title and http(s) URL.",
                ],
            },
        }
    )
    return json.dumps(
        prompt_payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_research_report(response_text: str) -> dict[str, Any]:
    sections: dict[str, list[str]] = {
        "Executive Summary": [],
        "Planner Guidance": [],
        "Milestone Refinement Hints": [],
        "Problem Framing Hints": [],
        "Spec Implications": [],
        "Todo Definition Hints": [],
        "Backing Reasoning": [],
        "Backing Sources": [],
        "Open Questions": [],
    }
    current_section = ""
    for raw_line in str(response_text or "").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        matched_section = ""
        for heading in sections:
            normalized_heading = heading.lower()
            normalized_line = stripped.lstrip("#").strip().lower()
            if normalized_line == normalized_heading:
                matched_section = heading
                break
        if matched_section:
            current_section = matched_section
            continue
        if current_section:
            sections[current_section].append(line)

    executive_lines = [line.strip() for line in sections["Executive Summary"] if line.strip()]
    planner_guidance = "\n".join(line.rstrip() for line in sections["Planner Guidance"]).strip()
    backing_sources = parse_backing_sources(sections["Backing Sources"])
    headline = executive_lines[0] if executive_lines else "외부 research 결과를 정리했습니다."
    return {
        "headline": headline,
        "planner_guidance": planner_guidance,
        "milestone_refinement_hints": _parse_section_items(sections["Milestone Refinement Hints"]),
        "problem_framing_hints": _parse_section_items(sections["Problem Framing Hints"]),
        "spec_implications": _parse_section_items(sections["Spec Implications"]),
        "todo_definition_hints": _parse_section_items(sections["Todo Definition Hints"]),
        "backing_reasoning": _parse_section_items(sections["Backing Reasoning"]),
        "backing_sources": backing_sources,
        "open_questions": _parse_section_items(sections["Open Questions"]),
    }


def _parse_section_items(lines: list[str]) -> list[str]:
    items: list[str] = []
    for raw_line in lines:
        stripped = str(raw_line or "").strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            stripped = stripped[2:].strip()
        if stripped:
            items.append(stripped)
    return items


def normalize_research_report_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_collapse_whitespace(item) for item in value if _collapse_whitespace(item)]
    if isinstance(value, str):
        return [_collapse_whitespace(value)] if _collapse_whitespace(value) else []
    return []


def parse_backing_sources(lines: list[str]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith("- title:"):
            if current:
                sources.append(current)
            current = {"title": stripped.split(":", 1)[1].strip(), "url": "", "summary": "", "relevance": "", "published_at": ""}
            continue
        if current is None:
            url_match = re.search(r"https?://\S+", stripped)
            if url_match:
                sources.append(
                    {
                        "title": stripped.replace(url_match.group(0), "").strip(" -|"),
                        "url": url_match.group(0),
                        "summary": "",
                        "relevance": "",
                        "published_at": "",
                    }
                )
            continue
        for key in ("url", "published_at", "relevance", "summary"):
            marker = f"{key}:"
            if lowered.startswith(marker):
                current[key] = stripped.split(":", 1)[1].strip()
                break
    if current:
        sources.append(current)
    return [item for item in sources if item.get("title") or item.get("url")]


def valid_backing_sources(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    sources: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = _collapse_whitespace(item.get("title") or "")
        url = _collapse_whitespace(item.get("url") or "")
        if not title or not (url.startswith("http://") or url.startswith("https://")):
            continue
        normalized = {
            "title": title,
            "url": url,
            "published_at": _collapse_whitespace(item.get("published_at") or ""),
            "relevance": _collapse_whitespace(item.get("relevance") or ""),
            "summary": _collapse_whitespace(item.get("summary") or ""),
        }
        sources.append(normalized)
    return sources


def validate_source_backed_research_report(
    signal: dict[str, Any],
    parsed_report: dict[str, Any],
) -> dict[str, Any]:
    normalized_report = dict(parsed_report or {})
    for field in RESEARCH_REPORT_LIST_FIELDS:
        normalized_report[field] = normalize_research_report_list(normalized_report.get(field))
    sources = valid_backing_sources(normalized_report.get("backing_sources"))
    if bool((signal or {}).get("needed")):
        if not sources:
            raise ValueError(
                "External research reports must include at least one backing source with title and http(s) URL."
            )
        if not _collapse_whitespace(normalized_report.get("planner_guidance") or ""):
            raise ValueError("External research reports must include Planner Guidance for planner.")
        if not normalized_report["backing_reasoning"]:
            raise ValueError("External research reports must include Backing Reasoning that connects sources to planning.")
        if not any(normalized_report[field] for field in RESEARCH_PLANNING_HINT_FIELDS):
            raise ValueError(
                "External research reports must include planning hints for milestone refinement, problem framing, specs, or todos."
            )
    normalized_report["backing_sources"] = sources
    return normalized_report


__all__ = [
    "ALLOWED_RESEARCH_SIGNAL_REASON_CODES",
    "MODEL_RESEARCH_SIGNAL_REASON_CODES",
    "RESEARCH_PLANNING_HINT_FIELDS",
    "RESEARCH_REPORT_LIST_FIELDS",
    "RESEARCH_SUBJECT_DEFINITION_FIELDS",
    "RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED",
    "RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING",
    "RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE",
    "RESEARCH_REASON_CODE_NOT_NEEDED_NO_SUBJECT",
    "REQUIREMENT_TRACEABILITY_MATRIX_FIELD",
    "closeout_original_requirements_from_request",
    "build_research_decision_prompt",
    "build_research_prompt",
    "default_research_planner_guidance",
    "default_research_signal",
    "mark_requirement_traceability_research_status",
    "normalize_research_decision",
    "normalize_requirement_traceability_matrix",
    "normalize_research_subject_definition",
    "normalize_research_report_list",
    "parse_backing_sources",
    "parse_research_report",
    "requirement_traceability_rows_requiring_research",
    "research_reason_code_summary",
    "research_skip_summary",
    "source_refs_from_backing_sources",
    "valid_backing_sources",
    "validate_source_backed_research_report",
]
