#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_FIELDS = "number,title,state,labels,assignees,updatedAt,url"


def _repo_dir(repo_dir: str | None) -> Path:
    if repo_dir:
        return Path(repo_dir).expanduser().resolve()
    workspace = Path("workspace")
    if workspace.is_dir():
        return workspace.resolve()
    return Path(".").resolve()


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def _confirm_repo(cwd: Path) -> int:
    if shutil.which("gh") is None:
        print("GitHub CLI `gh` is not installed or not on PATH.", file=sys.stderr)
        return 127
    if not cwd.is_dir():
        print(f"Repository directory does not exist or is not a directory: {cwd}", file=sys.stderr)
        return 2
    result = _run(["gh", "repo", "view", "--json", "nameWithOwner,url"], cwd=cwd)
    if result.returncode != 0:
        print(f"Unable to confirm GitHub repository from {cwd}.", file=sys.stderr)
        if result.stderr.strip():
            print(result.stderr.strip(), file=sys.stderr)
        return result.returncode
    print(f"Confirmed repository: {result.stdout.strip()}", file=sys.stderr)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="List GitHub issues from the linked project repository.")
    parser.add_argument(
        "--repo-dir",
        help="Repository directory. Defaults to ./workspace when present, otherwise current directory.",
    )
    parser.add_argument(
        "--state",
        choices=("open", "closed", "all"),
        help="Issue state. Defaults to all for searches, otherwise open.",
    )
    parser.add_argument("--search", help="GitHub issue search query.")
    parser.add_argument("--limit", type=int, default=30, help="Maximum issues to return.")
    parser.add_argument("--json-fields", default=DEFAULT_FIELDS, help="Comma-separated gh JSON fields.")
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be at least 1")

    cwd = _repo_dir(args.repo_dir)
    setup_status = _confirm_repo(cwd)
    if setup_status != 0:
        return setup_status

    state = args.state or ("all" if args.search else "open")
    command = [
        "gh",
        "issue",
        "list",
        "--state",
        state,
        "--limit",
        str(args.limit),
        "--json",
        args.json_fields,
    ]
    if args.search:
        command.extend(["--search", args.search])

    result = _run(command, cwd=cwd)
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    if result.stdout:
        print(result.stdout, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
