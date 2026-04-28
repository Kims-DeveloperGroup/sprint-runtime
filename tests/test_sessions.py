from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from teams_runtime.core.config import load_team_runtime_config
from teams_runtime.core.reports import read_process_summary
from teams_runtime.core.paths import RuntimePaths
from teams_runtime.core.template import scaffold_workspace
from teams_runtime.models import MessageEnvelope, RoleRuntimeConfig
from teams_runtime.runtime.base_runtime import RoleAgentRuntime, normalize_role_payload
from teams_runtime.runtime.session_manager import RoleSessionManager
from teams_runtime.runtime.codex_runner import CodexRunner, extract_json_object
from teams_runtime.runtime.internal.backlog_sourcing import BacklogSourcingRuntime
from teams_runtime.runtime.identities import local_identity
from teams_runtime.runtime.identities import local_runtime_identity
from teams_runtime.runtime.identities import sanitize_identity
from teams_runtime.runtime.identities import sanitize_runtime_identity
from teams_runtime.runtime.identities import service_identity
from teams_runtime.runtime.identities import service_runtime_identity
from teams_runtime.runtime.research_runtime import ResearchAgentRuntime


def _external_subject_definition(
    *,
    subject: str = "Current provider pricing comparison",
    query: str = "Compare current provider pricing with official sources.",
) -> dict[str, object]:
    return {
        "planning_decision": "provider cost assumption",
        "knowledge_gap": "current external provider pricing",
        "external_boundary": "official pricing changes outside the repository",
        "planner_impact": "planner should keep provider assumptions configurable until pricing is sourced",
        "candidate_subject": subject,
        "research_query": query,
        "source_requirements": ["official pricing pages", "current source-backed comparison"],
        "rejected_subjects": ["repo implementation details"],
        "no_subject_rationale": "",
    }


def _no_subject_definition(rationale: str = "The task is repo-local and local artifacts are enough for planner.") -> dict[str, object]:
    return {
        "planning_decision": "",
        "knowledge_gap": "",
        "external_boundary": "",
        "planner_impact": "",
        "candidate_subject": "",
        "research_query": "",
        "source_requirements": [],
        "rejected_subjects": ["repo-local implementation details"],
        "no_subject_rationale": rationale,
    }


class TeamsRuntimeSessionTests(unittest.TestCase):
    def test_runtime_identity_plan_helpers_match_compatibility_names(self):
        self.assertEqual(service_identity(" planner "), "planner")
        self.assertEqual(service_identity(""), "unknown")
        self.assertEqual(service_identity("planner"), service_runtime_identity("planner"))
        self.assertEqual(local_identity("orchestrator", "planner"), "orchestrator.local.planner")
        self.assertEqual(
            local_identity("orchestrator", "planner"),
            local_runtime_identity("orchestrator", "planner"),
        )
        self.assertEqual(sanitize_identity("orchestrator local/planner"), "orchestrator_local_planner")
        self.assertEqual(
            sanitize_identity("orchestrator local/planner"),
            sanitize_runtime_identity("orchestrator local/planner"),
        )

    def test_session_state_file_uses_sanitized_runtime_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)

            runtime_identity = "orchestrator local/planner"
            state_file = paths.session_state_file(
                "planner",
                runtime_identity=runtime_identity,
            )

            self.assertEqual(
                state_file.name,
                f"{sanitize_runtime_identity(runtime_identity)}.json",
            )

    def test_role_session_reuses_within_sprint_and_refreshes_across_sprints(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            paths.ensure_runtime_dirs()

            first_manager = RoleSessionManager(paths, "developer", "sprint-a")
            first_state = first_manager.ensure_session()
            second_state = first_manager.ensure_session()

            self.assertEqual(first_state.workspace_path, second_state.workspace_path)
            self.assertTrue(Path(first_state.workspace_path).is_dir())

            next_manager = RoleSessionManager(paths, "developer", "sprint-b")
            refreshed_state = next_manager.ensure_session()

            self.assertNotEqual(first_state.workspace_path, refreshed_state.workspace_path)
            archived_dir = paths.archived_session_dir("sprint-a", "developer")
            self.assertTrue(archived_dir.exists())
            self.assertTrue(any(archived_dir.glob("*.json")))

    def test_role_session_manager_isolates_local_runtime_identity_from_service_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            service_identity = service_runtime_identity("planner")
            local_identity = local_runtime_identity("orchestrator", "planner")

            service_manager = RoleSessionManager(
                paths,
                "planner",
                "sprint-a",
                runtime_identity=service_identity,
            )
            local_manager = RoleSessionManager(
                paths,
                "planner",
                "sprint-a",
                runtime_identity=local_identity,
            )

            service_state = service_manager.ensure_session()
            local_state = local_manager.ensure_session()

            self.assertNotEqual(service_state.workspace_path, local_state.workspace_path)
            self.assertEqual(service_state.runtime_identity, service_identity)
            self.assertEqual(local_state.runtime_identity, local_identity)
            self.assertTrue(paths.session_state_file("planner", runtime_identity=service_identity).exists())
            self.assertTrue(paths.session_state_file("planner", runtime_identity=local_identity).exists())

    def test_session_workspace_contains_role_and_shared_links(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            manager = RoleSessionManager(paths, "planner", "sprint-a")

            state = manager.ensure_session()
            workspace = Path(state.workspace_path)

            self.assertTrue((workspace / "AGENTS.md").exists())
            self.assertTrue((workspace / "todo.md").exists())
            self.assertTrue((workspace / "sources").exists())
            self.assertTrue((workspace / "workspace").exists())
            self.assertTrue((workspace / "workspace_context.md").exists())
            self.assertTrue((workspace / "shared_workspace").exists())
            self.assertTrue((workspace / ".teams_runtime").exists())
            self.assertFalse((workspace / "team_runtime.yaml").exists())
            self.assertFalse((workspace / "discord_agents_config.yaml").exists())
            context_text = (workspace / "workspace_context.md").read_text(encoding="utf-8")
            self.assertIn("./shared_workspace", context_text)
            self.assertIn("./.teams_runtime", context_text)

    def test_internal_parser_session_uses_internal_workspace_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            manager = RoleSessionManager(
                paths,
                "parser",
                "sprint-a",
                agent_root=paths.internal_agent_root("parser"),
            )

            state = manager.ensure_session()
            workspace = Path(state.workspace_path)

            self.assertTrue((workspace / "AGENTS.md").exists())
            self.assertTrue((workspace / "GEMINI.md").exists())
            self.assertTrue((workspace / "workspace").exists())
            self.assertTrue((workspace / "shared_workspace").exists())

    def test_internal_parser_local_runtime_identity_isolated_from_service_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            service_manager = RoleSessionManager(
                paths,
                "parser",
                "sprint-a",
                agent_root=paths.internal_agent_root("parser"),
                runtime_identity=service_runtime_identity("parser"),
            )
            local_manager = RoleSessionManager(
                paths,
                "parser",
                "sprint-a",
                agent_root=paths.internal_agent_root("parser"),
                runtime_identity=local_runtime_identity("orchestrator", "parser"),
            )

            service_state = service_manager.ensure_session()
            local_state = local_manager.ensure_session()

            self.assertNotEqual(service_state.workspace_path, local_state.workspace_path)
            self.assertEqual(service_state.runtime_identity, "parser")
            self.assertEqual(local_state.runtime_identity, "orchestrator.local.parser")

    def test_internal_sourcer_session_uses_internal_workspace_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            manager = RoleSessionManager(
                paths,
                "sourcer",
                "sprint-a",
                agent_root=paths.internal_agent_root("sourcer"),
            )

            state = manager.ensure_session()
            workspace = Path(state.workspace_path)

            self.assertTrue((workspace / "AGENTS.md").exists())
            self.assertTrue((workspace / "GEMINI.md").exists())
            self.assertTrue((workspace / "workspace").exists())
            self.assertTrue((workspace / "shared_workspace").exists())

    def test_internal_sourcer_local_runtime_identity_isolated_from_service_identity(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            service_manager = RoleSessionManager(
                paths,
                "sourcer",
                "sprint-a",
                agent_root=paths.internal_agent_root("sourcer"),
                runtime_identity=service_runtime_identity("sourcer"),
            )
            local_manager = RoleSessionManager(
                paths,
                "sourcer",
                "sprint-a",
                agent_root=paths.internal_agent_root("sourcer"),
                runtime_identity=local_runtime_identity("orchestrator", "sourcer"),
            )

            service_state = service_manager.ensure_session()
            local_state = local_manager.ensure_session()

            self.assertNotEqual(service_state.workspace_path, local_state.workspace_path)
            self.assertEqual(service_state.runtime_identity, "sourcer")
            self.assertEqual(local_state.runtime_identity, "orchestrator.local.sourcer")

    def test_internal_version_controller_session_exposes_local_skill(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            manager = RoleSessionManager(
                paths,
                "version_controller",
                "sprint-a",
                agent_root=paths.internal_agent_root("version_controller"),
            )

            state = manager.ensure_session()
            workspace = Path(state.workspace_path)
            skill_file = workspace / ".agents" / "skills" / "version_controller" / "SKILL.md"

            self.assertTrue((workspace / ".agents").exists())
            self.assertTrue(skill_file.exists())
            self.assertIn("name: version_controller", skill_file.read_text(encoding="utf-8"))

    def test_orchestrator_session_exposes_local_skills(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            manager = RoleSessionManager(paths, "orchestrator", "sprint-a")

            state = manager.ensure_session()
            workspace = Path(state.workspace_path)
            sprint_skill = workspace / ".agents" / "skills" / "sprint_orchestration" / "SKILL.md"
            agent_utilization_skill = workspace / ".agents" / "skills" / "agent_utilization" / "SKILL.md"
            agent_utilization_policy = workspace / ".agents" / "skills" / "agent_utilization" / "policy.yaml"
            handoff_skill = workspace / ".agents" / "skills" / "handoff_merging" / "SKILL.md"
            status_skill = workspace / ".agents" / "skills" / "status_reporting" / "SKILL.md"
            closeout_skill = workspace / ".agents" / "skills" / "sprint_closeout" / "SKILL.md"

            self.assertTrue((workspace / ".agents").exists())
            self.assertTrue(sprint_skill.exists())
            self.assertTrue(agent_utilization_skill.exists())
            self.assertTrue(agent_utilization_policy.exists())
            self.assertTrue(handoff_skill.exists())
            self.assertTrue(status_skill.exists())
            self.assertTrue(closeout_skill.exists())
            self.assertIn("name: sprint_orchestration", sprint_skill.read_text(encoding="utf-8"))
            self.assertIn("name: agent_utilization", agent_utilization_skill.read_text(encoding="utf-8"))
            self.assertIn("public_roles:", agent_utilization_policy.read_text(encoding="utf-8"))
            self.assertIn("name: handoff_merging", handoff_skill.read_text(encoding="utf-8"))
            self.assertIn("name: status_reporting", status_skill.read_text(encoding="utf-8"))
            self.assertIn("name: sprint_closeout", closeout_skill.read_text(encoding="utf-8"))

    def test_planner_session_exposes_local_skills(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            manager = RoleSessionManager(paths, "planner", "sprint-a")

            state = manager.ensure_session()
            workspace = Path(state.workspace_path)
            documentation_skill = workspace / ".agents" / "skills" / "documentation" / "SKILL.md"
            management_skill = workspace / ".agents" / "skills" / "backlog_management" / "SKILL.md"
            backlog_skill = workspace / ".agents" / "skills" / "backlog_decomposition" / "SKILL.md"
            sprint_skill = workspace / ".agents" / "skills" / "sprint_planning" / "SKILL.md"

            self.assertTrue((workspace / ".agents").exists())
            self.assertTrue(documentation_skill.exists())
            self.assertTrue(management_skill.exists())
            self.assertTrue(backlog_skill.exists())
            self.assertTrue(sprint_skill.exists())
            self.assertIn("name: documentation", documentation_skill.read_text(encoding="utf-8"))
            self.assertIn("name: backlog_management", management_skill.read_text(encoding="utf-8"))
            self.assertIn("name: backlog_decomposition", backlog_skill.read_text(encoding="utf-8"))
            self.assertIn("name: sprint_planning", sprint_skill.read_text(encoding="utf-8"))

    def test_session_workspace_links_parent_workspace_when_root_is_teams_generated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            manager = RoleSessionManager(paths, "planner", "sprint-a")

            state = manager.ensure_session()
            workspace_link = Path(state.workspace_path) / "workspace"

            self.assertTrue(workspace_link.exists())
            self.assertEqual(workspace_link.resolve(), Path(tmpdir).resolve())

    def test_existing_session_backfills_workspace_link_on_reuse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            manager = RoleSessionManager(paths, "planner", "sprint-a")

            state = manager.ensure_session()
            workspace_link = Path(state.workspace_path) / "workspace"
            workspace_link.unlink()

            reused = manager.ensure_session()

            self.assertEqual(reused.workspace_path, state.workspace_path)
            self.assertTrue(workspace_link.exists())
            self.assertEqual(workspace_link.resolve(), Path(tmpdir).resolve())

    def test_existing_session_removes_legacy_root_config_links_on_reuse(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            manager = RoleSessionManager(paths, "planner", "sprint-a")

            state = manager.ensure_session()
            session_workspace = Path(state.workspace_path)
            (session_workspace / "team_runtime.yaml").symlink_to(paths.workspace_root / "team_runtime.yaml")
            (session_workspace / "discord_agents_config.yaml").symlink_to(paths.workspace_root / "discord_agents_config.yaml")

            reused = manager.ensure_session()

            self.assertEqual(reused.workspace_path, state.workspace_path)
            self.assertFalse((session_workspace / "team_runtime.yaml").exists())
            self.assertFalse((session_workspace / "discord_agents_config.yaml").exists())

    def test_finalize_session_id_keeps_workspace_path_stable(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            manager = RoleSessionManager(paths, "planner", "sprint-a")

            state = manager.ensure_session()
            original_workspace = state.workspace_path
            original_dir = Path(original_workspace)

            finalized = manager.finalize_session_id(state, "019d192e-e0ec-7a22-a4c7-41192f58da8f")

            self.assertEqual(finalized.workspace_path, original_workspace)
            self.assertEqual(finalized.session_id, "019d192e-e0ec-7a22-a4c7-41192f58da8f")
            self.assertTrue(original_dir.is_dir())

    def test_role_runtime_overrides_mismatched_request_id_and_role_from_model_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )

            class _FakeRunner:
                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    return (
                        '{"request_id":"old-request","role":"developer","status":"completed","summary":"ok"}',
                        "session-1",
                    )

            runtime.codex_runner = _FakeRunner()
            envelope = MessageEnvelope(
                request_id="new-request",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="scope",
            )
            request_record = {
                "request_id": "new-request",
                "scope": "scope",
                "body": "",
                "artifacts": [],
            }

            payload = runtime.run_task(envelope, request_record)

            self.assertEqual(payload["request_id"], "new-request")
            self.assertEqual(payload["role"], "planner")

    def test_role_runtime_logs_task_start_and_result(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )

            class _FakeRunner:
                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    return (
                        '{"request_id":"request-1","role":"planner","status":"completed","summary":"ok","artifacts":["a.md"]}',
                        "session-1",
                    )

            runtime.codex_runner = _FakeRunner()
            envelope = MessageEnvelope(
                request_id="request-1",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="scope",
            )
            request_record = {
                "request_id": "request-1",
                "scope": "scope",
                "body": "",
                "artifacts": [],
                "sprint_id": "260331-Sprint-08:47",
                "todo_id": "todo-1",
                "backlog_id": "backlog-1",
            }

            with self.assertLogs("teams_runtime.runtime.codex", level="INFO") as captured:
                payload = runtime.run_task(envelope, request_record)

            joined = "\n".join(captured.output)
            self.assertIn("task_start request_id=request-1", joined)
            self.assertIn("task_result request_id=request-1", joined)
            self.assertIn("todo_id=todo-1", joined)
            self.assertEqual(payload["status"], "completed")

    def test_research_runtime_skips_external_research_for_repo_local_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = ResearchAgentRuntime(
                paths=paths,
                role="research",
                sprint_id="260418-Sprint-10:00",
                runtime_config=RoleRuntimeConfig(),
                research_defaults=load_team_runtime_config(tmpdir).research_defaults,
            )
            envelope = MessageEnvelope(
                request_id="request-local",
                sender="orchestrator",
                target="research",
                intent="route",
                urgency="normal",
                scope="teams_runtime research role을 추가합니다",
                body="teams_runtime workflow와 planner handoff를 수정합니다",
            )
            request_record = {
                "request_id": "request-local",
                "scope": envelope.scope,
                "body": envelope.body,
                "artifacts": [],
                "params": {"workflow": {"step": "research_initial", "phase_owner": "research"}},
                "sprint_id": "260418-Sprint-10:00",
            }

            decision_output = json.dumps(
                {
                    "needed": False,
                    "subject": "",
                    "research_query": "",
                    "reason_code": "not_needed_no_subject",
                    "research_subject_definition": _no_subject_definition(),
                    "planner_guidance": "planner는 repo/local sprint context만으로 planning을 이어가면 됩니다.",
                },
                ensure_ascii=False,
            )
            with patch.object(runtime.codex_runner, "run", return_value=(decision_output, "research-session-1")) as decision_mock, patch(
                "teams_runtime.runtime.research_runtime.run_deep_research_sync"
            ) as deep_research_mock:
                payload = runtime.run_task(envelope, request_record)

            decision_mock.assert_called_once()
            deep_research_mock.assert_not_called()
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(
                payload["proposals"]["research_signal"],
                {
                    "needed": False,
                    "subject": "",
                    "research_query": "",
                    "reason_code": "not_needed_no_subject",
                },
            )
            self.assertEqual(payload["proposals"]["research_report"]["headline"], "외부 research 불필요")
            self.assertEqual(payload["artifacts"], [])

    def test_research_runtime_runs_deep_research_and_writes_request_scoped_artifact(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = ResearchAgentRuntime(
                paths=paths,
                role="research",
                sprint_id="260418-Sprint-10:30",
                runtime_config=RoleRuntimeConfig(),
                research_defaults=load_team_runtime_config(tmpdir).research_defaults,
            )
            envelope = MessageEnvelope(
                request_id="request-external",
                sender="orchestrator",
                target="research",
                intent="route",
                urgency="normal",
                scope="latest OpenAI versus Gemini API pricing comparison with sources",
                body="Need current source-backed comparison before planner decides provider direction.",
            )
            request_record = {
                "request_id": "request-external",
                "scope": envelope.scope,
                "body": envelope.body,
                "artifacts": [],
                "params": {"workflow": {"step": "research_initial", "phase_owner": "research"}},
                "sprint_id": "260418-Sprint-10:30",
            }
            report_text = """# Executive Summary
Current API pricing differs enough that planner should preserve provider-specific cost assumptions.

# Planner Guidance
Prefer a provider-neutral abstraction until pricing volatility stabilizes.

# Milestone Refinement Hints
- Refine provider comparison into provider-neutral cost boundary planning.

# Problem Framing Hints
- Pricing volatility can change provider selection and abstraction decisions.

# Spec Implications
- Keep provider-specific pricing assumptions outside hard-coded spec commitments.

# Todo Definition Hints
- Split provider abstraction from cost verification acceptance criteria.

# Backing Reasoning
- Official pricing sources determine whether planner can lock provider assumptions.

# Backing Sources
- title: OpenAI API Pricing
  url: https://openai.com/api/pricing
  published_at: 2026-04-01
  relevance: Confirms current OpenAI list pricing.
  summary: Lists current flagship API pricing tiers.
- title: Gemini API Pricing
  url: https://ai.google.dev/gemini-api/docs/pricing
  published_at: 2026-04-02
  relevance: Confirms current Gemini list pricing.
  summary: Lists current Gemini model pricing tiers.

# Open Questions
- Should planner optimize for latency or pure token cost first?
"""

            decision_output = json.dumps(
                {
                    "needed": True,
                    "subject": "Current OpenAI versus Gemini API pricing comparison",
                    "research_query": "Compare current OpenAI and Gemini API pricing with official sources and note planner-impacting differences.",
                    "reason_code": "needed_external_grounding",
                    "research_subject_definition": _external_subject_definition(
                        subject="Current OpenAI versus Gemini API pricing comparison",
                        query="Compare current OpenAI and Gemini API pricing with official sources and note planner-impacting differences.",
                    ),
                    "planner_guidance": "planner는 외부 가격 근거가 정리되기 전까지 provider cost assumptions를 고정하지 마세요.",
                },
                ensure_ascii=False,
            )
            with patch.object(runtime.codex_runner, "run", return_value=(decision_output, "research-session-2")) as decision_mock, patch(
                "teams_runtime.runtime.research_runtime.run_deep_research_sync",
                return_value=SimpleNamespace(
                    completed=True,
                    response_text=report_text,
                    url="https://gemini.google.com/test-url",
                ),
            ) as deep_research_mock:
                payload = runtime.run_task(envelope, request_record)

            decision_mock.assert_called_once()
            deep_research_mock.assert_called_once()
            self.assertIn(
                "Compare current OpenAI and Gemini API pricing with official sources",
                deep_research_mock.call_args.args[0],
            )
            self.assertEqual(
                deep_research_mock.call_args.kwargs["profile_path"],
                str(Path(tmpdir).resolve() / "chrome_profile"),
            )
            self.assertEqual(
                deep_research_mock.call_args.kwargs["mode"],
                ["Pro", "프로", "최상위"],
            )
            artifact = paths.sprint_research_file("260418-Sprint-10:30", "request-external")
            self.assertTrue(artifact.exists())
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["artifacts"], ["shared_workspace/sprints/260418-Sprint-10:30/research/request-external.md"])
            self.assertEqual(
                payload["proposals"]["research_signal"],
                {
                    "needed": True,
                    "subject": "Current OpenAI versus Gemini API pricing comparison",
                    "research_query": "Compare current OpenAI and Gemini API pricing with official sources and note planner-impacting differences.",
                    "reason_code": "needed_external_grounding",
                },
            )
            self.assertEqual(len(payload["proposals"]["research_report"]["backing_sources"]), 2)

    def test_research_runtime_fails_external_research_without_valid_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = ResearchAgentRuntime(
                paths=paths,
                role="research",
                sprint_id="260418-Sprint-10:32",
                runtime_config=RoleRuntimeConfig(),
                research_defaults=load_team_runtime_config(tmpdir).research_defaults,
            )
            envelope = MessageEnvelope(
                request_id="request-external-no-sources",
                sender="orchestrator",
                target="research",
                intent="route",
                urgency="normal",
                scope="latest provider pricing comparison with sources",
                body="Need source-backed comparison before planner decides provider direction.",
            )
            request_record = {
                "request_id": "request-external-no-sources",
                "scope": envelope.scope,
                "body": envelope.body,
                "artifacts": [],
                "params": {"workflow": {"step": "research_initial", "phase_owner": "research"}},
                "sprint_id": "260418-Sprint-10:32",
            }
            report_text = """# Executive Summary
Pricing may differ enough to affect provider planning.

# Planner Guidance
Keep provider assumptions soft.

# Backing Sources
- title: Provider Pricing
  url: not-a-url
  relevance: Invalid source URL should not pass validation.
  summary: No usable source URL.

# Open Questions
- Which provider should be preferred?
"""
            decision_output = json.dumps(
                {
                    "needed": True,
                    "subject": "Current provider pricing comparison",
                    "research_query": "Compare current provider pricing with official sources.",
                    "reason_code": "needed_external_grounding",
                    "research_subject_definition": _external_subject_definition(),
                    "planner_guidance": "planner는 외부 가격 근거가 정리되기 전까지 provider assumptions를 고정하지 마세요.",
                },
                ensure_ascii=False,
            )
            with patch.object(runtime.codex_runner, "run", return_value=(decision_output, "research-session-no-sources")), patch(
                "teams_runtime.runtime.research_runtime.run_deep_research_sync",
                return_value=SimpleNamespace(
                    completed=True,
                    response_text=report_text,
                    url="https://gemini.google.com/test-url",
                ),
            ):
                payload = runtime.run_task(envelope, request_record)

            artifact_hint = "shared_workspace/sprints/260418-Sprint-10:32/research/request-external-no-sources.md"
            artifact = paths.sprint_research_file("260418-Sprint-10:32", "request-external-no-sources")
            self.assertEqual(payload["status"], "failed")
            self.assertIn("backing source", payload["error"])
            self.assertTrue(artifact.exists())
            self.assertEqual(payload["artifacts"], [artifact_hint])
            self.assertEqual(payload["proposals"]["research_report"]["report_artifact"], artifact_hint)
            self.assertEqual(payload["proposals"]["research_report"]["research_url"], "https://gemini.google.com/test-url")
            self.assertEqual(payload["proposals"]["research_report"]["backing_sources"], [])
            self.assertEqual(
                payload["proposals"]["research_report"]["failure_details"]["failure_stage"],
                "validate_report",
            )
            self.assertEqual(
                payload["proposals"]["research_report"]["failure_details"]["parsed_backing_source_count"],
                1,
            )

    def test_research_runtime_records_enable_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = ResearchAgentRuntime(
                paths=paths,
                role="research",
                sprint_id="260418-Sprint-10:33",
                runtime_config=RoleRuntimeConfig(),
                research_defaults=load_team_runtime_config(tmpdir).research_defaults,
            )
            envelope = MessageEnvelope(
                request_id="request-external-enable-fail",
                sender="orchestrator",
                target="research",
                intent="route",
                urgency="normal",
                scope="latest provider pricing comparison with sources",
                body="Need source-backed comparison before planner decides provider direction.",
            )
            request_record = {
                "request_id": "request-external-enable-fail",
                "scope": envelope.scope,
                "body": envelope.body,
                "artifacts": [],
                "params": {"workflow": {"step": "research_initial", "phase_owner": "research"}},
                "sprint_id": "260418-Sprint-10:33",
            }
            decision_output = json.dumps(
                {
                    "needed": True,
                    "subject": "Current provider pricing comparison",
                    "research_query": "Compare current provider pricing with official sources.",
                    "reason_code": "needed_external_grounding",
                    "research_subject_definition": _external_subject_definition(),
                    "planner_guidance": "planner는 외부 가격 근거가 정리되기 전까지 provider assumptions를 고정하지 마세요.",
                },
                ensure_ascii=False,
            )
            error_message = (
                "Deep Research could not be enabled for this session. "
                "Diagnostics: url=https://gemini.google.com/app; menu_items=['Deep Research']"
            )
            with patch.object(runtime.codex_runner, "run", return_value=(decision_output, "research-session-enable-fail")), patch(
                "teams_runtime.runtime.research_runtime.run_deep_research_sync",
                side_effect=RuntimeError(error_message),
            ):
                payload = runtime.run_task(envelope, request_record)

            self.assertEqual(payload["status"], "failed")
            self.assertIn("Diagnostics:", payload["error"])
            self.assertEqual(payload["artifacts"], [])
            self.assertEqual(
                payload["proposals"]["research_report"]["failure_details"]["failure_stage"],
                "run_deep_research",
            )
            self.assertEqual(
                payload["proposals"]["research_report"]["failure_details"]["exception_type"],
                "RuntimeError",
            )
            self.assertEqual(payload["proposals"]["research_report"]["report_artifact"], "")
            self.assertEqual(payload["proposals"]["research_report"]["research_url"], "")

    def test_research_runtime_fails_when_deep_research_never_reaches_final_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = ResearchAgentRuntime(
                paths=paths,
                role="research",
                sprint_id="260418-Sprint-10:34",
                runtime_config=RoleRuntimeConfig(),
                research_defaults=load_team_runtime_config(tmpdir).research_defaults,
            )
            envelope = MessageEnvelope(
                request_id="request-external-incomplete",
                sender="orchestrator",
                target="research",
                intent="route",
                urgency="normal",
                scope="latest provider pricing comparison with sources",
                body="Need source-backed comparison before planner decides provider direction.",
            )
            request_record = {
                "request_id": "request-external-incomplete",
                "scope": envelope.scope,
                "body": envelope.body,
                "artifacts": [],
                "params": {"workflow": {"step": "research_initial", "phase_owner": "research"}},
                "sprint_id": "260418-Sprint-10:34",
            }
            decision_output = json.dumps(
                {
                    "needed": True,
                    "subject": "Current provider pricing comparison",
                    "research_query": "Compare current provider pricing with official sources.",
                    "reason_code": "needed_external_grounding",
                    "research_subject_definition": _external_subject_definition(),
                    "planner_guidance": "planner는 외부 가격 근거가 정리되기 전까지 provider assumptions를 고정하지 마세요.",
                },
                ensure_ascii=False,
            )
            with patch.object(runtime.codex_runner, "run", return_value=(decision_output, "research-session-incomplete")), patch(
                "teams_runtime.runtime.research_runtime.run_deep_research_sync",
                return_value=SimpleNamespace(
                    completed=False,
                    response_text="중간 계획 텍스트",
                    url="https://gemini.google.com/app/report-123",
                ),
            ):
                payload = runtime.run_task(envelope, request_record)

            self.assertEqual(payload["status"], "failed")
            self.assertIn("did not reach final report completion", payload["error"])
            self.assertEqual(payload["artifacts"], [])
            self.assertEqual(
                payload["proposals"]["research_report"]["failure_details"]["failure_stage"],
                "await_final_report",
            )
            self.assertEqual(
                payload["proposals"]["research_report"]["failure_details"]["exception_type"],
                "RuntimeError",
            )
            self.assertEqual(
                payload["proposals"]["research_report"]["research_url"],
                "https://gemini.google.com/app/report-123",
            )

    def test_research_runtime_resolves_default_profile_from_project_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            runtime_workspace = project_root / "teams_generated"
            scaffold_workspace(runtime_workspace)
            config_path = runtime_workspace / "team_runtime.yaml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    '  profile_path: "./chrome_profile"\n',
                    '  profile_path: ""\n',
                    1,
                ),
                encoding="utf-8",
            )
            paths = RuntimePaths.from_root(runtime_workspace)
            runtime = ResearchAgentRuntime(
                paths=paths,
                role="research",
                sprint_id="260418-Sprint-10:35",
                runtime_config=RoleRuntimeConfig(),
                research_defaults=load_team_runtime_config(runtime_workspace).research_defaults,
            )
            envelope = MessageEnvelope(
                request_id="request-profile-default",
                sender="orchestrator",
                target="research",
                intent="route",
                urgency="normal",
                scope="latest provider pricing",
                body="Need external grounding.",
            )
            request_record = {
                "request_id": "request-profile-default",
                "scope": envelope.scope,
                "body": envelope.body,
                "artifacts": [],
                "params": {"workflow": {"step": "research_initial", "phase_owner": "research"}},
                "sprint_id": "260418-Sprint-10:35",
            }

            effective_config = runtime._merge_research_config(envelope, request_record)

            self.assertEqual(
                effective_config.profile_path,
                str(project_root.resolve() / "chrome_profile"),
            )

    def test_research_runtime_resolves_relative_profile_override_from_project_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            runtime_workspace = project_root / "teams_generated"
            scaffold_workspace(runtime_workspace)
            paths = RuntimePaths.from_root(runtime_workspace)
            runtime = ResearchAgentRuntime(
                paths=paths,
                role="research",
                sprint_id="260418-Sprint-10:40",
                runtime_config=RoleRuntimeConfig(),
                research_defaults=load_team_runtime_config(runtime_workspace).research_defaults,
            )
            envelope = MessageEnvelope(
                request_id="request-profile-override",
                sender="orchestrator",
                target="research",
                intent="route",
                urgency="normal",
                scope="latest provider pricing",
                body="Need external grounding.",
            )
            request_record = {
                "request_id": "request-profile-override",
                "scope": envelope.scope,
                "body": envelope.body,
                "artifacts": [],
                "params": {
                    "research": {"profile_path": "profiles/research"},
                    "workflow": {"step": "research_initial", "phase_owner": "research"},
                },
                "sprint_id": "260418-Sprint-10:40",
            }

            effective_config = runtime._merge_research_config(envelope, request_record)

            self.assertEqual(
                effective_config.profile_path,
                str(project_root.resolve() / "profiles" / "research"),
            )

    def test_research_runtime_blocks_when_decision_output_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = ResearchAgentRuntime(
                paths=paths,
                role="research",
                sprint_id="260418-Sprint-10:45",
                runtime_config=RoleRuntimeConfig(),
                research_defaults=load_team_runtime_config(tmpdir).research_defaults,
            )
            envelope = MessageEnvelope(
                request_id="request-invalid-decision",
                sender="orchestrator",
                target="research",
                intent="route",
                urgency="normal",
                scope="Need current API pricing validation before planner commits to provider assumptions",
                body="Researcher should decide if external grounding is needed.",
            )
            request_record = {
                "request_id": "request-invalid-decision",
                "scope": envelope.scope,
                "body": envelope.body,
                "artifacts": [],
                "params": {"workflow": {"step": "research_initial", "phase_owner": "research"}},
                "sprint_id": "260418-Sprint-10:45",
            }
            invalid_output = json.dumps(
                {
                    "needed": True,
                    "subject": "",
                    "research_query": "",
                    "reason_code": "needed_external_grounding",
                    "research_subject_definition": {
                        **_external_subject_definition(subject="", query=""),
                        "candidate_subject": "",
                        "research_query": "",
                    },
                    "planner_guidance": "외부 근거가 필요합니다.",
                },
                ensure_ascii=False,
            )

            with patch.object(runtime.codex_runner, "run", return_value=(invalid_output, "research-session-3")) as decision_mock, patch(
                "teams_runtime.runtime.research_runtime.run_deep_research_sync"
            ) as deep_research_mock:
                payload = runtime.run_task(envelope, request_record)

            decision_mock.assert_called_once()
            deep_research_mock.assert_not_called()
            self.assertEqual(payload["status"], "blocked")
            self.assertEqual(
                payload["proposals"]["research_signal"],
                {
                    "needed": False,
                    "subject": "",
                    "research_query": "",
                    "reason_code": "blocked_decision_failed",
                },
            )
            self.assertIn("candidate_subject", payload["error"])

    def test_role_runtime_uses_request_sprint_scope_for_workspace_reuse_and_refresh(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="configured-session-scope",
                runtime_config=RoleRuntimeConfig(),
            )

            class _FakeRunner:
                def __init__(self):
                    self.calls: list[dict[str, str | None]] = []

                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    self.calls.append(
                        {
                            "workspace": str(workspace),
                            "session_id": session_id,
                            "prompt": str(prompt),
                        }
                    )
                    if "260403-Sprint-09:00" in str(prompt):
                        resolved_session_id = "session-restartable"
                    else:
                        resolved_session_id = "session-fresh"
                    return (
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "ok",
                                "error": "",
                                "proposals": {},
                                "artifacts": [],
                            },
                            ensure_ascii=False,
                        ),
                        resolved_session_id,
                    )

            runtime.codex_runner = _FakeRunner()

            def _build_inputs(request_id: str, sprint_id: str) -> tuple[MessageEnvelope, dict[str, str | list[str]]]:
                return (
                    MessageEnvelope(
                        request_id=request_id,
                        sender="orchestrator",
                        target="planner",
                        intent="plan",
                        urgency="normal",
                        scope="scope",
                        params={"sprint_id": sprint_id},
                    ),
                    {
                        "request_id": request_id,
                        "scope": "scope",
                        "body": "",
                        "artifacts": [],
                        "sprint_id": sprint_id,
                    },
                )

            first_envelope, first_request = _build_inputs("request-1", "260403-Sprint-09:00")
            first_payload = runtime.run_task(first_envelope, first_request)

            state_after_first = runtime.session_manager.load()
            self.assertIsNotNone(state_after_first)
            self.assertEqual(state_after_first.sprint_id, "260403-Sprint-09:00")
            self.assertIn("260403-Sprint-09:00", runtime.codex_runner.calls[0]["prompt"] or "")
            self.assertEqual(runtime.codex_runner.calls[0]["session_id"], None)

            second_envelope, second_request = _build_inputs("request-2", "260403-Sprint-09:00")
            second_payload = runtime.run_task(second_envelope, second_request)

            self.assertEqual(runtime.codex_runner.calls[1]["session_id"], "session-restartable")
            self.assertEqual(first_payload["session_workspace"], second_payload["session_workspace"])

            third_envelope, third_request = _build_inputs("request-3", "260403-Sprint-10:30")
            third_payload = runtime.run_task(third_envelope, third_request)

            state_after_third = runtime.session_manager.load()
            self.assertIsNotNone(state_after_third)
            self.assertEqual(state_after_third.sprint_id, "260403-Sprint-10:30")
            self.assertEqual(runtime.codex_runner.calls[2]["session_id"], None)
            self.assertNotEqual(first_payload["session_workspace"], third_payload["session_workspace"])

    def test_role_runtime_local_identity_does_not_overwrite_service_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            service_runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
                session_identity=service_runtime_identity("planner"),
            )
            local_runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
                session_identity=local_runtime_identity("orchestrator", "planner"),
            )

            class _TaggedRunner:
                def __init__(self, session_id: str):
                    self.session_id = session_id

                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    return (
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "ok",
                                "error": "",
                                "proposals": {},
                                "artifacts": [],
                            },
                            ensure_ascii=False,
                        ),
                        self.session_id,
                    )

            service_runtime.codex_runner = _TaggedRunner("planner-service-session")
            local_runtime.codex_runner = _TaggedRunner("planner-local-session")
            envelope = MessageEnvelope(
                request_id="request-1",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="scope",
            )
            request_record = {
                "request_id": "request-1",
                "scope": "scope",
                "body": "",
                "artifacts": [],
                "sprint_id": "sprint-a",
            }

            service_payload = service_runtime.run_task(envelope, dict(request_record))
            local_payload = local_runtime.run_task(envelope, dict(request_record))

            service_state = service_runtime.session_manager.load()
            local_state = local_runtime.session_manager.load()

            self.assertIsNotNone(service_state)
            self.assertIsNotNone(local_state)
            self.assertEqual(service_state.session_id, "planner-service-session")
            self.assertEqual(local_state.session_id, "planner-local-session")
            self.assertNotEqual(service_payload["session_workspace"], local_payload["session_workspace"])
            self.assertTrue(
                paths.session_state_file(
                    "planner",
                    runtime_identity=local_runtime_identity("orchestrator", "planner"),
                ).exists()
            )

    def test_role_runtime_prompt_mentions_project_workspace_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            envelope = MessageEnvelope(
                request_id="new-request",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="scope",
            )
            request_record = {
                "request_id": "new-request",
                "scope": "scope",
                "body": "",
                "artifacts": [],
            }

            prompt = runtime._build_prompt(envelope, request_record)

            self.assertIn("./workspace", prompt)
            self.assertIn("contains teams_generated", prompt)
            self.assertIn("not duplicated in this session root", prompt)
            self.assertIn("./workspace_context.md", prompt)
            self.assertIn("Treat `Current request` as the source of truth.", prompt)
            self.assertIn("sources/<request_id>.request.md", prompt)

    def test_role_runtime_prompt_mentions_generated_workspace_path_when_using_default_layout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            envelope = MessageEnvelope(
                request_id="new-request",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="scope",
            )
            request_record = {
                "request_id": "new-request",
                "scope": "scope",
                "body": "",
                "artifacts": [],
            }

            prompt = runtime._build_prompt(envelope, request_record)

            self.assertIn("./workspace/teams_generated", prompt)

    def test_planner_role_runtime_prompt_treats_attachments_as_planning_inputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            envelope = MessageEnvelope(
                request_id="attachment-request",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="첨부 문서를 보고 계획 정리",
            )
            request_record = {
                "request_id": "attachment-request",
                "scope": "첨부 문서를 보고 계획 정리",
                "body": "",
                "artifacts": [
                    "./shared_workspace/sprints/260404-Sprint-12-00/attachments/att-1_spec.pdf",
                ],
            }

            prompt = runtime._build_prompt(envelope, request_record)

            self.assertIn("Treat `Current request.artifacts` as planning reference inputs.", prompt)
            self.assertIn("kickoff.md", prompt)
            self.assertIn("shared_workspace/sprints/.../attachments/...", prompt)
            self.assertIn("./shared_workspace/sprint_history/index.md", prompt)
            self.assertIn("smallest relevant prior sprint history file(s)", prompt)
            self.assertIn("Use prior sprint history as comparative evidence only", prompt)
            self.assertIn("not directly readable in the current session", prompt)
            self.assertIn("Do not default sprint planning or backlog decomposition to three items.", prompt)
            self.assertIn("Current request.params._teams_kind == \"blocked_backlog_review\"", prompt)
            self.assertIn("Only reopened `pending` items may be promoted into the sprint", prompt)

    def test_planner_prompt_classifies_message_changes_and_splits_mixed_cases(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            envelope = MessageEnvelope(
                request_id="message-routing-request",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="디스코드 메시지 변경 분기",
            )
            request_record = {
                "request_id": "message-routing-request",
                "scope": "디스코드 메시지 변경 분기",
                "body": "",
                "artifacts": [],
            }

            prompt = runtime._build_prompt(envelope, request_record)

            self.assertIn("`renderer-only` applies only when semantic meaning, copy hierarchy, user decision path, and CTA are already fixed", prompt)
            self.assertIn("reading order, omission tolerance, title/summary/body/action priority, or CTA wording/tone", prompt)
            self.assertIn("`readability-only` as a non-advisory bucket only when the same reading order still holds", prompt)
            self.assertIn("`relay` when immediate status/warning/action priority changes", prompt)
            self.assertIn("`handoff` when the next role's first-read context changes", prompt)
            self.assertIn("`summary` when long-term keep-vs-omit rules change", prompt)
            self.assertIn("Split it into `technical slice` and `designer advisory slice`", prompt)
            self.assertIn("before/after message example or intended output hierarchy", prompt)
            self.assertIn("`표시 오류` or `사용자 판단 혼선`", prompt)

    def test_developer_prompt_preserves_message_contract_without_redesign(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="developer",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            envelope = MessageEnvelope(
                request_id="developer-message-contract",
                sender="orchestrator",
                target="developer",
                intent="implement",
                urgency="normal",
                scope="메시지 contract 구현",
            )
            request_record = {
                "request_id": "developer-message-contract",
                "scope": "메시지 contract 구현",
                "body": "",
                "artifacts": [],
            }

            prompt = runtime._build_prompt(envelope, request_record)

            self.assertIn("`same meaning / same priority / same CTA` preservation work", prompt)
            self.assertIn("Do not redesign information order, omission policy, or CTA wording", prompt)
            self.assertIn("do not silently make the UX decision in code", prompt)

    def test_orchestrator_role_runtime_prompt_routes_sprint_work_to_skill_and_cli(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="orchestrator",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            envelope = MessageEnvelope(
                request_id="sprint-request",
                sender="user",
                target="orchestrator",
                intent="route",
                urgency="normal",
                scope="스프린트 현황 파악",
                body="스프린트 현황 파악",
            )
            request_record = {
                "request_id": "sprint-request",
                "scope": "스프린트 현황 파악",
                "body": "스프린트 현황 파악",
                "artifacts": [],
            }

            prompt = runtime._build_prompt(envelope, request_record)

            self.assertIn("./.agents/skills/sprint_orchestration/SKILL.md", prompt)
            self.assertIn("python -m teams_runtime sprint start|stop|restart|status --workspace-root ./workspace", prompt)
            self.assertIn("compatibility-only fallback", prompt)
            self.assertIn("./.agents/skills/status_reporting/SKILL.md", prompt)

    def test_backlog_sourcer_prompt_prefers_active_sprint_milestone_relevance(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = BacklogSourcingRuntime(
                paths=paths,
                sprint_id="configured-scope",
                runtime_config=RoleRuntimeConfig(),
            )

            prompt = runtime._build_prompt(
                findings=[{"title": "alert routing", "summary": "routing issue", "scope": "alert routing"}],
                scheduler_state={"active_sprint_id": "260331-Sprint-14:00"},
                active_sprint={
                    "sprint_id": "260331-Sprint-14:00",
                    "milestone_title": "workflow initial",
                    "status": "running",
                    "phase": "ongoing",
                },
                backlog_counts={"pending": 1, "selected": 0, "blocked": 0, "done": 0, "total": 1},
                existing_backlog=[],
            )

            self.assertIn("focus only on backlog items that clearly advance that milestone", prompt)
            self.assertIn("prefer returning no backlog items over returning unrelated work", prompt)
            self.assertIn("set `milestone_title` to that active sprint milestone on every returned item", prompt)

    def test_codex_runner_resume_ignores_stale_output_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            stale_output = workspace / ".teams_runtime_codex_output.txt"
            stale_output.write_text("stale-output", encoding="utf-8")
            runner = CodexRunner(RoleRuntimeConfig())

            with patch(
                "teams_runtime.runtime.codex_runner.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout='{"summary":"fresh-output"}', stderr=""),
            ):
                output, resolved_session_id = runner.run(workspace, "prompt", "session-123")

            self.assertEqual(output, '{"summary":"fresh-output"}')
            self.assertEqual(resolved_session_id, "session-123")
            self.assertFalse(stale_output.exists())

    def test_extract_json_object_recovers_tail_dict_from_malformed_fenced_json(self):
        malformed_output = """```json
{status: blocked}
``` 
diff --git a/file.txt b/file.txt
index 111..222 100644
--- a/file.txt
+++ b/file.txt
@@ -1,1 +1,1 @@
-old
+new
{"request_id":"request-20260411-123","role":"developer","status":"completed","summary":"mixed output recovered"}
"""

        payload = extract_json_object(malformed_output)
        self.assertEqual(payload["request_id"], "request-20260411-123")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["summary"], "mixed output recovered")

    def test_codex_runner_preserves_valid_json_output_on_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            output_path = workspace / ".teams_runtime_codex_output.txt"
            runner = CodexRunner(RoleRuntimeConfig())

            def _fake_run(*args, **kwargs):
                output_path.write_text(
                    json.dumps(
                        {
                            "status": "blocked",
                            "summary": "blocked backlog 검토 결과를 유지합니다.",
                            "artifacts": ["./shared_workspace/backlog.md"],
                            "error": "",
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="ERROR: Error running remote compact task: usage limit",
                )

            with patch("teams_runtime.runtime.codex_runner.subprocess.run", side_effect=_fake_run):
                output, resolved_session_id = runner.run(workspace, "prompt", "session-123")

            self.assertIn('"status": "blocked"', output)
            self.assertEqual(resolved_session_id, "session-123")

    def test_codex_runner_nonzero_exit_with_malformed_fenced_output_does_not_raise_jsondecodeerror(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            output_path = workspace / ".teams_runtime_codex_output.txt"
            runner = CodexRunner(RoleRuntimeConfig())

            mixed_output = """```json
{"status": "blocked",
```
context compacted
{"request_id":"request-20260411-456","role":"developer","status":"completed","summary":"non-zero mixed output recovered"}
"""

            def _fake_run(*args, **kwargs):
                output_path.write_text(mixed_output, encoding="utf-8")
                return SimpleNamespace(returncode=1, stdout="", stderr="usage limit reached")

            with patch("teams_runtime.runtime.codex_runner.subprocess.run", side_effect=_fake_run):
                output, resolved_session_id = runner.run(workspace, "prompt", "session-123")

            payload = extract_json_object(output)
            self.assertEqual(payload["request_id"], "request-20260411-456")
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["summary"], "non-zero mixed output recovered")
            self.assertEqual(resolved_session_id, "session-123")

    def test_codex_runner_raises_on_nonzero_exit_without_valid_json_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            runner = CodexRunner(RoleRuntimeConfig())

            with patch(
                "teams_runtime.runtime.codex_runner.subprocess.run",
                return_value=SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="ERROR: Error running remote compact task: usage limit",
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "usage limit"):
                    runner.run(workspace, "prompt", "session-123")

    def test_gemini_runner_preserves_valid_json_output_on_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = Path(tmpdir)
            runner = CodexRunner(RoleRuntimeConfig(model="gemini-1.5-pro"))

            mock_stdout = json.dumps(
                {
                    "response": json.dumps(
                        {
                            "status": "blocked",
                            "summary": "blocked backlog 검토 결과를 유지합니다.",
                            "error": "",
                        },
                        ensure_ascii=False,
                    ),
                    "session_id": "session-gemini-123",
                },
                ensure_ascii=False,
            )

            with patch(
                "teams_runtime.runtime.codex_runner.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout=mock_stdout, stderr=""),
            ):
                output, resolved_session_id = runner.run(workspace, "prompt", None)

            self.assertIn('"status": "blocked"', output)
            self.assertEqual(resolved_session_id, "session-gemini-123")

    def test_codex_runner_builds_gemini_command_when_gemini_model_specified(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            manager = RoleSessionManager(paths, "planner", "sprint-a")
            state = manager.ensure_session()
            
            runtime_config = RoleRuntimeConfig(model="gemini-1.5-pro")
            runner = CodexRunner(runtime_config)

            mock_stdout = json.dumps({
                "response": '{"summary":"ok"}',
                "session_id": "session-resolved-123"
            })

            with patch(
                "teams_runtime.runtime.codex_runner.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout=mock_stdout, stderr=""),
            ) as mock_run:
                output, resolved_session_id = runner.run(Path(state.workspace_path), "my prompt", "session-123", bypass_sandbox=True)

            command = mock_run.call_args.args[0]
            self.assertEqual(command[0], "gemini")
            self.assertIn("--resume", command)
            self.assertIn("session-123", command)
            self.assertIn("--model", command)
            self.assertIn("gemini-1.5-pro", command)
            self.assertIn("--yolo", command)
            self.assertIn("--output-format", command)
            self.assertIn("json", command)
            self.assertIn("--prompt", command)
            self.assertIn("my prompt", command)
            
            self.assertEqual(output, '{"summary":"ok"}')
            self.assertEqual(resolved_session_id, "session-resolved-123")
            
            self.assertNotIn("--skip-git-repo-check", command)
            self.assertNotIn("-c", command)
            
            env_passed = mock_run.call_args.kwargs.get("env", {})
            self.assertIn("GEMINI_SYSTEM_MD", env_passed)
            self.assertTrue(str(env_passed["GEMINI_SYSTEM_MD"]).endswith("GEMINI.md"))

    def test_codex_runner_handles_gemini_error_and_camelcase_session_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            manager = RoleSessionManager(paths, "planner", "sprint-a")
            state = manager.ensure_session()
            
            runtime_config = RoleRuntimeConfig(model="gemini-1.5-pro")
            runner = CodexRunner(runtime_config)

            # Test camelCase sessionId
            mock_stdout_camel = json.dumps({
                "response": '{"summary":"ok"}',
                "sessionId": "session-camel-123"
            })
            with patch(
                "teams_runtime.runtime.codex_runner.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout=mock_stdout_camel, stderr=""),
            ):
                output, resolved_session_id = runner.run(Path(state.workspace_path), "prompt", None)
                self.assertEqual(resolved_session_id, "session-camel-123")

            # Test Gemini error in JSON
            mock_stdout_error = json.dumps({
                "response": "",
                "error": {"message": "Resource exhausted"}
            })
            with patch(
                "teams_runtime.runtime.codex_runner.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout=mock_stdout_error, stderr=""),
            ):
                with self.assertRaisesRegex(RuntimeError, "Resource exhausted"):
                    runner.run(Path(state.workspace_path), "prompt", None)

    def test_codex_runner_adds_runtime_workspace_targets_as_writable_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            manager = RoleSessionManager(paths, "planner", "sprint-a")
            state = manager.ensure_session()
            runner = CodexRunner(RoleRuntimeConfig())

            with patch(
                "teams_runtime.runtime.codex_runner.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout='{"summary":"ok"}', stderr=""),
            ) as mock_run:
                runner.run(Path(state.workspace_path), "prompt", None)

            command = mock_run.call_args.args[0]
            kwargs = mock_run.call_args.kwargs
            self.assertIn("--add-dir", command)
            self.assertIn(str(Path(tmpdir).resolve()), command)
            self.assertIn(str((workspace_root / "shared_workspace").resolve()), command)
            self.assertIn(str((workspace_root / ".teams_runtime").resolve()), command)
            self.assertIn("--full-auto", command)
            self.assertEqual(command[:3], ["codex", "exec", "-"])
            self.assertNotIn("prompt", command)
            self.assertEqual(kwargs["input"], "prompt")

    def test_codex_runner_resume_does_not_pass_add_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            manager = RoleSessionManager(paths, "planner", "sprint-a")
            state = manager.ensure_session()
            runner = CodexRunner(RoleRuntimeConfig())

            with patch(
                "teams_runtime.runtime.codex_runner.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout='{"summary":"ok"}', stderr=""),
            ) as mock_run:
                runner.run(Path(state.workspace_path), "prompt", "session-123")

            command = mock_run.call_args.args[0]
            kwargs = mock_run.call_args.kwargs
            self.assertEqual(command[:3], ["codex", "exec", "resume"])
            self.assertIn("--model", command)
            self.assertIn("--skip-git-repo-check", command)
            self.assertIn("--full-auto", command)
            self.assertNotIn("--add-dir", command)
            self.assertEqual(command[-2:], ["session-123", "-"])
            self.assertEqual(kwargs["input"], "prompt")

    def test_codex_runner_large_prompt_stays_out_of_argv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            manager = RoleSessionManager(paths, "planner", "sprint-a")
            state = manager.ensure_session()
            runner = CodexRunner(RoleRuntimeConfig())
            large_prompt = "planner-context-" * 4096

            with patch(
                "teams_runtime.runtime.codex_runner.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout='{"summary":"ok"}', stderr=""),
            ) as mock_run:
                runner.run(Path(state.workspace_path), large_prompt, None)

            command = mock_run.call_args.args[0]
            kwargs = mock_run.call_args.kwargs
            self.assertNotIn(large_prompt, command)
            self.assertEqual(kwargs["input"], large_prompt)

    def test_role_runtime_version_controller_retries_with_bypass_on_sandbox_denial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="version_controller",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
                agent_root=paths.internal_agent_root("version_controller"),
            )

            class _FakeRunner:
                def __init__(self):
                    self.calls: list[dict[str, object]] = []

                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    self.calls.append(
                        {
                            "workspace": str(workspace),
                            "session_id": session_id,
                            "bypass_sandbox": bypass_sandbox,
                        }
                    )
                    if not bypass_sandbox:
                        return (
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "summary": "git helper 실행 중 index.lock 권한 오류로 task 커밋이 실패했습니다.",
                                    "error": "fatal: Unable to create '/repo/.git/index.lock': Operation not permitted",
                                    "commit_status": "failed",
                                },
                                ensure_ascii=False,
                            ),
                            "session-1",
                        )
                    return (
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "task 변경을 커밋했습니다.",
                                "error": "",
                                "commit_status": "committed",
                                "commit_sha": "abc123",
                                "commit_message": "message",
                                "commit_paths": ["teams_runtime/runtime/codex.py"],
                                "change_detected": True,
                            },
                            ensure_ascii=False,
                        ),
                        "session-1",
                    )

            fake_runner = _FakeRunner()
            runtime.codex_runner = fake_runner
            envelope = MessageEnvelope(
                request_id="request-1",
                sender="orchestrator",
                target="version_controller",
                intent="route",
                urgency="normal",
                scope="commit task changes",
            )
            request_record = {
                "request_id": "request-1",
                "scope": "commit task changes",
                "body": "",
                "artifacts": [],
            }

            payload = runtime.run_task(envelope, request_record)

            self.assertEqual(len(fake_runner.calls), 1)
            self.assertTrue(fake_runner.calls[0]["bypass_sandbox"])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["commit_status"], "committed")
            self.assertEqual(payload["commit_sha"], "abc123")

    def test_role_runtime_planner_retries_with_bypass_on_backlog_persistence_denial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            scaffold_workspace(tmpdir)
            paths = RuntimePaths.from_root(tmpdir)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )

            class _FakeRunner:
                def __init__(self):
                    self.calls: list[dict[str, object]] = []

                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    self.calls.append(
                        {
                            "workspace": str(workspace),
                            "session_id": session_id,
                            "bypass_sandbox": bypass_sandbox,
                        }
                    )
                    if not bypass_sandbox:
                        return (
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "summary": "스프린트 실행 계획은 정리했지만 backlog의 스프린트 계획 필드 직접 반영은 샌드박스 쓰기 제한으로 완료하지 못했습니다.",
                                    "error": "planner backlog persistence blocked: PermissionError writing /repo/teams_generated/.teams_runtime/backlog/backlog-20260330-135e2a64.json",
                                },
                                ensure_ascii=False,
                            ),
                            "session-2",
                        )
                    return (
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "backlog와 스프린트 계획 필드를 직접 반영했습니다.",
                                "error": "",
                                "proposals": {
                                    "backlog_write": {
                                        "status": "updated",
                                        "backlog_id": "backlog-20260330-135e2a64",
                                    }
                                },
                            },
                            ensure_ascii=False,
                        ),
                        "session-2",
                    )

            fake_runner = _FakeRunner()
            runtime.codex_runner = fake_runner
            envelope = MessageEnvelope(
                request_id="request-1",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="plan sprint backlog updates",
            )
            request_record = {
                "request_id": "request-1",
                "scope": "plan sprint backlog updates",
                "body": "",
                "artifacts": [],
            }

            payload = runtime.run_task(envelope, request_record)

            self.assertEqual(len(fake_runner.calls), 1)
            self.assertTrue(fake_runner.calls[0]["bypass_sandbox"])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(
                payload["proposals"]["backlog_write"]["backlog_id"],
                "backlog-20260330-135e2a64",
            )
            self.assertEqual(
                payload["proposals"]["backlog_writes"][0]["backlog_id"],
                "backlog-20260330-135e2a64",
            )

    def test_role_runtime_planner_defaults_to_bypass_for_initial_phase_persistence_requests(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            state = runtime.session_manager.ensure_session()
            runtime.session_manager.finalize_session_id(state, "session-planner-1")

            class _FakeRunner:
                def __init__(self):
                    self.calls: list[dict[str, object]] = []

                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    self.calls.append(
                        {
                            "workspace": str(workspace),
                            "session_id": session_id,
                            "bypass_sandbox": bypass_sandbox,
                        }
                    )
                    return (
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "artifact_sync 결과를 shared_workspace에 직접 반영했습니다.",
                                "error": "",
                            },
                            ensure_ascii=False,
                        ),
                        "session-planner-2",
                    )

            fake_runner = _FakeRunner()
            runtime.codex_runner = fake_runner
            envelope = MessageEnvelope(
                request_id="request-1",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="sync sprint planning artifacts",
            )
            request_record = {
                "request_id": "request-1",
                "scope": "sync sprint planning artifacts",
                "body": "",
                "artifacts": [],
                "sprint_id": "sprint-a",
                "params": {
                    "sprint_id": "sprint-a",
                    "sprint_phase": "initial",
                    "initial_phase_step": "artifact_sync",
                },
            }

            payload = runtime.run_task(envelope, request_record)

            self.assertEqual(len(fake_runner.calls), 1)
            self.assertEqual(fake_runner.calls[0]["session_id"], "session-planner-1")
            self.assertTrue(fake_runner.calls[0]["bypass_sandbox"])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["summary"], "artifact_sync 결과를 shared_workspace에 직접 반영했습니다.")

    def test_role_runtime_developer_defaults_to_bypass(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="developer",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            state = runtime.session_manager.ensure_session()
            runtime.session_manager.finalize_session_id(state, "session-developer-1")

            class _FakeRunner:
                def __init__(self):
                    self.calls: list[dict[str, object]] = []

                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    self.calls.append(
                        {
                            "workspace": str(workspace),
                            "session_id": session_id,
                            "bypass_sandbox": bypass_sandbox,
                        }
                    )
                    return (
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "developer 구현을 반영했습니다.",
                                "error": "",
                            },
                            ensure_ascii=False,
                        ),
                        "session-developer-1",
                    )

            fake_runner = _FakeRunner()
            runtime.codex_runner = fake_runner
            envelope = MessageEnvelope(
                request_id="request-1",
                sender="orchestrator",
                target="developer",
                intent="implement",
                urgency="normal",
                scope="implement requested change",
            )
            request_record = {
                "request_id": "request-1",
                "scope": "implement requested change",
                "body": "",
                "artifacts": [],
            }

            payload = runtime.run_task(envelope, request_record)

            self.assertEqual(len(fake_runner.calls), 1)
            self.assertEqual(fake_runner.calls[0]["session_id"], "session-developer-1")
            self.assertTrue(fake_runner.calls[0]["bypass_sandbox"])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["summary"], "developer 구현을 반영했습니다.")

    def test_role_runtime_planner_non_persistence_request_stays_sandboxed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            state = runtime.session_manager.ensure_session()
            runtime.session_manager.finalize_session_id(state, "session-planner-1")

            class _FakeRunner:
                def __init__(self):
                    self.calls: list[dict[str, object]] = []

                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    self.calls.append(
                        {
                            "workspace": str(workspace),
                            "session_id": session_id,
                            "bypass_sandbox": bypass_sandbox,
                        }
                    )
                    return (
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "planning 문서를 검토했습니다.",
                                "error": "",
                            },
                            ensure_ascii=False,
                        ),
                        "session-planner-1",
                    )

            fake_runner = _FakeRunner()
            runtime.codex_runner = fake_runner
            envelope = MessageEnvelope(
                request_id="request-1",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="review planning notes",
            )
            request_record = {
                "request_id": "request-1",
                "scope": "review planning notes",
                "body": "",
                "artifacts": [],
            }

            payload = runtime.run_task(envelope, request_record)

            self.assertEqual(len(fake_runner.calls), 1)
            self.assertEqual(fake_runner.calls[0]["session_id"], "session-planner-1")
            self.assertTrue(fake_runner.calls[0]["bypass_sandbox"])
            self.assertEqual(payload["status"], "completed")

    def test_role_runtime_planner_retries_with_bypass_on_read_only_shared_workspace_denial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            state = runtime.session_manager.ensure_session()
            runtime.session_manager.finalize_session_id(state, "session-planner-1")

            class _FakeRunner:
                def __init__(self):
                    self.calls: list[dict[str, object]] = []

                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    self.calls.append(
                        {
                            "workspace": str(workspace),
                            "session_id": session_id,
                            "bypass_sandbox": bypass_sandbox,
                        }
                    )
                    if not bypass_sandbox:
                        return (
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "summary": "초기 backlog와 sprint 문서를 정리했지만 현재 세션에서 ./workspace가 읽기 전용이라 직접 반영하지 못했습니다.",
                                    "error": "./workspace 경로가 현재 세션에서 쓰기 불가하여 shared_workspace/sprints 와 backlog 저장소를 갱신하지 못했습니다.",
                                },
                                ensure_ascii=False,
                            ),
                            "session-planner-1",
                        )
                    return (
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "shared_workspace와 backlog 상태를 직접 반영했습니다.",
                                "error": "",
                            },
                            ensure_ascii=False,
                        ),
                        "session-planner-2",
                    )

            fake_runner = _FakeRunner()
            runtime.codex_runner = fake_runner
            envelope = MessageEnvelope(
                request_id="request-1",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="plan sprint backlog updates",
            )
            request_record = {
                "request_id": "request-1",
                "scope": "plan sprint backlog updates",
                "body": "",
                "artifacts": [],
            }

            payload = runtime.run_task(envelope, request_record)

            self.assertEqual(len(fake_runner.calls), 1)
            self.assertEqual(fake_runner.calls[0]["session_id"], "session-planner-1")
            self.assertTrue(fake_runner.calls[0]["bypass_sandbox"])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["summary"], "shared_workspace와 backlog 상태를 직접 반영했습니다.")

    def test_role_runtime_planner_retries_with_bypass_on_artifact_sync_shared_workspace_denial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            state = runtime.session_manager.ensure_session()
            runtime.session_manager.finalize_session_id(state, "session-planner-1")

            class _FakeRunner:
                def __init__(self):
                    self.calls: list[dict[str, object]] = []

                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    self.calls.append(
                        {
                            "workspace": str(workspace),
                            "session_id": session_id,
                            "bypass_sandbox": bypass_sandbox,
                        }
                    )
                    if not bypass_sandbox:
                        return (
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "summary": "artifact_sync 대상인 shared_workspace 문서가 현재 세션에서 쓰기 불가라서 plan/spec/iteration 동기화를 적용하지 못했습니다.",
                                    "error": "",
                                },
                                ensure_ascii=False,
                            ),
                            "session-planner-1",
                        )
                    return (
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "artifact_sync 결과를 직접 반영했습니다.",
                                "error": "",
                            },
                            ensure_ascii=False,
                        ),
                        "session-planner-2",
                    )

            fake_runner = _FakeRunner()
            runtime.codex_runner = fake_runner
            envelope = MessageEnvelope(
                request_id="request-1",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="sync planning artifacts",
            )
            request_record = {
                "request_id": "request-1",
                "scope": "sync planning artifacts",
                "body": "",
                "artifacts": [],
            }

            payload = runtime.run_task(envelope, request_record)

            self.assertEqual(len(fake_runner.calls), 1)
            self.assertEqual(fake_runner.calls[0]["session_id"], "session-planner-1")
            self.assertTrue(fake_runner.calls[0]["bypass_sandbox"])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["summary"], "artifact_sync 결과를 직접 반영했습니다.")

    def test_role_runtime_planner_retries_with_bypass_on_writable_roots_symlink_denial(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="planner",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            state = runtime.session_manager.ensure_session()
            runtime.session_manager.finalize_session_id(state, "session-planner-1")

            class _FakeRunner:
                def __init__(self):
                    self.calls: list[dict[str, object]] = []

                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    self.calls.append(
                        {
                            "workspace": str(workspace),
                            "session_id": session_id,
                            "bypass_sandbox": bypass_sandbox,
                        }
                    )
                    if not bypass_sandbox:
                        return (
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "summary": "요청된 sprint 문서와 backlog/todo 구조는 검토·분해했지만, 세션에서 `./shared_workspace`와 `./.teams_runtime`가 writable하지 않아 planner-owned 반영을 완료하지 못했습니다.",
                                    "error": "`./shared_workspace`와 `./.teams_runtime`가 세션 writable roots 밖의 symlink target이라 planner-owned sprint/backlog persistence를 완료할 수 없습니다.",
                                },
                                ensure_ascii=False,
                            ),
                            "session-planner-1",
                        )
                    return (
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "shared_workspace와 backlog 상태를 직접 반영했습니다.",
                                "error": "",
                            },
                            ensure_ascii=False,
                        ),
                        "session-planner-2",
                    )

            fake_runner = _FakeRunner()
            runtime.codex_runner = fake_runner
            envelope = MessageEnvelope(
                request_id="request-1",
                sender="orchestrator",
                target="planner",
                intent="plan",
                urgency="normal",
                scope="plan sprint backlog updates",
            )
            request_record = {
                "request_id": "request-1",
                "scope": "plan sprint backlog updates",
                "body": "",
                "artifacts": [],
            }

            payload = runtime.run_task(envelope, request_record)

            self.assertEqual(len(fake_runner.calls), 1)
            self.assertEqual(fake_runner.calls[0]["session_id"], "session-planner-1")
            self.assertTrue(fake_runner.calls[0]["bypass_sandbox"])
            self.assertEqual(payload["status"], "completed")

    def test_role_runtime_orchestrator_retries_with_bypass_using_fresh_session_for_sprint_lifecycle(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="orchestrator",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            state = runtime.session_manager.ensure_session()
            runtime.session_manager.finalize_session_id(state, "session-orchestrator-1")

            class _FakeRunner:
                def __init__(self):
                    self.calls: list[dict[str, object]] = []

                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    self.calls.append(
                        {
                            "workspace": str(workspace),
                            "session_id": session_id,
                            "bypass_sandbox": bypass_sandbox,
                        }
                    )
                    if not bypass_sandbox:
                        return (
                            json.dumps(
                                {
                                    "status": "blocked",
                                    "summary": "스프린트 시작은 해석했지만 sprint lifecycle CLI가 sprint_scheduler.json에 쓰지 못했습니다.",
                                    "error": "PermissionError: [Errno 1] Operation not permitted: '/repo/teams_generated/.teams_runtime/sprint_scheduler.json'",
                                    "insights": [
                                        "sprint lifecycle mutation failed inside the current sandboxed session"
                                    ],
                                },
                                ensure_ascii=False,
                            ),
                            "session-orchestrator-1",
                        )
                    return (
                        json.dumps(
                            {
                                "status": "completed",
                                "summary": "스프린트를 시작했습니다.",
                                "error": "",
                            },
                            ensure_ascii=False,
                        ),
                        "session-orchestrator-2",
                    )

            fake_runner = _FakeRunner()
            runtime.codex_runner = fake_runner
            envelope = MessageEnvelope(
                request_id="request-1",
                sender="user",
                target="orchestrator",
                intent="route",
                urgency="normal",
                scope="스프린트 시작해. milestone: KIS 스캘핑 고도화",
                body="스프린트 시작해. milestone: KIS 스캘핑 고도화",
            )
            request_record = {
                "request_id": "request-1",
                "scope": envelope.scope,
                "body": envelope.body,
                "artifacts": [],
            }

            payload = runtime.run_task(envelope, request_record)

            self.assertEqual(len(fake_runner.calls), 1)
            self.assertEqual(fake_runner.calls[0]["session_id"], "session-orchestrator-1")
            self.assertTrue(fake_runner.calls[0]["bypass_sandbox"])
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["summary"], "스프린트를 시작했습니다.")

    def test_role_runtime_returns_structured_failed_payload_when_codex_runner_raises_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace_root = Path(tmpdir) / "teams_generated"
            scaffold_workspace(workspace_root)
            paths = RuntimePaths.from_root(workspace_root)
            runtime = RoleAgentRuntime(
                paths=paths,
                role="developer",
                sprint_id="sprint-a",
                runtime_config=RoleRuntimeConfig(),
            )
            state = runtime.session_manager.ensure_session()
            runtime.session_manager.finalize_session_id(state, "session-developer-1")

            class _FailingRunner:
                def __init__(self):
                    self.calls: list[dict[str, object]] = []

                def run(self, workspace, prompt, session_id, *, bypass_sandbox=False):
                    self.calls.append(
                        {
                            "workspace": str(workspace),
                            "session_id": session_id,
                            "bypass_sandbox": bypass_sandbox,
                        }
                    )
                    raise RuntimeError("role execution failed due malformed mixed output")

            fake_runner = _FailingRunner()
            runtime.codex_runner = fake_runner
            envelope = MessageEnvelope(
                request_id="request-1",
                sender="orchestrator",
                target="developer",
                intent="implement",
                urgency="normal",
                scope="implement defensive parsing",
            )
            request_record = {
                "request_id": "request-1",
                "scope": "implement defensive parsing",
                "body": "",
                "artifacts": [],
            }

            payload = runtime.run_task(envelope, request_record)

            self.assertEqual(len(fake_runner.calls), 1)
            self.assertEqual(fake_runner.calls[0]["session_id"], "session-developer-1")
            self.assertTrue(fake_runner.calls[0]["bypass_sandbox"])
            self.assertEqual(payload["status"], "failed")
            self.assertIn("malformed mixed output", payload["error"])
            self.assertEqual(payload["summary"], "")
            self.assertEqual(payload["request_id"], "request-1")

    def test_normalize_role_payload_coerces_common_shape_issues(self):
        payload = normalize_role_payload(
            {
                "status": "completed",
                "summary": "ok",
                "insights": "single insight",
                "artifacts": "workspace/file.txt",
                "proposals": {
                    "routing": {
                        "recommended_next_role": "developer",
                    }
                },
                "next_role": "invalid-role",
                "approval_needed": "yes",
            }
        )

        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(payload["insights"], ["single insight"])
        self.assertEqual(payload["artifacts"], ["workspace/file.txt"])
        self.assertEqual(payload["proposals"], {"routing": {}})
        self.assertEqual(payload["next_role"], "")
        self.assertNotIn("approval_needed", payload)
        self.assertIn("coerced insights from string", payload["validation_notes"])
        self.assertIn("converted approval_needed to blocked", payload["error"])

    def test_normalize_role_payload_rejects_invalid_status(self):
        payload = normalize_role_payload(
            {
                "status": "done",
                "summary": "ok",
            }
        )

        self.assertEqual(payload["status"], "failed")
        self.assertIn("invalid status=done", payload["error"])

    def test_normalize_role_payload_normalizes_planner_backlog_aliases_and_receipts(self):
        payload = normalize_role_payload(
            {
                "role": "planner",
                "status": "completed",
                "summary": "ok",
                "proposals": {
                    "planned_backlog_updates": [
                        {
                            "title": "manual sprint todo finalization",
                            "scope": "manual sprint todo finalization",
                            "summary": "Persist the finalized sprint-ready backlog selection.",
                            "kind": "feature",
                            "planned_in_sprint_id": "260410-Sprint-16:32",
                        }
                    ],
                    "backlog_write": {
                        "status": "updated",
                        "backlog_id": "backlog-20260330-135e2a64",
                        "path": "./.teams_runtime/backlog/backlog-20260330-135e2a64.json",
                    },
                },
            }
        )

        self.assertEqual(len(payload["proposals"]["backlog_items"]), 1)
        self.assertEqual(
            payload["proposals"]["backlog_items"][0]["planned_in_sprint_id"],
            "260410-Sprint-16:32",
        )
        self.assertEqual(payload["proposals"]["backlog_write"]["backlog_id"], "backlog-20260330-135e2a64")
        self.assertEqual(len(payload["proposals"]["backlog_writes"]), 1)
        self.assertEqual(
            payload["proposals"]["backlog_writes"][0]["artifact_path"],
            "./.teams_runtime/backlog/backlog-20260330-135e2a64.json",
        )
        self.assertIn("normalized planner planned_backlog_updates to backlog_items", payload["validation_notes"])
        self.assertIn("normalized planner backlog_write to backlog_writes", payload["validation_notes"])

    def test_read_process_summary_returns_na_when_ps_unavailable(self):
        with patch("teams_runtime.core.reports.subprocess.run", side_effect=PermissionError("ps blocked")):
            summary = read_process_summary(123)

        self.assertIn("N/A", summary)
        self.assertIn("ps blocked", summary)
