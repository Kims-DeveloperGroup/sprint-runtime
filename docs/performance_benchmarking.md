# Full-Sprint Performance Benchmarking

This guide defines the repeatable integration benchmark used to quantify model-call
cost and performance changes in `teams_runtime`. It is intentionally separate from
the README because it describes an operator-only live experiment, not normal runtime
startup.

## Objective

The benchmark answers a narrow question:

> For the same complete sprint, how do call count, duration, prompt size, native token
> usage, and optional estimated cost change when request-event prompt compaction is
> enabled?

The first experiment targets the optimization merged in PR #5. Both arms run the same
merged source revision and deployed role/model configuration:

| Arm | `prompt_context.enabled` | Event projection |
| --- | --- | --- |
| Before | `false` | The complete request `events` array is embedded in each applicable prompt. |
| After | `true` | At most 16 events are embedded: the latest 8 events plus the latest older evidence for roles not represented in that tail. |

This is a feature-toggle comparison, not a comparison between two Git commits. Keeping
the source revision fixed removes unrelated code changes from the experiment.

The default run is one paired smoke test. A single pair can reveal large regressions
and validate measurement coverage, but it is not statistically significant. Provider
routing, tool use, cache state, and model behavior are nondeterministic, so end-to-end
deltas are not attributable to compaction alone. Use repeated pairs before making a
capacity or budget commitment.

## Full-Sprint Scenario

Each arm receives a fresh, isolated, no-remote Git repository. The benchmark scaffolds
the normal team workspace and adds a deliberately defective Python function:

```python
def sum_positive(values):
    return sum(values)
```

The protected test oracle is:

```python
assert sum_positive([5, -8, 2]) == 7
assert sum_positive([-5, 0, -3]) == 0
assert sum_positive([]) == 0
```

The initial test run must fail. The sprint milestone asks the team to preserve the
public function, fix the behavior, run the `unittest` suite, leave protected benchmark
files unchanged, and commit the result. The complete production sprint workflow is
used: planning, explicit benchmark auto-confirmation, backlog/TODO execution, governed
role handoffs, QA, version control, and closeout.

An arm passes its behavior and workflow gates only when all of the following are true:

- the initial defective fixture was reproduced before the arm started
- the final protected behavior oracle passes
- protected scenario and test files have the same SHA-256 hashes
- the sprint reaches a terminal completed state
- closeout is verified
- no TODO is blocked or failed
- at least one task commit exists
- the final Git worktree is clean
- the isolated repository still has no Git remotes
- every persisted invocation has native token usage
- the call journal is present, uses a supported schema, and reconciles every
  reservation to exactly one terminal attempt
- the After arm records at least one real compacted prompt projection
- the Before and After non-feature configuration fingerprints match

## Backfill

In this benchmark, **Backfill** means adding a deterministic history prefix to the
canonical initial sprint-planning request before it is relayed to research or
planner. The same canonical request, including the prefix, is used by later routing
steps. It does not mean importing production telemetry, replaying customer data, or
modifying an existing workspace.

The benchmark generates 48 content-safe historical events. They contain neutral
checkpoints and one evidence checkpoint for each workflow role:

```json
[
  {
    "created_at": "2026-01-01T00:01:00+00:00",
    "type": "role_report",
    "actor": "research",
    "summary": "Historical research checkpoint 02.",
    "payload": {
      "role": "research",
      "status": "completed",
      "summary": "Stable benchmark evidence 02."
    }
  },
  {
    "created_at": "2026-01-01T00:02:00+00:00",
    "type": "benchmark_checkpoint",
    "actor": "orchestrator",
    "summary": "Neutral historical checkpoint 03.",
    "payload": {"sequence": 3}
  }
]
```

The full 48-event sequence and its canonical SHA-256 hash are identical in both arms.
It is prepended exactly once, before the initial planning request's normal `created`
and `delegated` events. The benchmark persists and hash-verifies the prefix before
the first provider call. This produces a realistic long-history prompt from the
beginning of the sprint without introducing facts that could change the desired
implementation.

Backfill serves three purposes:

1. It guarantees that the request exceeds the 16-event compaction threshold.
2. It gives the selector older evidence from every role to preserve.
3. It makes prompt-size and token deltas reproducible across paired runs.

The generated history is saved as `.benchmark/history_seed.json` inside a retained,
sanitized arm snapshot. Reports store only its hash and counts. The benchmark never
backfills the normal telemetry store with fabricated model invocations.

## Compaction

Compaction is executed immediately before an applicable role prompt is built. The
canonical request JSON on disk remains complete. Only the request projection embedded
in the model prompt changes.

The selection policy is
`recent_tail_plus_latest_role_evidence`:

1. If compaction is disabled, or the request has at most `max_events`, include every
   event.
2. Select the final `recent_events` events. The benchmark uses 8.
3. Record which roles already have evidence in that recent tail.
4. Scan older events from newest to oldest.
5. For each role not yet represented, include its most recent `role_report` evidence.
6. Stop when every available role is represented or `max_events` is reached. The
   benchmark uses 16.
7. Restore selected events to chronological order and embed their complete objects,
   not summaries.
8. Add a projection notice with total, included, and omitted counts plus the path to
   the complete canonical request.

For example, the first relayed planning request normally has the 48-event Backfill
followed by its `created` and `delegated` events. Before compaction, the prompt input
contains:

```json
{
  "request_id": "req-example",
  "events": [
    {"type": "role_report", "payload": {"role": "research", "status": "completed"}},
    "... 47 additional deterministic historical events ...",
    {"type": "created", "actor": "sprint_runner"},
    {"type": "delegated", "actor": "orchestrator"}
  ]
}
```

The Before arm embeds all 50 events. The After arm's projection contains the latest
8 events and the most recent older evidence for up to 8 missing roles:

```json
{
  "compacted": true,
  "total_events": 50,
  "included_events": 16,
  "omitted_events": 34,
  "recent_events": 8,
  "max_events": 16,
  "selection": "recent_tail_plus_latest_role_evidence",
  "canonical_request": "./.teams_runtime/requests/req-example.json"
}
```

The projected `events` array contains 16 complete event objects in chronological
order. The other 34 events still exist in the canonical request. The prompt tells the
role to open that file only when a decision requires evidence missing from the
projection.

Telemetry records the projection policy and counts on the physical provider attempt.
This proves that compaction was eligible and executed; token reduction by itself is
not accepted as proof.

## Running The Benchmark

Run from the `teams_runtime` source checkout:

```bash
TEAMS_RUNTIME_LIVE_BENCHMARK=1 \
python -m teams_runtime benchmark sprint-ab \
  --live \
  --runtime-config ../teams_generated/team_runtime.yaml \
  --repetitions 1 \
  --max-invocations 20 \
  --call-timeout-seconds 300 \
  --run-timeout-seconds 1800 \
  --keep-workspaces failures
```

Live calls require both the environment variable and `--live`. Omitting either is a
preflight failure and makes no model call.

Options:

| Option | Default | Meaning |
| --- | ---: | --- |
| `--runtime-config PATH` | required | Deployed `team_runtime.yaml` or the workspace directory containing it. |
| `--repetitions N` | `1` | Number of paired experiments. Pair 1 runs Before/After; pair 2 runs After/Before, then order alternates. |
| `--max-invocations N` | `20` | Hard physical model-call cap for each arm, including retries and contract repairs. |
| `--call-timeout-seconds N` | `300` | Hard timeout for one provider process group. |
| `--run-timeout-seconds N` | `1800` | Hard wall-clock timeout for one complete arm. |
| `--keep-workspaces MODE` | `failures` | `none`, `failures`, or `all`. |
| `--rate-card-file PATH` | unset | Optional YAML pricing snapshot. |
| `--output-dir PATH` | `.teams_runtime/benchmarks` | Parent directory for benchmark artifacts. |
| `--benchmark-id ID` | generated | Stable 1-96 character artifact directory name. |
| `--allow-dirty-source` | false | Permit a dirty checkout and record its content-free state hash. |
| `--json` | false | Print a machine-readable completion summary. |

Exit codes:

| Code | Meaning |
| ---: | --- |
| `0` | Every pair passed quality and comparability gates. |
| `1` | Artifacts were preserved, but at least one pair is partial or inconclusive. |
| `2` | Usage or preflight failure; no valid benchmark was started. |

## Execution Safety

The live worker is deliberately narrower than normal runtime execution:

- every role uses the internal file relay; no Discord listener or message is used
- sprint GitHub issue publication is replaced with a local `skipped_benchmark` record
- external deep research is disabled and remains an unresolved risk if requested
- the fixture repository has no remote and safe Git configuration disables prompts,
  signing, system/global configuration, and repository hooks
- benchmark model execution supports Codex only; Gemini CLI requests are rejected
- approval policy is `never` and sandbox mode is `workspace-write`
- dangerous sandbox-bypass requests and automatic bypass retries are rejected
- MCP servers, web search, plugins, hooks, computer use, and multi-agent features are
  disabled for the provider process
- the provider receives only the authentication and transport variables needed by the
  outer Codex CLI
- tool shells inherit no outer environment and receive an explicit non-secret
  allowlist
- writable paths must resolve inside the isolated arm root
- a shared atomic journal reserves a call before launch, so concurrent roles cannot
  exceed the arm's physical-call budget
- a benchmark-only launcher waits on a private pipe and executes the provider only
  after its PID and process group are durably journaled; parent death closes the
  pipe and exits the launcher without starting the provider
- provider processes run in dedicated process groups and are terminated on call
  timeout
- an arm timeout terminates active provider groups before the worker is stopped

When a call cap or timeout is reached, the benchmark does not silently raise the
limit. It preserves partial telemetry, marks the arm inconclusive, and proceeds to the
other arm when doing so remains safe.

Live benchmarks are never run automatically in CI. Deterministic fake-worker
integration tests exercise scheduling, fixtures, aggregation, reporting, and failure
paths without credentials or provider calls.

## Reports

Artifacts are written under:

```text
.teams_runtime/benchmarks/<benchmark-id>/
├── report.json
├── report.md
├── runs/
│   ├── pair-001-before/
│   │   ├── run.json
│   │   ├── metrics.json
│   │   ├── model_invocations.jsonl
│   │   ├── sprint.json
│   │   ├── quality.json
│   │   ├── call_journal.json
│   │   └── worker.log
│   └── pair-001-after/
└── workspaces/
    └── ... sanitized snapshots retained according to --keep-workspaces
```

The execution report includes:

- journal-reserved, telemetry-observed, completed, failed, timed-out,
  launch-failed, parent-terminated, active, and rejected attempt counts
- physical telemetry invocation and logical-call counts
- primary, contract-repair, sandbox-retry, failed, and completed counts
- tool-call count and coverage
- provider and end-to-end wall duration, including p50 and p95 provider latency
- prompt and output character counts
- input, cached input, uncached input, output, reasoning-output, and total tokens
- native-token coverage
- optional estimated cost and pricing coverage
- compaction eligibility, executions, total/included/omitted events, and maximum
  included events
- per-role/provider/model groups
- matched primary calls keyed by role, purpose, workflow step, and occurrence
- full-sprint Before/After deltas and reduction percentages

Provider usage is reported only for telemetry-observed calls. Native-token,
tool-call, and pricing coverage use journal-reserved attempts as their denominator.
If the hard arm deadline terminates a provider before telemetry can be finalized,
the call journal records a terminal `terminated` attempt while its token usage
remains unmeasured. Reports show both counts, leave aggregate and per-group costs
unpriced, and never infer tokens for that attempt. If the journal is absent,
unsupported, unreconciled, incomplete, or has more telemetry records than
reservations, all coverage percentages and cost totals remain unknown. Coverage
also requires unique, nonempty telemetry invocation IDs that match a subset of the
journaled invocation IDs. Reports persist only bounded mismatch counts and a
SHA-256 identity digest, not the journal's raw identity list.

Reports are content-safe: they do not contain prompts, model responses, tool output,
raw errors, raw session IDs, credentials, or environment values.

### Optional Rate Card

Pricing is operator-supplied and never fetched automatically. A rate-card file can
use a top-level `rate_cards` mapping:

```yaml
rate_cards:
  "codex_cli/gpt-5.5":
    input_per_million_usd: 0.00
    cached_input_per_million_usd: 0.00
    output_per_million_usd: 0.00
  "codex_cli/gpt-5.3-codex-spark":
    input_per_million_usd: 0.00
    cached_input_per_million_usd: 0.00
    output_per_million_usd: 0.00
```

Replace zero placeholders with the pricing agreement applicable to the deployment.
Cost is reported only when every invocation in both arms has a matching rate and the
native usage required by that rate. Otherwise cost remains `null`/`unpriced`; partial
coverage is never presented as a complete total.

## Interpreting Results

Prioritize evidence in this order:

1. Verify both arms passed all quality and comparability gates.
2. Verify the same history hash and non-feature configuration hash were used.
3. Verify 100% native token coverage.
4. Verify the After arm recorded actual omissions and the Before arm did not.
5. Compare matched primary calls to isolate prompts that exercised the same role,
   purpose, and workflow step.
6. Compare full-sprint totals to capture routing, retries, and downstream effects.
7. Treat cost as an estimate only when pricing coverage is 100%.

A one-pair report is labeled `preliminary_smoke`; its sample standard deviation is
`null`. For a stronger estimate, rerun with at least three pairs. Execution order
alternates automatically to reduce a simple warm-cache or time-order bias. Do not
combine reports from different source revisions, deployed model maps, history hashes,
or rate-card snapshots.

## Troubleshooting

`preflight_failed`:

- confirm both live opt-ins are present
- confirm the runtime config exists and defines every role model/reasoning pair
- commit source changes, or deliberately use `--allow-dirty-source`
- choose a new benchmark ID or remove only an explicitly disposable old output

`call_budget_exhausted`:

- inspect `call_journal.json` and the role/purpose aggregates
- do not increase the cap to make an individual result appear comparable
- simplify the fixture only by creating a new scenario version, or deliberately
  schedule a separately documented higher-budget experiment

`timeout`:

- inspect `worker.log`, terminal journal entries, and partial invocation telemetry
- distinguish a single provider timeout from the full-arm timeout
- rerun the unchanged pair after a transient provider incident; do not merge a
  replacement arm into the old pair

`native_token_coverage_incomplete`:

- confirm the installed Codex CLI emits terminal usage in JSON mode
- retain the result as evidence of a measurement gap
- do not substitute character-count estimates for native tokens in a comparable pair

`after_compaction_not_observed`:

- verify the deterministic history hash
- inspect the sanitized `model_invocations.jsonl` projection metadata in the arm report
- treat the pair as inconclusive even if input tokens decreased

Retained paths are sanitized, allowlisted snapshots rather than execution workspaces.
They include only pre-execution benchmark metadata, deterministic fixture/test files,
the baseline `benchmark_app.py`, the benchmark task, and the arm configuration. The
post-execution implementation is represented only by a SHA-256 digest, byte count, and
changed/not-changed flag. Git metadata, `.teams_runtime`, logs, model sessions, provider
output, role workspaces, model-mutated file content, and all unrecognized files are
excluded. Remove snapshots according to the project's normal retention policy.
