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
RESEARCH_REASON_CODE_SCHEMA_ERROR_PREFIX = "Unsupported research reason_code:"


def _collapse_whitespace(value: Any) -> str:
    return " ".join(str(value or "").strip().split())


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


def normalize_research_decision(raw_payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raise ValueError("Research decision response must be a JSON object.")
    reason_code = str(raw_payload.get("reason_code") or "").strip()
    if reason_code not in MODEL_RESEARCH_SIGNAL_REASON_CODES:
        raise ValueError(f"Unsupported research reason_code: {reason_code or 'empty'}")
    needed = bool(raw_payload.get("needed"))
    subject = _collapse_whitespace(raw_payload.get("subject") or "")
    research_query = _collapse_whitespace(raw_payload.get("research_query") or "")
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
        "planner_guidance": planner_guidance,
    }


def is_research_reason_code_schema_error(error: Any) -> bool:
    return str(error or "").strip().startswith(RESEARCH_REASON_CODE_SCHEMA_ERROR_PREFIX)


def build_research_decision_retry_prompt(original_prompt: str, reason_code_error: str) -> str:
    return "\n".join(
        [
            original_prompt,
            "",
            "Your previous research decision JSON violated the strict research decision schema:",
            f"- {reason_code_error}",
            "",
            "Return the complete research decision JSON again.",
            "The `reason_code` field must be exactly one string from this enum:",
            "- `needed_external_grounding`",
            "- `not_needed_local_evidence`",
            "- `not_needed_no_subject`",
            "Do not return placeholders, combined candidates, uppercase variants, or empty reason_code.",
        ]
    )


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
            "You must decide the research subject and the concrete research query yourself.",
            "Do not use keyword heuristics. Read the provided request context and make the judgment.",
            "Return strict JSON only with this exact shape:",
            "{",
            '  "needed": true,',
            '  "subject": "",',
            '  "research_query": "",',
            '  "reason_code": "<required: needed_external_grounding, not_needed_local_evidence, or not_needed_no_subject>",',
            '  "planner_guidance": "짧은 한국어 planner guidance"',
            "}",
            "Rules:",
            "- `reason_code` is mandatory and must be exactly one of the three strings below.",
            "- Do not copy the placeholder or return a combined candidate expression such as `needed_external_grounding|not_needed_local_evidence|not_needed_no_subject`.",
            "- `needed_external_grounding`: use only when external sources could materially change planner decisions.",
            "- `not_needed_local_evidence`: use when there is a concrete research-shaped question, but local repo/request/sprint evidence is already enough for planner.",
            "- `not_needed_no_subject`: use when the request does not contain a genuine external research subject.",
            "- The runtime reserves `blocked_decision_failed`; do not emit it.",
            "- When reason_code is `needed_external_grounding` or `not_needed_local_evidence`, `subject` and `research_query` must both be non-empty and concrete.",
            "- When reason_code is `not_needed_no_subject`, `subject` and `research_query` must both be empty strings.",
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
    local_sources_checked: list[str],
    artifact_hint: str,
) -> str:
    return "\n".join(
        [
            "You are preparing a planner-support research brief for teams_runtime.",
            "Focus on the smallest external research subject that materially changes planning decisions.",
            "Return Markdown with exactly these headings: Executive Summary, Planner Guidance, Backing Sources, Open Questions.",
            "Under Backing Sources, use repeated bullet groups in this exact format:",
            "- title: ...",
            "  url: ...",
            "  published_at: ...",
            "  relevance: ...",
            "  summary: ...",
            f"The raw report will be saved to: {artifact_hint}",
            "",
            f"Research subject: {signal.get('subject') or 'N/A'}",
            f"Research query: {signal.get('research_query') or signal.get('subject') or 'N/A'}",
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


def parse_research_report(response_text: str) -> dict[str, Any]:
    sections: dict[str, list[str]] = {
        "Executive Summary": [],
        "Planner Guidance": [],
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
    open_questions = [
        line.strip()[2:].strip() if line.strip().startswith("- ") else line.strip()
        for line in sections["Open Questions"]
        if line.strip()
    ]
    backing_sources = parse_backing_sources(sections["Backing Sources"])
    headline = executive_lines[0] if executive_lines else "외부 research 결과를 정리했습니다."
    return {
        "headline": headline,
        "planner_guidance": planner_guidance,
        "backing_sources": backing_sources,
        "open_questions": [item for item in open_questions if item],
    }


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


__all__ = [
    "ALLOWED_RESEARCH_SIGNAL_REASON_CODES",
    "MODEL_RESEARCH_SIGNAL_REASON_CODES",
    "RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED",
    "RESEARCH_REASON_CODE_NEEDED_EXTERNAL_GROUNDING",
    "RESEARCH_REASON_CODE_NOT_NEEDED_LOCAL_EVIDENCE",
    "RESEARCH_REASON_CODE_NOT_NEEDED_NO_SUBJECT",
    "build_research_decision_prompt",
    "build_research_decision_retry_prompt",
    "build_research_prompt",
    "default_research_planner_guidance",
    "default_research_signal",
    "is_research_reason_code_schema_error",
    "normalize_research_decision",
    "parse_backing_sources",
    "parse_research_report",
    "research_reason_code_summary",
    "research_skip_summary",
]
