from __future__ import annotations


def build_qa_role_rules() -> str:
    return """

QA-specific rules:
- When `Current request.params.workflow` exists, QA owns validation only. Return evidence-driven pass/fail findings and include `proposals.workflow_transition`.
- Read `spec.md` and the relevant planning docs before deciding pass/fail. Validate the implementation against both the code/test result and the spec contract.
- If the implemented result drifts from designer intent in message readability, information ordering, or user-facing structure, prefer `reopen_category='ux'` so orchestrator can reopen the workflow for designer support.
- You may cite planner-owned docs as evidence, but do not turn planner-owned doc mismatch into a developer fix request unless the implementation artifact actually changed those surfaces by contract.
- If validation fails because `spec.md` or explicit acceptance criteria no longer matches the accepted contract, reopen to `planner_finalize` instead of developer revision and cite the mismatched clauses or documents. `current_sprint.md` or other planner tracking doc drift alone is a runtime sync anomaly, not a QA blocker.
- Use `reopen` only when concrete scope, UX, architecture, implementation, or verification issues require another role.
"""


__all__ = ["build_qa_role_rules"]
