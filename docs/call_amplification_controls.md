# Call Amplification Controls

This document describes the runtime controls introduced for optimization issue #2:

- independent model and reasoning tiers for internal helper agents
- a bounded architect review-cycle budget
- a separate bounded workflow-reopen budget
- benchmark provenance for every effective model tier

The controls reduce avoidable model cost while preserving the public-role workflow and the evidence needed to evaluate quality.

## Why These Calls Matter

The internal `parser`, `sourcer`, and `version_controller` agents perform narrow, high-frequency tasks. Before independent configuration, all three inherited the orchestrator model and reasoning level. That made a routine classification, milestone sourcing pass, or commit check use the same tier as broad orchestration reasoning.

Review and reopen paths can amplify this baseline further. A single implementation todo may traverse:

```text
architect -> developer -> architect -> developer -> qa
```

If review or QA repeatedly requests revisions, each new handoff adds another model call and carries more accumulated context. The two workflow budgets provide deterministic upper bounds on that amplification.

## Internal Helper Tiers

Newly scaffolded workspaces use these defaults:

| Agent | Model | Reasoning | Workload rationale |
|---|---|---|---|
| `parser` | `gpt-5.4-mini` | `low` | Narrow intent normalization and status classification |
| `sourcer` | `gpt-5.4-mini` | `medium` | Goal and milestone framing needs more synthesis than classification |
| `version_controller` | `gpt-5.4-mini` | `low` | Constrained Git inspection and policy-guided commit execution |

Public role defaults are unchanged. In particular, planner, architect, developer, QA, and orchestrator quality settings do not change as part of helper tiering.

The helper default uses [`gpt-5.4-mini`](https://developers.openai.com/api/docs/models/gpt-5.4-mini) because OpenAI positions it as a faster, efficient model for high-volume coding and subagent workloads. This is a rollout hypothesis, not a guarantee of equivalent quality, so the benchmark and rollback gates below remain mandatory.

The configured tiers live in `team_runtime.yaml`:

```yaml
internal_agent_defaults:
  parser:
    model: "gpt-5.4-mini"
    reasoning: "low"
  sourcer:
    model: "gpt-5.4-mini"
    reasoning: "medium"
  version_controller:
    model: "gpt-5.4-mini"
    reasoning: "low"
```

Change one helper with the CLI:

```bash
python -m teams_runtime config internal set \
  --workspace-root teams_generated \
  --agent sourcer \
  --model gpt-5.4-mini \
  --reasoning medium
python -m teams_runtime restart \
  --workspace-root teams_generated \
  --agent orchestrator
```

The helpers are orchestrator-local runtimes, so an orchestrator restart is required after a change.

## Backward Compatibility

An existing workspace does not need to add `internal_agent_defaults` immediately. If the section or an individual helper entry is absent, that helper inherits the effective orchestrator model and reasoning level.

For example, this legacy configuration remains valid:

```yaml
role_defaults:
  orchestrator:
    model: "gpt-5.5"
    reasoning: "medium"
```

Its effective helper configuration is:

```yaml
internal_agent_defaults:
  parser: {model: "gpt-5.5", reasoning: "medium"}
  sourcer: {model: "gpt-5.5", reasoning: "medium"}
  version_controller: {model: "gpt-5.5", reasoning: "medium"}
```

This fallback preserves legacy behavior. Running `config internal set` creates only the requested override; omitted fields continue to inherit during loading.

## Review-Cycle Budget

`implementation_review_cycle_limit` limits architect implementation reviews. The default is `3`.

This lowers the default architect-review entry ceiling from `20` to `3`: 17 fewer possible review entries, or an 85% reduction in that structural bound. Actual call savings depend on how often work would otherwise revise, and must be measured rather than inferred from the ceiling alone.

A review cycle is counted when the workflow routes developer output to `architect_review`. Initial architecture guidance is not a review cycle. When the architect is already processing the review at the configured limit, an explicit continuation to another developer revision is blocked instead of opening another implementation loop. A successful review may still advance to QA at the limit.

Configure it in the generated orchestrator policy:

```yaml
workflow_contract:
  implementation_review_cycle_limit: 3
```

## Reopen Budget

`implementation_reopen_limit` is an independent cap for orchestrator-governed `reopen` transitions. The default is `3`.

The counter increments once when a valid reopen request is accepted and routed. Categories include `scope`, `ux`, `architecture`, `implementation`, and `verification`. Provider retries and contract-repair attempts are not reopens and do not affect this counter.

The limit is checked before the next model handoff. With a limit of `3`, the first three reopen transitions are routed; the fourth is terminally blocked and requires operator intervention.

Configure it beside the review limit:

```yaml
workflow_contract:
  implementation_review_cycle_limit: 3
  implementation_reopen_limit: 3
```

The orchestrator copies both limits into each new internal request's persisted workflow state. Restart the orchestrator after changing the policy. Existing in-flight requests retain their persisted limits so a deployment does not silently change an active workflow's contract.

### Reopen Example

Input state and QA transition:

```json
{
  "workflow": {
    "phase": "validation",
    "step": "qa_validation",
    "reopen_count": 3,
    "reopen_limit": 3
  },
  "transition": {
    "outcome": "reopen",
    "target_phase": "implementation",
    "target_step": "developer_revision",
    "reopen_category": "verification"
  }
}
```

Result before any fourth developer call:

```json
{
  "next_role": "",
  "terminal_status": "blocked",
  "workflow_state": {
    "phase": "validation",
    "step": "qa_validation",
    "phase_status": "blocked",
    "reopen_count": 3,
    "reopen_limit": 3,
    "reopen_category": "verification"
  }
}
```

The persisted terminal summary includes the observed count and configured limit.

## Benchmark And Telemetry Evidence

The sprint benchmark copies both `role_defaults` and `internal_agent_defaults` into every isolated arm. Its source configuration hash includes the effective helper values, including inherited values from a legacy config. Reports record a single `runtime_model_map` containing public roles and internal helpers.

This prevents two runs with different helper tiers from being treated as the same configuration. It also makes per-agent telemetry groups directly reconcilable with benchmark provenance.

For a controlled before/after experiment:

1. Keep the scenario, source revision, repetition count, prompt-context policy, and public role tiers fixed.
2. Run the baseline with helpers set to the orchestrator tier and higher workflow budgets.
3. Run the candidate with the scaffold helper tiers and three-call budgets.
4. Compare invocations per completed todo, input/output tokens, estimated cost, p95 latency, repair rate, reopen count, completion status, and QA evidence.
5. Treat a lower-cost run as acceptable only when completion and QA evidence remain equivalent.

## Rollout And Rollback

Roll out one workspace at a time and inspect helper-group telemetry after representative sprints. If helper quality regresses, raise only the affected helper's reasoning level or model before changing public roles.

To restore the legacy helper behavior, set each helper to the current orchestrator model and reasoning level. To relax workflow limits during an incident, set explicit higher integers in the workspace policy and restart the orchestrator. Keep a finite bound so a malformed workflow cannot reopen indefinitely.

Do not compare runs whose model maps or source configuration hashes differ in unplanned ways. The benchmark report now exposes those differences for this reason.
