from __future__ import annotations

import argparse
import asyncio
import os
import shutil
from pathlib import Path
from typing import Any, Callable


DispatchRunCallback = Callable[[Path, str | None], object]
DispatchSyncCallback = Callable[..., int]
Printer = Callable[[object], object]


def _notify_missing_gh(printer: Printer) -> None:
    if shutil.which("gh") is not None:
        return
    printer(
        "GitHub CLI `gh` is not installed. Sprint GitHub issue publishing will be skipped until you install gh "
        "and authenticate with `gh auth login` or GH_TOKEN/GITHUB_TOKEN."
    )


def _notify_missing_github_token(printer: Printer) -> None:
    if os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"):
        return
    printer(
        "GitHub token missing. Run `gh auth login` or set GH_TOKEN/GITHUB_TOKEN before sprint GitHub issue publishing."
    )


def build_parser(
    *,
    all_runtime_agents: list[str] | tuple[str, ...],
    team_roles: list[str] | tuple[str, ...],
    relay_transport_internal: str,
    relay_transport_discord: str,
    default_relay_transport: str,
    workspace_root_help_text: str,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Standalone multi-bot teams runtime.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create or refresh a portable workspace.")
    init_parser.add_argument(
        "--workspace-root",
        default=None,
        help=workspace_root_help_text,
    )
    init_mode_group = init_parser.add_mutually_exclusive_group()
    init_mode_group.add_argument(
        "--refresh-prompts",
        action="store_true",
        help=(
            "Compatibility alias for the default existing-workspace behavior: refresh copied role prompts "
            "and runtime skill assets without resetting .teams_runtime/ or shared_workspace/."
        ),
    )
    init_mode_group.add_argument(
        "--reset",
        action="store_true",
        help=(
            "Rebuild generated workspace files from scratch. Preserves discord_agents_config.yaml and "
            "archived sprint history, but resets runtime state and shared workspace files."
        ),
    )

    for command in ("run", "start", "status", "stop", "restart"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument(
            "--workspace-root",
            default=None,
            help=workspace_root_help_text,
        )
        command_parser.add_argument("--agent", choices=all_runtime_agents, help="Optional single agent target.")
        if command in {"run", "start", "restart"}:
            command_parser.add_argument(
                "--relay-transport",
                choices=(relay_transport_internal, relay_transport_discord),
                default=default_relay_transport,
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
        help=workspace_root_help_text,
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
        help=workspace_root_help_text,
    )
    role_set_parser.add_argument("--agent", choices=team_roles, required=True, help="Target team role.")
    role_set_parser.add_argument("--model", help="Optional model override to save in team_runtime.yaml.")
    role_set_parser.add_argument("--reasoning", help="Optional reasoning level to save in team_runtime.yaml.")

    research_parser = config_subparsers.add_parser("research")
    research_subparsers = research_parser.add_subparsers(dest="research_command", required=True)
    research_set_parser = research_subparsers.add_parser("set")
    research_set_parser.add_argument(
        "--workspace-root",
        default=None,
        help=workspace_root_help_text,
    )
    research_set_parser.add_argument("--app", help="Optional Gemini app override for deep research.")
    research_set_parser.add_argument("--notebook", help="Optional NotebookLM source keyword.")
    research_set_parser.add_argument(
        "--file",
        action="append",
        default=None,
        help="Optional Drive file keyword to include. Repeat to add more than one.",
    )
    research_set_parser.add_argument("--mode", help="Optional Gemini chat mode override.")
    research_set_parser.add_argument("--profile-path", help="Optional local browser profile path.")
    research_set_parser.add_argument("--completion-timeout", type=float, help="Deep research completion timeout in seconds.")
    research_set_parser.add_argument("--callback-timeout", type=float, help="Deep research callback timeout in seconds.")
    research_cleanup_group = research_set_parser.add_mutually_exclusive_group()
    research_cleanup_group.add_argument(
        "--cleanup",
        dest="cleanup",
        action="store_true",
        help="Clean up the browser session after deep research completes.",
    )
    research_cleanup_group.add_argument(
        "--no-cleanup",
        dest="cleanup",
        action="store_false",
        help="Keep the browser session artifacts after deep research completes.",
    )
    research_set_parser.set_defaults(cleanup=None)

    sprint_parser = subparsers.add_parser("sprint")
    sprint_subparsers = sprint_parser.add_subparsers(dest="sprint_command", required=True)
    for sprint_command in ("start", "stop", "restart", "status"):
        sprint_command_parser = sprint_subparsers.add_parser(sprint_command)
        sprint_command_parser.add_argument(
            "--workspace-root",
            default=None,
            help=workspace_root_help_text,
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


def dispatch_main(
    args: argparse.Namespace,
    *,
    workspace_root: Path,
    parser: argparse.ArgumentParser,
    run_services: Callable[..., object],
    cmd_init: DispatchSyncCallback,
    cmd_start: DispatchSyncCallback,
    cmd_status: DispatchSyncCallback,
    cmd_stop: DispatchSyncCallback,
    cmd_restart: DispatchSyncCallback,
    cmd_list: DispatchSyncCallback,
    cmd_config_role_set: DispatchSyncCallback,
    cmd_config_research_set: DispatchSyncCallback,
    cmd_sprint_start: DispatchSyncCallback,
    cmd_sprint_stop: DispatchSyncCallback,
    cmd_sprint_restart: DispatchSyncCallback,
    cmd_sprint_status: DispatchSyncCallback,
    default_relay_transport: str,
) -> int:
    if args.command == "init":
        return cmd_init(
            workspace_root,
            refresh_prompts=bool(getattr(args, "refresh_prompts", False)),
            reset=bool(getattr(args, "reset", False)),
        )
    if args.command == "run":
        asyncio.run(
            run_services(
                workspace_root,
                args.agent,
                relay_transport=str(getattr(args, "relay_transport", default_relay_transport)),
            )
        )
        return 0
    if args.command == "start":
        return cmd_start(
            workspace_root,
            args.agent,
            relay_transport=str(getattr(args, "relay_transport", default_relay_transport)),
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
            relay_transport=str(getattr(args, "relay_transport", default_relay_transport)),
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
        if args.config_command == "research" and args.research_command == "set":
            return cmd_config_research_set(
                workspace_root,
                app=getattr(args, "app", None),
                notebook=getattr(args, "notebook", None),
                files=getattr(args, "file", None),
                mode=getattr(args, "mode", None),
                profile_path=getattr(args, "profile_path", None),
                completion_timeout=getattr(args, "completion_timeout", None),
                callback_timeout=getattr(args, "callback_timeout", None),
                cleanup=getattr(args, "cleanup", None),
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


async def run_services_impl(
    workspace_root: Path,
    role: str | None,
    *,
    relay_transport: str,
    runtime_paths_cls: Any,
    validate_runtime_discord_agents_config: Callable[[Path], object],
    requires_runtime_discord_validation: Callable[[str | None], bool],
    all_runtime_agents: list[str] | tuple[str, ...],
    build_agent_service: Callable[..., Any],
    run_foreground_role_service: Callable[[Any, str, Callable[[], Any]], Any],
) -> None:
    paths = runtime_paths_cls.from_root(workspace_root)
    paths.ensure_runtime_dirs()
    if requires_runtime_discord_validation(role):
        validate_runtime_discord_agents_config(paths.workspace_root)
    roles = [role] if role else list(all_runtime_agents)
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


def cmd_init_impl(
    workspace_root: Path,
    *,
    scaffold_workspace: Callable[[Path], list[Path]],
    refresh_workspace_prompts: Callable[[Path], list[Path]] | None = None,
    load_github_token_env: Callable[[Path], object] | None = None,
    refresh_prompts: bool = False,
    reset: bool = False,
    printer: Printer = print,
) -> int:
    looks_like_workspace = (
        (workspace_root / "team_runtime.yaml").exists()
        or (workspace_root / "discord_agents_config.yaml").exists()
    )
    if refresh_prompts or (looks_like_workspace and not reset):
        if refresh_workspace_prompts is None:
            raise ValueError("refresh_workspace_prompts callback is required when refreshing workspace prompts")
        updated = refresh_workspace_prompts(workspace_root)
        printer(f"Refreshed {len(updated)} workspace prompt files at {workspace_root}")
        if load_github_token_env is not None:
            load_github_token_env(workspace_root)
        _notify_missing_gh(printer)
        _notify_missing_github_token(printer)
        return 0
    created = scaffold_workspace(workspace_root)
    printer(f"Scaffolded {len(created)} workspace files at {workspace_root}")
    if load_github_token_env is not None:
        load_github_token_env(workspace_root)
    _notify_missing_gh(printer)
    _notify_missing_github_token(printer)
    return 0


def cmd_start_impl(
    workspace_root: Path,
    role: str | None,
    *,
    relay_transport: str,
    runtime_paths_cls: Any,
    requires_runtime_discord_validation: Callable[[str | None], bool],
    validate_runtime_discord_agents_config: Callable[[Path], object],
    all_runtime_agents: list[str] | tuple[str, ...],
    build_agent_service: Callable[..., Any],
    start_background_role_service: Callable[..., int],
    printer: Printer = print,
) -> int:
    paths = runtime_paths_cls.from_root(workspace_root)
    if requires_runtime_discord_validation(role):
        validate_runtime_discord_agents_config(paths.workspace_root)
    roles = [role] if role else list(all_runtime_agents)
    for item in roles:
        build_agent_service(
            paths.workspace_root,
            item,
            enable_discord_client=False,
            relay_transport=relay_transport,
        )
    for item in roles:
        pid = start_background_role_service(paths, item, relay_transport=relay_transport)
        printer(f"Started {item} service in background (PID {pid}).")
    return 0


def _format_session_sprint_scope(session_state: dict[str, object]) -> str:
    sprint_id = str(session_state.get("sprint_id") or session_state.get("active_sprint_id") or "").strip()
    return f"sprint_id={sprint_id or 'N/A'}"


def _format_scheduler_sprint_scope(scheduler_state: dict[str, object]) -> str:
    active_sprint_id = str(scheduler_state.get("active_sprint_id") or "").strip()
    return f"active_sprint_id={active_sprint_id or 'N/A'}"


def _load_cli_sprint_status(
    paths: Any,
    *,
    read_json: Callable[[Path], dict[str, object]],
) -> tuple[dict[str, object], bool, dict[str, object]]:
    scheduler = read_json(paths.sprint_scheduler_file)
    sprint_id = str(scheduler.get("active_sprint_id") or "").strip()
    sprint_record = read_json(paths.sprint_file(sprint_id)) if sprint_id else {}
    is_active = bool(sprint_record)
    if not sprint_record:
        sprint_files = sorted(paths.sprints_dir.glob("*.json"))
        if sprint_files:
            sprint_record = read_json(sprint_files[-1])
    return sprint_record, is_active, scheduler


def _render_cli_sprint_status(
    sprint_record: dict[str, object],
    *,
    is_active: bool,
    scheduler: dict[str, object],
) -> str:
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


def _format_role_runtime_summary(runtime_config: Any, role: str) -> str:
    if role == "research":
        research_runtime = runtime_config.research_defaults
        app = str(research_runtime.app or "").strip() or "default"
        mode = str(research_runtime.mode or "").strip() or "default"
        return (
            "engine=deep_research "
            f"app={app} mode={mode} "
            f"completion_timeout={int(research_runtime.completion_timeout)} "
            f"callback_timeout={int(research_runtime.callback_timeout)}"
        )
    role_runtime = runtime_config.role_defaults.get(role)
    if role_runtime is None:
        return "model=N/A reasoning=N/A"
    model = str(role_runtime.model or "").strip() or "N/A"
    reasoning = "None" if "gemini" in model.lower() else str(role_runtime.reasoning or "").strip() or "medium"
    return f"model={model} reasoning={reasoning}"


def cmd_status_impl(
    workspace_root: Path,
    role: str | None,
    request_id: str | None = None,
    *,
    sprint: bool = False,
    backlog: bool = False,
    runtime_paths_cls: Any,
    load_team_runtime_config: Callable[[Path], Any],
    read_json: Callable[[Path], dict[str, object]],
    iter_json_records: Callable[[Path], list[dict[str, object]]],
    role_service_status: Callable[[Any, str], tuple[bool, int | None]],
    all_runtime_agents: list[str] | tuple[str, ...],
    list_command: Callable[[Path, str | None], int],
    printer: Printer = print,
) -> int:
    paths = runtime_paths_cls.from_root(workspace_root)
    runtime_config = load_team_runtime_config(paths.workspace_root)
    if request_id:
        return list_command(workspace_root, request_id)
    if sprint:
        sprint_record, is_active, scheduler = _load_cli_sprint_status(paths, read_json=read_json)
        if not sprint_record:
            printer("No sprint record found.")
            return 1
        printer(_render_cli_sprint_status(sprint_record, is_active=is_active, scheduler=scheduler))
        return 0
    if backlog:
        items = list(iter_json_records(paths.backlog_dir))
        pending_count = sum(1 for item in items if str(item.get("status") or "") == "pending")
        selected_count = sum(1 for item in items if str(item.get("status") or "") == "selected")
        blocked_count = sum(1 for item in items if str(item.get("status") or "") == "blocked")
        done_count = sum(1 for item in items if str(item.get("status") or "") == "done")
        carried_count = sum(1 for item in items if str(item.get("status") or "") == "carried_over")
        printer(
            f"backlog_pending={pending_count} backlog_selected={selected_count} "
            f"backlog_blocked={blocked_count} "
            f"backlog_done={done_count} backlog_carried_over={carried_count}"
        )
        return 0
    roles = [role] if role else list(all_runtime_agents)
    for item in roles:
        running, pid = role_service_status(paths, item)
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
        printer(
            f"{item}: status={status} pid={pid or 'N/A'} "
            f"{_format_session_sprint_scope(session_state)} "
            f"session={session_state.get('session_id') or 'N/A'} "
            f"{model_summary} {listener_summary}"
        )
    return 0


def cmd_stop_impl(
    workspace_root: Path,
    role: str | None,
    *,
    runtime_paths_cls: Any,
    all_runtime_agents: list[str] | tuple[str, ...],
    stop_background_role_service: Callable[[Any, str], tuple[bool, str]],
    printer: Printer = print,
) -> int:
    paths = runtime_paths_cls.from_root(workspace_root)
    roles = [role] if role else list(all_runtime_agents)
    exit_code = 0
    for item in roles:
        stopped, message = stop_background_role_service(paths, item)
        printer(message)
        if not stopped and "not running" not in message.lower() and "stale pid" not in message.lower():
            exit_code = 1
    return exit_code


def cmd_restart_impl(
    workspace_root: Path,
    role: str | None,
    *,
    relay_transport: str,
    stop_command: Callable[[Path, str | None], int],
    start_command: Callable[..., int],
) -> int:
    stop_code = stop_command(workspace_root, role)
    start_code = start_command(workspace_root, role, relay_transport=relay_transport)
    return 1 if stop_code or start_code else 0


def cmd_list_impl(
    workspace_root: Path,
    request_id: str | None,
    *,
    runtime_paths_cls: Any,
    load_team_runtime_config: Callable[[Path], Any],
    read_json: Callable[[Path], dict[str, object]],
    iter_json_records: Callable[[Path], list[dict[str, object]]],
    role_service_status: Callable[[Any, str], tuple[bool, int | None]],
    all_runtime_agents: list[str] | tuple[str, ...],
    printer: Printer = print,
) -> int:
    paths = runtime_paths_cls.from_root(workspace_root)
    runtime_config = load_team_runtime_config(paths.workspace_root)
    if request_id:
        record = read_json(paths.request_file(request_id))
        if not record:
            printer(f"request_id {request_id} not found.")
            return 1
        printer(record)
        return 0

    for role_name in all_runtime_agents:
        running, pid = role_service_status(paths, role_name)
        reload_state = read_json(paths.agent_state_file(role_name))
        listener_status = str(reload_state.get("listener_status") or "").strip()
        listener_error = str(reload_state.get("listener_error_category") or "").strip()
        listener_summary = f" listener={listener_status or 'n/a'}"
        if listener_error and listener_status != "connected":
            listener_summary += f" listener_error={listener_error}"
        model_summary = _format_role_runtime_summary(runtime_config, role_name)
        printer(
            f"{role_name}: {'running' if running else 'stopped'} pid={pid or 'N/A'} "
            f"{model_summary} {listener_summary}"
        )
    backlog_items = list(iter_json_records(paths.backlog_dir))
    pending_count = sum(1 for item in backlog_items if str(item.get("status") or "") == "pending")
    selected_count = sum(1 for item in backlog_items if str(item.get("status") or "") == "selected")
    blocked_count = sum(1 for item in backlog_items if str(item.get("status") or "") == "blocked")
    printer(
        f"backlog_pending={pending_count} backlog_selected={selected_count} "
        f"backlog_blocked={blocked_count} backlog_total={pending_count + selected_count + blocked_count}"
    )
    scheduler = read_json(paths.sprint_scheduler_file)
    if scheduler:
        printer(
            f"{_format_scheduler_sprint_scope(scheduler)} "
            f"next_slot_at={scheduler.get('next_slot_at') or 'N/A'}"
        )
    for record in iter_json_records(paths.requests_dir):
        printer(
            f"request_id={record.get('request_id')} status={record.get('status')} "
            f"current_role={record.get('current_role')}"
        )
    return 0


def cmd_config_role_set_impl(
    workspace_root: Path,
    role: str,
    *,
    model: str | None = None,
    reasoning: str | None = None,
    update_team_runtime_role_defaults: Callable[..., Any],
    runtime_paths_cls: Any,
    printer: Printer = print,
) -> int:
    updated = update_team_runtime_role_defaults(
        workspace_root,
        role,
        model=model,
        reasoning=reasoning,
    )
    effective_reasoning = "None" if "gemini" in updated.model.lower() else updated.reasoning
    config_path = runtime_paths_cls.from_root(workspace_root).workspace_root / "team_runtime.yaml"
    printer(f"Updated {config_path}")
    printer(f"role={role} model={updated.model} reasoning={effective_reasoning}")
    printer(f"Restart the role to apply changes: python -m teams_runtime restart --agent {role}")
    return 0


def cmd_config_research_set_impl(
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
    update_team_runtime_research_defaults: Callable[..., Any],
    runtime_paths_cls: Any,
    printer: Printer = print,
) -> int:
    updated = update_team_runtime_research_defaults(
        workspace_root,
        app=app,
        notebook=notebook,
        files=files,
        mode=mode,
        profile_path=profile_path,
        completion_timeout=completion_timeout,
        callback_timeout=callback_timeout,
        cleanup=cleanup,
    )
    config_path = runtime_paths_cls.from_root(workspace_root).workspace_root / "team_runtime.yaml"
    files_summary = ", ".join(updated.files) if updated.files else "[]"
    printer(f"Updated {config_path}")
    printer(
        "research "
        f"app={updated.app or 'default'} "
        f"notebook={updated.notebook or 'default'} "
        f"files={files_summary} "
        f"mode={updated.mode or 'default'} "
        f"profile_path={updated.profile_path or 'default'} "
        f"completion_timeout={updated.completion_timeout:g} "
        f"callback_timeout={updated.callback_timeout:g} "
        f"cleanup={str(updated.cleanup).lower()}"
    )
    printer("Restart the research role to apply changes: python -m teams_runtime restart --agent research")
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


def cmd_sprint_start_impl(
    workspace_root: Path,
    milestone: str,
    *,
    brief: str = "",
    requirements: list[str] | None = None,
    artifacts: list[str] | None = None,
    source_request_id: str = "",
    team_service_cls: Any,
    printer: Printer = print,
) -> int:
    service = team_service_cls(workspace_root, "orchestrator", enable_discord_client=False)
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
    printer(message)
    return 0


def cmd_sprint_stop_impl(
    workspace_root: Path,
    *,
    team_service_cls: Any,
    printer: Printer = print,
) -> int:
    service = team_service_cls(workspace_root, "orchestrator", enable_discord_client=False)
    message = asyncio.run(service.stop_sprint_lifecycle(resume_mode="await"))
    printer(message)
    return 0


def cmd_sprint_restart_impl(
    workspace_root: Path,
    *,
    team_service_cls: Any,
    printer: Printer = print,
) -> int:
    service = team_service_cls(workspace_root, "orchestrator", enable_discord_client=False)
    message = asyncio.run(service.restart_sprint_lifecycle(resume_mode="await"))
    printer(message)
    return 0


def cmd_sprint_status_impl(
    workspace_root: Path,
    *,
    status_command: Callable[..., int],
) -> int:
    return status_command(workspace_root, None, sprint=True)


__all__ = [
    "build_parser",
    "cmd_config_research_set_impl",
    "cmd_config_role_set_impl",
    "cmd_init_impl",
    "cmd_list_impl",
    "cmd_restart_impl",
    "cmd_sprint_restart_impl",
    "cmd_sprint_start_impl",
    "cmd_sprint_status_impl",
    "cmd_sprint_stop_impl",
    "cmd_start_impl",
    "cmd_status_impl",
    "cmd_stop_impl",
    "dispatch_main",
    "run_services_impl",
]
