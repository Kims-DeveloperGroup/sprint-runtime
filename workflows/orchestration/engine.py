from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any, Callable

from teams_runtime.shared.models import RequestRecord, TEAM_ROLES, WorkflowState
from teams_runtime.workflows.roles import (
    EXECUTION_AGENT_ROLES,
    AgentUtilizationPolicy,
    get_agent_capability,
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
WORKFLOW_STEP_RESEARCH_INITIAL = "research_initial"
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
    WORKFLOW_STEP_RESEARCH_INITIAL,
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
WORKFLOW_QA_REOPEN_REQUIRED_DOC_NAMES = ("spec.md", "todo_backlog.md", "iteration_log.md", "current_sprint.md")


def default_workflow_state() -> WorkflowState:
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


def research_first_workflow_state() -> WorkflowState:
    state = dict(default_workflow_state())
    state["step"] = WORKFLOW_STEP_RESEARCH_INITIAL
    state["phase_owner"] = "research"
    return state


def initial_workflow_state(review_cycle_limit: int | None = None) -> WorkflowState:
    state = research_first_workflow_state()
    if review_cycle_limit is not None:
        state["review_cycle_limit"] = max(1, int(review_cycle_limit))
    return state


def normalize_workflow_state(raw: Any) -> WorkflowState:
    state = default_workflow_state()
    if not isinstance(raw, dict):
        return state
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


def infer_legacy_internal_workflow_state(request_record: RequestRecord) -> WorkflowState:
    current_role = str(
        request_record.get("current_role")
        or request_record.get("next_role")
        or request_record.get("owner_role")
        or ""
    ).strip().lower()
    if current_role not in {"research", "planner", "designer", "architect"}:
        return {}
    state = default_workflow_state()
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
    if qa_reports or developer_reports:
        return {}
    if current_role == "research":
        state["step"] = WORKFLOW_STEP_RESEARCH_INITIAL
        state["phase_owner"] = "research"
        state["phase_status"] = "active"
        return state
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


def set_request_workflow_state(request_record: RequestRecord, workflow_state: WorkflowState) -> None:
    params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
    params["workflow"] = dict(workflow_state or default_workflow_state())
    request_record["params"] = params


def workflow_transition(result: dict[str, Any]) -> dict[str, Any]:
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


def workflow_transition_requests_explicit_continuation(transition: dict[str, Any]) -> bool:
    outcome = str(transition.get("outcome") or "").strip().lower()
    reopen_category = str(transition.get("reopen_category") or "").strip().lower()
    if outcome in {"advance", "continue"}:
        return True
    return outcome == "reopen" and reopen_category in WORKFLOW_REOPEN_CATEGORIES


def workflow_transition_requests_validation_handoff(transition: dict[str, Any]) -> bool:
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


def workflow_should_close_in_planning(
    *,
    workflow_state: dict[str, Any],
    current_role: str,
    transition: dict[str, Any],
    proposals: dict[str, Any],
    artifacts: list[str],
    request_indicates_execution_flag: bool,
) -> bool:
    if str(current_role or "").strip().lower() != "planner":
        return False
    if not workflow_state:
        return False
    step = str(workflow_state.get("step") or "").strip().lower()
    if step not in {WORKFLOW_STEP_PLANNER_DRAFT, WORKFLOW_STEP_PLANNER_FINALIZE}:
        return False
    requested_role = str((transition or {}).get("requested_role") or "").strip().lower()
    if requested_role in PLANNING_ADVISORY_ROLES:
        return False
    normalized_proposals = dict(proposals or {})
    has_planning_contract = any(
        normalized_proposals.get(key) is not None
        for key in ("root_cause_contract", "todo_brief", "planning_note")
    )
    transition_outcome = str((transition or {}).get("outcome") or "").strip().lower()
    explicit_continuation = workflow_transition_requests_explicit_continuation(transition or {})
    explicit_planning_close = transition_outcome == "complete" or (
        bool((transition or {}).get("finalize_phase")) and not explicit_continuation
    )
    normalized_artifacts = [str(item).strip() for item in (artifacts or []) if str(item).strip()]
    planning_surface_only = bool(normalized_artifacts) and all(
        is_planning_surface_artifact_hint(item) for item in normalized_artifacts
    )
    if not has_planning_contract and request_indicates_execution_flag and not (
        explicit_planning_close and planning_surface_only
    ):
        return False
    if not normalized_artifacts and not has_planning_contract:
        return False
    if normalized_artifacts and not planning_surface_only:
        return False
    return True


def workflow_review_cycle_limit_reached(workflow_state: dict[str, Any]) -> bool:
    review_cycle_count = max(0, int((workflow_state or {}).get("review_cycle_count") or 0))
    review_cycle_limit = max(1, int((workflow_state or {}).get("review_cycle_limit") or DEFAULT_WORKFLOW_REVIEW_CYCLE_LIMIT))
    return review_cycle_count >= review_cycle_limit


def workflow_reason(result: dict[str, Any], transition: dict[str, Any], default: str) -> str:
    return (
        str(transition.get("reason") or "").strip()
        or str(result.get("summary") or "").strip()
        or default
    )


def workflow_terminal_block_state(workflow_state: dict[str, Any], *, category: str = "") -> WorkflowState:
    updated_state = dict(workflow_state or default_workflow_state())
    updated_state["phase_status"] = "blocked"
    if category in WORKFLOW_REOPEN_CATEGORIES:
        updated_state["reopen_category"] = category
    return updated_state


def workflow_route_to_research_initial_state(workflow_state: dict[str, Any]) -> WorkflowState:
    updated_state = dict(workflow_state or research_first_workflow_state())
    updated_state["phase"] = WORKFLOW_PHASE_PLANNING
    updated_state["step"] = WORKFLOW_STEP_RESEARCH_INITIAL
    updated_state["phase_owner"] = "research"
    updated_state["phase_status"] = "active"
    return updated_state


def workflow_route_to_planner_draft_state(workflow_state: dict[str, Any], *, category: str = "") -> WorkflowState:
    updated_state = dict(workflow_state or default_workflow_state())
    updated_state["phase"] = WORKFLOW_PHASE_PLANNING
    updated_state["step"] = WORKFLOW_STEP_PLANNER_DRAFT
    updated_state["phase_owner"] = "planner"
    updated_state["phase_status"] = "active"
    if category in WORKFLOW_REOPEN_CATEGORIES:
        updated_state["reopen_category"] = category
    return updated_state


def workflow_route_to_planner_finalize_state(workflow_state: dict[str, Any], *, category: str = "") -> WorkflowState:
    updated_state = dict(workflow_state or default_workflow_state())
    updated_state["phase"] = WORKFLOW_PHASE_PLANNING
    updated_state["step"] = WORKFLOW_STEP_PLANNER_FINALIZE
    updated_state["phase_owner"] = "planner"
    updated_state["phase_status"] = "finalizing"
    if category in WORKFLOW_REOPEN_CATEGORIES:
        updated_state["reopen_category"] = category
    return updated_state


def workflow_route_to_planning_advisory_state(
    workflow_state: dict[str, Any],
    *,
    role: str,
    category: str = "",
) -> WorkflowState:
    updated_state = dict(workflow_state or default_workflow_state())
    updated_state["phase"] = WORKFLOW_PHASE_PLANNING
    updated_state["step"] = WORKFLOW_STEP_PLANNER_ADVISORY
    updated_state["phase_owner"] = role
    updated_state["phase_status"] = "active"
    updated_state["planning_pass_count"] = int(updated_state.get("planning_pass_count") or 0) + 1
    if category in WORKFLOW_REOPEN_CATEGORIES:
        updated_state["reopen_category"] = category
    return updated_state


def workflow_route_to_architect_guidance_state(workflow_state: dict[str, Any], *, category: str = "") -> WorkflowState:
    updated_state = dict(workflow_state or default_workflow_state())
    updated_state["phase"] = WORKFLOW_PHASE_IMPLEMENTATION
    updated_state["step"] = WORKFLOW_STEP_ARCHITECT_GUIDANCE
    updated_state["phase_owner"] = "architect"
    updated_state["phase_status"] = "active"
    if category in WORKFLOW_REOPEN_CATEGORIES:
        updated_state["reopen_category"] = category
    return updated_state


def workflow_route_to_developer_build_state(
    workflow_state: dict[str, Any],
    *,
    step: str = WORKFLOW_STEP_DEVELOPER_BUILD,
    category: str = "",
) -> WorkflowState:
    updated_state = dict(workflow_state or default_workflow_state())
    updated_state["phase"] = WORKFLOW_PHASE_IMPLEMENTATION
    updated_state["step"] = step
    updated_state["phase_owner"] = "developer"
    updated_state["phase_status"] = "active"
    if category in WORKFLOW_REOPEN_CATEGORIES:
        updated_state["reopen_category"] = category
    return updated_state


def workflow_route_to_architect_review_state(workflow_state: dict[str, Any], *, category: str = "") -> WorkflowState:
    updated_state = dict(workflow_state or default_workflow_state())
    updated_state["phase"] = WORKFLOW_PHASE_IMPLEMENTATION
    updated_state["step"] = WORKFLOW_STEP_ARCHITECT_REVIEW
    updated_state["phase_owner"] = "architect"
    updated_state["phase_status"] = "active"
    updated_state["review_cycle_count"] = int(updated_state.get("review_cycle_count") or 0) + 1
    if category in WORKFLOW_REOPEN_CATEGORIES:
        updated_state["reopen_category"] = category
    return updated_state


def workflow_route_to_qa_state(workflow_state: dict[str, Any]) -> WorkflowState:
    updated_state = dict(workflow_state or default_workflow_state())
    updated_state["phase"] = WORKFLOW_PHASE_VALIDATION
    updated_state["step"] = WORKFLOW_STEP_QA_VALIDATION
    updated_state["phase_owner"] = "qa"
    updated_state["phase_status"] = "active"
    return updated_state


def workflow_complete_state(workflow_state: dict[str, Any]) -> WorkflowState:
    updated_state = dict(workflow_state or default_workflow_state())
    updated_state["phase"] = WORKFLOW_PHASE_CLOSEOUT
    updated_state["step"] = WORKFLOW_STEP_CLOSEOUT
    updated_state["phase_owner"] = "version_controller"
    updated_state["phase_status"] = "completed"
    return updated_state


def workflow_mark_reopen_state(
    workflow_state: dict[str, Any],
    *,
    current_role: str,
    category: str,
) -> WorkflowState:
    updated_state = dict(workflow_state or default_workflow_state())
    updated_state["reopen_source_role"] = current_role
    updated_state["reopen_category"] = category if category in WORKFLOW_REOPEN_CATEGORIES else ""
    return updated_state


_WORKFLOW_STATE_EXPORTS = [
    "DEFAULT_WORKFLOW_REVIEW_CYCLE_LIMIT",
    "PLANNING_ADVISORY_ROLES",
    "WORKFLOW_CONTRACT_VERSION",
    "WORKFLOW_PHASE_CLOSEOUT",
    "WORKFLOW_PHASE_IMPLEMENTATION",
    "WORKFLOW_PHASE_PLANNING",
    "WORKFLOW_PHASE_VALIDATION",
    "WORKFLOW_PHASES",
    "WORKFLOW_POLICY_SOURCE",
    "WORKFLOW_QA_REOPEN_REQUIRED_DOC_NAMES",
    "WORKFLOW_REOPEN_CATEGORIES",
    "WORKFLOW_SELECTION_SOURCE",
    "WORKFLOW_STEP_ARCHITECT_GUIDANCE",
    "WORKFLOW_STEP_ARCHITECT_REVIEW",
    "WORKFLOW_STEP_CLOSEOUT",
    "WORKFLOW_STEP_DEVELOPER_BUILD",
    "WORKFLOW_STEP_DEVELOPER_REVISION",
    "WORKFLOW_STEP_PLANNER_ADVISORY",
    "WORKFLOW_STEP_PLANNER_DRAFT",
    "WORKFLOW_STEP_PLANNER_FINALIZE",
    "WORKFLOW_STEP_QA_VALIDATION",
    "WORKFLOW_STEP_RESEARCH_INITIAL",
    "WORKFLOW_STEPS",
    "default_workflow_state",
    "infer_legacy_internal_workflow_state",
    "initial_workflow_state",
    "normalize_workflow_state",
    "research_first_workflow_state",
    "set_request_workflow_state",
    "workflow_complete_state",
    "workflow_mark_reopen_state",
    "workflow_reason",
    "workflow_review_cycle_limit_reached",
    "workflow_route_to_architect_guidance_state",
    "workflow_route_to_architect_review_state",
    "workflow_route_to_developer_build_state",
    "workflow_route_to_planner_draft_state",
    "workflow_route_to_planner_finalize_state",
    "workflow_route_to_planning_advisory_state",
    "workflow_route_to_qa_state",
    "workflow_route_to_research_initial_state",
    "workflow_terminal_block_state",
    "workflow_should_close_in_planning",
    "workflow_transition",
    "workflow_transition_requests_explicit_continuation",
    "workflow_transition_requests_validation_handoff",
]


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


def normalize_artifact_hint(value: Any) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def is_planning_surface_artifact_hint(artifact_hint: Any) -> bool:
    normalized = normalize_artifact_hint(artifact_hint).lower()
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


def is_planner_owned_surface_artifact_hint(artifact_hint: Any) -> bool:
    normalized = normalize_artifact_hint(artifact_hint).lower()
    if not normalized or not normalized.startswith("shared_workspace/"):
        return False
    name = PurePosixPath(normalized).name
    if name in PLANNING_SURFACE_ROOT_DOC_NAMES:
        return True
    return "/sprints/" in normalized and name in PLANNER_OWNED_SPRINT_DOC_NAMES


def required_workflow_planner_doc_hints(
    *,
    reopen_source_role: str,
    request_artifacts: list[Any] | None = None,
    sprint_artifact_hints: list[Any] | None = None,
) -> list[str]:
    required: list[str] = []

    def append_required(hint: Any) -> None:
        normalized_hint = normalize_artifact_hint(hint)
        if normalized_hint and normalized_hint not in required:
            required.append(normalized_hint)

    normalized_request_artifacts = [
        normalize_artifact_hint(item)
        for item in (request_artifacts or [])
        if is_planning_surface_artifact_hint(item)
    ]
    if str(reopen_source_role or "").strip().lower() == "qa":
        for name in WORKFLOW_QA_REOPEN_REQUIRED_DOC_NAMES:
            match = next(
                (artifact for artifact in normalized_request_artifacts if PurePosixPath(artifact).name == name),
                "",
            )
            append_required(match)
        for hint in (sprint_artifact_hints or []):
            append_required(hint)
    return required


def workflow_planner_doc_contract_violation(
    *,
    workflow_state: dict[str, Any],
    role: str,
    result_artifacts: list[Any] | None,
    required_hints: list[str] | None,
    artifact_exists: Callable[[str], bool],
) -> tuple[list[str], list[str], list[str]]:
    if str((workflow_state or {}).get("phase") or "").strip().lower() != WORKFLOW_PHASE_PLANNING:
        return ([], [], [])
    if str(role or "").strip().lower() != "planner":
        return ([], [], [])
    normalized_result_artifacts = [
        normalize_artifact_hint(item)
        for item in (result_artifacts or [])
        if is_planning_surface_artifact_hint(item)
    ]
    planner_owned_artifacts = [
        artifact for artifact in normalized_result_artifacts if is_planner_owned_surface_artifact_hint(artifact)
    ]
    missing_required = [
        artifact
        for artifact in (required_hints or [])
        if artifact not in planner_owned_artifacts
    ]
    missing_files = [
        artifact for artifact in planner_owned_artifacts if not artifact_exists(artifact)
    ]
    return (planner_owned_artifacts, missing_required, missing_files)


def planner_contract_reopen_result(
    result: dict[str, Any],
    *,
    planner_owned_artifacts: list[str],
    missing_required: list[str],
    missing_files: list[str],
) -> dict[str, Any]:
    normalized_result = dict(result)
    proposals = dict(normalized_result.get("proposals") or {})
    unresolved_items: list[str] = []
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
        "target_phase": "planning",
        "target_step": "planner_finalize",
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
    return normalized_result


def qa_result_requires_planner_reopen(
    *,
    workflow_state: dict[str, Any],
    role: str,
    result: dict[str, Any],
    transition: dict[str, Any],
) -> bool:
    if str((workflow_state or {}).get("step") or "").strip().lower() != WORKFLOW_STEP_QA_VALIDATION:
        return False
    if str(role or "").strip().lower() != "qa":
        return False
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


def qa_runtime_sync_result(result: dict[str, Any]) -> dict[str, Any]:
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
    return normalized_result


def qa_planner_reopen_result(
    result: dict[str, Any],
    *,
    transition: dict[str, Any],
) -> dict[str, Any]:
    normalized_result = dict(result)
    proposals = dict(normalized_result.get("proposals") or {})
    unresolved_items = [
        str(item).strip()
        for item in (transition.get("unresolved_items") or [])
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
        "target_phase": "planning",
        "target_step": "planner_finalize",
        "requested_role": "planner",
        "reopen_category": "scope",
        "reason": (
            str(transition.get("reason") or "").strip()
            or "QA가 spec/todo planning contract mismatch를 확인해 planner 재정렬이 필요합니다."
        ),
        "unresolved_items": unresolved_items,
        "finalize_phase": False,
    }
    normalized_result["proposals"] = proposals
    if str(normalized_result.get("status") or "").strip().lower() not in {"blocked", "failed"}:
        normalized_result["status"] = "completed"
    return normalized_result


def qa_result_is_runtime_sync_anomaly(
    *,
    workflow_state: dict[str, Any],
    role: str,
    result: dict[str, Any],
    transition: dict[str, Any],
) -> bool:
    if str((workflow_state or {}).get("step") or "").strip().lower() != WORKFLOW_STEP_QA_VALIDATION:
        return False
    if str(role or "").strip().lower() != "qa":
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
    return not qa_result_requires_planner_reopen(
        workflow_state=workflow_state,
        role=role,
        result=result,
        transition=transition,
    )


def sanitize_implementation_result(
    *,
    workflow_state: dict[str, Any],
    role: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    if str(role or "").strip().lower() not in {"architect", "developer"}:
        return result
    if str((workflow_state or {}).get("phase") or "").strip().lower() != WORKFLOW_PHASE_IMPLEMENTATION:
        return result
    planner_owned_artifacts = [
        str(item).strip()
        for item in (result.get("artifacts") or [])
        if is_planner_owned_surface_artifact_hint(item)
    ]
    if not planner_owned_artifacts:
        return result
    normalized_result = dict(result)
    normalized_result["artifacts"] = [
        str(item).strip()
        for item in (normalized_result.get("artifacts") or [])
        if str(item).strip() and not is_planner_owned_surface_artifact_hint(item)
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
    return normalized_result


def enforce_workflow_role_report_contract(
    *,
    workflow_state: dict[str, Any],
    role: str,
    result: dict[str, Any],
    planner_doc_contract: tuple[list[str], list[str], list[str]] | None = None,
    qa_requires_planner_reopen_flag: bool = False,
    qa_runtime_sync_anomaly_flag: bool = False,
    transition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not workflow_state:
        return result
    normalized_role = str(role or "").strip().lower()
    if normalized_role == "planner":
        planner_owned_artifacts, missing_required, missing_files = planner_doc_contract or ([], [], [])
        if not planner_owned_artifacts or missing_required or missing_files:
            return planner_contract_reopen_result(
                result,
                planner_owned_artifacts=planner_owned_artifacts,
                missing_required=missing_required,
                missing_files=missing_files,
            )
        return result
    if normalized_role == "qa" and qa_runtime_sync_anomaly_flag:
        return qa_runtime_sync_result(result)
    if normalized_role == "qa" and qa_requires_planner_reopen_flag:
        return qa_planner_reopen_result(
            result,
            transition=dict(transition or {}),
        )
    return sanitize_implementation_result(
        workflow_state=workflow_state,
        role=normalized_role,
        result=result,
    )


_WORKFLOW_ROLE_POLICY_EXPORTS = [
    "PLANNING_SURFACE_ROOT_DOC_NAMES",
    "PLANNING_SURFACE_SPRINT_DOC_NAMES",
    "PLANNER_OWNED_SPRINT_DOC_NAMES",
    "enforce_workflow_role_report_contract",
    "is_planner_owned_surface_artifact_hint",
    "is_planning_surface_artifact_hint",
    "normalize_artifact_hint",
    "planner_contract_reopen_result",
    "qa_planner_reopen_result",
    "qa_result_is_runtime_sync_anomaly",
    "qa_result_requires_planner_reopen",
    "qa_runtime_sync_result",
    "required_workflow_planner_doc_hints",
    "sanitize_implementation_result",
    "workflow_planner_doc_contract_violation",
]


def workflow_route_decision(
    next_role: str,
    *,
    workflow_state: dict[str, Any],
    reason: str,
    requested_role: str = "",
    matched_signals: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "next_role": next_role,
        "workflow_state": workflow_state,
        "route_reason": str(reason or "").strip(),
        "requested_role": str(requested_role or "").strip(),
        "matched_signals": [
            str(item).strip()
            for item in (matched_signals or [])
            if str(item).strip()
        ],
    }


def workflow_terminal_block_decision(
    workflow_state: dict[str, Any],
    *,
    summary: str,
    category: str = "",
) -> dict[str, Any]:
    updated_state = workflow_terminal_block_state(workflow_state, category=category)
    return {
        "next_role": "",
        "workflow_state": updated_state,
        "terminal_status": "blocked",
        "terminal_summary": summary,
    }


def workflow_route_to_research_initial_decision(
    workflow_state: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    updated_state = workflow_route_to_research_initial_state(workflow_state)
    return workflow_route_decision(
        "research",
        workflow_state=updated_state,
        reason=reason,
        requested_role="research",
        matched_signals=["workflow:research_initial"],
    )


def workflow_route_to_planner_draft_decision(
    workflow_state: dict[str, Any],
    *,
    reason: str,
    category: str = "",
) -> dict[str, Any]:
    updated_state = workflow_route_to_planner_draft_state(workflow_state, category=category)
    return workflow_route_decision(
        "planner",
        workflow_state=updated_state,
        reason=reason,
        requested_role="planner",
        matched_signals=["workflow:planner_draft"],
    )


def workflow_route_to_planner_finalize_decision(
    workflow_state: dict[str, Any],
    *,
    reason: str,
    category: str = "",
) -> dict[str, Any]:
    updated_state = workflow_route_to_planner_finalize_state(workflow_state, category=category)
    return workflow_route_decision(
        "planner",
        workflow_state=updated_state,
        reason=reason,
        requested_role="planner",
        matched_signals=["workflow:planner_finalize"],
    )


def workflow_route_to_planning_advisory_decision(
    workflow_state: dict[str, Any],
    *,
    role: str,
    reason: str,
    category: str = "",
) -> dict[str, Any]:
    updated_state = dict(workflow_state or default_workflow_state())
    if updated_state["planning_pass_count"] >= updated_state["planning_pass_limit"]:
        return workflow_route_to_planner_finalize_decision(
            updated_state,
            reason="planning advisory pass 한도에 도달해 planner finalization으로 되돌립니다.",
            category=category,
        )
    updated_state = workflow_route_to_planning_advisory_state(
        updated_state,
        role=role,
        category=category,
    )
    return workflow_route_decision(
        role,
        workflow_state=updated_state,
        reason=reason,
        requested_role=role,
        matched_signals=[f"workflow:planning_advisory:{role}"],
    )


def workflow_route_to_architect_guidance_decision(
    workflow_state: dict[str, Any],
    *,
    reason: str,
    category: str = "",
) -> dict[str, Any]:
    updated_state = workflow_route_to_architect_guidance_state(workflow_state, category=category)
    return workflow_route_decision(
        "architect",
        workflow_state=updated_state,
        reason=reason,
        requested_role="architect",
        matched_signals=["workflow:architect_guidance"],
    )


def workflow_route_to_developer_build_decision(
    workflow_state: dict[str, Any],
    *,
    reason: str,
    step: str = WORKFLOW_STEP_DEVELOPER_BUILD,
    category: str = "",
) -> dict[str, Any]:
    updated_state = workflow_route_to_developer_build_state(
        workflow_state,
        step=step,
        category=category,
    )
    return workflow_route_decision(
        "developer",
        workflow_state=updated_state,
        reason=reason,
        requested_role="developer",
        matched_signals=[f"workflow:{step}"],
    )


def workflow_route_to_architect_review_decision(
    workflow_state: dict[str, Any],
    *,
    reason: str,
    category: str = "",
) -> dict[str, Any]:
    updated_state = workflow_route_to_architect_review_state(workflow_state, category=category)
    return workflow_route_decision(
        "architect",
        workflow_state=updated_state,
        reason=reason,
        requested_role="architect",
        matched_signals=["workflow:architect_review"],
    )


def workflow_route_to_qa_decision(
    workflow_state: dict[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    updated_state = workflow_route_to_qa_state(workflow_state)
    return workflow_route_decision(
        "qa",
        workflow_state=updated_state,
        reason=reason,
        requested_role="qa",
        matched_signals=["workflow:qa_validation"],
    )


def workflow_complete_decision(
    workflow_state: dict[str, Any],
    *,
    summary: str,
) -> dict[str, Any]:
    updated_state = workflow_complete_state(workflow_state)
    return {
        "next_role": "",
        "workflow_state": updated_state,
        "terminal_summary": summary,
    }


def workflow_reopen_decision(
    workflow_state: dict[str, Any],
    *,
    current_role: str,
    category: str,
    reason: str,
) -> dict[str, Any]:
    updated_state = workflow_mark_reopen_state(
        workflow_state,
        current_role=current_role,
        category=category,
    )
    if category == "scope":
        return workflow_route_to_planner_finalize_decision(updated_state, reason=reason, category=category)
    if category == "ux":
        return workflow_route_to_planning_advisory_decision(
            updated_state,
            role="designer",
            reason=reason,
            category=category,
        )
    if category == "architecture":
        if current_role == "qa":
            return workflow_route_to_architect_review_decision(updated_state, reason=reason, category=category)
        return workflow_route_to_architect_guidance_decision(updated_state, reason=reason, category=category)
    if category in {"implementation", "verification"}:
        current_step = str(updated_state.get("step") or "").strip().lower()
        step = (
            WORKFLOW_STEP_DEVELOPER_REVISION
            if current_role == "qa" or current_step in {WORKFLOW_STEP_ARCHITECT_REVIEW, WORKFLOW_STEP_DEVELOPER_REVISION}
            else WORKFLOW_STEP_DEVELOPER_BUILD
        )
        return workflow_route_to_developer_build_decision(
            updated_state,
            reason=reason,
            step=step,
            category=category,
        )
    return workflow_route_to_planner_finalize_decision(updated_state, reason=reason, category=category)


def workflow_review_cycle_limit_block_decision(
    workflow_state: dict[str, Any],
    *,
    reason: str,
    category: str = "",
) -> dict[str, Any]:
    review_cycle_count = max(0, int((workflow_state or {}).get("review_cycle_count") or 0))
    review_cycle_limit = max(
        1,
        int((workflow_state or {}).get("review_cycle_limit") or DEFAULT_WORKFLOW_REVIEW_CYCLE_LIMIT),
    )
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
    return workflow_terminal_block_decision(
        workflow_state,
        summary=combined_summary or limit_summary,
        category=category,
    )


def derive_workflow_routing_decision(
    workflow_state: dict[str, Any],
    transition: dict[str, Any],
    *,
    current_role: str,
    reason: str,
    should_close_in_planning: bool = False,
) -> dict[str, Any] | None:
    outcome = str(transition.get("outcome") or "").strip().lower()
    requested_role = str(transition.get("requested_role") or "").strip().lower()
    reopen_category = str(transition.get("reopen_category") or "").strip().lower()
    step = str((workflow_state or {}).get("step") or "").strip().lower()

    if outcome == "block":
        return workflow_terminal_block_decision(
            workflow_state,
            summary=reason,
            category=reopen_category,
        )

    if step == WORKFLOW_STEP_RESEARCH_INITIAL:
        return workflow_route_to_planner_draft_decision(
            workflow_state,
            reason=reason or "research prepass 결과를 planner가 planning에 반영합니다.",
            category=reopen_category,
        )

    if step in {WORKFLOW_STEP_PLANNER_DRAFT, WORKFLOW_STEP_PLANNER_FINALIZE}:
        if current_role != "planner":
            return workflow_route_to_planner_finalize_decision(
                workflow_state,
                reason="planning owner인 planner가 최종 정리를 이어갑니다.",
            )
        if outcome == "reopen" and (
            requested_role == "planner"
            or str(transition.get("target_step") or "").strip().lower() == WORKFLOW_STEP_PLANNER_FINALIZE
            or reopen_category == "scope"
        ):
            return workflow_route_to_planner_finalize_decision(
                workflow_state,
                reason=reason or "planner가 planning 문서/contract를 다시 정리합니다.",
                category=reopen_category,
            )
        if requested_role in PLANNING_ADVISORY_ROLES and outcome in {"continue", "reopen", ""}:
            if int((workflow_state or {}).get("planning_pass_count") or 0) >= int((workflow_state or {}).get("planning_pass_limit") or 0):
                return workflow_terminal_block_decision(
                    workflow_state,
                    summary="planning advisory pass 한도에 도달해 planner가 더 이상 specialist pass를 열 수 없습니다.",
                    category=reopen_category,
                )
            return workflow_route_to_planning_advisory_decision(
                workflow_state,
                role=requested_role,
                reason=reason,
                category=reopen_category,
            )
        if should_close_in_planning:
            return workflow_complete_decision(
                workflow_state,
                summary=reason or "planner가 문서/계획 surface를 planning 단계에서 마무리했습니다.",
            )
        return workflow_route_to_architect_guidance_decision(
            workflow_state,
            reason=reason or "planning이 정리되어 implementation guidance를 시작합니다.",
            category=reopen_category,
        )

    if step == WORKFLOW_STEP_PLANNER_ADVISORY:
        return workflow_route_to_planner_finalize_decision(
            workflow_state,
            reason=reason or "specialist advisory를 planner가 반영합니다.",
            category=reopen_category,
        )

    if step == WORKFLOW_STEP_ARCHITECT_GUIDANCE:
        if outcome == "reopen":
            return workflow_reopen_decision(
                workflow_state,
                current_role=current_role or "architect",
                category=reopen_category,
                reason=reason,
            )
        return workflow_route_to_developer_build_decision(
            workflow_state,
            reason=reason or "architect guidance를 바탕으로 developer 구현을 시작합니다.",
            step=WORKFLOW_STEP_DEVELOPER_BUILD,
            category=reopen_category,
        )

    if step == WORKFLOW_STEP_DEVELOPER_BUILD:
        if outcome == "reopen":
            return workflow_reopen_decision(
                workflow_state,
                current_role=current_role or "developer",
                category=reopen_category,
                reason=reason,
            )
        return workflow_route_to_architect_review_decision(
            workflow_state,
            reason=reason or "developer 구현 결과를 architect가 리뷰합니다.",
            category=reopen_category,
        )

    if step == WORKFLOW_STEP_ARCHITECT_REVIEW:
        if workflow_transition_requests_validation_handoff(transition):
            return workflow_route_to_qa_decision(
                workflow_state,
                reason=reason or "architect review를 통과해 QA 검증으로 넘깁니다.",
            )
        if (
            workflow_review_cycle_limit_reached(workflow_state)
            and workflow_transition_requests_explicit_continuation(transition)
        ):
            return workflow_review_cycle_limit_block_decision(
                workflow_state,
                reason=reason,
                category=reopen_category or "implementation",
            )
        if outcome == "reopen":
            if reopen_category in {"", "implementation"}:
                return workflow_route_to_developer_build_decision(
                    workflow_state,
                    reason=reason or "architect review 결과를 developer가 반영합니다.",
                    step=WORKFLOW_STEP_DEVELOPER_REVISION,
                    category=reopen_category,
                )
            return workflow_reopen_decision(
                workflow_state,
                current_role=current_role or "architect",
                category=reopen_category,
                reason=reason,
            )
        return workflow_route_to_developer_build_decision(
            workflow_state,
            reason=reason or "architect review 결과를 developer가 반영합니다.",
            step=WORKFLOW_STEP_DEVELOPER_REVISION,
            category=reopen_category,
        )

    if step == WORKFLOW_STEP_DEVELOPER_REVISION:
        if outcome == "reopen":
            return workflow_reopen_decision(
                workflow_state,
                current_role=current_role or "developer",
                category=reopen_category,
                reason=reason,
            )
        if str(transition.get("target_step") or "").strip().lower() == WORKFLOW_STEP_ARCHITECT_REVIEW:
            return workflow_route_to_architect_review_decision(
                workflow_state,
                reason=reason or "developer revision 결과를 architect가 다시 리뷰합니다.",
                category=reopen_category,
            )
        return workflow_route_to_qa_decision(
            workflow_state,
            reason=reason or "developer revision이 끝나 QA 검증으로 넘깁니다.",
        )

    if step == WORKFLOW_STEP_QA_VALIDATION:
        if outcome == "reopen":
            return workflow_reopen_decision(
                workflow_state,
                current_role=current_role or "qa",
                category=reopen_category,
                reason=reason,
            )
        return workflow_complete_decision(
            workflow_state,
            summary=reason or "QA 검증을 마쳐 closeout으로 진행합니다.",
        )

    return None


def coerce_nonterminal_workflow_role_result(
    result: dict[str, Any],
    *,
    transition: dict[str, Any],
    workflow_decision: dict[str, Any] | None,
) -> dict[str, Any]:
    result_status = str(result.get("status") or "").strip().lower()
    error_text = str(result.get("error") or "").strip()
    if result_status not in {"failed", "blocked"} and not error_text:
        return result
    if not workflow_transition_requests_explicit_continuation(transition):
        return result
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


def normalize_routing_reference_text(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    for token in ("_", "-", "/", "\\", ".", "(", ")", "[", "]", "{", "}", ":", ","):
        normalized = normalized.replace(token, " ")
    return " ".join(normalized.split())


def routing_phase_for_role(role: str) -> str:
    return {
        "research": "planning",
        "planner": "planning",
        "designer": "design",
        "architect": "architecture",
        "developer": "implementation",
        "qa": "validation",
    }.get(str(role or "").strip(), "planning")


def match_reference_terms(
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
        normalized_term = normalize_routing_reference_text(term)
        if normalized_term and normalized_term in text:
            labeled = f"{prefix}:{term}"
            if labeled not in matches:
                matches.append(labeled)
        if len(matches) >= limit:
            break
    return matches


def routing_signal_matches(
    role: str,
    *,
    policy: AgentUtilizationPolicy,
    intent: str,
    text: str,
) -> list[str]:
    capability = get_agent_capability(role, policy)
    matches: list[str] = []
    if intent and intent in capability.owned_intents:
        matches.append(f"intent:{intent}")
    matches.extend(
        match_reference_terms(
            capability.routing_signals,
            text=text,
            prefix="routing",
            limit=4 - len(matches) if len(matches) < 4 else 0,
        )
    )
    return matches[:4]


def strongest_domain_matches(
    role: str,
    *,
    policy: AgentUtilizationPolicy,
    text: str,
) -> list[str]:
    capability = get_agent_capability(role, policy)
    matches = match_reference_terms(
        capability.strongest_for,
        text=text,
        prefix="strength",
        limit=3,
    )
    if len(matches) < 3:
        matches.extend(
            match_reference_terms(
                capability.strongest_domain_signals,
                text=text,
                prefix="strength_signal",
                limit=3 - len(matches),
            )
        )
    return matches[:3]


def preferred_skill_matches(
    role: str,
    *,
    policy: AgentUtilizationPolicy,
    text: str,
) -> list[str]:
    capability = get_agent_capability(role, policy)
    matches = match_reference_terms(
        capability.preferred_skills,
        text=text,
        prefix="preferred_skill",
        limit=2,
    )
    if len(matches) < 2:
        matches.extend(
            match_reference_terms(
                capability.preferred_skill_signals,
                text=text,
                prefix="skill_signal",
                limit=2 - len(matches),
            )
        )
    return matches[:2]


def behavior_trait_matches(
    role: str,
    *,
    policy: AgentUtilizationPolicy,
    text: str,
) -> list[str]:
    capability = get_agent_capability(role, policy)
    matches = match_reference_terms(
        capability.behavior_traits,
        text=text,
        prefix="behavior_trait",
        limit=2,
    )
    if len(matches) < 2:
        matches.extend(
            match_reference_terms(
                capability.behavior_signals,
                text=text,
                prefix="behavior_signal",
                limit=2 - len(matches),
            )
        )
    return matches[:2]


def should_not_handle_matches(
    role: str,
    *,
    policy: AgentUtilizationPolicy,
    text: str,
) -> list[str]:
    capability = get_agent_capability(role, policy)
    return match_reference_terms(
        capability.should_not_handle,
        text=text,
        prefix="forbidden",
        limit=2,
    )


def role_hint_score(
    role: str,
    *,
    policy: AgentUtilizationPolicy,
    intent: str,
    text: str,
) -> int:
    capability = get_agent_capability(role, policy)
    score = 0
    if intent and intent in capability.owned_intents:
        score += 5
    score += len(routing_signal_matches(role, policy=policy, intent=intent, text=text))
    score += len(strongest_domain_matches(role, policy=policy, text=text)) * 2
    score += len(preferred_skill_matches(role, policy=policy, text=text))
    score += len(behavior_trait_matches(role, policy=policy, text=text))
    return score


def execution_evidence_score(
    role: str,
    *,
    policy: AgentUtilizationPolicy,
    intent: str,
    text: str,
) -> int:
    capability = get_agent_capability(role, policy)
    score = 0
    if intent and intent in capability.owned_intents:
        score += 5
    score += len(routing_signal_matches(role, policy=policy, intent=intent, text=text))
    score += len(strongest_domain_matches(role, policy=policy, text=text)) * 2
    score += len(preferred_skill_matches(role, policy=policy, text=text))
    return score


def request_indicates_execution(
    *,
    policy: AgentUtilizationPolicy,
    intent: str,
    text: str,
) -> bool:
    if intent in {"design", "architect", "implement", "execute", "qa"}:
        return True
    return any(
        execution_evidence_score(role, policy=policy, intent=intent, text=text) > 0
        for role in EXECUTION_AGENT_ROLES
    )


def classify_request_state(
    request_record: dict[str, Any],
    *,
    policy: AgentUtilizationPolicy,
    current_role: str,
    requested_role: str,
    selection_source: str,
    text: str,
    is_internal_sprint_request: bool,
) -> str:
    intent = str(request_record.get("intent") or "").strip().lower()
    if selection_source == "planning_resume":
        return "blocked_resume"
    if current_role == "planner":
        if requested_role in EXECUTION_AGENT_ROLES:
            return "execution_opened"
        if request_indicates_execution(policy=policy, intent=intent, text=text):
            return "execution_opened"
        return "planning_only"
    if current_role == "research":
        return "planning_only"
    if current_role in {"designer", "architect"}:
        return "implementation_ready"
    if current_role == "developer":
        return "qa_pending"
    if current_role == "qa":
        return "closeout_ready"
    if is_internal_sprint_request:
        return "execution_opened"
    return "planning_only"


def derive_routing_phase(
    *,
    policy: AgentUtilizationPolicy,
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
        return routing_phase_for_role(requested_role)
    if current_role == "research":
        return "planning"
    if current_role == "planner":
        if request_state_class == "planning_only":
            return "planning"
        hinted_role = ""
        hinted_score = -1
        hinted_priority = -1
        for role in EXECUTION_AGENT_ROLES:
            score = role_hint_score(role, policy=policy, intent=intent, text=text)
            capability = get_agent_capability(role, policy)
            priority = int(capability.routing_priority or 0)
            if score > hinted_score or (score == hinted_score and priority > hinted_priority):
                hinted_role = role
                hinted_score = score
                hinted_priority = priority
        return routing_phase_for_role(hinted_role or "planner")
    if current_role in EXECUTION_AGENT_ROLES:
        return routing_phase_for_role(current_role)
    return "planning"


def score_candidate_role(
    role: str,
    *,
    policy: AgentUtilizationPolicy,
    intent: str,
    text: str,
    routing_phase: str,
    request_state_class: str,
) -> dict[str, Any]:
    capability = get_agent_capability(role, policy)
    weights = policy.weights
    matched_signals = routing_signal_matches(role, policy=policy, intent=intent, text=text)
    matched_strongest_domains = strongest_domain_matches(role, policy=policy, text=text)
    matched_preferred_skills = preferred_skill_matches(role, policy=policy, text=text)
    matched_behavior_traits = behavior_trait_matches(role, policy=policy, text=text)
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


def build_governed_routing_selection(
    request_record: dict[str, Any],
    *,
    policy: AgentUtilizationPolicy,
    current_role: str,
    requested_role: str,
    selection_source: str,
    routing_text: str,
    is_internal_sprint_request: bool,
    planner_reentry_has_explicit_signal: bool,
) -> dict[str, Any]:
    normalized_requested_role = str(requested_role or "").strip()
    intent = str(request_record.get("intent") or "").strip().lower()
    text = normalize_routing_reference_text(routing_text)
    request_state_class = classify_request_state(
        request_record,
        policy=policy,
        current_role=current_role,
        requested_role=normalized_requested_role,
        selection_source=selection_source,
        text=text,
        is_internal_sprint_request=is_internal_sprint_request,
    )
    routing_phase = derive_routing_phase(
        policy=policy,
        current_role=current_role,
        requested_role=normalized_requested_role,
        selection_source=selection_source,
        request_state_class=request_state_class,
        intent=intent,
        text=text,
    )
    base_selection = {
        "policy_source": policy.policy_source,
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
        selected_role = policy.user_intake_role
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
            policy.sourcer_review_role
            if selection_source in {"sourcer_review", "blocked_backlog_review"}
            else policy.planning_resume_role
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
            else policy.sprint_initial_default_role
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

    if current_role == "research":
        candidate_roles = ["planner"]
    elif current_role == "planner":
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
        capability = get_agent_capability(current_role, policy)
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
            not is_internal_sprint_request
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
        disallowed_matches = should_not_handle_matches(candidate_role, policy=policy, text=text)
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
        score_details = score_candidate_role(
            candidate_role,
            policy=policy,
            intent=intent,
            text=text,
            routing_phase=routing_phase,
            request_state_class=request_state_class,
        )
        capability = get_agent_capability(candidate_role, policy)
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
        and not is_internal_sprint_request
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
        policy.planner_reentry_requires_explicit_signal
        and current_role in EXECUTION_AGENT_ROLES
        and selected_role == "planner"
        and not normalized_requested_role
        and not planner_reentry_has_explicit_signal
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
        "policy_source": policy.policy_source,
        "routing_phase": routing_phase,
        "request_state_class": request_state_class,
    }


__all__ = [
    *_WORKFLOW_ROLE_POLICY_EXPORTS,
    *_WORKFLOW_STATE_EXPORTS,
    "behavior_trait_matches",
    "build_governed_routing_selection",
    "classify_request_state",
    "coerce_nonterminal_workflow_role_result",
    "derive_workflow_routing_decision",
    "derive_routing_phase",
    "execution_evidence_score",
    "match_reference_terms",
    "normalize_routing_reference_text",
    "preferred_skill_matches",
    "request_indicates_execution",
    "role_hint_score",
    "routing_phase_for_role",
    "routing_signal_matches",
    "score_candidate_role",
    "should_not_handle_matches",
    "strongest_domain_matches",
    "workflow_complete_decision",
    "workflow_reopen_decision",
    "workflow_review_cycle_limit_block_decision",
    "workflow_route_decision",
    "workflow_route_to_architect_guidance_decision",
    "workflow_route_to_architect_review_decision",
    "workflow_route_to_developer_build_decision",
    "workflow_route_to_planner_draft_decision",
    "workflow_route_to_planner_finalize_decision",
    "workflow_route_to_planning_advisory_decision",
    "workflow_route_to_qa_decision",
    "workflow_route_to_research_initial_decision",
    "workflow_terminal_block_decision",
]
