from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from libs.gemini.deep_research import run_deep_research_sync
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.models import (
    MessageEnvelope,
    RequestRecord,
    ResearchRuntimeConfig,
    RoleResult,
    RoleRuntimeConfig,
    RoleSessionState,
)
from teams_runtime.runtime.base_runtime import (
    RoleAgentRuntime,
    _collapse_whitespace,
    normalize_role_payload,
)
from teams_runtime.runtime.codex_runner import extract_json_object
from teams_runtime.workflows.roles.research import (
    RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED,
    build_research_decision_prompt,
    build_research_decision_retry_prompt,
    build_research_prompt,
    default_research_planner_guidance,
    default_research_signal,
    is_research_reason_code_schema_error,
    normalize_research_decision,
    parse_research_report,
    research_skip_summary,
)


class ResearchAgentRuntime(RoleAgentRuntime):
    def __init__(
        self,
        *,
        paths: RuntimePaths,
        role: str,
        sprint_id: str,
        runtime_config: RoleRuntimeConfig,
        research_defaults: ResearchRuntimeConfig,
        agent_root: Path | None = None,
        session_identity: str | None = None,
    ):
        super().__init__(
            paths=paths,
            role=role,
            sprint_id=sprint_id,
            runtime_config=runtime_config,
            agent_root=agent_root,
            session_identity=session_identity,
        )
        self.research_defaults = research_defaults

    def run_task(self, envelope: MessageEnvelope, request_record: RequestRecord) -> RoleResult:
        with self._run_lock:
            current_sprint_id = self._resolve_request_sprint_id(envelope, request_record)
            session_manager = self._session_manager_for_sprint(current_sprint_id)
            state = session_manager.ensure_session()
            request_id = str(request_record.get("request_id") or envelope.request_id or "").strip() or "unknown"
            artifact_path = self.paths.sprint_research_file(current_sprint_id, request_id)
            artifact_hint = self._workspace_relative_path(artifact_path)
            local_sources_checked = self._local_sources_checked(envelope, request_record, current_sprint_id=current_sprint_id)
            effective_config = self._merge_research_config(envelope, request_record)
            active_session_id = state.session_id or None
            try:
                decision, resolved_session_id = self._run_research_decision(
                    envelope,
                    request_record,
                    state=state,
                    local_sources_checked=local_sources_checked,
                )
                active_session_id = resolved_session_id or active_session_id
            except Exception as exc:
                signal = default_research_signal(reason_code=RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED)
                planner_guidance = default_research_planner_guidance(
                    signal,
                    local_sources_checked=local_sources_checked,
                )
                payload = normalize_role_payload(
                    {
                        "request_id": request_record.get("request_id") or request_id,
                        "role": self.role,
                        "status": "blocked",
                        "summary": "research 필요 판단을 완료하지 못했습니다.",
                        "insights": [],
                        "proposals": {
                            "research_signal": signal,
                            "research_report": {
                                "report_artifact": "",
                                "headline": "research 필요 판단 실패",
                                "planner_guidance": planner_guidance,
                                "backing_sources": [],
                                "open_questions": [],
                                "effective_config": asdict(effective_config),
                            },
                        },
                        "artifacts": [],
                        "error": str(exc) or "research decision failed",
                        "session_id": active_session_id or "",
                        "session_workspace": state.workspace_path,
                    }
                )
                state = session_manager.finalize_session_id(state, active_session_id)
                payload["session_id"] = state.session_id
                payload["session_workspace"] = state.workspace_path
                return payload

            signal = dict(decision.get("signal") or {})
            planner_guidance = str(decision.get("planner_guidance") or "").strip() or default_research_planner_guidance(
                signal,
                local_sources_checked=local_sources_checked,
            )
            payload: dict[str, Any] = {
                "request_id": request_record.get("request_id") or request_id,
                "role": self.role,
                "status": "completed",
                "summary": "",
                "insights": [],
                "proposals": {
                    "research_signal": signal,
                },
                "artifacts": [],
                "error": "",
                "session_id": active_session_id or "",
                "session_workspace": state.workspace_path,
            }
            if signal["needed"]:
                prompt = build_research_prompt(
                    envelope,
                    request_record,
                    signal=signal,
                    local_sources_checked=local_sources_checked,
                    artifact_hint=artifact_hint,
                )
                try:
                    result = run_deep_research_sync(
                        prompt,
                        app_name=effective_config.app,
                        notebook=effective_config.notebook,
                        files=list(effective_config.files),
                        mode=effective_config.mode,
                        profile_path=effective_config.profile_path,
                        completion_timeout=effective_config.completion_timeout,
                        callback_timeout=effective_config.callback_timeout,
                        cleanup=effective_config.cleanup,
                    )
                    response_text = str(result.response_text or "").strip()
                    if not response_text:
                        raise RuntimeError("Deep research returned an empty report.")
                    artifact_path.parent.mkdir(parents=True, exist_ok=True)
                    artifact_path.write_text(response_text.rstrip() + "\n", encoding="utf-8")
                    parsed_report = parse_research_report(response_text)
                    payload["summary"] = (
                        parsed_report["headline"]
                        or "외부 research를 수행하고 planner용 source-backed guidance를 정리했습니다."
                    )
                    payload["proposals"]["research_report"] = {
                        "report_artifact": artifact_hint,
                        "headline": parsed_report["headline"],
                        "planner_guidance": parsed_report["planner_guidance"],
                        "backing_sources": parsed_report["backing_sources"],
                        "open_questions": parsed_report["open_questions"],
                        "effective_config": asdict(effective_config),
                    }
                    payload["artifacts"] = [artifact_hint]
                except Exception as exc:
                    payload["status"] = "failed"
                    payload["summary"] = "research prepass를 완료하지 못했습니다."
                    payload["error"] = str(exc) or "research execution failed"
                    payload["proposals"]["research_report"] = {
                        "report_artifact": "",
                        "headline": "external research 실행 실패",
                        "planner_guidance": planner_guidance,
                        "backing_sources": [],
                        "open_questions": [],
                        "effective_config": asdict(effective_config),
                    }
            else:
                payload["summary"] = research_skip_summary(signal)
                payload["proposals"]["research_report"] = {
                    "report_artifact": "",
                    "headline": "외부 research 불필요",
                    "planner_guidance": planner_guidance,
                    "backing_sources": [],
                    "open_questions": [],
                    "effective_config": asdict(effective_config),
                }
            payload = normalize_role_payload(payload)
            state = session_manager.finalize_session_id(state, active_session_id)
            payload["session_id"] = state.session_id
            payload["session_workspace"] = state.workspace_path
            return payload

    def _workspace_relative_path(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.paths.workspace_root.resolve()))
        except ValueError:
            return str(path)

    def _merge_research_config(
        self,
        envelope: MessageEnvelope,
        request_record: RequestRecord,
    ) -> ResearchRuntimeConfig:
        request_params = dict(request_record.get("params") or {}) if isinstance(request_record.get("params"), dict) else {}
        raw_override = request_params.get("research")
        if not isinstance(raw_override, dict):
            raw_override = envelope.params.get("research") if isinstance(envelope.params.get("research"), dict) else {}
        base = self.research_defaults
        override = dict(raw_override or {})
        files_override = override.get("files")
        if files_override is None:
            files_value = base.files
        elif isinstance(files_override, list):
            files_value = tuple(str(item).strip() for item in files_override if str(item).strip())
        else:
            files_value = base.files

        def choose_text(key: str, current: str | None) -> str | None:
            value = override.get(key)
            if value is None:
                return current
            normalized = str(value).strip()
            return normalized or current

        def choose_timeout(key: str, current: float) -> float:
            value = override.get(key)
            if value in (None, ""):
                return current
            try:
                normalized = float(value)
            except (TypeError, ValueError):
                return current
            return normalized if normalized > 0 else current

        return ResearchRuntimeConfig(
            app=choose_text("app", base.app),
            notebook=choose_text("notebook", base.notebook),
            files=files_value,
            mode=choose_text("mode", base.mode),
            profile_path=choose_text("profile_path", base.profile_path),
            completion_timeout=choose_timeout("completion_timeout", base.completion_timeout),
            callback_timeout=choose_timeout("callback_timeout", base.callback_timeout),
            cleanup=bool(override.get("cleanup")) if "cleanup" in override else base.cleanup,
        )

    def _request_reference_text(self, envelope: MessageEnvelope, request_record: RequestRecord) -> str:
        proposals = dict(request_record.get("result") or {}).get("proposals") if isinstance(request_record.get("result"), dict) else {}
        planning_hint = ""
        if isinstance(proposals, dict):
            planning_hint = json.dumps(proposals, ensure_ascii=False, sort_keys=True)
        parts = [
            str(request_record.get("intent") or "").strip(),
            str(request_record.get("scope") or "").strip(),
            str(request_record.get("body") or "").strip(),
            str(envelope.scope or "").strip(),
            str(envelope.body or "").strip(),
            planning_hint,
        ]
        return _collapse_whitespace(" ".join(part for part in parts if str(part).strip()))

    def _local_sources_checked(
        self,
        envelope: MessageEnvelope,
        request_record: RequestRecord,
        *,
        current_sprint_id: str,
    ) -> list[str]:
        checked = ["request.scope", "request.body"]
        for item in list(request_record.get("artifacts") or []) + list(envelope.artifacts or []):
            candidate = str(item or "").strip()
            if not candidate:
                continue
            checked.append(candidate)
        current_sprint = self.paths.current_sprint_file
        if current_sprint.exists():
            checked.append(self._workspace_relative_path(current_sprint))
        for filename in ("milestone.md", "plan.md", "spec.md", "todo_backlog.md", "iteration_log.md"):
            candidate = self.paths.sprint_artifact_file(current_sprint_id, filename)
            if candidate.exists():
                checked.append(self._workspace_relative_path(candidate))
        deduped: list[str] = []
        for item in checked:
            if item not in deduped:
                deduped.append(item)
        return deduped[:8]

    def _run_research_decision(
        self,
        envelope: MessageEnvelope,
        request_record: RequestRecord,
        *,
        state: RoleSessionState,
        local_sources_checked: list[str],
    ) -> tuple[dict[str, Any], str | None]:
        prompt = build_research_decision_prompt(
            envelope,
            request_record,
            local_sources_checked=local_sources_checked,
        )
        output, resolved_session_id = self.codex_runner.run(
            Path(state.workspace_path),
            prompt,
            state.session_id or None,
            bypass_sandbox=self._request_requires_default_bypass(envelope, request_record),
        )
        raw_payload = extract_json_object(output)
        try:
            return normalize_research_decision(raw_payload), resolved_session_id
        except ValueError as exc:
            if not is_research_reason_code_schema_error(exc):
                raise
            retry_output, retry_session_id = self.codex_runner.run(
                Path(state.workspace_path),
                build_research_decision_retry_prompt(prompt, str(exc)),
                None,
                bypass_sandbox=self._request_requires_default_bypass(envelope, request_record),
            )
            retry_payload = extract_json_object(retry_output)
            return normalize_research_decision(retry_payload), retry_session_id or resolved_session_id


__all__ = ["ResearchAgentRuntime"]
