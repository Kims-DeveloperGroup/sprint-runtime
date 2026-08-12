from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from teams_runtime.runtime.identities import sanitize_runtime_identity
from teams_runtime.shared.models import ModelRateCard, TelemetryRuntimeConfig
from teams_runtime.shared.paths import RuntimePaths
from teams_runtime.shared.persistence import RUNTIME_TIMEZONE, normalize_runtime_datetime, runtime_now, runtime_now_iso


LOGGER = logging.getLogger(__name__)
TELEMETRY_SCHEMA_VERSION = 1
TELEMETRY_WARNING_INTERVAL_SECONDS = 60.0
VALID_ATTEMPT_KINDS = {"primary", "sandbox_retry", "contract_repair"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        normalized = int(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return normalized if normalized >= 0 else None


def _timestamp(value: datetime | None = None) -> str:
    return normalize_runtime_datetime(value).isoformat() if value is not None else runtime_now_iso()


def hash_session_id(value: str | None) -> str:
    normalized = _text(value)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def normalized_error_category(exc: BaseException | None, *, exit_code: int | None = None) -> str:
    if exc is None and exit_code in (None, 0):
        return ""
    if isinstance(exc, FileNotFoundError):
        return "cli_not_found"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if exit_code not in (None, 0):
        return "nonzero_exit"
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return "provider_output_invalid"
    return "provider_exception" if exc is not None else "unknown"


@dataclass(slots=True, frozen=True)
class ModelUsage:
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None
    total_tokens: int | None = None
    tool_calls: int | None = None
    source: str = "unavailable"

    @classmethod
    def from_values(
        cls,
        *,
        input_tokens: Any = None,
        cached_input_tokens: Any = None,
        output_tokens: Any = None,
        reasoning_output_tokens: Any = None,
        total_tokens: Any = None,
        tool_calls: Any = None,
    ) -> "ModelUsage":
        values = {
            "input_tokens": _optional_non_negative_int(input_tokens),
            "cached_input_tokens": _optional_non_negative_int(cached_input_tokens),
            "output_tokens": _optional_non_negative_int(output_tokens),
            "reasoning_output_tokens": _optional_non_negative_int(reasoning_output_tokens),
            "total_tokens": _optional_non_negative_int(total_tokens),
            "tool_calls": _optional_non_negative_int(tool_calls),
        }
        if values["total_tokens"] is None and values["input_tokens"] is not None and values["output_tokens"] is not None:
            values["total_tokens"] = values["input_tokens"] + values["output_tokens"]
        token_values = tuple(values[name] for name in values if name != "tool_calls")
        return cls(**values, source="native" if any(value is not None for value in token_values) else "unavailable")


@dataclass(slots=True, frozen=True)
class ModelInvocationContext:
    invocation_id: str
    operation_id: str
    logical_call_id: str
    attempt_index: int
    attempt_kind: str
    runtime_identity: str
    role: str
    purpose: str
    workflow_step: str = ""
    request_id: str = ""
    sprint_id: str = ""
    todo_id: str = ""
    backlog_id: str = ""
    goal_id: str = ""
    prompt_context_enabled: bool | None = None
    prompt_context_total_events: int | None = None
    prompt_context_included_events: int | None = None
    prompt_context_omitted_events: int | None = None
    prompt_context_recent_events: int | None = None
    prompt_context_max_events: int | None = None
    prompt_context_selection_policy: str = ""


class InvocationSequence:
    def __init__(
        self,
        *,
        runtime_identity: str,
        role: str,
        purpose: str,
        workflow_step: str = "",
        request_id: str = "",
        sprint_id: str = "",
        todo_id: str = "",
        backlog_id: str = "",
        goal_id: str = "",
        operation_id: str | None = None,
    ):
        self.runtime_identity = _text(runtime_identity)
        self.role = _text(role)
        self.purpose = _text(purpose) or "role_task"
        self.workflow_step = _text(workflow_step)
        self.request_id = _text(request_id)
        self.sprint_id = _text(sprint_id)
        self.todo_id = _text(todo_id)
        self.backlog_id = _text(backlog_id)
        self.goal_id = _text(goal_id)
        self.operation_id = _text(operation_id) or uuid.uuid4().hex
        self.logical_call_id = uuid.uuid4().hex
        self._attempt_index = 0
        self.prompt_context_enabled: bool | None = None
        self.prompt_context_total_events: int | None = None
        self.prompt_context_included_events: int | None = None
        self.prompt_context_omitted_events: int | None = None
        self.prompt_context_recent_events: int | None = None
        self.prompt_context_max_events: int | None = None
        self.prompt_context_selection_policy = ""

    @classmethod
    def from_request(
        cls,
        *,
        runtime_identity: str,
        role: str,
        purpose: str,
        request_record: dict[str, Any],
        envelope: Any = None,
        sprint_id: str = "",
    ) -> "InvocationSequence":
        params = request_record.get("params") if isinstance(request_record.get("params"), dict) else {}
        workflow = params.get("workflow") if isinstance(params.get("workflow"), dict) else request_record.get("workflow")
        workflow = workflow if isinstance(workflow, dict) else {}
        return cls(
            runtime_identity=runtime_identity,
            role=role,
            purpose=purpose,
            workflow_step=_text(workflow.get("step")),
            request_id=_text(request_record.get("request_id") or getattr(envelope, "request_id", "")),
            sprint_id=_text(request_record.get("sprint_id") or sprint_id),
            todo_id=_text(request_record.get("todo_id")),
            backlog_id=_text(request_record.get("backlog_id")),
            goal_id=_text(request_record.get("goal_id") or params.get("goal_id")),
        )

    def start_logical_call(self, *, purpose: str | None = None) -> None:
        self.logical_call_id = uuid.uuid4().hex
        self._attempt_index = 0
        if purpose is not None:
            self.purpose = _text(purpose) or self.purpose

    def set_prompt_context_projection(
        self,
        projection: Any,
        *,
        enabled: bool,
        selection_policy: str = "",
    ) -> None:
        """Attach content-free prompt projection evidence to subsequent attempts."""
        self.prompt_context_enabled = bool(enabled)
        self.prompt_context_total_events = _optional_non_negative_int(
            getattr(projection, "total_events", None)
        )
        self.prompt_context_included_events = _optional_non_negative_int(
            getattr(projection, "included_events", None)
        )
        self.prompt_context_omitted_events = _optional_non_negative_int(
            getattr(projection, "omitted_events", None)
        )
        self.prompt_context_recent_events = _optional_non_negative_int(
            getattr(projection, "recent_events", None)
        )
        self.prompt_context_max_events = _optional_non_negative_int(
            getattr(projection, "max_events", None)
        )
        self.prompt_context_selection_policy = _text(selection_policy)

    def clear_prompt_context_projection(self) -> None:
        self.prompt_context_enabled = None
        self.prompt_context_total_events = None
        self.prompt_context_included_events = None
        self.prompt_context_omitted_events = None
        self.prompt_context_recent_events = None
        self.prompt_context_max_events = None
        self.prompt_context_selection_policy = ""

    def next(self, attempt_kind: str = "primary") -> ModelInvocationContext:
        normalized_kind = _text(attempt_kind)
        if normalized_kind not in VALID_ATTEMPT_KINDS:
            raise ValueError(f"Unsupported telemetry attempt kind: {attempt_kind}")
        self._attempt_index += 1
        return ModelInvocationContext(
            invocation_id=uuid.uuid4().hex,
            operation_id=self.operation_id,
            logical_call_id=self.logical_call_id,
            attempt_index=self._attempt_index,
            attempt_kind=normalized_kind,
            runtime_identity=self.runtime_identity,
            role=self.role,
            purpose=self.purpose,
            workflow_step=self.workflow_step,
            request_id=self.request_id,
            sprint_id=self.sprint_id,
            todo_id=self.todo_id,
            backlog_id=self.backlog_id,
            goal_id=self.goal_id,
            prompt_context_enabled=self.prompt_context_enabled,
            prompt_context_total_events=self.prompt_context_total_events,
            prompt_context_included_events=self.prompt_context_included_events,
            prompt_context_omitted_events=self.prompt_context_omitted_events,
            prompt_context_recent_events=self.prompt_context_recent_events,
            prompt_context_max_events=self.prompt_context_max_events,
            prompt_context_selection_policy=self.prompt_context_selection_policy,
        )


def calculate_estimated_cost(
    usage: ModelUsage,
    rate_card: ModelRateCard | None,
) -> float | None:
    if rate_card is None:
        return None
    if rate_card.per_invocation_usd is not None:
        return rate_card.per_invocation_usd
    if usage.input_tokens is None or usage.output_tokens is None:
        return None
    input_rate = rate_card.input_per_million_usd
    output_rate = rate_card.output_per_million_usd
    if input_rate is None or output_rate is None:
        return None
    cached_tokens = min(usage.cached_input_tokens or 0, usage.input_tokens)
    uncached_tokens = max(usage.input_tokens - cached_tokens, 0)
    cached_rate = rate_card.cached_input_per_million_usd
    if cached_rate is None:
        cached_rate = input_rate
    cost = (
        uncached_tokens * input_rate
        + cached_tokens * cached_rate
        + usage.output_tokens * output_rate
    ) / 1_000_000
    return round(cost, 12)


class ModelTelemetryRecorder:
    def __init__(
        self,
        paths: RuntimePaths,
        runtime_identity: str,
        config: TelemetryRuntimeConfig | None = None,
        *,
        output_dir: Path | None = None,
    ):
        self.paths = paths
        self.runtime_identity = _text(runtime_identity) or "unknown"
        self.config = config or TelemetryRuntimeConfig()
        self.output_dir = (
            Path(output_dir).expanduser().resolve()
            if output_dir is not None
            else paths.model_invocations_dir
        )
        self._last_warning_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled)

    def _warning(self, exc: BaseException) -> None:
        now = time.monotonic()
        if now - self._last_warning_at < TELEMETRY_WARNING_INTERVAL_SECONDS:
            return
        self._last_warning_at = now
        LOGGER.warning("Model telemetry write failed for %s: %s", self.runtime_identity, type(exc).__name__)

    def record(
        self,
        context: ModelInvocationContext,
        *,
        provider: str,
        model: str,
        reasoning: str,
        cli_version: str,
        started_at: datetime,
        ended_at: datetime,
        duration_ms: int,
        session_id_before: str | None,
        session_id_after: str | None,
        status: str,
        exit_code: int | None,
        error_category: str,
        prompt_chars: int,
        output_chars: int,
        usage: ModelUsage | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            normalized_usage = usage or ModelUsage()
            provider_key = f"{_text(provider)}/{_text(model)}"
            rate_card = self.config.rate_cards.get(provider_key)
            estimated_cost = calculate_estimated_cost(normalized_usage, rate_card)
            session_id = _text(session_id_after or session_id_before)
            record = {
                "schema_version": TELEMETRY_SCHEMA_VERSION,
                "invocation_id": context.invocation_id,
                "operation_id": context.operation_id,
                "logical_call_id": context.logical_call_id,
                "attempt_index": context.attempt_index,
                "attempt_kind": context.attempt_kind,
                "started_at": _timestamp(started_at),
                "ended_at": _timestamp(ended_at),
                "duration_ms": max(int(duration_ms), 0),
                "pid": os.getpid(),
                "runtime_identity": context.runtime_identity or self.runtime_identity,
                "role": context.role,
                "purpose": context.purpose,
                "workflow_step": context.workflow_step,
                "request_id": context.request_id,
                "sprint_id": context.sprint_id,
                "todo_id": context.todo_id,
                "backlog_id": context.backlog_id,
                "goal_id": context.goal_id,
                "prompt_context_enabled": context.prompt_context_enabled,
                "prompt_context_total_events": context.prompt_context_total_events,
                "prompt_context_included_events": context.prompt_context_included_events,
                "prompt_context_omitted_events": context.prompt_context_omitted_events,
                "prompt_context_recent_events": context.prompt_context_recent_events,
                "prompt_context_max_events": context.prompt_context_max_events,
                "prompt_context_selection_policy": context.prompt_context_selection_policy,
                "provider": _text(provider),
                "model": _text(model),
                "reasoning": _text(reasoning),
                "cli_version": _text(cli_version),
                "session_mode": (
                    "not_applicable"
                    if _text(provider) == "gemini_deep_research"
                    else ("resume" if _text(session_id_before) else "new")
                ),
                "session_id_hash": hash_session_id(session_id),
                "status": "completed" if _text(status) == "completed" else "failed",
                "exit_code": exit_code,
                "error_category": _text(error_category),
                "prompt_chars": max(int(prompt_chars), 0),
                "output_chars": max(int(output_chars), 0),
                "tool_calls": normalized_usage.tool_calls,
                "input_tokens": normalized_usage.input_tokens,
                "cached_input_tokens": normalized_usage.cached_input_tokens,
                "output_tokens": normalized_usage.output_tokens,
                "reasoning_output_tokens": normalized_usage.reasoning_output_tokens,
                "total_tokens": normalized_usage.total_tokens,
                "usage_source": normalized_usage.source,
                "estimated_cost_usd": estimated_cost,
                "rate_card": asdict(rate_card) if rate_card is not None else None,
            }
            day = normalize_runtime_datetime(started_at).date().isoformat()
            identity = sanitize_runtime_identity(context.runtime_identity or self.runtime_identity)
            path = self.output_dir / day / f"{identity}.{os.getpid()}.jsonl"
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            try:
                path.parent.chmod(0o700)
            except OSError:
                pass
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
            try:
                path.chmod(0o600)
            except OSError:
                pass
        except Exception as exc:  # Telemetry is deliberately fail-open.
            self._warning(exc)


def record_external_invocation(
    recorder: ModelTelemetryRecorder,
    context: ModelInvocationContext,
    *,
    provider: str,
    model: str,
    reasoning: str,
    started_at: datetime,
    started_monotonic: float,
    status: str,
    prompt_chars: int,
    output_chars: int,
    error_category: str = "",
) -> None:
    ended_at = runtime_now()
    recorder.record(
        context,
        provider=provider,
        model=model,
        reasoning=reasoning,
        cli_version="",
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=int((time.monotonic() - started_monotonic) * 1000),
        session_id_before=None,
        session_id_after=None,
        status=status,
        exit_code=None,
        error_category=error_category,
        prompt_chars=prompt_chars,
        output_chars=output_chars,
        usage=ModelUsage(),
    )


def run_with_optional_telemetry(
    runner: Any,
    workspace: Path,
    prompt: str,
    session_id: str | None,
    *,
    invocation_context: ModelInvocationContext,
    bypass_sandbox: bool = False,
) -> tuple[str, str | None]:
    run_method = runner.run
    try:
        parameters = inspect.signature(run_method).parameters
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        accepts_context = "invocation_context" in parameters or accepts_kwargs
        accepts_bypass = "bypass_sandbox" in parameters or accepts_kwargs
    except (TypeError, ValueError):
        accepts_context = True
        accepts_bypass = True
    kwargs: dict[str, Any] = {}
    if accepts_bypass:
        kwargs["bypass_sandbox"] = bypass_sandbox
    if accepts_context:
        kwargs["invocation_context"] = invocation_context
    return run_method(workspace, prompt, session_id, **kwargs)


def run_task_with_optional_telemetry_purpose(
    runtime: Any,
    envelope: Any,
    request_record: dict[str, Any],
    *,
    telemetry_purpose: str,
) -> dict[str, Any]:
    run_method = runtime.run_task
    try:
        parameters = inspect.signature(run_method).parameters
        accepts_purpose = "telemetry_purpose" in parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
    except (TypeError, ValueError):
        accepts_purpose = True
    if accepts_purpose:
        return run_method(
            envelope,
            request_record,
            telemetry_purpose=telemetry_purpose,
        )
    return run_method(envelope, request_record)


def _parse_record_timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value))
    except ValueError:
        return None
    return normalize_runtime_datetime(parsed)


def _date_range(started_at: datetime, ended_at: datetime) -> Iterable[str]:
    current = normalize_runtime_datetime(started_at).date()
    last = normalize_runtime_datetime(ended_at).date()
    while current <= last:
        yield current.isoformat()
        current += timedelta(days=1)


def _nearest_rank(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(math.ceil(percentile * len(ordered)) - 1, 0)
    return ordered[index]


def _empty_group(role: str, purpose: str, provider: str, model: str) -> dict[str, Any]:
    return {
        "role": role,
        "purpose": purpose,
        "provider": provider,
        "model": model,
        "invocation_count": 0,
        "primary_count": 0,
        "failed_count": 0,
        "contract_repair_count": 0,
        "sandbox_retry_count": 0,
        "tool_call_count": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "uncached_input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "prompt_context_observed_count": 0,
        "prompt_context_compacted_count": 0,
        "duration_ms": 0,
        "estimated_cost_usd": None,
    }


def aggregate_model_invocations(
    paths: RuntimePaths,
    *,
    hours: float = 24.0,
    request_id: str = "",
    sprint_id: str = "",
    role: str = "",
    now: datetime | None = None,
) -> dict[str, Any]:
    if not math.isfinite(hours) or hours <= 0:
        raise ValueError("hours must be a positive finite number")
    ended_at = normalize_runtime_datetime(now or runtime_now())
    started_at = ended_at - timedelta(hours=hours)
    filters = {
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "request_id": _text(request_id),
        "sprint_id": _text(sprint_id),
        "role": _text(role),
    }
    invocations: list[dict[str, Any]] = []
    invalid_count = 0
    for day in _date_range(started_at, ended_at):
        day_dir = paths.model_invocations_dir / day
        if not day_dir.is_dir():
            continue
        for shard in sorted(day_dir.glob("*.jsonl")):
            try:
                handle = shard.open("r", encoding="utf-8")
            except OSError:
                invalid_count += 1
                continue
            with handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        invalid_count += 1
                        continue
                    if not isinstance(record, dict) or record.get("schema_version") != TELEMETRY_SCHEMA_VERSION:
                        invalid_count += 1
                        continue
                    record_started = _parse_record_timestamp(record.get("started_at"))
                    if record_started is None:
                        invalid_count += 1
                        continue
                    if record_started < started_at or record_started > ended_at:
                        continue
                    if filters["request_id"] and _text(record.get("request_id")) != filters["request_id"]:
                        continue
                    if filters["sprint_id"] and _text(record.get("sprint_id")) != filters["sprint_id"]:
                        continue
                    if filters["role"] and _text(record.get("role")) != filters["role"]:
                        continue
                    invocations.append(record)

    durations: list[int] = []
    logical_calls: set[str] = set()
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    group_priced_counts: dict[tuple[str, str, str, str], int] = {}
    token_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    tokens = {name: 0 for name in token_fields}
    completed_count = failed_count = primary_count = repair_count = sandbox_count = 0
    prompt_chars = output_chars = 0
    native_usage_count = priced_count = tool_call_coverage_count = 0
    tool_call_count = 0
    prompt_context_observed_count = 0
    prompt_context_enabled_count = 0
    prompt_context_eligible_count = 0
    prompt_context_compacted_count = 0
    prompt_context_total_events = 0
    prompt_context_included_events = 0
    prompt_context_omitted_events = 0
    prompt_context_selection_policies: set[str] = set()
    total_cost = 0.0
    for record in invocations:
        logical_id = _text(record.get("logical_call_id"))
        if logical_id:
            logical_calls.add(logical_id)
        duration = _optional_non_negative_int(record.get("duration_ms")) or 0
        durations.append(duration)
        prompt_chars += _optional_non_negative_int(record.get("prompt_chars")) or 0
        output_chars += _optional_non_negative_int(record.get("output_chars")) or 0
        if _text(record.get("status")) == "completed":
            completed_count += 1
        else:
            failed_count += 1
        attempt_kind = _text(record.get("attempt_kind"))
        if attempt_kind == "primary":
            primary_count += 1
        if attempt_kind == "contract_repair":
            repair_count += 1
        if attempt_kind == "sandbox_retry":
            sandbox_count += 1
        if _text(record.get("usage_source")) == "native":
            native_usage_count += 1
        for name in token_fields:
            tokens[name] += _optional_non_negative_int(record.get(name)) or 0
        input_tokens = _optional_non_negative_int(record.get("input_tokens"))
        cached_input_tokens = _optional_non_negative_int(record.get("cached_input_tokens")) or 0
        uncached_input_tokens = (
            max(input_tokens - min(cached_input_tokens, input_tokens), 0)
            if input_tokens is not None
            else 0
        )
        tool_calls = _optional_non_negative_int(record.get("tool_calls"))
        if tool_calls is not None:
            tool_call_coverage_count += 1
            tool_call_count += tool_calls

        prompt_context_enabled = record.get("prompt_context_enabled")
        context_counts = tuple(
            _optional_non_negative_int(record.get(name))
            for name in (
                "prompt_context_total_events",
                "prompt_context_included_events",
                "prompt_context_omitted_events",
            )
        )
        prompt_context_observed = isinstance(prompt_context_enabled, bool) and all(
            value is not None for value in context_counts
        )
        prompt_context_compacted = False
        if prompt_context_observed:
            total_events, included_events, omitted_events = context_counts
            prompt_context_observed_count += 1
            prompt_context_enabled_count += int(prompt_context_enabled)
            prompt_context_total_events += int(total_events or 0)
            prompt_context_included_events += int(included_events or 0)
            prompt_context_omitted_events += int(omitted_events or 0)
            max_events = _optional_non_negative_int(record.get("prompt_context_max_events"))
            if max_events is not None and int(total_events or 0) > max_events:
                prompt_context_eligible_count += 1
            prompt_context_compacted = bool(prompt_context_enabled) and int(omitted_events or 0) > 0
            prompt_context_compacted_count += int(prompt_context_compacted)
            selection_policy = _text(record.get("prompt_context_selection_policy"))
            if selection_policy:
                prompt_context_selection_policies.add(selection_policy)
        cost = record.get("estimated_cost_usd")
        if isinstance(cost, (int, float)) and math.isfinite(float(cost)):
            priced_count += 1
            total_cost += float(cost)

        key = tuple(_text(record.get(name)) for name in ("role", "purpose", "provider", "model"))
        group = groups.setdefault(key, _empty_group(*key))
        group["invocation_count"] += 1
        group["duration_ms"] += duration
        if attempt_kind == "primary":
            group["primary_count"] += 1
        if _text(record.get("status")) != "completed":
            group["failed_count"] += 1
        if attempt_kind == "contract_repair":
            group["contract_repair_count"] += 1
        if attempt_kind == "sandbox_retry":
            group["sandbox_retry_count"] += 1
        group["tool_call_count"] += tool_calls or 0
        group["uncached_input_tokens"] += uncached_input_tokens
        if prompt_context_observed:
            group["prompt_context_observed_count"] += 1
        if prompt_context_compacted:
            group["prompt_context_compacted_count"] += 1
        for source, target in (
            ("input_tokens", "input_tokens"),
            ("cached_input_tokens", "cached_input_tokens"),
            ("output_tokens", "output_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            group[target] += _optional_non_negative_int(record.get(source)) or 0
        if isinstance(cost, (int, float)) and math.isfinite(float(cost)):
            group["estimated_cost_usd"] = round((group["estimated_cost_usd"] or 0.0) + float(cost), 12)
            group_priced_counts[key] = group_priced_counts.get(key, 0) + 1

    count = len(invocations)
    for key, group in groups.items():
        if group_priced_counts.get(key, 0) != group["invocation_count"]:
            group["estimated_cost_usd"] = None
    return {
        "schema_version": TELEMETRY_SCHEMA_VERSION,
        "generated_at": runtime_now_iso(),
        "filters": filters,
        "totals": {
            "invocation_count": count,
            "physical_attempt_count": count,
            "logical_call_count": len(logical_calls),
            "primary_count": primary_count,
            "completed_count": completed_count,
            "failed_count": failed_count,
            "contract_repair_count": repair_count,
            "sandbox_retry_count": sandbox_count,
            "tool_call_count": tool_call_count,
            "prompt_chars": prompt_chars,
            "output_chars": output_chars,
            "estimated_cost_usd": (
                round(total_cost, 12) if count and priced_count == count else None
            ),
            "token_coverage_percent": round(native_usage_count * 100 / count, 2) if count else 0.0,
            "tool_call_coverage_percent": (
                round(tool_call_coverage_count * 100 / count, 2) if count else 0.0
            ),
            "pricing_coverage_percent": round(priced_count * 100 / count, 2) if count else 0.0,
            "invalid_record_count": invalid_count,
        },
        "tokens": {
            "input": tokens["input_tokens"],
            "cached_input": tokens["cached_input_tokens"],
            "uncached_input": sum(
                int(group["uncached_input_tokens"]) for group in groups.values()
            ),
            "output": tokens["output_tokens"],
            "reasoning_output": tokens["reasoning_output_tokens"],
            "total": tokens["total_tokens"],
        },
        "prompt_context": {
            "observed_invocation_count": prompt_context_observed_count,
            "enabled_invocation_count": prompt_context_enabled_count,
            "eligible_invocation_count": prompt_context_eligible_count,
            "compacted_invocation_count": prompt_context_compacted_count,
            "total_events": prompt_context_total_events,
            "included_events": prompt_context_included_events,
            "omitted_events": prompt_context_omitted_events,
            "coverage_percent": (
                round(prompt_context_observed_count * 100 / count, 2) if count else 0.0
            ),
            "selection_policies": sorted(prompt_context_selection_policies),
        },
        "latency_ms": {
            "total": sum(durations),
            "p50": _nearest_rank(durations, 0.50),
            "p95": _nearest_rank(durations, 0.95),
            "max": max(durations, default=0),
        },
        "groups": sorted(
            groups.values(),
            key=lambda item: (-int(item["total_tokens"]), -int(item["duration_ms"]), item["role"], item["purpose"]),
        ),
    }


def render_model_metrics_summary(summary: dict[str, Any]) -> str:
    totals = dict(summary.get("totals") or {})
    tokens = dict(summary.get("tokens") or {})
    latency = dict(summary.get("latency_ms") or {})
    filters = dict(summary.get("filters") or {})
    lines = [
        "Model telemetry",
        f"window={filters.get('started_at')}..{filters.get('ended_at')}",
        (
            f"invocations={totals.get('invocation_count', 0)} logical_calls={totals.get('logical_call_count', 0)} "
            f"completed={totals.get('completed_count', 0)} failed={totals.get('failed_count', 0)} "
            f"repairs={totals.get('contract_repair_count', 0)} sandbox_retries={totals.get('sandbox_retry_count', 0)}"
        ),
        (
            f"tokens input={tokens.get('input', 0)} cached={tokens.get('cached_input', 0)} "
            f"output={tokens.get('output', 0)} reasoning={tokens.get('reasoning_output', 0)} "
            f"total={tokens.get('total', 0)} coverage={totals.get('token_coverage_percent', 0.0):.2f}%"
        ),
        (
            f"latency_ms total={latency.get('total', 0)} p50={latency.get('p50', 0)} "
            f"p95={latency.get('p95', 0)} max={latency.get('max', 0)}"
        ),
    ]
    cost = totals.get("estimated_cost_usd")
    cost_text = "unpriced" if cost is None else f"${float(cost):.6f}"
    lines.append(
        f"estimated_cost={cost_text} pricing_coverage={totals.get('pricing_coverage_percent', 0.0):.2f}% "
        f"invalid_records={totals.get('invalid_record_count', 0)}"
    )
    if not totals.get("invocation_count"):
        lines.append("No model telemetry matched the requested filters.")
        return "\n".join(lines)
    lines.append("role\tpurpose\tprovider/model\tcalls\tfailures\trepairs\ttokens\tduration_ms\tcost")
    for group in summary.get("groups") or []:
        group_cost = group.get("estimated_cost_usd")
        group_cost_text = "unpriced" if group_cost is None else f"${float(group_cost):.6f}"
        lines.append(
            "\t".join(
                (
                    _text(group.get("role")) or "N/A",
                    _text(group.get("purpose")) or "N/A",
                    f"{_text(group.get('provider'))}/{_text(group.get('model'))}",
                    str(group.get("invocation_count", 0)),
                    str(group.get("failed_count", 0)),
                    str(group.get("contract_repair_count", 0)),
                    str(group.get("total_tokens", 0)),
                    str(group.get("duration_ms", 0)),
                    group_cost_text,
                )
            )
        )
    return "\n".join(lines)


__all__ = [
    "InvocationSequence",
    "ModelInvocationContext",
    "ModelTelemetryRecorder",
    "ModelUsage",
    "TELEMETRY_SCHEMA_VERSION",
    "aggregate_model_invocations",
    "calculate_estimated_cost",
    "hash_session_id",
    "normalized_error_category",
    "record_external_invocation",
    "render_model_metrics_summary",
    "run_task_with_optional_telemetry_purpose",
    "run_with_optional_telemetry",
]
