from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


TEAM_ROLES = (
    "orchestrator",
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
    model: str = "gpt-5.4"
    reasoning: str = "medium"


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
        )
