"""Compatibility facade for repository/git operations."""

from teams_runtime.workflows.repository_ops import *  # noqa: F401,F403
from teams_runtime.workflows.repository_ops import main


if __name__ == "__main__":
    raise SystemExit(main())
