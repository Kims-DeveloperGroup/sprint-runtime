from __future__ import annotations

import asyncio
import fcntl
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.formatting import read_runtime_log_tail
from teams_runtime.shared.persistence import read_json, utc_now_iso, write_json


def _update_agent_runtime_state(paths: RuntimePaths, role: str, **updates) -> None:
    state = read_json(paths.agent_state_file(role))
    if not isinstance(state, dict):
        state = {"role": role}
    state.update({key: value for key, value in updates.items()})
    state["updated_at"] = utc_now_iso()
    write_json(paths.agent_state_file(role), state)


def _list_process_table() -> list[tuple[int, str]]:
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    rows: list[tuple[int, str]] = []
    for raw_line in str(result.stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if not parts:
            continue
        pid_text = parts[0].strip()
        if not pid_text.isdigit():
            continue
        command = parts[1].strip() if len(parts) > 1 else ""
        rows.append((int(pid_text), command))
    return rows


def _is_runtime_service_command(command: str) -> bool:
    normalized = str(command or "").strip()
    if not normalized:
        return False
    padded = f" {normalized} "
    if "teams_runtime.cli" in normalized:
        return True
    if " -m teams_runtime " in padded:
        return True
    first_token = Path(normalized.split(None, 1)[0]).name
    return first_token == "teams_runtime"


def _matching_role_service_pids(paths: RuntimePaths, role: str, *, exclude_pid: int | None = None) -> list[int]:
    workspace_arg = f"--workspace-root {paths.workspace_root}"
    role_arg = f"--agent {role}"
    matches: list[int] = []
    for pid, command in _list_process_table():
        if exclude_pid is not None and pid == exclude_pid:
            continue
        if not _is_runtime_service_command(command) or " run " not in f" {command} ":
            continue
        if workspace_arg not in command or role_arg not in command:
            continue
        if is_process_running(pid):
            matches.append(pid)
    return sorted(set(matches))


def write_pid_file(pid_file: Path, pid: int) -> None:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{pid}\n", encoding="utf-8")


def read_pid_file(pid_file: Path) -> int | None:
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    return int(raw) if raw.isdigit() else None


def remove_pid_file(pid_file: Path) -> None:
    try:
        pid_file.unlink()
    except FileNotFoundError:
        return


def try_acquire_lock(lock_file: Path):
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_file.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def release_lock(lock_handle) -> None:
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock_handle.close()


def _read_process_stat(pid: int) -> str:
    if pid <= 0:
        return ""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    lines = [line.strip() for line in str(result.stdout or "").splitlines() if line.strip()]
    return lines[0] if lines else ""


def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    process_stat = _read_process_stat(pid)
    if process_stat.startswith("Z"):
        return False
    return True


def _signal_role_service(pid: int, sig: signal.Signals) -> None:
    try:
        process_group_id = os.getpgid(pid)
    except OSError:
        os.kill(pid, sig)
        return
    if process_group_id == pid:
        os.killpg(process_group_id, sig)
        return
    os.kill(pid, sig)


def _record_role_service_stopped(paths: RuntimePaths, role: str, targets: set[int]) -> None:
    remove_pid_file(paths.agent_pid_file(role))
    previous = read_json(paths.agent_state_file(role))
    write_json(
        paths.agent_state_file(role),
        {
            "role": role,
            "pid": sorted(targets)[-1],
            "status": "stopped",
            "started_at": previous.get("started_at", ""),
            "updated_at": utc_now_iso(),
            "runtime_log_file": str(paths.agent_runtime_log(role)),
        },
    )


def role_service_status(paths: RuntimePaths, role: str) -> tuple[bool, int | None]:
    pid = read_pid_file(paths.agent_pid_file(role))
    discovered = _matching_role_service_pids(paths, role)
    if pid is not None and is_process_running(pid):
        return True, pid
    if discovered:
        return True, discovered[-1]
    if pid is not None:
        _update_agent_runtime_state(
            paths,
            role,
            pid=pid,
            status="stopped",
            process_status="stale_pid",
            last_error=f"PID file points to a non-running process: {pid}",
            last_failure_at=utc_now_iso(),
            recovery_action="stale pid 파일을 정리하고 서비스를 다시 시작합니다.",
        )
    return False, None


def build_background_command(
    workspace_root: Path,
    role: str,
    *,
    relay_transport: str = "internal",
) -> list[str]:
    return [
        sys.executable,
        "-u",
        "-m",
        "teams_runtime.cli",
        "run",
        "--workspace-root",
        str(workspace_root),
        "--agent",
        role,
        "--relay-transport",
        str(relay_transport or "internal").strip() or "internal",
    ]


def build_background_env() -> dict[str, str]:
    env = dict(os.environ)
    package_parent = str(Path(__file__).resolve().parents[3])
    existing = [item for item in str(env.get("PYTHONPATH") or "").split(os.pathsep) if item]
    if package_parent not in existing:
        env["PYTHONPATH"] = os.pathsep.join([package_parent, *existing])
    elif existing:
        env["PYTHONPATH"] = os.pathsep.join(existing)
    else:
        env["PYTHONPATH"] = package_parent
    return env


def _archive_runtime_log(paths: RuntimePaths, role: str) -> Path | None:
    runtime_log = paths.agent_runtime_log(role)
    if not runtime_log.exists() or runtime_log.stat().st_size <= 0:
        return None
    archive_dir = paths.agent_log_archive_dir
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    archived_log = archive_dir / f"{role}-{stamp}.log"
    suffix = 1
    while archived_log.exists():
        archived_log = archive_dir / f"{role}-{stamp}-{suffix}.log"
        suffix += 1
    runtime_log.replace(archived_log)
    return archived_log


def start_background_role_service(
    paths: RuntimePaths,
    role: str,
    *,
    relay_transport: str = "internal",
) -> int:
    discovered = _matching_role_service_pids(paths, role)
    if discovered:
        pid_list = ", ".join(str(pid) for pid in discovered)
        raise RuntimeError(f"{role} service is already running with PID {pid_list}.")
    running, existing_pid = role_service_status(paths, role)
    if running and existing_pid is not None:
        raise RuntimeError(f"{role} service is already running with PID {existing_pid}.")
    lock_handle = try_acquire_lock(paths.agent_lock_file(role))
    if lock_handle is None:
        pid_hint = read_pid_file(paths.agent_pid_file(role))
        suffix = f" with PID {pid_hint}" if pid_hint is not None else ""
        raise RuntimeError(f"{role} service is already running{suffix}.")
    release_lock(lock_handle)

    runtime_dir = paths.agent_runtime_dir(role)
    runtime_dir.mkdir(parents=True, exist_ok=True)
    runtime_log = paths.agent_runtime_log(role)
    archived_log = _archive_runtime_log(paths, role)
    with runtime_log.open("w", encoding="utf-8") as handle:
        handle.write(
            "[teams_runtime] service_start "
            f"role={role} started_at={utc_now_iso()} archived_log={archived_log or 'none'}\n"
        )
        handle.flush()
        process = subprocess.Popen(
            build_background_command(
                paths.workspace_root,
                role,
                relay_transport=relay_transport,
            ),
            cwd=str(paths.workspace_root),
            env=build_background_env(),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    time.sleep(1.0)
    if process.poll() is not None:
        tail = read_runtime_log_tail(runtime_log)
        _update_agent_runtime_state(
            paths,
            role,
            pid=process.pid,
            status="failed",
            process_status="exited_immediately",
            last_error=tail or f"{role} service exited immediately after launch.",
            last_failure_at=utc_now_iso(),
            recovery_action="runtime log를 확인해 토큰/환경/연결 오류를 수정한 뒤 서비스를 다시 시작합니다.",
        )
        raise RuntimeError(f"{role} service exited immediately after launch.\n{tail}")
    return process.pid


def stop_background_role_service(paths: RuntimePaths, role: str, timeout_seconds: float = 5.0) -> tuple[bool, str]:
    pid = read_pid_file(paths.agent_pid_file(role))
    targets = set(_matching_role_service_pids(paths, role))
    if pid is not None and is_process_running(pid):
        targets.add(pid)
    if not targets:
        if pid is not None:
            remove_pid_file(paths.agent_pid_file(role))
            return False, f"Removed stale pid file for {role} service PID {pid}."
        return False, f"{role} service is not running."
    for target_pid in sorted(targets):
        try:
            _signal_role_service(target_pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
    deadline = time.monotonic() + max(0.0, timeout_seconds)
    while time.monotonic() < deadline:
        remaining = [target_pid for target_pid in targets if is_process_running(target_pid)]
        if not remaining:
            _record_role_service_stopped(paths, role, targets)
            pid_list = ", ".join(str(target_pid) for target_pid in sorted(targets))
            return True, f"Stopped {role} service with PID {pid_list}."
        time.sleep(0.1)
    remaining = [target_pid for target_pid in targets if is_process_running(target_pid)]
    for target_pid in remaining:
        try:
            _signal_role_service(target_pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
    force_deadline = time.monotonic() + 1.0
    while time.monotonic() < force_deadline:
        remaining = [target_pid for target_pid in targets if is_process_running(target_pid)]
        if not remaining:
            _record_role_service_stopped(paths, role, targets)
            pid_list = ", ".join(str(target_pid) for target_pid in sorted(targets))
            return True, f"Force-stopped {role} service with PID {pid_list} after SIGTERM timeout."
        time.sleep(0.1)
    pid_list = ", ".join(str(target_pid) for target_pid in sorted(targets))
    return False, f"Timed out waiting for {role} service PID {pid_list} to exit."


async def run_foreground_role_service(
    paths: RuntimePaths,
    role: str,
    coroutine_factory: Callable[[], Awaitable[None]],
) -> None:
    current_pid = os.getpid()
    discovered = _matching_role_service_pids(paths, role, exclude_pid=current_pid)
    if discovered:
        pid_list = ", ".join(str(pid) for pid in discovered)
        raise RuntimeError(f"{role} service is already running with PID {pid_list}.")
    lock_handle = try_acquire_lock(paths.agent_lock_file(role))
    if lock_handle is None:
        existing_pid = read_pid_file(paths.agent_pid_file(role))
        suffix = f" with PID {existing_pid}" if existing_pid is not None else ""
        raise RuntimeError(f"{role} service is already running{suffix}.")
    write_pid_file(paths.agent_pid_file(role), current_pid)
    write_json(
        paths.agent_state_file(role),
        {
            "role": role,
            "pid": current_pid,
            "status": "running",
            "started_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
            "runtime_log_file": str(paths.agent_runtime_log(role)),
        },
    )
    try:
        await coroutine_factory()
    finally:
        previous = read_json(paths.agent_state_file(role))
        write_json(
            paths.agent_state_file(role),
                {
                    "role": role,
                    "pid": current_pid,
                    "status": "stopped",
                    "started_at": previous.get("started_at", ""),
                    "updated_at": utc_now_iso(),
                    "runtime_log_file": str(paths.agent_runtime_log(role)),
                },
            )
        remove_pid_file(paths.agent_pid_file(role))
        release_lock(lock_handle)
