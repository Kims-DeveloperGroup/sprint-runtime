from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from teams_runtime.benchmarking.scenario import (
    BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
    BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
    BENCHMARK_TARGET_INCLUDED_EVENTS,
    BENCHMARK_TARGET_OMITTED_EVENTS,
    BENCHMARK_TARGET_PURPOSE,
    BENCHMARK_TARGET_ROLE,
    BENCHMARK_TARGET_TOTAL_EVENTS,
    BENCHMARK_TARGET_WORKFLOW_STEP,
)
from teams_runtime.shared.prompt_context import PROMPT_EVENT_SELECTION_POLICY


SAFE_INVOCATION_FIELDS = frozenset(
    {
        "schema_version",
        "invocation_id",
        "operation_id",
        "logical_call_id",
        "attempt_index",
        "attempt_kind",
        "started_at",
        "ended_at",
        "duration_ms",
        "pid",
        "runtime_identity",
        "role",
        "purpose",
        "workflow_step",
        "request_id",
        "sprint_id",
        "todo_id",
        "backlog_id",
        "goal_id",
        "provider",
        "model",
        "reasoning",
        "cli_version",
        "session_mode",
        "session_id_hash",
        "status",
        "exit_code",
        "error_category",
        "prompt_chars",
        "output_chars",
        "tool_calls",
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
        "usage_source",
        "estimated_cost_usd",
        "rate_card",
        "prompt_context_enabled",
        "prompt_context_total_events",
        "prompt_context_included_events",
        "prompt_context_omitted_events",
        "prompt_context_recent_events",
        "prompt_context_max_events",
        "prompt_context_selection_policy",
        "prompt_context_representation_conflict",
        "prompt_context",
    }
)
_RATE_CARD_FIELDS = frozenset(
    {
        "input_per_million_usd",
        "cached_input_per_million_usd",
        "output_per_million_usd",
        "per_invocation_usd",
    }
)
_PROMPT_CONTEXT_COUNT_FIELDS = (
    "total_events",
    "included_events",
    "omitted_events",
    "recent_events",
    "max_events",
)


def _non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _strict_non_negative_int(value: Any) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return None
    return value


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    return normalized if math.isfinite(normalized) else None


def _sanitize_rate_card(value: Any) -> dict[str, float | None] | None:
    if not isinstance(value, dict):
        return None
    sanitized: dict[str, float | None] = {}
    for field_name in _RATE_CARD_FIELDS:
        raw_value = value.get(field_name)
        if raw_value is None:
            sanitized[field_name] = None
            continue
        normalized = _finite_number(raw_value)
        if normalized is not None and normalized >= 0:
            sanitized[field_name] = normalized
    return sanitized or None


def _sanitize_prompt_context(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    sanitized: dict[str, Any] = {}
    if isinstance(value.get("enabled"), bool):
        sanitized["enabled"] = value["enabled"]
    if isinstance(value.get("compacted"), bool):
        sanitized["compacted"] = value["compacted"]
    for field_name in _PROMPT_CONTEXT_COUNT_FIELDS:
        normalized = _strict_non_negative_int(value.get(field_name))
        if normalized is not None:
            sanitized[field_name] = normalized
    selection_policy = str(value.get("selection_policy") or "").strip()
    if selection_policy == PROMPT_EVENT_SELECTION_POLICY:
        sanitized["selection_policy"] = selection_policy
    return sanitized or None


def _flat_prompt_context(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "enabled": record.get("prompt_context_enabled"),
        "total_events": record.get("prompt_context_total_events"),
        "included_events": record.get("prompt_context_included_events"),
        "omitted_events": record.get("prompt_context_omitted_events"),
        "recent_events": record.get("prompt_context_recent_events"),
        "max_events": record.get("prompt_context_max_events"),
        "selection_policy": record.get("prompt_context_selection_policy"),
    }


def _prompt_context_present(context: Mapping[str, Any]) -> bool:
    return isinstance(context.get("enabled"), bool) or any(
        context.get(field_name) is not None
        for field_name in _PROMPT_CONTEXT_COUNT_FIELDS
    ) or bool(str(context.get("selection_policy") or "").strip())


def _prompt_context_identity(context: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        context.get("enabled")
        if isinstance(context.get("enabled"), bool)
        else None,
        *(
            _non_negative_int(context.get(field_name))
            for field_name in _PROMPT_CONTEXT_COUNT_FIELDS
        ),
        str(context.get("selection_policy") or "").strip(),
    )


def sanitize_invocation_record(record: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = {
        key: value
        for key, value in record.items()
        if key in SAFE_INVOCATION_FIELDS
    }
    rate_card = _sanitize_rate_card(sanitized.get("rate_card"))
    if rate_card is None:
        sanitized.pop("rate_card", None)
    else:
        sanitized["rate_card"] = rate_card
    context = _sanitize_prompt_context(sanitized.get("prompt_context"))
    if context is None:
        sanitized.pop("prompt_context", None)
    else:
        sanitized["prompt_context"] = context
    if not isinstance(sanitized.get("prompt_context_enabled"), bool):
        sanitized["prompt_context_enabled"] = None
    for field_name in (
        "prompt_context_total_events",
        "prompt_context_included_events",
        "prompt_context_omitted_events",
        "prompt_context_recent_events",
        "prompt_context_max_events",
    ):
        sanitized[field_name] = _strict_non_negative_int(
            sanitized.get(field_name)
        )
    if (
        str(sanitized.get("prompt_context_selection_policy") or "").strip()
        != PROMPT_EVENT_SELECTION_POLICY
    ):
        sanitized["prompt_context_selection_policy"] = ""
    flat_context = _flat_prompt_context(sanitized)
    sanitized["prompt_context_representation_conflict"] = bool(
        sanitized.get("prompt_context_representation_conflict") is True
        or (
            context is not None
            and _prompt_context_present(context)
            and _prompt_context_present(flat_context)
            and _prompt_context_identity(context)
            != _prompt_context_identity(flat_context)
        )
    )
    return sanitized


def load_telemetry_directory(metrics_root: Path) -> tuple[dict[str, Any], ...]:
    """Load and sanitize recorder shards from one trusted telemetry directory."""

    records: list[dict[str, Any]] = []
    metrics_root = Path(metrics_root).expanduser().resolve()
    if not metrics_root.is_dir():
        return ()
    for shard in sorted(metrics_root.rglob("*.jsonl")):
        try:
            lines = shard.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(sanitize_invocation_record(record))
    records.sort(
        key=lambda item: (
            str(item.get("started_at") or ""),
            str(item.get("invocation_id") or ""),
        )
    )
    return tuple(records)


def load_workspace_telemetry(workspace_root: Path) -> tuple[dict[str, Any], ...]:
    """Load the normal runtime telemetry store outside benchmark evidence paths."""

    return load_telemetry_directory(
        workspace_root / ".teams_runtime" / "metrics" / "model_invocations"
    )


def _nearest_rank(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(math.ceil(percentile * len(ordered)) - 1, 0)
    return ordered[index]


def _prompt_context(record: Mapping[str, Any]) -> dict[str, Any]:
    flat = _flat_prompt_context(record)
    if _prompt_context_present(flat):
        return flat
    nested = record.get("prompt_context")
    if isinstance(nested, dict):
        return dict(nested)
    return flat


def _identity_digest(values: Iterable[Any]) -> str:
    normalized = sorted(str(value or "").strip() for value in values)
    canonical = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def is_v2_target_projection(
    value: Mapping[str, Any],
    *,
    state_field: str,
) -> bool:
    """Match the exact target using only canonical, journaled flat fields."""

    if value.get("prompt_context_representation_conflict") is True:
        return False
    enabled = value.get("prompt_context_enabled")
    invocation_id = str(value.get("invocation_id") or "").strip()
    if not isinstance(enabled, bool) or not invocation_id:
        return False
    expected_included = (
        BENCHMARK_TARGET_INCLUDED_EVENTS
        if enabled
        else BENCHMARK_TARGET_TOTAL_EVENTS
    )
    expected_omitted = BENCHMARK_TARGET_OMITTED_EVENTS if enabled else 0
    return (
        str(value.get(state_field) or "").strip() == "completed"
        and str(value.get("attempt_kind") or "").strip() == "primary"
        and str(value.get("role") or "").strip() == BENCHMARK_TARGET_ROLE
        and str(value.get("purpose") or "").strip() == BENCHMARK_TARGET_PURPOSE
        and str(value.get("workflow_step") or "").strip()
        == BENCHMARK_TARGET_WORKFLOW_STEP
        and _strict_non_negative_int(value.get("prompt_context_total_events"))
        == BENCHMARK_TARGET_TOTAL_EVENTS
        and _strict_non_negative_int(value.get("prompt_context_included_events"))
        == expected_included
        and _strict_non_negative_int(value.get("prompt_context_omitted_events"))
        == expected_omitted
        and _strict_non_negative_int(value.get("prompt_context_recent_events"))
        == BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS
        and _strict_non_negative_int(value.get("prompt_context_max_events"))
        == BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS
        and str(value.get("prompt_context_selection_policy") or "").strip()
        == PROMPT_EVENT_SELECTION_POLICY
    )


def reduce_telemetry(
    records: Iterable[Mapping[str, Any]],
    *,
    expected_invocation_count: int | None = None,
    coverage_available: bool = True,
    verified_target_projection_count: int = 0,
    verified_target_invocation_ids_sha256: str = "",
) -> dict[str, Any]:
    normalized = [sanitize_invocation_record(record) for record in records]
    if not isinstance(coverage_available, bool):
        raise ValueError("coverage_available must be a boolean")
    if expected_invocation_count is not None and (
        isinstance(expected_invocation_count, bool)
        or not isinstance(expected_invocation_count, int)
        or expected_invocation_count < 0
    ):
        raise ValueError("expected_invocation_count must be a non-negative integer")
    if not coverage_available and expected_invocation_count is not None:
        raise ValueError(
            "expected_invocation_count must be omitted when coverage is unavailable"
        )
    if (
        isinstance(verified_target_projection_count, bool)
        or not isinstance(verified_target_projection_count, int)
        or verified_target_projection_count < 0
    ):
        raise ValueError(
            "verified_target_projection_count must be a non-negative integer"
        )
    normalized_verified_target_digest = str(
        verified_target_invocation_ids_sha256 or ""
    ).strip().lower()
    if normalized_verified_target_digest and (
        len(normalized_verified_target_digest) != 64
        or any(
            character not in "0123456789abcdef"
            for character in normalized_verified_target_digest
        )
    ):
        raise ValueError(
            "verified_target_invocation_ids_sha256 must be empty or a SHA-256 digest"
        )
    observed_invocation_count = len(normalized)
    coverage_denominator = max(
        observed_invocation_count,
        (
            expected_invocation_count
            if expected_invocation_count is not None
            else observed_invocation_count
        ),
    )
    logical_calls = {
        str(record.get("logical_call_id") or "")
        for record in normalized
        if str(record.get("logical_call_id") or "")
    }
    durations: list[int] = []
    totals = {
        "invocation_count": observed_invocation_count,
        "coverage_basis": (
            "call_journal"
            if coverage_available and expected_invocation_count is not None
            else (
                "observed_telemetry"
                if coverage_available
                else "unavailable_untrusted_call_journal"
            )
        ),
        "expected_invocation_count": (
            coverage_denominator if coverage_available else None
        ),
        "unobserved_invocation_count": (
            max(coverage_denominator - observed_invocation_count, 0)
            if coverage_available
            else None
        ),
        "logical_call_count": len(logical_calls),
        "primary_count": 0,
        "contract_repair_count": 0,
        "sandbox_retry_count": 0,
        "completed_count": 0,
        "failed_count": 0,
        "tool_call_count": 0,
        "prompt_chars": 0,
        "output_chars": 0,
    }
    tokens = {
        "input": 0,
        "cached_input": 0,
        "uncached_input": 0,
        "output": 0,
        "reasoning_output": 0,
        "total": 0,
    }
    native_usage_count = 0
    tool_call_usage_count = 0
    priced_count = 0
    total_cost = 0.0
    compaction = {
        "observed_invocation_count": 0,
        "unobserved_invocation_count": 0,
        "enabled_invocation_count": 0,
        "eligible_invocation_count": 0,
        "disabled_eligible_invocation_count": 0,
        "compacted_invocation_count": 0,
        "invalid_projection_count": 0,
        "total_events": 0,
        "included_events": 0,
        "omitted_events": 0,
        "max_observed_events": 0,
        "max_included_events": 0,
        "expected_recent_events": BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
        "expected_max_events": BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
        "expected_selection_policy": PROMPT_EVENT_SELECTION_POLICY,
        "target_total_events": BENCHMARK_TARGET_TOTAL_EVENTS,
        "target_role": BENCHMARK_TARGET_ROLE,
        "target_purpose": BENCHMARK_TARGET_PURPOSE,
        "target_workflow_step": BENCHMARK_TARGET_WORKFLOW_STEP,
        "target_included_events_when_enabled": BENCHMARK_TARGET_INCLUDED_EVENTS,
        "target_omitted_events_when_enabled": BENCHMARK_TARGET_OMITTED_EVENTS,
        "target_projection_count": 0,
        "target_projection_candidate_count": 0,
        "target_projection_verification_mismatch_count": 0,
        "target_projection_invocation_ids_sha256": "",
        "target_projection_identity_reconciled": False,
        "selection_policies": [],
    }
    selection_policies: set[str] = set()
    target_candidate_invocation_ids: list[str] = []
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for record in normalized:
        attempt_kind = str(record.get("attempt_kind") or "")
        if attempt_kind == "primary":
            totals["primary_count"] += 1
        elif attempt_kind == "contract_repair":
            totals["contract_repair_count"] += 1
        elif attempt_kind == "sandbox_retry":
            totals["sandbox_retry_count"] += 1
        if str(record.get("status") or "") == "completed":
            totals["completed_count"] += 1
        else:
            totals["failed_count"] += 1
        duration = _non_negative_int(record.get("duration_ms")) or 0
        durations.append(duration)
        tool_calls = _non_negative_int(record.get("tool_calls"))
        if tool_calls is not None:
            tool_call_usage_count += 1
            totals["tool_call_count"] += tool_calls
        totals["prompt_chars"] += _non_negative_int(record.get("prompt_chars")) or 0
        totals["output_chars"] += _non_negative_int(record.get("output_chars")) or 0
        raw_input_tokens = _non_negative_int(record.get("input_tokens"))
        raw_cached_tokens = _non_negative_int(record.get("cached_input_tokens"))
        raw_output_tokens = _non_negative_int(record.get("output_tokens"))
        raw_total_tokens = _non_negative_int(record.get("total_tokens"))
        input_tokens = raw_input_tokens or 0
        cached_tokens = min(raw_cached_tokens or 0, input_tokens)
        output_tokens = raw_output_tokens or 0
        tokens["input"] += input_tokens
        tokens["cached_input"] += cached_tokens
        tokens["uncached_input"] += max(input_tokens - cached_tokens, 0)
        tokens["output"] += output_tokens
        tokens["reasoning_output"] += _non_negative_int(record.get("reasoning_output_tokens")) or 0
        effective_total_tokens = (
            raw_total_tokens
            if raw_total_tokens is not None
            else input_tokens + output_tokens
        )
        tokens["total"] += effective_total_tokens
        complete_native_usage = (
            str(record.get("usage_source") or "") == "native"
            and raw_input_tokens is not None
            and raw_output_tokens is not None
            and effective_total_tokens >= raw_input_tokens + raw_output_tokens
        )
        if complete_native_usage:
            native_usage_count += 1
        cost = _finite_number(record.get("estimated_cost_usd"))
        if cost is not None:
            priced_count += 1
            total_cost += cost
        context = _prompt_context(record)
        total_events = _non_negative_int(context.get("total_events"))
        included_events = _non_negative_int(context.get("included_events"))
        omitted_events = _non_negative_int(context.get("omitted_events"))
        recent_events = _non_negative_int(context.get("recent_events"))
        max_events = _non_negative_int(context.get("max_events"))
        enabled = context.get("enabled")
        selection_policy = str(context.get("selection_policy") or "").strip()
        representation_conflict = (
            record.get("prompt_context_representation_conflict") is True
        )
        projection_candidate = isinstance(enabled, bool) or any(
            value is not None
            for value in (
                total_events,
                included_events,
                omitted_events,
                recent_events,
                max_events,
            )
        ) or bool(selection_policy)
        projection_observed = isinstance(enabled, bool) and all(
            value is not None
            for value in (
                total_events,
                included_events,
                omitted_events,
                recent_events,
                max_events,
            )
        )
        projection_valid = False
        if projection_observed:
            compaction["observed_invocation_count"] += 1
            compaction["enabled_invocation_count"] += int(enabled)
            if selection_policy:
                selection_policies.add(selection_policy)
            projection_valid = (
                not representation_conflict
                and max_events == BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS
                and recent_events == BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS
                and total_events == included_events + omitted_events
                and selection_policy == PROMPT_EVENT_SELECTION_POLICY
                and (
                    (
                        enabled
                        and (
                            (total_events <= max_events and included_events == total_events and omitted_events == 0)
                            or (
                                total_events > max_events
                                and recent_events <= included_events <= max_events
                                and omitted_events > 0
                            )
                        )
                    )
                    or (
                        not enabled
                        and included_events == total_events
                        and omitted_events == 0
                    )
                )
            )
            if not projection_valid:
                compaction["invalid_projection_count"] += 1
        elif projection_candidate:
            compaction["invalid_projection_count"] += 1
        if projection_valid and total_events is not None:
            compaction["max_observed_events"] = max(compaction["max_observed_events"], total_events)
            compaction["total_events"] += total_events
            if max_events is not None and total_events > max_events:
                compaction["eligible_invocation_count"] += 1
                if not enabled:
                    compaction["disabled_eligible_invocation_count"] += 1
        if projection_valid and included_events is not None:
            compaction["included_events"] += included_events
            compaction["max_included_events"] = max(
                compaction["max_included_events"],
                included_events,
            )
        if projection_valid and omitted_events is not None:
            compaction["omitted_events"] += omitted_events
            if enabled and omitted_events > 0:
                compaction["compacted_invocation_count"] += 1
        if projection_valid and is_v2_target_projection(
            record,
            state_field="status",
        ):
            compaction["target_projection_candidate_count"] += 1
            target_candidate_invocation_ids.append(
                str(record.get("invocation_id") or "").strip()
            )

        key = tuple(
            str(record.get(field_name) or "")
            for field_name in ("role", "purpose", "provider", "model")
        )
        group = groups.setdefault(
            key,
            {
                "role": key[0],
                "purpose": key[1],
                "provider": key[2],
                "model": key[3],
                "invocation_count": 0,
                "failed_count": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "duration_ms": 0,
                "estimated_cost_usd": None,
                "_priced_count": 0,
            },
        )
        group["invocation_count"] += 1
        group["failed_count"] += int(str(record.get("status") or "") != "completed")
        group["input_tokens"] += input_tokens
        group["cached_input_tokens"] += cached_tokens
        group["output_tokens"] += output_tokens
        group["total_tokens"] += effective_total_tokens
        group["duration_ms"] += duration
        if cost is not None:
            group["_priced_count"] += 1
            group["estimated_cost_usd"] = round(
                (group["estimated_cost_usd"] or 0.0) + cost,
                12,
            )

    count = observed_invocation_count
    for group in groups.values():
        if (
            group.pop("_priced_count") != group["invocation_count"]
            or not coverage_available
            or count != coverage_denominator
        ):
            group["estimated_cost_usd"] = None
    token_coverage = (
        round(native_usage_count * 100 / coverage_denominator, 2)
        if coverage_denominator
        else 0.0
    )
    tool_call_coverage = (
        round(tool_call_usage_count * 100 / coverage_denominator, 2)
        if coverage_denominator
        else 0.0
    )
    pricing_coverage = (
        round(priced_count * 100 / coverage_denominator, 2)
        if coverage_denominator
        else 0.0
    )
    totals["token_coverage_percent"] = (
        token_coverage if coverage_available else None
    )
    totals["tool_call_coverage_percent"] = (
        tool_call_coverage if coverage_available else None
    )
    totals["pricing_coverage_percent"] = (
        pricing_coverage if coverage_available else None
    )
    totals["estimated_cost_usd"] = (
        round(total_cost, 12)
        if coverage_available
        and coverage_denominator
        and count == coverage_denominator
        and priced_count == coverage_denominator
        else None
    )
    compaction["unobserved_invocation_count"] = (
        coverage_denominator - compaction["observed_invocation_count"]
        if coverage_available
        else None
    )
    target_candidate_count = int(
        compaction["target_projection_candidate_count"]
    )
    compaction["target_projection_verification_mismatch_count"] = abs(
        target_candidate_count - verified_target_projection_count
    )
    target_candidate_digest = _identity_digest(target_candidate_invocation_ids)
    compaction["target_projection_invocation_ids_sha256"] = (
        target_candidate_digest
    )
    target_identity_reconciled = bool(
        normalized_verified_target_digest
        and target_candidate_count == verified_target_projection_count
        and target_candidate_digest == normalized_verified_target_digest
    )
    compaction["target_projection_identity_reconciled"] = (
        target_identity_reconciled
    )
    if target_identity_reconciled:
        compaction["target_projection_count"] = verified_target_projection_count
    compaction["selection_policies"] = sorted(selection_policies)
    return {
        "totals": totals,
        "tokens": tokens,
        "latency_ms": {
            "provider_total": sum(durations),
            "p50": _nearest_rank(durations, 0.50),
            "p95": _nearest_rank(durations, 0.95),
            "max": max(durations, default=0),
        },
        "compaction": compaction,
        "groups": sorted(
            groups.values(),
            key=lambda item: (
                -int(item["total_tokens"]),
                -int(item["duration_ms"]),
                item["role"],
                item["purpose"],
            ),
        ),
    }


def _primary_groups(
    records: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, str, str], list[Mapping[str, Any]]]:
    grouped: defaultdict[
        tuple[str, str, str],
        list[Mapping[str, Any]],
    ] = defaultdict(list)
    for record in records:
        if str(record.get("attempt_kind") or "") != "primary":
            continue
        base = tuple(
            str(record.get(field_name) or "")
            for field_name in ("role", "purpose", "workflow_step")
        )
        grouped[base].append(record)
    return dict(grouped)


def _primary_identity(record: Mapping[str, Any]) -> tuple[Any, ...]:
    context = _prompt_context(record)
    return (
        str(record.get("provider") or ""),
        str(record.get("model") or ""),
        str(record.get("reasoning") or ""),
        str(record.get("status") or ""),
        _non_negative_int(context.get("total_events")),
    )


def _delta(before: float | int | None, after: float | int | None) -> dict[str, Any]:
    if before is None or after is None:
        return {
            "before": before,
            "after": after,
            "delta": None,
            "change_percent": None,
            "reduction": None,
            "reduction_percent": None,
        }
    delta = after - before
    reduction = before - after
    return {
        "before": before,
        "after": after,
        "delta": delta,
        "change_percent": round(delta * 100 / before, 4) if before else None,
        "reduction": reduction,
        "reduction_percent": round(reduction * 100 / before, 4) if before else None,
    }


def compare_metrics(
    before_metrics: Mapping[str, Any],
    after_metrics: Mapping[str, Any],
    *,
    before_wall_duration_ms: int,
    after_wall_duration_ms: int,
    before_records: Iterable[Mapping[str, Any]],
    after_records: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    before_totals = dict(before_metrics.get("totals") or {})
    after_totals = dict(after_metrics.get("totals") or {})
    before_tokens = dict(before_metrics.get("tokens") or {})
    after_tokens = dict(after_metrics.get("tokens") or {})
    before_latency = dict(before_metrics.get("latency_ms") or {})
    after_latency = dict(after_metrics.get("latency_ms") or {})
    end_to_end = {
        "invocation_count": _delta(
            before_totals.get("invocation_count"),
            after_totals.get("invocation_count"),
        ),
        "logical_call_count": _delta(
            before_totals.get("logical_call_count"),
            after_totals.get("logical_call_count"),
        ),
        "contract_repair_count": _delta(
            before_totals.get("contract_repair_count"),
            after_totals.get("contract_repair_count"),
        ),
        "sandbox_retry_count": _delta(
            before_totals.get("sandbox_retry_count"),
            after_totals.get("sandbox_retry_count"),
        ),
        "failed_count": _delta(
            before_totals.get("failed_count"),
            after_totals.get("failed_count"),
        ),
        "tool_call_count": _delta(
            before_totals.get("tool_call_count"),
            after_totals.get("tool_call_count"),
        ),
        "prompt_chars": _delta(
            before_totals.get("prompt_chars"),
            after_totals.get("prompt_chars"),
        ),
        "input_tokens": _delta(
            before_tokens.get("input"),
            after_tokens.get("input"),
        ),
        "cached_input_tokens": _delta(
            before_tokens.get("cached_input"),
            after_tokens.get("cached_input"),
        ),
        "uncached_input_tokens": _delta(
            before_tokens.get("uncached_input"),
            after_tokens.get("uncached_input"),
        ),
        "output_tokens": _delta(
            before_tokens.get("output"),
            after_tokens.get("output"),
        ),
        "reasoning_output_tokens": _delta(
            before_tokens.get("reasoning_output"),
            after_tokens.get("reasoning_output"),
        ),
        "total_tokens": _delta(
            before_tokens.get("total"),
            after_tokens.get("total"),
        ),
        "provider_duration_ms": _delta(
            before_latency.get("provider_total"),
            after_latency.get("provider_total"),
        ),
        "wall_duration_ms": _delta(before_wall_duration_ms, after_wall_duration_ms),
        "estimated_cost_usd": _delta(
            before_totals.get("estimated_cost_usd"),
            after_totals.get("estimated_cost_usd"),
        ),
    }

    before_primary = _primary_groups(before_records)
    after_primary = _primary_groups(after_records)
    matched: list[dict[str, Any]] = []
    unmatched_before = 0
    unmatched_after = 0
    ambiguous_groups = 0
    for key in sorted(before_primary.keys() | after_primary.keys()):
        before_group = before_primary.get(key, [])
        after_group = after_primary.get(key, [])
        unambiguous = (
            len(before_group) == 1
            and len(after_group) == 1
            and _primary_identity(before_group[0]) == _primary_identity(after_group[0])
        )
        if not unambiguous:
            unmatched_before += len(before_group)
            unmatched_after += len(after_group)
            ambiguous_groups += int(bool(before_group) and bool(after_group))
            continue
        before = before_group[0]
        after = after_group[0]
        matched.append(
            {
                "role": key[0],
                "purpose": key[1],
                "workflow_step": key[2],
                "occurrence": 1,
                "prompt_chars": _delta(
                    _non_negative_int(before.get("prompt_chars")),
                    _non_negative_int(after.get("prompt_chars")),
                ),
                "input_tokens": _delta(
                    _non_negative_int(before.get("input_tokens")),
                    _non_negative_int(after.get("input_tokens")),
                ),
                "cached_input_tokens": _delta(
                    _non_negative_int(before.get("cached_input_tokens")),
                    _non_negative_int(after.get("cached_input_tokens")),
                ),
                "total_tokens": _delta(
                    _non_negative_int(before.get("total_tokens")),
                    _non_negative_int(after.get("total_tokens")),
                ),
                "duration_ms": _delta(
                    _non_negative_int(before.get("duration_ms")),
                    _non_negative_int(after.get("duration_ms")),
                ),
            }
        )
    return {
        "end_to_end": end_to_end,
        "matched_primary_invocations": matched,
        "matched_primary_count": len(matched),
        "unmatched_before_primary_count": unmatched_before,
        "unmatched_after_primary_count": unmatched_after,
        "ambiguous_primary_group_count": ambiguous_groups,
    }


__all__ = [
    "SAFE_INVOCATION_FIELDS",
    "compare_metrics",
    "is_v2_target_projection",
    "load_telemetry_directory",
    "load_workspace_telemetry",
    "reduce_telemetry",
    "sanitize_invocation_record",
]
