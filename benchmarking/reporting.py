from __future__ import annotations

import json
import os
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from teams_runtime.benchmarking.metrics import compare_metrics
from teams_runtime.benchmarking.models import ArmResult, BenchmarkOptions
from teams_runtime.benchmarking.scenario import (
    BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
    BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
    DEFAULT_HISTORY_SEED_COUNT,
)
from teams_runtime.shared.prompt_context import PROMPT_EVENT_SELECTION_POLICY


REPORT_SCHEMA_VERSION = 2


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    write_text_atomic(
        path,
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
    )


def write_jsonl_atomic(path: Path, records: Iterable[Mapping[str, Any]]) -> None:
    content = "".join(
        json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n"
        for record in records
    )
    write_text_atomic(path, content)


def write_run_artifacts(run_dir: Path, result: ArmResult) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(run_dir / "run.json", result.to_dict())
    write_json_atomic(run_dir / "metrics.json", dict(result.metrics))
    write_json_atomic(run_dir / "sprint.json", result.sprint.to_dict())
    write_json_atomic(run_dir / "quality.json", result.quality.to_dict())
    write_jsonl_atomic(run_dir / "model_invocations.jsonl", result.invocation_records)


def _pair_comparability(before: ArmResult, after: ArmResult) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if before.comparable_config_hash != after.comparable_config_hash:
        reasons.append("non_feature_configuration_differs")
    for label, arm in (("before", before), ("after", after)):
        if arm.status != "completed":
            reasons.append(f"{label}_status_{arm.status}")
        if not arm.quality.passed:
            reasons.append(f"{label}_quality_failed")
        attempts = dict(arm.invocation_attempts)
        if attempts.get("journal_available") is not True:
            reasons.append(f"{label}_call_journal_missing")
        elif int(attempts.get("journal_schema_version") or 0) not in {1, 2}:
            reasons.append(f"{label}_call_journal_schema_unsupported")
        else:
            if attempts.get("reconciled") is not True:
                reasons.append(f"{label}_call_journal_not_reconciled")
            if attempts.get("identity_reconciled") is not True:
                reasons.append(
                    f"{label}_invocation_identity_not_reconciled"
                )
            for field_name, reason_suffix in (
                ("active_count", "active_attempts_present"),
                ("unknown_state_count", "unknown_attempt_states"),
                ("malformed_entry_count", "malformed_attempt_entries"),
                ("unaccounted_count", "unaccounted_attempts"),
                ("overaccounted_count", "overaccounted_attempts"),
                ("telemetry_overage_count", "telemetry_attempt_overage"),
                ("unobserved_attempt_count", "unobserved_attempts"),
                ("terminated_count", "terminated_attempts"),
                ("rejected_count", "rejected_attempts"),
                (
                    "journal_invocation_id_missing_count",
                    "journal_invocation_ids_missing",
                ),
                (
                    "journal_invocation_id_duplicate_count",
                    "journal_invocation_ids_duplicated",
                ),
                (
                    "telemetry_invocation_id_missing_count",
                    "telemetry_invocation_ids_missing",
                ),
                (
                    "telemetry_invocation_id_duplicate_count",
                    "telemetry_invocation_ids_duplicated",
                ),
                (
                    "telemetry_invocation_id_unmatched_count",
                    "telemetry_invocation_ids_unmatched",
                ),
            ):
                if int(attempts.get(field_name) or 0) > 0:
                    reasons.append(f"{label}_{reason_suffix}")
        totals = dict(arm.metrics.get("totals") or {})
        if float(totals.get("token_coverage_percent") or 0.0) != 100.0:
            reasons.append(f"{label}_native_token_coverage_incomplete")
        compaction = dict(arm.metrics.get("compaction") or {})
        if int(compaction.get("invalid_projection_count") or 0):
            reasons.append(f"{label}_prompt_projection_invalid")
        if int(compaction.get("max_observed_events") or 0) < DEFAULT_HISTORY_SEED_COUNT:
            reasons.append(f"{label}_backfill_not_observed")
        if compaction.get("selection_policies") != [PROMPT_EVENT_SELECTION_POLICY]:
            reasons.append(f"{label}_selection_policy_unverified")
    before_compaction = dict(before.metrics.get("compaction") or {})
    after_compaction = dict(after.metrics.get("compaction") or {})
    if int(before_compaction.get("enabled_invocation_count") or 0):
        reasons.append("before_compaction_unexpectedly_enabled")
    if int(before_compaction.get("eligible_invocation_count") or 0) <= 0:
        reasons.append("before_compaction_eligibility_not_observed")
    if int(before_compaction.get("disabled_eligible_invocation_count") or 0) <= 0:
        reasons.append("before_disabled_projection_not_observed")
    if int(before_compaction.get("compacted_invocation_count") or 0):
        reasons.append("before_compaction_unexpectedly_observed")
    if int(after_compaction.get("enabled_invocation_count") or 0) <= 0:
        reasons.append("after_compaction_not_enabled")
    if int(after_compaction.get("enabled_invocation_count") or 0) != int(
        after_compaction.get("observed_invocation_count") or 0
    ):
        reasons.append("after_prompt_projection_not_uniformly_enabled")
    if int(after_compaction.get("disabled_eligible_invocation_count") or 0):
        reasons.append("after_disabled_projection_observed")
    if int(after_compaction.get("compacted_invocation_count") or 0) <= 0:
        reasons.append("after_compaction_not_observed")
    return not reasons, reasons


def _build_pairs(runs: tuple[ArmResult, ...]) -> list[dict[str, Any]]:
    pair_indexes = sorted({run.arm.pair_index for run in runs})
    pairs: list[dict[str, Any]] = []
    for pair_index in pair_indexes:
        pair_runs = {
            run.arm.variant: run
            for run in runs
            if run.arm.pair_index == pair_index
        }
        before = pair_runs.get("before")
        after = pair_runs.get("after")
        if before is None or after is None:
            pairs.append(
                {
                    "pair_index": pair_index,
                    "comparable": False,
                    "inconclusive_reasons": ["missing_arm"],
                }
            )
            continue
        comparable, reasons = _pair_comparability(before, after)
        comparison = compare_metrics(
            before.metrics,
            after.metrics,
            before_wall_duration_ms=before.wall_duration_ms,
            after_wall_duration_ms=after.wall_duration_ms,
            before_records=before.invocation_records,
            after_records=after.invocation_records,
        )
        pairs.append(
            {
                "pair_index": pair_index,
                "execution_order": [
                    run.arm.variant
                    for run in sorted((before, after), key=lambda item: item.arm.order_index)
                ],
                "before_run_id": before.arm.run_id,
                "after_run_id": after.arm.run_id,
                "comparable": comparable,
                "inconclusive_reasons": reasons,
                "comparison": comparison,
            }
        )
    return pairs


def _aggregate_pair_metrics(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    comparable_pairs = [pair for pair in pairs if pair.get("comparable")]
    metric_values: dict[str, list[float]] = {}
    for pair in comparable_pairs:
        end_to_end = dict((pair.get("comparison") or {}).get("end_to_end") or {})
        for metric_name, delta_payload in end_to_end.items():
            reduction = (delta_payload or {}).get("reduction")
            if isinstance(reduction, (int, float)) and not isinstance(reduction, bool):
                metric_values.setdefault(metric_name, []).append(float(reduction))
    result: dict[str, Any] = {}
    for metric_name, values in sorted(metric_values.items()):
        result[metric_name] = {
            "pair_count": len(values),
            "mean_reduction": statistics.fmean(values),
            "median_reduction": statistics.median(values),
            "sample_standard_deviation": (
                statistics.stdev(values) if len(values) > 1 else None
            ),
        }
    return result


def build_report(
    *,
    benchmark_id: str,
    options: BenchmarkOptions,
    source_revision: Mapping[str, Any],
    source_config_hash: str,
    runtime_model_map: Mapping[str, Mapping[str, str]],
    rate_cards: Mapping[str, Mapping[str, float | None]],
    history_hash: str,
    runs: tuple[ArmResult, ...],
    started_at: str,
    ended_at: str,
) -> dict[str, Any]:
    pairs = _build_pairs(runs)
    comparable = bool(pairs) and all(pair.get("comparable") for pair in pairs)
    status = "comparable" if comparable else "inconclusive"
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "benchmark_id": benchmark_id,
        "benchmark": "sprint_ab",
        "classification": (
            "preliminary_smoke" if options.repetitions == 1 else "repeated_experiment"
        ),
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "provenance": {
            "source": dict(source_revision),
            "source_config_hash": source_config_hash,
            "history_hash": history_hash,
            "runtime_model_map": {
                role: dict(values)
                for role, values in runtime_model_map.items()
            },
            "rate_cards": {
                key: dict(values)
                for key, values in rate_cards.items()
            },
        },
        "controls": {
            "repetitions": options.repetitions,
            "max_invocations_per_arm": options.max_invocations,
            "call_timeout_seconds": options.call_timeout_seconds,
            "run_timeout_seconds": options.run_timeout_seconds,
            "keep_workspaces": options.keep_workspaces,
            "live": options.live,
            "a_b_definition": {
                "before": {"prompt_context_enabled": False},
                "after": {
                    "prompt_context_enabled": True,
                    "recent_events": BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
                    "max_events": BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
                },
            },
        },
        "runs": [run.to_dict() for run in runs],
        "pairs": pairs,
        "aggregate_reductions": _aggregate_pair_metrics(pairs),
        "interpretation": {
            "statistical_significance_claimed": False,
            "note": (
                "A one-pair run is preliminary. Full-sprint model routing is "
                "nondeterministic, so end-to-end deltas are not solely attributable "
                "to prompt compaction."
            ),
        },
    }


def _display(value: Any, *, missing: str = "N/A") -> str:
    if value is None:
        return missing
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Sprint Performance Benchmark",
        "",
        f"- Benchmark: `{report.get('benchmark_id', '')}`",
        f"- Status: **{report.get('status', 'inconclusive')}**",
        f"- Classification: `{report.get('classification', '')}`",
        f"- Started: `{report.get('started_at', '')}`",
        f"- Ended: `{report.get('ended_at', '')}`",
        "",
        "## Runs",
        "",
        "| Run | Variant | Status | Reserved | Telemetry | Completed | Failed | Timed out | Launch failed | Terminated | Active | Rejected | Repairs | Input tokens | Total tokens | Wall ms | Quality |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for run in report.get("runs") or []:
        metrics = dict(run.get("metrics") or {})
        totals = dict(metrics.get("totals") or {})
        tokens = dict(metrics.get("tokens") or {})
        attempts = dict(run.get("invocation_attempts") or {})
        quality = dict(run.get("quality") or {})
        lines.append(
            "| {run_id} | {variant} | {status} | {reserved} | {telemetry} | "
            "{completed} | {failed} | {timed_out} | {launch_failed} | {terminated} | "
            "{active} | {rejected} | {repairs} | {input_tokens} | {total_tokens} | "
            "{wall_ms} | {quality} |".format(
                run_id=run.get("run_id", ""),
                variant=run.get("variant", ""),
                status=run.get("status", ""),
                reserved=_display(attempts.get("reserved_count")),
                telemetry=totals.get("invocation_count", 0),
                completed=_display(attempts.get("completed_count")),
                failed=_display(attempts.get("failed_count")),
                timed_out=_display(attempts.get("timeout_count")),
                launch_failed=_display(attempts.get("launch_failed_count")),
                terminated=_display(attempts.get("terminated_count")),
                active=_display(attempts.get("active_count")),
                rejected=_display(attempts.get("rejected_count")),
                repairs=totals.get("contract_repair_count", 0),
                input_tokens=tokens.get("input", 0),
                total_tokens=tokens.get("total", 0),
                wall_ms=run.get("wall_duration_ms", 0),
                quality="pass" if quality.get("passed") else "fail",
            )
        )
    for pair in report.get("pairs") or []:
        lines.extend(
            [
                "",
                f"## Pair {int(pair.get('pair_index') or 0):03d}",
                "",
                f"- Comparable: `{str(bool(pair.get('comparable'))).lower()}`",
            ]
        )
        reasons = pair.get("inconclusive_reasons") or []
        if reasons:
            lines.append(f"- Inconclusive reasons: `{', '.join(str(item) for item in reasons)}`")
        comparison = dict(pair.get("comparison") or {})
        end_to_end = dict(comparison.get("end_to_end") or {})
        if end_to_end:
            lines.extend(
                [
                    "",
                    "| Metric | Before | After | Delta | Reduction | Reduction % |",
                    "| --- | ---: | ---: | ---: | ---: | ---: |",
                ]
            )
            for metric_name, values in end_to_end.items():
                values = dict(values or {})
                missing = "unpriced" if metric_name == "estimated_cost_usd" else "N/A"
                lines.append(
                    f"| {metric_name} | {_display(values.get('before'), missing=missing)} | "
                    f"{_display(values.get('after'), missing=missing)} | "
                    f"{_display(values.get('delta'), missing=missing)} | "
                    f"{_display(values.get('reduction'), missing=missing)} | "
                    f"{_display(values.get('reduction_percent'), missing=missing)} |"
                )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            str((report.get("interpretation") or {}).get("note") or ""),
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "build_report",
    "render_markdown",
    "utc_now_iso",
    "write_json_atomic",
    "write_jsonl_atomic",
    "write_run_artifacts",
    "write_text_atomic",
]
