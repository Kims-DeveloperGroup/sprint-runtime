from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from teams_runtime.models import INTERNAL_TEAM_AGENTS, TEAM_ROLES


@dataclass(frozen=True, slots=True)
class AgentCapability:
    name: str
    summary: str
    mission: str
    strongest_for: tuple[str, ...]
    preferred_skills: tuple[str, ...] = ()
    routing_signals: tuple[str, ...] = ()
    strongest_domain_signals: tuple[str, ...] = ()
    preferred_skill_signals: tuple[str, ...] = ()
    behavior_traits: tuple[str, ...] = ()
    behavior_signals: tuple[str, ...] = ()
    should_not_handle: tuple[str, ...] = ()
    allowed_next_roles: tuple[str, ...] = ()
    owned_intents: tuple[str, ...] = ()
    phase_fit: tuple[str, ...] = ()
    request_state_fit: tuple[str, ...] = ()
    default_intent: str = "route"
    internal_only: bool = False
    routing_priority: int = 0

    @property
    def expected_behavior(self) -> str:
        strengths = ", ".join(self.strongest_for[:2]) or self.summary
        traits = ", ".join(self.behavior_traits[:3])
        if traits:
            return f"{strengths} 중심으로 진행하고, {traits} 태도를 유지한다."
        return f"{strengths} 중심으로 진행한다."


@dataclass(frozen=True, slots=True)
class RoutingWeights:
    owned_intent: int = 60
    routing_signal: int = 10
    strongest_domain_signal: int = 12
    preferred_skill_signal: int = 8
    behavior_signal: int = 6
    phase_fit: int = 20
    request_state_fit: int = 12


@dataclass(frozen=True, slots=True)
class AgentUtilizationPolicy:
    policy_source: str
    policy_path: str
    weights: RoutingWeights
    public_capabilities: dict[str, AgentCapability]
    internal_capabilities: dict[str, AgentCapability]
    user_intake_role: str
    sourcer_review_role: str
    planning_resume_role: str
    sprint_initial_default_role: str
    sprint_force_qa: bool
    planner_reentry_requires_explicit_signal: bool
    verification_result_terminal: bool
    ignore_non_planner_backlog_proposals_for_routing: bool
    implementation_review_cycle_limit: int
    load_error: str = ""


def _normalize_str_tuple(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        normalized = values.strip()
        return (normalized,) if normalized else ()
    if not isinstance(values, (list, tuple, set)):
        return ()
    seen: list[str] = []
    for item in values:
        normalized = str(item or "").strip()
        if normalized and normalized not in seen:
            seen.append(normalized)
    return tuple(seen)


def _normalize_mapping(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, dict) else {}


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _coerce_public_role(value: Any, default: str) -> str:
    normalized = str(value or "").strip()
    if normalized in TEAM_ROLES and normalized != "orchestrator":
        return normalized
    return default


def _capability_from_payload(name: str, payload: dict[str, Any], *, internal_only: bool = False) -> AgentCapability:
    strongest_for = _normalize_str_tuple(payload.get("strongest_for"))
    summary = str(payload.get("summary") or "").strip()
    mission = str(payload.get("mission") or "").strip()
    return AgentCapability(
        name=name,
        summary=summary or name,
        mission=mission or summary or name,
        strongest_for=strongest_for or ((summary or name),),
        preferred_skills=_normalize_str_tuple(payload.get("preferred_skills")),
        routing_signals=_normalize_str_tuple(payload.get("routing_signals")),
        strongest_domain_signals=_normalize_str_tuple(payload.get("strongest_domain_signals")),
        preferred_skill_signals=_normalize_str_tuple(payload.get("preferred_skill_signals")),
        behavior_traits=_normalize_str_tuple(payload.get("behavior_traits")),
        behavior_signals=_normalize_str_tuple(payload.get("behavior_signals")),
        should_not_handle=_normalize_str_tuple(payload.get("should_not_handle")),
        allowed_next_roles=_normalize_str_tuple(payload.get("allowed_next_roles")),
        owned_intents=tuple(item.lower() for item in _normalize_str_tuple(payload.get("owned_intents"))),
        phase_fit=tuple(item.lower() for item in _normalize_str_tuple(payload.get("phase_fit"))),
        request_state_fit=tuple(item.lower() for item in _normalize_str_tuple(payload.get("request_state_fit"))),
        default_intent=str(payload.get("default_intent") or "route").strip() or "route",
        internal_only=internal_only or bool(payload.get("internal_only")),
        routing_priority=_coerce_int(payload.get("routing_priority"), 0),
    )


def _weights_from_payload(payload: dict[str, Any]) -> RoutingWeights:
    return RoutingWeights(
        owned_intent=_coerce_int(payload.get("owned_intent"), 60),
        routing_signal=_coerce_int(payload.get("routing_signal"), 10),
        strongest_domain_signal=_coerce_int(payload.get("strongest_domain_signal"), 12),
        preferred_skill_signal=_coerce_int(payload.get("preferred_skill_signal"), 8),
        behavior_signal=_coerce_int(payload.get("behavior_signal"), 6),
        phase_fit=_coerce_int(payload.get("phase_fit"), 20),
        request_state_fit=_coerce_int(payload.get("request_state_fit"), 12),
    )


DEFAULT_AGENT_UTILIZATION_POLICY_DATA: dict[str, Any] = {
    "version": 2,
    "policy_routes": {
        "user_intake": "planner",
        "sourcer_review": "planner",
        "planning_resume": "planner",
        "sprint_initial_default": "planner",
        "sprint_force_qa": True,
        "planner_reentry_requires_explicit_signal": True,
        "verification_result_terminal": True,
        "ignore_non_planner_backlog_proposals_for_routing": True,
    },
    "workflow_contract": {
        "planning_owner": "planner",
        "planning_advisory_roles": ["designer", "architect"],
        "planning_shared_pass_limit": 2,
        "planning_pass_limit_behavior": "planner_finalize_then_block",
        "implementation_review_cycle_limit": 3,
        "implementation_sequence": [
            "architect_guidance",
            "developer_build",
            "architect_review",
            "developer_revision",
            "qa_validation",
        ],
        "reopen_gate": "orchestrator",
        "reopen_role_map": {
            "scope": "planner",
            "ux": "designer",
            "architecture": "architect",
            "implementation": "developer",
            "verification": "developer",
        },
        "validation_owner": "qa",
        "closeout_owner": "version_controller",
    },
    "weights": {
        "owned_intent": 60,
        "routing_signal": 10,
        "strongest_domain_signal": 12,
        "preferred_skill_signal": 8,
        "behavior_signal": 6,
        "phase_fit": 20,
        "request_state_fit": 12,
    },
    "public_roles": {
        "orchestrator": {
            "summary": "Workflow governor for sprint, routing, and agent utilization",
            "mission": "Oversee sprint state, choose the right agent for each step, enforce ownership boundaries, and keep execution moving.",
            "strongest_for": [
                "workflow governance",
                "sprint oversight",
                "agent selection",
                "handoff coordination",
            ],
            "preferred_skills": [
                "agent_utilization",
                "sprint_orchestration",
                "handoff_merging",
                "status_reporting",
                "sprint_closeout",
            ],
            "routing_signals": ["workflow", "handoff", "routing", "sprint"],
            "strongest_domain_signals": ["workflow", "governor", "routing", "orchestration", "운영"],
            "preferred_skill_signals": ["agent_utilization", "routing policy", "delegation quality"],
            "behavior_traits": ["governing", "boundary-aware", "state-first", "decisive"],
            "behavior_signals": ["orchestrate", "coordinate", "govern", "workflow"],
            "should_not_handle": [
                "planner-owned backlog persistence",
                "developer implementation work",
                "version_controller commit execution",
            ],
            "allowed_next_roles": ["planner", "designer", "architect", "developer", "qa"],
            "owned_intents": [],
            "phase_fit": ["planning", "resume", "closeout"],
            "request_state_fit": ["planning_only", "closeout_ready", "blocked_resume"],
            "default_intent": "route",
            "routing_priority": 0,
        },
        "planner": {
            "summary": "Planning owner for backlog management, decomposition, and sprint shaping",
            "mission": "Turn requests and documents into plans, backlog decisions, and executable next steps.",
            "strongest_for": [
                "planning requests",
                "backlog management",
                "sprint planning",
                "document-backed decomposition",
            ],
            "preferred_skills": [
                "documentation",
                "backlog_management",
                "backlog_decomposition",
                "sprint_planning",
            ],
            "routing_signals": [
                "plan",
                "planning",
                "backlog",
                "milestone",
                "scope",
                "strategy",
                "document",
                "기획",
                "백로그",
                "우선순위",
                "문서",
            ],
            "strongest_domain_signals": [
                "acceptance criteria",
                "requirements",
                "backlog item",
                "todo",
                "milestone",
                "phase",
                "dependency",
                "priority",
                "scope",
                "spec",
                "기획",
                "요구사항",
            ],
            "preferred_skill_signals": [
                "documentation",
                "backlog management",
                "backlog decomposition",
                "sprint planning",
                "markdown",
                "문서화",
            ],
            "behavior_traits": ["structured", "document-first", "scope-shaping"],
            "behavior_signals": ["structured", "organize", "decompose", "prioritize", "정리"],
            "should_not_handle": ["direct code implementation", "commit execution"],
            "allowed_next_roles": ["designer", "architect", "developer", "qa"],
            "owned_intents": ["plan", "route"],
            "phase_fit": ["planning", "resume"],
            "request_state_fit": ["planning_only", "blocked_resume", "execution_opened"],
            "default_intent": "plan",
            "routing_priority": 0,
        },
        "designer": {
            "summary": "UX and communication specialist for user-facing flows and wording",
            "mission": "Shape interfaces, user-facing structure, and response design before implementation.",
            "strongest_for": ["UX flow", "copy and message design", "interaction clarity"],
            "preferred_skills": [],
            "routing_signals": [
                "ux",
                "ui",
                "design",
                "copy",
                "message",
                "button",
                "label",
                "flow",
                "interaction",
                "문구",
                "레이블",
                "가독성",
                "화면",
            ],
            "strongest_domain_signals": [
                "user-facing",
                "response wording",
                "discord reply",
                "tone",
                "copy",
                "layout",
                "message format",
                "ux flow",
                "가독성",
                "문구",
            ],
            "preferred_skill_signals": ["ux", "copy", "interaction", "presentation"],
            "behavior_traits": ["user-centered", "clarifying", "presentation-aware"],
            "behavior_signals": ["clarify", "readable", "scan", "friendly", "명확"],
            "should_not_handle": ["system architecture", "commit execution"],
            "allowed_next_roles": ["planner", "architect", "developer", "qa"],
            "owned_intents": ["design"],
            "phase_fit": ["design", "planning"],
            "request_state_fit": ["execution_opened", "implementation_ready"],
            "default_intent": "design",
            "routing_priority": 10,
        },
        "architect": {
            "summary": "Technical architecture specialist for codebase overviews, implementation specs, and change reviews",
            "mission": (
                "Overview modules, define interfaces and sequencing, write implementation-ready technical "
                "specifications, and review developer changes for structural fit."
            ),
            "strongest_for": [
                "system architecture",
                "technical specifications",
                "module structure overview",
                "implementation reviews",
            ],
            "preferred_skills": [],
            "routing_signals": [
                "architecture",
                "architect",
                "api",
                "schema",
                "interface",
                "contract",
                "pipeline",
                "boundary",
                "module",
                "system",
                "technical specification",
                "technical spec",
                "implementation spec",
                "module structure",
                "module overview",
                "codebase overview",
                "technical review",
                "code review",
                "developer change review",
                "구조",
                "아키텍처",
                "인터페이스",
                "계약",
                "파이프라인",
                "경계",
                "모듈 구조",
                "코드베이스",
                "기술 명세",
                "구현 명세",
                "아키텍처 리뷰",
                "코드 리뷰",
            ],
            "strongest_domain_signals": [
                "structure",
                "design contract",
                "file impact",
                "boundary",
                "module split",
                "module structure",
                "codebase overview",
                "workflow design",
                "schema",
                "api",
                "interface",
                "technical specification",
                "implementation review",
                "developer change review",
                "sequence",
            ],
            "preferred_skill_signals": [
                "architecture",
                "contract",
                "boundary",
                "interface",
                "technical spec",
                "module overview",
                "change review",
            ],
            "behavior_traits": [
                "systems-thinking",
                "constraint-aware",
                "sequencing-focused",
                "senior-guidance",
                "review-oriented",
            ],
            "behavior_signals": ["system", "constraint", "sequence", "boundary", "guide", "review", "구조", "명세"],
            "should_not_handle": ["backlog ownership", "commit execution"],
            "allowed_next_roles": ["planner", "developer", "qa"],
            "owned_intents": ["architect"],
            "phase_fit": ["architecture", "design", "implementation"],
            "request_state_fit": ["execution_opened", "implementation_ready"],
            "default_intent": "architect",
            "routing_priority": 20,
        },
        "developer": {
            "summary": "Implementation specialist for code changes and validation-ready output",
            "mission": "Implement changes in the project workspace and leave clear validation context for QA and version control.",
            "strongest_for": ["code implementation", "bug fixes", "feature delivery", "validation-ready outputs"],
            "preferred_skills": [],
            "routing_signals": [
                "implement",
                "implementation",
                "execute",
                "code",
                "coding",
                "fix",
                "bug",
                "refactor",
                "구현",
                "실제로 구현",
                "코드",
                "버그",
                "수정",
                "리팩터",
            ],
            "strongest_domain_signals": [
                "patch",
                "code change",
                "fix",
                "refactor",
                "feature",
                "test ready",
                "implementation",
                "workspace change",
                "구현",
                "코드",
            ],
            "preferred_skill_signals": ["implement", "code", "patch", "fix", "refactor"],
            "behavior_traits": ["execution-oriented", "concrete", "artifact-producing"],
            "behavior_signals": ["concrete", "artifact", "implement", "patch", "실행"],
            "should_not_handle": ["backlog persistence", "final commit ownership"],
            "allowed_next_roles": ["planner", "architect", "qa"],
            "owned_intents": ["implement", "execute"],
            "phase_fit": ["implementation", "architecture"],
            "request_state_fit": ["execution_opened", "implementation_ready"],
            "default_intent": "implement",
            "routing_priority": 30,
        },
        "qa": {
            "summary": "Validation specialist for regression review and release readiness",
            "mission": "Verify behavior, catch regressions, and decide whether work is ready to close.",
            "strongest_for": ["verification", "regression review", "release readiness"],
            "preferred_skills": [],
            "routing_signals": [
                "qa",
                "regression",
                "verification",
                "validate",
                "validation",
                "release readiness",
                "test",
                "testing",
                "회귀",
                "검증",
                "품질",
                "릴리즈",
                "테스트",
            ],
            "strongest_domain_signals": [
                "release",
                "ready to close",
                "validation",
                "regression",
                "test coverage",
                "readiness",
                "verify",
                "qa",
            ],
            "preferred_skill_signals": ["verify", "validation", "regression", "release readiness"],
            "behavior_traits": ["skeptical", "evidence-driven", "release-focused"],
            "behavior_signals": ["evidence", "skeptical", "regression", "verify", "검증"],
            "should_not_handle": ["feature planning", "commit execution"],
            "allowed_next_roles": ["planner"],
            "owned_intents": ["qa"],
            "phase_fit": ["validation", "closeout", "implementation"],
            "request_state_fit": ["qa_pending", "closeout_ready", "execution_opened"],
            "default_intent": "qa",
            "routing_priority": 0,
        },
    },
    "internal_agents": {
        "parser": {
            "summary": "Internal semantic intake agent for natural-language request normalization",
            "mission": "Normalize freeform intake into status or route intent for orchestrator.",
            "strongest_for": ["intent classification", "status detection", "intake normalization"],
            "behavior_traits": ["semantic", "narrow-scope", "classifier-like"],
            "should_not_handle": ["user work execution", "backlog persistence"],
            "internal_only": True,
        },
        "sourcer": {
            "summary": "Internal discovery agent for autonomous backlog candidate sourcing",
            "mission": "Scan runtime and workspace findings, then produce planner-review candidates instead of direct backlog writes.",
            "strongest_for": ["autonomous discovery", "finding synthesis", "candidate generation"],
            "behavior_traits": ["broad-scan", "exploratory", "candidate-oriented"],
            "should_not_handle": ["direct backlog persistence", "execution routing"],
            "internal_only": True,
        },
        "version_controller": {
            "summary": "Internal commit agent for task and sprint closeout version control",
            "mission": "Own commit execution and commit-policy application for task-completion and closeout flows.",
            "strongest_for": ["task commit execution", "closeout commit checks", "commit policy application"],
            "preferred_skills": ["version_controller"],
            "behavior_traits": ["narrow-scope", "git-focused", "policy-driven"],
            "should_not_handle": ["planning", "implementation", "backlog persistence"],
            "internal_only": True,
        },
    },
}


def build_agent_utilization_policy(
    payload: dict[str, Any] | None = None,
    *,
    policy_source: str,
    policy_path: str = "",
    load_error: str = "",
) -> AgentUtilizationPolicy:
    normalized_payload = deepcopy(payload or DEFAULT_AGENT_UTILIZATION_POLICY_DATA)
    public_roles = _normalize_mapping(normalized_payload.get("public_roles"))
    internal_agents = _normalize_mapping(normalized_payload.get("internal_agents"))
    policy_routes = _normalize_mapping(normalized_payload.get("policy_routes"))
    workflow_contract = _normalize_mapping(normalized_payload.get("workflow_contract"))
    public_capabilities = {
        role: _capability_from_payload(role, _normalize_mapping(role_payload))
        for role, role_payload in public_roles.items()
        if role in TEAM_ROLES
    }
    internal_capabilities = {
        name: _capability_from_payload(name, _normalize_mapping(agent_payload), internal_only=True)
        for name, agent_payload in internal_agents.items()
        if name in INTERNAL_TEAM_AGENTS
    }
    return AgentUtilizationPolicy(
        policy_source=policy_source,
        policy_path=policy_path,
        weights=_weights_from_payload(_normalize_mapping(normalized_payload.get("weights"))),
        public_capabilities=public_capabilities,
        internal_capabilities=internal_capabilities,
        user_intake_role=_coerce_public_role(policy_routes.get("user_intake"), "planner"),
        sourcer_review_role=_coerce_public_role(policy_routes.get("sourcer_review"), "planner"),
        planning_resume_role=_coerce_public_role(policy_routes.get("planning_resume"), "planner"),
        sprint_initial_default_role=_coerce_public_role(policy_routes.get("sprint_initial_default"), "planner"),
        sprint_force_qa=_coerce_bool(policy_routes.get("sprint_force_qa"), True),
        planner_reentry_requires_explicit_signal=_coerce_bool(
            policy_routes.get("planner_reentry_requires_explicit_signal"),
            True,
        ),
        verification_result_terminal=_coerce_bool(policy_routes.get("verification_result_terminal"), True),
        ignore_non_planner_backlog_proposals_for_routing=_coerce_bool(
            policy_routes.get("ignore_non_planner_backlog_proposals_for_routing"),
            True,
        ),
        implementation_review_cycle_limit=max(
            1,
            _coerce_int(workflow_contract.get("implementation_review_cycle_limit"), 3),
        ),
        load_error=load_error,
    )


DEFAULT_AGENT_UTILIZATION_POLICY = build_agent_utilization_policy(
    DEFAULT_AGENT_UTILIZATION_POLICY_DATA,
    policy_source="bundled_default",
)

PUBLIC_AGENT_CAPABILITIES: dict[str, AgentCapability] = DEFAULT_AGENT_UTILIZATION_POLICY.public_capabilities
INTERNAL_AGENT_CAPABILITIES: dict[str, AgentCapability] = DEFAULT_AGENT_UTILIZATION_POLICY.internal_capabilities
ALL_AGENT_CAPABILITIES: dict[str, AgentCapability] = {
    **PUBLIC_AGENT_CAPABILITIES,
    **INTERNAL_AGENT_CAPABILITIES,
}


def default_agent_utilization_policy() -> AgentUtilizationPolicy:
    return build_agent_utilization_policy(
        DEFAULT_AGENT_UTILIZATION_POLICY_DATA,
        policy_source="bundled_default",
    )


def render_agent_utilization_policy_yaml() -> str:
    return yaml.safe_dump(
        DEFAULT_AGENT_UTILIZATION_POLICY_DATA,
        allow_unicode=True,
        sort_keys=False,
    )


def agent_utilization_policy_file(workspace_root: str | Path) -> Path:
    workspace_path = Path(workspace_root).expanduser().resolve()
    return workspace_path / "orchestrator" / ".agents" / "skills" / "agent_utilization" / "policy.yaml"


def load_agent_utilization_policy(workspace_root: str | Path) -> AgentUtilizationPolicy:
    path = agent_utilization_policy_file(workspace_root)
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return build_agent_utilization_policy(
            DEFAULT_AGENT_UTILIZATION_POLICY_DATA,
            policy_source="default_fallback",
            load_error=f"Missing skill policy file: {path}",
        )
    except Exception as exc:
        return build_agent_utilization_policy(
            DEFAULT_AGENT_UTILIZATION_POLICY_DATA,
            policy_source="default_fallback",
            policy_path=str(path),
            load_error=f"Failed to load skill policy {path}: {exc}",
        )
    if not isinstance(payload, dict):
        return build_agent_utilization_policy(
            DEFAULT_AGENT_UTILIZATION_POLICY_DATA,
            policy_source="default_fallback",
            policy_path=str(path),
            load_error=f"Skill policy must contain a mapping: {path}",
        )
    return build_agent_utilization_policy(
        payload,
        policy_source="workspace_skill_policy",
        policy_path=str(path),
    )


def get_public_agent_capabilities(policy: AgentUtilizationPolicy | None = None) -> dict[str, AgentCapability]:
    return (policy or DEFAULT_AGENT_UTILIZATION_POLICY).public_capabilities


def get_internal_agent_capabilities(policy: AgentUtilizationPolicy | None = None) -> dict[str, AgentCapability]:
    return (policy or DEFAULT_AGENT_UTILIZATION_POLICY).internal_capabilities


def get_agent_capability(name: str, policy: AgentUtilizationPolicy | None = None) -> AgentCapability:
    normalized = str(name or "").strip()
    active_policy = policy or DEFAULT_AGENT_UTILIZATION_POLICY
    all_capabilities = {
        **active_policy.public_capabilities,
        **active_policy.internal_capabilities,
    }
    if normalized not in all_capabilities:
        raise KeyError(f"Unknown agent capability: {normalized}")
    return all_capabilities[normalized]


def role_descriptions(policy: AgentUtilizationPolicy | None = None) -> dict[str, str]:
    capabilities = get_public_agent_capabilities(policy)
    return {
        name: capability.summary
        for name, capability in capabilities.items()
        if name in TEAM_ROLES
    }


def internal_agent_descriptions(policy: AgentUtilizationPolicy | None = None) -> dict[str, str]:
    capabilities = get_internal_agent_capabilities(policy)
    return {
        name: capability.summary
        for name, capability in capabilities.items()
        if name in INTERNAL_TEAM_AGENTS
    }


def intent_to_role_map(policy: AgentUtilizationPolicy | None = None) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for name, capability in get_public_agent_capabilities(policy).items():
        if name not in TEAM_ROLES:
            continue
        for intent in capability.owned_intents:
            mapping[str(intent).strip().lower()] = name
    return mapping


EXECUTION_AGENT_ROLES = ("designer", "architect", "developer", "qa")
