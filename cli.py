from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Awaitable
from pathlib import Path

from teams_runtime.adapters.cli.commands import build_parser as build_cli_parser
from teams_runtime.adapters.cli.commands import cmd_config_research_set_impl
from teams_runtime.adapters.cli.commands import cmd_config_role_set_impl
from teams_runtime.adapters.cli.commands import cmd_init_impl
from teams_runtime.adapters.cli.commands import cmd_list_impl
from teams_runtime.adapters.cli.commands import cmd_restart_impl
from teams_runtime.adapters.cli.commands import cmd_sprint_restart_impl
from teams_runtime.adapters.cli.commands import cmd_sprint_start_impl
from teams_runtime.adapters.cli.commands import cmd_sprint_status_impl
from teams_runtime.adapters.cli.commands import cmd_sprint_stop_impl
from teams_runtime.adapters.cli.commands import cmd_start_impl
from teams_runtime.adapters.cli.commands import cmd_status_impl
from teams_runtime.adapters.cli.commands import cmd_stop_impl
from teams_runtime.adapters.cli.commands import dispatch_main
from teams_runtime.adapters.cli.commands import run_services_impl
from teams_runtime.adapters.discord.client import DiscordClient, DiscordListenError, classify_discord_exception
from teams_runtime.adapters.discord.lifecycle import (
    role_service_status,
    run_foreground_role_service,
    start_background_role_service,
    stop_background_role_service,
)
from teams_runtime.shared.config import (
    load_team_runtime_config,
    update_team_runtime_research_defaults,
    update_team_runtime_role_defaults,
    validate_runtime_discord_agents_config,
)
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import iter_json_records, read_json, utc_now_iso, write_json
from teams_runtime.workflows.orchestration.team_service import (
    RELAY_TRANSPORT_DISCORD,
    RELAY_TRANSPORT_INTERNAL,
    TeamService,
)
from teams_runtime.workflows.sprints.lifecycle import build_sprint_artifact_folder_name
from teams_runtime.core.template import refresh_workspace_prompt_assets, scaffold_workspace
from teams_runtime.shared.models import ALL_RUNTIME_AGENTS, INTERNAL_TEAM_AGENTS, TEAM_ROLES
from teams_runtime.runtime.session_manager import RoleSessionManager


logging.basicConfig(
    level=os.getenv("TEAMS_RUNTIME_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
LOGGER = logging.getLogger(__name__)

DEFAULT_WORKSPACE_DIRNAME = "teams_generated"
INTERNAL_AGENT_LISTENER_RETRY_SECONDS = 5.0
DEFAULT_RELAY_TRANSPORT = RELAY_TRANSPORT_INTERNAL


def _workspace_root_help_text() -> str:
    return (
        "Workspace root. Defaults to the current directory when it already contains runtime config; "
        "otherwise prefers ./teams_generated and then ./workspace/teams_generated."
    )


def _default_workspace_root_candidates(cwd: Path) -> list[Path]:
    return [
        cwd / DEFAULT_WORKSPACE_DIRNAME,
        cwd / "workspace" / DEFAULT_WORKSPACE_DIRNAME,
    ]


def _requires_runtime_discord_validation(role: str | None) -> bool:
    if role is None:
        return True
    return role in TEAM_ROLES or role == "sourcer"


class InternalAgentService:
    def __init__(self, workspace_root: Path, role: str):
        if role not in INTERNAL_TEAM_AGENTS:
            raise ValueError(f"Unsupported internal agent: {role}")
        self.paths = RuntimePaths.from_root(workspace_root)
        self.paths.ensure_runtime_dirs()
        self.role = role
        self.runtime_config = load_team_runtime_config(self.paths.workspace_root)
        self.discord_config = validate_runtime_discord_agents_config(self.paths.workspace_root)
        self.discord_agent_config = self.discord_config.internal_agents.get(role)
        self.session_manager = RoleSessionManager(
            self.paths,
            role,
            self.runtime_config.sprint_id,
            agent_root=self.paths.internal_agent_root(role),
        )
        self.discord_client: DiscordClient | None = None

    def _listener_state_metadata(self) -> dict[str, str]:
        metadata = {
            "listener_configured_role": self.role,
            "listener_resolved_workspace_root": str(self.paths.workspace_root),
            "listener_discord_config_path": str(self.discord_config.config_path or ""),
        }
        if self.discord_agent_config is not None:
            metadata["listener_expected_bot_id"] = str(self.discord_agent_config.bot_id or "").strip()
        return metadata

    def _update_state(self, **updates) -> None:
        state = read_json(self.paths.agent_state_file(self.role))
        if not isinstance(state, dict):
            state = {"role": self.role}
        state.update({key: value for key, value in updates.items()})
        state["updated_at"] = utc_now_iso()
        write_json(self.paths.agent_state_file(self.role), state)

    def _record_listener_health_state(
        self,
        *,
        status: str,
        error: str,
        category: str,
        recovery_action: str,
        connected_bot_name: str = "",
        connected_bot_id: str = "",
    ) -> None:
        payload = {
            "listener_status": str(status or "").strip(),
            "listener_error": str(error or "").strip(),
            "listener_error_category": str(category or "").strip(),
            "listener_recovery_action": str(recovery_action or "").strip(),
            "listener_connected_bot_name": str(connected_bot_name or "").strip(),
            "listener_connected_bot_id": str(connected_bot_id or "").strip(),
            "listener_updated_at": utc_now_iso(),
        }
        if payload["listener_status"] == "connected":
            payload["listener_connected_at"] = utc_now_iso()
        else:
            payload["listener_last_failure_at"] = utc_now_iso()
        payload.update(self._listener_state_metadata())
        self._update_state(**payload)

    async def _handle_presence_message(self, _message) -> None:
        return None

    async def _on_presence_ready(self) -> None:
        identity = self.discord_client.current_identity() if self.discord_client is not None else {}
        self._record_listener_health_state(
            status="connected",
            error="",
            category="connected",
            recovery_action="",
            connected_bot_name=str(identity.get("name") or ""),
            connected_bot_id=str(identity.get("id") or ""),
        )

    async def _run_discord_presence_once(self) -> None:
        if self.discord_agent_config is None:
            return
        client = DiscordClient(
            token_env=self.discord_agent_config.token_env,
            expected_bot_id=self.discord_agent_config.bot_id,
            transcript_log_file=self.paths.agent_discord_log(self.role),
            attachment_dir=self.paths.sprint_attachment_root(
                build_sprint_artifact_folder_name(self.runtime_config.sprint_id)
            ),
            client_name=self.role,
        )
        self.discord_client = client
        try:
            await client.listen(self._handle_presence_message, on_ready=self._on_presence_ready)
        finally:
            self.discord_client = None

    async def _listen_forever(self) -> None:
        if self.discord_agent_config is None:
            return
        while True:
            try:
                await self._run_discord_presence_once()
                diagnostics = {
                    "category": "client_disconnected",
                    "summary": "Discord client disconnected.",
                    "recovery_action": "자동 재연결을 기다리거나 서비스를 재시작합니다.",
                }
                LOGGER.warning("Discord listener disconnected for internal agent %s; retrying", self.role)
            except asyncio.CancelledError:
                raise
            except DiscordListenError as exc:
                diagnostics = classify_discord_exception(
                    exc,
                    token_env_name=self.discord_agent_config.token_env,
                    expected_bot_id=self.discord_agent_config.bot_id,
                )
                LOGGER.warning("Discord listener retry scheduled for internal agent %s after listen error: %s", self.role, exc)
            except Exception as exc:
                diagnostics = classify_discord_exception(
                    exc,
                    token_env_name=self.discord_agent_config.token_env,
                    expected_bot_id=self.discord_agent_config.bot_id,
                )
                LOGGER.exception("Discord listener failed for internal agent %s; retrying", self.role)
            self._record_listener_health_state(
                status="reconnecting",
                error=diagnostics["summary"],
                category=diagnostics["category"],
                recovery_action=diagnostics["recovery_action"],
            )
            await asyncio.sleep(INTERNAL_AGENT_LISTENER_RETRY_SECONDS)

    async def run(self) -> None:
        await asyncio.to_thread(self.session_manager.ensure_session)
        tasks: list[Awaitable[None]] = []
        if self.discord_agent_config is not None:
            tasks.append(self._listen_forever())
        if not tasks:
            await asyncio.Event().wait()
            return
        await asyncio.gather(*tasks)


def build_agent_service(
    workspace_root: Path,
    role: str,
    *,
    enable_discord_client: bool = True,
    relay_transport: str = DEFAULT_RELAY_TRANSPORT,
) -> TeamService | InternalAgentService:
    if role in INTERNAL_TEAM_AGENTS:
        return InternalAgentService(workspace_root, role)
    return TeamService(
        workspace_root,
        role,
        enable_discord_client=enable_discord_client,
        relay_transport=relay_transport,
    )


def is_workspace_root(path: str | Path) -> bool:
    candidate = Path(path).expanduser().resolve()
    return (candidate / "team_runtime.yaml").is_file() and (candidate / "discord_agents_config.yaml").is_file()


def resolve_workspace_root(raw: str | None) -> Path:
    normalized = str(raw or "").strip()
    if normalized:
        return Path(normalized).expanduser().resolve()

    cwd = Path.cwd().resolve()
    if is_workspace_root(cwd):
        return cwd
    for candidate in _default_workspace_root_candidates(cwd):
        if is_workspace_root(candidate):
            return candidate
    return cwd / DEFAULT_WORKSPACE_DIRNAME


def build_parser() -> argparse.ArgumentParser:
    return build_cli_parser(
        all_runtime_agents=ALL_RUNTIME_AGENTS,
        team_roles=TEAM_ROLES,
        relay_transport_internal=RELAY_TRANSPORT_INTERNAL,
        relay_transport_discord=RELAY_TRANSPORT_DISCORD,
        default_relay_transport=DEFAULT_RELAY_TRANSPORT,
        workspace_root_help_text=_workspace_root_help_text(),
    )


async def run_services(
    workspace_root: Path,
    role: str | None,
    *,
    relay_transport: str = DEFAULT_RELAY_TRANSPORT,
) -> None:
    await run_services_impl(
        workspace_root,
        role,
        relay_transport=relay_transport,
        runtime_paths_cls=RuntimePaths,
        validate_runtime_discord_agents_config=validate_runtime_discord_agents_config,
        requires_runtime_discord_validation=_requires_runtime_discord_validation,
        all_runtime_agents=ALL_RUNTIME_AGENTS,
        build_agent_service=build_agent_service,
        run_foreground_role_service=run_foreground_role_service,
    )


def cmd_init(workspace_root: Path, *, refresh_prompts: bool = False, reset: bool = False) -> int:
    cwd_config = Path.cwd().resolve() / "discord_agents_config.yaml"
    discord_config_source = cwd_config if reset and cwd_config.is_file() else None

    def scaffold_workspace_for_init(root: Path) -> list[Path]:
        return scaffold_workspace(root, discord_agents_config_source=discord_config_source)

    return cmd_init_impl(
        workspace_root,
        scaffold_workspace=scaffold_workspace_for_init,
        refresh_workspace_prompts=refresh_workspace_prompt_assets,
        refresh_prompts=refresh_prompts,
        reset=reset,
    )


def cmd_start(
    workspace_root: Path,
    role: str | None,
    *,
    relay_transport: str = DEFAULT_RELAY_TRANSPORT,
) -> int:
    return cmd_start_impl(
        workspace_root,
        role,
        relay_transport=relay_transport,
        runtime_paths_cls=RuntimePaths,
        requires_runtime_discord_validation=_requires_runtime_discord_validation,
        validate_runtime_discord_agents_config=validate_runtime_discord_agents_config,
        all_runtime_agents=ALL_RUNTIME_AGENTS,
        build_agent_service=build_agent_service,
        start_background_role_service=start_background_role_service,
    )


def cmd_status(
    workspace_root: Path,
    role: str | None,
    request_id: str | None = None,
    *,
    sprint: bool = False,
    backlog: bool = False,
) -> int:
    return cmd_status_impl(
        workspace_root,
        role,
        request_id=request_id,
        sprint=sprint,
        backlog=backlog,
        runtime_paths_cls=RuntimePaths,
        load_team_runtime_config=load_team_runtime_config,
        read_json=read_json,
        iter_json_records=iter_json_records,
        role_service_status=role_service_status,
        all_runtime_agents=ALL_RUNTIME_AGENTS,
        list_command=cmd_list,
    )


def cmd_stop(workspace_root: Path, role: str | None) -> int:
    return cmd_stop_impl(
        workspace_root,
        role,
        runtime_paths_cls=RuntimePaths,
        all_runtime_agents=ALL_RUNTIME_AGENTS,
        stop_background_role_service=stop_background_role_service,
    )


def cmd_restart(
    workspace_root: Path,
    role: str | None,
    *,
    relay_transport: str = DEFAULT_RELAY_TRANSPORT,
) -> int:
    return cmd_restart_impl(
        workspace_root,
        role,
        relay_transport=relay_transport,
        stop_command=cmd_stop,
        start_command=cmd_start,
    )


def cmd_list(workspace_root: Path, request_id: str | None) -> int:
    return cmd_list_impl(
        workspace_root,
        request_id,
        runtime_paths_cls=RuntimePaths,
        load_team_runtime_config=load_team_runtime_config,
        read_json=read_json,
        iter_json_records=iter_json_records,
        role_service_status=role_service_status,
        all_runtime_agents=ALL_RUNTIME_AGENTS,
    )


def cmd_config_role_set(
    workspace_root: Path,
    role: str,
    *,
    model: str | None = None,
    reasoning: str | None = None,
) -> int:
    return cmd_config_role_set_impl(
        workspace_root,
        role,
        model=model,
        reasoning=reasoning,
        update_team_runtime_role_defaults=update_team_runtime_role_defaults,
        runtime_paths_cls=RuntimePaths,
    )


def cmd_config_research_set(
    workspace_root: Path,
    *,
    app: str | None = None,
    notebook: str | None = None,
    files: list[str] | None = None,
    mode: str | None = None,
    profile_path: str | None = None,
    completion_timeout: float | None = None,
    callback_timeout: float | None = None,
    cleanup: bool | None = None,
) -> int:
    return cmd_config_research_set_impl(
        workspace_root,
        app=app,
        notebook=notebook,
        files=files,
        mode=mode,
        profile_path=profile_path,
        completion_timeout=completion_timeout,
        callback_timeout=callback_timeout,
        cleanup=cleanup,
        update_team_runtime_research_defaults=update_team_runtime_research_defaults,
        runtime_paths_cls=RuntimePaths,
    )


def cmd_sprint_start(
    workspace_root: Path,
    milestone: str,
    *,
    brief: str = "",
    requirements: list[str] | None = None,
    artifacts: list[str] | None = None,
    source_request_id: str = "",
) -> int:
    return cmd_sprint_start_impl(
        workspace_root,
        milestone,
        brief=brief,
        requirements=requirements,
        artifacts=artifacts,
        source_request_id=source_request_id,
        team_service_cls=TeamService,
    )


def cmd_sprint_stop(workspace_root: Path) -> int:
    return cmd_sprint_stop_impl(
        workspace_root,
        team_service_cls=TeamService,
    )


def cmd_sprint_restart(workspace_root: Path) -> int:
    return cmd_sprint_restart_impl(
        workspace_root,
        team_service_cls=TeamService,
    )


def cmd_sprint_status(workspace_root: Path) -> int:
    return cmd_sprint_status_impl(
        workspace_root,
        status_command=cmd_status,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace_root = resolve_workspace_root(getattr(args, "workspace_root", None))
    return dispatch_main(
        args,
        workspace_root=workspace_root,
        parser=parser,
        run_services=run_services,
        cmd_init=cmd_init,
        cmd_start=cmd_start,
        cmd_status=cmd_status,
        cmd_stop=cmd_stop,
        cmd_restart=cmd_restart,
        cmd_list=cmd_list,
        cmd_config_role_set=cmd_config_role_set,
        cmd_config_research_set=cmd_config_research_set,
        cmd_sprint_start=cmd_sprint_start,
        cmd_sprint_stop=cmd_sprint_stop,
        cmd_sprint_restart=cmd_sprint_restart,
        cmd_sprint_status=cmd_sprint_status,
        default_relay_transport=DEFAULT_RELAY_TRANSPORT,
    )


if __name__ == "__main__":
    raise SystemExit(main())
