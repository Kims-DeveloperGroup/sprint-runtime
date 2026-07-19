from __future__ import annotations

import io
import json
import math
import os
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
        self.assertEqual(usage.source, "native")
        self.assertEqual(final_message, '{"status":"completed"}')

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
            self.assertIsNotNone(record["estimated_cost_usd"])
            self.assertEqual(record["rate_card"]["input_per_million_usd"], 2.0)
            self.assertEqual(shards[0].parent.name, now.date().isoformat())
            self.assertTrue(shards[0].name.endswith(f".{os.getpid()}.jsonl"))

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
                    usage=ModelUsage.from_values(input_tokens=100, cached_input_tokens=25, output_tokens=20),
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
            self.assertEqual(summary["totals"]["logical_call_count"], 1)
            self.assertEqual(summary["totals"]["contract_repair_count"], 2)
            self.assertEqual(summary["totals"]["failed_count"], 1)
            self.assertEqual(summary["totals"]["invalid_record_count"], 1)
            self.assertEqual(summary["tokens"]["input"], 300)
            self.assertEqual(summary["latency_ms"]["p50"], 200)
            self.assertEqual(summary["latency_ms"]["p95"], 300)
            self.assertEqual(summary["totals"]["token_coverage_percent"], 100.0)
            self.assertEqual(summary["totals"]["pricing_coverage_percent"], 100.0)
            self.assertEqual(len(summary["groups"]), 1)
            self.assertIn("role\tpurpose", render_model_metrics_summary(summary))

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
