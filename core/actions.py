from __future__ import annotations

import secrets
import string
import subprocess
from pathlib import Path
from typing import Any

from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.persistence import read_json, utc_now_iso, write_json
from teams_runtime.core.reports import build_progress_report, read_process_summary, read_runtime_log_tail
from teams_runtime.models import ActionConfig, TeamRuntimeConfig


class ActionExecutor:
    def __init__(self, paths: RuntimePaths, runtime_config: TeamRuntimeConfig):
        self.paths = paths
        self.runtime_config = runtime_config

    def execute(
        self,
        *,
        request_id: str,
        action_name: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if action_name not in self.runtime_config.actions:
            raise ValueError(f"Unknown action: {action_name}")
        action = self.runtime_config.actions[action_name]
        rendered = self._render_command(action, params)
        if action.lifecycle == "managed":
            return self._start_managed(request_id=request_id, action=action, command=rendered)
        return self._run_foreground(request_id=request_id, action=action, command=rendered)

    def get_operation_status(self, operation_id: str) -> dict[str, Any]:
        record = read_json(self.paths.operation_file(operation_id))
        if not record:
            return {}
        pid = record.get("pid")
        running = False
        if isinstance(pid, int):
            running = read_process_summary(pid) != "N/A"
        record["running"] = running
        if record.get("status") == "running" and not running:
            record["status"] = "completed"
            record["updated_at"] = utc_now_iso()
            write_json(self.paths.operation_file(operation_id), record)
        return record

    def _render_command(self, action: ActionConfig, params: dict[str, Any]) -> list[str]:
        unknown = set(params) - set(action.allowed_params)
        if unknown:
            raise ValueError(
                f"Action '{action.name}' received unsupported params: {', '.join(sorted(unknown))}"
            )
        formatter = string.Formatter()
        rendered: list[str] = []
        for token in action.command:
            required_fields = [field_name for _, field_name, _, _ in formatter.parse(token) if field_name]
            for field_name in required_fields:
                if field_name not in params:
                    raise ValueError(f"Action '{action.name}' is missing param '{field_name}'.")
            rendered.append(token.format(**params))
        return rendered

    def _run_foreground(
        self,
        *,
        request_id: str,
        action: ActionConfig,
        command: list[str],
    ) -> dict[str, Any]:
        operation_id = f"{request_id}-{action.name}-{secrets.token_hex(3)}"
        result = subprocess.run(
            command,
            cwd=str(self.paths.workspace_root),
            capture_output=True,
            text=True,
            check=False,
        )
        log_output = "\n".join(
            part for part in [result.stdout.strip(), result.stderr.strip()] if part
        ).strip()
        self.paths.operations_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.paths.operation_log_file(operation_id)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(log_output + ("\n" if log_output else ""), encoding="utf-8")
        record = {
            "operation_id": operation_id,
            "request_id": request_id,
            "action": action.name,
            "command": command,
            "status": "completed" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
            "domain": action.domain,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "pid": None,
            "log_file": str(log_file),
        }
        write_json(self.paths.operation_file(operation_id), record)
        record["report"] = build_progress_report(
            request=f"{action.name} 실행",
            scope=" ".join(command),
            status="완료" if result.returncode == 0 else "실패",
            list_summary="N/A",
            detail_summary=f"returncode={result.returncode}",
            process_summary="N/A",
            log_summary=log_output or "없음",
            end_reason="없음" if result.returncode == 0 else f"returncode={result.returncode}",
            judgment="등록된 액션이 실행되었습니다." if result.returncode == 0 else "등록된 액션 실행에 실패했습니다.",
            next_action="필요하면 로그를 확인합니다.",
            artifacts=[str(log_file)],
        )
        return record

    def _start_managed(
        self,
        *,
        request_id: str,
        action: ActionConfig,
        command: list[str],
    ) -> dict[str, Any]:
        operation_id = f"{request_id}-{action.name}-{secrets.token_hex(3)}"
        self.paths.operations_dir.mkdir(parents=True, exist_ok=True)
        log_file = self.paths.operation_log_file(operation_id)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as handle:
            process = subprocess.Popen(
                command,
                cwd=str(self.paths.workspace_root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        record = {
            "operation_id": operation_id,
            "request_id": request_id,
            "action": action.name,
            "command": command,
            "status": "running",
            "returncode": None,
            "domain": action.domain,
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "pid": process.pid,
            "log_file": str(log_file),
        }
        write_json(self.paths.operation_file(operation_id), record)
        record["report"] = build_progress_report(
            request=f"{action.name} 실행",
            scope=" ".join(command),
            status="진행중",
            list_summary="N/A",
            detail_summary=f"operation_id={operation_id}",
            process_summary=read_process_summary(process.pid),
            log_summary=read_runtime_log_tail(log_file, max_lines=4),
            end_reason="없음",
            judgment="등록된 액션을 비동기로 시작했습니다.",
            next_action="status로 진행 상태를 확인합니다.",
            artifacts=[str(log_file), str(self.paths.operation_file(operation_id))],
        )
        return record
