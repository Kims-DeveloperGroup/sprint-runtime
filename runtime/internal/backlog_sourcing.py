from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import utc_now_iso
from teams_runtime.runtime.codex_runner import CodexRunner, extract_json_object
from teams_runtime.runtime.identities import service_runtime_identity
from teams_runtime.runtime.session_manager import RoleSessionManager
from teams_runtime.shared.models import RoleRuntimeConfig


LOGGER = logging.getLogger(__name__)
SOURCER_LOG_PREFIX = "[sourcer]"
ALLOWED_SOURCER_STATUSES = {"completed", "failed"}
ALLOWED_BACKLOG_KINDS = {"bug", "enhancement", "feature", "chore"}


def _sample_item_labels(items: list[dict[str, Any]], *, limit: int = 3) -> list[str]:
    labels: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("title") or item.get("scope") or item.get("summary") or "").strip()
        if not label:
            continue
        labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def normalize_backlog_sourcing_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload) if isinstance(payload, dict) else {}

    status = str(normalized.get("status") or "").strip().lower()
    if status not in ALLOWED_SOURCER_STATUSES:
        status = "failed" if status else "completed"
    normalized["status"] = status
    normalized["summary"] = str(normalized.get("summary") or "").strip()
    normalized["error"] = str(normalized.get("error") or "").strip()

    raw_items = normalized.get("backlog_items")
    if not isinstance(raw_items, list):
        single = normalized.get("backlog_item")
        raw_items = [single] if isinstance(single, (dict, str)) else []

    items: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if isinstance(raw_item, str):
            title = str(raw_item).strip()
            if not title:
                continue
            items.append(
                {
                    "title": title,
                    "summary": title,
                    "kind": "enhancement",
                    "scope": title,
                    "acceptance_criteria": [],
                    "origin": {},
                }
            )
            continue
        if not isinstance(raw_item, dict):
            continue
        title = str(
            raw_item.get("title")
            or raw_item.get("scope")
            or raw_item.get("summary")
            or ""
        ).strip()
        if not title:
            continue
        lowered_title = title.lower()
        summary_text = str(raw_item.get("summary") or title).strip()
        scope_text = str(raw_item.get("scope") or title).strip()
        if any(
            marker in lowered_title
            for marker in ("정리", "구체화", "반영", "동기화", "재구성", "업데이트", "개선", "prompt", "프롬프트", "문서", "라우팅", "회귀 테스트")
        ):
            if summary_text and summary_text != title:
                title = summary_text
            elif scope_text and scope_text != title:
                title = scope_text
        kind = str(raw_item.get("kind") or "enhancement").strip().lower().replace(" ", "_")
        if kind in {"new_feature", "new-feature"}:
            kind = "feature"
        if kind not in ALLOWED_BACKLOG_KINDS:
            kind = "enhancement"
        acceptance = raw_item.get("acceptance_criteria")
        if isinstance(acceptance, list):
            acceptance_criteria = [str(item).strip() for item in acceptance if str(item).strip()]
        elif isinstance(acceptance, str):
            acceptance_criteria = [acceptance.strip()] if acceptance.strip() else []
        else:
            acceptance_criteria = []
        origin = raw_item.get("origin")
        items.append(
            {
                "title": title,
                "summary": summary_text,
                "kind": kind,
                "scope": scope_text,
                "acceptance_criteria": acceptance_criteria,
                "origin": dict(origin or {}) if isinstance(origin, dict) else {},
            }
        )
    normalized["backlog_items"] = items
    return normalized


class BacklogSourcingRuntime:
    def __init__(
        self,
        *,
        paths: RuntimePaths,
        sprint_id: str,
        runtime_config: RoleRuntimeConfig,
        session_identity: str | None = None,
    ):
        self.paths = paths
        self.role = "sourcer"
        self.sprint_id = sprint_id
        self.runtime_identity = str(session_identity or service_runtime_identity(self.role)).strip() or service_runtime_identity(self.role)
        self.session_manager = RoleSessionManager(
            paths,
            self.role,
            sprint_id,
            agent_root=paths.internal_agent_root("sourcer"),
            runtime_identity=self.runtime_identity,
        )
        self.codex_runner = CodexRunner(runtime_config, role=self.role)
        self._run_lock = threading.Lock()

    def source(
        self,
        *,
        findings: list[dict[str, Any]],
        scheduler_state: dict[str, Any],
        active_sprint: dict[str, Any],
        backlog_counts: dict[str, int],
        existing_backlog: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self._run_lock:
            started_monotonic = time.monotonic()
            previous_state = self.session_manager.load()
            reused_session = (
                previous_state is not None
                and previous_state.sprint_id == self.sprint_id
                and Path(previous_state.workspace_path).is_dir()
            )
            monitoring: dict[str, Any] = {
                "started_at": utc_now_iso(),
                "reuse_session": reused_session,
                "findings_count": len(findings),
                "findings_sample": _sample_item_labels(findings),
                "existing_backlog_count": len(existing_backlog),
                "existing_backlog_sample": _sample_item_labels(existing_backlog),
                "fallback_used": False,
            }
            LOGGER.info(
                "%s start findings=%s existing_backlog=%s reuse_session=%s",
                SOURCER_LOG_PREFIX,
                len(findings),
                len(existing_backlog),
                reused_session,
            )
            state = self.session_manager.ensure_session()
            monitoring["session_workspace"] = state.workspace_path
            monitoring["session_id_before"] = state.session_id or ""
            LOGGER.info(
                "%s prompt_build_start workspace=%s session_id=%s findings_sample=%s",
                SOURCER_LOG_PREFIX,
                state.workspace_path,
                state.session_id or "new",
                ", ".join(monitoring["findings_sample"]) or "none",
            )
            prompt = self._build_prompt(
                findings=findings,
                scheduler_state=scheduler_state,
                active_sprint=active_sprint,
                backlog_counts=backlog_counts,
                existing_backlog=existing_backlog,
            )
            monitoring["prompt_chars"] = len(prompt)
            LOGGER.info(
                "%s prompt_build_complete chars=%s existing_backlog_sample=%s",
                SOURCER_LOG_PREFIX,
                len(prompt),
                ", ".join(monitoring["existing_backlog_sample"]) or "none",
            )
            try:
                LOGGER.info(
                    "%s codex_run_start workspace=%s previous_session_id=%s",
                    SOURCER_LOG_PREFIX,
                    state.workspace_path,
                    state.session_id or "new",
                )
                output, session_id = self.codex_runner.run(
                    Path(state.workspace_path),
                    prompt,
                    state.session_id or None,
                )
            except Exception:
                monitoring["codex_run_status"] = "failed"
                monitoring["completed_at"] = utc_now_iso()
                monitoring["elapsed_ms"] = int((time.monotonic() - started_monotonic) * 1000)
                LOGGER.exception(
                    "%s codex_run_failed elapsed_ms=%s workspace=%s",
                    SOURCER_LOG_PREFIX,
                    monitoring["elapsed_ms"],
                    state.workspace_path,
                )
                raise
            monitoring["codex_run_status"] = "completed"
            monitoring["output_chars"] = len(output)
            LOGGER.info(
                "%s codex_run_complete output_chars=%s session_id=%s",
                SOURCER_LOG_PREFIX,
                len(output),
                session_id or state.session_id or "unknown",
            )
            state = self.session_manager.finalize_session_id(state, session_id)
            monitoring["session_id"] = state.session_id
            monitoring["session_workspace"] = state.workspace_path
            try:
                payload = extract_json_object(output)
                monitoring["json_parse_status"] = "success"
                LOGGER.info("%s json_parse_success session_id=%s", SOURCER_LOG_PREFIX, state.session_id or "unknown")
            except ValueError:
                monitoring["json_parse_status"] = "failed"
                monitoring["json_parse_error"] = "Backlog sourcer response did not contain valid JSON."
                LOGGER.warning(
                    "%s json_parse_failed session_id=%s error=%s",
                    SOURCER_LOG_PREFIX,
                    state.session_id or "unknown",
                    monitoring["json_parse_error"],
                )
                payload = {
                    "status": "failed",
                    "summary": "",
                    "backlog_items": [],
                    "error": "Backlog sourcer response did not contain valid JSON.",
                }
            payload = normalize_backlog_sourcing_payload(payload)
            raw_items = payload.get("backlog_items") if isinstance(payload.get("backlog_items"), list) else []
            monitoring["raw_backlog_items_count"] = len(raw_items)
            monitoring["raw_backlog_item_sample"] = _sample_item_labels(raw_items)
            monitoring["fallback_used"] = bool(not raw_items)
            monitoring["completed_at"] = utc_now_iso()
            monitoring["elapsed_ms"] = int((time.monotonic() - started_monotonic) * 1000)
            LOGGER.info(
                "%s complete status=%s raw_items=%s fallback_used=%s elapsed_ms=%s",
                SOURCER_LOG_PREFIX,
                str(payload.get("status") or "").strip() or "unknown",
                len(raw_items),
                monitoring["fallback_used"],
                monitoring["elapsed_ms"],
            )
            payload.setdefault("session_id", state.session_id)
            payload.setdefault("session_workspace", state.workspace_path)
            payload["monitoring"] = monitoring
            return payload

    def _build_prompt(
        self,
        *,
        findings: list[dict[str, Any]],
        scheduler_state: dict[str, Any],
        active_sprint: dict[str, Any],
        backlog_counts: dict[str, int],
        existing_backlog: list[dict[str, Any]],
    ) -> str:
        return f"""You are the internal backlog sourcing agent inside teams_runtime.

You are not a public Discord bot. You independently inspect runtime findings and propose backlog work for later execution.

Return strict JSON only with this shape:
{{
  "status": "completed|failed",
  "summary": "short Korean sourcing summary",
  "backlog_items": [
    {{
      "title": "short backlog title",
      "summary": "why this should enter backlog",
      "kind": "bug|enhancement|feature|chore",
      "scope": "clear actionable scope",
      "acceptance_criteria": ["optional acceptance criterion"],
      "milestone_title": "optional active sprint milestone when clearly relevant",
      "origin": {{}}
    }}
  ],
  "error": ""
}}

Rules:
- Only propose real future work items, not journal-only observations.
- Prefer bug when the finding indicates failure, traceback, regression, or broken behavior.
- Prefer feature for genuinely new capability requests, not generic improvements.
- Prefer enhancement for improving an existing workflow, agent, or document.
- Prefer chore for maintenance/cleanup tasks with low user-facing product impact.
- Avoid duplicates when an equivalent backlog item already exists.
- Do not emit blocked-only restatements unless there is a concrete next task for later.
- Keep each backlog item independently actionable.
- If an active sprint milestone exists, focus only on backlog items that clearly advance that milestone.
- If an active sprint milestone exists, prefer returning no backlog items over returning unrelated work.
- If an active sprint milestone exists, set `milestone_title` to that active sprint milestone on every returned item.
- Do not set `planned_in_sprint_id`; sourcer is backlog-first and planner decides sprint todo promotion.

Configured sprint id: {self.sprint_id}
Scheduler state:
{json.dumps(scheduler_state, ensure_ascii=False, indent=2)}

Active sprint summary:
{json.dumps({
    "sprint_id": active_sprint.get("sprint_id") or "",
    "sprint_name": active_sprint.get("sprint_name") or active_sprint.get("sprint_display_name") or "",
    "phase": active_sprint.get("phase") or "",
    "milestone_title": active_sprint.get("milestone_title") or "",
    "status": active_sprint.get("status") or "",
    "trigger": active_sprint.get("trigger") or "",
}, ensure_ascii=False, indent=2)}

Backlog counts:
{json.dumps(backlog_counts, ensure_ascii=False, indent=2)}

Existing active backlog:
{json.dumps(existing_backlog, ensure_ascii=False, indent=2)}

Raw findings:
{json.dumps(findings, ensure_ascii=False, indent=2)}
"""


__all__ = [
    "BacklogSourcingRuntime",
    "normalize_backlog_sourcing_payload",
]
