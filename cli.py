from __future__ import annotations

import argparse
import asyncio
import logging
import os
from collections.abc import Awaitable
from pathlib import Path

from teams_runtime.core.config import (
    load_team_runtime_config,
    update_team_runtime_role_defaults,
    validate_runtime_discord_agents_config,
)
from teams_runtime.core.orchestration import (
    RELAY_TRANSPORT_DISCORD,
    RELAY_TRANSPORT_INTERNAL,
    TeamService,
)
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.persistence import iter_json_records, read_json, utc_now_iso, write_json
from teams_runtime.core.sprints import build_sprint_artifact_folder_name
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.discord.lifecycle import (
    role_service_status,
    run_foreground_role_service,
    start_background_role_service,
    stop_background_role_service,
)
from teams_runtime.discord.client import DiscordClient, DiscordListenError, classify_discord_exception
from teams_runtime.models import ALL_RUNTIME_AGENTS, INTERNAL_TEAM_AGENTS, TEAM_ROLES
from teams_runtime.runtime.codex import RoleSessionManager


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
    relay_transport: str = DEFAULT_RELAY_TRANSPORT,
) -> TeamService | InternalAgentService:
    if role in INTERNAL_TEAM_AGENTS:
        return InternalAgentService(workspace_root, role)
    return TeamService(workspace_root, role, relay_transport=relay_transport)


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
    parser = argparse.ArgumentParser(description="Standalone multi-bot teams runtime.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Scaffold a portable workspace.")
    init_parser.add_argument(
        "--workspace-root",
        default=None,
        help=_workspace_root_help_text(),
    )

    for command in ("run", "start", "status", "stop", "restart"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument(
            "--workspace-root",
            default=None,
            help=_workspace_root_help_text(),
        )
        command_parser.add_argument("--agent", choices=ALL_RUNTIME_AGENTS, help="Optional single agent target.")
        if command in {"run", "start", "restart"}:
            command_parser.add_argument(
                "--relay-transport",
                choices=(RELAY_TRANSPORT_INTERNAL, RELAY_TRANSPORT_DISCORD),
                default=DEFAULT_RELAY_TRANSPORT,
                help="Relay transport between team roles. Defaults to internal for runtime operations.",
            )
        if command == "status":
            command_parser.add_argument("--request-id", help="Optional request identifier.")
            command_parser.add_argument("--sprint", action="store_true", help="Show active or latest sprint summary.")
            command_parser.add_argument("--backlog", action="store_true", help="Show backlog summary.")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument(
        "--workspace-root",
        default=None,
        help=_workspace_root_help_text(),
    )
    list_parser.add_argument("--request-id", help="Optional request identifier to print.")

    config_parser = subparsers.add_parser("config")
    config_subparsers = config_parser.add_subparsers(dest="config_command", required=True)
    role_parser = config_subparsers.add_parser("role")
    role_subparsers = role_parser.add_subparsers(dest="role_command", required=True)
    role_set_parser = role_subparsers.add_parser("set")
    role_set_parser.add_argument(
        "--workspace-root",
        default=None,
        help=_workspace_root_help_text(),
    )
    role_set_parser.add_argument("--agent", choices=TEAM_ROLES, required=True, help="Target team role.")
    role_set_parser.add_argument("--model", help="Optional model override to save in team_runtime.yaml.")
    role_set_parser.add_argument("--reasoning", help="Optional reasoning level to save in team_runtime.yaml.")

    sprint_parser = subparsers.add_parser("sprint")
    sprint_subparsers = sprint_parser.add_subparsers(dest="sprint_command", required=True)
    for sprint_command in ("start", "stop", "restart", "status"):
        sprint_command_parser = sprint_subparsers.add_parser(sprint_command)
        sprint_command_parser.add_argument(
            "--workspace-root",
            default=None,
            help=_workspace_root_help_text(),
        )
        if sprint_command == "start":
            sprint_command_parser.add_argument("--milestone", required=True, help="Sprint milestone title.")
            sprint_command_parser.add_argument("--brief", help="Optional preserved kickoff brief for the sprint.")
            sprint_command_parser.add_argument(
                "--requirement",
                action="append",
                default=[],
                help="Optional kickoff requirement. Repeat to add more than one.",
            )
            sprint_command_parser.add_argument(
                "--artifact",
                action="append",
                default=[],
                help="Optional kickoff reference artifact path. Repeat to add more than one.",
            )
            sprint_command_parser.add_argument(
                "--source-request-id",
                default="",
                help="Optional originating request_id for runtime-driven sprint starts.",
            )

    return parser


async def run_services(
    workspace_root: Path,
    role: str | None,
    *,
    relay_transport: str = DEFAULT_RELAY_TRANSPORT,
) -> None:
    paths = RuntimePaths.from_root(workspace_root)
    paths.ensure_runtime_dirs()
    if _requires_runtime_discord_validation(role):
        validate_runtime_discord_agents_config(paths.workspace_root)
    roles = [role] if role else list(ALL_RUNTIME_AGENTS)
    services = [
        build_agent_service(
            paths.workspace_root,
            item,
            relay_transport=relay_transport,
        )
        for item in roles
    ]
    await asyncio.gather(
        *[
            run_foreground_role_service(paths, service.role, service.run)
            for service in services
        ]
    )


def cmd_init(workspace_root: Path) -> int:
    created = scaffold_workspace(workspace_root)
    print(f"Scaffolded {len(created)} workspace files at {workspace_root}")
    return 0


def cmd_start(
    workspace_root: Path,
    role: str | None,
    *,
    relay_transport: str = DEFAULT_RELAY_TRANSPORT,
) -> int:
    paths = RuntimePaths.from_root(workspace_root)
    if _requires_runtime_discord_validation(role):
        validate_runtime_discord_agents_config(paths.workspace_root)
    roles = [role] if role else list(ALL_RUNTIME_AGENTS)
    for item in roles:
        build_agent_service(paths.workspace_root, item, relay_transport=relay_transport)
    for item in roles:
        pid = start_background_role_service(paths, item, relay_transport=relay_transport)
        print(f"Started {item} service in background (PID {pid}).")
    return 0


def _format_session_sprint_scope(session_state: dict[str, object]) -> str:
    sprint_id = str(session_state.get("sprint_id") or session_state.get("active_sprint_id") or "").strip()
    return f"sprint_id={sprint_id or 'N/A'}"


def _format_scheduler_sprint_scope(scheduler_state: dict[str, object]) -> str:
    active_sprint_id = str(scheduler_state.get("active_sprint_id") or "").strip()
    return f"active_sprint_id={active_sprint_id or 'N/A'}"


def _load_cli_sprint_status(paths: RuntimePaths) -> tuple[dict[str, object], bool, dict[str, object]]:
    scheduler = read_json(paths.sprint_scheduler_file)
    sprint_id = str(scheduler.get("active_sprint_id") or "").strip()
    sprint_record = read_json(paths.sprint_file(sprint_id)) if sprint_id else {}
    is_active = bool(sprint_record)
    if not sprint_record:
        sprint_files = sorted(paths.sprints_dir.glob("*.json"))
        if sprint_files:
            sprint_record = read_json(sprint_files[-1])
    return sprint_record, is_active, scheduler


def _render_cli_sprint_status(sprint_record: dict[str, object], *, is_active: bool, scheduler: dict[str, object]) -> str:
    lines = [
        "## Sprint Summary",
        f"- view: {'active' if is_active else 'latest'}",
        f"- sprint_id: {sprint_record.get('sprint_id') or ''}",
        f"- sprint_name: {sprint_record.get('sprint_name') or sprint_record.get('sprint_display_name') or 'N/A'}",
        f"- phase: {sprint_record.get('phase') or 'N/A'}",
        f"- milestone_title: {sprint_record.get('milestone_title') or 'N/A'}",
        f"- status: {sprint_record.get('status') or ''}",
        f"- trigger: {sprint_record.get('trigger') or ''}",
        f"- started_at: {sprint_record.get('started_at') or ''}",
        f"- ended_at: {sprint_record.get('ended_at') or 'N/A'}",
        f"- closeout_status: {sprint_record.get('closeout_status') or 'N/A'}",
        f"- next_slot_at: {scheduler.get('next_slot_at') or 'N/A'}",
    ]
    return "\n".join(lines)


def cmd_status(
    workspace_root: Path,
    role: str | None,
    request_id: str | None = None,
    *,
    sprint: bool = False,
    backlog: bool = False,
) -> int:
    paths = RuntimePaths.from_root(workspace_root)
    runtime_config = load_team_runtime_config(paths.workspace_root)
    if request_id:
        return cmd_list(workspace_root, request_id)
    if sprint:
        sprint_record, is_active, scheduler = _load_cli_sprint_status(paths)
        if not sprint_record:
            print("No sprint record found.")
            return 1
        print(_render_cli_sprint_status(sprint_record, is_active=is_active, scheduler=scheduler))
        return 0
    if backlog:
        items = list(iter_json_records(paths.backlog_dir))
        pending_count = sum(1 for item in items if str(item.get("status") or "") == "pending")
        selected_count = sum(1 for item in items if str(item.get("status") or "") == "selected")
        blocked_count = sum(1 for item in items if str(item.get("status") or "") == "blocked")
        done_count = sum(1 for item in items if str(item.get("status") or "") == "done")
        carried_count = sum(1 for item in items if str(item.get("status") or "") == "carried_over")
        print(
            f"backlog_pending={pending_count} backlog_selected={selected_count} "
            f"backlog_blocked={blocked_count} "
            f"backlog_done={done_count} backlog_carried_over={carried_count}"
        )
        return 0
    roles = [role] if role else list(ALL_RUNTIME_AGENTS)
    for item in roles:
        running, pid = role_service_status(paths, item)
        session_state = {}
        try:
            session_state = read_json(paths.session_state_file(item))
        except Exception:
            session_state = {}
        status = "running" if running else "stopped"
        reload_state = read_json(paths.agent_state_file(item))
        listener_status = str(reload_state.get("listener_status") or "").strip()
        listener_error = str(reload_state.get("listener_error_category") or "").strip()
        listener_summary = f" listener={listener_status or 'n/a'}"
        if listener_error and listener_status != "connected":
            listener_summary += f" listener_error={listener_error}"
        model_summary = _format_role_runtime_summary(runtime_config, item)
        print(
            f"{item}: status={status} pid={pid or 'N/A'} "
            f"{_format_session_sprint_scope(session_state)} "
            f"session={session_state.get('session_id') or 'N/A'} "
            f"{model_summary} {listener_summary}"
        )
    return 0


def cmd_stop(workspace_root: Path, role: str | None) -> int:
    paths = RuntimePaths.from_root(workspace_root)
    roles = [role] if role else list(ALL_RUNTIME_AGENTS)
    exit_code = 0
    for item in roles:
        stopped, message = stop_background_role_service(paths, item)
        print(message)
        if not stopped and "not running" not in message.lower() and "stale pid" not in message.lower():
            exit_code = 1
    return exit_code


def cmd_restart(
    workspace_root: Path,
    role: str | None,
    *,
    relay_transport: str = DEFAULT_RELAY_TRANSPORT,
) -> int:
    stop_code = cmd_stop(workspace_root, role)
    start_code = cmd_start(workspace_root, role, relay_transport=relay_transport)
    return 1 if stop_code or start_code else 0


def cmd_list(workspace_root: Path, request_id: str | None) -> int:
    paths = RuntimePaths.from_root(workspace_root)
    runtime_config = load_team_runtime_config(paths.workspace_root)
    if request_id:
        record = read_json(paths.request_file(request_id))
        if not record:
            print(f"request_id {request_id} not found.")
            return 1
        print(record)
        return 0

    for role in ALL_RUNTIME_AGENTS:
        running, pid = role_service_status(paths, role)
        reload_state = read_json(paths.agent_state_file(role))
        listener_status = str(reload_state.get("listener_status") or "").strip()
        listener_error = str(reload_state.get("listener_error_category") or "").strip()
        listener_summary = f" listener={listener_status or 'n/a'}"
        if listener_error and listener_status != "connected":
            listener_summary += f" listener_error={listener_error}"
        model_summary = _format_role_runtime_summary(runtime_config, role)
        print(
            f"{role}: {'running' if running else 'stopped'} pid={pid or 'N/A'} "
            f"{model_summary} {listener_summary}"
        )
    backlog_items = list(iter_json_records(paths.backlog_dir))
    pending_count = sum(1 for item in backlog_items if str(item.get("status") or "") == "pending")
    selected_count = sum(1 for item in backlog_items if str(item.get("status") or "") == "selected")
    blocked_count = sum(1 for item in backlog_items if str(item.get("status") or "") == "blocked")
    print(
        f"backlog_pending={pending_count} backlog_selected={selected_count} "
        f"backlog_blocked={blocked_count} backlog_total={pending_count + selected_count + blocked_count}"
    )
    scheduler = read_json(paths.sprint_scheduler_file)
    if scheduler:
        print(
            f"{_format_scheduler_sprint_scope(scheduler)} "
            f"next_slot_at={scheduler.get('next_slot_at') or 'N/A'}"
        )
    for record in iter_json_records(paths.requests_dir):
        print(
            f"request_id={record.get('request_id')} status={record.get('status')} "
            f"current_role={record.get('current_role')}"
        )
    return 0


def _format_role_runtime_summary(runtime_config, role: str) -> str:
    role_runtime = runtime_config.role_defaults.get(role)
    if role_runtime is None:
        return "model=N/A reasoning=N/A"
    model = str(role_runtime.model or "").strip() or "N/A"
    reasoning = "None" if "gemini" in model.lower() else str(role_runtime.reasoning or "").strip() or "medium"
    return f"model={model} reasoning={reasoning}"


def cmd_config_role_set(
    workspace_root: Path,
    role: str,
    *,
    model: str | None = None,
    reasoning: str | None = None,
) -> int:
    updated = update_team_runtime_role_defaults(
        workspace_root,
        role,
        model=model,
        reasoning=reasoning,
    )
    effective_reasoning = "None" if "gemini" in updated.model.lower() else updated.reasoning
    config_path = RuntimePaths.from_root(workspace_root).workspace_root / "team_runtime.yaml"
    print(f"Updated {config_path}")
    print(f"role={role} model={updated.model} reasoning={effective_reasoning}")
    print(f"Restart the role to apply changes: python -m teams_runtime restart --agent {role}")
    return 0


def _build_cli_kickoff_request_text(milestone: str, brief: str, requirements: list[str]) -> str:
    lines = ["start sprint", f"milestone: {str(milestone or '').strip()}"]
    normalized_brief = str(brief or "").strip()
    normalized_requirements = [str(item).strip() for item in (requirements or []) if str(item).strip()]
    if normalized_brief:
        lines.extend(["brief:", normalized_brief])
    if normalized_requirements:
        lines.append("requirements:")
        lines.extend(f"- {item}" for item in normalized_requirements)
    return "\n".join(lines).strip()


def cmd_sprint_start(
    workspace_root: Path,
    milestone: str,
    *,
    brief: str = "",
    requirements: list[str] | None = None,
    artifacts: list[str] | None = None,
    source_request_id: str = "",
) -> int:
    service = TeamService(workspace_root, "orchestrator", enable_discord_client=False)
    normalized_requirements = [str(item).strip() for item in (requirements or []) if str(item).strip()]
    normalized_artifacts = [str(item).strip() for item in (artifacts or []) if str(item).strip()]
    message = asyncio.run(
        service.start_sprint_lifecycle(
            milestone,
            trigger="manual_start",
            resume_mode="await",
            kickoff_brief=str(brief or "").strip(),
            kickoff_requirements=normalized_requirements,
            kickoff_request_text=_build_cli_kickoff_request_text(milestone, brief, normalized_requirements),
            kickoff_source_request_id=str(source_request_id or "").strip(),
            kickoff_reference_artifacts=normalized_artifacts,
        )
    )
    print(message)
    return 0


def cmd_sprint_stop(workspace_root: Path) -> int:
    service = TeamService(workspace_root, "orchestrator", enable_discord_client=False)
    message = asyncio.run(service.stop_sprint_lifecycle(resume_mode="await"))
    print(message)
    return 0


def cmd_sprint_restart(workspace_root: Path) -> int:
    service = TeamService(workspace_root, "orchestrator", enable_discord_client=False)
    message = asyncio.run(service.restart_sprint_lifecycle(resume_mode="await"))
    print(message)
    return 0


def cmd_sprint_status(workspace_root: Path) -> int:
    return cmd_status(workspace_root, None, sprint=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    workspace_root = resolve_workspace_root(getattr(args, "workspace_root", None))

    if args.command == "init":
        return cmd_init(workspace_root)
    if args.command == "run":
        asyncio.run(
            run_services(
                workspace_root,
                args.agent,
                relay_transport=str(getattr(args, "relay_transport", DEFAULT_RELAY_TRANSPORT)),
            )
        )
        return 0
    if args.command == "start":
        return cmd_start(
            workspace_root,
            args.agent,
            relay_transport=str(getattr(args, "relay_transport", DEFAULT_RELAY_TRANSPORT)),
        )
    if args.command == "status":
        return cmd_status(
            workspace_root,
            args.agent,
            getattr(args, "request_id", None),
            sprint=bool(getattr(args, "sprint", False)),
            backlog=bool(getattr(args, "backlog", False)),
        )
    if args.command == "stop":
        return cmd_stop(workspace_root, args.agent)
    if args.command == "restart":
        return cmd_restart(
            workspace_root,
            args.agent,
            relay_transport=str(getattr(args, "relay_transport", DEFAULT_RELAY_TRANSPORT)),
        )
    if args.command == "list":
        return cmd_list(workspace_root, args.request_id)
    if args.command == "config":
        if args.config_command == "role" and args.role_command == "set":
            return cmd_config_role_set(
                workspace_root,
                args.agent,
                model=getattr(args, "model", None),
                reasoning=getattr(args, "reasoning", None),
            )
    if args.command == "sprint":
        if args.sprint_command == "start":
            return cmd_sprint_start(
                workspace_root,
                args.milestone,
                brief=str(getattr(args, "brief", "") or ""),
                requirements=list(getattr(args, "requirement", []) or []),
                artifacts=list(getattr(args, "artifact", []) or []),
                source_request_id=str(getattr(args, "source_request_id", "") or ""),
            )
        if args.sprint_command == "stop":
            return cmd_sprint_stop(workspace_root)
        if args.sprint_command == "restart":
            return cmd_sprint_restart(workspace_root)
        if args.sprint_command == "status":
            return cmd_sprint_status(workspace_root)
    parser.error(f"Unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
