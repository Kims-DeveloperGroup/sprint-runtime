---
name: agent_utilization
description: Use this skill inside the orchestrator agent workspace when the task is to choose the best agent for the next step, govern routing quality, and shape delegation around each agent's role, skills, strengths, and behavior.
---

# Agent Utilization Skill

## When To Use

Use this skill when orchestrator is deciding who should handle the next step, especially for:

- planner-to-execution routing after planning completes
- selecting the next role centrally from request state, policy, and capability signals
- choosing between designer, architect, developer, and qa for action-required work
- deciding whether planner should keep ownership or execution should continue
- shaping a handoff so the selected role uses its strongest skills and behavior

Do not use this skill to do the role-specific work itself.

## Read First

- `Current request`
- `Current request.result`
- `Current request.events`
- the latest role output
- sibling `policy.yaml` in this skill directory
- local orchestrator capability reference below

{ORCHESTRATOR_CAPABILITY_REFERENCE}

## Workflow

1. Identify the real next need.
   Decide whether the request still needs planning, design, architecture, implementation, qa, or an internal agent step.
   If `Current request.params.workflow` exists, respect its current phase and step before considering general capability scoring.
2. Load the routing policy.
   Treat sibling `policy.yaml` as the machine-readable routing and scoring authority, and treat this `SKILL.md` as the human operating guide for that policy.
   Read the policy guardrails first: `user_intake`, `sourcer_review`, `planning_resume`, `sprint_initial_default`, `planner_reentry_requires_explicit_signal`, `verification_result_terminal`, and `ignore_non_planner_backlog_proposals_for_routing`.
3. Match the need to the best role.
   Use ownership boundaries, preferred skills, strongest domains, behavior traits, sprint phase fit, and request-state fit.
4. Score only the allowed candidates.
   Apply workflow policy bounds first, then compare only the allowed candidates using the skill policy's scoring weights and signal matches.
5. Govern the handoff centrally.
   Select the best allowed next role from request state, policy, and capability evidence, then record why that role was chosen.
6. Make delegation concrete.
   Include policy source, phase/state fit, selected strength, suggested skills, expected behavior, and score evidence in the routing context and handoff.
7. Preserve ownership boundaries.
   Reinforce planner-owned backlog persistence and version_controller-owned commit execution while still keeping orchestrator in charge of workflow.
8. Enforce the standard sprint collaboration path.
   Use planner-owned planning, bounded advisory passes, mandatory architect guidance before developer work, mandatory architect review before QA, and orchestrator-chosen reopen routing.

## Guardrails

- Do not consume role-local routing hints as routing authority.
- Do not bypass a workflow-managed phase/step contract just because summary prose mentions another role.
- Do not let implementation go to developer when the real need is still UX or architecture shaping.
- Do not bypass planner for backlog-management ownership.
- Do not reopen routing from non-planner backlog proposals or terminal verification results.
- Do not route commit work away from version_controller.
- Do not leave the reason for role selection implicit.
