from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import read_json


def dedupe_preserving_order(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for item in values:
        candidate = str(item or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def workspace_artifact_hint(paths: RuntimePaths, path: Path) -> str:
    resolved = path.resolve()
    local_roots = (
        (paths.shared_workspace_root.resolve(), "./shared_workspace"),
        (paths.runtime_root.resolve(), "./.teams_runtime"),
        (paths.docs_root.resolve(), "./docs"),
    )
    for root, prefix in local_roots:
        try:
            relative = resolved.relative_to(root)
        except ValueError:
            continue
        return prefix if str(relative) == "." else f"{prefix}/{relative.as_posix()}"
    base = "./workspace/teams_generated" if paths.workspace_root.name == "teams_generated" else "./workspace"
    try:
        relative = resolved.relative_to(paths.workspace_root.resolve())
    except ValueError:
        relative = resolved.relative_to(paths.project_workspace_root.resolve())
        return f"{base}/{relative.as_posix()}"
    return f"{base}/{relative.as_posix()}"


def resolve_artifact_path(paths: RuntimePaths, artifact_hint: str) -> Path | None:
    raw_hint = str(artifact_hint or "").strip()
    if not raw_hint:
        return None
    normalized = raw_hint.strip()

    if normalized.startswith("./"):
        normalized = normalized[2:]

    candidate_hints: list[str] = [normalized]
    if normalized.startswith("workspace/teams_generated/"):
        candidate_hints.append(normalized.removeprefix("workspace/teams_generated/"))
        candidate_hints.append(f"teams_generated/{normalized.removeprefix('workspace/teams_generated/')}")
    elif normalized.startswith("workspace/") and paths.workspace_root.name == "teams_generated":
        candidate_hints.append(normalized.removeprefix("workspace/"))

    candidates: list[Path] = []
    for hint in dedupe_preserving_order(candidate_hints):
        hint_path = Path(hint)
        if hint_path.is_absolute():
            candidate_abs = hint_path.resolve()
            if candidate_abs.exists():
                return candidate_abs
            continue

        workspace_prefix = "teams_generated" if paths.workspace_root.name == "teams_generated" else "workspace"
        base_prefixes = [".teams_runtime", "shared_workspace", "docs", workspace_prefix]
        if workspace_prefix == "workspace":
            base_prefixes.append("workspace/teams_generated")

        candidates.extend(
            [
                paths.workspace_root / hint_path,
                paths.runtime_root / hint_path,
                paths.project_workspace_root / hint_path,
                paths.backlog_dir / hint_path,
            ]
        )

        for base in (paths.workspace_root, paths.runtime_root, paths.project_workspace_root):
            for prefix in base_prefixes:
                if hint == prefix or hint.startswith(f"{prefix}/"):
                    continue
                candidates.append(base / prefix / hint_path)

        if hint.startswith("workspace/"):
            candidates.append(paths.project_workspace_root / hint_path)
        if hint.startswith(".."):
            candidates.append(paths.project_workspace_root / hint_path)

        if normalized.startswith("workspace/teams_generated/"):
            workspace_alias = normalized.removeprefix("workspace/teams_generated/")
            candidates.append(paths.workspace_root / workspace_alias)
            candidates.append(paths.project_workspace_root / f"teams_generated/{workspace_alias}")
            candidates.append(paths.runtime_root / workspace_alias)
            candidates.append(paths.runtime_root / f"teams_generated/{workspace_alias}")

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if resolved.exists():
            return resolved
    return None


def normalize_backlog_file_candidates(values: Any) -> list[Any]:
    normalized: list[Any] = []
    seen: set[int] = set()

    def walk(raw: Any) -> None:
        if isinstance(raw, dict):
            raw_items = raw.get("backlog_items")
            if isinstance(raw_items, list):
                for item in raw_items:
                    if isinstance(item, (str, dict)):
                        normalized_id = id(item)
                        if normalized_id not in seen:
                            seen.add(normalized_id)
                            normalized.append(item)
            raw_item = raw.get("backlog_item")
            if isinstance(raw_item, (str, dict)):
                raw_item_id = id(raw_item)
                if raw_item_id not in seen:
                    seen.add(raw_item_id)
                    normalized.append(raw_item)
            for nested in raw.values():
                walk(nested)
            return
        if isinstance(raw, list):
            for nested in raw:
                walk(nested)

    walk(values)
    return normalized


def collect_backlog_candidates_from_payload(payload: Any) -> list[Any]:
    if not payload:
        return []
    return normalize_backlog_file_candidates(payload)


def planner_backlog_write_receipts(proposals: dict[str, Any]) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_values: list[Any] = []
    raw_list = proposals.get("backlog_writes")
    if isinstance(raw_list, list):
        raw_values.extend(raw_list)
    raw_single = proposals.get("backlog_write")
    if isinstance(raw_single, dict):
        raw_values.append(raw_single)

    for raw_value in raw_values:
        if not isinstance(raw_value, dict):
            continue
        backlog_id = str(raw_value.get("backlog_id") or "").strip()
        artifact_path = str(
            raw_value.get("artifact_path")
            or raw_value.get("artifact")
            or raw_value.get("path")
            or ""
        ).strip()
        if not backlog_id and not artifact_path:
            continue
        dedupe_key = str(backlog_id or artifact_path).strip().lower()
        if dedupe_key and dedupe_key in seen:
            continue
        if dedupe_key:
            seen.add(dedupe_key)
        receipt = dict(raw_value)
        if backlog_id:
            receipt["backlog_id"] = backlog_id
        else:
            receipt.pop("backlog_id", None)
        if artifact_path:
            receipt["artifact_path"] = artifact_path
        else:
            receipt.pop("artifact_path", None)
        receipt.pop("artifact", None)
        receipt.pop("path", None)
        receipts.append(receipt)
    return receipts


def backlog_artifact_candidate_paths(request_record: dict[str, Any], result: dict[str, Any]) -> list[str]:
    candidate_paths: list[str] = []
    for raw_artifact in [
        *(request_record.get("artifacts") or []),
        *(result.get("artifacts") or []),
    ]:
        artifact = str(raw_artifact or "").strip()
        if not artifact:
            continue
        lower = artifact.lower()
        if "backlog-" not in lower or not lower.endswith(".json"):
            continue
        if artifact not in candidate_paths:
            candidate_paths.append(artifact)
    return candidate_paths


def collect_artifact_candidates(*sequences: Iterable[Any]) -> list[str]:
    values: list[str] = []
    for sequence in sequences:
        if not sequence:
            continue
        for raw_value in sequence:
            normalized = str(raw_value or "").strip()
            if normalized:
                values.append(normalized)
    return dedupe_preserving_order(values)


def load_backlog_candidates_from_artifact(paths: RuntimePaths, artifact_path: str) -> list[Any]:
    resolved = resolve_artifact_path(paths, artifact_path)
    if resolved is None or not resolved.is_file():
        return []
    payload = read_json(resolved)
    if isinstance(payload, dict):
        if "backlog_id" in payload:
            return [payload]
        candidates = collect_backlog_candidates_from_payload(payload)
        if candidates:
            return candidates
    elif isinstance(payload, list):
        return [item for item in payload if isinstance(item, (dict, str))]
    return []


def message_attachment_artifacts(paths: RuntimePaths, message: Any) -> list[str]:
    artifacts: list[str] = []
    for attachment in message.attachments:
        saved_path = str(attachment.saved_path or "").strip()
        if not saved_path:
            continue
        try:
            artifact_hint = workspace_artifact_hint(paths, Path(saved_path))
        except Exception:
            artifact_hint = saved_path
        if artifact_hint not in artifacts:
            artifacts.append(artifact_hint)
    return artifacts
