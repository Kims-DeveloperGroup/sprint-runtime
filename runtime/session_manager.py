from __future__ import annotations

import json
import uuid
from pathlib import Path

from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import utc_now_iso, write_json
from teams_runtime.runtime.identities import service_runtime_identity
from teams_runtime.shared.models import RoleSessionState


class RoleSessionManager:
    def __init__(
        self,
        paths: RuntimePaths,
        role: str,
        sprint_id: str,
        *,
        agent_root: Path | None = None,
        runtime_identity: str | None = None,
    ):
        self.paths = paths
        self.role = role
        self.sprint_id = sprint_id
        self._agent_root = agent_root
        self.runtime_identity = str(runtime_identity or service_runtime_identity(role)).strip() or service_runtime_identity(role)

    @property
    def role_root(self) -> Path:
        return self._agent_root or self.paths.role_root(self.role)

    def load(self) -> RoleSessionState | None:
        payload = {}
        state_file = self.paths.session_state_file(
            self.role,
            runtime_identity=self.runtime_identity,
        )
        try:
            payload = json.loads(state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        state = RoleSessionState.from_dict(payload)
        if not state.workspace_path:
            return None
        if state.runtime_identity and state.runtime_identity != self.runtime_identity:
            return None
        if not state.runtime_identity:
            state.runtime_identity = self.runtime_identity
        return state

    def ensure_session(self) -> RoleSessionState:
        self.paths.ensure_runtime_dirs()
        current = self.load()
        if (
            current is not None
            and current.sprint_id == self.sprint_id
            and Path(current.workspace_path).is_dir()
        ):
            self._seed_workspace(Path(current.workspace_path))
            current.last_used_at = utc_now_iso()
            self.save(current)
            return current
        if current is not None:
            self.archive(current)
        return self.create()

    def save(self, state: RoleSessionState) -> None:
        self.paths.role_sessions_dir.mkdir(parents=True, exist_ok=True)
        state.runtime_identity = self.runtime_identity
        write_json(
            self.paths.session_state_file(
                self.role,
                runtime_identity=self.runtime_identity,
            ),
            state.to_dict(),
        )

    def archive(self, state: RoleSessionState) -> None:
        archive_dir = self.paths.archived_session_dir(state.sprint_id or "unknown", self.role)
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_file = archive_dir / f"{Path(state.workspace_path).name or self.role}.json"
        state.runtime_identity = self.runtime_identity
        write_json(archive_file, state.to_dict())
        try:
            self.paths.session_state_file(
                self.role,
                runtime_identity=self.runtime_identity,
            ).unlink()
        except FileNotFoundError:
            pass

    def create(self) -> RoleSessionState:
        runtime_id = uuid.uuid4().hex
        sessions_root = self.role_root / "sessions"
        session_workspace = sessions_root / runtime_id
        session_workspace.mkdir(parents=True, exist_ok=False)
        self._seed_workspace(session_workspace)
        state = RoleSessionState(
            role=self.role,
            sprint_id=self.sprint_id,
            session_id="",
            workspace_path=str(session_workspace),
            created_at=utc_now_iso(),
            last_used_at=utc_now_iso(),
            runtime_identity=self.runtime_identity,
        )
        self.save(state)
        return state

    def finalize_session_id(self, state: RoleSessionState, session_id: str | None) -> RoleSessionState:
        normalized = str(session_id or "").strip()
        if not normalized:
            state.last_used_at = utc_now_iso()
            self.save(state)
            return state
        state.session_id = normalized
        state.last_used_at = utc_now_iso()
        self.save(state)
        return state

    def _seed_workspace(self, session_workspace: Path) -> None:
        shared_targets = [
            "AGENTS.md",
            "GEMINI.md",
            "todo.md",
            "history.md",
            "journal.md",
            "sources",
            ".agents",
            "workspace_manifest.json",
        ]
        for filename in shared_targets:
            source = self.role_root / filename
            target = session_workspace / filename
            if source.exists() and not (target.exists() or target.is_symlink()):
                target.symlink_to(source)
        project_workspace = self.paths.project_workspace_root
        workspace_link = session_workspace / "workspace"
        if project_workspace.exists() and not (workspace_link.exists() or workspace_link.is_symlink()):
            workspace_link.symlink_to(project_workspace)
        for legacy_name in ("team_runtime.yaml", "discord_agents_config.yaml"):
            legacy_target = session_workspace / legacy_name
            if legacy_target.is_symlink():
                legacy_target.unlink()
        for shared_name in (
            "shared_workspace",
            ".teams_runtime",
            "docs",
            "communication_protocol.md",
            "file_contracts.md",
            "COMMIT_POLICY.md",
        ):
            source = self.paths.workspace_root / shared_name
            target = session_workspace / Path(shared_name).name
            if source.exists() and not (target.exists() or target.is_symlink()):
                target.symlink_to(source)
        context_file = session_workspace / "workspace_context.md"
        context_file.write_text(self._build_workspace_context(session_workspace), encoding="utf-8")

    def _build_workspace_context(self, session_workspace: Path) -> str:
        return "\n".join(
            [
                "# Workspace Context",
                "",
                f"- session_workspace: {session_workspace}",
                f"- teams_runtime_root: {self.paths.workspace_root}",
                f"- project_workspace_root: {self.paths.project_workspace_root}",
                "- shared sprint/docs artifacts: use ./shared_workspace",
                "- teams runtime state files: use ./.teams_runtime",
                "- actual project edits outside teams runtime: use ./workspace",
                "- teams runtime config root is the current workspace when `./.teams_runtime` exists; only fall back to ./workspace/teams_generated when a session-local runtime path is unavailable",
                "- role-private coordination files live in the current session root alongside AGENTS.md, todo.md, history.md, journal.md, and sources/",
                "",
            ]
        )


__all__ = ["RoleSessionManager"]
