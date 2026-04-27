from __future__ import annotations

import json
import re
from typing import Any

from teams_runtime.shared.models import MessageEnvelope, RequestRecord


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


def _normalize_comparison_text(value: Any) -> str:
    return re.sub(r"\W+", "", str(value or "").strip().lower())


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
    reason_code = str(raw_payload.get("reason_code") or "").strip()
    if reason_code not in MODEL_RESEARCH_SIGNAL_REASON_CODES:
        raise ValueError(f"Unsupported research reason_code: {reason_code or 'empty'}")
    needed = bool(raw_payload.get("needed"))
    subject_definition = normalize_research_subject_definition(
        raw_payload,
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

    return {
        "signal": {
            "needed": needed,
            "subject": subject,
            "research_query": research_query,
            "reason_code": reason_code,
        },
        "research_subject_definition": subject_definition,
        "planner_guidance": planner_guidance,
    }


def build_research_decision_prompt(
    envelope: MessageEnvelope,
    request_record: RequestRecord,
    *,
    local_sources_checked: list[str],
) -> str:
    params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
    public_targeted = str(params.get("user_requested_role") or "").strip().lower() == "research"
    return "\n".join(
        [
            "You are the research prepass decision gate inside teams_runtime.",
            "Decide whether planner needs external grounding beyond the current local request, repo context, and sprint artifacts.",
            "You must first define the research subject, then choose the reason_code.",
            "Do not use keyword heuristics. Read the provided request context and make the judgment.",
            "Return strict JSON only with this exact shape:",
            "{",
            '  "needed": true,',
            '  "subject": "",',
            '  "research_query": "",',
            '  "reason_code": "needed_external_grounding|not_needed_local_evidence|not_needed_no_subject",',
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
            "Local sources already checked:",
            *[f"- {item}" for item in local_sources_checked],
            "",
            "Current request:",
            json.dumps(request_record, ensure_ascii=False, indent=2),
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
    local_sources_checked: list[str],
    artifact_hint: str,
) -> str:
    definition = dict(subject_definition or {})
    params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
    prompt_payload = {
        "research_mission": (
            "Prepare a source-backed research brief that helps planner refine the sprint milestone, "
            "write spec boundaries, discover further problems, and define reviewable todos."
        ),
        "defined_subject": {
            "subject": definition.get("candidate_subject") or signal.get("subject") or "",
            "query": definition.get("research_query") or signal.get("research_query") or signal.get("subject") or "",
            "planning_decision": definition.get("planning_decision") or "",
            "knowledge_gap": definition.get("knowledge_gap") or "",
            "external_boundary": definition.get("external_boundary") or "",
            "rejected_subjects": _normalize_text_list(definition.get("rejected_subjects")),
        },
        "planner_impact": {
            "impact_summary": definition.get("planner_impact") or "",
            "must_influence": [
                "milestone_refinement",
                "problem_framing",
                "spec_boundaries",
                "todo_decomposition",
                "acceptance_criteria",
            ],
        },
        "source_requirements": _normalize_text_list(definition.get("source_requirements")),
        "local_context_checked": [str(item).strip() for item in local_sources_checked if str(item).strip()],
        "sprint_context": {
            "requested_milestone_title": params.get("requested_milestone_title") or "",
            "current_milestone_title": params.get("milestone_title") or "",
            "kickoff_brief": params.get("kickoff_brief") or "",
            "kickoff_requirements": _normalize_text_list(params.get("kickoff_requirements")),
            "kickoff_reference_artifacts": _normalize_text_list(params.get("kickoff_reference_artifacts")),
            "request_scope": _collapse_whitespace(request_record.get("scope") or envelope.scope or ""),
            "request_summary": _collapse_whitespace(request_record.get("body") or envelope.body or ""),
        },
        "expected_report": {
            "raw_report_artifact": artifact_hint,
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
            "backing_sources_format": {
                "title": "source title",
                "url": "http(s) URL",
                "published_at": "publication or access date if available",
                "relevance": "why this source matters to planner",
                "summary": "short source-backed finding",
            },
            "rules": [
                "Focus on the smallest external research subject that materially changes planning decisions.",
                "Planner Guidance must explain how findings should change milestone framing, spec boundaries, or todo decomposition.",
                "Backing Reasoning must connect sources to planning recommendations instead of repeating source titles.",
                "Backing Sources must include title and http(s) URL.",
            ],
        },
    }
    return json.dumps(
        prompt_payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=False,
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
    "build_research_decision_prompt",
    "build_research_prompt",
    "default_research_planner_guidance",
    "default_research_signal",
    "normalize_research_decision",
    "normalize_research_subject_definition",
    "normalize_research_report_list",
    "parse_backing_sources",
    "parse_research_report",
    "research_reason_code_summary",
    "research_skip_summary",
    "valid_backing_sources",
    "validate_source_backed_research_report",
]
