from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from teams_runtime.core.template import scaffold_workspace
from teams_runtime.shared.models import TEAM_ROLES


SCENARIO_ID = "sum-positive-full-sprint-v2"
DEFAULT_HISTORY_SEED_COUNT = 48
BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS = 8
BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS = 16
BENCHMARK_TARGET_TOTAL_EVENTS = DEFAULT_HISTORY_SEED_COUNT + 2
BENCHMARK_TARGET_INCLUDED_EVENTS = BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS
BENCHMARK_TARGET_OMITTED_EVENTS = (
    BENCHMARK_TARGET_TOTAL_EVENTS - BENCHMARK_TARGET_INCLUDED_EVENTS
)
BENCHMARK_TARGET_ROLE = "research"
BENCHMARK_TARGET_PURPOSE = "research_decision"
BENCHMARK_TARGET_WORKFLOW_STEP = "research_initial"
SCENARIO_MILESTONE = (
    "Fix sum_positive(values) so it returns the sum of positive values only. "
    "Use exactly `return sum(value for value in values if value > 0)` as its only "
    "non-docstring statement; do not add imports, decorators, annotations, defaults, "
    "or other definitions. Preserve the public function, do not alter benchmark tests "
    "or scenario metadata, run the unittest suite, and commit the completed change."
)
PROTECTED_PATHS = (
    ".benchmark/scenario.json",
    ".benchmark/history_seed.json",
    "tests/__init__.py",
    "tests/test_benchmark_app.py",
)
_RATE_FIELDS = (
    "input_per_million_usd",
    "cached_input_per_million_usd",
    "output_per_million_usd",
    "per_invocation_usd",
)
_MAX_ORACLE_SOURCE_BYTES = 32 * 1024
_MAX_PROTECTED_FILE_BYTES = 1024 * 1024
_GIT_COMMAND_TIMEOUT_SECONDS = 10.0
_EMPTY_FILE_SHA256 = hashlib.sha256(b"").hexdigest()
_GIT_CONFIG_OVERRIDES = (
    "-c",
    f"core.hooksPath={os.devnull}",
    "-c",
    "core.fsmonitor=false",
    "-c",
    f"core.attributesFile={os.devnull}",
    "-c",
    "diff.external=",
    "-c",
    "core.pager=",
    "-c",
    "pager.status=false",
    "-c",
    "pager.remote=false",
    "-c",
    "interactive.diffFilter=",
    "-c",
    "maintenance.auto=false",
    "-c",
    "gc.auto=0",
)


class ScenarioError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class RuntimeSettings:
    role_defaults: Mapping[str, Mapping[str, str]]
    rate_cards: Mapping[str, Mapping[str, float | None]]
    source_config_hash: str


@dataclass(slots=True, frozen=True)
class ScenarioWorkspace:
    root: Path
    initial_commit: str
    initial_commit_count: int
    protected_hashes: Mapping[str, str]
    config_hash: str
    comparable_config_hash: str
    history_hash: str
    history_seed: tuple[Mapping[str, Any], ...]
    git_executable: Path | None = None


@dataclass(slots=True, frozen=True)
class WorkspaceInspection:
    behavior_oracle_passed: bool
    protected_files_unchanged: bool
    git_clean: bool
    commit_created: bool
    no_git_remotes: bool
    head_sha: str
    notes: tuple[str, ...]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _read_regular_file_at(
    root: Path,
    relative_path: str,
    *,
    max_bytes: int,
) -> bytes:
    relative = Path(relative_path)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError(f"Unsafe relative path: {relative_path!r}")

    directory_flags = os.O_RDONLY | os.O_DIRECTORY
    file_flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        directory_flags |= os.O_CLOEXEC
        file_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        directory_flags |= os.O_NOFOLLOW
        file_flags |= os.O_NOFOLLOW

    directory_fd = os.open(root, directory_flags)
    try:
        for component in relative.parts[:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(relative.parts[-1], file_flags, dir_fd=directory_fd)
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
                raise ValueError(f"Unsafe file type or size: {relative_path}")
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(file_fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > max_bytes:
                raise ValueError(f"File exceeds size limit: {relative_path}")
            return payload
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _protected_file_hash(root: Path, relative_path: str) -> str:
    payload = _read_regular_file_at(
        root,
        relative_path,
        max_bytes=_MAX_PROTECTED_FILE_BYTES,
    )
    return hashlib.sha256(payload).hexdigest()


def _is_string_literal(statement: ast.stmt) -> bool:
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


def _without_optional_docstring(statements: list[ast.stmt]) -> list[ast.stmt]:
    if statements and _is_string_literal(statements[0]):
        return statements[1:]
    return statements


def _is_name(node: ast.AST, identifier: str, context: type[ast.expr_context]) -> bool:
    return (
        isinstance(node, ast.Name)
        and node.id == identifier
        and isinstance(node.ctx, context)
    )


def _is_constrained_sum_positive(module: ast.Module) -> bool:
    module_body = _without_optional_docstring(module.body)
    if len(module_body) != 1 or not isinstance(module_body[0], ast.FunctionDef):
        return False
    function = module_body[0]
    arguments = function.args
    if (
        function.name != "sum_positive"
        or function.decorator_list
        or function.returns is not None
        or getattr(function, "type_params", ())
        or arguments.posonlyargs
        or len(arguments.args) != 1
        or arguments.args[0].arg != "values"
        or arguments.args[0].annotation is not None
        or arguments.vararg is not None
        or arguments.kwonlyargs
        or arguments.kw_defaults
        or arguments.kwarg is not None
        or arguments.defaults
    ):
        return False
    function_body = _without_optional_docstring(function.body)
    if len(function_body) != 1 or not isinstance(function_body[0], ast.Return):
        return False
    call = function_body[0].value
    if (
        not isinstance(call, ast.Call)
        or not _is_name(call.func, "sum", ast.Load)
        or len(call.args) != 1
        or call.keywords
        or not isinstance(call.args[0], ast.GeneratorExp)
    ):
        return False
    generator = call.args[0]
    if (
        not _is_name(generator.elt, "value", ast.Load)
        or len(generator.generators) != 1
    ):
        return False
    comprehension = generator.generators[0]
    if (
        comprehension.is_async
        or not _is_name(comprehension.target, "value", ast.Store)
        or not _is_name(comprehension.iter, "values", ast.Load)
        or len(comprehension.ifs) != 1
    ):
        return False
    predicate = comprehension.ifs[0]
    return (
        isinstance(predicate, ast.Compare)
        and _is_name(predicate.left, "value", ast.Load)
        and len(predicate.ops) == 1
        and isinstance(predicate.ops[0], ast.Gt)
        and len(predicate.comparators) == 1
        and isinstance(predicate.comparators[0], ast.Constant)
        and type(predicate.comparators[0].value) is int
        and predicate.comparators[0].value == 0
    )


def _sum_positive_ast_oracle(root: Path) -> bool:
    try:
        payload = _read_regular_file_at(
            root,
            "benchmark_app.py",
            max_bytes=_MAX_ORACLE_SOURCE_BYTES,
        )
        source = payload.decode("utf-8")
        module = ast.parse(source, filename="benchmark_app.py", mode="exec")
    except (MemoryError, OSError, RecursionError, SyntaxError, UnicodeError, ValueError):
        return False
    return _is_constrained_sum_positive(module)


def _read_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a YAML mapping: {path}")
    return payload


def _runtime_config_file(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    return resolved / "team_runtime.yaml" if resolved.is_dir() else resolved


def _normalize_role_defaults(payload: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    raw_defaults = payload.get("role_defaults")
    if not isinstance(raw_defaults, dict):
        raise ValueError("Runtime config must define role_defaults")
    normalized: dict[str, dict[str, str]] = {}
    for role in TEAM_ROLES:
        raw = raw_defaults.get(role)
        if not isinstance(raw, dict):
            raise ValueError(f"Runtime config must define role_defaults.{role}")
        model = str(raw.get("model") or "").strip()
        reasoning = str(raw.get("reasoning") or "").strip()
        if not model or not reasoning:
            raise ValueError(f"role_defaults.{role} must define model and reasoning")
        normalized[role] = {"model": model, "reasoning": reasoning}
    return normalized


def _normalize_rate(value: Any, *, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a finite non-negative number")
    try:
        normalized = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite non-negative number") from exc
    if normalized < 0 or normalized in {float("inf"), float("-inf")} or normalized != normalized:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return normalized


def _normalize_rate_cards(payload: Mapping[str, Any]) -> dict[str, dict[str, float | None]]:
    raw_cards: Any = payload.get("rate_cards")
    if raw_cards is None and isinstance(payload.get("telemetry"), dict):
        raw_cards = payload["telemetry"].get("rate_cards")
    if raw_cards in (None, {}):
        return {}
    if not isinstance(raw_cards, dict):
        raise ValueError("rate_cards must be a mapping")
    cards: dict[str, dict[str, float | None]] = {}
    for raw_key, raw_card in raw_cards.items():
        key = str(raw_key or "").strip()
        if "/" not in key or not isinstance(raw_card, dict):
            raise ValueError(f"Invalid rate card entry: {raw_key!r}")
        normalized = {
            field_name: _normalize_rate(
                raw_card.get(field_name),
                field_name=f"rate_cards.{key}.{field_name}",
            )
            for field_name in _RATE_FIELDS
        }
        if normalized["per_invocation_usd"] is None and (
            normalized["input_per_million_usd"] is None
            or normalized["output_per_million_usd"] is None
        ):
            raise ValueError(
                f"rate_cards.{key} requires per_invocation_usd or both input and output rates"
            )
        cards[key] = normalized
    return cards


def load_runtime_settings(
    runtime_config_path: Path,
    *,
    rate_card_path: Path | None = None,
) -> RuntimeSettings:
    config_file = _runtime_config_file(runtime_config_path)
    payload = _read_yaml(config_file)
    role_defaults = _normalize_role_defaults(payload)
    rate_cards: dict[str, dict[str, float | None]] = {}
    if rate_card_path is not None:
        rate_cards = _normalize_rate_cards(_read_yaml(rate_card_path.expanduser().resolve()))
    source_snapshot = {"role_defaults": role_defaults, "rate_cards": rate_cards}
    return RuntimeSettings(
        role_defaults=role_defaults,
        rate_cards=rate_cards,
        source_config_hash=canonical_hash(source_snapshot),
    )


def build_history_seed(
    count: int = DEFAULT_HISTORY_SEED_COUNT,
) -> tuple[Mapping[str, Any], ...]:
    if count < 24:
        raise ValueError("History seed must contain at least 24 events")
    roles = (
        "research",
        "planner",
        "designer",
        "architect",
        "developer",
        "qa",
        "version_controller",
        "orchestrator",
    )
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    events: list[Mapping[str, Any]] = []
    for index in range(count):
        timestamp = (started + timedelta(minutes=index)).isoformat()
        if index < len(roles) * 2 and index % 2 == 1:
            role = roles[index // 2]
            event: Mapping[str, Any] = {
                "created_at": timestamp,
                "type": "role_report",
                "actor": role,
                "summary": f"Historical {role} checkpoint {index + 1:02d}.",
                "payload": {
                    "role": role,
                    "status": "completed",
                    "summary": f"Stable benchmark evidence {index + 1:02d}.",
                },
            }
        else:
            event = {
                "created_at": timestamp,
                "type": "benchmark_checkpoint",
                "actor": "orchestrator",
                "summary": f"Neutral historical checkpoint {index + 1:02d}.",
                "payload": {"sequence": index + 1},
            }
        events.append(event)
    return tuple(events)


def _benchmark_config(
    workspace_root: Path,
    *,
    benchmark_id: str,
    run_id: str,
    prompt_context_enabled: bool,
    settings: RuntimeSettings,
) -> tuple[str, str]:
    path = workspace_root / "team_runtime.yaml"
    payload = _read_yaml(path)
    sprint = dict(payload.get("sprint") or {})
    sprint.update(
        {
            "id": f"{benchmark_id}-{run_id.rsplit('-', 1)[0]}",
            "mode": "hybrid",
            "start_mode": "manual_daily",
            "ingress_mode": "backlog_first",
            "discovery_scope": "workspace_only",
            "discovery_actions": [],
        }
    )
    payload["sprint"] = sprint
    payload["role_defaults"] = {
        role: dict(settings.role_defaults[role])
        for role in TEAM_ROLES
    }
    payload["research_defaults"] = {
        "app": "",
        "notebook": "",
        "files": [],
        "mode": "",
        "profile_path": "",
        "completion_timeout": 600,
        "callback_timeout": 1200,
        "cleanup": False,
        "reasoning_level": "Standard",
    }
    payload["prompt_context"] = {
        "enabled": prompt_context_enabled,
        "recent_events": BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS,
        "max_events": BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS,
    }
    payload["telemetry"] = {
        "enabled": True,
        "rate_cards": {
            key: {
                field_name: value
                for field_name, value in card.items()
                if value is not None
            }
            for key, card in settings.rate_cards.items()
        },
    }
    payload["actions"] = {}
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    comparable_payload = json.loads(json.dumps(payload))
    comparable_payload["prompt_context"].pop("enabled", None)
    return canonical_hash(payload), canonical_hash(comparable_payload)


def _resolve_git_executable(root: Path) -> Path:
    candidate = shutil.which("git", path=os.defpath)
    if not candidate:
        raise ScenarioError("Git executable is unavailable")
    try:
        resolved = Path(candidate).expanduser().resolve(strict=True)
        metadata = os.stat(resolved, follow_symlinks=False)
    except OSError as exc:
        raise ScenarioError("Git executable cannot be resolved safely") from exc
    if (
        not resolved.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or not os.access(resolved, os.X_OK)
        or resolved.is_relative_to(root)
    ):
        raise ScenarioError("Git executable is not a safe external executable")
    return resolved


def _run_git(
    root: Path,
    *args: str,
    git_executable: Path,
    attributes_source: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        metadata = os.stat(git_executable, follow_symlinks=False)
    except OSError as exc:
        raise ScenarioError("Pinned Git executable is unavailable") from exc
    if (
        not git_executable.is_absolute()
        or not stat.S_ISREG(metadata.st_mode)
        or not os.access(git_executable, os.X_OK)
        or git_executable.is_relative_to(root)
    ):
        raise ScenarioError("Pinned Git executable is unsafe")
    environment = {
        "HOME": os.devnull,
        "PATH": os.defpath,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_PAGER": "",
        "PAGER": "",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if attributes_source is not None:
        environment["GIT_ATTR_SOURCE"] = attributes_source
    try:
        completed = subprocess.run(
            (str(git_executable), "--no-pager", *_GIT_CONFIG_OVERRIDES, *args),
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            timeout=_GIT_COMMAND_TIMEOUT_SECONDS,
            env=environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScenarioError("Pinned Git command timed out") from exc
    if check and completed.returncode:
        raise ScenarioError(f"Git command failed: git {' '.join(args)}")
    return completed


def _initialize_git(root: Path, *, git_executable: Path) -> tuple[str, int]:
    _run_git(root, "init", "-b", "benchmark", git_executable=git_executable)
    (root / ".git" / "info" / "attributes").write_bytes(b"")
    _run_git(
        root,
        "config",
        "--local",
        "user.name",
        "teams-runtime-benchmark",
        git_executable=git_executable,
    )
    _run_git(
        root,
        "config",
        "--local",
        "user.email",
        "benchmark@invalid.local",
        git_executable=git_executable,
    )
    _run_git(
        root,
        "config",
        "--local",
        "commit.gpgsign",
        "false",
        git_executable=git_executable,
    )
    _run_git(
        root,
        "config",
        "--local",
        "tag.gpgsign",
        "false",
        git_executable=git_executable,
    )
    _run_git(
        root,
        "config",
        "--local",
        "core.hooksPath",
        ".git/benchmark-disabled-hooks",
        git_executable=git_executable,
    )
    (root / ".git" / "benchmark-disabled-hooks").mkdir(mode=0o700, exist_ok=True)
    _run_git(root, "add", "--all", git_executable=git_executable)
    _run_git(
        root,
        "commit",
        "-m",
        "[benchmark] seed defective sum_positive scenario",
        git_executable=git_executable,
    )
    head = _run_git(
        root,
        "rev-parse",
        "HEAD",
        git_executable=git_executable,
    ).stdout.strip()
    count = int(
        _run_git(
            root,
            "rev-list",
            "--count",
            "HEAD",
            git_executable=git_executable,
        ).stdout.strip()
    )
    if _run_git(root, "remote", git_executable=git_executable).stdout.strip():
        raise ScenarioError("Benchmark repository unexpectedly has a Git remote")
    return head, count


def _assert_defect_reproduces(root: Path) -> None:
    result = subprocess.run(
        (sys.executable, "-m", "unittest", "discover", "-s", "tests"),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root), "LC_ALL": "C"},
    )
    if result.returncode == 0:
        raise ScenarioError("Benchmark fixture must fail its baseline behavior oracle")


def create_scenario_workspace(
    workspace_root: Path,
    *,
    benchmark_id: str,
    run_id: str,
    prompt_context_enabled: bool,
    settings: RuntimeSettings,
) -> ScenarioWorkspace:
    root = workspace_root.expanduser().resolve()
    root.mkdir(parents=True, mode=0o700, exist_ok=False)
    root.chmod(0o700)
    git_executable = _resolve_git_executable(root)
    scaffold_workspace(root)
    history_seed = build_history_seed()
    scenario_payload = {
        "schema_version": 1,
        "scenario_id": SCENARIO_ID,
        "milestone": SCENARIO_MILESTONE,
        "protected_paths": list(PROTECTED_PATHS),
        "quality_command": ["python", "-m", "unittest", "discover", "-s", "tests"],
        "history_event_count": len(history_seed),
        "history_hash": canonical_hash(history_seed),
    }
    files = {
        ".benchmark/scenario.json": json.dumps(scenario_payload, indent=2, sort_keys=True) + "\n",
        ".benchmark/history_seed.json": json.dumps(history_seed, indent=2, sort_keys=True) + "\n",
        "benchmark_app.py": (
            '"""Small benchmark target with an intentional defect."""\n\n'
            "\n"
            "def sum_positive(values):\n"
            '    """Return the sum of positive numeric values."""\n'
            "    return sum(values)\n"
        ),
        "tests/__init__.py": "",
        "tests/test_benchmark_app.py": (
            "import unittest\n\n"
            "from benchmark_app import sum_positive\n\n\n"
            "class SumPositiveTests(unittest.TestCase):\n"
            "    def test_mixed_values(self):\n"
            "        self.assertEqual(sum_positive([5, -8, 2]), 7)\n\n"
            "    def test_non_positive_values(self):\n"
            "        self.assertEqual(sum_positive([-5, 0, -3]), 0)\n\n"
            "    def test_empty_values(self):\n"
            "        self.assertEqual(sum_positive([]), 0)\n\n\n"
            'if __name__ == "__main__":\n'
            "    unittest.main()\n"
        ),
        "BENCHMARK_TASK.md": (
            "# Benchmark Task\n\n"
            f"{SCENARIO_MILESTONE}\n\n"
            "Accepted implementation shape (comments, whitespace, and the existing "
            "module/function docstrings are optional):\n\n"
            "```python\n"
            "def sum_positive(values):\n"
            "    return sum(value for value in values if value > 0)\n"
            "```\n\n"
            "Acceptance command: `python -m unittest discover -s tests`\n"
        ),
        ".gitignore": (
            ".teams_runtime/\n"
            "logs/\n"
            "__pycache__/\n"
            "*.py[cod]\n"
            ".teams_runtime_codex_output.txt\n"
        ),
    }
    for relative_path, content in files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    config_hash, comparable_hash = _benchmark_config(
        root,
        benchmark_id=benchmark_id,
        run_id=run_id,
        prompt_context_enabled=prompt_context_enabled,
        settings=settings,
    )
    protected_hashes = {
        relative_path: _protected_file_hash(root, relative_path)
        for relative_path in PROTECTED_PATHS
    }
    _assert_defect_reproduces(root)
    initial_commit, commit_count = _initialize_git(
        root,
        git_executable=git_executable,
    )
    return ScenarioWorkspace(
        root=root,
        initial_commit=initial_commit,
        initial_commit_count=commit_count,
        protected_hashes=protected_hashes,
        config_hash=config_hash,
        comparable_config_hash=comparable_hash,
        history_hash=canonical_hash(history_seed),
        history_seed=history_seed,
        git_executable=git_executable,
    )


def inspect_scenario_workspace(
    scenario: ScenarioWorkspace,
    *,
    timeout_seconds: float = 30.0,
) -> WorkspaceInspection:
    # Retained for API compatibility; final verification is deliberately non-executing.
    del timeout_seconds
    root = scenario.root
    notes: list[str] = []
    oracle_passed = _sum_positive_ast_oracle(root)
    if not oracle_passed:
        notes.append("behavior_oracle_failed")

    protected_unchanged = True
    for relative_path, expected_hash in scenario.protected_hashes.items():
        try:
            observed_hash = _protected_file_hash(root, relative_path)
        except (OSError, ValueError):
            observed_hash = ""
        if observed_hash != expected_hash:
            protected_unchanged = False
            notes.append(f"protected_file_changed:{relative_path}")

    git_executable = scenario.git_executable
    if git_executable is None:
        git_clean = False
        head_sha = ""
        commit_created = False
        no_git_remotes = False
        notes.append("git_inspection_unavailable")
    else:
        try:
            try:
                attributes_unchanged = (
                    _protected_file_hash(root, ".git/info/attributes")
                    == _EMPTY_FILE_SHA256
                )
            except (OSError, ValueError):
                attributes_unchanged = False
            if attributes_unchanged:
                status = _run_git(
                    root,
                    "status",
                    "--porcelain",
                    "--untracked-files=all",
                    "--ignore-submodules=all",
                    "--no-ahead-behind",
                    git_executable=git_executable,
                    attributes_source=scenario.initial_commit,
                    check=False,
                )
                git_clean = status.returncode == 0 and not status.stdout.strip()
            else:
                git_clean = False
                notes.append("git_attributes_changed")
            head_result = _run_git(
                root,
                "rev-parse",
                "HEAD",
                git_executable=git_executable,
                check=False,
            )
            head_sha = (
                head_result.stdout.strip() if head_result.returncode == 0 else ""
            )
            commit_created = bool(head_sha and head_sha != scenario.initial_commit)
            remotes = _run_git(
                root,
                "remote",
                git_executable=git_executable,
                check=False,
            )
            no_git_remotes = remotes.returncode == 0 and not remotes.stdout.strip()
        except ScenarioError:
            git_clean = False
            head_sha = ""
            commit_created = False
            no_git_remotes = False
            notes.append("git_inspection_failed")
    if not git_clean:
        notes.append("git_worktree_not_clean")
    if not commit_created:
        notes.append("task_commit_missing")
    if not no_git_remotes:
        notes.append("git_remote_detected")
    return WorkspaceInspection(
        behavior_oracle_passed=oracle_passed,
        protected_files_unchanged=protected_unchanged,
        git_clean=git_clean,
        commit_created=commit_created,
        no_git_remotes=no_git_remotes,
        head_sha=head_sha,
        notes=tuple(notes),
    )


__all__ = [
    "BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS",
    "BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS",
    "BENCHMARK_TARGET_INCLUDED_EVENTS",
    "BENCHMARK_TARGET_OMITTED_EVENTS",
    "BENCHMARK_TARGET_PURPOSE",
    "BENCHMARK_TARGET_ROLE",
    "BENCHMARK_TARGET_TOTAL_EVENTS",
    "BENCHMARK_TARGET_WORKFLOW_STEP",
    "DEFAULT_HISTORY_SEED_COUNT",
    "PROTECTED_PATHS",
    "RuntimeSettings",
    "SCENARIO_ID",
    "SCENARIO_MILESTONE",
    "ScenarioError",
    "ScenarioWorkspace",
    "WorkspaceInspection",
    "build_history_seed",
    "canonical_hash",
    "create_scenario_workspace",
    "inspect_scenario_workspace",
    "load_runtime_settings",
]
