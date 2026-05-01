from __future__ import annotations

from typing import Any


ALLOWED_ROLE_STATUSES = {"completed", "blocked", "failed"}
PROMPT_STATUS_ENUM_LITERAL = "completed|blocked|failed"
PROMPT_PLACEHOLDER_SUMMARY = "short korean summary"
PROMPT_PLACEHOLDER_INSIGHT = "private role insight for journal.md"
WORKFLOW_TRANSITION_ALLOWED_OUTCOMES = {"continue", "advance", "reopen", "block", "complete"}
WORKFLOW_TRANSITION_REQUIRED_KEYS = (
    "outcome",
    "target_phase",
    "target_step",
    "reopen_category",
    "reason",
    "unresolved_items",
    "finalize_phase",
)
WORKFLOW_TRANSITION_REQUIRED_ROLES = {"planner", "designer", "architect", "developer", "qa"}
CONTRACT_STATUS_INVALID = "invalid"
CONTRACT_STATUS_REPAIRED = "repaired"


def render_role_result_contract(*, request_id: str, role: str, extra_fields: str = "") -> str:
    return f"""Use concrete values from this run only. Do not copy schema enums or placeholder example text literally.
Allowed `status` values are exactly `completed`, `blocked`, or `failed`.
{{
  "request_id": "{request_id}",
  "role": "{role}",
  "status": "completed",
  "summary": "이 세션에서 직접 확인한 실제 한국어 요약",
  "insights": [],
  "proposals": {{}},
  "artifacts": [],
  "error": ""
{extra_fields}
}}"""


def validate_role_result_contract(
    payload: dict[str, Any],
    *,
    request_record: dict[str, Any] | None = None,
    role: str = "",
) -> list[str]:
    if not isinstance(payload, dict):
        return ["payload_not_dict"]

    issues: list[str] = []
    normalized_role = str(role or payload.get("role") or "").strip().lower()
    status = str(payload.get("status") or "").strip().lower()
    if status == PROMPT_STATUS_ENUM_LITERAL:
        issues.append("copied_prompt_status_enum_literal")

    summary = str(payload.get("summary") or "").strip().lower()
    if summary == PROMPT_PLACEHOLDER_SUMMARY:
        issues.append("copied_placeholder_summary")

    raw_insights = payload.get("insights")
    insights = _normalize_string_list(raw_insights)
    if any(item.lower() == PROMPT_PLACEHOLDER_INSIGHT for item in insights):
        issues.append("copied_placeholder_insight")

    if request_record and _request_has_workflow(request_record) and normalized_role in WORKFLOW_TRANSITION_REQUIRED_ROLES:
        proposals = payload.get("proposals")
        transition = proposals.get("workflow_transition") if isinstance(proposals, dict) else None
        if not isinstance(transition, dict):
            issues.append("missing_workflow_transition")
        else:
            missing_keys = [key for key in WORKFLOW_TRANSITION_REQUIRED_KEYS if key not in transition]
            if missing_keys:
                issues.append(f"workflow_transition_missing_keys:{','.join(missing_keys)}")
            outcome = str(transition.get("outcome") or "").strip().lower()
            if outcome and outcome not in WORKFLOW_TRANSITION_ALLOWED_OUTCOMES:
                issues.append(f"invalid_workflow_transition_outcome:{outcome}")
            unresolved_items = transition.get("unresolved_items")
            if unresolved_items not in (None, "") and not isinstance(unresolved_items, (list, str)):
                issues.append("invalid_workflow_transition_unresolved_items")
            finalize_phase = transition.get("finalize_phase")
            if finalize_phase not in (None, True, False):
                issues.append("invalid_workflow_transition_finalize_phase")

    return issues


def describe_contract_issues(issues: list[str]) -> list[str]:
    descriptions: list[str] = []
    for issue in issues:
        if issue == "payload_not_dict":
            descriptions.append("role result payload가 JSON object가 아닙니다.")
        elif issue == "missing_json_object":
            descriptions.append("role response에서 JSON object를 찾지 못했습니다.")
        elif issue == "copied_prompt_status_enum_literal":
            descriptions.append("status에 prompt enum 예시 `completed|blocked|failed`가 그대로 복사되었습니다.")
        elif issue == "copied_placeholder_summary":
            descriptions.append("summary에 placeholder 예시 `short Korean summary`가 그대로 복사되었습니다.")
        elif issue == "copied_placeholder_insight":
            descriptions.append("insights에 placeholder 예시가 그대로 복사되었습니다.")
        elif issue == "missing_workflow_transition":
            descriptions.append("workflow-managed request인데 `proposals.workflow_transition`이 없습니다.")
        elif issue.startswith("workflow_transition_missing_keys:"):
            missing = issue.split(":", 1)[1]
            descriptions.append(f"`proposals.workflow_transition`에 필수 키가 없습니다: {missing}.")
        elif issue.startswith("invalid_workflow_transition_outcome:"):
            outcome = issue.split(":", 1)[1]
            descriptions.append(f"`workflow_transition.outcome` 값이 허용 범위를 벗어났습니다: {outcome}.")
        elif issue == "invalid_workflow_transition_unresolved_items":
            descriptions.append("`workflow_transition.unresolved_items`는 list 또는 string이어야 합니다.")
        elif issue == "invalid_workflow_transition_finalize_phase":
            descriptions.append("`workflow_transition.finalize_phase`는 boolean이어야 합니다.")
        else:
            descriptions.append(issue)
    return descriptions


def summarize_contract_issues(issues: list[str]) -> str:
    return " ".join(describe_contract_issues(list(issues))).strip()


def is_invalid_contract_payload(payload: dict[str, Any]) -> bool:
    return str(payload.get("contract_status") or "").strip().lower() == CONTRACT_STATUS_INVALID


def _normalize_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return []


def _request_has_workflow(request_record: dict[str, Any]) -> bool:
    params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
    return isinstance(params.get("workflow"), dict) and bool(params.get("workflow"))


__all__ = [
    "ALLOWED_ROLE_STATUSES",
    "CONTRACT_STATUS_INVALID",
    "CONTRACT_STATUS_REPAIRED",
    "PROMPT_PLACEHOLDER_INSIGHT",
    "PROMPT_PLACEHOLDER_SUMMARY",
    "PROMPT_STATUS_ENUM_LITERAL",
    "describe_contract_issues",
    "is_invalid_contract_payload",
    "render_role_result_contract",
    "summarize_contract_issues",
    "validate_role_result_contract",
]
