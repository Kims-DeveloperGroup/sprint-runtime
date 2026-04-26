#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _skill_workspace_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _resolve_command_root(workspace_root: Path) -> Path:
    checked: set[Path] = set()
    candidates = [Path.cwd().resolve(), *Path.cwd().resolve().parents, workspace_root, *workspace_root.parents]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in checked:
            continue
        checked.add(resolved)
        if (resolved / "teams_runtime" / "cli.py").is_file():
            return resolved
    return workspace_root


def _run(cmd: list[str], *, cwd: Path) -> tuple[int, str]:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    output = completed.stdout.strip()
    error = completed.stderr.strip()
    combined = output
    if error:
        combined = f"{combined}\n{error}".strip()
    return completed.returncode, combined


def _render_command_section(title: str, cmd: list[str], returncode: int, output: str) -> str:
    rendered_cmd = " ".join(cmd)
    body = output or "(no output)"
    return "\n".join(
        [
            f"## {title}",
            f"- command: `{rendered_cmd}`",
            f"- exit_code: {returncode}",
            "```text",
            body,
            "```",
        ]
    )


def _tail_lines(path: Path, line_count: int) -> str:
    if not path.exists():
        return f"(missing log file: {path})"
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if line_count <= 0:
        return ""
    return "\n".join(lines[-line_count:]) if lines else "(empty log file)"


def _build_status_command(args: argparse.Namespace, *, workspace_root: Path) -> list[str]:
    cmd = [sys.executable, "-m", "teams_runtime", "status", "--workspace-root", str(workspace_root)]
    if args.agent:
        cmd.extend(["--agent", args.agent])
    if args.request_id:
        cmd.extend(["--request-id", args.request_id])
    if args.sprint:
        cmd.append("--sprint")
    if args.backlog:
        cmd.append("--backlog")
    return cmd


def _build_ps_command() -> list[str]:
    return ["ps", "axo", "pid,ppid,stat,etime,command"]


def _filter_ps_output(output: str, *, agent: str | None) -> str:
    kept_lines: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("PID "):
            kept_lines.append(line)
            continue
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        command = parts[4]
        executable = Path(command.split()[0]).name if command.split() else ""
        if not executable.startswith("python"):
            continue
        if " -m teams_runtime" not in command and "teams_runtime.cli" not in command:
            continue
        if agent and f"--agent {agent}" not in command and agent not in command:
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines) if kept_lines else "(no matching process lines)"


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect a compact read-only teams_runtime snapshot.")
    parser.add_argument(
        "--workspace-root",
        default=".",
        help="Generated teams_runtime workspace root. Defaults to the workspace that owns this skill.",
    )
    parser.add_argument("--agent", help="Optional single agent target.")
    parser.add_argument("--request-id", help="Optional request identifier for status lookup.")
    parser.add_argument("--sprint", action="store_true", help="Include sprint-oriented status output.")
    parser.add_argument("--backlog", action="store_true", help="Include backlog-oriented status output.")
    parser.add_argument("--include-ps", action="store_true", help="Include a filtered ps snapshot.")
    parser.add_argument("--log-role", help="Optional role whose agent log should be tailed.")
    parser.add_argument("--log-tail", type=int, default=40, help="Number of log lines to tail when --log-role is set.")
    args = parser.parse_args()

    skill_workspace_root = _skill_workspace_root()
    workspace_root = Path(args.workspace_root).expanduser()
    if not workspace_root.is_absolute():
        workspace_root = (skill_workspace_root / workspace_root).resolve()
    command_root = _resolve_command_root(workspace_root)

    lines: list[str] = [
        "# teams_runtime snapshot",
        f"- generated_at: {datetime.now(timezone.utc).isoformat()}",
        f"- skill_workspace_root: `{skill_workspace_root}`",
        f"- workspace_root: `{workspace_root}`",
        f"- command_root: `{command_root}`",
    ]
    if args.agent:
        lines.append(f"- agent: `{args.agent}`")
    if args.request_id:
        lines.append(f"- request_id: `{args.request_id}`")
    if args.sprint:
        lines.append("- scope: `sprint`")
    if args.backlog:
        lines.append("- scope: `backlog`")

    list_cmd = [sys.executable, "-m", "teams_runtime", "list", "--workspace-root", str(workspace_root)]
    list_code, list_output = _run(list_cmd, cwd=command_root)
    lines.extend(["", _render_command_section("List", list_cmd, list_code, list_output)])

    status_cmd = _build_status_command(args, workspace_root=workspace_root)
    status_code, status_output = _run(status_cmd, cwd=command_root)
    lines.extend(["", _render_command_section("Status", status_cmd, status_code, status_output)])

    if args.include_ps:
        ps_cmd = _build_ps_command()
        ps_code, ps_output = _run(ps_cmd, cwd=command_root)
        filtered_output = _filter_ps_output(ps_output, agent=args.agent)
        lines.extend(["", _render_command_section("PS", ps_cmd, ps_code, filtered_output)])

    if args.log_role:
        log_path = workspace_root / "logs" / "agents" / f"{args.log_role}.log"
        log_output = _tail_lines(log_path, args.log_tail)
        lines.extend(
            [
                "",
                "## Log Tail",
                f"- path: `{log_path}`",
                f"- lines: {args.log_tail}",
                "```text",
                log_output or "(no output)",
                "```",
            ]
        )

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
