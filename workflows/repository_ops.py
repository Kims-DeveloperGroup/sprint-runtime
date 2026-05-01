from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import string
from pathlib import Path
from typing import Any

from teams_runtime.shared.formatting import (
    build_progress_report,
    read_process_summary,
    read_runtime_log_tail,
)
from teams_runtime.shared.models import ActionConfig, TeamRuntimeConfig
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import read_json, utc_now_iso, write_json


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


def _collapse_commit_text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _looks_meta_behavior_label(value: Any) -> bool:
    normalized = _collapse_commit_text(value).lower()
    if not normalized:
        return False
    meta_markers = (
        "정리",
        "구체화",
        "반영",
        "동기화",
        "재구성",
        "업데이트",
        "개선",
        "prompt",
        "프롬프트",
        "문서",
        "라우팅",
        "회귀 테스트",
        "regression test",
    )
    return any(marker in normalized for marker in meta_markers)


def _short_commit_target(path: str) -> str:
    normalized = _decode_git_quoted_path(str(path or "").strip())
    if not normalized:
        return "task"
    target = Path(normalized).name.strip()
    return target or "task"


def _is_test_path(path: str) -> bool:
    normalized = _decode_git_quoted_path(str(path or "").strip())
    if not normalized:
        return False
    target = Path(normalized)
    lower_name = target.name.lower()
    lower_parts = {part.lower() for part in target.parts}
    return (
        "tests" in lower_parts
        or lower_name.startswith("test_")
        or lower_name.endswith("_test.py")
    )


def _is_markdown_doc_path(path: str) -> bool:
    normalized = _decode_git_quoted_path(str(path or "").strip())
    if not normalized:
        return False
    target = Path(normalized)
    lower_name = target.name.lower()
    return target.suffix.lower() == ".md" or lower_name in {"agents.md", "gemini.md"} or lower_name.startswith("readme")


def _is_code_path(path: str) -> bool:
    normalized = _decode_git_quoted_path(str(path or "").strip())
    if not normalized:
        return False
    suffix = Path(normalized).suffix.lower()
    return suffix in {
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".h",
        ".hpp",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".m",
        ".mm",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".swift",
        ".ts",
        ".tsx",
    }


def _commit_target_priority(path: str) -> int:
    normalized = _decode_git_quoted_path(str(path or "").strip())
    if not normalized:
        return 99
    if _is_code_path(normalized) and not _is_test_path(normalized) and not _is_markdown_doc_path(normalized):
        return 0
    if not _is_test_path(normalized) and not _is_markdown_doc_path(normalized):
        return 1
    if _is_test_path(normalized):
        return 2
    return 3


def _select_commit_target_path(changed_paths: list[str]) -> str:
    normalized_paths = sorted(
        {
            _decode_git_quoted_path(str(item).strip())
            for item in (changed_paths or [])
            if str(item).strip()
        }
    )
    if not normalized_paths:
        return ""
    return min(normalized_paths, key=lambda path: (_commit_target_priority(path), path))


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )


def detect_repo_root(start_path: Path) -> Path | None:
    result = _run_git(start_path, ["rev-parse", "--show-toplevel"])
    root = result.stdout.strip()
    return Path(root).resolve() if result.returncode == 0 and root else None


def _decode_git_quoted_path(path_text: str) -> str:
    normalized = str(path_text or "").strip()
    if len(normalized) < 2 or not (normalized.startswith('"') and normalized.endswith('"')):
        return normalized

    body = normalized[1:-1]
    raw_bytes = bytearray()
    index = 0
    simple_escapes = {
        "a": 0x07,
        "b": 0x08,
        "f": 0x0C,
        "n": 0x0A,
        "r": 0x0D,
        "t": 0x09,
        "v": 0x0B,
        "\\": 0x5C,
        '"': 0x22,
    }

    while index < len(body):
        char = body[index]
        if char != "\\":
            raw_bytes.extend(char.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(body):
            raw_bytes.append(0x5C)
            break
        escaped = body[index]
        if escaped in simple_escapes:
            raw_bytes.append(simple_escapes[escaped])
            index += 1
            continue
        if escaped in "01234567":
            octal_digits = [escaped]
            index += 1
            for _ in range(2):
                if index < len(body) and body[index] in "01234567":
                    octal_digits.append(body[index])
                    index += 1
                else:
                    break
            raw_bytes.append(int("".join(octal_digits), 8))
            continue
        raw_bytes.extend(escaped.encode("utf-8"))
        index += 1

    return raw_bytes.decode("utf-8", errors="surrogateescape")


def _parse_status_paths(status_output: str) -> set[str]:
    paths: set[str] = set()
    for raw_line in status_output.splitlines():
        line = raw_line.rstrip()
        if len(line) < 4:
            continue
        body = line[3:]
        if " -> " in body:
            body = body.split(" -> ", 1)[1]
        normalized = _decode_git_quoted_path(body.strip())
        if normalized:
            paths.add(normalized)
    return paths


def capture_git_baseline(project_root: Path) -> dict[str, Any]:
    repo_root = detect_repo_root(project_root)
    if repo_root is None:
        return {"repo_root": "", "head_sha": "", "dirty_paths": []}
    head = _run_git(repo_root, ["rev-parse", "HEAD"]).stdout.strip()
    status = _run_git(repo_root, ["status", "--porcelain=v1", "-uall"]).stdout
    return {
        "repo_root": str(repo_root),
        "head_sha": head,
        "dirty_paths": sorted(_parse_status_paths(status)),
    }


def collect_sprint_owned_paths(project_root: Path, baseline: dict[str, Any]) -> tuple[Path | None, list[str]]:
    repo_root_text = str(baseline.get("repo_root") or "").strip()
    repo_root = Path(repo_root_text).resolve() if repo_root_text else detect_repo_root(project_root)
    if repo_root is None:
        return None, []
    status = _run_git(repo_root, ["status", "--porcelain=v1", "-uall"]).stdout
    current_paths = _parse_status_paths(status)
    baseline_paths = {str(item).strip() for item in (baseline.get("dirty_paths") or []) if str(item).strip()}
    return repo_root, sorted(current_paths - baseline_paths)


def inspect_sprint_closeout(project_root: Path, baseline: dict[str, Any], sprint_id: str = "") -> dict[str, Any]:
    repo_root, uncommitted_paths = collect_sprint_owned_paths(project_root, baseline)
    if repo_root is None:
        return {
            "status": "no_repo",
            "repo_root": "",
            "commit_count": 0,
            "commit_shas": [],
            "commits": [],
            "representative_commit_sha": "",
            "uncommitted_paths": [],
            "message": "git repository를 찾을 수 없습니다.",
        }
    head_sha = str(baseline.get("head_sha") or "").strip()
    commit_records: list[tuple[str, str]] = []
    if head_sha:
        log_result = _run_git(repo_root, ["log", "--format=%H%x1f%s", "--reverse", f"{head_sha}..HEAD"])
        if log_result.returncode != 0:
            return {
                "status": "failed",
                "repo_root": str(repo_root),
                "commit_count": 0,
                "commit_shas": [],
                "commits": [],
                "representative_commit_sha": "",
                "uncommitted_paths": uncommitted_paths,
                "message": log_result.stderr.strip() or log_result.stdout.strip() or "git log failed",
            }
        for line in log_result.stdout.splitlines():
            if not line.strip():
                continue
            sha, _separator, subject = line.partition("\x1f")
            normalized_sha = sha.strip()
            if normalized_sha:
                commit_records.append((normalized_sha, subject.strip()))
    sprint_token = f"[{str(sprint_id or '').strip()}]" if str(sprint_id or "").strip() else ""
    sprint_commits = [
        (sha, subject)
        for sha, subject in commit_records
        if not sprint_token or sprint_token in subject or str(sprint_id or "").strip() in subject
    ]
    commit_shas = [sha for sha, _subject in commit_records]
    sprint_commit_shas = [sha for sha, _subject in sprint_commits]
    sprint_commit_sha_set = set(sprint_commit_shas)
    commits = [
        {
            "sha": sha,
            "short_sha": sha[:7],
            "subject": subject,
            "sprint_tagged": sha in sprint_commit_sha_set,
        }
        for sha, subject in commit_records
    ]
    status = "verified"
    message = "스프린트 closeout 검증을 완료했습니다."
    if uncommitted_paths:
        status = "pending_changes"
        message = "스프린트 소유 변경 파일 중 아직 커밋되지 않은 항목이 있습니다."
    elif not commit_records:
        status = "no_new_commits"
        message = "baseline 이후 새 커밋은 없지만 미커밋 sprint-owned 변경도 없습니다."
    elif sprint_token and not sprint_commit_shas:
        status = "warning_missing_sprint_tag"
        message = "baseline 이후 새 커밋은 확인되었고 미커밋 sprint-owned 변경도 없습니다. sprint_id 태그 커밋은 없어 권장사항 경고만 남깁니다."
    return {
        "status": status,
        "repo_root": str(repo_root),
        "commit_count": len(commit_shas),
        "commit_shas": commit_shas,
        "commits": commits,
        "representative_commit_sha": commit_shas[-1] if commit_shas else "",
        "sprint_tagged_commit_count": len(sprint_commit_shas),
        "sprint_tagged_commit_shas": sprint_commit_shas,
        "uncommitted_paths": uncommitted_paths,
        "message": message,
    }


def build_sprint_commit_message(sprint_id: str) -> str:
    normalized_sprint_id = str(sprint_id or "").strip()
    if normalized_sprint_id:
        return f"[{normalized_sprint_id}] chore: sprint closeout"
    return "chore: sprint closeout"


def build_task_commit_message(
    sprint_id: str,
    todo_id: str,
    backlog_id: str,
    changed_paths: list[str],
    summary: str,
    title: str = "",
    functional_title: str = "",
) -> str:
    normalized_sprint_id = str(sprint_id or "").strip()
    task_token = str(todo_id or "").strip() or str(backlog_id or "").strip() or "task"
    target = _short_commit_target(_select_commit_target_path(changed_paths))
    behavior = ""
    for index, candidate in enumerate((functional_title, title, summary)):
        collapsed = _collapse_commit_text(candidate)
        if not collapsed:
            continue
        if index == 0 and _looks_meta_behavior_label(collapsed):
            continue
        behavior = collapsed
        break
    if not behavior:
        behavior = _collapse_commit_text(summary) or "complete task changes"
    if normalized_sprint_id:
        return f"[{normalized_sprint_id}] {task_token} {target}: {behavior}"
    return f"{task_token} {target}: {behavior}"


def commit_sprint_changes(project_root: Path, baseline: dict[str, Any], message: str) -> dict[str, Any]:
    repo_root, changed_paths = collect_sprint_owned_paths(project_root, baseline)
    if repo_root is None:
        return {
            "status": "no_repo",
            "repo_root": "",
            "changed_paths": [],
            "commit_sha": "",
            "commit_message": message,
            "message": "git repository를 찾을 수 없습니다.",
        }
    if not changed_paths:
        return {
            "status": "no_changes",
            "repo_root": str(repo_root),
            "changed_paths": [],
            "commit_sha": "",
            "commit_message": message,
            "message": "스프린트 소유 변경 파일이 없습니다.",
        }
    add_result = _run_git(repo_root, ["add", "-A", "--", *changed_paths])
    if add_result.returncode != 0:
        return {
            "status": "failed",
            "repo_root": str(repo_root),
            "changed_paths": changed_paths,
            "commit_sha": "",
            "commit_message": message,
            "message": add_result.stderr.strip() or add_result.stdout.strip() or "git add failed",
        }
    commit_result = _run_git(repo_root, ["commit", "-m", message])
    if commit_result.returncode != 0:
        return {
            "status": "failed",
            "repo_root": str(repo_root),
            "changed_paths": changed_paths,
            "commit_sha": "",
            "commit_message": message,
            "message": commit_result.stderr.strip() or commit_result.stdout.strip() or "git commit failed",
        }
    head = _run_git(repo_root, ["rev-parse", "HEAD"]).stdout.strip()
    return {
        "status": "committed",
        "repo_root": str(repo_root),
        "changed_paths": changed_paths,
        "commit_sha": head,
        "commit_message": message,
        "message": commit_result.stdout.strip() or commit_result.stderr.strip() or "commit created",
    }


def auto_commit_task_changes(
    project_root: Path,
    baseline: dict[str, Any],
    sprint_id: str,
    todo_id: str,
    backlog_id: str,
    summary: str,
    title: str = "",
    functional_title: str = "",
) -> dict[str, Any]:
    repo_root, changed_paths = collect_sprint_owned_paths(project_root, baseline)
    if repo_root is None:
        return {
            "status": "no_repo",
            "repo_root": "",
            "changed_paths": [],
            "commit_sha": "",
            "commit_message": "",
            "message": "git repository를 찾을 수 없습니다.",
        }
    if not changed_paths:
        return {
            "status": "no_changes",
            "repo_root": str(repo_root),
            "changed_paths": [],
            "commit_sha": "",
            "commit_message": "",
            "message": "Task 소유 변경 파일이 없습니다.",
        }
    commit_message = build_task_commit_message(
        sprint_id=sprint_id,
        todo_id=todo_id,
        backlog_id=backlog_id,
        changed_paths=changed_paths,
        summary=summary,
        title=title,
        functional_title=functional_title,
    )
    commit_result = commit_sprint_changes(project_root, baseline, commit_message)
    commit_result["commit_message"] = commit_message
    return commit_result


def build_version_control_helper_command(payload_file: str) -> str:
    return f'python -m teams_runtime.core.git_ops apply-version-control --payload-file "{str(payload_file).strip()}"'


def run_version_control_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload) if isinstance(payload, dict) else {}
    mode = str(normalized.get("mode") or "").strip().lower()
    project_root_text = str(normalized.get("project_root") or "").strip()
    baseline = normalized.get("baseline") if isinstance(normalized.get("baseline"), dict) else {}
    if not project_root_text:
        return {
            "mode": mode,
            "status": "failed",
            "repo_root": "",
            "changed_paths": [],
            "commit_sha": "",
            "commit_message": "",
            "commit_status": "failed",
            "change_detected": False,
            "message": "project_root가 필요합니다.",
        }
    project_root = Path(project_root_text).expanduser().resolve()
    if mode == "task":
        result = auto_commit_task_changes(
            project_root,
            baseline,
            sprint_id=str(normalized.get("sprint_id") or ""),
            todo_id=str(normalized.get("todo_id") or ""),
            backlog_id=str(normalized.get("backlog_id") or ""),
            summary=str(normalized.get("summary") or ""),
            title=str(normalized.get("title") or ""),
            functional_title=str(normalized.get("functional_title") or ""),
        )
    elif mode == "closeout":
        commit_message = str(normalized.get("commit_message") or "").strip() or build_sprint_commit_message(
            str(normalized.get("sprint_id") or "")
        )
        result = commit_sprint_changes(project_root, baseline, commit_message)
        result["commit_message"] = commit_message
    else:
        return {
            "mode": mode,
            "status": "failed",
            "repo_root": "",
            "changed_paths": [],
            "commit_sha": "",
            "commit_message": "",
            "commit_status": "failed",
            "change_detected": False,
            "message": f"지원하지 않는 version control mode입니다: {mode or 'unknown'}",
        }
    changed_paths = [
        str(item).strip()
        for item in (result.get("changed_paths") or [])
        if str(item).strip()
    ]
    result["mode"] = mode
    result["change_detected"] = bool(changed_paths)
    result["commit_status"] = str(result.get("status") or "").strip()
    result["changed_paths"] = changed_paths
    return result


def _load_json_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"status": "failed", "message": f"payload file을 찾을 수 없습니다: {path}"}
    except json.JSONDecodeError as exc:
        return {"status": "failed", "message": f"payload file JSON 파싱에 실패했습니다: {exc}"}
    if not isinstance(payload, dict):
        return {"status": "failed", "message": "payload file은 JSON object여야 합니다."}
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="teams_runtime git helper commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    apply_parser = subparsers.add_parser("apply-version-control")
    apply_parser.add_argument("--payload-file", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "apply-version-control":
        parser.error(f"unsupported command: {args.command}")
    payload = _load_json_payload(Path(args.payload_file))
    result = run_version_control_payload(payload)
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
