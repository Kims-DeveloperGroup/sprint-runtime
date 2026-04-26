from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, TypedDict


TEAM_ROLES = (
    "orchestrator",
    "research",
    "planner",
    "designer",
    "architect",
    "developer",
    "qa",
)

INTERNAL_TEAM_AGENTS = (
    "parser",
    "sourcer",
    "version_controller",
)

ALL_RUNTIME_AGENTS = (
    *TEAM_ROLES,
    *INTERNAL_TEAM_AGENTS,
)

TERMINAL_REQUEST_STATUSES = {
    "completed",
    "committed",
    "failed",
    "cancelled",
}


class ReplyRoute(TypedDict, total=False):
    author_id: str
    author_name: str
    channel_id: str
    guild_id: str
    is_dm: bool
    message_id: str


class RequestEvent(TypedDict, total=False):
    type: str
    event_type: str
    actor: str
    summary: str
    payload: dict[str, Any]
    created_at: str
    updated_at: str


class WorkflowState(TypedDict, total=False):
    phase: str
    step: str
    phase_status: str
    planning_owner: str
    selected_role: str
    selection_source: str
    policy_source: str
    contract_version: int
    advisory_pass_count: int
    review_cycle_count: int
    reopen_category: str
    last_transition_at: str
    last_completed_role: str


class BacklogItem(TypedDict, total=False):
    backlog_id: str
    title: str
    summary: str
    scope: str
    kind: str
    status: str
    priority: str
    acceptance_criteria: list[str]
    artifacts: list[str]
    origin: dict[str, Any]
    source_request_id: str
    selected_at: str
    completed_at: str
    blocked_reason: str
    blocked_by_role: str
    required_inputs: list[str]
    recommended_next_step: str
    created_at: str
    updated_at: str


class SprintTodo(TypedDict, total=False):
    todo_id: str
    title: str
    summary: str
    status: str
    owner_role: str
    backlog_id: str
    request_id: str
    artifacts: list[str]
    started_at: str
    completed_at: str


class SprintState(TypedDict, total=False):
    sprint_id: str
    status: str
    milestone: str
    refined_milestone: str
    kickoff_brief: str
    requirements: list[str]
    artifacts: list[str]
    todos: list[SprintTodo]
    created_at: str
    updated_at: str
    completed_at: str


class RoleResult(TypedDict, total=False):
    request_id: str
    role: str
    status: str
    summary: str
    insights: list[str]
    proposals: dict[str, Any]
    artifacts: list[str]
    next_role: str
    error: str
    validation_notes: list[str]


class RequestRecord(TypedDict, total=False):
    request_id: str
    status: str
    intent: str
    urgency: str
    scope: str
    body: str
    artifacts: list[str]
    params: dict[str, Any]
    current_role: str
    next_role: str
    owner_role: str
    sprint_id: str
    source_message_created_at: str
    created_at: str
    updated_at: str
    fingerprint: str
    reply_route: ReplyRoute
    events: list[RequestEvent]
    result: RoleResult
    workflow: WorkflowState


@dataclass(slots=True, frozen=True)
class RoleAgentConfig:
    role: str
    name: str
    description: str
    token_env: str
    bot_id: str


@dataclass(slots=True, frozen=True)
class DiscordAgentsConfig:
    agents: dict[str, RoleAgentConfig]
    relay_channel_id: str
    startup_channel_id: str
    report_channel_id: str
    internal_agents: dict[str, RoleAgentConfig] = field(default_factory=dict)
    config_path: str = ""

    def get_role(self, role: str) -> RoleAgentConfig:
        return self.agents[role]

    def get_internal_agent(self, agent_name: str) -> RoleAgentConfig:
        return self.internal_agents[agent_name]

    @property
    def trusted_bot_ids(self) -> set[str]:
        return {
            *(config.bot_id for config in self.agents.values()),
            *(config.bot_id for config in self.internal_agents.values()),
        }


@dataclass(slots=True, frozen=True)
class RoleRuntimeConfig:
    model: str = "gpt-5.5"
    reasoning: str = "medium"


@dataclass(slots=True, frozen=True)
class ResearchRuntimeConfig:
    app: str | None = None
    notebook: str | None = None
    files: tuple[str, ...] = ()
    mode: str | None = None
    profile_path: str | None = None
    completion_timeout: float = 600.0
    callback_timeout: float = 1200.0
    cleanup: bool = False


@dataclass(slots=True, frozen=True)
class ActionConfig:
    name: str
    command: tuple[str, ...]
    lifecycle: str
    domain: str
    allowed_params: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class TeamRuntimeConfig:
    sprint_id: str
    sprint_interval_minutes: int = 180
    sprint_timezone: str = "Asia/Seoul"
    sprint_mode: str = "hybrid"
    sprint_start_mode: str = "auto"
    sprint_cutoff_time: str = "22:00"
    sprint_overlap_policy: str = "no_overlap"
    sprint_ingress_mode: str = "backlog_first"
    sprint_discovery_scope: str = "broad_scan"
    sprint_discovery_actions: tuple[str, ...] = ()
    ingress_dm: bool = True
    ingress_mentions: bool = True
    allowed_guild_ids: tuple[str, ...] = ()
    role_defaults: dict[str, RoleRuntimeConfig] = field(default_factory=dict)
    research_defaults: ResearchRuntimeConfig = field(default_factory=ResearchRuntimeConfig)
    actions: dict[str, ActionConfig] = field(default_factory=dict)


@dataclass(slots=True)
class MessageEnvelope:
    request_id: str | None
    sender: str
    target: str
    intent: str
    urgency: str
    scope: str
    artifacts: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    def to_dict(self, *, include_routing: bool = False) -> dict[str, Any]:
        payload = {
            "request_id": self.request_id or "",
            "intent": self.intent,
            "urgency": self.urgency,
            "scope": self.scope,
            "artifacts": list(self.artifacts),
            "params": dict(self.params),
            "body": self.body,
        }
        if include_routing:
            payload["from"] = self.sender
            payload["to"] = self.target
        return payload


@dataclass(slots=True)
class RoleSessionState:
    role: str
    sprint_id: str
    session_id: str
    workspace_path: str
    created_at: str
    last_used_at: str
    runtime_identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RoleSessionState":
        return cls(
            role=str(payload.get("role") or "").strip(),
            sprint_id=str(payload.get("sprint_id") or "").strip(),
            session_id=str(payload.get("session_id") or "").strip(),
            workspace_path=str(payload.get("workspace_path") or "").strip(),
            created_at=str(payload.get("created_at") or "").strip(),
            last_used_at=str(payload.get("last_used_at") or "").strip(),
            runtime_identity=str(payload.get("runtime_identity") or "").strip(),
        )


__all__ = [
    "ALL_RUNTIME_AGENTS",
    "ActionConfig",
    "BacklogItem",
    "DiscordAgentsConfig",
    "INTERNAL_TEAM_AGENTS",
    "MessageEnvelope",
    "ReplyRoute",
    "RequestEvent",
    "RequestRecord",
    "ResearchRuntimeConfig",
    "RoleAgentConfig",
    "RoleResult",
    "RoleRuntimeConfig",
    "RoleSessionState",
    "SprintState",
    "SprintTodo",
    "TEAM_ROLES",
    "TERMINAL_REQUEST_STATUSES",
    "TeamRuntimeConfig",
    "WorkflowState",
]
