from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from teams_runtime.models import INTERNAL_TEAM_AGENTS, TEAM_ROLES


@dataclass(slots=True, frozen=True)
class RuntimePaths:
    workspace_root: Path

    @classmethod
    def from_root(cls, workspace_root: str | Path) -> "RuntimePaths":
        return cls(Path(workspace_root).expanduser().resolve())

    @property
    def runtime_root(self) -> Path:
        return self.workspace_root / ".teams_runtime"

    @property
    def project_workspace_root(self) -> Path:
        if self.workspace_root.name == "teams_generated":
            return self.workspace_root.parent
        return self.workspace_root

    @property
    def logs_root(self) -> Path:
        return self.workspace_root / "logs"

    @property
    def agent_logs_dir(self) -> Path:
        return self.logs_root / "agents"

    @property
    def agent_log_archive_dir(self) -> Path:
        return self.agent_logs_dir / "archive"

    @property
    def discord_logs_dir(self) -> Path:
        return self.logs_root / "discord"

    @property
    def operations_logs_dir(self) -> Path:
        return self.logs_root / "operations"

    @property
    def requests_dir(self) -> Path:
        return self.runtime_root / "requests"

    @property
    def backlog_dir(self) -> Path:
        return self.runtime_root / "backlog"

    @property
    def sprints_dir(self) -> Path:
        return self.runtime_root / "sprints"

    @property
    def operations_dir(self) -> Path:
        return self.runtime_root / "operations"

    @property
    def role_sessions_dir(self) -> Path:
        return self.runtime_root / "role_sessions"

    @property
    def archive_dir(self) -> Path:
        return self.runtime_root / "archive"

    def agent_runtime_dir(self, role: str) -> Path:
        return self.runtime_root / "agents" / role

    def agent_pid_file(self, role: str) -> Path:
        return self.agent_runtime_dir(role) / "service.pid"

    def agent_lock_file(self, role: str) -> Path:
        return self.agent_runtime_dir(role) / "service.lock"

    def agent_runtime_log(self, role: str) -> Path:
        return self.agent_logs_dir / f"{role}.log"

    def agent_discord_log(self, role: str) -> Path:
        return self.discord_logs_dir / f"{role}.jsonl"

    def agent_state_file(self, role: str) -> Path:
        return self.agent_runtime_dir(role) / "service.json"

    def request_file(self, request_id: str) -> Path:
        return self.requests_dir / f"{request_id}.json"

    def backlog_file(self, backlog_id: str) -> Path:
        return self.backlog_dir / f"{backlog_id}.json"

    def sprint_file(self, sprint_id: str) -> Path:
        return self.sprints_dir / f"{sprint_id}.json"

    def sprint_events_file(self, sprint_id: str) -> Path:
        return self.sprints_dir / f"{sprint_id}.events.jsonl"

    @property
    def sprint_scheduler_file(self) -> Path:
        return self.runtime_root / "sprint_scheduler.json"

    def session_state_file(self, role: str) -> Path:
        return self.role_sessions_dir / f"{role}.json"

    def archived_session_dir(self, sprint_id: str, role: str) -> Path:
        return self.archive_dir / sprint_id / role

    def operation_file(self, operation_id: str) -> Path:
        return self.operations_dir / f"{operation_id}.json"

    def operation_log_file(self, operation_id: str) -> Path:
        return self.operations_logs_dir / f"{operation_id}.log"

    def role_root(self, role: str) -> Path:
        return self.workspace_root / role

    @property
    def internal_root(self) -> Path:
        return self.workspace_root / "internal"

    def internal_agent_root(self, agent_name: str) -> Path:
        return self.internal_root / agent_name

    def role_sources_dir(self, role: str) -> Path:
        return self.role_root(role) / "sources"

    def role_todo_file(self, role: str) -> Path:
        return self.role_root(role) / "todo.md"

    def role_history_file(self, role: str) -> Path:
        return self.role_root(role) / "history.md"

    def role_journal_file(self, role: str) -> Path:
        return self.role_root(role) / "journal.md"

    def role_request_snapshot_file(self, role: str, request_id: str) -> Path:
        return self.role_sources_dir(role) / f"{request_id}.request.md"

    @property
    def shared_workspace_root(self) -> Path:
        return self.workspace_root / "shared_workspace"

    @property
    def shared_attachments_root(self) -> Path:
        return self.shared_workspace_root / "attachments"

    def shared_attachment_dir(self, identifier: str) -> Path:
        normalized = str(identifier or "").strip() or "unknown"
        return self.shared_attachments_root / normalized

    def sprint_attachment_root(self, folder_name: str) -> Path:
        return self.sprint_artifact_dir(folder_name) / "attachments"

    def sprint_attachment_dir(self, folder_name: str, identifier: str) -> Path:
        normalized = str(identifier or "").strip() or "unknown"
        return self.sprint_attachment_root(folder_name) / normalized

    @property
    def shared_planning_file(self) -> Path:
        return self.shared_workspace_root / "planning.md"

    @property
    def shared_decision_log_file(self) -> Path:
        return self.shared_workspace_root / "decision_log.md"

    @property
    def shared_history_file(self) -> Path:
        return self.shared_workspace_root / "shared_history.md"

    @property
    def shared_sync_contract_file(self) -> Path:
        return self.shared_workspace_root / "sync_contract.md"

    @property
    def shared_backlog_file(self) -> Path:
        return self.shared_workspace_root / "backlog.md"

    @property
    def shared_completed_backlog_file(self) -> Path:
        return self.shared_workspace_root / "completed_backlog.md"

    @property
    def current_sprint_file(self) -> Path:
        return self.shared_workspace_root / "current_sprint.md"

    @property
    def sprint_artifacts_root(self) -> Path:
        return self.shared_workspace_root / "sprints"

    @property
    def sprint_history_root(self) -> Path:
        return self.shared_workspace_root / "sprint_history"

    @property
    def sprint_history_index_file(self) -> Path:
        return self.sprint_history_root / "index.md"

    def sprint_history_file(self, sprint_id: str) -> Path:
        return self.sprint_history_root / f"{sprint_id}.md"

    def sprint_artifact_dir(self, folder_name: str) -> Path:
        return self.sprint_artifacts_root / str(folder_name or "").strip()

    def sprint_artifact_file(self, folder_name: str, filename: str) -> Path:
        return self.sprint_artifact_dir(folder_name) / filename

    @property
    def docs_root(self) -> Path:
        return self.workspace_root / "docs"

    def ensure_runtime_dirs(self) -> None:
        for path in (
            self.logs_root,
            self.agent_logs_dir,
            self.agent_log_archive_dir,
            self.discord_logs_dir,
            self.operations_logs_dir,
            self.runtime_root,
            self.requests_dir,
            self.backlog_dir,
            self.sprints_dir,
            self.operations_dir,
            self.role_sessions_dir,
            self.archive_dir,
            self.shared_workspace_root,
            self.shared_attachments_root,
            self.sprint_artifacts_root,
            self.sprint_history_root,
            self.internal_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        for role in TEAM_ROLES:
            self.role_sources_dir(role).mkdir(parents=True, exist_ok=True)
        for agent_name in INTERNAL_TEAM_AGENTS:
            self.internal_agent_root(agent_name).mkdir(parents=True, exist_ok=True)
            (self.internal_agent_root(agent_name) / "sources").mkdir(parents=True, exist_ok=True)
