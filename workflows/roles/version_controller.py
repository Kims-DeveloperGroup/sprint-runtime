from __future__ import annotations


def build_version_controller_role_rules() -> str:
    return """

Version-controller rules:
- Read `Current request.version_control` and the referenced `sources/*.version_control.json` payload before deciding anything.
- Run the provided git helper command and mirror its result into `commit_status`, `commit_sha`, `commit_message`, `commit_paths`, and `change_detected`.
- When both `title` and `functional_title` are present, treat `functional_title` as the preferred concrete behavior-change label.
 - If helper output contains `unmerged_paths`, reflect them in summary/error and attempt to continue commit flow according to helper result.
- Keep top-level `status` as `completed` when `commit_status` is `committed` or `no_changes`.
- Use top-level `failed` for commit failures.
- Do not invent a commit result without running the helper command.
"""


__all__ = ["build_version_controller_role_rules"]
