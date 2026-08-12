from __future__ import annotations

import io
import json
import math
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from teams_runtime.cli import build_parser, cmd_metrics
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.runtime.base_runtime import RoleAgentRuntime
from teams_runtime.runtime.codex_runner import CodexRunner, parse_codex_jsonl, parse_gemini_usage
from teams_runtime.runtime.model_telemetry import (
    InvocationSequence,
    ModelTelemetryRecorder,
    ModelUsage,
    aggregate_model_invocations,
    calculate_estimated_cost,
    hash_session_id,
    render_model_metrics_summary,
)
from teams_runtime.shared.config import load_team_runtime_config
from teams_runtime.shared.models import (
    MessageEnvelope,
    ModelRateCard,
    RoleRuntimeConfig,
    TelemetryRuntimeConfig,
)
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import runtime_now


class ModelTelemetryTests(unittest.TestCase):
    def test_codex_jsonl_parser_recovers_session_usage_and_final_message(self):
        stdout = "\n".join(
            (
                json.dumps({"type": "thread.started", "thread_id": "thread-123"}),
                "not-json",
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "agent_message", "text": '{"status":"completed"}'},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "command-1", "type": "command_execution"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item/completed",
                        "item": {"id": "mcp-1", "type": "mcp_tool_call"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "mcp-1", "type": "mcp_tool_call"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "file_change"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"type": "web_search"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 100,
                            "cached_input_tokens": 60,
                            "output_tokens": 25,
                            "reasoning_output_tokens": 5,
                            "total_tokens": 125,
                        },
                    }
                ),
            )
        )

        session_id, usage, final_message = parse_codex_jsonl(stdout)

        self.assertEqual(session_id, "thread-123")
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.cached_input_tokens, 60)
        self.assertEqual(usage.output_tokens, 25)
        self.assertEqual(usage.reasoning_output_tokens, 5)
        self.assertEqual(usage.total_tokens, 125)
        self.assertEqual(usage.tool_calls, 4)
        self.assertEqual(usage.source, "native")
        self.assertEqual(final_message, '{"status":"completed"}')

    def test_codex_jsonl_parser_does_not_double_count_terminal_tool_usage(self):
        stdout = "\n".join(
            (
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {"id": "command-1", "type": "command_execution"},
                    }
                ),
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 10,
                            "output_tokens": 2,
                            "total_tokens": 12,
                            "tool_calls": 3,
                        },
                    }
                ),
            )
        )

        _session_id, usage, _final_message = parse_codex_jsonl(stdout)

        self.assertEqual(usage.input_tokens, 10)
        self.assertEqual(usage.output_tokens, 2)
        self.assertEqual(usage.total_tokens, 12)
        self.assertEqual(usage.tool_calls, 3)
        self.assertEqual(usage.source, "native")

    def test_gemini_usage_parser_sums_models_and_tool_calls(self):
        usage = parse_gemini_usage(
            {
                "models": {
                    "model-a": {
                        "tokens": {
                            "prompt": 40,
                            "cached": 20,
                            "candidates": 10,
                            "thoughts": 3,
                            "total": 50,
                        }
                    },
                    "model-b": {
                        "tokens": {
                            "prompt": 60,
                            "cached": 15,
                            "candidates": 25,
                            "thoughts": 7,
                            "total": 85,
                        }
                    },
                },
                "tools": {"totalCalls": 4},
            }
        )

        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.cached_input_tokens, 35)
        self.assertEqual(usage.output_tokens, 35)
        self.assertEqual(usage.reasoning_output_tokens, 10)
        self.assertEqual(usage.total_tokens, 135)
        self.assertEqual(usage.tool_calls, 4)

    def test_tool_only_usage_does_not_claim_native_token_coverage(self):
        usage = ModelUsage.from_values(tool_calls=3)

        self.assertEqual(usage.tool_calls, 3)
        self.assertEqual(usage.source, "unavailable")

    def test_usage_parsers_ignore_non_finite_counts(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(source="model_usage", value=value):
                usage = ModelUsage.from_values(
                    input_tokens=value,
                    cached_input_tokens=value,
                    output_tokens=value,
                    reasoning_output_tokens=value,
                    total_tokens=value,
                    tool_calls=value,
                )
                self.assertEqual(usage, ModelUsage())

        codex_event = json.dumps(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": math.inf,
                    "output_tokens": math.nan,
                },
            }
        )
        _session_id, codex_usage, _final_message = parse_codex_jsonl(codex_event)
        self.assertEqual(codex_usage, ModelUsage())

        gemini_usage = parse_gemini_usage(
            {
                "models": {
                    "model-a": {
                        "tokens": {
                            "prompt": math.inf,
                            "candidates": math.nan,
                        }
                    }
                },
                "tools": {"totalCalls": -math.inf},
            }
        )
        self.assertEqual(gemini_usage, ModelUsage())

    def test_cost_calculation_separates_cached_input(self):
        usage = ModelUsage.from_values(input_tokens=1_000_000, cached_input_tokens=400_000, output_tokens=200_000)
        rate = ModelRateCard(
            input_per_million_usd=2.0,
            cached_input_per_million_usd=0.5,
            output_per_million_usd=8.0,
        )

        self.assertEqual(calculate_estimated_cost(usage, rate), 3.0)
        self.assertEqual(
            calculate_estimated_cost(ModelUsage(), ModelRateCard(per_invocation_usd=1.25)),
            1.25,
        )

    def test_invocation_sequence_carries_prompt_context_projection_across_attempts(self):
        sequence = InvocationSequence(
            runtime_identity="service-planner",
            role="planner",
            purpose="role_task",
        )
        sequence.set_prompt_context_projection(
            SimpleNamespace(
                total_events=100,
                included_events=16,
                omitted_events=84,
                recent_events=8,
                max_events=16,
            ),
            enabled=True,
            selection_policy="recent_tail_plus_latest_role_evidence",
        )

        primary = sequence.next("primary")
        repair = sequence.next("contract_repair")

        for context in (primary, repair):
            self.assertTrue(context.prompt_context_enabled)
            self.assertEqual(context.prompt_context_total_events, 100)
            self.assertEqual(context.prompt_context_included_events, 16)
            self.assertEqual(context.prompt_context_omitted_events, 84)
            self.assertEqual(context.prompt_context_recent_events, 8)
            self.assertEqual(context.prompt_context_max_events, 16)
            self.assertEqual(
                context.prompt_context_selection_policy,
                "recent_tail_plus_latest_role_evidence",
            )

    def test_recorder_writes_privacy_safe_daily_shard_and_rate_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            rate = ModelRateCard(
                input_per_million_usd=2.0,
                cached_input_per_million_usd=1.0,
                output_per_million_usd=4.0,
            )
            recorder = ModelTelemetryRecorder(
                paths,
                "local:planner",
                TelemetryRuntimeConfig(rate_cards={"codex_cli/gpt-5.5": rate}),
            )
            sequence = InvocationSequence(
                runtime_identity="local:planner",
                role="planner",
                purpose="role_task",
                workflow_step="planner_draft",
                request_id="request-1",
                sprint_id="sprint-a",
                goal_id="goal-1",
            )
            sequence.set_prompt_context_projection(
                SimpleNamespace(
                    total_events=100,
                    included_events=16,
                    omitted_events=84,
                    recent_events=8,
                    max_events=16,
                ),
                enabled=True,
                selection_policy="recent_tail_plus_latest_role_evidence",
            )
            now = runtime_now()
            recorder.record(
                sequence.next(),
                provider="codex_cli",
                model="gpt-5.5",
                reasoning="xhigh",
                cli_version="codex-cli test",
                started_at=now,
                ended_at=now + timedelta(seconds=1),
                duration_ms=1000,
                session_id_before="secret-session-id",
                session_id_after="secret-session-id",
                status="completed",
                exit_code=0,
                error_category="",
                prompt_chars=500,
                output_chars=100,
                usage=ModelUsage.from_values(input_tokens=100, cached_input_tokens=40, output_tokens=20),
            )

            shards = list(paths.model_invocations_dir.rglob("*.jsonl"))
            self.assertEqual(len(shards), 1)
            raw_text = shards[0].read_text(encoding="utf-8")
            record = json.loads(raw_text)
            self.assertNotIn("secret-session-id", raw_text)
            self.assertNotIn("prompt", record)
            self.assertNotIn("response", record)
            self.assertNotIn("workspace", record)
            self.assertEqual(record["session_id_hash"], hash_session_id("secret-session-id"))
            self.assertEqual(record["session_mode"], "resume")
            self.assertEqual(record["goal_id"], "goal-1")
            self.assertTrue(record["prompt_context_enabled"])
            self.assertEqual(record["prompt_context_total_events"], 100)
            self.assertEqual(record["prompt_context_included_events"], 16)
            self.assertEqual(record["prompt_context_omitted_events"], 84)
            self.assertEqual(record["prompt_context_recent_events"], 8)
            self.assertEqual(record["prompt_context_max_events"], 16)
            self.assertEqual(
                record["prompt_context_selection_policy"],
                "recent_tail_plus_latest_role_evidence",
            )
            self.assertIsNotNone(record["estimated_cost_usd"])
            self.assertEqual(record["rate_card"]["input_per_million_usd"], 2.0)
            self.assertEqual(shards[0].parent.name, now.date().isoformat())
            self.assertTrue(shards[0].name.endswith(f".{os.getpid()}.jsonl"))

    def test_recorder_custom_output_is_external_private_daily_shard(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            paths = RuntimePaths.from_root(workspace)
            output_dir = root / "private-telemetry"
            recorder = ModelTelemetryRecorder(
                paths,
                "benchmark:planner",
                output_dir=output_dir,
            )
            now = runtime_now()
            context = InvocationSequence(
                runtime_identity="benchmark:planner",
                role="planner",
                purpose="role_task",
            ).next()

            recorder.record(
                context,
                provider="codex_cli",
                model="gpt-5.5",
                reasoning="high",
                cli_version="codex-cli test",
                started_at=now,
                ended_at=now,
                duration_ms=1,
                session_id_before=None,
                session_id_after=None,
                status="completed",
                exit_code=0,
                error_category="",
                prompt_chars=1,
                output_chars=1,
            )

            expected_day_dir = output_dir / now.date().isoformat()
            expected_shard = expected_day_dir / f"benchmark_planner.{os.getpid()}.jsonl"
            self.assertEqual(recorder.output_dir, output_dir.resolve())
            self.assertTrue(expected_shard.is_file())
            self.assertFalse(paths.model_invocations_dir.exists())
            self.assertEqual(
                json.loads(expected_shard.read_text(encoding="utf-8"))["invocation_id"],
                context.invocation_id,
            )
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(expected_day_dir.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(expected_shard.stat().st_mode), 0o600)

    def test_disabled_recorder_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            recorder = ModelTelemetryRecorder(
                paths,
                "service-planner",
                TelemetryRuntimeConfig(enabled=False),
            )
            now = runtime_now()
            sequence = InvocationSequence(runtime_identity="service-planner", role="planner", purpose="role_task")
            recorder.record(
                sequence.next(),
                provider="codex_cli",
                model="gpt-5.5",
                reasoning="medium",
                cli_version="",
                started_at=now,
                ended_at=now,
                duration_ms=0,
                session_id_before=None,
                session_id_after=None,
                status="completed",
                exit_code=0,
                error_category="",
                prompt_chars=1,
                output_chars=1,
            )

            self.assertFalse(paths.model_invocations_dir.exists())

    def test_recorder_is_fail_open_during_record_preparation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            recorder = ModelTelemetryRecorder(paths, "service-planner")
            sequence = InvocationSequence(runtime_identity="service-planner", role="planner", purpose="role_task")
            now = runtime_now()

            with patch(
                "teams_runtime.runtime.model_telemetry.calculate_estimated_cost",
                side_effect=RuntimeError("telemetry preparation failed"),
            ):
                recorder.record(
                    sequence.next(),
                    provider="codex_cli",
                    model="gpt-5.5",
                    reasoning="medium",
                    cli_version="",
                    started_at=now,
                    ended_at=now,
                    duration_ms=0,
                    session_id_before=None,
                    session_id_after=None,
                    status="completed",
                    exit_code=0,
                    error_category="",
                    prompt_chars=1,
                    output_chars=1,
                )

            self.assertFalse(paths.model_invocations_dir.exists())

    def test_aggregation_filters_counts_percentiles_cost_and_invalid_lines(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            recorder = ModelTelemetryRecorder(
                paths,
                "service-planner",
                TelemetryRuntimeConfig(
                    rate_cards={
                        "codex_cli/gpt-5.5": ModelRateCard(
                            input_per_million_usd=1.0,
                            cached_input_per_million_usd=1.0,
                            output_per_million_usd=1.0,
                        )
                    }
                ),
            )
            now = runtime_now()
            sequence = InvocationSequence(
                runtime_identity="service-planner",
                role="planner",
                purpose="role_task",
                request_id="request-1",
                sprint_id="sprint-a",
            )
            sequence.set_prompt_context_projection(
                SimpleNamespace(
                    total_events=100,
                    included_events=16,
                    omitted_events=84,
                    recent_events=8,
                    max_events=16,
                ),
                enabled=True,
                selection_policy="recent_tail_plus_latest_role_evidence",
            )
            for index, duration in enumerate((100, 200, 300), start=1):
                recorder.record(
                    sequence.next("primary" if index == 1 else "contract_repair"),
                    provider="codex_cli",
                    model="gpt-5.5",
                    reasoning="xhigh",
                    cli_version="test",
                    started_at=now - timedelta(minutes=5),
                    ended_at=now - timedelta(minutes=4),
                    duration_ms=duration,
                    session_id_before=None,
                    session_id_after=f"session-{index}",
                    status="completed" if index < 3 else "failed",
                    exit_code=0 if index < 3 else 1,
                    error_category="" if index < 3 else "nonzero_exit",
                    prompt_chars=10,
                    output_chars=5,
                    usage=ModelUsage.from_values(
                        input_tokens=100,
                        cached_input_tokens=25,
                        output_tokens=20,
                        tool_calls=index,
                    ),
                )
            shard = next(paths.model_invocations_dir.rglob("*.jsonl"))
            with shard.open("a", encoding="utf-8") as handle:
                handle.write("partial-json\n")

            summary = aggregate_model_invocations(
                paths,
                hours=1,
                request_id="request-1",
                sprint_id="sprint-a",
                role="planner",
                now=now,
            )

            self.assertEqual(summary["totals"]["invocation_count"], 3)
            self.assertEqual(summary["totals"]["physical_attempt_count"], 3)
            self.assertEqual(summary["totals"]["logical_call_count"], 1)
            self.assertEqual(summary["totals"]["primary_count"], 1)
            self.assertEqual(summary["totals"]["contract_repair_count"], 2)
            self.assertEqual(summary["totals"]["failed_count"], 1)
            self.assertEqual(summary["totals"]["tool_call_count"], 6)
            self.assertEqual(summary["totals"]["tool_call_coverage_percent"], 100.0)
            self.assertEqual(summary["totals"]["invalid_record_count"], 1)
            self.assertEqual(summary["tokens"]["input"], 300)
            self.assertEqual(summary["tokens"]["uncached_input"], 225)
            self.assertEqual(
                summary["prompt_context"],
                {
                    "observed_invocation_count": 3,
                    "enabled_invocation_count": 3,
                    "eligible_invocation_count": 3,
                    "compacted_invocation_count": 3,
                    "total_events": 300,
                    "included_events": 48,
                    "omitted_events": 252,
                    "coverage_percent": 100.0,
                    "selection_policies": ["recent_tail_plus_latest_role_evidence"],
                },
            )
            self.assertEqual(summary["latency_ms"]["p50"], 200)
            self.assertEqual(summary["latency_ms"]["p95"], 300)
            self.assertEqual(summary["totals"]["token_coverage_percent"], 100.0)
            self.assertEqual(summary["totals"]["pricing_coverage_percent"], 100.0)
            self.assertEqual(len(summary["groups"]), 1)
            self.assertEqual(summary["groups"][0]["primary_count"], 1)
            self.assertEqual(summary["groups"][0]["tool_call_count"], 6)
            self.assertEqual(summary["groups"][0]["uncached_input_tokens"], 225)
            self.assertEqual(summary["groups"][0]["prompt_context_observed_count"], 3)
            self.assertEqual(summary["groups"][0]["prompt_context_compacted_count"], 3)
            self.assertIn("role\tpurpose", render_model_metrics_summary(summary))

    def test_aggregation_accepts_records_without_optional_projection_or_tool_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            recorder = ModelTelemetryRecorder(paths, "service-planner")
            now = runtime_now()
            sequence = InvocationSequence(
                runtime_identity="service-planner",
                role="planner",
                purpose="role_task",
            )
            recorder.record(
                sequence.next(),
                provider="codex_cli",
                model="gpt-5.5",
                reasoning="xhigh",
                cli_version="test",
                started_at=now,
                ended_at=now,
                duration_ms=10,
                session_id_before=None,
                session_id_after="session-1",
                status="completed",
                exit_code=0,
                error_category="",
                prompt_chars=10,
                output_chars=5,
                usage=ModelUsage.from_values(
                    input_tokens=100,
                    cached_input_tokens=150,
                    output_tokens=20,
                ),
            )
            shard = next(paths.model_invocations_dir.rglob("*.jsonl"))
            legacy_record = json.loads(shard.read_text(encoding="utf-8"))
            legacy_record.pop("tool_calls")
            for key in tuple(legacy_record):
                if key.startswith("prompt_context_"):
                    legacy_record.pop(key)
            shard.write_text(json.dumps(legacy_record) + "\n", encoding="utf-8")

            summary = aggregate_model_invocations(paths, hours=1, now=now)

            self.assertEqual(summary["tokens"]["uncached_input"], 0)
            self.assertEqual(summary["totals"]["tool_call_count"], 0)
            self.assertEqual(summary["totals"]["tool_call_coverage_percent"], 0.0)
            self.assertEqual(summary["prompt_context"]["observed_invocation_count"], 0)
            self.assertEqual(summary["prompt_context"]["coverage_percent"], 0.0)

    def test_aggregation_hides_partial_cost_totals_and_group_subtotals(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            recorder = ModelTelemetryRecorder(
                paths,
                "service-planner",
                TelemetryRuntimeConfig(
                    rate_cards={
                        "codex_cli/gpt-5.5": ModelRateCard(
                            input_per_million_usd=1.0,
                            output_per_million_usd=1.0,
                        )
                    }
                ),
            )
            now = runtime_now()
            sequence = InvocationSequence(
                runtime_identity="service-planner",
                role="planner",
                purpose="role_task",
            )
            usages = (
                ModelUsage.from_values(input_tokens=100, output_tokens=20),
                ModelUsage(),
            )
            for index, usage in enumerate(usages):
                recorder.record(
                    sequence.next("primary" if index == 0 else "contract_repair"),
                    provider="codex_cli",
                    model="gpt-5.5",
                    reasoning="xhigh",
                    cli_version="test",
                    started_at=now,
                    ended_at=now,
                    duration_ms=10,
                    session_id_before=None,
                    session_id_after=f"session-{index}",
                    status="completed",
                    exit_code=0,
                    error_category="",
                    prompt_chars=10,
                    output_chars=5,
                    usage=usage,
                )

            summary = aggregate_model_invocations(paths, hours=1, now=now)

            self.assertEqual(summary["totals"]["pricing_coverage_percent"], 50.0)
            self.assertIsNone(summary["totals"]["estimated_cost_usd"])
            self.assertEqual(len(summary["groups"]), 1)
            self.assertIsNone(summary["groups"][0]["estimated_cost_usd"])

    def test_aggregation_distinguishes_disabled_eligible_history_from_compaction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            paths = RuntimePaths.from_root(tmpdir)
            recorder = ModelTelemetryRecorder(paths, "service-planner")
            now = runtime_now()
            variants = (
                (False, 100, 0),
                (True, 16, 84),
            )
            for index, (enabled, included_events, omitted_events) in enumerate(variants):
                sequence = InvocationSequence(
                    runtime_identity="service-planner",
                    role="planner",
                    purpose=f"variant-{index}",
                )
                sequence.set_prompt_context_projection(
                    SimpleNamespace(
                        total_events=100,
                        included_events=included_events,
                        omitted_events=omitted_events,
                        recent_events=8,
                        max_events=16,
                    ),
                    enabled=enabled,
                    selection_policy="recent_tail_plus_latest_role_evidence",
                )
                recorder.record(
                    sequence.next(),
                    provider="codex_cli",
                    model="gpt-5.5",
                    reasoning="xhigh",
                    cli_version="test",
                    started_at=now,
                    ended_at=now,
                    duration_ms=10,
                    session_id_before=None,
                    session_id_after=f"session-{index}",
                    status="completed",
                    exit_code=0,
                    error_category="",
                    prompt_chars=10,
                    output_chars=5,
                )

            prompt_context = aggregate_model_invocations(
                paths,
                hours=1,
                now=now,
            )["prompt_context"]

            self.assertEqual(prompt_context["observed_invocation_count"], 2)
            self.assertEqual(prompt_context["enabled_invocation_count"], 1)
            self.assertEqual(prompt_context["eligible_invocation_count"], 2)
            self.assertEqual(prompt_context["compacted_invocation_count"], 1)
            self.assertEqual(prompt_context["total_events"], 200)
            self.assertEqual(prompt_context["included_events"], 116)
            self.assertEqual(prompt_context["omitted_events"], 84)

    def test_codex_runner_records_native_usage_without_changing_tuple_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            paths = RuntimePaths.from_root(workspace)
            recorder = ModelTelemetryRecorder(paths, "service-planner")
            runner = CodexRunner(
                RoleRuntimeConfig(model="gpt-5.5", reasoning="xhigh"),
                role="planner",
                telemetry_recorder=recorder,
            )
            sequence = InvocationSequence(
                runtime_identity="service-planner",
                role="planner",
                purpose="role_task",
                request_id="request-1",
            )
            output_file = workspace / ".teams_runtime_codex_output.txt"
            role_output = '{"request_id":"request-1","role":"planner","status":"completed","summary":"ok"}'
            stdout = "\n".join(
                (
                    json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 10, "cached_input_tokens": 3, "output_tokens": 4},
                        }
                    ),
                )
            )

            def fake_run(*_args, **_kwargs):
                output_file.write_text(role_output, encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

            CodexRunner._version_cache["codex"] = "codex-cli test"
            with patch("teams_runtime.runtime.codex_runner.subprocess.run", side_effect=fake_run):
                result = runner.run(
                    workspace,
                    "private prompt text",
                    None,
                    invocation_context=sequence.next(),
                )

            self.assertEqual(result, (role_output, "thread-1"))
            record = json.loads(next(paths.model_invocations_dir.rglob("*.jsonl")).read_text(encoding="utf-8"))
            self.assertEqual(record["input_tokens"], 10)
            self.assertEqual(record["cached_input_tokens"], 3)
            self.assertEqual(record["output_tokens"], 4)
            self.assertEqual(record["total_tokens"], 14)
            self.assertEqual(record["tool_calls"], 0)
            self.assertNotIn("private prompt text", json.dumps(record))

    def test_role_contract_repair_records_correlated_attempts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            responses = [
                "not-json",
                '{"request_id":"request-1","role":"planner","status":"completed","summary":"repaired","artifacts":[]}',
            ]

            def fake_run(_command, **kwargs):
                workspace = Path(kwargs["cwd"])
                output_file = workspace / ".teams_runtime_codex_output.txt"
                output_file.write_text(responses.pop(0), encoding="utf-8")
                stdout = "\n".join(
                    (
                        json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                            }
                        ),
                    )
                )
                return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

            envelope = MessageEnvelope(
                request_id="request-1",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="plan",
            )
            request = {"request_id": "request-1", "scope": "plan", "body": "", "artifacts": []}
            CodexRunner._version_cache["codex"] = "codex-cli test"
            with patch("teams_runtime.runtime.codex_runner.subprocess.run", side_effect=fake_run):
                result = runtime.run_task(envelope, request)

            self.assertEqual(result["status"], "completed")
            records = []
            for shard in paths.model_invocations_dir.rglob("*.jsonl"):
                records.extend(json.loads(line) for line in shard.read_text(encoding="utf-8").splitlines())
            self.assertEqual(len(records), 2)
            self.assertEqual([record["attempt_kind"] for record in records], ["primary", "contract_repair"])
            self.assertEqual({record["operation_id"] for record in records}, {records[0]["operation_id"]})
            self.assertEqual({record["logical_call_id"] for record in records}, {records[0]["logical_call_id"]})
            self.assertEqual([record["attempt_index"] for record in records], [1, 2])

    def test_config_defaults_and_rate_card_validation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            config_path = Path(tmpdir) / "team_runtime.yaml"
            payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            payload.pop("telemetry", None)
            config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

            config = load_team_runtime_config(tmpdir)

            self.assertTrue(config.telemetry.enabled)
            self.assertEqual(config.telemetry.rate_cards, {})

            payload["telemetry"] = {
                "enabled": True,
                "rate_cards": {
                    "codex_cli/gpt-5.5": {
                        "input_per_million_usd": 2,
                        "output_per_million_usd": 8,
                    }
                },
            }
            config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
            config = load_team_runtime_config(tmpdir)
            rate = config.telemetry.rate_cards["codex_cli/gpt-5.5"]
            self.assertEqual(rate.cached_input_per_million_usd, 2.0)

            payload["telemetry"]["rate_cards"]["codex_cli/gpt-5.5"]["input_per_million_usd"] = math.inf
            config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "non-negative finite"):
                load_team_runtime_config(tmpdir)

            for invalid_key in ("codex_cli", "/gpt-5.5", "codex_cli/"):
                payload["telemetry"]["rate_cards"] = {
                    invalid_key: {"per_invocation_usd": 1},
                }
                config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "provider/model"):
                    load_team_runtime_config(tmpdir)

    def test_metrics_cli_parser_and_json_output(self):
        args = build_parser().parse_args(
            ["metrics", "--hours", "12", "--request-id", "request-1", "--agent", "planner", "--json"]
        )
        self.assertEqual(args.command, "metrics")
        self.assertEqual(args.hours, 12.0)
        self.assertTrue(args.json)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cmd_metrics(Path(tmpdir), hours=1, as_json=True)
            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["totals"]["invocation_count"], 0)

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = cmd_metrics(Path(tmpdir), hours=0)
            self.assertEqual(exit_code, 2)

    def test_dedicated_telemetry_document_is_indexed(self):
        package_root = Path(__file__).resolve().parents[1]
        document = package_root / "docs" / "telemetry.md"
        docs_index = package_root / "docs" / "README.md"

        self.assertTrue(document.is_file())
        content = document.read_text(encoding="utf-8")
        self.assertIn("## Record Schema", content)
        self.assertIn("## Privacy And Security", content)
        self.assertIn("## CLI Reference", content)
        self.assertIn("## Troubleshooting", content)
        self.assertIn("telemetry.md", docs_index.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
