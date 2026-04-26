from __future__ import annotations

from pathlib import Path
import shutil
import tempfile

from teams_runtime.workflows.roles import (
    DEFAULT_AGENT_UTILIZATION_POLICY,
    internal_agent_descriptions,
    render_agent_utilization_policy_yaml,
    role_descriptions,
)
from teams_runtime.workflows.sprints.reporting import (
    load_sprint_history_index,
    render_sprint_history_index_rows,
)
from teams_runtime.shared.models import TEAM_ROLES


ROLE_DESCRIPTIONS = role_descriptions(DEFAULT_AGENT_UTILIZATION_POLICY)
INTERNAL_AGENT_DESCRIPTIONS = internal_agent_descriptions(DEFAULT_AGENT_UTILIZATION_POLICY)


def _runtime_templates_root() -> Path:
    return Path(__file__).resolve().parent.parent / "templates"


def _load_template_asset(relative_path: str) -> str:
    asset_path = _runtime_templates_root() / relative_path
    return asset_path.read_text(encoding="utf-8")


def _render_orchestrator_capability_reference() -> str:
    lines = ["## Agent Capability Reference"]
    for agent_name in ("research", "planner", "designer", "architect", "developer", "qa", "parser", "sourcer", "version_controller"):
        capability = (
            DEFAULT_AGENT_UTILIZATION_POLICY.public_capabilities.get(agent_name)
            or DEFAULT_AGENT_UTILIZATION_POLICY.internal_capabilities.get(agent_name)
        )
        if capability is None:
            continue
        strongest = ", ".join(capability.strongest_for[:2]) or capability.summary
        skills = ", ".join(capability.preferred_skills) if capability.preferred_skills else "N/A"
        traits = ", ".join(capability.behavior_traits[:3]) if capability.behavior_traits else "N/A"
        lines.append(
            f"- `{agent_name}`: {capability.summary}; strongest_for={strongest}; preferred_skills={skills}; behavior={traits}"
        )
    return "\n".join(lines)


ORCHESTRATOR_CAPABILITY_REFERENCE = _render_orchestrator_capability_reference()
ORCHESTRATOR_AGENT_UTILIZATION_POLICY_YAML = render_agent_utilization_policy_yaml()

def _load_role_prompt(role: str) -> str:
    return _load_template_asset(f"prompts/{role}.md")


def _load_internal_agent_prompt(agent_name: str) -> str:
    if agent_name == "version_controller":
        return _load_template_asset("prompts/version_controller.md")
    return _load_template_asset(f"prompts/internal/{agent_name}.md")


ORCHESTRATOR_SPRINT_ORCHESTRATION_SKILL = _load_template_asset(
    "scaffold/orchestrator/.agents/skills/sprint_orchestration/SKILL.md"
)
ORCHESTRATOR_AGENT_UTILIZATION_SKILL = _load_template_asset(
    "scaffold/orchestrator/.agents/skills/agent_utilization/SKILL.md"
).replace("{ORCHESTRATOR_CAPABILITY_REFERENCE}", ORCHESTRATOR_CAPABILITY_REFERENCE)
ORCHESTRATOR_HANDOFF_MERGING_SKILL = _load_template_asset(
    "scaffold/orchestrator/.agents/skills/handoff_merging/SKILL.md"
)
ORCHESTRATOR_STATUS_REPORTING_SKILL = _load_template_asset(
    "scaffold/orchestrator/.agents/skills/status_reporting/SKILL.md"
)
ORCHESTRATOR_SPRINT_CLOSEOUT_SKILL = _load_template_asset(
    "scaffold/orchestrator/.agents/skills/sprint_closeout/SKILL.md"
)
PLANNER_DOCUMENTATION_SKILL = _load_template_asset(
    "scaffold/planner/.agents/skills/documentation/SKILL.md"
)
PLANNER_BACKLOG_MANAGEMENT_SKILL = _load_template_asset(
    "scaffold/planner/.agents/skills/backlog_management/SKILL.md"
)
PLANNER_BACKLOG_DECOMPOSITION_SKILL = _load_template_asset(
    "scaffold/planner/.agents/skills/backlog_decomposition/SKILL.md"
)
PLANNER_SPRINT_PLANNING_SKILL = _load_template_asset(
    "scaffold/planner/.agents/skills/sprint_planning/SKILL.md"
)
VERSION_CONTROLLER_SKILL = _load_template_asset(
    "scaffold/internal/version_controller/.agents/skills/version_controller/SKILL.md"
)
TEAMS_RUNTIME_OPERATOR_SKILL = _load_template_asset("scaffold/.agents/skills/teams-runtime/SKILL.md")
TEAMS_RUNTIME_OPERATOR_OPENAI_YAML = _load_template_asset(
    "scaffold/.agents/skills/teams-runtime/agents/openai.yaml"
)
TEAMS_RUNTIME_OPERATOR_SNAPSHOT_SCRIPT = _load_template_asset(
    "scaffold/.agents/skills/teams-runtime/scripts/collect_runtime_snapshot.py"
)
TEAMS_COMMIT_POLICY = _load_template_asset("scaffold/COMMIT_POLICY.md")


INIT_REUSABLE_FILES = {
    "discord_agents_config.yaml",
    "COMMIT_POLICY.md",
}

INIT_PRESERVED_PATHS = {
    "shared_workspace/sprint_history",
}

INIT_PRESERVED_SKIP_RESTORE_FILES = {
    "shared_workspace/sprint_history/index.md",
}

INIT_RESET_PATHS = {
    "README.md",
    "team_runtime.yaml",
    "communication_protocol.md",
    "file_contracts.md",
    ".agents",
    "internal",
    "logs",
    "shared_workspace",
    ".teams_runtime",
    *TEAM_ROLES,
}


def _workspace_manifest(
    agent_name: str,
    description: str,
    *,
    write_shared_planning: bool,
    write_shared_decisions: bool,
) -> str:
    return """{
  "agent": "%s",
  "description": "%s",
  "permissions": {
    "write_private": true,
    "write_shared_planning": %s,
    "write_shared_decisions": %s,
    "write_shared_history": true
  },
  "artifacts": {
    "todo": "todo.md",
    "history": "history.md",
    "journal": "journal.md",
    "sources": "sources/"
  }
}
""" % (
        agent_name,
        description,
        "true" if write_shared_planning else "false",
        "true" if write_shared_decisions else "false",
    )


def build_default_workspace_files() -> dict[str, str]:
    files: dict[str, str] = {
        "README.md": _load_template_asset("scaffold/README.md"),
        "discord_agents_config.yaml": _load_template_asset("scaffold/discord_agents_config.yaml"),
        "team_runtime.yaml": _load_template_asset("scaffold/team_runtime.yaml"),
        "communication_protocol.md": _load_template_asset("scaffold/communication_protocol.md"),
        "file_contracts.md": _load_template_asset("scaffold/file_contracts.md"),
        "shared_workspace/README.md": _load_template_asset("scaffold/shared_workspace/README.md"),
        "shared_workspace/project_schedule.md": _load_template_asset("scaffold/shared_workspace/project_schedule.md"),
        "shared_workspace/backlog.md": _load_template_asset("scaffold/shared_workspace/backlog.md"),
        "shared_workspace/completed_backlog.md": _load_template_asset("scaffold/shared_workspace/completed_backlog.md"),
        "shared_workspace/current_sprint.md": _load_template_asset("scaffold/shared_workspace/current_sprint.md"),
        "shared_workspace/sprints/README.md": _load_template_asset("scaffold/shared_workspace/sprints/README.md"),
        "shared_workspace/sprint_history/index.md": _load_template_asset("scaffold/shared_workspace/sprint_history/index.md"),
        "shared_workspace/planning.md": _load_template_asset("scaffold/shared_workspace/planning.md"),
        "shared_workspace/decision_log.md": _load_template_asset("scaffold/shared_workspace/decision_log.md"),
        "shared_workspace/shared_history.md": _load_template_asset("scaffold/shared_workspace/shared_history.md"),
        "shared_workspace/sync_contract.md": _load_template_asset("scaffold/shared_workspace/sync_contract.md"),
        "COMMIT_POLICY.md": TEAMS_COMMIT_POLICY,
        ".agents/skills/teams-runtime/SKILL.md": TEAMS_RUNTIME_OPERATOR_SKILL,
        ".agents/skills/teams-runtime/agents/openai.yaml": TEAMS_RUNTIME_OPERATOR_OPENAI_YAML,
        ".agents/skills/teams-runtime/scripts/collect_runtime_snapshot.py": TEAMS_RUNTIME_OPERATOR_SNAPSHOT_SCRIPT,
    }
    for role in TEAM_ROLES:
        write_shared_planning = role in {"orchestrator", "research", "planner", "designer", "architect"}
        write_shared_decisions = role in {"orchestrator", "architect"}
        role_prompt = _load_role_prompt(role)
        files[f"{role}/AGENTS.md"] = role_prompt
        files[f"{role}/GEMINI.md"] = role_prompt
        files[f"{role}/todo.md"] = f"# {role.title()} Todo\n"
        files[f"{role}/history.md"] = f"# {role.title()} History\n"
        files[f"{role}/journal.md"] = f"# {role.title()} Journal\n"
        files[f"{role}/sources/README.md"] = f"# {role.title()} Sources\n"
        files[f"{role}/workspace_manifest.json"] = _workspace_manifest(
            role,
            ROLE_DESCRIPTIONS[role],
            write_shared_planning=write_shared_planning,
            write_shared_decisions=write_shared_decisions,
        )
    files["orchestrator/.agents/skills/sprint_orchestration/SKILL.md"] = ORCHESTRATOR_SPRINT_ORCHESTRATION_SKILL
    files["orchestrator/.agents/skills/agent_utilization/SKILL.md"] = ORCHESTRATOR_AGENT_UTILIZATION_SKILL
    files["orchestrator/.agents/skills/agent_utilization/policy.yaml"] = ORCHESTRATOR_AGENT_UTILIZATION_POLICY_YAML
    files["orchestrator/.agents/skills/handoff_merging/SKILL.md"] = ORCHESTRATOR_HANDOFF_MERGING_SKILL
    files["orchestrator/.agents/skills/status_reporting/SKILL.md"] = ORCHESTRATOR_STATUS_REPORTING_SKILL
    files["orchestrator/.agents/skills/sprint_closeout/SKILL.md"] = ORCHESTRATOR_SPRINT_CLOSEOUT_SKILL
    files["planner/.agents/skills/documentation/SKILL.md"] = PLANNER_DOCUMENTATION_SKILL
    files["planner/.agents/skills/backlog_management/SKILL.md"] = PLANNER_BACKLOG_MANAGEMENT_SKILL
    files["planner/.agents/skills/backlog_decomposition/SKILL.md"] = PLANNER_BACKLOG_DECOMPOSITION_SKILL
    files["planner/.agents/skills/sprint_planning/SKILL.md"] = PLANNER_SPRINT_PLANNING_SKILL
    for agent_name in ("parser", "sourcer", "version_controller"):
        internal_prompt = _load_internal_agent_prompt(agent_name)
        files[f"internal/{agent_name}/AGENTS.md"] = internal_prompt
        files[f"internal/{agent_name}/GEMINI.md"] = internal_prompt
        files[f"internal/{agent_name}/todo.md"] = f"# {agent_name.title()} Todo\n"
        files[f"internal/{agent_name}/history.md"] = f"# {agent_name.title()} History\n"
        files[f"internal/{agent_name}/journal.md"] = f"# {agent_name.title()} Journal\n"
        files[f"internal/{agent_name}/sources/README.md"] = f"# {agent_name.title()} Sources\n"
        files[f"internal/{agent_name}/workspace_manifest.json"] = _workspace_manifest(
            agent_name,
            INTERNAL_AGENT_DESCRIPTIONS[agent_name],
            write_shared_planning=False,
            write_shared_decisions=False,
        )
    files["internal/version_controller/.agents/skills/version_controller/SKILL.md"] = VERSION_CONTROLLER_SKILL
    return files


def _is_prompt_refresh_asset(relative_path: str) -> bool:
    normalized = str(relative_path or "").replace("\\", "/").strip("/")
    if not normalized:
        return False
    if normalized.startswith(".agents/skills/") or "/.agents/skills/" in normalized:
        return True
    if normalized.endswith("/AGENTS.md") or normalized.endswith("/GEMINI.md"):
        top_level = normalized.split("/", 1)[0]
        if top_level in TEAM_ROLES:
            return True
        return normalized.startswith("internal/")
    return False


def build_prompt_refresh_files() -> dict[str, str]:
    return {
        relative_path: content
        for relative_path, content in build_default_workspace_files().items()
        if _is_prompt_refresh_asset(relative_path)
    }


def refresh_workspace_prompt_assets(workspace_root: str | Path) -> list[Path]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    if not workspace_path.exists():
        raise FileNotFoundError(f"Workspace does not exist: {workspace_path}")
    if not (
        (workspace_path / "team_runtime.yaml").exists()
        or (workspace_path / "discord_agents_config.yaml").exists()
    ):
        raise FileNotFoundError(
            f"Refusing to refresh prompts because {workspace_path} does not look like a teams_runtime workspace."
        )
    updated: list[Path] = []
    for relative_path, content in build_prompt_refresh_files().items():
        target = workspace_path / relative_path
        if target.exists() and target.is_dir():
            raise IsADirectoryError(f"Refusing to overwrite directory with prompt asset: {target}")
        if target.exists() and not target.is_symlink():
            try:
                if target.read_text(encoding="utf-8") == content:
                    continue
            except UnicodeDecodeError:
                pass
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        updated.append(target)
    return updated


def scaffold_workspace(
    workspace_root: str | Path,
    *,
    discord_agents_config_source: str | Path | None = None,
) -> list[Path]:
    workspace_path = Path(workspace_root).expanduser().resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)
    files = build_default_workspace_files()
    config_source = (
        Path(discord_agents_config_source).expanduser().resolve()
        if discord_agents_config_source is not None
        else None
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        preserved_root = Path(tmpdir)
        _backup_init_preserved_paths(workspace_path, preserved_root)
        if config_source is not None:
            _reload_discord_agents_config_source(workspace_path, config_source)

        for relative_path in sorted(INIT_RESET_PATHS):
            target = workspace_path / relative_path
            if not (target.exists() or target.is_symlink()):
                continue
            if relative_path in INIT_REUSABLE_FILES:
                continue
            if target.is_symlink() or target.is_file():
                target.unlink()
                continue
            shutil.rmtree(target)

        created: list[Path] = []
        for relative_path, content in files.items():
            target = workspace_path / relative_path
            if target.exists() or target.is_symlink():
                if relative_path in INIT_REUSABLE_FILES and (target.is_file() or target.is_symlink()):
                    continue
                raise FileExistsError(f"Refusing to overwrite existing file: {target}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            created.append(target)

        _restore_init_preserved_paths(workspace_path, preserved_root)
        _rebuild_preserved_sprint_history_index(workspace_path, preserved_root)
        return created


def _reload_discord_agents_config_source(workspace_path: Path, source_path: Path) -> None:
    if not source_path.is_file():
        raise FileNotFoundError(f"discord_agents_config source does not exist: {source_path}")
    target = workspace_path / "discord_agents_config.yaml"
    target_resolved = target.resolve() if target.exists() or target.is_symlink() else target
    if source_path == target_resolved:
        return
    if target.exists() and target.is_dir():
        raise IsADirectoryError(f"Refusing to overwrite directory with discord config: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target)


def _backup_init_preserved_paths(workspace_path: Path, preserved_root: Path) -> None:
    for relative_path in sorted(INIT_PRESERVED_PATHS):
        source = workspace_path / relative_path
        if not (source.exists() or source.is_symlink()):
            continue
        backup = preserved_root / relative_path
        backup.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink() or source.is_file():
            shutil.copy2(source, backup, follow_symlinks=False)
            continue
        shutil.copytree(source, backup, symlinks=True)


def _restore_init_preserved_paths(workspace_path: Path, preserved_root: Path) -> None:
    for relative_path in sorted(INIT_PRESERVED_PATHS):
        backup = preserved_root / relative_path
        if not backup.exists():
            continue
        target = workspace_path / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        if backup.is_symlink() or backup.is_file():
            if relative_path in INIT_PRESERVED_SKIP_RESTORE_FILES:
                continue
            shutil.copy2(backup, target, follow_symlinks=False)
            continue
        target.mkdir(parents=True, exist_ok=True)
        for child in backup.iterdir():
            child_relative_path = f"{relative_path}/{child.name}"
            if child_relative_path in INIT_PRESERVED_SKIP_RESTORE_FILES:
                continue
            destination = target / child.name
            if child.is_symlink() or child.is_file():
                shutil.copy2(child, destination, follow_symlinks=False)
                continue
            shutil.copytree(child, destination, symlinks=True, dirs_exist_ok=True)


def _rebuild_preserved_sprint_history_index(workspace_path: Path, preserved_root: Path) -> None:
    target_index = workspace_path / "shared_workspace" / "sprint_history" / "index.md"
    preserved_history_root = preserved_root / "shared_workspace" / "sprint_history"
    rows_by_sprint_id: dict[str, dict[str, object]] = {}
    preserved_index = preserved_history_root / "index.md"
    for row in load_sprint_history_index(preserved_index):
        sprint_id = str(row.get("sprint_id") or "").strip()
        if not sprint_id:
            continue
        rows_by_sprint_id[sprint_id] = dict(row)
    for history_path in sorted(preserved_history_root.glob("*.md")):
        if history_path.name == "index.md":
            continue
        parsed = _load_preserved_sprint_history_metadata(history_path)
        sprint_id = str(parsed.get("sprint_id") or "").strip()
        if not sprint_id:
            continue
        merged = dict(rows_by_sprint_id.get(sprint_id) or {})
        for key, value in parsed.items():
            if key == "todo_count":
                if int(value or 0) > 0 or key not in merged:
                    merged[key] = int(value or 0)
                continue
            if str(value or "").strip():
                merged[key] = value
            elif key not in merged:
                merged[key] = value
        rows_by_sprint_id[sprint_id] = merged
    if not rows_by_sprint_id:
        return
    target_index.write_text(
        render_sprint_history_index_rows(list(rows_by_sprint_id.values())),
        encoding="utf-8",
    )


def _load_preserved_sprint_history_metadata(history_path: Path) -> dict[str, object]:
    try:
        lines = history_path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    metadata: dict[str, object] = {
        "sprint_id": history_path.stem,
        "status": "",
        "milestone_title": "",
        "started_at": "",
        "ended_at": "",
        "commit_sha": "",
        "todo_count": 0,
    }
    todo_count = 0
    for raw_line in lines:
        line = str(raw_line).strip()
        if line.startswith("### "):
            todo_count += 1
            continue
        if not line.startswith("- "):
            continue
        key, separator, value = line[2:].partition(":")
        if not separator:
            continue
        normalized_key = key.strip()
        normalized_value = value.strip()
        if normalized_key == "sprint_id":
            metadata["sprint_id"] = normalized_value or history_path.stem
        elif normalized_key == "status":
            metadata["status"] = normalized_value
        elif normalized_key == "milestone_title":
            metadata["milestone_title"] = "" if normalized_value == "N/A" else normalized_value
        elif normalized_key == "started_at":
            metadata["started_at"] = normalized_value
        elif normalized_key == "ended_at":
            metadata["ended_at"] = normalized_value
        elif normalized_key == "commit_sha":
            metadata["commit_sha"] = "" if normalized_value == "N/A" else normalized_value
    metadata["todo_count"] = todo_count
    return metadata
