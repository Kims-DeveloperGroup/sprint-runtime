from __future__ import annotations

from typing import Any


ALLOWED_PLANNER_BACKLOG_KINDS = {"bug", "enhancement", "feature", "chore"}


def _normalize_string_list_field(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return []


def normalize_planner_backlog_candidate(raw_item: Any) -> dict[str, Any] | None:
    if isinstance(raw_item, str):
        title = str(raw_item).strip()
        if not title:
            return None
        return {
            "title": title,
            "summary": title,
            "kind": "enhancement",
            "scope": title,
            "acceptance_criteria": [],
            "origin": {},
        }
    if not isinstance(raw_item, dict):
        return None

    normalized = dict(raw_item)
    title = str(
        normalized.get("title")
        or normalized.get("scope")
        or normalized.get("summary")
        or ""
    ).strip()
    if not title:
        return None

    kind = str(normalized.get("kind") or "enhancement").strip().lower().replace(" ", "_")
    if kind in {"new_feature", "new-feature"}:
        kind = "feature"
    if kind not in ALLOWED_PLANNER_BACKLOG_KINDS:
        kind = "enhancement"

    normalized["title"] = title
    normalized["summary"] = str(normalized.get("summary") or title).strip()
    normalized["scope"] = str(normalized.get("scope") or title).strip()
    normalized["kind"] = kind
    normalized["acceptance_criteria"] = _normalize_string_list_field(normalized.get("acceptance_criteria"))
    origin = normalized.get("origin")
    normalized["origin"] = dict(origin or {}) if isinstance(origin, dict) else {}
    return normalized


def normalize_planner_backlog_write(raw_write: Any) -> dict[str, Any] | None:
    if not isinstance(raw_write, dict):
        return None

    normalized = dict(raw_write)
    backlog_id = str(normalized.get("backlog_id") or "").strip()
    artifact_path = str(
        normalized.get("artifact_path")
        or normalized.get("artifact")
        or normalized.get("path")
        or ""
    ).strip()
    if not backlog_id and not artifact_path:
        return None

    status = str(normalized.get("status") or "").strip().lower()
    changed_fields = _normalize_string_list_field(
        normalized.get("changed_fields")
        if normalized.get("changed_fields") is not None
        else normalized.get("fields")
    )

    if backlog_id:
        normalized["backlog_id"] = backlog_id
    else:
        normalized.pop("backlog_id", None)
    if artifact_path:
        normalized["artifact_path"] = artifact_path
    else:
        normalized.pop("artifact_path", None)
    if status:
        normalized["status"] = status
    else:
        normalized.pop("status", None)
    if changed_fields:
        normalized["changed_fields"] = changed_fields
    else:
        normalized.pop("changed_fields", None)
    normalized.pop("artifact", None)
    normalized.pop("path", None)
    normalized.pop("fields", None)
    return normalized


def normalize_planner_proposals(proposals: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    normalized = dict(proposals) if isinstance(proposals, dict) else {}
    validation_notes: list[str] = []

    raw_items: list[Any] = []
    if isinstance(normalized.get("backlog_items"), list):
        raw_items.extend(normalized.get("backlog_items") or [])
    if isinstance(normalized.get("backlog_item"), (dict, str)):
        raw_items.append(normalized.get("backlog_item"))
        validation_notes.append("normalized planner backlog_item to backlog_items")
    if isinstance(normalized.get("planned_backlog_updates"), list):
        raw_items.extend(normalized.get("planned_backlog_updates") or [])
        validation_notes.append("normalized planner planned_backlog_updates to backlog_items")

    backlog_items: list[dict[str, Any]] = []
    seen_backlog_items: set[str] = set()
    for raw_item in raw_items:
        candidate = normalize_planner_backlog_candidate(raw_item)
        if candidate is None:
            continue
        dedupe_key = "::".join(
            [
                str(candidate.get("title") or "").strip().lower(),
                str(candidate.get("scope") or "").strip().lower(),
                str(candidate.get("kind") or "").strip().lower(),
            ]
        )
        if dedupe_key in seen_backlog_items:
            continue
        seen_backlog_items.add(dedupe_key)
        backlog_items.append(candidate)
    if backlog_items:
        normalized["backlog_items"] = backlog_items
        if len(backlog_items) == 1:
            normalized["backlog_item"] = backlog_items[0]
    else:
        normalized.pop("backlog_items", None)
        normalized.pop("backlog_item", None)
    normalized.pop("planned_backlog_updates", None)

    raw_writes: list[Any] = []
    if isinstance(normalized.get("backlog_writes"), list):
        raw_writes.extend(normalized.get("backlog_writes") or [])
    if isinstance(normalized.get("backlog_write"), dict):
        raw_writes.append(normalized.get("backlog_write"))
        validation_notes.append("normalized planner backlog_write to backlog_writes")

    backlog_writes: list[dict[str, Any]] = []
    seen_backlog_writes: set[str] = set()
    for raw_write in raw_writes:
        receipt = normalize_planner_backlog_write(raw_write)
        if receipt is None:
            continue
        dedupe_key = str(receipt.get("backlog_id") or receipt.get("artifact_path") or "").strip().lower()
        if dedupe_key and dedupe_key in seen_backlog_writes:
            continue
        if dedupe_key:
            seen_backlog_writes.add(dedupe_key)
        backlog_writes.append(receipt)
    if backlog_writes:
        normalized["backlog_writes"] = backlog_writes
        if len(backlog_writes) == 1:
            normalized["backlog_write"] = backlog_writes[0]
    else:
        normalized.pop("backlog_writes", None)
        normalized.pop("backlog_write", None)

    return normalized, validation_notes


def build_planner_role_rules(team_workspace_hint: str) -> str:
    return f"""

Planner-specific rules:
- Treat `Current request.artifacts` as planning reference inputs. If they point to local docs or saved sprint attachments such as `shared_workspace/sprints/.../attachments/...`, inspect them directly and extract requirements, constraints, dependencies, and acceptance criteria into your planning output.
- If sprint kickoff context is preserved separately in `kickoff.md` or `Current request.params.kickoff_*`, treat that kickoff content as immutable source-of-truth and add derived framing in milestone/plan/spec outputs instead of rewriting the original kickoff brief.
- If `Current request.result.proposals.research_report` or `research_prepass` exists, treat it as a planning input with leverage, not a footnote. Use its `milestone_refinement_hints`, `problem_framing_hints`, `spec_implications`, `todo_definition_hints`, `backing_reasoning`, and `backing_sources` when refining milestones, writing specs, and defining backlog/todos.
- If `research_subject_definition` exists, read `planning_decision`, `knowledge_gap`, `external_boundary`, `planner_impact`, `source_requirements`, and `rejected_subjects` before framing the planning problem.
- Treat the user-provided milestone as an abstract entry point. Do not adopt it unchanged unless you also provide developed problem framing, source-backed rationale, and traceable `research_refs`.
- For sprint-relevant planning work such as `initial`, `ongoing_review`, sprint continuity, or sprint-related backlog shaping, inspect `./shared_workspace/sprint_history/index.md` first and then only the smallest relevant prior sprint history file(s) under `./shared_workspace/sprint_history/` to recover carry-over work, repeated blockers, milestone continuity, and already-closed decisions. Use prior sprint history as comparative evidence only; the current request, active sprint docs, and kickoff context remain authoritative. If no relevant history exists, proceed without blocking.
- If `Current request.artifacts`, `params.verified_source_artifact`, or the request body/scope point to an existing local planning/spec Markdown file, inspect that file before blocking for a missing source document.
- If a referenced attachment exists but is not directly readable in the current session, say that explicitly in your result and keep the attachment visible as a referenced planning input instead of silently ignoring it.
- If a confirmed planning/spec document already contains actionable next steps, backlog bundles, or implementation phases, convert them into `proposals.backlog_item` or `proposals.backlog_items` with concrete title/scope/summary/acceptance_criteria instead of stopping at prose-only confirmation.
- For action-required requests based on an existing plan document, do not stop after document verification when backlog decomposition is already possible.
- Treat one backlog item as one `independently reviewable implementation slice`.
- If a candidate backlog item spans multiple subsystems, contracts, phases, deliverables, or separate architect/developer/qa review tracks, split it before returning backlog proposals.
- Do not copy prior local planner history or shared planning logs that happened to use three items; current source material determines the count.
- Do not default sprint planning or backlog decomposition to three items. Return the exact number of backlog items/todos justified by the milestone and source material after splitting work into smaller reviewable slices; one item is valid, and more than three is valid when the milestone genuinely requires it.
- For sprint docs inside the teams runtime workspace, modify `./shared_workspace/...` directly instead of going through `./workspace/...`.
- For planner-owned runtime persistence, prefer `./.teams_runtime/...` over `./workspace/teams_generated/.teams_runtime/...` when the session exposes that local path.
- For planner-owned backlog persistence, prefer the helper in `teams_runtime.workflows.state.backlog_store` with canonical `backlog_items` payloads instead of inventing ad-hoc proposal keys or relying on orchestrator-side merges.
- After backlog persistence succeeds, return `proposals.backlog_writes` receipts with the affected `backlog_id` values and any touched artifact paths. `proposals.backlog_item` / `proposals.backlog_items` remain rationale and planning context, not persistence instructions for orchestrator.
- Do not choose `next_role`. Orchestrator owns routing selection after your report, so leave clear execution-ready summary, backlog proposals, acceptance criteria, and downstream-relevant context instead.
- If `Current request.params._teams_kind == "blocked_backlog_review"`, inspect the current blocked backlog candidates, explicitly decide which items remain blocked versus reopen to `pending`, clear blocker fields when reopening, and do not mark reopened work `selected` in this request.
- When `Current request.params.workflow` exists, treat planner as the sole planning owner. Use `proposals.workflow_transition` to either request one advisory specialist pass from `designer` or `architect`, or finalize planning so implementation can advance.
- For Discord/operator message changes, classify the work first: `renderer-only` applies only when semantic meaning, copy hierarchy, user decision path, and CTA are already fixed, and the change is limited to rendering/contract repair such as escaping, field mapping, compact-contract wiring, or truncation fixes.
- If the request changes reading order, omission tolerance, title/summary/body/action priority, or CTA wording/tone, treat it as designer advisory work instead of renderer-only implementation.
- Treat `readability-only` as a non-advisory bucket only when the same reading order still holds and the fix merely makes the already-approved structure easier to scan.
- For Discord message layers, use distinct trigger language: `relay` when immediate status/warning/action priority changes, `handoff` when the next role's first-read context changes, and `summary` when long-term keep-vs-omit rules change.
- If a request mixes rendering repair with user-facing message judgment, do not keep both concerns inside one execution slice. Split it into `technical slice` and `designer advisory slice`, and state whether `technical slice 선행` or `designer advisory 선행` applies before handing work forward.
- When planner opens designer advisory for message judgment, preserve the minimum evidence needed for the split: before/after message example or intended output hierarchy, plus one line saying whether the problem is `표시 오류` or `사용자 판단 혼선`.
- If `Current request.params.sprint_phase == "initial"` and `Current request.params.initial_phase_step` is set, treat the request as a planner-only substep:
  - `milestone_refinement`: preserve the original sprint kickoff brief/requirements/reference artifacts, then refine the milestone title/framing and update milestone-facing docs only.
    If source-backed research exists, return `proposals.sprint_plan_update.revised_milestone_title`, `refinement_rationale`, `problem_framing`, and `research_refs`.
  - `artifact_sync`: update plan/spec/iteration-log artifacts only.
  - `backlog_definition`: derive sprint-relevant backlog from the current milestone, kickoff requirements, research prepass, and `spec.md`; create or reopen backlog items before any selection. Backlog zero is invalid. Every backlog item must include concrete `acceptance_criteria` plus `origin.milestone_ref`, `origin.requirement_refs`, and `origin.spec_refs`. If source-backed research exists, every sprint-relevant backlog item must also include `origin.research_refs`.
  - `backlog_prioritization`: persist prioritization for the already-defined sprint-relevant backlog with `priority_rank` and `milestone_title`, but do not set `planned_in_sprint_id` or execution todos yet.
  - `todo_finalization`: finalize the execution-ready sprint backlog set and persist `planned_in_sprint_id` for chosen items.
- During initial-phase substeps before `todo_finalization`, do not open execution and do not leave `planned_in_sprint_id`/selected todo state behind.
- During `backlog_definition`, treat persisted backlog as historical input, not as a hard cap. If milestone/requirements/spec are not fully covered, create or reopen sprint-relevant backlog instead of returning backlog zero.
- During `initial` planning and `ongoing_review`, reconsider blocked backlog when it is relevant to the sprint. Only reopened `pending` items may be promoted into the sprint; blocked items stay out of selection until reopened.
- When you create or revise backlog/todo titles, prefer functional wording that says what changed behaviorally or contractually. Avoid meta activity labels such as `정리`, `구체화`, `반영`, `문서/라우팅/회귀 테스트 반영`, or `prompt 개선` unless the functional change cannot be stated more concretely.
- Planner owns backlog/current-sprint/sprint-planning surfaces such as `shared_workspace/backlog.md`, `completed_backlog.md`, `current_sprint.md`, and sprint docs like `milestone.md`, `plan.md`, `spec.md`, `todo_backlog.md`, `iteration_log.md`.
- If the work is a document/planning-only clarification on those surfaces, close it in planning instead of opening implementation for architect/developer/qa.
- Do not report planning complete unless the relevant planner-owned docs were actually created or updated and their paths are present in `artifacts`.
- If QA reopens a request for spec/acceptance mismatch, rewrite the affected planning docs first and only then report planner finalization.
- If `Current request.params._teams_kind == "sprint_closeout_report"`, inspect the persisted sprint evidence in `Current request.artifacts` and return `proposals.sprint_report` with this shape:
  - `headline`: one-line sprint significance
  - `changes`: list of objects with `title`, `why`, `what_changed`, `meaning`, `how`, optional `artifacts`, optional `request_ids`
  - `timeline`: short fact-based lines
  - `agent_contributions`: optional list of `{{role, summary, artifacts}}`
  - `issues`: optional short lines
  - `achievements`: optional short lines
  - `highlight_artifacts`: optional artifact lines
- In `sprint_closeout_report`, do not mutate backlog or sprint state. Base the draft on persisted request JSON, role reports, sprint docs, and linked artifacts only.
"""


__all__ = [
    "build_planner_role_rules",
    "normalize_planner_backlog_candidate",
    "normalize_planner_backlog_write",
    "normalize_planner_proposals",
]
