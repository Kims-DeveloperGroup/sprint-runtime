from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from teams_runtime.core.template import scaffold_workspace
from teams_runtime.shared.models import TEAM_ROLES


SCENARIO_ID = "sum-positive-full-sprint-v1"
DEFAULT_HISTORY_SEED_COUNT = 48
BENCHMARK_PROMPT_CONTEXT_RECENT_EVENTS = 8
BENCHMARK_PROMPT_CONTEXT_MAX_EVENTS = 16
SCENARIO_MILESTONE = (
    "Fix sum_positive(values) so it returns the sum of positive values only. "
    "Preserve the public function, do not alter benchmark tests or scenario metadata, "
    "run the unittest suite, and commit the completed change."
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


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ("git", *args),
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        env={
            "HOME": os.environ.get("HOME", ""),
            "PATH": os.environ.get("PATH", ""),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        },
    )
    if check and completed.returncode:
        raise ScenarioError(f"Git command failed: git {' '.join(args)}")
    return completed


def _initialize_git(root: Path) -> tuple[str, int]:
    _run_git(root, "init", "-b", "benchmark")
    _run_git(root, "config", "--local", "user.name", "teams-runtime-benchmark")
    _run_git(root, "config", "--local", "user.email", "benchmark@invalid.local")
    _run_git(root, "config", "--local", "commit.gpgsign", "false")
    _run_git(root, "config", "--local", "tag.gpgsign", "false")
    _run_git(root, "config", "--local", "core.hooksPath", ".git/benchmark-disabled-hooks")
    (root / ".git" / "benchmark-disabled-hooks").mkdir(mode=0o700, exist_ok=True)
    _run_git(root, "add", "--all")
    _run_git(root, "commit", "-m", "[benchmark] seed defective sum_positive scenario")
    head = _run_git(root, "rev-parse", "HEAD").stdout.strip()
    count = int(_run_git(root, "rev-list", "--count", "HEAD").stdout.strip())
    if _run_git(root, "remote").stdout.strip():
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
        relative_path: file_hash(root / relative_path)
        for relative_path in PROTECTED_PATHS
    }
    _assert_defect_reproduces(root)
    initial_commit, commit_count = _initialize_git(root)
    return ScenarioWorkspace(
        root=root,
        initial_commit=initial_commit,
        initial_commit_count=commit_count,
        protected_hashes=protected_hashes,
        config_hash=config_hash,
        comparable_config_hash=comparable_hash,
        history_hash=canonical_hash(history_seed),
        history_seed=history_seed,
    )


def inspect_scenario_workspace(
    scenario: ScenarioWorkspace,
    *,
    timeout_seconds: float = 30.0,
) -> WorkspaceInspection:
    root = scenario.root
    notes: list[str] = []
    try:
        oracle = subprocess.run(
            (sys.executable, "-m", "unittest", "discover", "-s", "tests"),
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
            env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root), "LC_ALL": "C"},
        )
        oracle_passed = oracle.returncode == 0
        if not oracle_passed:
            notes.append("behavior_oracle_failed")
    except subprocess.TimeoutExpired:
        oracle_passed = False
        notes.append("behavior_oracle_timeout")

    protected_unchanged = True
    for relative_path, expected_hash in scenario.protected_hashes.items():
        path = root / relative_path
        if not path.is_file() or file_hash(path) != expected_hash:
            protected_unchanged = False
            notes.append(f"protected_file_changed:{relative_path}")

    status = _run_git(root, "status", "--porcelain", "--untracked-files=all", check=False)
    git_clean = status.returncode == 0 and not status.stdout.strip()
    if not git_clean:
        notes.append("git_worktree_not_clean")
    head_result = _run_git(root, "rev-parse", "HEAD", check=False)
    head_sha = head_result.stdout.strip() if head_result.returncode == 0 else ""
    commit_created = bool(head_sha and head_sha != scenario.initial_commit)
    if not commit_created:
        notes.append("task_commit_missing")
    remotes = _run_git(root, "remote", check=False)
    no_git_remotes = remotes.returncode == 0 and not remotes.stdout.strip()
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
