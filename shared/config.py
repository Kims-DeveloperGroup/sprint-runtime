"""Runtime config loading and mutation helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from teams_runtime.shared.models import (
    ActionConfig,
    DiscordAgentsConfig,
    ResearchRuntimeConfig,
    RoleAgentConfig,
    RoleRuntimeConfig,
    TEAM_ROLES,
    TeamRuntimeConfig,
)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Missing config file: {path}") from exc
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"Config file must contain a mapping: {path}")
    return payload


def _normalize_snowflake(value: Any, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or not normalized.isdigit():
        raise ValueError(f"{field_name} must be a non-empty numeric Discord snowflake.")
    return normalized


_TIME_PATTERN = re.compile(r"^(?P<hour>\d{2}):(?P<minute>\d{2})$")
_SCAFFOLD_PLACEHOLDER_SNOWFLAKES = frozenset(
    {
        "111111111111111111",
        "111111111111111112",
        "111111111111111113",
        "111111111111111114",
        "111111111111111115",
        "111111111111111116",
        "111111111111111117",
        "111111111111111118",
        "111111111111111119",
    }
)
_ALLOW_PLACEHOLDER_IDS_ENV = "TEAMS_RUNTIME_ALLOW_PLACEHOLDER_IDS"


def _normalize_cutoff_time(value: Any) -> str:
    normalized = str(value or "22:00").strip()
    match = _TIME_PATTERN.fullmatch(normalized)
    if not match:
        raise ValueError("team_runtime.yaml sprint.cutoff_time must use HH:MM 24-hour format.")
    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour > 23 or minute > 59:
        raise ValueError("team_runtime.yaml sprint.cutoff_time must use a valid 24-hour time.")
    return f"{hour:02d}:{minute:02d}"


def _normalize_optional_text(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_string_sequence(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings when provided.")
    normalized: list[str] = []
    for item in value:
        candidate = str(item or "").strip()
        if not candidate:
            raise ValueError(f"{field_name} must contain only non-empty strings.")
        normalized.append(candidate)
    return tuple(normalized)


def _normalize_positive_timeout(value: Any, *, field_name: str, default: float) -> float:
    if value in (None, ""):
        return default
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a positive number.") from exc
    if normalized <= 0:
        raise ValueError(f"{field_name} must be a positive number.")
    return normalized


def _normalize_research_defaults(value: Any) -> ResearchRuntimeConfig:
    payload = dict(value or {}) if isinstance(value, dict) else {}
    return ResearchRuntimeConfig(
        app=_normalize_optional_text(payload.get("app")),
        notebook=_normalize_optional_text(payload.get("notebook")),
        files=_normalize_string_sequence(payload.get("files"), field_name="team_runtime.yaml research_defaults.files"),
        mode=_normalize_optional_text(payload.get("mode")),
        profile_path=_normalize_optional_text(payload.get("profile_path")),
        completion_timeout=_normalize_positive_timeout(
            payload.get("completion_timeout"),
            field_name="team_runtime.yaml research_defaults.completion_timeout",
            default=600.0,
        ),
        callback_timeout=_normalize_positive_timeout(
            payload.get("callback_timeout"),
            field_name="team_runtime.yaml research_defaults.callback_timeout",
            default=1200.0,
        ),
        cleanup=bool(payload.get("cleanup", False)),
    )


def _ensure_non_placeholder_snowflake(
    value: str,
    *,
    field_name: str,
    config_path: Path,
    allow_placeholder_ids: bool,
) -> str:
    if allow_placeholder_ids or value not in _SCAFFOLD_PLACEHOLDER_SNOWFLAKES:
        return value
    raise ValueError(
        f"{field_name} in {config_path} still uses scaffold placeholder snowflake {value}. "
        "Use the real Discord channel/bot ID before starting runtime listeners."
    )


def runtime_placeholder_ids_allowed() -> bool:
    normalized = str(os.getenv(_ALLOW_PLACEHOLDER_IDS_ENV) or "").strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _runtime_discord_config_fingerprint(config: DiscordAgentsConfig) -> dict[str, Any]:
    return {
        "relay_channel_id": config.relay_channel_id,
        "startup_channel_id": config.startup_channel_id,
        "report_channel_id": config.report_channel_id,
        "agents": {
            role: {
                "token_env": agent.token_env,
                "bot_id": agent.bot_id,
            }
            for role, agent in sorted(config.agents.items())
        },
        "internal_agents": {
            role: {
                "token_env": agent.token_env,
                "bot_id": agent.bot_id,
            }
            for role, agent in sorted(config.internal_agents.items())
        },
    }


def _validate_duplicate_runtime_discord_config(
    workspace_path: Path,
    runtime_config: DiscordAgentsConfig,
    *,
    allow_placeholder_ids: bool,
) -> None:
    if workspace_path.name != "teams_generated":
        return
    duplicate_path = workspace_path.parent / "discord_agents_config.yaml"
    if not duplicate_path.is_file():
        return
    if duplicate_path.resolve() == Path(runtime_config.config_path).resolve():
        return
    duplicate_config = load_discord_agents_config(
        duplicate_path.parent,
        allow_placeholder_ids=allow_placeholder_ids,
    )
    if _runtime_discord_config_fingerprint(duplicate_config) == _runtime_discord_config_fingerprint(runtime_config):
        return
    raise ValueError(
        "Runtime workspace discord_agents_config.yaml does not match the duplicate file at "
        f"{duplicate_path}. Use {runtime_config.config_path} as the operational source of truth "
        "or sync both files before starting runtime listeners."
    )


def load_discord_agents_config(
    workspace_root: str | Path,
    *,
    allow_placeholder_ids: bool = True,
) -> DiscordAgentsConfig:
    workspace_path = Path(workspace_root).expanduser().resolve()
    config_path = workspace_path / "discord_agents_config.yaml"
    payload = _load_yaml(config_path)
    raw_agents = payload.get("agents")
    if not isinstance(raw_agents, dict):
        raise ValueError("discord_agents_config.yaml must define an 'agents' mapping.")

    relay_channel_id = str(payload.get("relay_channel_id") or "").strip()
    relay_channel_env = str(payload.get("relay_channel_env") or "").strip()
    if not relay_channel_id and relay_channel_env:
        relay_channel_id = str(os.getenv(relay_channel_env) or "").strip()
    relay_channel_id = _normalize_snowflake(
        relay_channel_id,
        field_name="relay_channel_id",
    )
    relay_channel_id = _ensure_non_placeholder_snowflake(
        relay_channel_id,
        field_name="relay_channel_id",
        config_path=config_path,
        allow_placeholder_ids=allow_placeholder_ids,
    )
    startup_channel_id = str(payload.get("startup_channel_id") or "").strip()
    startup_channel_env = str(payload.get("startup_channel_env") or "").strip()
    if not startup_channel_id and startup_channel_env:
        startup_channel_id = str(os.getenv(startup_channel_env) or "").strip()
    if not startup_channel_id:
        startup_channel_id = relay_channel_id
    startup_channel_id = _normalize_snowflake(
        startup_channel_id,
        field_name="startup_channel_id",
    )
    startup_channel_id = _ensure_non_placeholder_snowflake(
        startup_channel_id,
        field_name="startup_channel_id",
        config_path=config_path,
        allow_placeholder_ids=allow_placeholder_ids,
    )
    report_channel_id = str(payload.get("report_channel_id") or "").strip()
    report_channel_env = str(payload.get("report_channel_env") or "").strip()
    if not report_channel_id and report_channel_env:
        report_channel_id = str(os.getenv(report_channel_env) or "").strip()
    if not report_channel_id:
        report_channel_id = startup_channel_id
    report_channel_id = _normalize_snowflake(
        report_channel_id,
        field_name="report_channel_id",
    )
    report_channel_id = _ensure_non_placeholder_snowflake(
        report_channel_id,
        field_name="report_channel_id",
        config_path=config_path,
        allow_placeholder_ids=allow_placeholder_ids,
    )

    agents: dict[str, RoleAgentConfig] = {}
    for role in TEAM_ROLES:
        raw = raw_agents.get(role)
        if not isinstance(raw, dict):
            raise ValueError(f"discord_agents_config.yaml is missing role '{role}'.")
        token_env = str(raw.get("token_env") or "").strip()
        if not token_env:
            raise ValueError(f"Role '{role}' is missing token_env.")
        agents[role] = RoleAgentConfig(
            role=role,
            name=str(raw.get("name") or role).strip() or role,
            description=str(raw.get("description") or "").strip(),
            token_env=token_env,
            bot_id=_ensure_non_placeholder_snowflake(
                _normalize_snowflake(raw.get("bot_id"), field_name=f"{role}.bot_id"),
                field_name=f"{role}.bot_id",
                config_path=config_path,
                allow_placeholder_ids=allow_placeholder_ids,
            ),
        )

    raw_internal_agents = payload.get("internal_agents") or {}
    if not isinstance(raw_internal_agents, dict):
        raise ValueError("discord_agents_config.yaml internal_agents must be a mapping when provided.")
    internal_agents: dict[str, RoleAgentConfig] = {}
    for agent_name, raw in raw_internal_agents.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Internal agent '{agent_name}' must be a mapping.")
        token_env = str(raw.get("token_env") or "").strip()
        if not token_env:
            raise ValueError(f"Internal agent '{agent_name}' is missing token_env.")
        normalized_agent_name = str(agent_name or "").strip()
        if not normalized_agent_name:
            raise ValueError("discord_agents_config.yaml internal_agents keys must be non-empty.")
        internal_agents[normalized_agent_name] = RoleAgentConfig(
            role=str(raw.get("role") or normalized_agent_name).strip() or normalized_agent_name,
            name=str(raw.get("name") or normalized_agent_name).strip() or normalized_agent_name,
            description=str(raw.get("description") or "").strip(),
            token_env=token_env,
            bot_id=_ensure_non_placeholder_snowflake(
                _normalize_snowflake(raw.get("bot_id"), field_name=f"internal_agents.{normalized_agent_name}.bot_id"),
                field_name=f"internal_agents.{normalized_agent_name}.bot_id",
                config_path=config_path,
                allow_placeholder_ids=allow_placeholder_ids,
            ),
        )

    return DiscordAgentsConfig(
        agents=agents,
        relay_channel_id=relay_channel_id,
        startup_channel_id=startup_channel_id,
        report_channel_id=report_channel_id,
        internal_agents=internal_agents,
        config_path=str(config_path),
    )


def validate_runtime_discord_agents_config(workspace_root: str | Path) -> DiscordAgentsConfig:
    workspace_path = Path(workspace_root).expanduser().resolve()
    allow_placeholder_ids = runtime_placeholder_ids_allowed()
    config = load_discord_agents_config(
        workspace_root,
        allow_placeholder_ids=allow_placeholder_ids,
    )
    _validate_duplicate_runtime_discord_config(
        workspace_path,
        config,
        allow_placeholder_ids=allow_placeholder_ids,
    )
    return config


def load_team_runtime_config(workspace_root: str | Path) -> TeamRuntimeConfig:
    workspace_path = Path(workspace_root).expanduser().resolve()
    payload = _load_yaml(workspace_path / "team_runtime.yaml")
    raw_sprint = payload.get("sprint")
    if not isinstance(raw_sprint, dict) or not str(raw_sprint.get("id") or "").strip():
        raise ValueError("team_runtime.yaml must define sprint.id.")
    interval_minutes = int(raw_sprint.get("interval_minutes") or 180)
    if interval_minutes <= 0:
        raise ValueError("team_runtime.yaml sprint.interval_minutes must be a positive integer.")
    sprint_mode = str(raw_sprint.get("mode") or "hybrid").strip().lower() or "hybrid"
    if sprint_mode not in {"hybrid", "rolling", "wall_clock"}:
        raise ValueError("team_runtime.yaml sprint.mode must be hybrid, rolling, or wall_clock.")
    sprint_start_mode = str(raw_sprint.get("start_mode") or "auto").strip().lower() or "auto"
    if sprint_start_mode not in {"auto", "manual_daily"}:
        raise ValueError("team_runtime.yaml sprint.start_mode must be auto or manual_daily.")
    sprint_cutoff_time = _normalize_cutoff_time(raw_sprint.get("cutoff_time") or "22:00")
    overlap_policy = str(raw_sprint.get("overlap_policy") or "no_overlap").strip().lower() or "no_overlap"
    if overlap_policy != "no_overlap":
        raise ValueError("team_runtime.yaml sprint.overlap_policy currently supports only no_overlap.")
    ingress_mode = str(raw_sprint.get("ingress_mode") or "backlog_first").strip().lower() or "backlog_first"
    if ingress_mode not in {"backlog_first", "immediate_if_idle", "dual_path"}:
        raise ValueError(
            "team_runtime.yaml sprint.ingress_mode must be backlog_first, immediate_if_idle, or dual_path."
        )
    discovery_scope = str(raw_sprint.get("discovery_scope") or "broad_scan").strip().lower() or "broad_scan"
    if discovery_scope not in {"workspace_only", "plus_git", "broad_scan"}:
        raise ValueError(
            "team_runtime.yaml sprint.discovery_scope must be workspace_only, plus_git, or broad_scan."
        )
    discovery_actions = raw_sprint.get("discovery_actions") or []
    if not isinstance(discovery_actions, list) or not all(
        isinstance(item, str) and item.strip() for item in discovery_actions
    ):
        raise ValueError("team_runtime.yaml sprint.discovery_actions must be a list of strings.")
    ingress = payload.get("ingress") or {}
    if "approval" in payload:
        raise ValueError("team_runtime.yaml approval is no longer supported.")
    raw_role_defaults = payload.get("role_defaults") or {}
    role_defaults: dict[str, RoleRuntimeConfig] = {}
    for role in TEAM_ROLES:
        defaults = raw_role_defaults.get(role) or {}
        reasoning = str(defaults.get("reasoning") or "").strip() or (
            "high" if role == "developer" else "medium"
        )
        model = str(defaults.get("model") or "").strip() or "gpt-5.5"
        role_defaults[role] = RoleRuntimeConfig(model=model, reasoning=reasoning)
    raw_research_defaults = payload.get("research_defaults")
    if raw_research_defaults not in (None, {}) and not isinstance(raw_research_defaults, dict):
        raise ValueError("team_runtime.yaml research_defaults must be a mapping.")
    research_defaults = _normalize_research_defaults(raw_research_defaults)

    actions: dict[str, ActionConfig] = {}
    raw_actions = payload.get("actions") or {}
    if raw_actions and not isinstance(raw_actions, dict):
        raise ValueError("team_runtime.yaml actions must be a mapping.")
    for name, raw in raw_actions.items():
        if not isinstance(raw, dict):
            raise ValueError(f"Action '{name}' must be a mapping.")
        raw_command = raw.get("command")
        if not isinstance(raw_command, list) or not raw_command or not all(
            isinstance(item, str) and item.strip() for item in raw_command
        ):
            raise ValueError(f"Action '{name}' must declare a non-empty string command list.")
        lifecycle = str(raw.get("lifecycle") or "foreground").strip().lower()
        if lifecycle not in {"foreground", "managed"}:
            raise ValueError(f"Action '{name}' has unsupported lifecycle '{lifecycle}'.")
        allowed_params = raw.get("allowed_params") or []
        if not isinstance(allowed_params, list) or not all(
            isinstance(item, str) and item.strip() for item in allowed_params
        ):
            raise ValueError(f"Action '{name}' allowed_params must be a list of strings.")
        if "approval_required" in raw:
            raise ValueError(f"Action '{name}' approval_required is no longer supported.")
        actions[name] = ActionConfig(
            name=str(name).strip(),
            command=tuple(raw_command),
            lifecycle=lifecycle,
            domain=str(raw.get("domain") or "기타").strip() or "기타",
            allowed_params=tuple(item.strip() for item in allowed_params),
        )

    allowed_guild_ids = payload.get("allowed_guild_ids") or []
    if not isinstance(allowed_guild_ids, list):
        raise ValueError("team_runtime.yaml allowed_guild_ids must be a list.")

    return TeamRuntimeConfig(
        sprint_id=str(raw_sprint.get("id")).strip(),
        sprint_interval_minutes=interval_minutes,
        sprint_timezone=str(raw_sprint.get("timezone") or "Asia/Seoul").strip() or "Asia/Seoul",
        sprint_mode=sprint_mode,
        sprint_start_mode=sprint_start_mode,
        sprint_cutoff_time=sprint_cutoff_time,
        sprint_overlap_policy=overlap_policy,
        sprint_ingress_mode=ingress_mode,
        sprint_discovery_scope=discovery_scope,
        sprint_discovery_actions=tuple(item.strip() for item in discovery_actions),
        ingress_dm=bool(ingress.get("dm", True)),
        ingress_mentions=bool(ingress.get("mentions", True)),
        allowed_guild_ids=tuple(str(item).strip() for item in allowed_guild_ids if str(item).strip()),
        role_defaults=role_defaults,
        research_defaults=research_defaults,
        actions=actions,
    )


def update_team_runtime_role_defaults(
    workspace_root: str | Path,
    role: str,
    *,
    model: str | None = None,
    reasoning: str | None = None,
) -> RoleRuntimeConfig:
    normalized_role = str(role or "").strip()
    if normalized_role not in TEAM_ROLES:
        raise ValueError(f"Unsupported role: {normalized_role or role}")

    normalized_model = None if model is None else str(model).strip()
    normalized_reasoning = None if reasoning is None else str(reasoning).strip()
    if normalized_model == "":
        raise ValueError("model must be a non-empty string when provided.")
    if normalized_reasoning == "":
        raise ValueError("reasoning must be a non-empty string when provided.")
    if normalized_model is None and normalized_reasoning is None:
        raise ValueError("At least one of model or reasoning must be provided.")

    workspace_path = Path(workspace_root).expanduser().resolve()
    config_path = workspace_path / "team_runtime.yaml"
    payload = _load_yaml(config_path)
    raw_role_defaults = payload.get("role_defaults")
    if raw_role_defaults is None:
        raw_role_defaults = {}
        payload["role_defaults"] = raw_role_defaults
    if not isinstance(raw_role_defaults, dict):
        raise ValueError("team_runtime.yaml role_defaults must be a mapping.")

    current_defaults = raw_role_defaults.get(normalized_role) or {}
    if not isinstance(current_defaults, dict):
        current_defaults = {}

    updated_defaults = dict(current_defaults)
    if normalized_model is not None:
        updated_defaults["model"] = normalized_model
    if normalized_reasoning is not None:
        updated_defaults["reasoning"] = normalized_reasoning
    raw_role_defaults[normalized_role] = updated_defaults

    config_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    runtime_config = load_team_runtime_config(workspace_path)
    return runtime_config.role_defaults[normalized_role]


def update_team_runtime_research_defaults(
    workspace_root: str | Path,
    *,
    app: str | None = None,
    notebook: str | None = None,
    files: list[str] | None = None,
    mode: str | None = None,
    profile_path: str | None = None,
    completion_timeout: float | None = None,
    callback_timeout: float | None = None,
    cleanup: bool | None = None,
) -> ResearchRuntimeConfig:
    updates = {
        "app": app,
        "notebook": notebook,
        "files": files,
        "mode": mode,
        "profile_path": profile_path,
        "completion_timeout": completion_timeout,
        "callback_timeout": callback_timeout,
        "cleanup": cleanup,
    }
    if not any(value is not None for value in updates.values()):
        raise ValueError("At least one research setting must be provided.")

    workspace_path = Path(workspace_root).expanduser().resolve()
    config_path = workspace_path / "team_runtime.yaml"
    payload = _load_yaml(config_path)
    raw_research_defaults = payload.get("research_defaults")
    if raw_research_defaults is None:
        raw_research_defaults = {}
        payload["research_defaults"] = raw_research_defaults
    if not isinstance(raw_research_defaults, dict):
        raise ValueError("team_runtime.yaml research_defaults must be a mapping.")

    if app is not None:
        normalized = str(app).strip()
        if not normalized:
            raise ValueError("app must be a non-empty string when provided.")
        raw_research_defaults["app"] = normalized
    if notebook is not None:
        normalized = str(notebook).strip()
        if not normalized:
            raise ValueError("notebook must be a non-empty string when provided.")
        raw_research_defaults["notebook"] = normalized
    if files is not None:
        normalized_files = [str(item).strip() for item in files]
        if not all(normalized_files):
            raise ValueError("files must contain only non-empty strings.")
        raw_research_defaults["files"] = normalized_files
    if mode is not None:
        normalized = str(mode).strip()
        raw_research_defaults["mode"] = normalized
    if profile_path is not None:
        normalized = str(profile_path).strip()
        raw_research_defaults["profile_path"] = normalized
    if completion_timeout is not None:
        if float(completion_timeout) <= 0:
            raise ValueError("completion_timeout must be a positive number.")
        raw_research_defaults["completion_timeout"] = float(completion_timeout)
    if callback_timeout is not None:
        if float(callback_timeout) <= 0:
            raise ValueError("callback_timeout must be a positive number.")
        raw_research_defaults["callback_timeout"] = float(callback_timeout)
    if cleanup is not None:
        raw_research_defaults["cleanup"] = bool(cleanup)

    config_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    runtime_config = load_team_runtime_config(workspace_path)
    return runtime_config.research_defaults
