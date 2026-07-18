from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from teams_runtime.runtime.model_telemetry import (
    ModelInvocationContext,
    ModelTelemetryRecorder,
    ModelUsage,
    normalized_error_category,
)
from teams_runtime.shared.models import RoleRuntimeConfig
from teams_runtime.shared.persistence import runtime_now


SESSION_ID_PATTERN = re.compile(r"session id:\s*([0-9a-fA-F-]+)", re.IGNORECASE)
LOGGER = logging.getLogger(__name__)


def _nested_mapping(payload: Any, *keys: str) -> dict[str, Any]:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _first_value(payload: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in payload:
            return payload.get(name)
    return None


def _usage_from_mapping(payload: dict[str, Any]) -> ModelUsage:
    return ModelUsage.from_values(
        input_tokens=_first_value(payload, "input_tokens", "inputTokens", "prompt_tokens", "promptTokens"),
        cached_input_tokens=_first_value(
            payload,
            "cached_input_tokens",
            "cachedInputTokens",
            "cached_tokens",
            "cachedTokens",
            "cached",
        ),
        output_tokens=_first_value(payload, "output_tokens", "outputTokens", "candidate_tokens", "candidateTokens"),
        reasoning_output_tokens=_first_value(
            payload,
            "reasoning_output_tokens",
            "reasoningOutputTokens",
            "reasoning_tokens",
            "thoughts",
        ),
        total_tokens=_first_value(payload, "total_tokens", "totalTokens"),
        tool_calls=_first_value(payload, "tool_calls", "toolCalls"),
    )


def parse_codex_jsonl(stdout: str) -> tuple[str | None, ModelUsage, str]:
    session_id: str | None = None
    usage = ModelUsage()
    final_message = ""
    for raw_line in str(stdout or "").splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or event.get("method") or "").strip()
        params = event.get("params") if isinstance(event.get("params"), dict) else {}
        candidate_session = (
            event.get("thread_id")
            or event.get("session_id")
            or params.get("thread_id")
            or params.get("session_id")
        )
        if candidate_session:
            session_id = str(candidate_session).strip() or session_id
        if event_type in {"turn.completed", "turn/completed", "task_complete"}:
            usage_payload = event.get("usage")
            if not isinstance(usage_payload, dict):
                usage_payload = params.get("usage") if isinstance(params.get("usage"), dict) else {}
            if not usage_payload:
                usage_payload = _nested_mapping(event, "turn", "usage")
            candidate_usage = _usage_from_mapping(usage_payload)
            if candidate_usage.source == "native":
                usage = candidate_usage
        if event_type in {"item.completed", "item/completed", "agent_message"}:
            item = event.get("item") if isinstance(event.get("item"), dict) else params.get("item")
            item = item if isinstance(item, dict) else event
            item_type = str(item.get("type") or item.get("kind") or "").strip()
            if item_type in {"agent_message", "message"} or event_type == "agent_message":
                candidate_text = item.get("text") or item.get("message") or item.get("content")
                if isinstance(candidate_text, list):
                    candidate_text = "".join(
                        str(part.get("text") or "") if isinstance(part, dict) else str(part)
                        for part in candidate_text
                    )
                if candidate_text:
                    final_message = str(candidate_text).strip()
    return session_id, usage, final_message


def parse_gemini_usage(stats: Any) -> ModelUsage:
    if not isinstance(stats, dict):
        return ModelUsage()
    if any(key in stats for key in ("input_tokens", "inputTokens", "total_tokens", "totalTokens")):
        return _usage_from_mapping(stats)
    models = stats.get("models")
    if not isinstance(models, dict):
        return ModelUsage()
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
    }
    observed = {name: False for name in totals}
    for model_metrics in models.values():
        if not isinstance(model_metrics, dict):
            continue
        tokens = model_metrics.get("tokens") if isinstance(model_metrics.get("tokens"), dict) else model_metrics
        aliases = {
            "input_tokens": ("prompt", "input_tokens", "inputTokens", "input"),
            "cached_input_tokens": ("cached", "cached_input_tokens", "cachedInputTokens"),
            "output_tokens": ("candidates", "output_tokens", "outputTokens"),
            "reasoning_output_tokens": ("thoughts", "reasoning_output_tokens", "reasoningOutputTokens"),
            "total_tokens": ("total", "total_tokens", "totalTokens"),
        }
        for target, names in aliases.items():
            value = _first_value(tokens, *names)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                totals[target] += int(value)
                observed[target] = True
    tools = stats.get("tools") if isinstance(stats.get("tools"), dict) else {}
    return ModelUsage.from_values(
        **{name: totals[name] if observed[name] else None for name in totals},
        tool_calls=_first_value(tools, "totalCalls", "total_calls", "tool_calls"),
    )


def extract_json_object(text: str) -> dict[str, Any]:
    normalized = str(text or "").strip()
    if not normalized:
        raise ValueError("Empty response.")

    candidates: list[str] = [normalized]
    if normalized.startswith("```"):
        lines = normalized.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            candidates.append("\n".join(lines[1:-1]).strip())

        fenced_segments: list[str] = []
        segment_lines: list[str] = []
        in_fenced_json = False
        for line in lines:
            stripped = line.strip()
            if not in_fenced_json:
                if stripped.startswith("```"):
                    in_fenced_json = True
                    segment_lines = []
                continue
            if stripped == "```":
                fenced_segments.append("\n".join(segment_lines).strip())
                segment_lines = []
                in_fenced_json = False
                continue
            segment_lines.append(line)

        merged_fenced = "\n".join(segment for segment in fenced_segments if segment).strip()
        if merged_fenced:
            candidates.append(merged_fenced)

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload

    decoder = json.JSONDecoder()
    for index, char in enumerate(normalized):
        if char != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(normalized[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("No JSON object found in response.")


class CodexRunner:
    _version_cache: dict[str, str] = {}

    def __init__(
        self,
        runtime_config: RoleRuntimeConfig,
        *,
        role: str = "",
        telemetry_recorder: ModelTelemetryRecorder | None = None,
    ):
        self.runtime_config = runtime_config
        self.role = str(role or "").strip()
        self.telemetry_recorder = telemetry_recorder

    def _cli_version(self, cli_name: str) -> str:
        if self.telemetry_recorder is None or not self.telemetry_recorder.enabled:
            return ""
        cached = self._version_cache.get(cli_name)
        if cached is not None:
            return cached
        try:
            process = subprocess.run(
                [cli_name, "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
            version = str(process.stdout or process.stderr or "").strip().splitlines()[0]
        except Exception:
            version = ""
        self._version_cache[cli_name] = version
        return version

    def _discover_extra_writable_dirs(self, workspace: Path) -> list[str]:
        extra_dirs: list[str] = []
        seen: set[str] = set()
        for directory_name in ("workspace", "shared_workspace", ".teams_runtime"):
            candidate = workspace / directory_name
            if not candidate.exists():
                continue
            try:
                resolved = candidate.resolve()
            except OSError:
                continue
            resolved_text = str(resolved)
            if resolved != workspace and resolved_text not in seen:
                seen.add(resolved_text)
                extra_dirs.append(resolved_text)
        return extra_dirs

    def _build_command(
        self,
        *,
        workspace: Path,
        prompt: str,
        session_id: str | None,
        output_file: Path,
        bypass_sandbox: bool,
    ) -> tuple[list[str], str | None]:
        is_gemini = "gemini" in self.runtime_config.model.lower()

        if is_gemini:
            command = ["gemini"]
            if session_id:
                command.extend(["--resume", session_id])
            command.extend(["--model", self.runtime_config.model])

            for extra_dir in self._discover_extra_writable_dirs(workspace):
                command.extend(["--include-directories", extra_dir])

            if bypass_sandbox:
                command.append("--yolo")

            command.extend(["--output-format", "json"])
            command.extend(["--prompt", prompt])
            return command, None

        command = ["codex", "exec"]
        if session_id:
            command.extend(
                [
                    "resume",
                    "--model",
                    self.runtime_config.model,
                    "-o",
                    str(output_file),
                    "--skip-git-repo-check",
                ]
            )
            if bypass_sandbox:
                command.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                command.append("--full-auto")
            command.append("--json")
            command.extend(["-c", f'model_reasoning_effort="{self.runtime_config.reasoning}"'])
            command.extend(["-c", 'personality="friendly"'])
            command.extend([session_id, "-"])
            return command, prompt
        command.extend(
            [
                "-",
                "--model",
                self.runtime_config.model,
                "-o",
                str(output_file),
                "--skip-git-repo-check",
                "-C",
                str(workspace),
            ]
        )
        for extra_dir in self._discover_extra_writable_dirs(workspace):
            command.extend(["--add-dir", extra_dir])
        if bypass_sandbox:
            command.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            command.append("--full-auto")
        command.append("--json")
        command.extend(["-c", f'model_reasoning_effort="{self.runtime_config.reasoning}"'])
        command.extend(["-c", 'personality="friendly"'])
        return command, prompt

    def run(
        self,
        workspace: Path,
        prompt: str,
        session_id: str | None,
        *,
        bypass_sandbox: bool = False,
        invocation_context: ModelInvocationContext | None = None,
    ) -> tuple[str, str | None]:
        abs_workspace = workspace.expanduser().resolve()
        output_file = abs_workspace / ".teams_runtime_codex_output.txt"
        try:
            output_file.unlink()
        except FileNotFoundError:
            pass
        command, stdin_input = self._build_command(
            workspace=abs_workspace,
            prompt=prompt,
            session_id=session_id,
            output_file=output_file,
            bypass_sandbox=bypass_sandbox,
        )

        is_gemini = "gemini" in self.runtime_config.model.lower()
        env = {**os.environ, "HOME": str(Path.home())}
        if is_gemini:
            env["GEMINI_SYSTEM_MD"] = str(abs_workspace / "GEMINI.md")
            gemini_dir = abs_workspace / ".gemini"
            gemini_dir.mkdir(parents=True, exist_ok=True)
            skills_symlink = gemini_dir / "skills"
            agents_skills = abs_workspace / ".agents" / "skills"
            if agents_skills.exists() and not skills_symlink.exists():
                try:
                    skills_symlink.symlink_to(agents_skills)
                except OSError:
                    pass

        started_at = runtime_now()
        started_monotonic = time.monotonic()
        process = None
        output = ""
        resolved_session_id = session_id
        usage = ModelUsage()
        captured_error: BaseException | None = None
        completed = False
        try:
            process = subprocess.run(
                command,
                cwd=str(abs_workspace),
                capture_output=True,
                input=stdin_input,
                text=True,
                env=env,
                check=False,
            )
            if is_gemini:
                try:
                    res_json = json.loads(process.stdout)
                    output = str(res_json.get("response") or "").strip()
                    resolved_session_id = res_json.get("session_id") or res_json.get("sessionId") or session_id
                    usage = parse_gemini_usage(res_json.get("stats"))
                    if not output and res_json.get("error"):
                        error_info = res_json.get("error")
                        output = error_info.get("message") if isinstance(error_info, dict) else str(error_info)
                except json.JSONDecodeError:
                    output = process.stdout.strip() or process.stderr.strip()
            else:
                combined = "\n".join(part for part in [process.stdout.strip(), process.stderr.strip()] if part).strip()
                event_session_id, usage, final_message = parse_codex_jsonl(process.stdout)
                session_match = SESSION_ID_PATTERN.search(combined)
                resolved_session_id = event_session_id or (session_match.group(1).strip() if session_match else session_id)
                if output_file.exists():
                    output = output_file.read_text(encoding="utf-8").strip()
                if not output:
                    output = final_message or process.stderr.strip() or process.stdout.strip()

            if process.returncode != 0:
                cli_label = "Gemini" if is_gemini else "Codex"
                if output:
                    try:
                        extract_json_object(output)
                    except ValueError:
                        raise RuntimeError(output or f"{cli_label} command failed.")
                    LOGGER.warning(
                        "[%s] %s command exited with code %s but produced a valid JSON payload; preserving role result",
                        self.role,
                        cli_label,
                        process.returncode,
                    )
                else:
                    raise RuntimeError(f"{cli_label} command failed.")
            completed = True
            return output, resolved_session_id
        except BaseException as exc:
            captured_error = exc
            raise
        finally:
            if self.telemetry_recorder is not None and invocation_context is not None:
                cli_name = "gemini" if is_gemini else "codex"
                exit_code = process.returncode if process is not None else None
                ended_at = runtime_now()
                duration_ms = int((time.monotonic() - started_monotonic) * 1000)
                cli_version = self._cli_version(cli_name)
                self.telemetry_recorder.record(
                    invocation_context,
                    provider="gemini_cli" if is_gemini else "codex_cli",
                    model=self.runtime_config.model,
                    reasoning="" if is_gemini else self.runtime_config.reasoning,
                    cli_version=cli_version,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_ms=duration_ms,
                    session_id_before=session_id,
                    session_id_after=resolved_session_id,
                    status="completed" if completed else "failed",
                    exit_code=exit_code,
                    error_category=normalized_error_category(captured_error, exit_code=exit_code),
                    prompt_chars=len(prompt),
                    output_chars=len(output),
                    usage=usage,
                )


__all__ = ["CodexRunner", "extract_json_object", "parse_codex_jsonl", "parse_gemini_usage"]
