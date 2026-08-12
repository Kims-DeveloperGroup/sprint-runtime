from __future__ import annotations

import json
import math
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from teams_runtime.runtime.execution_policy import (
    DEFAULT_MODEL_EXECUTION_POLICY,
    InvocationReservation,
    ModelExecutionPolicy,
    ModelExecutionPolicyViolation,
    ModelInvocationTimeout,
    quarantine_unsafe_workspace_entries,
)
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
CODEX_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
    }
)
BENCHMARK_DISABLED_CODEX_FEATURES = (
    "apps",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "enable_fanout",
    "enable_mcp_apps",
    "hooks",
    "image_generation",
    "in_app_browser",
    "multi_agent",
    "multi_agent_v2",
    "plugin_sharing",
    "plugins",
    "standalone_web_search",
    "web_search_request",
    "workspace_dependencies",
)
BENCHMARK_PROVIDER_ENVIRONMENT_KEYS = (
    "CODEX_API_KEY",
    "CODEX_HOME",
    "CURL_CA_BUNDLE",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_ORG_ID",
    "OPENAI_PROJECT_ID",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TEMP",
    "TMP",
    "TMPDIR",
)
_BENCHMARK_LAUNCHER_PATH = Path(__file__).with_name(
    "benchmark_launcher.py"
)
_BENCHMARK_LAUNCH_READY_BYTE = b"\x01"


class _BenchmarkProcessSafetyAbort(BaseException):
    """Abort the dedicated worker when provider cleanup cannot be proven."""


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
    completed_tool_calls = 0
    completed_tool_item_ids: set[tuple[str, str]] = set()
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
            normalized_item_type = item_type.lower().replace("-", "_").replace(".", "_")
            if (
                event_type in {"item.completed", "item/completed"}
                and (
                    normalized_item_type in CODEX_TOOL_ITEM_TYPES
                    or normalized_item_type.endswith("_tool_call")
                )
            ):
                item_id = str(item.get("id") or item.get("item_id") or "").strip()
                item_identity = (normalized_item_type, item_id)
                if not item_id or item_identity not in completed_tool_item_ids:
                    completed_tool_calls += 1
                    if item_id:
                        completed_tool_item_ids.add(item_identity)
            if item_type in {"agent_message", "message"} or event_type == "agent_message":
                candidate_text = item.get("text") or item.get("message") or item.get("content")
                if isinstance(candidate_text, list):
                    candidate_text = "".join(
                        str(part.get("text") or "") if isinstance(part, dict) else str(part)
                        for part in candidate_text
                    )
                if candidate_text:
                    final_message = str(candidate_text).strip()
    if usage.source == "native" or completed_tool_calls:
        usage = ModelUsage.from_values(
            input_tokens=usage.input_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            output_tokens=usage.output_tokens,
            reasoning_output_tokens=usage.reasoning_output_tokens,
            total_tokens=usage.total_tokens,
            tool_calls=max(usage.tool_calls or 0, completed_tool_calls),
        )
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
            if (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                and value >= 0
            ):
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
        execution_policy: ModelExecutionPolicy | None = None,
    ):
        self.runtime_config = runtime_config
        self.role = str(role or "").strip()
        self.telemetry_recorder = telemetry_recorder
        self.execution_policy = execution_policy or DEFAULT_MODEL_EXECUTION_POLICY

    def _cli_version(self, cli_name: str) -> str:
        if self.telemetry_recorder is None or not self.telemetry_recorder.enabled:
            return ""
        executable = (
            self._codex_executable()
            if cli_name == "codex"
            else cli_name
        )
        cached = self._version_cache.get(executable)
        if cached is not None:
            return cached
        try:
            run_options: dict[str, Any] = {}
            if self.execution_policy.benchmark_mode:
                run_options["env"] = self._provider_environment()
                run_options["timeout"] = min(
                    float(self.execution_policy.call_timeout_seconds or 10.0),
                    10.0,
                )
            process = subprocess.run(
                [executable, "--version"],
                capture_output=True,
                text=True,
                check=False,
                **run_options,
            )
            version = str(process.stdout or process.stderr or "").strip().splitlines()[0]
        except Exception:
            version = ""
        self._version_cache[executable] = version
        return version

    def _codex_executable(self) -> str:
        if not self.execution_policy.benchmark_mode:
            return "codex"
        executable = self.execution_policy.codex_executable
        if executable is None:
            raise ModelExecutionPolicyViolation(
                "Benchmark execution has no pinned Codex executable."
            )
        return str(executable)

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
                self.execution_policy.assert_workspace_allowed(resolved)
                seen.add(resolved_text)
                extra_dirs.append(resolved_text)
        return extra_dirs

    def _provider_environment(self) -> dict[str, str]:
        if not self.execution_policy.benchmark_mode:
            return {**os.environ, "HOME": str(Path.home())}
        environment = {
            key: value
            for key in BENCHMARK_PROVIDER_ENVIRONMENT_KEYS
            if (value := os.environ.get(key)) is not None
        }
        # The explicit policy must override outer HOME, CODEX_HOME, temp, Git,
        # and locale values while provider-only auth variables remain available.
        environment.update(self.execution_policy.shell_environment)
        environment.setdefault("PATH", os.defpath)
        environment["NO_COLOR"] = "1"
        return environment

    def _append_benchmark_codex_controls(
        self,
        command: list[str],
        *,
        supports_sandbox_option: bool,
    ) -> None:
        if supports_sandbox_option:
            command.extend(["--sandbox", "workspace-write"])
        command.extend(
            [
                "--ignore-user-config",
                "--ignore-rules",
                "-c",
                'approval_policy="never"',
                "-c",
                'sandbox_mode="workspace-write"',
                "-c",
                "sandbox_workspace_write.exclude_slash_tmp=true",
                "-c",
                "sandbox_workspace_write.exclude_tmpdir_env_var=true",
                "-c",
                "mcp_servers={}",
                "-c",
                'shell_environment_policy.inherit="none"',
            ]
        )
        for name, value in self.execution_policy.shell_environment.items():
            command.extend(
                [
                    "-c",
                    f"shell_environment_policy.set.{name}={json.dumps(value, ensure_ascii=True)}",
                ]
            )
        for feature_name in BENCHMARK_DISABLED_CODEX_FEATURES:
            command.extend(["--disable", feature_name])

    @staticmethod
    def _process_group_exists(process_group_id: int | None) -> bool:
        if (
            process_group_id is None
            or process_group_id <= 1
            or not hasattr(os, "killpg")
        ):
            return False
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    @classmethod
    def _stop_remaining_process_group(
        cls,
        process_group_id: int | None,
        *,
        grace_seconds: float,
    ) -> None:
        if not cls._process_group_exists(process_group_id):
            return
        assert process_group_id is not None
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            pass
        deadline = time.monotonic() + max(grace_seconds, 0.0)
        while cls._process_group_exists(process_group_id) and time.monotonic() < deadline:
            time.sleep(0.05)
        if cls._process_group_exists(process_group_id):
            try:
                os.killpg(process_group_id, signal.SIGKILL)
            except ProcessLookupError:
                return
            except OSError:
                pass
        kill_deadline = time.monotonic() + max(grace_seconds, 0.1)
        while cls._process_group_exists(process_group_id) and time.monotonic() < kill_deadline:
            time.sleep(0.05)
        if cls._process_group_exists(process_group_id):
            raise _BenchmarkProcessSafetyAbort(
                "Benchmark provider process-group cleanup could not be proven."
            )

    def _quarantine_workspace_after_provider(self, workspace: Path) -> None:
        integrity_root = self.execution_policy.allowed_workspace_root or workspace
        try:
            removed_kinds = quarantine_unsafe_workspace_entries(integrity_root)
        except (ModelExecutionPolicyViolation, OSError, ValueError) as exc:
            raise _BenchmarkProcessSafetyAbort(
                "Benchmark workspace integrity could not be proven after provider exit."
            ) from exc
        if removed_kinds:
            raise ModelExecutionPolicyViolation(
                "Benchmark provider created unsafe filesystem entries; they were quarantined."
            )

    @classmethod
    def _terminate_process_group(
        cls,
        process: subprocess.Popen[str],
        *,
        process_group_id: int | None,
        grace_seconds: float,
    ) -> tuple[str, str]:
        def send_group_signal(group_signal: signal.Signals) -> None:
            try:
                if process_group_id is not None and hasattr(os, "killpg"):
                    os.killpg(process_group_id, group_signal)
                elif process.poll() is None and group_signal == signal.SIGTERM:
                    process.terminate()
                elif process.poll() is None:
                    process.kill()
            except ProcessLookupError:
                pass

        send_group_signal(signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            send_group_signal(signal.SIGKILL)
            stdout, stderr = process.communicate()
        cls._stop_remaining_process_group(
            process_group_id,
            grace_seconds=grace_seconds,
        )
        return str(stdout or ""), str(stderr or "")

    def _run_benchmark_process(
        self,
        command: list[str],
        *,
        cwd: Path,
        stdin_input: str | None,
        env: dict[str, str],
        reservation: InvocationReservation,
    ) -> subprocess.CompletedProcess[str]:
        if os.name != "posix":
            raise ModelExecutionPolicyViolation(
                "Benchmark provider launch requires POSIX file-descriptor handoff."
            )
        ready_read_fd, ready_write_fd = os.pipe()
        try:
            try:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        str(_BENCHMARK_LAUNCHER_PATH),
                        "--ready-fd",
                        str(ready_read_fd),
                        "--",
                        *command,
                    ],
                    cwd=str(cwd),
                    stdin=(
                        subprocess.PIPE
                        if stdin_input is not None
                        else None
                    ),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    start_new_session=True,
                    pass_fds=(ready_read_fd,),
                )
            finally:
                os.close(ready_read_fd)
        except BaseException:
            try:
                os.close(ready_write_fd)
            except OSError:
                pass
            raise

        # The launcher is the future provider process: exec preserves both its
        # PID and its session/process-group identity.
        process_group_id = process.pid
        try:
            reservation.mark_started(
                pid=process.pid,
                process_group_id=process_group_id,
            )
            if (
                os.write(
                    ready_write_fd,
                    _BENCHMARK_LAUNCH_READY_BYTE,
                )
                != len(_BENCHMARK_LAUNCH_READY_BYTE)
            ):
                raise OSError("Benchmark provider launch handoff was incomplete.")
        except BaseException:
            try:
                os.close(ready_write_fd)
            except OSError:
                pass
            self._terminate_process_group(
                process,
                process_group_id=process_group_id,
                grace_seconds=float(self.execution_policy.kill_grace_seconds),
            )
            raise
        else:
            try:
                os.close(ready_write_fd)
            except OSError:
                pass

        timeout_seconds = float(self.execution_policy.call_timeout_seconds or 0)
        try:
            stdout, stderr = process.communicate(
                input=stdin_input,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            stdout, stderr = self._terminate_process_group(
                process,
                process_group_id=process_group_id,
                grace_seconds=float(self.execution_policy.kill_grace_seconds),
            )
            completed_process = subprocess.CompletedProcess(
                command,
                process.returncode,
                stdout,
                stderr,
            )
            raise ModelInvocationTimeout(
                timeout_seconds,
                completed_process=completed_process,
            )
        self._stop_remaining_process_group(
            process_group_id,
            grace_seconds=float(self.execution_policy.kill_grace_seconds),
        )
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            str(stdout or ""),
            str(stderr or ""),
        )

    @staticmethod
    def _reservation_result(
        *,
        completed: bool,
        process: Any,
        captured_error: BaseException | None,
    ) -> tuple[str, str]:
        if isinstance(captured_error, ModelInvocationTimeout):
            return "timeout", "timeout"
        if process is None:
            return "launch_failed", (
                normalized_error_category(captured_error) or "launch_failed"
            )
        if process.returncode not in (None, 0):
            return "failed", "nonzero_exit"
        if captured_error is not None or not completed:
            return "failed", (
                normalized_error_category(captured_error, exit_code=process.returncode)
                or "runner_error"
            )
        return "completed", "completed"

    def _build_command(
        self,
        *,
        workspace: Path,
        prompt: str,
        session_id: str | None,
        output_file: Path | None,
        bypass_sandbox: bool,
    ) -> tuple[list[str], str | None]:
        is_gemini = "gemini" in self.runtime_config.model.lower()

        if is_gemini:
            if self.execution_policy.benchmark_mode:
                raise ModelExecutionPolicyViolation(
                    "Benchmark execution currently supports only the Codex CLI because "
                    "Gemini cannot provide the same non-interactive workspace-write policy."
                )
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

        command = [self._codex_executable(), "exec"]
        if session_id:
            command.extend(["resume", "--model", self.runtime_config.model])
            if not self.execution_policy.benchmark_mode:
                if output_file is None:
                    raise ValueError("Codex output path is required")
                command.extend(["-o", str(output_file)])
            command.append("--skip-git-repo-check")
            if self.execution_policy.benchmark_mode:
                self._append_benchmark_codex_controls(
                    command,
                    supports_sandbox_option=False,
                )
            elif bypass_sandbox:
                command.append("--dangerously-bypass-approvals-and-sandbox")
            else:
                command.append("--full-auto")
            command.append("--json")
            command.extend(["-c", f'model_reasoning_effort="{self.runtime_config.reasoning}"'])
            command.extend(["-c", 'personality="friendly"'])
            command.extend([session_id, "-"])
            return command, prompt
        command.extend(["-", "--model", self.runtime_config.model])
        if not self.execution_policy.benchmark_mode:
            if output_file is None:
                raise ValueError("Codex output path is required")
            command.extend(["-o", str(output_file)])
        command.extend(["--skip-git-repo-check", "-C", str(workspace)])
        for extra_dir in self._discover_extra_writable_dirs(workspace):
            command.extend(["--add-dir", extra_dir])
        if self.execution_policy.benchmark_mode:
            self._append_benchmark_codex_controls(
                command,
                supports_sandbox_option=True,
            )
        elif bypass_sandbox:
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
        self.execution_policy.assert_workspace_allowed(abs_workspace)
        if self.execution_policy.benchmark_mode and bypass_sandbox:
            raise ModelExecutionPolicyViolation(
                "Benchmark execution forbids sandbox bypass requests."
            )
        if self.execution_policy.benchmark_mode:
            for directory_key in ("HOME", "CODEX_HOME", "TMPDIR", "TMP", "TEMP"):
                directory_value = self.execution_policy.shell_environment.get(directory_key)
                if not directory_value:
                    continue
                directory_path = Path(directory_value).expanduser().resolve()
                self.execution_policy.assert_workspace_allowed(directory_path)
                directory_path.mkdir(mode=0o700, parents=True, exist_ok=True)
        output_file = (
            None
            if self.execution_policy.benchmark_mode
            else abs_workspace / ".teams_runtime_codex_output.txt"
        )
        if output_file is not None:
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
        env = self._provider_environment()
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
        reservation: InvocationReservation | None = None
        try:
            if self.execution_policy.benchmark_mode:
                budget = self.execution_policy.invocation_budget
                if budget is None:
                    raise ModelExecutionPolicyViolation(
                        "Benchmark execution has no invocation budget."
                    )
                reservation = budget.reserve(
                    invocation_context,
                    provider="gemini_cli" if is_gemini else "codex_cli",
                    role=self.role,
                )
                try:
                    process = self._run_benchmark_process(
                        command,
                        cwd=abs_workspace,
                        stdin_input=stdin_input,
                        env=env,
                        reservation=reservation,
                    )
                except ModelInvocationTimeout as exc:
                    process = exc.completed_process
                    self._quarantine_workspace_after_provider(abs_workspace)
                    raise
                self._quarantine_workspace_after_provider(abs_workspace)
            else:
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
                if output_file is not None and output_file.exists():
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
            if reservation is not None:
                reservation_state, stop_reason = self._reservation_result(
                    completed=completed,
                    process=process,
                    captured_error=captured_error,
                )
                reservation.complete(
                    state=reservation_state,
                    exit_code=process.returncode if process is not None else None,
                    stop_reason=stop_reason,
                )
            should_record_telemetry = (
                not self.execution_policy.benchmark_mode or reservation is not None
            )
            if (
                should_record_telemetry
                and self.telemetry_recorder is not None
                and invocation_context is not None
            ):
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
