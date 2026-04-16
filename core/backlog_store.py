from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.persistence import iter_json_records, write_json, utc_now_iso
from teams_runtime.core.sprints import build_backlog_item, render_backlog_markdown


ACTIVE_BACKLOG_STATUSES = {"pending", "selected", "blocked"}
COMPLETED_BACKLOG_STATUS = "done"


def _looks_meta_backlog_title(value: Any) -> bool:
    normalized = " ".join(str(value or "").strip().lower().split())
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


def build_backlog_fingerprint(*, title: str, scope: str, kind: str) -> str:
    normalized = "|".join(
        [
            " ".join(str(title or "").strip().lower().split()),
            " ".join(str(scope or "").strip().lower().split()),
            str(kind or "").strip().lower(),
        ]
    )
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def normalize_backlog_acceptance_criteria(values: Any) -> list[str]:
    if isinstance(values, list):
        return [str(item).strip() for item in values if str(item).strip()]
    if isinstance(values, str):
        normalized = str(values).strip()
        return [normalized] if normalized else []
    return []


def iter_backlog_items(paths: RuntimePaths) -> list[dict[str, Any]]:
    return list(iter_json_records(paths.backlog_dir))


def refresh_backlog_markdown(paths: RuntimePaths) -> None:
    items = iter_backlog_items(paths)
    active_items = [
        item
        for item in items
        if str(item.get("status") or "").strip().lower() in ACTIVE_BACKLOG_STATUSES
    ]
    completed_items = [
        item
        for item in items
        if str(item.get("status") or "").strip().lower() == COMPLETED_BACKLOG_STATUS
    ]
    paths.shared_backlog_file.write_text(
        render_backlog_markdown(active_items),
        encoding="utf-8",
    )
    paths.shared_completed_backlog_file.write_text(
        render_backlog_markdown(
            completed_items,
            title="Completed Backlog",
            empty_message="completed backlog 없음",
        ),
        encoding="utf-8",
    )


def _extract_payload_items(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    collected: list[Any] = []
    raw_items = payload.get("backlog_items")
    if isinstance(raw_items, list):
        collected.extend(raw_items)
    single_item = payload.get("backlog_item")
    if isinstance(single_item, (str, dict)):
        collected.append(single_item)
    if collected:
        return collected
    if any(key in payload for key in ("title", "scope", "summary")):
        return [payload]
    return []


def _normalize_candidate(
    raw_item: Any,
    *,
    default_source: str,
    source_request_id: str = "",
) -> dict[str, Any] | None:
    explicit_fields: set[str] = set()
    if isinstance(raw_item, str):
        title = str(raw_item).strip()
        scope = title
        summary = title
        kind = "enhancement"
        source = default_source
        origin: dict[str, Any] = {"request_id": source_request_id} if source_request_id else {}
        item = build_backlog_item(
            title=title,
            summary=summary,
            kind=kind,
            source=source,
            scope=scope,
            origin=origin,
        )
    elif isinstance(raw_item, dict):
        explicit_fields = {str(key).strip() for key in raw_item.keys()}
        title = str(
            raw_item.get("title")
            or raw_item.get("scope")
            or raw_item.get("summary")
            or ""
        ).strip()
        scope = str(raw_item.get("scope") or title).strip()
        summary = str(raw_item.get("summary") or title or scope).strip()
        if _looks_meta_backlog_title(title) and summary and not _looks_meta_backlog_title(summary):
            title = summary
        elif _looks_meta_backlog_title(title) and scope and not _looks_meta_backlog_title(scope):
            title = scope
        kind = str(raw_item.get("kind") or "enhancement").strip().lower() or "enhancement"
        source = str(raw_item.get("source") or default_source).strip().lower() or default_source
        origin = dict(raw_item.get("origin") or {})
        if source_request_id and "request_id" not in origin:
            origin["request_id"] = source_request_id
        item = build_backlog_item(
            title=title,
            summary=summary,
            kind=kind,
            source=source,
            scope=scope,
            acceptance_criteria=normalize_backlog_acceptance_criteria(
                raw_item.get("acceptance_criteria")
            ),
            origin=origin,
            backlog_id=str(raw_item.get("backlog_id") or "").strip() or None,
            milestone_title=str(raw_item.get("milestone_title") or "").strip(),
            priority_rank=int(raw_item.get("priority_rank") or 0),
            planned_in_sprint_id=str(raw_item.get("planned_in_sprint_id") or "").strip(),
            added_during_active_sprint=bool(raw_item.get("added_during_active_sprint")),
        )
        for field_name in (
            "status",
            "selected_in_sprint_id",
            "completed_in_sprint_id",
            "carry_over_of",
            "blocked_reason",
            "blocked_by_role",
            "recommended_next_step",
        ):
            if field_name in explicit_fields:
                item[field_name] = str(raw_item.get(field_name) or "").strip()
        if "required_inputs" in explicit_fields:
            item["required_inputs"] = [
                str(value).strip()
                for value in (raw_item.get("required_inputs") or [])
                if str(value).strip()
            ]
    else:
        return None
    if not str(item.get("title") or "").strip():
        return None
    item["_explicit_fields"] = sorted(explicit_fields)
    item["fingerprint"] = build_backlog_fingerprint(
        title=str(item.get("title") or ""),
        scope=str(item.get("scope") or ""),
        kind=str(item.get("kind") or ""),
    )
    return item


def _merge_item(existing: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    explicit_fields = {
        str(field).strip()
        for field in (candidate.get("_explicit_fields") or [])
        if str(field).strip()
    }
    always_fields = {"title", "scope", "kind", "fingerprint"}
    for field_name in (
        "title",
        "summary",
        "kind",
        "source",
        "scope",
        "milestone_title",
        "priority_rank",
        "planned_in_sprint_id",
        "status",
        "selected_in_sprint_id",
        "completed_in_sprint_id",
        "carry_over_of",
        "blocked_reason",
        "blocked_by_role",
        "recommended_next_step",
        "fingerprint",
    ):
        if field_name in always_fields or field_name in explicit_fields:
            merged[field_name] = candidate.get(field_name)
    if "acceptance_criteria" in explicit_fields and "acceptance_criteria" in candidate:
        merged["acceptance_criteria"] = list(candidate.get("acceptance_criteria") or [])
    elif list(candidate.get("acceptance_criteria") or []) and not list(merged.get("acceptance_criteria") or []):
        merged["acceptance_criteria"] = list(candidate.get("acceptance_criteria") or [])
    if "required_inputs" in explicit_fields:
        merged["required_inputs"] = list(candidate.get("required_inputs") or [])
    if str(merged.get("status") or "").strip().lower() in {"pending", "selected", "done"}:
        merged["blocked_reason"] = ""
        merged["blocked_by_role"] = ""
        merged["required_inputs"] = []
        merged["recommended_next_step"] = ""
    if candidate.get("added_during_active_sprint"):
        merged["added_during_active_sprint"] = True
    merged.setdefault("origin", {})
    merged["origin"].update(dict(candidate.get("origin") or {}))
    return merged


def merge_backlog_payload(
    *,
    workspace_root: str | Path,
    payload: Any,
    default_source: str = "planner",
    source_request_id: str = "",
) -> dict[str, Any]:
    paths = RuntimePaths.from_root(workspace_root)
    raw_items = _extract_payload_items(payload)
    normalized_candidates = [
        candidate
        for candidate in (
            _normalize_candidate(
                raw_item,
                default_source=default_source,
                source_request_id=source_request_id,
            )
            for raw_item in raw_items
        )
        if candidate is not None
    ]
    existing_items = iter_backlog_items(paths)
    existing_by_backlog_id = {
        str(item.get("backlog_id") or "").strip(): item
        for item in existing_items
        if str(item.get("backlog_id") or "").strip()
    }
    existing_by_fingerprint = {
        str(item.get("fingerprint") or "").strip(): item
        for item in existing_items
        if str(item.get("fingerprint") or "").strip()
    }
    persisted_items: list[dict[str, Any]] = []
    added = 0
    updated = 0
    for candidate in normalized_candidates:
        backlog_id = str(candidate.get("backlog_id") or "").strip()
        fingerprint = str(candidate.get("fingerprint") or "").strip()
        existing = existing_by_backlog_id.get(backlog_id) if backlog_id else None
        if existing is None and fingerprint:
            existing = existing_by_fingerprint.get(fingerprint)
        if existing:
            merged = _merge_item(existing, candidate)
            merged["updated_at"] = utc_now_iso()
            write_json(paths.backlog_file(str(merged.get("backlog_id") or "")), merged)
            existing_by_backlog_id[str(merged.get("backlog_id") or "")] = merged
            existing_by_fingerprint[str(merged.get("fingerprint") or "")] = merged
            updated += 1
            persisted_items.append(merged)
            continue
        new_item = dict(candidate)
        new_item.pop("_explicit_fields", None)
        new_item["updated_at"] = str(new_item.get("created_at") or utc_now_iso())
        write_json(paths.backlog_file(str(new_item.get("backlog_id") or "")), new_item)
        existing_by_backlog_id[str(new_item.get("backlog_id") or "")] = new_item
        existing_by_fingerprint[str(new_item.get("fingerprint") or "")] = new_item
        added += 1
        persisted_items.append(new_item)
    refresh_backlog_markdown(paths)
    return {
        "added": added,
        "updated": updated,
        "items": [
            {
                "backlog_id": str(item.get("backlog_id") or ""),
                "title": str(item.get("title") or ""),
                "status": str(item.get("status") or ""),
                "fingerprint": str(item.get("fingerprint") or ""),
            }
            for item in persisted_items
        ],
    }


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Persist planner-owned backlog updates into teams runtime state.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    merge_parser = subparsers.add_parser("merge", help="Merge backlog payload into runtime backlog state.")
    merge_parser.add_argument("--workspace-root", required=True)
    merge_parser.add_argument("--input-file", required=True)
    merge_parser.add_argument("--source", default="planner")
    merge_parser.add_argument("--request-id", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_argument_parser()
    args = parser.parse_args(argv)
    input_path = Path(args.input_file).expanduser().resolve()
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    if args.command == "merge":
        result = merge_backlog_payload(
            workspace_root=args.workspace_root,
            payload=payload,
            default_source=str(args.source or "planner"),
            source_request_id=str(args.request_id or ""),
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
