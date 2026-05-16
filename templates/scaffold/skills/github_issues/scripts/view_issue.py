#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


DEFAULT_FIELDS = "number,title,state,body,labels,assignees,author,createdAt,updatedAt,url,comments"


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
    parser = argparse.ArgumentParser(description="Read one GitHub issue with comments from the linked project repository.")
    parser.add_argument("issue", help="Issue number or URL.")
    parser.add_argument(
        "--repo-dir",
        help="Repository directory. Defaults to ./workspace when present, otherwise current directory.",
    )
    parser.add_argument("--json-fields", default=DEFAULT_FIELDS, help="Comma-separated gh JSON fields.")
    args = parser.parse_args()

    cwd = _repo_dir(args.repo_dir)
    setup_status = _confirm_repo(cwd)
    if setup_status != 0:
        return setup_status

    result = _run(
        [
            "gh",
            "issue",
            "view",
            args.issue,
            "--comments",
            "--json",
            args.json_fields,
        ],
        cwd=cwd,
    )
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    if result.stdout:
        print(result.stdout, end="")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
