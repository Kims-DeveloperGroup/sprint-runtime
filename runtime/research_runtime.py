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
    RESEARCH_REPORT_LIST_FIELDS,
    RESEARCH_REASON_CODE_BLOCKED_DECISION_FAILED,
    build_research_decision_prompt,
    build_research_prompt,
    default_research_planner_guidance,
    default_research_signal,
    normalize_research_decision,
    parse_research_report,
    research_skip_summary,
    validate_source_backed_research_report,
)


DEFAULT_RESEARCH_PROFILE_DIRNAME = "chrome_profile"
DEFAULT_DEEP_RESEARCH_MODE_KEYWORDS = ("Pro", "프로", "최상위")


def _empty_research_subject_definition() -> dict[str, Any]:
    return {
        "planning_decision": "",
        "knowledge_gap": "",
        "external_boundary": "",
        "planner_impact": "",
        "candidate_subject": "",
        "research_query": "",
        "source_requirements": [],
        "rejected_subjects": [],
        "no_subject_rationale": "",
    }


def _resolve_deep_research_mode(mode: str | None) -> str | list[str]:
    return list(DEFAULT_DEEP_RESEARCH_MODE_KEYWORDS)


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
                subject_definition = _empty_research_subject_definition()
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
                            "research_subject_definition": subject_definition,
                            "research_report": {
                                "report_artifact": "",
                                "headline": "research 필요 판단 실패",
                                "planner_guidance": planner_guidance,
                                "research_subject_definition": subject_definition,
                                "backing_sources": [],
                                **{field: [] for field in RESEARCH_REPORT_LIST_FIELDS},
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
            subject_definition = dict(decision.get("research_subject_definition") or {})
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
                    "research_subject_definition": subject_definition,
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
                    subject_definition=subject_definition,
                    local_sources_checked=local_sources_checked,
                    artifact_hint=artifact_hint,
                )
                deep_research_result: Any | None = None
                response_text = ""
                parsed_report: dict[str, Any] | None = None
                failure_stage = "run_deep_research"
                try:
                    deep_research_result = run_deep_research_sync(
                        prompt,
                        app_name=effective_config.app,
                        notebook=effective_config.notebook,
                        files=list(effective_config.files),
                        mode=_resolve_deep_research_mode(effective_config.mode),
                        profile_path=effective_config.profile_path,
                        completion_timeout=effective_config.completion_timeout,
                        callback_timeout=effective_config.callback_timeout,
                        cleanup=effective_config.cleanup,
                    )
                    if not bool(getattr(deep_research_result, "completed", False)):
                        failure_stage = "await_final_report"
                        current_url = str(getattr(deep_research_result, "url", "") or "").strip()
                        location_hint = f" Last URL: {current_url}" if current_url else ""
                        raise RuntimeError(
                            "Deep research did not reach final report completion before timeout."
                            + location_hint
                        )
                    response_text = str(deep_research_result.response_text or "").strip()
                    failure_stage = "response_validation"
                    if not response_text:
                        raise RuntimeError("Deep research returned an empty report.")
                    if deep_research_result.url:
                        response_text += f"\n\n---\n**Deep Research URL:** {deep_research_result.url}\n"
                    failure_stage = "write_artifact"
                    artifact_path.parent.mkdir(parents=True, exist_ok=True)
                    artifact_path.write_text(response_text.rstrip() + "\n", encoding="utf-8")
                    failure_stage = "parse_report"
                    parsed_report = parse_research_report(response_text)
                    failure_stage = "validate_report"
                    parsed_report = validate_source_backed_research_report(
                        signal,
                        parsed_report,
                    )
                    payload["summary"] = (
                        parsed_report["headline"]
                        or "외부 research를 수행하고 planner용 source-backed guidance를 정리했습니다."
                    )
                    payload["proposals"]["research_report"] = {
                        "report_artifact": artifact_hint,
                        "research_url": deep_research_result.url or "",
                        "headline": parsed_report["headline"],
                        "planner_guidance": parsed_report["planner_guidance"],
                        "research_subject_definition": subject_definition,
                        "milestone_refinement_hints": parsed_report["milestone_refinement_hints"],
                        "problem_framing_hints": parsed_report["problem_framing_hints"],
                        "spec_implications": parsed_report["spec_implications"],
                        "todo_definition_hints": parsed_report["todo_definition_hints"],
                        "backing_reasoning": parsed_report["backing_reasoning"],
                        "backing_sources": parsed_report["backing_sources"],
                        "open_questions": parsed_report["open_questions"],
                        "effective_config": asdict(effective_config),
                    }
                    payload["artifacts"] = [artifact_hint]
                except Exception as exc:
                    artifact_written = artifact_path.exists()
                    parsed_backing_sources = (
                        parsed_report.get("backing_sources")
                        if isinstance(parsed_report, dict) and isinstance(parsed_report.get("backing_sources"), list)
                        else []
                    )
                    failure_details: dict[str, Any] = {
                        "failure_stage": failure_stage,
                        "exception_type": type(exc).__name__,
                        "artifact_written": artifact_written,
                        "parsed_backing_source_count": len(parsed_backing_sources),
                    }
                    if response_text:
                        failure_details["response_excerpt"] = response_text[:1000]
                    if artifact_written:
                        failure_details["report_artifact"] = artifact_hint
                    if deep_research_result is not None and getattr(deep_research_result, "url", None):
                        failure_details["research_url"] = str(deep_research_result.url or "")
                    if parsed_backing_sources:
                        failure_details["parsed_backing_sources"] = parsed_backing_sources[:3]

                    payload["status"] = "failed"
                    payload["summary"] = "research prepass를 완료하지 못했습니다."
                    payload["error"] = str(exc) or "research execution failed"
                    payload["artifacts"] = [artifact_hint] if artifact_written else []
                    payload["proposals"]["research_report"] = {
                        "report_artifact": artifact_hint if artifact_written else "",
                        "research_url": (
                            str(getattr(deep_research_result, "url", "") or "")
                            if deep_research_result is not None
                            else ""
                        ),
                        "headline": "external research 실행 실패",
                        "planner_guidance": planner_guidance,
                        "research_subject_definition": subject_definition,
                        "backing_sources": [],
                        **{field: [] for field in RESEARCH_REPORT_LIST_FIELDS},
                        "open_questions": [],
                        "failure_details": failure_details,
                        "effective_config": asdict(effective_config),
                    }
            else:
                payload["summary"] = research_skip_summary(signal)
                payload["proposals"]["research_report"] = {
                    "report_artifact": "",
                    "research_url": "",
                    "headline": "외부 research 불필요",
                    "planner_guidance": planner_guidance,
                    "research_subject_definition": subject_definition,
                    "backing_sources": [],
                    **{field: [] for field in RESEARCH_REPORT_LIST_FIELDS},
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

        def resolve_profile_path(raw_value: str | None) -> str:
            normalized = str(raw_value or "").strip()
            if normalized:
                profile_path = Path(normalized).expanduser()
                if profile_path.is_absolute():
                    return str(profile_path)
                return str(self.paths.project_workspace_root / profile_path)
            return str(self.paths.project_workspace_root / DEFAULT_RESEARCH_PROFILE_DIRNAME)

        return ResearchRuntimeConfig(
            app=choose_text("app", base.app),
            notebook=choose_text("notebook", base.notebook),
            files=files_value,
            mode=choose_text("mode", base.mode),
            profile_path=resolve_profile_path(choose_text("profile_path", base.profile_path)),
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
        return normalize_research_decision(raw_payload, request_record=request_record), resolved_session_id


__all__ = ["ResearchAgentRuntime"]
