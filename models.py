from __future__ import annotations

"""Legacy compatibility facade for shared runtime contracts.

The canonical home for shared `teams_runtime` contracts is now
`teams_runtime.shared.models`. This module remains as an import-stable shim
until internal callers and external consumers finish migrating.
"""

from teams_runtime.shared.models import (
    ALL_RUNTIME_AGENTS,
    ActionConfig,
    BacklogItem,
    DiscordAgentsConfig,
    INTERNAL_TEAM_AGENTS,
    MessageEnvelope,
    ReplyRoute,
    RequestEvent,
    RequestRecord,
    ResearchRuntimeConfig,
    RoleAgentConfig,
    RoleResult,
    RoleRuntimeConfig,
    RoleSessionState,
    SprintState,
    SprintTodo,
    TEAM_ROLES,
    TERMINAL_REQUEST_STATUSES,
    TeamRuntimeConfig,
    WorkflowState,
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
